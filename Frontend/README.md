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

The interface includes a writing-first composer, word and prompt counters, local model selection, human review dialog, checkpoint inspection, rollback, service checks, and audit/evaluation views. The backend allows CORS only for the expected localhost development origins. Keep both services bound to localhost. This is a research prototype: task title history is held in browser session memory/API process memory, reviewer names are not authenticated, and automated checks do not guarantee factual correctness.

Generated versions now display an automatic quality panel with a weighted score,
per-dimension ratings, rule findings, and semantic-evaluator status. When the
backend uses real Ollama and generation mode is `ollama`, semantic scoring is on
by default; configure `AGENT_SEMANTIC_EVALUATION=0` to use deterministic checks
only. The score is advisory and does not bypass the human approval gate.

## Testing and result views

The **Audit & evaluation** view calls `/api/health`, `/api/health/integrations`,
`/api/models`, `/api/tasks/{task_id}/events`, `/api/evaluations/run`, and the
evaluation history/report routes. MySQL checks confirm connectivity and required
tables, Chroma checks the configured persistent collection, and Git checks the
configured artifact repository. These are availability checks; the Chroma count
does not prove that a particular query will retrieve relevant evidence.

Use **Recovery Metrics** to inspect the selected persisted run. It shows all five
metrics with units, denominators, definitions, and unavailable reasons. The run
table includes expected versus actual outcomes and checkpoint references. Reports
are retrieved from SQL and can be opened as JSON. Recovery experiments are
controlled synthetic trials and are distinct from `pytest` and factuality tests.

**Run automated tests** invokes a fixed `python -m pytest -q` command with
isolated temporary test storage and a timeout; no command or test path is accepted
from the browser. **Run factuality benchmark** uses the fixed smoke dataset, a
selected installed Ollama model, and a temporary Chroma collection. Both save
their actual status and output in the configured SQL store. These diagnostic runs
do not use the user's task evidence collection.

Generated factuality evaluations appear in the writing studio and checkpoint
inspection. The UI displays actual verdicts, cited evidence IDs/passages, claim
retrieval candidate counts, threshold-filter counts, eligible passage counts,
model/evaluator identifiers, and explicit errors. Empty claim extraction is an
unscored failure and routes the candidate to human review. The UI does not
substitute benchmark results for a live draft evaluation.

**Load Demo Scenario** creates or reuses one reserved demo workspace and indexes
the supplied passage in the configured task-scoped evidence adapter. It fills the
prompt and source fields, but does not generate, approve, or roll back content.
Review and confirm those actions yourself. The demo source ID is stable, so
repeated setup does not duplicate the workspace or passage. There is no reset
button because deleting demo data from a shared local service needs a separate
ownership-safe cleanup path.

Checkpoint inspection identifies the saved plan status/steps, submitted prompt,
retrieved evidence references/excerpts, draft, evaluations, decision, and review
state. Conversation history is deliberately not captured. Common credential
patterns are redacted from snapshot strings; this is not a general-purpose secret
scanner. The checkpoint table displays Git commit, SHA-256, timestamp, review,
integrity, and recovery eligibility returned by the backend.

### Current version indicator and lost-work metric
The Versions & recovery view now displays the checkpoint currently loaded in the editor, including its version number, checkpoint ID, approval state, Git commit prefix, and integrity status when available. Generating a draft, inspecting a checkpoint, or recovering a safe checkpoint updates this indicator. The Recovery Experiment lost-work metric uses word-token sequence alignment: inserted/replaced word tokens in the post-checkpoint candidate are counted as lost if those exact token positions are not retained in the restored artifact. This is a reproducible lexical proxy, not a semantic estimate of the value of work; the report exposes the unit and denominator.

### Opening an evaluation report

In **Audit & evaluation**, run a Recovery experiment or select a saved recovery run in **Historical run**, then choose **Open evaluation report**. The report opens in a readable standalone tab with run metadata, recorded metrics, trial outcomes, and a Print / Save as PDF action. If the browser blocks the new tab, allow pop-ups for the local DraftGuard page and retry. The report uses persisted backend data; missing metrics are shown as not measured.

Advanced prototype controls (Recovery Metrics, Recovery Experiment, automated tests, and factuality benchmark) are grouped under the collapsed **Developer tools** disclosure on the Audit & evaluation page. They remain available when expanded and are not shown by default in the normal demo view.


### Selecting a recovery version

The Versions & recovery view can restore a specific historical version. Use **Choose version to recover** and enter a version number shown in the safe-version list, or use the **Recover** action on an eligible table row. Only checkpoints marked safe with verified snapshot integrity are offered; the backend revalidates eligibility and the recorded Git artifact before writing the selected state. The latest-safe recovery API remains available separately.
