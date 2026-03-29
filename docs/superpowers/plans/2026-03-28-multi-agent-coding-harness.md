# Multi-Agent Coding Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GAN 구조 기반의 Planner/Generator/Evaluator 3-에이전트 자율 코딩 시스템을 Python 오케스트레이터 + Claude CLI로 구현한다.

**Architecture:** Python 오케스트레이터가 `claude -p` CLI를 서브프로세스로 호출하여 세 에이전트를 순차 실행. 에이전트 간 통신은 `comms/` 디렉터리의 파일 읽기/쓰기. 평가 에이전트는 `chrome-devtools` MCP로 실행 중인 앱을 동적 테스트.

**Tech Stack:** Python 3.11+, Claude CLI (`claude -p`), chrome-devtools MCP

---

## File Structure

```
harness/
├── orchestrator.py          # 메인 진입점 + 오케스트레이션 루프
├── config.py                # 설정값 (임계값, 경로, 서버 명령)
├── cli.py                   # Claude CLI 호출 래퍼
├── state.py                 # status.json 읽기/쓰기 + 상태 관리
├── spec_parser.py           # spec.md에서 기능 목록 파싱
├── server.py                # dev server 기동/종료 관리
├── agents/
│   ├── planner.md           # Planner 시스템 프롬프트
│   ├── generator.md         # Generator 시스템 프롬프트
│   └── evaluator.md         # Evaluator 시스템 프롬프트
├── tests/
│   ├── test_config.py
│   ├── test_cli.py
│   ├── test_state.py
│   ├── test_spec_parser.py
│   ├── test_server.py
│   └── test_orchestrator.py
└── comms/                   # 런타임 생성 (gitignored)
```

---

### Task 1: Project Setup & Config

**Files:**
- Create: `config.py`
- Create: `tests/test_config.py`
- Create: `requirements.txt`
- Create: `.gitignore`

- [ ] **Step 1: Create .gitignore**

```gitignore
__pycache__/
*.pyc
comms/
output/
.pytest_cache/
```

- [ ] **Step 2: Create requirements.txt**

```
pytest==8.3.4
```

- [ ] **Step 3: Write the failing test for config**

```python
# tests/test_config.py
from config import CONFIG


def test_config_has_required_keys():
    required = [
        "max_contract_negotiations",
        "max_implementation_attempts",
        "max_integration_attempts",
        "dev_server_start_cmd",
        "dev_server_stop_cmd",
        "dev_server_url",
        "dev_server_startup_wait",
        "output_dir",
        "comms_dir",
        "mcp_tool",
    ]
    for key in required:
        assert key in CONFIG, f"Missing config key: {key}"


def test_config_defaults():
    assert CONFIG["max_contract_negotiations"] == 2
    assert CONFIG["max_implementation_attempts"] == 3
    assert CONFIG["max_integration_attempts"] == 2
    assert CONFIG["dev_server_start_cmd"] is None
    assert CONFIG["dev_server_url"] == "http://localhost:5173"
    assert CONFIG["output_dir"] == "output"
    assert CONFIG["comms_dir"] == "comms"
    assert CONFIG["mcp_tool"] == "chrome-devtools"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 5: Write config.py**

```python
# config.py
import os

CONFIG = {
    "max_contract_negotiations": 2,
    "max_implementation_attempts": 3,
    "max_integration_attempts": 2,
    "dev_server_start_cmd": None,
    "dev_server_stop_cmd": None,
    "dev_server_url": "http://localhost:5173",
    "dev_server_startup_wait": 5,
    "output_dir": "output",
    "comms_dir": "comms",
    "mcp_tool": "chrome-devtools",
}


def get_harness_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def get_comms_dir() -> str:
    return os.path.join(get_harness_root(), CONFIG["comms_dir"])


def get_output_dir() -> str:
    return os.path.join(get_harness_root(), CONFIG["output_dir"])


def get_agents_dir() -> str:
    return os.path.join(get_harness_root(), "agents")
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add config.py tests/test_config.py requirements.txt .gitignore
git commit -m "feat: add project setup and config module"
```

---

### Task 2: Claude CLI Wrapper

**Files:**
- Create: `cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import subprocess
from unittest.mock import patch, MagicMock
from cli import call_agent


def test_call_agent_constructs_correct_command():
    """Verify the CLI command is built correctly without actually calling claude."""
    mock_result = MagicMock()
    mock_result.stdout = "Agent response"
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        result = call_agent(
            system_prompt_file="agents/planner.md",
            user_prompt="Build a todo app",
            allowed_tools=["Read", "Write"],
            cwd="/tmp/test",
        )

        args = mock_run.call_args
        cmd = args[0][0]

        assert "claude" in cmd[0]
        assert "-p" in cmd
        assert "--system-prompt" in cmd
        assert "--allowed-tools" in cmd or "--allowedTools" in cmd
        assert result == "Agent response"


