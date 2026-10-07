#!/bin/bash
# Mac用の起動スクリプト。CLINIC_DB_PATHをrepo外のProduction DBへ向けてから起動する。
# Supabase WRITE切替時はgit-ignore済み `.supabase-runtime.env.local` にruntime URLと
# CLINIC_WRITE_BACKEND=supabaseを保存する。secret値は表示しない。
#
# CLINIC_DB_PATHが既に設定されていればそれを優先する。未設定時は既定の配置場所
# ($HOME/CrestixData/clinic-lead/clinics.sqlite3) を使う。このパスはリポジトリに
# ハードコードされたユーザー固有パスではなく、$HOME経由の規約上のデフォルトです。
#
# DBが存在しない・clinicsテーブルがない場合はここで明確に停止し、
# 空DBの自動生成やrepo内の古いDBへのfallbackは一切しない。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_DB_PATH="$HOME/CrestixData/clinic-lead/clinics.sqlite3"
CLINIC_DB_PATH="${CLINIC_DB_PATH:-$DEFAULT_DB_PATH}"
export CLINIC_DB_PATH

if [ ! -f "$CLINIC_DB_PATH" ]; then
    echo "エラー: Production DBが見つかりません: $CLINIC_DB_PATH" >&2
    echo "CLINIC_DB_PATHを正しいProduction DBのパスに設定してから実行してください。" >&2
    exit 1
fi

if ! sqlite3 "file://$CLINIC_DB_PATH?mode=ro" "SELECT 1 FROM clinics LIMIT 1;" >/dev/null 2>&1; then
    echo "エラー: $CLINIC_DB_PATH にclinicsテーブルが見つからないか、SQLiteとして開けません。" >&2
    exit 1
fi

echo "Production DB: $CLINIC_DB_PATH"

if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
elif [ -x "$REPO_ROOT/.venv_mac/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv_mac/bin/python3"
else
    echo "エラー: .venv または .venv_mac が見つかりません。先にセットアップしてください。" >&2
    exit 1
fi

exec "$PYTHON" "$REPO_ROOT/scripts/launch_v2.py" "$@"
