import json
from types import SimpleNamespace

from hf_downloader.worker import HeartbeatReporter, TelemetryState


def test_worker_heartbeat_is_written(tmp_path):
    path = tmp_path / "worker.heartbeat.json"
    state = TelemetryState()
    reporter = HeartbeatReporter(path, state)
    reporter.start()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid"] > 0
    assert payload["timestamp"] > 0
    assert "io_read_bytes" in payload
    reporter.stop("complete")
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["stage"] == "complete"


def test_worker_reports_progress_for_each_active_file():
    state = TelemetryState()
    state.begin_file("weights/model-01.safetensors", 1000)
    state.begin_file("weights/model-02.safetensors", 2000)
    state.observe("weights/model-01.safetensors", SimpleNamespace(n=250, total=1000, desc="downloading"))
    state.observe("weights/model-02.safetensors", SimpleNamespace(n=1000, total=2000, desc="downloading"))
    snapshot = state.snapshot()
    assert snapshot["downloaded_bytes"] == 1250
    assert snapshot["files"] == [
        {"path": "weights/model-01.safetensors", "downloaded": 250, "total": 1000, "state": "active"},
        {"path": "weights/model-02.safetensors", "downloaded": 1000, "total": 2000, "state": "active"},
    ]
    state.complete_file("weights/model-01.safetensors", 1000)
    completed = state.snapshot()["files"]
    assert completed[0]["downloaded"] == 1000
    assert completed[0]["state"] == "complete"
