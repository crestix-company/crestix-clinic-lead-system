"""Extracts the IAP-authenticated operator's email from the request headers
Cloud Run's IAP proxy injects, for audit/display on Cloud write panels --
and separately, checks that email against a server-side business-operator
allowlist before any write panel may proceed.

Review finding (Issue #130 PR #131 follow-up): "IAP認証と業務上の操作権限を
分ける。ヘッダーのメールアドレス表示だけをWRITE認可に使用しない." IAP only
proves "this is a real, SSO-authenticated person the organization trusts
with *some* access to this URL" -- it says nothing about whether that person
is actually authorized to perform Comdesk data-entry operations. The first
version of this module treated "a non-empty IAP email" as sufficient to
proceed, which conflates authentication with authorization.

operator_authorized_for_cloud_write() is the actual authorization boundary:
a comma-separated allowlist of exact email addresses in an env var, checked
server-side (never client-controllable -- IAP headers themselves can't be
spoofed past the proxy, and the allowlist is read from the container's own
environment, not from any request). Default is an empty allowlist, i.e.
fail-closed: nobody is authorized until a human explicitly lists them.
"""
import os

IAP_EMAIL_HEADER = "X-Goog-Authenticated-User-Email"
_IAP_EMAIL_PREFIX = "accounts.google.com:"
CLOUD_WRITE_OPERATORS_ENV_VAR = "CLINIC_CLOUD_WRITE_OPERATORS"


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


def _cloud_write_operator_allowlist(env=None):
    env = os.environ if env is None else env
    raw = (env.get(CLOUD_WRITE_OPERATORS_ENV_VAR) or "").strip()
    if not raw:
        return frozenset()
    return frozenset(email.strip().casefold() for email in raw.split(",") if email.strip())


def operator_authorized_for_cloud_write(operator_email, env=None):
    """The actual authorization check -- identity alone (operator_email being
    non-None) is NOT sufficient. Returns False for None, blank, or any email
    not in the explicit CLINIC_CLOUD_WRITE_OPERATORS allowlist. Default
    (unset/empty allowlist) authorizes nobody."""
    if not operator_email:
        return False
    allowlist = _cloud_write_operator_allowlist(env)
    return operator_email.strip().casefold() in allowlist
