"""MHLW(医療情報ネット)診療科 -> Crestix対象9科 mappingの回帰テスト。

前半(TestClassifyLogic)はcrestix_department_mapping.classify()の純粋ロジックのみを検証し、
MHLW元CSVやsidecar DBが無くてもCIで常に実行できる。

後半(TestSidecarRegression)はmhlw_dry_run/clinic_mhlw_departments_final.sqlite3と
./data/clinics.sqlite3 に依存する実データ回帰テストで、生成物が無い環境では
自動的にskipされる(別PCで初回pullした直後などを想定)。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mhlw_dry_run.crestix_department_mapping import CRESTIX_TARGETS, classify  # noqa: E402
from src.master.filters import Filters  # noqa: E402
from src.master.store import ClinicStore, MHLW_SIDECAR_PATH, mhlw_sidecar_available  # noqa: E402

PRODUCTION_DB = Path(__file__).resolve().parents[1] / "data" / "clinics.sqlite3"

# 2026-09-30: ISリーダー正式回答によりREVIEW 37件をALIASへ確定(crestix_department_mapping.py参照)。
# 値は再生成したsidecarから確認した新しい正式値。旧値(ISリーダー回答前): 皮膚科1784,循環器内科1291,
# 消化器内科1331,眼科952,糖尿病内科454,泌尿器科402,産婦人科293,美容整形外科154,歯科32、union 5144。
# 2026-09-30(同日追記): fixed_export.pyだけにあった病院・センター除外をfilters.py側にも適用し、
# UI count = CSV rowsを一致させた(count invariant整理)。除外適用前: 皮膚科1910,循環器内科1329,
# 消化器内科1582,眼科953,糖尿病内科627,泌尿器科434,産婦人科603,美容整形外科395,歯科39、union 5690。
EXPECTED_DEPARTMENT_COUNTS = {
    "皮膚科": 1900, "循環器内科": 1317, "消化器内科": 1574, "眼科": 947, "糖尿病内科": 624,
    "泌尿器科": 429, "産婦人科": 590, "美容整形外科": 392, "歯科": 38,
}
EXPECTED_UNION_UNIQUE_CLINICS = 5660


class TestClassifyLogic:
    """substringでの誤採用(「内科」を含むから対象、等)が絶対に起きないことを固定する。"""

    def test_shindo_naika_is_not_a_target(self):
        # 心療内科：「内科」を含むが心身医学領域。対象9科のいずれにもならない。
        status, target, _ = classify("07003", "心療内科")
        assert status == "EXCLUDE"
        assert target == ""
        assert target not in CRESTIX_TARGETS

    def test_rounen_shindo_naika_is_not_a_target(self):
        status, target, _ = classify("07005", "老年心療内科")
        assert status == "EXCLUDE"
        assert target == ""

    def test_seikei_geka_is_not_biyou_seikei(self):
        # 整形外科(骨・関節)：「整形」を含むが美容整形外科とは全くの別科。
        status, target, _ = classify("02035", "整形外科")
        assert status == "EXCLUDE"
        assert target != "美容整形外科"
        assert target == ""

    def test_ippan_geka_is_unmapped(self):
        # 一般外科(MHLW上の「外科」02001)：美容整形外科はもちろん、どの対象科にも該当しない。
        status, target, _ = classify("02001", "外科")
        assert status == "UNMAPPED"
        assert target == ""

    def test_biyou_geka_is_alias_for_biyou_seikei(self):
        # 美容外科(MHLW標準名)だけがCrestix「美容整形外科」のALIAS。
        status, target, _ = classify("02037", "美容外科")
        assert status == "ALIAS"
        assert target == "美容整形外科"

    def test_exact_literal_matches(self):
        exact_pairs = {
            "01006": "糖尿病内科", "01017": "循環器内科", "01020": "消化器内科",
            "04001": "産婦人科", "05001": "眼科", "06001": "皮膚科",
            "06004": "泌尿器科", "08001": "歯科",
        }
        for code, name in exact_pairs.items():
            status, target, _ = classify(code, name)
            assert status == "EXACT"
            assert target == name

    def test_only_exact_or_alias_ever_map_to_a_crestix_target(self):
        # DECISIONS辞書の全エントリを検査：statusがEXACT/ALIAS以外ならtargetは常に空文字。
        from mhlw_dry_run.crestix_department_mapping import DECISIONS
        for code, (status, target, _reason) in DECISIONS.items():
            if status not in ("EXACT", "ALIAS"):
                assert target == "" or target.endswith("(候補)"), (code, status, target)

    def test_freetext_bucket_is_excluded_not_unmapped(self):
        # 01991等の自由記載バケットはEXCLUDE(統制語彙外)であって、UNMAPPEDと区別する。
        status, target, _ = classify("01991", "インフルエンザ")
        assert status == "EXCLUDE"
        assert target == ""

    def test_unknown_controlled_code_defaults_unmapped(self):
        status, target, _ = classify("09004", "放射線科")
        assert status == "UNMAPPED"
        assert target == ""


sidecar_missing = pytest.mark.skipif(
    not mhlw_sidecar_available(), reason=f"sidecar DB not generated: {MHLW_SIDECAR_PATH}. Run mhlw_dry_run/build_sidecar_db.py first.")
production_db_missing = pytest.mark.skipif(
    not PRODUCTION_DB.exists(), reason=f"production DB not present: {PRODUCTION_DB}")


@sidecar_missing
@production_db_missing
class TestSidecarRegression:
    """実データ(sidecar DB + clinics.sqlite3)に対する回帰テスト。生成物が無い環境ではskip。"""

    @pytest.fixture
    def store(self):
        return ClinicStore(str(PRODUCTION_DB))

    def test_per_department_counts_unchanged(self, store):
        for dept, expected in EXPECTED_DEPARTMENT_COUNTS.items():
            actual = store.count(Filters(active_only=False, hp_only=False, mhlw_departments=[dept]))
            assert actual == expected, f"{dept}: expected {expected}, got {actual}"

    def test_union_of_all_9_targets(self, store):
        actual = store.count(Filters(active_only=False, hp_only=False, mhlw_departments=list(CRESTIX_TARGETS)))
        assert actual == EXPECTED_UNION_UNIQUE_CLINICS

    def test_false_friends_yield_zero(self, store):
        # 「整形外科」「心療内科」「外科」はCrestix対象科名として一切filterに使えない
        # (=sidecarにその名前のcrestix_departmentが存在しないためcount=0になる)。
        for name in ("整形外科", "心療内科", "外科"):
            assert store.count(Filters(active_only=False, hp_only=False, mhlw_departments=[name])) == 0

    def test_or_filter_does_not_duplicate_clinics(self, store):
        # 複数診療科に該当する医院が一覧で重複表示されないことを、生のidリストの重複有無で確認する。
        f = Filters(active_only=False, hp_only=False, mhlw_departments=["消化器内科", "循環器内科"])
        with store.connect() as c:
            from src.master.filters import where
            sql, args = where(f)
            ids = [r[0] for r in c.execute("SELECT id FROM clinics WHERE " + sql, args)]
        assert len(ids) == len(set(ids))
        assert len(ids) == store.count(f)

    def test_final_sidecar_population_is_fixed(self):
        with sqlite3.connect(f"file:{MHLW_SIDECAR_PATH}?mode=ro", uri=True) as conn:
            clinics, records, names = conn.execute(
                "SELECT count(DISTINCT clinic_id),count(*),count(DISTINCT mhlw_department_name) "
                "FROM clinic_mhlw_departments_final").fetchone()
        assert (clinics, records, names) == (9830, 28117, 227)

    def test_mapping_status_is_always_exact_or_alias_in_sidecar(self):
        # sidecarに入っているmapping_statusはEXACT/ALIASのみ(REVIEW/UNMAPPED/EXCLUDEは営業filterに使わない)。
        with sqlite3.connect(f"file:{MHLW_SIDECAR_PATH}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT mapping_status,crestix_department FROM clinic_mhlw_departments").fetchall()
        assert all(not category or status in {"EXACT", "ALIAS"} for status, category in rows)


class TestSidecarAbsentSafety:
    """sidecarが存在しない環境でも、既存機能がクラッシュしないことを確認する(silent failure禁止の裏返し)。"""

    def test_mhlw_sidecar_available_reflects_file_presence(self):
        assert mhlw_sidecar_available() == MHLW_SIDECAR_PATH.exists()

    @production_db_missing
    def test_existing_filters_unaffected_when_mhlw_departments_empty(self):
        store = ClinicStore(str(PRODUCTION_DB))
        # mhlw_departments=[]("指定なし")のときはATTACHの有無に関係なく常に安全に動く。
        # 13970は全legacy件数。病院・センター除外(必須条件)を差し引いた13254が正しい母数。
        assert store.count(Filters(active_only=False, hp_only=False)) == 13254
