"""Extracts the IAP-authenticated operator's email from the request headers
Cloud Run's IAP proxy injects, for audit/display on Cloud write panels.

This is an audit/display identity only -- it is never a substitute for the
DB-level role and connection separation already enforced elsewhere
(src/cloud_run/job_control.py, src/master/comdesk_snapshot_import.py). A
missing or malformed header must fail closed (no identity -> no write UI),
never fall back to an anonymous or assumed operator.
"""

IAP_EMAIL_HEADER = "X-Goog-Authenticated-User-Email"
_IAP_EMAIL_PREFIX = "accounts.google.com:"


def parse_iap_authenticated_email(header_value):
    """Parses the raw IAP header value into a plain email address.

    IAP sends this header as "accounts.google.com:user@example.com". Returns
    None for missing, blank, or malformed values -- callers must treat None
    as "no identified operator" and refuse to proceed, not substitute a
    placeholder.
    """
    value = (header_value or "").strip()
    if not value:
        return None
    if value.startswith(_IAP_EMAIL_PREFIX):
        value = value[len(_IAP_EMAIL_PREFIX):].strip()
    if not value or "@" not in value:
        return None
    return value


def operator_email_from_headers(headers):
    """headers: a mapping-like object (e.g. Streamlit's st.context.headers)."""
    if headers is None:
        return None
    return parse_iap_authenticated_email(headers.get(IAP_EMAIL_HEADER))
