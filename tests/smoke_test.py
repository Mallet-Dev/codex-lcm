"""End-to-end smoke test for codex-lcm (no LLM key — uses truncation fallback)."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import codex_lcm.mcp_server as mcp
from codex_lcm.assembler import Assembler
from codex_lcm.capture import Capturer
from codex_lcm.compaction import CompactionEngine
from codex_lcm.config import LCMConfig
from codex_lcm.db import LCMDatabase
from codex_lcm.secrets import redact

# Use a throwaway DB so we don't touch the real one.
TMP = Path(tempfile.mkdtemp(prefix="codex-lcm-test-"))
os.environ["CODEX_LCM_DB_PATH"] = str(TMP / "test.db")
os.environ["CODEX_LCM_FRESH_TAIL"] = "3"  # small tail so compaction triggers

PY = sys.executable

failures = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


# --- 1. capture pipeline (direct) ---
config = LCMConfig()
db = LCMDatabase(config)
SID = "test-session-1"
db.ensure_session(SID, "/work/demo", "git@github.com:me/demo.git", "main", "gpt-5")

cap = Capturer(db, config)
cap.capture_user(SID, "Build a login page with React and Supabase email auth", turn_id="t1")
cap.capture_tool(SID, "Bash", {"command": "npm install @supabase/supabase-js"}, "added 45 packages", "u1")
cap.capture_tool(SID, "Write", {"path": "src/Login.tsx"}, "wrote 120 lines", "u2")
cap.capture_assistant(SID, "Created Login.tsx with email/password form and Supabase client.", turn_id="t1")

cap.capture_user(SID, "Add password reset flow and a tests file", turn_id="t2")
cap.capture_tool(SID, "Bash", {"command": "npm test"}, "2 passed, 1 failed: reset.test.ts", "u3")
cap.capture_assistant(SID, "Fixed the reset flow and added reset.test.ts; all tests pass.", turn_id="t2")

cap.capture_user(SID, "Wire up Google OAuth now", turn_id="t3")
cap.capture_assistant(SID, "Added Google OAuth provider config to the Supabase client.", turn_id="t3")

check("captured 9 messages", db.get_message_count(SID) == 9, str(db.get_message_count(SID)))
check("unsummarized count", db.get_unsummarized_count(SID) == 9)

# --- 2. compaction (truncation fallback, no API key) ---
engine = CompactionEngine(db, config)
result = engine.run_compaction(SID)
print(f"       compaction result: {result}")
check("compaction created leaf summaries", result.leaf_summaries_created >= 1)
summaries = db.get_summaries(SID)
check("summaries persisted", len(summaries) >= 1, f"{len(summaries)} summaries")

# --- 3. assembler restore ---
asm = Assembler(db, config)
restore = asm.build_restore_context(SID, is_compacted=True)
check("restore context non-empty", "<lc-restore>" in restore and len(restore) > 50)
check("restore mentions compaction", "compacted" in restore.lower() or "summar" in restore.lower())

# --- 4. MCP tools (direct) ---
check("lcm_status returns stats", "sessions" in mcp.tool_status(db, {}))
grep = mcp.tool_grep(db, {"query": "supabase"})
check("lcm_grep finds supabase", "supabase" in grep.lower(), repr(grep[:80]))
recall = mcp.tool_recall(db, {"query": "login page"})
check("lcm_recall returns content", len(recall) > 20)
sid_sum = summaries[0]["summary_id"]
exp = mcp.tool_expand(db, {"summary_id": sid_sum})
check("lcm_expand returns summary", sid_sum in exp)
desc = mcp.tool_describe(db, {"session_id": SID})
check("lcm_describe returns session", SID in desc)
recent = mcp.tool_recent(db, {"hours": 24})
check("lcm_recent lists session", SID[:12] in recent)

# --- 5. secret redaction ---
api_key = "s" + "k-" + ("a" * 32)
github_token = "gh" + "p_" + ("b" * 36)
dirty = f'api_key="{api_key}" token={github_token}'
clean = redact(dirty)
check("redacts api key", api_key not in clean)
check("redacts github token", github_token not in clean)

db.close()

# --- 6. hook entry points via subprocess (as Codex invokes them) ---
print("\n--- hook subprocess tests ---")


def run_hook(module, payload):
    r = subprocess.run(
        [PY, "-m", f"codex_lcm.hooks.{module}"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ},
        timeout=30,
    )
    return r


# user_prompt_submit
r = run_hook("user_prompt_submit", {"session_id": "sub-session", "cwd": "/work", "model": "gpt-5", "prompt": "hello world", "turn_id": "t99"})
check("user_prompt_submit exits 0", r.returncode == 0, r.stderr.strip())

# stop
r = run_hook("stop", {"session_id": "sub-session", "cwd": "/work", "model": "gpt-5", "turn_id": "t99", "last_assistant_message": "hi back"})
check("stop exits 0", r.returncode == 0, r.stderr.strip())

# session_start (no context yet → no stdout)
r = run_hook("session_start", {"session_id": "sub-session", "cwd": "/work", "model": "gpt-5", "source": "startup"})
check("session_start exits 0", r.returncode == 0, r.stderr.strip())

# verify sub-session captured
db2 = LCMDatabase(config)
check("subprocess capture persisted", db2.get_message_count("sub-session") >= 1)
db2.close()

# --- 7. MCP server protocol via stdin ---
print("\n--- MCP server protocol test ---")
msgs = [
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "lcm_status", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "lcm_grep", "arguments": {"query": "supabase"}}},
]
stdin = "\n".join(json.dumps(m) for m in msgs) + "\n"
r = subprocess.run(
    [PY, "-m", "codex_lcm.mcp_server"],
    input=stdin, capture_output=True, text=True, env={**os.environ}, timeout=30,
)
lines = [line for line in r.stdout.strip().split("\n") if line.strip()]
check("mcp server returns responses", len(lines) >= 4, f"{len(lines)} responses")
try:
    init_resp = json.loads(lines[0])
    check("initialize result", init_resp.get("result", {}).get("serverInfo", {}).get("name") == "codex-lcm")
    # Notification (notifications/initialized) produces no response line, so
    # responses map: [0]=init, [1]=tools/list, [2]=lcm_status, [3]=lcm_grep
    tools_resp = json.loads(lines[1])
    tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
    check("tools/list has 6 tools", len(tool_names) == 6, str(tool_names))
    status_resp = json.loads(lines[2])
    check("tools/call lcm_status ok", status_resp["result"]["isError"] is False)
    grep_resp = json.loads(lines[3])
    check("tools/call lcm_grep ok", grep_resp["result"]["isError"] is False)
except Exception as e:
    check("mcp responses parse", False, str(e))

print("\n" + "=" * 50)
if failures:
    print(f"FAILED: {len(failures)} checks")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
