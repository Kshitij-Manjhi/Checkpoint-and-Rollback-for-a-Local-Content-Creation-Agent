# FASTFACT-inspired factuality evaluator — implementation notes

## What changed
- Added `Backend/src/agent_runtime/factuality_evaluator.py`.
  - Extracts atomic claims from a draft using the configured local model.
  - Retrieves task-scoped passages from the configured retrieval index.
  - Only uses records tagged `evidence`, `source`, `reference`, or `document`.
  - Verifies each claim against retrieved text and records verdict, valid evidence IDs, source text, and rationale.
  - Converts unsupported verdicts without valid evidence IDs to `insufficient_evidence`.
  - Returns a fail-closed error result for malformed model output or runtime failures.
- Added factuality integration to `ContentWorkflow`, gated by `AGENT_FACTUALITY_EVALUATION=1`.
- Added deterministic policy combination:
  - Any `contradicted` claim causes `reject`.
  - `insufficient_evidence` or evaluation failure routes to human review.
  - Existing human approval remains required for safe checkpoints.
- Added `POST /api/tasks/{task_id}/evidence` to index task-scoped evidence.
- Added `Backend/data/factuality_benchmark.json` with three manually authored smoke-test cases.
- Added `Backend/run_factuality_benchmark.py` to run the fixed cases with local Ollama/Qwen and save detailed JSON results.
- Updated README and `.env.example` configuration notes.
- Added tests for evidence use, contradicted claims, insufficient evidence, invalid evidence IDs, non-evidence records, malformed model output, and the evidence API.

## Validation performed in this build environment
- `python -m pytest -q` with `AGENT_RUNTIME_STORAGE=sqlite`: **57 passed, 1 skipped**.
- Python syntax compilation: **30 Python files compiled, no syntax errors**.
- The test run used fake model responses and SQLite test fixtures. It did not test live Ollama judgement accuracy, live MySQL credentials/connectivity, or a real production Chroma corpus.

## How to enable the integrated evaluator
Set before starting the backend:
```powershell
$env:AGENT_FACTUALITY_EVALUATION = "1"
$env:AGENT_FACTUALITY_MODEL = "qwen3:4b"
```
Use the MySQL-backed API configuration to enable the configured persistent Chroma retrieval index, create a task, and post source passages to `/api/tasks/{task_id}/evidence` before generating. See the root `README.md`.

## How to run the smoke benchmark
From `Backend`, after dependencies and Ollama are running:
```powershell
python run_factuality_benchmark.py --model qwen3:4b
```
It writes `Backend/data/factuality_results.json`. The benchmark has only three cases and is for integration demonstration, not statistically meaningful performance claims.

## Limitations
- This is a small FASTFACT-inspired adaptation, not a reproduction of FASTFACT.
- The model still extracts claims and proposes verdicts; evidence IDs are checked programmatically, but verdict correctness is not guaranteed.
- Current Chroma vectors use deterministic feature hashing rather than trained semantic embeddings. Retrieval can miss paraphrases or return related but non-supporting text.
- The smoke dataset is tiny and manually authored. Build a larger manually labelled benchmark before making accuracy claims.
- `AGENT_FACTUALITY_EVALUATION` defaults to off to avoid changing the existing workflow unexpectedly.
