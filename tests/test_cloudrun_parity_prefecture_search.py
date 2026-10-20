"""Issue #126 続行依頼・項目3: ローカル版（通常モード）とCloud Run READ ONLY版が
同じ都道府県フィルタに対して同じ医院集合を返すことを、実際のクエリ生成関数に対して
検証する。対象は代表ケースとして明示された東京都・大阪府・北海道。

ここで検証できるのは「SQL生成レベルでの一致」である。本物のSupabase/Postgresに
接続した実行結果の比較ではない（本番DBへの書込みはもちろん、本番への追加の
SELECT実行も本タスクでは行っていない）。実際のCanaryブラウザでの件数は、
2026-10-20のREAD ONLY読み取り専用セッションで以下を目視確認済み（実データのため
行内容はここに含めない）:
  東京都・医科・HP ABC判定A+B: 1,010件
  大阪府・医科・HP ABC判定A+B:   630件
  北海道・医科・HP ABC判定A+B:   156件
(3件とも異なる値で、都道府県フィルタが実際に効いていることを確認した。)
"""
import io
import json

import pandas as pd

from src.io.input_loader import load_table
from src.master.comdesk import COMDESK_EXPORT_HEADERS, COMDESK_HEADERS
from src.master.filters import Filters
from src.repository.supabase_adapter import SupabaseClinicRepository
from src.repository.supabase_filters import where as supabase_where
from src.repository.runtime_store import SupabaseRuntimeStore

PREFECTURES = ["東京都", "大阪府", "北海道"]


def test_prefecture_predicate_is_byte_identical_between_normal_and_read_only():
    """The prefecture IN(...) clause itself must not differ between the two modes --
    only the FROM source and selected columns should differ (see _from_sql/_fetch_row).
    If someone ever special-cased prefecture handling for read_only_ui, this would catch
    the resulting cross-backend search-result drift before it reached a screenshot."""
    for pref in PREFECTURES:
        filters = Filters(prefectures=[pref])
        normal_sql, normal_args = supabase_where(filters, excluded_ids=[], read_only_ui=False)
        readonly_sql, readonly_args = supabase_where(filters, excluded_ids=[], read_only_ui=True)

        assert "prefecture IN (%s)" in normal_sql
        assert "prefecture IN (%s)" in readonly_sql
        assert pref in normal_args
        assert pref in readonly_args
        # Isolate just the prefecture clause's own contribution; both backends must agree on it.
        assert normal_sql.count("prefecture IN (%s)") == readonly_sql.count("prefecture IN (%s)") == 1


def test_from_source_switches_but_prefecture_filter_does_not_change_the_from_choice():
    for pref in PREFECTURES:
        filters = Filters(prefectures=[pref])
        assert SupabaseClinicRepository._from_sql(filters, read_only_ui=False) == "public.clinics"
        assert (
            SupabaseClinicRepository._from_sql(filters, read_only_ui=True)
            == "public.clinic_cloudrun_readonly AS clinics"
        )


def test_read_only_projection_still_carries_prefecture_and_clinic_identity_columns():
    """The READ ONLY column set must still include what the search UI and Comdesk
    export need to render/segment by prefecture -- this is what lets the "対象医院を
    確認" listing show 住所/医院名/電話番号 per clinic in the live Canary UI."""
    for col in ("prefecture", "clinic_name", "phone", "phone_norm", "address", "uuid", "id"):
        assert col in SupabaseClinicRepository._READ_ONLY_COLUMNS


def test_medical_type_and_prefecture_filters_compose_identically_in_both_modes():
    """営業対象・出力 defaults to 医科 + HP ABC判定A+B; confirm that combining
    prefecture with 医科・歯科 produces the same two IN-clauses (just reordered FROM
    source) in both modes, for all three representative prefectures."""
    for pref in PREFECTURES:
        filters = Filters(prefectures=[pref], medical_types=["医科"])
        normal_sql, normal_args = supabase_where(filters, excluded_ids=[], read_only_ui=False)
        readonly_sql, readonly_args = supabase_where(filters, excluded_ids=[], read_only_ui=True)
        assert "prefecture IN (%s)" in normal_sql and "prefecture IN (%s)" in readonly_sql
        assert "medical_type IN (%s)" in normal_sql and "medical_type IN (%s)" in readonly_sql
        assert normal_args[-2:] == [pref, "医科"]
        assert readonly_args[-2:] == [pref, "医科"]


