"""Fail-closed guard shared by Preview-only UI and all write/external-action paths."""
from __future__ import annotations

import os


ENV_VAR = "CLINIC_COMDESK_PREVIEW_ONLY"


class PreviewOnlyModeError(RuntimeError):
    """Raised when a mutation or external action is attempted in preview-only mode."""


def comdesk_preview_only_enabled() -> bool:
    value = os.environ.get(ENV_VAR, "").strip().casefold()
    if value in {"", "0"}:
        return False
    if value == "1":
        return True
    raise RuntimeError(f"{ENV_VAR}は1または0で指定してください。")


def assert_not_comdesk_preview_only(action: str = "この操作") -> None:
    if comdesk_preview_only_enabled():
        raise PreviewOnlyModeError(
            f"{action}はComdesk Preview専用モードでは実行できません。"
        )
