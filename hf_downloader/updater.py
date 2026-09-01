from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from packaging.version import InvalidVersion, Version

from .version import APP_VERSION


GITHUB_OWNER = "RNFcraft"
GITHUB_REPOSITORY = "HF-Downloader"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
RELEASE_PATH_PREFIX = f"/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/download/"
CHECK_INTERVAL_SECONDS = 6 * 60 * 60
NETWORK_TIMEOUT_SECONDS = 15
MAX_RELEASE_RESPONSE_BYTES = 2 * 1024 * 1024
STRICT_CHECKSUM = False


class UpdateState(str, Enum):
    IDLE = "IDLE"
    CHECKING = "CHECKING"
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    NO_UPDATE = "NO_UPDATE"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING = "VERIFYING"
    READY_TO_INSTALL = "READY_TO_INSTALL"
    INSTALLING = "INSTALLING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class UpdateErrorCode(str, Enum):
    NETWORK_ERROR = "NETWORK_ERROR"
    API_ERROR = "API_ERROR"
    INVALID_RELEASE = "INVALID_RELEASE"
    INSTALLER_NOT_FOUND = "INSTALLER_NOT_FOUND"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    HASH_MISMATCH = "HASH_MISMATCH"
    INSTALL_FAILED = "INSTALL_FAILED"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    CANCELLED = "CANCELLED"


class UpdateError(RuntimeError):
    def __init__(self, code: UpdateErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UpdateInfo:
    available: bool
    current_version: str
    latest_version: str
    release_name: str
    changelog: str
    published_at: str
    installer_name: str
    installer_url: str
    checksum_url: str | None
    expected_sha256: str | None
    release_url: str
    download_size: int


def normalize_version(value: str) -> str:
    normalized = value.strip()
    if normalized.lower().startswith("v"):
        normalized = normalized[1:]
    return normalized


def is_newer_version(current: str, latest: str) -> bool:
    try:
        return Version(normalize_version(latest)) > Version(normalize_version(current))
    except InvalidVersion as exc:
        raise UpdateError(UpdateErrorCode.INVALID_RELEASE, "Release содержит некорректный номер версии.") from exc


def _release_version(payload: dict[str, Any], assets: list[dict[str, Any]]) -> tuple[str, Version]:
    candidates = [str(payload.get("tag_name") or ""), str(payload.get("name") or "")]
    candidates.extend(str(asset.get("name") or "") for asset in assets)
    for candidate in candidates:
        normalized = normalize_version(candidate)
        try:
            return normalized, Version(normalized)
        except InvalidVersion:
            match = re.search(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?!\d)", candidate)
            if match:
                try:
                    return match.group(1), Version(match.group(1))
                except InvalidVersion:
                    continue
    raise UpdateError(UpdateErrorCode.INVALID_RELEASE, "Release не содержит корректный номер версии.")


def is_frozen_build() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_installed_build() -> bool:
    if not is_frozen_build() or sys.platform != "win32":
        return False
    executable = Path(sys.executable).resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        expected = Path(local_app_data) / "Programs" / "HF Downloader"
        try:
            executable.relative_to(expected.resolve())
            return True
        except ValueError:
            pass
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{A51556B3-661B-4BA1-A2E8-BBD75B1B8D41}_is1"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            install_location = Path(winreg.QueryValueEx(key, "InstallLocation")[0]).resolve()
        executable.relative_to(install_location)
        return True
    except (OSError, ValueError, ImportError):
        return False


def _updates_directory() -> Path:
    return Path(tempfile.gettempdir()) / "HF Downloader Update"


def _logger() -> logging.Logger:
    logger = logging.getLogger("hf_downloader.updater")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "HF Downloader" / "logs"
    try:
        base.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(base / "updater.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [Updater] %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())
    return logger


LOGGER = _logger()


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"HF-Downloader/{APP_VERSION}",
        },
    )


def _trusted_asset_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "github.com" and parsed.path.startswith(RELEASE_PATH_PREFIX)


