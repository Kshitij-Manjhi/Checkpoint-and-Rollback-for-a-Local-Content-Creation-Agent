import json

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
