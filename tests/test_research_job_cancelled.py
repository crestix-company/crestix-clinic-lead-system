import json
import hashlib

from src.master.filters import Filters
from src.master.jobs import (
    ITEM_TRANSITIONS,
    TERMINAL_ITEM_STATES,
    create_job,
    job_status,
    repair_reset_job_items,
    reset_job,
    run_job,
)
from src.master.samples import SampleFetcher, SampleProvider, sample_records
from src.master.store import ClinicStore
from scripts.repair_cancelled_reset_items import main as repair_main


def make_job(store,status,states):
    store.import_master(sample_records()[:len(states)])
    jid = create_job(store,Filters(active_only=False,hp_only=False),limit=len(states))
    with store.connect() as connection:
        connection.execute("UPDATE research_jobs SET status=? WHERE id=?",(status,jid))
        clinic_ids = [row[0] for row in connection.execute(
            "SELECT clinic_id FROM research_job_items WHERE job_id=? ORDER BY clinic_id",(jid,)
        )]
        for clinic_id,state in zip(clinic_ids,states):
            connection.execute(
                "UPDATE research_job_items SET state=? WHERE job_id=? AND clinic_id=?",
                (state,jid,clinic_id),
            )
    return jid


def test_cancelled_state_model_is_terminal():
    assert ("PENDING","CANCELLED") in ITEM_TRANSITIONS
    assert "CANCELLED" in TERMINAL_ITEM_STATES
    assert not any(source=="CANCELLED" for source,_ in ITEM_TRANSITIONS)
    assert ("PENDING","DONE") not in ITEM_TRANSITIONS
    assert ("DONE","CANCELLED") not in ITEM_TRANSITIONS


def test_reset_job_cancels_pending_without_deleting_results(tmp_path):
    store = ClinicStore(tmp_path/"reset.db")
    jid = make_job(store,"PAUSED",["PENDING","DONE"])
    with store.connect() as connection:
        clinic_id = connection.execute(
            "SELECT clinic_id FROM research_job_items WHERE job_id=? ORDER BY clinic_id LIMIT 1",(jid,)
        ).fetchone()[0]
        connection.execute("INSERT INTO research_results VALUES(?,?,?)",
                           (clinic_id,json.dumps({"kept":True}),"fixture"))
    reset_job(store,jid)
    assert job_status(store,jid)["counts"] == {"CANCELLED":1,"DONE":1}
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_results").fetchone()[0] == 1


def test_cancelled_is_not_claimed_and_reset_job_cannot_resume(tmp_path):
    store = ClinicStore(tmp_path/"claim.db")
    jid = make_job(store,"RESET",["CANCELLED"])
    provider = SampleProvider()
    assert run_job(store,jid,provider,SampleFetcher()) is True
    assert provider.calls == 0
    assert job_status(store,jid)["status"] == "RESET"
    assert job_status(store,jid)["counts"] == {"CANCELLED":1}


def test_historical_repair_dry_run_execute_and_idempotency(tmp_path):
    store = ClinicStore(tmp_path/"repair.db")
    reset_id = make_job(store,"RESET",["PENDING","RUNNING","DONE"])
    paused_id = make_job(store,"PAUSED",["PENDING"])

    dry = repair_reset_job_items(store,[reset_id,paused_id],dry_run=True)
    assert dry["before"] == 1 and dry["changed"] == 0 and dry["after"] == 1
    assert job_status(store,reset_id)["counts"] == {"DONE":1,"PENDING":1,"RUNNING":1}

    executed = repair_reset_job_items(store,[reset_id,paused_id],dry_run=False)
    assert executed["before"] == 1 and executed["changed"] == 1 and executed["after"] == 0
    assert job_status(store,reset_id)["counts"] == {"CANCELLED":1,"DONE":1,"RUNNING":1}
    assert job_status(store,paused_id)["counts"] == {"PENDING":1}

    second = repair_reset_job_items(store,[reset_id,paused_id],dry_run=False)
    assert second["before"] == second["changed"] == second["after"] == 0


def test_repair_cli_dry_run_is_byte_for_byte_read_only(tmp_path):
    path = tmp_path/"readonly.db"
    store = ClinicStore(path)
    jid = make_job(store,"RESET",["PENDING"])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = repair_main(["--db",str(path),"--job-id",jid])
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["before"] == result["after"] == 1
    assert result["changed"] == 0
    assert before == after
