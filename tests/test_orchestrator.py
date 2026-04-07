import json
import os
import tempfile
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from cli import AgentResult, AgentTimeout, InfraError
from config import CONFIG, CONTRACT_AGREED, TOOLS_EVALUATOR
from orchestrator import Orchestrator, UserAbort, _check_verdict_pass, _check_contract_agreed, _parse_failed_parts, _parse_automation_limited


SAMPLE_SPEC = """# Test App

## Vision
Test app.

## Target Users
Testers.

## Features

### P0: Login
- Description: User login

### P1: Dashboard
- Description: Main dashboard

## UX Flow
1. Login -> Dashboard

## Sprints

### Sprint 1: Foundation
Features: Login
Goal: Users can log in

### Sprint 2: Dashboard
Features: Dashboard
Goal: Users see a dashboard
"""

SAMPLE_SPEC_NO_SPRINTS = """# Test App

## Vision
Test app.

## Features

### P0: Login
- Description: User login

## UX Flow
1. Login
"""


def _mock_result(text: str) -> AgentResult:
    return AgentResult(result=text, input_tokens=100, output_tokens=50, turns=3)


def _setup_orch(tmpdir):
    orch = Orchestrator(root_dir=tmpdir)
    orch.setup()
    agents_dir = os.path.join(tmpdir, "agents")
    os.makedirs(agents_dir, exist_ok=True)
    for name in ("planner", "generator", "evaluator"):
        with open(os.path.join(agents_dir, f"{name}.md"), "w") as f:
            f.write(f"# {name} prompt")
    return orch


def test_orchestrator_init_creates_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        assert os.path.isdir(os.path.join(tmpdir, "comms"))
        assert os.path.isdir(os.path.join(tmpdir, "output"))


# --- Verdict / Contract parsing ---

def test_verdict_pass_strict_match():
    assert _check_verdict_pass("### Verdict: PASS") is True
    assert _check_verdict_pass("Verdict: PASS") is True
    assert _check_verdict_pass("## Verdict: PASS") is True


def test_verdict_pass_rejects_false_positives():
    assert _check_verdict_pass("Expected Verdict: PASS but crashed") is False
    assert _check_verdict_pass("Verdict: PASS with caveats") is False
    assert _check_verdict_pass("The Verdict: PASS should not match") is False


def test_verdict_fail():
    assert _check_verdict_pass("### Verdict: FAIL") is False


def test_contract_agreed_strict_match():
    assert _check_contract_agreed("AGREED\nLooks good.") is True
    assert _check_contract_agreed("AGREED") is True


def test_contract_agreed_rejects_false_positives():
    assert _check_contract_agreed("NOT AGREED") is False
    assert _check_contract_agreed("We have NOT AGREED on this.") is False


# --- Plan phase ---

@pytest.mark.anyio
async def test_orchestrator_plan_phase():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        async def mock_planner(*args, **kwargs):
            with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
                f.write(SAMPLE_SPEC)
            return _mock_result("Spec written.")

        with patch("orchestrator.call_agent", side_effect=mock_planner):
            await orch.plan("Build a test app")

        assert os.path.exists(os.path.join(tmpdir, "comms", "spec.md"))
        status = json.load(open(os.path.join(tmpdir, "comms", "status.json")))
        assert status["phase"] == "building"


@pytest.mark.anyio
async def test_orchestrator_plan_parses_sprints():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        async def mock_planner(*args, **kwargs):
            with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
                f.write(SAMPLE_SPEC)
            return _mock_result("Spec written.")

        with patch("orchestrator.call_agent", side_effect=mock_planner):
            await orch.plan("Build a test app")

        assert len(orch.sprints) == 2
        assert orch.sprints[0].name == "Foundation"
        assert orch.sprints[1].name == "Dashboard"


@pytest.mark.anyio
async def test_orchestrator_plan_fallback_single_sprint():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        async def mock_planner(*args, **kwargs):
            with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
                f.write(SAMPLE_SPEC_NO_SPRINTS)
            return _mock_result("Spec written.")

        with patch("orchestrator.call_agent", side_effect=mock_planner):
            await orch.plan("Build a test app")

        assert len(orch.sprints) == 1
        assert orch.sprints[0].name == "Full Build"