def _asset_digest(asset: dict[str, Any]) -> str | None:
    digest = str(asset.get("digest") or "")
    if digest.lower().startswith("sha256:") and re.fullmatch(r"[0-9a-fA-F]{64}", digest[7:]):
        return digest[7:].lower()
    return None


def parse_release(payload: dict[str, Any], current_version: str = APP_VERSION) -> UpdateInfo:
    if payload.get("draft") or payload.get("prerelease"):
        raise UpdateError(UpdateErrorCode.INVALID_RELEASE, "GitHub вернул нестабильный или черновой release.")
    assets = [item for item in payload.get("assets", []) if isinstance(item, dict)]
    latest, parsed_version = _release_version(payload, assets)
    if parsed_version.is_prerelease:
        raise UpdateError(UpdateErrorCode.INVALID_RELEASE, "Pre-release обновления отключены.")

    expected_name = f"HF-Downloader-Setup-{latest}.exe"
    installer = next((item for item in assets if item.get("name") == expected_name), None)
    if installer is None:
        installer = next(
            (item for item in assets if str(item.get("name", "")).lower().endswith(".exe") and "hf-downloader-setup" in str(item.get("name", "")).lower()),
            None,
        )
    if installer is None:
        raise UpdateError(UpdateErrorCode.INSTALLER_NOT_FOUND, "В GitHub Release отсутствует Windows installer.")
    installer_name = str(installer.get("name") or "")
    if Path(installer_name).name != installer_name or not installer_name.lower().endswith(".exe"):
        raise UpdateError(UpdateErrorCode.INVALID_RELEASE, "Installer имеет небезопасное имя файла.")
    installer_url = str(installer.get("browser_download_url") or "")
    if not _trusted_asset_url(installer_url):
        raise UpdateError(UpdateErrorCode.INVALID_RELEASE, "Installer имеет недоверенный адрес загрузки.")

    checksum_name = f"{installer_name}.sha256"
    checksum = next((item for item in assets if item.get("name") == checksum_name), None)
    checksum_url = str(checksum.get("browser_download_url")) if checksum else None
    if checksum_url and not _trusted_asset_url(checksum_url):
        checksum_url = None
    release_url = str(payload.get("html_url") or "")
    release_parsed = urlparse(release_url)
    if release_parsed.scheme != "https" or release_parsed.hostname != "github.com" or not release_parsed.path.startswith(f"/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/"):
        release_url = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases"

    return UpdateInfo(
        available=is_newer_version(current_version, latest),
        current_version=current_version,
        latest_version=latest,
        release_name=str(payload.get("name") or f"HF Downloader v{latest}"),
        changelog=str(payload.get("body") or "Описание изменений не опубликовано."),
        published_at=str(payload.get("published_at") or ""),
        installer_name=installer_name,
        installer_url=installer_url,
        checksum_url=checksum_url,
        expected_sha256=_asset_digest(installer),
        release_url=release_url,
        download_size=max(0, int(installer.get("size") or 0)),
    )


