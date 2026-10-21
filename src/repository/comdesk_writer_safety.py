"""Fail-closed input and identity helpers for the Supabase Comdesk writer."""
from src.master.comdesk import infer_comdesk_columns, record_from_row


def _key(value):
    return "".join(str(value).strip().casefold().replace("_", " ").split())


UUID_HEADERS = {_key(value) for value in ("UUID", "Comdesk UUID", "comdesk_uuid")}
CASE_HEADERS = {_key(value) for value in (
    "案件ID", "ケースID", "管理ID", "リードID", "lead_id", "case_id", "案件番号",
)}
PROJECT_HEADERS = {_key(value) for value in (
    "プロジェクトID", "案件プロジェクトID", "project_id", "project_uuid",
)}


def prepare_comdesk_writer_import(
    table, mapping=None, *, project_scope_id=None, project_scope_confirmed=False,
):
    """Validate distinct source UUID, case ID, and project scope before DB access."""
    supplied_mapping = mapping is not None
    mapping = dict(mapping or infer_comdesk_columns(table))
    mapping.setdefault("uuid", None)
    mapping.setdefault("comdesk_case_id", None)
    mapping.setdefault("comdesk_project_id", None)

    for field in ("uuid", "comdesk_case_id", "comdesk_project_id"):
        column = mapping.get(field)
        if column is not None and (not isinstance(column, int) or not 0 <= column < len(table.headers)):
            raise ValueError(f"{field}の列指定を確認してください。")

    uuid_col = mapping["uuid"]
    if uuid_col is not None and _key(table.headers[uuid_col]) not in UUID_HEADERS:
        if supplied_mapping:
            raise ValueError("Comdesk UUID列に案件IDを割り当てることはできません。HOLD: 取込を停止しました。")
        # Legacy inference treats case IDs as UUIDs. Drop that unsafe guess; the
        # case identifier is resolved independently below and remains provenance.
        mapping["uuid"] = None

    if mapping["comdesk_case_id"] is None:
        case_hits = [i for i, header in enumerate(table.headers) if _key(header) in CASE_HEADERS]
        mapping["comdesk_case_id"] = case_hits[0] if len(case_hits) == 1 else None
    case_col = mapping["comdesk_case_id"]
    if case_col is None or _key(table.headers[case_col]) not in CASE_HEADERS:
        raise ValueError("独立したComdesk案件IDがありません。HOLD: 取込を開始できません。")

    if mapping["comdesk_project_id"] is None:
        project_hits = [i for i, header in enumerate(table.headers) if _key(header) in PROJECT_HEADERS]
        mapping["comdesk_project_id"] = project_hits[0] if len(project_hits) == 1 else None
    project_col = mapping["comdesk_project_id"]
    fixed_scope = str(project_scope_id or "").strip()
    if fixed_scope and not project_scope_confirmed:
        raise ValueError("プロジェクトスコープの管理者確認がありません。HOLD: 取込を開始できません。")
    if project_col is not None and _key(table.headers[project_col]) not in PROJECT_HEADERS:
        raise ValueError("確認済みプロジェクトスコープ列を指定してください。")
    if project_col is None and not fixed_scope:
        raise ValueError("確認済みプロジェクトスコープがありません。HOLD: 取込を開始できません。")
    if fixed_scope and (len(fixed_scope) > 256 or any(ord(ch) < 32 for ch in fixed_scope)):
        raise ValueError("プロジェクトスコープの形式を確認してください。")

    prepared = []
    seen = set()
    for row_number, raw in enumerate(table.data.values.tolist(), start=2):
        record = record_from_row(raw, mapping)
        case_id = str(record.get("comdesk_case_id", "")).strip()
        scope = str(record.get("comdesk_project_id", "") or fixed_scope).strip()
        if not case_id or not scope:
            raise ValueError(f"{row_number}行目: 案件IDまたはプロジェクトスコープがありません。HOLD: 取込を停止しました。")
        if len(case_id) > 256 or len(scope) > 256 or any(ord(ch) < 32 for ch in case_id + scope):
            raise ValueError(f"{row_number}行目: 案件IDまたはプロジェクトスコープの形式が不正です。")
        pair = (scope, case_id)
        if pair in seen:
            raise ValueError(f"{row_number}行目: 同一プロジェクトの案件IDが重複しています。HOLD: 取込を停止しました。")
        seen.add(pair)
        record["comdesk_project_id"] = scope
        record["comdesk_case_id"] = case_id
        prepared.append((row_number - 2, raw, record))
    return mapping, prepared


def comdesk_match_record(record):
    """Keep Comdesk UUID out of clinics.uuid matching while retaining provenance."""
    return {**record, "uuid": ""}


def clinic_master_fields(record):
    """Fields a Comdesk source may supplement; identity/contact IDs are excluded."""
    blocked = {"uuid", "comdesk_case_id", "comdesk_project_id", "hp_candidate_url", "hp_rank", "email", "email_address"}
    return {key: value for key, value in record.items() if key not in blocked}


def validate_comdesk_review_resolution(record, *, target_id, existing_clinic_id):
    """Only resolve a scoped Comdesk HOLD to an already existing Clinic Master row."""
    if not str(record.get("comdesk_case_id", "")).strip() or not str(record.get("comdesk_project_id", "")).strip():
        raise ValueError("案件IDまたはプロジェクトスコープが未確認です。HOLDを解除できません。")
    if target_id is None and existing_clinic_id is None:
        raise ValueError("Comdesk UUIDからClinic UUIDを発行・登録できません。Clinic Masterへの追加は別途確認してください。")


def reject_comdesk_clinic_creation(record):
    """Comdesk source records cannot be used as canonical new-clinic payloads."""
    if "comdesk_case_id" in record or "comdesk_project_id" in record:
        raise ValueError("Comdesk案件からClinic Master医院を自動登録できません。HOLDを維持してください。")
