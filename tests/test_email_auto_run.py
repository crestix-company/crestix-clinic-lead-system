from threading import Lock
from threading import Event

from src.master.email_auto_run import EmailAutoRunner
from src.master.email_research_config import EmailResearchConfig
from src.repository.email_research_repository import email_controller_health
from datetime import datetime, timedelta, timezone
import psycopg
import pytest


class Result:
    candidates = []
    research_status = "NO_EMAIL_FOUND"
    metrics = {"search_query_count": 2}


class Shared:
    def __init__(self):
        self.lock = Lock()
        self.pending = [1, 2, 3, 4]
        self.claimed = set()
        self.completed = []
        self.run = {"status": "RUNNING", "stop_requested": False}


class Repo:
    def __init__(self, shared): self.s = shared
    def sync_targets(self, _targets): return 0
    def create_auto_run(self, **kwargs): return "run"
    def auto_run(self, run_id): return dict(self.s.run)
    def update_auto_run(self, run_id, **values): self.s.run.update(values)
    def claim_next(self, **kwargs):
        with self.s.lock:
            if not self.s.pending: return None
            cid = self.s.pending.pop(0)
            assert cid not in self.s.claimed
            self.s.claimed.add(cid)
            return {"clinic_id": cid, "claim_token": f"claim-{cid}"}
    def complete_claim(self, **values):
        with self.s.lock: self.s.completed.append(values["clinic_id"])
    def fail_claim(self, **values): raise AssertionError(values)


def test_start_records_target_reader_stage_without_attempting_sync_or_run_creation():
    class StartRepo:
        def sync_targets(self, _targets):
            raise AssertionError("sync must not run after target reader failure")
        def create_auto_run(self, **_kwargs):
            raise AssertionError("run must not be created after target reader failure")

    def failed_target_reader():
        raise psycopg.OperationalError("password authentication failed for redacted-role")

    runner = EmailAutoRunner(lambda: StartRepo(), lambda *_args: Result(),
                             target_reader=failed_target_reader,
                             config=EmailResearchConfig(workers=1))
    with pytest.raises(psycopg.OperationalError):
        runner.start()
    assert runner.last_start_stage == "target_reader"
    assert not runner.running()


def test_multiple_workers_never_duplicate_claim_and_checkpoint_each_clinic():
    shared = Shared()
    runner = EmailAutoRunner(lambda: Repo(shared), lambda claim, cfg: Result(),
                             config=EmailResearchConfig(workers=4))
    outcome = runner.run("run")
    assert outcome.processed == 4
    assert sorted(shared.completed) == [1, 2, 3, 4]
    assert len(shared.claimed) == 4


def test_resume_existing_run_continues_remaining_checkpoint():
    shared = Shared()
    shared.completed = [1, 2]
    shared.claimed = {1, 2}
    shared.pending = [3, 4]
    runner = EmailAutoRunner(lambda: Repo(shared), lambda claim, cfg: Result(),
                             config=EmailResearchConfig(workers=2))
    outcome = runner.run("run")
    assert outcome.processed == 2
    assert shared.completed == [1, 2, 3, 4]


def test_running_email_consumer_picks_up_clinic_verified_after_start():
    from threading import Event, Lock

    class ProducerConsumerRepo:
        def __init__(self):
            self.lock = Lock()
            self.run = {"status": "RUNNING", "stop_requested": False}
            self.pending = []
            self.newly_hp_verified = []
            self.refreshes = 0
            self.completed = []
            self.refreshed = Event()
            self.completed_event = Event()
        def sync_targets(self, _targets):
            with self.lock:
                self.refreshes += 1
                self.refreshed.set()
                self.pending.extend(self.newly_hp_verified)
                self.newly_hp_verified.clear()
                return len(self.pending)
        def create_auto_run(self, **_kwargs): return "live-run"
        def auto_run(self, _run_id): return dict(self.run)
        def update_auto_run(self, _run_id, **values): self.run.update(values)
        def claim_next(self, **_kwargs):
            with self.lock:
                if not self.pending: return None
                return {"clinic_id": self.pending.pop(0), "claim_token": "one-claim"}
        def complete_claim(self, **values):
            self.completed.append(values["clinic_id"])
            self.completed_event.set()
        def record_auto_run_result(self, _run_id, **_kwargs): pass
        def fail_claim(self, **values): self.run["worker_error"] = values["error"]

    repo = ProducerConsumerRepo()
    runner = EmailAutoRunner(lambda: repo, lambda _claim, _cfg: Result(),
                             target_reader=lambda: [],
                             config=EmailResearchConfig(workers=1, idle_poll_seconds=0.01,
                                                        target_sync_interval_seconds=0.01))
    assert runner.start() == "live-run"
    assert repo.refreshed.wait(2)
    with repo.lock:
        repo.newly_hp_verified.append(881)
    assert repo.completed_event.wait(3)
    runner.kill("live-run")
    runner._thread.join(3)
    assert repo.completed == [881]
    assert repo.run["status"] == "STOPPED", repo.run


