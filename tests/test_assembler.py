"""Tests for bounded chronological context restoration."""

from codex_lcm.assembler import Assembler
from codex_lcm.config import LCMConfig


SID = "restore-session"


def test_restore_respects_token_limit_and_keeps_newest_tail(db, monkeypatch):
    monkeypatch.setenv("CODEX_LCM_FRESH_TAIL", "10")
    config = LCMConfig()
    db.ensure_session(SID, "/work/restore")
    for seq in range(10):
        marker = "NEWEST_MARKER" if seq == 9 else f"older-{seq}"
        db.ingest_message(
            SID,
            seq,
            "assistant",
            marker + " " + ("x" * 1000),
            stable_key=f"restore:{seq}",
        )

    restored = Assembler(db, config).build_restore_context(SID, token_limit=200)

    assert len(restored) <= 200 * 4
    assert "NEWEST_MARKER" in restored
    assert restored.endswith("</lc-restore>")


def test_restore_tiny_limits_remain_bounded_and_prioritize_newest_tail(db, monkeypatch):
    monkeypatch.setenv("CODEX_LCM_FRESH_TAIL", "3")
    config = LCMConfig()
    db.ensure_session(SID, "/work/restore")
    db.ingest_message(SID, 0, "assistant", "OLDER_CONTENT", stable_key="tiny:0")
    db.ingest_message(
        SID,
        1,
        "assistant",
        "NEWEST_MARKER with the latest useful detail",
        stable_key="tiny:1",
    )

    one_token = Assembler(db, config).build_restore_context(SID, token_limit=1)
    ten_tokens = Assembler(db, config).build_restore_context(SID, token_limit=10)
    twenty_tokens = Assembler(db, config).build_restore_context(SID, token_limit=20)

    assert len(one_token) <= 4
    assert len(ten_tokens) <= 40
    assert "NEWEST_MARKER" in ten_tokens
    assert len(twenty_tokens) <= 80
    assert "NEWEST_MARKER" in twenty_tokens
