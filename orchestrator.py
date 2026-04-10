import asyncio
import anyio
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

from cli import call_agent, AgentError, AgentTimeout, InfraError, fmt_tokens, fmt_elapsed
from config import (
    CONFIG, CONTRACT_AGREED,
    TOOLS_READ_WRITE, TOOLS_FULL, TOOLS_EVALUATOR,
    get_harness_root, resolve_mode, resolve_agent_model,
)
from sprint import Sprint, parse_sprints
from state import HarnessState
from server import DevServer
from verifier import (
    classify_criteria, run_verification, backup_db, restore_db,
    VerificationResult,
)


def _check_verdict_pass(text: str) -> bool:
    """Check if overall verdict is PASS.

    In parallel eval, multiple Verdict lines may exist. ANY FAIL = overall FAIL.
    Only returns True if at least one PASS exists and zero FAILs exist.
    """
    has_pass = bool(re.search(r"^#{0,3}\s*Verdict:\s*PASS\s*$", text, re.MULTILINE))
    has_fail = bool(re.search(r"^#{0,3}\s*Verdict:\s*FAIL\s*$", text, re.MULTILINE))
    return has_pass and not has_fail


def _check_contract_agreed(text: str) -> bool:
    """Match CONTRACT_AGREED only at the start of a line (not inside 'NOT AGREED')."""
    return bool(re.search(rf"^{CONTRACT_AGREED}\b", text, re.MULTILINE))


_FRONTEND_CRITERIA = ["Design Quality", "Originality", "Craft", "UI Functionality"]
_BACKEND_CRITERIA = ["Product Depth", "Functionality", "Code Quality"]


def _parse_failed_parts(text: str) -> tuple[bool, bool]:
    """Parse evaluation text to determine which parts failed.

    Returns (frontend_passed, backend_passed).
    """
    def _part_passed(criteria: list[str]) -> bool:
        for c in criteria:
            match = re.search(rf"\|\s*{c}\s*\|\s*(PASS|FAIL)\s*\|", text)
            if match and match.group(1) == "FAIL":
                return False
        return True

    return _part_passed(_FRONTEND_CRITERIA), _part_passed(_BACKEND_CRITERIA)


_CRITERIA_RE = re.compile(r"^\s*- \[[x ]\] ", re.MULTILINE)


def _count_criteria(text: str) -> int:
    """Count checkbox-format criteria lines (- [x] or - [ ])."""
    return len(_CRITERIA_RE.findall(text))


def _parse_eval_score(text: str) -> tuple[int, int]:
    """Parse pass/fail counts from evaluation text.

    Returns (passed_count, total_count). Returns (0, 0) if unparseable.
    """
    passed = len(re.findall(r"^\s*- \[x\] ", text, re.MULTILINE))
    failed = len(re.findall(r"^\s*- \[ \] ", text, re.MULTILINE))
    total = passed + failed
    return passed, total


@dataclass
class CriteriaSection:
    """A ### section of testable criteria."""
    header: str
    raw_text: str
    criteria_count: int


def parse_criteria_sections(criteria_text: str) -> list[CriteriaSection]:
    """Split criteria text into sections by ### headers."""
    sections = []
    current_header = ""
    current_lines: list[str] = []

    for line in criteria_text.split("\n"):
        if line.startswith("### "):
            # Save previous section
            if current_header:
                raw = "\n".join(current_lines)
                count = len(_CRITERIA_RE.findall(raw))
                if count > 0:
                    sections.append(CriteriaSection(
                        header=current_header, raw_text=raw, criteria_count=count,
                    ))
            current_header = line[4:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    # Last section
    if current_header:
        raw = "\n".join(current_lines)
        count = len(_CRITERIA_RE.findall(raw))
        if count > 0:
            sections.append(CriteriaSection(
                header=current_header, raw_text=raw, criteria_count=count,
            ))

    return sections


# Cross-cutting sections that belong to the lead evaluator
_LEAD_SECTION_KEYWORDS = {
    "visual design", "responsive", "ui states", "error handling",
    "motion", "animation", "landing",
}


def _is_lead_section(header: str) -> bool:
    lower = header.lower()
    return any(kw in lower for kw in _LEAD_SECTION_KEYWORDS)


def assign_sections_to_evaluators(
    sections: list[CriteriaSection],
    target_per_evaluator: int = 25,
) -> list[list[CriteriaSection]]:
    """Distribute sections into N evaluator buckets.

    Bucket 0 is the lead evaluator (gets cross-cutting + quality assessment).
    Returns [[lead_sections], [bucket1], [bucket2], ...].
    Returns a single bucket if only 1 section or few criteria.
    """
    if len(sections) <= 1:
        return [sections]

    total = sum(s.criteria_count for s in sections)
    if total <= target_per_evaluator:
        return [sections]

    # Separate lead sections from feature sections
    lead_sections = [s for s in sections if _is_lead_section(s.header)]
    feature_sections = [s for s in sections if not _is_lead_section(s.header)]

    # Calculate how many feature evaluators we need
    feature_total = sum(s.criteria_count for s in feature_sections)
    n_feature = max(1, round(feature_total / target_per_evaluator))
    n_feature = min(n_feature, 2)  # cap at 2 feature evaluators (+ 1 lead = 3 total)

    # Distribute feature sections round-robin
    buckets: list[list[CriteriaSection]] = [[] for _ in range(n_feature)]
    bucket_counts = [0] * n_feature
    for section in feature_sections:
        # Put in the lightest bucket
        min_idx = bucket_counts.index(min(bucket_counts))
        buckets[min_idx].append(section)
        bucket_counts[min_idx] += section.criteria_count

    # Lead bucket is index 0 in the final result
    return [lead_sections] + buckets


_LEAD_ONLY_KEYWORDS = (
    "quality assessment", "evidence", "verdict",
    "full-stack verification", "regression analysis",
)


def _extract_checkboxes_by_section(text: str) -> list[tuple[str, str]]:
    """Extract (section_header, checkbox_line) pairs from evaluator text."""
    results = []
    current_section = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### ") or stripped.startswith("#### "):
            header = stripped.lstrip("#").strip()
            # Skip lead-only sections
            if any(kw in header.lower() for kw in _LEAD_ONLY_KEYWORDS):
                current_section = ""
                continue
            if "pass rate" in header.lower() or "feature testing" in header.lower():
                continue
            current_section = header
        elif current_section and re.match(r"^\s*- \[[x ]\] ", stripped):
            results.append((current_section, line))
    return results


def _extract_lead_sections(text: str) -> str:
    """Extract Quality Assessment, Evidence, Verdict from lead evaluator text."""
    lines = []
    capture = False
    for line in text.split("\n"):
        if re.match(r"^#{1,4}\s", line):
            header = line.lstrip("#").strip().lower()
            if any(kw in header for kw in _LEAD_ONLY_KEYWORDS):
                capture = True
            elif "required changes" in header:
                capture = False
                continue
            elif capture:
                capture = False
        if capture:
            lines.append(line)
    return "\n".join(lines)


def _extract_required_changes(text: str) -> list[str]:
    """Extract individual Required Changes items. Filters out 'None'."""
    items = []
    in_changes = False
    current = []
    for line in text.split("\n"):
        if re.match(r"^#{1,4}\s*Required Changes", line):
            in_changes = True
            continue
        if in_changes:
            if re.match(r"^#{1,4}\s", line) and "Required" not in line:
                if current:
                    items.append("\n".join(current))
                    current = []
                in_changes = False
                continue
            stripped = line.strip()
            if not stripped or stripped.lower() in ("none", "none.", "n/a"):
                continue
            if re.match(r"^\d+[\.\)]\s", stripped):
                if current:
                    items.append("\n".join(current))
                current = [stripped]
            elif current:
                current.append(line)
    if current:
        items.append("\n".join(current))
    return items


def merge_evaluations(
    eval_texts: list[str],
    lead_index: int = 0,
) -> str:
    """Merge N evaluator outputs into single clean evaluation.

    Structure:
    - Feature checkboxes: from ALL evaluators, grouped by section
    - Quality Assessment, Evidence, Verdict: from lead ONLY
    - Required Changes: from lead, renumbered
    """
    if len(eval_texts) == 1:
        return eval_texts[0]

    # 1. Extract checkboxes from all evaluators, grouped by section
    from collections import OrderedDict
    sections: OrderedDict[str, list[str]] = OrderedDict()
    for text in eval_texts:
        if not text:
            continue
        for section, line in _extract_checkboxes_by_section(text):
            sections.setdefault(section, []).append(line)

    # 2. Build feature testing block
    feature_block = ""
    for section, lines in sections.items():
        feature_block += f"#### {section}\n"
        feature_block += "\n".join(lines) + "\n\n"

    # 3. Compute pass rate from all checkboxes
    all_lines = [l for lines in sections.values() for l in lines]
    passed = sum(1 for l in all_lines if re.match(r"^\s*- \[x\]", l))
    total = len(all_lines)
    pct = int(passed / total * 100) if total > 0 else 0

    # 4. Extract lead-only sections
    lead_text = eval_texts[lead_index] if lead_index < len(eval_texts) else ""
    lead_sections = _extract_lead_sections(lead_text)

    # 5. Extract and renumber Required Changes from lead
    changes = _extract_required_changes(lead_text)
    changes_section = ""
    if changes:
        numbered = []
        for idx, change in enumerate(changes, 1):
            renumbered = re.sub(r"^\d+[\.\)]\s*", "", change, count=1)
            numbered.append(f"{idx}. {renumbered}")
        changes_section = "### Required Changes\n" + "\n".join(numbered) + "\n"

    # 6. Assemble
    merged = "## Application Evaluation (Parallel)\n\n"
    merged += f"### Feature Pass Rate: {passed}/{total} ({pct}%)\n\n"
    merged += "### Feature Testing\n\n"
    merged += feature_block
    if lead_sections:
        merged += lead_sections + "\n\n"
    if changes_section:
        merged += changes_section

    return merged


def _parse_automation_limited(text: str) -> tuple[int, int]:
    """Count automation-limited vs total criteria in evaluation text.

    Returns (automation_limited_count, total_criteria_count).
    """
    lines = text.split("\n")
    total = 0
    limited = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r"^- \[[x ]\] ", stripped):
            total += 1
            if "automation-limited" in stripped.lower():
                limited += 1
    return limited, total


def _slug_from_spec(spec: str) -> str:
    match = re.search(r"^#\s+(.+)$", spec, re.MULTILINE)
    if match:
        name = match.group(1).strip()
        slug = re.sub(r"[^a-z0-9\s-]", "", name.lower())
        return re.sub(r"[\s]+", "-", slug).strip("-") or "app"
    return "app"


def _make_server(comms_dir: str, output_dir: str) -> DevServer:
    return DevServer(
        comms_dir=comms_dir,
        output_dir=output_dir,
        startup_wait=CONFIG["dev_server_startup_wait"],
        health_timeout=CONFIG["dev_server_health_timeout"],
        health_url=CONFIG["dev_server_url"],
    )


def _read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


def _read_file_optional(path: str) -> str:
    try:
        return _read_file(path)
    except FileNotFoundError:
        return ""


