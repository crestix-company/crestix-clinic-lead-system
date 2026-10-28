"""HP調査履歴を clinic_id 単位で集計するREAD ONLY指標。"""
import sqlite3
from pathlib import Path

from src.master.hp_effective_rank import hp_batch_path


class BatchMetricInvariantError(RuntimeError):
    pass


def web_research_metrics(production_db_path, batch_sidecar_path=None):
    """URL母集団とbatch結果をclinic_idで結合し、2つの不変式を検証して返す。"""
    production = Path(production_db_path)
    sidecar = Path(batch_sidecar_path or hp_batch_path())
    if not production.exists() or not sidecar.exists():
        return None
    conn = sqlite3.connect(f"file:{production}?mode=ro", uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS hpbatch", (f"file:{sidecar}?mode=ro",))
        clinic_columns = {row[1] for row in conn.execute("PRAGMA table_info(clinics)")}
        unmerged_clause = " AND (merged_into IS NULL OR merged_into='')" if "merged_into" in clinic_columns else ""
        row = conn.execute(
            f"""
            WITH url_clinics AS (
              SELECT id FROM clinics WHERE (hp_url<>'' OR maps_website_url<>''){unmerged_clause}
            ), joined AS (
              SELECT u.id,r.clinic_id AS history_clinic_id,r.fetch_status,r.treatment_categories
              FROM url_clinics u
              LEFT JOIN hpbatch.hp_research_batch_results r ON r.clinic_id=u.id
            )
            SELECT
              count(*) AS url_acquired,
              sum(CASE WHEN fetch_status='OK' THEN 1 ELSE 0 END) AS researched,
              sum(CASE WHEN history_clinic_id IS NOT NULL AND (fetch_status IS NULL OR fetch_status NOT IN ('OK','REVIEW')) THEN 1 ELSE 0 END) AS failed,
              sum(CASE WHEN fetch_status='REVIEW' THEN 1 ELSE 0 END) AS review,
              sum(CASE WHEN history_clinic_id IS NULL THEN 1 ELSE 0 END) AS not_researched,
              sum(CASE WHEN fetch_status='OK' AND json_array_length(treatment_categories)>0 THEN 1 ELSE 0 END) AS treatment_detected,
              sum(CASE WHEN fetch_status='OK' AND json_array_length(treatment_categories)=0 THEN 1 ELSE 0 END) AS treatment_not_detected
            FROM joined
            """
        ).fetchone()
    finally:
        conn.close()
    keys = ("url_acquired", "researched", "failed", "review", "not_researched",
            "treatment_detected", "treatment_not_detected")
    result = {key: int(value or 0) for key, value in zip(keys, row)}
    if result["url_acquired"] != result["researched"] + result["failed"] + result["review"] + result["not_researched"]:
        raise BatchMetricInvariantError("WebサイトURL件数と調査状態の合計が一致しません。")
    if result["researched"] != result["treatment_detected"] + result["treatment_not_detected"]:
        raise BatchMetricInvariantError("Webサイト調査完了件数と治療カテゴリ判定の合計が一致しません。")
    return result