def test_existing_primary_is_skipped_by_claim_contract():
    sql = open("src/repository/email_research_repository.py", encoding="utf-8").read()
    assert "s.primary_email_candidate_id IS NULL" in sql
    assert "SKIP LOCKED" in sql
    assert "claim_expires_at<now()" in sql


def test_stale_email_controller_is_recoverable_and_keeps_same_run():
    at = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    run = {
        "run_id": "same-run", "status": "RUNNING",
        "metrics": {"controller_id": "dead-pc", "controller_lease_until":
                    (at - timedelta(seconds=1)).isoformat()},
    }
    assert email_controller_health(run, at=at) == "RECOVERABLE"
    assert run["run_id"] == "same-run"


def test_live_email_controller_is_active():
    at = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    run = {
        "run_id": "run", "status": "RUNNING",
        "metrics": {"controller_id": "active-pc", "controller_lease_until":
                    (at + timedelta(seconds=1)).isoformat()},
    }
    assert email_controller_health(run, at=at) == "ACTIVE"


def test_eight_workers_reclaim_eight_expired_claims_without_duplicates():
    shared = Shared()
    shared.pending = list(range(1, 9))
    runner = EmailAutoRunner(lambda: Repo(shared), lambda claim, cfg: Result(),
                             config=EmailResearchConfig(workers=8))
    outcome = runner.run("run")
    assert outcome.processed == 8
    assert sorted(shared.completed) == list(range(1, 9))
    assert len(shared.claimed) == 8


def test_worker_rebuilds_connection_after_transient_db_disconnect_and_continues():
    shared = Shared()

    class DisconnectedRepo(Repo):
        def auto_run(self, run_id):
            raise psycopg.OperationalError("server closed the connection unexpectedly")

    repos = [DisconnectedRepo(shared), Repo(shared)]
    runner = EmailAutoRunner(lambda: repos.pop(0) if repos else Repo(shared),
                             lambda claim, cfg: Result(),
                             config=EmailResearchConfig(workers=1))
    outcome = runner.run("run")
    assert outcome.processed == 4
    assert sorted(shared.completed) == [1, 2, 3, 4]


def test_one_clinic_failure_does_not_stop_worker_from_processing_following_clinics():
    shared = Shared()
    failed = []

    class FailureIsolatingRepo(Repo):
        def fail_claim(self, **values):
            failed.append(values["clinic_id"])

    def research_one(claim, _config):
        if claim["clinic_id"] == 1:
            raise RuntimeError("one clinic failed")
        return Result()

    runner = EmailAutoRunner(lambda: FailureIsolatingRepo(shared), research_one,
                             config=EmailResearchConfig(workers=1))
    outcome = runner.run("run")
    assert failed == [1]
    assert outcome.errors == 1
    assert outcome.processed == 3
    assert shared.completed == [2, 3, 4]


