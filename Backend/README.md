# Backend — Step 1: Verifiable Checkpoint Baseline

This is the first modular implementation step for **Checkpoint and Rollback for a Local Content-Creation Agent**.

## Scope of this step

- Create task-scoped, versioned checkpoints.
- Store JSON snapshots and their SHA-256 digests in a local SQLite database.
- Restore the latest checkpoint marked safe for a task.
- Verify snapshot integrity before returning restored state.
- Record checkpoint creation and verified rollback events.
- Measure rollback latency for the restore operation.

This step deliberately does **not** call Ollama, generate content, decide whether content is safe, or expose an API. Those will be integrated after the recovery primitive is tested independently.

## Setup

From this directory:

```bash
python -m venv .venv
# Windows PowerShell
.venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate

python -m pip install -e ".[test]"
pytest -q
```

## Minimal usage

```python
from agent_runtime import CheckpointManager
from agent_runtime.store import SQLiteCheckpointStore

store = SQLiteCheckpointStore("data/runtime.sqlite3")
manager = CheckpointManager(store)

checkpoint = manager.create_checkpoint(
    task_id="article-001",
    state={"prompt": "Write an introduction", "draft": "Initial draft", "version": 1},
    is_safe=True,
)

restored = manager.restore_latest_safe("article-001")
assert restored.verification_passed
print(restored.state)
print(f"Rollback latency: {restored.rollback_latency_ms:.3f} ms")
```

## Current implementation boundary

SQLite is used as a lightweight local experiment adapter so the first recovery tests run without a database server. The project brief's proposed MySQL metadata store and Git/object-store artifact storage remain possible later adapters; this choice is not a claim that the final storage architecture has been decided.

The `is_safe` flag is supplied by the caller in this step. It is not an AI safety judgement. The next step will define the evaluator contract and failure-injection scenarios.

## Initial correctness properties

1. A restore returns the exact state saved in the selected safe checkpoint.
2. A newer unsafe checkpoint is never selected for safe recovery.
3. Checkpoints belonging to different task IDs are isolated.
4. A missing safe checkpoint produces an explicit error.
5. A modified snapshot is rejected when its SHA-256 digest no longer matches.
