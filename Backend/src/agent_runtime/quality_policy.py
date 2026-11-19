"""Transparent deterministic content checks and an explainable baseline quality score.

These heuristics are guardrails, not a general-purpose safety classifier or a factuality proof.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from enum import Enum
import re


class Decision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


WEIGHTS = {
    "relevance": 0.30,
    "completeness": 0.25,
    "clarity": 0.20,
    "factual_support": 0.15,
    "style_format": 0.10,
}


@dataclass(frozen=True)
class EvaluationResult:
    decision: Decision
    quality_score: float  # Legacy normalized score in [0, 1], retained for compatibility.
    reasons: tuple[str, ...]
    checks: dict[str, bool]
    scores: dict[str, float]
    quality_score_percent: float
    evaluator_version: str = "draftguard-quality-1.0"
    evaluation_mode: str = "deterministic"
    semantic_evaluation: dict | None = None

    def to_dict(self) -> dict:
        value = asdict(self)
        value["decision"] = self.decision.value
        value["reasons"] = list(self.reasons)
        return value


class QualityPolicyEvaluator:
    def __init__(self, min_characters: int = 80, pass_threshold: float = 55.0):
        if min_characters < 1:
            raise ValueError("min_characters must be positive")
        if not 0 <= pass_threshold <= 100:
            raise ValueError("pass_threshold must be between 0 and 100")
        self.min_characters = min_characters
        self.pass_threshold = pass_threshold

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {token for token in re.findall(r"[a-zA-Z0-9]{3,}", value.lower())}

    def _scores(self, prompt: str, text: str) -> dict[str, float]:
        words = re.findall(r"\b[\w'-]+\b", text)
        sentences = [s.strip() for s in re.split(r"[.!?\n]+", text) if s.strip()]
        prompt_tokens, content_tokens = self._tokens(prompt), self._tokens(text)
        overlap = len(prompt_tokens & content_tokens) / max(len(prompt_tokens), 1)
        relevance = 2.5 + min(2.0, overlap * 2.5)
        if not prompt_tokens:
            relevance = 3.0
        completeness = 2.5 + min(1.5, len(words) / 120.0)
        headings = len(re.findall(r"(?m)^\s{0,3}(?:#{1,6}\s+|[A-Z][A-Za-z /&-]{2,}:?\s*$)", text))
        if headings >= 2:
            completeness += 0.5
        average_sentence = len(words) / max(len(sentences), 1)
        clarity = 3.5 if 8 <= average_sentence <= 28 else (3.0 if average_sentence <= 40 else 2.2)
        if len(text) < self.min_characters:
            clarity = min(clarity, 2.0)
        # Without an evidence corpus, factual support is unverified. This neutral
        # baseline is deliberately not a claim that the content is factually true.
        has_reference_signal = bool(re.search(r"\b(source|reference|citation|according to|evidence)\b|https?://", text, re.I))
        factual_support = 3.0 if has_reference_signal else 2.5
        style_format = 3.5 if len(text.splitlines()) > 2 else 2.8
        style_format = min(style_format, 4.0)
        return {k: round(max(0.0, min(5.0, v)), 2) for k, v in {
            "relevance": relevance, "completeness": completeness, "clarity": clarity,
            "factual_support": factual_support, "style_format": style_format,
        }.items()}

    @staticmethod
    def _weighted_percent(scores: dict[str, float]) -> float:
        return round(sum(scores[name] * weight for name, weight in WEIGHTS.items()) / 5 * 100, 2)

    def evaluate(self, prompt: str, content: str) -> EvaluationResult:
        text = content.strip()
        lowered = text.lower()
        reasons: list[str] = []
        prohibited = (
            "steal passwords", "ignore all safety", "exfiltrate secrets",
            "malicious payload", "bypass authentication",
        )
        matched = [marker for marker in prohibited if marker in lowered]
        high_impact = (
            "guaranteed cure", "diagnose you with", "guaranteed investment return",
            "legal advice you must follow", "risk-free investment",
        )
        high_impact_found = any(marker in lowered for marker in high_impact)
        checks = {
            "non_empty": bool(text),
            "minimum_length": len(text) >= self.min_characters,
            "prohibited_marker_absent": not matched,
            "high_impact_claim_review": not high_impact_found,
        }
        if matched:
            reasons.extend("POLICY_MARKER_DETECTED:" + marker.upper() for marker in matched)
        if not text:
            reasons.append("EMPTY_CONTENT")
        elif len(text) < self.min_characters:
            reasons.append("QUALITY_BELOW_MIN_LENGTH")
        if high_impact_found:
            reasons.append("HIGH_IMPACT_CLAIM_REQUIRES_HUMAN_REVIEW")

        scores = self._scores(prompt, text)
        score_percent = self._weighted_percent(scores)
        if matched or not text:
            decision = Decision.REJECT
        elif not checks["minimum_length"] or high_impact_found or score_percent < self.pass_threshold:
            decision = Decision.NEEDS_HUMAN_REVIEW
            if score_percent < self.pass_threshold:
                reasons.append("OVERALL_QUALITY_BELOW_THRESHOLD")
        else:
            decision = Decision.ACCEPT
        return EvaluationResult(
            decision=decision,
            quality_score=round(score_percent / 100, 4),
            reasons=tuple(reasons), checks=checks, scores=scores,
            quality_score_percent=score_percent,
        )

    def combine_semantic(self, base: EvaluationResult, semantic: dict) -> EvaluationResult:
        """Merge validated semantic scores without allowing them to override hard rules."""
        scores = dict(base.scores)
        scores.update(semantic["scores"])
        percent = self._weighted_percent(scores)
        reasons = list(base.reasons) + list(semantic.get("reasons", []))
        decision = base.decision
        if base.decision != Decision.REJECT:
            if semantic.get("status") != "ok":
                decision = Decision.NEEDS_HUMAN_REVIEW
                reasons.append("SEMANTIC_EVALUATION_UNAVAILABLE_OR_INVALID")
            elif base.decision == Decision.NEEDS_HUMAN_REVIEW or percent < self.pass_threshold:
                decision = Decision.NEEDS_HUMAN_REVIEW
            elif min(scores.values()) < 2.0:
                decision = Decision.NEEDS_HUMAN_REVIEW
                reasons.append("SEMANTIC_DIMENSION_BELOW_THRESHOLD")
            else:
                decision = Decision.ACCEPT
        return EvaluationResult(
            decision=decision, quality_score=round(percent / 100, 4),
            reasons=tuple(dict.fromkeys(reasons)), checks=base.checks, scores=scores,
            quality_score_percent=percent, evaluator_version="draftguard-quality-1.0",
            evaluation_mode="hybrid", semantic_evaluation=semantic,
        )
