"""HP ABC v2 candidateをProductionへ書かず、営業用effective rankを導出する。"""
import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from src.master.data_paths import hp_batch_sidecar_path


HP_BATCH_ENV_VAR = "HP_RESEARCH_BATCH_DB_PATH"
DEFAULT_HP_BATCH_PATH = Path.home() / "CrestixData" / "clinic-lead" / "hp_abc_batch_sidecar.sqlite3"


def effective_hp_rank(machine_rank, old_hp_rank_db):
    """正式ルール。machine_rankの原値は変更せずA/B/C/Dへ正規化する。"""
    machine = str(machine_rank or "UNKNOWN").strip().upper()
    old = str(old_hp_rank_db or "").strip().upper()
    if machine in {"A", "B", "C", "D"}:
        return machine
    if machine == "UNKNOWN" and old in {"A", "B"}:
        return old
    return "D"


def effective_rank_reason(machine_rank, old_hp_rank_db):
    machine = str(machine_rank or "UNKNOWN").strip().upper()
    old = str(old_hp_rank_db or "").strip().upper()
    if machine in {"A", "B", "C", "D"}:
        return f"v2-precision-first判定: {machine}"
    if machine == "UNKNOWN" and old in {"A", "B"}:
        return f"旧{old}踏襲"
    if machine == "NO_HP":
        return "HPなしのためD"
    return "UNKNOWNをDへ正規化"


def hp_batch_path():
    return hp_batch_sidecar_path()


@lru_cache(maxsize=8)
def _load_machine_ranks_cached(path_string, mtime_ns, size):
    path = Path(path_string)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return {
            int(cid): rank
            for cid, rank in conn.execute(
                "SELECT clinic_id,hp_abc_candidate FROM hp_research_batch_results "
                "WHERE fetch_status='OK' AND hp_abc_candidate<>''"
            )
        }


def load_machine_ranks(path=None):
    """成功したv2結果だけをREAD ONLYで読む。失敗・空欄はUNKNOWNとして扱う。"""
    path = Path(path or hp_batch_path())
    if not path.exists():
        return {}
    try:
        stat = path.stat()
        return _load_machine_ranks_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except sqlite3.Error:
        return {}
