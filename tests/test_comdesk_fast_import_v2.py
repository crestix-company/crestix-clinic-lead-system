import inspect
import json

import pandas as pd
import pytest

from src.io.input_loader import InputTable
from src.master.comdesk import COMDESK_HEADERS, record_from_row
from src.master.matching import match_record
from src.normalizer.address import normalize_address
from src.normalizer.clinic_name import normalize_clinic_name
from src.normalizer.phone import tel_match_key
from src.repository.supabase_write_adapter import (
    _PrefetchedMatchingConnection,
    SupabaseClinicWriteRepository,
)


MAPPING = {
    "uuid": 0,
    "clinic_name": 1,
    "phone": 2,
    "prefecture": 3,
    "address": 4,
    "address2": 5,
}


def _record(prefecture, address, *, name="医院", uuid="", phone=""):
    return record_from_row([uuid, name, phone, prefecture, address, ""], MAPPING)


@pytest.mark.parametrize(
    ("prefecture", "address"),
    [
        ("宮崎県", "北諸県郡三股町樺山4672-4"),
        ("宮崎県", "東諸県郡綾町南俣443-6"),
        ("岐阜県", "岐阜市県町1-6"),
    ],
)
def test_comdesk_prefecture_parser_does_not_misread_county_or_city_text(prefecture, address):
    result = _record(prefecture, address)
    assert result["prefecture"] == prefecture
    assert result["address"] == prefecture + address


def test_comdesk_prefecture_parser_still_rejects_real_prefecture_mismatch():
    with pytest.raises(ValueError, match="都道府県の列と住所の都道府県が一致しません"):
        _record("宮崎県", "鹿児島県鹿児島市中央町1-1")


def _candidate(
    clinic_id,
    *,
    uuid="",
    phone="",
    name="テスト医院",
    address="東京都新宿区1-1",
    prefecture="東京都",
    medical_type="医科",
    medical_key="",
    merge_hold=False,
):
    name_norm = normalize_clinic_name(name)
    return {
        "id": clinic_id,
        "base_json": {
            "clinic_name": name,
            "phone": phone,
            "address": address,
            "prefecture": prefecture,
            "medical_type": medical_type,
        },
        "uuid": uuid,
        "medical_key": medical_key,
        "tel_match_key": tel_match_key(phone),
        "name_norm": name_norm,
        "name_prefix": name_norm[:2],
        "address_norm": normalize_address(address),
        "prefecture": prefecture,
        "medical_type": medical_type,
        "merge_hold": merge_hold,
        "source_as_of_date": "",
    }


def test_prefetched_matcher_keeps_phone_exact_contract():
    conn = _PrefetchedMatchingConnection([_candidate(10, phone="03-1111-2222")])
    result = match_record(
        conn,
        {
            "clinic_name": "テスト医院",
            "phone": "03-1111-2222",
            "address": "東京都新宿区1-1",
            "prefecture": "東京都",
            "medical_type": "医科",
            "uuid": "",
        },
    )
    assert (result.status, result.candidates, result.reason, result.score) == (
        "MATCHED",
        [10],
        "電話番号キー完全一致",
        100,
    )


def test_prefetched_matcher_keeps_duplicate_phone_ambiguous():
    conn = _PrefetchedMatchingConnection([
        _candidate(10, phone="03-1111-2222", name="甲医院"),
        _candidate(20, phone="03-1111-2222", name="乙医院"),
    ])
    result = match_record(
        conn,
        {
            "clinic_name": "",
            "phone": "03-1111-2222",
            "address": "",
            "prefecture": "東京都",
            "medical_type": "医科",
            "uuid": "",
        },
    )
    assert result.status == "AMBIGUOUS"
    assert result.candidates == [10, 20]


