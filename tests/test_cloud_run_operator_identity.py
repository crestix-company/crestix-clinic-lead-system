from src.cloud_run.operator_identity import operator_email_from_headers, parse_iap_authenticated_email


def test_parses_standard_iap_header_format():
    assert parse_iap_authenticated_email("accounts.google.com:user@example.com") == "user@example.com"


def test_passes_through_bare_email_without_prefix():
    assert parse_iap_authenticated_email("user@example.com") == "user@example.com"


def test_missing_header_returns_none():
    assert parse_iap_authenticated_email(None) is None
    assert parse_iap_authenticated_email("") is None
    assert parse_iap_authenticated_email("   ") is None


def test_malformed_value_without_at_sign_returns_none():
    assert parse_iap_authenticated_email("accounts.google.com:") is None
    assert parse_iap_authenticated_email("not-an-email") is None


def test_operator_email_from_headers_mapping():
    assert operator_email_from_headers({"X-Goog-Authenticated-User-Email": "accounts.google.com:a@b.com"}) == "a@b.com"
    assert operator_email_from_headers({}) is None
    assert operator_email_from_headers(None) is None
