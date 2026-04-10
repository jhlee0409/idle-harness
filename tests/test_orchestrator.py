import json
import os
import tempfile
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from cli import AgentResult, AgentTimeout, InfraError
from config import CONFIG, CONTRACT_AGREED, TOOLS_EVALUATOR
from orchestrator import (
    Orchestrator, UserAbort, _check_verdict_pass, _check_contract_agreed,
    _parse_failed_parts, _parse_automation_limited,
    parse_criteria_sections, assign_sections_to_evaluators, CriteriaSection,
    merge_evaluations,
)


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


def test_verdict_parallel_fail_overrides_pass():
    """In parallel eval, ANY FAIL verdict = overall FAIL."""
    merged = (
        "--- Evaluator 0 ---\n"
        "### Verdict: FAIL\n"
        "--- Evaluator 1 ---\n"
        "### Verdict: PASS\n"
    )
    assert _check_verdict_pass(merged) is False


def test_verdict_parallel_all_pass():
    """All evaluators PASS = overall PASS."""
    merged = (
        "--- Evaluator 0 ---\n"
        "### Verdict: PASS\n"
        "--- Evaluator 1 ---\n"
        "### Verdict: PASS\n"
    )
    assert _check_verdict_pass(merged) is True


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

        with patch.object(orch, "build", side_effect=mock_build), \
             patch.object(orch, "evaluate", side_effect=mock_evaluate), \
             patch.object(orch, "verify", AsyncMock(return_value=None)), \
             patch.object(orch, "_smoke_test", AsyncMock(return_value=(True, "OK"))):
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

        with patch.object(orch, "build", mock_build), \
             patch.object(orch, "evaluate", mock_eval), \
             patch.object(orch, "verify", AsyncMock(return_value=None)), \
             patch.object(orch, "_smoke_test", AsyncMock(return_value=(True, "OK"))), \
             patch("orchestrator.input", return_value="c"):  # continue
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

        with patch.object(orch, "build", mock_build), \
             patch.object(orch, "evaluate", mock_eval), \
             patch.object(orch, "verify", AsyncMock(return_value=None)), \
             patch.object(orch, "_smoke_test", AsyncMock(return_value=(True, "OK"))), \
             patch("orchestrator.input", return_value="a"):  # abort
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

        with patch.object(orch, "generate_criteria", side_effect=mock_generate_criteria), \
             patch.object(orch, "build", side_effect=mock_build), \
             patch.object(orch, "evaluate", side_effect=mock_evaluate), \
             patch.object(orch, "verify", AsyncMock(return_value=None)), \
             patch.object(orch, "_smoke_test", AsyncMock(return_value=(True, "OK"))):
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

        with patch.object(orch, "build", side_effect=mock_build), \
             patch.object(orch, "evaluate", side_effect=mock_evaluate), \
             patch.object(orch, "verify", AsyncMock(return_value=None)), \
             patch.object(orch, "_smoke_test", AsyncMock(return_value=(True, "OK"))):
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


# --- Verifier integration ---

@pytest.mark.anyio
async def test_verifier_fail_skips_evaluate():
    """When verifier FAILs, evaluate() should NOT be called."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        from verifier import VerificationResult, CheckResult, CheckType, CheckStatus
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n- [x] API works")

        build_called = False
        eval_called = False

        async def mock_build(s, cp):
            nonlocal build_called
            build_called = True

        async def mock_evaluate(s, cp):
            nonlocal eval_called
            eval_called = True
            return True

        # Verifier returns FAIL
        vresult = VerificationResult(
            failed=[CheckResult("API broken", CheckType.API, CheckStatus.FAIL, message="500")],
        )

        async def mock_verify(s, cp):
            return vresult

        with patch.object(orch, "build", side_effect=mock_build), \
             patch.object(orch, "evaluate", side_effect=mock_evaluate), \
             patch.object(orch, "verify", side_effect=mock_verify), \
             patch.object(orch, "_smoke_test", AsyncMock(return_value=(True, "OK"))), \
             patch.dict("config.CONFIG", {"max_build_attempts": 1}), \
             patch("orchestrator._ask_user_continue"):
            await orch._retry_build_eval(sprint, contract_path, "Build")

        assert build_called
        assert not eval_called  # evaluate should NOT have been called


@pytest.mark.anyio
async def test_verifier_pass_proceeds_to_evaluate():
    """When verifier PASSes, evaluate() should be called."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        from verifier import VerificationResult
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n- [x] API works")

        eval_called = False

        async def mock_build(s, cp):
            pass

        async def mock_evaluate(s, cp):
            nonlocal eval_called
            eval_called = True
            return True

        # Verifier returns PASS (no failures)
        vresult = VerificationResult(overall_pass=True)

        async def mock_verify(s, cp):
            return vresult

        with patch.object(orch, "build", side_effect=mock_build), \
             patch.object(orch, "evaluate", side_effect=mock_evaluate), \
             patch.object(orch, "verify", side_effect=mock_verify), \
             patch.object(orch, "_smoke_test", AsyncMock(return_value=(True, "OK"))):
            passed = await orch._retry_build_eval(sprint, contract_path, "Build")

        assert eval_called
        assert passed


