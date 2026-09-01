import json
from types import SimpleNamespace

from hf_downloader.app import DesktopApi, WEB_DIR, load_settings, save_settings, update_settings
from hf_downloader.downloader import DownloadEvent
from hf_downloader.version import APP_VERSION


def test_invalid_settings_values_fall_back_to_safe_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"workers": "broken", "retries": None, "repo_type": "wrong", "transport": "turbo"}),
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings["workers"] == 4
    assert settings["retries"] == 6
    assert settings["repo_type"] == "auto"
    assert settings["transport"] == "auto"
    assert settings["check_for_updates"] is True
    assert settings["ignored_update_version"] is None


def test_settings_are_written_atomically(tmp_path):
    path = tmp_path / "settings.json"
    save_settings({"workers": 2}, path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"workers": 2}
    assert not path.with_suffix(".json.tmp").exists()


def test_updater_preferences_merge_without_losing_download_settings(tmp_path):
    path = tmp_path / "settings.json"
    save_settings({"destination": "D:/models", "workers": 7}, path)
    update_settings({"check_for_updates": False, "ignored_update_version": "1.2.0"}, path)
    settings = load_settings(path)
    assert settings["destination"] == "D:/models"
    assert settings["workers"] == 7
    assert settings["check_for_updates"] is False
    assert settings["ignored_update_version"] == "1.2.0"


def test_desktop_api_drains_download_events():
    api = DesktopApi()
    api._events.put(DownloadEvent(kind="status", message="ready"))
    assert api.poll_events()[0]["message"] == "ready"
    assert api.poll_events() == []


def test_close_waits_for_manager_shutdown():
    calls = []
    api = DesktopApi()
    api._manager = SimpleNamespace(active=True, shutdown=lambda timeout: calls.append(timeout))
    api.shutdown()
    assert calls == [12]


def test_web_ui_exposes_all_transport_profiles_and_telemetry():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for transport in ("auto", "xet_adaptive", "xet_conservative", "plain_http"):
        assert f'value="{transport}"' in html
    for element_id in ("attempt-value", "active-transport", "worker-state", "heartbeat", "current-file", "event-log"):
        assert f'id="{element_id}"' in html
    assert "Content-Security-Policy" not in html
    for element_id in ("check-update", "auto-update-check", "update-modal", "update-progress-bar", "update-install"):
        assert f'id="{element_id}"' in html


def test_initial_state_contains_every_backend_transport():
    state = DesktopApi().initial_state()
    assert {item["value"] for item in state["transports"]} == {
        "auto", "xet_adaptive", "xet_conservative", "plain_http"
    }


def test_version_has_one_source_of_truth():
    root = WEB_DIR.parents[1]
    assert APP_VERSION == (root / "VERSION").read_text(encoding="utf-8").strip()
    assert '#define AppVersion "' not in (root / "installer" / "HFDownloader.iss").read_text(encoding="utf-8")
    assert 'Get-Content -LiteralPath (Join-Path $projectDir "VERSION")' in (root / "build.ps1").read_text(encoding="utf-8")
