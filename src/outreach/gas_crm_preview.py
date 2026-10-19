"""Pure read-only GAS/CRM preflight. No IO beyond in-memory CSV."""
from __future__ import annotations
import csv
from dataclasses import dataclass
from io import StringIO
from typing import Mapping, Sequence
from src.outreach.gas_sheet_bridge import Decision, UUID_PATTERN, plan_registration

CANDIDATE_COLUMNS = ("clinic_uuid", "clinic_name", "campaign_id", "recipient_email", "template_kind", "gmail_draft_id")
GAS_HISTORY_COLUMNS = ("operation_key", "clinic_uuid", "gas_case_id", "gmail_draft_id")
CRM_MAPPING_COLUMNS = ("organization_id", "clinic_uuid", "crm_company_id")
MAX_BYTES = 3_000_000
MAX_ROWS = 5_000

class PreviewInputError(ValueError):
    pass

@dataclass(frozen=True)
class CasePreview:
    clinic_uuid: str
    clinic_name: str
    campaign_id: str
    operation_key: str
    gas_status: str
    crm_status: str
    reasons: tuple[str, ...]

def read_csv_snapshot(raw: bytes, *, required_columns: Sequence[str]) -> list[dict[str, str]]:
    """Reject incomplete or oversized snapshots without persisting them."""
    if not raw or len(raw) > MAX_BYTES:
        raise PreviewInputError("CSV is empty or exceeds 3 MB")
    try:
        body = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise PreviewInputError("UTF-8 CSV required") from exc
    reader = csv.DictReader(StringIO(body, newline=""))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or any(not h or h.strip() != h for h in headers):
        raise PreviewInputError("Invalid or duplicate CSV headers")
    missing = set(required_columns) - set(headers)
    if missing:
        raise PreviewInputError("Missing columns: " + ", ".join(sorted(missing)))
    rows = []
    for row in reader:
        if len(rows) == MAX_ROWS:
            raise PreviewInputError("CSV exceeds 5,000 rows")
        if None in row:
            raise PreviewInputError("CSV row has extra fields")
        rows.append({k: (v or "").strip() for k, v in row.items()})
    return rows

def _crm_index(rows: Sequence[Mapping[str, str]]) -> tuple[dict[str, set[tuple[str, str]]], set[str]]:
    by_clinic: dict[str, set[tuple[str, str]]] = {}
    by_company: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        clinic = (row.get("clinic_uuid") or "").lower().strip()
        org = (row.get("organization_id") or "").lower().strip()
        company = (row.get("crm_company_id") or "").lower().strip()
        if not all(UUID_PATTERN.fullmatch(v) for v in (clinic, org, company)):
            continue
        by_clinic.setdefault(clinic, set()).add((org, company))
        by_company.setdefault((org, company), set()).add(clinic)
    conflicts = {c for clinics in by_company.values() if len(clinics) > 1 for c in clinics}
    return by_clinic, conflicts

def preview_cases(candidates: Sequence[Mapping[str, str]],
                  gas_history: Sequence[Mapping[str, str]],
                  crm_mappings: Sequence[Mapping[str, str]], *,
                  history_verified: bool = False) -> list[CasePreview]:
    """Only a PREVIEW. Never authorizes external writes or sends."""
    existing_keys = {(r.get("operation_key") or "").strip() for r in gas_history}
    existing_drafts = {(r.get("gmail_draft_id") or "").strip() for r in gas_history if r.get("gmail_draft_id")}
    crm_by_clinic, crm_conflicts = _crm_index(crm_mappings)
    seen_keys: set[str] = set()
    seen_drafts: set[str] = set()
    results = []
    for row in candidates:
        clinic = (row.get("clinic_uuid") or "").strip().lower()
        name = (row.get("clinic_name") or "").strip()
        campaign = (row.get("campaign_id") or "").strip()
        draft = (row.get("gmail_draft_id") or "").strip()
        decision = plan_registration(
            clinic_uuid=clinic, campaign_id=campaign,
            recipient_email=(row.get("recipient_email") or "").strip(),
            template_kind=(row.get("template_kind") or "").strip(),
            existing_keys=list(existing_keys), history_verified=history_verified,
        )
        status = {Decision.MATCHED:"PREVIEW_OK", Decision.REVIEW:"REVIEW", Decision.BLOCKED:"HOLD"}[decision.decision]
        reasons = [decision.reason] if decision.reason else []
        if not name:
            status = "HOLD"
            reasons.append("Missing clinic name")
        if not draft:
            status = "HOLD"
            reasons.append("Existing Gmail Draft ID required")
        elif draft in existing_drafts or draft in seen_drafts:
            status = "REVIEW"
            reasons.append("Duplicate Draft ID; no new registration")
        if draft:
            seen_drafts.add(draft)
        if decision.operation_key:
            if decision.operation_key in seen_keys:
                status = "REVIEW"
                reasons.append("Duplicate operation key in candidates")
            seen_keys.add(decision.operation_key)
        if not history_verified:
            status = "HOLD"
        assoc = crm_by_clinic.get(clinic, set())
        if clinic in crm_conflicts or len(assoc) > 1:
            crm_status = "REVIEW"
            reasons.append("Conflicting CRM company mapping")
        elif not assoc:
            crm_status = "HOLD"
            reasons.append("CRM organization/company mapping unverified")
        else:
            crm_status = "MAPPED"
        results.append(CasePreview(clinic, name, campaign, decision.operation_key,
                                   status, crm_status, tuple(reasons)))
    return results

def to_safe_csv(results: Sequence[CasePreview]) -> bytes:
    """Export an audit without email/Draft IDs; protect spreadsheet formulas."""
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(("clinic_uuid", "clinic_name", "campaign_id", "operation_key", "gas_status", "crm_status", "reasons"))
    def safe(v: str) -> str:
        return "'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v
    for r in results:
        writer.writerow(tuple(safe(x) for x in (r.clinic_uuid, r.clinic_name,
                        r.campaign_id, r.operation_key, r.gas_status,
                        r.crm_status, " | ".join(r.reasons))))
    return out.getvalue().encode("utf-8-sig")