def test_call_agent_with_mcp_tools():
    """Verify MCP tools are included in allowed tools."""
    mock_result = MagicMock()
    mock_result.stdout = "Response"
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        call_agent(
            system_prompt_file="agents/evaluator.md",
            user_prompt="Evaluate the app",
            allowed_tools=["Read", "Write"],
            mcp_tools=["chrome-devtools"],
            cwd="/tmp/test",
        )

        cmd = args = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "chrome-devtools" in cmd_str or "mcp" in cmd_str


def test_call_agent_raises_on_failure():
    """Verify exception on non-zero exit."""
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.stderr = "Error"
    mock_result.returncode = 1

    with patch("subprocess.run", return_value=mock_result):
        try:
            call_agent(
                system_prompt_file="agents/planner.md",
                user_prompt="test",
                allowed_tools=["Read"],
                cwd="/tmp",
            )
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "Error" in str(e)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cli'`

- [ ] **Step 3: Write cli.py**

```python
# cli.py
import subprocess
import os


def call_agent(
    system_prompt_file: str,
    user_prompt: str,
    allowed_tools: list[str],
    cwd: str,
    mcp_tools: list[str] | None = None,
    timeout: int = 600,
) -> str:
    """Call a Claude agent via CLI subprocess.

    Args:
        system_prompt_file: Path to the .md file containing the system prompt.
        user_prompt: The prompt to send to the agent.
        allowed_tools: List of tool names the agent can use.
        cwd: Working directory for the subprocess.
        mcp_tools: Optional list of MCP tool prefixes to allow.
        timeout: Timeout in seconds (default 10 minutes).

    Returns:
        The agent's text response.

    Raises:
        RuntimeError: If the CLI exits with non-zero status.
    """
    system_prompt = _read_system_prompt(system_prompt_file)

    tools = list(allowed_tools)
    if mcp_tools:
        for mcp in mcp_tools:
            tools.append(f"mcp__{mcp}__*")

    cmd = [
        "claude",
        "-p",
        "--system-prompt", system_prompt,
        "--allowedTools", ",".join(tools),
        "--output-format", "text",
        "--model", "opus",
        "--dangerously-skip-permissions",
        user_prompt,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Agent failed (exit {result.returncode}): {result.stderr}"
        )

    return result.stdout.strip()


def _read_system_prompt(path: str) -> str:
    with open(path, "r") as f:
        return f.read()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli.py tests/test_cli.py
git commit -m "feat: add Claude CLI wrapper module"
```

---

### Task 3: State Management

**Files:**
- Create: `state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state.py
import json
import os
import tempfile
from state import HarnessState


def test_init_creates_status_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        path = os.path.join(tmpdir, "status.json")
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["phase"] == "planning"
        assert data["features"] == []


def test_set_features():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        features = [
            {"name": "Auth", "priority": "P0"},
            {"name": "Dashboard", "priority": "P1"},
        ]
        state.set_features(features)
        loaded = state.load()
        assert len(loaded["features"]) == 2
        assert loaded["features"][0]["name"] == "Auth"
        assert loaded["features"][0]["contract_status"] == "pending"
        assert loaded["features"][0]["implementation_status"] == "pending"
        assert loaded["features"][0]["attempts"] == 0


def test_update_feature_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.set_features([{"name": "Auth", "priority": "P0"}])
        state.update_feature(0, contract_status="agreed")
        loaded = state.load()
        assert loaded["features"][0]["contract_status"] == "agreed"


def test_advance_phase():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.set_phase("implementing")
        loaded = state.load()
        assert loaded["phase"] == "implementing"


def test_increment_attempts():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.set_features([{"name": "Auth", "priority": "P0"}])
        state.increment_attempts(0)
        state.increment_attempts(0)
        loaded = state.load()
        assert loaded["features"][0]["attempts"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'state'`

- [ ] **Step 3: Write state.py**

```python
# state.py
import json
import os


class HarnessState:
    def __init__(self, comms_dir: str):
        self.path = os.path.join(comms_dir, "status.json")
        self.comms_dir = comms_dir

    def init(self):
        os.makedirs(self.comms_dir, exist_ok=True)
        os.makedirs(os.path.join(self.comms_dir, "screenshots"), exist_ok=True)
        data = {
            "phase": "planning",
            "current_feature_index": 0,
            "features": [],
            "integration_test": {"status": "pending", "attempts": 0},
        }
        self._save(data)

    def load(self) -> dict:
        with open(self.path, "r") as f:
            return json.load(f)

    def _save(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def set_phase(self, phase: str):
        data = self.load()
        data["phase"] = phase
        self._save(data)

    def set_features(self, features: list[dict]):
        data = self.load()
        data["features"] = [
            {
                "name": f["name"],
                "priority": f["priority"],
                "contract_status": "pending",
                "implementation_status": "pending",
                "attempts": 0,
            }
            for f in features
        ]
        self._save(data)

    def update_feature(self, index: int, **kwargs):
        data = self.load()
        data["features"][index].update(kwargs)
        self._save(data)

    def increment_attempts(self, index: int):
        data = self.load()
        data["features"][index]["attempts"] += 1
        self._save(data)

    def set_current_feature(self, index: int):
        data = self.load()
        data["current_feature_index"] = index
        self._save(data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add state.py tests/test_state.py
git commit -m "feat: add state management module"
```

