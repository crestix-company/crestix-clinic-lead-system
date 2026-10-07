"""既に旧版が8501で動いていても、空いているローカルポートで起動する。"""
from pathlib import Path
import argparse
import os
import shlex
import socket
import subprocess
import sys


RUNTIME_ENV_FILE = ".supabase-runtime.env.local"
RUNTIME_ENV_KEYS = {"SUPABASE_RUNTIME_DB_URL", "CLINIC_WRITE_BACKEND", "CLINIC_DATA_BACKEND"}


def load_runtime_env(root):
    """Load the ignored runtime contract without printing secret values."""
    path = root / RUNTIME_ENV_FILE
    if not path.exists():
        return False
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError(f"{RUNTIME_ENV_FILE} の権限を600にしてください。")
    parsed = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = shlex.split(line, posix=True)
        if parts and parts[0] == "export":
            parts = parts[1:]
        if len(parts) != 1 or "=" not in parts[0]:
            raise RuntimeError(f"{RUNTIME_ENV_FILE}:{number} の形式を確認してください。")
        key, value = parts[0].split("=", 1)
        if key not in RUNTIME_ENV_KEYS:
            raise RuntimeError(f"{RUNTIME_ENV_FILE}:{number} に許可されていない設定があります。")
        parsed[key] = value
    selected_write = os.environ.get("CLINIC_WRITE_BACKEND", parsed.get("CLINIC_WRITE_BACKEND", "sqlite"))
    runtime_url = os.environ.get("SUPABASE_RUNTIME_DB_URL", parsed.get("SUPABASE_RUNTIME_DB_URL", ""))
    if selected_write.strip().lower() == "supabase" and not runtime_url:
        raise RuntimeError("Supabase WRITEにはSUPABASE_RUNTIME_DB_URLが必要です。admin URLへのfallbackは行いません。")
    for key, value in parsed.items():
        os.environ.setdefault(key, value)
    return True


def choose_port(preferred=8501):
    for port in range(preferred,preferred+20):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1",port))
                return port
            except OSError:
                continue
    raise RuntimeError("利用できるポートがありません。古いアプリのターミナルを終了してください。")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--headless",action="store_true")
    parser.add_argument("--port",type=int,default=8501)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    load_runtime_env(root)
    port=choose_port(args.port)
    print(f"クリニック営業マスター： http://127.0.0.1:{port}/",flush=True)
    command=[sys.executable,"-m","streamlit","run",str(root/"app_v2.py"),"--server.address","127.0.0.1",
             "--server.port",str(port),"--browser.serverAddress","127.0.0.1","--browser.serverPort",str(port),
             "--browser.gatherUsageStats","false","--global.developmentMode","false"]
    if args.headless:
        command += ["--server.headless","true"]
    return subprocess.call(command,cwd=root)


if __name__=="__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(1)
