from pathlib import Path

from src.enrichment.hp_analysis import Page, identity
from src.master.hp_human_review import reanalyze_auto_verified_hp, review_snapshot


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


def test_human_review_ui_has_no_auto_reanalysis_business_category():
    ui = Path("src/hp_human_review_ui.py").read_text(encoding="utf-8")
    repository = Path("src/repository/hp_human_review_repository.py").read_text(encoding="utf-8")
    assert "AUTO_REANALYSIS_PENDING" not in ui
    assert "AUTO_REANALYSIS_RUNNING" not in ui
    assert "自動再解析待ち" not in ui
    assert "auto_reanalysis_pending" not in ui
    assert "auto_reanalysis_pending" not in repository


def test_legacy_candidate_is_not_hidden_from_review_queue_before_backfill(monkeypatch):
    from src.repository.hp_human_review_repository import SupabaseHpHumanReviewRepository

    row = {
        "clinic_id": 123,
        "result_json": {
            "research_status": "REVIEW",
            "hp_status": "REVIEW",
            "hp_checked_at": "2026-10-08T10:00:00+00:00",
            "hp_candidates": [{
                "url": "https://clinic.example/",
                "score": 100,
                "reasons": ["電話番号一致", "住所一致", "院長名一致"],
                "name_match": False,
                "phone_match": True,
                "address_match": True,
            }],
        },
        "human_review_id": None,
    }
    repo = SupabaseHpHumanReviewRepository(object())
    monkeypatch.setattr(repo, "available", lambda: True)
    monkeypatch.setattr(repo, "_current_rows", lambda: [row])

    assert repo.legacy_name_only_mismatch_candidates(limit=None)[0]["clinic_id"] == 123
    assert repo.queue(limit=None)[0]["clinic_id"] == 123


class _Search:
    def search(self, *_args, **_kwargs):
        raise AssertionError("existing official candidate must not search")


class _Fetcher:
    def __init__(self, page):
        self.page = page

    def fetch(self, url, allowed_host=None):
        assert url == self.page.url
        return self.page


def test_new_research_auto_verifies_and_continues_normal_content_analysis(monkeypatch):
    import src.enrichment.researcher as researcher_module

    page = _page()
    monkeypatch.setattr(researcher_module, "crawl", lambda *_args: ([page], []))
    monkeypatch.setattr(
        researcher_module,
        "analyze",
        lambda *_args: {"marketing_signals": [], "treatment_categories": ["矯正歯科"]},
    )
    result, pages = researcher_module.Researcher(_Search(), fetcher=_Fetcher(page)).hp({
        **_record(),
        "hp_candidate_url": page.url,
        "marketing_signals": [],
    })

    assert result["research_status"] == "SUCCESS"
    assert result["hp_verified"] is True
    assert result["hp_identity_source"] == "AUTO_NAME_ONLY_MISMATCH"
    assert result["hp_identity_rule"] == "NAME_ONLY_MISMATCH_AUTO_VERIFY"
    assert result["treatment_categories"] == ["矯正歯科"]
    assert pages[0]["url"] == page.url


def test_new_research_content_failure_keeps_auto_verified_identity(monkeypatch):
    import src.enrichment.researcher as researcher_module
    from src.enrichment.safe_web import WebError

    page = _page()

    def fail_crawl(*_args):
        raise WebError("HTTP 503")

    monkeypatch.setattr(researcher_module, "crawl", fail_crawl)
    result, _pages = researcher_module.Researcher(_Search(), fetcher=_Fetcher(page)).hp({
        **_record(),
        "hp_candidate_url": page.url,
        "marketing_signals": [],
    })

    assert result["hp_status"] == "VERIFIED"
    assert result["hp_verified"] is True
    assert result["hp_identity_source"] == "AUTO_NAME_ONLY_MISMATCH"
    assert result["hp_identity_rule"] == "NAME_ONLY_MISMATCH_AUTO_VERIFY"
    assert result["hp_content_status"] == "FETCH_FAILED"
    assert result["research_status"] == "ERROR"
    assert result["hp_candidates"] == []


class _ResearchRepo:
    def __init__(self):
        self.saved = []

    def get_saved_research(self, _clinic_id):
        return {"marketing_signals": []}

    def save_research(self, clinic_id, result, pages):
        self.saved.append((clinic_id, result, pages))


