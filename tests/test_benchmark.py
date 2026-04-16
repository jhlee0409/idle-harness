"""Tests for benchmark.py — Pass@k, Pass^k, CNA calculations and report generation."""

import json
import math
import os
import tempfile

from benchmark import (
    pass_at_k,
    pass_power_k,
    cost_normalized_accuracy,
    RunResult,
    PromptStats,
    BenchmarkReport,
    collect_run_result,
    save_results,
    generate_report,
    compare_reports,
    _find_new_output,
)


# ---------------------------------------------------------------------------
# Unit tests: metric formulas
# ---------------------------------------------------------------------------

def test_pass_at_k_basic():
    # 10 attempts, 7 correct, sample 1 → raw success rate
    result = pass_at_k(10, 7, 1)
    assert abs(result - 0.7) < 0.001

def test_pass_at_k_all_correct():
    assert pass_at_k(10, 10, 3) == 1.0

def test_pass_at_k_none_correct():
    assert pass_at_k(10, 0, 3) == 0.0

def test_pass_at_k_sample_size_equals_n():
    # If k == n, then at least 1 correct if c >= 1
    assert pass_at_k(5, 3, 5) == 1.0
    assert pass_at_k(5, 0, 5) == 0.0

def test_pass_at_k_high_k_boosts_probability():
    # 70% success rate → Pass@1=70%, Pass@3 should be much higher
    p1 = pass_at_k(100, 70, 1)
    p3 = pass_at_k(100, 70, 3)
    p10 = pass_at_k(100, 70, 10)
    assert p1 < p3 < p10
    assert abs(p1 - 0.7) < 0.01
    assert p3 > 0.95  # ~97%

def test_pass_at_k_edge_zero_n():
    assert pass_at_k(0, 0, 1) == 0.0

def test_pass_at_k_edge_zero_k():
    assert pass_at_k(10, 5, 0) == 0.0

def test_pass_at_k_k_greater_than_n():
    # k > n should cap at n
    result = pass_at_k(5, 3, 10)
    assert result == 1.0  # If we sample all 5 and 3 are correct, at least 1 correct


def test_pass_power_k_basic():
    # 70% success rate, k=3 → 0.7^3 = 0.343
    result = pass_power_k(10, 7, 3)
    assert abs(result - 0.343) < 0.001

def test_pass_power_k_perfect():
    assert pass_power_k(10, 10, 5) == 1.0

def test_pass_power_k_zero():
    assert pass_power_k(10, 0, 3) == 0.0

def test_pass_power_k_high_k_reduces_probability():
    # Same 70% rate: Pass^1=70%, Pass^3=34.3%, Pass^5=16.8%
    p1 = pass_power_k(10, 7, 1)
    p3 = pass_power_k(10, 7, 3)
    p5 = pass_power_k(10, 7, 5)
    assert p1 > p3 > p5
    assert abs(p1 - 0.7) < 0.01
    assert abs(p3 - 0.343) < 0.01

def test_pass_power_k_edge_zero_n():
    assert pass_power_k(0, 0, 1) == 0.0


def test_cna_basic():
    # Score 80%, cost $10 → CNA = 800
    assert cost_normalized_accuracy(80, 10) == 800.0

def test_cna_zero_cost():
    assert cost_normalized_accuracy(80, 0) == 0.0

def test_cna_higher_score_lower_cost_is_better():
    # 90% at $3 vs 85% at $15
    cna_cheap = cost_normalized_accuracy(90, 3)
    cna_expensive = cost_normalized_accuracy(85, 15)
    assert cna_cheap > cna_expensive


# ---------------------------------------------------------------------------
# PromptStats aggregation
# ---------------------------------------------------------------------------

def _make_runs(n: int, c: int, score_pcts: list[int] | None = None,
               costs: list[float] | None = None) -> list[RunResult]:
    runs = []
    for i in range(n):
        passed = i < c
        score = (score_pcts[i] if score_pcts else (100 if passed else 50))
        cost = (costs[i] if costs else 10.0)
        runs.append(RunResult(
            prompt="test app", slug=f"test-app-{i}",
            passed=passed, score_pct=score,
            cost_usd=cost, elapsed_s=600,
            build_attempts=3,
        ))
    return runs


def test_prompt_stats_basic():
    stats = PromptStats(prompt="test", runs=_make_runs(10, 7))
    assert stats.n == 10
    assert stats.c == 7
    assert abs(stats.success_rate - 0.7) < 0.01