# --- Parse eval score ---

def test_parse_eval_score_mixed():
    from orchestrator import _parse_eval_score
    text = """
- [x] Feature A works | screenshots/a.png
- [x] Feature B works | screenshots/b.png
- [ ] Feature C broken ← FAIL | screenshots/c.png
- [x] Feature D works | screenshots/d.png
"""
    passed, total = _parse_eval_score(text)
    assert passed == 3
    assert total == 4


def test_parse_eval_score_empty():
    from orchestrator import _parse_eval_score
    assert _parse_eval_score("No criteria here") == (0, 0)


def test_parse_eval_score_all_pass():
    from orchestrator import _parse_eval_score
    text = "- [x] A\n- [x] B\n- [x] C\n"
    passed, total = _parse_eval_score(text)
    assert passed == 3
    assert total == 3


# --- Evaluation preservation (overwrite bug fix) ---

@pytest.mark.anyio
async def test_evaluate_preserves_disk_eval_when_more_detailed():
    """When evaluator writes detailed eval to disk via Write tool and returns
    a summary, the disk version should be preserved."""
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
            f.write("# Contract\n- [ ] Login works\n- [ ] Data persists")

        # Simulate evaluator writing detailed eval to disk (via Write tool)
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        detailed_eval = (
            "- [x] Login works | screenshots/login.png\n"
            "- [ ] Data persists ← FAIL | screenshots/persist.png\n"
            "### Verdict: FAIL\n"
        )
        with open(eval_path, "w") as f:
            f.write(detailed_eval)

        # Agent returns only a summary (the bug case)
        summary_response = "Evaluation complete.\n### Verdict: FAIL\n55/71 criteria passed."

        mock = AsyncMock(return_value=_mock_result(summary_response))
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server"):
                passed = await orch.evaluate(sprint, contract_path)

        assert passed is False
        # The detailed version should be preserved on disk
        with open(eval_path) as f:
            content = f.read()
        assert "- [x] Login works" in content
        assert "- [ ] Data persists" in content

        # Attempt file should also have the detailed version
        attempt_path = os.path.join(sprint_dir, "evaluation_attempt_1.md")
        with open(attempt_path) as f:
            attempt_content = f.read()
        assert "- [x] Login works" in attempt_content


@pytest.mark.anyio
async def test_evaluate_uses_response_when_no_disk_eval():
    """When no prior eval exists on disk, agent response is used normally."""
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
            f.write("# Contract\n- [ ] Login works")

        response = "- [x] Login works | screenshots/login.png\n### Verdict: PASS\n"
        mock = AsyncMock(return_value=_mock_result(response))
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server"):
                passed = await orch.evaluate(sprint, contract_path)

        assert passed is True
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        with open(eval_path) as f:
            content = f.read()
        assert "- [x] Login works" in content


# --- Smoke test ---

@pytest.mark.anyio
async def test_smoke_test_fail_skips_eval():
    """When smoke test fails, evaluate should not be called."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n- [ ] App loads")

        build_called = False
        eval_called = False

        async def mock_build(s, cp):
            nonlocal build_called
            build_called = True

        async def mock_evaluate(s, cp):
            nonlocal eval_called
            eval_called = True
            return True

        with patch.object(orch, "build", side_effect=mock_build), \
             patch.object(orch, "evaluate", side_effect=mock_evaluate), \
             patch.object(orch, "verify", AsyncMock(return_value=None)), \
             patch.object(orch, "_smoke_test", AsyncMock(return_value=(False, "Server crashed"))), \
             patch.dict("config.CONFIG", {"max_build_attempts": 1}), \
             patch("orchestrator._ask_user_continue"):
            await orch._retry_build_eval(sprint, contract_path, "Build")

        assert build_called
        assert not eval_called  # smoke fail should skip eval

        # Smoke failure should be written as evaluation feedback
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        with open(eval_path) as f:
            content = f.read()
        assert "Smoke Test: FAIL" in content
        assert "Server crashed" in content


# --- Regression detection ---

@pytest.mark.anyio
async def test_regression_resets_generator_session():
    """Score dropping >20pp from best should reset Generator session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")

        # Simulate score history: 87% → 28% (>20pp drop)
        orch._generator_session_id = "existing-session-123"
        orch.state.add_eval_score(1, 1, 87, 100)
        orch.state.add_eval_score(1, 2, 28, 100)

        orch._check_regression(sprint, "Sprint 1")

        # Session should have been reset
        assert orch._generator_session_id is None