def check_for_update(current_version: str = APP_VERSION) -> UpdateInfo:
    LOGGER.info("Current version: %s; checking GitHub Releases", current_version)
    try:
        with urllib.request.urlopen(_request(GITHUB_API_URL), timeout=NETWORK_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(UpdateErrorCode.NETWORK_ERROR, "Не удалось подключиться к GitHub Releases.") from exc
    if len(raw) > MAX_RELEASE_RESPONSE_BYTES:
        raise UpdateError(UpdateErrorCode.API_ERROR, "Ответ GitHub Releases слишком большой.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(UpdateErrorCode.API_ERROR, "GitHub вернул некорректный ответ.") from exc
    if not isinstance(payload, dict):
        raise UpdateError(UpdateErrorCode.API_ERROR, "GitHub вернул неожиданный формат ответа.")
    info = parse_release(payload, current_version)
    LOGGER.info("Latest version: %s; available=%s", info.latest_version, info.available)
    return info


class UpdateManager:
    def __init__(self, *, strict_checksum: bool = STRICT_CHECKSUM) -> None:
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._info: UpdateInfo | None = None
        self._installer_path: Path | None = None
        self._strict_checksum = strict_checksum
        self._status: dict[str, Any] = {
            "state": UpdateState.IDLE,
            "message": "",
            "error_code": None,
            "downloaded": 0,
            "total": 0,
            "checksum_verified": False,
            "checksum_missing": False,
            "manual": False,
        }

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._status["state"] in {UpdateState.CHECKING, UpdateState.DOWNLOADING, UpdateState.VERIFYING, UpdateState.INSTALLING}

    def status(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self._status)
            payload["state"] = payload["state"].value
            payload["info"] = asdict(self._info) if self._info else None
            payload["can_install"] = is_installed_build()
            return payload

    def _set(self, state: UpdateState, message: str = "", **values: Any) -> None:
        with self._lock:
            self._status.update(state=state, message=message, **values)

    def _fail(self, exc: BaseException) -> None:
        code = exc.code if isinstance(exc, UpdateError) else UpdateErrorCode.API_ERROR
        LOGGER.warning("%s: %s", code, exc)
        self._set(UpdateState.FAILED, str(exc), error_code=code.value)

    def start_check(self, *, manual: bool = False, ignored_version: str | None = None, callback: Callable[[UpdateInfo | None], None] | None = None) -> bool:
        if self.busy:
            return False
        self._set(UpdateState.CHECKING, "Проверяем GitHub Releases…", manual=manual, error_code=None)

        def work() -> None:
            info: UpdateInfo | None = None
            try:
                info = check_for_update()
                with self._lock:
                    self._info = info
                if info.available and info.latest_version != ignored_version:
                    self._set(UpdateState.UPDATE_AVAILABLE, f"Доступна версия {info.latest_version}.")
                else:
                    self._set(UpdateState.NO_UPDATE, "Установлена актуальная версия.")
            except BaseException as exc:
                self._fail(exc)
            finally:
                if callback:
                    callback(info)

        self._thread = threading.Thread(target=work, name="update-check", daemon=True)
        self._thread.start()
        return True

    def start_download(self) -> bool:
        with self._lock:
            if self.busy or self._info is None or not self._info.available:
                return False
            info = self._info
        self._cancel.clear()
        self._set(UpdateState.DOWNLOADING, "Скачиваем обновление…", downloaded=0, total=info.download_size, error_code=None)

        def work() -> None:
            partial: Path | None = None
            try:
                directory = _updates_directory()
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / info.installer_name
                partial = target.with_suffix(target.suffix + ".part")
                partial.unlink(missing_ok=True)
                request = urllib.request.Request(info.installer_url, headers={"User-Agent": f"HF-Downloader/{APP_VERSION}"})
                with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response, partial.open("wb") as output:
                    total = int(response.headers.get("Content-Length") or info.download_size or 0)
                    downloaded = 0
                    while True:
                        if self._cancel.is_set():
                            raise UpdateError(UpdateErrorCode.CANCELLED, "Скачивание обновления отменено.")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded += len(chunk)
                        self._set(UpdateState.DOWNLOADING, "Скачиваем обновление…", downloaded=downloaded, total=total)
                if total and downloaded != total:
                    raise UpdateError(UpdateErrorCode.DOWNLOAD_FAILED, "Скачивание installer оборвалось до завершения.")
                os.replace(partial, target)
                partial = None
                self._set(UpdateState.VERIFYING, "Проверяем SHA-256…")
                expected = info.expected_sha256 or self._download_checksum(info)
                if expected:
                    actual = self._sha256(target)
                    if actual.lower() != expected.lower():
                        target.unlink(missing_ok=True)
                        raise UpdateError(UpdateErrorCode.HASH_MISMATCH, "Проверка SHA-256 не пройдена. Installer удалён.")
                    verified, missing = True, False
                    LOGGER.info("SHA-256 verified")
                elif self._strict_checksum:
                    target.unlink(missing_ok=True)
                    raise UpdateError(UpdateErrorCode.HASH_MISMATCH, "Release не содержит обязательную SHA-256 сумму.")
                else:
                    verified, missing = False, True
                    LOGGER.warning("Checksum is absent; continuing in non-strict mode")
                with self._lock:
                    self._installer_path = target
                self._set(UpdateState.READY_TO_INSTALL, "Обновление готово к установке.", checksum_verified=verified, checksum_missing=missing)
            except UpdateError as exc:
                if partial:
                    partial.unlink(missing_ok=True)
                if exc.code == UpdateErrorCode.CANCELLED:
                    self._set(UpdateState.CANCELLED, str(exc), error_code=exc.code.value, downloaded=0)
                else:
                    self._fail(exc)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if partial:
                    partial.unlink(missing_ok=True)
                self._fail(UpdateError(UpdateErrorCode.DOWNLOAD_FAILED, "Не удалось скачать обновление."))
                LOGGER.warning("Download failed: %r", exc)

        self._thread = threading.Thread(target=work, name="update-download", daemon=True)
        self._thread.start()
        return True

    def _download_checksum(self, info: UpdateInfo) -> str | None:
        if not info.checksum_url:
            return None
        try:
            request = urllib.request.Request(info.checksum_url, headers={"User-Agent": f"HF-Downloader/{APP_VERSION}"})
            with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
                text = response.read(4096).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateError(UpdateErrorCode.DOWNLOAD_FAILED, "Не удалось получить SHA-256 файл.") from exc
        match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
        if not match:
            raise UpdateError(UpdateErrorCode.INVALID_RELEASE, "SHA-256 файл имеет некорректный формат.")
        return match.group(1).lower()

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                if self._cancel.is_set():
                    path.unlink(missing_ok=True)
                    raise UpdateError(UpdateErrorCode.CANCELLED, "Проверка обновления отменена.")
                digest.update(chunk)
        return digest.hexdigest()

    def cancel_download(self) -> bool:
        with self._lock:
            cancellable = self._status["state"] in {UpdateState.DOWNLOADING, UpdateState.VERIFYING}
        if cancellable:
            self._cancel.set()
        return cancellable

    def dismiss(self, version: str) -> None:
        with self._lock:
            matches = self._info is not None and self._info.latest_version == version
        if matches:
            self._set(UpdateState.NO_UPDATE, "Эта версия обновления пропущена.")

    def shutdown(self, timeout: float = 3.0) -> None:
        self._cancel.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def launch_installer(self) -> None:
        with self._lock:
            path = self._installer_path
            ready = self._status["state"] == UpdateState.READY_TO_INSTALL
        if not ready or path is None or not path.is_file():
            raise UpdateError(UpdateErrorCode.INSTALL_FAILED, "Проверенный installer не найден.")
        if not is_installed_build():
            raise UpdateError(UpdateErrorCode.INSTALL_FAILED, "Автоустановка доступна только для установленной версии.")
        try:
            subprocess.Popen([str(path), "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"], close_fds=True)
        except (OSError, PermissionError) as exc:
            raise UpdateError(UpdateErrorCode.PERMISSION_ERROR, "Windows не разрешила запустить installer.") from exc
        LOGGER.info("Starting installer: %s", path.name)
        self._set(UpdateState.INSTALLING, "Installer запущен.")

    def open_release(self) -> bool:
        with self._lock:
            url = self._info.release_url if self._info else f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases"
        return webbrowser.open(url)

    def cleanup_old_update_files(self, *, max_age_seconds: int = 24 * 60 * 60) -> None:
        directory = _updates_directory()
        try:
            resolved = directory.resolve()
            expected = (Path(tempfile.gettempdir()) / "HF Downloader Update").resolve()
            if resolved != expected or not directory.is_dir():
                return
            cutoff = time.time() - max_age_seconds
            for pattern in ("HF-Downloader-Setup-*.exe", "HF-Downloader-Setup-*.exe.part"):
                for path in directory.glob(pattern):
                    try:
                        if path.is_file() and path.stat().st_mtime < cutoff:
                            path.unlink()
                    except OSError:
                        continue
        except OSError:
            return
