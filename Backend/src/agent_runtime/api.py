"""FastAPI interface for the shared checkpoint/content workflow."""
from __future__ import annotations
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Optional local .env file; environment variables supplied by the shell take precedence.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass

from .checkpoint import CheckpointManager, CheckpointNotFoundError, SnapshotIntegrityError
from .model_provider import OllamaUnavailableError
from .quality_policy import QualityPolicyEvaluator
from .store import SQLiteCheckpointStore
from .mysql_store import MySQLCheckpointStore
from .artifact_store import GitArtifactStore
from .retrieval_store import LocalEvidenceIndex
from .workflow import ContentWorkflow


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    mode: Literal["no_llm", "ollama"] = "no_llm"
    model_name: str | None = None
    continue_from_previous: bool = True
    baseline_checkpoint_id: str | None = Field(default=None, max_length=80)


class TaskCreateRequest(BaseModel):
    task_id: str | None = None
    title: str = Field(default="Untitled task", min_length=1, max_length=200)


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=2000)


class EvidenceRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    reference_id: str | None = Field(default=None, max_length=300)


class DiagnosticRequest(BaseModel):
    model_name: str | None = Field(default=None, max_length=160)
    timeout_seconds: int = Field(default=180, ge=30, le=300)


_DIAGNOSTIC_LOCK = threading.Lock()
DEMO_TASK_ID = "draftguard-demo-responsible-checkpoints-v1"
DEMO_TITLE = "Demo: Responsible checkpoint recovery"
DEMO_EVIDENCE_ID = "draftguard-demo-evidence-v1"
DEMO_EVIDENCE = (
    "DraftGuard stores checkpoint artifacts in Git and uses MySQL to maintain structured metadata and audit records. "
    "Each checkpoint records its Git commit hash in the metadata. During recovery, DraftGuard validates the recorded "
    "Git commit and the artifact's SHA-256 digest before restoring the approved content."
)
DEMO_PROMPT = (
    "Explain how DraftGuard stores, approves, and recovers content checkpoints. Describe the role of Git in storing "
    "versioned artifacts and the role of MySQL in tracking checkpoint metadata and audit records. Explain how Git "
    "commit hashes and SHA-256 digests help verify checkpoint integrity during recovery. Use the supplied evidence "
    "for factual claims and identify any claims that the evidence does not establish."
)