@pytest.mark.anyio
async def test_no_regression_keeps_session():
    """Small score improvements should not reset the session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")

        orch._generator_session_id = "existing-session-123"
        orch.state.add_eval_score(1, 1, 70, 100)
        orch.state.add_eval_score(1, 2, 75, 100)

        orch._check_regression(sprint, "Sprint 1")

        # Session should still be intact
        assert orch._generator_session_id == "existing-session-123"


@pytest.mark.anyio
async def test_downward_trend_resets_session():
    """Three consecutive drops should reset the session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")

        orch._generator_session_id = "existing-session-123"
        orch.state.add_eval_score(1, 1, 80, 100)
        orch.state.add_eval_score(1, 2, 75, 100)
        orch.state.add_eval_score(1, 3, 70, 100)

        orch._check_regression(sprint, "Sprint 1")

        assert orch._generator_session_id is None


# --- Eval score tracking in state ---

def test_state_eval_score_tracking():
    """State should track eval scores and return history."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from state import HarnessState
        state = HarnessState(tmpdir)
        state.init()

        state.add_eval_score(1, 1, 73, 84)
        state.add_eval_score(1, 2, 41, 146)
        state.add_eval_score(2, 1, 50, 100)

        # All scores
        all_scores = state.get_eval_scores()
        assert len(all_scores) == 3

        # Sprint 1 only
        s1 = state.get_eval_scores(1)
        assert len(s1) == 2
        assert s1[0]["pct"] == 86  # 73/84
        assert s1[1]["pct"] == 28  # 41/146

        # Last score
        last = state.get_last_eval_score(1)
        assert last["passed"] == 41
        assert last["total"] == 146

        # No scores for sprint 3
        assert state.get_last_eval_score(3) is None


# --- Self-eval honesty (integration-level) ---

@pytest.mark.anyio
async def test_self_eval_discrepancy_logged():
    """When self-eval claims 100% but last eval was <80%, discrepancy is logged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n- [ ] Feature A\n- [ ] Feature B")

        # Record a previous eval score of 50%
        orch.state.add_eval_score(1, 1, 50, 100)

        # Generator claims 100% in self_eval
        self_eval_content = (
            "# Self-Evaluation\n"
            "- [x] Feature A\n- [x] Feature B\n"
            "Self-eval pass rate: 2/2 (100%)\n"
        )

        # Mock call_agent to write self_eval.md
        async def mock_call_agent(**kwargs):
            # Write self_eval.md like the generator would
            with open(os.path.join(sprint_dir, "self_eval.md"), "w") as f:
                f.write(self_eval_content)
            return _mock_result("Build complete")

        with patch("orchestrator.call_agent", side_effect=mock_call_agent):
            await orch.build(sprint, contract_path)

        # The build should complete. The discrepancy is logged, not raised.
        # We verify by checking the log file for the warning.
        log_path = os.path.join(orch.output_dir, "harness.log")
        with open(log_path) as f:
            log_content = f.read()
        assert "SELF-EVAL DISCREPANCY" in log_content


# --- Previous evaluation passed to Evaluator ---