def test_controller_heartbeat_reconnects_after_one_transient_disconnect():
    import time

    shared = Shared()
    shared.pending = [1]
    shared.heartbeat_calls = 0

    class ControllerRepo(Repo):
        def acquire_controller(self, run_id, controller_id):
            return {"run_id": run_id, "status": "RUNNING"}
        def heartbeat_controller(self, run_id, controller_id):
            with self.s.lock:
                self.s.heartbeat_calls += 1
                if self.s.heartbeat_calls == 1:
                    raise psycopg.OperationalError("connection dropped")
            return True
        def finish_controller(self, run_id, controller_id, *, status, error=""):
            self.s.run["status"] = status
            return True

    def slow_research(_claim, _config):
        time.sleep(0.12)
        return Result()

    runner = EmailAutoRunner(lambda: ControllerRepo(shared), slow_research,
                             config=EmailResearchConfig(workers=1))
    runner.controller_heartbeat_interval_seconds = 0.01
    runner.lease_retry_base_seconds = 0.01
    outcome = runner.run("run")
    assert outcome.processed == 1
    assert shared.heartbeat_calls >= 2
    assert shared.run["status"] == "COMPLETED"


def test_stop_is_terminal_and_resume_only_accepts_paused_run():
    shared = Shared()
    runner = EmailAutoRunner(lambda: Repo(shared), lambda claim, cfg: Result(),
                             target_reader=lambda: [],
                             config=EmailResearchConfig(workers=1))
    runner.kill("run")
    assert shared.run["status"] == "STOPPED"
    assert runner.resume("run") is False


def test_pause_resume_and_stop_halt_new_claims():
    shared = Shared()
    entered = Event()
    release = Event()

    def blocking_research(_claim, _config):
        entered.set()
        assert release.wait(5)
        return Result()

    runner = EmailAutoRunner(lambda: Repo(shared), blocking_research,
                             target_reader=lambda: [],
                             config=EmailResearchConfig(workers=1))
    assert runner.start() == "run"
    assert entered.wait(5)
    runner.pause("run")
    release.set()
    runner._thread.join(5)
    assert shared.run["status"] == "PAUSED"
    assert runner.resume("run") == "run"
    deadline = __import__("time").monotonic() + 5
    while len(shared.completed) < 4 and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert shared.completed == [1, 2, 3, 4]
    runner.kill("run")
    runner._thread.join(5)
    assert shared.run["status"] == "STOPPED"

    shared = Shared()
    entered, release = Event(), Event()
    runner = EmailAutoRunner(lambda: Repo(shared), blocking_research,
                             target_reader=lambda: [],
                             config=EmailResearchConfig(workers=1))
    runner.start()
    assert entered.wait(5)
    runner.kill("run")
    release.set()
    runner._thread.join(5)
    assert shared.run["status"] == "STOPPED"
    assert shared.pending == [2, 3, 4]
    assert runner.resume("run") is False


def test_auto_run_controls_work_offline_and_stop_is_not_resumable():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import os
import streamlit as st
from types import SimpleNamespace
import src.email_research_ui as ui
class FakeRepo:
    run = None
    def available(self): return True
    def summary(self):
        return dict(total=5, processed=0, remaining=5, primary=0, true=0,
                    candidate_review=0, no_email_found=0, error=0, false=0, found=0,
                    elapsed_seconds=0, search_query_count=0, http_attempts=0,
                    retry_count=0, timeout_count=0, query1_called=0,
                    query2_called=0, search_0_count=0)
    def latest_auto_run(self): return self.run
    def cohort_batch_summary(self): return None
    def primary_listing(self, *, limit=500): return []
class FakeHumanRepo:
    def available(self): return True
    def summary(self): return dict(unreviewed=0, reviewed=0)
class FakeRunner:
    def __init__(self, repo): self.repo=repo
    def start(self):
        self.repo.run=dict(run_id="offline",status="RUNNING",metrics={})
        return "offline"
    def pause(self, run_id): self.repo.run["status"]="PAUSED"
    def resume(self, run_id):
        if self.repo.run["status"] != "PAUSED": return False
        self.repo.run["status"]="RUNNING"
        return True
    def kill(self, run_id): self.repo.run["status"]="STOPPED"
