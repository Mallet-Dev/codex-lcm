"""Compaction engine for codex-lcm — leaf summarization and DAG condensation."""

from __future__ import annotations

import sys
from typing import Optional

from .config import LCMConfig
from .db import LCMDatabase
from .summarizer import Summarizer, deterministic_truncation, concatenate_messages, concatenate_summaries


class CompactionResult:
    """Result of a compaction pass."""

    def __init__(self) -> None:
        self.leaf_summaries_created: int = 0
        self.condensed_summaries_created: int = 0
        self.tokens_before: int = 0
        self.tokens_after: int = 0
        self.rounds: int = 0
        self.stalled: bool = False
        self.spend_limit_hit: bool = False
        self.errors: list[str] = []

    @property
    def tokens_saved(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)

    @property
    def progress_ratio(self) -> float:
        if self.tokens_before == 0:
            return 0.0
        return self.tokens_saved / self.tokens_before

    def __repr__(self) -> str:
        return (
            f"CompactionResult(rounds={self.rounds}, "
            f"leaves={self.leaf_summaries_created}, "
            f"condensed={self.condensed_summaries_created}, "
            f"saved={self.tokens_saved} tokens ({self.progress_ratio:.1%}), "
            f"stalled={self.stalled}, spend_limit={self.spend_limit_hit})"
        )


class CompactionEngine:
    """Manages the DAG compaction lifecycle for a session."""

    def __init__(self, db: LCMDatabase, config: LCMConfig, summarizer: Optional[Summarizer] = None) -> None:
        self.db = db
        self.config = config
        self.summarizer = summarizer or Summarizer(config)

    def run_compaction(self, session_id: str) -> CompactionResult:
        """Run a full compaction sweep on the session."""
        result = CompactionResult()
        result.tokens_before = self.db.get_context_token_count(session_id)

        for rnd in range(self.config.max_rounds):
            result.rounds = rnd + 1
            made_progress = False

            if self.db.get_spend_count(session_id, self.config.summary_call_window_ms) >= self.config.max_summary_calls:
                result.spend_limit_hit = True
                break

            # Phase 1: Leaf compaction (oldest unsummarized messages → leaf summary)
            leaf_created = self._leaf_pass(session_id, result)
            if leaf_created > 0:
                made_progress = True
                result.leaf_summaries_created += leaf_created

            if self.db.get_spend_count(session_id, self.config.summary_call_window_ms) >= self.config.max_summary_calls:
                result.spend_limit_hit = True
                break

            # Phase 2: Condensation (fold N same-depth summaries into a deeper one)
            condensed_created = self._condensation_pass(session_id, result)
            if condensed_created > 0:
                made_progress = True
                result.condensed_summaries_created += condensed_created

            if not made_progress:
                break

            if self.db.get_spend_count(session_id, self.config.summary_call_window_ms) >= self.config.max_summary_calls:
                result.spend_limit_hit = True
                break

        result.tokens_after = self.db.get_context_token_count(session_id)

        if result.rounds > 0 and result.progress_ratio < self.config.progress_threshold:
            result.stalled = True
            print(
                f"Compaction stalled: only {result.progress_ratio:.1%} tokens saved in {result.rounds} rounds",
                file=sys.stderr,
            )

        return result

    def _leaf_pass(self, session_id: str, result: CompactionResult) -> int:
        """Summarize the oldest contiguous chunk of unsummarized messages.

        Leaves the fresh tail (most recent N raw messages) untouched.
        """
        all_messages = self.db.get_messages(session_id)
        if len(all_messages) <= self.config.fresh_tail:
            return 0

        tail_start = max(0, len(all_messages) - self.config.fresh_tail)
        unsummarized = self.db.get_unsummarized_messages(session_id)
        eligible = [m for m in unsummarized if m["seq"] < tail_start]
        if not eligible:
            return 0

        chunks = self._chunk_by_tokens(eligible, self.config.leaf_chunk_tokens)
        chunk = chunks[0]

        msg_text = concatenate_messages(chunk)
        prev_summary = self._get_latest_summary_text(session_id)

        if not self._reserve_summary_attempt(session_id, result):
            return 0
        content = self.summarizer.summarize_leaf(msg_text, prev_summary)
        if content is None and self._reserve_summary_attempt(session_id, result):
            content = self.summarizer.summarize_leaf_aggressive(msg_text)
        if content is None:
            content = deterministic_truncation(msg_text, target_tokens=512)
            result.errors.append("leaf: LLM failed, used truncation fallback")

        if content and len(content.strip()) > 50:
            earliest = chunk[0].get("created_at")
            latest = chunk[-1].get("created_at")
            self.db.create_leaf_summary(
                session_id=session_id,
                content=content,
                message_seqs=[m["seq"] for m in chunk],
                earliest_at=earliest,
                latest_at=latest,
            )
            return 1

        result.errors.append("leaf: all summarization methods produced empty output")
        return 0

    def _condensation_pass(self, session_id: str, result: CompactionResult) -> int:
        """Fold a chronological contiguous run at the shallowest eligible depth."""
        active = self.db.get_summaries(session_id)

        for depth in sorted({summary["depth"] for summary in active}):
            min_fanout = self.config.leaf_min_fanout if depth == 0 else self.config.condensed_min_fanout
            run: list[dict] = []
            fanout: list[dict] = []
            for summary in active:
                if summary["depth"] == depth:
                    run.append(summary)
                    if len(run) == min_fanout:
                        fanout = run
                        break
                else:
                    run = []
            if not fanout:
                continue
            sums_text = concatenate_summaries(fanout)

            if not self._reserve_summary_attempt(session_id, result):
                return 0
            content = self.summarizer.summarize_condensed(sums_text, depth=depth + 1)
            if content is None:
                content = deterministic_truncation(sums_text, target_tokens=800)
                result.errors.append("condensation: LLM failed, used truncation fallback")

            if content and len(content.strip()) > 50:
                earliest = fanout[0].get("earliest_at")
                latest = fanout[-1].get("latest_at")
                self.db.create_condensed_summary(
                    session_id=session_id,
                    content=content,
                    parent_ids=[s["summary_id"] for s in fanout],
                    depth=depth + 1,
                    earliest_at=earliest,
                    latest_at=latest,
                )
                return 1
            return 0

        return 0

    def _reserve_summary_attempt(self, session_id: str, result: CompactionResult) -> bool:
        reserved = self.db.try_reserve_summary_call(
            session_id,
            self.config.summary_call_window_ms,
            self.config.max_summary_calls,
        )
        if not reserved:
            result.spend_limit_hit = True
        return reserved

    def _chunk_by_tokens(self, messages: list[dict], max_tokens: int) -> list[list[dict]]:
        """Split messages into chunks that fit within max_tokens."""
        chunks: list[list[dict]] = []
        current_chunk: list[dict] = []
        current_tokens = 0

        for msg in messages:
            msg_tokens = msg.get("token_count", len(msg.get("content", "")) // 4)
            if current_tokens + msg_tokens > max_tokens and current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_tokens = 0
            current_chunk.append(msg)
            current_tokens += msg_tokens

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _get_latest_summary_text(self, session_id: str) -> Optional[str]:
        """Most recent summary content, for continuity (avoid repeating known info)."""
        rows = self.db.conn.execute(
            "SELECT content FROM summaries WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchall()
        if rows:
            return rows[0]["content"]
        return None
