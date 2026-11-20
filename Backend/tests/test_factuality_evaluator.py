import json
import pytest

from agent_runtime.factuality_evaluator import EvidenceFactualityEvaluator
from agent_runtime.quality_policy import Decision, QualityPolicyEvaluator


class FakeRetrieval:
    def __init__(self, items):
        self.items = items
    def query(self, text, *, task_id, limit=3):
        return self.items


class SequenceProvider:
    def __init__(self, responses):
        self.responses = list(responses)
    def generate(self, model, prompt, *, system=None):
        return self.responses.pop(0)


def extraction(*claims):
    return json.dumps({"claims": [{"claim": c} for c in claims]})


def verification(verdict, ids=("source-1",), reason="Matched against evidence"):
    return json.dumps({"verdict": verdict, "evidence_ids": list(ids), "reason": reason})


def test_supported_claim_must_cite_retrieved_evidence():
    provider = SequenceProvider([
        extraction("The project uses SHA-256 to verify checkpoint integrity."),
        verification("supported", ["source-1"]),
    ])
    retrieval = FakeRetrieval([{"id": "source-1", "kind": "evidence",
                                "text": "Checkpoint snapshots are hashed using SHA-256.",
                                "score": 0.9}])
    result = EvidenceFactualityEvaluator(provider, "fake-model", retrieval).evaluate(
        "Explain checkpoint integrity", "The project uses SHA-256 to verify checkpoint integrity.",
        task_id="task-1")
    assert result["status"] == "ok"
    assert result["counts"]["supported"] == 1
    assert result["claims"][0]["evidence"][0]["id"] == "source-1"
    assert result["claims"][0]["cited_source_ids"] == ["source-1"]
    assert result["claims"][0]["retrieval_diagnostics"]["eligible_passage_count"] == 1


def test_live_style_provider_is_asked_for_schema_constrained_json():
    class StructuredProvider(SequenceProvider):
        def __init__(self, responses):
            super().__init__(responses)
            self.schemas = []
        def generate_json(self, model, prompt, *, system=None, schema=None):
            self.schemas.append(schema)
            return self.generate(model, prompt, system=system)

    provider = StructuredProvider([
        extraction("Git stores versioned artifacts."),
        verification("supported", ["source-1"]),
    ])
    retrieval = FakeRetrieval([{"id": "source-1", "kind": "evidence",
                                "text": "Git stores checkpoint artifacts.", "score": 0.9}])
    result = EvidenceFactualityEvaluator(provider, "fake-model", retrieval).evaluate(
        "Explain Git", "Git stores versioned artifacts.", task_id="task-1")
    assert result["status"] == "ok"
    assert provider.schemas[0]["required"] == ["claims"]
    assert provider.schemas[0]["properties"]["claims"]["minItems"] == 1
    assert set(provider.schemas[1]["required"]) == {"verdict", "evidence_ids", "reason"}


def test_contradicted_claim_is_recorded_with_evidence():
    provider = SequenceProvider([
        extraction("The project stores passwords in plaintext."),
        verification("contradicted", ["source-1"], "The source says credentials are not stored."),
    ])
    retrieval = FakeRetrieval([{"id": "source-1", "kind": "evidence",
                                "text": "The application does not store user passwords.",
                                "score": 0.8}])
    result = EvidenceFactualityEvaluator(provider, "fake-model", retrieval).evaluate(
        "Describe storage", "The project stores passwords in plaintext.", task_id="task-1")
    assert result["claims"][0]["verdict"] == "contradicted"
    base = QualityPolicyEvaluator(min_characters=1).evaluate("Describe storage", "A sufficiently long draft for a policy test.")
    combined = QualityPolicyEvaluator(min_characters=1).combine_factuality(base, result)
    assert combined.decision == Decision.REJECT
    assert "CONTRADICTED_FACTUAL_CLAIM" in combined.reasons


def test_missing_evidence_never_becomes_supported():
    provider = SequenceProvider([extraction("A claim without indexed evidence.")])
    retrieval = FakeRetrieval([])
    result = EvidenceFactualityEvaluator(provider, "fake-model", retrieval).evaluate(
        "Check facts", "A claim without indexed evidence.", task_id="task-1")
    assert result["claims"][0]["verdict"] == "insufficient_evidence"
    base = QualityPolicyEvaluator(min_characters=1).evaluate("Check facts", "A sufficiently long draft that needs review.")
    combined = QualityPolicyEvaluator(min_characters=1).combine_factuality(base, result)
    assert combined.decision == Decision.NEEDS_HUMAN_REVIEW
    assert result["claims"][0]["retrieval_diagnostics"]["candidate_count"] == 0


