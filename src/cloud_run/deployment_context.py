"""Detects whether this process is running as a Cloud Run container, using
Cloud Run's own automatically-injected Knative env var (K_SERVICE) -- never
something a human sets manually, so it can't be forgotten or misconfigured
the way an opt-in flag could. A Mac/local process never has this set.

Review finding (Issue #130 PR #131 follow-up): CLINIC_READ_ONLY_UI=false on
a Cloud Run deployment previously meant "identical to Mac" for every
pre-existing write surface (Step1 legacy Comdesk import, 厚生局 import,
Google Maps import, HP Job dispatch, Email調査, 詳細設定) -- none of those
were ever individually reviewed for multi-operator IAP-authenticated Cloud
access (no operator identity, no preview/confirm, no dedicated writer
connection). is_cloud_run_deployment() lets app_v2.py lock all of that
legacy surface down on Cloud Run regardless of CLINIC_READ_ONLY_UI, so only
deliberately-built, individually-flagged Cloud write paths (like
cloud_comdesk_import_ui) can ever run there.
"""
import os


def is_cloud_run_deployment(env=None):
    env = os.environ if env is None else env
    return bool((env.get("K_SERVICE") or "").strip())
