#!/usr/bin/env python3
"""Read-only parity harness for Step7 Comdesk import matching.

Usage:
  SUPABASE_RUNTIME_DB_URL='postgresql://...' \
  python scripts/comdesk_import_parity.py /path/to/comdesk.csv

The harness never writes. It compares the legacy row-query matcher with the prefetched fast
matcher against one REPEATABLE READ / READ ONLY snapshot, while simulating prior rows' UUID/base
updates in memory so later-row matching keeps the import's sequential semantics.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict

from src.io.input_loader import load_table
from src.master.comdesk import infer_comdesk_columns, record_from_row
from src.master.matching import match_record
from src.repository.supabase_adapter import connect
from src.repository.supabase_write_adapter import (
    _MatchingConnection,
    _PrefetchedMatchingConnection,
    SupabaseClinicWriteRepository,
)


def _condition_matches(row, statement, args):
    if row.get("merged_into") is not None:
        return False
    if "uuid=?" in statement:
        return str(row.get("uuid") or "") == str(args[0]).strip()
    if "medical_key=?" in statement:
        return str(row.get("medical_key") or "") == str(args[0])
    if "tel_match_key=?" in statement:
        return str(row.get("tel_match_key") or "") == str(args[0])
    if "name_norm=? AND address_norm=?" in statement:
        return (
            str(row.get("name_norm") or "") == str(args[0])
            and str(row.get("address_norm") or "") == str(args[1])
        )
    if "name_prefix=? AND (prefecture=? OR prefecture='')" in statement:
        return (
            str(row.get("name_prefix") or "") == str(args[0])
            and str(row.get("prefecture") or "") in {str(args[1]), ""}
        )
    raise ValueError(f"unsupported match query: {statement}")


class OverlayLegacyConnection:
    """Canonical DB query per matcher lookup + in-memory overlay of earlier CSV rows."""

    def __init__(self, cursor):
        self._db = _MatchingConnection(cursor)
        self._overlay = {}
        self._cache = {}

    def execute(self, statement, args=()):
        db_rows = list(self._db.execute(statement, args))
        output, seen = [], set()
        for raw in db_rows:
            cid = int(raw["id"])
            self._cache[cid] = dict(raw)
            row = self._overlay.get(cid, dict(raw))
            if _condition_matches(row, statement, args):
                output.append(dict(row))
                seen.add(cid)
        for cid, row in self._overlay.items():
            if cid in seen:
                continue
            if _condition_matches(row, statement, args):
                output.append(dict(row))
        return output

    def get(self, clinic_id):
        cid = int(clinic_id)
        if cid in self._overlay:
            return dict(self._overlay[cid])
        if cid not in self._cache:
            raise KeyError(f"candidate {cid} was not loaded by the canonical matcher")
        return dict(self._cache[cid])

    def upsert(self, row):
        self._overlay[int(row["id"])] = dict(row)


def _prepare(table, mapping):
    prepared = []
    for index, raw in enumerate(table.data.values.tolist()):
        try:
            record = record_from_row(raw, mapping)
        except ValueError as exc:
            raise ValueError(f"{index + 2}行目: {exc}") from exc
        if not record.get("clinic_name", "").strip():
            raise ValueError(f"{index + 2}行目の医院名が空白です。")
        prepared.append((index, raw, record))
    return prepared


def _simulate(matcher, prepared):
    decisions = []
    histories = []
    next_temp_id = -1
    for index, _raw, record in prepared:
        match = match_record(matcher, record)
        resolved = None
        if match.status == "MATCHED":
            resolved = int(match.candidates[0])
            state = matcher.get(resolved)
            before = dict(state.get("base_json") or {})
            base = dict(before)
            base.update({
                key: value for key, value in record.items()
                if value and not base.get(key)
            })
            state["base_json"] = base
            state["uuid"] = state.get("uuid", "") or record.get("uuid", "")
            current_as_of = str(state.get("source_as_of_date") or "")
            incoming_as_of = str(record.get("as_of", "") or "")
            state["source_as_of_date"] = (
                current_as_of if incoming_as_of < current_as_of else incoming_as_of
            )
            SupabaseClinicWriteRepository._comdesk_refresh_match_fields(state)
            matcher.upsert(state)
            if before != base:
                histories.append((resolved, before, dict(base), match.reason))
        elif match.status == "NEW":
            resolved = next_temp_id
            next_temp_id -= 1
            state = {
                "id": resolved,
                "base_json": dict(record),
                "uuid": record.get("uuid", ""),
                "medical_key": "",
                "merge_hold": False,
                "merged_into": None,
                "source_as_of_date": str(record.get("as_of", "") or ""),
            }
            SupabaseClinicWriteRepository._comdesk_refresh_match_fields(state)
            matcher.upsert(state)
            histories.append((resolved, {}, dict(record), match.reason))

        decisions.append({
            "row_number": index,
            "status": match.status,
            "candidates": list(match.candidates),
            "reason": match.reason,
            "score": match.score,
            "clinic_id": resolved,
            "uuid": record.get("uuid", ""),
        })
    final = {}
    for decision in decisions:
        cid = decision["clinic_id"]
        if cid is not None:
            state = matcher.get(cid)
            final[cid] = {
                "uuid": state.get("uuid", ""),
                "base_json": state.get("base_json") or {},
                "tel_match_key": state.get("tel_match_key", ""),
                "name_norm": state.get("name_norm", ""),
                "address_norm": state.get("address_norm", ""),
                "prefecture": state.get("prefecture", ""),
                "medical_type": state.get("medical_type", ""),
            }
    return {"decisions": decisions, "histories": histories, "final": final}


def _first_diff(left, right):
    if left == right:
        return None
    if isinstance(left, list) and isinstance(right, list):
        for index, (a, b) in enumerate(zip(left, right)):
            if a != b:
                return {"index": index, "legacy": a, "fast": b}
        return {"length": [len(left), len(right)]}
    if isinstance(left, dict) and isinstance(right, dict):
        keys = sorted(set(left) | set(right), key=str)
        for key in keys:
            if left.get(key) != right.get(key):
                return {"key": key, "legacy": left.get(key), "fast": right.get(key)}
    return {"legacy": left, "fast": right}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--dsn", default=os.environ.get("SUPABASE_RUNTIME_DB_URL", ""))
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("SUPABASE_RUNTIME_DB_URL or --dsn is required")

    raw = open(args.csv, "rb").read()
    table = load_table(raw, os.path.basename(args.csv))
    mapping = infer_comdesk_columns(table)
    prepared = _prepare(table, mapping)

    started = time.perf_counter()
    conn = connect(args.dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

            legacy = OverlayLegacyConnection(cur)
            legacy_result = _simulate(legacy, prepared)

            candidates = SupabaseClinicWriteRepository._prefetch_comdesk_candidates(
                cur, [item[2] for item in prepared]
            )
            fast = _PrefetchedMatchingConnection(
                candidates, fuzzy_fallback=_MatchingConnection(cur)
            )
            fast_result = _simulate(fast, prepared)

        conn.rollback()  # explicit: harness is read-only and never commits
    finally:
        conn.close()

    report = {
        "csv_rows": len(prepared),
        "prefetched_candidates": len(candidates),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "decision_match": legacy_result["decisions"] == fast_result["decisions"],
        "history_match": legacy_result["histories"] == fast_result["histories"],
        "final_state_match": legacy_result["final"] == fast_result["final"],
        "decision_diff": _first_diff(
            legacy_result["decisions"], fast_result["decisions"]
        ),
        "history_diff": _first_diff(
            legacy_result["histories"], fast_result["histories"]
        ),
        "final_state_diff": _first_diff(
            legacy_result["final"], fast_result["final"]
        ),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    if not all(
        report[key]
        for key in ("decision_match", "history_match", "final_state_match")
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