class Orchestrator:
    def __init__(self, root_dir: str | None = None):
        self.root = root_dir or get_harness_root()
        self.comms_dir = os.path.join(self.root, CONFIG["comms_dir"])
        self.output_base = os.path.join(self.root, CONFIG["output_dir"])
        self.output_dir = None
        self.agents_dir = os.path.join(self.root, "agents")

        self.spec_path = os.path.join(self.comms_dir, "spec.md")
        self._spec: str | None = None
        self.sprints: list[Sprint] = []
        self._created_dirs: set[str] = set()

        self._planner_prompt = None
        self._generator_prompt = None
        self._evaluator_prompt = None

        # Continuous session: Generator keeps context across build attempts
        self._generator_session_id: str | None = None
        # Cache contracts to prevent generator from modifying criteria between retries
        self._cached_contracts: dict[str, str] = {}

        self.state = HarnessState(self.comms_dir)
        self.server = None

    @property
    def spec(self) -> str:
        if self._spec is None:
            self._spec = _read_file(self.spec_path)
        return self._spec

    def _load_prompt(self, name: str) -> str:
        return _read_file(os.path.join(self.agents_dir, f"{name}.md"))

    @property
    def planner_prompt(self) -> str:
        if self._planner_prompt is None:
            self._planner_prompt = self._load_prompt("planner")
        return self._planner_prompt

    @property
    def generator_prompt(self) -> str:
        if self._generator_prompt is None:
            self._generator_prompt = self._load_prompt("generator")
        return self._generator_prompt

    @property
    def evaluator_prompt(self) -> str:
        if self._evaluator_prompt is None:
            self._evaluator_prompt = self._load_prompt("evaluator")
        return self._evaluator_prompt

    def _track_cost(self, agent_result, label: str = ""):
        self.state.add_cost(agent_result.input_tokens, agent_result.output_tokens, label)
        cost_usd = getattr(agent_result, "cost_usd", 0) or 0
        if cost_usd > 0:
            self.state.add_cost_usd(cost_usd)

    def _init_output(self):
        slug = _slug_from_spec(self.spec)
        base = os.path.join(self.output_base, slug)
        # Avoid collision with existing output from previous runs
        self.output_dir = base
        n = 2
        while os.path.isdir(self.output_dir) and os.listdir(self.output_dir):
            self.output_dir = f"{base}-{n}"
            n += 1
        os.makedirs(self.output_dir, exist_ok=True)
        subprocess.run(
            ["git", "init"], cwd=self.output_dir, capture_output=True
        )
        self.server = _make_server(self.comms_dir, self.output_dir)
        # Start project-level log file
        _log_init(os.path.join(self.output_dir, "harness.log"))
        _log("Harness", f"Project directory: output/{slug}/")

    def _sprint_dir(self, sprint: Sprint) -> str:
        d = os.path.join(self.comms_dir, "sprints", f"sprint-{sprint.number}")
        if d not in self._created_dirs:
            os.makedirs(d, exist_ok=True)
            os.makedirs(os.path.join(d, "screenshots"), exist_ok=True)
            self._created_dirs.add(d)
        return d

    def setup(self):
        os.makedirs(self.output_base, exist_ok=True)
        # Clean stale artifacts from previous runs to prevent data leakage
        sprints_dir = os.path.join(self.comms_dir, "sprints")
        if os.path.isdir(sprints_dir):
            shutil.rmtree(sprints_dir)
        for stale in ("spec.md", "dev_server.json", "integration_evaluation.md",
                      "testable_criteria.md", "criteria_response.md"):
            path = os.path.join(self.comms_dir, stale)
            if os.path.exists(path):
                os.remove(path)
        # Clean up integration eval attempt files
        import glob
        for f in glob.glob(os.path.join(self.comms_dir, "integration_evaluation_attempt_*.md")):
            os.remove(f)
        for f in glob.glob(os.path.join(self.comms_dir, "integration_fix_*.md")):
            os.remove(f)
        self._created_dirs.clear()
        self.state.init()

    async def plan(self, user_prompt: str):
        _log("Planner", "Generating spec...")
        start = time.time()
        design_skill_path = os.path.join(self.agents_dir, "frontend-design-skill.md")
        prompt = (
            f"User request: {user_prompt}\n\n"
            f"First, read the frontend design skill at {design_skill_path} for visual design guidance.\n\n"
            f"Then generate a complete product specification. "
            f"Write the spec to {self.spec_path} using the Write tool."
        )
        agent_result = await call_agent(
            system_prompt=self.planner_prompt,
            user_prompt=prompt,
            allowed_tools=TOOLS_READ_WRITE,
            cwd=self.root,
            timeout=CONFIG["agent_timeout_planner"],
            model=resolve_agent_model("planner"),
        )
        self._track_cost(agent_result, "planner")

        if not os.path.exists(self.spec_path):
            raise RuntimeError("Planner did not write spec.md")

        # Cache spec and initialize output
        self._spec = _read_file(self.spec_path)
        self._init_output()
        self.sprints = parse_sprints(self.spec)

        elapsed = int(time.time() - start)
        self.state.record_plan_time(elapsed)
        self.state.set_sprint_info(current=0, total=len(self.sprints))
        self.state.set_phase("building")
        _log("Planner", f"Spec generated — {len(self.sprints)} sprint(s). ({_fmt_stats(agent_result, start)})")
        _print_spec_summary(self.spec, self.sprints)

    async def generate_criteria(self) -> str:
        """Generate testable criteria from spec (simple mode).

        The Evaluator extracts concrete, interaction-level test criteria from the
        high-level spec. This replaces contract negotiation in simple mode and
        ensures Generator builds to sufficient depth.
        """
        criteria_path = os.path.join(self.comms_dir, "testable_criteria.md")
        _log("Evaluator", "Generating testable criteria from spec...")
        start = time.time()

        prompt = (
            f"Generate testable criteria from this product spec.\n\n"
            f"Product spec:\n{self.spec}\n\n"
            f"Use the Criteria Generation Mode described in your instructions.\n"
            f"Cover EVERY feature (P0, P1, P2) with 5-15 criteria each.\n"
            f"Ignore the '## Sprints' section — generate criteria for ALL features as one unit.\n\n"
            f"Write the criteria to {criteria_path} using the Write tool."
        )
        agent_result = await call_agent(
            system_prompt=self.evaluator_prompt,
            user_prompt=prompt,
            allowed_tools=TOOLS_EVALUATOR,
            cwd=self.root,
            timeout=CONFIG["agent_timeout_criteria"],
            model=resolve_agent_model("evaluator"),
        )
        self._track_cost(agent_result, "criteria")

        if not os.path.exists(criteria_path):
            raise RuntimeError(
                "Criteria generation failed — Evaluator did not write testable_criteria.md. "
                "Cannot proceed without testable criteria."
            )

        criteria_text = _read_file(criteria_path)
        # Count criteria lines and validate quality bounds
        criteria_count = _count_criteria(criteria_text)
        if criteria_count < 10:
            raise RuntimeError(
                f"Criteria generation produced only {criteria_count} criteria (minimum 10 required). "
                f"The Evaluator may have failed to parse the spec correctly."
            )
        _log("Harness", f"Criteria count: {criteria_count}")
        elapsed_s = int(time.time() - start)
        _log("Evaluator", f"Generated {criteria_count} testable criteria. ({_fmt_stats(agent_result, start)})")
        # Save criteria generation response for analysis
        _save_agent_response(self.comms_dir, "criteria_response.md", agent_result.result)
        return criteria_path

    async def negotiate_contract(self, sprint: Sprint) -> str:
        sprint_dir = self._sprint_dir(sprint)
        proposal_path = os.path.join(sprint_dir, "contract_proposal.md")
        review_path = os.path.join(sprint_dir, "contract_review.md")
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")

        max_rounds = CONFIG["max_negotiation_rounds"]
        _log("Contract", f"Sprint {sprint.number}: Negotiating (max {max_rounds} rounds)...")
        start = time.time()

        for round_num in range(max_rounds):
            review_context = _read_file_optional(review_path)
            if review_context:
                review_context = f"\n\nEvaluator's review of your previous proposal:\n{review_context}"

            gen_prompt = (
                f"Propose a sprint contract for Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Sprint scope — Features: {', '.join(sprint.features)}. "
                f"Goal: {sprint.goal}\n\n"
                f"Product spec: Read from {self.spec_path}\n"
                f"{review_context}\n\n"
                f"Write your contract proposal to {proposal_path} using the Write tool. "
                f"Use the Contract Proposal Mode described in your instructions."
            )
            _log("Contract", f"Sprint {sprint.number}: Round {round_num + 1}/{max_rounds} — Generator proposing...")
            gen_result = await call_agent(
                system_prompt=self.generator_prompt,
                user_prompt=gen_prompt,
                allowed_tools=TOOLS_READ_WRITE,
                cwd=self.root,
                timeout=CONFIG["agent_timeout_negotiate"],
                model=resolve_agent_model("generator"),
            )
            self._track_cost(gen_result, "negotiate")

            proposal = _read_file(proposal_path)
            eval_prompt = (
                f"Review this sprint contract proposal for Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Proposal:\n{proposal}\n\n"
                f"Full product spec:\n{self.spec}\n\n"
                f"Write your review to {review_path} using the Write tool. "
                f"Use the Contract Review Mode described in your instructions. "
                f"If the criteria are complete and testable, write '{CONTRACT_AGREED}' at the top."
            )
            _log("Contract", f"Sprint {sprint.number}: Round {round_num + 1}/{max_rounds} — Evaluator reviewing...")
            eval_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=eval_prompt,
                allowed_tools=TOOLS_EVALUATOR,
                cwd=self.root,
                timeout=CONFIG["agent_timeout_negotiate"],
                model=resolve_agent_model("evaluator"),
            )
            self._track_cost(eval_result, "negotiate")

            # Check both agent response and written file for AGREED
            agreed = _check_contract_agreed(eval_result.result)
            if not agreed:
                review_text = _read_file_optional(review_path)
                if review_text:
                    agreed = _check_contract_agreed(review_text)

            if agreed:
                shutil.copy(proposal_path, contract_path)
                _log("Contract", f"Sprint {sprint.number}: Agreed in round {round_num + 1}. ({_elapsed(start)})")
                _print_contract_summary(contract_path)
                break
        else:
            shutil.copy(proposal_path, contract_path)
            _log("Contract", f"Sprint {sprint.number}: No agreement — using last proposal. ({_elapsed(start)})")
            _print_contract_summary(contract_path)

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "negotiate", elapsed)
        return contract_path

    @staticmethod
    def _sprint_label(sprint: Sprint) -> str:
        """'Sprint N' for full mode, 'Build' for simple mode (name='Full Build')."""
        if sprint.name == "Full Build":
            return "Build"
        return f"Sprint {sprint.number}"

    async def build(self, sprint: Sprint, contract_path: str):
        # Use cached contract (GAN principle: generator cannot weaken its own test plan)
        cache_key = contract_path
        if cache_key not in self._cached_contracts:
            self._cached_contracts[cache_key] = _read_file(contract_path)
        contract = self._cached_contracts[cache_key]

        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        eval_context = _read_file_optional(eval_path)
        if eval_context:
            # Extract automation-limited items so Generator knows to self-verify them
            limited_items = [
                line.strip() for line in eval_context.split("\n")
                if "automation-limited" in line.lower()
            ]
            limited_section = ""
            if limited_items:
                items_text = "\n".join(f"  {item}" for item in limited_items)
                limited_section = (
                    f"\n\n⚠️ AUTOMATION-LIMITED ITEMS ({len(limited_items)} criteria the Evaluator "
                    f"could NOT test via browser automation):\n{items_text}\n"
                    f"These features MUST work but the Evaluator cannot verify them. "
                    f"YOU must self-test each one: run the app, trigger the interaction "
                    f"programmatically or via curl/API, and confirm correct behavior. "
                    f"If any of these are broken, the next evaluation will fail again."
                )

            # Score trajectory from eval history (file-based, not harness injection)
            score_trajectory = ""
            scores = self.state.get_eval_scores(sprint.number)
            if scores:
                valid = [s for s in scores if s.get("total", 0) > 0]
                if valid:
                    trajectory = " → ".join(f"{s['pct']}%" for s in valid)
                    latest = valid[-1]
                    direction = ""
                    if len(valid) >= 2:
                        diff = latest["pct"] - valid[-2]["pct"]
                        direction = f" ({'↑' if diff > 0 else '↓'} {abs(diff)}pp from last)"
                    score_trajectory = (
                        f"\n\nEval score trajectory: {trajectory}{direction}\n"
                        f"Latest: {latest['passed']}/{latest['total']} ({latest['pct']}%)"
                    )

            eval_context = (
                f"\n\nPrevious evaluation feedback:\n{eval_context}\n\n"
                f"{score_trajectory}\n\n"
                f"Make a strategic decision based on the score trajectory:\n"
                f"**REFINE** if score is improving and failures are specific, fixable issues.\n"
                f"**PIVOT** if score is stagnant or declining, or the evaluator described "
                f"fundamental problems (generic design, multiple simultaneous failures).\n"
                f"State your decision explicitly: 'STRATEGY: REFINE — [reason]' or "
                f"'STRATEGY: PIVOT — [reason]'."
                f"{limited_section}"
            )

        self.state.increment(sprint.number, "build")
        attempt = self.state.get_sprint_attempt(sprint.number, "build")

        # Generator runs in output_dir, but dev_server.json must land in comms_dir
        dev_server_json_path = os.path.join(self.comms_dir, "dev_server.json")

        label = self._sprint_label(sprint)
        is_simple = sprint.name == "Full Build"
        _log("Generator", f"{label} — Building (attempt {attempt})...")
        start = time.time()

        if is_simple:
            criteria_count = _count_criteria(contract)
            prompt = (
                f"Build the COMPLETE application.\n\n"
                f"Testable criteria ({criteria_count} criteria — the Evaluator will test EACH ONE):\n{contract}\n\n"
                f"Product spec (read for visual design language and feature details): "
                f"Read from {self.spec_path}\n"
                f"{eval_context}\n\n"
                f"You have {criteria_count} testable criteria to implement. The Evaluator will test "
                f"EACH ONE individually by interacting with the running app. A feature that exists "
                f"in the UI but doesn't work when interacted with will FAIL.\n\n"
                f"FIRST BUILD QUALITY IS CRITICAL. Each evaluation cycle costs $10-20 and 30+ "
                f"minutes. A thorough first build that takes 1-2 hours but covers 90%+ of criteria "
                f"is far cheaper than a rushed 20-minute build that triggers 5+ eval-fix cycles. "
                f"Do NOT stop after implementing the basic structure.\n\n"
                f"Work in the current directory. Self-verify: build must succeed, app must run. "
                f"Commit your changes with git.\n\n"
                f"Write the dev server config to this ABSOLUTE path: {dev_server_json_path}\n\n"
                f"MANDATORY BEFORE FINISHING — verification checklist:\n"
                f"1. Start both servers and confirm they run without errors\n"
                f"2. Run `curl` against EVERY API endpoint — paste the actual output\n"
                f"3. Open {CONFIG['dev_server_url']} in the browser and verify the page loads\n"
                f"4. Write your self-evaluation to: {self._sprint_dir(sprint)}/self_eval.md\n"
                f"   Format: '- [x] criterion' or '- [ ] criterion — reason (NOT IMPLEMENTED)'\n"
                f"   Be honest. Claiming 100% when features are incomplete wastes an entire "
                f"   eval cycle ($15+). Mark criteria you did NOT verify as '- [ ]'.\n"
                f"   'Self-eval pass rate: X/Y (Z%)'. If <90%, keep building."
            )
        else:
            prompt = (
                f"Build Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Sprint contract:\n{contract}\n\n"
                f"Product spec: Read from {self.spec_path}\n"
                f"{eval_context}\n\n"
                f"Implement ALL criteria in the sprint contract. "
                f"Read the product spec file for visual design language and feature details. "
                f"Work in the current directory. Self-verify: build must succeed, app must run. "
                f"Commit your changes with git.\n\n"
                f"Write the dev server config to this ABSOLUTE path: {dev_server_json_path}\n\n"
                f"MANDATORY: Before finishing, write a self-evaluation to this ABSOLUTE path: "
                f"{self._sprint_dir(sprint)}/self_eval.md\n"
                f"Format: for each testable criterion, write '- [x] criterion text' if satisfied "
                f"or '- [ ] criterion text — reason' if not. Count your pass rate at the end: "
                f"'Self-eval pass rate: X/Y (Z%)'. If <90%, keep building."
            )

        try:
            agent_result = await call_agent(
                system_prompt=self.generator_prompt,
                user_prompt=prompt,
                allowed_tools=TOOLS_FULL,
                cwd=self.output_dir,
                max_turns=CONFIG["generator_max_turns"],
                resume=self._generator_session_id,
                timeout=CONFIG["agent_timeout_build"],
                model=resolve_agent_model("generator"),
            )
        except AgentError:
            # Reset conversation — crashed session can't be resumed
            self._generator_session_id = None
            raise
        # Maintain continuous session across build attempts (per article recommendation)
        if agent_result.session_id:
            self._generator_session_id = agent_result.session_id
        self._track_cost(agent_result, "build")
        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "build", elapsed)
        _log("Generator", f"{label} — Build complete. ({_fmt_stats(agent_result, start)})")
        _print_build_summary(self.output_dir)
        # Save Generator response for post-run analysis
        attempt = self.state.get_sprint_attempt(sprint.number, "build")
        _save_agent_response(
            self._sprint_dir(sprint),
            f"generator_response_build_{attempt}.md",
            agent_result.result,
        )

        # Log Generator self-eval and cross-reference with last Evaluator score
        self_eval_path = os.path.join(self._sprint_dir(sprint), "self_eval.md")
        self_eval_text = _read_file_optional(self_eval_path)
        if self_eval_text:
            rate_match = re.search(r"pass rate:\s*(\d+)/(\d+)", self_eval_text, re.IGNORECASE)
            if rate_match:
                passed_count = int(rate_match.group(1))
                total_count = int(rate_match.group(2))
                pct = int(passed_count / total_count * 100) if total_count > 0 else 0
                _log("Generator", f"Self-eval: {passed_count}/{total_count} ({pct}%) criteria satisfied.")
                if pct < 90:
                    _log("Harness", f"Self-eval below 90% ({pct}%). Generator should have continued building.")

                # Honesty check: compare self-eval with last Evaluator score
                last_eval = self.state.get_last_eval_score(sprint.number)
                if last_eval and pct > 95 and last_eval["pct"] < 80:
                    _log("Harness",
                         f"⚠ SELF-EVAL DISCREPANCY: Generator claims {pct}% but "
                         f"last Evaluator scored {last_eval['pct']}%. "
                         f"Generator may not be honestly self-testing.")

    async def evaluate(self, sprint: Sprint, contract_path: str) -> bool:
        """Dispatch to parallel or single evaluation based on criteria structure."""
        if contract_path not in self._cached_contracts:
            self._cached_contracts[contract_path] = _read_file(contract_path)
        contract = self._cached_contracts[contract_path]

        sections = parse_criteria_sections(contract)
        buckets = assign_sections_to_evaluators(sections)

        if len(buckets) > 1:
            return await self._evaluate_parallel(sprint, contract_path, buckets)
        return await self._evaluate_single(sprint, contract_path)

    async def _run_single_evaluator(
        self, evaluator_id: int, is_lead: bool,
        criteria_text: str, screenshots_dir: str,
        prev_eval_section: str,
        eval_output_path: str = "",
        section_names: list[str] | None = None,
    ) -> str:
        """Run one evaluator agent. Returns evaluation text."""
        os.makedirs(screenshots_dir, exist_ok=True)

        # Each parallel evaluator writes to its OWN file (not shared evaluation.md)
        write_instruction = ""
        if eval_output_path:
            write_instruction = (
                f"\n\nIMPORTANT: Write your complete evaluation to this EXACT path "
                f"using the Write tool: {eval_output_path}\n"
                f"Also include the full evaluation in your text response."
            )

        # Scope restriction + data isolation for parallel evaluators
        scope_block = ""
        if section_names:
            names_list = "\n".join(f"  - {n}" for n in section_names)
            scope_block = (
                f"\n\nSCOPE RESTRICTION — You are ONLY responsible for these sections:\n"
                f"{names_list}\n\n"
                f"Do NOT write checkbox results for criteria outside these sections. "
                f"Other evaluators are testing those sections simultaneously. "
                f"Duplicate or contradictory results for the same criterion break the evaluation.\n\n"
                f"DATA ISOLATION — Other evaluators are testing the same app concurrently.\n"
                f"Prefix ALL test data you create with 'eval{evaluator_id}-' to avoid conflicts.\n"
                f"Examples: user 'eval{evaluator_id}-testuser@test.com', "
                f"recipe 'eval{evaluator_id}-Test Recipe', board 'eval{evaluator_id}-Test Board'.\n"
                f"Do NOT delete data that doesn't have your prefix — it belongs to another evaluator."
            )

        if is_lead:
            prompt = (
                f"Evaluate this SUBSET of the application (you are evaluator {evaluator_id}, "
                f"the LEAD evaluator responsible for Quality Assessment).\n\n"
                f"Your criteria:\n{criteria_text}\n\n"
                f"Product spec (for design quality assessment):\n{self.spec}\n\n"
                f"Test each criterion in your assigned sections. Take screenshots to {screenshots_dir}/.\n"
                f"Write BOTH Feature Testing checkboxes AND the full Quality Assessment "
                f"(Frontend + Backend tables, Evidence, Evidence Audit, Verdict, Required Changes).\n"
                f"Navigate to {CONFIG['dev_server_url']}."
                f"{scope_block}"
                f"{write_instruction}"
                f"{prev_eval_section}"
            )
        else:
            prompt = (
                f"Evaluate this SUBSET of the application (you are evaluator {evaluator_id}).\n\n"
                f"Your criteria:\n{criteria_text}\n\n"
                f"Product spec (for context):\n{self.spec}\n\n"
                f"Test each criterion in your assigned sections. Take screenshots to {screenshots_dir}/.\n"
                f"Write Feature Testing checkboxes ONLY for your assigned sections.\n"
                f"DO NOT write any of these — the lead evaluator handles them:\n"
                f"- Quality Assessment tables (Frontend/Backend)\n"
                f"- Evidence section\n"
                f"- Evidence Audit\n"
                f"- Verdict (PASS/FAIL)\n"
                f"- Required Changes\n\n"
                f"If you find issues, mark them as FAIL checkboxes with explanation.\n"
                f"Navigate to {CONFIG['dev_server_url']}."
                f"{scope_block}"
                f"{write_instruction}"
                f"{prev_eval_section}"
            )

        label = f"eval-{evaluator_id}" + (" lead" if is_lead else "")
        result = await call_agent(
            system_prompt=self.evaluator_prompt,
            user_prompt=prompt,
            allowed_tools=TOOLS_EVALUATOR,
            mcp_servers=CONFIG.get("mcp_servers"),
            cwd=self.root,
            timeout=CONFIG["agent_timeout_eval"],
            model=resolve_agent_model("evaluator"),
            status_label=label,
        )
        self._track_cost(result, "eval")

        # Use whichever source has more criteria: text response or per-evaluator disk file
        best = self._pick_best_eval(result.result, eval_output_path)

        # Validation: if both sources have 0 criteria, retry once
        _, criteria_count = _parse_eval_score(best)
        if criteria_count == 0:
            _log("Evaluator", f"Evaluator {evaluator_id} returned 0 criteria. Retrying once...")
            retry_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=prompt,
                allowed_tools=TOOLS_EVALUATOR,
                mcp_servers=CONFIG.get("mcp_servers"),
                cwd=self.root,
                timeout=CONFIG["agent_timeout_eval"],
                model=resolve_agent_model("evaluator"),
            )
            self._track_cost(retry_result, "eval")
            best = self._pick_best_eval(retry_result.result, eval_output_path)

        return best

    @staticmethod
    def _pick_best_eval(agent_response: str, eval_output_path: str) -> str:
        """Return whichever has more criteria: text response or disk file."""
        disk_eval = _read_file_optional(eval_output_path) if eval_output_path else ""
        _, response_criteria = _parse_eval_score(agent_response)
        _, disk_criteria = _parse_eval_score(disk_eval)
        if disk_criteria > response_criteria:
            return disk_eval
        return agent_response

    async def _evaluate_parallel(
        self, sprint: Sprint, contract_path: str,
        buckets: list[list[CriteriaSection]],
    ) -> bool:
        """Run N evaluator agents in parallel, merge results."""
        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")

        self.state.increment(sprint.number, "eval")
        attempt = self.state.get_sprint_attempt(sprint.number, "eval")
        label = self._sprint_label(sprint)

        n_evaluators = len(buckets)
        criteria_counts = [sum(s.criteria_count for s in b) for b in buckets]
        _log("Evaluator", f"{label} — Parallel evaluation: {n_evaluators} evaluators "
             f"({', '.join(str(c) for c in criteria_counts)} criteria each)")

        # Previous evaluation for regression comparison
        prev_eval = ""
        if attempt > 1:
            prev_path = os.path.join(sprint_dir, f"evaluation_attempt_{attempt - 1}.md")
            prev_eval = _read_file_optional(prev_path)

        # Build criteria text per evaluator
        evaluator_criteria = []
        for bucket in buckets:
            text = "\n\n".join(s.raw_text for s in bucket)
            evaluator_criteria.append(text)

        # Build previous eval section (same for all evaluators — full prev eval)
        prev_eval_section = ""
        if prev_eval and _count_criteria(prev_eval) > 0:
            prev_eval_section = (
                f"\n\n## Previous Evaluation (attempt {attempt - 1})\n\n"
                f"Compare your findings against this previous evaluation. "
                f"For each criterion:\n"
                f"- If it was PASS before and is now FAIL, label it **REGRESSION** — "
                f"this is critical, the Generator broke something that worked.\n"
                f"- If it was FAIL before and is now PASS, label it **FIXED**.\n"
                f"- If it was FAIL before and is still FAIL, label it **PERSISTENT**.\n\n"
                f"In your Required Changes section, prioritize regressions first, "
                f"then persistent failures. For each change, note its blast radius.\n\n"
                f"Previous evaluation:\n{prev_eval}"
            )

        # Start server once for all evaluators
        _log("Evaluator", f"{label} — Starting servers...")
        self.server.start()
        _log("Evaluator", f"{label} — Testing (attempt {attempt}, {n_evaluators} parallel)...")
        start = time.time()

        try:
            # Launch evaluators with staggered start (10s between each)
            # to avoid API rate limit spikes from simultaneous requests
            # Build section name lists per evaluator for scope restriction
            bucket_section_names = [
                [s.header for s in bucket] for bucket in buckets
            ]

            async def _staggered_eval(i, delay):
                if delay > 0:
                    await asyncio.sleep(delay)
                screenshots_dir = os.path.join(sprint_dir, "screenshots", f"eval-{i}")
                eval_output = os.path.join(sprint_dir, f"evaluation_part_{i}.md")
                return await self._run_single_evaluator(
                    evaluator_id=i,
                    is_lead=(i == 0),
                    criteria_text=evaluator_criteria[i],
                    screenshots_dir=screenshots_dir,
                    prev_eval_section=prev_eval_section,
                    eval_output_path=eval_output,
                    section_names=bucket_section_names[i],
                )

            tasks = [_staggered_eval(i, i * 10) for i in range(n_evaluators)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Progressive fallback: if ALL evaluators crashed, retry with fewer
            all_crashed = all(isinstance(r, Exception) for r in results)
            if all_crashed:
                crash_msg = str(results[0])[:200]
                _log("Evaluator", f"{label} — ALL {n_evaluators} evaluators crashed: {crash_msg}")

                # Try with half
                fallback_n = max(1, n_evaluators // 2)
                if fallback_n < n_evaluators:
                    _log("Evaluator", f"{label} — Retrying with {fallback_n} evaluators...")
                    merged_buckets = [[] for _ in range(fallback_n)]
                    for i, bucket in enumerate(buckets):
                        merged_buckets[i % fallback_n].extend(bucket)
                    merged_criteria = ["\n\n".join(s.raw_text for s in b) for b in merged_buckets]

                    fb_tasks = []
                    for i in range(fallback_n):
                        fb_dir = os.path.join(sprint_dir, "screenshots", f"eval-fb-{i}")
                        fb_output = os.path.join(sprint_dir, f"evaluation_fb_{i}.md")
                        fb_tasks.append(self._run_single_evaluator(
                            evaluator_id=i, is_lead=(i == 0),
                            criteria_text=merged_criteria[i],
                            screenshots_dir=fb_dir,
                            prev_eval_section=prev_eval_section,
                            eval_output_path=fb_output,
                        ))
                    results = await asyncio.gather(*fb_tasks, return_exceptions=True)
                    all_crashed = all(isinstance(r, Exception) for r in results)
                    buckets = merged_buckets

                # Still all crashed — single evaluator fallback
                if all_crashed:
                    _log("Evaluator", f"{label} — Fallback to single evaluator...")
                    all_criteria = "\n\n".join(s.raw_text for b in buckets for s in b)
                    fb_dir = os.path.join(sprint_dir, "screenshots", "eval-single-fb")
                    try:
                        fb_single_output = os.path.join(sprint_dir, "evaluation_fb_single.md")
                        single_result = await self._run_single_evaluator(
                            evaluator_id=0, is_lead=True,
                            criteria_text=all_criteria, screenshots_dir=fb_dir,
                            prev_eval_section=prev_eval_section,
                            eval_output_path=fb_single_output,
                        )
                        results = [single_result]
                        all_sections = [s for b in buckets for s in b]
                        buckets = [all_sections]
                    except Exception as exc:
                        _log("Evaluator", f"{label} — All fallbacks exhausted: {exc}")
                        raise AgentError(f"All evaluators crashed including fallbacks: {crash_msg}")

            # Collect successful results, mark crashes as FAIL
            eval_texts = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    _log("Evaluator", f"{label} — Evaluator {i} CRASHED: {result}")
                    fail_lines = []
                    if i < len(buckets):
                        for section in buckets[i]:
                            fail_lines.append(f"#### {section.header}")
                            for line in section.raw_text.split("\n"):
                                if re.match(r"^\s*- \[[x ]\] ", line.strip()):
                                    criterion = re.sub(r"^\s*- \[[x ]\] ", "", line.strip())
                                    fail_lines.append(f"- [ ] {criterion} ← FAIL (evaluator crashed)")
                    eval_texts.append("\n".join(fail_lines))
                else:
                    eval_texts.append(result)
                    elapsed_i = int(time.time() - start)
                    _log("Evaluator", f"{label} — Evaluator {i} complete ({elapsed_i}s)")

            # Detect leniency disparity between evaluators
            per_eval_rates = []
            for i, text in enumerate(eval_texts):
                p, t = _parse_eval_score(text)
                if t > 0:
                    per_eval_rates.append((i, int(p / t * 100)))
            if len(per_eval_rates) > 1:
                rates = [r[1] for r in per_eval_rates]
                if max(rates) - min(rates) > 25:
                    rate_str = ", ".join(f"eval-{r[0]}: {r[1]}%" for r in per_eval_rates)
                    _log("Evaluator", f"{label} — ⚠ LENIENCY DISPARITY: {rate_str}")

            # Merge all results
            best_eval = merge_evaluations(eval_texts, lead_index=0)

            # Validate: did we lose criteria during merge?
            input_criteria = sum(s.criteria_count for b in buckets for s in b)
            _, output_criteria = _parse_eval_score(best_eval)
            if output_criteria < input_criteria:
                lost = input_criteria - output_criteria
                _log("Evaluator", f"{label} — ⚠ Merge lost {lost} criteria "
                     f"({output_criteria}/{input_criteria} recovered). "
                     f"Some evaluators may have returned empty responses.")

            with open(eval_path, "w") as f:
                f.write(best_eval)
            _save_agent_response(
                sprint_dir, f"evaluation_attempt_{attempt}.md", best_eval,
            )
        finally:
            _log("Evaluator", f"{label} — Stopping servers...")
            self.server.stop()
            # Kill zombie Chromium/Playwright from parallel MCP instances
            _cleanup_playwright()

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "eval", elapsed)

        passed = _check_verdict_pass(best_eval)

        if passed:
            limited, total = _parse_automation_limited(best_eval)
            if total > 0 and limited / total > 0.10:
                _log("Evaluator",
                     f"{label} — Verdict was PASS but {limited}/{total} criteria "
                     f"({int(limited/total*100)}%) marked automation-limited. "
                     f"Overriding to FAIL.")
                passed = False

        stats = f"{elapsed}s, {n_evaluators} evaluators"
        if passed:
            _log("Evaluator", f"{label} — {_C.GREEN}{_C.BOLD}PASS{_C.RESET} ({stats})")
        else:
            _log("Evaluator", f"{label} — {_C.RED}{_C.BOLD}FAIL{_C.RESET} ({stats})")
        _print_eval_summary(best_eval)
        if not passed:
            _print_required_changes(best_eval)

        eval_passed, eval_total = _parse_eval_score(best_eval)
        if eval_total > 0:
            pct = int(eval_passed / eval_total * 100)
            self.state.add_eval_score(sprint.number, attempt, eval_passed, eval_total)
            _log("Evaluator", f"{label} — Score: {eval_passed}/{eval_total} ({pct}%)")

        return passed

    async def _evaluate_single(self, sprint: Sprint, contract_path: str) -> bool:
        """Single evaluator (original behavior, for small criteria sets)."""
        # Use cached contract (GAN principle — same as build())
        if contract_path not in self._cached_contracts:
            self._cached_contracts[contract_path] = _read_file(contract_path)
        contract = self._cached_contracts[contract_path]

        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        screenshots_dir = os.path.join(sprint_dir, "screenshots")

        self.state.increment(sprint.number, "eval")
        attempt = self.state.get_sprint_attempt(sprint.number, "eval")

        # Read previous evaluation for regression comparison (article principle:
        # file-based communication, Evaluator compares directly)
        prev_eval_section = ""
        if attempt > 1:
            prev_attempt_path = os.path.join(
                sprint_dir, f"evaluation_attempt_{attempt - 1}.md"
            )
            prev_eval = _read_file_optional(prev_attempt_path)
            if prev_eval and _count_criteria(prev_eval) > 0:
                prev_eval_section = (
                    f"\n\n## Previous Evaluation (attempt {attempt - 1})\n\n"
                    f"Compare your findings against this previous evaluation. "
                    f"For each criterion:\n"
                    f"- If it was PASS before and is now FAIL, label it **REGRESSION** — "
                    f"this is critical, the Generator broke something that worked.\n"
                    f"- If it was FAIL before and is now PASS, label it **FIXED**.\n"
                    f"- If it was FAIL before and is still FAIL, label it **PERSISTENT**.\n\n"
                    f"In your Required Changes section, prioritize regressions first, "
                    f"then persistent failures. For each change, note its blast radius: "
                    f"\"fixing this unblocks N other criteria\" when applicable.\n\n"
                    f"Previous evaluation:\n{prev_eval}"
                )

        label = self._sprint_label(sprint)
        _log("Evaluator", f"{label} — Starting servers...")
        self.server.start()
        _log("Evaluator", f"{label} — Testing (attempt {attempt})...")
        start = time.time()
        is_simple = sprint.name == "Full Build"
        try:
            if is_simple:
                prompt = (
                    f"Evaluate the COMPLETE application.\n\n"
                    f"Testable criteria (test EACH one individually):\n{contract}\n\n"
                    f"Product spec (use for design quality assessment — Visual Design Language, "
                    f"color palette, typography, layout direction):\n{self.spec}\n\n"
                    f"For each criterion: interact with the app, verify the expected result, "
                    f"take a screenshot as evidence. Mark PASS or FAIL per criterion.\n"
                    f"A criterion that cannot be verified through interaction is FAIL.\n"
                    f"Do NOT mark a criterion PASS based on DOM/CSS inspection alone — "
                    f"you must perform the described user action and observe the result.\n\n"
                    f"Navigate to {CONFIG['dev_server_url']}. "
                    f"Take screenshots — save them to {screenshots_dir}/. "
                    f"Write your evaluation as a response."
                    f"{prev_eval_section}"
                )
            else:
                prompt = (
                    f"Evaluate Sprint {sprint.number}: {sprint.name}.\n\n"
                    f"Sprint contract (test against these criteria):\n{contract}\n\n"
                    f"Product spec:\n{self.spec}\n\n"
                    f"Navigate to {CONFIG['dev_server_url']}. Test every criterion in the sprint contract. "
                    f"Take screenshots as evidence — save them to {screenshots_dir}/. "
                    f"Write your evaluation as a response."
                    f"{prev_eval_section}"
                )

            eval_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=prompt,
                allowed_tools=TOOLS_EVALUATOR,
                mcp_servers=CONFIG.get("mcp_servers"),
                cwd=self.root,
                timeout=CONFIG["agent_timeout_eval"],
                model=resolve_agent_model("evaluator"),
            )
            self._track_cost(eval_result, "eval")

            # Preserve the most detailed evaluation content.
            # The Evaluator may write a detailed per-criterion evaluation to
            # evaluation.md via its Write tool, then return only a summary as
            # its text response. Use whichever version has more criteria.
            agent_response = eval_result.result
            disk_eval = _read_file_optional(eval_path)
            _, response_criteria = _parse_eval_score(agent_response)
            _, disk_criteria = _parse_eval_score(disk_eval)

            if disk_criteria > response_criteria:
                # Evaluator wrote a more detailed version to disk — keep it
                best_eval = disk_eval
                _log("Evaluator", f"{label} — Preserved disk evaluation ({disk_criteria} criteria) "
                     f"over agent response ({response_criteria} criteria)")
            else:
                # Agent response is the detailed version (or equally detailed)
                best_eval = agent_response
                with open(eval_path, "w") as f:
                    f.write(agent_response)

            # Save attempt-specific copy for quality tracking history
            _save_agent_response(
                sprint_dir,
                f"evaluation_attempt_{attempt}.md",
                best_eval,
            )
        finally:
            _log("Evaluator", f"{label} — Stopping servers...")
            self.server.stop()

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "eval", elapsed)

        passed = _check_verdict_pass(best_eval)

        # Reject PASS if too many criteria were skipped as automation-limited
        if passed:
            limited, total = _parse_automation_limited(best_eval)
            if total > 0 and limited / total > 0.10:
                _log("Evaluator",
                     f"{label} — Verdict was PASS but {limited}/{total} criteria "
                     f"({int(limited/total*100)}%) marked automation-limited. "
                     f"Overriding to FAIL — too many untested features.")
                passed = False

        stats = _fmt_stats(eval_result, start)
        if passed:
            _log("Evaluator", f"{label} — {_C.GREEN}{_C.BOLD}PASS{_C.RESET} ({stats})")
        else:
            _log("Evaluator", f"{label} — {_C.RED}{_C.BOLD}FAIL{_C.RESET} ({stats})")
        _print_eval_summary(best_eval)
        if not passed:
            _print_required_changes(best_eval)

        # Track eval score for regression detection
        eval_passed, eval_total = _parse_eval_score(best_eval)
        if eval_total > 0:
            pct = int(eval_passed / eval_total * 100)
            self.state.add_eval_score(sprint.number, attempt, eval_passed, eval_total)
            _log("Evaluator", f"{label} — Score: {eval_passed}/{eval_total} ({pct}%)")

        return passed

    async def verify(self, sprint: Sprint, contract_path: str) -> VerificationResult | None:
        """Run deterministic verification on the built app.

        Returns VerificationResult, or None if verifier is disabled.
        On FAIL, writes feedback to evaluation.md so Generator sees it on retry.
        """
        if not CONFIG.get("verifier_enabled", False):
            return None

        # Use cached contract
        if contract_path not in self._cached_contracts:
            self._cached_contracts[contract_path] = _read_file(contract_path)
        contract = self._cached_contracts[contract_path]

        sprint_dir = self._sprint_dir(sprint)
        artifacts_dir = os.path.join(sprint_dir, "verifier_artifacts")
        label = self._sprint_label(sprint)

        _log("Verifier", f"{label} — Classifying criteria...")
        typed_checks = await classify_criteria(contract, self.root)
        deterministic = [c for c in typed_checks if c.check_type.value != "subjective"]
        subjective = [c for c in typed_checks if c.check_type.value == "subjective"]
        _log("Verifier", f"{label} — {len(deterministic)} deterministic + {len(subjective)} subjective criteria")

        # Backup DB before verification (state reset)
        db_backup = backup_db(self.output_dir)

        _log("Verifier", f"{label} — Starting servers...")
        self.server.start()
        try:
            _log("Verifier", f"{label} — Running deterministic checks...")
            start = time.time()
            vresult = await run_verification(
                typed_checks, CONFIG["dev_server_url"],
                self.output_dir, artifacts_dir,
            )
            elapsed = int(time.time() - start)

            if vresult.overall_pass:
                _log("Verifier", f"{label} — {_C.GREEN}{_C.BOLD}PASS{_C.RESET} "
                     f"({len(vresult.passed)} passed, {len(vresult.subjective)} subjective, {elapsed}s)")
            else:
                _log("Verifier", f"{label} — {_C.RED}{_C.BOLD}FAIL{_C.RESET} "
                     f"({len(vresult.failed)} failed, {len(vresult.passed)} passed, {elapsed}s)")
                # Write feedback for Generator
                eval_path = os.path.join(sprint_dir, "evaluation.md")
                with open(eval_path, "w") as f:
                    f.write(vresult.feedback_text())
        finally:
            _log("Verifier", f"{label} — Stopping servers...")
            self.server.stop()
            # Restore DB after verification (clean state for Evaluator)
            if db_backup:
                restore_db(self.output_dir, db_backup)

        return vresult

    async def _smoke_test(self) -> tuple[bool, str]:
        """Quick health check: can the app load and render any UI elements?

        Returns (ok, message). Starts and stops the server internally.
        Catches crashes that would waste 30+ minutes of Evaluator time.
        """
        import urllib.request
        import urllib.error

        try:
            self.server.start()
        except RuntimeError as exc:
            return False, f"Server failed to start: {exc}"

        try:
            url = CONFIG["dev_server_url"]
            try:
                resp = urllib.request.urlopen(url, timeout=10)
                body = resp.read().decode("utf-8", errors="replace")
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                return False, f"HTTP request failed: {exc}"

            # Check for blank page: the HTML should have a non-empty <div id="root"> or similar
            if len(body) < 100:
                return False, f"Response body too small ({len(body)} bytes) — likely blank page"

            # Check for common React/Vue crash indicators
            if "<div id=\"root\"></div>" in body and "<script" in body:
                # Empty root div with scripts = React app that hasn't rendered yet
                # This is normal for SPA — the JS needs to execute.
                # We can't detect React runtime errors from HTML alone.
                # Still better than nothing: at least the server responded.
                pass

            return True, "OK"
        finally:
            self.server.stop()

    def _check_regression(self, sprint: Sprint, label: str):
        """Detect eval score regression and reset Generator session if needed.

        Triggers session reset when:
        - Score drops >20 percentage points from the best score seen
        - Two consecutive score drops (downward trend)
        """
        scores = self.state.get_eval_scores(sprint.number)
        if len(scores) < 2:
            return

        current = scores[-1]
        # Skip if current score is from a crash (no criteria evaluated)
        if current.get("total", 0) == 0:
            return

        previous = scores[-2]
        # Only consider scores with actual criteria for best calculation
        valid_scores = [s for s in scores if s.get("total", 0) > 0]
        if len(valid_scores) < 2:
            return
        best_pct = max(s["pct"] for s in valid_scores)

        # Catastrophic regression: >20pp drop from best
        drop_from_best = best_pct - current["pct"]
        if drop_from_best > 20:
            _log("Harness", f"{label} — REGRESSION DETECTED: score dropped from best "
                 f"{best_pct}% to {current['pct']}% (Δ{drop_from_best}pp). "
                 f"Resetting Generator session to clear stale context.")
            self._generator_session_id = None
            return

        # Two consecutive drops
        if len(scores) >= 3:
            prev_prev = scores[-3]
            if current["pct"] < previous["pct"] < prev_prev["pct"]:
                _log("Harness", f"{label} — DOWNWARD TREND: "
                     f"{prev_prev['pct']}% → {previous['pct']}% → {current['pct']}%. "
                     f"Resetting Generator session.")
                self._generator_session_id = None

    async def _retry_build_eval(self, sprint: Sprint, contract_path: str, label: str):
        """Shared retry loop for both full and simple modes."""
        sprint_passed = False
        consecutive_crashes = 0
        for attempt in range(CONFIG["max_build_attempts"]):
            # Cooldown after consecutive crashes (likely infrastructure issue)
            if consecutive_crashes >= 3:
                cooldown = min(60 * (2 ** (consecutive_crashes - 3)), 300)
                _log("Harness", f"{label} — {consecutive_crashes} consecutive crashes. "
                     f"Cooling down {cooldown}s (likely infrastructure issue).")
                await asyncio.sleep(cooldown)

            try:
                await self.build(sprint, contract_path)

                # Post-build smoke test before expensive evaluation
                smoke_ok, smoke_msg = await self._smoke_test()
                if not smoke_ok:
                    _log("Harness", f"{label} — SMOKE TEST FAIL: {smoke_msg}")
                    _log("Harness", f"{label} — Skipping Evaluator (app not functional)")
                    sprint_dir = self._sprint_dir(sprint)
                    eval_path = os.path.join(sprint_dir, "evaluation.md")
                    with open(eval_path, "w") as f:
                        f.write(
                            f"## Smoke Test: FAIL\n\n"
                            f"The application failed a basic health check before full evaluation.\n\n"
                            f"**Error:** {smoke_msg}\n\n"
                            f"### Verdict: FAIL\n\n"
                            f"### Required Changes\n"
                            f"1. Fix the app crash so it loads at {CONFIG['dev_server_url']}. "
                            f"Error details: {smoke_msg}\n"
                            f"2. After fixing, verify both servers start and the main page loads "
                            f"before handing off to evaluation.\n"
                        )
                    passed = False
                    consecutive_crashes = 0
                else:
                    vresult = await self.verify(sprint, contract_path)
                    if vresult is not None and not vresult.overall_pass:
                        _log("Harness", f"{label} — Verifier FAIL, skipping Evaluator (saving time + cost)")
                        passed = False
                    else:
                        passed = await self.evaluate(sprint, contract_path)
                    consecutive_crashes = 0
            except InfraError as exc:
                _log("Harness", f"{label} attempt {attempt + 1} INFRA ERROR: {exc}")
                _log("Harness", f"Infrastructure error — rebuilding won't help. Stopping retries for {label}.")
                passed = False
                break
            except AgentTimeout as exc:
                _log("Harness", f"{label} attempt {attempt + 1} TIMEOUT: {exc}")
                _log("Harness", f"Agent timed out — likely stuck. Stopping retries for {label}.")
                passed = False
                break
            except AgentError as exc:
                _log("Harness", f"{label} attempt {attempt + 1} error: {exc}")
                passed = False
                consecutive_crashes += 1
            except RuntimeError as exc:
                _log("Harness", f"{label} attempt {attempt + 1} server error: {exc}")
                passed = False
                consecutive_crashes += 1

            if passed:
                sprint_passed = True
                break

            # Regression detection: skip after crashes (no valid score to compare)
            if consecutive_crashes == 0:
                self._check_regression(sprint, label)

            if attempt == CONFIG["max_build_attempts"] - 1:
                # Delegate to user: continue or abort? (raises UserAbort)
                _ask_user_continue(label)

        self.state.set_sprint_result(sprint.number, sprint_passed)
        return sprint_passed

    async def run(self, user_prompt: str):
        print("=" * 60)
        print("  Idle Harness")
        print("=" * 60)
        total_start = time.time()

        self._mode = resolve_mode()
        gen_model = resolve_agent_model("generator")
        eval_model = resolve_agent_model("evaluator")
        plan_model = resolve_agent_model("planner")
        if gen_model == eval_model == plan_model:
            _log("Harness", f"Mode: {self._mode} (model: {gen_model})")
        else:
            _log("Harness", f"Mode: {self._mode} (generator: {gen_model}, evaluator: {eval_model}, planner: {plan_model})")

        self.setup()
        try:
            await self.plan(user_prompt)

            if self._mode == "simple":
                await self._run_simple()
            else:
                await self._run_full()
        except UserAbort:
            _log("Harness", "Aborted by user.")
        except (AgentError, RuntimeError) as exc:
            _log("Harness", f"Fatal error: {exc}")
        finally:
            self.state.set_phase("completed")
            if self.output_dir:
                self._print_report(total_start)
                self._archive_comms()
            _log_close()

    async def _run_full(self):
        run_start = time.time()
        for sprint in self.sprints:
            self.state.set_sprint_info(current=sprint.number, total=len(self.sprints))
            elapsed_total = _elapsed(run_start)
            print(f"\n{_C.BOLD}{'─' * 60}")
            _log("Harness", f"Sprint {sprint.number}/{len(self.sprints)}: {sprint.name} (elapsed: {elapsed_total})")
            print(f"{'─' * 60}{_C.RESET}")

            sprint_start = time.time()
            contract_path = await self.negotiate_contract(sprint)
            passed = await self._retry_build_eval(sprint, contract_path, f"Sprint {sprint.number}")

            # If backend passed but frontend design failed, enter design refinement
            if not passed:
                sprint_dir = self._sprint_dir(sprint)
                eval_path = os.path.join(sprint_dir, "evaluation.md")
                eval_text = _read_file_optional(eval_path)
                if eval_text:
                    fe_pass, be_pass = _parse_failed_parts(eval_text)
                    if be_pass and not fe_pass:
                        _log("Harness", "Backend passed but design failed — entering design refinement.")
                        passed = await self._design_refinement(sprint, contract_path)
                        if passed:
                            self.state.set_sprint_result(sprint.number, True)

            # Sprint summary
            sprint_time = _elapsed(sprint_start)
            attempts = self.state.get_sprint_attempt(sprint.number, "build")
            icon = f"{_C.GREEN}PASS{_C.RESET}" if passed else f"{_C.RED}FAIL{_C.RESET}"
            _log("Harness", f"Sprint {sprint.number} complete: {icon} ({attempts} attempt(s), {sprint_time})")

            if not passed:
                _log("Harness", f"Sprint {sprint.number} incomplete — continuing to next sprint.")

        # Final integration evaluation across all sprints
        await self._integration_eval()

    async def _integration_eval(self):
        """Final integration evaluation across all sprints with build-fix-eval loop."""
        print(f"\n{_C.BOLD}{'─' * 60}")
        _log("Harness", "Final Integration Evaluation")
        print(f"{'─' * 60}{_C.RESET}")
        max_attempts = CONFIG["max_build_attempts"]
        eval_path = os.path.join(self.comms_dir, "integration_evaluation.md")

        for attempt in range(1, max_attempts + 1):
            # --- Evaluate ---
            _log("Evaluator", f"Integration eval (attempt {attempt}/{max_attempts})...")
            self.server.start()
            start = time.time()
            try:
                # Previous evaluation for regression comparison
                prev_eval_section = ""
                if attempt > 1:
                    prev_attempt_path = os.path.join(
                        self.comms_dir, f"integration_evaluation_attempt_{attempt - 1}.md"
                    )
                    prev_eval = _read_file_optional(prev_attempt_path)
                    if prev_eval and _count_criteria(prev_eval) > 0:
                        prev_eval_section = (
                            f"\n\n## Previous Evaluation (attempt {attempt - 1})\n\n"
                            f"Compare your findings against this previous evaluation. "
                            f"Label regressions (was PASS, now FAIL), fixes (was FAIL, now PASS), "
                            f"and persistent failures. Prioritize regressions in Required Changes.\n\n"
                            f"Previous evaluation:\n{prev_eval}"
                        )

                prompt = (
                    f"Final Integration Evaluation.\n\n"
                    f"All sprints have been built. Evaluate the COMPLETE application end-to-end.\n\n"
                    f"Full product spec:\n{self.spec}\n\n"
                    f"Navigate to {CONFIG['dev_server_url']}. "
                    f"Test the full user journey across all features — not just individual sprints. "
                    f"Verify that features from different sprints work together correctly. "
                    f"Take screenshots as evidence. "
                    f"Write your evaluation as a response."
                    f"{prev_eval_section}"
                )
                eval_result = await call_agent(
                    system_prompt=self.evaluator_prompt,
                    user_prompt=prompt,
                    allowed_tools=TOOLS_EVALUATOR,
                    mcp_servers=CONFIG.get("mcp_servers"),
                    cwd=self.root,
                    timeout=CONFIG["agent_timeout_integration"],
                    model=resolve_agent_model("evaluator"),
                )
                self._track_cost(eval_result, "integration_eval")

                # Preserve detailed evaluation (same logic as evaluate())
                agent_response = eval_result.result
                disk_eval = _read_file_optional(eval_path)
                _, response_criteria = _parse_eval_score(agent_response)
                _, disk_criteria = _parse_eval_score(disk_eval)

                if disk_criteria > response_criteria:
                    best_eval = disk_eval
                    _log("Evaluator", f"Integration — Preserved disk evaluation "
                         f"({disk_criteria} criteria) over agent response ({response_criteria} criteria)")
                else:
                    best_eval = agent_response
                    with open(eval_path, "w") as f:
                        f.write(agent_response)

                _save_agent_response(
                    self.comms_dir,
                    f"integration_evaluation_attempt_{attempt}.md",
                    best_eval,
                )
            except AgentTimeout as exc:
                _log("Harness", f"Integration eval attempt {attempt} TIMEOUT: {exc}")
                _log("Harness", f"Agent timed out — stopping integration eval.")
                self.state.set_sprint_result(0, False)
                return
            except AgentError as exc:
                _log("Harness", f"Integration eval attempt {attempt} error: {exc}")
                continue
            finally:
                self.server.stop()

            passed = _check_verdict_pass(best_eval)
            stats = _fmt_stats(eval_result, start)

            # Track integration eval score
            eval_passed, eval_total = _parse_eval_score(best_eval)
            if eval_total > 0:
                pct = int(eval_passed / eval_total * 100)
                self.state.add_eval_score(0, attempt, eval_passed, eval_total)
                _log("Evaluator", f"Integration — Score: {eval_passed}/{eval_total} ({pct}%)")

            if passed:
                _log("Evaluator", f"Integration eval — {_C.GREEN}{_C.BOLD}PASS{_C.RESET} ({stats})")
                self.state.set_sprint_result(0, True)  # 0 = integration
                return

            _log("Evaluator", f"Integration eval — {_C.RED}{_C.BOLD}FAIL{_C.RESET} ({stats})")
            _print_eval_summary(best_eval)
            _print_required_changes(best_eval)

            # --- Generator fix pass (unless last attempt) ---
            if attempt < max_attempts:
                _log("Generator", f"Integration fix (attempt {attempt}/{max_attempts})...")
                fix_start = time.time()
                fix_prompt = (
                    f"Integration evaluation FAILED. Fix the issues below.\n\n"
                    f"Evaluation feedback:\n{best_eval}\n\n"
                    f"Product spec: Read from {self.spec_path}\n\n"
                    f"Fix ALL Required Changes listed in the evaluation. "
                    f"Self-verify: build must succeed, app must run. "
                    f"Commit your changes with git."
                )
                try:
                    fix_result = await call_agent(
                        system_prompt=self.generator_prompt,
                        user_prompt=fix_prompt,
                        allowed_tools=TOOLS_FULL,
                        cwd=self.output_dir,
                        max_turns=CONFIG["generator_max_turns"],
                        resume=self._generator_session_id,
                        timeout=CONFIG["agent_timeout_build"],
                        model=resolve_agent_model("generator"),
                    )
                    if fix_result.session_id:
                        self._generator_session_id = fix_result.session_id
                    self._track_cost(fix_result, "integration_fix")
                    _log("Generator", f"Integration fix complete. ({_fmt_stats(fix_result, fix_start)})")
                    _save_agent_response(
                        self.comms_dir, f"integration_fix_{attempt}.md", fix_result.result,
                    )
                except AgentTimeout as exc:
                    self._generator_session_id = None
                    _log("Harness", f"Integration fix TIMEOUT: {exc}")
                    _log("Harness", f"Agent timed out — stopping integration eval.")
                    self.state.set_sprint_result(0, False)
                    return
                except AgentError as exc:
                    self._generator_session_id = None
                    _log("Harness", f"Integration fix error: {exc}")

        self.state.set_sprint_result(0, False)
        _log("Harness", f"Integration eval failed after {max_attempts} attempts.")
        _ask_user_continue("Integration evaluation")

    def _archive_comms(self):
        """Copy comms/ artifacts into output/{slug}/.harness/ for project-level persistence."""
        if not self.output_dir:
            return
        archive_dir = os.path.join(self.output_dir, ".harness")
        if os.path.isdir(archive_dir):
            shutil.rmtree(archive_dir)
        shutil.copytree(self.comms_dir, archive_dir)
        _log("Harness", f"Build artifacts archived to {os.path.relpath(archive_dir)}")

    async def _design_refinement(self, sprint: Sprint, contract_path: str):
        """Design-focused iteration loop (per article: 5-15 iterations for frontend design).

        Runs after backend criteria pass but frontend criteria (Design Quality,
        Originality, Craft, UI Functionality) still fail. Only touches visual/UX
        code — no backend rebuilds.
        """
        max_iters = CONFIG["max_design_iterations"]
        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        screenshots_dir = os.path.join(sprint_dir, "screenshots")
        # Use cached contract (GAN principle — same as build())
        if contract_path not in self._cached_contracts:
            self._cached_contracts[contract_path] = _read_file(contract_path)
        contract = self._cached_contracts[contract_path]

        print(f"\n{_C.BOLD}{'─' * 60}")
        _log("Harness", f"Design Refinement (up to {max_iters} iterations)")
        print(f"{'─' * 60}{_C.RESET}")

        iteration = 0
        for iteration in range(1, max_iters + 1):
            # --- Generator: design-only fix ---
            prev_eval = _read_file_optional(eval_path)
            _log("Generator", f"Design iteration {iteration}/{max_iters}...")
            fix_start = time.time()
            fix_prompt = (
                f"DESIGN REFINEMENT — backend is working, focus ONLY on visual design.\n\n"
                f"The backend criteria (Product Depth, Functionality, Code Quality) passed. "
                f"But frontend criteria failed. Fix the design issues below.\n\n"
                f"Testable criteria (the Evaluator will re-test these):\n{contract}\n\n"
                f"Previous evaluation:\n{prev_eval}\n\n"
                f"Product spec: Read from {self.spec_path}\n\n"
                f"Make a strategic decision: REFINE if the design direction is promising, "
                f"or PIVOT to an entirely different aesthetic if the current approach isn't working. "
                f"State your decision explicitly: 'STRATEGY: REFINE' or 'STRATEGY: PIVOT'.\n\n"
                f"Do NOT change backend logic, API endpoints, or database schema. "
                f"Focus on: colors, typography, layout, animations, textures, component styling. "
                f"Commit your changes with git."
            )
            try:
                fix_result = await call_agent(
                    system_prompt=self.generator_prompt,
                    user_prompt=fix_prompt,
                    allowed_tools=TOOLS_FULL,
                    cwd=self.output_dir,
                    max_turns=CONFIG["generator_max_turns"],
                    resume=self._generator_session_id,
                    timeout=CONFIG["agent_timeout_build"],
                    model=resolve_agent_model("generator"),
                )
                if fix_result.session_id:
                    self._generator_session_id = fix_result.session_id
                self._track_cost(fix_result, "design_refinement")
                _log("Generator", f"Design iteration {iteration} complete. ({_fmt_stats(fix_result, fix_start)})")
                _save_agent_response(
                    self._sprint_dir(sprint),
                    f"generator_response_design_{iteration}.md",
                    fix_result.result,
                )
            except AgentTimeout as exc:
                self._generator_session_id = None
                _log("Harness", f"Design iteration {iteration} TIMEOUT: {exc}")
                _log("Harness", f"Agent timed out — stopping design refinement.")
                break
            except AgentError as exc:
                self._generator_session_id = None
                _log("Harness", f"Design iteration {iteration} error: {exc}")
                continue

            # --- Evaluator: re-assess ---
            _log("Evaluator", f"Design eval {iteration}/{max_iters}...")
            self.server.start()
            eval_start = time.time()
            try:
                eval_prompt = (
                    f"Design Refinement Evaluation (iteration {iteration}).\n\n"
                    f"The backend is working. Focus your assessment on frontend design criteria: "
                    f"Design Quality, Originality, Craft, UI Functionality.\n\n"
                    f"Sprint contract:\n{contract}\n\n"
                    f"Product spec:\n{self.spec}\n\n"
                    f"Navigate to {CONFIG['dev_server_url']}. "
                    f"Evaluate the visual design against the spec's design language. "
                    f"Also verify that backend functionality was NOT broken by the design changes. "
                    f"Take screenshots — save to {screenshots_dir}/. "
                    f"Write your evaluation as a response."
                )
                eval_result = await call_agent(
                    system_prompt=self.evaluator_prompt,
                    user_prompt=eval_prompt,
                    allowed_tools=TOOLS_EVALUATOR,
                    mcp_servers=CONFIG.get("mcp_servers"),
                    cwd=self.root,
                    timeout=CONFIG["agent_timeout_eval"],
                    model=resolve_agent_model("evaluator"),
                )
                self._track_cost(eval_result, "design_eval")
                with open(eval_path, "w") as f:
                    f.write(eval_result.result)
                _save_agent_response(
                    self._sprint_dir(sprint),
                    f"evaluation_design_{iteration}.md",
                    eval_result.result,
                )
            except AgentTimeout as exc:
                _log("Harness", f"Design eval {iteration} TIMEOUT: {exc}")
                _log("Harness", f"Agent timed out — stopping design refinement.")
                break
            except AgentError as exc:
                _log("Harness", f"Design eval {iteration} error: {exc}")
                continue
            finally:
                self.server.stop()

            passed = _check_verdict_pass(eval_result.result)
            stats = _fmt_stats(eval_result, eval_start)
            if passed:
                _log("Evaluator", f"Design eval {iteration} — {_C.GREEN}{_C.BOLD}PASS{_C.RESET} ({stats})")
                self.state.set_sprint_result(sprint.number, True)
                return True

            fe_pass, _ = _parse_failed_parts(eval_result.result)
            _log("Evaluator", f"Design eval {iteration} — {_C.RED}{_C.BOLD}FAIL{_C.RESET} ({stats})")
            _print_eval_summary(eval_result.result)
            if not fe_pass:
                _print_required_changes(eval_result.result)
            else:
                # Frontend passed but backend regressed — stop design loop
                _log("Harness", "Frontend design passed but backend regressed. Exiting design loop.")
                break

        _log("Harness", f"Design refinement ended after {iteration} iteration(s).")
        _ask_user_continue("Design refinement")
        return False

    async def _review_criteria(self, criteria_path: str) -> str:
        """Generator reviews criteria before building (lightweight contract review).

        Article principle: Generator should understand and vet criteria before building.
        Returns path to the (possibly updated) criteria file.
        """
        criteria_text = _read_file(criteria_path)
        criteria_count = _count_criteria(criteria_text)
        _log("Generator", f"Reviewing {criteria_count} testable criteria...")
        start = time.time()

        prompt = (
            f"Review these testable criteria BEFORE you start building. "
            f"You will be evaluated against each one.\n\n"
            f"Criteria:\n{criteria_text}\n\n"
            f"For each criterion, briefly assess:\n"
            f"1. Is it clear what to build and how to verify it?\n"
            f"2. Are there any that conflict with each other?\n"
            f"3. Are there any that are technically impossible to implement?\n\n"
            f"Write a brief review (10-20 lines). Flag any issues. "
            f"Then write ACKNOWLEDGED to confirm you understand the full scope.\n\n"
            f"Do NOT start building yet. Just review and acknowledge."
        )

        try:
            result = await call_agent(
                system_prompt=self.generator_prompt,
                user_prompt=prompt,
                allowed_tools=TOOLS_READ_WRITE,
                cwd=self.output_dir or self.root,
                timeout=CONFIG.get("agent_timeout_negotiate", 300),
                model=resolve_agent_model("generator"),
            )
            # Start the Generator's continuous session from this review
            if result.session_id:
                self._generator_session_id = result.session_id
            self._track_cost(result, "criteria_review")
            _log("Generator", f"Criteria review complete. ({_fmt_stats(result, start)})")
        except (AgentError, AgentTimeout) as exc:
            _log("Harness", f"Criteria review failed ({exc}), proceeding with original criteria.")

        return criteria_path

    async def _run_simple(self):
        single_sprint = Sprint(number=1, name="Full Build")
        self.state.set_sprint_info(current=1, total=1)
        _log("Harness", "=== Simple mode: build + evaluation ===")

        # Generate testable criteria from spec (replaces contract negotiation)
        criteria_path = await self.generate_criteria()

        sprint_dir = self._sprint_dir(single_sprint)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        # Copy criteria to sprint dir as the contract
        shutil.copy(criteria_path, contract_path)

        # Generator reviews criteria before building (article: contract negotiation)
        await self._review_criteria(contract_path)

        passed = await self._retry_build_eval(single_sprint, contract_path, "Simple mode")

        # If backend passed but frontend design failed, enter design refinement loop
        if not passed:
            eval_path = os.path.join(sprint_dir, "evaluation.md")
            eval_text = _read_file_optional(eval_path)
            if eval_text:
                fe_pass, be_pass = _parse_failed_parts(eval_text)
                if be_pass and not fe_pass:
                    _log("Harness", "Backend passed but frontend design failed — entering design refinement.")
                    if await self._design_refinement(single_sprint, contract_path):
                        self.state.set_sprint_result(single_sprint.number, True)

    def _print_report(self, total_start: float):
        status = self.state.load()
        print("\n" + "=" * 60)
        print("  FINAL REPORT")
        print("=" * 60)
        print(f"  Project:        {self.output_dir}")
        print(f"  Mode:           {self._mode}")
        print(f"  Sprints:        {status.get('total_sprints', 1)}")
        print(f"  Build attempts: {self.state.total('build', status)}")
        print(f"  Eval attempts:  {self.state.total('eval', status)}")

        sprint_results = self.state.get_sprint_results(status)
        timings = status.get("timings", {})
        sprint_timings = {st["sprint"]: st for st in timings.get("sprints", [])}

        # Planning
        if timings.get("plan"):
            print(f"  Planning:       {_elapsed_secs(timings['plan'])}")

        # Per-sprint: result + timing combined
        for num in sorted(sprint_results):
            passed = sprint_results[num]
            color = _C.GREEN if passed else _C.RED
            icon = f"{color}{_C.BOLD}{'PASS' if passed else 'FAIL'}{_C.RESET}"
            if num == 0:
                label = "Integration"
            elif self._mode == "simple":
                label = "Build"
            else:
                label = f"Sprint {num}"

            st = sprint_timings.get(num, {})
            parts = []
            if "negotiate" in st:
                parts.append(f"negotiate {_elapsed_secs(st['negotiate'])}")
            if "build" in st:
                parts.append(f"build {' + '.join(_elapsed_secs(b) for b in st['build'])}")
            if "eval" in st:
                parts.append(f"eval {' + '.join(_elapsed_secs(e) for e in st['eval'])}")
            timing_str = f" ({', '.join(parts)})" if parts else ""

            print(f"  {label:14s} {icon}{timing_str}")

        # Cost
        cost = status.get("cost", {})
        total_usd = cost.get("total_usd", 0)
        if total_usd > 0:
            print(f"  Cost:           ${total_usd:.2f}")
        input_t = cost.get("input_tokens", 0)
        output_t = cost.get("output_tokens", 0)
        if input_t or output_t:
            print(f"  Tokens:         {input_t:,} in / {output_t:,} out")
            by_phase = cost.get("by_phase", {})
            for phase, pcost in sorted(by_phase.items()):
                pi = pcost.get("input_tokens", 0)
                po = pcost.get("output_tokens", 0)
                print(f"    {phase:16s} {pi:,} in / {po:,} out")

        print(f"  Total time:     {_elapsed(total_start)}")
        print("=" * 60)

        # Next Steps
        if self.output_dir and os.path.isdir(self.output_dir):
            print(f"\n{_C.BOLD}  Next Steps{_C.RESET}")
            print(f"  {'─' * 40}")
            rel_output = os.path.relpath(self.output_dir)
            # Detect project structure for run commands
            has_backend = os.path.isdir(os.path.join(self.output_dir, "backend"))
            has_frontend = os.path.isdir(os.path.join(self.output_dir, "frontend"))
            has_root_pkg = os.path.exists(os.path.join(self.output_dir, "package.json"))

            if has_backend and has_frontend:
                print(f"    cd {rel_output}")
                print(f"    cd backend && pip install -r requirements.txt && cd ..")
                print(f"    cd frontend && npm install && cd ..")
                print(f"    # Start servers:")
                print(f"    cd backend && uvicorn main:app --reload --port 8006 &")
                print(f"    cd frontend && npm run dev -- --port 8005")
            elif has_root_pkg:
                print(f"    cd {rel_output}")
                print(f"    npm install && npm run dev")
            else:
                print(f"    cd {rel_output}")

            print(f"\n    # Or use the serve command:")
            print(f"    python orchestrator.py serve")
            print()


