#!/bin/sh
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$repo_root/cairn/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -x "$repo_root/cairn/.venv/bin/python" ]; then
  exec "$repo_root/cairn/.venv/bin/python" -m cairn.controller.local "$@"
fi
exec uv run --project "$repo_root/cairn" python -m cairn.controller.local "$@"