---

### Task 4: Spec Parser

**Files:**
- Create: `spec_parser.py`
- Create: `tests/test_spec_parser.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spec_parser.py
from spec_parser import parse_features

SAMPLE_SPEC = """# Todo App

## Vision
A simple todo application.

## Target Users
Everyone.

## Features

### P0: User Authentication
- Description: Login and registration
- User Story: As a user, I want to log in
- Acceptance Criteria: Users can register and log in

### P0: Task Management
- Description: Create, edit, delete tasks
- User Story: As a user, I want to manage tasks
- Acceptance Criteria: Full CRUD for tasks

### P1: Task Categories
- Description: Organize tasks by category
- User Story: As a user, I want to categorize tasks
- Acceptance Criteria: Assign and filter by category

### P2: Analytics Dashboard
- Description: View task completion stats
- User Story: As a user, I want to see my productivity
- Acceptance Criteria: Charts showing completion rates

## AI Integration Opportunities
- Smart task suggestions

## UX Flow
1. Login -> Dashboard -> Task List
"""


def test_parse_features_extracts_all():
    features = parse_features(SAMPLE_SPEC)
    assert len(features) == 4


def test_parse_features_extracts_names():
    features = parse_features(SAMPLE_SPEC)
    names = [f["name"] for f in features]
    assert "User Authentication" in names
    assert "Task Management" in names
    assert "Task Categories" in names
    assert "Analytics Dashboard" in names


def test_parse_features_extracts_priorities():
    features = parse_features(SAMPLE_SPEC)
    priorities = {f["name"]: f["priority"] for f in features}
    assert priorities["User Authentication"] == "P0"
    assert priorities["Task Categories"] == "P1"
    assert priorities["Analytics Dashboard"] == "P2"


def test_parse_features_sorted_by_priority():
    features = parse_features(SAMPLE_SPEC)
    priorities = [f["priority"] for f in features]
    assert priorities == sorted(priorities)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_spec_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spec_parser'`

- [ ] **Step 3: Write spec_parser.py**

```python
# spec_parser.py
import re


def parse_features(spec_content: str) -> list[dict]:
    """Parse features from spec.md content.

    Expects format:
        ### P0: Feature Name
        - Description: ...
        - User Story: ...
        - Acceptance Criteria: ...

    Returns list of dicts sorted by priority, each with 'name' and 'priority'.
    """
    pattern = r"###\s+(P\d):\s+(.+)"
    features = []

    for match in re.finditer(pattern, spec_content):
        priority = match.group(1)
        name = match.group(2).strip()
        features.append({"name": name, "priority": priority})

    features.sort(key=lambda f: f["priority"])
    return features
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_spec_parser.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add spec_parser.py tests/test_spec_parser.py
git commit -m "feat: add spec parser module"
```

---

### Task 5: Dev Server Manager

**Files:**
- Create: `server.py`
- Create: `tests/test_server.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server.py
import json
import os
import tempfile
from unittest.mock import patch, MagicMock
from server import DevServer


def test_detect_server_cmd_from_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        dev_server_json = os.path.join(tmpdir, "dev_server.json")
        with open(dev_server_json, "w") as f:
            json.dump({"start": "npm run dev", "stop": "kill"}, f)
        server = DevServer(comms_dir=tmpdir, output_dir="/tmp/out")
        cmd = server.detect_from_comms()
        assert cmd["start"] == "npm run dev"


def test_detect_server_cmd_fallback_package_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = os.path.join(tmpdir, "output")
        os.makedirs(output_dir)
        pkg = {"scripts": {"dev": "vite"}}
        with open(os.path.join(output_dir, "package.json"), "w") as f:
            json.dump(pkg, f)

        server = DevServer(comms_dir=tmpdir, output_dir=output_dir)
        cmd = server.detect_from_package_json()
        assert cmd["start"] == "npm run dev"


def test_start_stop_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        server = DevServer(comms_dir=tmpdir, output_dir=tmpdir)
        server.start_cmd = "echo 'server started' & sleep 100"

        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_popen.return_value = mock_proc
            server.start()
            assert server.process is not None

            server.stop()
            mock_proc.terminate.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server'`

