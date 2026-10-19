"""Lossless, local-only CSV import preview. No rows are merged or written."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata
from collections import Counter
from dataclasses import dataclass


ALIASES = {
    "clinic_id": ("clinic_id", "clinic id", "clinicid", "医院id", "clinic id（参照）"),
    "clinic_uuid": ("clinic_uuid", "clinic uuid", "uuid", "clinic uuid（参照）"),
    "case_id": ("case_id", "案件id", "案件id（現行）", "gas_case_id"),
    "operation_key": ("operation_key", "operation key"),
    "gmail_draft_id": ("gmail draft id", "下書きid", "draft id", "現存draft id"),
    "gmail_message_id": ("gmail message id", "sent message id", "message id"),
    "gmail_thread_id": ("gmail thread id", "下書きthreadid", "threadid"),
}


class CSVPreviewError(ValueError):
    pass


@dataclass(frozen=True)
class CSVImportPreview:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    row_numbers: tuple[int, ...]
    type_counts: tuple[dict[str, int], ...]
    key_suggestions: dict[str, tuple[str, ...]]
    row_hashes: tuple[str, ...]
    ragged_rows: tuple[int, ...]


def _cell_type(value: str) -> str:
    value = value.strip()
    if not value:
        return "empty"
    lowered = value.casefold()
    if lowered in {"true", "false", "yes", "no", "有", "無"}:
        return "boolean-like"
    try:
        int(value.replace(",", ""))
        return "integer-like"
    except ValueError:
        pass
    try:
        float(value.replace(",", ""))
        return "number-like"
    except ValueError:
        return "text"


def preview_csv(data: bytes | str) -> CSVImportPreview:
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else str(data)
    try:
        parsed = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise CSVPreviewError("CSVを解析できません") from exc
    if not parsed or not parsed[0] or not any(cell.strip() for cell in parsed[0]):
        raise CSVPreviewError("ヘッダー行がありません")
    headers = tuple(parsed[0])
    data_rows = parsed[1:]
    row_numbers = tuple(range(2, len(parsed) + 1))
    ragged = tuple(row_numbers[i] for i, row in enumerate(data_rows) if len(row) != len(headers))
    normalized = [unicodedata.normalize("NFKC", h).strip().casefold() for h in headers]
    suggestions = {
        key: tuple(headers[i] for i, header in enumerate(normalized) if header in {
            unicodedata.normalize("NFKC", alias).strip().casefold() for alias in aliases
        })
        for key, aliases in ALIASES.items()
    }
    padded = tuple(tuple(row[:len(headers)] + [""] * max(0, len(headers) - len(row))) for row in data_rows)
    type_counts = tuple(dict(Counter(_cell_type(row[col]) for row in padded)) for col in range(len(headers)))
    hashes = tuple(hashlib.sha256(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                   for row in padded)
    return CSVImportPreview(headers, padded, row_numbers, type_counts, suggestions, hashes, ragged)


__all__ = ["CSVImportPreview", "CSVPreviewError", "preview_csv"]
