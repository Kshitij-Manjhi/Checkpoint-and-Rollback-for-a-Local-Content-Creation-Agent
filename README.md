# DraftGuard — Quality Evaluation Engine

DraftGuard is a local-first content-creation prototype with versioned checkpoints, verified rollback, automatic quality checks, optional local Ollama/Qwen semantic scoring, and human-controlled approval.

## Run on Windows PowerShell

### 1. Backend

```powershell
cd Backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
# Configure a local MySQL database first; copy Backend/.env.example to Backend/.env, then edit the URL/password. See Backend/README.md.
$env:AGENT_RUNTIME_STORAGE = "mysql"
$env:AGENT_RUNTIME_MYSQL_URL = "mysql://draftguard:CHANGE_ME@127.0.0.1:3306/draftguard"
$env:AGENT_ARTIFACT_GIT_DIR = "runtime_data/artifact_repository"
$env:AGENT_EVIDENCE_DIR = "runtime_data/evidence_index"
python -m uvicorn agent_runtime.api:app --host 127.0.0.1 --port 8000
```

### 2. Frontend — separate terminal

```powershell
cd Frontend
python -m http.server 5500 --bind 127.0.0.1
```

Open `http://127.0.0.1:5500`. API documentation is at `http://127.0.0.1:8000/docs`.

## Semantic quality evaluation

When real Ollama is used for an `ollama` generation, semantic evaluation is enabled automatically by default and uses the selected model. It requests structured JSON scores for relevance, completeness, clarity, factual support, and style/format. Malformed model output or evaluation errors route the candidate to human review rather than silently passing it.

PowerShell configuration before starting the backend:

```powershell
$env:AGENT_SEMANTIC_EVALUATION = "auto" # auto, 1, or 0
# Optional: model used for semantic evaluation in template mode when explicitly enabled
$env:AGENT_QUALITY_MODEL = "qwen3:4b"
```

- `auto`: semantic scoring for real Ollama-generated drafts (default).
- `1`: explicitly enable semantic scoring; set `AGENT_QUALITY_MODEL` to use it in template mode.
- `0`: deterministic evaluation only.

## Evaluation behavior

- Deterministic rule checks run on every generated candidate.
- Five quality dimensions are scored from 0–5 and combined using initial weights: relevance 30%, completeness 25%, clarity 20%, factual support 15%, style/format 10%.
- `quality_score` remains normalized from 0–1 for compatibility; `quality_score_percent` is 0–100.
- Blocking policy markers cannot be overridden by a high semantic score.
- A high score does not mark a checkpoint safe. Human approval is still required.
- Rollback continues to restore the latest integrity-verified safe checkpoint.
- Factual support is not independently verified unless evidence is supplied and checked. These scores are prototype indicators, not safety guarantees.

## Test

From `Backend`:

```powershell
python -m pytest -q
```

The test suite uses fake providers for predictable behavior. Passing unit tests does not establish live Ollama quality accuracy; run and label real examples separately for research evaluation.

The current dashboard also provides a fixed automated-test runner, a separate local Ollama factuality smoke benchmark, integration availability probes, and persisted synthetic recovery experiment reports. See `Frontend/README.md`, `HLD.md`, `LLD.md`, and `STEP10_EXPERIMENTAL_EVALUATION.md` for the actual endpoint behavior, metric definitions, limitations, and reproducibility steps. Unit tests use temporary SQLite only as test infrastructure; deployment mode continues to require configured MySQL, persistent ChromaDB, and Git artifact storage.

## Step 9 — Persistence and reliability

- Task IDs, titles, and creation timestamps are now stored in SQLite rather than only in API process memory.
- Task metadata and checkpoints remain available when a new API app instance starts against the same database file.
- Existing databases are migrated non-destructively by creating the `tasks` table if it is absent. Older checkpoint-only tasks are reconstructed with their task ID as a fallback title when accessed.
- Rollback checks safe checkpoints newest-first and verifies each snapshot's SHA-256 digest and JSON-object shape before restoring it.
- If the newest safe checkpoint is corrupt or has an empty/missing snapshot payload, the runtime records `ROLLBACK_CANDIDATE_REJECTED` and tries an older safe checkpoint. If no verified safe checkpoint remains, rollback fails closed with an explicit error.
- The original `is_safe` flag is not changed merely because a snapshot is corrupt; the corrupted record remains visible for investigation, but is ineligible for restoration.

