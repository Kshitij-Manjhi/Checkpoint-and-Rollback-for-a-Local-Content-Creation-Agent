# DraftGuard — Quality Evaluation Engine

DraftGuard is a local-first content-creation prototype with versioned checkpoints, verified rollback, automatic quality checks, optional local Ollama/Qwen semantic scoring, and human-controlled approval.

## Run on Windows PowerShell

### 1. Backend

```powershell
cd Backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
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

## Step 9 — Persistence and reliability

- Task IDs, titles, and creation timestamps are now stored in SQLite rather than only in API process memory.
- Task metadata and checkpoints remain available when a new API app instance starts against the same database file.
- Existing databases are migrated non-destructively by creating the `tasks` table if it is absent. Older checkpoint-only tasks are reconstructed with their task ID as a fallback title when accessed.
- Rollback checks safe checkpoints newest-first and verifies each snapshot's SHA-256 digest and JSON-object shape before restoring it.
- If the newest safe checkpoint is corrupt or has an empty/missing snapshot payload, the runtime records `ROLLBACK_CANDIDATE_REJECTED` and tries an older safe checkpoint. If no verified safe checkpoint remains, rollback fails closed with an explicit error.
- The original `is_safe` flag is not changed merely because a snapshot is corrupt; the corrupted record remains visible for investigation, but is ineligible for restoration.

### Step 9 validation

From `Backend`, run `python -m pytest -q`. The Step 9 suite adds tests for task metadata across app restarts, duplicate task IDs across app instances, fallback from corrupt and empty snapshots, and fail-closed behavior when every safe snapshot is corrupt. Tests use temporary SQLite files and fake providers; live browser and Ollama testing are separate manual checks.
