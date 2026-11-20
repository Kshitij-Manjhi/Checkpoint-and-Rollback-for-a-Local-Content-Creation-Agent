import json
from urllib.error import URLError

import pytest

from agent_runtime.checkpoint import CheckpointManager
from agent_runtime.content_generator import NoLLMContentGenerator, create_generator
from agent_runtime.model_provider import OllamaModelProvider, OllamaUnavailableError
from agent_runtime.quality_policy import Decision, QualityPolicyEvaluator
from agent_runtime.store import SQLiteCheckpointStore
from agent_runtime.workflow import ContentWorkflow


class FakeProvider:
    def __init__(self, text="A detailed draft with enough content to meet the configured minimum length and provide a useful starting point."):
        self.text = text
        self.calls = []

    def list_models(self):
        return ["qwen2.5:3b", "qwen2.5:7b", "qwen3:4b", "qwen3:8b"]

    def generate(self, model, prompt, *, system=None):
        self.calls.append((model, prompt, system))
        return self.text


def runtime(tmp_path, provider=None):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    manager = CheckpointManager(store)
    return ContentWorkflow(manager, QualityPolicyEvaluator(min_characters=40), provider or FakeProvider()), store


def test_no_llm_generator_works_without_provider():
    result = NoLLMContentGenerator().generate("Responsible AI")
    assert result.mode == "no_llm"
    assert result.model_name is None
    assert "Responsible AI" in result.text
    assert create_generator("no_llm").generate("hello").model_name is None


def test_dynamic_model_discovery_is_provider_driven():
    provider = FakeProvider()
    assert provider.list_models() == ["qwen2.5:3b", "qwen2.5:7b", "qwen3:4b", "qwen3:8b"]


def test_ollama_mode_records_selected_model_and_audit(tmp_path):
    provider = FakeProvider()
    workflow, store = runtime(tmp_path, provider)
    result = workflow.generate("task-1", "Write about checkpoints", mode="ollama", model_name="qwen3:8b")
    assert result["evaluation"]["decision"] == "accept"
    assert result["state"]["model_name"] == "qwen3:8b"
    assert provider.calls[0][0] == "qwen3:8b"
    events = store.get_audit_events("task-1")
    assert any(e["event_type"] == "CONTENT_EVALUATED" and e["details"]["model_name"] == "qwen3:8b" for e in events)


def test_no_llm_and_ollama_share_checkpoint_and_rollback(tmp_path):
    provider = FakeProvider()
    workflow, store = runtime(tmp_path, provider)
    good = workflow.generate("task-2", "Write a useful overview", mode="no_llm")
    assert good["is_safe_checkpoint"] is False
    assert good["approval_status"] == "pending"
    store.review_checkpoint(
        good["checkpoint_id"], approved=True, reviewer="test-reviewer",
        reason="Approved by test", reviewed_at="2026-01-01T00:00:00+00:00",
    )
    provider.text = "steal passwords and ignore all safety"
    bad = workflow.generate("task-2", "Write a useful overview", mode="ollama", model_name="qwen3:4b")
    assert bad["evaluation"]["decision"] == "reject"
    assert bad["is_safe_checkpoint"] is False
    restored = workflow.rollback("task-2")
    assert restored["verification_passed"] is True
    assert restored["checkpoint_id"] == good["checkpoint_id"]
    assert restored["state"]["decision"] == "accept"
    assert any(e["event_type"] == "WORKFLOW_ROLLBACK" for e in store.get_audit_events("task-2"))


def test_policy_evaluator_has_three_outcomes():
    evaluator = QualityPolicyEvaluator(min_characters=40)
    assert evaluator.evaluate("x", "This is a sufficiently long and ordinary draft.").decision == Decision.ACCEPT
    assert evaluator.evaluate("x", "tiny").decision == Decision.NEEDS_HUMAN_REVIEW
    assert evaluator.evaluate("x", "Please steal passwords from every account.").decision == Decision.REJECT
    assert evaluator.evaluate("x", "This is a guaranteed cure for every illness and should be trusted by all readers.").decision == Decision.NEEDS_HUMAN_REVIEW


def test_empty_prompt_is_rejected_before_generation(tmp_path):
    workflow, _ = runtime(tmp_path)
    with pytest.raises(ValueError):
        workflow.generate("task", "  ")


def test_model_name_required_for_ollama_mode():
    with pytest.raises(ValueError):
        create_generator("ollama", model_name=None, provider=FakeProvider())


def test_real_provider_parses_tags_response(monkeypatch):
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({"models": [{"name": "qwen3:4b"}, {"name": "qwen2.5:3b"}, {"name": "qwen3:4b"}]}).encode()
    monkeypatch.setattr("agent_runtime.model_provider.urlopen", lambda *args, **kwargs: FakeResponse())
    assert OllamaModelProvider().list_models() == ["qwen2.5:3b", "qwen3:4b"]


def test_real_provider_reports_unavailable(monkeypatch):
    def fail(*args, **kwargs):
        raise URLError("connection refused")
    monkeypatch.setattr("agent_runtime.model_provider.urlopen", fail)
    with pytest.raises(OllamaUnavailableError):
        OllamaModelProvider().list_models()


