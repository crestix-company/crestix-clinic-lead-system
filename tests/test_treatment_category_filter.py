"""診療科 → 治療カテゴリ抽出（synthetic fixture。本番DBは使わない）。"""
import html
import pytest
from streamlit.testing.v1 import AppTest
from src.utils.config import ROOT, read_config
from src.enrichment.hp_analysis import Page
from src.master.filters import Filters
from src.master.samples import sample_records
from src.master.store import ClinicStore
from src.normalizer.departments import normalize_departments, DEPARTMENTS
from src.scoring.research_scoring import treatments

ALL = dict(active_only=False, hp_only=False)


def menu_page(name, *labels, body=""):
    links = "".join(f"<a href='/t{i}'>{html.escape(l)}</a>" for i, l in enumerate(labels))
    return Page("https://clinic.example/", f"<title>{html.escape(name)}</title><h1>{html.escape(name)}</h1>{links}{body}")


def cats(name, *labels, record=None, pages=None):
    rec = {"clinic_name": name, **(record or {})}
    return treatments(pages or [menu_page(name, *labels)], record=rec)["treatment_categories"]


# ---- 診療科normalizer -------------------------------------------------
def test_existing_ten_departments_are_kept():
    assert normalize_departments("内 消 循 糖 眼 皮 ひ 整外 歯") == ["内科", "消化器内科", "循環器内科", "糖尿病内科", "眼科", "皮膚科", "泌尿器科", "整形外科", "歯科"]
    assert normalize_departments("不明な科") == ["その他"]
    assert DEPARTMENTS[-1] == "その他"


@pytest.mark.parametrize("raw,expected", [
    ("呼内", "呼吸器内科"), ("心内", "心療内科"), ("精", "精神科"), ("小", "小児科"), ("脳外", "脳神経外科"),
    ("形外", "形成外科"), ("美外", "美容外科"), ("アレ", "アレルギー科"), ("耳い", "耳鼻咽喉科"),
    ("腎臓内科", "腎臓内科"), ("血液内科", "血液内科"), ("乳腺外科", "乳腺外科"), ("肛門外科", "肛門外科"),
    ("血管外科", "血管外科"), ("心臓血管外科", "心臓血管外科"), ("産婦人科", "産婦人科"), ("婦人科", "婦人科"),
    ("神経内科", "神経内科"), ("美容皮膚科", "皮膚科"), ("小児皮膚科", "皮膚科"), ("小児眼科", "眼科"),
    ("女性泌尿器科", "泌尿器科"), ("小児耳鼻咽喉科", "耳鼻咽喉科"), ("児童精神科", "精神科"), ("矯歯", "歯科"),
])
def test_new_department_names(raw, expected):
    assert normalize_departments(raw) == [expected]


@pytest.mark.parametrize("raw", ["産婦", "婦", "産", "神内", "脳内", "神", "心外", "呼", "外", "麻", "放"])
def test_ambiguous_abbreviations_are_not_guessed(raw):
    assert normalize_departments(raw) == ["その他"]


# ---- 診療科 → 治療カテゴリ 対応表 ---------------------------------------
def test_department_treatment_mapping_uses_known_names():
    mapping = read_config(ROOT / "config/treatment_departments.yml")
    keywords = read_config(ROOT / "config/treatment_keywords.yml")
    assert mapping["眼科"] == ["白内障", "緑内障", "ICL", "オルソケラトロジー", "硝子体"]
    assert mapping["皮膚科"] == ["ニキビ・ニキビ跡", "日帰り手術", "アトピー・乾癬"]
    assert mapping["歯科"] == ["矯正", "インプラント"]
    for dept, items in mapping.items():
        assert dept in DEPARTMENTS
        assert all(t in keywords for t in items), dept


def test_existing_keywords_are_kept():
    kw = read_config(ROOT / "config/treatment_keywords.yml")
    for k in ["胃内視鏡", "上部消化管内視鏡", "下部消化管内視鏡", "消化管内視鏡", "消化器内視鏡", "内視鏡", "胃カメラ", "大腸カメラ",
              "大腸内視鏡", "大腸ポリープ切除", "日帰りポリープ切除", "鎮静剤 内視鏡", "ポリープ切除"]:
        assert k in kw["内視鏡"]
    assert {"ICL", "眼内コンタクトレンズ"} <= set(kw["ICL"])
    assert {"泌尿器科", "尿路結石", "前立腺", "膀胱鏡"} <= set(kw["泌尿器科"])
    assert {"ED", "ED治療", "ED外来", "ED治療薬"} <= set(kw["ED"])
    assert "男性更年期" in kw and "循環器内科" in kw and "皮膚科" in kw


