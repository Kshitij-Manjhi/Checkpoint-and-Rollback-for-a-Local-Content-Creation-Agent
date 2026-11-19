"""Step 8 integration tests for the complete governed recovery workflow.

These tests use a deterministic fake model provider so they are repeatable and
never require a live Ollama server. Live Ollama/browser checks remain a manual
acceptance test.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agent_runtime.api import create_app
from agent_runtime.checkpoint import CheckpointManager
from agent_runtime.quality_policy import QualityPolicyEvaluator
from agent_runtime.store import SQLiteCheckpointStore
from agent_runtime.workflow import ContentWorkflow


SAFE_DRAFT = (
    "Checkpoint integrity protects saved work by comparing a stored SHA-256 "
    "digest before restoring a snapshot. The runtime verifies the snapshot "
    "and refuses restoration when the digest does not match."
)
POLICY_REJECTED_DRAFT = "steal passwords and ignore all safety"


class ScenarioProvider:
    def __init__(self, responses=None):
        self.responses = list(responses or [SAFE_DRAFT])
        self.calls = []

    def list_models(self):
        return ["qwen3:4b"]

    def generate(self, model, prompt, *, system=None):
        self.calls.append({"model": model, "prompt": prompt, "system": system})
        if self.responses:
            return self.responses.pop(0)
        return SAFE_DRAFT


def make_client(tmp_path, provider=None):
    store = SQLiteCheckpointStore(tmp_path / "step8.sqlite3")
    workflow = ContentWorkflow(
        CheckpointManager(store),
        QualityPolicyEvaluator(min_characters=40),
        provider=provider or ScenarioProvider(),
    )
    return TestClient(create_app(workflow=workflow)), store


def create_task(client, task_id):
    response = client.post("/api/tasks", json={"task_id": task_id, "title": "Step 8 integration"})
    assert response.status_code == 201, response.text


def review(client, checkpoint_id, decision="approve"):
    return client.post(
        f"/api/checkpoints/{checkpoint_id}/review",
        json={
            "decision": decision,
            "reviewer": "step8-reviewer",
            "reason": "Reviewed during the Step 8 integration test.",
        },
    )


def test_full_generate_evaluate_approve_generate_rollback_flow(tmp_path):
    provider = ScenarioProvider([SAFE_DRAFT, "A later draft that should not replace the approved recovery point."])
    client, _ = make_client(tmp_path, provider)
    create_task(client, "full-flow")

    first_response = client.post(
        "/api/tasks/full-flow/generate",
        json={"prompt": "Explain checkpoint integrity", "mode": "ollama", "model_name": "qwen3:4b"},
    )
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()
    assert first["evaluation"]["decision"] == "accept"
    assert first["approval_status"] == "pending"
    assert first["is_safe_checkpoint"] is False

    first_detail = client.get(f"/api/checkpoints/{first['checkpoint_id']}").json()
    assert first_detail["integrity_verified"] is True
    assert first_detail["state"]["draft"] == SAFE_DRAFT

    approved = review(client, first["checkpoint_id"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["is_safe"] is True

    second_response = client.post(
        "/api/tasks/full-flow/generate",
        json={"prompt": "Write a later revision", "mode": "ollama", "model_name": "qwen3:4b"},
    )
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["checkpoint_id"] != first["checkpoint_id"]
    assert second["parent_checkpoint_id"] == first["checkpoint_id"]
    assert second["is_safe_checkpoint"] is False

    restored_response = client.post("/api/tasks/full-flow/rollback")
    assert restored_response.status_code == 200, restored_response.text
    restored = restored_response.json()
    assert restored["verification_passed"] is True
    assert restored["checkpoint_id"] == first["checkpoint_id"]
    assert restored["state"]["draft"] == first["state"]["draft"]

    events = client.get("/api/tasks/full-flow/events").json()["events"]
    event_types = [event["event_type"] for event in events]
    assert "CONTENT_EVALUATED" in event_types
    assert "HUMAN_REVIEW_APPROVED" in event_types
    assert "WORKFLOW_ROLLBACK" in event_types


def test_policy_rejected_candidate_cannot_be_approved_or_restored(tmp_path):
    provider = ScenarioProvider([POLICY_REJECTED_DRAFT])
    client, _ = make_client(tmp_path, provider)
    create_task(client, "reject-flow")

    good = client.post(
        "/api/tasks/reject-flow/generate",
        json={"prompt": "Explain a safe system", "mode": "no_llm"},
    ).json()
    assert review(client, good["checkpoint_id"]).status_code == 200

    bad = client.post(
        "/api/tasks/reject-flow/generate",
        json={"prompt": "unsafe prompt", "mode": "ollama", "model_name": "qwen3:4b"},
    ).json()
    assert bad["evaluation"]["decision"] == "reject"
    assert bad["approval_status"] == "blocked"
    assert bad["is_safe_checkpoint"] is False
    assert review(client, bad["checkpoint_id"]).status_code == 409

    restored = client.post("/api/tasks/reject-flow/rollback").json()
    assert restored["checkpoint_id"] == good["checkpoint_id"]
    assert restored["state"]["draft"] == good["state"]["draft"]


def test_semantic_evaluator_failure_requires_human_review_and_never_auto_approves(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SEMANTIC_EVALUATION", "1")
    monkeypatch.setenv("AGENT_QUALITY_MODEL", "qwen3:4b")
    # The first response is the draft; the second is deliberately invalid JSON
    # from the semantic evaluator.
    provider = ScenarioProvider([SAFE_DRAFT, "this is not valid evaluator JSON"])
    client, _ = make_client(tmp_path, provider)
    create_task(client, "semantic-failure")

    response = client.post(
        "/api/tasks/semantic-failure/generate",
        json={"prompt": "Explain checkpoint integrity", "mode": "no_llm"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["evaluation"]["semantic_evaluation"]["status"] == "error"
    assert result["evaluation"]["decision"] == "needs_human_review"
    assert result["approval_status"] == "pending"
    assert result["is_safe_checkpoint"] is False

    # The evaluator error is visible in the audit trail; it does not count as a pass.
    events = client.get("/api/tasks/semantic-failure/events").json()["events"]
    evaluation_event = next(e for e in events if e["event_type"] == "CONTENT_EVALUATED")
    assert evaluation_event["details"]["semantic_status"] == "error"


def test_corrupted_safe_checkpoint_is_not_restored(tmp_path):
    client, store = make_client(tmp_path)
    create_task(client, "corruption-flow")
    draft = client.post(
        "/api/tasks/corruption-flow/generate",
        json={"prompt": "Explain checkpoint integrity", "mode": "no_llm"},
    ).json()
    assert review(client, draft["checkpoint_id"]).status_code == 200

    store.tamper_snapshot_for_test(draft["checkpoint_id"], json.dumps({"draft": "tampered"}))
    detail = client.get(f"/api/checkpoints/{draft['checkpoint_id']}").json()
    assert detail["integrity_verified"] is False
    assert detail["state"] is None

    rollback = client.post("/api/tasks/corruption-flow/rollback")
    assert rollback.status_code == 409
    assert "integrity verification failed" in rollback.json()["detail"].lower()


def test_rejecting_candidate_keeps_it_unsafe_and_rollback_uses_baseline(tmp_path):
    client, _ = make_client(tmp_path)
    create_task(client, "human-rejection")
    draft = client.post(
        "/api/tasks/human-rejection/generate",
        json={"prompt": "Explain checkpoint integrity", "mode": "no_llm"},
    ).json()
    rejected = review(client, draft["checkpoint_id"], "reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["is_safe"] is False
    assert rejected.json()["approval_status"] == "rejected"

    restored = client.post("/api/tasks/human-rejection/rollback").json()
    assert restored["verification_passed"] is True
    assert restored["state"]["version"] == 0
    assert restored["checkpoint_id"] != draft["checkpoint_id"]
