"""Local, independent ledger recording GAS registration outcomes.

Maintains the correspondence between `operation_key`, `clinic_uuid`,
`GAS case_id`, `Gmail draft_id`, `Gmail thread_id`, and `tracking_token`
(see ~/ClinicLeadGASIntegration/bridge/docs/id_lifecycle.md) on the Python
side, independently of whatever GAS itself stores in PropertiesService/Sheets.

This module deliberately mirrors the safe-file-locking pattern already used
by `src/outreach/gmail_drafts.py` (`_ledger_append` / `_ledger_read_fd` /
`_ledger_contains`: 0700 dir, 0600 file, `fcntl.flock`, atomic-ish append) --
reusing a proven *mechanism* is not the same as duplicating the
duplicate-registration *policy*. This ledger does not decide whether a
registration is a duplicate; it only records what `GasBridgeClient` observed.
GAS's own sheet + PropertiesService reservation remain the authoritative
source of truth for duplicate prevention.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .gas_bridge_client import GasCaseRegistrationResult

DEFAULT_LEDGER_PATH = Path(".secrets/outreach_gas_bridge/case_ledger.jsonl")


class GasBridgeLedgerError(RuntimeError):
    """Safe ledger failure; never contains clinic data or credentials in its message."""


@dataclass(frozen=True)
class GasBridgeLedgerRecord:
    operation_key: str
    clinic_uuid: str
    status: str
    case_id: str = ""
    draft_id: str = ""
    thread_id: str = ""
    tracking_token: str = ""
    reason: str = ""
    recorded_at: str = ""


def _ledger_read_fd(fd: int) -> list[dict]:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        raw += chunk
    records: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GasBridgeLedgerError("ledger is malformed; refusing to proceed") from exc
        if not isinstance(item, dict) or not item.get("operation_key"):
            raise GasBridgeLedgerError("ledger record is invalid; refusing to proceed")
        records.append(item)
    return records


def _reject_unsafe_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise GasBridgeLedgerError("ledger permissions or file type are unsafe")


def record_result(
    path: Path, *, operation_key: str, clinic_uuid: str, result: GasCaseRegistrationResult
) -> GasBridgeLedgerRecord:
    """Appends one outcome. Multiple records for the same operation_key are
    allowed (e.g. PENDING followed later by REGISTERED on retry); callers
    should treat the most recent record as current (see `find_by_operation_key`).
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    _reject_unsafe_path(path)

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        _ledger_read_fd(fd)  # validates any existing content before appending; raises if corrupted
        record = GasBridgeLedgerRecord(
            operation_key=operation_key,
            clinic_uuid=clinic_uuid,
            status=result.status.value,
            case_id=result.case_id,
            draft_id=result.draft_id,
            thread_id=result.thread_id,
            tracking_token=result.tracking_token,
            reason=result.reason,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        encoded = (json.dumps(record.__dict__, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        os.lseek(fd, 0, os.SEEK_END)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise GasBridgeLedgerError("ledger write did not complete")
            remaining = remaining[written:]
        os.fsync(fd)
        return record
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def find_by_operation_key(path: Path, operation_key: str) -> dict | None:
    """Returns the most recent ledger record for `operation_key`, or None."""
    if not path.exists():
        return None
    _reject_unsafe_path(path)

    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        records = _ledger_read_fd(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    for item in reversed(records):
        if item.get("operation_key") == operation_key:
            return item
    return None