repo=st.session_state.setdefault("email_test_repo",FakeRepo())
runner=st.session_state.setdefault("email_test_runner",FakeRunner(repo))
orig_enabled=os.environ.get("EMAIL_AUTO_RUN_ENABLED")
orig_repo=ui.email_repositories_for_ui
orig_target_status=ui.email_target_reader_status
orig_runner=ui._auto_runner
orig_rerun=ui.st.rerun
os.environ["EMAIL_AUTO_RUN_ENABLED"]="true"
ui.email_repositories_for_ui=lambda:SimpleNamespace(email_research=repo,email_human_review=FakeHumanRepo())
ui.email_target_reader_status=lambda:SimpleNamespace(ready=True,message="")
ui._auto_runner=lambda: runner
ui.st.rerun=lambda **_kwargs: orig_rerun()
try:
    class FakeStore: is_supabase_runtime = True
    ui.email_research_page(FakeStore())
finally:
    ui.email_repositories_for_ui=orig_repo
    ui.email_target_reader_status=orig_target_status
    ui._auto_runner=orig_runner
    ui.st.rerun=orig_rerun
    if orig_enabled is None: os.environ.pop("EMAIL_AUTO_RUN_ENABLED",None)
    else: os.environ["EMAIL_AUTO_RUN_ENABLED"]=orig_enabled
''')
    app.run()
    next(button for button in app.button if button.label == "メール自動調査を開始").click().run()
    repo = app.session_state["email_test_repo"]
    assert repo.run["status"] == "RUNNING"
    next(button for button in app.button if button.label == "一時停止").click().run()
    assert repo.run["status"] == "PAUSED"
    next(button for button in app.button if button.label == "再開").click().run()
    assert repo.run["status"] == "RUNNING"
    next(button for button in app.button if button.label == "停止").click().run()
    assert repo.run["status"] == "STOPPED"
    assert next(button for button in app.button if button.label == "再開").disabled


def test_simple_flow_keeps_hp_controls_and_adds_independent_email_control():
    import inspect
    import app_v2
    source = inspect.getsource(app_v2.simple_workflow_ui)
    assert '"HP自動調査を開始"' in source
    assert "email_auto_research_card(store, demo=demo)" in source


def test_email_research_card_does_not_change_hp_run_state_offline():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import os
import streamlit as st
from types import SimpleNamespace
import src.email_research_ui as ui
class Repo:
    run=None
    def available(self): return True
    def summary(self): return dict(total=1,processed=0,remaining=1,primary=0,email_obtained=0,
      true=0,candidate_review=0,no_email_found=0,error=0,found=0,elapsed_seconds=0,
      search_query_count=0,http_attempts=0,retry_count=0,timeout_count=0,query1_called=0,
      query2_called=0,search_0_count=0)
    def latest_auto_run(self): return self.run
class Human:
    def available(self): return True
    def summary(self): return dict(unreviewed=0,unreviewed_clinics=0,reviewed=0)
class Runner:
    def start(self): repo.run=dict(run_id="e1",status="RUNNING",metrics={}); return "e1"
    def pause(self,r): repo.run["status"]="PAUSED"
    def resume(self,r): repo.run["status"]="RUNNING"; return True
    def kill(self,r): repo.run["status"]="STOPPED"
class Store: is_supabase_runtime=True
hp_run={"run_id":"hp-current-unchanged","status":"RUNNING","claims":7}
repo=st.session_state.setdefault("repo",Repo())
orig_repo=ui.email_repositories_for_ui; orig_runner=ui._auto_runner
orig_target_status=ui.email_target_reader_status
orig_rerun=ui.st.rerun
os.environ["EMAIL_AUTO_RUN_ENABLED"]="true"
ui.email_repositories_for_ui=lambda:SimpleNamespace(email_research=repo,email_human_review=Human())
ui.email_target_reader_status=lambda:SimpleNamespace(ready=True,message="")
ui._auto_runner=lambda:Runner()
ui.st.rerun=lambda **_kwargs: orig_rerun()
try: ui.email_auto_research_card(Store())
finally: ui.email_repositories_for_ui=orig_repo; ui.email_target_reader_status=orig_target_status; ui._auto_runner=orig_runner; ui.st.rerun=orig_rerun
st.session_state["hp_run"]=hp_run
''').run()
    assert not app.exception and not app.error
    assert any(button.label == "メール自動調査を開始" for button in app.button)
    next(button for button in app.button if button.label == "メール自動調査を開始").click().run()
    assert app.session_state["repo"].run["status"] == "RUNNING"
    assert app.session_state["hp_run"] == {"run_id":"hp-current-unchanged","status":"RUNNING","claims":7}