def test_prompt_stats_pass_at_k():
    stats = PromptStats(prompt="test", runs=_make_runs(10, 7))
    assert abs(stats.pass_at_k(1) - 0.7) < 0.01
    assert stats.pass_at_k(3) > 0.95


def test_prompt_stats_pass_power_k():
    stats = PromptStats(prompt="test", runs=_make_runs(10, 7))
    assert abs(stats.pass_power_k(3) - 0.343) < 0.01


def test_prompt_stats_avg_cna():
    stats = PromptStats(prompt="test", runs=_make_runs(
        5, 3, score_pcts=[100, 100, 100, 50, 50], costs=[10, 10, 10, 10, 10]
    ))
    # CNA per run: 100/10*100=1000, 1000, 1000, 500, 500 → avg = 800
    assert abs(stats.avg_cna - 800) < 0.01


def test_prompt_stats_score_stddev():
    stats = PromptStats(prompt="test", runs=_make_runs(
        4, 2, score_pcts=[90, 80, 60, 50]
    ))
    mean = (90 + 80 + 60 + 50) / 4  # 70
    variance = ((90-70)**2 + (80-70)**2 + (60-70)**2 + (50-70)**2) / 3
    expected_std = math.sqrt(variance)
    assert abs(stats.score_stddev - expected_std) < 0.01


# ---------------------------------------------------------------------------
# BenchmarkReport
# ---------------------------------------------------------------------------

def test_benchmark_report_overall():
    stats1 = PromptStats(prompt="app1", runs=_make_runs(10, 7))
    stats2 = PromptStats(prompt="app2", runs=_make_runs(10, 5))
    report = BenchmarkReport(prompts=[stats1, stats2])
    assert report.total_runs == 20
    assert report.total_passes == 12
    assert abs(report.overall_pass_rate - 0.6) < 0.01
    # Average of two Pass@1 values: (0.7 + 0.5) / 2 = 0.6
    assert abs(report.overall_pass_at_k(1) - 0.6) < 0.01


# ---------------------------------------------------------------------------
# Collect from harness output
# ---------------------------------------------------------------------------

def test_collect_run_result():
    with tempfile.TemporaryDirectory() as tmpdir:
        harness_dir = os.path.join(tmpdir, ".harness")
        os.makedirs(harness_dir)
        status = {
            "sprint_results": {"1": True},
            "eval_scores": [{"sprint": 1, "attempt": 1, "passed": 45, "total": 50, "pct": 90}],
            "cost": {"total_usd": 12.50, "by_phase": {}},
            "sprint_attempts": {"1": {"build": 3, "eval": 3}},
            "timings": {"plan": 120, "sprints": [{"sprint": 1, "build": [300, 200], "eval": [60]}]},
            "run_meta": {"started_at": "2026-04-15T10:00:00Z"},
        }
        with open(os.path.join(harness_dir, "status.json"), "w") as f:
            json.dump(status, f)

        result = collect_run_result(tmpdir, "build a todo app")
        assert result is not None
        assert result.passed is True
        assert result.score_pct == 90
        assert abs(result.cost_usd - 12.50) < 0.01
        assert result.build_attempts == 3


def test_collect_run_result_uses_best_score_not_last():
    """When last eval is a crash (0%), use the best score instead."""
    with tempfile.TemporaryDirectory() as tmpdir:
        harness_dir = os.path.join(tmpdir, ".harness")
        os.makedirs(harness_dir)
        status = {
            "sprint_results": {},
            "eval_scores": [
                {"sprint": 1, "attempt": 1, "passed": 65, "total": 117, "pct": 55},
                {"sprint": 1, "attempt": 2, "passed": 86, "total": 146, "pct": 58},
                {"sprint": 1, "attempt": 3, "passed": 22, "total": 29, "pct": 75},
                {"sprint": 1, "attempt": 4, "passed": 0, "total": 146, "pct": 0},  # crash
            ],
            "cost": {"total_usd": 193.38, "by_phase": {}},
            "sprint_attempts": {"1": {"build": 10, "eval": 4}},
            "timings": {"plan": 168, "sprints": []},
            "run_meta": {},
        }
        with open(os.path.join(harness_dir, "status.json"), "w") as f:
            json.dump(status, f)

        result = collect_run_result(tmpdir, "mood board")
        assert result is not None
        assert result.score_pct == 75  # best, not last (0%)
        assert result.passed is False
        assert any("last_score_crash" in w for w in result.warnings)


