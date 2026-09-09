#!/usr/bin/env bash
# Thin wrapper around the change-request intake CLI, so the command is
# `./scripts/request.sh import` rather than
# `python -m scripts.change_requests.cli import`.
#
# Prefers the repo virtualenv: extract_msg is a dev-only dependency and is
# normally installed there, not system-wide.
#
#   ./scripts/request.sh setup
#   ./scripts/request.sh import
#   ./scripts/request.sh validate CR-2026-001
#   ./scripts/request.sh start CR-2026-001 --repo fa-web-api
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [ -x "$repo_root/.venv/bin/python" ]; then
  python_bin="$repo_root/.venv/bin/python"
elif [ -x "$repo_root/.venv/Scripts/python.exe" ]; then
  python_bin="$repo_root/.venv/Scripts/python.exe"
else
  python_bin="$(command -v python3 || command -v python)"
fi

exec "$python_bin" -m scripts.change_requests.cli "$@"
