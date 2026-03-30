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


def test_init_has_sprint_fields():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        data = state.load()
        assert data["current_sprint"] == 0
        assert data["total_sprints"] == 0
        assert data["timings"] == {"plan": 0, "sprints": []}


def test_set_sprint_info():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.set_sprint_info(current=1, total=3)
        data = state.load()
        assert data["current_sprint"] == 1
        assert data["total_sprints"] == 3


def test_record_timing():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.record_plan_time(280)
        data = state.load()
        assert data["timings"]["plan"] == 280


def test_record_sprint_timing():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.add_sprint_timing(1, "negotiate", 45)
        state.add_sprint_timing(1, "build", 320)
        state.add_sprint_timing(1, "eval", 60)
        state.add_sprint_timing(1, "build", 180)
        data = state.load()
        sprint_timing = data["timings"]["sprints"][0]
        assert sprint_timing["sprint"] == 1
        assert sprint_timing["negotiate"] == 45
        assert sprint_timing["build"] == [320, 180]
        assert sprint_timing["eval"] == [60]