# ---- 治療カテゴリ Positive -----------------------------------------------
@pytest.mark.parametrize("category,label", [
    ("オルソケラトロジー", "オルソケラトロジー"), ("硝子体", "硝子体手術"), ("白内障", "日帰り白内障手術"), ("緑内障", "MIGS"),
    ("ICL", "眼内コンタクトレンズ"),
    ("泌尿器科", "ESWL 体外衝撃波結石破砕術"), ("泌尿器科", "HoLEP"), ("泌尿器科", "性病検査"), ("泌尿器科", "過活動膀胱ボトックス"),
    ("下肢静脈瘤", "下肢静脈瘤 日帰り手術"), ("下肢静脈瘤", "血管内焼灼術"), ("睡眠時無呼吸", "CPAP"), ("睡眠時無呼吸", "SAS外来"),
    ("日帰り手術", "粉瘤手術（くり抜き法）"), ("日帰り手術", "眼瞼下垂手術"),
    ("アトピー・乾癬", "デュピクセント"), ("アトピー・乾癬", "アトピー性皮膚炎の生物学的製剤"), ("アトピー・乾癬", "紫外線療法"),
    ("糖尿病", "糖尿病専門外来"), ("糖尿病", "CGM"), ("糖尿病", "1型糖尿病"),
    ("内視鏡", "胃カメラ"), ("内視鏡", "大腸カメラ"), ("内視鏡", "大腸ポリープ切除"),
])
def test_positive_menu_labels(category, label):
    assert category in cats("テストクリニック", label)


def test_ed_and_men_menopause_and_existing_categories_still_work():
    assert "ED" in cats("テストクリニック", "ED治療")
    assert "男性更年期" in cats("テストクリニック", "男性更年期外来")
    assert "循環器内科" in cats("テストクリニック", "心エコー")
    assert "皮膚科" in cats("テストクリニック", "ニキビ治療")


def test_one_clinic_keeps_multiple_categories():
    result = cats("消化器・糖尿病クリニック", "胃カメラ", "糖尿病専門外来")
    assert {"内視鏡", "糖尿病"} <= set(result)


# ---- 内視鏡 --------------------------------------------------------------
def test_endoscopy_polyp_context():
    assert "内視鏡" not in cats("婦人科サンプル", "子宮鏡下内膜ポリープ切除術")
    assert "内視鏡" not in cats("婦人科サンプル", "ポリープ切除")
    assert "内視鏡" in cats("テストクリニック", "大腸ポリープ切除")
    # 医院名・診療科は条件にしない。keyword周辺の文脈だけで判定する。
    assert "内視鏡" in cats("消化器クリニック", "日帰りポリープ切除")
    assert "内視鏡" in cats("テストクリニック", "日帰りポリープ切除")
    assert "内視鏡" in cats("女性と消化器のクリニック", "日帰りポリープ切除", record={"departments": "婦"})
    assert "内視鏡" not in cats("テストクリニック", "婦人科 日帰りポリープ切除")
    assert "内視鏡" not in cats("テストクリニック", "鼻茸（鼻ポリープ）日帰りポリープ切除")
    assert "内視鏡" not in cats("テストクリニック", "子宮内膜ポリープ 日帰りポリープ切除")


def test_gynecology_endoscopy_and_gastroscopy_are_true_for_clinic():
    result = cats("女性と消化器のクリニック", "子宮鏡・腹腔鏡・婦人科内視鏡", "胃カメラ")
    assert "内視鏡" in result


# ---- 糖尿病 ----------------------------------------------------------------
def test_diabetes_incidental_care_is_excluded():
    page = menu_page("一般内科クリニック", "風邪", "高血圧", "脂質異常症", "糖尿病")
    assert "糖尿病" not in treatments([page], record={"clinic_name": "一般内科クリニック", "departments": "内科"})["treatment_categories"]


