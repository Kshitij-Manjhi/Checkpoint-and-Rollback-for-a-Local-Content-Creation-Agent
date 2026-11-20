"""Allowlisted local diagnostics; never accepts arbitrary shell commands or paths."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any


def _run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> tuple[int | None, str, bool]:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, shell=False)
        output = (result.stdout + "\n" + result.stderr)[-25000:]
        return result.returncode, output, False
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + "\n" + (exc.stderr or ""))
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return None, output[-25000:], True


def run_pytest_suite(backend_dir: str | Path, *, timeout_seconds: int = 300) -> dict[str, Any]:
    backend = Path(backend_dir).resolve()
    started = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="draftguard-pytest-") as temp_dir:
        temp = Path(temp_dir)
        env = os.environ.copy()
        env.update({"AGENT_RUNTIME_STORAGE": "sqlite", "AGENT_RUNTIME_DB": str(temp / "tests.sqlite3"),
                    "AGENT_FACTUALITY_EVALUATION": "0", "AGENT_SEMANTIC_EVALUATION": "0",
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(backend / "src")})
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   "--basetemp", str(temp / "pytest-tmp")]
        return_code, output, timed_out = _run(command, cwd=backend, env=env, timeout=timeout_seconds)
    counts = {name: 0 for name in ("passed", "failed", "skipped", "errors", "deselected")}
    for name, value in re.findall(r"(\d+) (passed|failed|skipped|error|errors|deselected)", output):
        key = "errors" if value in {"error", "errors"} else value
        counts[key] += int(name)
    summary = re.findall(r"=+\s*(.*?)\s*=+", output)
    safe_failure_lines = [line.strip()[:300] for line in output.splitlines()
                          if re.search(r"FAILED |ERROR |AssertionError|ImportError|ModuleNotFoundError", line)][:30]
    return {"experiment_id": run_id, "run_type": "pytest", "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "timed_out" if timed_out else "passed" if return_code == 0 else "failed",
            "return_code": return_code, "timeout_seconds": timeout_seconds, "counts": counts,
            "summary": summary[-1] if summary else None, "failure_details": safe_failure_lines,
            "command": "python -m pytest -q -p no:cacheprovider", "cwd": "Backend",
            "output_tail": _safe_text(output[-6000:])}


def run_factuality_benchmark(backend_dir: str | Path, model: str, *, timeout_seconds: int = 180) -> dict[str, Any]:
    backend = Path(backend_dir).resolve()
    started = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="draftguard-factuality-") as temp_dir:
        temp = Path(temp_dir)
        output_path = temp / "factuality-report.json"
        index_path = temp / "chroma-index"
        command = [sys.executable, "run_factuality_benchmark.py", "--model", model,
                   "--dataset", str(backend / "data" / "factuality_benchmark.json"),
                   "--output", str(output_path), "--index-dir", str(index_path),
                   "--timeout", str(timeout_seconds)]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(backend / "src")
        return_code, output, timed_out = _run(command, cwd=backend, env=env,
                                               timeout=timeout_seconds * 4 + 30)
        report = None
        if output_path.exists():
            try:
                report = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = None
    if isinstance(report, dict):
        report.pop("dataset_path", None)
        parts = urlsplit(str(report.get("ollama_url", "")))
        report["ollama_url"] = f"{parts.scheme}://{parts.hostname or 'configured-host'}" + (f":{parts.port}" if parts.port else "")
    status = "timed_out" if timed_out else (report or {}).get("status", "failed")
    return {"experiment_id": run_id, "run_type": "factuality_benchmark",
            "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": status, "return_code": return_code, "timeout_seconds": timeout_seconds,
            "model_name": model, "command": f"python run_factuality_benchmark.py --model {model}",
            "report": report, "failure_details": _safe_text(output[-6000:]) if return_code else None}


def _safe_text(value: str) -> str:
    value = re.sub(r"(?i)(api[_-]?key|password|passwd|secret|access[_-]?token)\s*[:=]\s*([^\s,;]+)",
                   r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    return value
