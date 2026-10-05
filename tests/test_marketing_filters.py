"""広告・集客施策フィルター・施策数・HP制作会社（synthetic fixture。本番DBは使わない）。"""
import pytest
from streamlit.testing.v1 import AppTest
from src.utils.config import ROOT, read_config
from src.enrichment.hp_analysis import Page
from src.master.filters import Filters, AD_COUNT_SQL
from src.master.samples import sample_records
from src.master.scope import SCOPE_ALL
from src.master.store import ClinicStore
from src.scoring.research_scoring import (hp_signals, production_companies, analyze, AD_SIGNAL_NAMES, AD_SIGNAL_LABELS,
                                          SIGNAL_NAMES, dedupe_signals, signal, finalize_result)

ALL = dict(active_only=False, hp_only=False)
REC = {"clinic_name": "テストクリニック", "phone": "03-0000-0001", "address": "東京都千代田区試験町1-1-1"}


def home(body, url="https://clinic.example/"):
    return Page(url, f"<title>テストクリニック</title><h1>テストクリニック</h1>{body}")


def names(body):
    return [s["name"] for s in hp_signals(REC, [home(body)])]


def ad_count(signals):
    return len([n for n in dict.fromkeys(signals) if n in AD_SIGNAL_NAMES])


# ---- 施策定義 --------------------------------------------------------------
def test_ad_signal_definition_is_18_and_excludes_production_and_midday():
    assert len(AD_SIGNAL_NAMES) == 18
    assert "HP制作会社の制作実績" not in AD_SIGNAL_NAMES
    assert "昼の検査・手術専用枠" not in AD_SIGNAL_NAMES
    assert set(AD_SIGNAL_LABELS) == set(AD_SIGNAL_NAMES)
    assert len(SIGNAL_NAMES) == 20  # 既存の定義は削除しない
    for label in ["Doctors File", "Medical DOC", "地域ドクターズ", "Instagram", "LINE", "YouTube", "TikTok",
                  "オンライン診療", "AIチャット", "漫画", "専門サイト", "独自LP"]:
        assert label in AD_SIGNAL_LABELS.values()


# ---- 個別施策 --------------------------------------------------------------
IG = "<a href='https://www.instagram.com/test_clinic/'>Instagram</a>"
LINE = "<a href='https://lin.ee/abcd'>LINE予約</a>"
YT = "<a href='https://www.youtube.com/@testclinic'>YouTube</a>"


def test_instagram_only_counts_one():
    got = names(IG)
    assert got == ["Instagram公式運用"] and ad_count(got) == 1


def test_three_sns_count_three():
    assert ad_count(names(IG + LINE + YT)) == 3


def test_same_signal_multiple_links_counts_once():
    got = names(IG + IG.replace("test_clinic", "test_clinic2") + IG)
    assert got.count("Instagram公式運用") == 1 and ad_count(got) == 1


def test_online_consultation_negative_is_not_counted():
    ended = names("<p><a href='/online/'>オンライン診療を終了しました</a></p>")
    assert "オンライン診療" not in ended and ad_count(ended) == 0
    for text in ["オンライン診療は休止中です", "オンライン診療を中止しました", "現在オンライン診療は行っていません"]:
        assert "オンライン診療" not in names(f"<p><a href='/online/'>{text}</a></p>"), text
    assert "オンライン診療" in names("<p><a href='/online/'>オンライン診療のご予約はこちら</a></p>")


# ---- HP制作会社 ------------------------------------------------------------
FOOTER_ZERO = "<footer><a href='https://zeromedical.tv/'>ホームページ制作 ゼロメディカル</a></footer>"


def test_production_company_is_not_ad_signal():
    page = home(IG + FOOTER_ZERO)
    comp = production_companies([page])["hp_production_companies"]
    sig = [s["name"] for s in hp_signals(REC, [page])]
    assert comp == ["ゼロメディカル"]
    assert ad_count(sig) == 1  # 制作会社は広告・集客施策数に含めない


