"""Dependency-free MCP server (stdio / newline-delimited JSON-RPC 2.0).

Exposes codex-lcm recall tools to Codex. Implemented with the standard library
only, so it is immune to MCP SDK version churn and works out of the box.

Protocol: read one JSON-RPC message per line from stdin, write one JSON-RPC
response per line to stdout. Handles initialize / tools/list / tools/call /
ping, plus empty responses for notifications.
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import __version__
from .config import LCMConfig
from .db import LCMDatabase

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "codex-lcm"
MAX_EXPAND_PAGE_CHARS = 2000
MAX_GREP_RESULTS = 10
MAX_RECALL_RESULTS = 5
MAX_RECENT_RESULTS = 20
MAX_TOOL_OUTPUT_CHARS = MAX_RECENT_RESULTS * 1200

TOOLS: list[dict[str, Any]] = [
    {
        "name": "lcm_status",
        "description": "Health and storage status of the codex-lcm memory database.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "lcm_grep",
        "description": (
            "FTS5 lexical search with literal substring fallback across messages and summaries. "
            "Returns snippets with session and summary references."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms."},
                "session_id": {"type": "string", "description": "Optional session to restrict search to."},
                "limit": {
                    "type": "integer",
                    "maximum": MAX_GREP_RESULTS,
                    "description": f"Max results (default and maximum {MAX_GREP_RESULTS}).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "lcm_recall",
        "description": (
            "Lexical FTS5 recall with literal substring fallback across retained summaries and raw messages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What are you trying to remember?"},
                "limit": {
                    "type": "integer",
                    "maximum": MAX_RECALL_RESULTS,
                    "description": f"Max results (default and maximum {MAX_RECALL_RESULTS}).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "lcm_expand",
        "description": (
            "Drill into a summary: for a leaf summary returns its source messages, "
            "for a condensed summary returns its parent summaries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary_id": {"type": "string", "description": "The summary id (sum_...)."},
                "session_id": {"type": "string", "description": "Session id for raw message expansion."},
                "message_seq": {"type": "integer", "description": "Message sequence for raw expansion."},
                "offset": {"type": "integer", "description": "Character offset for paged raw expansion."},
                "max_chars": {"type": "integer", "description": "Characters to return (maximum 2000)."},
            },
        },
    },
    {
        "name": "lcm_recent",
        "description": "Recent activity across sessions in a time window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {"type": "number", "description": "Look-back window in hours (default 24)."},
                "limit": {
                    "type": "integer",
                    "maximum": MAX_RECENT_RESULTS,
                    "description": f"Max sessions (default and maximum {MAX_RECENT_RESULTS}).",
                },
            },
        },
    },
    {
        "name": "lcm_describe",
        "description": "Structured metadata for a session or summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "summary_id": {"type": "string"},
            },
        },
    },
]


def _safe_fts(query: str) -> Optional[str]:
    """Build a safe FTS5 MATCH expression from free text.

    Splits on non-word characters, quotes each token, enables prefix matching,
    and ORs the terms so multi-term queries recall broadly.
    """
    tokens = [t for t in re.split(r"\W+", query) if t]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"*' for t in tokens)


def _truncate(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def _bound_tool_output(text: str) -> str:
    marker = "\n…[tool output truncated]"
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[: MAX_TOOL_OUTPUT_CHARS - len(marker)] + marker


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _bounded_positive_int(value: Any, name: str, maximum: int) -> int:
    result = _positive_int(value, name)
    if result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero")
    return result


# --- Tool implementations ---


def tool_status(db: LCMDatabase, args: dict) -> str:
    s = db.stats()
    return (
        "codex-lcm status:\n"
        f"  db: {s['db_path']} ({s['db_size_mb']} MB)\n"
        f"  sessions: {s['sessions']}\n"
        f"  messages: {s['messages']}\n"
        f"  summaries: {s['summaries']}"
    )


def tool_grep(db: LCMDatabase, args: dict) -> str:
    query = args.get("query", "")
    session_id = args.get("session_id")
    limit = _bounded_positive_int(args.get("limit", MAX_GREP_RESULTS), "limit", MAX_GREP_RESULTS)
    fts = _safe_fts(query)
    if not fts:
        return "No searchable terms provided."

    summaries = db.search_summaries(fts, session_id=session_id, limit=limit)
    messages = db.search_messages(fts, session_id=session_id, limit=limit)
    ranked = [("summary", item) for item in summaries] + [("message", item) for item in messages]
    if not ranked and query.strip():
        summaries = db.search_summaries_substring(query.strip(), session_id=session_id, limit=limit)
        messages = db.search_messages_substring(query.strip(), session_id=session_id, limit=limit)
        ranked = [("summary", item) for item in summaries] + [("message", item) for item in messages]
    ranked.sort(key=lambda item: item[1].get("rank", 0))

    lines = [f"lcm_grep results for '{query}':"]
    for kind, item in ranked[:limit]:
        if kind == "summary":
            s = item
            lines.append(
                f"\n[Summary] {s['summary_id']} (depth {s['depth']}) "
                f"{s.get('earliest_at','')}→{s.get('latest_at','')}:\n{_truncate(s['content'])}"
            )
        else:
            m = item
            lines.append(
                f"\n[Message] {m.get('session_id','')}#{m.get('seq','?')} "
                f"{m.get('role','?')}: {_truncate(m['content'])}"
            )
    if not ranked:
        lines.append("No matches.")
    return "\n".join(lines)


def tool_recall(db: LCMDatabase, args: dict) -> str:
    query = args.get("query", "")
    limit = _bounded_positive_int(
        args.get("limit", MAX_RECALL_RESULTS), "limit", MAX_RECALL_RESULTS
    )
    fts = _safe_fts(query)
    if not fts:
        return "No searchable terms provided."

    summaries = db.search_summaries(fts, session_id=None, limit=limit)
    messages = db.search_messages(fts, session_id=None, limit=limit)
    ranked = [("summary", item) for item in summaries] + [("message", item) for item in messages]
    if not ranked and query.strip():
        summaries = db.search_summaries_substring(query.strip(), session_id=None, limit=limit)
        messages = db.search_messages_substring(query.strip(), session_id=None, limit=limit)
        ranked = [("summary", item) for item in summaries] + [("message", item) for item in messages]
    ranked.sort(key=lambda item: item[1].get("rank", 0))
    if not ranked:
        return f"No prior context found for '{query}'."
    lines = [f"Recall for '{query}':"]
    for kind, item in ranked[:limit]:
        if kind == "summary":
            s = item
            lines.append(
                f"### Summary {s['summary_id']} (depth {s['depth']}, "
                f"{s.get('earliest_at','')}→{s.get('latest_at','')})\n"
                f"{_truncate(s['content'], 1200)}"
            )
        else:
            m = item
            lines.append(
                f"### Message {m.get('session_id','')}#{m.get('seq','?')} ({m.get('role','?')})\n"
                f"{_truncate(m['content'], 1200)}"
            )
    return "\n\n".join(lines)


def _page_text(label: str, payload: str, args: dict) -> str:
    offset = _nonnegative_int(args.get("offset", 0), "offset")
    max_chars = _bounded_positive_int(
        args.get("max_chars", MAX_EXPAND_PAGE_CHARS),
        "max_chars",
        MAX_EXPAND_PAGE_CHARS,
    )
    end = min(len(payload), offset + max_chars)
    lines = [f"{label} chars {offset}:{end}/{len(payload)}:", payload[offset:end]]
    if end < len(payload):
        lines.append(f"next_offset: {end}")
    return "\n".join(lines)


def tool_expand(db: LCMDatabase, args: dict) -> str:
    summary_id = args.get("summary_id")
    session_id = args.get("session_id")
    message_seq = args.get("message_seq")

    if session_id is not None and message_seq is not None:
        seq = _nonnegative_int(message_seq, "message_seq")
        message = db.get_message(session_id, seq)
        if not message:
            return f"Message '{session_id}#{seq}' not found."
        payload = json.dumps(
            {
                "session_id": session_id,
                "seq": seq,
                "role": message["role"],
                "content": message["content"],
                "parts": [
                    {"kind": part["kind"], "content": part["content"]}
                    for part in db.get_message_parts(session_id, seq)
                ],
            },
            ensure_ascii=False,
        )
        return _page_text(f"Message {session_id}#{seq}", payload, args)

    if not summary_id:
        return "Provide summary_id, or session_id with message_seq."
    summary = db.get_summary(summary_id)
    if not summary:
        return f"Summary '{summary_id}' not found."

    lines = [
        f"Summary {summary_id} (session={summary['session_id']}, kind={summary['kind']}, depth={summary['depth']}, "
        f"{summary.get('earliest_at','')}→{summary.get('latest_at','')}):\n"
        f"{summary['content']}\n"
    ]

    if summary["kind"] == "leaf":
        sources = db.get_summary_source_messages(summary_id)
        lines.append(f"\nSource messages ({len(sources)}):")
        for m in sources:
            lines.append(
                f"  {m['session_id']}#{m['seq']} {m['role']}: {_truncate(m['content'], 500)}"
            )
    else:
        parents = db.get_summary_parents(summary_id)
        lines.append(f"\nParent summaries ({len(parents)}):")
        for p in parents:
            lines.append(f"  - {p['summary_id']} (depth {p['depth']}): {_truncate(p['content'], 300)}")
    return _page_text(f"Summary {summary_id}", "\n".join(lines), args)


def tool_recent(db: LCMDatabase, args: dict) -> str:
    hours = _positive_number(args.get("hours", 24), "hours")
    limit = _bounded_positive_int(
        args.get("limit", MAX_RECENT_RESULTS), "limit", MAX_RECENT_RESULTS
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    sessions = db.list_sessions(updated_since=cutoff, limit=limit)

    lines = [f"Sessions active in the last {hours:g} hours:"]
    found = 0
    for s in sessions:
        lines.append(
            f"  - {s['session_id']}  updated {s['updated_at']}  "
            f"cwd={s.get('cwd','?')}  model={s.get('model','?')}"
        )
        found += 1
    if not found:
        lines.append("  (none)")
    return "\n".join(lines)


def tool_describe(db: LCMDatabase, args: dict) -> str:
    session_id = args.get("session_id")
    summary_id = args.get("summary_id")

    if summary_id:
        summary = db.get_summary(summary_id)
        if not summary:
            return f"Summary '{summary_id}' not found."
        return (
            f"Summary {summary_id}:\n"
            f"  kind: {summary['kind']}\n"
            f"  depth: {summary['depth']}\n"
            f"  session: {summary['session_id']}\n"
            f"  tokens: {summary['token_count']}\n"
            f"  descendants: {summary['descendant_count']}\n"
            f"  range: {summary.get('earliest_at','')} → {summary.get('latest_at','')}\n"
            f"  created: {summary['created_at']}"
        )

    if session_id:
        s = db.get_session(session_id)
        if not s:
            return f"Session '{session_id}' not found."
        msg_count = db.get_message_count(session_id)
        sum_count = len(db.get_summaries(session_id))
        tokens = db.get_context_token_count(session_id)
        return (
            f"Session {session_id}:\n"
            f"  cwd: {s.get('cwd','?')}\n"
            f"  model: {s.get('model','?')}\n"
            f"  project: {s.get('project_key','?')}\n"
            f"  messages: {msg_count}\n"
            f"  summaries: {sum_count}\n"
            f"  context tokens (est): {tokens}\n"
            f"  created: {s.get('created_at','')}\n"
            f"  updated: {s.get('updated_at','')}"
        )

    return "Provide session_id or summary_id."


def call_tool(name: str, args: dict, db: LCMDatabase) -> dict[str, Any]:
    dispatch = {
        "lcm_status": tool_status,
        "lcm_grep": tool_grep,
        "lcm_recall": tool_recall,
        "lcm_expand": tool_expand,
        "lcm_recent": tool_recent,
        "lcm_describe": tool_describe,
    }
    fn = dispatch.get(name)
    if fn is None:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    try:
        text = _bound_tool_output(fn(db, args or {}))
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Tool error: {e}"}], "isError": True}


def main() -> None:
    config = LCMConfig()
    db = LCMDatabase(config)
    out = sys.stdout

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }
                out.write(json.dumps(response) + "\n")
                out.flush()
                continue

            if (
                not isinstance(msg, dict)
                or msg.get("jsonrpc") != "2.0"
                or not isinstance(msg.get("method"), str)
            ):
                msg_id = msg.get("id") if isinstance(msg, dict) else None
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32600, "message": "Invalid Request"},
                }
                out.write(json.dumps(response) + "\n")
                out.flush()
                continue

            method = msg["method"]
            msg_id = msg.get("id")

            # Notifications have no id — no response expected.
            if msg_id is None:
                continue

            response: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
            try:
                if method == "initialize":
                    response["result"] = {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": SERVER_NAME, "version": __version__},
                    }
                elif method == "tools/list":
                    response["result"] = {"tools": TOOLS}
                elif method == "tools/call":
                    params = msg.get("params") or {}
                    if not isinstance(params, dict):
                        raise ValueError("params must be an object")
                    tool_name = params.get("name")
                    if not isinstance(tool_name, str):
                        raise ValueError("name must be a string")
                    tool_args = params.get("arguments") or {}
                    if not isinstance(tool_args, dict):
                        raise ValueError("arguments must be an object")
                    response["result"] = call_tool(tool_name, tool_args, db)
                elif method == "ping":
                    response["result"] = {}
                elif method == "resources/list":
                    response["result"] = {"resources": []}
                elif method == "prompts/list":
                    response["result"] = {"prompts": []}
                else:
                    response["error"] = {"code": -32601, "message": f"Method not found: {method}"}
            except ValueError as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": str(exc)},
                }
            except Exception as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": str(exc)},
                }

            out.write(json.dumps(response) + "\n")
            out.flush()
    finally:
        db.close()


if __name__ == "__main__":
    main()