def _print_spec_summary(spec: str, sprints: list):
    """Print key info from the generated spec."""
    # Product name
    match = re.search(r"^#\s+(.+)$", spec, re.MULTILINE)
    name = match.group(1).strip() if match else "Unknown"

    # Count features (### P0/P1/P2 sections)
    features = len(re.findall(r"^### P\d+:", spec, re.MULTILINE))

    # Aesthetic direction
    direction = ""
    in_aesthetic = False
    for line in spec.split("\n"):
        stripped = line.strip()
        if "aesthetic direction" in stripped.lower():
            in_aesthetic = True
            continue
        if in_aesthetic and stripped and not stripped.startswith("#"):
            direction = stripped[:60]
            break

    print(f"    {_C.DIM}Product: {name}{_C.RESET}")
    print(f"    {_C.DIM}Features: {features} (P0/P1/P2){_C.RESET}")
    if direction:
        print(f"    {_C.DIM}Design: {direction}{_C.RESET}")
    sprint_names = ", ".join(f"{s.number}:{s.name}" for s in sprints)
    print(f"    {_C.DIM}Sprints: {sprint_names}{_C.RESET}")


def _print_build_summary(output_dir: str):
    """Print a brief summary of what exists in the output directory."""
    if not output_dir or not os.path.isdir(output_dir):
        return
    has_backend = os.path.isdir(os.path.join(output_dir, "backend"))
    has_frontend = os.path.isdir(os.path.join(output_dir, "frontend"))

    # Count git commits
    count_result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=output_dir, capture_output=True, text=True,
    )
    total_commits = int(count_result.stdout.strip()) if count_result.returncode == 0 and count_result.stdout.strip() else 0
    result = subprocess.run(
        ["git", "log", "--oneline", "-3"],
        cwd=output_dir, capture_output=True, text=True,
    )
    commits = result.stdout.strip().split("\n") if result.returncode == 0 and result.stdout.strip() else []

    parts = []
    if has_frontend:
        parts.append("frontend")
    if has_backend:
        parts.append("backend")
    stack = " + ".join(parts) if parts else "project"

    print(f"    {_C.DIM}Stack: {stack}{_C.RESET}")
    if total_commits:
        print(f"    {_C.DIM}Commits: {total_commits}{_C.RESET}")
        for c in commits:
            print(f"    {_C.DIM}  {c}{_C.RESET}")