def test_diabetes_positive_by_keyword_and_by_department():
    assert "糖尿病" in cats("テストクリニック", "糖尿病内科")
    assert "糖尿病" in cats("テストクリニック", "糖尿病専門外来")
    assert "糖尿病" in cats("テストクリニック", "CGM")
    by_dept = treatments([menu_page("テストクリニック", "風邪")], record={"clinic_name": "テストクリニック", "departments": "内 糖内"})
    assert "糖尿病" in by_dept["treatment_categories"]
    assert any(e["source"] == "DEPARTMENT" for e in by_dept["treatment_evidence"])


def test_diabetes_clinic_name_is_strong_evidence():
    assert "糖尿病" in cats("小川内科・糖尿病クリニック", "風邪", record={"departments": "内"})
    assert "糖尿病" in cats("浦上小児内分泌・糖尿病クリニック", "診療案内")
    assert "糖尿病" not in cats("一般内科クリニック", "高血圧・脂質異常症・糖尿病などに対応", record={"departments": "内"})
    assert "糖尿病" not in cats("糖尿病網膜症眼科", "白内障")


def test_diabetic_retinopathy_is_still_not_diabetes():
    assert "糖尿病" not in cats("眼科サンプル", "糖尿病網膜症")


# ---- 皮膚科 ニキビ ----------------------------------------------------------
def test_acne_requires_selfpay_or_beauty_dermatology_not_price():
    plain = {"clinic_name": "一般皮膚科クリニック", "departments": "皮"}
    assert "ニキビ・ニキビ跡" not in cats("一般皮膚科クリニック", "ニキビ治療（保険診療）", record=plain)
    assert "ニキビ・ニキビ跡" not in cats("一般皮膚科クリニック", "ニキビにも対応", record=plain)
    assert "ニキビ・ニキビ跡" not in cats("一般皮膚科クリニック", "ニキビ跡治療", record=plain)
    assert "ニキビ・ニキビ跡" not in cats("一般皮膚科クリニック", "クレーター治療", record=plain)
    for label in ["ポテンツァ", "ダーマペン", "サブシジョン", "フラクショナルレーザー", "イソトレチノイン"]:
        assert "ニキビ・ニキビ跡" in cats("一般皮膚科クリニック", label, record=plain), label
    assert "ニキビ・ニキビ跡" in cats("美容皮膚科クリニック", "ニキビ跡治療", record={"departments": "皮"})
    assert "ニキビ・ニキビ跡" in cats("美容皮膚科クリニック", "美容皮膚科", "ポテンツァ", record={"departments": "皮"})
    assert "ニキビ・ニキビ跡" in cats("テストクリニック", "自由診療", "ダーマペン", record={"departments": "皮"})
    assert "ニキビ・ニキビ跡" in cats("テストクリニック", "ダーマペン", record={"departments": "美容皮膚科"})


def test_acne_selfpay_from_dedicated_page_body():
    home = menu_page("テスト皮膚科", "ニキビ跡治療")
    detail = Page("https://clinic.example/t0", "<title>ニキビ跡治療</title><h1>ニキビ跡治療</h1><p>自費診療です。</p>")
    assert "ニキビ・ニキビ跡" in treatments([home, detail], record={"clinic_name": "テスト皮膚科"})["treatment_categories"]
    detail2 = Page("https://clinic.example/t0", "<title>ニキビ跡治療</title><h1>ニキビ跡治療</h1><p>保険診療です。</p>")
    assert "ニキビ・ニキビ跡" not in treatments([home, detail2], record={"clinic_name": "テスト皮膚科"})["treatment_categories"]


# ---- 汎用語の文脈 ----------------------------------------------------------
def test_generic_terms_need_context():
    assert "下肢静脈瘤" not in cats("テストクリニック", "日帰り手術")
    assert "下肢静脈瘤" not in cats("テストクリニック", "レーザー治療")
    assert "アトピー・乾癬" not in cats("リウマチ科サンプル", "関節リウマチの生物学的製剤")
    assert "日帰り手術" not in cats("テストクリニック", "日帰り手術")


# ---- 歯科 ----------------------------------------------------------------
DENTAL = {"departments": "歯", "medical_type": "歯科"}


