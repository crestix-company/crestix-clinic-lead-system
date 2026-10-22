import json

from src.io.input_loader import load_table
from src.master.comdesk import COMDESK_EXPORT_HEADERS, COMDESK_HEADERS
from src.master.comdesk_sync import plan_sync, apply_sync
from src.master.store import ClinicStore, dumps, now
from src.repository.sqlite_write_adapter import SqliteClinicWriteRepository
from src.master.fixed_export import fixed_row


def _table(**values):
    row = [""] * len(COMDESK_EXPORT_HEADERS)
    for header, value in values.items():
        row[COMDESK_EXPORT_HEADERS.index(header)] = value
    return load_table((",".join(COMDESK_EXPORT_HEADERS) + "\n" + ",".join(row) + "\n").encode(), "sync.csv")


def _clinic(store, uid="CLINIC-1", rank="B", url="https://old.example.jp", *, maps_url="", maps_status=""):
    effective = {"clinic_name": "検証医院", "phone": "03-1111-2222", "address": "東京都千代田区1-1",
                 "hp_url": url, "hp_rank": rank, "hp_status": "VERIFIED"}
    with store.connect() as c:
        c.execute("INSERT INTO clinics(uuid,clinic_name,phone,address,hp_url,hp_rank,hp_status,maps_website_url,maps_presence_status,effective_json,base_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (uid, effective["clinic_name"], effective["phone"], effective["address"], url, rank, "VERIFIED", maps_url, maps_status,
                   dumps(effective), dumps(effective), now(), now()))
        clinic_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        original = [""] * len(COMDESK_HEADERS)
        original[COMDESK_HEADERS.index("UUID")] = "COMDESK-1"
        original[COMDESK_HEADERS.index("URL")] = url
        original[COMDESK_HEADERS.index("診療日")] = "月火"
        original[COMDESK_HEADERS.index("午前始")] = "09:00"
        original[COMDESK_HEADERS.index("午後終")] = "18:00"
        c.execute("INSERT OR IGNORE INTO templates(id,headers_json,mapping_json,created_at) VALUES(?,?,?,?)",
                  ("test-template", dumps(COMDESK_HEADERS), "{}", now()))
        c.execute("INSERT INTO comdesk_original_rows(clinic_id,template_id,row_json,uuid,source_hash,row_number,created_at) VALUES(?,?,?,?,?,?,?)",
                  (clinic_id, "test-template", dumps(original), "COMDESK-1", "test-source", clinic_id, now()))
        return clinic_id


