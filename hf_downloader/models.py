from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse


class InvalidHuggingFaceURL(ValueError):
    """Raised when a value cannot be interpreted as a Hub repository URL."""


@dataclass(frozen=True, slots=True)
class HubSource:
    repo_id: str
    repo_type: str = "model"
    revision: str = "main"
    path: str | None = None
    path_kind: str = "repository"

    @property
    def folder_name(self) -> str:
        prefix = {"dataset": "dataset--", "space": "space--"}.get(self.repo_type, "")
        return prefix + self.repo_id.replace("/", "--")

    @property
    def allow_patterns(self) -> str | None:
        if not self.path:
            return None
        return self.path if self.path_kind == "file" else f"{self.path.rstrip('/')}/*"


_VALID_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate(repo_id: str, repo_type: str, revision: str, path: str | None, kind: str) -> HubSource:
    parts = repo_id.split("/")
    if len(parts) != 2 or not all(_VALID_PART.fullmatch(part) for part in parts):
        raise InvalidHuggingFaceURL("Ожидается репозиторий вида автор/название.")
    if repo_type not in {"model", "dataset", "space"}:
        raise InvalidHuggingFaceURL("Поддерживаются модели, датасеты и Spaces.")
    if not revision or any(char in revision for char in "\\\0"):
        raise InvalidHuggingFaceURL("Некорректная ревизия репозитория.")
    normalized_path = None
    if path:
        pure = PurePosixPath(path.strip("/"))
        if pure.is_absolute() or ".." in pure.parts:
            raise InvalidHuggingFaceURL("Некорректный путь внутри репозитория.")
        normalized_path = pure.as_posix()
    return HubSource(repo_id, repo_type, revision, normalized_path, kind)


def parse_huggingface_source(value: str, selected_type: str = "auto") -> HubSource:
    """Parse web URLs, hf:// URIs, or a bare owner/repo id."""
    raw = value.strip()
    if not raw:
        raise InvalidHuggingFaceURL("Вставьте ссылку Hugging Face или ID репозитория.")

    if raw.startswith("hf://"):
        body = raw[5:].split("?", 1)[0]
        repo_type = "model"
        if body.startswith("datasets/"):
            repo_type, body = "dataset", body[9:]
        elif body.startswith("spaces/"):
            repo_type, body = "space", body[7:]
        elif body.startswith("models/"):
            repo_type, body = "model", body[7:]
        pieces = body.split("/")
        if len(pieces) < 2:
            raise InvalidHuggingFaceURL("Неполный hf:// адрес.")
        owner = pieces[0]
        repo_revision = pieces[1]
        repo, sep, revision = repo_revision.partition("@")
        embedded_path = "/".join(pieces[2:]) or None
        kind = "folder" if raw.split("?", 1)[0].endswith("/") else ("file" if embedded_path else "repository")
        return _validate(f"{owner}/{repo}", _chosen_type(selected_type, repo_type), unquote(revision) if sep else "main", embedded_path, kind)

    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        if parsed.hostname not in {"huggingface.co", "www.huggingface.co"}:
            raise InvalidHuggingFaceURL("Допускаются только ссылки с huggingface.co.")
        pieces = [unquote(part) for part in parsed.path.split("/") if part]
        repo_type = "model"
        if pieces and pieces[0] in {"datasets", "spaces", "models"}:
            marker = pieces.pop(0)
            repo_type = {"datasets": "dataset", "spaces": "space", "models": "model"}[marker]
        if len(pieces) < 2:
            raise InvalidHuggingFaceURL("В ссылке отсутствует автор или название репозитория.")
        repo_id = "/".join(pieces[:2])
        rest = pieces[2:]
        revision, inner_path, kind = "main", None, "repository"
        if rest and rest[0] in {"blob", "resolve", "tree"}:
            action = rest[0]
            if len(rest) < 2:
                raise InvalidHuggingFaceURL("В ссылке отсутствует ревизия.")
            revision = rest[1]
            inner_path = "/".join(rest[2:]) or None
            kind = "folder" if action == "tree" else "file"
        return _validate(repo_id, _chosen_type(selected_type, repo_type), revision, inner_path, kind)

    bare = raw.removeprefix("datasets/").removeprefix("models/").removeprefix("spaces/")
    inferred = "dataset" if raw.startswith("datasets/") else "space" if raw.startswith("spaces/") else "model"
    return _validate(bare.rstrip("/"), _chosen_type(selected_type, inferred), "main", None, "repository")


def _chosen_type(selected: str, inferred: str) -> str:
    return inferred if selected == "auto" else selected


def destination_for(root: str | Path, source: HubSource, create_subfolder: bool = True) -> Path:
    base = Path(root).expanduser().resolve()
    return base / source.folder_name if create_subfolder else base
