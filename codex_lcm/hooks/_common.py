"""Shared helpers for codex-lcm hook scripts."""

from __future__ import annotations

import os
import json
import shutil
# All invocations use fixed argv lists without a shell.
import subprocess  # nosec B404
import sys
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit


def read_input() -> dict[str, Any]:
    """Read and parse the JSON payload Codex writes to the hook's stdin."""
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def git_info(cwd: str) -> tuple[Optional[str], Optional[str]]:
    """Return (remote_url, branch) for the session cwd, or (None, None)."""
    if not cwd or not os.path.isdir(cwd):
        return None, None
    git = shutil.which("git")
    if git is None:
        return None, None
    remote: Optional[str] = None
    branch: Optional[str] = None
    try:
        # The executable is resolved to an absolute path and every argv token is explicit.
        raw_remote = subprocess.check_output(  # nosec
            [git, "-C", cwd, "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip() or None
        remote = _sanitize_remote_url(raw_remote)
    except (OSError, subprocess.SubprocessError):
        remote = None
    try:
        branch = subprocess.check_output(  # nosec
            [git, "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip() or None
    except (OSError, subprocess.SubprocessError):
        branch = None
    return remote, branch


def _sanitize_remote_url(remote: Optional[str]) -> Optional[str]:
    """Remove URL credentials and non-identity query data before persistence."""
    if not remote or "://" not in remote:
        return remote
    try:
        parsed = urlsplit(remote)
        if not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return None
    safe_user = "git@" if parsed.username == "git" and parsed.password is None else ""
    return urlunsplit((parsed.scheme, f"{safe_user}{host}{port}", parsed.path, "", ""))


def spawn_compaction(session_id: str) -> None:
    """Spawn a detached background compaction worker for a session.

    The worker is flock-guarded, so concurrent spawns are harmless (one wins,
    the rest exit immediately). Detached via start_new_session so it survives
    the Codex hook process exiting.
    """
    try:
        env = dict(os.environ)
        # sys.executable is absolute and the worker command does not use a shell.
        subprocess.Popen(  # nosec B603
            [sys.executable, "-m", "codex_lcm", "compact", "--session", session_id],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError):
        # Never let a worker spawn failure break the Codex turn.
        return
