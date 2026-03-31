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
        assert data["sprint_attempts"] == {}
        assert data["cost"] == {"input_tokens": 0, "output_tokens": 0}


def test_set_phase():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.set_phase("building")
        loaded = state.load()
        assert loaded["phase"] == "building"


def test_increment_per_sprint():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.increment(1, "build")
        state.increment(1, "build")
        state.increment(2, "build")
        state.increment(1, "eval")
        assert state.get_sprint_attempt(1, "build") == 2
        assert state.get_sprint_attempt(2, "build") == 1
        assert state.get_sprint_attempt(1, "eval") == 1


def test_total_across_sprints():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.increment(1, "build")
        state.increment(1, "build")
        state.increment(2, "build")
        state.increment(1, "eval")
        state.increment(2, "eval")
        assert state.total("build") == 3
        assert state.total("eval") == 2


def test_total_with_preloaded_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.increment(1, "build")
        state.increment(1, "build")
        data = state.load()
        # Reuse loaded data instead of re-reading
        assert state.total("build", data) == 2


def test_cost_tracking():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.add_cost(1000, 500)
        state.add_cost(2000, 800)
        data = state.load()
        assert data["cost"]["input_tokens"] == 3000
        assert data["cost"]["output_tokens"] == 1300


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
