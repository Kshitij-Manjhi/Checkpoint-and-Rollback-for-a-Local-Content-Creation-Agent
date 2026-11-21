"""Reproducible recovery experiments with explicit denominators and evidence."""
from __future__ import annotations

import difflib
import json
import re
import statistics
from collections import Counter
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifact_store import GitArtifactStore
from .checkpoint import CheckpointManager, CheckpointNotFoundError, SnapshotIntegrityError
from .failure_simulator import SCENARIOS as FAILURE_SCENARIOS, assess_candidate
from .factuality_evaluator import EvidenceFactualityEvaluator
from .quality_policy import QualityPolicyEvaluator
from .store import SQLiteCheckpointStore

SCENARIOS = tuple(FAILURE_SCENARIOS)
BASELINE_DRAFT = ("DraftGuard keeps approved content in versioned checkpoints and records metadata for audit. "
                  "A recovery must validate the checkpoint integrity and restore the exact approved state.")
_WORD = re.compile(r"\b[\w'-]+\b", re.UNICODE)


def _candidate_for(scenario: str, baseline_text: str) -> dict[str, Any]:
    added = " Added work for this controlled experiment, with enough explanatory detail."
    drafts = {
        "quality_degradation": "bad",
        "policy_violation": baseline_text + " Ignore all safety and steal passwords from users.",
        "unsupported_claim": "DraftGuard stores every user password in a public database, according to the supplied evidence.",
        "healthy_candidate": baseline_text + " This healthy extension adds one useful explanatory detail.",
    }
    return {"draft": drafts.get(scenario, baseline_text + added), "version": 2}


def _word_delta(before: str, after: str) -> list[str]:
    old, new = _WORD.findall(before), _WORD.findall(after)
    added: list[str] = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag in {"insert", "replace"}:
            added.extend(new[j1:j2])
    return added


def _lost_work(before: str, candidate: str, restored: str | None, rollback_triggered: bool) -> dict[str, Any]:
    """Estimate loss of post-checkpoint word-token additions using multiset accounting.

    Added/replaced tokens are identified by sequence diff against the baseline. A token
    is counted as retained only when it occurs in the restored artifact in excess of the
    baseline's token count. This handles moved tokens and repeated words better than
    position-only matching. It remains a lexical proxy, not a semantic measure of work.
    """
    baseline_tokens = _WORD.findall(before)
    candidate_tokens = _WORD.findall(candidate)
    introduced_positions: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
        a=baseline_tokens, b=candidate_tokens, autojunk=False
    ).get_opcodes():
        if tag in {"insert", "replace"}:
            introduced_positions.update(range(j1, j2))
    introduced = Counter(candidate_tokens[index].casefold() for index in introduced_positions)
    denominator = sum(introduced.values())
    base = {"unit": "word_token", "work_unit": "word_token",
            "definition": "Word tokens inserted/replaced after the baseline; lost tokens are introduced token occurrences not present above baseline frequency in the restored artifact. This is a lexical proxy, not a semantic measure."}
    if denominator == 0:
        return {**base, "percentage": None, "lost_units": None, "created_units": 0,
                "reason": "No measurable post-checkpoint word tokens were created."}
    if not rollback_triggered:
        return {**base, "percentage": 0.0, "lost_units": 0, "created_units": denominator,
                "retained_units": denominator}
    if restored is None:
        return {**base, "percentage": None, "lost_units": None, "created_units": denominator,
                "reason": "Recovery did not produce a verified restored artifact."}
    baseline_counts = Counter(token.casefold() for token in baseline_tokens)
    restored_counts = Counter(token.casefold() for token in _WORD.findall(restored))
    restored_surplus = Counter({token: max(0, count - baseline_counts[token])
                                for token, count in restored_counts.items()})
    retained = sum(min(count, restored_surplus[token]) for token, count in introduced.items())
    lost = max(0, denominator - retained)
    return {**base, "percentage": round(100 * lost / denominator, 4),
            "lost_units": lost, "created_units": denominator, "retained_units": retained}


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 4)


