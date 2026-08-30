from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# Metadata calls use conservative timeouts. Transport-specific variables are set
# independently for every worker before that process imports huggingface_hub.
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "600")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

from huggingface_hub import HfApi, snapshot_download

from .models import HubSource


TransportProfile = Literal["auto", "xet_adaptive", "xet_conservative", "plain_http"]
TRANSPORT_KEYS = {
    "HF_HUB_DISABLE_XET",
    "HF_XET_HIGH_PERFORMANCE",
    "HF_XET_HP",
    "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY",
    "HF_XET_FIXED_DOWNLOAD_CONCURRENCY",
    "HF_XET_NUM_CONCURRENT_RANGE_GETS",
    "HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY",
}
TRANSPORT_LABELS = {
    "auto": "Auto",
    "xet_adaptive": "Xet Adaptive",
    "xet_conservative": "Xet Conservative",
    "plain_http": "Plain HTTP",
}


class DownloadCancelled(RuntimeError):
    pass


class PermanentDownloadError(RuntimeError):
    pass


class TransientDownloadError(RuntimeError):
    pass


class WorkerStalled(TransientDownloadError):
    pass


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    path: str
    size: int
    commit_hash: str = ""


@dataclass(frozen=True, slots=True)
class FileProgress:
    path: str
    downloaded: int
    total: int


