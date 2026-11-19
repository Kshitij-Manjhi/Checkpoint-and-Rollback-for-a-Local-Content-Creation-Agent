"""Step 11 persistent provenance and responsible-AI audit regression tests."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agent_runtime.api import create_app
from agent_runtime.checkpoint import CheckpointManager
from agent_runtime.quality_policy import QualityPolicyEvaluator
from agent_runtime.store import SQLiteCheckpointStore
from agent_runtime.workflow import ContentWorkflow


DRAFT = (
    "Checkpoint provenance records each generated artifact, its evaluation, "
    "human review, and the verified recovery point."
)


class Provider:
    version = "fake-provider-2"
    model_versions = {"qwen3:4b": "sha256:fixture-digest"}

    def __init__(self, fail=False):
        self.fail = fail

    def list_models(self):
        return ["qwen3:4b"]

    def generate(self, model, prompt, *, system=None):
        if self.fail:
            raise RuntimeError("fixture generation failure")
        return DRAFT


def build(db_path, *, provider=None, raise_server_exceptions=True):
    store = SQLiteCheckpointStore(db_path)
    workflow = ContentWorkflow(
        CheckpointManager(store), QualityPolicyEvaluator(min_characters=40),
        provider=provider or Provider(),
    )
    client = TestClient(create_app(workflow=workflow), raise_server_exceptions=raise_server_exceptions)
    return client, store


def create_and_generate(client, task_id):
    assert client.post("/api/tasks", json={"task_id": task_id, "title": "Audit"}).status_code == 201
    return client.post(f"/api/tasks/{task_id}/generate", json={
        "prompt": "Explain checkpoint lineage", "mode": "ollama", "model_name": "qwen3:4b",
    }).json()


def test_provenance_is_complete_and_survives_app_restart(tmp_path):
    db_path = tmp_path / "audit.sqlite3"
    client, _ = build(db_path)
    draft = create_and_generate(client, "provenance")
    generated = next(e for e in client.get("/api/tasks/provenance/events").json()["events"]
                     if e["event_type"] == "CONTENT_GENERATED")
    details = generated["details"]
    assert details["prompt"] == "Explain checkpoint lineage"
    assert details["output"] == DRAFT
    assert details["provider"] == "ollama"
    assert details["provider_version"] == "fake-provider-2"
    assert details["model_version"] == "sha256:fixture-digest"
    assert details["checkpoint_id"] == draft["checkpoint_id"]
    assert details["parent_checkpoint_id"] == draft["parent_checkpoint_id"]

    client2, _ = build(db_path)
    events = client2.get("/api/tasks/provenance/events").json()["events"]
    evaluated = next(e for e in events if e["event_type"] == "CONTENT_EVALUATED")
    assert evaluated["details"]["scores"]
    assert evaluated["details"]["evaluator_version"]
    assert evaluated["details"]["findings"] is not None
    assert evaluated["details"]["policy_decision"] == "accept"
    assert evaluated["details"]["checkpoint_integrity_verified"] is True


def test_approve_reject_and_rollback_audits_record_reviewer_and_target(tmp_path):
    client, _ = build(tmp_path / "reviews.sqlite3")
    approved_draft = create_and_generate(client, "review-audit")
    approved = client.post(f"/api/checkpoints/{approved_draft['checkpoint_id']}/review", json={
        "decision": "approve", "reviewer": "alice", "reason": "Reviewed and safe to restore.",
    })
    assert approved.status_code == 200
    rejected_draft = client.post("/api/tasks/review-audit/generate", json={
        "prompt": "Another version", "mode": "no_llm",
    }).json()
    rejected = client.post(f"/api/checkpoints/{rejected_draft['checkpoint_id']}/review", json={
        "decision": "reject", "reviewer": "bob", "reason": "Needs factual sources.",
    })
    assert rejected.status_code == 200
    rollback = client.post(f"/api/checkpoints/{approved_draft['checkpoint_id']}/rollback")
    assert rollback.status_code == 200
    assert rollback.json()["checkpoint_id"] == approved_draft["checkpoint_id"]

    events = client.get("/api/tasks/review-audit/events").json()["events"]
    approve_event = next(e for e in events if e["event_type"] == "HUMAN_REVIEW_APPROVED")
    reject_event = next(e for e in events if e["event_type"] == "HUMAN_REVIEW_REJECTED")
    rollback_event = next(e for e in events if e["event_type"] == "WORKFLOW_ROLLBACK")
    assert approve_event["details"]["reviewer"] == "alice"
    assert approve_event["details"]["reason"] == "Reviewed and safe to restore."
    assert approve_event["created_at"]
    assert reject_event["details"]["reviewer"] == "bob"
    assert reject_event["details"]["reason"] == "Needs factual sources."
    assert rollback_event["details"]["requested_checkpoint_id"] == approved_draft["checkpoint_id"]
    assert rollback_event["details"]["target_checkpoint_id"] == rollback.json()["checkpoint_id"]
    assert rollback_event["details"]["integrity_outcome"] == "verified"


def test_generation_and_integrity_failure_are_audited(tmp_path):
    client, store = build(tmp_path / "failures.sqlite3", provider=Provider(fail=True),
                          raise_server_exceptions=False)
    assert client.post("/api/tasks", json={"task_id": "generation-failure"}).status_code == 201
    response = client.post("/api/tasks/generation-failure/generate", json={
        "prompt": "A prompt that must be retained", "mode": "ollama", "model_name": "qwen3:4b",
    })
    assert response.status_code == 500
    events = client.get("/api/tasks/generation-failure/events").json()["events"]
    failed = next(e for e in events if e["event_type"] == "GENERATION_FAILED")
    assert failed["details"]["prompt"] == "A prompt that must be retained"
    assert failed["details"]["failure_type"] == "RuntimeError"

    # The baseline generated before the provider failure is damaged to force
    # the rollback integrity-failure path and its durable audit event.
    baseline_id = client.get("/api/tasks/generation-failure/checkpoints").json()["checkpoints"][0]["checkpoint_id"]
    store.tamper_snapshot_for_test(baseline_id, json.dumps({"damaged": True}))
    rollback = client.post("/api/tasks/generation-failure/rollback")
    assert rollback.status_code == 409
    events = client.get("/api/tasks/generation-failure/events").json()["events"]
    assert any(e["event_type"] == "ROLLBACK_FAILED" and
               e["details"]["verification_passed"] is False for e in events)
    assert any(e["event_type"] == "WORKFLOW_ROLLBACK_FAILED" and
               e["details"]["outcome"] == "no_restore" for e in events)
