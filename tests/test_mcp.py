"""Tests for the MCP server — protocol + tools."""

import json
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import codex_lcm.mcp_server as mcp
from codex_lcm.compaction import CompactionEngine
from codex_lcm.capture import Capturer

from conftest import MockSummarizer

SID = "sess-m"


def _seed(db):
    db.ensure_session(SID, "/work/demo")
    for i in range(9):
        role = ["user", "tool", "assistant"][i % 3]
        db.ingest_message(SID, i, role, f"[{i}] wiring google oauth and the callback route", stable_key=f"m{i}")


def _run_server(stdin_lines):
    return subprocess.run(
        [sys.executable, "-m", "codex_lcm.mcp_server"],
        input="\n".join(json.dumps(m) for m in stdin_lines) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_server_raw(stdin_text):
    return subprocess.run(
        [sys.executable, "-m", "codex_lcm.mcp_server"],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_protocol_handshake_and_tools(db, config):
    _seed(db)
    r = _run_server([
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ])
    lines = [line for line in r.stdout.strip().split("\n") if line.strip()]
    assert len(lines) == 2  # init + tools/list; the notification gets no response

    init = json.loads(lines[0])
    assert init["result"]["serverInfo"]["name"] == "codex-lcm"
    assert "tools" in init["result"]["capabilities"]

    tools = json.loads(lines[1])["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"lcm_status", "lcm_grep", "lcm_recall", "lcm_expand", "lcm_recent", "lcm_describe"} <= names


def test_unknown_method_returns_jsonrpc_error(db, config):
    r = _run_server([{"jsonrpc": "2.0", "id": 5, "method": "bogus/method"}])
    resp = json.loads([line for line in r.stdout.strip().split("\n") if line.strip()][0])
    assert resp["error"]["code"] == -32601


def test_malformed_requests_return_errors_without_stopping_server(db, config):
    r = _run_server_raw(
        "not-json\n"
        "[]\n"
        '{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
    )

    assert r.returncode == 0
    responses = [json.loads(line) for line in r.stdout.splitlines() if line]
    assert [response.get("error", {}).get("code") for response in responses[:2]] == [
        -32700,
        -32600,
    ]
    assert responses[2] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_call_tool_unknown_is_error(db, config):
    result = mcp.call_tool("nope", {}, db)
    assert result["isError"] is True


def test_grep_and_recall_find_content(db, config):
    _seed(db)
    engine = CompactionEngine(db, config, MockSummarizer())
    engine.run_compaction(SID)

    grep = mcp.tool_grep(db, {"query": "oauth"})
    assert "oauth" in grep.lower()

    recall = mcp.tool_recall(db, {"query": "oauth callback"})
    assert len(recall) > 20


def test_expand_leaf_returns_sources(db, config):
    _seed(db)
    engine = CompactionEngine(db, config, MockSummarizer())
    engine.run_compaction(SID)
    leaf = [s for s in db.get_summaries(SID, active_only=False) if s["kind"] == "leaf"][0]

    out = mcp.tool_expand(db, {"summary_id": leaf["summary_id"]})
    assert "Source messages" in out
    assert leaf["summary_id"] in out
    assert f"{SID}#" in out


def test_expand_condensed_returns_parents(db, config):
    _seed(db)
    p1 = db.create_leaf_summary(SID, "parent one content long enough", [0, 1, 2])
    p2 = db.create_leaf_summary(SID, "parent two content long enough", [3, 4, 5])
    p3 = db.create_leaf_summary(SID, "parent three content long enough", [6, 7, 8])
    cid = db.create_condensed_summary(SID, "rolled up parents", [p1, p2, p3], depth=1)

    out = mcp.tool_expand(db, {"summary_id": cid})
    assert "Parent summaries" in out
    assert p1 in out and p2 in out and p3 in out


def test_expand_summary_uses_bounded_character_pages(db, config):
    db.ensure_session(SID, "/work/demo")
    source_seqs = []
    for index in range(10):
        db.ingest_message(
            SID,
            index,
            "user",
            f"source-{index}-" + ("x" * 1_000),
            stable_key=f"bounded-summary-{index}",
        )
        source_seqs.append(index)
    summary_id = db.create_leaf_summary(
        SID,
        "bounded summary expansion",
        source_seqs,
    )

    first = mcp.tool_expand(db, {"summary_id": summary_id, "max_chars": 100})
    second = mcp.tool_expand(
        db,
        {"summary_id": summary_id, "offset": 100, "max_chars": 100},
    )

    assert "next_offset: 100" in first
    assert "chars 0:100/" in first
    assert "chars 100:200/" in second
    assert first != second
    with pytest.raises(ValueError, match="max_chars"):
        mcp.tool_expand(
            db,
            {"summary_id": summary_id, "max_chars": mcp.MAX_EXPAND_PAGE_CHARS + 1},
        )


def test_describe_session_and_summary(db, config):
    _seed(db)
    out = mcp.tool_describe(db, {"session_id": SID})
    assert SID in out and "messages" in out

    sid = db.create_leaf_summary(SID, "a leaf summary with plenty of content", [0])
    out = mcp.tool_describe(db, {"summary_id": sid})
    assert "depth: 0" in out


def test_fts_query_sanitization():
    # FTS injection-style input must not raise or return garbage.
    q = mcp._safe_fts('oauth "OR 1=1 -- ; DROP TABLE')
    assert q is not None
    assert '"oauth"*' in q


def test_grep_falls_back_to_literal_substring(db, config):
    db.ensure_session(SID, "/work/demo")
    db.ingest_message(
        SID,
        0,
        "assistant",
        "stored marker alphabeticalnebulaword for substring recovery",
        stable_key="substring",
    )

    out = mcp.tool_grep(db, {"query": "beticalneb", "session_id": SID})

    assert "alphabeticalnebulaword" in out


def test_recall_combines_summary_and_raw_evidence(db, config):
    db.ensure_session(SID, "/work/demo")
    db.ingest_message(SID, 0, "user", "deployment handoff raw marker", stable_key="raw")
    db.create_leaf_summary(
        SID,
        "deployment handoff summary marker with enough retained detail",
        [],
    )

    out = mcp.tool_recall(db, {"query": "deployment handoff"})

    assert "summary marker" in out
    assert "raw marker" in out


def test_recent_filters_complete_utc_timestamps(db, config):
    now = datetime.now(timezone.utc)
    db.ensure_session("recent-session", "/work/recent")
    db.ensure_session("stale-session", "/work/stale")
    db.conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
        ((now - timedelta(minutes=30)).isoformat(), "recent-session"),
    )
    db.conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
        ((now - timedelta(hours=2)).isoformat(), "stale-session"),
    )
    db.conn.commit()

    out = mcp.tool_recent(db, {"hours": 1})

    assert "recent-session" in out
    assert "stale-session" not in out
    with pytest.raises(ValueError, match="hours"):
        mcp.tool_recent(db, {"hours": 0})


@pytest.mark.parametrize("value", [True, "1", math.inf, -math.inf, math.nan])
def test_recent_hours_rejects_non_json_numbers(value, db, config):
    with pytest.raises(ValueError, match="hours"):
        mcp.tool_recent(db, {"hours": value})


@pytest.mark.parametrize("value", [True, False, 1.9, "2"])
def test_numeric_arguments_reject_non_integer_json_types(value):
    with pytest.raises(ValueError, match="limit"):
        mcp._positive_int(value, "limit")
    with pytest.raises(ValueError, match="offset"):
        mcp._nonnegative_int(value, "offset")


@pytest.mark.parametrize(
    ("tool", "args", "maximum"),
    [
        (mcp.tool_grep, {"query": "missing"}, mcp.MAX_GREP_RESULTS),
        (mcp.tool_recall, {"query": "missing"}, mcp.MAX_RECALL_RESULTS),
        (mcp.tool_recent, {}, mcp.MAX_RECENT_RESULTS),
    ],
)
def test_result_limits_enforce_documented_maximum(tool, args, maximum, db, config):
    tool(db, {**args, "limit": maximum})

    with pytest.raises(ValueError, match="limit"):
        tool(db, {**args, "limit": maximum + 1})


def test_mcp_dispatch_applies_finite_output_ceiling(db, config):
    result = mcp.call_tool(
        "lcm_grep",
        {"query": "q" * (mcp.MAX_TOOL_OUTPUT_CHARS + 1)},
        db,
    )
    text = result["content"][0]["text"]

    assert result["isError"] is False
    assert len(text) <= mcp.MAX_TOOL_OUTPUT_CHARS
    assert text.endswith("…[tool output truncated]")


def test_expand_message_pages_full_tool_parts(db, config):
    db.ensure_session(SID, "/work/demo")
    Capturer(db, config).capture_tool(
        SID,
        "Bash",
        {"command": "A" * 5000 + " INPUT_TAIL"},
        "B" * 6000 + " OUTPUT_TAIL",
        tool_use_id="paged",
    )

    first = mcp.tool_expand(
        db,
        {"session_id": SID, "message_seq": 0, "offset": 0, "max_chars": 100},
    )
    tail = mcp.tool_expand(
        db,
        {"session_id": SID, "message_seq": 0, "offset": 14_000, "max_chars": 2_000},
    )

    assert "next_offset: 100" in first
    assert "OUTPUT_TAIL" in tail


def test_expand_message_rejects_page_larger_than_documented_boundary(db, config):
    db.ensure_session(SID, "/work/demo")
    Capturer(db, config).capture_tool(
        SID,
        "Bash",
        {"command": "A" * 5_000},
        "B" * 6_000,
        tool_use_id="bounded-page",
    )

    with pytest.raises(ValueError, match="max_chars"):
        mcp.tool_expand(
            db,
            {"session_id": SID, "message_seq": 0, "max_chars": 50_000},
        )


def test_recall_tool_description_is_lexical_not_semantic():
    recall = next(tool for tool in mcp.TOOLS if tool["name"] == "lcm_recall")
    assert "lexical" in recall["description"].lower()
    assert "semantic" not in recall["description"].lower()
