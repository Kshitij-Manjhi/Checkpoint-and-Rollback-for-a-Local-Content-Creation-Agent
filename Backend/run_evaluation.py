"""Run controlled recovery experiments and print a readable terminal report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from agent_runtime.evaluation import run_experiment


def _fmt(value: Any, *, suffix: str = "", precision: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{precision}f}{suffix}"
    return str(value)


def _metric_value(metrics: dict[str, Any], key: str, field: str = "value") -> Any:
    return metrics.get(key, {}).get(field)


def print_terminal_report(result: dict[str, Any], elapsed_seconds: float) -> None:
    """Print aggregate metrics and per-scenario outcomes without extra dependencies."""
    metrics = result.get("metrics", {})
    trials = result.get("trials", [])
    total = result.get("total_trials", len(trials))
    outcomes: dict[str, dict[str, int]] = {}
    for trial in trials:
        scenario = str(trial.get("scenario", "unknown"))
        bucket = outcomes.setdefault(scenario, {"total": 0, "expected": 0, "unmeasured": 0, "unexpected": 0})
        bucket["total"] += 1
        outcome = str(trial.get("outcome", "unexpected"))
        if outcome in {"expected", "expected_fallback", "expected_fail_closed"}:
            bucket["expected"] += 1
        elif outcome == "unmeasured":
            bucket["unmeasured"] += 1
        else:
            bucket["unexpected"] += 1

    recovery = metrics.get("recovery_success_rate", {})
    latency = metrics.get("rollback_latency_ms", {})
    lost = metrics.get("lost_work_percentage", {})
    false_rate = metrics.get("false_rollback_trigger_rate", {})
    quality = metrics.get("post_recovery_quality", {})

    # Recovery-specific fault-injection metrics. Count only trials that exercised
    # the relevant scenario, and print numerator/denominator so results are auditable.
    recoverable_restored = (recovery.get("numerator", 0), recovery.get("denominator", 0))
    corruption_scenarios = {"corrupted_snapshot", "corrupted_checkpoint_artifact"}
    corruption_trials = [trial for trial in trials if trial.get("scenario") in corruption_scenarios]
    corruption_fallbacks = [trial for trial in corruption_trials
                            if trial.get("outcome") == "expected_fallback"]
    missing_scenarios = {"missing_checkpoint", "missing_checkpoint_artifact"}
    missing_trials = [trial for trial in trials if trial.get("scenario") in missing_scenarios]
    missing_detected = [trial for trial in missing_trials
                        if trial.get("outcome") == "expected_fail_closed"]

    print("\n" + "=" * 76)
    print(" DRAFTGUARD | RECOVERY EXPERIMENT RESULTS")
    print("=" * 76)
    print(f"Status: {result.get('status', 'unknown')}    |    Trials: {total}    |    Repetitions/scenario: {result.get('repetitions_per_scenario', 'N/A')}")
    print(f"Run type: {'Synthetic controlled test' if result.get('synthetic') else result.get('run_type', 'unknown')}    |    Duration: {elapsed_seconds:.2f} s")
    print("\nAGGREGATE METRICS")
    print("-" * 76)
    rows = [
        ("Recovery success rate", f"{_fmt(_metric_value(metrics, 'recovery_success_rate') * 100 if _metric_value(metrics, 'recovery_success_rate') is not None else None, suffix='%')}  ({recovery.get('numerator', 0)}/{recovery.get('denominator', 0)} eligible recoveries)"),
        ("Rollback latency — mean", _fmt(latency.get("mean"), suffix=" ms", precision=4)),
        ("Rollback latency — median", _fmt(latency.get("median"), suffix=" ms", precision=4)),
        ("Rollback latency — p95", _fmt(latency.get("p95"), suffix=" ms", precision=4)),
        ("Lost-work estimate", f"{_fmt(_metric_value(metrics, 'lost_work_percentage'), suffix='%')}  ({lost.get('numerator', 0)}/{lost.get('denominator', 0)} word tokens)"),
        ("False rollback trigger rate", f"{_fmt(_metric_value(metrics, 'false_rollback_trigger_rate') * 100 if _metric_value(metrics, 'false_rollback_trigger_rate') is not None else None, suffix='%')}  ({false_rate.get('numerator', 0)}/{false_rate.get('denominator', 0)} healthy controls)"),
        ("Post-recovery quality", f"{_fmt(_metric_value(metrics, 'post_recovery_quality'), suffix='/100')}  ({quality.get('numerator', 0)}/{quality.get('denominator', 0)} successful recoveries)"),
        ("Restored drafts passing policy", f"{quality.get('accepted_count', 0)}/{quality.get('denominator', 0)}  ({_fmt(quality.get('accepted_rate') * 100 if quality.get('accepted_rate') is not None else None, suffix='%')})"),
    ]
    for label, value in rows:
        print(f"  {label:<34} {value}")

    print("\nREQUESTED RECOVERY CHECKS")
    print("-" * 76)
    print(f"  {'Recoverable failures restored':<34} {recoverable_restored[0]}/{recoverable_restored[1]}")
    print(f"  {'Mean rollback latency':<34} {_fmt(latency.get('mean'), suffix=' ms', precision=4)}")
    print(f"  {'False rollback rate':<34} {_fmt(_metric_value(metrics, 'false_rollback_trigger_rate') * 100 if _metric_value(metrics, 'false_rollback_trigger_rate') is not None else None, suffix='%')}  ({false_rate.get('numerator', 0)}/{false_rate.get('denominator', 0)} healthy controls)")
    corruption_rate = (100 * len(corruption_fallbacks) / len(corruption_trials)) if corruption_trials else None
    missing_rate = (100 * len(missing_detected) / len(missing_trials)) if missing_trials else None
    print(f"  {'Corrupt-checkpoint fallback':<34} {len(corruption_fallbacks)}/{len(corruption_trials)} ({_fmt(corruption_rate, suffix='%')})")
    print(f"  {'Missing-checkpoint detection':<34} {len(missing_detected)}/{len(missing_trials)} ({_fmt(missing_rate, suffix='%')})")
    if not corruption_trials:
        print("  Note: no corruption scenarios were present in this run.")
    if not missing_trials:
        print("  Note: no missing-checkpoint scenarios were present in this run.")

    if latency.get("p95") is None and latency.get("p95_unavailable_reason"):
        print(f"  Note: {latency['p95_unavailable_reason']}")

    print("\nSCENARIO BREAKDOWN")
    print("-" * 76)
    print(f"  {'Scenario':<34} {'Trials':>6} {'Expected':>9} {'Unmeasured':>11} {'Unexpected':>11}")
    print("  " + "-" * 72)
    for scenario in result.get("scenario_names", outcomes.keys()):
        counts = outcomes.get(scenario, {"total": 0, "expected": 0, "unmeasured": 0, "unexpected": 0})
        print(f"  {scenario:<34} {counts['total']:>6} {counts['expected']:>9} {counts['unmeasured']:>11} {counts['unexpected']:>11}")

    unexpected_trials = [trial for trial in trials if trial.get("outcome") not in {"expected", "expected_fallback", "expected_fail_closed", "unmeasured"}]
    if unexpected_trials:
        print("\nUNEXPECTED TRIALS")
        for trial in unexpected_trials:
            print(f"  - {trial.get('scenario')} repetition {trial.get('repetition')}: {trial.get('error') or trial.get('outcome')}")
    unmeasured = [trial for trial in trials if trial.get("outcome") == "unmeasured"]
    if unmeasured:
        print(f"\nNote: {len(unmeasured)} trial(s) were unmeasured; inspect the JSON report for the reason.")
    print("\nInterpretation: controlled synthetic fault injection, not a real-world AI safety benchmark.")
    print("Lost work is a word-token proxy; quality is a heuristic score, not proof of factuality.")
    print("=" * 76)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic checkpoint recovery experiments and display terminal metrics.")
    parser.add_argument("--output", default="data/evaluation.sqlite3", help="SQLite database path; JSON report is written alongside it")
    parser.add_argument("--repetitions", type=int, default=5, help="Trials per scenario (10 scenarios x repetitions = total trials)")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 100:
        parser.error("--repetitions must be between 1 and 100")

    started = perf_counter()
    print(f"Starting DraftGuard recovery experiment: {args.repetitions} repetitions x 10 scenarios = {args.repetitions * 10} trials")
    result = run_experiment(args.output, repetitions=args.repetitions)
    elapsed = perf_counter() - started
    print_terminal_report(result, elapsed)
    report_path = Path(args.output).with_suffix(".json")
    print(f"\nDetailed JSON report: {report_path}")
    print(f"SQLite experiment database: {args.output}")


if __name__ == "__main__":
    main()
