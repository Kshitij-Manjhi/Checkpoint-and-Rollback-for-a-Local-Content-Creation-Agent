"""FastAPI interface for the shared checkpoint/content workflow."""
from __future__ import annotations
import os
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .checkpoint import CheckpointManager, CheckpointNotFoundError, SnapshotIntegrityError
from .model_provider import OllamaUnavailableError
from .quality_policy import QualityPolicyEvaluator
from .store import SQLiteCheckpointStore
from .workflow import ContentWorkflow


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    mode: Literal["no_llm", "ollama"] = "no_llm"
    model_name: str | None = None


class TaskCreateRequest(BaseModel):
    task_id: str | None = None
    title: str = Field(default="Untitled task", min_length=1, max_length=200)


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=2000)


def create_app(database_path: str | Path | None = None, workflow: ContentWorkflow | None = None) -> FastAPI:
    """App factory makes storage/workflow injectable for tests and deployments."""
    if workflow is None:
        db_path = database_path or os.environ.get("AGENT_RUNTIME_DB", "runtime_data/runtime.sqlite3")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        store = SQLiteCheckpointStore(db_path)
        workflow = ContentWorkflow(CheckpointManager(store), QualityPolicyEvaluator())

    app = FastAPI(
        title="Checkpoint and Rollback Agent API",
        description="Local-first content generation, deterministic evaluation, checkpointing, and recovery.",
        version="0.5.0",
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
    app.state.tasks = {}

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "checkpoint-rollback-agent"}

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
        existing = app.state.tasks.get(task_id)
        if existing:
            raise HTTPException(status_code=409, detail="task_id already exists in this API session")
        task = {"task_id": task_id, "title": request.title.strip(), "created": True}
        app.state.tasks[task_id] = task
        return task

    def ensure_task(task_id: str) -> dict[str, Any]:
        task = app.state.tasks.get(task_id)
        if task is None:
            # Permit task IDs from a prior API process if the SQLite store has checkpoints.
            row = workflow.checkpoints.store.latest_safe_checkpoint(task_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Task not found")
            task = {"task_id": task_id, "title": task_id, "created": False}
            app.state.tasks[task_id] = task
        return task

    @app.post("/api/tasks/{task_id}/generate")
    def generate(task_id: str, request: GenerateRequest) -> dict[str, Any]:
        ensure_task(task_id)
        try:
            return workflow.generate(task_id, request.prompt, mode=request.mode, model_name=request.model_name)
        except OllamaUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        task = ensure_task(task_id)
        events = workflow.checkpoints.store.get_audit_events(task_id)
        checkpoint = workflow.checkpoints.store.latest_safe_checkpoint(task_id)
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
        result = workflow.checkpoints.store.review_checkpoint(
            checkpoint_id, approved=approved, reviewer=request.reviewer.strip(),
            reason=request.reason.strip(), reviewed_at=reviewed_at,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Checkpoint not found")
        workflow.checkpoints.store.add_audit_event(
            task_id=result["task_id"], event_type="HUMAN_REVIEW_APPROVED" if approved else "HUMAN_REVIEW_REJECTED",
            checkpoint_id=checkpoint_id,
            details={"reviewer": request.reviewer.strip(), "reason": request.reason.strip(),
                     "approval_status": result["approval_status"], "is_safe": approved},
            created_at=reviewed_at,
        )
        return result

    @app.post("/api/checkpoints/{checkpoint_id}/rollback")
    def rollback_checkpoint(checkpoint_id: str) -> dict[str, Any]:
        record = workflow.checkpoints.store.get_checkpoint(checkpoint_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Checkpoint not found")
        task_id = record["task_id"]
        try:
            restored = workflow.rollback(task_id)
        except CheckpointNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SnapshotIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if restored["checkpoint_id"] != checkpoint_id:
            # The public rollback action restores latest verified safe checkpoint.
            # If user asked for an older safe checkpoint, the endpoint explicitly
            # tells the client which checkpoint was actually selected.
            restored["requested_checkpoint_id"] = checkpoint_id
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
        path = Path(os.environ.get("AGENT_EVALUATION_DB", "runtime_data/evaluation_api.sqlite3"))
        try:
            report = run_experiment(path, repetitions=repetitions)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return report

    @app.get("/api/tasks/{task_id}/events")
    def task_events(task_id: str) -> dict[str, Any]:
        ensure_task(task_id)
        return {"task_id": task_id, "events": workflow.checkpoints.store.get_audit_events(task_id)}

    return app


app = create_app()
