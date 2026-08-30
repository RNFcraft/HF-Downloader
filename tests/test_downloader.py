from types import SimpleNamespace

from hf_downloader import downloader
from hf_downloader.downloader import (
    DownloadManager,
    DownloadPlan,
    PermanentDownloadError,
    RepositoryFile,
    TransientDownloadError,
    WorkerStalled,
    build_worker_environment,
    classify_download_error,
    effective_workers,
    format_bytes,
    format_duration,
    friendly_error,
)
from hf_downloader.models import parse_huggingface_source


def test_human_readable_values():
    assert format_bytes(1536) == "1.5 КБ"
    assert format_duration(65) == "1 мин 05 с"
    assert format_duration(None) == "—"


def test_friendly_access_errors():
    assert "token" in friendly_error(RuntimeError("401 Unauthorized")).lower()
    assert "условия" in friendly_error(RuntimeError("403 gated repo"))


def test_plan_uses_only_selected_files(monkeypatch, tmp_path):
    captured = {}

    def fake_snapshot_download(**kwargs):
        captured.update(kwargs)
        return [SimpleNamespace(filename="weights/model-01.gguf", file_size=25, will_download=True)]

    monkeypatch.setattr(downloader, "snapshot_download", fake_snapshot_download)
    plan = DownloadManager(__import__("queue").Queue()).plan(
        parse_huggingface_source("org/repo"),
        tmp_path,
        token=None,
        ignore_patterns=None,
        selected_files=["weights/model-01.gguf"],
    )
    assert captured["allow_patterns"] == ["weights/model-01.gguf"]
    assert plan.files == 1
    assert plan.total_bytes == 25


def test_transport_environments_are_isolated():
    dirty = {
        "PATH": "ok",
        "HF_XET_HIGH_PERFORMANCE": "1",
        "HF_HUB_DISABLE_XET": "1",
        "HF_XET_FIXED_DOWNLOAD_CONCURRENCY": "99",
    }
    adaptive = build_worker_environment(dirty, "xet_adaptive", token=None)
    assert "HF_XET_HIGH_PERFORMANCE" not in adaptive
    assert "HF_HUB_DISABLE_XET" not in adaptive
    assert "HF_XET_FIXED_DOWNLOAD_CONCURRENCY" not in adaptive
    assert adaptive["HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY"] == "1"
    assert adaptive["HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY"] == "true"

    conservative = build_worker_environment(dirty, "xet_conservative", token="secret")
    assert "HF_XET_HIGH_PERFORMANCE" not in conservative
    assert conservative["HF_XET_FIXED_DOWNLOAD_CONCURRENCY"] == "2"
    assert conservative["HF_XET_NUM_CONCURRENT_RANGE_GETS"] == "2"
    assert conservative["HF_TOKEN"] == "secret"

    http = build_worker_environment(dirty, "plain_http", token=None)
    assert http["HF_HUB_DISABLE_XET"] == "1"
    assert "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY" not in http
    assert "HF_TOKEN" not in http


def test_hdd_worker_limits():
    assert effective_workers("xet_adaptive", 8) == 8
    assert effective_workers("xet_conservative", 8) == 4
    assert effective_workers("plain_http", 20) == 8


def test_error_classification():
    assert classify_download_error("401 Unauthorized") is PermanentDownloadError
    assert classify_download_error("RevisionNotFoundError") is PermanentDownloadError
    assert classify_download_error("connection reset by peer") is TransientDownloadError


def test_progress_counts_only_plan_files(tmp_path):
    selected = tmp_path / "selected.bin"
    selected.write_bytes(b"x" * 10)
    (tmp_path / "unrelated.bin").write_bytes(b"x" * 1000)
    plan = DownloadPlan(
        files=1,
        total_bytes=100,
        download_bytes=90,
        destination=tmp_path,
        free_bytes=10000,
        entries=(RepositoryFile("selected.bin", 100),),
    )
    assert DownloadManager._measure(plan) == 10
    assert DownloadManager._completed_files(plan) == 0


