"""GAS連携 READ ONLY 接続・ID解決Canary（Dry-run、書込/Gmail操作なし）。

Clinic Master（CLINIC_READONLY_DB_URLのREAD ONLY専用ロール）・Email Research
（EMAIL_RESEARCH_DB_URL経由、src.outreach.gas_email_lookup）・既存Gmail配信台帳
（outreach.email_delivery_records、SELECTのみ）の3接続を実データに対して検証し、
Clinic ID解決件数・PRIMARYメール取得件数・GASペイロード完成件数・既存Gmail
下書き/送信済みによる除外件数を集計する。

Clinic ID（public.clinics.id、常時付与）とComdesk UUID（public.clinics.uuid、
src.master.comdeskのCSV取込時にComdeskの案件ID/管理ID/リードID列から複写される
外部ID。新規にUUIDを生成するコードは存在せず、Comdesk未取込の医院では常に空文字）
は別の識別子であり、用途も異なる。

- email_ready（Clinic ID基準、Comdesk UUID不要）: Primary Emailの取得・GASへの
  送信対象医院の集計には、内部Clinic IDとEmail Research結果があれば十分。
  Comdesk UUIDが空の医院（Comdesk未取込の新規医院）を理由だけで除外しない。
- gas_case_registration_ready（Comdesk UUID必須、現行契約を維持）:
  ~/ClinicLeadGASIntegration/bridge/docs/python_integration_contract.md と
  clinic_lead_gas_bridge.js（clinicLeadBridgeValidateRequest_、22/22テスト済み・
  本番未デプロイ）は双方ともclinicUuidを必須としており、src/outreach/
  gas_bridge_client.pyもこれに追随している。この契約はGAS側コード（別リポジトリ）
  との合意事項であり、Python側だけを一方的に緩めると実デプロイ後にGASがREJECTED
  を返す可能性があるため変更していない。Comdesk UUID未発行の医院をGASケースとして
  登録するには、Python/GAS双方の契約変更が別途必要（本Canaryのdocstring末尾の
  提案を参照）。

DB書込・Migration・ロール変更・Gmail下書き作成・送信は一切行わない。出力は
--out ディレクトリの gas_canary_report.json のみ（既存の解析CSVには一切触れない）。

将来の仕様変更案（未実装・Python/GAS双方の合意が別途必要）:
Comdesk未取込の新規医院をGASケースとして登録できるようにするには、
clinic_lead_gas_bridge.js の clinicLeadBridgeValidateRequest_ と
gas_bridge_client.py の validate_request() の双方で、
「clinicUuidがUUID形式」または「clinicUuidが空でclinicIdが有効な正の整数」の
いずれかを許容するよう同時に変更する必要がある（既存のclinicUuid必須パスは
変更しない、完全に追加的な変更）。GASはclinicId基準で新規ケースを作成し、
その結果Comdeskへ案件登録された後にComdesk UUIDが付与される流れを想定する
（UUIDの発行・書込はこのリポジトリ側では一切行わない）。

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

    # --- Readiness: two distinct metrics, deliberately not conflated ---
    # email_ready: Clinic ID + Primary Email + not Gmail-excluded. Comdesk UUID
    # (clinic_uuid) is informational only and never gates this -- a clinic with no
    # Comdesk UUID yet (never imported via src.master.comdesk) is just as eligible
    # for a GAS Email payload as one that has one.
    # gas_case_registration_ready: the subset that can ALSO be sent through
    # gas_bridge_client.GasCaseRegistrationRequest today, which still requires a
    # verified clinic_uuid -- that requirement lives in the cross-repo contract
    # (~/ClinicLeadGASIntegration/bridge, clinicLeadBridgeValidateRequest_) and is
    # intentionally left alone here (see module docstring's proposal).
    email_ready = 0
    gas_case_registration_ready = 0
    comdesk_uuid_missing_count = 0
    for row in clinic_id_resolved:
        clinic_id = row["clinic_id"]
        has_uuid = bool(row["clinic_uuid"])
        if not has_uuid:
            comdesk_uuid_missing_count += 1
        has_email = emails.get(clinic_id) is not None and emails[clinic_id].found
        excluded_by_gmail = clinic_id in gmail_excluded_clinic_ids
        if has_email and not excluded_by_gmail:
            email_ready += 1
            if has_uuid:
                gas_case_registration_ready += 1

    report = {
        "scanned": len(records),
        "candidates": len(candidates),
        "clinic_id_resolved": len(clinic_id_resolved),
        "skipped_no_clinic_id": skipped_no_clinic_id,
        "comdesk_uuid_missing_count": comdesk_uuid_missing_count,
        "primary_email_found": primary_email_found,
        "email_missing_reasons": email_missing_reasons,
        "gmail_excluded_count": len(gmail_excluded_clinic_ids),
        "gmail_status_counts": gmail_status_counts,
        "email_ready": email_ready,
        "gas_case_registration_ready": gas_case_registration_ready,
        "comdesk_uuid_used_as_filter_for_email_ready": False,
        "comdesk_uuid_required_for_gas_case_registration": True,
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
