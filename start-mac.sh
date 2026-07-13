#!/bin/zsh
set -eu
cd "$(dirname "$0")"
exec python3.11 -m uvicorn app.main:app --host 127.0.0.1 --port 8765
