from pathlib import Path

from src.enrichment.hp_analysis import Page, identity
from src.master.hp_human_review import review_snapshot


def _record():
    return {
        "clinic_name": "さくら内科クリニック",
        "phone": "03-1234-5678",
        "address": "東京都千代田区神田1丁目2番3号",
        "manager_name": "山田太郎",
    }


def _page(*, title="法人サイト", phone=True, address=True, manager=True, url="https://clinic.example/"):
    record = _record()
    parts = [
        f"<title>{title}</title>",
        f"<h1>{title}</h1>",
    ]
    if phone:
        parts.append(f"<p>{record['phone']}</p>")
    if address:
        parts.append(f"<p>{record['address']}</p>")
    if manager:
        parts.append(f"<p>院長 {record['manager_name']}</p>")
    return Page(url, "".join(parts))


def test_name_only_mismatch_auto_verifies():
    check = identity(_record(), _page())
    assert check["name_match"] is False
    assert check["phone_match"] is True
    assert check["address_match"] is True
    assert check["manager_match"] is True
    assert check["verified"] is True
    assert check["identity_rule"] == "NAME_ONLY_MISMATCH_AUTO_VERIFY"
    assert "医院名のみ不一致・電話番号/住所/院長名一致で自動本人確認" in check["reasons"]


def test_name_mismatch_without_manager_stays_review():
    check = identity(_record(), _page(manager=False))
    assert check["name_match"] is False
    assert check["phone_match"] is True
    assert check["address_match"] is True
    assert check["manager_match"] is False
    assert check["verified"] is False


def test_name_mismatch_without_address_stays_review():
    check = identity(_record(), _page(address=False))
    assert check["verified"] is False


def test_name_mismatch_without_phone_stays_review():
    check = identity(_record(), _page(phone=False))
    assert check["verified"] is False


def test_standard_name_plus_phone_rule_is_unchanged():
    record = _record()
    check = identity(record, _page(title=record["clinic_name"], address=False, manager=False))
    assert check["verified"] is True
    assert check["identity_rule"] == "STANDARD"


def test_external_listing_is_never_auto_verified():
    check = identity(
        _record(),
        _page(url="https://job-medley.com/facility/1234/"),
    )
    assert check["verified"] is False


def test_legacy_review_snapshot_marks_name_only_mismatch_auto_accept():
    result = {
        "research_status": "REVIEW",
        "hp_status": "REVIEW",
        "hp_checked_at": "2026-10-08T10:00:00+00:00",
        "hp_candidates": [{
            "url": "https://clinic.example/",
            "score": 100,
            "reasons": ["電話番号一致", "住所一致", "院長名一致"],
            "verified": False,
            "name_match": False,
            "phone_match": True,
            "address_match": True,
        }],
    }
    snapshot = review_snapshot(result)
    assert snapshot["manager_match"] is True
    assert snapshot["auto_accept_without_human"] is True
    assert snapshot["auto_accept_reason"] == "医院名のみ不一致・電話番号/住所/院長名一致"


def test_legacy_phone_address_without_manager_stays_human_review():
    result = {
        "research_status": "REVIEW",
        "hp_status": "REVIEW",
        "hp_checked_at": "2026-10-08T10:00:00+00:00",
        "hp_candidates": [{
            "url": "https://clinic.example/",
            "score": 80,
            "reasons": ["電話番号一致", "住所一致"],
            "verified": False,
            "name_match": False,
            "phone_match": True,
            "address_match": True,
        }],
    }
    snapshot = review_snapshot(result)
    assert snapshot["auto_accept_without_human"] is False


def test_legacy_reprocessing_is_not_human_review():
    source = Path("scripts/reprocess_hp_name_only_mismatch.py").read_text(encoding="utf-8")
    assert "reanalyze_auto_verified_hp" in source
    assert "save_review(" not in source


def test_comdesk_export_accepts_auto_reverified_review_history():
    source = Path("src/repository/runtime_store.py").read_text(encoding="utf-8")
    assert "AUTO_NAME_ONLY_MISMATCH" in source
    assert "research.research_results rr" in source
