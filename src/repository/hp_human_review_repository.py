"""Supabase persistence for HP Human Review and training labels."""
from __future__ import annotations

import json
import uuid

from psycopg.types.json import Jsonb

from src.master.hp_human_review import (
    BUCKET_LABELS,
    POSITIVE_DECISIONS,
    VALID_DECISIONS,
    review_snapshot,
)
from src.master.store import dumps, now


TABLE_NAME = "provenance.hp_human_reviews"
RUN_TABLE_NAME = "provenance.hp_human_review_research_runs"
CLAIM_TABLE_NAME = "provenance.hp_human_review_claims"


def _strip_nul(value):
    """Remove NUL data that PostgreSQL JSONB cannot represent, without rewriting source TEXT."""
    if isinstance(value, str):
        return value.replace("\\u0000", "").replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_nul(item) for key, item in value.items()}
    return value


def _as_dict(value):
    if isinstance(value, dict):
        return _strip_nul(value)
    if not value:
        return {}
    # research.research_results.result_json is intentionally TEXT for legacy compatibility.
    # Some historical rows contain the JSON escape sequence \\u0000. PostgreSQL JSONB cannot
    # materialize that code point, so normalize it only in-memory for Human Review.
    cleaned = str(value).replace("\\u0000", "")
    return _strip_nul(json.loads(cleaned))


