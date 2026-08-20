"""Stop hook — capture the final assistant message and trigger background compaction."""

import sys
import os

from ..config import LCMConfig
from ..db import LCMDatabase
from ..capture import Capturer
from ._common import read_input, git_info, spawn_compaction


def main() -> None:
    data = read_input()
    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    cwd = data.get("cwd", "")
    model = data.get("model", "")
    turn_id = data.get("turn_id")
    last_message = data.get("last_assistant_message") or ""

    db = None
    try:
        config = LCMConfig()
        db = LCMDatabase(config)
        remote, branch = git_info(cwd)
        db.ensure_session(session_id, cwd, remote, branch, model)

        capturer = Capturer(db, config)
        capturer.capture_assistant(session_id, last_message, turn_id)

        # Trigger background compaction only when there's a real backlog of
        # unsummarized messages beyond the protected fresh tail.
        backlog_margin = int(os.environ.get("CODEX_LCM_COMPACTION_BACKLOG", "5"))
        unsummarized = db.get_unsummarized_count(session_id)
        if unsummarized > config.fresh_tail + backlog_margin:
            spawn_compaction(session_id)
    except Exception:
        return
    finally:
        if db is not None:
            db.close()

    # No stdout output → no continuation decision; Codex proceeds normally.
    sys.exit(0)


if __name__ == "__main__":
    main()
