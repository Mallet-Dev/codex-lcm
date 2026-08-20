"""Shared pytest fixtures for codex-lcm."""

import os
import sys

import pytest

# Ensure the package is importable when running tests from the repo.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_lcm.config import LCMConfig  # noqa: E402
from codex_lcm.db import LCMDatabase  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isolated LCMDatabase with a small fresh tail so compaction triggers."""
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CODEX_LCM_FRESH_TAIL", "3")
    monkeypatch.setenv("CODEX_LCM_MAX_SUMMARY_CALLS", "50")
    config = LCMConfig()
    database = LCMDatabase(config)
    yield database
    database.close()


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CODEX_LCM_FRESH_TAIL", "3")
    return LCMConfig()


class MockSummarizer:
    """Drop-in Summarizer replacement with deterministic, compressing output.

    Returned content is kept above 50 chars (the engine's minimum) while still
    being much smaller than the raw input, so token savings are real.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _compress(self, text: str, prefix: str) -> str:
        self.calls.append(prefix)
        return f"{prefix}: {text[:80]}... [{len(text)}c]"

    def summarize_leaf(self, messages_text: str, previous_context=None):
        return self._compress(messages_text, "LEAF")

    def summarize_leaf_aggressive(self, messages_text: str):
        return self._compress(messages_text, "LEAF_AGGRESSIVE")

    def summarize_condensed(self, summaries_text: str, depth: int):
        return self._compress(summaries_text, f"CONDENSED:d{depth}")
