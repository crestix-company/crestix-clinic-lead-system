#!/bin/bash
# 通常版：READ ONLY UIとHP Writerを別接続にして起動する。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
elif [ -x "$HOME/ClinicLeadRuntime/.venv/bin/python3" ]; then
    PYTHON="$HOME/ClinicLeadRuntime/.venv/bin/python3"
else
    echo "エラー: Python環境がありません。最新版に.venvを作成してください。" >&2
    exit 1
fi

exec "$PYTHON" "$REPO_ROOT/scripts/launch_v2_mac_normal.py" --port 8501 --strict-port "$@"