- [ ] **Step 3: Write server.py**

```python
# server.py
import json
import os
import subprocess
import time


class DevServer:
    def __init__(self, comms_dir: str, output_dir: str, startup_wait: int = 5):
        self.comms_dir = comms_dir
        self.output_dir = output_dir
        self.startup_wait = startup_wait
        self.start_cmd: str | None = None
        self.process: subprocess.Popen | None = None

    def detect_from_comms(self) -> dict | None:
        path = os.path.join(self.comms_dir, "dev_server.json")
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            self.start_cmd = data.get("start")
            return data
        return None

    def detect_from_package_json(self) -> dict | None:
        pkg_path = os.path.join(self.output_dir, "package.json")
        if os.path.exists(pkg_path):
            with open(pkg_path) as f:
                pkg = json.load(f)
            scripts = pkg.get("scripts", {})
            if "dev" in scripts:
                self.start_cmd = "npm run dev"
                return {"start": "npm run dev", "stop": "kill"}
        return None

    def detect(self) -> str | None:
        result = self.detect_from_comms()
        if result:
            return result["start"]
        result = self.detect_from_package_json()
        if result:
            return result["start"]
        return None

    def start(self):
        if not self.start_cmd:
            self.detect()
        if not self.start_cmd:
            raise RuntimeError("No dev server command found. Cannot start server.")

        self.process = subprocess.Popen(
            self.start_cmd,
            shell=True,
            cwd=self.output_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(self.startup_wait)

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "feat: add dev server manager module"
```

---

### Task 6: Agent System Prompts

**Files:**
- Create: `agents/planner.md`
- Create: `agents/generator.md`
- Create: `agents/evaluator.md`

- [ ] **Step 1: Write planner.md**

```markdown
# Planner Agent

You are the Planner agent in a multi-agent coding harness. Your role is to transform a short user prompt (1-4 sentences) into a comprehensive, ambitious product specification.

## Your Output

Write a complete product spec to `comms/spec.md` in this exact format:

```
# [Product Name]

