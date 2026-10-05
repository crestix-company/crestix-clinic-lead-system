"""最新Sales Target Classification（SSOT）をfilter/UIへ安全に接続するためのsidecar。

旧hp_rank（clinics.hp_rank, A/B/C/D/NO_HP/UNKNOWN）は医院のHP内容評価であり、
営業対象の正式分類ではない。正式なSales Tier分類は
artifacts/sales_target_reclassification/配下のCSV（SSOT）にある。

このモジュールはCSVをそのままATTACHできないため、確認済みschemaで
一度だけ軽量sqliteへ変換し、Treatment Research sidecarと同じ
「clinics.sqlite3にはWRITEしない・READ ONLYでATTACHする」パターンに揃える。
clinics.sqlite3（Production DB）へは一切書き込まない。
"""
import csv
import os
import sqlite3

from src.utils.config import ROOT

SALES_CLASSIFICATION_DIR = ROOT / "artifacts" / "sales_target_reclassification"

# 優先順位: 最新population updateのv3 candidateを最優先。
# v3が無い環境ではv2 candidate、さらに無ければ確定版final.csvへ安全にfallbackする。
# 推測で中間ファイルを合成しない。
SALES_CLASSIFICATION_CANDIDATES = (
    SALES_CLASSIFICATION_DIR / "sales_target_classification_v3_candidate.csv",
    SALES_CLASSIFICATION_DIR / "sales_target_classification_final_v2_candidate.csv",
    SALES_CLASSIFICATION_DIR / "sales_target_classification_final.csv",
)

SALES_CLASSIFICATION_SIDECAR_PATH = SALES_CLASSIFICATION_DIR / "sales_classification_sidecar.sqlite3"
SALES_CLASSIFICATION_ATTACH_NAME = "salesdb"
SALES_CLASSIFICATION_TABLE = "clinic_sales_classification"
SALES_CLASSIFICATION_QUALIFIED_TABLE = f"{SALES_CLASSIFICATION_ATTACH_NAME}.{SALES_CLASSIFICATION_TABLE}"

# CSVのsales_tier（人間可読な英語ラベル）→ UIで使う短いコード。
TIER_CODE_BY_RAW = {
    "VERIFIED_TREATMENT": "A",
    "LIKELY_TREATMENT": "B",
    "SPECIALTY_TARGET": "C",
    "UNKNOWN": "D",
}
TIER_LABELS = {
    "A": "A VERIFIED（HP確認済み治療）",
    "B": "B LIKELY（治療の可能性が高い）",
    "C": "C SPECIALTY（診療科単位のみ）",
    "D": "D UNKNOWN（分類不能・営業対象外）",
}
REQUIRED_COLUMNS = {"clinic_id", "sales_tier", "sales_usable", "confidence", "human_review_needed"}


class SalesClassificationUnavailableError(RuntimeError):
    pass


def sales_classification_source_path():
    """実在する最新classification artifactのPathを返す。無ければNone（推測で生成しない）。"""
    for path in SALES_CLASSIFICATION_CANDIDATES:
        if path.exists():
            return path
    return None


def _to_bool(value):
    return str(value).strip().lower() in {"true", "1", "yes"}


def _source_signature(path):
    stat = path.stat()
    return f"{path}:{stat.st_mtime_ns}:{stat.st_size}"


