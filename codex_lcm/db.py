"""SQLite database layer for codex-lcm.

Assembly model (mirrors lossless-claw's, simplified):

    active context = [active summaries (oldest → newest)] + [unsummarized raw messages]

When old messages are folded into a leaf summary, the summary becomes the
active representation and the raw messages remain in the DB (lossless) but are
no longer "active". When leaf summaries are condensed, the new summary becomes
active and the parent summaries are marked inactive — still reachable via
lcm_expand for drill-down. Nothing is ever deleted.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .config import LCMConfig

_SCHEMA = """
-- Sessions (one per Codex session_id)
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    project_key      TEXT NOT NULL,
    cwd              TEXT,
    git_remote       TEXT,
    git_branch       TEXT,
    model            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Messages (raw, never deleted)
CREATE TABLE IF NOT EXISTS messages (
    seq              INTEGER NOT NULL,
    session_id       TEXT NOT NULL,
    role             TEXT NOT NULL,
    content          TEXT NOT NULL,
    token_count      INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    stable_key       TEXT,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_stable
    ON messages(session_id, stable_key) WHERE stable_key IS NOT NULL;

-- Message parts (structured content blocks)
CREATE TABLE IF NOT EXISTS message_parts (
    session_id       TEXT NOT NULL,
    message_seq      INTEGER NOT NULL,
    part_idx         INTEGER NOT NULL,
    kind             TEXT NOT NULL,
    content          TEXT NOT NULL,
    PRIMARY KEY (session_id, message_seq, part_idx),
    FOREIGN KEY (session_id, message_seq) REFERENCES messages(session_id, seq)
);

-- Summaries (the DAG). `active` marks whether a node is the current
-- representation in the assembled context; folded children become inactive.
CREATE TABLE IF NOT EXISTS summaries (
    summary_id       TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    depth            INTEGER NOT NULL DEFAULT 0,
    kind             TEXT NOT NULL,
    content          TEXT NOT NULL,
    token_count      INTEGER NOT NULL,
    earliest_at      TEXT,
    latest_at        TEXT,
    descendant_count INTEGER DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id, active, depth);

-- summary_messages: leaf -> source messages
CREATE TABLE IF NOT EXISTS summary_messages (
    summary_id       TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    message_seq      INTEGER NOT NULL,
    PRIMARY KEY (summary_id, session_id, message_seq),
    FOREIGN KEY (summary_id) REFERENCES summaries(summary_id)
);

-- summary_parents: condensed -> source summaries
CREATE TABLE IF NOT EXISTS summary_parents (
    summary_id       TEXT NOT NULL,
    parent_id        TEXT NOT NULL,
    PRIMARY KEY (summary_id, parent_id),
    FOREIGN KEY (summary_id) REFERENCES summaries(summary_id)
);

-- Summary spend tracking
CREATE TABLE IF NOT EXISTS summary_spend (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

-- FTS5 for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
    summary_id UNINDEXED,
    content,
    session_id UNINDEXED,
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    session_id UNINDEXED,
    seq UNINDEXED,
    content,
    role UNINDEXED,
    tokenize='porter unicode61'
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def _gen_summary_id(content: str) -> str:
    h = hashlib.sha256(f"{content}{time.time()}".encode()).hexdigest()
    return f"sum_{h[:16]}"


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class LCMDatabase:
    """SQLite-backed persistence for codex-lcm."""

    _BUSY_TIMEOUT_MS = 5_000

    def __init__(self, config: LCMConfig) -> None:
        self.config = config
        self.db_path = config.db_path
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._secure_database_files()
            self._conn.execute(f"PRAGMA busy_timeout={self._BUSY_TIMEOUT_MS}")
            self._enable_wal()
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
            self._secure_database_files()
        return self._conn

    def _enable_wal(self) -> None:
        """Enable WAL, retrying lock races within SQLite's configured busy window."""
        conn = self._conn
        if conn is None:  # pragma: no cover - only called during connection setup
            raise RuntimeError("database connection is not initialized")
        deadline = time.monotonic() + (self._BUSY_TIMEOUT_MS / 1000)
        while True:
            try:
                current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(current_mode).lower() != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0)

    def _secure_database_files(self) -> None:
        """Restrict the database and SQLite sidecars to the current user."""
        if os.name != "posix":
            return
        for path in (
            self.db_path,
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
        ):
            if path.exists():
                try:
                    os.chmod(path, 0o600)
                except FileNotFoundError:
                    # SQLite may remove a transient sidecar after exists().
                    pass

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- Sessions ---

    def ensure_session(
        self,
        session_id: str,
        cwd: str,
        git_remote: Optional[str] = None,
        git_branch: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        pk = LCMConfig.project_key(cwd, git_remote)
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO sessions
               (session_id, project_key, cwd, git_remote, git_branch, model, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   updated_at = excluded.updated_at,
                   model = COALESCE(excluded.model, sessions.model)""",
            (session_id, pk, cwd, git_remote, git_branch, model, now, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row)

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(
        self,
        project_key: Optional[str] = None,
        updated_since: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        project_filter = project_key or None
        updated_filter = updated_since or None
        rows = self.conn.execute(
            """SELECT * FROM sessions
               WHERE (? IS NULL OR project_key = ?)
                 AND (? IS NULL OR updated_at >= ?)
               ORDER BY updated_at DESC
               LIMIT ?""",
            (
                project_filter,
                project_filter,
                updated_filter,
                updated_filter,
                limit if limit is not None else -1,
            ),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Messages ---

    def ingest_message(
        self,
        session_id: str,
        seq: int,
        role: str,
        content: str,
        stable_key: Optional[str] = None,
        parts: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        """Ingest a single message. Returns True if inserted, False if duplicate."""
        now = _now_iso()
        tokens = _estimate_tokens(content)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            inserted = self._insert_message_rows(
                session_id, seq, role, content, tokens, now, stable_key, parts
            )
            if not inserted:
                self.conn.rollback()
                return False
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        stable_key: Optional[str] = None,
        parts: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[int]:
        """Atomically append a message and return its allocated sequence.

        Sequence allocation, message parts, the FTS row, and the session update
        share one write transaction. A duplicate stable key returns ``None``.
        """
        now = _now_iso()
        tokens = _estimate_tokens(content)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if stable_key:
                existing = self.conn.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? AND stable_key = ?",
                    (session_id, stable_key),
                ).fetchone()
                if existing:
                    self.conn.rollback()
                    return None
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            inserted = self._insert_message_rows(
                session_id, seq, role, content, tokens, now, stable_key, parts
            )
            if not inserted:
                self.conn.rollback()
                return None
            self.conn.commit()
            return seq
        except Exception:
            self.conn.rollback()
            raise

    def _insert_message_rows(
        self,
        session_id: str,
        seq: int,
        role: str,
        content: str,
        tokens: int,
        now: str,
        stable_key: Optional[str],
        parts: Optional[list[dict[str, Any]]],
    ) -> bool:
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO messages
               (seq, session_id, role, content, token_count, created_at, stable_key)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (seq, session_id, role, content, tokens, now, stable_key),
        )
        if cursor.rowcount != 1:
            return False

        for idx, part in enumerate(parts or []):
            self.conn.execute(
                """INSERT INTO message_parts
                   (session_id, message_seq, part_idx, kind, content)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    session_id,
                    seq,
                    idx,
                    part.get("kind", "text"),
                    part.get("content", ""),
                ),
            )
        self.conn.execute(
            "INSERT INTO messages_fts (session_id, seq, content, role) VALUES (?, ?, ?, ?)",
            (session_id, seq, content, role),
        )
        self.conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        return True

    def get_messages(
        self, session_id: str, start_seq: int = 0, limit: Optional[int] = None
    ) -> list[dict[str, Any]]:
        if limit:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND seq >= ? ORDER BY seq LIMIT ?",
                (session_id, start_seq, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND seq >= ? ORDER BY seq",
                (session_id, start_seq),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_message(self, session_id: str, seq: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND seq = ?",
            (session_id, seq),
        ).fetchone()
        return dict(row) if row else None

    def get_message_count(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row["cnt"] if row else 0

    def get_message_parts(self, session_id: str, message_seq: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM message_parts
               WHERE session_id = ? AND message_seq = ?
               ORDER BY part_idx""",
            (session_id, message_seq),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_unsummarized_count(self, session_id: str) -> int:
        """Count messages not yet folded into a leaf summary."""
        row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM messages m
               WHERE m.session_id = ?
                 AND NOT EXISTS (
                   SELECT 1 FROM summary_messages sm
                   WHERE sm.session_id = m.session_id AND sm.message_seq = m.seq
                 )""",
            (session_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_unsummarized_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Messages not yet folded into a summary, ordered by seq."""
        rows = self.conn.execute(
            """SELECT m.* FROM messages m
               WHERE m.session_id = ?
                 AND NOT EXISTS (
                   SELECT 1 FROM summary_messages sm
                   WHERE sm.session_id = m.session_id AND sm.message_seq = m.seq
                 )
               ORDER BY m.seq""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_context_token_count(self, session_id: str) -> int:
        """Assembled context size: active summaries + unsummarized raw messages."""
        sum_tokens = self.conn.execute(
            "SELECT COALESCE(SUM(token_count), 0) FROM summaries WHERE session_id = ? AND active = 1",
            (session_id,),
        ).fetchone()[0]

        unsum_tokens = self.conn.execute(
            """SELECT COALESCE(SUM(m.token_count), 0) FROM messages m
               WHERE m.session_id = ?
                 AND NOT EXISTS (
                   SELECT 1 FROM summary_messages sm
                   WHERE sm.session_id = m.session_id AND sm.message_seq = m.seq
                 )""",
            (session_id,),
        ).fetchone()[0]

        return sum_tokens + unsum_tokens

    # --- Summaries ---

    def create_leaf_summary(
        self,
        session_id: str,
        content: str,
        message_seqs: list[int],
        earliest_at: Optional[str] = None,
        latest_at: Optional[str] = None,
    ) -> str:
        sid = _gen_summary_id(content)
        tokens = _estimate_tokens(content)
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO summaries
               (summary_id, session_id, depth, kind, content, token_count,
                earliest_at, latest_at, descendant_count, active, created_at)
               VALUES (?, ?, 0, 'leaf', ?, ?, ?, ?, 0, 1, ?)""",
            (sid, session_id, content, tokens, earliest_at, latest_at, now),
        )
        for msg_seq in message_seqs:
            self.conn.execute(
                "INSERT OR IGNORE INTO summary_messages (summary_id, session_id, message_seq) VALUES (?, ?, ?)",
                (sid, session_id, msg_seq),
            )
        self.conn.execute(
            "INSERT INTO summaries_fts (summary_id, content, session_id) VALUES (?, ?, ?)",
            (sid, content, session_id),
        )
        self.conn.commit()
        return sid

    def create_condensed_summary(
        self,
        session_id: str,
        content: str,
        parent_ids: list[str],
        depth: int,
        earliest_at: Optional[str] = None,
        latest_at: Optional[str] = None,
    ) -> str:
        sid = _gen_summary_id(content)
        tokens = _estimate_tokens(content)
        now = _now_iso()
        descendant_count = self._count_descendants(parent_ids)
        self.conn.execute(
            """INSERT INTO summaries
               (summary_id, session_id, depth, kind, content, token_count,
                earliest_at, latest_at, descendant_count, active, created_at)
               VALUES (?, ?, ?, 'condensed', ?, ?, ?, ?, ?, 1, ?)""",
            (sid, session_id, depth, content, tokens, earliest_at, latest_at, descendant_count, now),
        )
        for pid in parent_ids:
            self.conn.execute(
                "INSERT OR IGNORE INTO summary_parents (summary_id, parent_id) VALUES (?, ?)",
                (sid, pid),
            )
        # Deactivate the parents — they are now represented by this summary.
        self.conn.executemany(
            "UPDATE summaries SET active = 0 WHERE summary_id = ?",
            [(pid,) for pid in parent_ids],
        )
        self.conn.execute(
            "INSERT INTO summaries_fts (summary_id, content, session_id) VALUES (?, ?, ?)",
            (sid, content, session_id),
        )
        self.conn.commit()
        return sid

    def get_summary(self, summary_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM summaries WHERE summary_id = ?", (summary_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_summaries(
        self, session_id: str, depth: Optional[int] = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        if active_only and depth is not None:
            rows = self.conn.execute(
                """SELECT * FROM summaries
                   WHERE session_id = ? AND depth = ? AND active = 1
                   ORDER BY COALESCE(earliest_at, created_at), created_at""",
                (session_id, depth),
            ).fetchall()
        elif active_only:
            rows = self.conn.execute(
                """SELECT * FROM summaries
                   WHERE session_id = ? AND active = 1
                   ORDER BY COALESCE(earliest_at, created_at), created_at""",
                (session_id,),
            ).fetchall()
        elif depth is not None:
            rows = self.conn.execute(
                """SELECT * FROM summaries
                   WHERE session_id = ? AND depth = ?
                   ORDER BY COALESCE(earliest_at, created_at), created_at""",
                (session_id, depth),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM summaries
                   WHERE session_id = ?
                   ORDER BY COALESCE(earliest_at, created_at), created_at""",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_summary_source_messages(self, summary_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT m.* FROM messages m
               JOIN summary_messages sm ON m.session_id = sm.session_id AND m.seq = sm.message_seq
               WHERE sm.summary_id = ?
               ORDER BY m.seq""",
            (summary_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_summary_parents(self, summary_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT s.* FROM summaries s
               JOIN summary_parents sp ON s.summary_id = sp.parent_id
               WHERE sp.summary_id = ?
               ORDER BY COALESCE(s.earliest_at, s.created_at), s.created_at""",
            (summary_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _count_descendants(self, parent_ids: list[str]) -> int:
        total = 0
        for parent_id in dict.fromkeys(parent_ids):
            row = self.conn.execute(
                "SELECT descendant_count FROM summaries WHERE summary_id = ?",
                (parent_id,),
            ).fetchone()
            if row is not None:
                total += int(row["descendant_count"]) + 1
        return total

    # --- Spend tracking ---

    def try_reserve_summary_call(self, session_id: str, window_ms: int, max_calls: int) -> bool:
        """Atomically reserve one summarizer attempt within the rolling spend window."""
        if max_calls <= 0:
            return False
        threshold = datetime.now(timezone.utc).timestamp() - (window_ms / 1000)
        threshold_iso = datetime.fromtimestamp(threshold, timezone.utc).isoformat()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "DELETE FROM summary_spend WHERE session_id = ? AND created_at < ?",
                (session_id, threshold_iso),
            )
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM summary_spend WHERE session_id = ? AND created_at >= ?",
                (session_id, threshold_iso),
            ).fetchone()
            if row and row["cnt"] >= max_calls:
                self.conn.commit()
                return False
            self.conn.execute(
                "INSERT INTO summary_spend (session_id, created_at) VALUES (?, ?)",
                (session_id, _now_iso()),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def get_spend_count(self, session_id: str, window_ms: int) -> int:
        threshold = datetime.now(timezone.utc).timestamp() - (window_ms / 1000)
        threshold_iso = datetime.fromtimestamp(threshold, timezone.utc).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM summary_spend WHERE session_id = ? AND created_at >= ?",
            (session_id, threshold_iso),
        ).fetchone()
        return row["cnt"] if row else 0

    # --- Search ---

    def search_summaries(self, query: str, session_id: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        if session_id:
            rows = self.conn.execute(
                """SELECT s.summary_id, s.session_id, s.depth, s.kind, s.content,
                          s.earliest_at, s.latest_at, s.created_at,
                          bm25(summaries_fts) as rank
                   FROM summaries_fts
                   JOIN summaries s ON summaries_fts.summary_id = s.summary_id
                   WHERE summaries_fts MATCH ? AND summaries_fts.session_id = ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, session_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT s.summary_id, s.session_id, s.depth, s.kind, s.content,
                          s.earliest_at, s.latest_at, s.created_at,
                          bm25(summaries_fts) as rank
                   FROM summaries_fts
                   JOIN summaries s ON summaries_fts.summary_id = s.summary_id
                   WHERE summaries_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_messages(self, query: str, session_id: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        if session_id:
            rows = self.conn.execute(
                """SELECT messages_fts.seq, messages_fts.session_id, messages_fts.role,
                          messages_fts.content, bm25(messages_fts) as rank
                   FROM messages_fts
                   WHERE messages_fts MATCH ? AND messages_fts.session_id = ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, session_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT messages_fts.seq, messages_fts.session_id, messages_fts.role,
                          messages_fts.content, bm25(messages_fts) as rank
                   FROM messages_fts
                   WHERE messages_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_summaries_substring(
        self, query: str, session_id: Optional[str] = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        pattern = _like_pattern(query)
        rows = self.conn.execute(
            """SELECT summary_id, session_id, depth, kind, content,
                       earliest_at, latest_at, created_at, 0 AS rank
                FROM summaries
                WHERE content LIKE ? ESCAPE '\\'
                  AND (? IS NULL OR session_id = ?)
                ORDER BY created_at DESC
                LIMIT ?""",
            (pattern, session_id, session_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_messages_substring(
        self, query: str, session_id: Optional[str] = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        pattern = _like_pattern(query)
        rows = self.conn.execute(
            """SELECT seq, session_id, role, content, 0 AS rank
                FROM messages
                WHERE content LIKE ? ESCAPE '\\'
                  AND (? IS NULL OR session_id = ?)
                ORDER BY created_at DESC
                LIMIT ?""",
            (pattern, session_id, session_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- Maintenance ---

    def db_size_bytes(self) -> int:
        return self.db_path.stat().st_size if self.db_path.exists() else 0

    def stats(self) -> dict[str, Any]:
        session_count = self.conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        msg_count = self.conn.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
        sum_count = self.conn.execute("SELECT COUNT(*) as c FROM summaries").fetchone()["c"]
        return {
            "db_path": str(self.db_path),
            "db_size_mb": round(self.db_size_bytes() / (1024 * 1024), 2),
            "sessions": session_count,
            "messages": msg_count,
            "summaries": sum_count,
        }