def test_dental_orthodontics_and_implant_positive():
    for label in ["矯正歯科", "歯列矯正", "マウスピース矯正", "インビザライン"]:
        assert "矯正" in cats("テスト歯科", label, record=DENTAL), label
    assert "矯正" in cats("テスト歯科", "矯正治療", record=DENTAL)
    for label in ["インプラント", "インプラント治療", "口腔インプラント"]:
        assert "インプラント" in cats("テスト歯科", label, record=DENTAL), label
    assert "インプラント" in cats("麻布インプラント歯科", "診療案内", record=DENTAL)


def test_dental_negative_cases():
    assert "インプラント" not in cats("美容外科クリニック", "豊胸 シリコンインプラント")
    assert "インプラント" not in cats("美容外科クリニック", "豊胸インプラント", record=DENTAL)
    assert "インプラント" not in cats("婦人科クリニック", "避妊インプラント")
    assert "インプラント" not in cats("テストクリニック", "神経刺激装置インプラント")
    assert "インプラント" not in cats("テストクリニック", "電子インプラント")
    assert "インプラント" not in cats("テストクリニック", "金属インプラント")
    assert "インプラント" not in cats("テストクリニック", "インプラント")
    assert "矯正" not in cats("整形外科クリニック", "O脚矯正治療")
    assert "矯正" not in cats("眼科クリニック", "視力矯正治療")
    assert "矯正" not in cats("耳鼻科クリニック", "鼻中隔矯正術")
    assert "矯正" not in cats("整形外科クリニック", "巻き爪矯正")
    assert "矯正" not in cats("眼科クリニック", "屈折矯正手術")
    assert "矯正" not in cats("テストクリニック", "頭蓋矯正ヘルメット")
    assert "矯正" not in cats("テスト歯科", "O脚矯正治療", record=DENTAL)


def test_implant_auxiliary_terms_are_not_positive_alone():
    for label in ["インプラント周囲炎", "日本口腔インプラント学会", "インプラント講習", "インプラント除去", "インプラントリカバリー"]:
        assert "インプラント" not in cats("テスト歯科", label, record=DENTAL), label
    assert "インプラント" in cats("テスト歯科", "インプラント周囲炎", "インプラント治療", record=DENTAL)


# ---- 診療科フィルター（DB） ----------------------------------------------------
def _clinic(i, name, departments, medical_type="医科"):
    r = dict(sample_records()[0])
    r.update({"clinic_id": f"c-{i}", "clinic_name": name, "phone": f"03-1111-{i:04d}", "address": f"東京都千代田区試験町{i}-1-1",
              "departments": departments, "medical_type": medical_type,
              "facility_type": "歯科診療所" if medical_type == "歯科" else "診療所"})
    return r


@pytest.fixture
def filter_store(tmp_path):
    store = ClinicStore(tmp_path / "synthetic.db")
    specs = [
        (1, "あ眼科", "眼", ["白内障"]), (2, "い眼科", "眼", ["硝子体"]), (3, "う眼科", "眼", ["緑内障", "ICL"]),
        (4, "え眼科", "眼", ["オルソケラトロジー"]), (5, "お眼科", "眼", []),
        (6, "か皮膚科", "皮", ["ニキビ・ニキビ跡"]), (7, "き皮膚科", "皮", []),
        (8, "く泌尿器科", "ひ", ["泌尿器科"]), (9, "け内科", "内 糖内", ["糖尿病", "内視鏡"]),
        (10, "こ歯科", "歯", ["矯正"]), (11, "さ歯科", "歯", ["インプラント"]), (12, "し歯科", "歯", []),
    ]
    store.import_master([_clinic(i, n, d, "歯科" if "歯" in d else "医科") for i, n, d, _ in specs])
    ids = {r["clinic_name"]: r["id"] for r in store.query(Filters(**ALL), limit=100)}
    for i, n, d, cs in specs:
        store.save_research(ids[n], {"hp_status": "VERIFIED", "hp_url": "https://x.example/", "treatment_categories": cs})
    return store


def names(store, **kw):
    return sorted(r["clinic_name"] for r in store.query(Filters(**ALL, **kw), limit=100))