class SupabaseHpHumanReviewRepository:
    """Append-only human labels for the current HP auto-decision attempt."""

    def __init__(self, conn):
        self._conn = conn

    def available(self):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s),to_regclass(%s)", (TABLE_NAME, RUN_TABLE_NAME))
                row = cur.fetchone()
                exists = row[0] is not None and row[1] is not None
            self._conn.commit()
            return exists
        except Exception:
            self._conn.rollback()
            raise

    def claiming_available(self):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (CLAIM_TABLE_NAME,))
                exists = cur.fetchone()[0] is not None
            self._conn.commit()
            return exists
        except Exception:
            self._conn.rollback()
            raise

    def active_claim_count(self):
        if not self.claiming_available():
            return 0
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM provenance.hp_human_review_claims "
                    "WHERE lease_until > clock_timestamp()"
                )
                count = int(cur.fetchone()[0] or 0)
            self._conn.commit()
            return count
        except Exception:
            self._conn.rollback()
            raise

    def active_claim_for_owner(self, owner_token):
        owner_token = str(owner_token or "").strip()
        if not owner_token or not self.claiming_available():
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT clinic_id,hp_checked_at,owner_label,claimed_at,lease_until
                    FROM provenance.hp_human_review_claims
                    WHERE owner_token=%s
                      AND lease_until > clock_timestamp()
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (owner_token,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if not row:
            return None
        return {
            "clinic_id": int(row[0]),
            "hp_checked_at": row[1],
            "owner_label": row[2] or "",
            "claimed_at": row[3],
            "lease_until": row[4],
        }

    def claim(self, *, clinic_id, hp_checked_at, owner_token, owner_label="", lease_seconds=300):
        """Atomically claim one clinic unless another live owner already holds it."""
        owner_token = str(owner_token or "").strip()
        if not owner_token:
            raise ValueError("Human Review端末トークンがありません。")
        hp_checked_at = str(hp_checked_at or "")
        if not hp_checked_at:
            raise ValueError("HP調査時刻がありません。")
        lease_seconds = max(60, min(int(lease_seconds or 300), 1800))
        if not self.claiming_available():
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO provenance.hp_human_review_claims(
                      clinic_id,hp_checked_at,owner_token,owner_label,
                      claimed_at,lease_until,updated_at
                    ) VALUES(
                      %s,%s,%s,%s,
                      clock_timestamp(),
                      clock_timestamp() + (%s * interval '1 second'),
                      clock_timestamp()
                    )
                    ON CONFLICT(clinic_id) DO UPDATE SET
                      hp_checked_at=EXCLUDED.hp_checked_at,
                      owner_token=EXCLUDED.owner_token,
                      owner_label=EXCLUDED.owner_label,
                      claimed_at=CASE
                        WHEN provenance.hp_human_review_claims.owner_token=EXCLUDED.owner_token
                          THEN provenance.hp_human_review_claims.claimed_at
                        ELSE clock_timestamp()
                      END,
                      lease_until=clock_timestamp() + (%s * interval '1 second'),
                      updated_at=clock_timestamp()
                    WHERE provenance.hp_human_review_claims.lease_until <= clock_timestamp()
                       OR provenance.hp_human_review_claims.owner_token=EXCLUDED.owner_token
                       OR provenance.hp_human_review_claims.hp_checked_at<>EXCLUDED.hp_checked_at
                    RETURNING clinic_id,hp_checked_at,owner_label,claimed_at,lease_until
                    """,
                    (
                        int(clinic_id), hp_checked_at, owner_token, str(owner_label or ""),
                        lease_seconds, lease_seconds,
                    ),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if not row:
            return None
        return {
            "clinic_id": int(row[0]),
            "hp_checked_at": row[1],
            "owner_label": row[2] or "",
            "claimed_at": row[3],
            "lease_until": row[4],
        }

    def renew_claim(self, *, clinic_id, hp_checked_at, owner_token, owner_label="", lease_seconds=300):
        owner_token = str(owner_token or "").strip()
        if not owner_token or not self.claiming_available():
            return None
        lease_seconds = max(60, min(int(lease_seconds or 300), 1800))
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE provenance.hp_human_review_claims
                    SET owner_label=%s,
                        lease_until=clock_timestamp() + (%s * interval '1 second'),
                        updated_at=clock_timestamp()
                    WHERE clinic_id=%s
                      AND hp_checked_at=%s
                      AND owner_token=%s
                      AND lease_until > clock_timestamp()
                    RETURNING clinic_id,hp_checked_at,owner_label,claimed_at,lease_until
                    """,
                    (
                        str(owner_label or ""), lease_seconds, int(clinic_id),
                        str(hp_checked_at or ""), owner_token,
                    ),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if not row:
            return None
        return {
            "clinic_id": int(row[0]),
            "hp_checked_at": row[1],
            "owner_label": row[2] or "",
            "claimed_at": row[3],
            "lease_until": row[4],
        }

    def release_claim(self, *, clinic_id, owner_token, hp_checked_at=None):
        owner_token = str(owner_token or "").strip()
        if not owner_token or not self.claiming_available():
            return False
        params = [int(clinic_id), owner_token]
        checked_sql = ""
        if hp_checked_at is not None:
            checked_sql = " AND hp_checked_at=%s"
            params.append(str(hp_checked_at or ""))
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM provenance.hp_human_review_claims "
                    "WHERE clinic_id=%s AND owner_token=%s" + checked_sql + " RETURNING clinic_id",
                    tuple(params),
                )
                row = cur.fetchone()
            self._conn.commit()
            return bool(row)
        except Exception:
            self._conn.rollback()
            raise

    def claim_next(self, *, owner_token, owner_label="", priority="ALL", lease_seconds=300):
        """Return the caller's current claim, otherwise atomically claim the next queue row."""
        if not self.claiming_available():
            return None

        current = self.active_claim_for_owner(owner_token)
        rows = self.queue(include_reviewed=False, priority=priority, limit=200)
        if current:
            for row in rows:
                if (
                    int(row["clinic_id"]) == int(current["clinic_id"])
                    and str(row["snapshot"].get("hp_checked_at") or "") == str(current["hp_checked_at"] or "")
                ):
                    renewed = self.renew_claim(
                        clinic_id=row["clinic_id"],
                        hp_checked_at=row["snapshot"]["hp_checked_at"],
                        owner_token=owner_token,
                        owner_label=owner_label,
                        lease_seconds=lease_seconds,
                    )
                    if renewed:
                        row["claim"] = renewed
                        return row
            self.release_claim(
                clinic_id=current["clinic_id"],
                owner_token=owner_token,
                hp_checked_at=current["hp_checked_at"],
            )

        for row in rows:
            claimed = self.claim(
                clinic_id=row["clinic_id"],
                hp_checked_at=row["snapshot"]["hp_checked_at"],
                owner_token=owner_token,
                owner_label=owner_label,
                lease_seconds=lease_seconds,
            )
            if claimed:
                row["claim"] = claimed
                return row
        return None

    def _current_rows(self):
        """Return current HP REVIEW attempts plus the latest human label for that attempt."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      c.id AS clinic_id,
                      c.clinic_name,
                      c.prefecture,
                      c.phone,
                      c.address,
                      c.maps_website_url,
                      rr.result_json,
                      rr.updated_at AS result_updated_at,
                      latest_review.id AS human_review_id,
                      latest_review.human_decision,
                      latest_review.selected_url AS reviewed_url,
                      latest_review.reviewer,
                      latest_review.review_note,
                      latest_review.reviewed_at,
                      latest_run.status AS reanalysis_status,
                      latest_run.treatment_categories AS reanalysis_treatment_categories,
                      latest_run.treatment_count AS reanalysis_treatment_count,
                      latest_run.error_detail AS reanalysis_error,
                      latest_run.finished_at AS reanalysis_finished_at,
                      source_job.job_id,
                      source_job.auto_run_id
                    FROM research.research_results rr
                    JOIN public.clinics c ON c.id=rr.clinic_id
                    LEFT JOIN LATERAL (
                      SELECT h.id,h.human_decision,h.selected_url,h.reviewer,h.review_note,h.reviewed_at
                      FROM provenance.hp_human_reviews h
                      WHERE h.clinic_id=c.id
                        AND h.hp_checked_at=COALESCE(replace(rr.result_json, chr(92) || 'u0000', '')::jsonb->>'hp_checked_at','')
                      ORDER BY h.reviewed_at DESC,h.id DESC
                      LIMIT 1
                    ) latest_review ON true
                    LEFT JOIN LATERAL (
                      SELECT x.status,x.treatment_categories,x.treatment_count,x.error_detail,x.finished_at
                      FROM provenance.hp_human_review_research_runs x
                      WHERE x.human_review_id=latest_review.id
                      ORDER BY x.finished_at DESC,x.id DESC
                      LIMIT 1
                    ) latest_run ON true
                    LEFT JOIN LATERAL (
                      SELECT j.id AS job_id,COALESCE(j.options_json::jsonb->>'auto_run_id','') AS auto_run_id
                      FROM research.research_job_items i
                      JOIN research.research_jobs j ON j.id=i.job_id
                      WHERE i.clinic_id=c.id
                        AND i.state='DONE'
                        AND i.result='REVIEW'
                        AND j.kind='hp'
                      ORDER BY j.updated_at DESC,j.created_at DESC,j.id DESC
                      LIMIT 1
                    ) source_job ON true
                    WHERE c.merged_into IS NULL
                      AND c.merge_hold=false
                      AND c.active=true
                      AND COALESCE(replace(rr.result_json, chr(92) || 'u0000', '')::jsonb->>'hp_checked_at','')<>''
                      AND (
                        COALESCE(replace(rr.result_json, chr(92) || 'u0000', '')::jsonb->>'hp_status','')='REVIEW'
                        OR COALESCE(replace(rr.result_json, chr(92) || 'u0000', '')::jsonb->>'hp_content_status','')='ACCESS_RESTRICTED'
                      )
                    """
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            self._conn.commit()
            return rows
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def _decorate(row):
        result = _as_dict(row.get("result_json"))
        snapshot = review_snapshot(result)
        output = dict(row)
        output["result_json"] = result
        output["snapshot"] = snapshot
        output["reviewed"] = bool(row.get("human_review_id"))
        return output

    def queue(self, *, include_reviewed=False, priority="ALL", limit=200):
        if not self.available():
            return []
        rows = [self._decorate(row) for row in self._current_rows()]
        if not include_reviewed:
            rows = [row for row in rows if not row["reviewed"]]
        if priority in {"HIGH", "MEDIUM", "LOW"}:
            rows = [row for row in rows if row["snapshot"]["priority"] == priority]
        priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        rows.sort(
            key=lambda row: (
                priority_order.get(row["snapshot"]["priority"], 9),
                -int(row["snapshot"].get("score") or 0),
                int(row["clinic_id"]),
            )
        )
        return rows if limit is None else rows[: max(0, int(limit))]

    def summary(self):
        if not self.available():
            return {
                "available": False, "total": 0, "reviewed": 0, "unreviewed": 0,
                "HIGH": 0, "MEDIUM": 0, "LOW": 0,
            }
        rows = self.queue(include_reviewed=True, limit=None)
        unreviewed = [row for row in rows if not row["reviewed"]]
        return {
            "available": True,
            "total": len(rows),
            "reviewed": sum(row["reviewed"] for row in rows),
            "unreviewed": len(unreviewed),
            "HIGH": sum(row["snapshot"]["priority"] == "HIGH" for row in unreviewed),
            "MEDIUM": sum(row["snapshot"]["priority"] == "MEDIUM" for row in unreviewed),
            "LOW": sum(row["snapshot"]["priority"] == "LOW" for row in unreviewed),
            "claimed": self.active_claim_count() if self.claiming_available() else 0,
        }

    def save_review(
        self,
        *,
        clinic_id,
        hp_checked_at,
        human_decision,
        selected_url="",
        reviewer,
        review_note="",
        research_job_id="",
        auto_run_id="",
        claim_owner_token="",
    ):
        if human_decision not in VALID_DECISIONS:
            raise ValueError("Human Reviewの判定値を確認してください。")
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise ValueError("確認者を入力してください。")
        selected_url = str(selected_url or "").strip()
        review_note = str(review_note or "").strip()
        review_id = uuid.uuid4().hex

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT result_json FROM research.research_results WHERE clinic_id=%s FOR UPDATE",
                    (int(clinic_id),),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("現在のHP調査結果が見つかりません。")
                result = _as_dict(row[0])
                snapshot = review_snapshot(result)
                current_checked_at = snapshot.get("hp_checked_at") or ""
                if not current_checked_at or current_checked_at != str(hp_checked_at or ""):
                    raise ValueError("HP調査結果が更新されています。画面を更新して最新結果を確認してください。")

                claim_owner_token = str(claim_owner_token or "").strip()
                if claim_owner_token:
                    cur.execute(
                        """
                        SELECT owner_token,hp_checked_at,(lease_until > clock_timestamp())
                        FROM provenance.hp_human_review_claims
                        WHERE clinic_id=%s
                        FOR UPDATE
                        """,
                        (int(clinic_id),),
                    )
                    claim_row = cur.fetchone()
                    if (
                        not claim_row
                        or claim_row[0] != claim_owner_token
                        or str(claim_row[1] or "") != current_checked_at
                        or not bool(claim_row[2])
                    ):
                        raise ValueError("この医院のレビューClaimが失効しました。次の医院を取得してください。")

                allowed_urls = set(snapshot.get("candidate_urls") or [])
                if human_decision in POSITIVE_DECISIONS:
                    if not selected_url:
                        raise ValueError("正しいHP URLを選択してください。")
                    if selected_url not in allowed_urls:
                        raise ValueError("現在の要確認候補にないURLは保存できません。")

                cur.execute(
                    """
                    INSERT INTO provenance.hp_human_reviews(
                      id,clinic_id,hp_checked_at,research_job_id,auto_run_id,selected_url,
                      human_decision,reviewer,review_note,rule_version,auto_snapshot,reviewed_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        review_id, int(clinic_id), current_checked_at,
                        str(research_job_id or ""), str(auto_run_id or ""), selected_url,
                        human_decision, reviewer, review_note, snapshot["rule_version"],
                        Jsonb(snapshot), now(),
                    ),
                )

                if human_decision in POSITIVE_DECISIONS:
                    audit_note = "HP Human Review: " + human_decision
                    if review_note:
                        audit_note += " / " + review_note
                    for field, value in (("hp_url", selected_url), ("hp_status", "VERIFIED")):
                        cur.execute(
                            "SELECT value_json FROM provenance.manual_overrides "
                            "WHERE clinic_id=%s AND field=%s FOR UPDATE",
                            (int(clinic_id), field),
                        )
                        before = cur.fetchone()
                        cur.execute(
                            """
                            INSERT INTO provenance.manual_overrides(
                              clinic_id,field,value_json,source,note,updated_at
                            ) VALUES(%s,%s,%s,%s,%s,%s)
                            ON CONFLICT(clinic_id,field) DO UPDATE SET
                              value_json=EXCLUDED.value_json,
                              source=EXCLUDED.source,
                              note=EXCLUDED.note,
                              updated_at=EXCLUDED.updated_at
                            """,
                            (
                                int(clinic_id), field, Jsonb(value),
                                "HP Human Review", audit_note, now(),
                            ),
                        )
                        cur.execute(
                            """
                            INSERT INTO provenance.change_history(
                              clinic_id,action,before_json,after_json,note,created_at
                            ) VALUES(%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                int(clinic_id), "HP Human Review:" + field,
                                dumps(before[0] if before else None), dumps(value),
                                audit_note, now(),
                            ),
                        )

                    # Keep public.clinics projection consistent with the manual override in this transaction.
                    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository
                    SupabaseClinicWriteRepository._refresh_projection_tx(
                        cur, int(clinic_id), is_authoritative_official_source=False
                    )

                if claim_owner_token:
                    cur.execute(
                        """
                        DELETE FROM provenance.hp_human_review_claims
                        WHERE clinic_id=%s
                          AND hp_checked_at=%s
                          AND owner_token=%s
                        """,
                        (int(clinic_id), current_checked_at, claim_owner_token),
                    )

            self._conn.commit()
            return review_id
        except Exception:
            self._conn.rollback()
            raise

    def record_reanalysis(
        self,
        *,
        human_review_id,
        clinic_id,
        selected_url,
        status,
        treatment_categories=None,
        error_detail="",
        started_at=None,
        finished_at=None,
    ):
        if status not in {"DONE", "FAILED"}:
            raise ValueError("治療カテゴリ再解析の状態を確認してください。")
        categories = treatment_categories if isinstance(treatment_categories, list) else []
        run_id = uuid.uuid4().hex
        started_at = started_at or now()
        finished_at = finished_at or now()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT human_decision,selected_url FROM provenance.hp_human_reviews WHERE id=%s",
                    (str(human_review_id),),
                )
                review = cur.fetchone()
                if not review:
                    raise ValueError("Human Review履歴が見つかりません。")
                if review[0] not in POSITIVE_DECISIONS:
                    raise ValueError("Positive Human Reviewだけが治療カテゴリ再解析の対象です。")
                if str(review[1] or "") != str(selected_url or ""):
                    raise ValueError("Human Reviewで確定したURLと再解析URLが一致しません。")
                cur.execute(
                    """
                    INSERT INTO provenance.hp_human_review_research_runs(
                      id,human_review_id,clinic_id,selected_url,status,identity_source,
                      treatment_categories,treatment_count,error_detail,started_at,finished_at
                    ) VALUES(%s,%s,%s,%s,%s,'HUMAN_REVIEW',%s,%s,%s,%s,%s)
                    """,
                    (
                        run_id, str(human_review_id), int(clinic_id), str(selected_url or ""),
                        status, Jsonb(categories), len(categories), str(error_detail or ""),
                        started_at, finished_at,
                    ),
                )
            self._conn.commit()
            return run_id
        except Exception:
            self._conn.rollback()
            raise

    def latest_reanalysis(self, human_review_id):
        if not self.available():
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status,treatment_categories,treatment_count,error_detail,started_at,finished_at
                    FROM provenance.hp_human_review_research_runs
                    WHERE human_review_id=%s
                    ORDER BY finished_at DESC,id DESC
                    LIMIT 1
                    """,
                    (str(human_review_id),),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if not row:
            return None
        return {
            "status": row[0],
            "treatment_categories": row[1] if isinstance(row[1], list) else [],
            "treatment_count": int(row[2] or 0),
            "error_detail": row[3] or "",
            "started_at": row[4],
            "finished_at": row[5],
        }

    def analytics(self):
        if not self.available():
            return {
                "available": False, "reviewed": 0, "positive": 0,
                "negative": 0, "uncertain": 0, "by_bucket": [],
            }
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (clinic_id,hp_checked_at)
                      id,clinic_id,hp_checked_at,human_decision,reviewer,review_note,
                      rule_version,auto_snapshot,reviewed_at,selected_url
                    FROM provenance.hp_human_reviews
                    ORDER BY clinic_id,hp_checked_at,reviewed_at DESC,id DESC
                    """
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        stats = {}
        for row in rows:
            snapshot = _as_dict(row.get("auto_snapshot"))
            bucket = str(snapshot.get("bucket") or "OTHER")
            entry = stats.setdefault(bucket, {"bucket": bucket, "reviewed": 0, "decided": 0, "positive": 0})
            entry["reviewed"] += 1
            decision = row.get("human_decision")
            if decision != "UNCERTAIN":
                entry["decided"] += 1
                if decision in POSITIVE_DECISIONS:
                    entry["positive"] += 1

        by_bucket = []
        for bucket, entry in stats.items():
            decided = entry["decided"]
            by_bucket.append({
                **entry,
                "label": BUCKET_LABELS.get(bucket, bucket),
                "official_rate": (entry["positive"] / decided) if decided else None,
            })
        by_bucket.sort(key=lambda item: (-item["reviewed"], item["label"]))

        return {
            "available": True,
            "reviewed": len(rows),
            "positive": sum(row.get("human_decision") in POSITIVE_DECISIONS for row in rows),
            "negative": sum(row.get("human_decision") == "NOT_OFFICIAL" for row in rows),
            "uncertain": sum(row.get("human_decision") == "UNCERTAIN" for row in rows),
            "by_bucket": by_bucket,
        }

    def training_rows(self):
        """Latest human label per HP attempt for offline rule/model evaluation."""
        if not self.available():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (h.clinic_id,h.hp_checked_at)
                      h.id,h.clinic_id,c.clinic_name,c.prefecture,c.phone,
                      h.hp_checked_at,h.research_job_id,h.auto_run_id,h.selected_url,
                      h.human_decision,h.reviewer,h.review_note,h.rule_version,
                      h.auto_snapshot,h.reviewed_at,
                      latest_run.status AS reanalysis_status,
                      latest_run.treatment_categories AS reanalysis_treatment_categories,
                      latest_run.treatment_count AS reanalysis_treatment_count,
                      latest_run.error_detail AS reanalysis_error,
                      latest_run.finished_at AS reanalysis_finished_at
                    FROM provenance.hp_human_reviews h
                    JOIN public.clinics c ON c.id=h.clinic_id
                    LEFT JOIN LATERAL (
                      SELECT x.status,x.treatment_categories,x.treatment_count,x.error_detail,x.finished_at
                      FROM provenance.hp_human_review_research_runs x
                      WHERE x.human_review_id=h.id
                      ORDER BY x.finished_at DESC,x.id DESC
                      LIMIT 1
                    ) latest_run ON true
                    ORDER BY h.clinic_id,h.hp_checked_at,h.reviewed_at DESC,h.id DESC
                    """
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            self._conn.commit()
            return rows
        except Exception:
            self._conn.rollback()
            raise
