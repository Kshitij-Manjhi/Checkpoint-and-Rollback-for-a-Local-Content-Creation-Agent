# DraftGuard recovery, quality and factuality fixes

## Changes

- **Lost-work percentage:** Counts inserted/replaced word-token occurrences from the candidate draft, estimates which remain above baseline frequency in the restored artifact, and aggregates only trials where rollback was triggered and a measurable restored artifact exists. Healthy controls no longer dilute the loss denominator. This remains a lexical proxy, not a semantic measure of work.
- **False rollback triggers:** Keeps the denominator limited to pre-labelled healthy negative-control trials. A trigger is counted as false only when the system actually triggers rollback on a healthy control. Reported as numerator / healthy-control denominator.
- **Post-recovery quality:** Reports the quality score on integrity-verified restored content, compares it with baseline quality, and now includes the count/rate of restored drafts that pass the configured quality policy. The score is heuristic and does not prove factual correctness.
- **Quality/policy detection:** Recovery trials now apply both the configured quality evaluator and the transparent deterministic experiment oracle. Additional explicit policy markers cover credential theft/exfiltration, API-key disclosure, system-prompt disclosure, safety-check disabling, and authentication bypass. Hard policy findings cannot be overridden by quality scores.
- **Factuality evaluation:** The controlled unsupported-claim experiment now adds clearly labeled, task-scoped synthetic evidence before evaluating its intentionally false password-storage claim. Factuality verdicts still require cited evidence IDs; missing retrieval remains `insufficient_evidence`, not a fabricated supported/contradicted verdict. The evaluator already reports candidate counts, eligibility/filter reasons, and source IDs, and tests now cover empty retrieval and the evidence-backed contradiction path.
- **UI:** Post-recovery quality card now includes policy-pass count and rate alongside the score/baseline delta.

## Validation

- Backend tests: **90 passed, 4 skipped** with `AGENT_RUNTIME_STORAGE=sqlite` and `PYTHONPATH=src`.
- Frontend tests: **2 passed**.
- `node --check Frontend/app.js`: passed.
- A one-repetition synthetic recovery experiment completed with 10 trials. In that run, recovery success was 5/5 eligible attempts, false rollback triggers were 0/1 healthy controls, lost work was 100% (39/39 measured introduced tokens), and post-recovery quality was 63.15/100 with 5/5 restored drafts passing the configured policy.

## Limitations

- These are local automated tests and synthetic experiment results, not a live MySQL/Ollama/Chroma integration run.
- Factuality evaluation still depends on retrieval quality and the local model's claim extraction/verdict. The synthetic evidence used by the controlled unsupported-claim scenario is test fixture data, not a production source.
- Word-token lost work cannot measure semantic loss, importance, or user effort.
- Deterministic policy markers are guardrails, not a complete safety classifier; human review remains required.