def test_collect_run_result_passed_no_scores():
    """Older format: passed but no eval_scores → infer 100%."""
    with tempfile.TemporaryDirectory() as tmpdir:
        harness_dir = os.path.join(tmpdir, ".harness")
        os.makedirs(harness_dir)
        status = {
            "sprint_results": {"1": True},
            "eval_scores": [],
            "cost": {"total_usd": 80.0, "by_phase": {}},
            "sprint_attempts": {"1": {"build": 4, "eval": 4}},
            "timings": {"plan": 158, "sprints": []},
            "run_meta": {},
        }
        with open(os.path.join(harness_dir, "status.json"), "w") as f:
            json.dump(status, f)

        result = collect_run_result(tmpdir, "recipe app")
        assert result is not None
        assert result.passed is True
        assert result.score_pct == 100  # inferred
        assert any("no_eval_scores" in w for w in result.warnings)


def test_collect_run_result_no_sprint_results_warns():
    """Older format with no sprint_results → warning."""
    with tempfile.TemporaryDirectory() as tmpdir:
        harness_dir = os.path.join(tmpdir, ".harness")
        os.makedirs(harness_dir)
        status = {
            "sprint_results": {},
            "eval_scores": [{"sprint": 1, "attempt": 1, "passed": 37, "total": 151, "pct": 24}],
            "cost": {"total_usd": 42.81, "by_phase": {}},
            "sprint_attempts": {"1": {"build": 2, "eval": 1}},
            "timings": {"plan": 199, "sprints": []},
            "run_meta": {},
        }
        with open(os.path.join(harness_dir, "status.json"), "w") as f:
            json.dump(status, f)

        result = collect_run_result(tmpdir, "test")
        assert result is not None
        assert result.passed is False
        assert any("no_sprint_results" in w for w in result.warnings)


def test_collect_run_result_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = collect_run_result(tmpdir, "test")
        assert result is None


# ---------------------------------------------------------------------------
# Save / Load report round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_report():
    with tempfile.TemporaryDirectory() as tmpdir:
        stats = PromptStats(prompt="build a blog", runs=_make_runs(5, 3))
        report = BenchmarkReport(prompts=[stats], generated_at="2026-04-15T10:00:00Z")
        save_results(tmpdir, report)

        loaded = generate_report(tmpdir)
        assert loaded.total_runs == 5
        assert loaded.total_passes == 3
        assert len(loaded.prompts) == 1
        assert loaded.prompts[0].prompt == "build a blog"


# ---------------------------------------------------------------------------
# Compare reports
# ---------------------------------------------------------------------------

def test_compare_reports():
    before = BenchmarkReport(prompts=[
        PromptStats(prompt="test app", runs=_make_runs(10, 5)),
    ])
    after = BenchmarkReport(prompts=[
        PromptStats(prompt="test app", runs=_make_runs(10, 8)),
    ])
    comparison = compare_reports(before, after)
    assert "Pass@1" in comparison
    assert "Pass^3" in comparison
    assert "test app" in comparison


# ---------------------------------------------------------------------------
# _find_new_output
# ---------------------------------------------------------------------------

def test_find_new_output_detects_new_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "existing-app"))
        known = {"existing-app"}
        os.makedirs(os.path.join(tmpdir, "new-app"))
        result = _find_new_output(tmpdir, known)
        assert result == "new-app"


def test_find_new_output_returns_none_if_no_new():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "existing"))
        known = {"existing"}
        result = _find_new_output(tmpdir, known)
        assert result is None


def test_find_new_output_nonexistent_dir():
    result = _find_new_output("/nonexistent/path", set())
    assert result is None


# ---------------------------------------------------------------------------
# Round-trip with warnings
# ---------------------------------------------------------------------------

def test_save_load_preserves_warnings():
    with tempfile.TemporaryDirectory() as tmpdir:
        run = RunResult(
            prompt="test", slug="test-app",
            passed=True, score_pct=100,
            cost_usd=50.0, elapsed_s=600,
            build_attempts=2,
            warnings=["no_eval_scores: inferred"],
        )
        stats = PromptStats(prompt="test", runs=[run])
        report = BenchmarkReport(prompts=[stats], generated_at="2026-04-16T00:00:00Z")
        save_results(tmpdir, report)

        loaded = generate_report(tmpdir)
        loaded_run = loaded.prompts[0].runs[0]
        assert loaded_run.warnings == ["no_eval_scores: inferred"]
