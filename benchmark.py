"""Benchmark suite for Pass@k / Pass^k evaluation of the harness.

Runs a fixed set of prompts N times each, collects results from
output/{slug}/.harness/status.json, and computes reliability metrics.

Usage:
    python benchmark.py run prompts.json --runs 5
    python benchmark.py report results/
    python benchmark.py compare results_before/ results_after/
"""

import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Metric calculations
# ---------------------------------------------------------------------------

def _comb(n: int, k: int) -> int:
    """Binomial coefficient C(n, k). Returns 0 if k > n."""
    if k > n or k < 0:
        return 0
    return math.comb(n, k)


def pass_at_k(n: int, c: int, k: int) -> float:
    """Probability that at least 1 of k samples is correct.

    Formula: Pass@k = 1 - C(n-c, k) / C(n, k)
    where n = total attempts, c = correct attempts, k = sample size.
    """
    if n == 0 or k == 0:
        return 0.0
    if c >= n:
        return 1.0
    if k > n:
        k = n
    return 1.0 - _comb(n - c, k) / _comb(n, k)


def pass_power_k(n: int, c: int, k: int) -> float:
    """Probability of succeeding on ALL k independent attempts.

    Formula: Pass^k = (c/n)^k
    """
    if n == 0 or k == 0:
        return 0.0
    p = c / n
    return p ** k


