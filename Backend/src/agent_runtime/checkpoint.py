"""Checkpoint creation and verified restoration."""

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import Checkpoint, RestoreResult
from .store import SQLiteCheckpointStore


class CheckpointNotFoundError(LookupError):
    """Raised when a task has no safe checkpoint to restore."""


class SnapshotIntegrityError(RuntimeError):
    """Raised when a stored snapshot does not match its recorded SHA-256 digest."""


def _canonical_json(state: Mapping[str, Any]) -> str:
    try:
        return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Checkpoint state must contain JSON-serializable values") from exc


def _to_checkpoint(row: Any) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=row["checkpoint_id"],
        task_id=row["task_id"],
        sequence=row["sequence"],
        parent_checkpoint_id=row["parent_checkpoint_id"],
        snapshot_sha256=row["snapshot_sha256"],
        is_safe=bool(row["is_safe"]),
        created_at=row["created_at"],
    )


class CheckpointManager:
    """Creates versioned snapshots and restores the latest verified safe one."""

    def __init__(self, store: SQLiteCheckpointStore):
        self.store = store

    def create_checkpoint(
        self,
        task_id: str,
        state: Mapping[str, Any],
        *,
        is_safe: bool,
        parent_checkpoint_id: str | None = None,
    ) -> Checkpoint:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")

        snapshot_json = _canonical_json(state)
        digest = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        checkpoint_id = str(uuid.uuid4())
        record = {
            "checkpoint_id": checkpoint_id,
            "task_id": task_id,
            "parent_checkpoint_id": parent_checkpoint_id,
            "snapshot_json": snapshot_json,
            "snapshot_sha256": digest,
            "is_safe": bool(is_safe),
            "created_at": created_at,
        }
        sequence = self.store.insert_checkpoint(record)
        return Checkpoint(
            checkpoint_id=checkpoint_id,
            task_id=task_id,
            sequence=sequence,
            parent_checkpoint_id=parent_checkpoint_id,
            snapshot_sha256=digest,
            is_safe=bool(is_safe),
            created_at=created_at,
        )

    @staticmethod
    def _verified_state(row: Any) -> dict[str, Any] | None:
        snapshot_json = row["snapshot_json"]
        if not isinstance(snapshot_json, str) or not snapshot_json:
            return None
        actual_digest = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        if actual_digest != row["snapshot_sha256"]:
            return None
        try:
            state = json.loads(snapshot_json)
        except (json.JSONDecodeError, TypeError):
            return None
        return state if isinstance(state, dict) else None

    def latest_verified_safe_checkpoint(self, task_id: str) -> Any | None:
        """Return newest integrity-verified safe checkpoint, skipping damaged candidates."""
        for row in self.store.list_safe_checkpoints(task_id):
            if self._verified_state(row) is not None:
                return row
        return None

    def restore_latest_safe(self, task_id: str) -> RestoreResult:
        started = time.perf_counter()
        candidates = self.store.list_safe_checkpoints(task_id)
        if not candidates:
            raise CheckpointNotFoundError(f"No safe checkpoint exists for task '{task_id}'")

        row = None
        restored_state = None
        skipped = []
        for candidate in candidates:
            state = self._verified_state(candidate)
            if state is not None:
                row, restored_state = candidate, state
                break
            skipped.append(candidate["checkpoint_id"])
            self.store.add_audit_event(
                task_id=task_id, event_type="ROLLBACK_CANDIDATE_REJECTED",
                checkpoint_id=candidate["checkpoint_id"],
                details={"verification_passed": False, "reason": "missing, corrupt, or invalid snapshot; candidate skipped"},
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        if row is None:
            raise SnapshotIntegrityError(
                f"No integrity-verified safe checkpoint exists for task '{task_id}'"
            )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        checkpoint = _to_checkpoint(row)
        self.store.add_audit_event(
            task_id=task_id,
            event_type="ROLLBACK_VERIFIED",
            checkpoint_id=checkpoint.checkpoint_id,
            details={"verification_passed": True, "rollback_latency_ms": round(elapsed_ms, 3),
                     "skipped_corrupt_checkpoint_ids": skipped},
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return RestoreResult(
            checkpoint=checkpoint,
            state=restored_state,
            verification_passed=True,
            rollback_latency_ms=elapsed_ms,
        )