# --- Contract negotiation ---

@pytest.mark.anyio
async def test_orchestrator_negotiate_contract():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None  # force re-read
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")

        call_count = 0

        async def mock_negotiate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
            os.makedirs(sprint_dir, exist_ok=True)
            if call_count == 1:
                with open(os.path.join(sprint_dir, "contract_proposal.md"), "w") as f:
                    f.write("# Sprint Contract\n## Testable Criteria\n1. Login works")
                return _mock_result("Proposal written.")
            else:
                with open(os.path.join(sprint_dir, "contract_review.md"), "w") as f:
                    f.write("AGREED\nLooks good.")
                return _mock_result("AGREED\nLooks good.")

        with patch("orchestrator.call_agent", side_effect=mock_negotiate):
            contract_path = await orch.negotiate_contract(sprint)

        assert os.path.exists(contract_path)
        assert "sprint-1" in contract_path


# --- Build ---

@pytest.mark.anyio
async def test_orchestrator_build_with_sprint():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None  # force re-read
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock = AsyncMock(return_value=_mock_result("Built successfully"))
        with patch("orchestrator.call_agent", mock):
            await orch.build(sprint, contract_path)

        assert orch.state.get_sprint_attempt(1, "build") == 1


# --- Evaluate ---

@pytest.mark.anyio
async def test_orchestrator_evaluate_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None  # force re-read
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock = AsyncMock(return_value=_mock_result("### Verdict: PASS"))
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server"):
                passed = await orch.evaluate(sprint, contract_path)

        assert passed is True


@pytest.mark.anyio
async def test_orchestrator_evaluate_fail():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None  # force re-read
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock = AsyncMock(return_value=_mock_result("### Verdict: FAIL\n### Required Changes\n1. Fix the button"))
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server"):
                passed = await orch.evaluate(sprint, contract_path)

        assert passed is False


# --- Slug ---

@pytest.mark.anyio
async def test_slug_ascii_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        spec_with_korean = "# Mystic Arcana — AI 타로 리딩 웹 앱\n\n## Vision\nTest.\n"
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(spec_with_korean)
        orch._spec = None  # force re-read
        orch._init_output()
        assert "타로" not in orch.output_dir
        assert "mystic-arcana" in orch.output_dir


# --- Cost tracking ---

@pytest.mark.anyio
async def test_plan_tracks_cost():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        async def mock_planner(*args, **kwargs):
            with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
                f.write(SAMPLE_SPEC)
            return AgentResult(result="Done", input_tokens=5000, output_tokens=2000, turns=5)

        with patch("orchestrator.call_agent", side_effect=mock_planner):
            await orch.plan("Build a test app")

        status = orch.state.load()
        assert status["cost"]["input_tokens"] == 5000
        assert status["cost"]["output_tokens"] == 2000


# --- InfraError stops retries ---

@pytest.mark.anyio
async def test_retry_stops_on_infra_error():
    """InfraError should stop retries immediately — no further build attempts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        build_count = 0

        async def mock_build(s, cp):
            nonlocal build_count
            build_count += 1

        async def mock_evaluate(s, cp):
            raise InfraError("JSON message exceeded maximum buffer size of 1048576 bytes")

        with patch.object(orch, "build", side_effect=mock_build):
            with patch.object(orch, "evaluate", side_effect=mock_evaluate):
                passed = await orch._retry_build_eval(sprint, contract_path, "Sprint 1")

        assert passed is False
        # Should have only attempted ONE build, not max_build_attempts
        assert build_count == 1


# --- UserAbort on max failures ---

@pytest.mark.anyio
async def test_retry_asks_user_on_max_failures_continue():
    """After max_build_attempts failures, user is asked. Choosing continue still marks sprint as FAIL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock_build = AsyncMock()
        mock_eval = AsyncMock(return_value=False)  # evaluate returns bool

        with patch.object(orch, "build", mock_build):
            with patch.object(orch, "evaluate", mock_eval):
                with patch("orchestrator.input", return_value="c"):  # continue
                    passed = await orch._retry_build_eval(sprint, contract_path, "Sprint 1")

        assert passed is False
        assert mock_build.call_count == CONFIG["max_build_attempts"]


