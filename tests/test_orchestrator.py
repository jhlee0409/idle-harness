import json
import os
import tempfile
import pytest
from unittest.mock import patch, AsyncMock
from cli import AgentResult
from orchestrator import Orchestrator, _check_verdict_pass, _check_contract_agreed


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
