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


## Terminal content-agent entry points

The standalone terminal entry points use descriptive filenames:

- `python content_agent.py` — local content workflow with simulated content generation.
- `python ollama_content_agent.py` — local content workflow connected to Ollama (`qwen3:4b` by default).

Both write local state under `runtime_data/`. These standalone entry points are
kept separate from the tested modular checkpoint and evaluation engine in
`src/agent_runtime/`; their simple rule checks are not a production safety evaluator.


## Step 3 — Model selection, generation, and deterministic evaluation

Step 3 adds a modular workflow on top of the same tested checkpoint store and
rollback manager. It does not replace the Step 1/2 experiment harness.

### Dynamic Ollama model discovery

The provider reads `GET http://localhost:11434/api/tags` each time
`OllamaModelProvider.list_models()` is called. Model names are not hardcoded.
The names available depend on what is installed and what Ollama reports.
Generation uses `POST /api/generate` with `stream: false`.

### Two generation modes

- `no_llm`: deterministic template-based content, no Ollama process, API key,
  model download, or external dependency required.
- `ollama`: uses a caller-selected model returned by dynamic discovery.

### Shared workflow example

```python
from agent_runtime import CheckpointManager, ContentWorkflow, OllamaModelProvider
from agent_runtime.store import SQLiteCheckpointStore

store = SQLiteCheckpointStore("runtime_data/runtime.sqlite3")
workflow = ContentWorkflow(CheckpointManager(store))

# Always available, even when Ollama is not installed/running:
result = workflow.generate("task-001", "Explain checkpoint recovery", mode="no_llm")
print(result["evaluation"]["decision"])

# When Ollama is running, list installed models and select one:
provider = OllamaModelProvider()
models = provider.list_models()
print(models)
result = workflow.generate(
    "task-001", "Explain checkpoint recovery",
    mode="ollama", model_name=models[0],  # replace with the user's selection
)
print(result["state"]["model_name"])
```

A first run creates an initial safe baseline. Each generated candidate is
evaluated and checkpointed through the same `CheckpointManager`. Only an
`accept` result is marked safe. Rejected or review-needed candidates are saved
as unsafe checkpoints for traceability and cannot be selected by safe rollback.
`workflow.rollback(task_id)` uses the same verified restore path from Step 1.

### Quality and policy decisions

`QualityPolicyEvaluator` returns `accept`, `reject`, or `needs_human_review`,
along with a score, reason codes, and per-check outcomes. The checks are explicit
and deterministic: empty content and known prohibited markers are rejected;
short drafts and a small set of high-impact claims require human review.
These rules are intentionally narrow and transparent. They are **not** a
comprehensive safety classifier, factuality verifier, or substitute for human
approval. The generator model is not used as the sole safety judge.

Audit events include task/checkpoint IDs and, for generated candidates, mode,
selected model name (or null in no-LLM mode), decision, reason codes, and lengths.
Prompt and full draft text are stored in the checkpoint snapshot; treat the local
SQLite database as project data and do not put real secrets or sensitive personal
data in test prompts.

### Run all tests

```powershell
python -m pip install -e ".[test]"
python -m pytest -v
```

The Step 3 tests use a fake provider for predictable tests and mocked HTTP
responses for model discovery, so the suite does not require Ollama to be running.


### Interactive Step 3 CLI

From the `Backend` folder after installing the package:

```powershell
python -m pip install -e ".[test]"
python step3_cli.py
```

Choose No-LLM mode for a fully local deterministic template. Choose Ollama mode
to query the currently installed model list and select a model by number. The
modular workflow stores checkpoints and audit events in
`runtime_data/runtime.sqlite3`. This CLI is a prototype interface, not a web API.


## Step 4 — REST API for frontend integration

Step 4 adds a FastAPI adapter around the same `ContentWorkflow`, checkpoint
manager, quality/policy evaluator, and Step 2 evaluation runner. The API does
not implement a second checkpoint or rollback engine.

### Install and run

