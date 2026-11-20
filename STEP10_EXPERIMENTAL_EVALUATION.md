# DraftGuard Step 10 — Experimental Evaluation

## Purpose

Step 10 measures the existing deterministic checkpoint/rollback runtime under repeatable synthetic workloads. It is intended to provide reproducible engineering measurements, not to claim that the system is a validated real-world safety classifier.

## Reproduce the experiment

From `Backend/`:

```powershell
python run_evaluation.py --repetitions 10 --output data/step10_experiment.sqlite3
```

The command writes a SQLite experiment database and a JSON report beside it. Generated `data/` output should remain local and should not be committed unless intentionally included as research evidence.

## Scenario set

Each repetition runs five scenarios:

- `quality_degradation`: a candidate fails the deterministic minimum-length rule.
- `policy_violation`: a candidate contains one of the test policy markers.
- `corrupted_snapshot`: the newest safe snapshot is corrupted; recovery should fall back to an older verified safe checkpoint.
- `missing_checkpoint`: no safe checkpoint exists; the runtime should report the condition rather than inventing a recovery.
- `healthy_candidate`: a negative control should not trigger rollback.

The policy and quality checks are transparent synthetic oracles. They are not general-purpose content quality or safety judgements.

## Metric definitions

- **Recovery success rate:** successful exact restoration divided by failure trials where a safe baseline existed. `missing_checkpoint` is reported separately because no recovery target exists.
- **Rollback latency:** elapsed time for an actual rollback attempt, including SQLite lookup, integrity verification and audit writes. Healthy controls do not have a rollback latency.
- **False rollback rate:** rollback triggers on healthy negative-control trials divided by all healthy controls.
- **Lost-work percentage:** character-count proxy for candidate content discarded by a successful rollback. This is intentionally simple and must not be interpreted as semantic work loss.
- **Post-recovery quality:** deterministic synthetic quality score of the restored state, not a human or semantic evaluation score.
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

These values are from one environment-specific run and may vary, especially latency. The no-checkpoint baseline simply leaves the failed candidate as the active state; it is a controlled comparator, not a production implementation. The 100% lost-work proxy means the post-checkpoint candidate text was discarded during successful rollback in these synthetic failure cases. It does not measure human effort or semantic value.

## Interpretation and limitations

- The experiment validates the mechanics of checkpoint selection, integrity verification and fallback under these specific test conditions.
- It does not establish real-world AI safety, factual correctness, or the quality of Qwen-generated content.
- The candidate evaluator uses deterministic marker/length rules to make tests repeatable.
- A single run is not enough for strong performance claims. Repeat on the target machine and report the distribution (median and p95 latency) for a final research report.
- Future work should compare against human-labelled content and include a more realistic measure of work lost, such as accepted edits since the last safe checkpoint.