def test_rank_update_email_unverified_blank_preservation_and_export_contract(tmp_path):
    store = ClinicStore(tmp_path / "isolated.sqlite")
    clinic_id = _clinic(store)
    repo = SqliteClinicWriteRepository(store)
    url_plan = plan_sync(_table(UUID="COMDESK-1", URL="https://new.example.jp"), repo)
    assert url_plan["rows"][0]["status"] == "HOLD"
    assert "URL" in url_plan["rows"][0]["reason"] or "hp_url" in url_plan["rows"][0]["reason"]
    table = _table(UUID="COMDESK-1", **{"HPランク": "C", "メールアドレス": "desk@example.jp"})
    plan = plan_sync(table, repo)
    assert plan["counts"] == {"AUTO_UPDATE": 1, "NO_CHANGE": 0, "HOLD": 0}
    assert plan["rows"][0]["changes"]["hp_rank"] == {"before": "B", "after": "C"}
    assert plan["rows"][0]["changes"]["email"]["after"] == "desk@example.jp"
    apply_sync(repo, plan)
    assert apply_sync(repo, plan) == {"already_applied": True, "updated": 1}
    with store.connect() as c:
        row = c.execute("SELECT uuid,hp_rank,hp_url,base_json FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        assert row["uuid"] == "CLINIC-1"
        assert row["hp_rank"] == "C"
        assert row["hp_url"] == "https://old.example.jp"
        assert json.loads(row["base_json"])["comdesk_unverified_email"] == "desk@example.jp"
        assert c.execute("SELECT count(*) FROM change_history WHERE clinic_id=? AND action='Comdesk医院情報同期'", (clinic_id,)).fetchone()[0] == 1
    reopened = ClinicStore(tmp_path / "isolated.sqlite")
    persisted = SqliteClinicWriteRepository(reopened).comdesk_sync_clinics(["COMDESK-1"])["COMDESK-1"][0]
    assert persisted["hp_rank"] == "C"
    assert persisted["comdesk_sync_hp_rank"] == "C"
    assert persisted["email"] == "desk@example.jp"
    assert reopened.get(clinic_id)["effective_hp_rank"] == "C"
    assert plan_sync(_table(UUID="COMDESK-1", **{"HPランク": "C", "メールアドレス": "desk@example.jp"}), repo)["counts"]["NO_CHANGE"] == 1
    assert len(COMDESK_EXPORT_HEADERS) == 30


def test_empty_fields_duplicate_uuid_and_manual_rank_are_held_or_unchanged(tmp_path):
    store = ClinicStore(tmp_path / "isolated.sqlite")
    clinic_id = _clinic(store)
    repo = SqliteClinicWriteRepository(store)
    with store.connect() as c:
        c.execute("INSERT INTO manual_overrides(clinic_id,field,value_json,source,note,updated_at) VALUES(?,?,?,?,?,?)",
                  (clinic_id, "hp_rank", '"B"', "手動確認", "", now()))
    plan = plan_sync(_table(UUID="CLINIC-1", **{"HPランク": "C", "メールアドレス": ""}), repo)
    assert plan["counts"]["HOLD"] == 1
    assert plan["rows"][0]["changes"] == {}
    with store.connect() as c:
        c.execute("INSERT INTO clinics(uuid,clinic_name,effective_json,base_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                  ("CLINIC-2", "同UUID医院", "{}", "{}", now(), now()))
        other_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO comdesk_original_rows(clinic_id,template_id,row_json,uuid,source_hash,row_number,created_at) VALUES(?,?,?,?,?,?,?)",
                  (other_id, "test-template", "[]", "COMDESK-1", "duplicate-source", 1, now()))
    duplicate = plan_sync(_table(UUID="COMDESK-1", **{"HPランク": "C"}), repo)
    assert duplicate["rows"][0]["status"] == "HOLD"
    missing = plan_sync(_table(UUID="MISSING", **{"HPランク": "C"}), repo)
    assert missing["rows"][0]["status"] == "HOLD"

    race_store = ClinicStore(tmp_path / "manual-race.sqlite")
    race_id = _clinic(race_store)
    race_repo = SqliteClinicWriteRepository(race_store)
    stale = plan_sync(_table(UUID="COMDESK-1", **{"HPランク": "C"}), race_repo)
    with race_store.connect() as c:
        c.execute("INSERT INTO manual_overrides(clinic_id,field,value_json,source,note,updated_at) VALUES(?,?,?,?,?,?)",
                  (race_id, "hp_rank", '"B"', "手動確認", "after preview", now()))
    try:
        apply_sync(race_repo, stale)
    except ValueError as exc:
        assert "手動確定値" in str(exc)
    else:
        raise AssertionError("HPランクの後発手動overrideを上書きできてしまいました")
    assert race_store.get(race_id)["hp_rank"] == "B"


def test_hours_roundtrip_preserves_unmodified_days_and_shifts(tmp_path):
    store = ClinicStore(tmp_path / "hours.sqlite")
    clinic_id = _clinic(store)
    repo = SqliteClinicWriteRepository(store)
    table = _table(UUID="COMDESK-1", **{"休診日": "日曜", "診療日": "月火水木", "午前始": "09:30", "午後始": "14:00"})
    plan = plan_sync(table, repo)
    assert plan["counts"]["AUTO_UPDATE"] == 1
    assert "afternoon_end" not in plan["rows"][0]["changes"]
    assert plan["rows"][0]["changes"]["business_days"]["before"] == "月火"
    apply_sync(repo, plan)
    with store.connect() as c:
        effective = json.loads(c.execute("SELECT effective_json FROM clinics WHERE id=?", (clinic_id,)).fetchone()[0])
    raw = [""] * len(COMDESK_HEADERS)
    raw[COMDESK_HEADERS.index("UUID")] = "CLINIC-1"
    raw[COMDESK_HEADERS.index("URL")] = "https://old.example.jp"
    raw[COMDESK_HEADERS.index("診療日")] = "月火"
    raw[COMDESK_HEADERS.index("午前始")] = "09:00"
    raw[COMDESK_HEADERS.index("午後終")] = "18:00"
    output = fixed_row(effective, COMDESK_HEADERS, {}, raw)
    assert output[COMDESK_HEADERS.index("診療日")] == "月火水木"
    assert output[COMDESK_HEADERS.index("休診日")] == "日曜"
    assert output[COMDESK_HEADERS.index("午前始")] == "09:30"
    assert output[COMDESK_HEADERS.index("午後始")] == "14:00"
    assert output[COMDESK_HEADERS.index("午後終")] == "18:00"


def test_maps_exact_url_auto_updates_and_human_reviewed_url_uses_safe_path(tmp_path):
    store = ClinicStore(tmp_path / "url.sqlite")
    cid = _clinic(store, maps_url="https://clinic.example.jp/new/", maps_status="MAPS_MATCHED_WEBSITE")
    repo = SqliteClinicWriteRepository(store)
    table = _table(UUID="COMDESK-1", URL="http://www.clinic.example.jp/new")
    plan = plan_sync(table, repo)
    assert plan["counts"]["AUTO_UPDATE"] == 1
    apply_sync(repo, plan)
    with store.connect() as c:
        current = c.execute("SELECT hp_url,hp_status,effective_json FROM clinics WHERE id=?", (cid,)).fetchone()
        assert current["hp_url"] == "http://www.clinic.example.jp/new"
        assert current["hp_status"] == "VERIFIED"
        effective = json.loads(current["effective_json"])
        assert effective["comdesk_verified_url"] == current["hp_url"]
    original = [""] * len(COMDESK_HEADERS)
    original[COMDESK_HEADERS.index("URL")] = "https://old.example.jp"
    assert fixed_row(effective, COMDESK_HEADERS, {}, original)[COMDESK_HEADERS.index("URL")] == current["hp_url"]

    other = ClinicStore(tmp_path / "human.sqlite")
    other_id = _clinic(other)
    repository = SqliteClinicWriteRepository(other)
    pending = plan_sync(_table(UUID="COMDESK-1", URL="https://confirmed-by-operator.example.jp/"), repository)
    assert pending["counts"]["HOLD"] == 1
    assert pending["rows"][0]["changes"]["hp_url"]["after"].startswith("https://")
    with other.connect() as c:
        c.execute("INSERT INTO research_results(clinic_id,result_json,updated_at) VALUES(?,?,?)",
                  (other_id, dumps({"hp_candidates": ["old-result"]}), now()))
    result = apply_sync(repository, pending, reviewed_url_rows=[2])
    assert result["updated"] == 1
    with other.connect() as c:
        record = c.execute("SELECT hp_url,hp_status FROM clinics WHERE id=?", (other_id,)).fetchone()
        research = c.execute("SELECT result_json FROM research_results WHERE clinic_id=?", (other_id,)).fetchone()[0]
        history_note = c.execute("SELECT note FROM change_history WHERE clinic_id=? AND action='Comdesk医院情報同期'", (other_id,)).fetchone()[0]
    assert record["hp_status"] == "VERIFIED"
    assert "old-result" in research
    assert "operator_confirmed_url" in history_note


def test_comdesk_unverified_email_is_not_an_outreach_recipient():
    from src.outreach.auto_draft_batch import plan_auto_drafts
    from src.outreach.generation import OutreachResult, READY
    from src.outreach.targets import EMAIL_MISSING, target_from_record

    record = {"id": 71, "uuid": "CLINIC-71", "clinic_name": "検証医院",
              "comdesk_unverified_email": "unverified@example.jp"}
    target = target_from_record(record, email="")  # Outreach supplies Email Research Primary only.
    result = OutreachResult(target=target, status=READY, subject="件名", body="本文")
    plan = plan_auto_drafts([result])
    assert target.email_status == EMAIL_MISSING
    assert not plan.eligible
    assert any("確認済みEmail Researchアドレス" in hold.reason for hold in plan.held)


def test_unverified_email_is_reexported_without_overriding_verified_email():
    from src.repository.runtime_store import SupabaseRuntimeStore

    class Cursor:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params=()):
            if "clinic_email_enrichment" in sql:
                self.rows = self.conn.verified_rows
            elif "comdesk_unverified_email" in sql:
                self.rows = [(71, "unverified@example.jp")]
            else:
                raise AssertionError(sql)

        def fetchall(self):
            return list(self.rows)

    class Connection:
        __module__ = "psycopg.test_fixture"

        def __init__(self, verified_rows):
            self.verified_rows = verified_rows

        def cursor(self):
            return Cursor(self)

    store = object.__new__(SupabaseRuntimeStore)
    store._conn = Connection([])
    rows = store._attach_export_metadata([{"id": 71, "effective_hp_rank": "B", "comdesk_sync_hp_rank": "C"}], [[""] * 28])
    assert rows[0][COMDESK_EXPORT_HEADERS.index("メールアドレス")] == "unverified@example.jp"
    assert rows[0][COMDESK_EXPORT_HEADERS.index("HPランク")] == "C"
    store._conn = Connection([(71, "verified@example.jp", "VERIFIED_EMAIL", True)])
    rows = store._attach_export_metadata([{"id": 71, "effective_hp_rank": "B", "comdesk_sync_hp_rank": "C"}], [[""] * 28])
    assert rows[0][COMDESK_EXPORT_HEADERS.index("メールアドレス")] == "verified@example.jp"
    rows = store._attach_export_metadata([{"id": 71, "hp_rank": "A", "effective_hp_rank": "B",
                                           "comdesk_sync_hp_rank": "C", "manual_fields": ["hp_rank"]}], [[""] * 28])
    assert rows[0][COMDESK_EXPORT_HEADERS.index("HPランク")] == "A"
