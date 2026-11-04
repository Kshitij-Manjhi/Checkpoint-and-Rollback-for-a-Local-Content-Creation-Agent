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


## Step 2 — Controlled failure simulation and evaluation

Step 2 adds a deterministic experimental harness. It is deliberately independent
of Ollama so that recovery behavior can be measured without LLM output variability.

### Failure scenarios

- `quality_degradation`: short candidate draft falls below a fixed length threshold.
- `policy_violation`: candidate contains one of a small set of explicit test markers.
- `corrupted_snapshot`: stored checkpoint content is modified without updating its digest.
- `missing_checkpoint`: the task has no safe checkpoint.
- `healthy_candidate`: negative control expected not to trigger rollback.

The quality and policy rules are synthetic test oracles. They are not a general
content-quality model, policy engine, or safety classifier.

### Run the experiment

```bash
python run_evaluation.py --repetitions 10 --output data/evaluation.sqlite3
```

The command writes a detailed JSON report next to the SQLite database. Generated
`data/` output is ignored by Git.

### Metrics and operational definitions

- **Recovery success rate:** successful exact restoration / trials requiring recovery.
  Corrupted and missing checkpoint trials are expected to be detected but cannot
  be recovered, so they count as unsuccessful recovery.
- **Rollback latency:** elapsed wall-clock time for the restore attempt, including
  lookup and integrity verification. Failed attempts are included.
- **Lost-work percentage:** proportion of candidate draft characters discarded
  when restoring the baseline in recoverable failure scenarios. This first
  experiment uses a simple character-count proxy.
- **False rollback trigger rate:** healthy control trials incorrectly flagged
  for rollback / all healthy control trials.
- **Post-recovery quality:** deterministic length-based score of the restored
  draft, reported as a bounded 0–1 proxy.

### Research limitations

These experiments establish behavior under controlled synthetic faults only.
They do not establish real-world policy detection accuracy, semantic quality,
tamper-proof auditability, deterministic LLM replay, or production resilience.
Record machine, OS, Python version, storage medium, repetitions, and commit hash
when collecting results for the final evaluation report.