def test_prefetched_matcher_reindexes_uuid_phone_and_address_for_later_rows():
    conn = _PrefetchedMatchingConnection([
        _candidate(10, uuid="", phone="", name="テスト医院", address=""),
    ])
    updated = conn.get(10)
    updated["uuid"] = "UUID-10"
    updated["base_json"].update({
        "phone": "03-1111-2222",
        "address": "東京都新宿区1-1",
    })
    SupabaseClinicWriteRepository._comdesk_refresh_match_fields(updated)
    conn.upsert(updated)

    by_uuid = match_record(conn, {
        "clinic_name": "", "phone": "", "address": "", "prefecture": "",
        "medical_type": "", "uuid": "UUID-10",
    })
    by_phone = match_record(conn, {
        "clinic_name": "テスト医院", "phone": "03-1111-2222",
        "address": "東京都新宿区1-1", "prefecture": "東京都",
        "medical_type": "医科", "uuid": "",
    })

    assert by_uuid.status == "MATCHED" and by_uuid.candidates == [10]
    assert by_phone.status == "MATCHED" and by_phone.candidates == [10]


class _StatefulCursor:
    COLUMNS = (
        "id", "base_json", "uuid", "medical_key", "tel_match_key", "name_norm",
        "name_prefix", "address_norm", "prefecture", "medical_type", "merge_hold",
        "source_as_of_date",
    )

    def __init__(self, conn):
        self.conn = conn
        self._result = None
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def _obj(value):
        return value.obj if hasattr(value, "obj") else value

    def _clinic_tuple(self, row):
        return tuple(row.get(column) for column in self.COLUMNS)

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        params = params or ()
        self.conn.executed.append((q, params))
        self._result = None

        if q == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ":
            self.conn.repeatable_read = True
            return

        if q.startswith("SELECT result_json FROM provenance.import_batches"):
            batch = params[0]
            self._result = (
                (self.conn.import_batches[batch],)
                if batch in self.conn.import_batches else None
            )
            return

        if q.startswith("INSERT INTO provenance.templates"):
            return

        if (
            q.startswith("SELECT id,base_json,uuid,medical_key,tel_match_key")
            or (
                q.startswith("WITH input_pairs AS")
                and "SELECT c.id,c.base_json,c.uuid,c.medical_key,c.tel_match_key" in q
            )
        ):
            rows = list(self.conn.clinics.values())
            if "uuid=ANY" in q:
                allowed = set(params[0])
                rows = [row for row in rows if row.get("uuid") in allowed]
            elif "medical_key=ANY" in q:
                allowed = set(params[0])
                rows = [row for row in rows if row.get("medical_key") in allowed]
            elif "tel_match_key=ANY" in q:
                allowed = set(params[0])
                rows = [row for row in rows if row.get("tel_match_key") in allowed]
            elif "name_norm=p.name_norm" in q:
                pairs = set(zip(params[0], params[1]))
                rows = [
                    row for row in rows
                    if (row.get("name_norm"), row.get("address_norm")) in pairs
                ]
            elif "c.name_prefix=p.name_prefix" in q:
                pairs = set(zip(params[0], params[1]))
                rows = [
                    row for row in rows
                    if any(
                        row.get("name_prefix") == prefix
                        and row.get("prefecture") in {prefecture, ""}
                        for prefix, prefecture in pairs
                    )
                ]
            elif "id=ANY" in q and "FOR UPDATE" in q:
                ids = set(params[0])
                rows = [row for row in rows if row["id"] in ids]
                if self.conn.mutate_before_lock:
                    self.conn.mutate_before_lock(self.conn)
                    self.conn.mutate_before_lock = None
                    rows = [self.conn.clinics[row["id"]] for row in rows]
            self._result = [self._clinic_tuple(row) for row in sorted(rows, key=lambda r: r["id"])]
            return

        if q.startswith("SELECT * FROM public.clinics WHERE merged_into IS NULL AND name_prefix=%s"):
            prefix, prefecture = params
            rows = [
                row for row in self.conn.clinics.values()
                if row.get("name_prefix") == prefix
                and row.get("prefecture") in {prefecture, ""}
            ]
            # _MatchingConnection expects cursor.description names for SELECT *.
            self.description = [
                type("Column", (), {"name": name}) for name in self.COLUMNS
            ]
            self._result = [
                self._clinic_tuple(row) for row in sorted(rows, key=lambda r: r["id"])
            ]
            return

        if q.startswith("INSERT INTO public.clinics") and "RETURNING id" in q:
            cid = self.conn.next_clinic_id
            self.conn.next_clinic_id += 1
            uuid_value, base, first_seen, last_seen, source_as_of = params
            base = self._obj(base)
            row = _candidate(
                cid,
                uuid=uuid_value,
                phone=base.get("phone", ""),
                name=base.get("clinic_name", ""),
                address=base.get("address", ""),
                prefecture=base.get("prefecture", ""),
                medical_type=base.get("medical_type", ""),
            )
            row.update(
                base_json=dict(base),
                source_as_of_date=str(source_as_of or ""),
                first_seen_at=first_seen,
                last_seen_at=last_seen,
            )
            self.conn.clinics[cid] = row
            self._result = (cid,)
            return

        if q.startswith("UPDATE public.clinics AS c SET base_json=v.base_json,uuid=v.uuid"):
            width = 5
            for offset in range(0, len(params), width):
                cid, base, uuid_value, last_seen, source_as_of = params[offset:offset + width]
                row = self.conn.clinics[int(cid)]
                row["base_json"] = dict(self._obj(base))
                row["uuid"] = uuid_value
                row["last_seen_at"] = last_seen
                row["source_as_of_date"] = str(source_as_of or "")
                SupabaseClinicWriteRepository._comdesk_refresh_match_fields(row)
            return

        if q.startswith("INSERT INTO provenance.change_history"):
            width = 6
            for offset in range(0, len(params), width):
                self.conn.change_history.append(params[offset:offset + width])
            return

        if q.startswith("INSERT INTO provenance.source_records"):
            width = 9
            returned = []
            for offset in range(0, len(params), width):
                row = params[offset:offset + width]
                sid = self.conn.next_source_id
                self.conn.next_source_id += 1
                self.conn.source_records.append((sid, *row))
                returned.append((sid, row[4]))
            self._result = returned
            return

        if q.startswith("INSERT INTO provenance.comdesk_original_rows"):
            width = 7
            for offset in range(0, len(params), width):
                self.conn.original_rows.append(params[offset:offset + width])
            return

        if q.startswith("INSERT INTO provenance.match_reviews"):
            width = 3
            for offset in range(0, len(params), width):
                row = params[offset:offset + width]
                self.conn.match_reviews.append((
                    row[0],
                    list(self._obj(row[1])),
                    row[2],
                ))
            return

        if q.startswith("SELECT clinic_id,result_json FROM research.research_results"):
            self._result = []
            return

        if q.startswith("SELECT clinic_id,field,value_json FROM provenance.manual_overrides"):
            self._result = []
            return

        if q.startswith("UPDATE public.clinics AS c SET") and "effective_json" in q:
            self.conn.projection_updates.append((q, params))
            return

        if q.startswith("INSERT INTO provenance.import_batches"):
            batch, source, result_json, _created_at = params
            assert source == "コムデスク"
            self.conn.import_batches[batch] = dict(self._obj(result_json))
            return

        raise AssertionError(f"unhandled SQL: {q}")

    def fetchone(self):
        return self._result[0] if isinstance(self._result, list) and self._result else self._result

    def fetchall(self):
        return self._result or []


