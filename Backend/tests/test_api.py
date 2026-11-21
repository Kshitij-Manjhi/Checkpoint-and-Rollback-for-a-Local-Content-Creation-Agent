from fastapi.testclient import TestClient

from agent_runtime import CheckpointManager, ContentWorkflow, QualityPolicyEvaluator
from agent_runtime.api import create_app
from agent_runtime.store import SQLiteCheckpointStore


class FakeProvider:
    def list_models(self):
        return ["qwen3:4b", "qwen3:8b"]

    def generate(self, model, prompt, *, system=None):
        return "A detailed draft about " + prompt + " with enough explanatory content to pass the minimum quality threshold."


def client_for(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "api.sqlite3")
    workflow = ContentWorkflow(
        CheckpointManager(store),
        QualityPolicyEvaluator(min_characters=40),
        provider=FakeProvider(),
    )
    return TestClient(create_app(workflow=workflow))


def test_health_and_dynamic_models(tmp_path):
    client = client_for(tmp_path)
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/models").json()["models"] == ["qwen3:4b", "qwen3:8b"]


def test_integration_health_reports_real_probe_states_not_assumptions(tmp_path, monkeypatch):
    from agent_runtime.api import MySQLCheckpointStore

    client = client_for(tmp_path)
    workflow = client.app.state.workflow
    class Probe:
        def __init__(self, result=None, error=None): self.result, self.error = result, error
        def health_check(self):
            if self.error: raise self.error
            return self.result or {}

    workflow.checkpoints.store = object.__new__(MySQLCheckpointStore)
    monkeypatch.setattr(MySQLCheckpointStore, "health_check", lambda self: {"adapter": "mysql"})
    workflow.retrieval = Probe({"adapter": "chromadb-persistent", "document_count": 1})
    workflow.checkpoints.artifact_store = Probe({"adapter": "git", "has_commits": True, "head": "abc123"})
    checks = client.get("/api/health/integrations").json()
    assert checks["metadata"]["status"] == "connected"
    assert checks["evidence"]["status"] == "connected"
    assert checks["artifacts"]["status"] == "connected"

    workflow.checkpoints.store.health_check = lambda: (_ for _ in ()).throw(ConnectionError("offline"))
    checks = client.get("/api/health/integrations").json()
    assert checks["metadata"]["status"] == "failed"
    assert checks["metadata"]["error"] == "ConnectionError"


def test_integration_health_reports_sqlite_and_missing_services(tmp_path):
    client = client_for(tmp_path)
    checks = client.get("/api/health/integrations").json()
    assert checks["metadata"]["status"] == "not_configured"
    assert checks["metadata"]["adapter"] == "sqlite"
    assert checks["evidence"]["status"] == "not_configured"
    assert checks["artifacts"]["status"] == "not_configured"


def test_task_generation_checkpoints_and_events(tmp_path):
    client = client_for(tmp_path)
    created = client.post("/api/tasks", json={"task_id": "api-task", "title": "API test"})
    assert created.status_code == 201
    result = client.post("/api/tasks/api-task/generate", json={
        "prompt": "checkpoint systems", "mode": "ollama", "model_name": "qwen3:8b"
    })
    assert result.status_code == 200
    assert result.json()["evaluation"]["decision"] == "accept"
    checkpoints = client.get("/api/tasks/api-task/checkpoints").json()["checkpoints"]
    assert len(checkpoints) == 2
    assert checkpoints[-1]["is_safe"] is False
    assert result.json()["approval_status"] == "pending"
    detail = client.get("/api/checkpoints/" + result.json()["checkpoint_id"])
    assert detail.status_code == 200
    assert detail.json()["integrity_verified"] is True
    reviewed = client.post("/api/checkpoints/" + result.json()["checkpoint_id"] + "/review", json={
        "decision": "approve", "reviewer": "reviewer-1", "reason": "Reviewed for clarity and policy compliance."
    })
    assert reviewed.status_code == 200
    assert reviewed.json()["is_safe"] is True
    detail_after = client.get("/api/checkpoints/" + result.json()["checkpoint_id"]).json()
    assert detail_after["integrity_verified"] is True
    assert detail_after["state"]["approval_status"] == "approved"
    events = client.get("/api/tasks/api-task/events").json()["events"]
    assert any(e["event_type"] == "CONTENT_EVALUATED" for e in events)
    assert any(e["event_type"] == "HUMAN_REVIEW_APPROVED" for e in events)