def test_claim_retrieval_reports_threshold_and_source_filter_reasons():
    class DetailedRetrieval(FakeRetrieval):
        def query_detailed(self, text, *, task_id, limit=5):
            return {"items": [{"id": "draft-1", "kind": "draft", "text": "A draft, not source evidence.", "score": .8}],
                    "diagnostics": {"candidate_count": 2, "eligible_passage_count": 1,
                                    "filtered_by_score_count": 1, "filtered_by_kind_count": 0,
                                    "min_score": .5, "candidates": [
                                        {"id": "draft-1", "kind": "draft", "score": .8},
                                        {"id": "weak-1", "kind": "evidence", "score": .3}]}}
    result = EvidenceFactualityEvaluator(SequenceProvider([extraction("A claim.")]), "fake-model",
                                         DetailedRetrieval([])).evaluate(
        "Check facts", "A claim.", task_id="task-1")
    claim = result["claims"][0]
    assert claim["verdict"] == "insufficient_evidence"
    assert claim["retrieval_diagnostics"]["candidate_count"] == 2
    assert claim["retrieval_diagnostics"]["filtered_by_kind_or_empty_count"] == 1
    assert claim["retrieval_diagnostics"]["filtered_by_score_count"] == 1


def test_configured_chroma_adapter_retrieves_demo_claim_and_filters_unrelated_query(tmp_path):
    pytest.importorskip("chromadb")
    from agent_runtime.retrieval_store import LocalEvidenceIndex
    source = ("DraftGuard stores checkpoint artifacts in Git and uses MySQL to maintain structured metadata and audit records. "
              "Each checkpoint records its Git commit hash in the metadata. During recovery, DraftGuard validates the recorded "
              "Git commit and the artifact's SHA-256 digest before restoring the approved content.")
    index = LocalEvidenceIndex(tmp_path / "demo-chroma")
    index.add(source, task_id="demo-task", kind="evidence", reference_id="Evidence_source_1")
    supported = index.query_detailed("Each checkpoint records its Git commit hash in the metadata.",
                                     task_id="demo-task", limit=5)
    claim_queries = [
        "Git stores and versions checkpoint artifacts.",
        "MySQL records checkpoint metadata and audit events.",
        "During recovery DraftGuard verifies the recorded Git commit and SHA-256 digest.",
    ]
    claim_results = [index.query_detailed(query, task_id="demo-task", limit=5)
                     for query in claim_queries]
    unrelated = index.query_detailed("What is the population of France?", task_id="demo-task", limit=5)
    unindexed = index.query_detailed("The source mentions a different task.", task_id="empty-task", limit=5)
    assert supported["items"] and supported["items"][0]["id"] == "Evidence_source_1"
    assert supported["diagnostics"]["eligible_passage_count"] == 1
    assert all(result["items"] and result["items"][0]["id"] == "Evidence_source_1"
               for result in claim_results)
    assert unrelated["items"] == []
    assert unrelated["diagnostics"]["filtered_by_score_count"] >= 1
    assert unindexed["diagnostics"]["candidate_count"] == 0


def test_chroma_retrieval_reaches_verifier_with_original_source_id(tmp_path):
    pytest.importorskip("chromadb")
    from agent_runtime.retrieval_store import LocalEvidenceIndex
    index = LocalEvidenceIndex(tmp_path / "verifier-chroma")
    index.add("Git stores checkpoint artifacts as versioned commits.", task_id="verify-task",
              kind="evidence", reference_id="git-source-9")
    provider = SequenceProvider([extraction("Git stores checkpoint artifacts as versioned commits."),
                                 verification("supported", ["git-source-9"])])
    result = EvidenceFactualityEvaluator(provider, "fake-model", index).evaluate(
        "Explain storage", "Git stores checkpoint artifacts as versioned commits.", task_id="verify-task")
    claim = result["claims"][0]
    assert claim["verdict"] == "supported"
    assert claim["cited_source_ids"] == ["git-source-9"]
    assert claim["evidence"][0]["id"] == "git-source-9"