@pytest.mark.anyio
async def test_retry_asks_user_on_max_failures_abort():
    """After max_build_attempts failures, user choosing abort raises UserAbort."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock_build = AsyncMock()
        mock_eval = AsyncMock(return_value=False)  # evaluate returns bool

        with patch.object(orch, "build", mock_build):
            with patch.object(orch, "evaluate", mock_eval):
                with patch("orchestrator.input", return_value="a"):  # abort
                    with pytest.raises(UserAbort):
                        await orch._retry_build_eval(sprint, contract_path, "Sprint 1")


# --- Integration evaluation ---

@pytest.mark.anyio
async def test_integration_eval_pass():
    """Integration eval after all sprints completes with PASS."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        mock = AsyncMock(return_value=_mock_result("### Verdict: PASS"))
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server", MagicMock()):
                await orch._integration_eval()

        status = orch.state.load()
        assert status["sprint_results"]["0"] is True


@pytest.mark.anyio
async def test_integration_eval_fail():
    """Integration eval fails after max_build_attempts with build-fix-eval loop."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        mock = AsyncMock(return_value=_mock_result("### Verdict: FAIL\n### Required Changes\n1. Fix everything"))
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server", MagicMock()):
                with patch("orchestrator._ask_user_continue"):
                    await orch._integration_eval()

        # max_build_attempts evals + (max_build_attempts - 1) generator fixes
        max_attempts = CONFIG["max_build_attempts"]
        expected_calls = max_attempts + (max_attempts - 1)  # evals + fixes
        assert mock.call_count == expected_calls
        status = orch.state.load()
        assert status["sprint_results"]["0"] is False


# --- Evaluator uses TOOLS_EVALUATOR not TOOLS_READ_WRITE ---

@pytest.mark.anyio
async def test_evaluator_uses_restricted_tools():
    """Evaluator agent must be called with TOOLS_EVALUATOR (no Read)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        captured_calls = []

        async def mock_call(*args, **kwargs):
            captured_calls.append(kwargs)
            return _mock_result("### Verdict: PASS")

        with patch("orchestrator.call_agent", side_effect=mock_call):
            with patch.object(orch, "server", MagicMock()):
                await orch.evaluate(sprint, contract_path)

        # The evaluator call should use TOOLS_EVALUATOR
        eval_call = captured_calls[0]
        assert "Read" not in eval_call["allowed_tools"]
        assert "Write" in eval_call["allowed_tools"]


# --- CONTRACT_AGREED used in negotiation ---

def test_contract_agreed_used_in_check():
    """_check_contract_agreed must match the CONTRACT_AGREED constant."""
    assert _check_contract_agreed(f"{CONTRACT_AGREED}\nLooks good.") is True
    assert _check_contract_agreed(f"NOT {CONTRACT_AGREED}") is False


# --- Simple mode ---

@pytest.mark.anyio
async def test_run_simple_generates_criteria():
    """Simple mode generates testable criteria before building."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        criteria_generated = False
        build_called = False
        eval_called = False

        async def mock_generate_criteria():
            nonlocal criteria_generated
            criteria_generated = True
            criteria_path = os.path.join(tmpdir, "comms", "testable_criteria.md")
            with open(criteria_path, "w") as f:
                f.write("- [ ] User can log in\n- [ ] Data persists after refresh\n")
            return criteria_path

        async def mock_build(sprint, contract_path):
            nonlocal build_called
            build_called = True
            # Contract should contain criteria, not raw spec
            with open(contract_path) as f:
                content = f.read()
            assert "- [ ]" in content  # criteria format, not spec

        async def mock_evaluate(sprint, contract_path):
            nonlocal eval_called
            eval_called = True
            return True

        with patch.object(orch, "generate_criteria", side_effect=mock_generate_criteria):
            with patch.object(orch, "build", side_effect=mock_build):
                with patch.object(orch, "evaluate", side_effect=mock_evaluate):
                    await orch._run_simple()

        assert criteria_generated
        assert build_called
        assert eval_called
        assert orch.state.get_sprint_results().get(1) is True


# --- Parse failed parts ---

def test_parse_failed_parts_both_pass():
    text = """
