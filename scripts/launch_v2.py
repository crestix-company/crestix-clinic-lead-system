"""Clinic Lead launcher.

Mac keeps the historical behavior of choosing the first free local port.
Windows additionally acquires a localhost-only single-instance guard before
starting Streamlit so multiple app_v2.py processes cannot be launched from the
one-click launcher at the same time.
"""
from pathlib import Path
import argparse
import socket
import subprocess
import sys

WINDOWS_INSTANCE_GUARD_PORT = 48501


def choose_port(preferred=8501):
    for port in range(preferred, preferred + 20):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("利用できるポートがありません。古いアプリのターミナルを終了してください。")


def acquire_windows_instance_guard(port=WINDOWS_INSTANCE_GUARD_PORT):
    """Keep one localhost socket open for the lifetime of the Windows app.

    This does not kill any process.  If another Clinic Lead launcher already
    owns the guard port, fail closed and ask the operator to close the existing
    instance first.
    """
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        guard.bind(("127.0.0.1", port))
        guard.listen(1)
        return guard
    except OSError as exc:
        guard.close()
        raise RuntimeError(
            "Clinic Leadは既に起動中の可能性があります。"
            "既存のClinic Lead画面/黒い起動ウィンドウを終了してから再実行してください。"
            f" (single-instance guard port={port}, error={exc})"
        ) from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    guard = None
    if sys.platform == "win32":
        guard = acquire_windows_instance_guard()

    try:
        port = choose_port(args.port)
        print(f"クリニック営業マスター： http://127.0.0.1:{port}/", flush=True)
        command = [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(root / "app_v2.py"),
            "--server.address",
            "127.0.0.1",
            "--server.port",
            str(port),
            "--browser.serverAddress",
            "127.0.0.1",
            "--browser.serverPort",
            str(port),
            "--browser.gatherUsageStats",
            "false",
            "--global.developmentMode",
            "false",
        ]
        if args.headless:
            command += ["--server.headless", "true"]
        return subprocess.call(command, cwd=root)
    finally:
        if guard is not None:
            guard.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(1)
