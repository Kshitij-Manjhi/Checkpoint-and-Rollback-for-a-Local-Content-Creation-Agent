"""Checkpoint creation and verified restoration."""

import hashlib
import json
import re
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


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


def _redact_text(value: str) -> str:
    redacted = value
    redacted = _SECRET_PATTERNS[0].sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[1].sub("Bearer [REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[2].sub("[REDACTED_AWS_KEY]", redacted)
    redacted = _SECRET_PATTERNS[3].sub("[REDACTED_API_KEY]", redacted)
    return redacted


def sanitize_checkpoint_state(value: Any) -> Any:
    """Copy JSON-like checkpoint state while redacting common credential forms."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [sanitize_checkpoint_state(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_checkpoint_state(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token|credential)", key_text):
                result[key_text] = "[REDACTED]" if item is not None else None
            else:
                result[key_text] = sanitize_checkpoint_state(item)
        return result
    return value


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


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


class CheckpointManager:
    """Creates versioned snapshots and restores the latest verified safe one."""

    def __init__(self, store: Any, artifact_store: Any | None = None):
        self.store = store
        self.artifact_store = artifact_store

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

        safe_state = sanitize_checkpoint_state(dict(state))
        snapshot_json = _canonical_json(safe_state)
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
        artifact_uri = None
        if self.artifact_store is not None:
            try:
                artifact_uri = self.artifact_store.put(checkpoint_id, safe_state, digest)
            except Exception as exc:
                self.store.add_audit_event(
                    task_id, "ARTIFACT_COMMIT_FAILED", checkpoint_id,
                    {"snapshot_sha256": digest, "failure_type": type(exc).__name__,
                     "failure": str(exc)[:1000], "outcome": "checkpoint_not_created"},
                    created_at,
                )
                raise
            record["artifact_commit_hash"] = artifact_uri
        try:
            sequence = self.store.insert_checkpoint(record)
        except Exception as exc:
            try:
                self.store.add_audit_event(
                    task_id, "CHECKPOINT_METADATA_PERSIST_FAILED", checkpoint_id,
                    {"artifact_commit_hash": artifact_uri, "snapshot_sha256": digest,
                     "failure_type": type(exc).__name__, "failure": str(exc)[:1000],
                     "outcome": "unreferenced_git_commit" if artifact_uri else "no_checkpoint"},
                    created_at,
                )
            except Exception:
                pass
            raise
        if artifact_uri:
            self.store.add_audit_event(task_id, "ARTIFACT_VERSIONED", checkpoint_id,
                {"artifact_commit_hash": artifact_uri, "artifact_sha256": digest}, created_at)
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
            state = self._verified_state(row)
            if state is not None:
                if self.artifact_store is not None:
                    artifact = self.artifact_store.get(row["checkpoint_id"], _row_value(row, "artifact_commit_hash"))
                    if not artifact or artifact.get("sha256") != row["snapshot_sha256"] or artifact.get("state") != state:
                        continue
                return row
        return None

    def restore_latest_safe(self, task_id: str) -> RestoreResult:
        started = time.perf_counter()
        candidates = self.store.list_safe_checkpoints(task_id)
        if not candidates:
            self.store.add_audit_event(
                task_id, "ROLLBACK_FAILED", "",
                {"target_checkpoint_id": None, "verification_passed": False,
                 "failure": "no_safe_checkpoint", "outcome": "no_restore"},
                datetime.now(timezone.utc).isoformat(),
            )
            raise CheckpointNotFoundError(f"No safe checkpoint exists for task '{task_id}'")

        row = None
        restored_state = None
        skipped = []
        for candidate in candidates:
            state = self._verified_state(candidate)
            if state is not None and self.artifact_store is not None:
                artifact = self.artifact_store.get(candidate["checkpoint_id"], _row_value(candidate, "artifact_commit_hash"))
                if not artifact or artifact.get("sha256") != candidate["snapshot_sha256"] or artifact.get("state") != state:
                    state = None
                else:
                    try:
                        restoration = self.artifact_store.restore(
                            candidate["checkpoint_id"], _row_value(candidate, "artifact_commit_hash"),
                            task_id, candidate["snapshot_sha256"],
                        )
                    except Exception as exc:
                        state = None
                        self.store.add_audit_event(
                            task_id=task_id, event_type="ROLLBACK_CANDIDATE_REJECTED",
                            checkpoint_id=candidate["checkpoint_id"],
                            details={"verification_passed": False, "reason": "Git restoration failed",
                                     "failure_type": type(exc).__name__, "failure": str(exc)[:1000]},
                            created_at=datetime.now(timezone.utc).isoformat(),
                        )
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
            self.store.add_audit_event(
                task_id, "ROLLBACK_FAILED", "",
                {"target_checkpoint_id": None, "verification_passed": False,
                 "failure": "no_integrity_verified_safe_checkpoint",
                 "skipped_corrupt_checkpoint_ids": skipped, "outcome": "no_restore"},
                datetime.now(timezone.utc).isoformat(),
            )
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
                     "skipped_corrupt_checkpoint_ids": skipped,
                     "artifact_commit_hash": _row_value(row, "artifact_commit_hash"),
                     "restored_artifact": restoration if self.artifact_store is not None else None},
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return RestoreResult(
            checkpoint=checkpoint,
            state=restored_state,
            verification_passed=True,
            rollback_latency_ms=elapsed_ms,
        )

    def review_checkpoint(self, checkpoint_id: str, *, approved: bool, reviewer: str,
                          reason: str, reviewed_at: str) -> Any:
        """Version review state first, then atomically publish the MySQL/SQLite metadata."""
        record = self.store.get_checkpoint(checkpoint_id)
        if record is None:
            return None
        state = record.get("state")
        if not isinstance(state, dict) or not record.get("integrity_verified"):
            raise SnapshotIntegrityError("Cannot review a checkpoint with a missing or corrupt snapshot")
        reviewed_state = dict(state)
        reviewed_state.update({
            "approval_status": "approved" if approved else "rejected",
            "reviewer": reviewer, "review_reason": reason, "reviewed_at": reviewed_at,
        })
        reviewed_state = sanitize_checkpoint_state(reviewed_state)
        if self.artifact_store is not None:
            digest = hashlib.sha256(_canonical_json(reviewed_state).encode("utf-8")).hexdigest()
            try:
                commit_hash = self.artifact_store.put(checkpoint_id, reviewed_state, digest)
            except Exception as exc:
                self.store.add_audit_event(
                    record["task_id"], "ARTIFACT_REVIEW_COMMIT_FAILED", checkpoint_id,
                    {"failure_type": type(exc).__name__, "failure": str(exc)[:1000],
                     "outcome": "review_not_published"}, reviewed_at,
                )
                raise
        else:
            commit_hash = None
        try:
            return self.store.review_checkpoint(
                checkpoint_id, approved=approved, reviewer=reviewed_state["reviewer"],
                reason=reviewed_state["review_reason"],
                reviewed_at=reviewed_at, artifact_commit_hash=commit_hash,
            )
        except Exception as exc:
            try:
                self.store.add_audit_event(
                    record["task_id"], "CHECKPOINT_REVIEW_METADATA_FAILED", checkpoint_id,
                    {"artifact_commit_hash": commit_hash,
                     "failure_type": type(exc).__name__, "failure": str(exc)[:1000],
                     "outcome": "reviewed_git_commit_not_published"}, reviewed_at,
                )
            except Exception:
                pass
            raise