class _StatefulConn:
    def __init__(self, clinics=()):
        self.clinics = {row["id"]: dict(row) for row in clinics}
        self.next_clinic_id = 1_000_000_100
        self.next_source_id = 1_000_001_000
        self.executed = []
        self.change_history = []
        self.source_records = []
        self.original_rows = []
        self.match_reviews = []
        self.import_batches = {}
        self.projection_updates = []
        self.repeatable_read = False
        self.mutate_before_lock = None
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return _StatefulCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _table(rows):
    headers = ["UUID", "名前", "Tel1", "都道府県", "住所１", "住所２"]
    frame = pd.DataFrame(rows, columns=headers)
    return InputTable(headers=headers, data=frame)


def test_fast_import_preserves_matched_history_provenance_originals_and_idempotency():
    clinic = _candidate(
        1_000_000_001,
        uuid="",
        phone="03-1111-2222",
        name="既存医院",
        address="東京都千代田区1-1",
    )
    conn = _StatefulConn([clinic])
    repo = SupabaseClinicWriteRepository(conn)
    table = _table([
        ["UUID-1", "既存医院", "03-1111-2222", "東京都", "千代田区1-1", ""],
    ])

    result = repo.import_comdesk(table, MAPPING)

    assert result["MATCHED"] == 1 and result["NEW"] == 0 and result["AMBIGUOUS"] == 0
    assert conn.repeatable_read is True
    assert conn.clinics[1_000_000_001]["uuid"] == "UUID-1"
    assert len(conn.change_history) >= 1
    assert len(conn.source_records) == 1
    assert len(conn.original_rows) == 1
    assert conn.match_reviews == []
    assert conn.projection_updates
    assert conn.committed == 1 and conn.rolled_back == 0
    assert any("FOR UPDATE" in q and "id=ANY" in q for q, _ in conn.executed)

    second = repo.import_comdesk(table, MAPPING)
    assert second["already_imported"] is True
    assert len(conn.source_records) == 1
    assert len(conn.original_rows) == 1