def test_auto_falls_back_after_two_stalls(monkeypatch, tmp_path):
    manager = DownloadManager(__import__("queue").Queue())
    transports = []

    def fake_download_once(_source, _plan, **options):
        transports.append(options["transport"])
        if len(transports) <= 2:
            raise WorkerStalled("stalled")

    monkeypatch.setattr(manager, "_download_once", fake_download_once)
    monkeypatch.setattr(manager.cancel_event, "wait", lambda _timeout: False)
    plan = DownloadPlan(0, 0, 0, tmp_path, 1000)
    manager._download_with_retries(
        parse_huggingface_source("org/repo"),
        plan,
        token=None,
        workers=4,
        retries=2,
        stall_timeout=600,
        transport="auto",
        ignore_patterns=None,
        selected_files=[],
    )
    assert transports == ["xet_adaptive", "xet_adaptive", "plain_http"]
    events = []
    while not manager.events.empty():
        events.append(manager.events.get())
    assert any(event.kind == "fallback" for event in events)


def test_permanent_error_is_not_retried(monkeypatch, tmp_path):
    manager = DownloadManager(__import__("queue").Queue())
    calls = 0

    def fake_download_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise PermanentDownloadError("404")

    monkeypatch.setattr(manager, "_download_once", fake_download_once)
    with __import__("pytest").raises(PermanentDownloadError):
        manager._download_with_retries(
            parse_huggingface_source("org/repo"),
            DownloadPlan(0, 0, 0, tmp_path, 1000),
            token=None,
            workers=4,
            retries=6,
            stall_timeout=600,
            transport="auto",
            ignore_patterns=None,
            selected_files=[],
        )
    assert calls == 1


def test_fallback_preserves_partial_file(monkeypatch, tmp_path):
    partial = tmp_path / "large.safetensors.incomplete"
    partial.write_bytes(b"partial")
    manager = DownloadManager(__import__("queue").Queue())
    calls = 0

    def fake_download_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        assert partial.read_bytes() == b"partial"
        if calls <= 2:
            raise WorkerStalled("stalled")

    monkeypatch.setattr(manager, "_download_once", fake_download_once)
    monkeypatch.setattr(manager.cancel_event, "wait", lambda _timeout: False)
    manager._download_with_retries(
        parse_huggingface_source("org/repo"),
        DownloadPlan(0, 0, 0, tmp_path, 1000),
        token=None,
        workers=4,
        retries=2,
        stall_timeout=600,
        transport="auto",
        ignore_patterns=None,
        selected_files=[],
    )
    assert partial.read_bytes() == b"partial"


def test_process_stop_escalates_to_terminate():
    class Process:
        def __init__(self):
            self.terminated = False
            self.waits = 0

        def poll(self):
            return None if not self.terminated else 0

        def wait(self, timeout):
            self.waits += 1
            if not self.terminated:
                raise __import__("subprocess").TimeoutExpired("worker", timeout)
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    process = Process()
    DownloadManager._stop_process(process, grace_seconds=0.01)
    assert process.terminated


def test_cancel_sets_flag_and_stops_active_worker(monkeypatch):
    manager = DownloadManager(__import__("queue").Queue())
    calls = []
    monkeypatch.setattr(manager, "_stop_active_process", lambda grace_seconds: calls.append(grace_seconds))
    manager.cancel()
    assert manager.cancel_event.is_set()
    assert calls == [1.0]


def test_resume_recognizes_already_completed_plan_file(tmp_path):
    target = tmp_path / "weights" / "model.safetensors"
    target.parent.mkdir()
    target.write_bytes(b"already-complete")
    plan = DownloadPlan(
        files=1,
        total_bytes=len(b"already-complete"),
        download_bytes=0,
        destination=tmp_path,
        free_bytes=1000,
        entries=(RepositoryFile("weights/model.safetensors", len(b"already-complete")),),
    )
    assert DownloadManager._measure(plan) == plan.total_bytes
    assert DownloadManager._completed_files(plan) == 1
