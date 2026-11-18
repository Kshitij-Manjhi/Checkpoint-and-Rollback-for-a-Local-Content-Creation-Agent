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
    "missing_checkpoint": FailureScenario(
        "missing_checkpoint", "No safe checkpoint exists for the task.", True
    ),
    "healthy_candidate": FailureScenario(
        "healthy_candidate", "Candidate passes the deterministic experimental checks.", False
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
