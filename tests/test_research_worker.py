"""Tests for the standalone Research Worker execution infrastructure (Step 5).

Covers checkpoint/resume, failure isolation, the shared final-results DB writer,
and bounded same-domain concurrency -- all without real HTTP (that's exercised by
the Dry Run, Step 8). evaluate_treatment_evidence/candidate_union themselves are
covered by tests/test_treatment_taxonomy_*.py and tests/test_candidate_union.py.
"""
import csv
import concurrent.futures
import sqlite3
import threading
import time

import pytest

from scripts import research_worker as worker


def _write_manifest(tmp_path, rows, manifest_id="phase7-fullrun-test0000"):
    path = tmp_path / "manifest.csv"
    fields = ["manifest_id", "clinic_id", "effective_official_hp_url", "url_source",
              "crestix_sales_category_hint", "git_commit_sha", "taxonomy_version",
              "evidence_engine_version", "generated_at"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            base = {"manifest_id": manifest_id, "url_source": "SAVED_HP_URL",
                    "crestix_sales_category_hint": "", "git_commit_sha": "deadbeef",
                    "taxonomy_version": "7A-v2", "evidence_engine_version": "phase7b-context-v3",
                    "generated_at": "2026-09-30T00:00:00+00:00"}
            base.update(row)
            writer.writerow(base)
    return path


class TestManifestLoading:
    def test_loads_valid_manifest(self, tmp_path):
        path = _write_manifest(tmp_path, [
            {"clinic_id": 1, "effective_official_hp_url": "https://a.example/"},
            {"clinic_id": 2, "effective_official_hp_url": "https://b.example/"},
        ])
        rows = worker.load_manifest(path)
        assert len(rows) == 2

    def test_rejects_duplicate_clinic_id(self, tmp_path):
        path = _write_manifest(tmp_path, [
            {"clinic_id": 1, "effective_official_hp_url": "https://a.example/"},
            {"clinic_id": 1, "effective_official_hp_url": "https://a.example/"},
        ])
        with pytest.raises(ValueError, match="duplicate clinic_id"):
            worker.load_manifest(path)

    def test_rejects_mixed_manifest_ids(self, tmp_path):
        path = tmp_path / "bad.csv"
        fields = ["manifest_id", "clinic_id", "effective_official_hp_url", "url_source",
                  "crestix_sales_category_hint", "git_commit_sha", "taxonomy_version",
                  "evidence_engine_version", "generated_at"]
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerow({"manifest_id": "a", "clinic_id": 1, "effective_official_hp_url": "https://a.example/",
                        "url_source": "", "crestix_sales_category_hint": "", "git_commit_sha": "x",
                        "taxonomy_version": "7A-v2", "evidence_engine_version": "v", "generated_at": ""})
            w.writerow({"manifest_id": "b", "clinic_id": 2, "effective_official_hp_url": "https://b.example/",
                        "url_source": "", "crestix_sales_category_hint": "", "git_commit_sha": "x",
                        "taxonomy_version": "7A-v2", "evidence_engine_version": "v", "generated_at": ""})
        with pytest.raises(ValueError, match="exactly one manifest_id"):
            worker.load_manifest(path)

    def test_rejects_empty_manifest(self, tmp_path):
        path = _write_manifest(tmp_path, [])
        with pytest.raises(ValueError, match="empty"):
            worker.load_manifest(path)


class TestProgressCheckpointResume:
    def test_seed_then_claim_marks_in_progress(self, tmp_path):
        db = worker.open_progress_db(tmp_path / "progress.sqlite3")
        worker.seed_progress(db, [1, 2, 3])
        claimed = worker.claim_next_batch(db, retry_failed=False, limit=10)
        assert sorted(claimed) == [1, 2, 3]
        statuses = dict(db.execute("SELECT clinic_id, status FROM clinic_progress"))
        assert all(s == "IN_PROGRESS" for s in statuses.values())

    def test_done_clinics_are_not_reclaimed_on_resume(self, tmp_path):
        db = worker.open_progress_db(tmp_path / "progress.sqlite3")
        worker.seed_progress(db, [1, 2])
        worker.claim_next_batch(db, retry_failed=False, limit=10)
        worker.mark_progress(db, 1, "DONE")
        worker.mark_progress(db, 2, "FETCH_FAILED", error="FETCH_ERROR")

        # simulate a fresh resume: nothing left to claim by default
        claimed = worker.claim_next_batch(db, retry_failed=False, limit=10)
        assert claimed == []

    def test_retry_failed_flag_reclaims_only_failed_not_done(self, tmp_path):
        db = worker.open_progress_db(tmp_path / "progress.sqlite3")
        worker.seed_progress(db, [1, 2])
        worker.claim_next_batch(db, retry_failed=False, limit=10)
        worker.mark_progress(db, 1, "DONE")
        worker.mark_progress(db, 2, "FETCH_FAILED", error="FETCH_ERROR")

        claimed = worker.claim_next_batch(db, retry_failed=True, limit=10)
        assert claimed == [2]

    def test_progress_summary_counts_and_pct(self, tmp_path):
        db = worker.open_progress_db(tmp_path / "progress.sqlite3")
        worker.seed_progress(db, [1, 2, 3, 4])
        worker.claim_next_batch(db, retry_failed=False, limit=10)
        worker.mark_progress(db, 1, "DONE")
        worker.mark_progress(db, 2, "DONE")
        summary = worker.progress_summary(db)
        assert summary["total"] == 4
        assert summary["counts"]["DONE"] == 2
        assert summary["progress_pct"] == 50.0

    def test_retry_failed_does_not_reclaim_forever_past_attempt_cap(self, tmp_path):
        # Regression: `resume --retry-failed` against a permanently-broken clinic
        # (e.g. access-restricted -> always FETCH_FAILED) must not reclaim it forever
        # within a single run -- that hung the worker indefinitely before this fix.
        db = worker.open_progress_db(tmp_path / "progress.sqlite3")
        worker.seed_progress(db, [1])
        for _ in range(worker.MAX_CLINIC_ATTEMPTS + 2):
            claimed = worker.claim_next_batch(db, retry_failed=True, limit=10)
            if not claimed:
                break
            worker.mark_progress(db, 1, "FETCH_FAILED", error="FETCH_ERROR")
        attempts = db.execute("SELECT attempts FROM clinic_progress WHERE clinic_id=1").fetchone()[0]
        assert attempts <= worker.MAX_CLINIC_ATTEMPTS
        assert worker.claim_next_batch(db, retry_failed=True, limit=10) == []

    def test_in_progress_left_by_a_crash_is_reclaimed_as_pending(self, tmp_path):
        # Simulates a process killed mid-batch: clinic_id 1 is stuck IN_PROGRESS.
        # run_worker's startup must reset it to PENDING so it is not lost forever.
        db = worker.open_progress_db(tmp_path / "progress.sqlite3")
        worker.seed_progress(db, [1, 2])
        worker.claim_next_batch(db, retry_failed=False, limit=10)  # both -> IN_PROGRESS
        worker.mark_progress(db, 2, "DONE")
        # clinic 1 stays IN_PROGRESS, simulating a crash before it finished

        reclaimed = db.execute("UPDATE clinic_progress SET status='PENDING' WHERE status='IN_PROGRESS'").rowcount
        assert reclaimed == 1
        claimed = worker.claim_next_batch(db, retry_failed=False, limit=10)
        assert claimed == [1]

    def test_seed_is_idempotent_across_restarts(self, tmp_path):
        path = tmp_path / "progress.sqlite3"
        db = worker.open_progress_db(path)
        worker.seed_progress(db, [1, 2])
        worker.claim_next_batch(db, retry_failed=False, limit=10)
        worker.mark_progress(db, 1, "DONE")
        db.close()

        db2 = worker.open_progress_db(path)
        worker.seed_progress(db2, [1, 2, 3])  # re-seeding must not reset clinic 1
        statuses = dict(db2.execute("SELECT clinic_id, status FROM clinic_progress"))
        assert statuses[1] == "DONE"
        assert statuses[3] == "PENDING"


def _status_row(research_status="DONE", candidate_count=1, **overrides):
    row = {
        "research_status": research_status, "candidate_count": candidate_count,
        "source_url": "https://a.example/", "final_url": "https://a.example/",
        "identity_verified": research_status == "DONE", "attempts": 1, "last_error": "",
        "taxonomy_version": "7A-v2", "evidence_engine_version": "phase7b-context-v3",
        "manifest_id": "phase7-fullrun-test0000", "git_commit_sha": "deadbeef",
        "researched_at": "2026-09-30T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class TestFinalResultsDbWriter:
    def test_schema_has_required_contract_fields(self, tmp_path):
        db = worker.open_final_db(tmp_path / "final.sqlite3")
        cols = {row[1] for row in db.execute("PRAGMA table_info(clinic_treatment_research_final)")}
        required = {
            "clinic_id", "treatment_category_id", "treatment_category_name", "research_status",
            "matched_alias", "source_url", "page_title", "provider_context", "exclusion_context",
            "evidence_engine_version", "taxonomy_version", "researched_at", "git_commit_sha", "manifest_id",
        }
        assert required <= cols

    def test_treatment_table_no_longer_allows_fetch_failed(self, tmp_path):
        # FETCH_FAILED moved entirely to clinic_research_status (clinic-level);
        # it is no longer a legal Treatment-level status.
        db = worker.open_final_db(tmp_path / "final.sqlite3")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO clinic_treatment_research_final "
                "(clinic_id, treatment_category_id, treatment_category_name, research_status, "
                " evidence_engine_version, taxonomy_version, researched_at, git_commit_sha, manifest_id) "
                "VALUES (1,'x','x','FETCH_FAILED','v','t','2026-01-01','sha','m')"
            )

    def test_clinic_research_status_schema_has_required_fields(self, tmp_path):
        db = worker.open_final_db(tmp_path / "final.sqlite3")
        cols = {row[1] for row in db.execute("PRAGMA table_info(clinic_research_status)")}
        required = {
            "clinic_id", "research_status", "candidate_count", "source_url", "final_url",
            "identity_verified", "attempts", "last_error", "taxonomy_version",
            "evidence_engine_version", "manifest_id", "git_commit_sha", "researched_at",
        }
        assert required <= cols

    def test_wal_mode_enabled(self, tmp_path):
        db = worker.open_final_db(tmp_path / "final.sqlite3")
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_atomic_write_is_readable_immediately_by_a_second_connection(self, tmp_path):
        path = tmp_path / "final.sqlite3"
        writer_db = worker.open_final_db(path)
        rows = [{
            "treatment_category_name": "胃カメラ検査", "research_status": "CONFIRMED",
            "matched_alias": "胃カメラ", "source_url": "https://a.example/",
            "page_title": "診療案内", "provider_context": "SELF_OFFER", "exclusion_context": "NONE",
            "evidence_engine_version": "phase7b-context-v3", "taxonomy_version": "7A-v2",
            "researched_at": "2026-09-30T00:00:00+00:00", "git_commit_sha": "deadbeef",
            "manifest_id": "phase7-fullrun-test0000",
        }]
        worker.write_clinic_atomic(writer_db, 42, rows, _status_row())

        reader_db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        got = reader_db.execute(
            "SELECT research_status FROM clinic_treatment_research_final WHERE clinic_id=?", (42,)
        ).fetchone()
        assert got == ("CONFIRMED",)
        status = reader_db.execute(
            "SELECT research_status FROM clinic_research_status WHERE clinic_id=?", (42,)
        ).fetchone()
        assert status == ("DONE",)

    def test_second_done_write_fully_replaces_prior_treatment_rows(self, tmp_path):
        # DELETE-then-INSERT, never an upsert/merge: a category present in the
        # OLD candidate_union but absent from the NEW one must not survive.
        db = worker.open_final_db(tmp_path / "final.sqlite3")
        old_rows = [{
            "treatment_category_name": "胃カメラ検査", "research_status": "REVIEW",
            "evidence_engine_version": "phase7b-context-v3", "taxonomy_version": "7A-v2",
            "researched_at": "2026-09-30T00:00:00+00:00", "git_commit_sha": "deadbeef", "manifest_id": "m",
        }, {
            "treatment_category_name": "白内障手術", "research_status": "NOT_CONFIRMED",
            "evidence_engine_version": "phase7b-context-v3", "taxonomy_version": "7A-v2",
            "researched_at": "2026-09-30T00:00:00+00:00", "git_commit_sha": "deadbeef", "manifest_id": "m",
        }]
        worker.write_clinic_atomic(db, 1, old_rows, _status_row(candidate_count=2))
        new_rows = [{
            "treatment_category_name": "胃カメラ検査", "research_status": "CONFIRMED",
            "evidence_engine_version": "phase7b-context-v3", "taxonomy_version": "7A-v2",
            "researched_at": "2026-09-30T01:00:00+00:00", "git_commit_sha": "deadbeef", "manifest_id": "m",
        }]
        worker.write_clinic_atomic(db, 1, new_rows, _status_row(candidate_count=1))
        rows = db.execute(
            "SELECT treatment_category_name, research_status FROM clinic_treatment_research_final WHERE clinic_id=1"
        ).fetchall()
        assert rows == [("胃カメラ検査", "CONFIRMED")]  # 白内障手術's stale row is gone


class TestLoadRecordsCarriesIdentityFields:
    def test_clinic_name_phone_address_reach_the_record(self, tmp_path):
        # Regression: identity() in src/enrichment/hp_analysis.py requires
        # clinic_name (always) and phone-or-address to ever verify a fetched page.
        # A manifest row missing these silently forces every clinic to
        # IDENTITY_NOT_VERIFIED -- caught by the Step 8 Dry Run before it could
        # turn the real Full Run into 9,399 clinics of pure REVIEW.
        rows = [{
            "manifest_id": "m", "clinic_id": "1", "clinic_name": "テストクリニック",
            "phone": "03-1234-5678", "address": "東京都千代田区1-1-1",
            "effective_official_hp_url": "https://a.example/", "crestix_sales_category_hint": "眼科",
            "git_commit_sha": "x",
        }]
        records = worker._load_records_for_manifest(rows)
        record = records[1]
        assert record["clinic_name"] == "テストクリニック"
        assert record["phone"] == "03-1234-5678"
        assert record["address"] == "東京都千代田区1-1-1"


class TestFetchFailedIsClinicLevelOnly:
    """FETCH_FAILED is a clinic_research_status concept now, never a
    clinic_treatment_research_final row -- see the sidecar consistency fix."""

    def test_identity_not_verified_rows_use_review_status_not_fetch_failed(self):
        # The HTTP fetch succeeded (we have content) -- only identity confirmation
        # failed, so this must match the original frozen engine's REVIEW convention,
        # not be conflated with a true fetch failure.
        rows = worker._non_ok_rows(
            {"effective_official_hp_url": "https://a.example/", "git_commit_sha": "x", "manifest_id": "m"},
            {"胃カメラ検査"}, "2026-09-30T00:00:00+00:00", "REVIEW", "IDENTITY_NOT_VERIFIED",
        )
        assert rows[0]["research_status"] == "REVIEW"


class TestRetryableErrorClassification:
    @pytest.mark.parametrize("message", [
        "ページ取得が時間上限を超えました。",
        "接続・SSL・タイムアウトのエラーです。",
        "HTTP 500。手動で確認してください。",
        "HTTP 502。手動で確認してください。",
        "HTTP 503。手動で確認してください。",
        "HTTP 504。手動で確認してください。",
    ])
    def test_transient_errors_are_retryable(self, message):
        assert worker.RETRYABLE_ERROR.search(message)

    @pytest.mark.parametrize("message", [
        "ローカルネットワークへのアクセスはできません。",
        "アクセス制限があるため、このサイトの取得を停止しました。",
        "HTML以外のページです。",
        "HTTP 403。手動で確認してください。",
        "HTTP 404。手動で確認してください。",
        "robots.txtで取得が許可されていません。",
        # 429: safe_web.py's SafeFetcher._get() already spends the one allowed
        # Retry-After-respecting retry at the fetch layer -- retrying again here
        # would double the retry budget, so this layer must not match it.
        "HTTP 429。手動で確認してください。",
        # SSL certificate failures are permanent for this host, not transient.
        "SSL証明書の検証に失敗しました。",
        # Non-standard/other 5xx the policy deliberately excludes (more likely a
        # permanent server misconfiguration than a transient blip).
        "HTTP 501。手動で確認してください。",
        "HTTP 505。手動で確認してください。",
    ])
    def test_permanent_errors_are_not_retryable(self, message):
        assert not worker.RETRYABLE_ERROR.search(message)


class _StubFetcher:
    """Stand-in for SafeFetcher: .fetch(url) raises/returns according to a
    pre-programmed sequence, one entry consumed per call."""
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    def fetch(self, url, allowed_host=None):
        self.calls += 1
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestMeteredFetcherTransientRetry:
    """Step 4 regression tests: transient-fetch-retry policy only -- no change
    to URL sanitization, the evidence engine, taxonomy, or CONFIRMED thresholds."""

    def _no_sleep(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(worker.time, "sleep", lambda s: sleeps.append(s))
        return sleeps

    def test_503_then_200_retries_to_success(self, monkeypatch):
        sleeps = self._no_sleep(monkeypatch)
        from src.enrichment.safe_web import WebError
        stub = _StubFetcher([WebError("HTTP 503。手動で確認してください。"), "PAGE_OK"])
        fetcher = worker._MeteredFetcher(stub)
        result = fetcher.fetch("https://clinic.example/")
        assert result == "PAGE_OK"
        assert stub.calls == 2
        assert fetcher.attempts == 2
        assert fetcher.successes == 1
        assert sleeps == [1.0]  # 1s backoff before the single retry

    def test_502_exhausts_retries_then_raises_fetch_failed(self, monkeypatch):
        self._no_sleep(monkeypatch)
        from src.enrichment.safe_web import WebError
        err = WebError("HTTP 502。手動で確認してください。")
        stub = _StubFetcher([err, err, err])
        fetcher = worker._MeteredFetcher(stub)
        with pytest.raises(WebError, match="502"):
            fetcher.fetch("https://clinic.example/")
        assert stub.calls == worker.MAX_RETRY_ATTEMPTS
        assert fetcher.successes == 0

    def test_timeout_then_success_retries(self, monkeypatch):
        sleeps = self._no_sleep(monkeypatch)
        from src.enrichment.safe_web import WebError
        stub = _StubFetcher([WebError("接続・SSL・タイムアウトのエラーです。"), "PAGE_OK"])
        fetcher = worker._MeteredFetcher(stub)
        assert fetcher.fetch("https://clinic.example/") == "PAGE_OK"
        assert sleeps == [1.0]

    def test_403_is_never_retried(self, monkeypatch):
        self._no_sleep(monkeypatch)
        from src.enrichment.safe_web import WebError
        stub = _StubFetcher([WebError("HTTP 403。手動で確認してください。"), "PAGE_OK"])
        fetcher = worker._MeteredFetcher(stub)
        with pytest.raises(WebError, match="403"):
            fetcher.fetch("https://clinic.example/")
        assert stub.calls == 1  # never reached the "PAGE_OK" second item

    def test_404_is_never_retried(self, monkeypatch):
        self._no_sleep(monkeypatch)
        from src.enrichment.safe_web import WebError
        stub = _StubFetcher([WebError("HTTP 404。手動で確認してください。"), "PAGE_OK"])
        fetcher = worker._MeteredFetcher(stub)
        with pytest.raises(WebError, match="404"):
            fetcher.fetch("https://clinic.example/")
        assert stub.calls == 1

    def test_robots_rejection_is_never_retried(self, monkeypatch):
        self._no_sleep(monkeypatch)
        from src.enrichment.safe_web import WebError
        stub = _StubFetcher([WebError("robots.txtで取得が許可されていません。"), "PAGE_OK"])
        fetcher = worker._MeteredFetcher(stub)
        with pytest.raises(WebError, match="robots"):
            fetcher.fetch("https://clinic.example/")
        assert stub.calls == 1

    def test_ssl_certificate_error_is_never_retried(self, monkeypatch):
        self._no_sleep(monkeypatch)
        from src.enrichment.safe_web import WebError
        stub = _StubFetcher([WebError("SSL証明書の検証に失敗しました。"), "PAGE_OK"])
        fetcher = worker._MeteredFetcher(stub)
        with pytest.raises(WebError, match="SSL証明書"):
            fetcher.fetch("https://clinic.example/")
        assert stub.calls == 1  # a permanent cert failure must not be retried, ever


class TestSidecarConsistencyInvariants:
    """The 9 scenarios from the sidecar consistency fix: clinic-level attempt
    status (clinic_research_status) and Treatment-level results
    (clinic_treatment_research_final) must never desync, regardless of retry
    history. Real production cases this reproduces exactly: clinic_id 1859/
    7166/7747/9048/9121 (candidate_count=0), 7639 (candidate_count=7), 11916
    (candidate_count=1) -- all previously had 28-43 stale FETCH_FAILED
    Treatment rows survive a later successful DONE."""

    def _db(self, tmp_path):
        return worker.open_final_db(tmp_path / "final.sqlite3")

    def _old_fetch_failed_rows(self, db, clinic_id, categories):
        # Simulates the OLD (pre-fix) behavior that produced the bug: 43
        # Treatment-level FETCH_FAILED rows from a failed attempt. The new
        # schema's CHECK constraint forbids this going forward (see
        # test_treatment_table_no_longer_allows_fetch_failed) -- this helper
        # writes directly, bypassing write_clinic_atomic, purely to recreate
        # the pre-existing-bad-state starting point for migration-style tests.
        db.execute("BEGIN IMMEDIATE")
        for cat in categories:
            db.execute(
                "INSERT INTO clinic_treatment_research_final "
                "(clinic_id, treatment_category_id, treatment_category_name, research_status, "
                " evidence_engine_version, taxonomy_version, researched_at, git_commit_sha, manifest_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (clinic_id, cat, cat, "NOT_CONFIRMED", "v", "t", "2026-01-01", "sha", "m"),
            )
        db.execute("COMMIT")

    def test_1_fetch_failed_then_done_candidate_zero_no_stale_rows(self, tmp_path):
        db = self._db(tmp_path)
        # Can't literally pre-seed FETCH_FAILED Treatment rows anymore (CHECK
        # constraint forbids it) -- the realistic equivalent is: clinic
        # previously had SOME Treatment rows from an earlier different
        # candidate_union, then a later attempt finds zero candidates.
        self._old_fetch_failed_rows(db, 1859, ["ED治療"])
        worker.write_clinic_atomic(db, 1859, [], _status_row(candidate_count=0))
        treatment_rows = db.execute(
            "SELECT * FROM clinic_treatment_research_final WHERE clinic_id=1859"
        ).fetchall()
        assert treatment_rows == []
        status = db.execute(
            "SELECT research_status FROM clinic_research_status WHERE clinic_id=1859"
        ).fetchone()
        assert status == ("DONE",)

    def test_2_fetch_failed_then_done_candidate_seven_only_current_rows_survive(self, tmp_path):
        db = self._db(tmp_path)
        self._old_fetch_failed_rows(db, 7639, [f"cat{i}" for i in range(43)])
        new_rows = [{
            "treatment_category_name": f"cat{i}", "research_status": "NOT_CONFIRMED",
            "evidence_engine_version": "v", "taxonomy_version": "t",
            "researched_at": "2026-01-02", "git_commit_sha": "sha", "manifest_id": "m",
        } for i in range(7)]
        worker.write_clinic_atomic(db, 7639, new_rows, _status_row(candidate_count=7))
        rows = db.execute(
            "SELECT treatment_category_name FROM clinic_treatment_research_final WHERE clinic_id=7639"
        ).fetchall()
        assert len(rows) == 7
        assert {r[0] for r in rows} == {f"cat{i}" for i in range(7)}

    def test_3_fetch_failed_then_done_review_one_only_current_result(self, tmp_path):
        db = self._db(tmp_path)
        self._old_fetch_failed_rows(db, 11916, [f"cat{i}" for i in range(43)])
        worker.write_clinic_atomic(db, 11916, [{
            "treatment_category_name": "cat0", "research_status": "REVIEW",
            "evidence_engine_version": "v", "taxonomy_version": "t",
            "researched_at": "2026-01-02", "git_commit_sha": "sha", "manifest_id": "m",
        }], _status_row(candidate_count=1))
        rows = db.execute(
            "SELECT treatment_category_name, research_status FROM clinic_treatment_research_final WHERE clinic_id=11916"
        ).fetchall()
        assert rows == [("cat0", "REVIEW")]

    def test_4_done_candidate_zero_has_zero_treatment_rows_and_done_status(self, tmp_path):
        db = self._db(tmp_path)
        worker.write_clinic_atomic(db, 5, [], _status_row(candidate_count=0))
        assert db.execute("SELECT COUNT(*) FROM clinic_treatment_research_final WHERE clinic_id=5").fetchone()[0] == 0
        assert db.execute("SELECT research_status FROM clinic_research_status WHERE clinic_id=5").fetchone() == ("DONE",)

    def test_5_unresearched_clinic_has_no_status_row(self, tmp_path):
        db = self._db(tmp_path)
        worker.write_clinic_atomic(db, 1, [], _status_row())  # some other clinic researched
        row = db.execute("SELECT * FROM clinic_research_status WHERE clinic_id=999").fetchone()
        assert row is None  # absence == NOT_RESEARCHED, per contract

    def test_6_treatment_category_filter_sees_confirmed_only(self, tmp_path):
        db = self._db(tmp_path)
        worker.write_clinic_atomic(db, 1, [
            {"treatment_category_name": "胃カメラ検査", "research_status": "CONFIRMED",
             "evidence_engine_version": "v", "taxonomy_version": "t", "researched_at": "2026-01-01",
             "git_commit_sha": "sha", "manifest_id": "m"},
            {"treatment_category_name": "白内障手術", "research_status": "REVIEW",
             "evidence_engine_version": "v", "taxonomy_version": "t", "researched_at": "2026-01-01",
             "git_commit_sha": "sha", "manifest_id": "m"},
            {"treatment_category_name": "ICL手術", "research_status": "NOT_CONFIRMED",
             "evidence_engine_version": "v", "taxonomy_version": "t", "researched_at": "2026-01-01",
             "git_commit_sha": "sha", "manifest_id": "m"},
        ], _status_row(candidate_count=3))
        confirmed = db.execute(
            "SELECT treatment_category_name FROM clinic_treatment_research_final "
            "WHERE clinic_id=1 AND research_status='CONFIRMED'"
        ).fetchall()
        assert confirmed == [("胃カメラ検査",)]

    def test_7_research_status_filter_uses_clinic_status_table(self, tmp_path):
        db = self._db(tmp_path)
        worker.write_clinic_atomic(db, 1, [], _status_row(research_status="DONE"))
        worker.write_clinic_atomic(db, 2, [], _status_row(research_status="FETCH_FAILED"))
        done = {r[0] for r in db.execute("SELECT clinic_id FROM clinic_research_status WHERE research_status='DONE'")}
        failed = {r[0] for r in db.execute("SELECT clinic_id FROM clinic_research_status WHERE research_status='FETCH_FAILED'")}
        assert done == {1}
        assert failed == {2}
        assert 3 not in done and 3 not in failed  # NOT_RESEARCHED: absent from both

    def test_8_no_duplicate_clinic_treatment_pairs(self, tmp_path):
        db = self._db(tmp_path)
        worker.write_clinic_atomic(db, 1, [{
            "treatment_category_name": "胃カメラ検査", "research_status": "CONFIRMED",
            "evidence_engine_version": "v", "taxonomy_version": "t", "researched_at": "2026-01-01",
            "git_commit_sha": "sha", "manifest_id": "m",
        }], _status_row(candidate_count=1))
        worker.write_clinic_atomic(db, 1, [{
            "treatment_category_name": "胃カメラ検査", "research_status": "NOT_CONFIRMED",
            "evidence_engine_version": "v", "taxonomy_version": "t", "researched_at": "2026-01-02",
            "git_commit_sha": "sha", "manifest_id": "m",
        }], _status_row(candidate_count=1))
        dupes = db.execute(
            "SELECT clinic_id, treatment_category_name, COUNT(*) c FROM clinic_treatment_research_final "
            "GROUP BY clinic_id, treatment_category_name HAVING c > 1"
        ).fetchall()
        assert dupes == []

    def test_9_distinct_clinic_count_matches_written_clinics(self, tmp_path):
        db = self._db(tmp_path)
        for cid in (1, 2, 3):
            worker.write_clinic_atomic(db, cid, [], _status_row())
        distinct = db.execute("SELECT COUNT(DISTINCT clinic_id) FROM clinic_research_status").fetchone()[0]
        assert distinct == 3


class TestDomainGateConcurrency:
    def test_same_domain_calls_are_serialized(self):
        gate = worker.DomainGate(global_concurrency=8)
        order = []
        lock = threading.Lock()

        def task(n):
            with lock:
                order.append(("start", n))
            time.sleep(0.02)
            with lock:
                order.append(("end", n))

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(gate.run, "same.example", task, n) for n in range(4)]
            for f in futures:
                f.result()

        # For same-domain=1, every "start" must be immediately followed by its own "end"
        # before the next "start" -- no interleaving.
        for i in range(0, len(order), 2):
            assert order[i][0] == "start"
            assert order[i + 1] == ("end", order[i][1])

    def test_different_domains_run_concurrently(self):
        gate = worker.DomainGate(global_concurrency=8)
        started = threading.Event()
        released = threading.Event()

        def blocker():
            started.set()
            released.wait(timeout=2)

        def other():
            started.wait(timeout=2)
            return "ran while blocker held its domain lock"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(gate.run, "domain-a.example", blocker)
            result = pool.submit(gate.run, "domain-b.example", other).result(timeout=2)
            released.set()
            f1.result()
        assert result == "ran while blocker held its domain lock"
