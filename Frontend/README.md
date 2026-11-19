# DraftGuard Frontend

This is a separate, responsive minimal writing studio built with plain HTML, CSS, and JavaScript. No frontend package installation or build step is required. It talks to the FastAPI backend over HTTP; the UI is not served from the backend package.

## Run locally

1. Start the backend in one terminal:

   ```powershell
   cd Backend
   python -m pip install -e ".[test]"
   python -m uvicorn agent_runtime.api:app --host 127.0.0.1 --port 8000
   ```

2. Start the frontend in a second terminal from the project root:

   ```powershell
   cd Frontend
   python -m http.server 5500 --bind 127.0.0.1
   ```

3. Open http://127.0.0.1:5500. The frontend calls http://127.0.0.1:8000. To change the backend URL, edit `config.js`.

The interface includes a writing-first composer, word and prompt counters, local model selection, human review dialog, checkpoint inspection, rollback, and audit/evaluation views. The backend allows CORS only for the expected localhost development origins. Keep both services bound to localhost. This is a research prototype: task title history is held in browser session memory/API process memory, reviewer names are not authenticated, and automated checks do not guarantee factual correctness.

Generated versions now display an automatic quality panel with a weighted score,
per-dimension ratings, rule findings, and semantic-evaluator status. When the
backend uses real Ollama and generation mode is `ollama`, semantic scoring is on
by default; configure `AGENT_SEMANTIC_EVALUATION=0` to use deterministic checks
only. The score is advisory and does not bypass the human approval gate.