def test_department_only_returns_whole_department(filter_store):
    assert names(filter_store, departments=["眼科"]) == ["あ眼科", "い眼科", "う眼科", "え眼科", "お眼科"]
    assert names(filter_store, departments=["皮膚科"]) == ["か皮膚科", "き皮膚科"]
    assert names(filter_store, departments=["泌尿器科"]) == ["く泌尿器科"]
    assert names(filter_store, departments=["歯科"]) == ["こ歯科", "さ歯科", "し歯科"]


@pytest.mark.parametrize("cat,expected", [("白内障", ["あ眼科"]), ("硝子体", ["い眼科"]), ("緑内障", ["う眼科"]), ("ICL", ["う眼科"]), ("オルソケラトロジー", ["え眼科"])])
def test_ophthalmology_with_treatment(filter_store, cat, expected):
    assert names(filter_store, departments=["眼科"], treatments=[cat]) == expected


def test_or_within_item_and_and_between_items(filter_store):
    assert names(filter_store, departments=["眼科"], treatments=["白内障", "緑内障"]) == ["あ眼科", "う眼科"]
    assert names(filter_store, departments=["皮膚科"], treatments=["白内障"]) == []
    assert names(filter_store, departments=["眼科", "皮膚科"], treatments=["硝子体", "ニキビ・ニキビ跡"]) == ["い眼科", "か皮膚科"]


def test_dental_with_treatment(filter_store):
    assert names(filter_store, departments=["歯科"], treatments=["矯正"]) == ["こ歯科"]
    assert names(filter_store, departments=["歯科"], treatments=["インプラント"]) == ["さ歯科"]


def test_multiple_departments_and_treatments_per_clinic(filter_store):
    row = next(r for r in filter_store.query(Filters(**ALL, keyword="け内科")))
    detail = filter_store.get(row["id"])
    assert set(detail["treatment_categories"]) == {"糖尿病", "内視鏡"}
    assert {"内科", "糖尿病内科"} <= set(detail["normalized_departments"])
    assert names(filter_store, departments=["内科"], treatments=["糖尿病"]) == ["け内科"]
    assert names(filter_store, departments=["糖尿病内科"], treatments=["内視鏡"]) == ["け内科"]


# ---- UI ----------------------------------------------------------------
def _sales_app(tmp_path, monkeypatch):
    db_path = tmp_path / "ui.db"
    ClinicStore(db_path)  # CLINIC_DB_PATH必須化に対応し、事前に空のスキーマだけ用意する
    monkeypatch.setenv("CLINIC_DB_PATH", str(db_path))
    monkeypatch.setenv("CLINIC_DEMO_DB_PATH", str(tmp_path / "demo.db"))
    at = AppTest.from_file(str(ROOT / "app_v2.py"), default_timeout=30).run()
    at.session_state["navigation"] = "営業対象・出力"
    return at.run()


def test_sales_ui_has_two_level_selection(tmp_path, monkeypatch):
    at = _sales_app(tmp_path, monkeypatch)
    assert not at.exception
    dep = next(m for m in at.multiselect if m.label == "診療科")
    assert "眼科" in dep.options and "歯科" in dep.options
    dep.select("眼科").run()
    tr = next(m for m in at.multiselect if m.label == "眼科の治療")
    assert tr.options[:5] == ["白内障", "白内障手術", "日帰り白内障手術", "多焦点眼内レンズ", "選定療養 白内障"]
    next(m for m in at.multiselect if m.label == "診療科").select("歯科").run()
    assert next(m for m in at.multiselect if m.label == "歯科の治療").options[-1].startswith("サイナスリフト")
    assert not at.exception
    assert "Tavily" not in " ".join(str(e.value) for e in list(at.markdown) + list(at.caption))


def test_sales_ui_keeps_selected_treatment_valid_after_department_change(tmp_path, monkeypatch):
    at = _sales_app(tmp_path, monkeypatch)
    next(m for m in at.multiselect if m.label == "診療科").select("眼科").run()
    next(m for m in at.multiselect if m.label == "眼科の治療").select("ICL").run()
    dep = next(m for m in at.multiselect if m.label == "診療科")
    dep.unselect("眼科").select("歯科").run()
    assert not at.exception
    assert all(m.label != "眼科の治療" for m in at.multiselect)
    assert next(m for m in at.multiselect if m.label == "歯科の治療").value == []