def create_app(database_path: str | Path | None = None, workflow: ContentWorkflow | None = None) -> FastAPI:
    """App factory makes storage/workflow injectable for tests and deployments."""
    if workflow is None:
        storage_mode = os.environ.get("AGENT_RUNTIME_STORAGE", "mysql").strip().lower()
        if storage_mode == "mysql":
            store = MySQLCheckpointStore()
            data_dir = Path(os.environ.get("AGENT_RUNTIME_DATA_DIR", "runtime_data"))
            artifact_store = GitArtifactStore(os.environ.get("AGENT_ARTIFACT_GIT_DIR", str(data_dir / "artifact_repository")))
            retrieval = LocalEvidenceIndex(os.environ.get("AGENT_EVIDENCE_DIR", str(data_dir / "evidence_index")))
            workflow = ContentWorkflow(CheckpointManager(store, artifact_store=artifact_store), QualityPolicyEvaluator(), retrieval=retrieval)
        elif storage_mode == "sqlite":
            # Explicit development/test mode only. It does not enable the required MySQL/Git/FAISS stack.
            db_path = database_path or os.environ.get("AGENT_RUNTIME_DB", "runtime_data/runtime.sqlite3")
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            store = SQLiteCheckpointStore(db_path)
            workflow = ContentWorkflow(CheckpointManager(store), QualityPolicyEvaluator())
        else:
            raise RuntimeError("AGENT_RUNTIME_STORAGE must be mysql or sqlite; use mysql for the required deployment stack.")

    app = FastAPI(
        title="Checkpoint and Rollback Agent API",
        description="Local-first content generation, optional evidence-grounded factuality checks, human-governed checkpointing, and recovery.",
        version="0.7.0",
    )
    # The frontend is a separate project folder and runs on its own local dev server.
    # Restrict CORS to the expected local development origins; do not expose this API publicly.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5500",
            "http://localhost:5500",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    app.state.workflow = workflow

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "checkpoint-rollback-agent"}

    @app.get("/api/health/integrations")
    def integration_health() -> dict[str, Any]:
        """Probe configured storage adapters with read-only health checks."""
        store = workflow.checkpoints.store
        if isinstance(store, MySQLCheckpointStore):
            try:
                metadata = {"status": "connected", **store.health_check()}
            except Exception as exc:
                metadata = {"status": "failed", "adapter": "mysql",
                            "error": type(exc).__name__}
        elif isinstance(store, SQLiteCheckpointStore):
            metadata = {"status": "not_configured", "adapter": "sqlite",
                        "message": "This runtime is using SQLite; MySQL is not configured."}
        else:
            metadata = {"status": "not_configured", "adapter": type(store).__name__}

        retrieval = workflow.retrieval
        if retrieval is None:
            evidence = {"status": "not_configured", "adapter": None}
        else:
            checker = getattr(retrieval, "health_check", None)
            if not callable(checker):
                evidence = {"status": "not_configured", "adapter": type(retrieval).__name__}
            else:
                try:
                    evidence = {"status": "connected", **checker()}
                except Exception as exc:
                    evidence = {"status": "failed", "adapter": type(retrieval).__name__,
                                "error": type(exc).__name__}

        artifact_store = workflow.checkpoints.artifact_store
        if artifact_store is None:
            artifacts = {"status": "not_configured", "adapter": None}
        else:
            checker = getattr(artifact_store, "health_check", None)
            if not callable(checker):
                artifacts = {"status": "not_configured", "adapter": type(artifact_store).__name__}
            else:
                try:
                    artifacts = {"status": "connected", **checker()}
                except Exception as exc:
                    artifacts = {"status": "failed", "adapter": type(artifact_store).__name__,
                                 "error": type(exc).__name__}
        return {"metadata": metadata, "evidence": evidence, "artifacts": artifacts}

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        try:
            names = workflow.available_models()
            return {"available": True, "models": names, "provider": "ollama"}
        except OllamaUnavailableError as exc:
            # Health and no-LLM mode remain usable when Ollama is offline.
            return {"available": False, "models": [], "provider": "ollama", "message": str(exc)}

    @app.post("/api/tasks", status_code=201)
    def create_task(request: TaskCreateRequest) -> dict[str, Any]:
        task_id = (request.task_id or str(uuid.uuid4())).strip()
        if not task_id:
            raise HTTPException(status_code=422, detail="task_id must not be blank")
        store = workflow.checkpoints.store
        created_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        title = request.title.strip()
        if not store.create_task(task_id, title, created_at):
            raise HTTPException(status_code=409, detail="task_id already exists")
        return {"task_id": task_id, "title": title, "created": True}

    def ensure_task(task_id: str) -> dict[str, Any]:
        store = workflow.checkpoints.store
        task = store.get_task(task_id)
        if task is not None:
            return {"task_id": task["task_id"], "title": task["title"], "created": False}
        # Backward compatibility for checkpoint records created before task metadata
        # was persisted. Recreate a minimal task record when historical checkpoints exist.
        checkpoints = store.list_checkpoints(task_id)
        if not checkpoints:
            raise HTTPException(status_code=404, detail="Task not found")
        created_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        store.create_task(task_id, task_id, created_at)
        task = store.get_task(task_id)
        return {"task_id": task["task_id"], "title": task["title"], "created": False}

    @app.post("/api/tasks/{task_id}/evidence", status_code=201)
    def add_evidence(task_id: str, request: EvidenceRequest) -> dict[str, Any]:
        ensure_task(task_id)
        if workflow.retrieval is None:
            raise HTTPException(status_code=503, detail="Evidence retrieval is not configured")
        evidence_id = request.reference_id or f"evidence:{uuid.uuid4()}"
        try:
            saved_id = workflow.retrieval.add(
                request.text.strip(), task_id=task_id, kind="evidence",
                reference_id=evidence_id,
            )
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=500, detail=f"Could not index evidence: {type(exc).__name__}") from exc
        if not saved_id:
            raise HTTPException(status_code=422, detail="Evidence text must not be blank")
        return {"task_id": task_id, "evidence_id": saved_id, "kind": "evidence", "indexed": True}

    @app.post("/api/demo/load")
    def load_demo() -> dict[str, Any]:
        """Idempotently prepare the named demo task and index its source; no generation/review/rollback."""
        if workflow.retrieval is None:
            raise HTTPException(status_code=503, detail="Demo evidence cannot be indexed: retrieval is not configured")
        store = workflow.checkpoints.store
        existing = store.get_task(DEMO_TASK_ID)
        if existing and existing.get("title") != DEMO_TITLE:
            raise HTTPException(status_code=409, detail="The reserved demo workspace ID is already used by another task")
        created = False
        if not existing:
            created_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            created = bool(store.create_task(DEMO_TASK_ID, DEMO_TITLE, created_at))
            existing = store.get_task(DEMO_TASK_ID)
            if not existing or existing.get("title") != DEMO_TITLE:
                raise HTTPException(status_code=409, detail="Could not safely reserve the demo workspace")
        try:
            evidence_id = workflow.retrieval.add(DEMO_EVIDENCE, task_id=DEMO_TASK_ID,
                                                 kind="evidence", reference_id=DEMO_EVIDENCE_ID)
        except Exception as exc:
            status = 409 if isinstance(exc, ValueError) else 503
            raise HTTPException(status_code=status,
                                detail=f"Demo evidence indexing failed ({type(exc).__name__}): {str(exc)[:250]}") from exc
        prepared_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        store.add_audit_event(DEMO_TASK_ID, "DEMO_WORKSPACE_PREPARED", None,
                              {"evidence_id": evidence_id, "indexed": True,
                               "generation_performed": False, "approval_performed": False,
                               "rollback_performed": False}, prepared_at)
        return {"task": {"task_id": DEMO_TASK_ID, "title": DEMO_TITLE}, "created": created,
                "prompt": DEMO_PROMPT, "evidence": DEMO_EVIDENCE,
                "evidence_id": evidence_id, "indexed": bool(evidence_id),
                "actions_performed": ["workspace_created_or_reused", "evidence_indexed"],
                "generation_performed": False, "approval_performed": False, "rollback_performed": False}

    @app.post("/api/tasks/{task_id}/generate")
    def generate(task_id: str, request: GenerateRequest) -> dict[str, Any]:
        ensure_task(task_id)
        try:
            return workflow.generate(task_id, request.prompt, mode=request.mode, model_name=request.model_name,
                                     continue_from_previous=request.continue_from_previous,
                                     baseline_checkpoint_id=request.baseline_checkpoint_id)
        except OllamaUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        task = ensure_task(task_id)
        events = workflow.checkpoints.store.get_audit_events(task_id)
        checkpoint = workflow.checkpoints.latest_verified_safe_checkpoint(task_id)
        state = None
        if checkpoint is not None:
            try:
                from .checkpoint import _to_checkpoint
                import json, hashlib
                snapshot = checkpoint["snapshot_json"]
                if hashlib.sha256(snapshot.encode("utf-8")).hexdigest() == checkpoint["snapshot_sha256"]:
                    state = json.loads(snapshot)
            except Exception:
                state = None
        return {"task": task, "latest_safe_state": state, "event_count": len(events)}

    @app.get("/api/tasks/{task_id}/checkpoints")
    def checkpoints(task_id: str) -> dict[str, Any]:
        ensure_task(task_id)
        # The store's public audit API is intentionally used for history; raw
        # checkpoint enumeration is added as a store method to keep SQL encapsulated.
        items = workflow.checkpoints.store.list_checkpoints(task_id)
        return {"task_id": task_id, "checkpoints": items}

    @app.get("/api/checkpoints/{checkpoint_id}")
    def checkpoint_detail(checkpoint_id: str) -> dict[str, Any]:
        record = workflow.checkpoints.store.get_checkpoint(checkpoint_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Checkpoint not found")
        return record

    @app.post("/api/checkpoints/{checkpoint_id}/review")
    def review_checkpoint(checkpoint_id: str, request: ReviewRequest) -> dict[str, Any]:
        record = workflow.checkpoints.store.get_checkpoint(checkpoint_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Checkpoint not found")
        state = record.get("state")
        if not isinstance(state, dict) or state.get("mode") == "system" or int(state.get("version", 0)) == 0:
            raise HTTPException(status_code=409, detail="The initial baseline is not a reviewable draft")
        if not request.reviewer.strip() or len(request.reason.strip()) < 3:
            raise HTTPException(status_code=422, detail="Reviewer and reason must not be blank")
        current_status = state.get("approval_status", "pending")
        if current_status in ("approved", "rejected"):
            raise HTTPException(status_code=409, detail=f"Checkpoint already reviewed: {current_status}")
        if request.decision == "approve" and state.get("decision") == "reject":
            raise HTTPException(status_code=409, detail="Automated policy rejection cannot be approved through the normal review route")
        approved = request.decision == "approve"
        reviewed_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        try:
            result = workflow.checkpoints.review_checkpoint(
                checkpoint_id, approved=approved, reviewer=request.reviewer.strip(),
                reason=request.reason.strip(), reviewed_at=reviewed_at,
            )
        except SnapshotIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Could not commit reviewed artifact: {type(exc).__name__}") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Checkpoint not found")
        return result

    @app.post("/api/checkpoints/{checkpoint_id}/rollback")
    def rollback_checkpoint(checkpoint_id: str) -> dict[str, Any]:
        record = workflow.checkpoints.store.get_checkpoint(checkpoint_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Checkpoint not found")
        task_id = record["task_id"]
        try:
            restored = workflow.rollback(task_id, requested_checkpoint_id=checkpoint_id)
        except CheckpointNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SnapshotIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return restored

    @app.post("/api/tasks/{task_id}/rollback")
    def rollback_task(task_id: str) -> dict[str, Any]:
        ensure_task(task_id)
        try:
            return workflow.rollback(task_id)
        except CheckpointNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SnapshotIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/evaluations/run")
    def run_evaluation(request: dict[str, Any] | None = None) -> dict[str, Any]:
        # Reuse the deterministic evaluation engine from Step 2, isolated in a
        # separate database file so experiments cannot mutate live task records.
        from .evaluation import run_experiment
        repetitions = 3
        if request and "repetitions" in request:
            try:
                repetitions = int(request["repetitions"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="repetitions must be an integer") from exc
        if repetitions < 1 or repetitions > 100:
            raise HTTPException(status_code=422, detail="repetitions must be between 1 and 100")
        path = Path(os.environ.get("AGENT_EVALUATION_REPORT_PATH", "runtime_data/evaluation_api.json"))
        try:
            data_dir = Path(os.environ.get("AGENT_RUNTIME_DATA_DIR", "runtime_data"))
            factuality_enabled = os.environ.get("AGENT_FACTUALITY_EVALUATION", "0").strip().lower() in {"1", "true", "yes", "on"}
            factuality_model = os.environ.get("AGENT_FACTUALITY_MODEL")
            report = run_experiment(path, repetitions=repetitions,
                metadata_store=workflow.checkpoints.store,
                artifact_root=data_dir / "evaluation_artifacts",
                evaluator=workflow.evaluator, provider=workflow.provider,
                retrieval=workflow.retrieval, factuality_enabled=factuality_enabled,
                factuality_model=factuality_model)
            workflow.checkpoints.store.add_audit_event("system:evaluations", "EVALUATION_RUN_COMPLETED", None,
                {"experiment_id": report["experiment_id"], "status": report["status"],
                 "trial_count": report["total_trials"], "metrics": report["metrics"]}, report["finished_at"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return report

    @app.get("/api/evaluations/runs")
    def evaluation_runs(limit: int = 50) -> dict[str, Any]:
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
        getter = getattr(workflow.checkpoints.store, "list_evaluation_runs", None)
        if not callable(getter):
            raise HTTPException(status_code=503, detail="Configured SQL store does not support evaluation history")
        return {"runs": getter(limit)}

    @app.get("/api/evaluations/runs/{experiment_id}")
    def evaluation_run(experiment_id: str) -> dict[str, Any]:
        getter = getattr(workflow.checkpoints.store, "get_evaluation_run", None)
        report = getter(experiment_id) if callable(getter) else None
        if report is None:
            raise HTTPException(status_code=404, detail="Evaluation run not found")
        return report

    @app.get("/api/evaluations/runs/{experiment_id}/report")
    def evaluation_report(experiment_id: str) -> dict[str, Any]:
        getter = getattr(workflow.checkpoints.store, "get_evaluation_run", None)
        report = getter(experiment_id) if callable(getter) else None
        if report is None:
            raise HTTPException(status_code=404, detail="Evaluation report not found")
        return {"report": report, "format": "application/json"}

    @app.post("/api/diagnostics/tests")
    def run_tests(request: DiagnosticRequest | None = None) -> dict[str, Any]:
        timeout_seconds = request.timeout_seconds if request else 300
        if not _DIAGNOSTIC_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A diagnostic run is already in progress")
        try:
            from .diagnostics import run_pytest_suite
            backend_dir = Path(__file__).resolve().parents[2]
            report = run_pytest_suite(backend_dir, timeout_seconds=timeout_seconds)
            workflow.checkpoints.store.save_evaluation_run(report)
            workflow.checkpoints.store.add_audit_event("system:diagnostics", "PYTEST_RUN_COMPLETED", None,
                {"run_id": report["experiment_id"], "status": report["status"], "counts": report["counts"]}, report["finished_at"])
            return report
        finally:
            _DIAGNOSTIC_LOCK.release()

    @app.post("/api/diagnostics/factuality")
    def run_factuality_diagnostic(request: DiagnosticRequest | None = None) -> dict[str, Any]:
        model = (request.model_name if request and request.model_name else
                 os.environ.get("AGENT_FACTUALITY_MODEL", "qwen3:4b"))
        timeout_seconds = request.timeout_seconds if request else 180
        if not _DIAGNOSTIC_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A diagnostic run is already in progress")
        try:
            try:
                available = workflow.available_models()
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"Ollama model discovery failed: {type(exc).__name__}") from exc
            if model not in available:
                raise HTTPException(status_code=422, detail=f"Requested factuality model is not installed: {model}")
            from .diagnostics import run_factuality_benchmark
            backend_dir = Path(__file__).resolve().parents[2]
            report = run_factuality_benchmark(backend_dir, model, timeout_seconds=timeout_seconds)
            workflow.checkpoints.store.save_evaluation_run(report)
            workflow.checkpoints.store.add_audit_event("system:diagnostics", "FACTUALITY_BENCHMARK_COMPLETED", None,
                {"run_id": report["experiment_id"], "status": report["status"], "model_name": model}, report["finished_at"])
            return report
        finally:
            _DIAGNOSTIC_LOCK.release()

    @app.get("/api/tasks/{task_id}/events")
    def task_events(task_id: str) -> dict[str, Any]:
        ensure_task(task_id)
        return {"task_id": task_id, "events": workflow.checkpoints.store.get_audit_events(task_id)}

    return app


app = create_app()