### Step 9 validation

From `Backend`, run `python -m pytest -q`. The Step 9 suite adds tests for task metadata across app restarts, duplicate task IDs across app instances, fallback from corrupt and empty snapshots, and fail-closed behavior when every safe snapshot is corrupt. Tests use temporary SQLite files and fake providers; live browser and Ollama testing are separate manual checks.

## Step 10 — Experimental evaluation

The evaluation runner now compares a no-checkpoint baseline with DraftGuard's checkpointed recovery under deterministic synthetic workloads. It reports recovery success, rollback latency, false rollback rate, a character-based lost-work proxy, post-recovery synthetic quality, missing-checkpoint detection, and fallback from a corrupted newest safe checkpoint.

Run from `Backend/`:

```powershell
python run_evaluation.py --repetitions 10 --output data/step10_experiment.sqlite3
```

See [`STEP10_EXPERIMENTAL_EVALUATION.md`](STEP10_EXPERIMENTAL_EVALUATION.md) and [`STEP10_REFERENCE_RESULTS.json`](STEP10_REFERENCE_RESULTS.json) for metric definitions and a reference run. These are deterministic synthetic experiments, not real-world safety or semantic-quality benchmarks.

## Step 11 — Provenance and responsible-AI audit

The configured metadata database retains generation prompts and outputs, provider and
model identifiers (and version metadata when supplied by the provider),
deterministic and semantic evaluator versions, scores, checks, findings, policy
decisions, checkpoint IDs, parent lineage, and snapshot integrity outcomes.
Human approve/reject records include the reviewer, reason, and timestamp and
are committed atomically with the checkpoint's review state. Rollback events
record the requested and restored checkpoint, verification result, and outcome;
generation, checkpoint-creation, and rollback failures are also persisted.

The audit is append-only at the application workflow level; it is not a
tamper-proof or cryptographically signed ledger. Prompt and output text are
stored in the configured metadata database (MySQL in deployment, SQLite in local tests) to support provenance review. API response
contracts, the human approval gate, and verified-safe rollback selection are
unchanged.

### Step 11 validation

Run `python -m pytest` from `Backend/`. The tests cover provenance contents,
metadata persistence across fresh app instances, approval and rejection records,
rollback target and integrity outcomes, and generation/rollback failure events.
Tests use fake providers. A live Ollama service and browser-based UI were not
used as part of this validation.


## Required architecture stack

Production-stack mode uses MySQL metadata, a dedicated Git artifact repository, persistent Chroma evidence retrieval, Python orchestration, and local Ollama/Qwen. The deployment path requires explicit MySQL configuration and does not silently downgrade to SQLite. See `Backend/README.md` for setup and the explicit SQLite-only test mode.

## Git-backed checkpoint artifacts

Git is the sole checkpoint artifact backend. DraftGuard commits each checkpoint payload as `artifacts/<checkpoint_id>.json` in the dedicated repository configured by `AGENT_ARTIFACT_GIT_DIR`. MySQL records the exact `artifact_commit_hash` with each checkpoint row; human approval creates a new immutable commit for the reviewed snapshot, then records that hash alongside the approved metadata. Rejected and pending candidates may also have commits for provenance, but they are not eligible for recovery.

Rollback checks safe checkpoints newest-first. For each candidate it verifies the metadata snapshot digest and JSON object, loads the artifact from the recorded commit (not the current `HEAD`), checks the checkpoint ID and SHA-256 digest, and compares the artifact state with the metadata snapshot. It atomically writes the verified state to `working/<sha256(task_id)>.json` inside the artifact repository and checks the restored digest again. A missing or damaged reference is audited and skipped; if no valid approved reference remains, rollback fails closed. Restore does not reset or rewrite Git history and does not push to any remote.

Git commit failure prevents checkpoint metadata from being marked safe and records an `ARTIFACT_COMMIT_FAILED` audit event when metadata storage is available. If a Git commit succeeds but the following metadata transaction fails, DraftGuard attempts a `CHECKPOINT_METADATA_PERSIST_FAILED` or `CHECKPOINT_REVIEW_METADATA_FAILED` audit event; the Git commit remains unreferenced and is not eligible for rollback. Live MySQL and Ollama availability must be evaluated separately from the automated Git/SQLite integration tests.

