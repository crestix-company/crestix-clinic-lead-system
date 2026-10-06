#!/usr/bin/env python3
"""HP再調査バッチ（Stage4 Canary）CLI。

Production DB / 既存Treatment sidecarはREAD ONLYのみ（hp_rank等は一切更新しない）。
結果は新規sidecar（--sidecar-path、デフォルトartifacts/hp_research_batch/hp_abc_batch_sidecar.sqlite3）
へのみ書き込む。clinic_id単位でidempotent・resume可能。

使い方:
  python scripts/hp_research_batch_run.py --limit 100 --batch-size 20
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock, Timeout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.master.hp_research_batch import run_batch, DEFAULT_SIDECAR_PATH
from src.master.research_sidecar import research_sidecar_path


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--production-db", default=None, help="Production clinics.sqlite3のパス（省略時はCLINIC_DB_PATH環境変数、無ければ既定の~/CrestixData）")
    p.add_argument("--limit", type=int, required=True, help="このバッチ実行で処理する最大件数")
    p.add_argument("--batch-size", type=int, default=20, help="進捗表示・チャンク処理の単位件数")
    p.add_argument("--max-workers", type=int, default=10, help="同時fetch数")
    p.add_argument("--sidecar-path", default=str(DEFAULT_SIDECAR_PATH), help="結果の保存先（新規sidecar、Production DBではない）")
    p.add_argument("--treatment-sidecar-path", default=None, help="既存Treatment sidecarのパス（READ ONLY、候補選定の参考のみ。省略時は既定パス）")
    p.add_argument("--lock-path", default=None, help="多重起動防止lock（省略時はsidecar_path+.process.lock）")
    p.add_argument("--pid-path", default=str(ROOT / "artifacts/hp_research_batch/background_run.pid"))
    p.add_argument("--progress-path", default=str(ROOT / "artifacts/hp_research_batch/progress.json"))
    return p.parse_args(argv)


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    args = parse_args(argv)
    production_db = args.production_db or os.getenv("CLINIC_DB_PATH")
    if not production_db:
        print("ERROR: --production-db または環境変数 CLINIC_DB_PATH を指定してください。", file=sys.stderr)
        return 1
    treatment_sidecar = args.treatment_sidecar_path or str(research_sidecar_path())

    pid_path = Path(args.pid_path)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(args.lock_path or (str(args.sidecar_path) + ".process.lock"))
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = {"status": "STARTING", "pid": os.getpid(), "started_at": started_at,
             "updated_at": started_at, "done": 0, "total": 0,
             "sidecar_path": str(Path(args.sidecar_path).resolve())}

    def progress(done, total):
        print(f"  ... {done}/{total} done", flush=True)
        state.update(status="RUNNING", done=done, total=total,
                     updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        _write_json(args.progress_path, state)

    try:
        with FileLock(str(lock_path), timeout=0):
            pid_path.write_text(str(os.getpid()) + "\n", encoding="ascii")
            _write_json(args.progress_path, state)
            report = run_batch(
                production_db_path=production_db,
                limit=args.limit,
                batch_size=args.batch_size,
                sidecar_path=args.sidecar_path,
                treatment_sidecar_path=treatment_sidecar,
                max_workers=args.max_workers,
                progress_cb=progress,
            )
            state.update(status="COMPLETED", done=report.attempted,
                         updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         succeeded=report.succeeded, failed=report.failed, retried=report.retried)
            _write_json(args.progress_path, state)
    except Timeout:
        print(f"ERROR: 既に同じsidecarのバッチが実行中です: {lock_path}", file=sys.stderr)
        return 2
    except BaseException as exc:
        state.update(status="FAILED", updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     error=f"{type(exc).__name__}: {exc}")
        _write_json(args.progress_path, state)
        raise
    print(f"TARGET={report.target} ATTEMPTED={report.attempted} OK={report.succeeded} "
          f"FAILED={report.failed} RETRIED={report.retried}")
    if report.elapsed_seconds:
        avg = sum(report.elapsed_seconds) / len(report.elapsed_seconds)
        print(f"AVG_ELAPSED_SECONDS={avg:.2f}")
    print(f"sidecar={args.sidecar_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
