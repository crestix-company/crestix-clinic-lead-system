"""RESET jobに残ったPENDING itemをCANCELLEDへ移す（既定はdry-run）。"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from src.master.jobs import repair_reset_job_items
from src.master.store import ClinicStore


class ReadOnlyStore:
    """dry-run時にschema変更も含めてSQLiteへの書き込みを禁止する。"""
    def __init__(self,path):
        self.path = Path(path).resolve()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(f"file:{self.path}?mode=ro",uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.rollback()
            connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="対象SQLiteファイル")
    parser.add_argument("--job-id", action="append", required=True,
                        help="対象RESET job ID（複数回指定可）")
    parser.add_argument("--execute", action="store_true",
                        help="実際に更新する。省略時はdry-run")
    args = parser.parse_args(argv)
    store = ClinicStore(args.db) if args.execute else ReadOnlyStore(args.db)
    result = repair_reset_job_items(store,args.job_id,dry_run=not args.execute)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return result


if __name__ == "__main__":
    main()
