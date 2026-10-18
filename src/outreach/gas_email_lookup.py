"""Primary Email lookup for the GAS integration, keyed strictly by internal Clinic ID.

Background (see docs/outreach_gas_sheet_bridge_phase1.md and
src/outreach/email_readonly_access.py): that module names a dedicated,
least-privilege ``EMAIL_READONLY_DB_URL`` role for Email Research reads. That role
has never been provisioned in Supabase and the variable is wired into no
``config/email_research.env*`` file and no ``scripts/launch_v2.py`` allowlist entry
existed before this change, so any caller depending on it always fails closed with
"EMAIL_READONLY_DB_URL が未設定です" -- this was the confirmed cause of the GAS
Email Research READ ONLY connection failure. ``EMAIL_READONLY_DB_URL`` is now a
documented, allowlisted, optional *stricter* path (see email_research.env.example);
until Supabase operations provisions that role, this module uses the same
``EMAIL_RESEARCH_DB_URL`` connection that ``scripts/outreach_mail_canary.py`` and
``build_email_repositories()`` already rely on successfully in production. The
connection is not role-verified read-only the way Clinic Master's
``CLINIC_READONLY_DB_URL`` is, but this module issues only SELECT statements and
implements no write method.

Clinic ID (``public.clinics.id``, bigint) is the only identifier this module
accepts. ``public.clinics.uuid`` -- the Comdesk UUID -- is copied in only by
``src.master.comdesk``'s CSV import (from Comdesk's own case/lead-id column; no
code anywhere generates one) and is therefore empty for any clinic never matched
into Comdesk. It is an external, often-empty identifier unrelated to Clinic
Master or Email Research row addressing, and must never gate this lookup --
confirmed live for clinic_id 74 and 98 (active, HP-verified, with a Primary
Email candidate query this module runs against), which have ``uuid=''`` for
this reason alone and are still resolved by this module.
"""
from __future__ import annotations

from dataclasses import dataclass

MISSING_NOT_RESEARCHED = "Email調査未完了"
MISSING_NOT_ELIGIBLE = "final_sales_eligible対象外（Human Review/除外コード）"


class GasEmailLookupError(RuntimeError):
    """The Email Research connection could not be opened or read safely."""


@dataclass(frozen=True)
class GasEmailResult:
    clinic_id: int
    email: str
    email_candidate_id: str = ""
    found: bool = False
    reason: str = ""


def _require_clinic_id(value) -> int:
    """Reject UUID/str identifiers outright; never let one reach a ::bigint[] cast."""
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ValueError(
                f"clinic_id must be an internal integer Clinic ID, not a UUID/string: {value!r}"
            )
        return int(text)
    if isinstance(value, bool):
        raise ValueError(f"clinic_id must be an integer, not a bool: {value!r}")
    return int(value)


def lookup_primary_emails(clinic_ids, *, environ=None,
                          build_repositories=None) -> dict[int, GasEmailResult]:
    """READ ONLY: one GasEmailResult per requested Clinic ID (found or not).

    Raises GasEmailLookupError if the Email Research DB cannot be reached safely
    (never returns a partial/empty map to mask a connection failure).
    """
    ids = sorted({_require_clinic_id(cid) for cid in clinic_ids})
    if not ids:
        return {}
    from src.repository.email_database import build_email_repositories
    factory = build_repositories or build_email_repositories
    bundle, status = factory(environ=environ)
    if bundle is None:
        raise GasEmailLookupError(f"Email調査DBに接続できません: {status.message}")
    wanted = set(ids)
    rows = bundle.email_research.primary_listing(limit=1_000_000)
    results: dict[int, GasEmailResult] = {}
    for row in rows:
        clinic_id = int(row["clinic_id"])
        if clinic_id not in wanted or clinic_id in results:
            continue
        if row.get("final_sales_eligible") is False:
            results[clinic_id] = GasEmailResult(clinic_id, "", reason=MISSING_NOT_ELIGIBLE)
            continue
        email = str(row.get("email") or "").strip()
        results[clinic_id] = GasEmailResult(
            clinic_id, email, str(row.get("candidate_id") or ""), found=bool(email),
            reason="" if email else MISSING_NOT_ELIGIBLE,
        )
    for clinic_id in ids:
        if clinic_id not in results:
            results[clinic_id] = GasEmailResult(clinic_id, "", reason=MISSING_NOT_RESEARCHED)
    return results
