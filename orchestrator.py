import anyio
import os
import re
import signal
import shutil
import subprocess
import sys
import time

from cli import call_agent, AgentError, AgentTimeout, InfraError, fmt_tokens, fmt_elapsed
from config import (
    CONFIG, CONTRACT_AGREED,
    TOOLS_READ_WRITE, TOOLS_FULL, TOOLS_EVALUATOR,
    get_harness_root,
)
from sprint import Sprint, parse_sprints
from state import HarnessState
from server import DevServer


def _check_verdict_pass(text: str) -> bool:
    """Match 'Verdict: PASS' only as a standalone markdown heading or line."""
    return bool(re.search(r"^#{0,3}\s*Verdict:\s*PASS\s*$", text, re.MULTILINE))


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
        self.output_dir = os.path.join(self.output_base, slug)
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
        for stale in ("spec.md", "dev_server.json", "integration_evaluation.md"):
            path = os.path.join(self.comms_dir, stale)
            if os.path.exists(path):
                os.remove(path)
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
        )
        self._track_cost(agent_result, "criteria")

        if not os.path.exists(criteria_path):
            raise RuntimeError(
                "Criteria generation failed — Evaluator did not write testable_criteria.md. "
                "Cannot proceed without testable criteria."
            )

        criteria_text = _read_file(criteria_path)
        # Count criteria lines and validate minimum quality
        criteria_count = _count_criteria(criteria_text)
        if criteria_count < 10:
            raise RuntimeError(
                f"Criteria generation produced only {criteria_count} criteria (minimum 10 required). "
                f"The Evaluator may have failed to parse the spec correctly."
            )
        elapsed_s = int(time.time() - start)
        _log("Evaluator", f"Generated {criteria_count} testable criteria. ({_fmt_stats(agent_result, start)})")
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

            eval_context = (
                f"\n\nPrevious evaluation feedback:\n{eval_context}\n\n"
                f"Make a strategic decision before coding:\n"
                f"**REFINE** if most criteria passed and failures are specific, fixable issues. "
                f"**PIVOT** if the evaluator described fundamental problems (generic design, "
                f"multiple simultaneous failures, or no improvement from previous attempt). "
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
                f"Do NOT stop after implementing the basic structure. Continue until every criterion "
                f"is satisfied. For complex applications this will take significant time — that is expected. "
                f"Walk through each criterion yourself before handing off. Count how many you've "
                f"satisfied vs total — if <90%, keep building.\n\n"
                f"Work in the current directory. Self-verify: build must succeed, app must run. "
                f"Commit your changes with git.\n\n"
                f"Write the dev server config to this ABSOLUTE path: {dev_server_json_path}\n\n"
                f"MANDATORY: Before finishing, write a self-evaluation to this ABSOLUTE path: "
                f"{self._sprint_dir(sprint)}/self_eval.md\n"
                f"Format: for each testable criterion, write '- [x] criterion text' if satisfied "
                f"or '- [ ] criterion text — reason' if not. Count your pass rate at the end: "
                f"'Self-eval pass rate: X/Y (Z%)'. If <90%, keep building."
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

        # Check Generator self-eval if written
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
        else:
            _log("Harness", "Generator did not write self_eval.md. Self-evaluation skipped.")

    async def evaluate(self, sprint: Sprint, contract_path: str) -> bool:
        # Use cached contract (GAN principle — same as build())
        if contract_path not in self._cached_contracts:
            self._cached_contracts[contract_path] = _read_file(contract_path)
        contract = self._cached_contracts[contract_path]

        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        screenshots_dir = os.path.join(sprint_dir, "screenshots")

        self.state.increment(sprint.number, "eval")
        attempt = self.state.get_sprint_attempt(sprint.number, "eval")

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
                )
            else:
                prompt = (
                    f"Evaluate Sprint {sprint.number}: {sprint.name}.\n\n"
                    f"Sprint contract (test against these criteria):\n{contract}\n\n"
                    f"Product spec:\n{self.spec}\n\n"
                    f"Navigate to {CONFIG['dev_server_url']}. Test every criterion in the sprint contract. "
                    f"Take screenshots as evidence — save them to {screenshots_dir}/. "
                    f"Write your evaluation as a response."
                )

            eval_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=prompt,
                allowed_tools=TOOLS_EVALUATOR,
                mcp_servers=CONFIG.get("mcp_servers"),
                cwd=self.root,
                timeout=CONFIG["agent_timeout_eval"],
            )
            self._track_cost(eval_result, "eval")

            with open(eval_path, "w") as f:
                f.write(eval_result.result)
        finally:
            _log("Evaluator", f"{label} — Stopping servers...")
            self.server.stop()

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "eval", elapsed)

        passed = _check_verdict_pass(eval_result.result)

        # Reject PASS if too many criteria were skipped as automation-limited
        if passed:
            limited, total = _parse_automation_limited(eval_result.result)
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
        _print_eval_summary(eval_result.result)
        if not passed:
            _print_required_changes(eval_result.result)
        return passed

    async def _retry_build_eval(self, sprint: Sprint, contract_path: str, label: str):
        """Shared retry loop for both full and simple modes."""
        sprint_passed = False
        for attempt in range(CONFIG["max_build_attempts"]):
            try:
                await self.build(sprint, contract_path)
                passed = await self.evaluate(sprint, contract_path)
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

            if passed:
                sprint_passed = True
                break

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

        self.setup()
        try:
            await self.plan(user_prompt)

            if CONFIG["mode"] == "simple":
                await self._run_simple()
            else:
                await self._run_full()
        except UserAbort:
            _log("Harness", "Aborted by user.")
        except (AgentError, RuntimeError) as exc:
            _log("Harness", f"Fatal error: {exc}")

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
                eval_context = _read_file_optional(eval_path)
                prev_feedback = ""
                if eval_context:
                    prev_feedback = f"\n\nPrevious evaluation feedback:\n{eval_context}"

                prompt = (
                        f"Final Integration Evaluation.\n\n"
                    f"All sprints have been built. Evaluate the COMPLETE application end-to-end.\n\n"
                    f"Full product spec:\n{self.spec}\n\n"
                    f"{prev_feedback}\n\n"
                    f"Navigate to {CONFIG['dev_server_url']}. "
                    f"Test the full user journey across all features — not just individual sprints. "
                    f"Verify that features from different sprints work together correctly. "
                    f"Take screenshots as evidence. "
                    f"Write your evaluation as a response."
                )
                eval_result = await call_agent(
                    system_prompt=self.evaluator_prompt,
                    user_prompt=prompt,
                    allowed_tools=TOOLS_EVALUATOR,
                    mcp_servers=CONFIG.get("mcp_servers"),
                    cwd=self.root,
                    timeout=CONFIG["agent_timeout_integration"],
                )
                self._track_cost(eval_result, "integration_eval")

                with open(eval_path, "w") as f:
                    f.write(eval_result.result)
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

            passed = _check_verdict_pass(eval_result.result)
            stats = _fmt_stats(eval_result, start)
            if passed:
                _log("Evaluator", f"Integration eval — {_C.GREEN}{_C.BOLD}PASS{_C.RESET} ({stats})")
                self.state.set_sprint_result(0, True)  # 0 = integration
                return

            _log("Evaluator", f"Integration eval — {_C.RED}{_C.BOLD}FAIL{_C.RESET} ({stats})")
            _print_required_changes(eval_result.result)

            # --- Generator fix pass (unless last attempt) ---
            if attempt < max_attempts:
                _log("Generator", f"Integration fix (attempt {attempt}/{max_attempts})...")
                fix_start = time.time()
                fix_prompt = (
                    f"Integration evaluation FAILED. Fix the issues below.\n\n"
                    f"Evaluation feedback:\n{eval_result.result}\n\n"
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
                    )
                    if fix_result.session_id:
                        self._generator_session_id = fix_result.session_id
                    self._track_cost(fix_result, "integration_fix")
                    _log("Generator", f"Integration fix complete. ({_fmt_stats(fix_result, fix_start)})")
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
                )
                if fix_result.session_id:
                    self._generator_session_id = fix_result.session_id
                self._track_cost(fix_result, "design_refinement")
                _log("Generator", f"Design iteration {iteration} complete. ({_fmt_stats(fix_result, fix_start)})")
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
                )
                self._track_cost(eval_result, "design_eval")
                with open(eval_path, "w") as f:
                    f.write(eval_result.result)
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
        print(f"  Mode:           {CONFIG['mode']}")
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
            elif CONFIG["mode"] == "simple":
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
                print(f"    cd backend && uvicorn main:app --reload --port 8000 &")
                print(f"    cd frontend && npm run dev")
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

    # Count recent git commits
    result = subprocess.run(
        ["git", "log", "--oneline", "-5"],
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
    if commits:
        print(f"    {_C.DIM}Recent commits: {len(commits)}{_C.RESET}")
        for c in commits[:3]:
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
        ts = time.strftime("%H:%M:%S")
        # Strip ANSI codes for file output
        clean = re.sub(r"\033\[[0-9;]*m", "", msg)
        _log_file.write(f"[{ts}] [{agent}] {clean}\n")
        _log_file.flush()


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

    cleaned = []
    # Clean comms/
    if os.path.isdir(comms_dir):
        shutil.rmtree(comms_dir)
        cleaned.append("comms/")

    if clean_all and os.path.isdir(output_base):
        shutil.rmtree(output_base)
        cleaned.append("output/")

    if cleaned:
        print(f"{_C.GREEN}Cleaned: {', '.join(cleaned)}{_C.RESET}")
    else:
        print("Nothing to clean.")


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

    if not cmd or cmd.startswith("-"):
        print(f"{_C.BOLD}Idle Harness{_C.RESET} — GAN-inspired multi-agent app builder\n")
        print("Usage:")
        print(f"  python orchestrator.py \"your app idea\"    Build an app")
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
