#!/bin/bash
# Mac用のSupabase-only起動スクリプト。
# Production routingは `config/production_runtime.env`、credentialはgit-ignore済み
# `.supabase-runtime.env.local` に保存する。secret値は表示しない。
#
# SQLite DB pathやsidecarは設定・検査・openしない。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
elif [ -x "$REPO_ROOT/.venv_mac/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv_mac/bin/python3"
else
    echo "エラー: .venv または .venv_mac が見つかりません。先にセットアップしてください。" >&2
    exit 1
fi

exec "$PYTHON" "$REPO_ROOT/scripts/launch_v2.py" "$@"
