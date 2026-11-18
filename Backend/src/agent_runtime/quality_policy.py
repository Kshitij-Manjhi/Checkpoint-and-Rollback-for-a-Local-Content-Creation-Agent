"""Transparent deterministic checks; these are guardrails, not a general safety classifier."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from enum import Enum


class Decision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


@dataclass(frozen=True)
class EvaluationResult:
    decision: Decision
    quality_score: float
    reasons: tuple[str, ...]
    checks: dict[str, bool]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["decision"] = self.decision.value
        value["reasons"] = list(self.reasons)
        return value


class QualityPolicyEvaluator:
    def __init__(self, min_characters: int = 80):
        if min_characters < 1:
            raise ValueError("min_characters must be positive")
        self.min_characters = min_characters

    def evaluate(self, prompt: str, content: str) -> EvaluationResult:
        text = content.strip()
        lowered = text.lower()
        reasons: list[str] = []
        checks = {
            "non_empty": bool(text),
            "minimum_length": len(text) >= self.min_characters,
            "prohibited_marker_absent": True,
            "high_impact_claim_review": True,
        }
        prohibited = (
            "steal passwords", "ignore all safety", "exfiltrate secrets",
            "malicious payload", "bypass authentication",
        )
        matched = [marker for marker in prohibited if marker in lowered]
        if matched:
            checks["prohibited_marker_absent"] = False
            reasons.extend("POLICY_MARKER_DETECTED:" + marker.upper() for marker in matched)
        if not text:
            reasons.append("EMPTY_CONTENT")
        elif len(text) < self.min_characters:
            reasons.append("QUALITY_BELOW_MIN_LENGTH")
        high_impact = (
            "guaranteed cure", "diagnose you with", "guaranteed investment return",
            "legal advice you must follow", "risk-free investment",
        )
        if any(marker in lowered for marker in high_impact):
            checks["high_impact_claim_review"] = False
            reasons.append("HIGH_IMPACT_CLAIM_REQUIRES_HUMAN_REVIEW")

        # Hard policy markers reject. High-impact claims or very short content
        # require review; other content is accepted by these limited checks.
        if matched or not text:
            decision = Decision.REJECT
        elif not checks["minimum_length"] or not checks["high_impact_claim_review"]:
            decision = Decision.NEEDS_HUMAN_REVIEW
        else:
            decision = Decision.ACCEPT
        score = min(len(text) / max(self.min_characters * 2, 1), 1.0)
        return EvaluationResult(decision, round(score, 4), tuple(reasons), checks)