def test_fast_import_new_then_later_uuid_matches_same_new_clinic():
    conn = _StatefulConn()
    repo = SupabaseClinicWriteRepository(conn)
    table = _table([
        ["UUID-NEW", "新規医院", "03-9999-0000", "東京都", "港区1-1", ""],
        ["UUID-NEW", "新規医院", "", "東京都", "港区1-1", ""],
    ])

    result = repo.import_comdesk(table, MAPPING)

    assert result["NEW"] == 1
    assert result["MATCHED"] == 1
    assert len(conn.clinics) == 1
    created_id = next(iter(conn.clinics))
    linked_source_ids = [row[1] for row in conn.source_records]
    assert linked_source_ids == [created_id, created_id]
    assert len(conn.original_rows) == 2


def test_fast_import_ambiguous_persists_review_candidates():
    conn = _StatefulConn([
        _candidate(1_000_000_001, phone="03-1111-2222", name="甲医院"),
        _candidate(1_000_000_002, phone="03-1111-2222", name="乙医院"),
    ])
    repo = SupabaseClinicWriteRepository(conn)
    table = _table([
        ["", "不明医院", "03-1111-2222", "東京都", "", ""],
    ])

    result = repo.import_comdesk(table, MAPPING)

    assert result["AMBIGUOUS"] == 1
    assert len(conn.source_records) == 1
    assert conn.source_records[0][1] is None
    assert len(conn.match_reviews) == 1
    assert conn.match_reviews[0][1] == [1_000_000_001, 1_000_000_002]


def test_fast_import_aborts_if_candidate_changes_between_prefetch_and_lock():
    clinic = _candidate(
        1_000_000_001,
        phone="03-1111-2222",
        name="既存医院",
        address="東京都千代田区1-1",
    )
    conn = _StatefulConn([clinic])

    def mutate(state):
        state.clinics[1_000_000_001]["base_json"]["address"] = "東京都千代田区9-9"
        SupabaseClinicWriteRepository._comdesk_refresh_match_fields(
            state.clinics[1_000_000_001]
        )

    conn.mutate_before_lock = mutate
    repo = SupabaseClinicWriteRepository(conn)
    table = _table([
        ["UUID-1", "既存医院", "03-1111-2222", "東京都", "千代田区1-1", ""],
    ])

    with pytest.raises(RuntimeError, match="安全のため取込を取り消しました"):
        repo.import_comdesk(table, MAPPING)

    assert conn.rolled_back == 1
    assert conn.committed == 0
    assert conn.source_records == []
    assert conn.original_rows == []


def test_fast_import_source_contains_concurrency_and_bulk_guards():
    source = inspect.getsource(SupabaseClinicWriteRepository.import_comdesk)
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ" in source
    assert "FOR UPDATE" in source
    assert "_prefetch_comdesk_candidates" in source
    assert "_bulk_insert_comdesk_source_records" in source
    assert "_projection_values" in source
    assert "_bulk_update_projection" in source
    assert "_upsert_tx(" not in source