@pytest.mark.parametrize("html,company", [
    ("<footer><a href='https://www.dr-bridge.co.jp/'>DR.BRIDGE｜クリニックホームページ制作</a></footer>", "DR.BRIDGE"),
    ("<footer><a href='https://medical.depoc.jp/'>DEPOC</a></footer>", "DEPOC"),
    ("<footer><a href='https://www.depoc-medical.jp/'>医療ホームページ制作</a></footer>", "DEPOC"),
    ("<div class='copyright'>Web Design by grits</div>", "grits"),
    ("<footer>ホームページ制作：グリッツ</footer>", "grits"),
    ("<footer><a href='https://medical-grits.jp/'>制作</a></footer>", "grits"),
    ("<footer>produced by HERO innovation</footer>", "HERO innovation"),
    ("<footer><a href='https://clinic-promotion.com/'>ヒーローイノベーション</a></footer>", "HERO innovation"),
    ("<footer>Produced by Method Innovation</footer>", "Method Innovation"),
    ("<footer><a href='https://method-innovation.co.jp/'>HP制作 メソッドイノベーション</a></footer>", "Method Innovation"),
    ("<p>サイト情報 <a href='https://zeromedical.tv/'>ホームページ制作</a></p>", "ゼロメディカル"),
])
def test_production_company_detection(html, company):
    assert production_companies([home(html)])["hp_production_companies"] == [company]


def test_production_company_negative_cases():
    body = ("<main><p>素敵なホームページを作っていただいた株式会社HERO innovationの熊川様に心から感謝しております。"
            "その他、お手伝いをしていただいた方々にこの場を持ってお礼を述べさせていただきます。</p></main>")
    assert production_companies([home(body)])["hp_production_companies"] == []
    assert production_companies([home("<footer>Powered by WordPress</footer>")])["hp_production_companies"] == []
    assert production_companies([home("<footer><a href='https://doctorsfile.jp/h/1/'>Doctors File</a></footer>")])["hp_production_companies"] == []
    assert production_companies([home("<footer><a href='https://medicaldoc.jp/c/1/'>Medical DOC</a></footer>")])["hp_production_companies"] == []
    # 自院ドメインへのリンクは制作会社ではない
    assert production_companies([home("<footer><a href='https://clinic.example/grits'>grits</a></footer>")])["hp_production_companies"] == []


def test_existing_production_signal_is_unchanged():
    page = home("<footer><a href='https://www.dr-bridge.co.jp/'>DR.BRIDGE｜クリニックホームページ制作</a></footer>")
    assert "HP制作会社の制作実績" in [s["name"] for s in hp_signals(REC, [page])]
    result = analyze(REC, [page])
    assert result["hp_production_companies"] == ["DR.BRIDGE"]


def test_existing_signal_count_and_hot_status_are_unchanged():
    sigs = [signal(n, "", "t", "t") for n in ["Instagram公式運用", "HP制作会社の制作実績", "昼の検査・手術専用枠"]]
    data = finalize_result({"marketing_signals": sigs})
    assert data["marketing_signal_count"] == 3 and data["hot_status"] == "かなりアツい"


# ---- フィルター（DB） --------------------------------------------------------
def _clinic(i, name, departments):
    r = dict(sample_records()[0])
    r.update({"clinic_id": f"m-{i}", "clinic_name": name, "phone": f"03-2222-{i:04d}",
              "address": f"東京都千代田区広告町{i}-1-1", "departments": departments})
    return r


