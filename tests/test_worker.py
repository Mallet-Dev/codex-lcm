"""Tests for the background worker's flock-based mutual exclusion."""

import os

import pytest

from codex_lcm.worker import run_worker, _acquire_lock


@pytest.mark.skipif(os.name == "nt", reason="flock unavailable on Windows")
def test_flock_prevents_concurrent_worker(config, tmp_path):
    # Hold the session lock, then spawn a worker: it must bail without doing work.
    lock_path = config.db_path.parent / "locks" / "busy-session.lock"
    lock = _acquire_lock(lock_path)
    assert lock is not None

    try:
        run_worker("busy-session", synchronous=False)
        log_path = config.db_path.parent / "logs" / "compaction.log"
        if log_path.exists():
            assert "busy-session" not in log_path.read_text()
    finally:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def test_second_acquire_returns_none(config):
    lock_path = config.db_path.parent / "locks" / "second.lock"
    first = _acquire_lock(lock_path)
    assert first is not None
    try:
        second = _acquire_lock(lock_path)
        assert second is None, "second lock acquisition must fail while held"
    finally:
        import fcntl
        fcntl.flock(first, fcntl.LOCK_UN)
        first.close()