@pytest.mark.anyio
async def test_evaluator_receives_previous_eval_for_regression_comparison():
    """On attempt 2+, Evaluator should receive the previous evaluation for comparison."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n- [ ] Login works\n- [ ] Data persists")

        # Simulate attempt 1 already happened (write previous eval attempt)
        prev_eval = (
            "- [x] Login works | screenshots/login.png\n"
            "- [ ] Data persists ← FAIL | screenshots/persist.png\n"
            "### Verdict: FAIL\n"
        )
        with open(os.path.join(sprint_dir, "evaluation_attempt_1.md"), "w") as f:
            f.write(prev_eval)
        # Set attempt counter so next eval is attempt 2
        orch.state.increment(1, "eval")  # attempt becomes 1

        captured_prompt = None

        async def mock_call_agent(**kwargs):
            nonlocal captured_prompt
            captured_prompt = kwargs.get("user_prompt", "")
            return _mock_result("- [x] Login works\n- [x] Data persists\n### Verdict: PASS")

        with patch("orchestrator.call_agent", side_effect=mock_call_agent):
            with patch.object(orch, "server"):
                await orch.evaluate(sprint, contract_path)

        # Evaluator prompt should contain previous evaluation
        assert "Previous Evaluation" in captured_prompt
        assert "REGRESSION" in captured_prompt
        assert "Login works" in captured_prompt


@pytest.mark.anyio
async def test_evaluator_no_previous_eval_on_first_attempt():
    """First evaluation attempt should NOT include previous eval section."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n- [ ] Login works")

        captured_prompt = None

        async def mock_call_agent(**kwargs):
            nonlocal captured_prompt
            captured_prompt = kwargs.get("user_prompt", "")
            return _mock_result("### Verdict: PASS")

        with patch("orchestrator.call_agent", side_effect=mock_call_agent):
            with patch.object(orch, "server"):
                await orch.evaluate(sprint, contract_path)

        assert "Previous Evaluation" not in captured_prompt


# --- Criteria section parsing ---

SAMPLE_CRITERIA = """\
# App — Testable Criteria

### Visual Design — Colors
- [ ] Background is #080809
- [ ] Buttons use amber #D4A843

### P0: Playlist Management
- [ ] Create playlist works
- [ ] Delete playlist shows confirmation
- [ ] Playlist persists after refresh

### P1: Search
- [ ] Typing queries API
- [ ] Results show album art

### Responsive Design
- [ ] Mobile 375px no horizontal scroll
"""


def test_parse_criteria_sections():
    sections = parse_criteria_sections(SAMPLE_CRITERIA)
    assert len(sections) == 4
    assert sections[0].header == "Visual Design — Colors"
    assert sections[0].criteria_count == 2
    assert sections[1].header == "P0: Playlist Management"
    assert sections[1].criteria_count == 3
    assert sections[2].header == "P1: Search"
    assert sections[2].criteria_count == 2
    assert sections[3].header == "Responsive Design"
    assert sections[3].criteria_count == 1


def test_parse_criteria_sections_empty():
    sections = parse_criteria_sections("No sections here")
    assert len(sections) == 0


def test_assign_sections_single_bucket():
    """Few criteria → single bucket, no parallelism."""
    sections = parse_criteria_sections(SAMPLE_CRITERIA)
    buckets = assign_sections_to_evaluators(sections, target_per_evaluator=25)
    assert len(buckets) == 1
    assert sum(s.criteria_count for s in buckets[0]) == 8


def test_assign_sections_parallel():
    """Many criteria → multiple buckets with lead separation."""
    # Create sections with enough criteria to trigger parallel
    sections = [
        CriteriaSection("Visual Design — Colors", "- [ ] a\n- [ ] b\n- [ ] c", 3),
        CriteriaSection("Responsive Design", "- [ ] d\n- [ ] e", 2),
        CriteriaSection("UI States", "- [ ] f\n- [ ] g", 2),
        CriteriaSection("P0: Canvas", "- [ ] h\n" * 10, 10),
        CriteriaSection("P0: Board", "- [ ] i\n" * 8, 8),
        CriteriaSection("P1: Collab", "- [ ] j\n" * 7, 7),
        CriteriaSection("P1: AI", "- [ ] k\n" * 6, 6),
        CriteriaSection("P2: Export", "- [ ] l\n" * 5, 5),
    ]
    buckets = assign_sections_to_evaluators(sections, target_per_evaluator=10)

    # Bucket 0 = lead (Visual Design, Responsive, UI States)
    lead_headers = {s.header for s in buckets[0]}
    assert "Visual Design — Colors" in lead_headers
    assert "Responsive Design" in lead_headers
    assert "UI States" in lead_headers

    # Feature sections distributed across other buckets
    assert len(buckets) >= 3  # lead + at least 2 feature buckets
    all_feature_headers = set()
    for bucket in buckets[1:]:
        for s in bucket:
            all_feature_headers.add(s.header)
    assert "P0: Canvas" in all_feature_headers
    assert "P1: AI" in all_feature_headers


