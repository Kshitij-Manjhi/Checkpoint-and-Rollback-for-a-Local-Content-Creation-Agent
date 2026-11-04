"""Domain models used by the checkpoint runtime."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    sequence: int
    parent_checkpoint_id: str | None
    snapshot_sha256: str
    is_safe: bool
    created_at: str


@dataclass(frozen=True)
class RestoreResult:
    checkpoint: Checkpoint
    state: dict[str, Any]
    verification_passed: bool
    rollback_latency_ms: float
