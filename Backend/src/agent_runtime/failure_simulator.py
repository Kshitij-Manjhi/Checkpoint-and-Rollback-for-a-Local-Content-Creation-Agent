"""Deterministic fault injection for controlled recovery experiments."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FailureScenario:
    name: str
    description: str
    should_trigger_rollback: bool


SCENARIOS = {
    "quality_degradation": FailureScenario(
        "quality_degradation", "Candidate content falls below a fixed quality threshold.", True
    ),
    "policy_violation": FailureScenario(
        "policy_violation", "Candidate contains a deterministic prohibited marker.", True
    ),
    "corrupted_snapshot": FailureScenario(
        "corrupted_snapshot", "Stored checkpoint bytes are modified after checkpoint creation.", True
    ),
    "corrupted_checkpoint_artifact": FailureScenario(
        "corrupted_checkpoint_artifact", "A Git checkpoint payload is corrupted after approval.", True
    ),
    "missing_checkpoint_artifact": FailureScenario(
        "missing_checkpoint_artifact", "The recorded Git commit reference is unavailable.", True
    ),
    "failed_rollback_integrity": FailureScenario(
        "failed_rollback_integrity", "Integrity validation prevents a restore.", True
    ),
    "unsupported_claim": FailureScenario(
        "unsupported_claim", "A factual claim has no indexed evidence passage.", True
    ),
    "generation_interrupted": FailureScenario(
        "generation_interrupted", "Generation stops before a candidate checkpoint is written.", True
    ),
    "healthy_candidate": FailureScenario(
        "healthy_candidate", "Candidate passes the configured checks and should not roll back.", False
    ),
    "missing_checkpoint": FailureScenario(
        "missing_checkpoint", "No safe checkpoint exists for the task.", True
    ),
}


def assess_candidate(state: dict[str, Any]) -> tuple[bool, list[str], float]:
    """Return (safe, reasons, quality_score) using transparent toy rules.

    These rules are experimental oracles, not a general safety classifier.
    """
    draft = str(state.get("draft", ""))
    reasons: list[str] = []
    score = min(len(draft.strip()) / 120.0, 1.0)
    if len(draft.strip()) < 40:
        reasons.append("QUALITY_BELOW_MIN_LENGTH")
    lowered = draft.lower()
    markers = ("steal passwords", "ignore all safety", "exfiltrate secrets")
    if any(marker in lowered for marker in markers):
        reasons.append("POLICY_MARKER_DETECTED")
    return not reasons, reasons, score


def inject_corruption(store, checkpoint_id: str) -> None:
    """Modify snapshot content without updating its digest (integrity fault)."""
    store.tamper_snapshot_for_test(checkpoint_id, '{"draft":"CORRUPTED SNAPSHOT"}')
