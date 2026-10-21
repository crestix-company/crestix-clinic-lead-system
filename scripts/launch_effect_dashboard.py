"""効果測定ダッシュボードの単独起動スクリプト（既存app_v2.pyには組み込まない）。

空いているローカルポートで起動する。既存の「メール送付文章」や他のStreamlitプロセスを
停止・再起動しない。
"""
from pathlib import Path
import argparse
import socket
import subprocess
import sys


def choose_port(preferred=8601):
    for port in range(preferred, preferred + 20):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("利用できるポートがありません。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--port", type=int, default=8601)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    port = choose_port(args.port)
    print(f"効果測定ダッシュボード: http://127.0.0.1:{port}/", flush=True)
    command = [
        sys.executable, "-m", "streamlit", "run", str(root / "src" / "outreach_effect_dashboard_ui.py"),
        "--server.address", "127.0.0.1", "--server.port", str(port),
        "--browser.serverAddress", "127.0.0.1", "--browser.serverPort", str(port),
        "--browser.gatherUsageStats", "false", "--global.developmentMode", "false",
    ]
    if args.headless:
        command += ["--server.headless", "true"]
    return subprocess.call(command, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
