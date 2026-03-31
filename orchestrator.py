import anyio
import os
import re
import shutil
import subprocess
import sys
import time

from cli import call_agent, AgentError
from config import (
    CONFIG, TOOLS_READONLY, TOOLS_FULL, get_harness_root,
)
from sprint import Sprint, parse_sprints
from state import HarnessState
from server import DevServer


def _check_verdict_pass(text: str) -> bool:
    """Match 'Verdict: PASS' only as a standalone markdown heading or line."""
    return bool(re.search(r"^#{0,3}\s*Verdict:\s*PASS\s*$", text, re.MULTILINE))


def _check_contract_agreed(text: str) -> bool:
    """Match 'AGREED' only at the start of a line (not inside 'NOT AGREED')."""
    return bool(re.search(r"^AGREED\b", text, re.MULTILINE))


class Orchestrator:
    def __init__(self, root_dir: str | None = None):
        self.root = root_dir or get_harness_root()
        self.comms_dir = os.path.join(self.root, CONFIG["comms_dir"])
        self.output_base = os.path.join(self.root, CONFIG["output_dir"])
        self.output_dir = None
        self.agents_dir = os.path.join(self.root, "agents")

        self.spec_path = os.path.join(self.comms_dir, "spec.md")
        self.sprints: list[Sprint] = []
        self.sprint_results: dict[int, bool] = {}

        self._planner_prompt = None
        self._generator_prompt = None
        self._evaluator_prompt = None

        self.state = HarnessState(self.comms_dir)
        self.server = None

    def _load_prompt(self, name: str) -> str:
        path = os.path.join(self.agents_dir, f"{name}.md")
        with open(path) as f:
            return f.read()

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

    def _track_cost(self, agent_result):
        """Record token usage from an agent call."""
        self.state.add_cost(agent_result.input_tokens, agent_result.output_tokens)

    def _init_output(self):
        """Parse product name from spec and create output/{name}/ directory."""
        with open(self.spec_path) as f:
            spec = f.read()

        match = re.search(r"^#\s+(.+)$", spec, re.MULTILINE)
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
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(d, "screenshots"), exist_ok=True)
        return d

    def setup(self):
        os.makedirs(self.output_base, exist_ok=True)
        self.state.init()

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

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
        self._track_cost(agent_result)

        if not os.path.exists(self.spec_path):
            raise RuntimeError("Planner did not write spec.md")

        self._init_output()

        with open(self.spec_path) as f:
            spec = f.read()
        self.sprints = parse_sprints(spec)

        elapsed = int(time.time() - start)
        self.state.record_plan_time(elapsed)
        self.state.set_sprint_info(current=0, total=len(self.sprints))
        self.state.set_phase("building")
        _log("Planner", f"Spec generated — {len(self.sprints)} sprint(s). ({_elapsed(start)})")

    # ------------------------------------------------------------------
    # Contract negotiation
    # ------------------------------------------------------------------

    async def negotiate_contract(self, sprint: Sprint) -> str:
        sprint_dir = self._sprint_dir(sprint)
        proposal_path = os.path.join(sprint_dir, "contract_proposal.md")
        review_path = os.path.join(sprint_dir, "contract_review.md")
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")

        with open(self.spec_path) as f:
            spec = f.read()

        _log("Contract", f"Sprint {sprint.number}: Negotiating...")
        start = time.time()

        for round_num in range(CONFIG["max_negotiation_rounds"]):
            # Generator proposes
            review_context = ""
            if os.path.exists(review_path):
                with open(review_path) as f:
                    review_context = f"\n\nEvaluator's review of your previous proposal:\n{f.read()}"

            gen_prompt = (
                f"Propose a sprint contract for Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Sprint scope — Features: {', '.join(sprint.features)}. "
                f"Goal: {sprint.goal}\n\n"
                f"Full product spec:\n{spec}\n"
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
            self._track_cost(gen_result)

            # Evaluator reviews
            with open(proposal_path) as f:
                proposal = f.read()

            eval_prompt = (
                f"Review this sprint contract proposal for Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Proposal:\n{proposal}\n\n"
                f"Full product spec:\n{spec}\n\n"
                f"Write your review to {review_path} using the Write tool. "
                f"Use the Contract Review Mode described in your instructions. "
                f"If the criteria are complete and testable, write 'AGREED' at the top."
            )
            eval_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=eval_prompt,
                allowed_tools=TOOLS_READONLY,
                cwd=self.root,
            )
            self._track_cost(eval_result)

            # Check both agent response and written file for AGREED
            agreed = _check_contract_agreed(eval_result.result)
            if not agreed and os.path.exists(review_path):
                with open(review_path) as f:
                    agreed = _check_contract_agreed(f.read())

            if agreed:
                shutil.copy(proposal_path, contract_path)
                _log("Contract", f"Sprint {sprint.number}: Agreed in round {round_num + 1}. ({_elapsed(start)})")
                break
        else:
            # No agreement — use last proposal
            shutil.copy(proposal_path, contract_path)
            _log("Contract", f"Sprint {sprint.number}: No agreement — using last proposal. ({_elapsed(start)})")

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "negotiate", elapsed)
        return contract_path

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    async def build(self, sprint: Sprint, contract_path: str):
        with open(self.spec_path) as f:
            spec = f.read()
        with open(contract_path) as f:
            contract = f.read()

        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        eval_context = ""
        try:
            with open(eval_path) as f:
                eval_context = f"\n\nPrevious evaluation feedback:\n{f.read()}"
        except FileNotFoundError:
            pass

        self.state.increment_build(sprint.number)
        attempt = self.state.get_sprint_attempt(sprint.number, "build")

        # Absolute path for dev_server.json so Generator writes to the right place
        dev_server_json_path = os.path.join(self.comms_dir, "dev_server.json")

        _log("Generator", f"Sprint {sprint.number} — Building (attempt {attempt})...")
        start = time.time()
        prompt = (
            f"Build Sprint {sprint.number}: {sprint.name}.\n\n"
            f"Sprint contract:\n{contract}\n\n"
            f"Full product spec:\n{spec}\n"
            f"{eval_context}\n\n"
            f"Implement ALL criteria in the sprint contract. "
            f"Work in the current directory. Self-verify: build must succeed, app must run. "
            f"Commit your changes with git.\n\n"
            f"Write the dev server config to this ABSOLUTE path: {dev_server_json_path}"
        )

        agent_result = await call_agent(
            system_prompt=self.generator_prompt,
            user_prompt=prompt,
            allowed_tools=TOOLS_FULL,
            cwd=self.output_dir,
            max_turns=CONFIG["generator_max_turns"],
        )
        self._track_cost(agent_result)
        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "build", elapsed)
        _log("Generator", f"Sprint {sprint.number} — Build complete. ({_elapsed(start)})")

    # ------------------------------------------------------------------
    # Evaluate
    # ------------------------------------------------------------------

    async def evaluate(self, sprint: Sprint, contract_path: str) -> bool:
        with open(self.spec_path) as f:
            spec = f.read()
        with open(contract_path) as f:
            contract = f.read()

        sprint_dir = self._sprint_dir(sprint)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        screenshots_dir = os.path.join(sprint_dir, "screenshots")

        self.state.increment_eval(sprint.number)
        attempt = self.state.get_sprint_attempt(sprint.number, "eval")

        _log("Evaluator", f"Sprint {sprint.number} — Starting servers...")
        self.server.start()
        _log("Evaluator", f"Sprint {sprint.number} — Testing (attempt {attempt})...")
        start = time.time()
        try:
            prompt = (
                f"Evaluate Sprint {sprint.number}: {sprint.name}.\n\n"
                f"Sprint contract (test against these criteria):\n{contract}\n\n"
                f"Product spec:\n{spec}\n\n"
                f"Navigate to {CONFIG['dev_server_url']}. Test every criterion in the sprint contract. "
                f"Take screenshots as evidence — save them to {screenshots_dir}/. "
                f"Write your evaluation as a response."
            )

            eval_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=prompt,
                allowed_tools=TOOLS_READONLY,
                mcp_tools=[CONFIG["mcp_tool"]],
                cwd=self.root,
            )
            self._track_cost(eval_result)

            with open(eval_path, "w") as f:
                f.write(eval_result.result)
        finally:
            _log("Evaluator", f"Sprint {sprint.number} — Stopping servers...")
            self.server.stop()

        elapsed = int(time.time() - start)
        self.state.add_sprint_timing(sprint.number, "eval", elapsed)

        passed = _check_verdict_pass(eval_result.result)
        if passed:
            _log("Evaluator", f"Sprint {sprint.number} — PASS ({_elapsed(start)})")
        else:
            _log("Evaluator", f"Sprint {sprint.number} — FAIL ({_elapsed(start)})")
            for line in eval_result.result.split("\n"):
                if line.strip().startswith(("1.", "2.", "3.")):
                    print(f"    {line.strip()}")
        return passed

    # ------------------------------------------------------------------
    # Run — full pipeline
    # ------------------------------------------------------------------

    async def run(self, user_prompt: str):
        print("=" * 60)
        print("  Multi-Agent Coding Harness")
        print("=" * 60)
        total_start = time.time()

        self.setup()
        await self.plan(user_prompt)

        if CONFIG["mode"] == "simple":
            await self._run_simple()
        else:
            await self._run_full()

        self.state.set_phase("completed")
        self._print_report(total_start)

    async def _run_full(self):
        """Full pipeline: sprint decomposition + contract negotiation + build-eval loop."""
        for sprint in self.sprints:
            self.state.set_sprint_info(current=sprint.number, total=len(self.sprints))
            _log("Harness", f"=== Sprint {sprint.number}/{len(self.sprints)}: {sprint.name} ===")

            contract_path = await self.negotiate_contract(sprint)

            sprint_passed = False
            for attempt in range(CONFIG["max_build_attempts"]):
                try:
                    await self.build(sprint, contract_path)
                    passed = await self.evaluate(sprint, contract_path)
                except AgentError as exc:
                    _log("Harness", f"Sprint {sprint.number} attempt {attempt + 1} error: {exc}")
                    passed = False

                if passed:
                    sprint_passed = True
                    break

                if attempt == CONFIG["max_build_attempts"] - 1:
                    _log("Harness", f"Sprint {sprint.number} failed after {CONFIG['max_build_attempts']} attempts.")

            self.sprint_results[sprint.number] = sprint_passed

            if not sprint_passed:
                _log("Harness", f"Sprint {sprint.number} incomplete — continuing to next sprint.")

    async def _run_simple(self):
        """Simple mode: build-eval loop without sprint decomposition or contract negotiation."""
        single_sprint = Sprint(number=1, name="Full Build")
        self.state.set_sprint_info(current=1, total=1)
        _log("Harness", "=== Simple mode: build + evaluation (no contracts) ===")

        # Create a pseudo-contract from the spec for the evaluator
        sprint_dir = self._sprint_dir(single_sprint)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(self.spec_path) as f:
            spec = f.read()
        with open(contract_path, "w") as f:
            f.write(spec)

        sprint_passed = False
        for attempt in range(CONFIG["max_build_attempts"]):
            try:
                await self.build(single_sprint, contract_path)
                passed = await self.evaluate(single_sprint, contract_path)
            except AgentError as exc:
                _log("Harness", f"Simple mode attempt {attempt + 1} error: {exc}")
                passed = False

            if passed:
                sprint_passed = True
                break

            if attempt == CONFIG["max_build_attempts"] - 1:
                _log("Harness", f"Simple mode: failed after {CONFIG['max_build_attempts']} attempts.")

        self.sprint_results[1] = sprint_passed

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _print_report(self, total_start: float):
        status = self.state.load()
        print("\n" + "=" * 60)
        print("  FINAL REPORT")
        print("=" * 60)
        print(f"  Project:        {self.output_dir}")
        print(f"  Mode:           {CONFIG['mode']}")
        print(f"  Sprints:        {status.get('total_sprints', 1)}")
        print(f"  Build attempts: {self.state.total_builds()}")
        print(f"  Eval attempts:  {self.state.total_evals()}")

        # Sprint results
        for num, passed in self.sprint_results.items():
            icon = "PASS" if passed else "FAIL"
            print(f"  Sprint {num}:       {icon}")

        # Cost
        cost = status.get("cost", {})
        input_t = cost.get("input_tokens", 0)
        output_t = cost.get("output_tokens", 0)
        if input_t or output_t:
            print(f"  Tokens:         {input_t:,} in / {output_t:,} out")

        # Timings
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


def _log(agent: str, msg: str):
    print(f"[{agent}] {msg}")


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


def main():
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py \"<your product idea>\"")
        sys.exit(1)

    user_prompt = " ".join(sys.argv[1:])
    orch = Orchestrator()
    anyio.run(orch.run, user_prompt)


if __name__ == "__main__":
    main()