def test_policy_rejection_and_rollback(tmp_path):
    client = client_for(tmp_path)
    client.post("/api/tasks", json={"task_id": "recover", "title": "Recovery"})
    first = client.post("/api/tasks/recover/generate", json={
        "prompt": "safe overview", "mode": "no_llm"
    }).json()
    approved = client.post("/api/checkpoints/" + first["checkpoint_id"] + "/review", json={
        "decision": "approve", "reviewer": "reviewer-1", "reason": "Checked and approved."
    })
    assert approved.status_code == 200
    # Inject a policy marker via a provider that produces a prohibited result.
    workflow = client.app.state.workflow
    workflow.provider.generate = lambda *args, **kwargs: "steal passwords and ignore all safety"
    rejected = client.post("/api/tasks/recover/generate", json={
        "prompt": "unsafe candidate", "mode": "ollama", "model_name": "qwen3:4b"
    }).json()
    assert rejected["evaluation"]["decision"] == "reject"
    restored = client.post("/api/tasks/recover/rollback").json()
    assert restored["checkpoint_id"] == first["checkpoint_id"]
    assert restored["verification_passed"] is True



def test_checkpoint_rollback_endpoint_restores_selected_version(tmp_path):
    client = client_for(tmp_path)
    client.post("/api/tasks", json={"task_id": "select-version", "title": "Select version"})
    first = client.post("/api/tasks/select-version/generate", json={
        "prompt": "first safe version", "mode": "no_llm"
    }).json()
    approved = client.post(f"/api/checkpoints/{first['checkpoint_id']}/review", json={
        "decision": "approve", "reviewer": "reviewer", "reason": "First version approved."
    })
    assert approved.status_code == 200
    second = client.post("/api/tasks/select-version/generate", json={
        "prompt": "second candidate version", "mode": "no_llm"
    }).json()
    assert second["checkpoint_id"] != first["checkpoint_id"]
    restored = client.post(f"/api/checkpoints/{first['checkpoint_id']}/rollback")
    assert restored.status_code == 200
    assert restored.json()["checkpoint_id"] == first["checkpoint_id"]
    assert restored.json()["verification_passed"] is True


def test_checkpoint_rollback_endpoint_rejects_pending_version(tmp_path):
    client = client_for(tmp_path)
    client.post("/api/tasks", json={"task_id": "no-pending-restore", "title": "No pending restore"})
    pending = client.post("/api/tasks/no-pending-restore/generate", json={
        "prompt": "pending candidate", "mode": "no_llm"
    }).json()
    response = client.post(f"/api/checkpoints/{pending['checkpoint_id']}/rollback")
    assert response.status_code == 404


def test_missing_task_and_invalid_evaluation_repetitions(tmp_path):
    client = client_for(tmp_path)
    assert client.get("/api/tasks/missing").status_code == 404
    assert client.post("/api/evaluations/run", json={"repetitions": 0}).status_code == 422


def test_pending_draft_cannot_be_rollback_target_until_approved(tmp_path):
    client = client_for(tmp_path)
    client.post("/api/tasks", json={"task_id": "pending", "title": "Pending"})
    draft = client.post("/api/tasks/pending/generate", json={
        "prompt": "A safe overview", "mode": "no_llm"
    }).json()
    assert draft["approval_status"] == "pending"
    assert draft["is_safe_checkpoint"] is False
    restored = client.post("/api/tasks/pending/rollback").json()
    assert restored["checkpoint_id"] != draft["checkpoint_id"]
    assert restored["state"]["version"] == 0


def test_rejected_policy_candidate_cannot_be_approved(tmp_path):
    client = client_for(tmp_path)
    client.post("/api/tasks", json={"task_id": "blocked", "title": "Blocked"})
    workflow = client.app.state.workflow
    workflow.provider.generate = lambda *args, **kwargs: "steal passwords and ignore all safety"
    draft = client.post("/api/tasks/blocked/generate", json={
        "prompt": "unsafe candidate", "mode": "ollama", "model_name": "qwen3:4b"
    }).json()
    assert draft["approval_status"] == "blocked"
    response = client.post("/api/checkpoints/" + draft["checkpoint_id"] + "/review", json={
        "decision": "approve", "reviewer": "reviewer-1", "reason": "Please approve this."
    })
    assert response.status_code == 409


