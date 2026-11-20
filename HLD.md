# DraftGuard High-Level Design

## Purpose

DraftGuard is a local-first content workflow that evaluates generated drafts, requires human approval before a draft becomes a rollback target, preserves provenance, and restores a verified approved state after a later failure. This document describes the current implementation and the Git artifact decision.

## System components

| Component | Responsibility | Persistence |
|---|---|---|
| Python API and workflow | Orchestrates generation, evaluation, review, checkpoint creation, and rollback | None as system of record |
| Local Ollama/Qwen provider | Generates content and optionally evaluates semantic quality and factual claims | Model runtime remains local |
| Quality, semantic, factuality evaluators | Apply deterministic policy checks and optional evidence-grounded checks | Findings are recorded as checkpoint/audit metadata |
| Checkpoint manager | Canonicalizes state, hashes snapshots, coordinates Git and metadata writes, selects safe restore candidates | Coordinates stores |
| Git artifact repository | Stores immutable checkpoint JSON versions and provides commit-addressed recovery | Dedicated local Git repository; no remote required |
| MySQL metadata store | Stores tasks, checkpoint sequence/lineage, approval status, content digest, Git commit hash, and audit events | MySQL in deployment |
| Chroma evidence index | Stores task-scoped source/evidence passages and supports retrieval | Local persistent ChromaDB directory |
| Frontend | Displays tasks, generated drafts, checkpoint history, review, rollback, service checks, demo setup, diagnostics, and recovery experiment reports | Calls the local API |

SQLite is retained for isolated development and tests. It is not a deployment fallback and is not an artifact backend. There is no separate local JSON object store.

## High-level data flow

1. A task receives a prompt and optional indexed evidence.
2. The workflow retrieves relevant task-scoped context and generates a candidate.
3. Deterministic quality/policy checks and configured semantic/factuality evaluators produce a decision.
4. The checkpoint manager serializes the state as canonical JSON and computes SHA-256.
5. Git commits `artifacts/<checkpoint_id>.json`; MySQL stores the snapshot metadata and returned commit hash. The candidate is pending or blocked and cannot be restored unless a reviewer approves it.
6. On approval, DraftGuard creates a new Git commit containing reviewer information and the reviewed state. MySQL updates the digest, safety flag, commit hash, and audit record together.
7. On rollback, DraftGuard examines approved candidates newest-first, validates metadata and the exact referenced Git commit, then atomically writes the verified state to a task-specific file in the Git repository's `working/` directory.
8. DraftGuard records verification, selected checkpoint/commit, outcome, and latency in the metadata audit log.

9. The recovery experiment runner executes labelled synthetic failure cases in isolated Git artifact repositories, stores run summaries in the configured SQL metadata store, and exposes history and report JSON to the dashboard. The browser's fixed test runner invokes the existing automated suite with temporary test storage. A separate factuality smoke benchmark uses its own temporary Chroma index; it does not replace checks against task evidence.

10. The reserved demo setup endpoint creates or reuses one demo task and indexes its stable evidence passage. It does not generate, approve, or restore a draft.

## Trust and recovery boundaries

- Git commit hashes identify immutable snapshot versions. The mutable `HEAD` is not used as the authority when restoring a recorded checkpoint.
- MySQL is authoritative for task/checkpoint eligibility and lineage; Git is authoritative for committed artifact bytes. Rollback requires both records to agree.
- Chroma is an evidence retrieval index, not a checkpoint store or proof that a factual claim is correct.
- A model score never grants approval. The human review gate controls rollback eligibility.
- Restore writes a working copy atomically and verifies its digest. It does not reset a branch, rewrite history, delete commits, or push to a remote.
- Git and MySQL do not share a distributed transaction. If Git succeeds and MySQL fails, Git may retain an unreferenced commit. If the Git commit fails, checkpoint creation or review publication is not reported as successful.

## Operational boundaries

The API is a local prototype and has no authentication or tenant isolation. Ollama, live MySQL, and browser UI checks require those services to be available. Automated unit/integration tests use isolated temporary metadata databases and fake model providers; results from those tests do not establish live model accuracy or production resilience. The experiment metrics are labelled synthetic engineering measures, not validated safety or factuality metrics.
