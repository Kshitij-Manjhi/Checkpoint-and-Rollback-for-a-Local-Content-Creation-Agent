import argparse
import json
from agent_runtime.evaluation import run_experiment

parser = argparse.ArgumentParser(description="Run deterministic checkpoint recovery experiments.")
parser.add_argument("--output", default="data/evaluation.sqlite3", help="SQLite database path")
parser.add_argument("--repetitions", type=int, default=5, help="Trials per scenario")
args = parser.parse_args()

result = run_experiment(args.output, repetitions=args.repetitions)
print(json.dumps({k: v for k, v in result.items() if k != "trials"}, indent=2))
print(f"Detailed report: {args.output.rsplit('.', 1)[0]}.json")
