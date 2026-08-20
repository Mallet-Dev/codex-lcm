"""Config loading for codex-lcm."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path.home() / ".codex-lcm" / "codex-lcm.db"
DEFAULT_FRESH_TAIL = 25
DEFAULT_LEAF_CHUNK_TOKENS = 20_000
DEFAULT_LEAF_MIN_FANOUT = 3
DEFAULT_CONDENSED_MIN_FANOUT = 3
DEFAULT_MAX_ROUNDS = 5
DEFAULT_MAX_SUMMARY_CALLS = 12
DEFAULT_SUMMARY_CALL_WINDOW_MS = 600_000  # 10 minutes
DEFAULT_SUMMARY_TIMEOUT_MS = 60_000
DEFAULT_PROGRESS_THRESHOLD = 0.10  # stop if < 10% tokens saved
DEFAULT_ADDITIONAL_CONTEXT_LIMIT = 5000
DEFAULT_COMPACTION_MODEL = "gpt-4.1-mini"
DEFAULT_CONDENSATION_MODEL = "gpt-4.1"


class LCMConfig:
    """Codex-lcm configuration loaded from environment + defaults."""

    def __init__(self) -> None:
        self.db_path = Path(os.environ.get("CODEX_LCM_DB_PATH", DEFAULT_DB_PATH))

        self.fresh_tail = int(os.environ.get("CODEX_LCM_FRESH_TAIL", DEFAULT_FRESH_TAIL))
        self.leaf_chunk_tokens = int(os.environ.get("CODEX_LCM_LEAF_CHUNK_TOKENS", DEFAULT_LEAF_CHUNK_TOKENS))
        self.leaf_min_fanout = int(os.environ.get("CODEX_LCM_LEAF_MIN_FANOUT", DEFAULT_LEAF_MIN_FANOUT))
        self.condensed_min_fanout = int(os.environ.get("CODEX_LCM_CONDENSED_MIN_FANOUT", DEFAULT_CONDENSED_MIN_FANOUT))
        self.max_rounds = int(os.environ.get("CODEX_LCM_MAX_ROUNDS", DEFAULT_MAX_ROUNDS))
        self.max_summary_calls = int(os.environ.get("CODEX_LCM_MAX_SUMMARY_CALLS", DEFAULT_MAX_SUMMARY_CALLS))
        self.summary_call_window_ms = int(os.environ.get("CODEX_LCM_SUMMARY_CALL_WINDOW_MS", DEFAULT_SUMMARY_CALL_WINDOW_MS))
        self.summary_timeout_ms = int(os.environ.get("CODEX_LCM_SUMMARY_TIMEOUT_MS", DEFAULT_SUMMARY_TIMEOUT_MS))
        self.progress_threshold = float(os.environ.get("CODEX_LCM_PROGRESS_THRESHOLD", DEFAULT_PROGRESS_THRESHOLD))
        self.additional_context_limit = int(os.environ.get("CODEX_LCM_ADDITIONAL_CONTEXT_LIMIT", DEFAULT_ADDITIONAL_CONTEXT_LIMIT))

        # LLM model for compaction
        self.compaction_model = os.environ.get("CODEX_LCM_COMPACTION_MODEL", DEFAULT_COMPACTION_MODEL)
        self.condensation_model = os.environ.get("CODEX_LCM_CONDENSATION_MODEL", DEFAULT_CONDENSATION_MODEL)

        # Keep credentials paired with their endpoint namespace. Mixing a global
        # OPENAI_API_KEY with a scoped CODEX_LCM_BASE_URL could disclose the key
        # to an unintended provider.
        scoped_api_key = os.environ.get("CODEX_LCM_API_KEY")
        scoped_base_url = os.environ.get("CODEX_LCM_BASE_URL")
        if scoped_api_key or scoped_base_url:
            self.api_key = scoped_api_key or None
            self.base_url = scoped_base_url or None
        else:
            self.api_key = os.environ.get("OPENAI_API_KEY")
            self.base_url = os.environ.get("OPENAI_BASE_URL")

        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix" and self.db_path.parent == DEFAULT_DB_PATH.parent:
            os.chmod(self.db_path.parent, 0o700)

    @staticmethod
    def project_key(cwd: str, git_remote: Optional[str] = None) -> str:
        """Compute a stable project key from cwd + git remote."""
        raw = f"{cwd}:{git_remote or 'unknown'}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