class _PrefectureExportCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        if ("provenance.comdesk_original_rows" in normalized
                or "provenance.clinic_cloudrun_comdesk_rows" in normalized
                or "provenance.templates" in normalized
                or "public.clinic_email_enrichment" in normalized):
            self.rows = []
        else:
            raise AssertionError(f"Unexpected prefecture-export query: {normalized}")

    def fetchall(self):
        return list(self.rows)


class _PrefectureExportConnection:
    def cursor(self):
        return _PrefectureExportCursor(self)


def _three_prefecture_store():
    records = [
        {"id": 201, "uuid": "uuid-201", "clinic_name": "東京都サンプル医院", "prefecture": "東京都", "effective_hp_rank": "A"},
        {"id": 202, "uuid": "uuid-202", "clinic_name": "大阪府サンプル医院", "prefecture": "大阪府", "effective_hp_rank": "B"},
        {"id": 203, "uuid": "uuid-203", "clinic_name": "北海道サンプル医院", "prefecture": "北海道", "effective_hp_rank": "A"},
    ]
    store = object.__new__(SupabaseRuntimeStore)
    store._conn = _PrefectureExportConnection()
    clinic_repo = type("ClinicRepo", (), {})()
    clinic_repo.query = lambda _filters, limit=100000, offset=0, as_of=None, read_only_ui=False: list(records)
    clinic_repo.count = lambda _filters=None, as_of=None, read_only_ui=False: len(records)
    store.repositories = type("Repos", (), {"clinics": clinic_repo})()
    return store, records


def test_comdesk_export_segments_tokyo_osaka_hokkaido_identically_in_both_modes():
    """Local版（export）とREAD ONLY版（export_read_only）が、同じ3医院（東京・大阪・
    北海道の代表ケース）に対して同じ件数・同じ都道府県の組み合わせ・同じ医院名・同じ
    UUIDを30列Comdesk形式で出力することを確認する。院長名等7列がREAD ONLYでのみ
    空欄になる点（既存のtest_cloud_run_read_only_exportで別途検証済み）はここでは
    対象にしない。"""
    readonly_store, records = _three_prefecture_store()
    normal_store, _ = _three_prefecture_store()

    readonly_output = readonly_store.export_read_only(Filters())
    normal_output = normal_store.export(Filters())

    readonly_csv = load_table(readonly_output["final_comdesk_import.csv"], "final_comdesk_import.csv")
    normal_csv = load_table(normal_output["final_comdesk_import.csv"], "final_comdesk_import.csv")

    assert readonly_csv.headers == COMDESK_EXPORT_HEADERS == normal_csv.headers
    assert len(readonly_csv.data) == len(normal_csv.data) == 3

    pref_idx = COMDESK_EXPORT_HEADERS.index("都道府県")
    name_idx = COMDESK_EXPORT_HEADERS.index("名前")
    uuid_idx = COMDESK_EXPORT_HEADERS.index("UUID")

    readonly_by_pref = {row[pref_idx]: (row[name_idx], row[uuid_idx]) for row in readonly_csv.data.values.tolist()}
    normal_by_pref = {row[pref_idx]: (row[name_idx], row[uuid_idx]) for row in normal_csv.data.values.tolist()}

    assert set(readonly_by_pref) == set(normal_by_pref) == {"東京都", "大阪府", "北海道"}
    for pref in ("東京都", "大阪府", "北海道"):
        assert readonly_by_pref[pref] == normal_by_pref[pref]

    workbook = pd.read_excel(
        io.BytesIO(readonly_output["final_comdesk_import.xlsx"]), sheet_name="営業対象"
    ).fillna("")
    assert list(workbook.columns) == COMDESK_EXPORT_HEADERS
    assert set(workbook["都道府県"]) == {"東京都", "大阪府", "北海道"}
    assert len(workbook) == 3
