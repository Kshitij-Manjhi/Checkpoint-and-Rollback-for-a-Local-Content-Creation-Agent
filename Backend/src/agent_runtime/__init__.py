"""Core runtime for the local content-creation agent."""

from .checkpoint import CheckpointManager, CheckpointNotFoundError, SnapshotIntegrityError
from .models import Checkpoint, RestoreResult

__all__ = [
    "Checkpoint",
    "CheckpointManager",
    "CheckpointNotFoundError",
    "RestoreResult",
    "SnapshotIntegrityError",
]

from .failure_simulator import assess_candidate, SCENARIOS
from .evaluation import run_experiment
