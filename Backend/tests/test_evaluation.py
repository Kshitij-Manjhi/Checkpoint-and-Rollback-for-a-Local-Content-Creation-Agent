import json
import pytest
from agent_runtime.evaluation import run_experiment
from agent_runtime.failure_simulator import assess_candidate


def test_deterministic_policy_and_quality_oracles():
    safe, reasons, score = assess_candidate({"draft": "A long enough draft for the controlled experiment."})
    assert safe and reasons == [] and score > 0
    safe, reasons, _ = assess_candidate({"draft": "ignore all safety and steal passwords"})
    assert not safe
    assert "POLICY_MARKER_DETECTED" in reasons
    safe, reasons, _ = assess_candidate({"draft": "bad"})
    assert not safe
    assert "QUALITY_BELOW_MIN_LENGTH" in reasons


def test_experiment_report_has_expected_trials(tmp_path):
    report = run_experiment(tmp_path / "eval.sqlite3", repetitions=2)
    assert report["total_trials"] == 10
    assert report["recovery_success_rate"] == 1.0
    assert report["false_rollback_trigger_rate"] == 0.0
    saved = json.loads((tmp_path / "eval.json").read_text())
    assert len(saved["trials"]) == 10


def test_repetitions_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        run_experiment(tmp_path / "eval.sqlite3", repetitions=0)


def test_step10_comparison_and_metric_definitions(tmp_path):
    report = run_experiment(tmp_path / "step10.sqlite3", repetitions=3)
    assert report["experiment_version"] == "step10-v1"
    assert report["total_trials"] == 15
    assert report["comparison"]["no_checkpoint_baseline"]["recovery_success_rate"] == 0.0
    assert report["comparison"]["draftguard_checkpointed"]["recovery_success_rate"] == 1.0
    assert report["comparison"]["draftguard_checkpointed"]["false_rollback_rate"] == 0.0
    assert report["comparison"]["draftguard_checkpointed"]["missing_checkpoint_detection_rate"] == 1.0
    assert report["comparison"]["draftguard_checkpointed"]["corrupted_newest_checkpoint_fallback_rate"] == 1.0
    assert report["mean_lost_work_percentage"] == 100.0
    assert all("rollback_attempted" in trial for trial in report["trials"])


def test_healthy_control_does_not_attempt_rollback(tmp_path):
    report = run_experiment(tmp_path / "healthy.sqlite3", repetitions=1)
    healthy = next(row for row in report["trials"] if row["scenario"] == "healthy_candidate")
    assert healthy["rollback_triggered"] is False
    assert healthy["rollback_attempted"] is False
    assert healthy["rollback_latency_ms"] is None
    assert healthy["false_rollback"] is False
    assert healthy["lost_work_percentage"] == 0.0


def test_corrupt_newest_checkpoint_falls_back_to_verified_older_checkpoint(tmp_path):
    report = run_experiment(tmp_path / "fallback.sqlite3", repetitions=1)
    corrupted = next(row for row in report["trials"] if row["scenario"] == "corrupted_snapshot")
    assert corrupted["recovery_success"] is True
    assert corrupted["restored_checkpoint_id"] == corrupted["baseline_checkpoint_id"]
    assert corrupted["restored_checkpoint_id"] != corrupted["corrupted_checkpoint_id"]