def cost_normalized_accuracy(score_pct: float, cost_usd: float) -> float:
    """CNA = score / cost * 100. Higher is better.

    Returns 0 if cost is 0 (free run — shouldn't happen in practice).
    """
    if cost_usd <= 0:
        return 0.0
    return score_pct / cost_usd * 100


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Result of a single harness run for one prompt."""
    prompt: str
    slug: str
    passed: bool
    score_pct: int  # 0-100 (best score, not last)
    cost_usd: float
    elapsed_s: int
    build_attempts: int
    eval_scores: list[dict] = field(default_factory=list)
    timestamp: str = ""
    warnings: list[str] = field(default_factory=list)  # data quality issues


@dataclass
class PromptStats:
    """Aggregated stats for one prompt across N runs."""
    prompt: str
    runs: list[RunResult] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def c(self) -> int:
        """Number of successful runs (final PASS)."""
        return sum(1 for r in self.runs if r.passed)

    @property
    def success_rate(self) -> float:
        return self.c / self.n if self.n > 0 else 0.0

    def pass_at_k(self, k: int) -> float:
        return pass_at_k(self.n, self.c, k)

    def pass_power_k(self, k: int) -> float:
        return pass_power_k(self.n, self.c, k)

    @property
    def avg_score(self) -> float:
        if not self.runs:
            return 0.0
        return sum(r.score_pct for r in self.runs) / len(self.runs)

    @property
    def avg_cost(self) -> float:
        if not self.runs:
            return 0.0
        return sum(r.cost_usd for r in self.runs) / len(self.runs)

    @property
    def avg_cna(self) -> float:
        """Average Cost-Normalized Accuracy across runs."""
        cnas = [cost_normalized_accuracy(r.score_pct, r.cost_usd)
                for r in self.runs if r.cost_usd > 0]
        return sum(cnas) / len(cnas) if cnas else 0.0

    @property
    def score_stddev(self) -> float:
        if len(self.runs) < 2:
            return 0.0
        mean = self.avg_score
        variance = sum((r.score_pct - mean) ** 2 for r in self.runs) / (len(self.runs) - 1)
        return math.sqrt(variance)


@dataclass
class BenchmarkReport:
    """Full benchmark report across all prompts."""
    prompts: list[PromptStats] = field(default_factory=list)
    generated_at: str = ""

    @property
    def total_runs(self) -> int:
        return sum(p.n for p in self.prompts)

    @property
    def total_passes(self) -> int:
        return sum(p.c for p in self.prompts)

    @property
    def overall_pass_rate(self) -> float:
        total = self.total_runs
        return self.total_passes / total if total > 0 else 0.0

    def overall_pass_at_k(self, k: int) -> float:
        """Average Pass@k across all prompts."""
        if not self.prompts:
            return 0.0
        return sum(p.pass_at_k(k) for p in self.prompts) / len(self.prompts)

    def overall_pass_power_k(self, k: int) -> float:
        """Average Pass^k across all prompts."""
        if not self.prompts:
            return 0.0
        return sum(p.pass_power_k(k) for p in self.prompts) / len(self.prompts)

    @property
    def overall_avg_cna(self) -> float:
        if not self.prompts:
            return 0.0
        return sum(p.avg_cna for p in self.prompts) / len(self.prompts)


# ---------------------------------------------------------------------------
# Result collection from harness output
# ---------------------------------------------------------------------------

def collect_run_result(output_dir: str, prompt: str) -> RunResult | None:
    """Collect a RunResult from a completed harness output directory.

    Reads .harness/status.json for scores, cost, and timing.

    Score selection: uses the BEST eval score (not last), since the last
    attempt may be a crash (0%) that doesn't reflect actual build quality.
    For passed builds with no eval_scores, uses 100% (it passed all criteria).
    """
    status_path = os.path.join(output_dir, ".harness", "status.json")
    if not os.path.exists(status_path):
        return None

    with open(status_path) as f:
        status = json.load(f)

    # Determine final pass/fail from sprint_results
    sprint_results = status.get("sprint_results", {})
    passed = all(sprint_results.values()) if sprint_results else False

    # Get BEST eval score (not last — last may be a crash)
    eval_scores = status.get("eval_scores", [])
    if eval_scores:
        valid_scores = [s for s in eval_scores if s.get("total", 0) > 0]
        if valid_scores:
            best = max(valid_scores, key=lambda s: s["pct"])
            score_pct = best["pct"]
        else:
            score_pct = 0
    elif passed:
        # Passed but no eval_scores recorded (older format) — infer 100%
        score_pct = 100
    else:
        score_pct = 0

    # Cost
    cost = status.get("cost", {})
    cost_usd = cost.get("total_usd", 0.0)

    # Timing
    run_meta = status.get("run_meta", {})
    timestamp = run_meta.get("started_at", "")

    # Build attempts
    build_attempts = sum(
        e.get("build", 0)
        for e in status.get("sprint_attempts", {}).values()
    )

    # Elapsed (approximate from timing data)
    timings = status.get("timings", {})
    plan_time = timings.get("plan", 0)
    sprint_time = 0
    for st in timings.get("sprints", []):
        for key in ("build", "eval"):
            val = st.get(key, [])
            if isinstance(val, list):
                sprint_time += sum(val)
            elif isinstance(val, (int, float)):
                sprint_time += val
        negotiate = st.get("negotiate", 0)
        sprint_time += negotiate
    elapsed_s = plan_time + sprint_time

    slug = os.path.basename(output_dir)

    # Data quality warnings
    warnings = []
    if not sprint_results:
        warnings.append("no_sprint_results: older format, pass/fail inferred")
    if passed and not eval_scores:
        warnings.append("no_eval_scores: passed but score inferred as 100%")
    if eval_scores:
        last = eval_scores[-1]
        best_in_scores = max((s["pct"] for s in eval_scores if s.get("total", 0) > 0), default=0)
        last_pct = last.get("pct", 0) if last.get("total", 0) > 0 else 0
        if best_in_scores > last_pct:
            warnings.append(f"last_score_crash: last={last_pct}% best={best_in_scores}% (using best)")

    return RunResult(
        prompt=prompt,
        slug=slug,
        passed=passed,
        score_pct=score_pct,
        cost_usd=cost_usd,
        elapsed_s=int(elapsed_s),
        build_attempts=build_attempts,
        eval_scores=eval_scores,
        timestamp=timestamp,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(results_dir: str) -> BenchmarkReport:
    """Generate a BenchmarkReport from a results directory.

    Expected structure:
        results_dir/
            benchmark_results.json   <- saved by save_results()
    """
    results_path = os.path.join(results_dir, "benchmark_results.json")
    if not os.path.exists(results_path):
        raise FileNotFoundError(f"No benchmark_results.json in {results_dir}")

    with open(results_path) as f:
        data = json.load(f)

    report = BenchmarkReport(generated_at=data.get("generated_at", ""))
    for prompt_data in data.get("prompts", []):
        stats = PromptStats(prompt=prompt_data["prompt"])
        for run_data in prompt_data.get("runs", []):
            stats.runs.append(RunResult(**run_data))
        report.prompts.append(stats)

    return report


def save_results(results_dir: str, report: BenchmarkReport):
    """Persist benchmark results to JSON."""
    os.makedirs(results_dir, exist_ok=True)
    data = {
        "generated_at": report.generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "total_runs": report.total_runs,
            "total_passes": report.total_passes,
            "overall_pass_rate": round(report.overall_pass_rate, 4),
            "pass_at_1": round(report.overall_pass_at_k(1), 4),
            "pass_at_3": round(report.overall_pass_at_k(3), 4),
            "pass_at_5": round(report.overall_pass_at_k(5), 4),
            "pass_power_3": round(report.overall_pass_power_k(3), 4),
            "pass_power_5": round(report.overall_pass_power_k(5), 4),
            "avg_cna": round(report.overall_avg_cna, 2),
        },
        "prompts": [],
    }

    for stats in report.prompts:
        prompt_data = {
            "prompt": stats.prompt,
            "n": stats.n,
            "c": stats.c,
            "success_rate": round(stats.success_rate, 4),
            "avg_score": round(stats.avg_score, 2),
            "score_stddev": round(stats.score_stddev, 2),
            "avg_cost": round(stats.avg_cost, 2),
            "avg_cna": round(stats.avg_cna, 2),
            "pass_at_1": round(stats.pass_at_k(1), 4),
            "pass_at_3": round(stats.pass_at_k(3), 4),
            "pass_at_5": round(stats.pass_at_k(5), 4),
            "pass_power_3": round(stats.pass_power_k(3), 4),
            "pass_power_5": round(stats.pass_power_k(5), 4),
            "runs": [],
        }
        for run in stats.runs:
            run_data = {
                "prompt": run.prompt,
                "slug": run.slug,
                "passed": run.passed,
                "score_pct": run.score_pct,
                "cost_usd": round(run.cost_usd, 2),
                "elapsed_s": run.elapsed_s,
                "build_attempts": run.build_attempts,
                "eval_scores": run.eval_scores,
                "timestamp": run.timestamp,
            }
            if run.warnings:
                run_data["warnings"] = run.warnings
            prompt_data["runs"].append(run_data)
        data["prompts"].append(prompt_data)

    with open(os.path.join(results_dir, "benchmark_results.json"), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def compare_reports(before: BenchmarkReport, after: BenchmarkReport) -> str:
    """Generate a comparison between two benchmark reports."""
    lines = ["## Benchmark Comparison\n"]
    lines.append(f"| Metric | Before | After | Delta |")
    lines.append(f"|--------|--------|-------|-------|")

    def _row(name: str, before_val: float, after_val: float, fmt: str = ".1%"):
        delta = after_val - before_val
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"| {name} | {before_val:{fmt}} | {after_val:{fmt}} | {sign}{delta:{fmt}} |"
        )

    _row("Pass@1", before.overall_pass_at_k(1), after.overall_pass_at_k(1))
    _row("Pass@3", before.overall_pass_at_k(3), after.overall_pass_at_k(3))
    _row("Pass@5", before.overall_pass_at_k(5), after.overall_pass_at_k(5))
    _row("Pass^3", before.overall_pass_power_k(3), after.overall_pass_power_k(3))
    _row("Pass^5", before.overall_pass_power_k(5), after.overall_pass_power_k(5))
    _row("Avg CNA", before.overall_avg_cna, after.overall_avg_cna, ".1f")
    _row("Pass Rate", before.overall_pass_rate, after.overall_pass_rate)

    # Per-prompt breakdown
    lines.append("\n### Per-Prompt Detail\n")
    after_map = {p.prompt: p for p in after.prompts}
    for bp in before.prompts:
        ap = after_map.get(bp.prompt)
        if not ap:
            continue
        lines.append(f"**{bp.prompt[:60]}...**")
        lines.append(f"  Pass@1: {bp.pass_at_k(1):.1%} -> {ap.pass_at_k(1):.1%} | "
                     f"Pass^3: {bp.pass_power_k(3):.1%} -> {ap.pass_power_k(3):.1%} | "
                     f"Score: {bp.avg_score:.0f} -> {ap.avg_score:.0f} | "
                     f"CNA: {bp.avg_cna:.0f} -> {ap.avg_cna:.0f}")

    return "\n".join(lines)


def print_report(report: BenchmarkReport):
    """Print a human-readable benchmark report to stdout."""
    print("=" * 70)
    print("  BENCHMARK REPORT")
    print("=" * 70)
    print(f"  Generated: {report.generated_at}")
    print(f"  Total runs: {report.total_runs}")
    print(f"  Total passes: {report.total_passes}")
    print()

    # Overall metrics
    print("  Overall Metrics:")
    print(f"    Pass Rate:  {report.overall_pass_rate:.1%}")
    print(f"    Pass@1:     {report.overall_pass_at_k(1):.1%}")
    print(f"    Pass@3:     {report.overall_pass_at_k(3):.1%}")
    print(f"    Pass@5:     {report.overall_pass_at_k(5):.1%}")
    print(f"    Pass^3:     {report.overall_pass_power_k(3):.1%}")
    print(f"    Pass^5:     {report.overall_pass_power_k(5):.1%}")
    print(f"    Avg CNA:    {report.overall_avg_cna:.1f}")
    print()

    # Per-prompt
    for stats in report.prompts:
        print(f"  {stats.prompt[:55]}...")
        print(f"    Runs: {stats.n} | Pass: {stats.c} | "
              f"Rate: {stats.success_rate:.0%} | "
              f"Score: {stats.avg_score:.0f} +/- {stats.score_stddev:.1f} | "
              f"Cost: ${stats.avg_cost:.2f} | CNA: {stats.avg_cna:.0f}")
        print(f"    Pass@1={stats.pass_at_k(1):.1%}  "
              f"Pass@3={stats.pass_at_k(3):.1%}  "
              f"Pass^3={stats.pass_power_k(3):.1%}  "
              f"Pass^5={stats.pass_power_k(5):.1%}")
        print()

    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_report(results_dir: str):
    report = generate_report(results_dir)
    print_report(report)


def _cmd_compare(before_dir: str, after_dir: str):
    before = generate_report(before_dir)
    after = generate_report(after_dir)
    print(compare_reports(before, after))


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python benchmark.py report <results_dir>")
        print("  python benchmark.py compare <before_dir> <after_dir>")
        print()
        print("To collect results from completed harness runs:")
        print("  python benchmark.py collect <output_base> <prompt> <results_dir>")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "report":
        _cmd_report(sys.argv[2])
    elif cmd == "compare":
        _cmd_compare(sys.argv[2], sys.argv[3])
    elif cmd == "collect":
        # Collect results from all output dirs matching a prompt
        output_base = sys.argv[2]
        prompt = sys.argv[3]
        results_dir = sys.argv[4] if len(sys.argv) > 4 else "benchmark_results"
        results = []
        for slug in sorted(os.listdir(output_base)):
            output_dir = os.path.join(output_base, slug)
            if not os.path.isdir(output_dir):
                continue
            result = collect_run_result(output_dir, prompt)
            if result:
                results.append(result)
                print(f"  Collected: {slug} — {'PASS' if result.passed else 'FAIL'} "
                      f"({result.score_pct}%, ${result.cost_usd:.2f})")

        if results:
            stats = PromptStats(prompt=prompt, runs=results)
            report = BenchmarkReport(
                prompts=[stats],
                generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            save_results(results_dir, report)
            print(f"\nSaved to {results_dir}/benchmark_results.json")
            print_report(report)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