def test_email_start_auth_failure_is_explicit_persistent_and_secret_free():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import os
import psycopg
from types import SimpleNamespace
import src.email_research_ui as ui
class Repo:
    def available(self): return True
    def summary(self): return dict(total=1,processed=0,remaining=1,primary=0,email_obtained=0,
      true=0,candidate_review=0,no_email_found=0,error=0,found=0,elapsed_seconds=0,
      search_query_count=0,http_attempts=0,retry_count=0,timeout_count=0,query1_called=0,
      query2_called=0,search_0_count=0)
    def latest_auto_run(self): return dict(run_id="old",status="COMPLETED",metrics={})
class Human:
    def available(self): return True
    def summary(self): return dict(unreviewed=0,unreviewed_clinics=0,reviewed=0)
class Runner:
    last_start_stage="target_reader"
    def start(self):
        raise psycopg.OperationalError("password authentication failed host=must-not-leak")
class Store: is_supabase_runtime=True
repo=Repo()
orig_repo=ui.email_repositories_for_ui; orig_runner=ui._auto_runner
orig_target_status=ui.email_target_reader_status
os.environ["EMAIL_AUTO_RUN_ENABLED"]="true"
ui.email_repositories_for_ui=lambda:SimpleNamespace(email_research=repo,email_human_review=Human())
ui.email_target_reader_status=lambda:SimpleNamespace(ready=True,message="")
ui._auto_runner=lambda:Runner()
try: ui.email_auto_research_card(Store())
finally: ui.email_repositories_for_ui=orig_repo; ui.email_target_reader_status=orig_target_status; ui._auto_runner=orig_runner
''')
    app.run()
    next(button for button in app.button if button.label == "メール自動調査を開始").click().run()
    assert not app.exception
    assert any("開始失敗（FULL対象取得）" in error.value for error in app.error)
    assert all("must-not-leak" not in error.value for error in app.error)
    # A timer/fragment rerun must not erase the actionable error immediately.
    app.run()
    assert any("DB認証に失敗" in error.value for error in app.error)


def test_email_start_commit_outcome_unknown_does_not_claim_run_was_not_created():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import os
import psycopg
from types import SimpleNamespace
import src.email_research_ui as ui
class Repo:
    def available(self): return True
    def summary(self): return dict(total=1,processed=0,remaining=1,primary=0,email_obtained=0,
      true=0,candidate_review=0,no_email_found=0,error=0,found=0,elapsed_seconds=0,
      search_query_count=0,http_attempts=0,retry_count=0,timeout_count=0,query1_called=0,
      query2_called=0,search_0_count=0)
    def latest_auto_run(self): return dict(run_id="possibly-created",status="COMPLETED",metrics={})
class Human:
    def available(self): return True
    def summary(self): return dict(unreviewed=0,unreviewed_clinics=0,reviewed=0)
class Runner:
    last_start_stage="create_auto_run"
    def start(self):
        # Simulate the client losing the response after the database committed.
        raise psycopg.OperationalError("connection lost after commit; secret=must-not-leak")
class Store: is_supabase_runtime=True
repo=Repo()
orig_repo=ui.email_repositories_for_ui; orig_runner=ui._auto_runner
orig_target_status=ui.email_target_reader_status
os.environ["EMAIL_AUTO_RUN_ENABLED"]="true"
ui.email_repositories_for_ui=lambda:SimpleNamespace(email_research=repo,email_human_review=Human())
ui.email_target_reader_status=lambda:SimpleNamespace(ready=True,message="")
ui._auto_runner=lambda:Runner()
try: ui.email_auto_research_card(Store())
finally: ui.email_repositories_for_ui=orig_repo; ui.email_target_reader_status=orig_target_status; ui._auto_runner=orig_runner
''')
    app.run()
    next(button for button in app.button if button.label == "メール自動調査を開始").click().run()
    assert not app.exception
    messages = [error.value for error in app.error]
    assert any("開始結果を確認できません" in message for message in messages)
    assert any("再実行せず" in message and "Run一覧を再読込" in message for message in messages)
    assert all("新規Runは作成されていません" not in message for message in messages)
    assert all("must-not-leak" not in message for message in messages)