def _print_eval_summary(eval_text: str):
    """Print feature pass rate and design assessment from evaluation."""
    # Feature pass rate
    rate_match = re.search(r"Feature Pass Rate:\s*(\d+/\d+\s*\(\d+%?\))", eval_text)
    if rate_match:
        print(f"    {_C.DIM}Features: {rate_match.group(1)}{_C.RESET}")

    # Two-part assessment — extract verdicts from both Frontend and Backend tables
    criteria = [
        "Design Quality", "Originality", "Craft", "UI Functionality",  # Frontend
        "Product Depth", "Functionality", "Code Quality",              # Backend
    ]
    verdicts = []
    for c in criteria:
        match = re.search(rf"\|\s*{c}\s*\|\s*(PASS|FAIL)\s*\|", eval_text)
        if match:
            v = match.group(1)
            color = _C.GREEN if v == "PASS" else _C.RED
            verdicts.append(f"{c}: {color}{v}{_C.RESET}")
    if verdicts:
        print(f"    {_C.DIM}{' | '.join(verdicts)}{_C.RESET}")


def _print_contract_summary(contract_path: str):
    """Print a brief summary of the agreed sprint contract."""
    text = _read_file_optional(contract_path)
    if not text:
        return

    # Extract scope line
    scope = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith("**scope") or stripped.lower().startswith("scope"):
            scope = re.sub(r"^\*{0,2}scope\*{0,2}[:\s]*", "", stripped, flags=re.IGNORECASE).strip()
            break

    # Count criteria (lines starting with a number or dash-bracket pattern)
    criteria_count = 0
    in_criteria = False
    for line in text.split("\n"):
        stripped = line.strip()
        if "testable criteria" in stripped.lower() or "criteria" in stripped.lower() and "##" in stripped:
            in_criteria = True
            continue
        if in_criteria and stripped.startswith("#"):
            in_criteria = False
        if in_criteria and stripped and (stripped[0].isdigit() or stripped.startswith("- [")):
            criteria_count += 1

    # Extract design decisions (first line only)
    design = ""
    in_design = False
    for line in text.split("\n"):
        stripped = line.strip()
        if "design decision" in stripped.lower():
            in_design = True
            continue
        if in_design and stripped.startswith("#"):
            break
        if in_design and stripped and not design:
            design = re.sub(r"^[-*]\s*", "", stripped)[:80]

    parts = []
    if scope:
        parts.append(f"Scope: {scope[:80]}")
    if criteria_count:
        parts.append(f"Criteria: {criteria_count} testable items")
    if design:
        parts.append(f"Design: {design}")

    for part in parts:
        print(f"    {_C.DIM}{part}{_C.RESET}")


