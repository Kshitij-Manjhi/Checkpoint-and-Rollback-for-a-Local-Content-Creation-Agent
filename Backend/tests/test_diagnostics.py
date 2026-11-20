import json
import subprocess

from agent_runtime import diagnostics


def test_pytest_runner_uses_fixed_command_and_parses_real_counts(tmp_path, monkeypatch):
    captured = {}
    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 1,
            stdout="3 passed, 1 failed, 2 skipped, 1 error in 1.2s\nFAILED tests/test_x.py::test_case",
            stderr="")
    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    report = diagnostics.run_pytest_suite(tmp_path, timeout_seconds=40)
    assert captured["kwargs"]["shell"] is False
    assert captured["command"][1:4] == ["-m", "pytest", "-q"]
    assert report["counts"] == {"passed": 3, "failed": 1, "skipped": 2, "errors": 1, "deselected": 0}
    assert report["status"] == "failed"
    assert report["failure_details"]


def test_factuality_runner_uses_temporary_chroma_and_redacts_service_url(tmp_path, monkeypatch):
    commands = []
    def fake_run(command, **kwargs):
        commands.append(command)
        output_path = command[command.index("--output") + 1]
        with open(output_path, "w", encoding="utf-8") as stream:
            json.dump({"status": "incomplete", "ollama_url": "http://user:secret@localhost:11434",
                       "case_count": 3, "results": []}, stream)
        return subprocess.CompletedProcess(command, 2, stdout="incomplete", stderr="")
    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    report = diagnostics.run_factuality_benchmark(tmp_path, "qwen3:4b", timeout_seconds=30)
    assert report["status"] == "incomplete"
    assert report["report"]["ollama_url"] == "http://localhost:11434"
    assert "secret" not in json.dumps(report)
    assert "--index-dir" in commands[0]
