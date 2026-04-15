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
        assert data["cost"] == {"total_usd": 0.0, "by_phase": {}}


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
        state.add_cost_usd(1.50)
        state.add_cost_usd(2.25)
        data = state.load()
        assert data["cost"]["total_usd"] == 3.75


def test_cost_tracking_by_phase():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.add_cost_usd(0.18, "planner")
        state.add_cost_usd(4.67, "build")
        state.add_cost_usd(2.92, "build")
        state.add_cost_usd(7.71, "eval")
        data = state.load()
        assert abs(data["cost"]["total_usd"] - 15.48) < 0.01
        assert abs(data["cost"]["by_phase"]["planner"] - 0.18) < 0.01
        assert abs(data["cost"]["by_phase"]["build"] - 7.59) < 0.01
        assert abs(data["cost"]["by_phase"]["eval"] - 7.71) < 0.01


def test_sprint_results_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.set_sprint_result(1, True)
        state.set_sprint_result(2, False)
        state.set_sprint_result(3, True)
        results = state.get_sprint_results()
        assert results == {1: True, 2: False, 3: True}


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


def test_get_cna_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.add_cost_usd(10.0, "build")
        state.add_eval_score(1, 1, 80, 100)
        cna = state.get_cna(sprint_num=1)
        # CNA = 80 / 10 * 100 = 800
        assert abs(cna - 800.0) < 0.01


def test_get_cna_zero_cost():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.add_eval_score(1, 1, 80, 100)
        assert state.get_cna(sprint_num=1) == 0.0


def test_get_cna_no_scores():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.add_cost_usd(10.0)
        assert state.get_cna() == 0.0


def test_get_run_efficiency():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = HarnessState(tmpdir)
        state.init()
        state.add_cost_usd(15.0, "build")
        state.add_eval_score(1, 1, 45, 50)
        state.increment(1, "build")
        state.increment(1, "build")
        state.increment(1, "eval")
        eff = state.get_run_efficiency()
        assert eff["score_pct"] == 90
        assert abs(eff["cost_usd"] - 15.0) < 0.01
        assert eff["cna"] == round(90 / 15 * 100, 1)
        assert eff["build_attempts"] == 2
        assert eff["eval_attempts"] == 1
