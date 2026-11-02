"""Fail-closed input and identity helpers for the Supabase Comdesk writer."""
from dataclasses import dataclass
import hashlib
import json
import math

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


@dataclass(frozen=True)
class ComdeskRejection:
    row_number: int
    reason_code: str
    reason_detail: str
    raw_record: list
    normalized_record: dict | None
    project_scope_id: str | None
    source_hash: str


@dataclass
class ComdeskValidationResult:
    mapping: dict
    accepted_rows: list
    rejections: list[ComdeskRejection]


def _json_safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def _source_hash(raw):
    encoded = json.dumps(_json_safe(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_comdesk_writer_import(
    table, mapping=None, *, project_scope_id=None, project_scope_confirmed=False,
):
    """Return accepted rows and row-level rejections without writing to Clinic Master."""
    mapping = dict(mapping or infer_comdesk_columns(table))
    mapping.setdefault("uuid", None)
    mapping.setdefault("comdesk_case_id", None)
    mapping.setdefault("comdesk_project_id", None)

    for field in ("uuid", "comdesk_case_id", "comdesk_project_id"):
        column = mapping.get(field)
        if column is not None and (not isinstance(column, int) or not 0 <= column < len(table.headers)):
            raise ValueError(f"{field}の列指定を確認してください。")

    uuid_col = mapping["uuid"]
    invalid_uuid_mapping = False
    if uuid_col is not None and _key(table.headers[uuid_col]) not in UUID_HEADERS:
        invalid_uuid_mapping = True
        mapping["uuid"] = None

    if mapping["comdesk_case_id"] is None:
        case_hits = [i for i, header in enumerate(table.headers) if _key(header) in CASE_HEADERS]
        mapping["comdesk_case_id"] = case_hits[0] if len(case_hits) == 1 else None
    case_col = mapping["comdesk_case_id"]
    invalid_case_mapping = case_col is not None and _key(table.headers[case_col]) not in CASE_HEADERS
    if invalid_case_mapping:
        mapping["comdesk_case_id"] = None
        case_col = None

    if mapping["comdesk_project_id"] is None:
        project_hits = [i for i, header in enumerate(table.headers) if _key(header) in PROJECT_HEADERS]
        mapping["comdesk_project_id"] = project_hits[0] if len(project_hits) == 1 else None
    project_col = mapping["comdesk_project_id"]
    fixed_scope = str(project_scope_id or "").strip()
    if fixed_scope and not project_scope_confirmed:
        raise ValueError("プロジェクトスコープの管理者確認がありません。HOLD: 取込を開始できません。")
    invalid_project_mapping = project_col is not None and _key(table.headers[project_col]) not in PROJECT_HEADERS
    if invalid_project_mapping:
        mapping["comdesk_project_id"] = None
        project_col = None
    if fixed_scope and (len(fixed_scope) > 256 or any(ord(ch) < 32 for ch in fixed_scope)):
        raise ValueError("プロジェクトスコープの形式を確認してください。")

    prepared = []
    rejections = []
    seen = set()
    for row_number, raw in enumerate(table.data.values.tolist(), start=2):
        raw_record = _json_safe(raw)
        row_errors = []
        record = None
        try:
            record = record_from_row(raw, mapping)
        except (ValueError, AttributeError, IndexError, TypeError) as exc:
            row_errors.append(("INVALID_RECORD_FORMAT", str(exc)))

        if invalid_uuid_mapping:
            row_errors.append((
                "INVALID_UUID_MAPPING",
                "Comdesk UUID列にはUUID列以外を割り当てられません。",
            ))
        if invalid_case_mapping or case_col is None:
            row_errors.append((
                "MISSING_CASE_ID",
                "独立したComdesk案件ID列を特定できません。",
            ))
        if project_col is None and not fixed_scope:
            row_errors.append((
                "MISSING_PROJECT_SCOPE",
                "確認済みプロジェクトスコープ列または固定スコープがありません。",
            ))

        scope = fixed_scope
        case_id = ""
        if record is not None:
            case_id = str(record.get("comdesk_case_id", "") or "").strip()
            scope = str(record.get("comdesk_project_id", "") or fixed_scope).strip()
            if not str(record.get("clinic_name", "") or "").strip():
                row_errors.append(("MISSING_CLINIC_NAME", "医院名が空白です。"))
        if case_col is not None and not case_id:
            row_errors.append(("MISSING_CASE_ID", "案件IDが空白です。"))
        if project_col is not None and not scope:
            row_errors.append(("MISSING_PROJECT_SCOPE", "プロジェクトスコープが空白です。"))
        if case_id and scope and (
            len(case_id) > 256 or len(scope) > 256
            or any(ord(ch) < 32 for ch in case_id + scope)
        ):
            row_errors.append((
                "INVALID_IDENTIFIER_FORMAT",
                "案件IDまたはプロジェクトスコープの形式が不正です。",
            ))
        pair = (scope, case_id)
        if case_id and scope and pair in seen:
            row_errors.append((
                "DUPLICATE_CASE_IN_SCOPE",
                "同一プロジェクト内で案件IDが重複しています。",
            ))
        elif case_id and scope:
            seen.add(pair)

        if row_errors:
            source_hash = _source_hash(raw_record)
            for reason_code, reason_detail in dict.fromkeys(row_errors):
                rejections.append(ComdeskRejection(
                    row_number=row_number,
                    reason_code=reason_code,
                    reason_detail=reason_detail,
                    raw_record=raw_record,
                    normalized_record=_json_safe(record) if record is not None else None,
                    project_scope_id=scope or None,
                    source_hash=source_hash,
                ))
            continue

        record["comdesk_project_id"] = scope
        record["comdesk_case_id"] = case_id
        prepared.append((row_number - 2, raw_record, record))
    return ComdeskValidationResult(mapping, prepared, rejections)


def prepare_comdesk_writer_import(
    table, mapping=None, *, project_scope_id=None, project_scope_confirmed=False,
):
    """Compatibility guard: raise on the first invalid row before any writer access."""
    preflight_mapping = dict(mapping or infer_comdesk_columns(table))
    uuid_col = preflight_mapping.get("uuid")
    if isinstance(uuid_col, int) and 0 <= uuid_col < len(table.headers):
        if _key(table.headers[uuid_col]) not in UUID_HEADERS:
            raise ValueError(
                "Comdesk UUID列に案件IDを割り当てることはできません。HOLD: 取込を停止しました。"
            )
    case_col = preflight_mapping.get("comdesk_case_id")
    if case_col is None:
        case_hits = [i for i, header in enumerate(table.headers) if _key(header) in CASE_HEADERS]
        case_col = case_hits[0] if len(case_hits) == 1 else None
    if (
        case_col is None
        or not isinstance(case_col, int)
        or not 0 <= case_col < len(table.headers)
        or _key(table.headers[case_col]) not in CASE_HEADERS
    ):
        raise ValueError("独立したComdesk案件IDがありません。HOLD: 取込を開始できません。")
    project_col = preflight_mapping.get("comdesk_project_id")
    if project_col is None:
        project_hits = [i for i, header in enumerate(table.headers) if _key(header) in PROJECT_HEADERS]
        project_col = project_hits[0] if len(project_hits) == 1 else None
    if (
        (project_col is None or not isinstance(project_col, int)
         or not 0 <= project_col < len(table.headers))
        and not str(project_scope_id or "").strip()
    ):
        raise ValueError("確認済みプロジェクトスコープがありません。HOLD: 取込を開始できません。")
    result = validate_comdesk_writer_import(
        table, mapping, project_scope_id=project_scope_id,
        project_scope_confirmed=project_scope_confirmed,
    )
    if result.rejections:
        first = result.rejections[0]
        raise ValueError(f"{first.row_number}行目: {first.reason_detail} HOLD: 取込を停止しました。")
    return result.mapping, result.accepted_rows


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
