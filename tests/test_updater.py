import hashlib
import io
import logging
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from hf_downloader.app import DesktopApi
from hf_downloader import updater
from hf_downloader.updater import (
    UpdateError,
    UpdateErrorCode,
    UpdateManager,
    UpdateState,
    is_newer_version,
    parse_release,
)


@pytest.fixture(autouse=True)
def disable_persistent_updater_log(monkeypatch):
    logger = logging.getLogger("hf_downloader.updater.tests")
    logger.disabled = True
    monkeypatch.setattr(updater, "LOGGER", logger)


def release_payload(version="1.2.0", *, installer=True, checksum=False, digest=None, prerelease=False):
    name = f"HF-Downloader-Setup-{version}.exe"
    assets = []
    if installer:
        asset = {
            "name": name,
            "browser_download_url": f"https://github.com/RNFcraft/HF-Downloader/releases/download/v{version}/{name}",
            "size": 7,
        }
        if digest:
            asset["digest"] = f"sha256:{digest}"
        assets.append(asset)
    if checksum:
        assets.append({
            "name": f"{name}.sha256",
            "browser_download_url": f"https://github.com/RNFcraft/HF-Downloader/releases/download/v{version}/{name}.sha256",
            "size": 90,
        })
    return {
        "tag_name": f"v{version}",
        "name": f"HF Downloader v{version}",
        "body": "<b>text only</b>\n- fixed",
        "published_at": "2026-09-01T00:00:00Z",
        "html_url": f"https://github.com/RNFcraft/HF-Downloader/releases/tag/v{version}",
        "draft": False,
        "prerelease": prerelease,
        "assets": assets,
    }


class FakeResponse:
    def __init__(self, data: bytes, content_length=None, on_read=None):
        self._stream = io.BytesIO(data)
        self.headers = {"Content-Length": str(content_length if content_length is not None else len(data))}
        self._on_read = on_read

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        if self._on_read:
            callback, self._on_read = self._on_read, None
            callback()
        return self._stream.read(size)


def wait_for_state(manager, states, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = manager.status()
        if status["state"] in states:
            return status
        time.sleep(0.01)
    raise AssertionError(f"Updater did not reach {states}: {manager.status()}")


@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [("1.1.2", "1.1.2", False), ("1.1.2", "1.1.3", True), ("1.9.0", "1.10.0", True)],
)
def test_semantic_version_comparison(current, latest, expected):
    assert is_newer_version(current, latest) is expected


def test_prerelease_and_missing_installer_are_rejected():
    with pytest.raises(UpdateError) as prerelease:
        parse_release(release_payload(prerelease=True), "1.1.2")
    assert prerelease.value.code == UpdateErrorCode.INVALID_RELEASE
    with pytest.raises(UpdateError) as missing:
        parse_release(release_payload(installer=False), "1.1.2")
    assert missing.value.code == UpdateErrorCode.INSTALLER_NOT_FOUND


def test_version_falls_back_to_release_name_for_legacy_tag():
    payload = release_payload("1.1.2")
    payload["tag_name"] = "Release-version"
    info = parse_release(payload, "1.1.1")
    assert info.latest_version == "1.1.2"
    assert info.available is True


def test_release_asset_must_belong_to_official_repository():
    payload = release_payload()
    payload["assets"][0]["browser_download_url"] = "https://example.com/update.exe"
    with pytest.raises(UpdateError) as error:
        parse_release(payload, "1.1.2")
    assert error.value.code == UpdateErrorCode.INVALID_RELEASE


def test_network_failure_is_non_fatal_manager_state(monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    manager = UpdateManager()
    assert manager.start_check(manual=True)
    status = wait_for_state(manager, {"FAILED"})
    assert status["error_code"] == "NETWORK_ERROR"


def test_interrupted_download_removes_partial_file(monkeypatch, tmp_path):
    manager = UpdateManager()
    manager._info = parse_release(release_payload(), "1.1.2")
    manager._set(UpdateState.UPDATE_AVAILABLE)
    monkeypatch.setattr(updater, "_updates_directory", lambda: tmp_path)
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(b"short", content_length=20))
    assert manager.start_download()
    status = wait_for_state(manager, {"FAILED"})
    assert status["error_code"] == "DOWNLOAD_FAILED"
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.exe"))


def test_hash_mismatch_deletes_installer(monkeypatch, tmp_path):
    manager = UpdateManager()
    manager._info = parse_release(release_payload(digest="0" * 64), "1.1.2")
    manager._set(UpdateState.UPDATE_AVAILABLE)
    monkeypatch.setattr(updater, "_updates_directory", lambda: tmp_path)
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(b"payload"))
    assert manager.start_download()
    status = wait_for_state(manager, {"FAILED"})
    assert status["error_code"] == "HASH_MISMATCH"
    assert not list(tmp_path.glob("*.exe"))


def test_valid_checksum_reaches_ready_state(monkeypatch, tmp_path):
    data = b"payload"
    digest = hashlib.sha256(data).hexdigest()
    manager = UpdateManager(strict_checksum=True)
    manager._info = parse_release(release_payload(digest=digest), "1.1.2")
    manager._set(UpdateState.UPDATE_AVAILABLE)
    monkeypatch.setattr(updater, "_updates_directory", lambda: tmp_path)
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(data))
    assert manager.start_download()
    status = wait_for_state(manager, {"READY_TO_INSTALL"})
    assert status["checksum_verified"] is True
    assert list(tmp_path.glob("*.exe"))


def test_user_cancel_removes_partial_and_keeps_manager_usable(monkeypatch, tmp_path):
    manager = UpdateManager()
    manager._info = parse_release(release_payload(), "1.1.2")
    manager._set(UpdateState.UPDATE_AVAILABLE)
    monkeypatch.setattr(updater, "_updates_directory", lambda: tmp_path)
    response = FakeResponse(b"payload", on_read=manager.cancel_download)
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_a, **_k: response)
    assert manager.start_download()
    status = wait_for_state(manager, {"CANCELLED"})
    assert status["error_code"] == "CANCELLED"
    assert not list(tmp_path.glob("*"))


def test_install_requires_confirmation_when_hf_download_is_active():
    api = DesktopApi()
    api._manager = SimpleNamespace(active=True)
    result = api.install_update(False)
    assert result == {"ok": False, "requires_download_stop": True}


def test_changelog_is_rendered_as_text_not_raw_html():
    script = (Path(__file__).parents[1] / "hf_downloader" / "web" / "app.js").read_text(encoding="utf-8")
    assert "update-changelog'].textContent" in script
    assert "update-changelog'].innerHTML" not in script