def _print_required_changes(text: str):
    """Extract and print Required Changes from evaluation text."""
    in_changes = False
    for line in text.split("\n"):
        stripped = line.strip()
        if "Required Changes" in stripped:
            in_changes = True
            continue
        if in_changes and stripped.startswith("#"):
            break
        if in_changes and stripped and stripped[0].isdigit():
            # Truncate long lines for readability
            display = stripped[:200] + "..." if len(stripped) > 200 else stripped
            print(f"    {_C.RED}{display}{_C.RESET}")


def _fmt_tokens_ar(agent_result) -> str:
    return fmt_tokens(agent_result.input_tokens + agent_result.output_tokens)


def _fmt_cost(agent_result) -> str:
    cost = getattr(agent_result, "cost_usd", 0) or 0
    return f"${cost:.2f}" if cost > 0 else ""


def _fmt_stats(agent_result, start: float) -> str:
    parts = [_elapsed(start), f"{agent_result.turns} turns"]
    tok = _fmt_tokens_ar(agent_result)
    if tok != "0":
        parts.append(f"{tok} tokens")
    cost = _fmt_cost(agent_result)
    if cost:
        parts.append(cost)
    return ", ".join(parts)


# --- ANSI colors ---
class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

_AGENT_COLORS = {
    "Planner": _C.CYAN,
    "Contract": _C.MAGENTA,
    "Generator": _C.BLUE,
    "Evaluator": _C.YELLOW,
    "Harness": _C.DIM,
}


