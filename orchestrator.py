import anyio
import os
import re
import subprocess
import sys
import time

from cli import call_agent
from config import CONFIG, VERDICT_PASS, TOOLS_READONLY, TOOLS_FULL, get_harness_root
from state import HarnessState
from server import DevServer


class Orchestrator:
    def __init__(self, root_dir: str | None = None):
        self.root = root_dir or get_harness_root()
        self.comms_dir = os.path.join(self.root, CONFIG["comms_dir"])
        self.output_base = os.path.join(self.root, CONFIG["output_dir"])
        self.output_dir = None  # Set after plan() parses product name
        self.agents_dir = os.path.join(self.root, "agents")

        self.spec_path = os.path.join(self.comms_dir, "spec.md")
        self.eval_path = os.path.join(self.comms_dir, "evaluation.md")

        self._planner_prompt = None
        self._generator_prompt = None
        self._evaluator_prompt = None

        self.state = HarnessState(self.comms_dir)
        self.server = None  # Set after output_dir is known

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

    def _init_output(self):
        """Parse product name from spec and create output/{name}/ directory."""
        with open(self.spec_path) as f:
            spec = f.read()

        match = re.search(r"^#\s+(.+)$", spec, re.MULTILINE)
        if match:
            name = match.group(1).strip()
            slug = re.sub(r"[^\w\s-]", "", name.lower())
            slug = re.sub(r"[\s_]+", "-", slug).strip("-")
        else:
            slug = "app"

        self.output_dir = os.path.join(self.output_base, slug)
        os.makedirs(self.output_dir, exist_ok=True)
        subprocess.run(
            ["git", "init"], cwd=self.output_dir, capture_output=True
        )
        self.server = DevServer(
            self.comms_dir, self.output_dir, CONFIG["dev_server_startup_wait"]
        )
        _log("Harness", f"Project directory: output/{slug}/")

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
        await call_agent(
            system_prompt=self.planner_prompt,
            user_prompt=prompt,
            allowed_tools=TOOLS_READONLY,
            cwd=self.root,
        )

        if not os.path.exists(self.spec_path):
            raise RuntimeError("Planner did not write spec.md")

        self._init_output()
        self.state.set_phase("building")
        _log("Planner", f"Spec generated. ({_elapsed(start)})")

    async def build(self):
        with open(self.spec_path) as f:
            spec = f.read()

        eval_context = ""
        try:
            with open(self.eval_path) as f:
                eval_context = f"\n\nPrevious evaluation feedback:\n{f.read()}"
        except FileNotFoundError:
            pass

        self.state.increment_build()
        status = self.state.load()
        attempt = status["build_attempts"]

        _log("Generator", f"Building application (attempt {attempt}/{CONFIG['max_build_attempts']})...")
        start = time.time()
        prompt = (
            f"Build the complete application based on this product spec.\n\n"
            f"Spec:\n{spec}\n"
            f"{eval_context}\n\n"
            f"Implement ALL features in the spec. "
            f"Work in the current directory. Self-verify: build must succeed, app must run. "
            f"Commit your changes with git."
        )

        await call_agent(
            system_prompt=self.generator_prompt,
            user_prompt=prompt,
            allowed_tools=TOOLS_FULL,
            cwd=self.output_dir,
        )
        _log("Generator", f"Build complete. ({_elapsed(start)})")

    async def evaluate(self) -> bool:
        with open(self.spec_path) as f:
            spec = f.read()

        self.state.increment_eval()
        status = self.state.load()
        attempt = status["eval_attempts"]

        _log("Evaluator", "Starting servers...")
        self.server.start()
        _log("Evaluator", f"Testing application (attempt {attempt}/{CONFIG['max_build_attempts']})...")
        start = time.time()
        try:
            prompt = (
                f"Evaluate the running application against the product spec.\n\n"
                f"Product spec:\n{spec}\n\n"
                f"Navigate to {CONFIG['dev_server_url']}. Test the complete user journey: "
                f"every feature, navigation flow, data persistence, design quality, originality. "
                f"Take screenshots as evidence. Write your evaluation as a response."
            )

            eval_result = await call_agent(
                system_prompt=self.evaluator_prompt,
                user_prompt=prompt,
                allowed_tools=TOOLS_READONLY,
                mcp_tools=[CONFIG["mcp_tool"]],
                cwd=self.root,
            )

            with open(self.eval_path, "w") as f:
                f.write(eval_result)
        finally:
            _log("Evaluator", "Stopping servers...")
            self.server.stop()

        passed = VERDICT_PASS in eval_result
        if passed:
            _log("Evaluator", f"PASS ({_elapsed(start)})")
        else:
            _log("Evaluator", f"FAIL ({_elapsed(start)})")
            for line in eval_result.split("\n"):
                if line.strip().startswith(("1.", "2.", "3.")):
                    print(f"    {line.strip()}")
        return passed

    async def run(self, user_prompt: str):
        print("=" * 60)
        print("  Multi-Agent Coding Harness")
        print("=" * 60)
        total_start = time.time()

        self.setup()
        await self.plan(user_prompt)

        for attempt in range(CONFIG["max_build_attempts"]):
            await self.build()
            passed = await self.evaluate()
            if passed:
                break

            if attempt == CONFIG["max_build_attempts"] - 1:
                _log("Harness", f"Failed after {CONFIG['max_build_attempts']} attempts.")

        self.state.set_phase("completed")
        self._print_report(total_start)

    def _print_report(self, total_start: float):
        status = self.state.load()
        print("\n" + "=" * 60)
        print("  FINAL REPORT")
        print("=" * 60)
        print(f"  Project:        {self.output_dir}")
        print(f"  Build attempts: {status['build_attempts']}")
        print(f"  Eval attempts:  {status['eval_attempts']}")
        print(f"  Total time:     {_elapsed(total_start)}")
        print("=" * 60)


def _log(agent: str, msg: str):
    print(f"[{agent}] {msg}")


def _elapsed(start: float) -> str:
    secs = int(time.time() - start)
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
