import html

from src.enrichment.hp_analysis import Page
from src.enrichment.profiles import estimate_profile_age
from src.scoring.research_scoring import hp_signals, treatments


def menu_page(name, label):
    return Page(
        "https://clinic.example/",
        f"<title>{html.escape(name)}</title><h1>{html.escape(name)}</h1>"
        f"<a href='/treatment'>{html.escape(label)}</a>",
    )


def test_v255_non_gi_endoscopy_real_labels_are_excluded():
    cases = [
        ("お茶の水美容形成クリニック", "内視鏡フェイスリフト"),
        ("岩佐耳鼻咽喉科", "声がれ（嗄声） 声のかすれ。内視鏡で声帯を確認します。 くわしく見る →"),
        ("飯田橋駅マール産婦人科クリニック", "医療設備（超音波・内視鏡・手術室）"),
        ("婦人科サンプル", "子宮鏡・腹腔鏡・婦人科内視鏡"),
        ("耳鼻科サンプル", "鼻内視鏡・喉頭内視鏡・電子内視鏡"),
    ]
    for name, label in cases:
        result = treatments([menu_page(name, label)], record={"clinic_name": name})
        assert "内視鏡" not in result["treatment_categories"]


def test_v255_gi_endoscopy_real_labels_are_kept():
    labels = [
        "胃内視鏡検査",
        "大腸内視鏡検査",
        "上部消化管内視鏡検査（胃カメラ）",
        "内視鏡センター（胃・大腸）",
        "日帰りポリープ切除",
        "鎮静剤を使用した内視鏡",
    ]
    for label in labels:
        result = treatments([menu_page("消化器クリニック", label)], record={"clinic_name": "消化器クリニック"})
        assert "内視鏡" in result["treatment_categories"], label


def test_v255_other_treatment_categories_unchanged_on_menu_links():
    cases = [
        ("白内障", "多焦点眼内レンズ"),
        ("緑内障", "SLT"),
        ("ICL", "ICL"),
        ("糖尿病", "糖尿病専門医"),
        ("泌尿器科", "尿路結石"),
        ("循環器内科", "心エコー"),
        ("皮膚科", "ニキビ治療"),
    ]
    for category, label in cases:
        result = treatments([menu_page("テストクリニック", label)], record={"clinic_name": "テストクリニック"})
        assert category in result["treatment_categories"], (category, label)


def test_v255_nagura_history_1919_is_not_director_graduation():
    page = Page(
        "http://www.nagura-cl.jp/greeting.html",
        """
        <h2>院長ごあいさつ・沿革</h2>
        <p>院長 名倉直秀</p>
        <p>プロフィール 聖マリアンナ医科大学、同大学院卒業。医学博士。</p>
        <p>history 沿革 明和7年 業祖 名倉直賢が開業。
        第5代 名倉謙蔵は東大別科を卒業、医師の資格を取得。
        大正8年 (1919年) 第6代 名倉重雄は東大医学部を卒業。</p>
        """,
    )
    result = estimate_profile_age(
        {
            "manager_name": "名倉 直秀",
            "designation_date": "2006-04-01",
            # v25.4で保存された誤った自動結果がrecordへ混ざるケースも再現。
            "graduation_year": 1919,
            "graduation_evidence": [{"year": 1919, "evidence": "沿革の別人物"}],
        },
        [page],
        current_year=2026,
    )
    assert result["graduation_year"] is None
    assert result["age_probability_under_59"] is None
    assert result["age_estimation_confidence"] in {"UNKNOWN", "REVIEW"}


def test_v255_pre1960_weak_year_falls_to_review():
    page = Page(
        "https://clinic.example/director",
        """
        <h2>院長 山田太郎</h2>
        <p>当院の歴史・沿革についてご紹介します。</p>
        <p>1955年 東京大学医学部卒業。その後、医院の礎を築きました。</p>
        """,
    )
    result = estimate_profile_age({"manager_name": "山田 太郎"}, [page], current_year=2026)
    assert result["graduation_year"] is None
    assert result["age_probability_under_59"] is None
    assert result["age_estimation_confidence"] == "REVIEW"


def test_v255_hanzomon_1982_is_kept_and_advisor_year_not_mixed():
    page = Page(
        "https://hanzomon-icho-clinic.com/doctor.html",
        """
        <h3>院長 掛谷和俊</h3>
        <p>1982年宮崎大学医学部卒業。消化器癌の研究で博士号を取得。</p>
        <h3>特別顧問 新谷弘実</h3>
        <p>1960年順天堂大学医学部卒業。1963年に渡米。</p>
        """,
    )
    result = estimate_profile_age(
        {"manager_name": "掛谷 和俊", "designation_date": "2004-10-01"},
        [page],
        current_year=2026,
    )
    assert result["graduation_year"] == 1982
    assert result["age_estimation_confidence"] == "MODEL_ESTIMATE"


def test_v255_online_terminated_is_not_counted():
    page = Page(
        "https://www.iidabashi-eye.com/online/index.html",
        """
        <title>オンライン診療終了のお知らせ | 飯田橋眼科クリニック</title>
        <h1>オンライン診療終了のお知らせ</h1>
        <p>飯田橋眼科クリニックではオンライン診療を終了いたしました。
        現在はオンライン診療の受付を行っておりません。</p>
        <a href='/online/'>オンライン診療</a>
        """,
    )
    signals = hp_signals(
        {"clinic_name": "飯田橋眼科クリニック", "phone": "03-5276-2722", "address": "東京都千代田区"},
        [page],
    )
    assert "オンライン診療" not in [s["name"] for s in signals]