@dataclass(slots=True)
class DownloadPlan:
    files: int
    total_bytes: int
    download_bytes: int
    destination: Path
    free_bytes: int
    entries: tuple[RepositoryFile, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class DownloadEvent:
    kind: str
    message: str = ""
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    average_speed: float = 0.0
    eta: float | None = None
    files_done: int = 0
    files_total: int = 0
    attempt: int = 0
    transport: str = ""
    current_file: str = ""
    heartbeat_age: float | None = None
    worker_alive: bool = False
    active_files: tuple[FileProgress, ...] = field(default_factory=tuple)


def effective_transport(profile: TransportProfile, auto_fallback: bool = False) -> str:
    if profile == "auto":
        return "plain_http" if auto_fallback else "xet_adaptive"
    return profile


def effective_workers(profile: str, requested: int) -> int:
    bounded = max(1, min(int(requested), 8))
    return min(bounded, 4) if profile == "xet_conservative" else bounded


def build_worker_environment(
    base: dict[str, str],
    profile: str,
    *,
    token: str | None,
) -> dict[str, str]:
    """Build an isolated transport environment; no profile leaks into the next attempt."""
    env = base.copy()
    for key in TRANSPORT_KEYS:
        env.pop(key, None)
    env.update(
        {
            "HF_HUB_DOWNLOAD_TIMEOUT": "600",
            "HF_HUB_ETAG_TIMEOUT": "60",
            "HF_HUB_DISABLE_PROGRESS_BARS": "0",
            "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
        }
    )
    if token:
        env["HF_TOKEN"] = token
    else:
        env.pop("HF_TOKEN", None)
    if profile == "plain_http":
        env["HF_HUB_DISABLE_XET"] = "1"
    elif profile == "xet_conservative":
        env["HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY"] = "1"
        env["HF_XET_FIXED_DOWNLOAD_CONCURRENCY"] = "2"
        env["HF_XET_NUM_CONCURRENT_RANGE_GETS"] = "2"
        env["HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY"] = "false"
    else:
        # Adaptive Xet deliberately has no HIGH_PERFORMANCE or fixed concurrency.
        env["HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY"] = "1"
        env["HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY"] = "true"
    return env


def classify_download_error(error: BaseException | str) -> type[RuntimeError]:
    text = str(error).lower()
    permanent_markers = (
        "401", "403", "404", "unauthorized", "forbidden", "gated",
        "repositorynotfound", "revisionnotfound", "entrynotfound",
        "repository not found", "revision not found", "invalid repo",
        "invalid repository", "invalid revision",
    )
    return PermanentDownloadError if any(marker in text for marker in permanent_markers) else TransientDownloadError


class DownloadManager:
    """Resumable downloader with per-attempt transports and process lifecycle control."""

    def __init__(self, events: queue.Queue[DownloadEvent]) -> None:
        self.events = events
        self.cancel_event = threading.Event()
        self._active = False
        self._manager_thread: threading.Thread | None = None
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None

    @property
    def active(self) -> bool:
        return self._active

    def cancel(self) -> None:
        self.cancel_event.set()
        self._emit("status", "Остановка worker с сохранением partial-файлов…")
        self._stop_active_process(grace_seconds=1.0)

    def shutdown(self, timeout: float = 12.0) -> bool:
        """Synchronously stop worker and manager before the GUI process exits."""
        self.cancel_event.set()
        self._stop_active_process(grace_seconds=1.0)
        thread = self._manager_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, timeout))
        self._stop_active_process(grace_seconds=0.0)
        return not (thread and thread.is_alive())

    def start(
        self,
        source: HubSource,
        destination: Path,
        *,
        token: str | None,
        workers: int,
        retries: int,
        stall_timeout: int,
        transport: TransportProfile = "auto",
        ignore_patterns: list[str] | None = None,
        selected_files: list[str] | None = None,
    ) -> None:
        if self._active:
            raise RuntimeError("Загрузка уже выполняется.")
        if transport not in TRANSPORT_LABELS:
            raise ValueError(f"Неизвестный transport profile: {transport}")
        self.cancel_event.clear()
        self._active = True
        self._manager_thread = threading.Thread(
            target=self._run,
            args=(source, destination),
            kwargs={
                "token": token,
                "workers": workers,
                "retries": retries,
                "stall_timeout": stall_timeout,
                "transport": transport,
                "ignore_patterns": ignore_patterns,
                "selected_files": selected_files,
            },
            daemon=True,
            name="hf-download-manager",
        )
        self._manager_thread.start()

    def _run(self, source: HubSource, destination: Path, **options: Any) -> None:
        try:
            destination.mkdir(parents=True, exist_ok=True)
            self._emit("status", "Получение точного плана выбранных файлов…")
            plan = self.plan(
                source,
                destination,
                token=options["token"],
                ignore_patterns=options["ignore_patterns"],
                selected_files=options["selected_files"],
            )
            self._emit(
                "plan",
                f"План: {plan.files} файлов. Свободное место проверено.",
                downloaded=self._measure(plan),
                total=plan.total_bytes,
                files_done=self._completed_files(plan),
                files_total=plan.files,
            )
            required = max(plan.download_bytes, 0)
            reserve = min(max(512 * 1024 * 1024, required // 20), 5 * 1024**3)
            if required + reserve > plan.free_bytes:
                raise OSError(
                    f"Недостаточно места: требуется примерно {format_bytes(required + reserve)}, "
                    f"свободно {format_bytes(plan.free_bytes)}."
                )
            self._download_with_retries(source, plan, **options)
            if self.cancel_event.is_set():
                raise DownloadCancelled("Загрузка остановлена. Partial-файлы сохранены.")
            self._emit(
                "complete",
                f"Готово: {destination}",
                downloaded=plan.total_bytes,
                total=plan.total_bytes,
                files_done=plan.files,
                files_total=plan.files,
            )
        except DownloadCancelled as exc:
            self._emit("cancelled", str(exc))
        except Exception as exc:
            self._emit("error", friendly_error(exc))
        finally:
            self._active = False

    def plan(
        self,
        source: HubSource,
        destination: Path,
        *,
        token: str | None,
        ignore_patterns: list[str] | None,
        selected_files: list[str] | None = None,
    ) -> DownloadPlan:
        result = snapshot_download(
            repo_id=source.repo_id,
            repo_type=None if source.repo_type == "model" else source.repo_type,
            revision=source.revision,
            local_dir=destination,
            token=token or None,
            allow_patterns=self._patterns(source, selected_files),
            ignore_patterns=ignore_patterns,
            etag_timeout=60,
            dry_run=True,
        )
        infos = result if isinstance(result, list) else []
        entries = tuple(
            RepositoryFile(
                path=str(getattr(item, "filename", "")),
                size=int(getattr(item, "file_size", getattr(item, "size", 0)) or 0),
                commit_hash=str(getattr(item, "commit_hash", "") or ""),
            )
            for item in infos
            if getattr(item, "filename", None)
        )
        total = sum(entry.size for entry in entries)
        needed = sum(
            int(getattr(item, "file_size", getattr(item, "size", 0)) or 0)
            for item in infos
            if bool(getattr(item, "will_download", True))
        )
        return DownloadPlan(
            files=len(entries),
            total_bytes=total,
            download_bytes=needed,
            destination=destination,
            free_bytes=shutil.disk_usage(destination).free,
            entries=entries,
        )

    @staticmethod
    def _patterns(source: HubSource, selected_files: list[str] | None) -> list[str] | str | None:
        return selected_files if selected_files is not None else source.allow_patterns

    def _download_with_retries(
        self,
        source: HubSource,
        plan: DownloadPlan,
        *,
        token: str | None,
        workers: int,
        retries: int,
        stall_timeout: int,
        transport: TransportProfile,
        ignore_patterns: list[str] | None,
        selected_files: list[str] | None,
    ) -> None:
        last_error: BaseException | None = None
        consecutive_stalls = 0
        auto_fallback = False
        for attempt in range(1, retries + 2):
            if self.cancel_event.is_set():
                raise DownloadCancelled("Загрузка остановлена. Partial-файлы сохранены.")
            profile = effective_transport(transport, auto_fallback)
            profile_workers = effective_workers(profile, workers)
            label = TRANSPORT_LABELS[profile]
            self._emit(
                "status",
                f"Попытка {attempt}/{retries + 1} · {label} · workers {profile_workers}",
                attempt=attempt,
                transport=label,
            )
            try:
                self._download_once(
                    source,
                    plan,
                    token=token,
                    workers=profile_workers,
                    stall_timeout=stall_timeout,
                    ignore_patterns=ignore_patterns,
                    selected_files=selected_files,
                    attempt=attempt,
                    transport=profile,
                )
                return
            except DownloadCancelled:
                raise
            except PermanentDownloadError:
                raise
            except WorkerStalled as exc:
                consecutive_stalls += 1
                last_error = exc
                if transport == "auto" and not auto_fallback and consecutive_stalls >= 2:
                    auto_fallback = True
                    consecutive_stalls = 0
                    self._emit(
                        "fallback",
                        "Xet нестабилен, переключение на Plain HTTP. Cache и partial-файлы сохранены.",
                        attempt=attempt,
                        transport="Plain HTTP",
                    )
            except TransientDownloadError as exc:
                consecutive_stalls = 0
                last_error = exc
            except Exception as exc:
                error_type = classify_download_error(exc)
                if error_type is PermanentDownloadError:
                    raise PermanentDownloadError(str(exc)) from exc
                consecutive_stalls = 0
                last_error = exc
            if attempt > retries:
                break
            delay = min(30, 2 ** (attempt - 1) * 2)
            self._emit(
                "retry",
                f"Временный сбой: {friendly_error(last_error or RuntimeError())} Повтор через {delay} с.",
                attempt=attempt,
                transport=TRANSPORT_LABELS[effective_transport(transport, auto_fallback)],
            )
            if self.cancel_event.wait(delay):
                raise DownloadCancelled("Загрузка остановлена. Partial-файлы сохранены.")
        assert last_error is not None
        raise TransientDownloadError(
            f"Не удалось завершить после {retries + 1} попыток: {friendly_error(last_error)}"
        ) from last_error

    def _download_once(self, source: HubSource, plan: DownloadPlan, **options: Any) -> None:
        started = time.monotonic()
        initial_downloaded = self._measure(plan)
        last_activity = started
        previous_visible = initial_downloaded
        previous_activity_seq = -1
        previous_io = (-1, -1)
        telemetry: dict[str, Any] = {}
        selection = {
            "allow": self._patterns(source, options["selected_files"]),
            "ignore": options["ignore_patterns"],
            "files": [
                {"path": entry.path, "size": entry.size, "commit_hash": entry.commit_hash}
                for entry in plan.entries
            ],
        }
        manifest_path: Path | None = None
        heartbeat_path: Path | None = None
        process: subprocess.Popen[str] | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as manifest:
                json.dump(selection, manifest, ensure_ascii=False)
                manifest_path = Path(manifest.name)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".heartbeat.json", delete=False) as heartbeat:
                heartbeat_path = Path(heartbeat.name)
            command = [
                sys.executable,
                "-m",
                "hf_downloader.worker",
                "--repo-id",
                source.repo_id,
                "--repo-type",
                source.repo_type,
                "--revision",
                source.revision,
                "--destination",
                str(plan.destination),
                "--workers",
                str(options["workers"]),
                "--selection-file",
                str(manifest_path),
                "--heartbeat-file",
                str(heartbeat_path),
                "--total-bytes",
                str(plan.total_bytes),
            ]
            child_env = build_worker_environment(
                os.environ,
                options["transport"],
                token=options["token"],
            )
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as error_log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=error_log,
                    text=True,
                    env=child_env,
                    creationflags=creation_flags,
                )
                with self._process_lock:
                    self._active_process = process
                samples: list[tuple[float, int]] = [(started, initial_downloaded)]
                while process.poll() is None:
                    if self.cancel_event.wait(0.6):
                        self._stop_process(process, grace_seconds=1.0)
                        raise DownloadCancelled("Загрузка остановлена. Partial-файлы сохранены.")
                    now = time.monotonic()
                    visible = self._measure(plan)
                    heartbeat = self._read_heartbeat(heartbeat_path)
                    if heartbeat:
                        telemetry = heartbeat
                    activity_seq = int(telemetry.get("activity_seq", -1))
                    io_pair = (
                        int(telemetry.get("io_read_bytes", -1)),
                        int(telemetry.get("io_write_bytes", -1)),
                    )
                    if visible != previous_visible or activity_seq != previous_activity_seq or io_pair != previous_io:
                        last_activity = now
                        previous_visible = visible
                        previous_activity_seq = activity_seq
                        previous_io = io_pair
                    heartbeat_timestamp = float(telemetry.get("timestamp", 0.0))
                    heartbeat_age = max(0.0, time.time() - heartbeat_timestamp) if heartbeat_timestamp else None
                    worker_downloaded = int(telemetry.get("downloaded_bytes", 0) or 0)
                    downloaded = min(plan.total_bytes, max(visible, worker_downloaded))
                    active_files = tuple(
                        FileProgress(
                            path=str(item.get("path", "")),
                            downloaded=max(0, int(item.get("downloaded", 0) or 0)),
                            total=max(0, int(item.get("total", 0) or 0)),
                        )
                        for item in telemetry.get("files", [])
                        if item.get("path")
                    )
                    samples.append((now, downloaded))
                    while len(samples) > 2 and samples[0][0] < now - 60:
                        samples.pop(0)
                    short_samples = [sample for sample in samples if sample[0] >= now - 12]
                    speed = self._sample_speed(short_samples)
                    average_speed = self._sample_speed(samples)
                    eta_speed = average_speed or speed
                    eta = (plan.total_bytes - downloaded) / eta_speed if eta_speed > 0 else None
                    self._emit(
                        "progress",
                        downloaded=downloaded,
                        total=plan.total_bytes,
                        speed=speed,
                        average_speed=average_speed,
                        eta=eta,
                        files_done=self._completed_files(plan),
                        files_total=plan.files,
                        attempt=options["attempt"],
                        transport=TRANSPORT_LABELS[options["transport"]],
                        current_file=str(telemetry.get("current_file", "")),
                        heartbeat_age=heartbeat_age,
                        worker_alive=heartbeat_age is not None and heartbeat_age < 20,
                        active_files=active_files,
                    )
                    if now - last_activity > options["stall_timeout"]:
                        self._stop_process(process, grace_seconds=0.5)
                        raise WorkerStalled(
                            f"Worker жив, но network/disk activity отсутствует более "
                            f"{options['stall_timeout']} секунд"
                        )
                    if heartbeat_age is not None and heartbeat_age > 30:
                        self._stop_process(process, grace_seconds=0.5)
                        raise WorkerStalled(f"Heartbeat worker отсутствует {heartbeat_age:.0f} секунд")
                process.wait(timeout=5)
                if process.returncode:
                    error_log.seek(0)
                    detail = error_log.read().strip() or f"Worker exit code {process.returncode}"
                    error_type = classify_download_error(detail)
                    raise error_type(detail[-4000:])
                final_telemetry = self._read_heartbeat(heartbeat_path)
                final_files = tuple(
                    FileProgress(
                        path=str(item.get("path", "")),
                        downloaded=max(0, int(item.get("downloaded", 0) or 0)),
                        total=max(0, int(item.get("total", 0) or 0)),
                    )
                    for item in final_telemetry.get("files", [])
                    if item.get("path")
                )
                if final_files:
                    self._emit(
                        "progress",
                        downloaded=plan.total_bytes,
                        total=plan.total_bytes,
                        speed=0.0,
                        average_speed=0.0,
                        eta=0.0,
                        files_done=plan.files,
                        files_total=plan.files,
                        attempt=options["attempt"],
                        transport=TRANSPORT_LABELS[options["transport"]],
                        current_file=str(final_telemetry.get("current_file", "")),
                        heartbeat_age=0.0,
                        worker_alive=True,
                        active_files=final_files,
                    )
        finally:
            if process is not None:
                if process.poll() is None:
                    self._stop_process(process, grace_seconds=0.0)
                with self._process_lock:
                    if self._active_process is process:
                        self._active_process = None
            if manifest_path:
                manifest_path.unlink(missing_ok=True)
            if heartbeat_path:
                heartbeat_path.unlink(missing_ok=True)

    def _stop_active_process(self, grace_seconds: float) -> None:
        with self._process_lock:
            process = self._active_process
        if process is not None:
            self._stop_process(process, grace_seconds=grace_seconds)

    @staticmethod
    def _stop_process(process: subprocess.Popen[str], grace_seconds: float) -> None:
        if process.poll() is not None:
            return
        if grace_seconds > 0:
            try:
                process.wait(timeout=grace_seconds)
                return
            except subprocess.TimeoutExpired:
                pass
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                return
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _read_heartbeat(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return {}

    @staticmethod
    def _sample_speed(samples: list[tuple[float, int]]) -> float:
        if len(samples) < 2:
            return 0.0
        elapsed = samples[-1][0] - samples[0][0]
        return max(0.0, (samples[-1][1] - samples[0][1]) / elapsed) if elapsed > 0 else 0.0

    @staticmethod
    def _measure(plan: DownloadPlan) -> int:
        total = 0
        for entry in plan.entries:
            path = plan.destination / Path(entry.path)
            try:
                if path.is_file():
                    total += min(path.stat().st_size, entry.size)
            except (FileNotFoundError, PermissionError, OSError):
                continue
        return min(total, plan.total_bytes)

    @staticmethod
    def _completed_files(plan: DownloadPlan) -> int:
        completed = 0
        for entry in plan.entries:
            try:
                path = plan.destination / Path(entry.path)
                if path.is_file() and path.stat().st_size >= entry.size:
                    completed += 1
            except (FileNotFoundError, PermissionError, OSError):
                continue
        return completed

    def _emit(self, kind: str, message: str = "", **values: Any) -> None:
        self.events.put(DownloadEvent(kind=kind, message=message, **values))


def format_bytes(value: float) -> str:
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    size = float(max(0, value))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "Б" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds > 365 * 24 * 3600:
        return "—"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {secs:02d} с"
    return f"{secs} с"


def friendly_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.lower()
    if "401" in text or "unauthorized" in lowered:
        return "Нет доступа (401). Укажите Hugging Face token или войдите в аккаунт."
    if "403" in text or "forbidden" in lowered or "gated" in lowered:
        return "Доступ запрещён (403). Примите условия репозитория и укажите token."
    if "404" in text or "not found" in lowered:
        return "Репозиторий, revision или файл не найден (404). Проверьте ссылку."
    if "disk" in lowered and ("space" in lowered or "мест" in lowered):
        return text
    if "timed out" in lowered or "timeout" in lowered or "stall" in lowered:
        return "Соединение или worker не проявляли activity слишком долго. Partial-данные сохранены. " + text
    return text[:1000]


def configure_hub_environment() -> None:
    """Configure metadata calls only; transport profiles belong to child workers."""
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "600")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


def list_repository_files(source: HubSource, token: str | None = None) -> list[RepositoryFile]:
    api = HfApi(token=token or None)
    repo_type = None if source.repo_type == "model" else source.repo_type
    path_in_repo = source.path if source.path else None
    if source.path_kind == "file" and source.path:
        entries = api.get_paths_info(
            repo_id=source.repo_id,
            paths=[source.path],
            revision=source.revision,
            repo_type=repo_type,
            token=token or None,
        )
    else:
        entries = api.list_repo_tree(
            repo_id=source.repo_id,
            path_in_repo=path_in_repo,
            recursive=True,
            revision=source.revision,
            repo_type=repo_type,
            token=token or None,
        )
    files = [
        RepositoryFile(path=str(item.path), size=int(getattr(item, "size", 0) or 0))
        for item in entries
        if not bool(getattr(item, "path", "").endswith("/")) and hasattr(item, "size")
    ]
    if source.path_kind == "file" and source.path:
        files = [item for item in files if item.path == source.path]
    return sorted(files, key=lambda item: item.path.casefold())
