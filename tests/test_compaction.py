"""Tests for the compaction engine (mock LLM + deterministic fallback)."""

from codex_lcm.compaction import CompactionEngine, CompactionResult
from codex_lcm.summarizer import Summarizer, deterministic_truncation, concatenate_messages

from conftest import MockSummarizer

SID = "sess-c"


def _seed(db, n=12, content="fixing the supabase auth flow and testing the reset endpoint"):
    db.ensure_session(SID, "/work/demo")
    for i in range(n):
        role = ["user", "tool", "assistant"][i % 3]
        db.ingest_message(SID, i, role, f"[{i}] {content} — detail number {i}", stable_key=f"m{i}")


def test_leaf_compaction_protects_fresh_tail(db, config):
    _seed(db, 9)
    engine = CompactionEngine(db, config, MockSummarizer())
    result = engine.run_compaction(SID)

    assert result.leaf_summaries_created >= 1
    # The last `fresh_tail` (3) messages must remain raw/unsummarized.
    unsummarized = db.get_unsummarized_messages(SID)
    assert max(m["seq"] for m in unsummarized) >= 6
    # Everything older is folded into a summary.
    assert db.get_summaries(SID)


def test_full_dag_leaf_and_condensation(db, config, monkeypatch):
    monkeypatch.setenv("CODEX_LCM_FRESH_TAIL", "2")
    monkeypatch.setenv("CODEX_LCM_LEAF_CHUNK_TOKENS", "150")
    monkeypatch.setenv("CODEX_LCM_MAX_ROUNDS", "8")
    monkeypatch.setenv("CODEX_LCM_LEAF_MIN_FANOUT", "3")
    monkeypatch.setenv("CODEX_LCM_CONDENSED_MIN_FANOUT", "3")
    from codex_lcm.config import LCMConfig
    cfg = LCMConfig()

    _seed(db, 24)
    mock = MockSummarizer()
    engine = CompactionEngine(db, cfg, mock)
    result = engine.run_compaction(SID)

    assert result.leaf_summaries_created >= 3
    assert result.condensed_summaries_created >= 1, "condensation should fold leaf summaries"
    assert result.tokens_after < result.tokens_before, "compaction must actually save tokens"
    assert result.stalled is False

    total = db.get_summaries(SID, active_only=False)
    active = db.get_summaries(SID, active_only=True)
    assert len(active) < len(total), "folded parents must be deactivated"

    # Condensed summaries expose their parent leaves for drill-down.
    condensed = [s for s in active if s["kind"] == "condensed"]
    assert condensed
    parents = db.get_summary_parents(condensed[0]["summary_id"])
    assert len(parents) >= 1