# ---- 実医院52件回帰で見つかったケース（全カテゴリ共通の記事・告知除外、糖尿病表記揺れ） ----
def _home(name, *links):
    body = "".join(f"<a href='{html.escape(h)}'>{html.escape(t)}</a>" for h, t in links)
    return Page("https://clinic.example/", f"<title>{html.escape(name)}</title><h1>{html.escape(name)}</h1>{body}")


def _cats_links(name, *links, record=None):
    return treatments([_home(name, *links)], record={"clinic_name": name, **(record or {})})["treatment_categories"]


def test_column_article_link_is_not_treatment_evidence():
    # 京野アート型: /column/ の記事リンクだけでは睡眠時無呼吸にしない
    assert "睡眠時無呼吸" not in _cats_links("不妊クリニック", ("/column/post-8886", "病気のはなし⑧ 睡眠時無呼吸症候群は精液所見や性機能に影響するか"))
    # 水道橋型: /blog/ の記事リンクだけではニキビにしない
    assert "ニキビ・ニキビ跡" not in _cats_links("皮フ科", ("/blog/isotretinoin", "【皮膚科専門医解説・監修】脂腺増殖症にイソトレチノインの内服は効果ある？"))
    assert "内視鏡" not in _cats_links("消化器クリニック", ("/topics/123/", "胃カメラ検査の新機器を導入しました"))
    assert "白内障" not in _cats_links("眼科", ("/notice/1/", "白内障手術"))


def test_same_treatment_on_menu_page_is_still_positive():
    assert "睡眠時無呼吸" in _cats_links("内科", ("/sas/", "睡眠時無呼吸症候群"), ("/column/1", "コラム一覧"))
    assert "ニキビ・ニキビ跡" in _cats_links("皮フ科", ("/beauty/isotretinoin/", "イソトレチノイン"), ("/blog/x", "イソトレチノインの解説"))


def test_ended_or_suspended_treatment_is_not_positive():
    # 中島クリニック型: 終了告知はED・泌尿器科の根拠にしない
    result = _cats_links("内科クリニック", ("/outpatient/ed/", "ED治療終了のご案内"))
    assert "ED" not in result and "泌尿器科" not in result
    for label in ["オンラインED治療は休止しています", "ED治療を中止しました", "現在ED治療は行っていません"]:
        assert "ED" not in _cats_links("内科クリニック", ("/ed/", label)), label
    # 否定はその根拠だけを除外し、医院全体をFalseにしない
    both = _cats_links("内科クリニック", ("/ed-end/", "ED治療終了のご案内"), ("/aga/", "AGA・ED治療"))
    assert "ED" in both


def test_diabetes_department_variants_and_outpatient_are_positive():
    for label in ["糖尿病・内分泌内科", "糖尿病・代謝内科", "糖尿病代謝内科", "糖尿病外来", "糖尿病・内分泌内科についてを見る"]:
        assert "糖尿病" in cats("テストクリニック", label), label
    assert "糖尿病" in cats("○○糖尿病クリニック", "診療案内")


def test_diabetes_general_page_without_specialty_is_false():
    # 青栁クリニック型: 糖尿病メニュー＋一般解説（HbA1c・インスリンの説明）だけではFalse
    home = _home("内科・循環器クリニック", ("/diabetes.html", "糖尿病"), ("/hypertension.html", "高血圧"))
    detail = Page("https://clinic.example/diabetes.html",
                  "<title>糖尿病</title><h1>糖尿病</h1><p>糖尿病の原因は？HbA1cは過去1〜2か月の血糖の平均です。"
                  "進行するとインスリン注射が必要になることがあります。糖尿病の治療なら当院へ。</p>")
    record = {"clinic_name": "内科・循環器クリニック", "departments": "内 循環器内科"}
    assert "糖尿病" not in treatments([home, detail], record=record)["treatment_categories"]
    # 三好クリニック型: 他院の専門外来への紹介はPositiveにしない
    home2 = _home("内科クリニック", ("/dm/index.html", "糖尿病"), ("/dm/1.html", "糖尿病1"))
    assert "糖尿病" not in treatments([home2], record={"clinic_name": "内科クリニック", "departments": "内"})["treatment_categories"]
    assert "糖尿病" not in cats("一般内科クリニック", "高血圧・脂質異常症・糖尿病", record={"departments": "内"})