SPECS = [
    # name, 診療科, 治療, signals, 制作会社
    (1, "あ眼科", "眼", ["白内障"], ["Instagram公式運用", "Doctors File掲載", "YouTube公式運用"], ["HERO innovation"]),
    (2, "い眼科", "眼", ["白内障"], ["LINE公式運用", "HP制作会社の制作実績", "昼の検査・手術専用枠"], ["grits"]),
    (3, "う眼科", "眼", ["白内障"], ["TikTok公式運用", "Doctors File掲載", "YouTube公式運用"], ["grits"]),
    (4, "え眼科", "眼", ["緑内障"], ["Instagram公式運用", "LINE公式運用", "オンライン診療"], ["grits"]),
    (5, "お皮膚科", "皮", ["ニキビ・ニキビ跡"], ["Instagram公式運用", "LINE公式運用", "マイナビ記事掲載", "Caloo Plus"], ["ゼロメディカル"]),
    (6, "か皮膚科", "皮", [], [], []),
]


@pytest.fixture
def mstore(tmp_path):
    store = ClinicStore(tmp_path / "marketing.db")
    store.import_master([_clinic(i, n, d) for i, n, d, *_ in SPECS])
    ids = {r["clinic_name"]: r["id"] for r in store.query(Filters(**ALL), limit=100)}
    for i, n, d, t, s, comp in SPECS:
        treatment_evidence = [
            {"category": category, "keyword": category, "source": "HOME_MENU", "confidence": .98}
            for category in t
        ]
        store.save_research(ids[n], {"hp_status": "VERIFIED", "hp_url": "https://x.example/", "treatment_categories": t,
                                     "treatment_evidence": treatment_evidence,
                                     "marketing_signals": [signal(x, "", "t", "t") for x in s], "hp_production_companies": comp})
    return store


def found(store, **kw):
    return sorted(r["clinic_name"] for r in store.query(Filters(**ALL, **kw), limit=100))


def test_ad_selection_is_or(mstore):
    assert found(mstore, signals=["Instagram公式運用", "LINE公式運用"]) == ["あ眼科", "い眼科", "え眼科", "お皮膚科"]
    assert "い眼科" in found(mstore, signals=["Instagram公式運用", "LINE公式運用"])  # Instagram=False, LINE=True


def test_ad_count_excludes_production_and_midday_and_counts_other_ads(mstore):
    with mstore.connect() as c:
        got = dict(c.execute("SELECT clinic_name," + AD_COUNT_SQL + " FROM clinics").fetchall())
    assert got == {"あ眼科": 3, "い眼科": 1, "う眼科": 3, "え眼科": 3, "お皮膚科": 4, "か皮膚科": 0}
    assert found(mstore, ad_min=2) == ["あ眼科", "う眼科", "え眼科", "お皮膚科"]
    assert found(mstore, ad_min=4) == ["お皮膚科"]
    # 既存の signal_count は変更しない（い眼科は制作会社・昼枠を含めて3のまま）
    row = mstore.get(next(r["id"] for r in mstore.query(Filters(**ALL, keyword="い眼科"))))
    assert row["marketing_signal_count"] == 3


def test_production_company_filter_is_or(mstore):
    assert found(mstore, production_companies=["HERO innovation", "grits"]) == ["あ眼科", "い眼科", "う眼科", "え眼科"]
    assert found(mstore, production_companies=["ゼロメディカル"]) == ["お皮膚科"]


def test_all_fields_are_and(mstore):
    got = found(mstore, departments=["眼科"], treatments=["白内障"], signals=["Instagram公式運用", "LINE公式運用"],
                ad_min=3, production_companies=["HERO innovation", "grits"])
    # い眼科: LINEありだが施策数1 / う眼科: IG・LINEなし / え眼科: 白内障でない
    assert got == ["あ眼科"]
    assert found(mstore, departments=["皮膚科"], treatments=["ニキビ・ニキビ跡"], production_companies=["ゼロメディカル", "grits"], ad_min=3) == ["お皮膚科"]


def test_midday_signal_filter_still_works(mstore):
    assert found(mstore, signals=["昼の検査・手術専用枠"]) == ["い眼科"]


# ---- UI ------------------------------------------------------------------
def _app(tmp_path, monkeypatch, db):
    monkeypatch.setenv("CLINIC_DB_PATH", str(db))
    monkeypatch.setenv("CLINIC_DEMO_DB_PATH", str(tmp_path / "demo.db"))
    at = AppTest.from_file(str(ROOT / "app_v2.py"), default_timeout=30).run()
    at.session_state["navigation"] = "営業対象・出力"
    return at.run()


