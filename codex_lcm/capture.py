"""Turn capture — persist user prompts, assistant messages, and tool activity."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .config import LCMConfig
from .db import LCMDatabase
from .secrets import redact

# Bound the searchable preview. Full redacted payloads live in message_parts.
DEFAULT_TOOL_RESULT_MAX_CHARS = 2000
DEFAULT_TOOL_INPUT_MAX_CHARS = 2000


class Capturer:
    """Captures turn events into the LCM database."""

    def __init__(self, db: LCMDatabase, config: LCMConfig) -> None:
        self.db = db
        self.config = config
        self.tool_result_max = int(
            os.environ.get("CODEX_LCM_TOOL_RESULT_MAX_CHARS", DEFAULT_TOOL_RESULT_MAX_CHARS)
        )
        self.tool_input_max = int(
            os.environ.get("CODEX_LCM_TOOL_INPUT_MAX_CHARS", DEFAULT_TOOL_INPUT_MAX_CHARS)
        )

    def capture_user(self, session_id: str, prompt: str, turn_id: Optional[str] = None) -> None:
        if not prompt:
            return
        stable_key = f"user:{turn_id}" if turn_id else None
        self.db.append_message(
            session_id=session_id,
            role="user",
            content=redact(prompt),
            stable_key=stable_key,
        )

    def capture_assistant(self, session_id: str, message: str, turn_id: Optional[str] = None) -> None:
        if not message:
            return
        stable_key = f"assistant:{turn_id}" if turn_id else None
        self.db.append_message(
            session_id=session_id,
            role="assistant",
            content=redact(message),
            stable_key=stable_key,
        )

    def capture_tool(
        self,
        session_id: str,
        tool_name: str,
        tool_input: Any,
        tool_response: Any,
        tool_use_id: Optional[str] = None,
    ) -> None:
        input_str = redact(self._serialize(tool_input))
        result_str = redact(self._serialize(tool_response))

        content = json.dumps(
            {
                "tool": tool_name,
                "input": self._preview(input_str, self.tool_input_max),
                "result": self._preview(result_str, self.tool_result_max),
            },
            ensure_ascii=False,
        )

        stable_key = f"tool:{tool_use_id}" if tool_use_id else None
        self.db.append_message(
            session_id=session_id,
            role="tool",
            content=content,
            stable_key=stable_key,
            parts=[
                {"kind": "tool_input", "content": input_str},
                {"kind": "tool_result", "content": result_str},
            ],
        )

    @staticmethod
    def _serialize(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                text = str(value)
        return text

    @staticmethod
    def _preview(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        omitted = len(text) - max_chars
        return text[:max_chars] + f"\n...[truncated preview; {omitted} chars retained in message_parts]..."
