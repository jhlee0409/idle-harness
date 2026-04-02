import anyio
import os
import re
import shutil
import subprocess
import sys
import time

from cli import call_agent, AgentError, InfraError
from config import (
    CONFIG, CONTRACT_AGREED,
    TOOLS_READONLY, TOOLS_FULL, TOOLS_EVALUATOR,
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
        self.sprint_results: dict[int, bool] = {}
        self._created_dirs: set[str] = set()

        self._planner_prompt = None
        self._generator_prompt = None
        self._evaluator_prompt = None

        # Continuous session: Generator keeps context across build attempts
        self._generator_conversation_id: str | None = None

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
        match = re.search(r"^#\s+(.+)$", self.spec, re.MULTILINE)
        if match:
            name = match.group(1).strip()
            slug = re.sub(r"[^a-z0-9\s-]", "", name.lower())
            slug = re.sub(r"[\s]+", "-", slug).strip("-")
        else:
            slug = "app"

        self.output_dir = os.path.join(self.output_base, slug)
        os.makedirs(self.output_dir, exist_ok=True)
        subprocess.run(
            ["git", "init"], cwd=self.output_dir, capture_output=True
        )
        self.server = DevServer(
            comms_dir=self.comms_dir,
            output_dir=self.output_dir,
            startup_wait=CONFIG["dev_server_startup_wait"],
            health_timeout=CONFIG["dev_server_health_timeout"],
            health_url=CONFIG["dev_server_url"],
        )
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
        self.state.init()

    async def plan(self, user_prompt: str):
        _log("Planner", "Generating spec...")
        start = time.time()
        prompt = (
            f"User request: {user_prompt}\n\n"
            f"Generate a complete product specification. "
            f"Write the spec to {self.spec_path} using the Write tool."
        )
        agent_result = await call_agent(
            system_prompt=self.planner_prompt,
            user_prompt=prompt,
            allowed_tools=TOOLS_READONLY,
            cwd=self.root,
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

    async def negotiate_contract(self, sprint: Sprint) -> str:
        sprint_dir = self._sprint_dir(sprint)
        proposal_path = os.path.join(sprint_dir, "contract_proposal.md")
        review_path = os.path.join(sprint_dir, "contract_review.md")
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")

        _log("Contract", f"Sprint {sprint.number}: Negotiating...")
        start = time.time()

        for round_num in range(CONFIG["max_negotiation_rounds"]):
            review_context = _read_file_optional(review_path)
            if review_context:
                review_context = f"\n\nEvaluator's review of your previous proposal:\n{review_context}"

            gen_prompt = (
                f"Propose a sprint contract for Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Sprint scope — Features: {', '.join(sprint.features)}. "
                f"Goal: {sprint.goal}\n\n"
                f"Full product spec:\n{self.spec}\n"
                f"{review_context}\n\n"
                f"Write your contract proposal to {proposal_path} using the Write tool. "
                f"Use the Contract Proposal Mode described in your instructions."
            )
            gen_result = await call_agent(
                system_prompt=self.generator_prompt,
                user_prompt=gen_prompt,
                allowed_tools=TOOLS_READONLY,
                cwd=self.root,
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
            eval_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=eval_prompt,
                allowed_tools=TOOLS_EVALUATOR,
                cwd=self.root,
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
                break
        else:
            shutil.copy(proposal_path, contract_path)
            _log("Contract", f"Sprint {sprint.number}: No agreement — using last proposal. ({_elapsed(start)})")

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "negotiate", elapsed)
        return contract_path

    async def build(self, sprint: Sprint, contract_path: str):
        contract = _read_file(contract_path)

        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        eval_context = _read_file_optional(eval_path)
        if eval_context:
            eval_context = f"\n\nPrevious evaluation feedback:\n{eval_context}"

        self.state.increment(sprint.number, "build")
        attempt = self.state.get_sprint_attempt(sprint.number, "build")

        # Generator runs in output_dir, but dev_server.json must land in comms_dir
        dev_server_json_path = os.path.join(self.comms_dir, "dev_server.json")

        _log("Generator", f"Sprint {sprint.number} — Building (attempt {attempt})...")
        start = time.time()
        prompt = (
            f"Build Sprint {sprint.number}: {sprint.name}.\n\n"
            f"Sprint contract:\n{contract}\n\n"
            f"Full product spec:\n{self.spec}\n"
            f"{eval_context}\n\n"
            f"Implement ALL criteria in the sprint contract. "
            f"Work in the current directory. Self-verify: build must succeed, app must run. "
            f"Commit your changes with git.\n\n"
            f"Write the dev server config to this ABSOLUTE path: {dev_server_json_path}"
        )

        try:
            agent_result = await call_agent(
                system_prompt=self.generator_prompt,
                user_prompt=prompt,
                allowed_tools=TOOLS_FULL,
                cwd=self.output_dir,
                max_turns=CONFIG["generator_max_turns"],
                resume=self._generator_conversation_id,
            )
        except AgentError:
            # Reset conversation — crashed session can't be resumed
            self._generator_conversation_id = None
            raise
        # Maintain continuous session across build attempts (per article recommendation)
        if agent_result.conversation_id:
            self._generator_conversation_id = agent_result.conversation_id
        self._track_cost(agent_result, "build")
        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "build", elapsed)
        _log("Generator", f"Sprint {sprint.number} — Build complete. ({_fmt_stats(agent_result, start)})")

    async def evaluate(self, sprint: Sprint, contract_path: str) -> bool:
        contract = _read_file(contract_path)

        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        screenshots_dir = os.path.join(sprint_dir, "screenshots")

        self.state.increment(sprint.number, "eval")
        attempt = self.state.get_sprint_attempt(sprint.number, "eval")

        _log("Evaluator", f"Sprint {sprint.number} — Starting servers...")
        self.server.start()
        _log("Evaluator", f"Sprint {sprint.number} — Testing (attempt {attempt})...")
        start = time.time()
        try:
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
                mcp_tools=[CONFIG["mcp_tool"]],
                cwd=self.root,
            )
            self._track_cost(eval_result, "eval")

            with open(eval_path, "w") as f:
                f.write(eval_result.result)
        finally:
            _log("Evaluator", f"Sprint {sprint.number} — Stopping servers...")
            self.server.stop()

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "eval", elapsed)

        passed = _check_verdict_pass(eval_result.result)
        stats = _fmt_stats(eval_result, start)
        if passed:
            _log("Evaluator", f"Sprint {sprint.number} — {_C.GREEN}{_C.BOLD}PASS{_C.RESET} ({stats})")
        else:
            _log("Evaluator", f"Sprint {sprint.number} — {_C.RED}{_C.BOLD}FAIL{_C.RESET} ({stats})")
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
            except AgentError as exc:
                _log("Harness", f"{label} attempt {attempt + 1} error: {exc}")
                passed = False

            if passed:
                sprint_passed = True
                break

            if attempt == CONFIG["max_build_attempts"] - 1:
                # Delegate to user: continue or abort? (raises UserAbort)
                _ask_user_continue(label)

        self.sprint_results[sprint.number] = sprint_passed
        self.state.set_sprint_result(sprint.number, sprint_passed)
        return sprint_passed

    async def run(self, user_prompt: str):
        print("=" * 60)
        print("  Idle Harness")
        print("=" * 60)
        total_start = time.time()

        self.setup()
        await self.plan(user_prompt)

        try:
            if CONFIG["mode"] == "simple":
                await self._run_simple()
            else:
                await self._run_full()
        except UserAbort:
            _log("Harness", "Aborted by user.")

        self.state.set_phase("completed")
        self._print_report(total_start)

    async def _run_full(self):
        for sprint in self.sprints:
            self.state.set_sprint_info(current=sprint.number, total=len(self.sprints))
            print(f"\n{_C.BOLD}{'─' * 60}")
            _log("Harness", f"Sprint {sprint.number}/{len(self.sprints)}: {sprint.name}")
            print(f"{'─' * 60}{_C.RESET}")

            contract_path = await self.negotiate_contract(sprint)
            passed = await self._retry_build_eval(sprint, contract_path, f"Sprint {sprint.number}")

            if not passed:
                _log("Harness", f"Sprint {sprint.number} incomplete — continuing to next sprint.")

        # Final integration evaluation across all sprints
        await self._integration_eval()

    async def _integration_eval(self):
        """Final integration evaluation across all sprints (spec §9)."""
        print(f"\n{_C.BOLD}{'─' * 60}")
        _log("Harness", "Final Integration Evaluation")
        print(f"{'─' * 60}{_C.RESET}")
        max_attempts = 2
        eval_path = os.path.join(self.comms_dir, "integration_evaluation.md")

        for attempt in range(1, max_attempts + 1):
            _log("Evaluator", f"Integration eval (attempt {attempt}/{max_attempts})...")
            self.server.start()
            start = time.time()
            try:
                prompt = (
                    f"Final Integration Evaluation.\n\n"
                    f"All sprints have been built. Evaluate the COMPLETE application end-to-end.\n\n"
                    f"Full product spec:\n{self.spec}\n\n"
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
                    mcp_tools=[CONFIG["mcp_tool"]],
                    cwd=self.root,
                )
                self._track_cost(eval_result, "integration_eval")

                with open(eval_path, "w") as f:
                    f.write(eval_result.result)
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
            else:
                _log("Evaluator", f"Integration eval — {_C.RED}{_C.BOLD}FAIL{_C.RESET} ({stats})")
                _print_required_changes(eval_result.result)

        self.state.set_sprint_result(0, False)
        _log("Harness", f"Integration eval failed after {max_attempts} attempts.")

    async def _run_simple(self):
        single_sprint = Sprint(number=1, name="Full Build")
        self.state.set_sprint_info(current=1, total=1)
        _log("Harness", "=== Simple mode: build + evaluation (no contracts) ===")

        sprint_dir = self._sprint_dir(single_sprint)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write(self.spec)

        await self._retry_build_eval(single_sprint, contract_path, "Simple mode")

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
        for num in sorted(sprint_results):
            passed = sprint_results[num]
            color = _C.GREEN if passed else _C.RED
            icon = f"{color}{_C.BOLD}{'PASS' if passed else 'FAIL'}{_C.RESET}"
            if num == 0:
                print(f"  Integration:    {icon}")
            else:
                print(f"  Sprint {num}:       {icon}")

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
                print(f"    {phase:12s}  {pi:,} in / {po:,} out")

        timings = status.get("timings", {})
        if timings.get("plan"):
            print(f"  Planning:       {_elapsed_secs(timings['plan'])}")
        for st in timings.get("sprints", []):
            parts = []
            if "negotiate" in st:
                parts.append(f"negotiate: {_elapsed_secs(st['negotiate'])}")
            if "build" in st:
                parts.append(f"build: {' + '.join(_elapsed_secs(b) for b in st['build'])}")
            if "eval" in st:
                parts.append(f"eval: {' + '.join(_elapsed_secs(e) for e in st['eval'])}")
            print(f"  Sprint {st['sprint']}:      {', '.join(parts)}")

        print(f"  Total time:     {_elapsed(total_start)}")
        print("=" * 60)


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


