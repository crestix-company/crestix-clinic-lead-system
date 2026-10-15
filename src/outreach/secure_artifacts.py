"""Private, no-overwrite artifact output helpers for outreach PII."""
from __future__ import annotations

import os
from pathlib import Path

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


def ensure_private_directory(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
    directory.chmod(DIRECTORY_MODE)
    return directory


def write_private_bytes(path: str | Path, data: bytes) -> Path:
    """Create one private artifact, refusing to replace any existing file."""
    target = Path(path)
    ensure_private_directory(target.parent)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
    except Exception:
        # Preserve any partial evidence instead of deleting an existing path implicitly.
        raise
    target.chmod(FILE_MODE)
    return target


def write_private_text(path: str | Path, value: str) -> Path:
    return write_private_bytes(path, value.encode("utf-8"))