# --- Evaluation merging ---

def test_merge_evaluations_combines_criteria():
    eval_a = (
        "#### Visual Design\n"
        "- [x] Background is #080809 | screenshots/a.png\n"
        "- [ ] Noise grain missing ← FAIL | screenshots/b.png\n"
        "\n### Quality Assessment — Frontend\n"
        "| Criterion | Verdict |\n| Design Quality | PASS |\n"
        "### Verdict: FAIL\n"
        "### Required Changes\n1. Add noise grain\n"
    )
    eval_b = (
        "#### P0: Canvas\n"
        "- [x] Create playlist works | screenshots/c.png\n"
        "- [x] Delete shows confirmation | screenshots/d.png\n"
        "- [ ] Drag broken ← FAIL | screenshots/e.png\n"
    )
    merged = merge_evaluations([eval_a, eval_b], lead_index=0)
    # All 5 criteria present
    assert "- [x] Background" in merged
    assert "- [ ] Noise grain" in merged
    assert "- [x] Create playlist" in merged
    assert "- [ ] Drag broken" in merged
    # Pass rate recomputed: 3/5
    assert "3/5" in merged
    # Quality Assessment from lead
    assert "Design Quality" in merged
    assert "Verdict: FAIL" in merged
    assert "Add noise grain" in merged


def test_merge_evaluations_single():
    """Single evaluator returns text unchanged."""
    text = "- [x] Works\n### Verdict: PASS"
    assert merge_evaluations([text]) == text


# --- Parallel evaluation dispatch ---

@pytest.mark.anyio
async def test_evaluate_dispatches_single_for_small_criteria():
    """Small criteria set → single evaluator path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        # Small criteria — single section, should route to _evaluate_single
        with open(contract_path, "w") as f:
            f.write("### Login\n- [ ] User can log in\n- [ ] Data persists")

        single_called = False
        original_single = orch._evaluate_single

        async def mock_single(s, cp):
            nonlocal single_called
            single_called = True
            return True

        with patch.object(orch, "_evaluate_single", side_effect=mock_single):
            await orch.evaluate(sprint, contract_path)

        assert single_called


@pytest.mark.anyio
async def test_evaluate_dispatches_parallel_for_many_sections():
    """Multiple sections with enough criteria → parallel path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        # Many sections with enough criteria to trigger parallel
        criteria = "\n".join([
            "### Visual Design\n" + "\n".join(f"- [ ] Design criterion {i}" for i in range(10)),
            "### Responsive Design\n" + "\n".join(f"- [ ] Responsive criterion {i}" for i in range(5)),
            "### P0: Feature A\n" + "\n".join(f"- [ ] Feature A criterion {i}" for i in range(15)),
            "### P1: Feature B\n" + "\n".join(f"- [ ] Feature B criterion {i}" for i in range(12)),
            "### P2: Feature C\n" + "\n".join(f"- [ ] Feature C criterion {i}" for i in range(10)),
        ])
        with open(contract_path, "w") as f:
            f.write(criteria)

        parallel_called = False

        async def mock_parallel(s, cp, buckets):
            nonlocal parallel_called
            parallel_called = True
            assert len(buckets) >= 2  # at least lead + 1 feature bucket
            return True

        with patch.object(orch, "_evaluate_parallel", side_effect=mock_parallel):
            await orch.evaluate(sprint, contract_path)

        assert parallel_called


