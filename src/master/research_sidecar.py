"""Phase7 Treatment Research 共通Runtime DB（別worktree/別プロセスのResearch Workerが書き込む）。

契約の詳細は mhlw_dry_run/TREATMENT_RESEARCH_CONTRACT.md を参照。
Treatment Research自体のロジック（evidence engine, taxonomy）はここでは一切扱わない。

Research WorkerとFilter/UI/CSVは別worktreeで並行動作するため、worktree内の相対パスを
sidecarとして使うと互いのプロセスが別々のSQLiteファイルを見てしまい、増分結果が反映されない。
そのため共通の絶対パス（デフォルト ~/CrestixData/clinic-lead/treatment_research_final.sqlite3、
環境変数 TREATMENT_RESEARCH_DB_PATH で上書き可）を単一のSource of Truthとする。

役割分担:
  Research Worker: このファイルへWRITEする（このモジュールの管轄外）。
  Filter/UI/CSV（このモジュール）: READ ONLYでATTACHする（SQLite URI mode=roで書き込みを物理的に禁止）。

sidecarが存在しない間はfilterが安全に0件扱いになるよう、既存のMHLW sidecar
（src/master/store.py の MHLW_SIDECAR_PATH / mhlw_sidecar_available()）と同じ
可用性チェックのパターンに揃える。

2026-10-01: Research側がsidecarの契約を分離した（commit 90bb9fe）。
  - clinic_treatment_research_final（Treatment単位・既存）: CONFIRMED/REVIEW/NOT_CONFIRMEDのみ。
    HP治療カテゴリfilterは引き続きこのテーブルのCONFIRMED行だけを見る（変更なし）。
  - clinic_research_status（新設・医院単位SSOT）: DONE/FETCH_FAILEDの2値、行が無ければNOT_RESEARCHED。
    Research Status filter（医院単位の調査状態）はこのテーブルを見る。
  両者は独立した軸であり、一方の値から他方を推測してはならない
  （詳細: mhlw_dry_run/TREATMENT_RESEARCH_CONTRACT.md）。
"""
import os
import sqlite3
from pathlib import Path

RESEARCH_SIDECAR_ENV_VAR = "TREATMENT_RESEARCH_DB_PATH"
RESEARCH_SIDECAR_DEFAULT_PATH = Path.home() / "CrestixData" / "clinic-lead" / "treatment_research_final.sqlite3"
RESEARCH_SIDECAR_ATTACH_NAME = "researchdb"

# Treatment単位（HP治療カテゴリfilter用）。CONFIRMED/REVIEW/NOT_CONFIRMEDのみ。契約変更なし。
RESEARCH_SIDECAR_TABLE = "clinic_treatment_research_final"
RESEARCH_SIDECAR_QUALIFIED_TABLE = f"{RESEARCH_SIDECAR_ATTACH_NAME}.{RESEARCH_SIDECAR_TABLE}"

# 医院単位SSOT（Research Status filter用）。DONE/FETCH_FAILEDのみ。行が無ければNOT_RESEARCHED。
CLINIC_RESEARCH_STATUS_TABLE = "clinic_research_status"
CLINIC_RESEARCH_STATUS_QUALIFIED_TABLE = f"{RESEARCH_SIDECAR_ATTACH_NAME}.{CLINIC_RESEARCH_STATUS_TABLE}"

# NOT_RESEARCHEDはテーブル上の値ではなく「該当clinic_idの行が1件もない」ことで表現する。
RESEARCH_STATUS_VALUES = ("DONE", "FETCH_FAILED")
RESEARCH_STATUS_UI_OPTIONS = (*RESEARCH_STATUS_VALUES, "NOT_RESEARCHED")


class ResearchSidecarUnavailableError(RuntimeError):
    pass


def research_sidecar_path():
    """共通Runtime DBの絶対パス。TREATMENT_RESEARCH_DB_PATHで都度override可能（frozen定数にしない）。"""
    raw = os.getenv(RESEARCH_SIDECAR_ENV_VAR)
    return Path(raw).expanduser() if raw else RESEARCH_SIDECAR_DEFAULT_PATH


def research_sidecar_readonly_uri(path=None):
    return f"file:{path or research_sidecar_path()}?mode=ro"


def research_sidecar_available():
    """HP治療カテゴリfilter（clinic_treatment_research_final）が安全に利用できるかの可用性チェック。"""
    return _sidecar_table_exists(RESEARCH_SIDECAR_TABLE)


def clinic_research_status_available():
    """Research Status filter（医院単位SSOT clinic_research_status）が安全に利用できるかの可用性チェック。
    clinic_treatment_research_finalとは独立したテーブルのため、別途チェックする
    （HP治療カテゴリとResearch Statusは互いに影響しない独立軸: §5 参照）。
    """
    return _sidecar_table_exists(CLINIC_RESEARCH_STATUS_TABLE)


def _sidecar_table_exists(table_name):
    path = research_sidecar_path()
    if not path.exists():
        return False
    try:
        with sqlite3.connect(research_sidecar_readonly_uri(path), uri=True) as conn:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table_name,)
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def treatment_research_category_options():
    """sidecarに存在するtreatment_category_name（HP治療カテゴリfilterの選択肢）。"""
    if not research_sidecar_available():
        return []
    try:
        with sqlite3.connect(research_sidecar_readonly_uri(), uri=True) as conn:
            return [r[0] for r in conn.execute(
                f"SELECT DISTINCT treatment_category_name FROM {RESEARCH_SIDECAR_TABLE} "
                "WHERE treatment_category_name<>'' ORDER BY treatment_category_name")]
    except sqlite3.Error as exc:
        raise ResearchSidecarUnavailableError(
            "Treatment Research sidecarを読み込めません。生成状態を確認してください。"
        ) from exc


def requires_research_sidecar(filters):
    return bool(filters and (filters.hp_treatment_categories or filters.research_status))


def ensure_research_sidecar(filters):
    """HP治療カテゴリとResearch Statusは独立したテーブルを読むため、使われている方だけを
    個別にチェックする（片方のsidecarが欠けていても他方のfilterは動作し続ける）。
    """
    if not filters:
        return
    if filters.hp_treatment_categories and not research_sidecar_available():
        raise ResearchSidecarUnavailableError(
            "Treatment Research sidecarがありません。HP治療カテゴリfilterは利用できません。"
        )
    if filters.research_status and not clinic_research_status_available():
        raise ResearchSidecarUnavailableError(
            "Treatment Research sidecar（clinic_research_status）がありません。Research Status filterは利用できません。"
        )
