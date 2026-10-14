#!/bin/bash
# macOS first-time setup for the one-click launcher.
#
# Creates an isolated Runtime clone at $HOME/ClinicLeadRuntime (never the
# development working copy this script is run from), sets up its virtual
# environment, checks for credential files (copying them from the source
# repo only if present there; never prompting for or printing values), and
# creates the Desktop launcher "Clinic Lead System.command".
#
# This script never touches the source repository's git state: it only
# reads its remote URL and, if present, copies its credential files. It
# never runs git fetch/pull/switch/reset against the source repo.
#
# Run once. Re-running is safe: it reuses the existing Runtime clone and
# venv instead of recreating them.
#
# Usage:
#   scripts/setup_mac_one_click.sh
#
# Override (mainly for tests):
#   CLINIC_RUNTIME_DIR - runtime clone directory (default: $HOME/ClinicLeadRuntime)
set -euo pipefail

error_exit() {
    echo "[ERROR] $1" >&2
    exit 1
}

blocked_exit() {
    echo "[BLOCKED] $1" >&2
    exit 2
}

# Works for a normal clone (.git is a directory) and a git worktree (.git is a
# file pointing at the main repo's gitdir) alike; `rev-parse --is-inside-work-tree`
# is the correct test for "is this a git working tree", not a `-d .git` check.
is_git_work_tree() {
    [ -e "$1" ] && git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

# Reads KEY=value out of an env file without ever printing the value.
# Exits BLOCKED if the key is missing or its value is empty.
require_nonempty_key() {
    local file="$1" key="$2"
    local line value
    line="$(grep -E "^${key}=" "$file" || true)"
    if [ -z "$line" ]; then
        blocked_exit "Email Research credentialに ${key} がありません。値は表示しません。"
    fi
    value="${line#*=}"
    if [ -z "$value" ]; then
        blocked_exit "Email Research credentialの ${key} が空です。値は表示しません。"
    fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="${CLINIC_RUNTIME_DIR:-$HOME/ClinicLeadRuntime}"

echo "== 前提確認 =="
command -v git >/dev/null 2>&1 || error_exit "git が見つかりません。"
command -v python3 >/dev/null 2>&1 || error_exit "python3 が見つかりません。Python 3.12以上をインストールしてください。"
if ! is_git_work_tree "$SOURCE_REPO_ROOT"; then
    error_exit "このスクリプトはgit working tree (通常clone、またはgit worktree) の scripts/ から実行してください: $SOURCE_REPO_ROOT"
fi

echo "== Runtime clone ($RUNTIME_DIR) =="
if is_git_work_tree "$RUNTIME_DIR"; then
    echo "既存のRuntime cloneを検出しました。originを検証します。"
else
    if [ -e "$RUNTIME_DIR" ]; then
        error_exit "$RUNTIME_DIR はgitリポジトリではない既存のパスです。削除するか別の場所を指定してください。"
    fi
    ORIGIN_URL="$(git -C "$SOURCE_REPO_ROOT" remote get-url origin)"
    git clone "$ORIGIN_URL" "$RUNTIME_DIR"
fi

echo "== Runtime origin検証 =="
SOURCE_ORIGIN_URL="$(git -C "$SOURCE_REPO_ROOT" remote get-url origin)"
RUNTIME_ORIGIN_URL="$(git -C "$RUNTIME_DIR" remote get-url origin 2>/dev/null || true)"
if [ -z "$RUNTIME_ORIGIN_URL" ] || [ "$SOURCE_ORIGIN_URL" != "$RUNTIME_ORIGIN_URL" ]; then
    blocked_exit "Runtime clone ($RUNTIME_DIR) のoriginがsource repoのoriginと一致しません。別のrepoへcredentialを誤配置しないため、credentialはコピーせず終了します。Runtimeを削除して再作成するか、正しいoriginのcloneに差し替えてください。"
fi
echo "Runtime origin: OK (source repoと一致)"

echo "== Supabase credential確認（値は表示しません） =="
CRED_FILE="$RUNTIME_DIR/.supabase-runtime.env.local"
EMAIL_FILE="$RUNTIME_DIR/config/email_research.env.local"

if [ ! -f "$CRED_FILE" ]; then
    SOURCE_CRED="$SOURCE_REPO_ROOT/.supabase-runtime.env.local"
    if [ -f "$SOURCE_CRED" ]; then
        mkdir -p "$(dirname "$CRED_FILE")"
        cp "$SOURCE_CRED" "$CRED_FILE"
    fi
fi
if [ ! -f "$CRED_FILE" ]; then
    blocked_exit "Supabase credential ($CRED_FILE) が見つかりません。値は要求・表示しません。安全な方法で配置してから再実行してください。"
fi
chmod 600 "$CRED_FILE"
echo "Supabase credential: OK"

echo "== Email Research credential確認（値は表示しません） =="
EMAIL_SOURCE="$SOURCE_REPO_ROOT/config/email_research.env.local"
if [ ! -f "$EMAIL_FILE" ] && [ -f "$EMAIL_SOURCE" ]; then
    mkdir -p "$(dirname "$EMAIL_FILE")"
    cp "$EMAIL_SOURCE" "$EMAIL_FILE"
fi
if [ ! -f "$EMAIL_FILE" ]; then
    blocked_exit "Email Research credential ($EMAIL_FILE) が見つかりません。値は要求・表示しません。安全な方法で配置してから再実行してください。"
fi
chmod 600 "$EMAIL_FILE"
require_nonempty_key "$EMAIL_FILE" "SERPAPI_API_KEY"
require_nonempty_key "$EMAIL_FILE" "EMAIL_AUTO_RUN_ENABLED"
echo "Email Research credential: OK"

echo "MAC PRIVATE CONFIG: PASS"

echo "== .gitignore確認 =="
if ! git -C "$RUNTIME_DIR" check-ignore -q ".supabase-runtime.env.local"; then
    error_exit ".supabase-runtime.env.local がgit ignore対象ではありません。"
fi
if [ -f "$EMAIL_FILE" ] && ! git -C "$RUNTIME_DIR" check-ignore -q "config/email_research.env.local"; then
    error_exit "config/email_research.env.local がgit ignore対象ではありません。"
fi

echo "== Python仮想環境 =="
PYTHON_VERSION_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 12) else 0)')"
if [ "$PYTHON_VERSION_OK" != "1" ]; then
    error_exit "Python 3.12以上が必要です。"
