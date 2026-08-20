"""Standalone public-release reliability gauntlet for codex-lcm.

Uses only throwaway storage. It exercises every installed hook, compares hook
latency with the timeout declared by the installer, stresses concurrent hook
writers, checks large-payload retention, and verifies worker-lock contention is
non-blocking.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from codex_lcm import cli
from codex_lcm.config import LCMConfig
from codex_lcm.db import LCMDatabase
from codex_lcm.worker import _acquire_lock


SAMPLES = 100  # enough observations to report p95 without making the gauntlet unbounded
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def hook_contracts() -> dict[str, tuple[str, float]]:
    contracts: dict[str, tuple[str, float]] = {}
    for event, groups in cli._build_hooks(sys.executable).items():
        hook = groups[0]["hooks"][0]
        module = hook["command"].split()[-1].rsplit(".", 1)[-1]
        contracts[module] = (event, float(hook["timeout"]))
    return contracts


def run_hook(
    module: str,
    payload: dict[str, Any],
    env: dict[str, str],
    timeout: float,
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", f"codex_lcm.hooks.{module}"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return result, time.perf_counter() - started


def benchmark_hooks(env: dict[str, str], contracts: dict[str, tuple[str, float]]) -> None:
    session_id = "latency-session"
    benchmark_cwd = str(Path.cwd())
    payloads: dict[str, Callable[[int], dict[str, Any]]] = {
        "session_start": lambda i: {
            "session_id": session_id,
            "cwd": benchmark_cwd,
            "model": "gpt-5",
            "source": "startup",
        },
        "user_prompt_submit": lambda i: {
            "session_id": session_id,
            "cwd": benchmark_cwd,
            "model": "gpt-5",
            "turn_id": f"user-{i}",
            "prompt": f"benchmark prompt {i}",
        },
        "post_tool_use": lambda i: {
            "session_id": session_id,
            "cwd": benchmark_cwd,
            "model": "gpt-5",
            "tool_name": "Bash",
            "tool_input": {"command": f"printf {i}"},
            "tool_response": f"benchmark output {i}",
            "tool_use_id": f"tool-{i}",
        },
        "pre_compact": lambda i: {
            "session_id": session_id,
            "cwd": benchmark_cwd,
            "model": "gpt-5",
            "trigger": "auto",
        },
        # Repeated samples use the documented no-session no-op path so this
        # benchmark does not launch 100 detached workers. A valid lock-contention
        # invocation is exercised separately below.
        "post_compact": lambda i: {"trigger": "auto"},
        "stop": lambda i: {
            "session_id": session_id,
            "cwd": benchmark_cwd,
            "model": "gpt-5",
            "turn_id": f"assistant-{i}",
            "last_assistant_message": f"benchmark assistant reply {i}",
        },
    }

    print("\n--- hook latency (100 samples each) ---")
    for module, (event, timeout) in contracts.items():
        timings: list[float] = []
        exits_ok = True
        stdout_valid = True
        for index in range(SAMPLES):
            result, elapsed = run_hook(module, payloads[module](index), env, timeout + 1)
            timings.append(elapsed)
            exits_ok = exits_ok and result.returncode == 0
            if result.stdout.strip():
                try:
                    json.loads(result.stdout)
                except json.JSONDecodeError:
                    stdout_valid = False
        telemetry = (
            f"p50={percentile(timings, 0.50) * 1000:.2f}ms "
            f"p95={percentile(timings, 0.95) * 1000:.2f}ms "
            f"max={max(timings) * 1000:.2f}ms timeout={timeout:.0f}s"
        )
        check(f"{event} exits successfully", exits_ok, telemetry)
        check(f"{event} stdout is empty or valid JSON", stdout_valid)
        check(f"{event} stays below configured timeout", max(timings) < timeout, telemetry)


def malformed_config_probe(env: dict[str, str], contracts: dict[str, tuple[str, float]]) -> None:
    print("\n--- fail-open malformed configuration ---")
    broken_env = {**env, "CODEX_LCM_FRESH_TAIL": "not-an-integer"}
    payload = {
        "session_id": "malformed-config-session",
        "cwd": str(Path.cwd()),
        "model": "gpt-5",
        "source": "startup",
        "prompt": "test prompt",
        "turn_id": "malformed-config-turn",
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "tool_response": "ok",
        "tool_use_id": "malformed-config-tool",
        "last_assistant_message": "test response",
    }
    for module in ("session_start", "user_prompt_submit", "post_tool_use", "stop"):
        event, timeout = contracts[module]
        result, _ = run_hook(module, payload, broken_env, timeout + 1)
        check(f"{event} fails open on malformed configuration", result.returncode == 0)


def fresh_database_initialization_stress(env: dict[str, str]) -> None:
    print("\n--- concurrent fresh-database initialization ---")
    session_id = "fresh-initialization-session"
    child = (
        "from codex_lcm.config import LCMConfig; "
        "from codex_lcm.db import LCMDatabase; "
        "import sys; "
        "i = int(sys.argv[1]); "
        "db = LCMDatabase(LCMConfig()); "
        f'db.ensure_session("{session_id}", ""); '
        f'db.append_message("{session_id}", "user", f"message-{{i}}", stable_key=f"key-{{i}}"); '
        "db.close()"
    )

    def invoke(index: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", child, str(index)],
            capture_output=True,
            text=True,
            env=env,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(invoke, range(128)))

    check(
        "fresh-database processes exit successfully",
        all(result.returncode == 0 for result in results),
    )
    db = LCMDatabase(LCMConfig())
    try:
        messages = db.get_messages(session_id)
        fts_count = db.conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        check("fresh-database writes are complete", len(messages) == 128, str(len(messages)))
        check("fresh-database FTS rows are complete", fts_count == 128, str(fts_count))
        check(
            "fresh-database sequences are contiguous",
            [message["seq"] for message in messages] == list(range(128)),
        )
    finally:
        db.close()


def concurrency_stress(env: dict[str, str], timeout: float) -> None:
    print("\n--- concurrent hook writers ---")

    def invoke(session_id: str, index: int):
        payload = {
            "session_id": session_id,
            "cwd": "",
            "model": "gpt-5",
            "tool_name": "Bash",
            "tool_input": {"command": f"concurrent {index}"},
            "tool_response": f"concurrent result {index}",
            "tool_use_id": f"{session_id}:{index}",
        }
        return run_hook("post_tool_use", payload, env, timeout + 1)

    same_session = "parallel-same-session"
    with ThreadPoolExecutor(max_workers=16) as pool:
        same_results = list(pool.map(lambda i: invoke(same_session, i), range(64)))

    cross_jobs = [(f"parallel-session-{group}", index) for group in range(4) for index in range(16)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        cross_results = list(pool.map(lambda job: invoke(*job), cross_jobs))

    check(
        "same-session concurrent hook processes exit successfully",
        all(result.returncode == 0 for result, _ in same_results),
    )
    check(
        "cross-session concurrent hook processes exit successfully",
        all(result.returncode == 0 for result, _ in cross_results),
    )

    db = LCMDatabase(LCMConfig())
    try:
        same_messages = db.get_messages(same_session)
        check("same-session writes are complete", len(same_messages) == 64, str(len(same_messages)))
        check(
            "same-session sequences are contiguous",
            [message["seq"] for message in same_messages] == list(range(64)),
        )
        for group in range(4):
            sid = f"parallel-session-{group}"
            check(f"{sid} writes are complete", db.get_message_count(sid) == 16)
        expected = 64 + len(cross_jobs)
        placeholders = ",".join("?" for _ in range(5))
        sessions = [same_session] + [f"parallel-session-{group}" for group in range(4)]
        fts_count = db.conn.execute(
            f"SELECT COUNT(*) FROM messages_fts WHERE session_id IN ({placeholders})",
            sessions,
        ).fetchone()[0]
        check("concurrent messages and FTS rows commit together", fts_count == expected, str(fts_count))
    finally:
        db.close()


def large_payload_probe(env: dict[str, str], timeout: float) -> None:
    print("\n--- large payload retention ---")
    session_id = "large-payload-session"
    input_tail = "INPUT_PAYLOAD_TAIL"
    output_tail = "OUTPUT_PAYLOAD_TAIL"
    payload = {
        "session_id": session_id,
        "cwd": "",
        "model": "gpt-5",
        "tool_name": "Bash",
        "tool_input": {"command": ("I" * 100_000) + input_tail},
        "tool_response": ("O" * 120_000) + output_tail,
        "tool_use_id": "large-payload",
    }
    result, elapsed = run_hook("post_tool_use", payload, env, timeout + 1)
    check("large-payload hook exits successfully", result.returncode == 0)
    check("large-payload hook stays below configured timeout", elapsed < timeout, f"{elapsed:.3f}s")

    db = LCMDatabase(LCMConfig())
    try:
        message = db.get_messages(session_id)[0]
        parts = db.get_message_parts(session_id, message["seq"])
        preview = json.loads(message["content"])
        check(
            "large tool preview is bounded",
            "truncated preview" in preview["input"] and "truncated preview" in preview["result"],
        )
        check("full tool input tail is retained", input_tail in parts[0]["content"])
        check("full tool output tail is retained", parts[1]["content"].endswith(output_tail))
    finally:
        db.close()


def worker_lock_probe(env: dict[str, str], timeout: float) -> None:
    print("\n--- worker lock contention ---")
    config = LCMConfig()
    session_id = "locked-worker-session"
    db = LCMDatabase(config)
    db.ensure_session(session_id, "")
    db.close()
    lock_path = config.db_path.parent / "locks" / f"{session_id}.lock"
    lock = _acquire_lock(lock_path)
    check("test acquired worker lock", lock is not None)
    if lock is None:
        return
    try:
        result, elapsed = run_hook(
            "post_compact",
            {"session_id": session_id, "trigger": "auto"},
            env,
            timeout + 1,
        )
        check("PostCompact returns while worker lock is held", result.returncode == 0)
        check("PostCompact lock-contention path is non-blocking", elapsed < timeout, f"{elapsed:.3f}s")
    finally:
        import fcntl

        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main() -> int:
    contracts = hook_contracts()
    expected = {
        "session_start",
        "user_prompt_submit",
        "post_tool_use",
        "pre_compact",
        "post_compact",
        "stop",
    }
    check("installer declares all six hooks", set(contracts) == expected, str(sorted(contracts)))

    with tempfile.TemporaryDirectory(prefix="codex-lcm-reliability-") as tmp:
        db_path = Path(tmp) / "reliability.db"
        env = {
            **os.environ,
            "CODEX_LCM_DB_PATH": str(db_path),
            "CODEX_LCM_COMPACTION_BACKLOG": "1000000",
            "CODEX_LCM_FRESH_TAIL": "5",
        }
        os.environ.update(
            {
                "CODEX_LCM_DB_PATH": str(db_path),
                "CODEX_LCM_COMPACTION_BACKLOG": "1000000",
                "CODEX_LCM_FRESH_TAIL": "5",
            }
        )

        fresh_database_initialization_stress(env)
        benchmark_hooks(env, contracts)
        malformed_config_probe(env, contracts)
        concurrency_stress(env, contracts["post_tool_use"][1])
        large_payload_probe(env, contracts["post_tool_use"][1])
        worker_lock_probe(env, contracts["post_compact"][1])

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"RELIABILITY GAUNTLET FAILED: {len(FAILURES)} checks")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("RELIABILITY GAUNTLET PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
