"""Background compaction worker — flock-guarded, detached, idempotent."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows fallback
    fcntl = None

from .config import LCMConfig
from .db import LCMDatabase
from .compaction import CompactionEngine


def _acquire_lock(lock_path: Path):
    """Return an open lock file handle holding an exclusive non-blocking flock.

    Returns None if another worker already holds the lock (so concurrent spawns
    collapse to a single active compactor).
    """
    if fcntl is None:
        return None  # no flock on this platform; skip locking
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(lock_path.parent, 0o700)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    f = os.fdopen(fd, "w")
    if os.name == "posix":
        os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def run_worker(session_id: str, synchronous: bool = False) -> None:
    config = LCMConfig()
    db = LCMDatabase(config)

    lock_path = config.db_path.parent / "locks" / f"{session_id}.lock"
    lock = _acquire_lock(lock_path)
    if lock is None:
        db.close()
        return  # another worker is compacting this session

    try:
        engine = CompactionEngine(db, config)
        result = engine.run_compaction(session_id)
        if synchronous:
            print(result)
        else:
            log_path = config.db_path.parent / "logs" / "compaction.log"
            log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name == "posix":
                os.chmod(log_path.parent, 0o700)
            fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                if os.name == "posix":
                    os.chmod(log_path, 0o600)
                f.write(f"{session_id}: {result}\n")
    finally:
        if lock is not None and fcntl is not None:
            with suppress(OSError):
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        db.close()
