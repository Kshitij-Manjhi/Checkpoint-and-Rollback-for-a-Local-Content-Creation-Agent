"""Interactive Step 3 entry point for the shared modular runtime."""
from __future__ import annotations
from pathlib import Path
from agent_runtime import (
    CheckpointManager, ContentWorkflow, OllamaModelProvider,
    OllamaUnavailableError, QualityPolicyEvaluator,
)
from agent_runtime.store import SQLiteCheckpointStore


def main() -> None:
    data_dir = Path("runtime_data")
    data_dir.mkdir(exist_ok=True)
    store = SQLiteCheckpointStore(data_dir / "runtime.sqlite3")
    provider = OllamaModelProvider()
    workflow = ContentWorkflow(
        CheckpointManager(store),
        evaluator=QualityPolicyEvaluator(),
        provider=provider,
    )
    print("Checkpoint + Rollback — Step 3")
    print("1. No-LLM mode (offline template generator)")
    print("2. Ollama mode (choose an installed model dynamically)")
    choice = input("Choose mode [1/2]: ").strip()
    if choice == "2":
        try:
            models = workflow.available_models()
        except OllamaUnavailableError as exc:
            print(f"\nOllama unavailable: {exc}")
            print("Start Ollama or rerun and choose No-LLM mode.")
            return
        if not models:
            print("Ollama is running but no models were found. Pull a model or choose No-LLM mode.")
            return
        print("\nInstalled models:")
        for index, name in enumerate(models, 1):
            print(f"{index}. {name}")
        try:
            selected = int(input("Select model number: ").strip())
            model_name = models[selected - 1]
        except (ValueError, IndexError):
            print("Invalid model selection.")
            return
        mode = "ollama"
    elif choice == "1":
        mode, model_name = "no_llm", None
    else:
        print("Invalid mode.")
        return

    task_id = input("Task ID (e.g. article-001): ").strip()
    prompt = input("What should the agent write? ").strip()
    if not task_id or not prompt:
        print("Task ID and prompt are required.")
        return
    try:
        result = workflow.generate(task_id, prompt, mode=mode, model_name=model_name)
    except (ValueError, OllamaUnavailableError) as exc:
        print(f"Generation failed: {exc}")
        return
    print("\n--- GENERATED DRAFT ---")
    print(result["state"]["draft"])
    print("\n--- EVALUATION ---")
    print(f"Decision: {result['evaluation']['decision']}")
    print(f"Reasons: {result['evaluation']['reasons'] or 'none'}")
    print(f"Checkpoint: {result['checkpoint_id']}")
    print(f"Model: {result['state']['model_name'] or 'none (no-LLM mode)'}")
    if result["evaluation"]["decision"] != "accept":
        print("\nThis candidate is not a safe checkpoint. The last accepted safe checkpoint remains available.")
        if input("Attempt rollback now? [y/N]: ").strip().lower() == "y":
            try:
                restored = workflow.rollback(task_id)
                print(f"Restored checkpoint {restored['checkpoint_id']}: {restored['verification_passed']}")
                print(restored["state"].get("draft", ""))
            except Exception as exc:
                print(f"Rollback failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