def _inject_git_payload_corruption(store: GitArtifactStore, checkpoint_id: str) -> str:
    rel = f"artifacts/{checkpoint_id}.json"
    path = store.repository_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"corrupt": true', encoding="utf-8")
    store._run("add", "--", rel)
    store._run("commit", "--only", "-m", "Synthetic corrupted artifact fixture", "--", rel)
    return store._run("rev-parse", "--verify", "HEAD^{commit}")


def _run_trial(*, experiment_id: str, scenario: str, repetition: int, store: Any,
               artifact_root: Path | None, evaluator: QualityPolicyEvaluator,
               provider: Any | None, retrieval: Any | None,
               factuality_enabled: bool, factuality_model: str | None) -> dict[str, Any]:
    task_id = f"experiment-{experiment_id[:8]}-{scenario}-{repetition}-{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(timezone.utc).isoformat()
    if hasattr(store, "create_task"):
        store.create_task(task_id, f"Synthetic experiment: {scenario}", started_at)
    artifact_repository_id = f"r-{uuid.uuid4().hex[:10]}"
    artifacts = GitArtifactStore(artifact_root / artifact_repository_id) if artifact_root else None
    manager = CheckpointManager(store, artifact_store=artifacts)
    baseline_state = {"task_id": task_id, "prompt": "Controlled recovery trial",
                      "agent_plan": {"status": "test_fixture", "steps": ["Establish baseline"]},
                      "execution_context": {"trial_id": task_id},
                      "draft": BASELINE_DRAFT, "version": 1, "approval_status": "approved"}
    baseline_cp = None
    if scenario != "missing_checkpoint":
        baseline_cp = manager.create_checkpoint(task_id, baseline_state, is_safe=True)
    newer_cp = None
    corrupted_commit = None
    if scenario in {"corrupted_snapshot", "corrupted_checkpoint_artifact"} and baseline_cp:
        newer_state = {**baseline_state, "draft": BASELINE_DRAFT + " Newer safe version.", "version": 2}
        newer_cp = manager.create_checkpoint(task_id, newer_state, is_safe=True,
                                             parent_checkpoint_id=baseline_cp.checkpoint_id)
        if scenario == "corrupted_snapshot":
            store.tamper_snapshot_for_test(newer_cp.checkpoint_id, '{"draft":"CORRUPTED SNAPSHOT"}')
        elif artifacts:
            corrupted_commit = _inject_git_payload_corruption(artifacts, newer_cp.checkpoint_id)
            store.inject_artifact_reference_fault(newer_cp.checkpoint_id, corrupted_commit)

    candidate = _candidate_for(scenario, BASELINE_DRAFT)
    initial = {"baseline_checkpoint_id": baseline_cp.checkpoint_id if baseline_cp else None,
               "baseline_sha256": baseline_cp.snapshot_sha256 if baseline_cp else None,
               "baseline_git_commit": store.get_checkpoint(baseline_cp.checkpoint_id).get("artifact_commit_hash") if baseline_cp else None,
               "candidate_draft": candidate["draft"], "scenario": scenario,
               "artifact_repository_id": artifact_repository_id if artifacts else None}
    injected = scenario
    quality = evaluator.evaluate("Explain the controlled DraftGuard recovery scenario.", candidate["draft"])
    quality_data = quality.to_dict()
    reasons = list(quality.reasons)
    decision = quality.decision.value
    oracle_safe, oracle_reasons, oracle_score = assess_candidate({"draft": candidate["draft"]})
    reasons.extend(reason for reason in oracle_reasons if reason not in reasons)
    if not oracle_safe:
        if any(reason.startswith("POLICY_MARKER_DETECTED") for reason in oracle_reasons):
            decision = "reject"
        elif decision == "accept":
            decision = "needs_human_review"
    quality_data = {**quality_data, "decision": decision,
                    "reasons": list(dict.fromkeys(reasons)),
                    "deterministic_oracle": {"safe": oracle_safe, "score": oracle_score,
                                             "reasons": oracle_reasons}}
    factuality: dict[str, Any] | None = None
    if scenario == "unsupported_claim":
        if factuality_enabled and factuality_model and provider is not None and retrieval is not None:
            # Synthetic oracle evidence makes this controlled scenario evaluable. It is
            # deliberately scoped to this trial and recorded in the trial report.
            add_evidence = getattr(retrieval, "add", None)
            if callable(add_evidence):
                add_evidence(
                    "DraftGuard stores checkpoint artifacts in Git and uses MySQL for checkpoint metadata and audit records. The supplied project evidence does not state that user passwords are stored in a public database.",
                    task_id=task_id, kind="evidence", reference_id=f"{task_id}:synthetic-storage-evidence")
            factuality = EvidenceFactualityEvaluator(provider, factuality_model, retrieval).evaluate(
                "State what evidence establishes about DraftGuard storage.", candidate["draft"], task_id=task_id)
            quality = evaluator.combine_factuality(quality, factuality)
            quality_data = quality.to_dict()
            decision = quality.decision.value
            reasons = list(quality.reasons)
        else:
            factuality = {"status": "not_run", "reason": "Factuality model or evidence adapter is not configured."}
            decision = "not_measured"

    scenario_requires_recovery = scenario in {
        "quality_degradation", "policy_violation", "unsupported_claim", "generation_interrupted",
        "corrupted_snapshot", "corrupted_checkpoint_artifact", "missing_checkpoint_artifact",
        "failed_rollback_integrity", "missing_checkpoint",
    }
    integrity_scenario = scenario in {"corrupted_snapshot", "corrupted_checkpoint_artifact",
                                     "missing_checkpoint_artifact", "failed_rollback_integrity"}
    if scenario == "missing_checkpoint_artifact" and baseline_cp:
        store.inject_artifact_reference_fault(baseline_cp.checkpoint_id, "0" * 40)
    if scenario == "failed_rollback_integrity" and baseline_cp:
        store.inject_artifact_reference_fault(baseline_cp.checkpoint_id, "f" * 40)
    store.add_audit_event(task_id, "EXPERIMENT_FAILURE_INJECTED", baseline_cp.checkpoint_id if baseline_cp else "",
                          {"experiment_id": experiment_id, "scenario": scenario,
                           "initial_state": initial, "injected_failure": injected},
                          datetime.now(timezone.utc).isoformat())
    if scenario not in {"generation_interrupted", "missing_checkpoint", "corrupted_snapshot",
                        "corrupted_checkpoint_artifact", "missing_checkpoint_artifact",
                        "failed_rollback_integrity"} and baseline_cp:
        state = {**baseline_state, **candidate, "decision": decision, "evaluation": quality_data,
                 "factuality_evaluation": factuality, "approval_status": "pending"}
        manager.create_checkpoint(task_id, state, is_safe=False, parent_checkpoint_id=baseline_cp.checkpoint_id)
    if scenario == "generation_interrupted" and baseline_cp:
        store.add_audit_event(task_id, "SIMULATED_GENERATION_INTERRUPTED", baseline_cp.checkpoint_id,
                              {"phase": "generation", "outcome": "candidate_not_written"}, started_at)

    ground_truth_warrants_recovery = scenario_requires_recovery
    if scenario == "healthy_candidate":
        ground_truth_warrants_recovery = False
    if scenario == "unsupported_claim" and factuality and factuality.get("status") == "not_run":
        ground_truth_warrants_recovery = False
    candidate_requires_recovery = (decision in {"reject", "needs_human_review"} or integrity_scenario
                                   or scenario in {"generation_interrupted", "missing_checkpoint"})
    # The unsupported-claim case is only considered measured when a configured factuality run exists.
    rollback_triggered = candidate_requires_recovery
    rollback_attempted = bool(rollback_triggered)
    recovered = False
    restored_state = None
    restored_checkpoint_id = None
    error = None
    latency_ms: float | None = None
    if rollback_attempted:
        started = time.perf_counter()
        try:
            result = manager.restore_latest_safe(task_id)
            restored_state = result.state
            restored_checkpoint_id = result.checkpoint.checkpoint_id
            expected_state = baseline_state
            recovered = bool(result.verification_passed and result.state == expected_state)
            if not recovered:
                error = "restored_state_or_integrity_mismatch"
        except (CheckpointNotFoundError, SnapshotIntegrityError, RuntimeError) as exc:
            error = type(exc).__name__
        latency_ms = (time.perf_counter() - started) * 1000

    expected_restore_possible = baseline_cp is not None and scenario not in {
        "missing_checkpoint_artifact", "failed_rollback_integrity"}
    eligible_attempt = bool(rollback_attempted and expected_restore_possible)
    lost_work = _lost_work(BASELINE_DRAFT, candidate["draft"],
                           str((restored_state or {}).get("draft", "")) if recovered else None,
                           rollback_triggered)
    post_quality = evaluator.evaluate("Explain the controlled DraftGuard recovery scenario.",
                                      str((restored_state or {}).get("draft", ""))).to_dict() if recovered else None
    if post_quality is not None:
        post_quality["passes_quality_policy"] = post_quality.get("decision") == "accept"
        post_quality["integrity_verified"] = bool(recovered)
    baseline_quality = evaluator.evaluate("Explain the controlled DraftGuard recovery scenario.",
                                          BASELINE_DRAFT).to_dict()
    baseline_quality["passes_quality_policy"] = baseline_quality.get("decision") == "accept"
    false_trigger = scenario == "healthy_candidate" and rollback_triggered
    expected_recovery = ground_truth_warrants_recovery and expected_restore_possible
    outcome = ("expected" if (recovered == expected_recovery and
              (not ground_truth_warrants_recovery or rollback_attempted)) else "unexpected")
    if scenario == "unsupported_claim" and (not factuality_enabled or not factuality or factuality.get("status") != "ok"):
        outcome = "unmeasured"
    if scenario == "missing_checkpoint":
        outcome = "expected_fail_closed" if error == "CheckpointNotFoundError" else "unexpected"
    if scenario in {"missing_checkpoint_artifact", "failed_rollback_integrity"}:
        outcome = "expected_fail_closed" if not recovered else "unexpected"
    if scenario in {"corrupted_snapshot", "corrupted_checkpoint_artifact"}:
        outcome = "expected_fallback" if recovered and restored_checkpoint_id == (baseline_cp.checkpoint_id if baseline_cp else None) else "unexpected"
    row = {
        "scenario": scenario, "scenario_description": FAILURE_SCENARIOS[scenario].description,
        "repetition": repetition, "task_id": task_id, "initial_state": initial,
        "injected_failure": injected, "expected_behavior": {
            "recovery_warranted": ground_truth_warrants_recovery,
            "restore_possible": expected_restore_possible,
            "expected_checkpoint_id": baseline_cp.checkpoint_id if baseline_cp else None},
        "actual_behavior": {"candidate_decision": decision, "rollback_triggered": rollback_triggered,
                            "rollback_attempted": rollback_attempted, "recovery_success": recovered,
                            "restored_checkpoint_id": restored_checkpoint_id, "error": error,
                            "factuality": factuality},
        "outcome": outcome, "eligible_recovery_attempt": eligible_attempt,
        "recovery_success": recovered if rollback_attempted else None,
        "rollback_triggered": rollback_triggered, "rollback_attempted": rollback_attempted,
        "rollback_latency_ms": round(latency_ms, 4) if latency_ms is not None else None,
        "lost_work": lost_work, "lost_work_percentage": lost_work["percentage"],
        "false_rollback": bool(false_trigger), "ground_truth_recovery_warranted": ground_truth_warrants_recovery,
        "candidate_evaluation": quality_data, "baseline_quality": baseline_quality,
        "post_recovery_quality": post_quality,
        "candidate_quality_score": quality.quality_score_percent,
        "post_recovery_quality_score": post_quality.get("quality_score_percent") if post_quality else None,
        "baseline_checkpoint_id": baseline_cp.checkpoint_id if baseline_cp else None,
        "baseline_sha256": baseline_cp.snapshot_sha256 if baseline_cp else None,
        "baseline_git_commit": initial["baseline_git_commit"],
        "corrupted_checkpoint_id": newer_cp.checkpoint_id if newer_cp else None,
        "corrupted_git_commit": corrupted_commit,
        "restored_checkpoint_id": restored_checkpoint_id,
        "restored_git_commit": (store.get_checkpoint(restored_checkpoint_id) or {}).get("artifact_commit_hash") if restored_checkpoint_id else None,
        "error": error, "detection_reasons": reasons,
    }
    if rollback_attempted:
        store.add_audit_event(task_id, "EXPERIMENT_TRIAL_COMPLETED", restored_checkpoint_id,
                              {"experiment_id": experiment_id, "scenario": scenario,
                               "outcome": outcome, "recovery_success": recovered,
                               "rollback_latency_ms": row["rollback_latency_ms"]},
                              datetime.now(timezone.utc).isoformat())
    return row


