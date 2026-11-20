# DraftGuard Low-Level Design

## Checkpoint record

The metadata row contains:

- `checkpoint_id`, `task_id`, `sequence`, `parent_checkpoint_id`
- `snapshot_json`, `snapshot_sha256`, `is_safe`, `created_at`
- `artifact_commit_hash` — the Git commit containing the corresponding snapshot version

MySQL creates the commit-hash column for new installations and adds it non-destructively to existing checkpoint tables when missing. SQLite mirrors this field for test and development runs. Audit rows include artifact versioning, review, rejection, rollback, and failure outcomes.

## Git artifact format

Each checkpoint version is a JSON object at `artifacts/<checkpoint_id>.json`:

```json
{
  "checkpoint_id": "<id>",
  "sha256": "<SHA-256 of canonical state JSON>",
  "state": { "...": "..." }
}
```

Canonical state JSON uses sorted keys, compact separators, UTF-8, and no ASCII escaping. The payload file is committed with a checkpoint-specific message. Review updates create a later commit for the same artifact path; older snapshots remain readable by their recorded commit hashes. Git identity is configured locally for the dedicated repository if missing.

## Checkpoint creation

1. Validate the task ID and JSON-serializability of state.
2. Produce canonical JSON and SHA-256.
3. Write the artifact file and stage only that path.
4. Commit it; verify that `HEAD` resolves to a commit and that this commit contains the expected path.
5. Insert checkpoint metadata, including the commit hash, and its creation audit row in one metadata transaction.
6. If the Git commit fails, do not insert a checkpoint and attempt to append `ARTIFACT_COMMIT_FAILED` to metadata. If metadata insertion fails after Git succeeds, the commit is unreferenced and is not treated as a valid checkpoint.

The Git repository has no automatic remote push. DraftGuard does not invoke `reset`, `checkout`, `push`, or `--force` for checkpoint or restore operations.

## Human approval

1. Load the checkpoint metadata and verify the stored snapshot digest.
2. Construct the reviewed state by adding approval status, reviewer, reason, and timestamp.
3. Compute the reviewed digest and commit the reviewed artifact version.
4. In a single metadata transaction, update state/digest/commit hash/safety flag and add the review audit event.
5. If the Git commit fails, preserve the pending metadata state and record `ARTIFACT_REVIEW_COMMIT_FAILED` when metadata is reachable.

The API rejects repeat reviews and prevents an automated policy rejection from becoming approved through the normal review route.

## Rollback and validation

For each safe checkpoint, newest-first:

1. Recompute the metadata snapshot digest; require valid JSON object state.
2. Require a recorded commit hash. Read `artifacts/<checkpoint_id>.json` from that exact commit.
3. Require matching checkpoint ID, object-shaped state, valid artifact digest, expected digest, and equality between Git state and metadata state.
4. Write canonical state plus a trailing newline to `working/<sha256(task_id)>.json` through a temporary file in the same directory, flush it, then atomically replace the working file.
5. Recompute the canonical state digest after writing. Return success and record the Git hash, restored path/digest, and latency only after validation passes.

Missing commits, unreadable or malformed JSON, mismatched IDs/digests/state, and failed writes are rejected and audited. The manager tries older safe candidates. If none pass, it records rollback failure and raises an integrity error. No Git ref or commit history is changed by restore.

## MySQL/Git consistency and failure cases

| Failure point | Result |
|---|---|
| Git commit fails during checkpoint creation | No checkpoint row is inserted; failure audit is attempted in metadata |
| Git succeeds, metadata insert fails | Unreferenced commit may remain; it is not eligible for rollback |
| Git commit fails during approval | Existing pending state remains authoritative; approval is not published |
| Metadata approval transaction fails after Git commit | New commit may remain unreferenced; checkpoint is not marked safe |
| Recorded Git commit is missing/corrupt | Candidate is rejected; older approved candidate is tried |
| Restored-file validation/write fails | Candidate is rejected; older approved candidate is tried; success is not reported |
| No candidate validates | Rollback fails closed and records the failure if metadata is available |

## Tests and integration

The automated suite exercises the checkpoint manager with SQLite metadata plus a real temporary Git repository, including approved version commits, modified working artifact recovery, exact content/digest verification, missing references, and failed commits. Deployment integration also depends on a reachable MySQL instance, an installed Git executable, and ChromaDB. Ollama smoke tests are live checks and should be reported separately from the test suite.
