from types import SimpleNamespace

from hf_downloader import app
from hf_downloader.app import HfDownloaderApp, currently_downloading
from hf_downloader.downloader import FileProgress


class Value:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class Button:
    def __init__(self):
        self.state = None

    def configure(self, **kwargs):
        self.state = kwargs.get("state")


def test_close_waits_for_manager_shutdown(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        manager=SimpleNamespace(active=True, shutdown=lambda timeout: calls.append(timeout) or True),
        status_var=Value(),
        start_button=Button(),
        stop_button=Button(),
        update_idletasks=lambda: calls.append("update"),
        destroy=lambda: calls.append("destroy"),
    )
    monkeypatch.setattr(app.messagebox, "askyesno", lambda *_args, **_kwargs: True)
    HfDownloaderApp._close(fake)
    assert calls == ["update", 12, "destroy"]
    assert fake.start_button.state == "disabled"


def test_active_file_list_hides_completed_downloads():
    files = (
        FileProgress("active.safetensors", 25, 100),
        FileProgress("complete.json", 500, 500),
        FileProgress("starting.bin", 0, 0),
    )

    assert currently_downloading(files) == (files[0], files[2])
