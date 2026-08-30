from pathlib import Path

import pytest

from hf_downloader.models import InvalidHuggingFaceURL, destination_for, parse_huggingface_source


@pytest.mark.parametrize(
    ("value", "repo_id", "repo_type", "revision", "path", "kind"),
    [
        ("google/gemma-3-1b-it", "google/gemma-3-1b-it", "model", "main", None, "repository"),
        ("https://huggingface.co/datasets/allenai/c4", "allenai/c4", "dataset", "main", None, "repository"),
        ("https://huggingface.co/org/repo/blob/main/config.json", "org/repo", "model", "main", "config.json", "file"),
        ("https://huggingface.co/org/repo/tree/dev/data/train", "org/repo", "model", "dev", "data/train", "folder"),
        ("hf://datasets/org/repo@v2/data/a.parquet", "org/repo", "dataset", "v2", "data/a.parquet", "file"),
        ("hf://models/org/repo", "org/repo", "model", "main", None, "repository"),
    ],
)
def test_parse_supported_sources(value, repo_id, repo_type, revision, path, kind):
    source = parse_huggingface_source(value)
    assert (source.repo_id, source.repo_type, source.revision, source.path, source.path_kind) == (
        repo_id, repo_type, revision, path, kind
    )


def test_selected_type_overrides_inference():
    assert parse_huggingface_source("org/repo", "dataset").repo_type == "dataset"


def test_rejects_non_huggingface_url():
    with pytest.raises(InvalidHuggingFaceURL):
        parse_huggingface_source("https://example.com/org/repo")


def test_rejects_parent_path():
    with pytest.raises(InvalidHuggingFaceURL):
        parse_huggingface_source("hf://org/repo/../secret")


def test_destination_is_repo_specific(tmp_path: Path):
    source = parse_huggingface_source("https://huggingface.co/datasets/org/repo")
    assert destination_for(tmp_path, source) == tmp_path.resolve() / "dataset--org--repo"