fi
if [ ! -x "$RUNTIME_DIR/.venv/bin/python3" ]; then
    python3 -m venv "$RUNTIME_DIR/.venv"
else
    echo ".venv は既に存在します。再利用します。"
fi
RUNTIME_PYTHON="$RUNTIME_DIR/.venv/bin/python3"

echo "== 依存関係インストール =="
LOCK_FILE="$RUNTIME_DIR/requirements-lock.txt"
if [ ! -f "$LOCK_FILE" ]; then
    error_exit "requirements-lock.txt が見つかりません: $LOCK_FILE"
fi
"$RUNTIME_PYTHON" -m pip install -r "$LOCK_FILE"

"$RUNTIME_PYTHON" -c "import streamlit,pandas,openpyxl,yaml,requests,bs4,rapidfuzz,filelock,tzdata,psycopg" \
    || error_exit "必要なPython依存関係が不足しています。"

echo "== Desktop launcher =="
DESKTOP_CREATOR="$RUNTIME_DIR/scripts/create_mac_desktop_launcher.sh"
if [ ! -f "$DESKTOP_CREATOR" ]; then
    error_exit "Runtime clone内に scripts/create_mac_desktop_launcher.sh が見つかりません。"
fi
chmod +x "$DESKTOP_CREATOR"
"$DESKTOP_CREATOR" "$RUNTIME_DIR"

echo ""
echo "========================================"
echo " Mac first-time setup complete"
echo "========================================"
echo "これ以降は、Desktopの \"Clinic Lead System\" をダブルクリックしてください。"
echo "起動ごとに: git switch main -> git fetch -> git pull --ff-only -> 最新版を起動 します。"