@pytest.mark.anyio
async def test_parallel_eval_handles_crash():
    """If one parallel evaluator crashes, its criteria are marked FAIL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")

        # Create buckets manually
        buckets = [
            [CriteriaSection("Visual Design", "### Visual Design\n- [ ] BG color\n- [ ] Font", 2)],
            [CriteriaSection("P0: Canvas", "### P0: Canvas\n- [ ] Pan\n- [ ] Zoom", 2)],
        ]

        call_count = 0

        async def mock_run_eval(evaluator_id, is_lead, criteria_text, screenshots_dir, prev_eval_section, eval_output_path=""):
            nonlocal call_count
            call_count += 1
            if evaluator_id == 0:
                return "- [x] BG color | s/a.png\n- [x] Font | s/b.png\n### Verdict: FAIL"
            raise RuntimeError("Evaluator 1 crashed")

        with patch.object(orch, "_run_single_evaluator", side_effect=mock_run_eval), \
             patch.object(orch, "server"):
            passed = await orch._evaluate_parallel(sprint, contract_path, buckets)

        assert passed is False
        assert call_count == 2  # both were attempted

        # Crashed evaluator's criteria should be marked FAIL in evaluation.md
        eval_path = os.path.join(sprint_dir, "evaluation.md")
        with open(eval_path) as f:
            content = f.read()
        assert "evaluator crashed" in content


# --- Parallel eval hardening tests ---

def test_assign_sections_caps_at_3_total():
    """Max 3 evaluators total (1 lead + 2 feature)."""
    sections = [
        CriteriaSection("Visual Design", "- [ ] a\n" * 10, 10),
        CriteriaSection("Responsive Design", "- [ ] b\n" * 5, 5),
        CriteriaSection("P0: A", "- [ ] c\n" * 20, 20),
        CriteriaSection("P0: B", "- [ ] d\n" * 20, 20),
        CriteriaSection("P1: C", "- [ ] e\n" * 20, 20),
        CriteriaSection("P1: D", "- [ ] f\n" * 20, 20),
        CriteriaSection("P2: E", "- [ ] g\n" * 15, 15),
    ]
    buckets = assign_sections_to_evaluators(sections, target_per_evaluator=25)
    # 1 lead + max 2 feature = 3 total
    assert len(buckets) <= 3


@pytest.mark.anyio
async def test_all_evaluators_use_same_model():
    """All evaluators (lead and feature) should use the configured evaluator model."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        captured_models = []

        async def mock_call_agent(**kwargs):
            captured_models.append(kwargs.get("model"))
            return _mock_result("- [x] test | s/a.png")

        with patch("orchestrator.call_agent", side_effect=mock_call_agent):
            await orch._run_single_evaluator(0, True, "- [ ] test", "/tmp/s", "")
            await orch._run_single_evaluator(1, False, "- [ ] test", "/tmp/s", "")

        from config import resolve_agent_model
        expected = resolve_agent_model("evaluator")
        assert captured_models[0] == expected
        assert captured_models[1] == expected


@pytest.mark.anyio
async def test_regression_skips_zero_total_scores():
    """Crash-generated 0% scores should not trigger regression detection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Test")

        orch._generator_session_id = "existing-session"
        # Real score, then crash score (0 total)
        orch.state.add_eval_score(1, 1, 80, 100)
        orch.state.add_eval_score(1, 2, 0, 0)  # crash — 0 total

        orch._check_regression(sprint, "Sprint 1")

        # Session should NOT be reset (crash score ignored)
        assert orch._generator_session_id == "existing-session"


@pytest.mark.anyio
async def test_merge_validation_detects_lost_criteria():
    """Post-merge validation should log when criteria are lost."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Full Build")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("### A\n- [ ] c1\n- [ ] c2\n### B\n- [ ] c3")

        # Bucket with 3 criteria total
        buckets = [
            [CriteriaSection("A", "### A\n- [ ] c1\n- [ ] c2", 2)],
            [CriteriaSection("B", "### B\n- [ ] c3", 1)],
        ]

        # Evaluator 0 returns 2 criteria, evaluator 1 returns empty (lost 1)
        async def mock_run(evaluator_id, is_lead, criteria_text, screenshots_dir, prev_eval_section, eval_output_path=""):
            if evaluator_id == 0:
                return "- [x] c1 | s/a.png\n- [ ] c2 ← FAIL | s/b.png\n### Verdict: FAIL"
            return ""  # empty — criteria lost

        with patch.object(orch, "_run_single_evaluator", side_effect=mock_run), \
             patch.object(orch, "server"):
            passed = await orch._evaluate_parallel(sprint, contract_path, buckets)

        # Check harness.log for the warning
        log_path = os.path.join(orch.output_dir, "harness.log")
        with open(log_path) as f:
            log = f.read()
        assert "Merge lost" in log


# --- Evaluator empty response retry ---

@pytest.mark.anyio
async def test_evaluator_retries_on_zero_criteria():
    """If evaluator returns 0 criteria, it should retry once."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._spec = None
        orch._init_output()

        call_count = 0

        async def mock_call_agent(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_result("Evaluation complete.")  # 0 criteria
            return _mock_result("- [x] Login works | s/a.png\n### Verdict: PASS")

        with patch("orchestrator.call_agent", side_effect=mock_call_agent):
            result = await orch._run_single_evaluator(
                0, True, "- [ ] Login works", "/tmp/s", ""
            )

        assert call_count == 2  # first call + retry
        assert "- [x] Login works" in result
