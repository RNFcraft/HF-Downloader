from __future__ import annotations

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import psutil
from tqdm.auto import tqdm


class TelemetryState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.activity_seq = 0
        self.current_file = ""
        self.stage = "starting"
        self.files: dict[str, dict[str, Any]] = {}
        self.on_change: Callable[[], None] | None = None

    def _notify(self) -> None:
        callback = self.on_change
        if callback is not None:
            callback()

    def begin_file(self, path: str, total: int) -> None:
        with self.lock:
            self.files[path] = {"downloaded": 0, "total": max(0, total), "state": "active"}
            self.current_file = path
            self.activity_seq += 1
            self.stage = "downloading"
        self._notify()

    def observe(self, path: str, bar: tqdm) -> None:
        description = str(getattr(bar, "desc", "") or "").strip()
        value = int(getattr(bar, "n", 0) or 0)
        total = int(getattr(bar, "total", 0) or 0)
        with self.lock:
            self.activity_seq += 1
            self.stage = "downloading"
            item = self.files.setdefault(path, {"downloaded": 0, "total": total, "state": "active"})
            item["downloaded"] = max(int(item["downloaded"]), value)
            item["total"] = max(int(item["total"]), total)
            item["state"] = "active"
            self.current_file = path if not description else path
        self._notify()

    def complete_file(self, path: str, total: int) -> None:
        with self.lock:
            item = self.files.setdefault(path, {"downloaded": 0, "total": total, "state": "active"})
            item["total"] = max(int(item["total"]), total)
            item["downloaded"] = int(item["total"])
            item["state"] = "complete"
            self.activity_seq += 1
        self._notify()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            downloaded = sum(min(int(item["downloaded"]), int(item["total"])) for item in self.files.values())
            files = [
                {
                    "path": path,
                    "downloaded": int(item["downloaded"]),
                    "total": int(item["total"]),
                    "state": str(item["state"]),
                }
                for path, item in self.files.items()
            ]
            return {
                "activity_seq": self.activity_seq,
                "downloaded_bytes": downloaded,
                "current_file": self.current_file,
                "stage": self.stage,
                "files": files,
            }

    def finish(self, stage: str) -> None:
        with self.lock:
            self.stage = stage
            self.activity_seq += 1
        self._notify()


class HeartbeatReporter:
    def __init__(self, path: Path, state: TelemetryState) -> None:
        self.path = path
        self.state = state
        self.stop_event = threading.Event()
        self.process = psutil.Process()
        self.write_lock = threading.Lock()
        self.last_write = 0.0
        self.state.on_change = self.notify
        self.thread = threading.Thread(target=self._run, daemon=True, name="hf-worker-heartbeat")

    def start(self) -> None:
        self._write()
        self.thread.start()

    def stop(self, stage: str) -> None:
        self.state.finish(stage)
        self._write()
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        while not self.stop_event.wait(1):
            self._write()

    def notify(self) -> None:
        if time.monotonic() - self.last_write >= 0.2:
            self._write()

    def _write(self) -> None:
        with self.write_lock:
            try:
                counters = self.process.io_counters()
                payload = self.state.snapshot() | {
                    "timestamp": time.time(),
                    "pid": os.getpid(),
                    "io_read_bytes": int(counters.read_bytes),
                    "io_write_bytes": int(counters.write_bytes),
                }
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, self.path)
                self.last_write = time.monotonic()
            except (OSError, psutil.Error):
                # Heartbeat is diagnostic; download errors are handled by hf_hub_download.
                pass


def make_tqdm_class(state: TelemetryState, path: str):
    devnull = open(os.devnull, "w", encoding="utf-8")

    class TelemetryTqdm(tqdm):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["file"] = devnull
            kwargs.setdefault("leave", False)
            super().__init__(*args, **kwargs)
            state.observe(path, self)

        def update(self, n: int | float = 1) -> bool | None:
            result = super().update(n)
            state.observe(path, self)
            return result

        def close(self) -> None:
            state.observe(path, self)
            super().close()

    return TelemetryTqdm, devnull


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--repo-type", choices=("model", "dataset", "space"), required=True)
    parser.add_argument("--revision", default="main")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--selection-file", required=True)
    parser.add_argument("--heartbeat-file", required=True)
    parser.add_argument("--total-bytes", type=int, default=0)
    args = parser.parse_args()

    # Import after process startup: all transport environment variables have
    # already been selected by DownloadManager for this exact attempt.
    from huggingface_hub import hf_hub_download

    selection = json.loads(Path(args.selection_file).read_text(encoding="utf-8"))
    state = TelemetryState()
    reporter = HeartbeatReporter(Path(args.heartbeat_file), state)
    reporter.start()
    devnull_handles = []
    try:
        files = selection.get("files") or []
        if not files:
            raise ValueError("Selection manifest does not contain exact files")

        def download_file(item: dict[str, Any]) -> str:
            path = str(item["path"])
            size = int(item.get("size", 0) or 0)
            state.begin_file(path, size)
            tqdm_class, devnull = make_tqdm_class(state, path)
            devnull_handles.append(devnull)
            result = hf_hub_download(
                repo_id=args.repo_id,
                filename=path,
                repo_type=None if args.repo_type == "model" else args.repo_type,
                revision=str(item.get("commit_hash") or args.revision),
                local_dir=args.destination,
                token=True if os.environ.get("HF_TOKEN") else None,
                etag_timeout=60,
                tqdm_class=tqdm_class,
            )
            state.complete_file(path, size)
            return result

        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8)), thread_name_prefix="hf-file") as pool:
            futures = [pool.submit(download_file, item) for item in files]
            for future in as_completed(futures):
                future.result()
        reporter.stop("complete")
        return 0
    except BaseException:
        reporter.stop("error")
        raise
    finally:
        for devnull in devnull_handles:
            devnull.close()


if __name__ == "__main__":
    raise SystemExit(main())