```powershell
python -m pip install -e ".[test]"
python -m uvicorn agent_runtime.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for interactive API documentation. The server
binds to localhost by default in the command above. Do not expose it to an
untrusted network without adding authentication, authorization, request limits,
and deployment hardening.

The default SQLite database is `runtime_data/runtime.sqlite3`. Override it with
the `AGENT_RUNTIME_DB` environment variable. The experimental evaluation runner
uses `AGENT_EVALUATION_DB`, defaulting to `runtime_data/evaluation_api.sqlite3`.

### Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/health` | Service health |
| GET | `/api/models` | Dynamically discover installed Ollama models; reports unavailable if Ollama is offline |
| POST | `/api/tasks` | Create a task ID/title |
| POST | `/api/tasks/{task_id}/generate` | Generate and evaluate a candidate |
| GET | `/api/tasks/{task_id}` | Task metadata and latest safe state |
| GET | `/api/tasks/{task_id}/checkpoints` | Checkpoint history |
| GET | `/api/checkpoints/{checkpoint_id}` | Checkpoint metadata, snapshot, and integrity status |
| POST | `/api/checkpoints/{checkpoint_id}/rollback` | Roll back the task to its latest verified safe checkpoint |
| POST | `/api/tasks/{task_id}/rollback` | Roll back a task to its latest verified safe checkpoint |
| POST | `/api/evaluations/run` | Run deterministic Step 2 experiments |
| GET | `/api/tasks/{task_id}/events` | Audit event history |

### Example requests

Create a task:

```json
{"task_id":"article-001","title":"Article draft"}
```

Generate without an LLM:

```json
{"prompt":"Explain recoverable AI workflows","mode":"no_llm"}
```

Generate with Ollama:

```json
{"prompt":"Explain recoverable AI workflows","mode":"ollama","model_name":"qwen3:8b"}
```

Run five repetitions per deterministic scenario:

```json
{"repetitions":5}
```

### Integration notes and current boundaries

- Task titles/creation metadata currently live in the API process memory. Checkpoints
  and audit events are persisted in SQLite. A future task registry migration can
  persist task titles and lifecycle metadata across restarts.
- `/api/checkpoints/{checkpoint_id}/rollback` identifies the task from the requested
  checkpoint and invokes the safe rollback policy. The runtime selects the latest
  verified safe checkpoint for that task; it does not promise arbitrary historical
  restore to an unsafe or older snapshot.
- `/api/checkpoints/{checkpoint_id}` returns the snapshot state only when its digest
  verifies. Treat this endpoint as sensitive local project data.
- The API has no authentication or user/tenant isolation and is intended for a
  local student prototype. Keep it bound to localhost unless secured.
- The deterministic evaluation endpoint writes its own experimental database/report
  and does not measure real-world LLM safety or semantic quality.

### API tests

The API tests use a fake Ollama provider and temporary SQLite databases. They
exercise task creation, model listing, generation, checkpoint inspection,
audit events, policy rejection, rollback, and invalid requests without requiring
a running Ollama server.


## Step 5 — Human approval gate and governed rollback

Step 5 changes the checkpoint eligibility rule: **automated acceptance is not human approval**.
The initial empty baseline remains safe, but every generated candidate is stored as
unsafe until a human reviewer approves it. Automated policy rejection is marked
`blocked` and cannot be approved through the normal review endpoint.

### Review a generated candidate

In Swagger at `http://127.0.0.1:8000/docs`:

1. Create a task and generate a draft.
2. Copy the returned `checkpoint_id`.
3. Call `POST /api/checkpoints/{checkpoint_id}/review` with one of these bodies.

Approve:
```json
{
  "decision": "approve",
  "reviewer": "reviewer-1",
  "reason": "Reviewed for clarity and policy compliance."
}
```

Reject:
```json
{
  "decision": "reject",
  "reviewer": "reviewer-1",
  "reason": "Contains unsupported claims and needs revision."
}
```

A checkpoint can be reviewed only once. The reviewer, decision, reason, and
timestamp are stored in the snapshot and recorded as an audit event. The snapshot
digest is recomputed after this metadata update. Approved checkpoints become
eligible for rollback; rejected and pending drafts remain ineligible. This is a
prototype review gate, not identity authentication: the API does not verify that
the reviewer string belongs to a real authorized person.

### Step 5 limitations

- Human review is represented by an API call; no graphical review UI is included yet.
- Authentication, reviewer identity verification, role-based access, and concurrent
  review hardening remain future work.
- Approval is a human workflow decision, not proof that the content is factually
  correct or harmless.
- The existing narrow deterministic policy checks remain unchanged and are not a
  comprehensive safety classifier.
