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

For the configured MySQL/Git/Chroma/Ollama deployment, follow `Backend/README.md`
for the required `.env` values before starting the backend. To run factuality
checks in the generation workflow, enable them in the backend process:

```powershell
$env:AGENT_FACTUALITY_EVALUATION = "1"
$env:AGENT_FACTUALITY_MODEL = "qwen3:4b"
python -m uvicorn agent_runtime.api:app --host 127.0.0.1 --port 8000
```

Start the frontend in a separate terminal:

```powershell
cd Frontend
python -m http.server 5500 --bind 127.0.0.1
```

## Demonstrate factuality and safe recovery

1. In the writing studio, create a workspace and add one or more source passages
   in **Evidence sources**. The page confirms whether the existing evidence API
   indexed each passage.
2. Select a discovered Ollama model and generate a draft. Review the actual
   factuality verdicts and cited passages under **Factuality evaluation**.
   `Not run` means the workflow did not return an evaluation; an evaluator error
   is displayed as **Failed**.
3. Open **Versions & recovery**, inspect the candidate’s integrity, Git commit,
   digest, and review status, then approve only after human review.
4. Generate a later draft to simulate a new update. Choose **Recover safe
   version**, confirm the action, and check that the response reports integrity
   verification. The audit view shows the recorded rollback outcome.
5. Use **Audit & evaluation** to run the deterministic recovery experiment and
   inspect its returned trials. This is distinct from `pytest` and the local
   factuality smoke benchmark.

The interface includes a writing-first composer, word and prompt counters, local model selection, human review dialog, checkpoint inspection, rollback, and audit/evaluation views. The backend allows CORS only for the expected localhost development origins. Keep both services bound to localhost. This is a research prototype: task title history is held in browser session memory/API process memory, reviewer names are not authenticated, and automated checks do not guarantee factual correctness.

Generated versions now display an automatic quality panel with a weighted score,
per-dimension ratings, rule findings, and semantic-evaluator status. When the
backend uses real Ollama and generation mode is `ollama`, semantic scoring is on
by default; configure `AGENT_SEMANTIC_EVALUATION=0` to use deterministic checks
only. The score is advisory and does not bypass the human approval gate.

## Testing and result views

The **Audit & evaluation** view calls `/api/health`, `/api/health/integrations`,
`/api/models`, `/api/tasks/{task_id}/events`, and `/api/evaluations/run`. MySQL,
ChromaDB, and Git statuses come from read-only checks against the configured
adapters. A service marked **Not configured** is not enabled for the current
backend mode; a failed check is displayed as **Unavailable**. Git health checks
repository access and report whether a commit exists, without changing history.

The recovery experiment view renders the returned synthetic scenario trials,
outcomes, and rollback timings. It is not the automated pytest suite or the
factuality benchmark. Unit tests remain a backend terminal operation:

```powershell
cd Backend
python -m pytest -q
```

The fixed factuality smoke benchmark also remains a backend CLI operation:

```powershell
cd Backend
python run_factuality_benchmark.py --model qwen3:4b --timeout 180
```

Generated factuality evaluations appear in the writing studio and when inspecting
their checkpoint. The UI displays the actual verdict, cited evidence IDs and
passages, model, evaluator version, and explicit errors from the checkpoint data.
It does not substitute benchmark numbers or treat evaluation errors as passes.
Use the **Evidence sources** form within a workspace to send passages through the
existing `POST /api/tasks/{task_id}/evidence` route; an explicit API success is
shown before generation. Evidence remains task-scoped. Factuality extraction
must return at least one claim. An empty claim list is unscored and requires
human review; it is not reported as a completed check.
The checkpoint table displays the Git commit, snapshot SHA-256, timestamp, review
state, integrity state, and recovery eligibility returned by the backend API.
