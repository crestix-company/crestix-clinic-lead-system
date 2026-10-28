"""Create the Step6 Reader env file without echoing or logging its password."""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import shlex
from urllib.parse import quote, urlsplit, urlunsplit

from scripts.launch_v2 import _parse_env_file
from scripts.launch_v2_mac_reader import verify_reader
from src.repository.email_database import _database_identity
from src.repository.mac_feature_access import (
    COMDESK_READER_COLUMNS,
    COMDESK_READER_FILE,
    READER_KEY,
    READER_ROLE,
)


RUNTIME_FILE = Path.home() / ".config" / "clinic-lead" / "runtime-writer.env"


def _reader_dsn(runtime_url: str, password: str) -> str:
    runtime = urlsplit(runtime_url)
    identity = _database_identity(runtime_url)
    if identity is None or identity[0] != "supabase-project":
        raise ValueError("Runtime接続先からSupabaseプロジェクトを照合できません。")
    if not runtime.hostname or not runtime.path:
        raise ValueError("Runtime接続先の形式を確認できません。")
    project_ref = identity[1]
    username = READER_ROLE
    if runtime.hostname.endswith(".pooler.supabase.com"):
        username = f"{READER_ROLE}.{project_ref}"
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{runtime.hostname}"
    if runtime.port:
        netloc += f":{runtime.port}"
    return urlunsplit((runtime.scheme, netloc, runtime.path, runtime.query, runtime.fragment))


def configure() -> None:
    target = COMDESK_READER_FILE
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError("Step6 Reader設定が既に存在します。上書きしません。")
    if os.name != "nt" and target.parent.stat().st_mode & 0o077:
        raise PermissionError("設定ディレクトリはmode 700にしてください。")

    runtime = _parse_env_file(RUNTIME_FILE, require_private=True).get("SUPABASE_RUNTIME_DB_URL", "")
    if not runtime:
        raise ValueError("既存Mac Runtimeから接続先を確認できません。")
    password = getpass.getpass("新しい clinic_comdesk_export_reader パスワード: ")
    confirmation = getpass.getpass("同じパスワードを再入力: ")
    if len(password) < 32 or password != confirmation:
        raise ValueError("32文字以上のパスワードを同じ内容で入力してください。")
    dsn = _reader_dsn(runtime, password)
    identity = _database_identity(dsn)
    if identity != _database_identity(runtime):
        raise ValueError("ReaderとMac RuntimeのSupabaseプロジェクトが一致しません。")

    role, count = verify_reader(
        dsn,
        required_tables=tuple(COMDESK_READER_COLUMNS),
        required_columns=COMDESK_READER_COLUMNS,
        count_relation="public.clinics",
    )
    if role != READER_ROLE:
        raise PermissionError("接続先のDBロールがStep6 Readerと一致しません。")

    line = f"{READER_KEY}={shlex.quote(dsn)}\n".encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(target, flags, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(line)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    print(f"Step6 Reader確認OK: role={role}, 医院={count}件。設定はmode 600で保存しました。")


if __name__ == "__main__":
    try:
        configure()
    except Exception:
        # Driver exceptions can contain connection details. Never print them here.
        print("Readerの検査または設定保存に失敗しました。秘密値を含む詳細は表示しません。",
              file=__import__("sys").stderr)
        raise SystemExit(1)
