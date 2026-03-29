import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
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
async def test_orchestrator_build():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)

        mock = AsyncMock(return_value="Built successfully")
        with patch("orchestrator.call_agent", mock):
            await orch.build()

        status = json.load(open(os.path.join(tmpdir, "comms", "status.json")))
        assert status["build_attempts"] == 1


@pytest.mark.anyio
async def test_orchestrator_evaluate_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)

        mock = AsyncMock(return_value="### Verdict: PASS")
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server"):
                passed = await orch.evaluate()

        assert passed is True


@pytest.mark.anyio
async def test_orchestrator_evaluate_fail():
    with tempfile.TemporaryDirectory() as tmpdir:
        orch = _setup_orch(tmpdir)

        with open(os.path.join(tmpdir, "comms", "spec.md"), "w") as f:
            f.write(SAMPLE_SPEC)

        mock = AsyncMock(return_value="### Verdict: FAIL\n### Required Changes\n1. Fix the button")
        with patch("orchestrator.call_agent", mock):
            with patch.object(orch, "server"):
                passed = await orch.evaluate()

        assert passed is False
