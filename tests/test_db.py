"""Tests for the database layer."""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from codex_lcm.config import LCMConfig
from codex_lcm.db import LCMDatabase


SID = "sess-1"


def _seed(db, n=10, start=0):
    db.ensure_session(SID, "/work/demo")
    for i in range(start, start + n):
        db.ingest_message(SID, i, "user" if i % 2 == 0 else "assistant", f"message number {i} about supabase auth", stable_key=f"k{i}")


def test_ingest_and_count(db):
    _seed(db, 5)
    assert db.get_message_count(SID) == 5
    assert db.get_unsummarized_count(SID) == 5


def test_stable_key_dedup(db):
    _seed(db, 2)
    inserted = db.ingest_message(SID, 2, "tool", '{"tool":"Bash"}', stable_key="k0")
    assert inserted is False  # duplicate rejected
    assert db.get_message_count(SID) == 2


def test_project_key_stable(db):
    pk1 = LCMConfig.project_key("/work/a", "git@github.com:me/x.git")
    pk2 = LCMConfig.project_key("/work/a", "git@github.com:me/x.git")
    pk3 = LCMConfig.project_key("/work/a", "git@github.com:me/y.git")
    assert pk1 == pk2
    assert pk1 != pk3


def test_database_uses_wal_mode(config):
    db = LCMDatabase(config)
    try:
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        db.close()


def test_database_retries_locked_wal_initialization(config, monkeypatch):
    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class LockedOnceConnection:
        def __init__(self):
            self.wal_attempts = 0

        def execute(self, statement):
            if statement == "PRAGMA journal_mode":
                return Result("delete")
            if statement == "PRAGMA journal_mode=WAL":
                self.wal_attempts += 1
                if self.wal_attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
                return Result("wal")
            raise AssertionError(statement)

    connection = LockedOnceConnection()
    db = LCMDatabase(config)
    db._conn = connection  # type: ignore[assignment]
    monkeypatch.setattr("codex_lcm.db.time.sleep", lambda _seconds: None)

    db._enable_wal()

    assert connection.wal_attempts == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are unavailable")
def test_database_and_wal_files_are_private(config):
    db = LCMDatabase(config)
    try:
        db.ensure_session(SID, "/work/demo")
        paths = [config.db_path]
        paths.extend(
            path
            for path in (
                config.db_path.with_name(config.db_path.name + "-wal"),
                config.db_path.with_name(config.db_path.name + "-shm"),
            )
            if path.exists()
        )
        assert paths
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    finally:
        db.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are unavailable")
def test_database_permission_hardening_tolerates_disappearing_sidecar(config, monkeypatch):
    config.db_path.touch()
    wal_path = config.db_path.with_name(config.db_path.name + "-wal")
    wal_path.touch()
    real_chmod = os.chmod

    def disappearing_sidecar(path, mode):
        if path == wal_path:
            wal_path.unlink(missing_ok=True)
            raise FileNotFoundError(path)
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", disappearing_sidecar)

    LCMDatabase(config)._secure_database_files()
    assert config.db_path.stat().st_mode & 0o777 == 0o600


def test_summary_call_reservation_prunes_expired_rows(db):
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.conn.execute(
        "INSERT INTO summary_spend (session_id, created_at) VALUES (?, ?)",
        (SID, expired),
    )
    db.conn.commit()

    assert db.try_reserve_summary_call(SID, window_ms=600_000, max_calls=2) is True
    rows = db.conn.execute(
        "SELECT created_at FROM summary_spend WHERE session_id = ?",
        (SID,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["created_at"] != expired

    db.conn.execute(
        "INSERT INTO summary_spend (session_id, created_at) VALUES (?, ?)",
        (SID, expired),
    )
    db.conn.commit()
    assert db.try_reserve_summary_call(SID, window_ms=600_000, max_calls=1) is False
    rows = db.conn.execute(
        "SELECT created_at FROM summary_spend WHERE session_id = ?",
        (SID,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["created_at"] != expired


def test_leaf_summary_links_and_unsummarized(db):
    _seed(db, 6)
    sid = db.create_leaf_summary(SID, "a summary of the oldest six messages", [0, 1, 2, 3, 4, 5])
    assert db.get_unsummarized_count(SID) == 0
    sources = db.get_summary_source_messages(sid)
    assert [m["seq"] for m in sources] == [0, 1, 2, 3, 4, 5]
    assert db.get_summary(sid)["kind"] == "leaf"


def test_condensation_deactivates_parents(db):
    _seed(db, 3)
    p1 = db.create_leaf_summary(SID, "summary one content that is long enough", [0])
    p2 = db.create_leaf_summary(SID, "summary two content that is long enough", [1])
    p3 = db.create_leaf_summary(SID, "summary three content that is long enough", [2])

    cid = db.create_condensed_summary(SID, "condensed rollup of all three", [p1, p2, p3], depth=1)

    assert db.get_summary(cid)["depth"] == 1
    assert db.get_summary(p1)["active"] == 0
    assert db.get_summary(p2)["active"] == 0
    assert db.get_summary(p3)["active"] == 0
    assert db.get_summary(cid)["active"] == 1

    # Active-only listing hides folded parents, keeps the condensed node.
    active = db.get_summaries(SID)
    ids = {s["summary_id"] for s in active}
    assert cid in ids
    assert p1 not in ids

    # Drill-down still reaches the folded parents (lossless).
    parents = db.get_summary_parents(cid)
    assert {p["summary_id"] for p in parents} == {p1, p2, p3}


def test_context_token_count_reflects_assembly(db):
    _seed(db, 6)
    before = db.get_context_token_count(SID)
    db.create_leaf_summary(SID, "compact summary", [0, 1, 2, 3])
    after = db.get_context_token_count(SID)
    # Summarized messages no longer count; the summary + 2 raw tail do.
    assert after < before


def test_fts_search(db):
    _seed(db, 4)
    hits = db.search_messages("supabase", session_id=SID)
    assert len(hits) >= 1
    assert all("supabase" in h["content"] for h in hits)


def test_active_summaries_and_parents_follow_source_chronology(db):
    _seed(db, 4)
    later = db.create_leaf_summary(
        SID,
        "later source summary with enough content for chronology",
        [2],
        earliest_at="2026-08-19T12:00:00+00:00",
        latest_at="2026-08-19T12:05:00+00:00",
    )
    earlier = db.create_leaf_summary(
        SID,
        "earlier source summary created second but ordered first",
        [0],
        earliest_at="2026-08-19T10:00:00+00:00",
        latest_at="2026-08-19T10:05:00+00:00",
    )

    assert [s["summary_id"] for s in db.get_summaries(SID)] == [earlier, later]

    condensed = db.create_condensed_summary(
        SID,
        "chronological parent rollup with sufficient detail",
        [later, earlier],
        depth=1,
        earliest_at="2026-08-19T10:00:00+00:00",
        latest_at="2026-08-19T12:05:00+00:00",
    )
    assert [p["summary_id"] for p in db.get_summary_parents(condensed)] == [earlier, later]
