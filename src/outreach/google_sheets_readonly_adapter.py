"""Injectable read-only boundary for the legacy outreach spreadsheet tabs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

TARGET_TABS = ("案件管理", "メール管理", "進捗")


@dataclass(frozen=True)
class ReadOnlySheetTab:
    tab_name: str
    headers: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]
    source_rows: tuple[int, ...]


class GoogleSheetsReadOnlyAdapter:
    """Reader injection only. It deliberately exposes no update/append method."""

    def __init__(self, get_values: Callable[[str, str], Sequence[Sequence[object]]]):
        self._get_values = get_values

    def read_outreach_tabs(self, spreadsheet_id: str) -> tuple[ReadOnlySheetTab, ...]:
        return self.read_tabs(spreadsheet_id, TARGET_TABS)

    def read_tabs(self, spreadsheet_id: str, tab_names: Sequence[str]) -> tuple[ReadOnlySheetTab, ...]:
        """Read explicitly named source tabs through the same read-only boundary."""
        if not str(spreadsheet_id).strip():
            raise ValueError("spreadsheet_id is required")
        result = []
        for tab in tab_names:
            if not str(tab).strip():
                raise ValueError("tab names must be non-empty")
            values = self._get_values(spreadsheet_id, f"'{tab}'!A:ZZ")
            matrix = [list(row) for row in values or ()]
            if not matrix:
                result.append(ReadOnlySheetTab(tab, (), (), ()))
                continue
            headers = tuple(str(value or "").strip() for value in matrix[0])
            width = len(headers)
            records, row_numbers = [], []
            for row_number, cells in enumerate(matrix[1:], start=2):
                padded = list(cells[:width]) + [""] * max(0, width - len(cells))
                records.append({header: "" if value is None else str(value)
                                for header, value in zip(headers, padded)})
                row_numbers.append(row_number)
            result.append(ReadOnlySheetTab(tab, headers, tuple(records), tuple(row_numbers)))
        return tuple(result)

