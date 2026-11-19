"""Step 9 persistence and reliability regression tests."""
from __future__ import annotations
import json

from fastapi.testclient import TestClient

from agent_runtime.api import create_app
from agent_runtime.checkpoint import CheckpointManager
from agent_runtime.quality_policy import QualityPolicyEvaluator
from agent_runtime.store import SQLiteCheckpointStore
from agent_runtime.workflow import ContentWorkflow

DRAFT_A = ("A complete and useful draft about reliable checkpoint storage. "
           "It explains snapshot verification and safe recovery behavior.")
DRAFT_B = ("A second complete draft with changed content. "
           "It exists to test recovery from a damaged latest checkpoint.")

class Provider:
    def __init__(self):
        self.responses = [DRAFT_A, DRAFT_B, DRAFT_A]
    def list_models(self):
        return ["qwen3:4b"]
    def generate(self, model, prompt, *, system=None):
        return self.responses.pop(0) if self.responses else DRAFT_A

def make_client(db_path, provider=None):
    store = SQLiteCheckpointStore(db_path)
    workflow = ContentWorkflow(CheckpointManager(store), QualityPolicyEvaluator(min_characters=40), provider=provider or Provider())
    return TestClient(create_app(workflow=workflow)), store

def review(client, checkpoint_id):
    return client.post(f"/api/checkpoints/{checkpoint_id}/review", json={
        "decision": "approve", "reviewer": "step9-reviewer",
        "reason": "Reviewed for persistence and recovery testing."
    })

def test_task_metadata_and_checkpoint_survive_api_restart(tmp_path):
    db_path = tmp_path / "restart.sqlite3"
    client1, _ = make_client(db_path)
    created = client1.post("/api/tasks", json={"task_id": "persist-me", "title": "Persistent workspace"})
    assert created.status_code == 201
    draft = client1.post("/api/tasks/persist-me/generate", json={"prompt": "Write draft A", "mode": "ollama", "model_name": "qwen3:4b"}).json()
    assert review(client1, draft["checkpoint_id"]).status_code == 200

    # A fresh app instance represents a backend restart over the same SQLite file.
    client2, _ = make_client(db_path)
    task = client2.get("/api/tasks/persist-me")
    assert task.status_code == 200
    assert task.json()["task"]["title"] == "Persistent workspace"
    assert task.json()["latest_safe_state"]["draft"] == DRAFT_A
    assert len(client2.get("/api/tasks/persist-me/checkpoints").json()["checkpoints"]) == 2
    assert client2.post("/api/tasks", json={"task_id": "persist-me", "title": "Duplicate"}).status_code == 409

def test_rollback_skips_corrupt_newest_safe_checkpoint(tmp_path):
    client, store = make_client(tmp_path / "fallback.sqlite3")
    client.post("/api/tasks", json={"task_id": "fallback", "title": "Fallback"})
    first = client.post("/api/tasks/fallback/generate", json={"prompt": "first draft", "mode": "ollama", "model_name": "qwen3:4b"}).json()
    assert review(client, first["checkpoint_id"]).status_code == 200
    second = client.post("/api/tasks/fallback/generate", json={"prompt": "second draft", "mode": "ollama", "model_name": "qwen3:4b"}).json()
    assert review(client, second["checkpoint_id"]).status_code == 200

    store.tamper_snapshot_for_test(second["checkpoint_id"], json.dumps({"draft": "tampered"}))
    restored = client.post("/api/tasks/fallback/rollback")
    assert restored.status_code == 200, restored.text
    assert restored.json()["checkpoint_id"] == first["checkpoint_id"]
    assert restored.json()["state"]["draft"] == DRAFT_A
    events = client.get("/api/tasks/fallback/events").json()["events"]
    assert any(e["event_type"] == "ROLLBACK_CANDIDATE_REJECTED" and e["checkpoint_id"] == second["checkpoint_id"] for e in events)

def test_empty_snapshot_is_skipped_and_older_checkpoint_restored(tmp_path):
    client, store = make_client(tmp_path / "missing-snapshot.sqlite3")
    client.post("/api/tasks", json={"task_id": "missing-snapshot", "title": "Missing snapshot"})
    first = client.post("/api/tasks/missing-snapshot/generate", json={"prompt": "first draft", "mode": "ollama", "model_name": "qwen3:4b"}).json()
    assert review(client, first["checkpoint_id"]).status_code == 200
    second = client.post("/api/tasks/missing-snapshot/generate", json={"prompt": "second draft", "mode": "ollama", "model_name": "qwen3:4b"}).json()
    assert review(client, second["checkpoint_id"]).status_code == 200
    store.tamper_snapshot_for_test(second["checkpoint_id"], "")

    restored = client.post("/api/tasks/missing-snapshot/rollback")
    assert restored.status_code == 200, restored.text
    assert restored.json()["checkpoint_id"] == first["checkpoint_id"]
    assert restored.json()["state"]["draft"] == DRAFT_A

def test_all_safe_snapshots_corrupt_fails_closed(tmp_path):
    client, store = make_client(tmp_path / "all-corrupt.sqlite3")
    client.post("/api/tasks", json={"task_id": "all-corrupt", "title": "All corrupt"})
    draft = client.post("/api/tasks/all-corrupt/generate", json={"prompt": "draft", "mode": "ollama", "model_name": "qwen3:4b"}).json()
    assert review(client, draft["checkpoint_id"]).status_code == 200
    # Corrupt both the approved candidate and its initial safe baseline.
    checkpoints = client.get("/api/tasks/all-corrupt/checkpoints").json()["checkpoints"]
    for item in checkpoints:
        if item["is_safe"]:
            store.tamper_snapshot_for_test(item["checkpoint_id"], "broken")
    response = client.post("/api/tasks/all-corrupt/rollback")
    assert response.status_code == 409
    assert "no integrity-verified safe checkpoint" in response.json()["detail"].lower()
