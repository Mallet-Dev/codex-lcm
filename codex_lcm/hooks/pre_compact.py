"""PreCompact hook — observe Codex's compaction trigger without blocking it."""

import sys

from ._common import read_input


def main() -> None:
    # We do not block Codex's own compaction. Our PostCompact hook handles the
    # mirror work after Codex finishes. Reading stdin keeps the protocol clean.
    _ = read_input()

    # Empty stdout → "continue" (let Codex compact).
    sys.exit(0)


if __name__ == "__main__":
    main()