def _fmt_tokens(agent_result) -> str:
    total = agent_result.input_tokens + agent_result.output_tokens
    if total >= 1_000_000:
        return f"{total / 1_000_000:.1f}M"
    if total >= 1_000:
        return f"{total / 1_000:.1f}k"
    return str(total)


def _fmt_cost(agent_result) -> str:
    cost = getattr(agent_result, "cost_usd", 0) or 0
    return f"${cost:.2f}" if cost > 0 else ""


def _fmt_stats(agent_result, start: float) -> str:
    parts = [_elapsed(start), f"{agent_result.turns} turns"]
    tok = _fmt_tokens(agent_result)
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
    print(f"  Continue to next sprint, or abort the harness?")
    print(f"{'=' * 60}{_C.RESET}")
    try:
        answer = input("  [c]ontinue / [a]bort (default: continue): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "c"
    if answer.startswith("a"):
        _log("Harness", "User chose to abort.")
        raise UserAbort()
    _log("Harness", "User chose to continue.")


def _log(agent: str, msg: str):
    color = _AGENT_COLORS.get(agent, "")
    print(f"{color}{_C.BOLD}[{agent}]{_C.RESET} {msg}")


def _elapsed(start: float) -> str:
    return _elapsed_secs(int(time.time() - start))


def _elapsed_secs(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    secs = secs % 60
    if mins < 60:
        return f"{mins}m {secs}s"
    hours = mins // 60
    mins = mins % 60
    return f"{hours}h {mins}m {secs}s"


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

def _find_mcp_server(mcp_tool: str, harness_root: str) -> str | None:
    import json
    search_paths = [
        os.path.join(harness_root, ".mcp.json"),
        os.path.join(harness_root, ".claude", "settings.json"),
        os.path.expanduser("~/.claude.json"),
        os.path.expanduser("~/.claude/settings.json"),
    ]
    for path in search_paths:
        try:
            with open(path) as f:
                config = json.load(f)
            mcp_servers = config.get("mcpServers", {})
            if mcp_tool in mcp_servers:
                return path
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return None


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


def _fix_mcp(mcp_tool: str, harness_root: str) -> bool:
    import json

    # Check if npx is available (needed for MCP server)
    npx_check = subprocess.run(["which", "npx"], capture_output=True, text=True)
    if npx_check.returncode != 0:
        print(_fail("npx", "npx not found. Install Node.js first."))
        return False

    mcp_json_path = os.path.join(harness_root, ".mcp.json")
    print(f"  {_C.DIM}→ Configuring {mcp_tool} MCP in .mcp.json{_C.RESET}")

    # Build the MCP config
    mcp_config = {}
    try:
        with open(mcp_json_path) as f:
            mcp_config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    servers = mcp_config.setdefault("mcpServers", {})
    servers[mcp_tool] = {
        "command": "npx",
        "args": ["@anthropic-ai/mcp-server-playwright"],
    }

    with open(mcp_json_path, "w") as f:
        json.dump(mcp_config, f, indent=2)

    print(_ok(f"MCP: {mcp_tool} configured in .mcp.json"))
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

    # 5. MCP server
    mcp_tool = CONFIG["mcp_tool"]
    harness_root = get_harness_root()
    mcp_path = _find_mcp_server(mcp_tool, harness_root)
    if mcp_path:
        rel = os.path.relpath(mcp_path, harness_root) if mcp_path.startswith(harness_root) else mcp_path
        print(_ok(f"MCP: {mcp_tool} ({rel})"))
    else:
        hint = f"'{mcp_tool}' not configured"
        print(_fail(f"MCP: {mcp_tool}", hint))
        issues.append((f"MCP: {mcp_tool}", hint, lambda: _fix_mcp(mcp_tool, harness_root)))

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


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--setup":
        _preflight(force_setup=True)
        print("Setup complete. Run:")
        print(f"  python orchestrator.py \"your app idea\"")
        return

    if len(sys.argv) < 2:
        print(f"{_C.BOLD}Idle Harness{_C.RESET} — GAN-inspired multi-agent app builder\n")
        print("Usage:")
        print(f"  python orchestrator.py \"your app idea\"    Build an app")
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