class UserAbort(Exception):
    """Raised when the user chooses to abort the harness."""


def _ask_user_continue(label: str):
    """Ask the user whether to continue after max failures. Raises UserAbort to stop."""
    print(f"\n{_C.RED}{'=' * 60}")
    print(f"  {label} failed after {CONFIG['max_build_attempts']} attempts.")
    print(f"{'=' * 60}{_C.RESET}")
    choice = _prompt_choice("Continue or abort?", [
        ("c", "Continue to next sprint"),
        ("a", "Abort the harness"),
    ])
    if choice == "a":
        _log("Harness", "User chose to abort.")
        raise UserAbort()
    _log("Harness", "User chose to continue.")


_log_file = None


def _log_init(path: str):
    global _log_file
    _log_file = open(path, "a")


def _log_close():
    global _log_file
    if _log_file:
        _log_file.close()
        _log_file = None


def _log(agent: str, msg: str):
    color = _AGENT_COLORS.get(agent, "")
    print(f"{color}{_C.BOLD}[{agent}]{_C.RESET} {msg}")
    if _log_file:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        # Strip ANSI codes for file output
        clean = re.sub(r"\033\[[0-9;]*m", "", msg)
        _log_file.write(f"[{ts}] [{agent}] {clean}\n")
        _log_file.flush()