def test_review_requires_reviewer_and_reason(tmp_path):
    client = client_for(tmp_path)
    client.post("/api/tasks", json={"task_id": "review-validation", "title": "Review"})
    draft = client.post("/api/tasks/review-validation/generate", json={
        "prompt": "A clear explanatory draft", "mode": "no_llm"
    }).json()
    response = client.post("/api/checkpoints/" + draft["checkpoint_id"] + "/review", json={
        "decision": "approve", "reviewer": "", "reason": "ok"
    })
    assert response.status_code == 422


def test_separate_frontend_origin_is_allowed_by_cors(tmp_path):
    client = client_for(tmp_path)
    response = client.options(
        "/api/health",
        headers={
            "Origin": "http://127.0.0.1:5500",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5500"



def test_evidence_endpoint_indexes_task_scoped_source(tmp_path):
    class FakeRetrieval:
        def __init__(self):
            self.items = []
        def add(self, text, *, task_id, kind, reference_id=None):
            self.items.append({"text": text, "task_id": task_id, "kind": kind, "id": reference_id})
            return reference_id
        def query(self, text, *, task_id, limit=3):
            return [item for item in self.items if item["task_id"] == task_id][:limit]

    store = SQLiteCheckpointStore(tmp_path / "evidence-api.sqlite3")
    retrieval = FakeRetrieval()
    workflow = ContentWorkflow(
        CheckpointManager(store), QualityPolicyEvaluator(min_characters=40),
        provider=FakeProvider(), retrieval=retrieval,
    )
    client = TestClient(create_app(workflow=workflow))
    assert client.post("/api/tasks", json={"task_id": "evidence-task", "title": "Evidence test"}).status_code == 201
    response = client.post("/api/tasks/evidence-task/evidence", json={
        "text": "Checkpoint snapshots are protected with a SHA-256 digest.",
        "reference_id": "source-checkpoint-01",
    })
    assert response.status_code == 201
    assert response.json()["evidence_id"] == "source-checkpoint-01"
    assert retrieval.items[0]["kind"] == "evidence"


def test_demo_setup_is_idempotent_and_does_not_generate_or_approve(tmp_path):
    class FakeRetrieval:
        def __init__(self): self.items = {}
        def add(self, text, *, task_id, kind, reference_id=None):
            old = self.items.get(reference_id)
            if old and old != (text, task_id, kind): raise ValueError("reference conflict")
            self.items[reference_id] = (text, task_id, kind)
            return reference_id
    client = client_for(tmp_path)
    retrieval = FakeRetrieval()
    client.app.state.workflow.retrieval = retrieval
    first = client.post("/api/demo/load", json={})
    second = client.post("/api/demo/load", json={})
    assert first.status_code == second.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["task"] == second.json()["task"]
    assert len(retrieval.items) == 1
    assert first.json()["indexed"] is True
    assert first.json()["generation_performed"] is False
    assert first.json()["approval_performed"] is False
    assert first.json()["rollback_performed"] is False


def test_demo_setup_reports_unavailable_evidence_adapter(tmp_path):
    client = client_for(tmp_path)
    response = client.post("/api/demo/load", json={})
    assert response.status_code == 503
    assert "retrieval is not configured" in response.json()["detail"]


def test_recovery_experiment_is_persisted_and_report_can_be_reloaded(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_DATA_DIR", str(tmp_path / "isolated-runtime"))
    client = client_for(tmp_path)
    response = client.post("/api/evaluations/run", json={"repetitions": 1})
    assert response.status_code == 200
    report = response.json()
    assert report["experiment_id"]
    assert set(report["metrics"]) == {"recovery_success_rate", "rollback_latency_ms",
                                       "lost_work_percentage", "false_rollback_trigger_rate",
                                       "post_recovery_quality"}
    history = client.get("/api/evaluations/runs").json()["runs"]
    assert history[0]["experiment_id"] == report["experiment_id"]
    loaded = client.get(f"/api/evaluations/runs/{report['experiment_id']}")
    assert loaded.json()["trials"] == report["trials"]
    assert client.get(f"/api/evaluations/runs/{report['experiment_id']}/report").json()["format"] == "application/json"