def _build_sidecar(source_path, sidecar_path):
    with source_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - fieldnames
        if missing:
            raise SalesClassificationUnavailableError(
                f"{source_path.name}に必要な列がありません: {sorted(missing)}"
            )
        rows = []
        for i, r in enumerate(reader):
            raw_tier = (r.get("sales_tier") or "").strip()
            tier_code = TIER_CODE_BY_RAW.get(raw_tier)
            if tier_code is None:
                raise SalesClassificationUnavailableError(
                    f"{source_path.name} {i+2}行目：未知のsales_tier値 {raw_tier!r}（clinic_id={r.get('clinic_id')}）"
                )
            try:
                clinic_id = int(r["clinic_id"])
            except (TypeError, ValueError) as exc:
                raise SalesClassificationUnavailableError(
                    f"{source_path.name} {i+2}行目：clinic_idが数値ではありません（{r.get('clinic_id')!r}）"
                ) from exc
            rows.append((
                clinic_id, r.get("clinic_name", ""), tier_code, raw_tier,
                r.get("sales_category", ""), r.get("treatment_category", ""),
                (r.get("confidence") or "").strip(),
                int(_to_bool(r.get("sales_usable"))),
                r.get("classification_source", ""),
                int(_to_bool(r.get("human_verified"))),
                int(_to_bool(r.get("human_review_needed"))),
                r.get("department", ""), r.get("crestix_department", ""),
            ))

    tmp_path = sidecar_path.with_suffix(".sqlite3.tmp")
    tmp_path.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute(f"""
            CREATE TABLE {SALES_CLASSIFICATION_TABLE}(
                clinic_id INTEGER PRIMARY KEY,
                clinic_name TEXT NOT NULL DEFAULT '',
                sales_tier TEXT NOT NULL DEFAULT '',
                sales_tier_raw TEXT NOT NULL DEFAULT '',
                sales_category TEXT NOT NULL DEFAULT '',
                treatment_category TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT '',
                sales_usable INTEGER NOT NULL DEFAULT 0,
                classification_source TEXT NOT NULL DEFAULT '',
                human_verified INTEGER NOT NULL DEFAULT 0,
                human_review_needed INTEGER NOT NULL DEFAULT 0,
                department TEXT NOT NULL DEFAULT '',
                crestix_department TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.executemany(
            f"INSERT INTO {SALES_CLASSIFICATION_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.execute(f"CREATE INDEX idx_sales_tier ON {SALES_CLASSIFICATION_TABLE}(sales_tier)")
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('source_signature',?)", (_source_signature(source_path),))
        conn.execute("INSERT INTO meta VALUES('source_path',?)", (str(source_path),))
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp_path, sidecar_path)


def ensure_sales_classification_sidecar():
    """最新artifactからsidecar sqliteを必要な時だけ再生成する。clinics.sqlite3へは書き込まない。

    戻り値: 使用したartifactのPath。artifactが無い場合はNone。
    """
    source_path = sales_classification_source_path()
    if source_path is None:
        return None
    sidecar_path = SALES_CLASSIFICATION_SIDECAR_PATH
    signature = _source_signature(source_path)
    if sidecar_path.exists():
        try:
            with sqlite3.connect(f"file:{sidecar_path}?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key='source_signature'").fetchone()
                if row and row[0] == signature:
                    return source_path
        except sqlite3.Error:
            pass
    _build_sidecar(source_path, sidecar_path)
    return source_path


def sales_classification_available():
    try:
        return ensure_sales_classification_sidecar() is not None
    except SalesClassificationUnavailableError:
        return False


def requires_sales_classification(filters):
    return bool(filters and (filters.sales_tiers or filters.sales_confidence or filters.exclude_human_review))


def ensure_sales_classification(filters):
    if requires_sales_classification(filters) and not sales_classification_available():
        raise SalesClassificationUnavailableError(
            "Sales Tier分類sidecarがありません。artifacts/sales_target_reclassification/配下のCSVを確認してください。"
        )


def sales_classification_summary():
    """件数表示用の実測サマリ。固定値をハードコードせず、その都度sidecarから集計する。"""
    if not sales_classification_available():
        return None
    with sqlite3.connect(f"file:{SALES_CLASSIFICATION_SIDECAR_PATH}?mode=ro", uri=True) as conn:
        total = conn.execute(f"SELECT count(*) FROM {SALES_CLASSIFICATION_TABLE}").fetchone()[0]
        usable = conn.execute(f"SELECT count(*) FROM {SALES_CLASSIFICATION_TABLE} WHERE sales_usable=1").fetchone()[0]
        by_tier = dict(conn.execute(f"SELECT sales_tier, count(*) FROM {SALES_CLASSIFICATION_TABLE} GROUP BY sales_tier"))
    return {
        "source_path": sales_classification_source_path(),
        "total": total,
        "sales_usable": usable,
        "by_tier": {t: by_tier.get(t, 0) for t in ("A", "B", "C", "D")},
    }