def _cleanup_playwright():
    """Kill zombie Chromium/Playwright processes left by MCP servers."""
    try:
        subprocess.run(
            "pkill -f 'chromium.*--headless' 2>/dev/null; "
            "pkill -f 'playwright' 2>/dev/null",
            shell=True, capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _save_agent_response(directory: str, filename: str, text: str):
    """Save an agent's full response text for post-run analysis."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, filename)
    with open(path, "w") as f:
        f.write(text)
    _log("Harness", f"Agent response saved to {os.path.relpath(path)}")


def _elapsed(start: float) -> str:
    return _elapsed_secs(int(time.time() - start))


_elapsed_secs = fmt_elapsed


def _ok(name: str) -> str:
    return f"  {_C.GREEN}✓{_C.RESET} {name}"


def _fail(name: str, hint: str) -> str:
    return f"  {_C.RED}✗{_C.RESET} {name} — {hint}"


def _is_ci() -> bool:
    return os.environ.get("CI", "").lower() in ("1", "true", "yes")


def _prompt_yn(question: str, default: bool = True) -> bool:
    if _is_ci():
        return False
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"  {question} {hint}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer:
        return default
    return answer.startswith("y")


def _prompt_choice(question: str, options: list[tuple[str, str]]) -> str:
    """Prompt user to pick from options. Returns the key."""
    if _is_ci():
        return options[0][0]
    for key, label in options:
        print(f"    [{key}] {label}")
    try:
        answer = input(f"  {question}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return options[0][0]
    for key, _ in options:
        if answer.startswith(key):
            return key
    return options[0][0]


# --- Dependency checks ---

def _check_auth_mode() -> tuple[str, str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        return "api", masked

    result = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    if result.returncode != 0:
        return "none", ""

    version = result.stdout.strip() or "installed"
    try:
        result = subprocess.run(
            ["claude", "-p", "echo ok", "--max-turns", "1"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return "oauth", version
    except subprocess.TimeoutExpired:
        pass
    return "cli_no_auth", version


# --- Fix actions ---

def _fix_pip():
    print(f"  {_C.DIM}→ Installing claude-agent-sdk...{_C.RESET}")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "claude-agent-sdk"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(_ok("claude_agent_sdk installed"))
        return True
    print(_fail("pip install", result.stderr.strip().split("\n")[-1]))
    return False


def _fix_auth() -> bool:
    choice = _prompt_choice("Choose auth method", [
        ("o", "OAuth login (uses subscription quota)"),
        ("a", "API key (pay per use, no quota limit)"),
    ])
    if choice == "o":
        print(f"  {_C.DIM}→ Running: claude login{_C.RESET}")
        result = subprocess.run(["claude", "login"], text=True)
        if result.returncode == 0:
            print(_ok("OAuth authenticated"))
            return True
        print(_fail("claude login", "Login failed. Try again manually: claude login"))
        return False
    else:
        try:
            key = input("  Enter ANTHROPIC_API_KEY: ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if not key:
            return False
        # Validate key format
        if not key.startswith("sk-ant-"):
            print(_fail("API key", "Invalid format. Key should start with sk-ant-"))
            return False
        # Write to shell profile for persistence
        shell = os.environ.get("SHELL", "/bin/bash")
        profile = os.path.expanduser("~/.zshrc" if "zsh" in shell else "~/.bashrc")
        export_line = f'export ANTHROPIC_API_KEY="{key}"'

        # Set in current process
        os.environ["ANTHROPIC_API_KEY"] = key

        if _prompt_yn(f"Add to {profile} for persistence?"):
            with open(profile, "a") as f:
                f.write(f"\n# Added by Idle Harness setup\n{export_line}\n")
            print(_ok(f"API key saved to {profile}"))
        else:
            print(_ok("API key set for this session only"))
        return True


# --- Main preflight ---

def _preflight(force_setup: bool = False):
    """Check dependencies. In interactive mode, offer to fix issues automatically."""
    is_ci = _is_ci()
    print(f"{_C.BOLD}Preflight checks{_C.RESET}")
    issues = []  # list of (name, hint, fix_fn)

    # 1. Python package
    try:
        import claude_agent_sdk  # noqa: F401
        print(_ok("claude_agent_sdk"))
    except ImportError:
        hint = "Not installed"
        print(_fail("claude_agent_sdk", hint))
        issues.append(("claude_agent_sdk", hint, _fix_pip))

    # 2. CLI tools (can't auto-install, just detect)
    for cmd, install_hint in [
        ("node", "Install Node.js 18+: https://nodejs.org"),
        ("npm", "Install Node.js 18+: https://nodejs.org"),
        ("git", "Install git: https://git-scm.com"),
    ]:
        result = subprocess.run(["which", cmd], capture_output=True, text=True)
        if result.returncode == 0:
            # Get version
            ver = subprocess.run([cmd, "--version"], capture_output=True, text=True)
            ver_str = ver.stdout.strip().split("\n")[0] if ver.returncode == 0 else ""
            print(_ok(f"{cmd} ({ver_str})" if ver_str else cmd))
        else:
            print(_fail(cmd, install_hint))
            issues.append((cmd, install_hint, None))  # None = can't auto-fix

    # 3. Claude CLI existence
    cli_result = subprocess.run(["which", "claude"], capture_output=True, text=True)
    has_cli = cli_result.returncode == 0
    if not has_cli:
        hint = "Not found. Install: https://docs.anthropic.com/en/docs/claude-code"
        print(_fail("claude cli", hint))
        issues.append(("claude cli", hint, None))

    # 4. Auth: OAuth or API key
    auth_mode, auth_detail = _check_auth_mode()
    if auth_mode == "api":
        print(_ok(f"auth: API key ({auth_detail})"))
    elif auth_mode == "oauth":
        print(_ok(f"auth: OAuth ({auth_detail})"))
    elif auth_mode == "cli_no_auth":
        hint = "Claude CLI found but not authenticated"
        print(_fail("auth", hint))
        issues.append(("auth", hint, _fix_auth if has_cli else None))
    else:
        hint = "No auth configured"
        print(_fail("auth", hint))
        issues.append(("auth", hint, _fix_auth if has_cli else None))

    # 5. MCP: SDK launches Playwright MCP automatically — just need npx
    npx_result = subprocess.run(["which", "npx"], capture_output=True, text=True)
    if npx_result.returncode == 0:
        print(_ok(f"MCP: {CONFIG['mcp_tool']} (SDK-managed via npx)"))
    else:
        hint = "npx not found. Install Node.js 18+: https://nodejs.org"
        print(_fail(f"MCP: {CONFIG['mcp_tool']}", hint))
        issues.append(("npx", hint, None))

    # All good
    if not issues:
        print(f"\n{_C.GREEN}{_C.BOLD}All checks passed{_C.RESET}\n")
        return

    # CI mode: fail hard
    if is_ci:
        print(f"\n{_C.RED}{_C.BOLD}Preflight failed — {len(issues)} error(s){_C.RESET}")
        sys.exit(1)

    # Interactive: offer to fix
    fixable = [(name, hint, fix) for name, hint, fix in issues if fix is not None]
    unfixable = [(name, hint) for name, hint, fix in issues if fix is None]

    if unfixable:
        print(f"\n  {_C.YELLOW}Manual steps required:{_C.RESET}")
        for name, hint in unfixable:
            print(f"    • {name}: {hint}")

    if fixable:
        if force_setup or _prompt_yn(f"\n  Fix {len(fixable)} issue(s) automatically?"):
            print()
            remaining_errors = []
            for name, hint, fix_fn in fixable:
                if not fix_fn():
                    remaining_errors.append(name)
            if remaining_errors:
                print(f"\n{_C.RED}{_C.BOLD}Could not fix: {', '.join(remaining_errors)}{_C.RESET}")
                sys.exit(1)
            if unfixable:
                print(f"\n{_C.YELLOW}Fix the manual steps above, then re-run.{_C.RESET}")
                sys.exit(1)
            print(f"\n{_C.GREEN}{_C.BOLD}All issues fixed{_C.RESET}\n")
            return
        else:
            print(f"\n{_C.RED}{_C.BOLD}Preflight failed — fix issues and re-run{_C.RESET}")
            sys.exit(1)

    # Only unfixable issues remain
    print(f"\n{_C.RED}{_C.BOLD}Preflight failed — fix the above issues and re-run{_C.RESET}")
    sys.exit(1)


def _cmd_serve():
    """Start the last-built app's dev servers."""
    harness_root = get_harness_root()
    comms_dir = os.path.join(harness_root, CONFIG["comms_dir"])
    output_base = os.path.join(harness_root, CONFIG["output_dir"])

    spec_path = os.path.join(comms_dir, "spec.md")
    try:
        spec = _read_file(spec_path)
    except FileNotFoundError:
        print(f"{_C.RED}No previous build found. Run the harness first.{_C.RESET}")
        sys.exit(1)

    slug = _slug_from_spec(spec)
    output_dir = os.path.join(output_base, slug)
    if not os.path.isdir(output_dir):
        print(f"{_C.RED}Output directory not found: {output_dir}{_C.RESET}")
        sys.exit(1)

    server = _make_server(comms_dir, output_dir)
    print(f"{_C.BOLD}Starting {slug}...{_C.RESET}")
    try:
        server.start()
        url = CONFIG["dev_server_url"]
        print(f"\n{_C.GREEN}{_C.BOLD}App running at {url}{_C.RESET}")
        print(f"{_C.DIM}Press Ctrl+C to stop{_C.RESET}\n")
        subprocess.run(["open", url], capture_output=True)
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n{_C.DIM}Stopping servers...{_C.RESET}")
        server.stop()
        print("Stopped.")


def _cmd_clean(clean_all: bool = False):
    """Clean comms/ artifacts. With --all, also cleans output/."""
    harness_root = get_harness_root()
    comms_dir = os.path.join(harness_root, CONFIG["comms_dir"])
    output_base = os.path.join(harness_root, CONFIG["output_dir"])

    print(f"{_C.BOLD}Cleaning harness artifacts{_C.RESET}")

    # Clean comms/
    if os.path.isdir(comms_dir):
        files = [f for f in os.listdir(comms_dir) if not f.startswith(".")]
        shutil.rmtree(comms_dir)
        print(f"  {_C.GREEN}✓{_C.RESET} comms/ removed ({len(files)} item(s))")
    else:
        print(f"  {_C.DIM}– comms/ already empty{_C.RESET}")

    # Clean output/
    if clean_all:
        if os.path.isdir(output_base):
            projects = [d for d in os.listdir(output_base)
                        if os.path.isdir(os.path.join(output_base, d))]
            # Force remove — output/ contains git repos with locked .git dirs
            subprocess.run(["rm", "-rf", output_base], capture_output=True)
            if projects:
                print(f"  {_C.GREEN}✓{_C.RESET} output/ removed ({len(projects)} project(s): {', '.join(projects)})")
            else:
                print(f"  {_C.GREEN}✓{_C.RESET} output/ removed (empty)")
        else:
            print(f"  {_C.DIM}– output/ already empty{_C.RESET}")

    print()


def _cmd_verify():
    """Run deterministic verifier on the last build without rebuilding."""
    harness_root = get_harness_root()
    orch = Orchestrator(root_dir=harness_root)

    if not os.path.exists(orch.spec_path):
        print(f"{_C.RED}No spec found. Run the harness first.{_C.RESET}")
        sys.exit(1)

    orch._spec = _read_file(orch.spec_path)
    orch._init_output()

    sprint = Sprint(number=1, name="Full Build")
    sprint_dir = orch._sprint_dir(sprint)
    contract_path = os.path.join(sprint_dir, "sprint_contract.md")
    if not os.path.exists(contract_path):
        print(f"{_C.RED}No contract found at {contract_path}. Run the harness first.{_C.RESET}")
        sys.exit(1)

    async def _run():
        _log("Harness", "=== Verify-only mode ===")
        vresult = await orch.verify(sprint, contract_path)
        if vresult is None:
            _log("Harness", "Verifier is disabled in config.")
            return
        if vresult.overall_pass:
            _log("Harness", f"{_C.GREEN}PASS{_C.RESET} — {len(vresult.passed)} deterministic checks passed, "
                 f"{len(vresult.subjective)} subjective deferred")
        else:
            _log("Harness", f"{_C.RED}FAIL{_C.RESET} — {len(vresult.failed)} failed, "
                 f"{len(vresult.passed)} passed")
            for r in vresult.failed:
                _log("Verifier", f"  FAIL: [{r.check_type.value}] {r.criterion}")
                if r.message:
                    _log("Verifier", f"        {r.message}")

    _preflight()
    anyio.run(_run)
    _log_close()


def _cmd_eval():
    """Re-evaluate the last build without rebuilding. Uses existing comms/ and output/."""
    harness_root = get_harness_root()
    orch = Orchestrator(root_dir=harness_root)

    # Restore state from existing artifacts
    if not os.path.exists(orch.spec_path):
        print(f"{_C.RED}No spec found. Run the harness first.{_C.RESET}")
        sys.exit(1)

    orch._spec = _read_file(orch.spec_path)
    orch._init_output()

    sprint = Sprint(number=1, name="Full Build")
    sprint_dir = orch._sprint_dir(sprint)
    contract_path = os.path.join(sprint_dir, "sprint_contract.md")
    if not os.path.exists(contract_path):
        print(f"{_C.RED}No contract found at {contract_path}. Run the harness first.{_C.RESET}")
        sys.exit(1)

    async def _run():
        _log("Harness", "=== Eval-only mode ===")
        passed = await orch.evaluate(sprint, contract_path)
        icon = f"{_C.GREEN}PASS{_C.RESET}" if passed else f"{_C.RED}FAIL{_C.RESET}"
        _log("Harness", f"Result: {icon}")

    _preflight()
    anyio.run(_run)
    _log_close()


def _cmd_build():
    """Re-build from existing spec and criteria without re-planning."""
    harness_root = get_harness_root()
    orch = Orchestrator(root_dir=harness_root)

    if not os.path.exists(orch.spec_path):
        print(f"{_C.RED}No spec found. Run the harness first.{_C.RESET}")
        sys.exit(1)

    orch._spec = _read_file(orch.spec_path)
    orch._init_output()

    sprint = Sprint(number=1, name="Full Build")
    sprint_dir = orch._sprint_dir(sprint)
    contract_path = os.path.join(sprint_dir, "sprint_contract.md")
    if not os.path.exists(contract_path):
        print(f"{_C.RED}No contract found. Run the harness first.{_C.RESET}")
        sys.exit(1)

    async def _run():
        _log("Harness", "=== Build-only mode ===")
        await orch.build(sprint, contract_path)

    _preflight()
    anyio.run(_run)
    _log_close()


def _cmd_criteria():
    """Re-generate testable criteria from existing spec."""
    harness_root = get_harness_root()
    orch = Orchestrator(root_dir=harness_root)

    if not os.path.exists(orch.spec_path):
        print(f"{_C.RED}No spec found. Run the harness first or use 'plan' first.{_C.RESET}")
        sys.exit(1)

    orch._spec = _read_file(orch.spec_path)
    orch._init_output()

    async def _run():
        _log("Harness", "=== Criteria-only mode ===")
        criteria_path = await orch.generate_criteria()
        _log("Harness", f"Criteria written to {criteria_path}")

    _preflight()
    anyio.run(_run)
    _log_close()


def _cmd_plan(user_prompt: str):
    """Run planner only — generate spec from prompt."""
    harness_root = get_harness_root()
    orch = Orchestrator(root_dir=harness_root)
    orch.setup()

    async def _run():
        await orch.plan(user_prompt)
        _log("Harness", f"Spec written to {orch.spec_path}")

    _preflight()
    anyio.run(_run)
    _log_close()


def main():
    cmd = sys.argv[1] if len(sys.argv) >= 2 else ""

    if cmd == "--setup":
        _preflight(force_setup=True)
        print("Setup complete. Run:")
        print(f"  python orchestrator.py \"your app idea\"")
        return

    if cmd == "serve":
        _cmd_serve()
        return

    if cmd == "clean":
        clean_all = "--all" in sys.argv
        _cmd_clean(clean_all)
        return

    if cmd == "verify":
        _cmd_verify()
        return

    if cmd == "eval":
        _cmd_eval()
        return

    if cmd == "build":
        _cmd_build()
        return

    if cmd == "criteria":
        _cmd_criteria()
        return

    if cmd == "plan":
        user_prompt = " ".join(sys.argv[2:])
        if not user_prompt:
            print(f"{_C.RED}Usage: python orchestrator.py plan \"your app idea\"{_C.RESET}")
            sys.exit(1)
        _cmd_plan(user_prompt)
        return

    if not cmd or cmd.startswith("-"):
        print(f"{_C.BOLD}Idle Harness{_C.RESET} — GAN-inspired multi-agent app builder\n")
        print("Usage:")
        print(f"  python orchestrator.py \"your app idea\"    Build an app (full pipeline)")
        print()
        print(f"  {_C.BOLD}Single-process commands:{_C.RESET}")
        print(f"  python orchestrator.py plan \"idea\"         Plan only — generate spec")
        print(f"  python orchestrator.py criteria            Generate criteria from existing spec")
        print(f"  python orchestrator.py build               Build from existing spec + criteria")
        print(f"  python orchestrator.py verify              Deterministic verification (no LLM eval)")
        print(f"  python orchestrator.py eval                Evaluate existing build (no rebuild)")
        print()
        print(f"  {_C.BOLD}Utilities:{_C.RESET}")
        print(f"  python orchestrator.py serve               Start the last-built app")
        print(f"  python orchestrator.py clean               Clean comms/ artifacts")
        print(f"  python orchestrator.py clean --all         Clean comms/ and output/")
        print(f"  python orchestrator.py --setup             Interactive setup")
        print(f"\nEnvironment:")
        print(f"  CI=1                                       Non-interactive mode")
        sys.exit(0)

    _preflight()

    user_prompt = " ".join(sys.argv[1:])
    orch = Orchestrator()
    anyio.run(orch.run, user_prompt)


if __name__ == "__main__":
    main()
