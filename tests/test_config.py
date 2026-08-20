"""Tests for provider credential and endpoint configuration."""

import sys
from types import SimpleNamespace

from codex_lcm.config import LCMConfig
from codex_lcm.summarizer import Summarizer


def _set_db_path(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "config.db"))


def test_scoped_provider_settings_take_precedence_as_a_pair(monkeypatch, tmp_path):
    _set_db_path(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "global-key-marker")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://global.example.invalid/v1")
    monkeypatch.setenv("CODEX_LCM_API_KEY", "scoped-key-marker")
    monkeypatch.setenv("CODEX_LCM_BASE_URL", "https://scoped.example.invalid/v1")

    config = LCMConfig()

    assert config.api_key == "scoped-key-marker"  # pragma: allowlist secret
    assert config.base_url == "https://scoped.example.invalid/v1"


def test_scoped_base_url_never_inherits_global_api_key(monkeypatch, tmp_path):
    _set_db_path(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "global-key-marker")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://global.example.invalid/v1")
    monkeypatch.delenv("CODEX_LCM_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_LCM_BASE_URL", "https://scoped.example.invalid/v1")

    config = LCMConfig()

    assert config.api_key is None
    assert config.base_url == "https://scoped.example.invalid/v1"
    initialized_with = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            initialized_with.append(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    assert Summarizer(config).client is None
    assert initialized_with == []


def test_global_provider_pair_is_used_without_scoped_settings(monkeypatch, tmp_path):
    _set_db_path(monkeypatch, tmp_path)
    monkeypatch.delenv("CODEX_LCM_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_LCM_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "global-key-marker")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://global.example.invalid/v1")

    config = LCMConfig()

    assert config.api_key == "global-key-marker"  # pragma: allowlist secret
    assert config.base_url == "https://global.example.invalid/v1"
