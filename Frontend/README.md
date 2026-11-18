# DraftGuard Frontend

This is a separate, lightweight frontend project. It talks to the FastAPI backend over HTTP; the UI is not served from the backend package.

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

The backend allows CORS only for the expected localhost development origins. Keep both services bound to localhost. This is a research prototype: task title history is held in browser session memory/API process memory, reviewer names are not authenticated, and automated checks do not guarantee factual correctness.
