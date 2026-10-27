#!/bin/bash
# Mac用：Supabase専用Readerでの安全なプレビュー起動。
# SQLite / runtime writer / 未確認の権限にはfallbackしない。
# launch_v2_mac_reader.py が権限を検証した後に launch_v2.py を起動する。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
elif [ -x "$REPO_ROOT/.venv_mac/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv_mac/bin/python3"
elif [ -x "$HOME/ClinicLeadRuntime/.venv/bin/python3" ]; then
    PYTHON="$HOME/ClinicLeadRuntime/.venv/bin/python3"
else
    echo "エラー: Python環境がありません。最新版に.venvを作成してください。" >&2
    exit 1
fi

HAS_PORT=false
for ARG in "$@"; do
    case "$ARG" in
        --port|--port=*) HAS_PORT=true ;;
    esac
done
if [ "$HAS_PORT" = true ]; then
    exec "$PYTHON" "$REPO_ROOT/scripts/launch_v2_mac_reader.py" "$@"
fi
exec "$PYTHON" "$REPO_ROOT/scripts/launch_v2_mac_reader.py" --port 8502 --strict-port "$@"
