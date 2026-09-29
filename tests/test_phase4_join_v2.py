"""Phase 4.1 v2 JOIN(mhlw_dry_run/phase4_join_v2.py)の回帰テスト。

前半(TestNormalizeV2)は mhlw_dry_run/normalize_v2.py の純粋関数のみを検証し、
MHLW CSV/Production DBに依存せず常時実行される。

後半(TestV2Regression)は生成物(phase4_join_v2.csv / join_summary_v2.json)に依存し、
無ければ自動skipする。既存(v1)の legacy_mhlw_join.csv が今回のv2実装で
変更されていないことも合わせて固定する。
"""
import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mhlw_dry_run.normalize_v2 import light_normalize_name, comparison_name, base_address  # noqa: E402
from src.normalizer.address import normalize_address  # noqa: E402

BASE = Path(__file__).resolve().parents[1] / "mhlw_dry_run"
V2_CSV = BASE / "phase4_join_v2.csv"
V2_JSON = BASE / "join_summary_v2.json"
V1_CSV = BASE / "legacy_mhlw_join.csv"


class TestNormalizeV2:
    def test_light_normalize_keeps_corp_prefix(self):
        assert light_normalize_name("医療法人社団　正誠会　林医院") == "医療法人社団正誠会林医院"

    def test_comparison_name_strips_iryohojin_via_existing_rule(self):
        assert comparison_name("医療法人社団　正誠会　林医院") == "林医院"

    def test_comparison_name_strips_extra_corp_types_conservatively(self):
        # 「公益財団法人」は種別トークンのみ除去し、団体名(早期胃癌検診協会)は残す
        # (医院本体名称を誤って削除しないため、"...会"境界を推測しない)。
        assert comparison_name("公益財団法人　早期胃癌検診協会　附属茅場町クリニック") == "早期胃癌検診協会附属茅場町クリニック"
        assert comparison_name("公益財団法人　研医会診療所") == "研医会診療所"

    def test_comparison_name_does_not_touch_clean_names(self):
        assert comparison_name("新宿内科クリニック") == "新宿内科クリニック"

    def test_base_address_strips_building_with_space_separator(self):
        full = normalize_address("東京都小金井市本町一丁目１８番３号　ユニーブル武蔵小金井スイート　地下１階Ｂ１０１")
        raw = "東京都小金井市本町一丁目１８番３号　ユニーブル武蔵小金井スイート　地下１階Ｂ１０１"
        assert base_address(full, raw) == "東京都小金井市本町1-18-3"

    def test_base_address_strips_building_without_space_via_regex_fallback(self):
        raw = "大阪府大阪市西区千代崎3-13-1イオンモール大阪ドームシティ4階"
        full = normalize_address(raw)
        assert base_address(full, raw) == "大阪府大阪市西区千代崎3-13-1"

    def test_base_address_no_building_is_unchanged(self):
        raw = "東京都千代田区隼町２番１５号"
        full = normalize_address(raw)
        assert base_address(full, raw) == full

    def test_raw_address_untouched_by_normalization(self):
        # normalize_address/base_addressはraw文字列を書き換えず、常に新しい文字列を返す。
        raw = "東京都新宿区西新宿1-2-3"
        _ = normalize_address(raw)
        assert raw == "東京都新宿区西新宿1-2-3"


v2_missing = pytest.mark.skipif(
    not (V2_CSV.exists() and V2_JSON.exists()),
    reason="phase4_join_v2 output not generated. Run mhlw_dry_run/phase4_join_v2.py first.")


@v2_missing
class TestV2Regression:
    @pytest.fixture
    def v2_rows(self):
        return list(csv.DictReader(open(V2_CSV, encoding="utf-8")))

    @pytest.fixture
    def v2_summary(self):
        return json.loads(V2_JSON.read_text(encoding="utf-8"))

    def test_all_13970_clinics_present(self, v2_rows):
        assert len(v2_rows) == 13970

    def test_rule_a_count_matches_existing_phase4_unchanged(self, v2_summary):
        # Rule A = 既存Phase4のNAME_ADDRESS件数をそのまま踏襲(3,274固定)。
        assert v2_summary["old_matched"] == 3274
        assert v2_summary["match_method_breakdown"]["NAME_ADDRESS_EXACT"] == 3274

    def test_new_matched_is_strictly_higher_than_old(self, v2_summary):
        assert v2_summary["new_matched"] > v2_summary["old_matched"]
        assert v2_summary["added_matched"] == v2_summary["new_matched"] - v2_summary["old_matched"]

    def test_no_duplicate_clinic_or_facility_assignment(self, v2_rows):
        matched = [r for r in v2_rows if r["join_status"] == "MATCHED"]
        cids = [r["clinic_id"] for r in matched]
        fids = [r["mhlw_facility_id"] for r in matched]
        assert len(cids) == len(set(cids)), "同一clinic_idが複数行にMATCHEDされている"
        assert len(fids) == len(set(fids)), "同一mhlw_facility_idが複数clinicにMATCHEDされている(1:1違反)"

    def test_no_cross_medical_type_matches(self, v2_rows):
        """医科clinicが歯科施設に(またはその逆に)MATCHEDされていないことを確認する。

        MHLW facility_typeはCSVに直接出力していないため、mhlw_department_masterの
        facility_type(医科/歯科)から間接的に検証する。
        """
        facility_type = {}
        with open(BASE / "mhlw_department_master.csv", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                facility_type[row["mhlw_facility_id"]] = row["facility_type"]
        import sqlite3
        db = Path(__file__).resolve().parents[1] / "data" / "clinics.sqlite3"
        if not db.exists():
            pytest.skip("production DB not present")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        medical_type = dict(conn.execute("SELECT id, medical_type FROM clinics"))
        conn.close()
        violations = []
        for r in v2_rows:
            if r["join_status"] != "MATCHED" or r["match_method"] == "NAME_ADDRESS_EXACT":
                continue  # Rule A(既存)は今回のガード対象外・変更していない
            mt = medical_type.get(int(r["clinic_id"]), "")
            ft = facility_type.get(r["mhlw_facility_id"], "")
            if mt and ft and mt != ft:
                violations.append((r["clinic_id"], mt, ft))
        assert violations == [], f"医科/歯科の種別不一致MATCHが存在する: {violations[:5]}"

    def test_unique_address_match_all_have_audit_row(self, v2_rows):
        audit_path = BASE / "unique_address_match_audit.csv"
        if not audit_path.exists():
            pytest.skip("audit file not generated")
        audit_ids = {r["clinic_id"] for r in csv.DictReader(open(audit_path, encoding="utf-8"))}
        unique_addr_ids = {r["clinic_id"] for r in v2_rows if r["match_method"] == "UNIQUE_ADDRESS_MATCH"}
        assert unique_addr_ids <= audit_ids

    def test_existing_v1_output_untouched(self):
        # 今回のv2実装がv1(既存Phase4)のNAME_ADDRESS件数を書き換えていないことを確認する。
        with open(V1_CSV, encoding="utf-8", newline="") as f:
            v1_matched = sum(1 for row in csv.DictReader(f) if row["confidence"] == "NAME_ADDRESS" and row["candidate_count"] == "1")
        assert v1_matched == 3274