class _HpRepo:
    def __init__(self):
        self.rows = []

    def upsert_result(self, row):
        self.rows.append(row)


class _Store:
    def get(self, clinic_id):
        return {**_record(), "id": clinic_id, "medical_type": "医科"}


class _DentalStore(_Store):
    def get(self, clinic_id):
        return {**super().get(clinic_id), "medical_type": "歯科", "departments_json": ["歯科"]}


class _DentalRepo:
    def __init__(self):
        self.rows = []

    def available(self):
        return True

    def replace_auto_tags_bulk(self, rows):
        self.rows.extend(rows)


def test_legacy_backfill_success_updates_canonical_result_and_dental_sidecar(monkeypatch):
    import src.enrichment.researcher as researcher_module
    import src.repository.write_backend as backend

    dental_repo = _DentalRepo()
    repos = type("Repos", (), {
        "research": _ResearchRepo(),
        "hp": _HpRepo(),
        "dental_sales_tags": dental_repo,
    })()
    monkeypatch.setattr(backend, "write_repositories_for", lambda _store: repos)
    monkeypatch.setattr(
        researcher_module,
        "research_human_verified_hp",
        lambda _record, url: ({
            "research_status": "SUCCESS",
            "hp_status": "VERIFIED",
            "hp_verified": True,
            "hp_url": url,
            "final_url": url,
            "hp_checked_at": "2026-10-08T11:00:00+00:00",
            "hp_identity_source": "HUMAN_REVIEW",
            "human_verified_source_url": url,
            "treatment_categories": ["矯正歯科"],
        }, [{"url": url, "title": "矯正歯科", "headings": "", "text": ""}]),
    )

    output = reanalyze_auto_verified_hp(
        _DentalStore(), clinic_id=123, selected_url="https://clinic.example/"
    )

    saved = repos.research.saved[0][1]
    assert output["status"] == "DONE"
    assert saved["hp_identity_source"] == "AUTO_NAME_ONLY_MISMATCH"
    assert saved["hp_identity_rule"] == "NAME_ONLY_MISMATCH_AUTO_VERIFY"
    assert saved["auto_verified_source_url"] == "https://clinic.example/"
    assert "human_verified_source_url" not in saved
    assert repos.hp.rows[0]["fetch_status"] == "OK"
    assert dental_repo.rows[0][0] == 123


def test_legacy_backfill_rejects_non_official_candidate_before_writes(monkeypatch):
    import src.repository.write_backend as backend
    import pytest

    repos = type("Repos", (), {
        "research": _ResearchRepo(),
        "hp": _HpRepo(),
        "dental_sales_tags": None,
    })()
    monkeypatch.setattr(backend, "write_repositories_for", lambda _store: repos)

    with pytest.raises(ValueError, match="公式HP候補"):
        reanalyze_auto_verified_hp(
            _Store(), clinic_id=123, selected_url="https://job-medley.com/facility/1234/"
        )
    assert repos.research.saved == []
    assert repos.hp.rows == []


def test_legacy_backfill_content_failure_is_terminal_auto_verified_not_review(monkeypatch):
    import src.enrichment.researcher as researcher_module
    import src.repository.write_backend as backend
    from src.enrichment.safe_web import WebError

    repos = type("Repos", (), {
        "research": _ResearchRepo(),
        "hp": _HpRepo(),
        "dental_sales_tags": None,
    })()
    monkeypatch.setattr(backend, "write_repositories_for", lambda _store: repos)
    monkeypatch.setattr(
        researcher_module,
        "research_human_verified_hp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(WebError("HTTP 503")),
    )

    output = reanalyze_auto_verified_hp(
        _Store(), clinic_id=123, selected_url="https://clinic.example/"
    )

    saved = repos.research.saved[0][1]
    assert output["status"] == "CONTENT_FAILED"
    assert output["fetch_status"] == "ERROR"
    assert saved["hp_status"] == "VERIFIED"
    assert saved["hp_verified"] is True
    assert saved["hp_identity_source"] == "AUTO_NAME_ONLY_MISMATCH"
    assert saved["hp_identity_rule"] == "NAME_ONLY_MISMATCH_AUTO_VERIFY"
    assert saved["hp_content_status"] == "FETCH_FAILED"
    assert saved["research_status"] == "ERROR"
    assert saved["hp_candidates"] == []
    assert repos.hp.rows[0]["fetch_status"] == "ERROR"
