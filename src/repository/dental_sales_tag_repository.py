"""Supabase persistence for dental sales tags and Human Review labels."""
from __future__ import annotations

import uuid

from psycopg.types.json import Jsonb

from src.master.dental_sales_tags import DEFINITION_BY_CODE, DentalTagEvidence


TAG_TABLE = "provenance.dental_sales_tags"
REVIEW_TABLE = "provenance.dental_sales_tag_reviews"
VALID_HUMAN_DECISIONS = frozenset({"CONFIRMED", "REJECTED", "UNCERTAIN"})


class SupabaseDentalSalesTagRepository:
    def __init__(self, conn):
        self._conn = conn

    def available(self):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s),to_regclass(%s)", (TAG_TABLE, REVIEW_TABLE))
                row = cur.fetchone()
                exists = bool(row and row[0] is not None and row[1] is not None)
            self._conn.commit()
            return exists
        except Exception:
            self._conn.rollback()
            raise

    def replace_auto_tags_bulk(self, classifications):
        """Replace derived auto tags for a set of clinics without touching Human Review history.

        classifications: iterable[(clinic_id, iterable[DentalTagEvidence])]
        Existing rows are deactivated first; current classifier output is then upserted active=true.
        """
        items = [(int(cid), tuple(tags or ())) for cid, tags in classifications]
        if not items:
            return {"clinics": 0, "tags": 0}
        clinic_ids = [cid for cid, _tags in items]
        rows = []
        for clinic_id, tags in items:
            for tag in tags:
                if not isinstance(tag, DentalTagEvidence):
                    raise TypeError("DentalTagEvidenceだけを保存できます。")
                rows.append((
                    clinic_id, tag.tag_code, tag.tag_label, int(tag.priority_group),
                    int(tag.sort_order), tag.auto_status, float(tag.confidence),
                    tag.source, tag.matched_alias, tag.evidence_text, tag.rule_id,
                    tag.classifier_version,
                ))
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE provenance.dental_sales_tags "
                    "SET active=false,computed_at=clock_timestamp() "
                    "WHERE clinic_id=ANY(%s::bigint[])",
                    (clinic_ids,),
                )
                if rows:
                    cur.executemany(
                        """
                        INSERT INTO provenance.dental_sales_tags(
                          clinic_id,tag_code,tag_label,priority_group,sort_order,
                          auto_status,confidence,source,matched_alias,evidence_text,
                          rule_id,classifier_version,active,computed_at
                        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,clock_timestamp())
                        ON CONFLICT(clinic_id,tag_code) DO UPDATE SET
                          tag_label=EXCLUDED.tag_label,
                          priority_group=EXCLUDED.priority_group,
                          sort_order=EXCLUDED.sort_order,
                          auto_status=EXCLUDED.auto_status,
                          confidence=EXCLUDED.confidence,
                          source=EXCLUDED.source,
                          matched_alias=EXCLUDED.matched_alias,
                          evidence_text=EXCLUDED.evidence_text,
                          rule_id=EXCLUDED.rule_id,
                          classifier_version=EXCLUDED.classifier_version,
                          active=true,
                          computed_at=clock_timestamp()
                        """,
                        rows,
                    )
            self._conn.commit()
            return {"clinics": len(items), "tags": len(rows)}
        except Exception:
            self._conn.rollback()
            raise

    def tags_for_clinic(self, clinic_id, *, effective=True):
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest_review AS (
                      SELECT DISTINCT ON (tag_code)
                        tag_code,human_decision,reviewer,review_note,reviewed_at
                      FROM provenance.dental_sales_tag_reviews
                      WHERE clinic_id=%s
                      ORDER BY tag_code,reviewed_at DESC,id DESC
                    )
                    SELECT
                      t.tag_code,t.tag_label,t.priority_group,t.sort_order,t.auto_status,
                      t.confidence,t.source,t.matched_alias,t.evidence_text,t.rule_id,
                      t.classifier_version,t.active,
                      r.human_decision,r.reviewer,r.review_note,r.reviewed_at
                    FROM provenance.dental_sales_tags t
                    LEFT JOIN latest_review r ON r.tag_code=t.tag_code
                    WHERE t.clinic_id=%s
                    ORDER BY t.priority_group,t.sort_order,t.tag_code
                    """,
                    (int(clinic_id), int(clinic_id)),
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if not effective:
            return rows
        output = []
        seen = set()
        for row in rows:
            seen.add(row["tag_code"])
            decision = row.get("human_decision")
            if decision == "REJECTED":
                continue
            if row["active"] and (row["auto_status"] == "CONFIRMED" or decision == "CONFIRMED"):
                output.append(row)
        # Human-confirmed labels remain effective even if a later auto classifier no longer emits
        # the tag. The definition provides stable display/priority metadata.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (tag_code)
                      tag_code,human_decision,reviewer,review_note,reviewed_at
                    FROM provenance.dental_sales_tag_reviews
                    WHERE clinic_id=%s
                    ORDER BY tag_code,reviewed_at DESC,id DESC
                    """,
                    (int(clinic_id),),
                )
                review_rows = cur.fetchall()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        for code, decision, reviewer, note, reviewed_at in review_rows:
            if code in seen or decision != "CONFIRMED" or code not in DEFINITION_BY_CODE:
                continue
            definition = DEFINITION_BY_CODE[code]
            output.append({
                "tag_code": code,
                "tag_label": definition.label,
                "priority_group": definition.priority_group,
                "sort_order": definition.sort_order,
                "auto_status": "",
                "confidence": 1.0,
                "source": "HUMAN_REVIEW",
                "matched_alias": "",
                "evidence_text": "",
                "rule_id": "HUMAN_REVIEW_ONLY",
                "classifier_version": "",
                "active": False,
                "human_decision": decision,
                "reviewer": reviewer,
                "review_note": note,
                "reviewed_at": reviewed_at,
            })
        output.sort(key=lambda row: (row["priority_group"], row["sort_order"], row["tag_code"]))
        return output

    def primary_for_clinic(self, clinic_id):
        rows = self.tags_for_clinic(clinic_id, effective=True)
        return rows[0] if rows else None

    def save_review(self, *, clinic_id, tag_code, human_decision, reviewer, review_note=""):
        if tag_code not in DEFINITION_BY_CODE:
            raise ValueError("歯科営業タグを確認してください。")
        if human_decision not in VALID_HUMAN_DECISIONS:
            raise ValueError("Human Review判定を確認してください。")
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise ValueError("確認者を入力してください。")
        review_id = uuid.uuid4().hex
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT medical_type FROM public.clinics WHERE id=%s",
                    (int(clinic_id),),
                )
                clinic = cur.fetchone()
                if not clinic or clinic[0] != "歯科":
                    raise ValueError("歯科医院ではありません。")
                cur.execute(
                    """
                    SELECT tag_code,tag_label,priority_group,sort_order,auto_status,
                           confidence,source,matched_alias,evidence_text,rule_id,classifier_version,active
                    FROM provenance.dental_sales_tags
                    WHERE clinic_id=%s AND tag_code=%s
                    """,
                    (int(clinic_id), tag_code),
                )
                row = cur.fetchone()
                definition = DEFINITION_BY_CODE[tag_code]
                snapshot = {
                    "tag_code": tag_code,
                    "tag_label": definition.label,
                    "priority_group": definition.priority_group,
                    "sort_order": definition.sort_order,
                }
                if row:
                    keys = (
                        "tag_code","tag_label","priority_group","sort_order","auto_status",
                        "confidence","source","matched_alias","evidence_text","rule_id",
                        "classifier_version","active",
                    )
                    snapshot.update(dict(zip(keys, row)))
                cur.execute(
                    """
                    INSERT INTO provenance.dental_sales_tag_reviews(
                      id,clinic_id,tag_code,human_decision,reviewer,review_note,
                      auto_snapshot,reviewed_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,clock_timestamp())
                    """,
                    (
                        review_id, int(clinic_id), tag_code, human_decision,
                        reviewer, str(review_note or "").strip(), Jsonb(snapshot),
                    ),
                )
            self._conn.commit()
            return review_id
        except Exception:
            self._conn.rollback()
            raise

    def priority_summary(self):
        """Effective clinic-level primary tag counts after latest Human Review overrides."""
        if not self.available():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest_review AS (
                      SELECT DISTINCT ON (clinic_id,tag_code)
                        clinic_id,tag_code,human_decision
                      FROM provenance.dental_sales_tag_reviews
                      ORDER BY clinic_id,tag_code,reviewed_at DESC,id DESC
                    ),
                    effective AS (
                      SELECT t.*
                      FROM provenance.dental_sales_tags t
                      LEFT JOIN latest_review r
                        ON r.clinic_id=t.clinic_id AND r.tag_code=t.tag_code
                      WHERE t.active=true
                        AND (
                          (t.auto_status='CONFIRMED' AND COALESCE(r.human_decision,'')<>'REJECTED')
                          OR r.human_decision='CONFIRMED'
                        )
                    ),
                    primary_tag AS (
                      SELECT DISTINCT ON (clinic_id)
                        clinic_id,priority_group,sort_order,tag_code,tag_label
                      FROM effective
                      ORDER BY clinic_id,priority_group,sort_order,tag_code
                    )
                    SELECT priority_group,tag_code,tag_label,count(*) AS clinics
                    FROM primary_tag
                    GROUP BY priority_group,tag_code,tag_label
                    ORDER BY priority_group,tag_code
                    """
                )
                rows = [
                    {
                        "priority_group": int(row[0]), "tag_code": row[1],
                        "tag_label": row[2], "clinics": int(row[3]),
                    }
                    for row in cur.fetchall()
                ]
            self._conn.commit()
            return rows
        except Exception:
            self._conn.rollback()
            raise

    def list_effective_clinics(self, *, priority_groups=None, tag_codes=None, limit=500, offset=0):
        """Clinic-level effective dental list ordered by sales priority."""
        priority_groups = [int(x) for x in (priority_groups or [])]
        tag_codes = [str(x) for x in (tag_codes or [])]
        filters, args = [], []
        if priority_groups:
            filters.append("p.priority_group=ANY(%s::smallint[])")
            args.append(priority_groups)
        if tag_codes:
            filters.append(
                "EXISTS(SELECT 1 FROM effective e2 "
                "WHERE e2.clinic_id=c.id AND e2.tag_code=ANY(%s::text[]))"
            )
            args.append(tag_codes)
        where_extra = (" AND " + " AND ".join(filters)) if filters else ""
        args.extend([max(1, min(int(limit or 500), 100000)), max(0, int(offset or 0))])
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest_review AS (
                      SELECT DISTINCT ON (clinic_id,tag_code)
                        clinic_id,tag_code,human_decision
                      FROM provenance.dental_sales_tag_reviews
                      ORDER BY clinic_id,tag_code,reviewed_at DESC,id DESC
                    ),
                    effective AS (
                      SELECT t.*
                      FROM provenance.dental_sales_tags t
                      LEFT JOIN latest_review r
                        ON r.clinic_id=t.clinic_id AND r.tag_code=t.tag_code
                      WHERE t.active=true
                        AND (
                          (t.auto_status='CONFIRMED' AND COALESCE(r.human_decision,'')<>'REJECTED')
                          OR r.human_decision='CONFIRMED'
                        )
                    ),
                    primary_tag AS (
                      SELECT DISTINCT ON (clinic_id)
                        clinic_id,priority_group,sort_order,tag_code,tag_label,
                        confidence,source,rule_id
                      FROM effective
                      ORDER BY clinic_id,priority_group,sort_order,tag_code
                    ),
                    all_tags AS (
                      SELECT clinic_id,
                             string_agg(tag_label,' / ' ORDER BY priority_group,sort_order,tag_code) AS labels,
                             string_agg(tag_code,' / ' ORDER BY priority_group,sort_order,tag_code) AS codes
                      FROM effective
                      GROUP BY clinic_id
                    )
                    SELECT
                      c.id,c.uuid,c.clinic_name,c.phone,c.address,c.prefecture,
                      COALESCE(NULLIF(c.hp_url,''),NULLIF(c.maps_website_url,''),'') AS hp_url,
                      p.priority_group,p.tag_code,p.tag_label,p.confidence,p.source,p.rule_id,
                      a.labels,a.codes
                    FROM public.clinics c
                    JOIN primary_tag p ON p.clinic_id=c.id
                    JOIN all_tags a ON a.clinic_id=c.id
                    WHERE c.medical_type='歯科'
                      AND c.merged_into IS NULL
                      AND c.merge_hold=false
                      AND c.active=true
                    """ + where_extra + """
                    ORDER BY p.priority_group,p.sort_order,c.id
                    LIMIT %s OFFSET %s
                    """,
                    tuple(args),
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            self._conn.commit()
            return rows
        except Exception:
            self._conn.rollback()
            raise

    def effective_count(self, *, priority_groups=None, tag_codes=None):
        priority_groups = [int(x) for x in (priority_groups or [])]
        tag_codes = [str(x) for x in (tag_codes or [])]
        filters, args = [], []
        if priority_groups:
            filters.append("p.priority_group=ANY(%s::smallint[])")
            args.append(priority_groups)
        if tag_codes:
            filters.append(
                "EXISTS(SELECT 1 FROM effective e2 "
                "WHERE e2.clinic_id=c.id AND e2.tag_code=ANY(%s::text[]))"
            )
            args.append(tag_codes)
        where_extra = (" AND " + " AND ".join(filters)) if filters else ""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest_review AS (
                      SELECT DISTINCT ON (clinic_id,tag_code)
                        clinic_id,tag_code,human_decision
                      FROM provenance.dental_sales_tag_reviews
                      ORDER BY clinic_id,tag_code,reviewed_at DESC,id DESC
                    ),
                    effective AS (
                      SELECT t.*
                      FROM provenance.dental_sales_tags t
                      LEFT JOIN latest_review r
                        ON r.clinic_id=t.clinic_id AND r.tag_code=t.tag_code
                      WHERE t.active=true
                        AND (
                          (t.auto_status='CONFIRMED' AND COALESCE(r.human_decision,'')<>'REJECTED')
                          OR r.human_decision='CONFIRMED'
                        )
                    ),
                    primary_tag AS (
                      SELECT DISTINCT ON (clinic_id)
                        clinic_id,priority_group,sort_order,tag_code,tag_label
                      FROM effective
                      ORDER BY clinic_id,priority_group,sort_order,tag_code
                    )
                    SELECT count(*)
                    FROM public.clinics c
                    JOIN primary_tag p ON p.clinic_id=c.id
                    WHERE c.medical_type='歯科'
                      AND c.merged_into IS NULL
                      AND c.merge_hold=false
                      AND c.active=true
                    """ + where_extra,
                    tuple(args),
                )
                count = int(cur.fetchone()[0] or 0)
            self._conn.commit()
            return count
        except Exception:
            self._conn.rollback()
            raise

    def review_queue(self, *, only_review=True, include_reviewed=False, limit=200):
        """Return auto tag candidates for Human Review, REVIEW first then confidence ascending."""
        if not self.available():
            return []
        conditions = ["t.active=true"]
        args = []
        if only_review:
            conditions.append("t.auto_status='REVIEW'")
        if not include_reviewed:
            conditions.append("r.human_decision IS NULL")
        args.append(max(1, min(int(limit or 200), 1000)))
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest_review AS (
                      SELECT DISTINCT ON (clinic_id,tag_code)
                        clinic_id,tag_code,human_decision,reviewer,review_note,reviewed_at
                      FROM provenance.dental_sales_tag_reviews
                      ORDER BY clinic_id,tag_code,reviewed_at DESC,id DESC
                    )
                    SELECT
                      t.clinic_id,c.clinic_name,c.prefecture,c.phone,c.address,
                      COALESCE(NULLIF(c.hp_url,''),NULLIF(c.maps_website_url,''),'') AS hp_url,
                      t.tag_code,t.tag_label,t.priority_group,t.sort_order,t.auto_status,
                      t.confidence,t.source,t.matched_alias,t.evidence_text,t.rule_id,
                      t.classifier_version,
                      r.human_decision,r.reviewer,r.review_note,r.reviewed_at
                    FROM provenance.dental_sales_tags t
                    JOIN public.clinics c ON c.id=t.clinic_id
                    LEFT JOIN latest_review r
                      ON r.clinic_id=t.clinic_id AND r.tag_code=t.tag_code
                    WHERE """ + " AND ".join(conditions) + """
                    ORDER BY
                      CASE WHEN t.auto_status='REVIEW' THEN 0 ELSE 1 END,
                      t.confidence ASC,
                      t.priority_group,t.sort_order,t.clinic_id
                    LIMIT %s
                    """,
                    tuple(args),
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            self._conn.commit()
            return rows
        except Exception:
            self._conn.rollback()
            raise

    def training_stats(self):
        if not self.available():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (clinic_id,tag_code)
                        clinic_id,tag_code,human_decision,auto_snapshot,reviewed_at,id
                      FROM provenance.dental_sales_tag_reviews
                      ORDER BY clinic_id,tag_code,reviewed_at DESC,id DESC
                    )
                    SELECT
                      tag_code,
                      COALESCE(auto_snapshot->>'rule_id','') AS rule_id,
                      count(*) AS reviewed,
                      count(*) FILTER (WHERE human_decision='CONFIRMED') AS confirmed,
                      count(*) FILTER (WHERE human_decision='REJECTED') AS rejected,
                      count(*) FILTER (WHERE human_decision='UNCERTAIN') AS uncertain
                    FROM latest
                    GROUP BY tag_code,COALESCE(auto_snapshot->>'rule_id','')
                    ORDER BY reviewed DESC,tag_code,rule_id
                    """
                )
                rows = [
                    {
                        "tag_code": row[0], "rule_id": row[1], "reviewed": int(row[2]),
                        "confirmed": int(row[3]), "rejected": int(row[4]), "uncertain": int(row[5]),
                    }
                    for row in cur.fetchall()
                ]
            self._conn.commit()
            return rows
        except Exception:
            self._conn.rollback()
            raise
