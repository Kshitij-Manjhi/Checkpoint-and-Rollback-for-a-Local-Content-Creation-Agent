import json

from run_factuality_benchmark import main


def test_cli_timeout_and_model_preflight(monkeypatch, capsys):
    class Provider:
        def __init__(self, timeout):
            assert timeout == 180
        def list_models(self):
            return ["other:latest"]

    monkeypatch.setattr("run_factuality_benchmark.OllamaModelProvider", Provider)
    monkeypatch.setattr("sys.argv", ["run_factuality_benchmark.py", "--timeout", "180"])
    assert main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "model_missing"
    assert "qwen3:4b" in report["error"]


def test_cli_connection_preflight_reports_underlying_error(monkeypatch, capsys):
    class Provider:
        def __init__(self, timeout):
            pass
        def list_models(self):
            raise RuntimeError("connection refused at http://localhost:11434/api/tags")

    monkeypatch.setattr("run_factuality_benchmark.OllamaModelProvider", Provider)
    monkeypatch.setattr("sys.argv", ["run_factuality_benchmark.py"])
    assert main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "preflight_error"
    assert "connection refused" in report["error"]
