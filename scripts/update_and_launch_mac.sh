#!/bin/bash
# macOS: safely fast-forward this Runtime clone to the latest main, then start
# Clinic Lead System. This is the Mac counterpart of
# scripts/update_and_launch_windows.ps1 and must stay behaviorally equivalent.
#
# Safety rules (do not relax these):
#   - Never runs with a dirty working tree (no changes are made; exits with an error instead).
#   - Only ever switches to and fast-forwards the main branch. No destructive or
#     history-rewriting git operation (hard reset, rebase, merge commit, force push, stash).
#   - Never prints credential values. Credentials live only in the git-ignored
#     .supabase-runtime.env.local / config/email_research.env.local files, which this
#     script never reads.
set -euo pipefail

error_exit() {
    echo "[ERROR] $1" >&2
    exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v git >/dev/null 2>&1; then
    error_exit "git が見つかりません。Xcode Command Line Tools (xcode-select --install) または Git をインストールしてください。"
fi

if [ ! -d "$REPO_ROOT/.git" ]; then
    error_exit "gitリポジトリではありません: $REPO_ROOT"
fi

DIRTY_STATUS="$(git status --porcelain)"
if [ -n "$DIRTY_STATUS" ]; then
    echo "$DIRTY_STATUS"
    error_exit "未コミットの変更があります。何も変更していません。コミットまたは退避してから再実行してください。"
fi

git switch main
git fetch origin main
git pull --ff-only origin main

exec "$SCRIPT_DIR/launch_v2_mac.sh" "$@"