## Vision
[2-3 sentences describing the product's purpose and value]

## Target Users
[Who will use this product and why]

## Features

### P0: [Feature Name]
- Description: [What this feature does]
- User Story: [As a <user>, I want <goal> so that <benefit>]
- Acceptance Criteria: [Concrete, testable criteria]

### P1: [Feature Name]
...

### P2: [Feature Name]
...

## AI Integration Opportunities
[List specific ways AI can enhance this product]

## UX Flow
[Step-by-step user journey through the application]
```

## Rules

1. **Focus on PRODUCT, not TECHNOLOGY.** Never specify tech stacks, frameworks, libraries, or implementation methods. That is the Generator's job. Specifying technical details causes cascading errors downstream.
2. **Be ambitious.** Expand the user's simple idea into a full product vision. A "todo app" should become a productivity platform with smart features.
3. **Prioritize ruthlessly.** P0 = core functionality that defines the product. P1 = important but not essential for MVP. P2 = nice-to-have polish.
4. **AI opportunities.** Actively seek places where AI can add value — smart suggestions, auto-categorization, natural language interfaces, content generation, etc.
5. **Testable criteria.** Every acceptance criterion must be something that can be verified by clicking through the running application.
6. **UX Flow must be concrete.** Describe specific screens, transitions, and interactions — not abstract concepts.
```

- [ ] **Step 2: Write generator.md**

```markdown
# Generator Agent

You are the Generator agent in a multi-agent coding harness. Your role is to implement features based on the product specification, one feature at a time.

## Modes of Operation

You operate in two modes depending on the task given to you:

### Mode 1: Contract Proposal

When asked to propose a feature contract, write `comms/feature_contract.md`:

```
## Feature: [Name from spec]

## Generator Proposal

### Deliverables
- [Specific files/components to create]
- [Specific behaviors to implement]

### Testable Checklist
- [ ] [Concrete, verifiable item — e.g. "Clicking 'Submit' with valid data creates a new record and shows success toast"]
- [ ] [Each item must be testable by interacting with the running app]

### Tech Approach (brief)
- [Framework/library choices with rationale]
- [Key architectural decisions]

## Evaluator Review
### Status: PENDING
```

### Mode 2: Implementation

When asked to implement a feature (after contract is agreed):

1. **Read the agreed contract** from `comms/feature_contract.md`
2. **Read any previous evaluation feedback** from `comms/evaluation.md` if this is a retry
3. **Implement the feature** in the `output/` directory
4. **Self-verify**: Run the build, check for errors, test basic functionality
5. **Git commit** with a meaningful message describing what was implemented
6. **If first feature**: Write `comms/dev_server.json` with the commands to start/stop the dev server:
   ```json
   {"start": "npm run dev", "stop": "kill"}
   ```

## Rules

1. **Analyze the existing codebase first.** If `output/` already has code, read it and follow its patterns, conventions, and tech stack.
2. **If starting fresh**, choose a modern, appropriate tech stack for the product. Do NOT wait for instructions on what stack to use.
3. **One feature at a time.** Only implement what is in the current contract. Do not add features from other parts of the spec.
4. **Self-verify before handoff.** Run the build. Fix any build errors. Check that the basic functionality works. Do not hand off broken code.
5. **On retry with feedback**: Focus on the Required Changes from the evaluation. You may modify related code as needed, but do not add new features outside the contract scope.
6. **Git discipline**: Make meaningful commits. Each commit message should describe what was implemented or fixed.
```

- [ ] **Step 3: Write evaluator.md**

```markdown
# Evaluator Agent

You are the Evaluator agent in a multi-agent coding harness. You are a strict, skeptical QA engineer. Your job is to find problems, not to praise work.

## Critical Principle

You must NEVER read the source code. You evaluate the RUNNING APPLICATION only, like a real user would. This is the GAN principle: you judge the output, not the process.

## Modes of Operation

### Mode 1: Contract Review

When asked to review a feature contract (`comms/feature_contract.md`):

1. Read the contract proposal
2. Read the product spec (`comms/spec.md`) for context
3. Critically evaluate:
   - Are all checklist items truly testable by interacting with the running app?
   - Are there missing edge cases? (empty states, error states, boundary conditions)
   - Is the scope too narrow or too broad for one feature?
4. Write your review into the `## Evaluator Review` section:

```
## Evaluator Review
### Status: AGREED / NEEDS_REVISION
### Modifications:
- [Added/changed checklist items with reasoning]
- [Removed items that are not testable]
```

### Mode 2: Feature Evaluation

When asked to evaluate a feature:

1. **Navigate to the running app** using chrome-devtools MCP tools
2. **Test every item** in the contract checklist by actually interacting with the app
3. **Take screenshots** as evidence — save to `comms/screenshots/` using the screenshot tool
4. **Assess design quality** across four criteria
5. **Write evaluation** to `comms/evaluation.md`:

```
## Feature: [Name]
## Attempt: N/3

### Contract Checklist
- [x] Item that passed (describe what you did and saw)
- [ ] Item that failed (describe what you did and what went wrong) ← FAIL

### Checklist Pass Rate: X/Y (Z%)

### Design Assessment
| Criterion | Verdict | Evidence | Screenshot |
|-----------|---------|----------|------------|
| Design Quality | PASS/FAIL | [Specific observations about color, typography, layout coherence] | screenshots/[name].png |
| Originality | PASS/FAIL | [Evidence of intentional creative choices vs template defaults] | screenshots/[name].png |
| Craft | PASS/FAIL | [Spacing, contrast ratio, technical fundamentals] | screenshots/[name].png |
| Functionality | PASS/FAIL | [Core interactions working as specified] | screenshots/[name].png |

### Verdict: PASS / FAIL

### Required Changes (if FAIL)
1. [Specific, actionable change with clear description of what is wrong and what "fixed" looks like]
2. [Each item must be independently verifiable]

### Context for Generator
- [Relevant context to help the Generator understand the issue]
- [Which contract items are affected]
```

## Rules

1. **Be skeptical by default.** Your job is to find problems. If you can't find any, look harder.
2. **Never read source code.** Only interact with the running application.
3. **Screenshot everything.** Every PASS and FAIL claim must have a screenshot as evidence.
4. **Checklist is law.** The agreed contract checklist is the primary pass/fail criterion. If even one item fails, the feature fails.
5. **All four design criteria must pass.** A beautiful app with broken functionality fails. A functional app with default Bootstrap styling fails on originality.
6. **Required Changes must be specific.** "Make it look better" is not acceptable. "Change the card background from default white (#fff) to a warm off-white (#faf8f5) to match the earth-tone palette" is acceptable.
7. **Test edge cases.** Empty states, error messages, loading states, mobile responsiveness. If the contract doesn't mention them but they're obviously broken, flag them.
```

- [ ] **Step 4: Commit**

```bash
mkdir -p agents
git add agents/planner.md agents/generator.md agents/evaluator.md
git commit -m "feat: add agent system prompts"
```

---

### Task 7: Orchestrator — Main Loop

**Files:**
- Create: `orchestrator.py`
- Create: `tests/__init__.py`
- Create: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/__init__.py
# (empty file to make tests a package)
```

```python
# tests/test_orchestrator.py
import json
import os
import tempfile
from unittest.mock import patch, MagicMock, call
from orchestrator import Orchestrator


def _make_spec():
    return """# Test App

## Vision
Test app.

## Target Users
Testers.

## Features

### P0: Login
- Description: User login
- User Story: As a user, I want to log in
- Acceptance Criteria: Can log in with email/password

### P1: Dashboard
- Description: Main dashboard
- User Story: As a user, I want to see my dashboard
- Acceptance Criteria: Shows summary data

## AI Integration Opportunities
None.

## UX Flow
1. Login -> Dashboard
"""


def test_orchestrator_init_creates_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = Orchestrator(root_dir=tmpdir)
        orch.setup()
        assert os.path.isdir(os.path.join(tmpdir, "comms"))
        assert os.path.isdir(os.path.join(tmpdir, "comms", "screenshots"))
        assert os.path.isdir(os.path.join(tmpdir, "output"))


def test_orchestrator_plan_phase():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = Orchestrator(root_dir=tmpdir)
        orch.setup()

        with patch("orchestrator.call_agent", return_value=_make_spec()):
            orch.plan("Build a test app")

        spec_path = os.path.join(tmpdir, "comms", "spec.md")
        assert os.path.exists(spec_path)

        status = json.load(open(os.path.join(tmpdir, "comms", "status.json")))
        assert len(status["features"]) == 2
        assert status["phase"] == "contracting"


def test_orchestrator_negotiate_contract():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = Orchestrator(root_dir=tmpdir)
        orch.setup()

        # Setup spec
        with patch("orchestrator.call_agent", return_value=_make_spec()):
            orch.plan("Build a test app")

        # Mock: Generator proposes, Evaluator agrees
        contract = "## Feature: Login\n## Evaluator Review\n### Status: AGREED"
        with patch("orchestrator.call_agent", return_value=contract):
            agreed = orch.negotiate_contract(0)

        assert agreed is True


def test_orchestrator_evaluate_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = Orchestrator(root_dir=tmpdir)
        orch.setup()

        eval_result = "### Verdict: PASS"
        with patch("orchestrator.call_agent", return_value=eval_result):
            with patch.object(orch, "server") as mock_server:
                passed = orch.evaluate(0)

        assert passed is True


def test_orchestrator_evaluate_fail():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = Orchestrator(root_dir=tmpdir)
        orch.setup()

        eval_result = "### Verdict: FAIL\n### Required Changes\n1. Fix the button"
        with patch("orchestrator.call_agent", return_value=eval_result):
            with patch.object(orch, "server") as mock_server:
                passed = orch.evaluate(0)

        assert passed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator'`

- [ ] **Step 3: Write orchestrator.py**

```python
# orchestrator.py
import json
import os
import subprocess
import sys

from cli import call_agent
from config import CONFIG, get_harness_root
from state import HarnessState
from spec_parser import parse_features
from server import DevServer


class Orchestrator:
    def __init__(self, root_dir: str | None = None):
        self.root = root_dir or get_harness_root()
        self.comms_dir = os.path.join(self.root, CONFIG["comms_dir"])
        self.output_dir = os.path.join(self.root, CONFIG["output_dir"])
        self.agents_dir = os.path.join(self.root, "agents")
        self.state = HarnessState(self.comms_dir)
        self.server = DevServer(
            self.comms_dir, self.output_dir, CONFIG["dev_server_startup_wait"]
        )

    def setup(self):
        os.makedirs(self.comms_dir, exist_ok=True)
        os.makedirs(os.path.join(self.comms_dir, "screenshots"), exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        self.state.init()
        self._init_output_git()

    def _init_output_git(self):
        git_dir = os.path.join(self.output_dir, ".git")
        if not os.path.isdir(git_dir):
            subprocess.run(
                ["git", "init"], cwd=self.output_dir, capture_output=True
            )

    # ── Phase 1: Planning ──

    def plan(self, user_prompt: str):
        print(f"[Planner] Generating spec from prompt...")
        prompt = (
            f"User request: {user_prompt}\n\n"
            f"Generate a complete product specification. "
            f"Write the full spec content as your response."
        )
        spec_content = call_agent(
            system_prompt_file=os.path.join(self.agents_dir, "planner.md"),
            user_prompt=prompt,
            allowed_tools=["Read", "Write"],
            cwd=self.root,
        )

        spec_path = os.path.join(self.comms_dir, "spec.md")
        with open(spec_path, "w") as f:
            f.write(spec_content)

        features = parse_features(spec_content)
        self.state.set_features(features)
        self.state.set_phase("contracting")
        print(f"[Planner] Spec generated with {len(features)} features.")

    # ── Phase 2: Contract Negotiation ──

    def negotiate_contract(self, feature_index: int) -> bool:
        status = self.state.load()
        feature = status["features"][feature_index]
        spec_path = os.path.join(self.comms_dir, "spec.md")

        for attempt in range(CONFIG["max_contract_negotiations"]):
            # Generator proposes contract
            print(f"[Generator] Proposing contract for '{feature['name']}' (attempt {attempt + 1})...")
            with open(spec_path) as f:
                spec = f.read()

            gen_prompt = (
                f"Read the product spec below and propose a feature contract for: {feature['name']}\n\n"
                f"Spec:\n{spec}\n\n"
                f"Write the contract content as your response in the feature_contract.md format."
            )
            contract = call_agent(
                system_prompt_file=os.path.join(self.agents_dir, "generator.md"),
                user_prompt=gen_prompt,
                allowed_tools=["Read", "Write"],
                cwd=self.output_dir,
            )

            contract_path = os.path.join(self.comms_dir, "feature_contract.md")
            with open(contract_path, "w") as f:
                f.write(contract)

            # Evaluator reviews contract
            print(f"[Evaluator] Reviewing contract...")
            eval_prompt = (
                f"Review the feature contract below. "
                f"Read comms/spec.md for product context.\n\n"
                f"Contract:\n{contract}\n\n"
                f"Write your review as a response. Include '### Status: AGREED' or '### Status: NEEDS_REVISION'."
            )
            review = call_agent(
                system_prompt_file=os.path.join(self.agents_dir, "evaluator.md"),
                user_prompt=eval_prompt,
                allowed_tools=["Read", "Write"],
                cwd=self.root,
            )

            # Update contract file with review
            with open(contract_path, "w") as f:
                f.write(review)

            if "AGREED" in review:
                self.state.update_feature(feature_index, contract_status="agreed")
                print(f"[Contract] Agreed on '{feature['name']}'.")
                return True

            print(f"[Contract] Needs revision (attempt {attempt + 1}/{CONFIG['max_contract_negotiations']}).")

        self.state.update_feature(feature_index, contract_status="failed")
        print(f"[Contract] Failed to agree on '{feature['name']}' after {CONFIG['max_contract_negotiations']} attempts.")
        return False

    # ── Phase 3: Implementation ──

    def implement(self, feature_index: int):
        status = self.state.load()
        feature = status["features"][feature_index]
        contract_path = os.path.join(self.comms_dir, "feature_contract.md")
        eval_path = os.path.join(self.comms_dir, "evaluation.md")

        with open(contract_path) as f:
            contract = f.read()

        eval_context = ""
        if os.path.exists(eval_path):
            with open(eval_path) as f:
                eval_context = f"\n\nPrevious evaluation feedback:\n{f.read()}"

        print(f"[Generator] Implementing '{feature['name']}'...")
        prompt = (
            f"Implement the following feature based on the agreed contract.\n\n"
            f"Contract:\n{contract}\n"
            f"{eval_context}\n\n"
            f"Work in the current directory. Self-verify: build must succeed, basic functionality must work. "
            f"Commit your changes with git."
        )

        call_agent(
            system_prompt_file=os.path.join(self.agents_dir, "generator.md"),
            user_prompt=prompt,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            cwd=self.output_dir,
        )

    # ── Phase 4: Evaluation ──

    def evaluate(self, feature_index: int) -> bool:
        status = self.state.load()
        feature = status["features"][feature_index]
        contract_path = os.path.join(self.comms_dir, "feature_contract.md")

        with open(contract_path) as f:
            contract = f.read()

        attempts = feature["attempts"] + 1

        self.server.start()
        try:
            print(f"[Evaluator] Testing '{feature['name']}' (attempt {attempts}/{CONFIG['max_implementation_attempts']})...")
            prompt = (
                f"Evaluate the running application for feature: {feature['name']}\n\n"
                f"Contract:\n{contract}\n\n"
                f"Attempt: {attempts}/{CONFIG['max_implementation_attempts']}\n\n"
                f"Navigate to {CONFIG['dev_server_url']} and test every checklist item. "
                f"Take screenshots as evidence. Write your evaluation as a response."
            )

            eval_result = call_agent(
                system_prompt_file=os.path.join(self.agents_dir, "evaluator.md"),
                user_prompt=prompt,
                allowed_tools=["Read", "Write"],
                mcp_tools=[CONFIG["mcp_tool"]],
                cwd=self.root,
            )

            eval_path = os.path.join(self.comms_dir, "evaluation.md")
            with open(eval_path, "w") as f:
                f.write(eval_result)
        finally:
            self.server.stop()

        passed = "Verdict: PASS" in eval_result
        self.state.increment_attempts(feature_index)

        if passed:
            self.state.update_feature(feature_index, implementation_status="passed")
            print(f"[Evaluator] PASS — '{feature['name']}'")
        else:
            self.state.update_feature(feature_index, implementation_status="failed")
            print(f"[Evaluator] FAIL — '{feature['name']}'")

        return passed

    # ── Phase 5: Integration Test ──

    def integration_test(self) -> bool:
        spec_path = os.path.join(self.comms_dir, "spec.md")
        with open(spec_path) as f:
            spec = f.read()

        self.server.start()
        try:
            print("[Evaluator] Running integration test...")
            prompt = (
                f"Perform a full integration test of the application.\n\n"
                f"Product spec:\n{spec}\n\n"
                f"Navigate to {CONFIG['dev_server_url']}. Test the complete user journey: "
                f"every feature working together, navigation flow, data persistence across features. "
                f"Take screenshots. Write your evaluation as a response."
            )
            result = call_agent(
                system_prompt_file=os.path.join(self.agents_dir, "evaluator.md"),
                user_prompt=prompt,
                allowed_tools=["Read", "Write"],
                mcp_tools=[CONFIG["mcp_tool"]],
                cwd=self.root,
            )

            eval_path = os.path.join(self.comms_dir, "evaluation.md")
            with open(eval_path, "w") as f:
                f.write(result)
        finally:
            self.server.stop()

        return "Verdict: PASS" in result

    # ── Main Run Loop ──

    def run(self, user_prompt: str):
        print("=" * 60)
        print("Multi-Agent Coding Harness")
        print("=" * 60)

        self.setup()

        # Phase 1: Plan
        self.plan(user_prompt)

        # Phase 2-4: Feature loop
        status = self.state.load()
        self.state.set_phase("implementing")

        for i, feature in enumerate(status["features"]):
            self.state.set_current_feature(i)
            print(f"\n{'─' * 40}")
            print(f"Feature {i + 1}/{len(status['features'])}: {feature['name']} [{feature['priority']}]")
            print(f"{'─' * 40}")

            # Negotiate contract
            agreed = self.negotiate_contract(i)
            if not agreed:
                print(f"[Skip] Could not agree on contract for '{feature['name']}'.")
                continue

            # Implement + Evaluate loop
            for attempt in range(CONFIG["max_implementation_attempts"]):
                self.implement(i)
                passed = self.evaluate(i)
                if passed:
                    break

                if attempt == CONFIG["max_implementation_attempts"] - 1:
                    print(f"[Skip] '{feature['name']}' failed after {CONFIG['max_implementation_attempts']} attempts.")
                    response = input("Continue to next feature? (y/n): ").strip().lower()
                    if response == "n":
                        print("[Abort] Stopping harness.")
                        return

        # Phase 5: Integration test
        self.state.set_phase("integration_testing")
        for attempt in range(CONFIG["max_integration_attempts"]):
            passed = self.integration_test()
            if passed:
                break
            if attempt < CONFIG["max_integration_attempts"] - 1:
                print("[Integration] FAIL — requesting fixes...")
                self.implement_integration_fixes()

        # Done
        self.state.set_phase("completed")
        self._print_report()

    def implement_integration_fixes(self):
        eval_path = os.path.join(self.comms_dir, "evaluation.md")
        with open(eval_path) as f:
            feedback = f.read()

        prompt = (
            f"The integration test found issues. Fix them.\n\n"
            f"Feedback:\n{feedback}\n\n"
            f"Fix only what is described. Commit changes."
        )
        call_agent(
            system_prompt_file=os.path.join(self.agents_dir, "generator.md"),
            user_prompt=prompt,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            cwd=self.output_dir,
        )

    def _print_report(self):
        status = self.state.load()
        print("\n" + "=" * 60)
        print("FINAL REPORT")
        print("=" * 60)
        for f in status["features"]:
            icon = "✓" if f["implementation_status"] == "passed" else "✗"
            print(f"  {icon} {f['name']} [{f['priority']}] — {f['implementation_status']} (attempts: {f['attempts']})")
        print(f"\nIntegration test: {status['integration_test']['status']}")
        print("=" * 60)


def main():
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py \"<your product idea>\"")
        sys.exit(1)

    user_prompt = " ".join(sys.argv[1:])
    orch = Orchestrator()
    orch.run(user_prompt)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jack/client/harness && python -m pytest tests/test_orchestrator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/__init__.py tests/test_orchestrator.py
git commit -m "feat: add orchestrator main loop"
```

---

### Task 8: End-to-End Smoke Test

**Files:**
- No new files — validates everything works together

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/jack/client/harness && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Verify CLI entry point works**

Run: `cd /Users/jack/client/harness && python orchestrator.py 2>&1 | head -5`
Expected: `Usage: python orchestrator.py "<your product idea>"`

- [ ] **Step 3: Final commit with all files**

```bash
git add -A
git commit -m "feat: complete multi-agent coding harness scaffold"
```
