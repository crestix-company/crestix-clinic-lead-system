"""HPランク改善Phase2.1: 実HTMLからのD detector候補特徴量抽出のテスト。

ネットワークアクセスはしない。すべて合成HTML文字列だけで検証する。
"""
from src.scoring.hp_rank_html_features import extract_html_features


def html_doc(body, head=""):
    return f"<html><head>{head}</head><body>{body}</body></html>"


def test_counts_images_and_flags_many_photos():
    body = "".join(f'<img src="p{i}.jpg">' for i in range(12))
    f = extract_html_features("https://example.test/", html_doc(body))
    assert f["image_count"] == 12
    assert f["clinic_photo_many"] is True


def test_few_photos_not_flagged():
    body = "".join(f'<img src="p{i}.jpg">' for i in range(3))
    f = extract_html_features("https://example.test/", html_doc(body))
    assert f["image_count"] == 3
    assert f["clinic_photo_many"] is False


def test_doctor_photo_candidate_via_alt_text():
    body = '<img src="a.jpg" alt="院長 山田太郎">'
    f = extract_html_features("https://example.test/", html_doc(body))
    assert f["doctor_photo_candidate"] is True


def test_doctor_photo_candidate_false_without_alt_keyword():
    body = '<img src="a.jpg" alt="待合室の様子">'
    f = extract_html_features("https://example.test/", html_doc(body))
    assert f["doctor_photo_candidate"] is False


def test_viewport_and_responsive_indicator():
    head = '<meta name="viewport" content="width=device-width"><style>@media (max-width:600px){}</style>'
    f = extract_html_features("https://example.test/", html_doc("", head))
    assert f["viewport_meta"] is True
    assert f["responsive_indicator"] is True


def test_no_viewport_no_responsive():
    f = extract_html_features("https://example.test/", html_doc(""))
    assert f["viewport_meta"] is False
    assert f["responsive_indicator"] is False


def test_phone_cta_via_tel_link():
    body = '<a href="tel:0312345678">電話する</a>'
    f = extract_html_features("https://example.test/", html_doc(body))
    assert f["phone_cta"] is True


def test_table_layout_indicator_threshold():
    few = extract_html_features("https://example.test/", html_doc("<table></table><table></table>"))
    many = extract_html_features("https://example.test/", html_doc("<table></table>" * 3))
    assert few["table_layout_indicator"] is False
    assert many["table_layout_indicator"] is True


def test_iframe_and_video_detection():
    body = '<iframe src="https://www.youtube.com/embed/xyz"></iframe>'
    f = extract_html_features("https://example.test/", html_doc(body))
    assert f["iframe_count"] == 1
    assert f["video_present"] is True


def test_copyright_year_recent_and_old():
    recent = extract_html_features("https://example.test/", html_doc("<footer>© 2025 Example Clinic</footer>"))
    old = extract_html_features("https://example.test/", html_doc("<footer>Copyright 2015 Example Clinic</footer>"))
    assert recent["copyright_year"] == 2025
    assert recent["copyright_year_recent"] is True
    assert old["copyright_year"] == 2015
    assert old["copyright_year_old"] is True


def test_deprecated_markup_detected():
    f = extract_html_features("https://example.test/", html_doc("<center><font color='red'>古い</font></center>"))
    assert f["deprecated_markup"] is True
    assert f["mobile_usability_proxy"] is False  # viewportなし かつ 古いタグあり


def test_https_status():
    f_https = extract_html_features("https://example.test/", html_doc(""))
    f_http = extract_html_features("http://example.test/", html_doc(""))
    assert f_https["https_status"] is True
    assert f_http["https_status"] is False
