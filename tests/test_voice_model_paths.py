"""The voice downloader and the voice server must agree on where models live.

Separate from test_neural_tts.py on purpose: that module skips itself unless the
60 MB voice files are on disk, and these are the tests that must run on a clean
machine — they are about *where* the files belong, not about the files.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

from agent import neural_tts as nt

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _fetch_module():
    """Import scripts/fetch_neural_voices.py without running its main()."""
    spec = importlib.util.spec_from_file_location(
        "fetch_neural_voices", ROOT / "scripts" / "fetch_neural_voices.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_downloader_and_server_share_one_default_directory():
    """Regression: the downloader hardcoded data/models/piper while the server
    resolved PIPER_MODELS_DIR. Setting the variable therefore moved the server
    but not the download, so 181 MB of models landed inside the repo — which is
    the workspace's whole persistence budget, and silently evicted source files.
    """
    assert _fetch_module().DEFAULT_DIR == nt.DEFAULT_MODELS_DIR


def test_downloader_honours_piper_models_dir(monkeypatch, tmp_path):
    """The same environment variable must move both halves."""
    monkeypatch.setenv("PIPER_MODELS_DIR", str(tmp_path))

    assert nt.models_dir() == str(tmp_path), "the server resolves the variable"
    assert _fetch_module()._resolved_dir() == str(tmp_path), \
        "and so must the downloader"


def test_the_resolved_default_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("PIPER_MODELS_DIR", raising=False)
    assert _fetch_module()._resolved_dir() == nt.DEFAULT_MODELS_DIR
