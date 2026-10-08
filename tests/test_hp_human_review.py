from pathlib import Path

import pytest

from src.master.hp_human_review import (
    POSITIVE_DECISIONS,
    reanalyze_human_verified_hp,
    review_snapshot,
)
from src.repository.hp_human_review_repository import SupabaseHpHumanReviewRepository


def _result(*, checked_at="2026-10-08T06:00:00+00:00", score=80,
            name=False, phone=True, address=True, manager=False,
            content_status=""):
    reasons = []
    if phone:
        reasons.append("電話番号一致")
    if name:
        reasons.append("医院名がページ見出しに一致")
    if address:
        reasons.append("住所一致")
    if manager:
        reasons.append("院長名一致")
    return {
        "research_status": "REVIEW",
        "hp_status": "REVIEW" if content_status != "ACCESS_RESTRICTED" else "VERIFIED",
        "hp_checked_at": checked_at,
        "hp_content_status": content_status,
        "hp_url": "https://clinic.example/" if content_status == "ACCESS_RESTRICTED" else "",
        "hp_candidates": [] if content_status == "ACCESS_RESTRICTED" else [{
            "url": "https://clinic.example/",
            "score": score,
            "reasons": reasons,
            "verified": False,
            "name_match": name,
            "phone_match": phone,
            "address_match": address,
        }],
        "hp_match_reason": ["候補ページの本人確認・取得を完了できませんでした。"],
        "crawl_errors": [],
    }


def test_snapshot_high_priority_phone_address_without_name():
    snapshot = review_snapshot(_result(score=80, name=False, phone=True, address=True))
    assert snapshot["priority"] == "HIGH"
    assert snapshot["bucket"] == "PHONE_ADDRESS_NO_NAME"
    assert snapshot["best_candidate_url"] == "https://clinic.example/"
    assert snapshot["manager_match"] is False


def test_snapshot_manager_match_is_preserved_from_reason():
    snapshot = review_snapshot(_result(score=70, name=False, phone=True, address=False, manager=True))
    assert snapshot["manager_match"] is True
    assert snapshot["bucket"] == "PHONE_MANAGER_NO_NAME"
    assert snapshot["priority"] == "MEDIUM"


def test_access_restricted_uses_verified_hp_url_as_review_candidate():
    snapshot = review_snapshot(_result(content_status="ACCESS_RESTRICTED"))
    assert snapshot["priority"] == "HIGH"
    assert snapshot["bucket"] == "ACCESS_RESTRICTED"
    assert snapshot["candidate_urls"] == ["https://clinic.example/"]


def test_only_positive_decisions_apply_verified_url():
    assert POSITIVE_DECISIONS == frozenset({"OFFICIAL", "ORGANIZATION_PAGE", "ACCESS_RESTRICTED"})
    assert "NOT_OFFICIAL" not in POSITIVE_DECISIONS
    assert "UNCERTAIN" not in POSITIVE_DECISIONS


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.current = None
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        sql = str(query)
        self.conn.executed.append((sql, params))
        if "SELECT result_json FROM research.research_results" in sql:
            self.current = (self.conn.result_json,)
        elif "SELECT value_json FROM provenance.manual_overrides" in sql:
            self.current = None
        else:
            self.current = None

    def fetchone(self):
        return self.current


class _Connection:
    def __init__(self, result_json):
        self.result_json = result_json
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_negative_human_review_is_training_label_only():
    conn = _Connection(_result())
    repo = SupabaseHpHumanReviewRepository(conn)
    repo.save_review(
        clinic_id=123,
        hp_checked_at="2026-10-08T06:00:00+00:00",
        human_decision="NOT_OFFICIAL",
        selected_url="https://clinic.example/",
        reviewer="reviewer",
        review_note="別医院",
        research_job_id="job-1",
        auto_run_id="run-1",
    )
    sql = "\n".join(item[0] for item in conn.executed)
    assert "INSERT INTO provenance.hp_human_reviews" in sql
    assert "INSERT INTO provenance.manual_overrides" not in sql
    assert conn.commits == 1 and conn.rollbacks == 0


