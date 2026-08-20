#!/usr/bin/env bash
# One-command installer for codex-lcm.
#
# Creates an isolated venv at $CODEX_LCM_VENV (default ~/.codex-lcm/venv),
# installs the package, wires hooks + MCP server into Codex, and runs doctor.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${CODEX_LCM_VENV:-$HOME/.codex-lcm/venv}"

echo "==> Creating venv at $VENV"
python3 -m venv "$VENV"

echo "==> Installing codex-lcm"
"$VENV/bin/python" -m pip install "$REPO_DIR"

echo "==> Wiring hooks + MCP server into Codex"
"$VENV/bin/codex-lcm" install

echo "==> Verifying"
"$VENV/bin/codex-lcm" doctor

echo
echo "Done. Restart Codex and use /hooks to review & trust the new hooks."
