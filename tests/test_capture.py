"""Capture-path tests: atomic append, concurrency, and payload retention."""

from concurrent.futures import ThreadPoolExecutor

from codex_lcm.capture import Capturer
from codex_lcm.db import LCMDatabase


SID = "capture-session"


def test_sequence_collision_does_not_create_phantom_fts_row(db):
    db.ensure_session(SID, "/work/demo")
    assert db.ingest_message(SID, 0, "user", "first payload", stable_key="first") is True

    inserted = db.ingest_message(
        SID,
        0,
        "assistant",
        "phantom collision payload",
        stable_key="collision",
    )

    assert inserted is False
    assert db.get_message_count(SID) == 1
    assert db.search_messages("phantom", session_id=SID) == []
    fts_count = db.conn.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE session_id = ?", (SID,)
    ).fetchone()[0]
    assert fts_count == 1


def test_concurrent_append_allocates_contiguous_sequences(config):
    setup_db = LCMDatabase(config)
    setup_db.ensure_session(SID, "/work/demo")
    setup_db.close()

    def append_one(index: int):
        local_db = LCMDatabase(config)
        try:
            local_db.ensure_session(SID, "/work/demo")
            return local_db.append_message(
                session_id=SID,
                role="tool",
                content=f"parallel payload {index}",
                stable_key=f"parallel:{index}",
            )
        finally:
            local_db.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        allocated = list(pool.map(append_one, range(100)))

    check_db = LCMDatabase(config)
    try:
        messages = check_db.get_messages(SID)
        assert len(messages) == 100
        assert [m["seq"] for m in messages] == list(range(100))
        assert sorted(allocated) == list(range(100))
        fts_count = check_db.conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE session_id = ?", (SID,)
        ).fetchone()[0]
        assert fts_count == 100
    finally:
        check_db.close()


def test_concurrent_duplicate_stable_key_is_exactly_once(config):
    setup_db = LCMDatabase(config)
    setup_db.ensure_session(SID, "/work/demo")
    setup_db.close()

    def append_duplicate(_: int):
        local_db = LCMDatabase(config)
        try:
            return local_db.append_message(
                session_id=SID,
                role="assistant",
                content="same event replayed",
                stable_key="assistant:one-turn",
            )
        finally:
            local_db.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(append_duplicate, range(40)))

    check_db = LCMDatabase(config)
    try:
        assert check_db.get_message_count(SID) == 1
        assert sum(seq is not None for seq in outcomes) == 1
        assert check_db.conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 1
    finally:
        check_db.close()


def test_tool_capture_keeps_full_redacted_payload_in_parts(db, config):
    db.ensure_session(SID, "/work/demo")
    capturer = Capturer(db, config)
    fake_token = "gh" + "p_" + ("a" * 36)
    fake_aws_value = "awsBase64+/=secret"
    fake_bearer = "abcDEF0123+/==XYZ"
    large_input = "I" * 5000 + f" token={fake_token}"
    large_output = "O" * 7000 + f" Authorization: Bearer {fake_bearer}"

    capturer.capture_tool(
        SID,
        "Bash",
        {"command": large_input, "aws_secret_access_key": fake_aws_value},
        large_output,
        tool_use_id="large-tool",
    )

    message = db.get_messages(SID)[0]
    parts = db.get_message_parts(SID, message["seq"])
    assert len(message["content"]) < len(large_input) + len(large_output)
    assert "truncated" in message["content"]
    assert [part["kind"] for part in parts] == ["tool_input", "tool_result"]
    assert len(parts[0]["content"]) > 5000
    assert len(parts[1]["content"]) > 7000
    persisted = message["content"] + "".join(part["content"] for part in parts)
    assert fake_token not in persisted
    assert fake_aws_value not in persisted
    assert fake_bearer not in persisted
    assert "[REDACTED" in persisted