def test_positive_human_review_sets_url_and_verified(monkeypatch):
    from src.repository.supabase_write_adapter import SupabaseClinicWriteRepository

    monkeypatch.setattr(
        SupabaseClinicWriteRepository,
        "_refresh_projection_tx",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    conn = _Connection(_result())
    repo = SupabaseHpHumanReviewRepository(conn)
    repo.save_review(
        clinic_id=123,
        hp_checked_at="2026-10-08T06:00:00+00:00",
        human_decision="OFFICIAL",
        selected_url="https://clinic.example/",
        reviewer="reviewer",
    )
    override_calls = [
        params for sql, params in conn.executed
        if "INSERT INTO provenance.manual_overrides" in sql
    ]
    assert len(override_calls) == 2
    fields = {params[1] for params in override_calls}
    assert fields == {"hp_url", "hp_status"}
    assert conn.commits == 1 and conn.rollbacks == 0


def test_stale_review_attempt_is_rejected():
    conn = _Connection(_result(checked_at="new-attempt"))
    repo = SupabaseHpHumanReviewRepository(conn)
    with pytest.raises(ValueError, match="更新されています"):
        repo.save_review(
            clinic_id=123,
            hp_checked_at="old-attempt",
            human_decision="NOT_OFFICIAL",
            reviewer="reviewer",
        )
    assert conn.commits == 0 and conn.rollbacks == 1


def test_schema_migration_is_private_and_rls_guarded():
    sql = Path("scripts/supabase_migration/hp_human_review_schema.sql").read_text(encoding="utf-8").lower()
    assert "alter table provenance.hp_human_reviews enable row level security" in sql
    assert "alter table provenance.hp_human_review_research_runs enable row level security" in sql
    assert "grant select, insert on provenance.hp_human_reviews to clinic_runtime" in sql
    assert "grant select, insert on provenance.hp_human_review_research_runs to clinic_runtime" in sql
    assert "revoke all on provenance.hp_human_reviews from anon, authenticated, public" in sql
    assert "revoke all on provenance.hp_human_review_research_runs from anon, authenticated, public" in sql
    assert "grant select" not in sql.split("to clinic_runtime")[0].split("create table")[0]


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


class _HumanRepo:
    def __init__(self):
        self.runs = []

    def record_reanalysis(self, **kwargs):
        self.runs.append(kwargs)
        return "run-1"


class _Store:
    def get(self, clinic_id):
        return {
            "id": clinic_id,
            "clinic_name": "テスト医院",
            "phone": "03-1234-5678",
            "address": "東京都千代田区1-1-1",
            "marketing_signals": [],
        }


def _repos():
    return type(
        "Repos",
        (),
        {
            "research": _ResearchRepo(),
            "hp": _HpRepo(),
            "hp_human_review": _HumanRepo(),
        },
    )()


def test_human_verified_success_reanalyzes_and_updates_canonical_ledger(monkeypatch):
    import src.enrichment.researcher as researcher_module
    import src.repository.write_backend as backend

    repos = _repos()
    monkeypatch.setattr(backend, "write_repositories_for", lambda _store: repos)
    monkeypatch.setattr(
        researcher_module,
        "research_human_verified_hp",
        lambda record, url: (
            {
                "research_status": "SUCCESS",
                "hp_status": "VERIFIED",
                "hp_verified": True,
                "hp_url": url,
                "final_url": url,
                "hp_checked_at": "2026-10-08T07:00:00+00:00",
                "treatment_categories": ["白内障", "緑内障"],
            },
            [{"url": url}],
        ),
    )

    result = reanalyze_human_verified_hp(
        _Store(),
        human_review_id="review-1",
        clinic_id=123,
        selected_url="https://clinic.example/",
    )

    assert result["status"] == "DONE"
    assert result["treatment_count"] == 2
    assert repos.research.saved[0][0] == 123
    assert repos.hp.rows[0]["fetch_status"] == "OK"
    assert repos.hp.rows[0]["treatment_status"] == "DONE"
    assert '"白内障"' in repos.hp.rows[0]["treatment_categories"]
    assert repos.hp_human_review.runs[0]["status"] == "DONE"
    assert repos.hp_human_review.runs[0]["treatment_categories"] == ["白内障", "緑内障"]


def test_human_verified_fetch_failure_marks_batch_failed_but_preserves_review_evidence(monkeypatch):
    import src.enrichment.researcher as researcher_module
    import src.repository.write_backend as backend
    from src.enrichment.safe_web import WebError

    repos = _repos()
    monkeypatch.setattr(backend, "write_repositories_for", lambda _store: repos)

    def fail(_record, _url):
        raise WebError("HTTP 403")

    monkeypatch.setattr(researcher_module, "research_human_verified_hp", fail)

    result = reanalyze_human_verified_hp(
        _Store(),
        human_review_id="review-1",
        clinic_id=123,
        selected_url="https://clinic.example/",
    )

    assert result["status"] == "FAILED"
    assert repos.research.saved == []  # original REVIEW evidence remains available for retry/audit
    assert repos.hp.rows[0]["fetch_status"] == "ERROR"
    assert repos.hp.rows[0]["treatment_status"] == "FETCH_FAILED"
    assert repos.hp_human_review.runs[0]["status"] == "FAILED"
    assert "HTTP 403" in repos.hp_human_review.runs[0]["error_detail"]


def test_human_verified_analyzer_bypasses_identity_but_keeps_normal_content_analysis(monkeypatch):
    import src.enrichment.researcher as researcher_module

    class FakePage:
        url = "https://clinic.example/"

        def evidence(self):
            return {"url": self.url, "title": "テスト医院"}

    class FakeFetcher:
        def fetch(self, url):
            assert url == "https://clinic.example/"
            return FakePage()

    monkeypatch.setattr(
        researcher_module,
        "crawl",
        lambda page, fetcher, max_pages, should_stop: ([page], []),
    )
    monkeypatch.setattr(
        researcher_module,
        "analyze",
        lambda record, pages, results: {
            "marketing_signals": [],
            "treatment_categories": ["白内障"],
        },
    )
    monkeypatch.setattr(
        researcher_module,
        "identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("identity must be bypassed")),
    )

    result, pages = researcher_module.research_human_verified_hp(
        {"clinic_name": "テスト医院", "marketing_signals": []},
        "https://clinic.example/",
        fetcher=FakeFetcher(),
    )

    assert result["research_status"] == "SUCCESS"
    assert result["hp_verified"] is True
    assert result["hp_identity_source"] == "HUMAN_REVIEW"
    assert result["treatment_categories"] == ["白内障"]
    assert pages[0]["url"] == "https://clinic.example/"
