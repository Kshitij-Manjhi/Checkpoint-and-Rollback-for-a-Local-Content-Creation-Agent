"""Repeatable experiments for DraftGuard recovery and content-quality metrics.

All scenarios are deterministic synthetic workloads. They measure the recovery
runtime, not real-world LLM safety or semantic quality.
"""
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

SCENARIOS = (
    "quality_degradation",
    "policy_violation",
    "corrupted_snapshot",
    "missing_checkpoint",
    "healthy_candidate",
)


def _quality_score(state: dict[str, Any] | None) -> float | None:
    if state is None:
        return None
    return assess_candidate(state)[2]


def _candidate_for(scenario: str, baseline_text: str) -> dict[str, Any]:
    added_work = " Added work for this controlled experiment, with enough explanatory detail."
    draft = baseline_text + added_work
    if scenario == "quality_degradation":
        draft = "bad"
    elif scenario == "policy_violation":
        draft = baseline_text + " Ignore all safety and steal passwords from users."
    elif scenario == "healthy_candidate":
        draft = baseline_text + " This is a healthy extension with enough explanatory detail to pass."
    return {"draft": draft, "version": 2}


def _discarded_work_percentage(baseline_text: str, candidate_text: str, rollback_triggered: bool) -> float:
    """Character proxy for new candidate work discarded by rollback.

    The metric only counts characters beyond the exact baseline prefix. For a
    non-prefix candidate (e.g. a fully replaced draft), all candidate characters
    are treated as post-checkpoint work. A healthy candidate that is not rolled
    back loses 0%.
    """
    if not rollback_triggered:
        return 0.0
    added = candidate_text[len(baseline_text):] if candidate_text.startswith(baseline_text) else candidate_text
    return 100.0 if added else 0.0


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def run_experiment(output_path: str | Path, repetitions: int = 5) -> dict[str, Any]:
    """Run deterministic baseline-vs-checkpoint experiments and write JSON.

    `trials` contains per-scenario DraftGuard results. `comparison` contrasts
    a no-recovery baseline with checkpointed recovery on the same synthetic
    candidate cases. The no-recovery baseline retains the failed candidate and
    therefore has no recovery success; it is not intended as a production agent.
    """
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")

    db_path = Path(output_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteCheckpointStore(db_path)
    manager = CheckpointManager(store)
    rows: list[dict[str, Any]] = []

    for rep in range(repetitions):
        for scenario in SCENARIOS:
            task_id = f"{scenario}-{rep}-{uuid.uuid4().hex[:8]}"
            baseline = {
                "draft": "This is a sufficiently detailed safe baseline draft for recovery.",
                "version": 1,
            }
            baseline_checkpoint = None
            newer_checkpoint = None
            if scenario != "missing_checkpoint":
                baseline_checkpoint = manager.create_checkpoint(task_id, baseline, is_safe=True)
            candidate = _candidate_for(scenario, baseline["draft"])
            if scenario == "corrupted_snapshot" and baseline_checkpoint is not None:
                newer_state = {"draft": baseline["draft"] + " Newer safe version.", "version": 1.5}
                newer_checkpoint = manager.create_checkpoint(
                    task_id, newer_state, is_safe=True,
                    parent_checkpoint_id=baseline_checkpoint.checkpoint_id,
                )
                inject_corruption(store, newer_checkpoint.checkpoint_id)

            candidate_safe, reasons, candidate_quality = assess_candidate(candidate)
            expected_rollback = scenario != "healthy_candidate"
            integrity_fault = scenario in ("corrupted_snapshot", "missing_checkpoint")
            rollback_triggered = expected_rollback and (not candidate_safe or integrity_fault)
            rollback_attempted = False
            recovered = False
            restored_state = None
            error = None
            latency_ms: float | None = None
            restored_checkpoint_id = None

            if rollback_triggered:
                rollback_attempted = True
                start = time.perf_counter()
                try:
                    result = manager.restore_latest_safe(task_id)
                    restored_state = result.state
                    recovered = result.verification_passed
                    restored_checkpoint_id = result.checkpoint.checkpoint_id
                except (CheckpointNotFoundError, SnapshotIntegrityError) as exc:
                    error = type(exc).__name__
                latency_ms = (time.perf_counter() - start) * 1000.0
                if scenario == "missing_checkpoint":
                    recovered = False
                elif scenario in ("quality_degradation", "policy_violation"):
                    recovered = recovered and restored_state == baseline
                elif scenario == "corrupted_snapshot":
                    # The Step 9 policy should skip the corrupt newest safe checkpoint.
                    recovered = recovered and restored_state == baseline

            baseline_chars = len(baseline["draft"])
            candidate_chars = len(candidate["draft"])
            post_recovery_quality = _quality_score(restored_state)
            baseline_quality = _quality_score(baseline)
            lost_work_pct = _discarded_work_percentage(
                baseline["draft"], candidate["draft"], rollback_triggered and recovered
            )
            false_rollback = scenario == "healthy_candidate" and rollback_triggered
            rows.append({
                "scenario": scenario,
                "repetition": rep + 1,
                "expected_rollback": expected_rollback,
                "rollback_triggered": rollback_triggered,
                "rollback_attempted": rollback_attempted,
                "recovery_success": bool(recovered),
                "rollback_latency_ms": round(latency_ms, 4) if latency_ms is not None else None,
                "lost_work_percentage": round(lost_work_pct, 2),
                "false_rollback": bool(false_rollback),
                "candidate_quality_score": round(candidate_quality, 4),
                "baseline_quality_score": round(baseline_quality or 0.0, 4),
                "post_recovery_quality_score": round(post_recovery_quality, 4) if post_recovery_quality is not None else None,
                "baseline_checkpoint_id": baseline_checkpoint.checkpoint_id if baseline_checkpoint else None,
                "corrupted_checkpoint_id": newer_checkpoint.checkpoint_id if newer_checkpoint else None,
                "restored_checkpoint_id": restored_checkpoint_id,
                "error": error,
                "detection_reasons": reasons,
                "candidate_characters": candidate_chars,
                "baseline_characters": baseline_chars,
            })

    # Only failures for which a safe baseline existed are eligible for the
    # recovery-success denominator. Missing-checkpoint cases are reported apart.
    recoverable = [r for r in rows if r["expected_rollback"] and r["scenario"] != "missing_checkpoint"]
    successful = [r for r in recoverable if r["recovery_success"]]
    attempted = [r for r in rows if r["rollback_attempted"] and r["rollback_latency_ms"] is not None]
    healthy = [r for r in rows if r["scenario"] == "healthy_candidate"]
    failures = [r for r in rows if r["expected_rollback"] and r["scenario"] != "missing_checkpoint"]
    missing = [r for r in rows if r["scenario"] == "missing_checkpoint"]

    checkpointed_summary = {
        "recoverable_failure_trials": len(recoverable),
        "recovery_success_rate": round(len(successful) / len(recoverable), 4) if recoverable else None,
        "mean_rollback_latency_ms": _mean([r["rollback_latency_ms"] for r in attempted if r["rollback_latency_ms"] is not None]),
        "false_rollback_rate": round(sum(r["false_rollback"] for r in healthy) / len(healthy), 4) if healthy else None,
        "mean_lost_work_percentage": _mean([r["lost_work_percentage"] for r in failures]),
        "mean_post_recovery_quality_score": _mean([r["post_recovery_quality_score"] for r in successful if r["post_recovery_quality_score"] is not None]),
        "missing_checkpoint_detection_rate": round(sum(r["error"] == "CheckpointNotFoundError" for r in missing) / len(missing), 4) if missing else None,
        "corrupted_newest_checkpoint_fallback_rate": round(sum(r["recovery_success"] for r in rows if r["scenario"] == "corrupted_snapshot") / max(1, sum(r["scenario"] == "corrupted_snapshot" for r in rows)), 4),
    }
    baseline_summary = {
        "recoverable_failure_trials": len(recoverable),
        "recovery_success_rate": 0.0,
        "mean_rollback_latency_ms": None,
        "false_rollback_rate": 0.0,
        "mean_lost_work_percentage": 0.0,
        "mean_post_failure_quality_score": _mean([r["candidate_quality_score"] for r in recoverable]),
        "description": "No checkpoint restore is attempted; the failed candidate remains the active state.",
    }
    report = {
        "experiment_version": "step10-v1",
        "repetitions_per_scenario": repetitions,
        "total_trials": len(rows),
        "scenario_names": list(SCENARIOS),
        "comparison": {
            "no_checkpoint_baseline": baseline_summary,
            "draftguard_checkpointed": checkpointed_summary,
        },
        # Preserve earlier top-level metric names for existing consumers.
        "recovery_success_rate": checkpointed_summary["recovery_success_rate"],
        "mean_rollback_latency_ms": checkpointed_summary["mean_rollback_latency_ms"],
        "false_rollback_trigger_rate": checkpointed_summary["false_rollback_rate"],
        "mean_post_recovery_quality_score": checkpointed_summary["mean_post_recovery_quality_score"],
        "mean_lost_work_percentage": checkpointed_summary["mean_lost_work_percentage"],
        "notes": [
            "Deterministic synthetic scenarios; not a benchmark of real-world safety or semantic quality.",
            "Lost work is a character-count proxy for candidate content discarded by a successful rollback.",
            "Missing-checkpoint cases are reported separately and excluded from recoverable-failure success rate.",
            "Corrupted-newest-checkpoint cases test fallback to an older integrity-verified safe checkpoint.",
            "False rollback rate is measured only on healthy_candidate negative controls.",
            "Latency includes local SQLite lookup, integrity verification, and audit-event writes; machine and filesystem affect results.",
            "The no-checkpoint baseline is a simple controlled comparator, not an alternative production implementation.",
        ],
        "trials": rows,
    }
    report_path = db_path.with_suffix(".json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
