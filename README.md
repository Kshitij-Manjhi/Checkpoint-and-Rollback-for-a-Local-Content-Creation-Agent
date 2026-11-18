# DraftGuard — Responsible Content Creation Prototype

The project is split into two sibling folders:

- `Backend/` — Python, FastAPI, SQLite checkpoint store, quality/policy checks, human review, audit events, and rollback.
- `Frontend/` — independent, responsive minimal writing studio with a focused editor, review dialog, checkpoint history, and audit/evaluation views.

## Start the backend

```powershell
cd Backend
python -m pip install -e ".[test]"
python -m pytest -v
python -m uvicorn agent_runtime.api:app --host 127.0.0.1 --port 8000
```

## Start the frontend (separate terminal)

```powershell
cd Frontend
python -m http.server 5500 --bind 127.0.0.1
```

Open http://127.0.0.1:5500. The frontend's backend URL is configured in `Frontend/config.js`. API docs remain at http://127.0.0.1:8000/docs.

## Architecture

The frontend uses plain HTML, CSS, and JavaScript with no build step or external runtime dependencies. It does not import Python or live inside the backend. It sends JSON HTTP requests to FastAPI. CORS is restricted to the configured local development origins. Keep both services on localhost; this prototype has no authentication or user isolation. The deterministic quality checks are intentionally narrow and do not guarantee factual correctness or absence of harm.
