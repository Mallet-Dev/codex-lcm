"""PostToolUse hook — capture tool calls and their results (the coding meat)."""

import sys
import os

from ..config import LCMConfig
from ..db import LCMDatabase
from ..capture import Capturer
from ._common import read_input, git_info


def main() -> None:
    data = read_input()
    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    # Opt-out for tool capture via CODEX_LCM_CAPTURE_TOOLS=0
    if os.environ.get("CODEX_LCM_CAPTURE_TOOLS", "1") == "0":
        sys.exit(0)

    cwd = data.get("cwd", "")
    model = data.get("model", "")
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_response = data.get("tool_response", "")
    tool_use_id = data.get("tool_use_id")

    db = None
    try:
        config = LCMConfig()
        db = LCMDatabase(config)
        remote, branch = git_info(cwd)
        db.ensure_session(session_id, cwd, remote, branch, model)
        Capturer(db, config).capture_tool(
            session_id, tool_name, tool_input, tool_response, tool_use_id
        )
    except Exception:
        return
    finally:
        if db is not None:
            db.close()

    sys.exit(0)


if __name__ == "__main__":
    main()