See [`HLD.md`](HLD.md) and [`LLD.md`](LLD.md) for the current architecture and recovery details. These documents describe the implementation, while the project-context planning file remains read-only reference material.


## Evidence-grounded factuality evaluation (FASTFACT-inspired prototype)

DraftGuard now includes an opt-in, lightweight adaptation of the FASTFACT claim-and-verify pattern. It is **not a reproduction of FASTFACT** and it does not treat a Qwen self-score as factual proof.

Set these variables before starting the backend:

```powershell
$env:AGENT_FACTUALITY_EVALUATION = "1"
# Optional; otherwise uses the selected generation model, then AGENT_QUALITY_MODEL
$env:AGENT_FACTUALITY_MODEL = "qwen3:4b"
```

The workflow asks the model to extract atomic factual claims, retrieves only indexed records marked `evidence`, `source`, `reference`, or `document`, then asks the model to verify each claim against the retrieved passages. The returned result records each claim, verdict, evidence IDs/text, and reason. Supported/contradicted verdicts are downgraded to `insufficient_evidence` if the verifier cites no valid retrieved passage. A contradicted claim rejects the draft; insufficient evidence or an evaluator error routes it to human review. Existing human approval remains required for safe checkpoints.

### Run the fixed smoke-test benchmark

The small, versioned starter dataset is `Backend/data/factuality_benchmark.json`. It has three manually authored cases (supported, contradicted, and insufficient evidence); it is a wiring/demo benchmark, not enough data to support a general accuracy claim.

From `Backend`, after installing project dependencies and starting Ollama:

```powershell
python run_factuality_benchmark.py --model qwen3:4b --timeout 180
```

The runner first checks the configured Ollama server (`OLLAMA_BASE_URL`, default `http://localhost:11434`) and confirms the requested model is installed. The timeout applies to both discovery and generation. It then indexes case evidence into a persistent local Chroma directory and writes trial details to `data/factuality_results.json`. It reports accuracy only over cases for which evaluation returns exactly one claim, marks the report incomplete, and exits nonzero if any case is unscored. Provider failures retain the underlying connection, HTTP, timeout, or Ollama error details; failed evaluations contain no claim verdicts and are never scored. Re-running reuses the persistent evidence index. For a clean repeat, remove `runtime_data/factuality_benchmark_index` before running again.

### Add evidence before generating

Create a task in the UI/API, then index source passages for that same task. Example using PowerShell (replace the task ID and text with your benchmark case):

```powershell
$body = @{
  text = "Checkpoint snapshots are serialized to canonical JSON and protected with a SHA-256 digest."
  reference_id = "benchmark-source-checkpoint-integrity-01"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/tasks/YOUR_TASK_ID/evidence" `
  -ContentType "application/json" -Body $body
```

Repeat for each source passage. Then generate through `POST /api/tasks/YOUR_TASK_ID/generate` using `{"prompt":"...","mode":"ollama","model_name":"qwen3:4b"}`. Inspect `state.factuality_evaluation` in the response/checkpoint and `CONTENT_EVALUATED` audit event. Evidence is task-scoped; an evidence passage added to one task is not automatically available to another.

### Limitations and reproducibility

- The evaluator is opt-in (`AGENT_FACTUALITY_EVALUATION=0` by default) so existing workflow behaviour remains unchanged unless explicitly enabled.
- Claim extraction and verdict generation still use an LLM; evidence IDs are validated in Python, but the semantic correctness of the verdict is not guaranteed. Evaluate against manually labelled examples.
- The existing Chroma index uses deterministic feature-hashing vectors, not a trained semantic embedding model. Retrieval may miss paraphrases or retrieve merely related passages. Check retrieval results in every benchmark case.
- If no eligible evidence is retrieved, the claim is marked `insufficient_evidence`; absence of evidence is not treated as proof that a claim is false.
- The benchmark must be fixed and versioned. Record the model name, prompt, evidence corpus/version, and run results. Do not present the current synthetic recovery experiment as a factuality benchmark.
- This implementation has unit tests with fake model responses; it does not establish real-model accuracy until tested with a labelled corpus and the actual local Ollama model.
