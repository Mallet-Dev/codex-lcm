"""Context assembler — builds the restore text from the DAG at session start."""

from __future__ import annotations

from typing import Optional
from .db import LCMDatabase
from .config import LCMConfig


RESTORE_HEADER = "<lc-restore>"
RESTORE_FOOTER = "</lc-restore>"
COMPACT_REMINDER = (
    "\n📋 [codex-lcm] Context was compacted. {summary_count} summaries available. "
    "Use lcm_grep, lcm_recall, or lcm_expand to query prior conversation history. "
    "Do NOT guess from summaries alone — verify with recall tools.\n"
)


class Assembler:
    """Assembles the restore context: active summaries + fresh tail."""

    def __init__(self, db: LCMDatabase, config: LCMConfig) -> None:
        self.db = db
        self.config = config

    def build_restore_context(
        self,
        session_id: str,
        is_compacted: bool = False,
        token_limit: Optional[int] = None,
    ) -> str:
        """Build the additionalContext string for the SessionStart hook."""
        limit = token_limit if token_limit is not None else self.config.additional_context_limit
        max_chars = max(0, limit * 4)

        summaries = self.db.get_summaries(session_id)
        unsummarized = self.db.get_unsummarized_messages(session_id)

        if max_chars == 0 or (not summaries and not unsummarized):
            return ""

        footer = f"\n{RESTORE_FOOTER}"
        wrapper_size = len(RESTORE_HEADER) + len(footer)
        newest_content = ""
        if unsummarized:
            newest_content = unsummarized[-1].get("content", "")
        elif summaries:
            newest_content = summaries[-1].get("content", "")

        # When the limit cannot hold the wrapper, the hard bound and newest
        # available context take precedence over markup.
        if max_chars < wrapper_size:
            return newest_content[:max_chars]

        remaining = max_chars - wrapper_size
        tail_text = ""
        if unsummarized and remaining:
            tail = unsummarized[-self.config.fresh_tail:]
            tail_header = "\n## Recent Messages (fresh tail)\n"
            latest = tail[-1]
            latest_entry = f"\n{latest.get('role', '?')}: {latest.get('content', '')[:500]}"
            if len(latest_entry) > remaining:
                tail_text = latest.get("content", "")[:remaining]
                remaining = 0
            else:
                prefix = tail_header if len(tail_header) + len(latest_entry) <= remaining else ""
                remaining -= len(prefix)
                entries: list[str] = []
                for message in reversed(tail):
                    entry = f"\n{message.get('role', '?')}: {message.get('content', '')[:500]}"
                    if len(entry) <= remaining:
                        entries.append(entry)
                        remaining -= len(entry)
                        continue
                    break
                tail_text = prefix + "".join(reversed(entries))

        summary_text = ""
        if summaries and remaining:
            summary_header = "\n## Prior Context Summaries\n"
            if len(summary_header) <= remaining:
                summary_text = summary_header
                remaining -= len(summary_header)
            for summary in summaries:
                entry = (
                    f"\n### [depth {summary['depth']}] "
                    f"{summary.get('earliest_at', '')} → {summary.get('latest_at', '')}\n"
                    f"{summary['content']}\n"
                )
                if len(entry) > remaining:
                    break
                summary_text += entry
                remaining -= len(entry)

        reminder = ""
        if is_compacted and summaries:
            candidate = COMPACT_REMINDER.format(summary_count=len(summaries))
            if len(candidate) <= remaining:
                reminder = candidate

        return RESTORE_HEADER + summary_text + tail_text + reminder + footer