def test_empty_claim_extraction_is_unscored_and_routes_to_review():
    provider = SequenceProvider([json.dumps({"claims": []})])
    result = EvidenceFactualityEvaluator(provider, "fake-model", FakeRetrieval([])).evaluate(
        "Check the factual claims", "DraftGuard stores checkpoint data in MySQL.", task_id="task-1")
    assert result["status"] == "error"
    assert result["claim_count"] == 0
    assert "unscored" in result["reasons"][0]
    base = QualityPolicyEvaluator(min_characters=1).evaluate(
        "Check the factual claims", "DraftGuard stores checkpoint data in MySQL.")
    combined = QualityPolicyEvaluator(min_characters=1).combine_factuality(base, result)
    assert combined.decision == Decision.NEEDS_HUMAN_REVIEW


def test_verifier_cannot_cite_an_unknown_evidence_id():
    provider = SequenceProvider([
        extraction("A factual claim."),
        verification("supported", ["not-a-real-source"]),
    ])
    retrieval = FakeRetrieval([{"id": "source-1", "kind": "evidence", "text": "Related source text.", "score": 0.9}])
    result = EvidenceFactualityEvaluator(provider, "fake-model", retrieval).evaluate(
        "Check facts", "A factual claim.", task_id="task-1")
    assert result["claims"][0]["verdict"] == "insufficient_evidence"
    assert result["claims"][0]["evidence"] == []


def test_non_evidence_retrieval_items_are_not_used_as_sources():
    provider = SequenceProvider([extraction("A claim from the draft.")])
    retrieval = FakeRetrieval([{"id": "draft-1", "kind": "draft", "text": "The same claim repeated.", "score": 0.99}])
    result = EvidenceFactualityEvaluator(provider, "fake-model", retrieval).evaluate(
        "Check facts", "A claim from the draft.", task_id="task-1")
    assert result["claims"][0]["verdict"] == "insufficient_evidence"


def test_malformed_model_response_fails_closed():
    provider = SequenceProvider(["not json"])
    result = EvidenceFactualityEvaluator(provider, "fake-model", FakeRetrieval([])).evaluate(
        "Check facts", "Some draft text.", task_id="task-1")
    assert result["status"] == "error"


def test_retrieval_failure_is_not_reported_as_insufficient_evidence():
    class BrokenRetrieval:
        def query(self, text, *, task_id, limit=3):
            raise ConnectionError("Chroma unavailable")
    provider = SequenceProvider([extraction("A factual claim.")])
    result = EvidenceFactualityEvaluator(provider, "fake-model", BrokenRetrieval()).evaluate(
        "Check facts", "A factual claim.", task_id="task-1")
    assert result["status"] == "error"
    assert result["claims"] == []
    assert "Chroma unavailable" in result["reasons"][0]


def test_provider_failure_preserves_actionable_diagnostic_and_no_verdict():
    class FailedProvider:
        def generate(self, model, prompt, *, system=None):
            from agent_runtime.model_provider import OllamaUnavailableError
            raise OllamaUnavailableError("connection refused at http://localhost:11434/api/generate")

    result = EvidenceFactualityEvaluator(FailedProvider(), "qwen3:4b", FakeRetrieval([])).evaluate(
        "Check facts", "Some factual claim.", task_id="task-1")
    assert result["status"] == "error"
    assert result["claims"] == []
    assert result["claim_count"] == 0
    assert result["counts"] == {}
    assert "connection refused" in result["reasons"][0]


def test_checkpoint_secret_patterns_are_redacted_without_losing_state_shape(tmp_path):
    from agent_runtime import CheckpointManager
    from agent_runtime.store import SQLiteCheckpointStore
    manager = CheckpointManager(SQLiteCheckpointStore(tmp_path / "secret-redaction.sqlite3"))
    checkpoint = manager.create_checkpoint("secret-task", {
        "prompt": "Explain API_KEY=sk-12345678901234567890 and preserve the task.",
        "draft": "Authorization: Bearer abc.def.ghi", "context": {"password": "supersecret"},
    }, is_safe=True)
    state = manager.store.get_checkpoint(checkpoint.checkpoint_id)["state"]
    assert "sk-12345678901234567890" not in state["prompt"]
    assert "Bearer abc.def.ghi" not in state["draft"]
    assert state["context"]["password"] == "[REDACTED]"
    assert "preserve the task" in state["prompt"]
