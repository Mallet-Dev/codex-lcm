"""LLM-backed summarizer for codex-lcm compaction."""

from __future__ import annotations

import sys
from typing import Any, Optional

from .config import LCMConfig


LEAF_PROMPT = """Summarize the following conversation messages. Capture:
- Key user requests and decisions
- Technical details (file paths, APIs, config changes, error messages)
- Tool calls and their outcomes
- Any conclusions or action items

Use bullet points with timestamps. Be concise but preserve every important detail.
Do NOT include filler or meta-commentary. Target ~400-600 tokens.

Messages to summarize:

"""

AGGRESSIVE_LEAF_PROMPT = """Extract only the most durable facts from these messages:
- What was the user's goal?
- What was decided / concluded?
- What files/APIs/configs were involved?
- What errors occurred and how were they resolved?

Be extremely concise. Target ~200 tokens maximum.

Messages:

"""

CONDENSED_PROMPT = """You are condensing multiple summaries into a higher-level overview.
Combine the following summaries into a single coherent narrative that preserves:
- The arc of work across the time range
- Key decisions and their rationale
- Important file/API/config references
- Errors resolved and lessons learned

Target ~800-1200 tokens. Use structured prose with timestamp markers.

Summaries to condense:

"""


class Summarizer:
    """LLM-backed summarizer using OpenAI-compatible API."""

    def __init__(self, config: LCMConfig) -> None:
        self.config = config
        self._client: Any = None
        self._client_initialized = False

    @property
    def client(self) -> Any:
        if not self._client_initialized:
            self._client_initialized = True
            # No explicit credential → no client. This also prevents the OpenAI
            # SDK from implicitly pairing OPENAI_API_KEY with a scoped endpoint.
            if not self.config.api_key:
                return None
            try:
                from openai import OpenAI
                kwargs = {}
                if self.config.api_key:
                    kwargs["api_key"] = self.config.api_key
                if self.config.base_url:
                    kwargs["base_url"] = self.config.base_url
                self._client = OpenAI(**kwargs)
            except ImportError:
                print("WARNING: openai package not installed; summarizer will use truncation fallback", file=sys.stderr)
            except Exception as e:
                print(f"WARNING: OpenAI client init failed ({e}); using truncation fallback", file=sys.stderr)
        return self._client

    def summarize_leaf(self, messages_text: str, previous_context: Optional[str] = None) -> Optional[str]:
        """Summarize a chunk of raw messages into a leaf summary."""
        prompt = LEAF_PROMPT
        if previous_context:
            prompt += f"\n[Previous summary for context — do NOT repeat this information]:\n{previous_context}\n"
        prompt += messages_text

        return self._call_llm(self.config.compaction_model, prompt, temperature=0.2, max_tokens=600)

    def summarize_leaf_aggressive(self, messages_text: str) -> Optional[str]:
        """Aggressive leaf summary — tighter prompt, lower temperature."""
        prompt = AGGRESSIVE_LEAF_PROMPT + messages_text
        return self._call_llm(self.config.compaction_model, prompt, temperature=0.1, max_tokens=300)

    def summarize_condensed(self, summaries_text: str, depth: int) -> Optional[str]:
        """Condense multiple summaries into a higher-level summary."""
        prompt = CONDENSED_PROMPT
        if depth > 1:
            prompt += f"\n[This is a depth-{depth} condensation. Be progressively more abstract.]\n"
        prompt += summaries_text

        return self._call_llm(self.config.condensation_model, prompt, temperature=0.2, max_tokens=1200)

    def _call_llm(self, model: str, prompt: str, temperature: float, max_tokens: int) -> Optional[str]:
        client = self.client
        if client is None:
            return None  # triggers deterministic fallback

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=self.config.summary_timeout_ms / 1000,
            )
            text = resp.choices[0].message.content
            if text and len(text.strip()) > 20:
                return text.strip()
            return None
        except Exception as e:
            print(f"WARNING: LLM summarization failed: {e}", file=sys.stderr)
            return None


def deterministic_truncation(text: str, target_tokens: int = 512) -> str:
    """Fallback: deterministic truncation when LLM unavailable or fails."""
    target_chars = target_tokens * 4
    if len(text) <= target_chars:
        return text
    # Keep first and last portions
    keep = target_chars - 100
    half = keep // 2
    return text[:half] + "\n\n[...truncated for context management...]\n\n" + text[-half:]


def concatenate_messages(messages: list[dict], include_timestamps: bool = True) -> str:
    """Concatenate message dicts into text suitable for summarization."""
    parts = []
    for m in messages:
        ts = m.get("created_at", "")
        role = m.get("role", "unknown")
        content = m.get("content", "")
        if include_timestamps and ts:
            parts.append(f"[{ts}] {role}:\n{content}")
        else:
            parts.append(f"{role}:\n{content}")
    return "\n\n---\n\n".join(parts)


def concatenate_summaries(summaries: list[dict]) -> str:
    """Concatenate summary dicts into text suitable for condensation."""
    parts = []
    for s in summaries:
        sid = s.get("summary_id", "?")
        depth = s.get("depth", 0)
        content = s.get("content", "")
        earliest = s.get("earliest_at", "")
        latest = s.get("latest_at", "")
        parts.append(f"=== Summary {sid} (depth {depth}, {earliest} → {latest}) ===\n{content}")
    return "\n\n".join(parts)