### Quality Assessment — Frontend
| Criterion | Verdict |
|-----------|---------|
| Design Quality | PASS |
| Originality | PASS |
| Craft | PASS |
| UI Functionality | PASS |

### Quality Assessment — Backend
| Criterion | Verdict |
|-----------|---------|
| Product Depth | PASS |
| Functionality | PASS |
| Code Quality | PASS |
"""
    fe, be = _parse_failed_parts(text)
    assert fe is True
    assert be is True


def test_parse_failed_parts_frontend_fail():
    text = """
### Quality Assessment — Frontend
| Criterion | Verdict |
|-----------|---------|
| Design Quality | FAIL |
| Originality | FAIL |
| Craft | PASS |
| UI Functionality | PASS |

### Quality Assessment — Backend
| Criterion | Verdict |
|-----------|---------|
| Product Depth | PASS |
| Functionality | PASS |
| Code Quality | PASS |
"""
    fe, be = _parse_failed_parts(text)
    assert fe is False
    assert be is True


def test_parse_failed_parts_backend_fail():
    text = """
### Quality Assessment — Frontend
| Criterion | Verdict |
|-----------|---------|
| Design Quality | PASS |
| Originality | PASS |
| Craft | PASS |
| UI Functionality | PASS |

### Quality Assessment — Backend
| Criterion | Verdict |
|-----------|---------|
| Product Depth | FAIL |
| Functionality | PASS |
| Code Quality | PASS |
"""
    fe, be = _parse_failed_parts(text)
    assert fe is True
    assert be is False


def test_parse_failed_parts_both_fail():
    text = """
| Design Quality | FAIL |
| Functionality | FAIL |
"""
    fe, be = _parse_failed_parts(text)
    assert fe is False
    assert be is False


# --- AgentTimeout stops retries ---

@pytest.mark.anyio
async def test_retry_stops_on_timeout():
    """AgentTimeout should stop retries immediately — agent is likely stuck."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        build_count = 0

        async def mock_build(s, cp):
            nonlocal build_count
            build_count += 1

        async def mock_evaluate(s, cp):
            raise AgentTimeout("Agent timed out after 45m")

        with patch.object(orch, "build", side_effect=mock_build):
            with patch.object(orch, "evaluate", side_effect=mock_evaluate):
                passed = await orch._retry_build_eval(sprint, contract_path, "Sprint 1")

        assert passed is False
        # Should have only attempted ONE build, not max_build_attempts
        assert build_count == 1


# --- Automation-limited parsing ---

def test_parse_automation_limited_counts_correctly():
    text = """
- [x] Feature that works (tested via click) | screenshots/test.png
- [ ] Feature that is broken ← FAIL | screenshots/fail.png
- [x] Another passing feature | screenshots/pass.png
- [ ] Drag-and-drop — automation-limited | screenshots/drag.png
"""
    limited, total = _parse_automation_limited(text)
    assert total == 4
    assert limited == 1


def test_parse_automation_limited_ignores_markdown_links():
    """Markdown links like - [text](url) should NOT count as criteria."""
    text = """
- [x] Real criterion passes | screenshots/test.png
- [ ] Real criterion fails | screenshots/fail.png
- [See documentation](https://example.com) for more info
- [Link text](url) should not match
"""
    limited, total = _parse_automation_limited(text)
    assert total == 2
    assert limited == 0


def test_parse_automation_limited_empty():
    limited, total = _parse_automation_limited("No criteria here.")
    assert total == 0
    assert limited == 0