def test_sales_ui_marketing_fields(tmp_path, monkeypatch, mstore):
    at = _app(tmp_path, monkeypatch, mstore.path)
    assert not at.exception
    labels = [getattr(e, "label", None) for e in at.main if getattr(e, "label", None)]
    assert labels.index("診療科") < labels.index("広告・集客施策") < labels.index("広告・集客施策数") < labels.index("HP制作会社") < labels.index("開業10年以内")
    assert "アツい" not in labels
    # 2026-10-05: 旧「HPランク A/B」チェックボックスを正式Sales Tier分類(SSOT)の
    # セレクトボックスへ差し替えた（同じ位置）。チェックボックスは2つに減る。
    assert [c.label for c in at.checkbox] == ["開業10年以内", "59歳以下 50%以上"]
    ads = next(m for m in at.multiselect if m.label == "広告・集客施策")
    assert ads.options[:4] == ["Doctors File", "Medical DOC", "地域ドクターズ", "Instagram"]
    count = next(s for s in at.selectbox if s.label == "広告・集客施策数")
    assert count.options == ["指定なし", "2施策以上", "3施策以上", "4施策以上"]  # 実データ最大=4
    comp = next(m for m in at.multiselect if m.label == "HP制作会社")
    assert comp.options == list(read_config(ROOT / "config/production_companies.yml"))
    body = " ".join(str(e.value) for e in list(at.markdown) + list(at.caption))
    assert "keyword" not in body and "instagram.com" not in body


def test_sales_ui_filters_combine_with_and(tmp_path, monkeypatch, mstore):
    at = _app(tmp_path, monkeypatch, mstore.path)
    # このtestはscope自体を検証しないため、既定の既存営業リスト（first_seen_atのcutoffで絞られる）を外し、
    # 合成fixtureの全件が対象になる全国Clinic Masterへ切り替える。
    next(s for s in at.selectbox if s.label == "対象データ").set_value(SCOPE_ALL).run()
    # このtestはSales Tier(正式分類SSOT)を検証しないため、既定のA+B+C(合成fixtureは分類対象外で0件になる)
    # を外し、Sales Tier不問にする。
    next(s for s in at.selectbox if s.label == "Sales Tier").set_value("指定なし（Sales Tier不問）").run()
    next(m for m in at.selectbox if m.label == "医科・歯科")
    at.multiselect[0].set_value([]).run()  # 都道府県の既定（東京都）は架空住所と一致するのでそのままでもよい
    next(m for m in at.multiselect if m.label == "診療科").select("眼科").run()
    next(m for m in at.multiselect if m.label == "眼科の治療").select("白内障").run()
    next(m for m in at.multiselect if m.label == "広告・集客施策").select("Instagram公式運用").select("LINE公式運用").run()
    next(s for s in at.selectbox if s.label == "広告・集客施策数").set_value(3).run()
    next(m for m in at.multiselect if m.label == "HP制作会社").select("HERO innovation").select("grits").run()
    assert not at.exception
    assert next(m.value for m in at.metric if m.label == "営業対象") == "1件"


def test_detail_filter_has_no_hot_and_uses_ad_count(tmp_path, monkeypatch, mstore):
    at = _app(tmp_path, monkeypatch, mstore.path)
    at.session_state["navigation"] = "詳細設定"
    at.run()
    sel = next(s for s in at.selectbox if s.options and s.options[0] == "マスター管理")
    sel.select("営業対象フィルター（詳細）").run()
    labels = [getattr(e, "label", None) for e in at.main if getattr(e, "label", None)]
    assert "アツさ" not in labels and "集客投資シグナル数の下限" not in labels
    assert "広告・集客施策数" in labels and "集客投資シグナル（いずれか）" in labels
    assert not at.exception