def test_deterministic_fallback_without_llm(db, config, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_LCM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_LCM_BASE_URL", raising=False)

    _seed(db, 9)
    engine = CompactionEngine(db, config, Summarizer(config))
    result = engine.run_compaction(SID)

    assert result.leaf_summaries_created >= 1
    assert any("truncation fallback" in e for e in result.errors), "LLM-less path must fall back deterministically"


def test_spend_guard_stops_compaction(db, config, monkeypatch):
    monkeypatch.setenv("CODEX_LCM_MAX_SUMMARY_CALLS", "1")
    from codex_lcm.config import LCMConfig
    cfg = LCMConfig()

    _seed(db, 20)
    engine = CompactionEngine(db, cfg, MockSummarizer())
    result = engine.run_compaction(SID)

    assert result.spend_limit_hit is True
    # Only one LLM (mock) summary call was allowed.
    assert db.get_spend_count(SID, 600_000) <= 1


def test_spend_guard_applies_between_leaf_and_condensation(db, monkeypatch):
    monkeypatch.setenv("CODEX_LCM_MAX_SUMMARY_CALLS", "1")
    from codex_lcm.config import LCMConfig

    cfg = LCMConfig()
    _seed(db, 12)
    db.create_leaf_summary(SID, "existing leaf one with enough summary detail", [0])
    db.create_leaf_summary(SID, "existing leaf two with enough summary detail", [1])
    db.create_leaf_summary(SID, "existing leaf three with enough summary detail", [2])

    result = CompactionEngine(db, cfg, MockSummarizer()).run_compaction(SID)

    assert result.leaf_summaries_created == 1
    assert result.condensed_summaries_created == 0
    assert result.spend_limit_hit is True
    assert db.get_spend_count(SID, 600_000) == 1


def test_spend_guard_counts_each_summarizer_attempt(db, monkeypatch):
    monkeypatch.setenv("CODEX_LCM_MAX_SUMMARY_CALLS", "1")
    from codex_lcm.config import LCMConfig

    class FailThenSucceedSummarizer(Summarizer):
        def __init__(self):
            self.calls = []

        def summarize_leaf(self, messages_text, previous_context=None):
            self.calls.append("normal")
            return None

        def summarize_leaf_aggressive(self, messages_text):
            self.calls.append("aggressive")
            return "aggressive summary with enough retained detail to persist"

        def summarize_condensed(self, summaries_text, depth):
            self.calls.append(f"condensed:{depth}")
            return "condensed summary with enough retained detail to persist"

    cfg = LCMConfig()
    _seed(db, 9)
    summarizer = FailThenSucceedSummarizer()

    result = CompactionEngine(db, cfg, summarizer).run_compaction(SID)

    assert summarizer.calls == ["normal"]
    assert db.get_spend_count(SID, cfg.summary_call_window_ms) == 1
    assert result.spend_limit_hit is True
    assert any("truncation fallback" in error for error in result.errors)


def test_anti_loop_stops_when_no_progress(db, config):
    _seed(db, 9)
    engine = CompactionEngine(db, config, MockSummarizer())
    engine.run_compaction(SID)
    second = engine.run_compaction(SID)
    # Second run must not keep adding summaries — nothing new to compact.
    assert second.leaf_summaries_created == 0
    assert second.condensed_summaries_created == 0


def test_deterministic_truncation_keeps_head_and_tail():
    text = "word " * 2000
    out = deterministic_truncation(text, target_tokens=512)
    assert len(out) < len(text)
    assert "truncated" in out
    assert out.startswith("word")


def test_concatenate_messages_includes_roles():
    msgs = [{"role": "user", "content": "hi", "created_at": "t"}, {"role": "tool", "content": "out"}]
    out = concatenate_messages(msgs)
    assert "user" in out and "tool" in out and "hi" in out


def test_condensation_never_crosses_interleaved_depth(db, config):
    _seed(db, 4)
    first = db.create_leaf_summary(
        SID, "first chronological leaf summary with enough detail", [0],
        earliest_at="2026-08-19T10:00:00+00:00", latest_at="2026-08-19T10:01:00+00:00",
    )
    second = db.create_leaf_summary(
        SID, "second chronological leaf summary with enough detail", [1],
        earliest_at="2026-08-19T10:02:00+00:00", latest_at="2026-08-19T10:03:00+00:00",
    )
    middle_parent = db.create_leaf_summary(
        SID, "middle parent leaf summary with enough detail", [2],
        earliest_at="2026-08-19T10:04:00+00:00", latest_at="2026-08-19T10:05:00+00:00",
    )
    middle = db.create_condensed_summary(
        SID, "middle depth-one summary with enough chronological detail", [middle_parent], depth=1,
        earliest_at="2026-08-19T10:04:00+00:00", latest_at="2026-08-19T10:05:00+00:00",
    )
    third = db.create_leaf_summary(
        SID, "third chronological leaf summary with enough detail", [3],
        earliest_at="2026-08-19T10:06:00+00:00", latest_at="2026-08-19T10:07:00+00:00",
    )

    engine = CompactionEngine(db, config, MockSummarizer())
    created = engine._condensation_pass(SID, CompactionResult())

    assert created == 0
    active_ids = {s["summary_id"] for s in db.get_summaries(SID)}
    assert {first, second, middle, third} <= active_ids


def test_hundred_turn_workload_builds_lossless_multitier_dag(db, monkeypatch):
    monkeypatch.setenv("CODEX_LCM_FRESH_TAIL", "5")
    monkeypatch.setenv("CODEX_LCM_LEAF_CHUNK_TOKENS", "50")
    monkeypatch.setenv("CODEX_LCM_MAX_ROUNDS", "200")
    monkeypatch.setenv("CODEX_LCM_MAX_SUMMARY_CALLS", "500")
    monkeypatch.setenv("CODEX_LCM_LEAF_MIN_FANOUT", "3")
    monkeypatch.setenv("CODEX_LCM_CONDENSED_MIN_FANOUT", "3")
    monkeypatch.setenv("CODEX_LCM_PROGRESS_THRESHOLD", "0")
    from codex_lcm.config import LCMConfig

    cfg = LCMConfig()
    db.ensure_session(SID, "/work/scale")
    for i in range(110):
        db.ingest_message(
            SID,
            i,
            ["user", "tool", "assistant"][i % 3],
            f"turn {i:03d} " + (f"chronological detail {i:03d} " * 20),
            stable_key=f"scale:{i}",
        )

    result = CompactionEngine(db, cfg, MockSummarizer()).run_compaction(SID)
    all_summaries = db.get_summaries(SID, active_only=False)
    active = db.get_summaries(SID)

    assert db.get_message_count(SID) == 110
    assert [m["seq"] for m in db.get_unsummarized_messages(SID)] == list(range(105, 110))
    assert max(summary["depth"] for summary in all_summaries) >= 3
    assert result.tokens_after < result.tokens_before
    assert [s["earliest_at"] for s in active] == sorted(s["earliest_at"] for s in active)

    links = db.conn.execute(
        """SELECT message_seq, COUNT(*) AS linked
           FROM summary_messages WHERE session_id = ?
           GROUP BY message_seq ORDER BY message_seq""",
        (SID,),
    ).fetchall()
    assert [(row["message_seq"], row["linked"]) for row in links] == [
        (seq, 1) for seq in range(105)
    ]

    for summary in (s for s in all_summaries if s["kind"] == "condensed"):
        parents = db.get_summary_parents(summary["summary_id"])
        assert parents
        for left, right in zip(parents, parents[1:]):
            assert left["latest_at"] <= right["earliest_at"]
