"""Ten-row local-only GAS/Gmail integration smoke using the isolated Phase J DB.

Live Sheets, GAS, Gmail, Supabase, and Cloud Run transports are never loaded here.
Real rows are read from the local Phase J snapshot and held when campaign evidence
is absent. Synthetic fixtures are clearly marked and use ``example.invalid``.
"""
from __future__ import annotations

from collections import Counter
import json
import re
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from src.outreach.gas_bridge_client import GasBridgeClient, GasCaseRegistrationRequest, GasCaseStatus
from src.outreach.gas_sheet_bridge import Decision, plan_registration, registration_key
from src.outreach.local_mock_draft_transport import LocalMockDraftTransport
from src.outreach.phase_j_local_db import connect_verified

RUN_ID = "gas-outreach-local-10-v1"
MAX_CASES = 10
REAL_CASE_LIMIT = 6
_FIXTURES = (
    {
        "gas_case_id": "FIXTURE-GAS-1001", "clinic_id": "FIXTURE-CLINIC-1001",
        "clinic_uuid": "a1234567-e89b-42d3-a456-426614174001",
        "clinic_name": "GAS連携テスト医院A", "recipient_email": "clinic-a@example.invalid",
        "campaign_id": "FIXTURE-CAMPAIGN-202610",
    },
    {
        "gas_case_id": "FIXTURE-GAS-1002", "clinic_id": "FIXTURE-CLINIC-1002",
        "clinic_uuid": "a1234567-e89b-42d3-a456-426614174001",
        "clinic_name": "GAS連携テスト医院A（重複検証）", "recipient_email": "clinic-a@example.invalid",
        "campaign_id": "FIXTURE-CAMPAIGN-202610",
    },
    {
        "gas_case_id": "FIXTURE-GAS-1003", "clinic_id": "FIXTURE-CLINIC-1003",
        "clinic_uuid": "b1234567-e89b-42d3-a456-426614174002",
        "clinic_name": "GAS連携テスト医院B", "recipient_email": "clinic-b@example.invalid",
        "campaign_id": "FIXTURE-CAMPAIGN-202610-B",
    },
    {
        "gas_case_id": "FIXTURE-GAS-1004", "clinic_id": "FIXTURE-CLINIC-1004",
        "clinic_uuid": "c1234567-e89b-42d3-a456-426614174003",
        "clinic_name": "GAS連携テスト医院C", "recipient_email": "clinic-c@example.invalid",
        "campaign_id": "",
    },
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


def _operation_key(case: Mapping[str, Any]) -> str:
    return registration_key(
        clinic_uuid=str(case.get("clinic_uuid") or ""),
        campaign_id=str(case.get("campaign_id") or ""),
        recipient_email=str(case.get("recipient_email") or ""),
        template_kind="LISTED",
    )


def _ensure_result_table() -> None:
    # This schema exists only in the verified Phase J Unix-socket database.
    with connect_verified() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS local_gas_test")
        conn.execute("REVOKE ALL ON SCHEMA local_gas_test FROM PUBLIC")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS local_gas_test.validation_results (
                run_id text NOT NULL,
                case_key text NOT NULL,
                source_kind text NOT NULL CHECK (source_kind IN ('LOCAL_SNAPSHOT','FIXTURE')),
                gas_case_id text NOT NULL DEFAULT '',
                clinic_id text NOT NULL DEFAULT '',
                clinic_uuid text NOT NULL DEFAULT '',
                campaign_id text NOT NULL DEFAULT '',
                result_status text NOT NULL CHECK (result_status IN ('MATCHED','HOLD','ERROR')),
                reason text NOT NULL DEFAULT '',
                operation_key text NOT NULL DEFAULT '',
                gas_result_status text NOT NULL DEFAULT '',
                mock_registered_case_id text NOT NULL DEFAULT '',
                mock_draft_id text NOT NULL DEFAULT '',
                draft_preview jsonb,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (run_id, case_key)
            )
        """)
        conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA local_gas_test FROM PUBLIC")


def _source_cases() -> list[dict[str, Any]]:
    with connect_verified() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute("""
            SELECT external_case_id, clinic_id::text, clinic_uuid, campaign_id, hold_reason
            FROM outreach.sales_cases
            WHERE coalesce(external_case_id, '') <> ''
              AND coalesce(clinic_uuid, '') <> ''
              AND coalesce(campaign_id, '') = ''
            ORDER BY external_case_id
            LIMIT %s
        """, (REAL_CASE_LIMIT,)).fetchall()
    return [
        {
            "case_key": "snapshot:" + str(row[0]), "source_kind": "LOCAL_SNAPSHOT",
            "gas_case_id": str(row[0]), "clinic_id": str(row[1]), "clinic_uuid": str(row[2]),
            "campaign_id": str(row[3] or ""), "clinic_name": "", "recipient_email": "",
            "source_hold_reason": str(row[4] or ""),
        }
        for row in rows
    ]


def _cases() -> list[dict[str, Any]]:
    cases = _source_cases()
    for item in _FIXTURES[: MAX_CASES - len(cases)]:
        fixture = dict(item)
        fixture.update({"case_key": "fixture:" + fixture["gas_case_id"], "source_kind": "FIXTURE",
                        "source_hold_reason": ""})
        cases.append(fixture)
    return cases[:MAX_CASES]


def _previous_results() -> list[dict[str, Any]]:
    with connect_verified() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute("""
            SELECT case_key, source_kind, gas_case_id, clinic_id, clinic_uuid, campaign_id,
                   result_status, reason, operation_key, gas_result_status,
                   mock_registered_case_id, mock_draft_id, draft_preview
            FROM local_gas_test.validation_results WHERE run_id=%s
        """, (RUN_ID,)).fetchall()
    return [dict(zip((
        "case_key", "source_kind", "gas_case_id", "clinic_id", "clinic_uuid", "campaign_id",
        "result_status", "reason", "operation_key", "gas_result_status",
        "mock_registered_case_id", "mock_draft_id", "draft_preview",
    ), row)) for row in rows]


def _save_results(results: list[dict[str, Any]]) -> None:
    with connect_verified() as conn:
        with conn.cursor() as cursor:
            cursor.executemany("""
            INSERT INTO local_gas_test.validation_results
              (run_id,case_key,source_kind,gas_case_id,clinic_id,clinic_uuid,campaign_id,
               result_status,reason,operation_key,gas_result_status,mock_registered_case_id,
               mock_draft_id,draft_preview,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
            ON CONFLICT (run_id,case_key) DO UPDATE SET
              source_kind=excluded.source_kind, gas_case_id=excluded.gas_case_id,
              clinic_id=excluded.clinic_id, clinic_uuid=excluded.clinic_uuid,
              campaign_id=excluded.campaign_id, result_status=excluded.result_status,
              reason=excluded.reason, operation_key=excluded.operation_key,
              gas_result_status=excluded.gas_result_status,
              mock_registered_case_id=excluded.mock_registered_case_id,
              mock_draft_id=excluded.mock_draft_id, draft_preview=excluded.draft_preview,
              updated_at=now()
            """, [(
                RUN_ID, row["case_key"], row["source_kind"], row["gas_case_id"], row["clinic_id"],
                row["clinic_uuid"], row["campaign_id"], row["result_status"], row["reason"],
                row["operation_key"], row["gas_result_status"], row["mock_registered_case_id"],
                row["mock_draft_id"], Jsonb(row["draft_preview"]) if row["draft_preview"] else None,
            ) for row in results])


def _run_once(cases: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mock_records: dict[str, Any] = {"by_operation": {}, "by_scope": {}}
    gas_records: dict[str, dict[str, str]] = {}
    prior_case_by_operation: dict[str, str] = {}
    for row in previous:
        if row["result_status"] != "MATCHED" or not row["operation_key"]:
            continue
        prior_case_by_operation[row["operation_key"]] = row["gas_case_id"]
        if row["mock_draft_id"]:
            draft = {
                "status": "DRAFT", "reason": "LOCAL MOCK: Gmailへ接続していません",
                "draftId": row["mock_draft_id"],
                "messageId": "mock-message-" + row["mock_draft_id"].removeprefix("mock-draft-"),
                "threadId": "mock-thread-" + row["mock_draft_id"].removeprefix("mock-draft-"),
                "correlationId": "local-mock", "operationKey": row["operation_key"],
            }
            mock_records["by_operation"][row["operation_key"]] = draft
            mock_records["by_scope"][f"{row['clinic_uuid']}:{row['campaign_id']}"] = row["operation_key"]
        if row["mock_registered_case_id"]:
            gas_records[row["operation_key"]] = {
                "caseId": row["mock_registered_case_id"], "draftId": row["mock_draft_id"],
                "threadId": "mock-thread-" + row["mock_draft_id"].removeprefix("mock-draft-"),
            }

    draft_transport = LocalMockDraftTransport(mock_records)

    def gas_transport(payload: dict[str, Any], *, timeout_seconds: float) -> tuple[int, str]:
        key = str(payload.get("operationKey") or "")
        previous_gas = gas_records.get(key)
        if previous_gas:
            response = {"status": "ALREADY_REGISTERED", **previous_gas}
        else:
            response = {
                "status": "REGISTERED", "caseId": "mock-gas-" + key[:12],
                "draftId": str(payload.get("existingDraft", {}).get("id") or ""),
                "threadId": "mock-thread-" + str(payload.get("existingDraft", {}).get("id") or ""),
            }
            gas_records[key] = {k: response[k] for k in ("caseId", "draftId", "threadId")}
        return 200, json.dumps(response, ensure_ascii=False)

    gas_client = GasBridgeClient(gas_transport)
    results: list[dict[str, Any]] = []
    seen_source_cases: set[str] = set()
    for case in cases:
        row = {
            "case_key": case["case_key"], "source_kind": case["source_kind"],
            "gas_case_id": str(case.get("gas_case_id") or ""), "clinic_id": str(case.get("clinic_id") or ""),
            "clinic_uuid": str(case.get("clinic_uuid") or ""), "campaign_id": str(case.get("campaign_id") or ""),
            "result_status": "HOLD", "reason": "", "operation_key": "",
            "gas_result_status": "", "mock_registered_case_id": "", "mock_draft_id": "",
            "draft_preview": None,
        }
        try:
            if case["source_kind"] == "LOCAL_SNAPSHOT":
                row["reason"] = case.get("source_hold_reason") or "ローカル原票にキャンペーンIDがありません"
                results.append(row)
                continue
            if not row["gas_case_id"] or not _UUID_RE.fullmatch(row["clinic_uuid"]):
                row["reason"] = "fixtureの案件IDまたはClinic UUIDが不正です"
                results.append(row)
                continue
            if not row["campaign_id"]:
                row["reason"] = "fixtureにキャンペーンIDがありません"
                results.append(row)
                continue
            if row["gas_case_id"] in seen_source_cases:
                row["reason"] = "同一GAS案件IDがテスト内で重複しています"
                results.append(row)
                continue
            seen_source_cases.add(row["gas_case_id"])
            plan = plan_registration(
                clinic_uuid=row["clinic_uuid"], campaign_id=row["campaign_id"],
                recipient_email=str(case["recipient_email"]), template_kind="LISTED",
                existing_keys=tuple(prior_case_by_operation), history_verified=True,
                legacy_case_id=row["gas_case_id"],
            )
            if plan.decision == Decision.BLOCKED:
                row["reason"] = plan.reason
                results.append(row)
                continue
            if plan.decision == Decision.REVIEW and prior_case_by_operation.get(plan.operation_key) != row["gas_case_id"]:
                row["operation_key"] = plan.operation_key
                row["reason"] = "同一医院・Campaign ID・宛先・テンプレートのMock登録があり、別案件としての重複をHOLDしました"
                results.append(row)
                continue
            row["operation_key"] = plan.operation_key
            draft_response = draft_transport(
                "mock://local-draft", json={
                    "operationKey": row["operation_key"], "clinicUuid": row["clinic_uuid"],
                    "campaignId": row["campaign_id"],
                }, headers={"Authorization": "Bearer local-mock-only"}, timeout=1.0,
            )
            draft = draft_response.json()
            if draft.get("status") == "HOLD":
                row["reason"] = str(draft.get("reason") or "同一医院・キャンペーンのMock Draft重複")
                results.append(row)
                continue
            if draft_response.status_code != 200 or draft.get("status") != "DRAFT":
                row["result_status"] = "ERROR"
                row["reason"] = "ローカルMock Draftの応答が不正です"
                results.append(row)
                continue
            row["mock_draft_id"] = str(draft.get("draftId") or "")
            request = GasCaseRegistrationRequest(
                operation_key=row["operation_key"], clinic_uuid=row["clinic_uuid"],
                clinic_name=str(case["clinic_name"]), recipient_email=str(case["recipient_email"]),
                existing_draft_id=row["mock_draft_id"], clinic_id=row["clinic_id"],
            )
            gas_result = gas_client.register_case(request)
            row["gas_result_status"] = gas_result.status.value
            row["mock_registered_case_id"] = gas_result.case_id
            if gas_result.is_success:
                row["result_status"] = "MATCHED"
                row["reason"] = "fixture照合済み。GAS/GmailはいずれもLOCAL MOCKのみ"
                row["draft_preview"] = {
                    "mode": "LOCAL MOCK ONLY — NOT SENT",
                    "to": str(case["recipient_email"]),
                    "subject": f"[TEST] {case['campaign_id']} / {case['clinic_name']}",
                    "body": f"これは合成fixtureのプレビューです。案件ID: {case['gas_case_id']}。送信されません。",
                }
                prior_case_by_operation[row["operation_key"]] = row["gas_case_id"]
            elif gas_result.status in {GasCaseStatus.TRANSPORT_ERROR, GasCaseStatus.INVALID_RESPONSE}:
                row["result_status"] = "ERROR"
                row["reason"] = gas_result.reason
            else:
                row["reason"] = gas_result.reason or gas_result.status.value
        except Exception as exc:  # per-row isolation keeps a failed case visible
            row["result_status"] = "ERROR"
            row["reason"] = f"ローカル処理エラー: {type(exc).__name__}"
        results.append(row)
    return results[:MAX_CASES]


def run_local_gas_validation() -> dict[str, Any]:
    """Run the same ten-row operation twice and persist via unique-key upserts."""
    _ensure_result_table()
    cases = _cases()
    first = _run_once(cases, _previous_results())
    _save_results(first)
    second = _run_once(cases, _previous_results())
    _save_results(second)
    summary = Counter(row["result_status"] for row in second)
    stable_fields = ("case_key", "result_status", "operation_key",
                     "mock_registered_case_id", "mock_draft_id")
    repeat_idempotent = len(first) == len(second) and all(
        tuple(a.get(key) for key in stable_fields) == tuple(b.get(key) for key in stable_fields)
        for a, b in zip(first, second)
    )
    return {
        "run_id": RUN_ID, "rows": second, "count": len(second),
        "matched": summary["MATCHED"], "hold": summary["HOLD"], "error": summary["ERROR"],
        "already_registered": sum(row["gas_result_status"] == "ALREADY_REGISTERED" for row in second),
        "repeat_idempotent": repeat_idempotent,
    }


def load_local_gas_validation_results() -> list[dict[str, Any]]:
    """Read saved test-only results from the verified local Phase J database."""
    with connect_verified() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        exists = conn.execute("""
            SELECT to_regclass('local_gas_test.validation_results') IS NOT NULL
        """).fetchone()[0]
        if not exists:
            return []
        rows = conn.execute("""
            SELECT case_key,source_kind,gas_case_id,clinic_id,clinic_uuid,campaign_id,
                   result_status,reason,gas_result_status,mock_registered_case_id,mock_draft_id,draft_preview
            FROM local_gas_test.validation_results WHERE run_id=%s ORDER BY case_key
        """, (RUN_ID,)).fetchall()
    keys = ("case_key", "source_kind", "gas_case_id", "clinic_id", "clinic_uuid", "campaign_id",
            "result_status", "reason", "gas_result_status", "mock_registered_case_id", "mock_draft_id", "draft_preview")
    return [dict(zip(keys, row)) for row in rows]
