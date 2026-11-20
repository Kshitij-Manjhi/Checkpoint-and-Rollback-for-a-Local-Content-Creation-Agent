"""Run the small fixed factuality benchmark against local Ollama/Qwen.

Run from Backend:
  python run_factuality_benchmark.py --model qwen3:4b
Results are written to data/factuality_results.json by default.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path

from agent_runtime.factuality_evaluator import EvidenceFactualityEvaluator
from agent_runtime.model_provider import OllamaModelProvider
from agent_runtime.retrieval_store import LocalEvidenceIndex


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3:4b")
    parser.add_argument("--dataset", default="data/factuality_benchmark.json")
    parser.add_argument("--output", default="data/factuality_results.json")
    parser.add_argument("--index-dir", default="runtime_data/factuality_benchmark_index")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Seconds to wait for Ollama requests (default: 120)")
    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    dataset_path = Path(args.dataset)
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    provider = OllamaModelProvider(timeout=args.timeout)
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        installed_models = provider.list_models()
    except Exception as exc:
        print(json.dumps({"status": "preflight_error", "model": args.model,
                          "ollama_url": base_url, "timeout_seconds": args.timeout,
                          "error": str(exc)}, indent=2))
        return 2
    if args.model not in installed_models:
        available = ", ".join(installed_models) if installed_models else "none"
        print(json.dumps({"status": "model_missing", "model": args.model,
                          "ollama_url": base_url, "available_models": installed_models,
                          "error": f"Model '{args.model}' is not installed. Available models: {available}. "
                                   f"Install it with: ollama pull {args.model}"}, indent=2))
        return 2
    index = LocalEvidenceIndex(args.index_dir, min_score=0.2)
    evaluator = EvidenceFactualityEvaluator(provider, args.model, index)
    results = []
    for case in dataset["cases"]:
        for item in case["evidence"]:
            index.add(item["text"], task_id=case["task_id"], kind="evidence",
                      reference_id=item["id"])
        result = evaluator.evaluate(case["prompt"], case["draft"], task_id=case["task_id"])
        observed = None
        claims = result.get("claims", [])
        # The benchmark drafts are intentionally short and contain one primary claim.
        if result.get("status") == "ok" and len(claims) == 1:
            observed = claims[0]["verdict"]
        results.append({
            "case_id": case["case_id"],
            "expected_verdict": case["expected_verdict"],
            "observed_verdict": observed,
            "correct": observed == case["expected_verdict"],
            "evaluation": result,
        })

    scored = [item for item in results if item["observed_verdict"] is not None]
    report = {
        "status": "ok" if len(scored) == len(results) else "incomplete",
        "dataset_id": dataset["dataset_id"],
        "dataset_path": str(dataset_path),
        "model": args.model,
        "ollama_url": base_url,
        "timeout_seconds": args.timeout,
        "evaluator_version": "draftguard-factuality-0.1",
        "case_count": len(results),
        "scored_case_count": len(scored),
        "correct_case_count": sum(item["correct"] for item in scored),
        "verdict_accuracy_on_scored_cases": (
            sum(item["correct"] for item in scored) / len(scored) if scored else None
        ),
        "warning": "Tiny smoke-test benchmark only; not a general factuality performance estimate.",
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2))
    print(f"Detailed results: {output}")
    return 0 if len(scored) == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
