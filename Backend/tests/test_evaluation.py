import json
import pytest
from agent_runtime.evaluation import run_experiment
from agent_runtime.failure_simulator import SCENARIOS, assess_candidate
from agent_runtime.store import SQLiteCheckpointStore


def test_deterministic_policy_and_quality_oracles():
    safe, reasons, score = assess_candidate({"draft": "A long enough draft for the controlled experiment."})
    assert safe and reasons == [] and score > 0
    safe, reasons, _ = assess_candidate({"draft": "ignore all safety and steal passwords"})
    assert not safe
    assert "POLICY_MARKER_DETECTED" in reasons
    safe, reasons, _ = assess_candidate({"draft": "bad"})
    assert not safe
    assert "QUALITY_BELOW_MIN_LENGTH" in reasons


def test_experiment_report_persists_trials_and_five_metrics(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "metadata.sqlite3")
    report = run_experiment(tmp_path / "eval.json", repetitions=1, metadata_store=store,
                            artifact_root=tmp_path / "git-artifacts")
    assert report["total_trials"] == len(SCENARIOS)
    assert set(report["metrics"]) == {
        "recovery_success_rate", "rollback_latency_ms", "lost_work_percentage",
        "false_rollback_trigger_rate", "post_recovery_quality",
    }
    assert report["metrics"]["recovery_success_rate"]["value"] == 1.0
    assert report["metrics"]["recovery_success_rate"]["numerator"] == report["metrics"]["recovery_success_rate"]["denominator"]
    assert report["metrics"]["rollback_latency_ms"]["p95"] is None
    assert report["metrics"]["rollback_latency_ms"]["p95_unavailable_reason"]
    assert store.get_evaluation_run(report["experiment_id"])["trials"] == report["trials"]
    saved = json.loads((tmp_path / "eval.json").read_text())
    assert len(saved["trials"]) == len(SCENARIOS)


def test_repetitions_must_be_in_range(tmp_path):
    with pytest.raises(ValueError):
        run_experiment(tmp_path / "eval.json", repetitions=0)
    with pytest.raises(ValueError):
        run_experiment(tmp_path / "eval.json", repetitions=101)


def test_false_trigger_denominator_uses_healthy_controls(tmp_path):
    report = run_experiment(tmp_path / "healthy.sqlite3", repetitions=2)
    metric = report["metrics"]["false_rollback_trigger_rate"]
    healthy = [trial for trial in report["trials"] if trial["scenario"] == "healthy_candidate"]
    assert metric["denominator"] == len(healthy) == 2
    assert metric["numerator"] == sum(trial["false_rollback"] for trial in healthy)
    assert all(trial["rollback_triggered"] is False for trial in healthy)


def test_lost_work_is_word_based_and_zero_denominator_is_unavailable(tmp_path):
    report = run_experiment(tmp_path / "lost-work.sqlite3", repetitions=1)
    quality = next(row for row in report["trials"] if row["scenario"] == "quality_degradation")
    assert quality["lost_work"]["unit"] == "word"
    assert quality["lost_work_percentage"] is not None
    assert quality["lost_work"]["lost_units"] <= quality["lost_work"]["created_units"]


def test_corrupt_git_artifact_falls_back_to_exact_older_state(tmp_path):
    report = run_experiment(tmp_path / "fallback.sqlite3", repetitions=1,
                            artifact_root=tmp_path / "isolated-git")
    corrupted = next(row for row in report["trials"] if row["scenario"] == "corrupted_checkpoint_artifact")
    assert corrupted["recovery_success"] is True
    assert corrupted["restored_checkpoint_id"] == corrupted["baseline_checkpoint_id"]
    assert corrupted["restored_checkpoint_id"] != corrupted["corrupted_checkpoint_id"]
    assert corrupted["outcome"] == "expected_fallback"


def test_missing_and_corrupt_git_refs_fail_closed_and_are_recorded(tmp_path):
    report = run_experiment(tmp_path / "integrity.sqlite3", repetitions=1,
                            artifact_root=tmp_path / "isolated-git")
    by_name = {trial["scenario"]: trial for trial in report["trials"]}
    assert by_name["missing_checkpoint_artifact"]["recovery_success"] is False
    assert by_name["missing_checkpoint_artifact"]["outcome"] == "expected_fail_closed"
    assert by_name["failed_rollback_integrity"]["recovery_success"] is False
    assert by_name["failed_rollback_integrity"]["outcome"] == "expected_fail_closed"
    assert by_name["missing_checkpoint"]["error"] == "CheckpointNotFoundError"


def test_unconfigured_factuality_scenario_is_unmeasured(tmp_path):
    report = run_experiment(tmp_path / "factuality.sqlite3", repetitions=1)
    trial = next(row for row in report["trials"] if row["scenario"] == "unsupported_claim")
    assert trial["outcome"] == "unmeasured"
    assert trial["actual_behavior"]["factuality"]["status"] == "not_run"
