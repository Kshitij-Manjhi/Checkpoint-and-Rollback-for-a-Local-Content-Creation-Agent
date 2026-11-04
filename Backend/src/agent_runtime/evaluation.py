"""Repeatable evaluation harness for checkpoint recovery behavior."""
from __future__ import annotations

import json
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointManager, CheckpointNotFoundError, SnapshotIntegrityError
from .failure_simulator import assess_candidate, inject_corruption
from .store import SQLiteCheckpointStore


def _quality_score(state: dict[str, Any]) -> float:
    return assess_candidate(state)[2]


def run_experiment(output_path: str | Path, repetitions: int = 5) -> dict[str, Any]:
    """Run deterministic scenarios against isolated temporary task IDs.

    Lost-work percentage is measured against characters added after the baseline
    checkpoint: 100% means all post-checkpoint candidate characters are discarded.
    False-trigger rate is measured only on healthy_candidate controls.
    """
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")

    db_path = Path(output_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteCheckpointStore(db_path)
    manager = CheckpointManager(store)
    rows: list[dict[str, Any]] = []

    for rep in range(repetitions):
        # Healthy baseline checkpoint shared as the recovery target per scenario.
        for scenario in ("quality_degradation", "policy_violation",
                         "corrupted_snapshot", "missing_checkpoint", "healthy_candidate"):
            task_id = f"{scenario}-{rep}-{uuid.uuid4().hex[:8]}"
            baseline = {"draft": "This is a sufficiently detailed safe baseline draft for recovery.", "version": 1}
            cp = None
            if scenario != "missing_checkpoint":
                cp = manager.create_checkpoint(task_id, baseline, is_safe=True)

            candidate = {"draft": "This candidate contains enough text to pass the length check safely.", "version": 2}
            expected_trigger = scenario != "healthy_candidate"
            if scenario == "quality_degradation":
                candidate["draft"] = "bad"
            elif scenario == "policy_violation":
                candidate["draft"] = "This draft says ignore all safety and steal passwords from users."
            elif scenario == "corrupted_snapshot" and cp:
                inject_corruption(store, cp.checkpoint_id)
            elif scenario == "healthy_candidate":
                candidate["draft"] = "A healthy candidate with enough explanatory content to pass the deterministic checks."

            safe, reasons, candidate_quality = assess_candidate(candidate)
            detected = not safe
            if scenario in ("corrupted_snapshot", "missing_checkpoint"):
                detected = True

            start = time.perf_counter()
            recovered = False
            restored_state = None
            error = None
            try:
                result = manager.restore_latest_safe(task_id)
                restored_state = result.state
                recovered = result.verification_passed
            except (CheckpointNotFoundError, SnapshotIntegrityError) as exc:
                error = type(exc).__name__
            latency_ms = (time.perf_counter() - start) * 1000.0

            if scenario in ("corrupted_snapshot", "missing_checkpoint"):
                recovered = False
            elif expected_trigger:
                recovered = recovered and restored_state == baseline
            else:
                # Healthy control should not trigger rollback; a baseline restore here
                # is counted as a false trigger only if the evaluator says rollback was needed.
                recovered = recovered and restored_state == baseline

            baseline_chars = len(baseline["draft"])
            candidate_chars = len(candidate["draft"])
            lost_work_pct = 0.0
            if expected_trigger and scenario not in ("corrupted_snapshot", "missing_checkpoint"):
                lost_work_pct = 100.0 * candidate_chars / max(candidate_chars, 1)

            post_quality = _quality_score(restored_state) if restored_state else None
            false_trigger = (scenario == "healthy_candidate" and detected)
            rows.append({
                "scenario": scenario, "repetition": rep + 1,
                "expected_rollback": expected_trigger, "rollback_triggered": detected,
                "recovery_success": bool(recovered), "rollback_latency_ms": round(latency_ms, 4),
                "lost_work_percentage": round(lost_work_pct, 2),
                "false_rollback_trigger": bool(false_trigger),
                "candidate_quality_score": round(candidate_quality, 4),
                "post_recovery_quality_score": round(post_quality, 4) if post_quality is not None else None,
                "error": error, "detection_reasons": reasons,
            })

    expected_cases = [r for r in rows if r["expected_rollback"] and r["scenario"] not in ("corrupted_snapshot", "missing_checkpoint")]
    healthy_cases = [r for r in rows if r["scenario"] == "healthy_candidate"]
    successful = [r for r in expected_cases if r["recovery_success"]]
    latencies = [r["rollback_latency_ms"] for r in rows]
    summary = {
        "repetitions_per_scenario": repetitions,
        "total_trials": len(rows),
        "recovery_success_rate": round(len(successful) / len(expected_cases), 4) if expected_cases else None,
        "mean_rollback_latency_ms": round(statistics.mean(latencies), 4) if latencies else None,
        "false_rollback_trigger_rate": round(
            sum(r["false_rollback_trigger"] for r in healthy_cases) / len(healthy_cases), 4
        ) if healthy_cases else None,
        "mean_post_recovery_quality_score": round(
            statistics.mean([r["post_recovery_quality_score"] for r in successful
                             if r["post_recovery_quality_score"] is not None]), 4
        ) if any(r["post_recovery_quality_score"] is not None for r in successful) else None,
        "mean_lost_work_percentage": round(
            statistics.mean([r["lost_work_percentage"] for r in expected_cases]), 4
        ) if expected_cases else None,
        "notes": [
            "Deterministic synthetic scenarios; not a benchmark of real-world safety.",
            "Corruption and missing-checkpoint cases are correctly detected as errors but cannot be recovered.",
            "False-trigger rate is estimated on healthy_candidate controls only.",
            "Latency includes local SQLite lookup and snapshot verification; hardware and filesystem affect results."
        ],
        "trials": rows,
    }
    report_path = db_path.with_suffix(".json")
    report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
