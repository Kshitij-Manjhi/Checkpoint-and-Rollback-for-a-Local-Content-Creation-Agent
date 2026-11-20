# Factuality benchmark fix verification

Run date: 2026-11-20 (Asia/Kolkata)

## Changes

- Ollama transport failures now preserve the URL, HTTP status/body, timeout or connection detail. Ollama error payloads are surfaced instead of being mistaken for successful generations.
- The factuality evaluator reports the exception detail and returns `status: error` with no claims or verdict counts when provider calls fail.
- The benchmark accepts `--timeout` (default 120 seconds), checks Ollama availability and model installation before initializing the evidence index, and records preflight errors clearly.
- Benchmark reports identify incomplete runs, score only successfully evaluated single-claim cases, and return a nonzero process status if any case is unscored.
- The one supported smoke-test case is a single atomic claim aligned with the cited evidence.

## Verification results

- Automated backend tests: **65 passed in 5.59 seconds** using SQLite test mode.
- Targeted provider/evaluator/CLI/workflow tests: **28 passed in 0.32 seconds**.
- Direct Ollama generation: `qwen3:4b` returned `OK` in **3.64 seconds**.
- Real local factuality smoke benchmark: **3/3 scored, 3/3 correct**, model `qwen3:4b`, timeout 180 seconds, Ollama at `http://localhost:11434`.
- Per-case verdicts: supported checkpoint hash — supported; plaintext password storage — contradicted; AES-256 encryption claim — insufficient evidence.

The three-case dataset is a smoke test only and does not support a general factuality accuracy claim. Full JSON output is in `data/factuality_results_codex.json`.