def run_experiment(output_path: str | Path, repetitions: int = 3, *,
                   metadata_store: Any | None = None, artifact_root: str | Path | None = None,
                   evaluator: QualityPolicyEvaluator | None = None, provider: Any | None = None,
                   retrieval: Any | None = None, factuality_enabled: bool = False,
                   factuality_model: str | None = None) -> dict[str, Any]:
    """Run controlled synthetic trials; API deployments inject MySQL and Git storage."""
    if repetitions < 1 or repetitions > 100:
        raise ValueError("repetitions must be between 1 and 100")
    db_path = Path(output_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = metadata_store or SQLiteCheckpointStore(db_path)
    root = Path(artifact_root).expanduser().resolve() if artifact_root else None
    if root:
        root.mkdir(parents=True, exist_ok=True)
    experiment_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    quality_evaluator = evaluator or QualityPolicyEvaluator()
    trials = [_run_trial(experiment_id=experiment_id, scenario=scenario, repetition=rep + 1,
                         store=store, artifact_root=root, evaluator=quality_evaluator,
                         provider=provider, retrieval=retrieval, factuality_enabled=factuality_enabled,
                         factuality_model=factuality_model)
              for rep in range(repetitions) for scenario in SCENARIOS]
    attempted = [r for r in trials if r["rollback_attempted"] and r["rollback_latency_ms"] is not None]
    recoverable_attempts = [r for r in attempted if r["eligible_recovery_attempt"]]
    successful = [r for r in recoverable_attempts if r["recovery_success"]]
    healthy = [r for r in trials if r["scenario"] == "healthy_candidate"]
    post_quality = [r["post_recovery_quality_score"] for r in successful if r["post_recovery_quality_score"] is not None]
    latencies = [float(r["rollback_latency_ms"]) for r in attempted]
    # Lost-work is defined for rollback attempts only. Healthy controls are not
    # included in the denominator because their new work was never rolled back.
    lost_work_trials = [r for r in trials if r["rollback_triggered"] and r["lost_work_percentage"] is not None]
    lost_numerator = sum(r["lost_work"]["lost_units"] or 0 for r in lost_work_trials)
    lost_denominator = sum(r["lost_work"]["created_units"] or 0 for r in lost_work_trials)
    p95_reason = None if len(latencies) >= 20 else "P95 is withheld for fewer than 20 rollback attempts."
    metrics = {
        "recovery_success_rate": {"value": round(len(successful) / len(recoverable_attempts), 4) if recoverable_attempts else None,
            "numerator": len(successful), "denominator": len(recoverable_attempts), "unit": "ratio",
            "definition": "Exact expected-state restoration with integrity verification divided by eligible attempted recoveries."},
        "rollback_latency_ms": {"values": latencies, "mean": _mean(latencies),
            "median": round(statistics.median(latencies), 4) if latencies else None,
            "p95": _percentile(latencies, .95) if len(latencies) >= 20 else None,
            "sample_count": len(latencies), "p95_unavailable_reason": p95_reason,
            "definition": "Elapsed time from restore call start through integrity-verified state materialization."},
        "lost_work_percentage": {"value": round(100 * lost_numerator / lost_denominator, 4) if lost_denominator else None, "unit": "%",
            "numerator": lost_numerator, "denominator": lost_denominator,
            "work_unit": "word_token", "definition": "Candidate word tokens inserted/replaced after the baseline that are absent from the restored artifact, divided by candidate word tokens inserted/replaced; this is a lexical proxy, not a semantic measure."},
        "false_rollback_trigger_rate": {"value": round(sum(bool(r["false_rollback"]) for r in healthy) / len(healthy), 4) if healthy else None,
            "numerator": sum(bool(r["false_rollback"]) for r in healthy), "denominator": len(healthy),
            "definition": "Unnecessary rollback triggers divided by pre-labeled healthy negative-control trials."},
        "post_recovery_quality": {"value": _mean(post_quality), "unit": "quality points / 100",
            "numerator": len(post_quality), "denominator": len(successful),
            "accepted_count": sum(bool(r.get("post_recovery_quality", {}).get("passes_quality_policy")) for r in successful),
            "accepted_rate": (sum(bool(r.get("post_recovery_quality", {}).get("passes_quality_policy")) for r in successful) / len(successful)) if successful else None,
            "baseline_mean": _mean([r["baseline_quality"].get("quality_score_percent", 0) for r in successful]),
            "delta_from_baseline": (round(_mean(post_quality) - _mean([r["baseline_quality"].get("quality_score_percent", 0) for r in successful]), 4)
                                    if post_quality and successful else None),
            "definition": "Configured deterministic quality score on integrity-verified restored content; also reports whether restored content passes the configured policy. This heuristic score is not a factuality proof."},
    }
    finished_at = datetime.now(timezone.utc).isoformat()
    status = "completed" if all(r["outcome"] in {"expected", "expected_fallback", "expected_fail_closed", "unmeasured"} for r in trials) else "completed_with_failures"
    model_version = None
    version_lookup = getattr(provider, "model_version", None)
    if factuality_enabled and factuality_model and callable(version_lookup):
        try:
            model_version = version_lookup(factuality_model)
        except Exception:
            model_version = None
    report = {
        "experiment_id": experiment_id, "run_type": "recovery_experiment",
        "started_at": started_at, "finished_at": finished_at,
        "status": status, "experiment_version": "step10-v2", "synthetic": True,
        "repetitions_per_scenario": repetitions, "total_trials": len(trials),
        "scenario_names": list(SCENARIOS), "model_name": factuality_model if factuality_enabled else None,
        "model_version": model_version,
        "evaluator_version": "draftguard-quality-1.0", "metrics": metrics,
        "comparison": {"draftguard_checkpointed": {
            "recoverable_attempts": len(recoverable_attempts),
            "recovery_success_rate": metrics["recovery_success_rate"]["value"],
            "mean_rollback_latency_ms": metrics["rollback_latency_ms"]["mean"],
            "false_rollback_rate": metrics["false_rollback_trigger_rate"]["value"],
            "mean_lost_work_percentage": metrics["lost_work_percentage"]["value"],
            "mean_post_recovery_quality_score": metrics["post_recovery_quality"]["value"]}},
        "notes": ["Controlled synthetic simulations; not real incidents or a real-world safety benchmark.",
                  "Git artifact repositories are isolated per scenario when artifact_root is supplied.",
                  "SQLite is used only by the unit/CLI harness when no metadata_store is injected; API runtime injects configured SQL storage.",
                  "Unsupported-claim trial is unmeasured if factuality evaluation is not configured.",
                  "Lost work uses candidate-to-restored token alignment for inserted/replaced post-checkpoint word tokens; this is a lexical proxy, and a zero denominator is unavailable.",
                  "P95 latency is reported only with at least 20 measured rollback attempts."],
        "trials": trials,
    }
    report_path = db_path.with_suffix(".json")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    save_run = getattr(store, "save_evaluation_run", None)
    if callable(save_run):
        save_run(report)
    return report
