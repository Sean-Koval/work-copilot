#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python bootstrap.py
if [ "$#" -eq 0 ]; then
  set -- demo
fi
exec .venv/bin/python -m work_copilot "$@"
