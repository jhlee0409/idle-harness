import json
import os
import shutil
import tempfile
import pytest
from unittest.mock import patch, AsyncMock
from orchestrator import Orchestrator


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
        assert os.path.isdir(os.path.join(tmpdir, "comms", "screenshots"))
        assert os.path.isdir(os.path.join(tmpdir, "output"))


@pytest.mark.anyio
async def test_orchestrator_plan_phase():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        async def mock_planner(*args, **kwargs):
            with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
                f.write(SAMPLE_SPEC)
            return "Spec written."

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
            return "Spec written."

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
            return "Spec written."

        with patch("orchestrator.call_agent", side_effect=mock_planner):
            await orch.plan("Build a test app")

        assert len(orch.sprints) == 1
        assert orch.sprints[0].name == "Full Build"


@pytest.mark.anyio
async def test_orchestrator_negotiate_contract():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
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
                return "Proposal written."
            else:
                with open(os.path.join(sprint_dir, "contract_review.md"), "w") as f:
                    f.write("AGREED\nLooks good.")
                return "AGREED\nLooks good."

        with patch("orchestrator.call_agent", side_effect=mock_negotiate):
            contract_path = await orch.negotiate_contract(sprint)

        assert os.path.exists(contract_path)
        assert "sprint-1" in contract_path


@pytest.mark.anyio
async def test_orchestrator_build_with_sprint():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock = AsyncMock(return_value="Built successfully")
        with patch("orchestrator.call_agent", mock):
            await orch.build(sprint, contract_path)

        status = json.load(open(os.path.join(tmpdir, "comms", "status.json")))
        assert status["build_attempts"] == 1


@pytest.mark.anyio
async def test_orchestrator_evaluate_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock = AsyncMock(return_value="### Verdict: PASS")
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
        orch._init_output()

        from sprint import Sprint
        sprint = Sprint(number=1, name="Foundation", features=["Login"], goal="Users can log in")
        sprint_dir = os.path.join(tmpdir, "comms", "sprints", "sprint-1")
        os.makedirs(sprint_dir, exist_ok=True)
        contract_path = os.path.join(sprint_dir, "sprint_contract.md")
        with open(contract_path, "w") as f:
            f.write("# Contract\n1. Login works")

        mock = AsyncMock(return_value="### Verdict: FAIL\n### Required Changes\n1. Fix the button")
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server"):
                passed = await orch.evaluate(sprint, contract_path)

        assert passed is False


@pytest.mark.anyio
async def test_slug_ascii_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)
        spec_with_korean = "# Mystic Arcana — AI 타로 리딩 웹 앱\n\n## Vision\nTest.\n"
        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(spec_with_korean)
        orch._init_output()
        assert "타로" not in orch.output_dir
        assert "mystic-arcana" in orch.output_dir
