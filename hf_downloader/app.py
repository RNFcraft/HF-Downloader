from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .downloader import (
    DownloadEvent,
    DownloadManager,
    TRANSPORT_LABELS,
    configure_hub_environment,
    friendly_error,
    list_repository_files,
)
from .models import HubSource, destination_for, parse_huggingface_source


WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_DESTINATION = Path.home() / "Downloads" / "HuggingFace"
DEFAULT_SETTINGS: dict[str, Any] = {
    "settings_version": 3,
    "destination": str(DEFAULT_DESTINATION),
    "repo_type": "auto",
    "create_subfolder": True,
    "workers": 4,
    "retries": 6,
    "stall_timeout": 600,
    "transport": "auto",
    "exclude": "",
}


def _settings_file() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "HF Downloader" / "settings.json"
    return Path.home() / ".hf-downloader" / "settings.json"


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def load_settings(path: Path | None = None) -> dict[str, Any]:
    target = path or _settings_file()
    stored: dict[str, Any] = {}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            stored = value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    settings = DEFAULT_SETTINGS | stored
    settings["settings_version"] = 3
    settings["workers"] = _bounded_int(settings.get("workers"), 4, 1, 8)
    settings["retries"] = _bounded_int(settings.get("retries"), 6, 0, 10)
    settings["stall_timeout"] = _bounded_int(settings.get("stall_timeout"), 600, 60, 1800)
    settings["repo_type"] = settings.get("repo_type") if settings.get("repo_type") in {"auto", "model", "dataset", "space"} else "auto"
    settings["transport"] = settings.get("transport") if settings.get("transport") in TRANSPORT_LABELS else "auto"
    settings["destination"] = str(settings.get("destination") or DEFAULT_DESTINATION)
    settings["create_subfolder"] = bool(settings.get("create_subfolder", True))
    settings["exclude"] = str(settings.get("exclude") or "")
    return settings


def save_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    target = path or _settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def _source_payload(source: HubSource) -> dict[str, Any]:
    return {
        "repo_id": source.repo_id,
        "repo_type": source.repo_type,
        "revision": source.revision,
        "path": source.path,
        "path_kind": source.path_kind,
        "folder_name": source.folder_name,
    }


class DesktopApi:
    """Thread-safe API exposed to the local JavaScript frontend."""

    def __init__(self) -> None:
        self._events: queue.Queue[DownloadEvent] = queue.Queue()
        self._manager = DownloadManager(self._events)
        self._window: Any = None
        self._lock = threading.RLock()
        self._selected_source: HubSource | None = None
        self._available_files: dict[str, int] = {}

    def initial_state(self) -> dict[str, Any]:
        return {
            "settings": load_settings(),
            "transports": [{"value": key, "label": label} for key, label in TRANSPORT_LABELS.items()],
            "version": "1.1.2",
        }

    def inspect_source(self, value: str, repo_type: str = "auto", token: str = "") -> dict[str, Any]:
        try:
            source = parse_huggingface_source(value, repo_type)
            files = list_repository_files(source, token.strip() or None)
            with self._lock:
                self._selected_source = source
                self._available_files = {item.path: item.size for item in files}
            return {
                "ok": True,
                "source": _source_payload(source),
                "files": [asdict(item) for item in files],
            }
        except Exception as exc:
            return {"ok": False, "error": friendly_error(exc)}

    def choose_destination(self, current: str = "") -> str | None:
        if self._window is None:
            return None
        import webview

        result = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=current or str(DEFAULT_DESTINATION))
        return str(result[0]) if result else None

    def start_download(self, options: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._manager.active:
                raise ValueError("Загрузка уже выполняется.")
            source = parse_huggingface_source(str(options.get("url", "")), str(options.get("repo_type", "auto")))
            with self._lock:
                if self._selected_source != source:
                    raise ValueError("Список файлов устарел. Обновите его перед загрузкой.")
                available = self._available_files.copy()
            selected = [str(path) for path in options.get("selected_files", []) if str(path) in available]
            if not selected:
                raise ValueError("Выберите хотя бы один файл.")
            root = str(options.get("destination", "")).strip()
            if not root:
                raise ValueError("Выберите папку назначения.")
            create_subfolder = bool(options.get("create_subfolder", True))
            destination = destination_for(root, source, create_subfolder)
            workers = _bounded_int(options.get("workers"), 4, 1, 8)
            retries = _bounded_int(options.get("retries"), 6, 0, 10)
            timeout = _bounded_int(options.get("stall_timeout"), 600, 60, 1800)
            transport = str(options.get("transport", "auto"))
            if transport not in TRANSPORT_LABELS:
                transport = "auto"
            exclude_text = str(options.get("exclude", ""))
            excludes = [part.strip() for part in exclude_text.split(",") if part.strip()]
            settings = {
                "settings_version": 3,
                "destination": root,
                "repo_type": source.repo_type if str(options.get("repo_type")) != "auto" else "auto",
                "create_subfolder": create_subfolder,
                "workers": workers,
                "retries": retries,
                "stall_timeout": timeout,
                "transport": transport,
                "exclude": exclude_text,
            }
            save_settings(settings)
            self._manager.start(
                source,
                destination,
                token=str(options.get("token", "")).strip() or None,
                workers=workers,
                retries=retries,
                stall_timeout=timeout,
                transport=transport,  # type: ignore[arg-type]
                ignore_patterns=excludes or None,
                selected_files=sorted(selected),
            )
            return {"ok": True, "destination": str(destination)}
        except Exception as exc:
            return {"ok": False, "error": friendly_error(exc)}

    def poll_events(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            try:
                items.append(asdict(self._events.get_nowait()))
            except queue.Empty:
                return items

    def cancel_download(self) -> dict[str, Any]:
        if self._manager.active:
            self._manager.cancel()
        return {"ok": True}

    def open_destination(self, path: str) -> dict[str, Any]:
        try:
            target = Path(path).expanduser()
            target.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def shutdown(self) -> None:
        if self._manager.active:
            self._manager.shutdown(timeout=12)


def run() -> None:
    import webview

    configure_hub_environment()
    api = DesktopApi()
    entrypoint = WEB_DIR / "index.html"
    window = webview.create_window(
        "HF Downloader",
        url=str(entrypoint),
        js_api=api,
        width=1180,
        height=820,
        min_size=(860, 620),
        background_color="#081018",
        text_select=True,
    )
    api._window = window
    window.events.closing += api.shutdown
    webview.start(gui="edgechromium", debug=False)
