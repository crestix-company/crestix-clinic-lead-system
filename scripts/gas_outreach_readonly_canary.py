"""GAS連携 READ ONLY 接続・ID解決Canary（Dry-run、書込/Gmail操作なし）。

Clinic Master（CLINIC_READONLY_DB_URLのREAD ONLY専用ロール）・Email Research
（EMAIL_RESEARCH_DB_URL経由、src.outreach.gas_email_lookup）・既存Gmail配信台帳
（outreach.email_delivery_records、SELECTのみ）の3接続を実データに対して検証し、
Clinic ID解決件数・PRIMARYメール取得件数・GASペイロード完成件数・既存Gmail
下書き/送信済みによる除外件数を集計する。

Clinic ID（public.clinics.id、常時付与）のみを識別子として使用する。Comdesk UUID
（src.master.comdesk、Comdeskへの取込が完了した医院のみ付与される外部ID）は
このCanaryでは一切の判定条件に使用しない -- 未付与であることを理由に対象から
除外することは絶対に行わない。

DB書込・Migration・ロール変更・Gmail下書き作成・送信は一切行わない。出力は
--out ディレクトリの gas_canary_report.json のみ（既存の解析CSVには一切触れない）。

  python scripts/gas_outreach_readonly_canary.py --limit 50 --out artifacts/gas_canary_50
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.outreach.gas_email_lookup import GasEmailLookupError, lookup_primary_emails  # noqa: E402
from src.outreach.readonly_access import ReadOnlyAccessError, open_readonly_clinic_repository  # noqa: E402
from src.repository.outreach_delivery_repository import (  # noqa: E402
    BLOCKING_STATUSES, OutreachDatabaseError, OutreachDeliveryRepository,
)

EXIT_PERMISSION = 2


def stop(code, message):
    print(f"[STOP] {message}", file=sys.stderr)
    return code


def _fetch_clinic_master_records(args, connect_fn):
    repository, conn = open_readonly_clinic_repository(connect_fn=connect_fn)
    try:
        from src.master.filters import Filters
        from src.master.scope import SCOPE_ALL
        return repository.query(Filters(
            active_only=True, hp_only=False, prefectures=args.prefecture, municipalities=args.municipality,
            departments=args.department, effective_ranks=args.rank, scope=SCOPE_ALL), args.scan)
    finally:
        conn.close()


def main(argv=None, *, clinic_connect_fn=None, outreach_connect_fn=None, email_build_repositories=None,
        fetch_records=None, open_outreach_repository=None):
    p = argparse.ArgumentParser()
    p.add_argument("--prefecture", action="append", default=[])
    p.add_argument("--municipality", action="append", default=[])
    p.add_argument("--department", action="append", default=[])
    p.add_argument("--rank", action="append", default=[])
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--scan", type=int, default=300)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    # --- Clinic Master: READ ONLY (role-verified) ---
    try:
        records = (fetch_records or _fetch_clinic_master_records)(args, clinic_connect_fn)
    except ReadOnlyAccessError as exc:
        return stop(EXIT_PERMISSION, str(exc))

    candidates = records[: max(0, args.limit)]
    clinic_id_resolved = [{"clinic_id": int(r.get("id") or 0), "clinic_uuid": str(r.get("uuid") or "").strip()}
                          for r in candidates if int(r.get("id") or 0) > 0]
    skipped_no_clinic_id = len(candidates) - len(clinic_id_resolved)
    clinic_ids = [row["clinic_id"] for row in clinic_id_resolved]

    # --- Email Research: READ ONLY (EMAIL_RESEARCH_DB_URL via gas_email_lookup) ---
    try:
        emails = lookup_primary_emails(clinic_ids, build_repositories=email_build_repositories)
    except GasEmailLookupError as exc:
        return stop(EXIT_PERMISSION, str(exc))

    email_missing_reasons: dict[str, int] = {}
    primary_email_found = 0
    for result in emails.values():
        if result.found:
            primary_email_found += 1
        else:
            email_missing_reasons[result.reason] = email_missing_reasons.get(result.reason, 0) + 1

    # --- Existing Gmail delivery ledger: READ ONLY (SELECT-only methods only) ---
    gmail_excluded_clinic_ids: set[int] = set()
    gmail_status_counts: dict[str, int] = {}
    try:
        if open_outreach_repository is not None:
            outreach_repo = open_outreach_repository()
        else:
            outreach_repo = OutreachDeliveryRepository.connect(connect_fn=outreach_connect_fn)
    except OutreachDatabaseError as exc:
        return stop(EXIT_PERMISSION, str(exc))
    try:
        statuses_by_clinic = outreach_repo.clinic_statuses(clinic_ids)
    finally:
        outreach_repo.close()
    for clinic_id, rows in statuses_by_clinic.items():
        for row in rows:
            status = str(row["status"])
            gmail_status_counts[status] = gmail_status_counts.get(status, 0) + 1
            if status in BLOCKING_STATUSES:
                gmail_excluded_clinic_ids.add(clinic_id)

    # --- GAS payload readiness: clinic_id + clinic_uuid + email, Comdesk UUID NEVER required ---
    gas_payload_ready = 0
    for row in clinic_id_resolved:
        clinic_id = row["clinic_id"]
        has_uuid = bool(row["clinic_uuid"])
        has_email = emails.get(clinic_id) is not None and emails[clinic_id].found
        excluded_by_gmail = clinic_id in gmail_excluded_clinic_ids
        if has_uuid and has_email and not excluded_by_gmail:
            gas_payload_ready += 1

    report = {
        "scanned": len(records),
        "candidates": len(candidates),
        "clinic_id_resolved": len(clinic_id_resolved),
        "skipped_no_clinic_id": skipped_no_clinic_id,
        "primary_email_found": primary_email_found,
        "email_missing_reasons": email_missing_reasons,
        "gmail_excluded_count": len(gmail_excluded_clinic_ids),
        "gmail_status_counts": gmail_status_counts,
        "gas_payload_ready": gas_payload_ready,
        "comdesk_uuid_used_as_filter": False,
        "writes_performed": False,
        "gmail_drafts_created": 0,
        "gmail_messages_sent": 0,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "gas_canary_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