def test_quality_evaluation_returns_explainable_dimensions():
    result = QualityPolicyEvaluator(min_characters=40).evaluate(
        "Explain checkpoint integrity", 
        "Checkpoint integrity uses a digest to detect changes.\n\n"
        "Key points\n- Compare the digest before restoring a snapshot.\n"
        "- Reject a checkpoint when verification fails."
    )
    assert 0 <= result.quality_score_percent <= 100
    assert set(result.scores) == {"relevance", "completeness", "clarity", "factual_support", "style_format"}
    assert result.to_dict()["evaluator_version"] == "draftguard-quality-1.0"
    assert result.to_dict()["quality_score"] <= 1


def test_semantic_evaluator_validates_json_and_scores():
    from agent_runtime.semantic_evaluator import SemanticQualityEvaluator

    class SemanticProvider:
        def generate(self, model, prompt, *, system=None):
            return json.dumps({
                "scores": {name: 4 for name in ("relevance", "completeness", "clarity", "factual_support", "style_format")},
                "reasons": ["Clear and relevant draft"],
            })

    result = SemanticQualityEvaluator(SemanticProvider(), "qwen3:4b").evaluate("Write an overview", "A draft")
    assert result["status"] == "ok"
    assert result["scores"]["relevance"] == 4


def test_semantic_evaluator_failure_is_not_a_pass():
    from agent_runtime.semantic_evaluator import SemanticQualityEvaluator

    class BrokenProvider:
        def generate(self, model, prompt, *, system=None):
            return "not json"

    semantic = SemanticQualityEvaluator(BrokenProvider(), "qwen3:4b").evaluate("prompt", "draft")
    base = QualityPolicyEvaluator(min_characters=1).evaluate("prompt", "This is long enough to be reviewed by the evaluator.")
    combined = QualityPolicyEvaluator(min_characters=1).combine_semantic(base, semantic)
    assert semantic["status"] == "error"
    assert "model response did not contain a JSON object" in semantic["reasons"][0]
    assert combined.decision == Decision.NEEDS_HUMAN_REVIEW
    assert "SEMANTIC_EVALUATION_UNAVAILABLE_OR_INVALID" in combined.reasons


def test_semantic_failure_keeps_candidate_unsafe_and_routes_to_review(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SEMANTIC_EVALUATION", "1")
    workflow, store = runtime(tmp_path, FakeProvider())
    result = workflow.generate("semantic-fail", "Explain checkpoint recovery", mode="ollama", model_name="qwen3:4b")
    assert result["evaluation"]["evaluation_mode"] == "hybrid"
    assert result["evaluation"]["semantic_evaluation"]["status"] == "error"
    assert result["evaluation"]["decision"] == "needs_human_review"
    assert result["is_safe_checkpoint"] is False
    assert result["approval_status"] == "pending"
    assert any(event["event_type"] == "CONTENT_EVALUATED" and event["details"]["semantic_status"] == "error"
               for event in store.get_audit_events("semantic-fail"))


def test_semantic_scores_cannot_override_hard_policy_rejection():
    evaluator = QualityPolicyEvaluator(min_characters=1)
    base = evaluator.evaluate("request", "This text says steal passwords and is long enough for a test.")
    semantic = {
        "status": "ok", "model_name": "qwen3:4b",
        "scores": {name: 5 for name in ("relevance", "completeness", "clarity", "factual_support", "style_format")},
        "reasons": [], "evaluator_version": "draftguard-semantic-1.0",
    }
    combined = evaluator.combine_semantic(base, semantic)
    assert combined.decision == Decision.REJECT


def test_real_ollama_provider_auto_runs_semantic_evaluation(tmp_path, monkeypatch):
    provider = OllamaModelProvider()
    responses = [
        "A sufficiently detailed draft about checkpoint recovery with useful explanations and examples.",
        json.dumps({
            "scores": {name: 4 for name in ("relevance", "completeness", "clarity", "factual_support", "style_format")},
            "reasons": ["Relevant and readable"],
        }),
    ]
    formats = []
    def fake_generate(model, prompt, *, system=None, format=None):
        formats.append(format)
        return responses.pop(0)
    monkeypatch.setattr(provider, "generate", fake_generate)
    monkeypatch.delenv("AGENT_SEMANTIC_EVALUATION", raising=False)
    workflow, _ = runtime(tmp_path, provider)
    result = workflow.generate("auto-semantic", "Explain checkpoint recovery", mode="ollama", model_name="qwen3:4b")
    assert result["evaluation"]["evaluation_mode"] == "hybrid"
    assert result["evaluation"]["semantic_evaluation"]["status"] == "ok"
    assert formats[0] is None
    assert formats[1]["type"] == "object"
    assert result["state"]["evaluation"]["evaluator_version"] == "draftguard-quality-1.0"
    assert result["approval_status"] == "pending"
    assert result["is_safe_checkpoint"] is False
