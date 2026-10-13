"""IS担当者設定（config/outreach_is_staff.yml）の読み込み。

担当者の追加・変更はYAMLの編集だけで反映され、コード修正は不要。
別ファイルを使う場合は環境変数 OUTREACH_IS_STAFF_CONFIG にパスを指定する。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

STAFF_CONFIG_ENV = "OUTREACH_IS_STAFF_CONFIG"
DEFAULT_STAFF_CONFIG = Path(__file__).resolve().parents[2] / "config" / "outreach_is_staff.yml"
MAX_NAME_LENGTH = 30


@dataclass(frozen=True)
class StaffConfig:
    names: tuple[str, ...]
    path: Path
    errors: tuple[str, ...] = ()


def staff_config_path(environ=None) -> Path:
    env = os.environ if environ is None else environ
    raw = (env.get(STAFF_CONFIG_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_STAFF_CONFIG


def validate_staff_name(name: str) -> str | None:
    """問題があればエラー文、なければNone。件名・本文に差し込むため1行の短い氏名のみ許可。"""
    text = str(name or "").strip()
    if not text:
        return "空の担当者名"
    if any(c in text for c in "\r\n\t"):
        return f"改行・タブを含む担当者名: {text!r}"
    if len(text) > MAX_NAME_LENGTH:
        return f"{MAX_NAME_LENGTH}文字を超える担当者名: {text[:MAX_NAME_LENGTH]}…"
    return None


def load_staff(path: Path | None = None, environ=None) -> StaffConfig:
    """Never raises: a missing/broken file yields no names plus an error message for the UI."""
    path = path or staff_config_path(environ)
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return StaffConfig((), path, (f"設定ファイルがありません: {path}",))
    except Exception as exc:  # yaml.YAMLError, OSError, UnicodeDecodeError
        return StaffConfig((), path, (f"設定ファイルを読めません: {type(exc).__name__}",))
    raw = data.get("staff") if isinstance(data, dict) else None
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return StaffConfig((), path, ("staff はリスト形式で記載してください",))
    names, errors = [], []
    for item in raw:
        error = validate_staff_name(item)
        if error:
            errors.append(error)
            continue
        text = str(item).strip()
        if text not in names:
            names.append(text)
    return StaffConfig(tuple(names), path, tuple(errors))
