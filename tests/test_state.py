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
        assert data["build_attempts"] == 0
        assert data["eval_attempts"] == 0


def test_set_phase():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.set_phase("building")
        loaded = state.load()
        assert loaded["phase"] == "building"


def test_increment_build():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.increment_build()
        state.increment_build()
        loaded = state.load()
        assert loaded["build_attempts"] == 2


def test_increment_eval():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.increment_eval()
        loaded = state.load()
        assert loaded["eval_attempts"] == 1