def test_email_start_runner_busy_is_not_silent():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import os
from types import SimpleNamespace
import src.email_research_ui as ui
class Repo:
    def available(self): return True
    def summary(self): return dict(total=1,processed=1,remaining=0,primary=0,email_obtained=0,
      true=0,candidate_review=0,no_email_found=1,error=0,found=0,elapsed_seconds=0,
      search_query_count=0,http_attempts=0,retry_count=0,timeout_count=0,query1_called=0,
      query2_called=0,search_0_count=0)
    def latest_auto_run(self): return dict(run_id="old",status="COMPLETED",metrics={})
class Human:
    def available(self): return True
    def summary(self): return dict(unreviewed=0,unreviewed_clinics=0,reviewed=0)
class Runner:
    last_start_stage="already_running"
    def start(self): return False
class Store: is_supabase_runtime=True
repo=Repo()
orig_repo=ui.email_repositories_for_ui; orig_runner=ui._auto_runner
orig_target_status=ui.email_target_reader_status
os.environ["EMAIL_AUTO_RUN_ENABLED"]="true"
ui.email_repositories_for_ui=lambda:SimpleNamespace(email_research=repo,email_human_review=Human())
ui.email_target_reader_status=lambda:SimpleNamespace(ready=True,message="")
ui._auto_runner=lambda:Runner()
try: ui.email_auto_research_card(Store())
finally: ui.email_repositories_for_ui=orig_repo; ui.email_target_reader_status=orig_target_status; ui._auto_runner=orig_runner
''')
    app.run()
    next(button for button in app.button if button.label == "メール自動調査を開始").click().run()
    assert not app.exception
    assert any("前回Runの終了処理" in error.value for error in app.error)


def test_email_top_kpis_and_japanese_tooltips_render_offline():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import streamlit as st
from types import SimpleNamespace
import app_v2
class EmailRepo:
    def available(self): return True
    def summary(self): return dict(email_obtained=8,not_researched=21,email_researched=13,remaining=21)
class HumanRepo:
    def available(self): return True
    def summary(self): return dict(unreviewed=4,unreviewed_clinics=3)
from src.repository import email_database
old=email_database.email_repositories_for_ui
email_database.email_repositories_for_ui=lambda:SimpleNamespace(email_research=EmailRepo(),email_human_review=HumanRepo())
try: app_v2.show_email_research_metrics(object())
finally: email_database.email_repositories_for_ui=old
''').run()
    assert not app.exception and not app.error
    metrics = {metric.label: metric for metric in app.metric}
    assert metrics["Email取得済み"].value == "8件"
    assert "営業利用できる" in metrics["Email取得済み"].help
    assert metrics["Email確認待ち"].value == "3件"
    assert "メール確認画面" in metrics["Email確認待ち"].help
    assert metrics["Email未調査"].value == "21件"
    assert "まだメールアドレス" in metrics["Email未調査"].help
    assert metrics["Email調査済み"].value == "13件"
    assert "Emailあり・確認待ち・Emailなし" in metrics["Email調査済み"].help


class CohortShared:
    def __init__(self, cohort):
        self.lock = Lock()
        self.cohort = set(cohort)
        self.pending = list(cohort)
        self.claimed = set()
        self.completed = set()
        self.run = {"status": "RUNNING", "stop_requested": False}
        self.sync_calls = 0


