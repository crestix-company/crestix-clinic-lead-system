"""Minimal flag check for Cloud Run write-capable UI panels.

Issue #130 P1 explicitly asks for a lightweight mechanism, not a generic
framework that stalls migration: each write-capable panel is gated by its
own explicitly-named env var, default OFF (unset/anything other than
1/true/yes/on). There is no registry object, no dynamic discovery -- adding
a new gated panel means adding one new env var name and one call to
cloud_write_feature_enabled(), same as the existing CLINIC_COMDESK_SNAPSHOT_
DIFF_ENABLED / OUTREACH_GMAIL_DRAFTS_ENABLED / EMAIL_AUTO_RUN_ENABLED flags
already used throughout this codebase.
"""
import os

_TRUE_VALUES = {"1", "true", "yes", "on"}


def cloud_write_feature_enabled(flag_env_var):
    return (os.environ.get(flag_env_var) or "").strip().casefold() in _TRUE_VALUES
