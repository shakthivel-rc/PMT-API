#!/usr/bin/env bash
#
# PMT — One-command dynamic installer
# Works across Linux, macOS, and Windows (Git Bash/WSL).
#
# Usage:
#   ./setup.sh              # Auto-detect best mode (Docker, falling back to Native)
#   ./setup.sh --docker     # Docker containerized mode
#   ./setup.sh --native     # Native mode (Python venv + npm + SQLite/MySQL)
#   ./setup.sh --check      # Doctor check / diagnose environment
#   ./setup.sh -y           # Non-interactive / assume yes
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/bootstrap.sh" "$@"