class CohortRepo:
    """Mimics only the surface EmailAutoRunner touches, plus cohort_pending_count() --
    the new signal that lets a Canary run stop outright instead of idle-polling forever."""
    def __init__(self, shared):
        self.s = shared

    def sync_targets(self, _targets):
        self.s.sync_calls += 1
        return 0

    def create_auto_run(self, **kwargs): return "canary-run"
    def auto_run(self, run_id): return dict(self.s.run)
    def update_auto_run(self, run_id, **values): self.s.run.update(values)

    def claim_next(self, **kwargs):
        with self.s.lock:
            if not self.s.pending:
                return None
            cid = self.s.pending.pop(0)
            self.s.claimed.add(cid)
            return {"clinic_id": cid, "claim_token": f"claim-{cid}"}

    def complete_claim(self, **values):
        with self.s.lock:
            cid = values["clinic_id"]
            self.s.claimed.discard(cid)
            self.s.completed.add(cid)

    def fail_claim(self, **values): raise AssertionError(values)

    def cohort_pending_count(self):
        # Pending = not yet terminal: still queued or still claimed/running.
        with self.s.lock:
            return len(self.s.cohort) - len(self.s.completed)


# E: once every cohort clinic_id is terminal (and none still claimed), the runner stops
# outright instead of entering the idle-poll loop.
def test_canary_runner_stops_once_entire_cohort_is_terminal():
    shared = CohortShared(cohort=[1, 2, 3, 4, 5])
    runner = EmailAutoRunner(lambda: CohortRepo(shared), lambda claim, cfg: Result(),
                             target_reader=lambda: (), config=EmailResearchConfig(workers=3))
    outcome = runner.run("canary-run")
    assert outcome.processed == 5
    assert shared.completed == {1, 2, 3, 4, 5}
    assert shared.run["status"] == "COMPLETED"
    assert shared.run["stop_requested"] is True


# F: a clinic_id that becomes newly eligible after the cohort finishes is never picked up
# -- the runner has already stopped, there is no further polling to catch it.
def test_canary_runner_does_not_resume_for_newly_eligible_clinics_after_stop():
    shared = CohortShared(cohort=[1, 2, 3])
    runner = EmailAutoRunner(lambda: CohortRepo(shared), lambda claim, cfg: Result(),
                             target_reader=lambda: (), config=EmailResearchConfig(workers=2))
    runner.run("canary-run")
    assert shared.completed == {1, 2, 3}
    # Simulate a clinic newly becoming eligible in the live pool after the Canary ended.
    shared.pending.append(99)
    shared.cohort.add(99)
    # The runner already returned from run() -- there is no worker loop left to notice
    # this, by construction. Assert the inert state directly: nothing further claimed it.
    assert 99 not in shared.claimed
    assert 99 not in shared.completed


# G: normal (non-Canary) mode is unaffected -- a repo without cohort_pending_count() at
# all skips the new branch entirely, and idle-polling behavior is unchanged (already
# covered by every existing test in this file using the plain Repo fake, none of which
# implement cohort_pending_count and all of which still pass unmodified). This test pins
# the other normal-mode case: a cohort-capable repository explicitly configured with
# cohort=None behaves identically to a repo that doesn't have the method at all.
def test_normal_mode_repo_with_cohort_pending_count_returning_none_does_not_stop_early():
    class NoneCohortRepo(CohortRepo):
        def cohort_pending_count(self):
            return None  # SupabaseEmailResearchRepository's real behavior when cohort=None

    shared = CohortShared(cohort=[1, 2])
    stop_after_drain = Event()

    def research_one(claim, cfg):
        return Result()

    runner = EmailAutoRunner(lambda: NoneCohortRepo(shared), research_one,
                             target_reader=lambda: (),
                             config=EmailResearchConfig(workers=1, idle_poll_seconds=0.01))
    run_id = runner.start()
    # Let it drain the 2 queued clinics, then idle-poll (NOT stop, since
    # cohort_pending_count() -> None means "not a Canary run" to the runner).
    import time
    deadline = time.monotonic() + 2
    while len(shared.completed) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert shared.completed == {1, 2}
    time.sleep(0.05)
    assert shared.run["status"] == "RUNNING"  # still idle-polling, not auto-stopped
    runner.kill(run_id)
    runner._thread.join(3)
