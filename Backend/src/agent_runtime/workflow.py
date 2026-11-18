"""Shared generation, evaluation, checkpointing, and rollback workflow."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from .checkpoint import CheckpointManager
from .content_generator import create_generator
from .model_provider import OllamaModelProvider
from .quality_policy import QualityPolicyEvaluator


class ContentWorkflow:
    def __init__(self, checkpoint_manager: CheckpointManager,
                 evaluator: QualityPolicyEvaluator | None = None,
                 provider: OllamaModelProvider | None = None):
        self.checkpoints = checkpoint_manager
        self.evaluator = evaluator or QualityPolicyEvaluator()
        self.provider = provider or OllamaModelProvider()

    def available_models(self) -> list[str]:
        return self.provider.list_models()

    def generate(self, task_id: str, prompt: str, *, mode: str = "no_llm",
                 model_name: str | None = None) -> dict[str, Any]:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        # Always preserve an initial known-safe state, even if the first candidate
        # is rejected. Subsequent runs preserve the latest accepted safe checkpoint.
        initial_state = {"task_id": task_id, "prompt": "", "draft": "", "version": 0,
                         "mode": "system", "model_name": None, "decision": "accept"}
        try:
            parent = self.checkpoints.store.latest_safe_checkpoint(task_id)
        except Exception:
            parent = None
        if parent is None:
            baseline = self.checkpoints.create_checkpoint(task_id, initial_state, is_safe=True)
            parent_id = baseline.checkpoint_id
            version = 1
        else:
            parent_id = parent["checkpoint_id"]
            version = int(parent["sequence"]) + 1

        generator = create_generator(mode, model_name=model_name, provider=self.provider)
        generated = generator.generate(prompt)
        evaluation = self.evaluator.evaluate(prompt, generated.text)
        state = {
            "task_id": task_id, "prompt": prompt, "draft": generated.text,
            "version": version, "mode": generated.mode, "model_name": generated.model_name,
            "decision": evaluation.decision.value, "evaluation": evaluation.to_dict(),
        }
        # Step 5 governance rule: automated checks never grant rollback safety.
        # Every generated candidate remains unsafe until a human reviewer approves it.
        state["approval_status"] = (
            "blocked" if evaluation.decision.value == "reject" else "pending"
        )
        checkpoint = self.checkpoints.create_checkpoint(
            task_id, state, is_safe=False, parent_checkpoint_id=parent_id,
        )
        self.checkpoints.store.add_audit_event(
            task_id=task_id,
            event_type="CONTENT_EVALUATED",
            checkpoint_id=checkpoint.checkpoint_id,
            details={
                "mode": generated.mode, "model_name": generated.model_name,
                "decision": evaluation.decision.value, "reasons": list(evaluation.reasons),
                "quality_score": evaluation.quality_score, "prompt_length": len(prompt),
                "content_length": len(generated.text),
            },
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"task_id": task_id, "checkpoint_id": checkpoint.checkpoint_id,
                "parent_checkpoint_id": parent_id, "state": state,
                "evaluation": evaluation.to_dict(), "is_safe_checkpoint": checkpoint.is_safe,
                "approval_status": state["approval_status"]}

    def rollback(self, task_id: str) -> dict[str, Any]:
        result = self.checkpoints.restore_latest_safe(task_id)
        self.checkpoints.store.add_audit_event(
            task_id=task_id, event_type="WORKFLOW_ROLLBACK",
            checkpoint_id=result.checkpoint.checkpoint_id,
            details={"verification_passed": result.verification_passed,
                     "rollback_latency_ms": round(result.rollback_latency_ms, 3)},
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"task_id": task_id, "checkpoint_id": result.checkpoint.checkpoint_id,
                "state": result.state, "verification_passed": result.verification_passed,
                "rollback_latency_ms": result.rollback_latency_ms}
