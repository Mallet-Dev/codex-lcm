"""SessionStart hook for Codex CLI.

Restores context from the LCM DAG by returning it as additionalContext.
Uses the shared helpers so it stays consistent with the other hooks.
"""

import sys
import json

from ..config import LCMConfig
from ..db import LCMDatabase
from ..assembler import Assembler
from ._common import read_input, git_info


def main() -> None:
    data = read_input()
    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    cwd = data.get("cwd", "")
    model = data.get("model", "")
    source = data.get("source", "startup")  # startup | resume | clear | compact

    db = None
    try:
        config = LCMConfig()
        db = LCMDatabase(config)
        remote, branch = git_info(cwd)
        db.ensure_session(session_id, cwd, remote, branch, model)

        assembler = Assembler(db, config)
        restore = assembler.build_restore_context(
            session_id,
            is_compacted=(source == "compact"),
            token_limit=config.additional_context_limit,
        )

        if restore:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": restore,
                }
            }
            print(json.dumps(output))
        # else: no context to restore — print nothing
    except Exception as e:
        # Never break a session start; log to stderr (Codex ignores it).
        print(f"codex-lcm SessionStart error: {e}", file=sys.stderr)
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    main()
