"""Start the normal Mac UI with separate, verified Supabase Reader and Writer URLs."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.launch_v2_mac_reader import (
    NORMAL_HP_REQUIRED_TABLES, NORMAL_READER_FILE, load_reader_dsn, verify_reader,
)

WRITER_FILE = Path.home() / ".config/clinic-lead/runtime-writer.env"
PROVIDER_FILE = Path.home() / ".config/clinic-lead/hp-provider.env"


def load_tavily_key(path=None):
    path = path or PROVIDER_FILE
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("HP検索Provider設定がありません。外部設定手順を確認してください。")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError("HP検索Provider設定の権限を600にしてください。")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = shlex.split(line)
        if parts and parts[0] == "export":
            parts = parts[1:]
        if len(parts) != 1 or "=" not in parts[0]:
            raise RuntimeError("HP検索Provider設定の形式が不正です。")
        key, value = parts[0].split("=", 1)
        if key != "TAVILY_API_KEY" or key in values:
            raise RuntimeError("HP検索Provider設定にはTAVILY_API_KEYだけを指定してください。")
        values[key] = value
    if not values.get("TAVILY_API_KEY", "").strip():
        raise RuntimeError("TAVILY_API_KEYが未設定です。")
    return values["TAVILY_API_KEY"]


def configure_normal_runtime(root, *, reader_file=None):
    from scripts.launch_v2 import load_runtime_env

    os.environ["CLINIC_RUNTIME_ENV_FILE"] = str(WRITER_FILE)
    os.environ["CLINIC_NORMAL_MAC_RUNTIME"] = "1"
    os.environ["CLINIC_READ_ONLY_UI"] = "0"
    os.environ["CLINIC_COMDESK_PREVIEW_ONLY"] = "0"
    load_runtime_env(root)
    writer = (os.environ.get("SUPABASE_RUNTIME_DB_URL") or "").strip()
    if not writer:
        raise RuntimeError("HP Writer設定がありません。既存の非公開runtime設定を確認してください。")
    reader = load_reader_dsn(reader_file or NORMAL_READER_FILE)
    tavily_key = load_tavily_key()
    if reader == writer:
        raise RuntimeError("ReaderとWriterの接続設定が同一です。別々の最小権限接続を設定してください。")
    role, count = verify_reader(
        reader, required_tables=NORMAL_HP_REQUIRED_TABLES,
        count_relation="public.clinic_cloudrun_readonly",
    )
    os.environ.update({
        "SUPABASE_READONLY_DB_URL": reader,
        "CLINIC_NORMAL_MAC_RUNTIME": "1",
        "CLINIC_READ_ONLY_UI": "0",
        "CLINIC_COMDESK_PREVIEW_ONLY": "0",
        "CLINIC_DATA_BACKEND": "supabase",
        "CLINIC_WRITE_BACKEND": "supabase",
        "EMAIL_AUTO_RUN_ENABLED": "false",
        "OUTREACH_GMAIL_DRAFTS_ENABLED": "false",
        "TAVILY_API_KEY": tavily_key,
    })
    return role, count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--strict-port", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    role, count = configure_normal_runtime(root)
    print(f"Supabase Reader確認OK: role={role}, 医院={count}件。HP JobはUIで明示操作するまで開始しません。", flush=True)
    command = [sys.executable, str(root / "scripts/launch_v2.py"), "--port", str(args.port)]
    if args.strict_port:
        command.append("--strict-port")
    if args.headless:
        command.append("--headless")
    return subprocess.call(command, cwd=root, env=os.environ.copy())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as exc:
        print(f"安全な起動を中止しました: {exc}", file=sys.stderr)
        sys.exit(1)
