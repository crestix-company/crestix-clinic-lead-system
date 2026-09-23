"""既に旧版が8501で動いていても、空いているローカルポートで起動する。"""
from pathlib import Path
import argparse
import socket
import subprocess
import sys


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
