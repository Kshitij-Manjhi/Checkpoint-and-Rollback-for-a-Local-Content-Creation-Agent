# Approved-draft working memory

DraftGuard can now revise the last approved draft instead of treating every generation as a blank-page task.

## How it works

- The writing UI has **Continue from an approved draft**, enabled by default.
- By default, the backend uses the latest integrity-verified, human-approved draft for the current task.
- To choose a particular older draft, select it in **Draft baseline for next generation** in the writing studio, or use **Use as next baseline** next to that approved version in **Versions & recovery**.
- An explicit selection is checked server-side: it must belong to the current task, be marked safe, have an `approved` review status, contain a non-empty draft, and pass snapshot integrity checks (plus Git artifact verification when configured). A pending/rejected/other-task/corrupt version is rejected.
- The selected draft's text is supplied to the generator as editable working context alongside the user's new instructions. The normal version chain still records new output as a new checkpoint; selecting a baseline does not delete later versions.
- The model is instructed to preserve unaffected material, apply the requested changes, return the complete updated draft, and not treat prior claims as independently verified facts.
- Only approved/safe checkpoints are used as memory. A new unreviewed or rejected draft is not automatically promoted to the baseline.
- The generated checkpoint records whether prior-draft memory was included, the exact baseline checkpoint ID, and whether selection was `explicit` or `latest_approved` in `execution_context`; the audit event records the same provenance.
- Turning the UI checkbox off sends `continue_from_previous: false` and starts without that baseline.

This is **task-scoped checkpoint memory**, not general conversation-history memory. It does not automatically include unsubmitted edits currently sitting in the browser editor; approve the version you want to become the baseline first.

## Verification

- Backend: `AGENT_RUNTIME_STORAGE=sqlite PYTHONPATH=src python -m pytest -q` — 99 passed, 4 skipped.
- Frontend: `node --test tests/*.test.js` — 2 passed.
- The backend suite was run in explicit SQLite test mode; this does not replace a live MySQL + Git + ChromaDB + Ollama integration test.

## User-selected baseline behavior

The baseline selector is independent of rollback selection. **Recover** loads an older safe checkpoint into the editor; **Use as next baseline** selects an approved checkpoint as working context for a future generation. The latter does not itself roll back the editor. The selection is held in the current browser session; verify the status message before generating.
