import pytest

from agent_runtime import (
    CheckpointManager,
    CheckpointNotFoundError,
    SnapshotIntegrityError,
)
from agent_runtime.store import SQLiteCheckpointStore


@pytest.fixture
def runtime(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "test.sqlite3")
    return CheckpointManager(store), store


def test_restore_returns_exact_latest_safe_state(runtime):
    manager, _ = runtime
    original = {"prompt": "Write about AI", "draft": "A safe draft", "version": 1}
    first = manager.create_checkpoint("task-1", original, is_safe=True)
    manager.create_checkpoint("task-1", {"draft": "bad output", "version": 2}, is_safe=False)

    restored = manager.restore_latest_safe("task-1")

    assert restored.state == original
    assert restored.checkpoint.checkpoint_id == first.checkpoint_id
    assert restored.verification_passed is True
    assert restored.rollback_latency_ms >= 0


def test_latest_safe_checkpoint_is_selected(runtime):
    manager, _ = runtime
    manager.create_checkpoint("task-1", {"version": 1}, is_safe=True)
    second = manager.create_checkpoint("task-1", {"version": 2}, is_safe=True)

    restored = manager.restore_latest_safe("task-1")

    assert restored.state == {"version": 2}
    assert restored.checkpoint.checkpoint_id == second.checkpoint_id


def test_tasks_are_isolated(runtime):
    manager, _ = runtime
    manager.create_checkpoint("task-A", {"draft": "A"}, is_safe=True)
    manager.create_checkpoint("task-B", {"draft": "B"}, is_safe=True)

    assert manager.restore_latest_safe("task-A").state == {"draft": "A"}
    assert manager.restore_latest_safe("task-B").state == {"draft": "B"}


def test_missing_safe_checkpoint_raises(runtime):
    manager, _ = runtime
    with pytest.raises(CheckpointNotFoundError):
        manager.restore_latest_safe("missing-task")


def test_modified_snapshot_is_rejected(runtime):
    manager, store = runtime
    checkpoint = manager.create_checkpoint("task-1", {"draft": "original"}, is_safe=True)
    store.tamper_snapshot_for_test(checkpoint.checkpoint_id, '{"draft":"tampered"}')

    with pytest.raises(SnapshotIntegrityError):
        manager.restore_latest_safe("task-1")


def test_non_json_state_is_rejected(runtime):
    manager, _ = runtime
    with pytest.raises(ValueError, match="JSON-serializable"):
        manager.create_checkpoint("task-1", {"bad": object()}, is_safe=True)
