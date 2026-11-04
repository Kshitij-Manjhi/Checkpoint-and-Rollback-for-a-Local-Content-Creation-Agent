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
