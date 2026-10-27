#!/bin/bash
# Mac用：Supabase専用Readerでの安全な起動。
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
MODE="normal"
for ARG in "$@"; do
    case "$ARG" in
        --comdesk-preview) MODE="preview" ;;
    esac
done
ARGS=()
for ARG in "$@"; do
    [ "$ARG" = "--comdesk-preview" ] || ARGS+=("$ARG")
done
if [ "$HAS_PORT" = true ]; then
    exec "$PYTHON" "$REPO_ROOT/scripts/launch_v2_mac_reader.py" --mode "$MODE" "${ARGS[@]}"
fi
DEFAULT_PORT=8501
[ "$MODE" = "preview" ] && DEFAULT_PORT=8502
exec "$PYTHON" "$REPO_ROOT/scripts/launch_v2_mac_reader.py" --mode "$MODE" --port "$DEFAULT_PORT" --strict-port "${ARGS[@]}"
