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


from src.cloud_run.operator_identity import operator_authorized_for_cloud_write


def test_default_empty_allowlist_authorizes_nobody():
    assert operator_authorized_for_cloud_write("a@example.com", env={}) is False


def test_none_or_blank_identity_never_authorized_even_with_allowlist():
    env = {"CLINIC_CLOUD_WRITE_OPERATORS": "a@example.com"}
    assert operator_authorized_for_cloud_write(None, env=env) is False
    assert operator_authorized_for_cloud_write("", env=env) is False


def test_exact_match_in_allowlist_is_authorized():
    env = {"CLINIC_CLOUD_WRITE_OPERATORS": "a@example.com,b@example.com"}
    assert operator_authorized_for_cloud_write("a@example.com", env=env) is True
    assert operator_authorized_for_cloud_write("b@example.com", env=env) is True


def test_case_insensitive_match():
    env = {"CLINIC_CLOUD_WRITE_OPERATORS": "A@Example.com"}
    assert operator_authorized_for_cloud_write("a@example.com", env=env) is True


def test_identity_not_in_allowlist_is_not_authorized():
    env = {"CLINIC_CLOUD_WRITE_OPERATORS": "a@example.com"}
    assert operator_authorized_for_cloud_write("stranger@example.com", env=env) is False


def test_identity_alone_without_allowlist_check_is_never_sufficient():
    """Regression for the exact gap the review flagged: a real, well-formed
    IAP email must not be treated as authorized just because it parsed."""
    assert operator_authorized_for_cloud_write("anyone@crestix-inc.com", env={}) is False
