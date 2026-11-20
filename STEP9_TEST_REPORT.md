# DraftGuard Step 9 — Persistence and Reliability Test Report

## Scope

- Persist task IDs, titles, and creation timestamps in SQLite.
- Confirm task/checkpoint visibility after constructing a fresh API app against the same SQLite database.
- Skip a corrupt or empty newest safe checkpoint and restore an older integrity-verified safe checkpoint.
- Fail closed when no safe checkpoint has a valid snapshot.
- Preserve existing generation, evaluation, review, and rollback API paths.

## Results

- Backend tests: **41 passed**.
- Python compilation: passed.
- Frontend JavaScript syntax check: passed.
- Tests use temporary SQLite databases and deterministic fake providers.
- Live Ollama/Qwen and live browser integration were not run as part of this validation.

## Reliability behavior

Rollback now examines safe checkpoints from newest to oldest. Each candidate must have a non-empty JSON snapshot whose SHA-256 digest matches the stored digest and whose root value is an object. Invalid candidates produce a `ROLLBACK_CANDIDATE_REJECTED` audit event. A valid older candidate may be restored. If all safe candidates are invalid, the operation returns an integrity error instead of restoring unverified content.

Task metadata is persisted in the `tasks` SQLite table. Existing databases are initialized non-destructively, and older task IDs that have checkpoints but no task row are reconstructed with the task ID as a fallback title.

## Limitations

- Task titles and creation timestamps persist; full multi-turn workspace memory has not yet been implemented.
- SQLite is intended for this local prototype. Authentication, multi-user authorization, backup/restore procedures, and production deployment hardening remain out of scope.
- The test suite does not prove resilience to disk failure or malicious modification of both snapshot data and its digest.
