"""UserPromptSubmit hook — capture the user's prompt before it is sent."""

import sys

from ..config import LCMConfig
from ..db import LCMDatabase
from ..capture import Capturer
from ._common import read_input, git_info


def main() -> None:
    data = read_input()
    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    cwd = data.get("cwd", "")
    model = data.get("model", "")
    prompt = data.get("prompt", "")
    turn_id = data.get("turn_id")

    db = None
    try:
        config = LCMConfig()
        db = LCMDatabase(config)
        remote, branch = git_info(cwd)
        db.ensure_session(session_id, cwd, remote, branch, model)
        Capturer(db, config).capture_user(session_id, prompt, turn_id)
    except Exception:
        # Capture must never block or break the user's turn.
        return
    finally:
        if db is not None:
            db.close()

    # No stdout output — we don't inject or block on prompt submit.
    sys.exit(0)


if __name__ == "__main__":
    main()
