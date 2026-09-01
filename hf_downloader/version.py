from __future__ import annotations

import sys
from pathlib import Path


def _version_file() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "VERSION"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent / "VERSION"


def get_current_version() -> str:
    version = _version_file().read_text(encoding="utf-8").strip()
    if not version:
        raise RuntimeError("VERSION is empty")
    return version


APP_VERSION = get_current_version()
