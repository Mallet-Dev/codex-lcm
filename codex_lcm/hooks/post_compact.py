"""PostCompact hook — mirror Codex's compaction by summarizing our own backlog."""

import sys

from ._common import read_input, spawn_compaction


def main() -> None:
    data = read_input()
    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    # After Codex compacts (manual or auto), kick our background worker to
    # summarize the same backlog. Async + flock-guarded, so it's cheap/safe.
    spawn_compaction(session_id)

    sys.exit(0)


if __name__ == "__main__":
    main()
