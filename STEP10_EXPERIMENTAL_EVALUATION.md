# DraftGuard Step 10 — Experimental Evaluation

## Purpose

Step 10 measures the existing deterministic checkpoint/rollback runtime under repeatable synthetic workloads. It is intended to provide reproducible engineering measurements, not to claim that the system is a validated real-world safety classifier.

## Reproduce the experiment

From `Backend/` (deployment mode uses configured MySQL and an isolated temporary Git repository):

```powershell
python run_evaluation.py --repetitions 10 --output runtime_data/step10-report.sqlite3
```

The API experiment endpoint uses the configured SQL metadata store (MySQL in deployment) and saves a JSON report. The standalone CLI uses SQLite only when explicitly selected as a development/test adapter; reports and runtime artifacts should remain local unless intentionally included as research evidence.

## Scenario set

Each repetition runs the configured scenario set, including:

- `quality_degradation`: a candidate fails the deterministic minimum-length rule.
- `policy_violation`: a candidate contains one of the test policy markers.
- `corrupted_snapshot`: the newest safe snapshot is corrupted; recovery should fall back to an older verified safe checkpoint.
- `missing_checkpoint`: no safe checkpoint exists; the runtime should report the condition rather than inventing a recovery.
- `healthy_candidate`: a negative control should not trigger rollback.
- `unsupported_claim` and `generation_interrupted`: factuality and interrupted-generation paths, reported as unmeasured when the relevant evaluator is unavailable.
- `corrupted_checkpoint_artifact`, `missing_checkpoint_artifact`, and `failed_rollback_integrity`: Git artifact reference and restore failure paths.

The policy and quality checks are transparent synthetic oracles. They are not general-purpose content quality or safety judgements.

## Metric definitions

- **Recovery success rate:** successful exact restoration divided by failure trials where a safe baseline existed. `missing_checkpoint` is reported separately because no recovery target exists.
- **Rollback latency:** elapsed time for a real rollback attempt; mean and median are reported, and p95 is shown only with at least 20 observations. Healthy controls do not have a rollback latency.
- **False rollback rate:** observed rollback triggers on explicitly labelled healthy controls divided by the number of those controls.
- **Lost work:** characters in the discarded candidate divided by candidate characters in trials with a measured successful recovery. It is a text-length proxy, not human effort or semantic value.
- **Post-recovery quality:** the application's deterministic quality evaluator score for the restored draft; it is not a human or semantic evaluation score.
- **Corrupt-checkpoint fallback rate:** share of corrupted-newest-checkpoint trials where an older verified safe checkpoint was restored.
- **Missing-checkpoint detection rate:** share of missing-checkpoint trials that return the expected explicit error.

## Reference run

A local deterministic run with 10 repetitions per scenario produced 50 trials:

| Metric | No-checkpoint baseline | DraftGuard checkpointed |
|---|---:|---:|
| Recoverable failure trials | 30 | 30 |
| Recovery success rate | 0.0% | 100.0% |
| Mean rollback latency | N/A | 1.3542 ms |
| False rollback rate on healthy controls | 0.0% | 0.0% |
| Mean lost-work proxy | 0.0% | 100.0% |
| Mean post-failure/recovery synthetic quality | 0.6611 | 0.5417 |
| Corrupt-newest fallback rate | N/A | 100.0% |
| Missing-checkpoint detection rate | N/A | 100.0% |

The checked-in reference values are historical and are not automatically presented as current results. Use the dashboard's run history and JSON report for the actual run, including its scenario outcomes, Git commit references, denominators, and unavailable metrics. The no-checkpoint baseline is a controlled comparator, not a production implementation.

## Interpretation and limitations

- The experiment validates the mechanics of checkpoint selection, integrity verification and fallback under these specific test conditions.
- It does not establish real-world AI safety, factual correctness, or the quality of Qwen-generated content.
- The candidate evaluator uses deterministic marker/length rules to make tests repeatable.
- p95 is omitted for fewer than 20 measured rollbacks. A single run is not enough for strong performance claims.
- Future work should compare against human-labelled content and include a more realistic measure of work lost, such as accepted edits since the last safe checkpoint.
