"""Opt-in DDL/concurrency verification; only accepts the named local Phase D DB."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import os
import ipaddress
from urllib.parse import urlparse
import uuid

import pytest


DSN = os.environ.get("PHASE_D_LOCAL_DATABASE_URL", "").strip()
TEST_DATABASE = "phase_d_shared_draft_verify_v2"
pytestmark = pytest.mark.skipif(not DSN, reason="explicit local Phase D PostgreSQL DSN required")


def _connect(psycopg):
    parsed = urlparse(DSN)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path != "/" + TEST_DATABASE:
        pytest.fail("Safety stop: Phase D DB test only permits its named local verification database")
    connection = psycopg.connect(DSN, connect_timeout=2)
    row = connection.execute("SELECT current_database(),inet_server_addr()::text").fetchone()
    database_name = row[0].decode("ascii") if isinstance(row[0], bytes) else row[0]
    address_text = row[1].decode("ascii") if isinstance(row[1], bytes) else row[1]
    server_ip = ipaddress.ip_interface(address_text).ip if address_text else None
    if database_name != TEST_DATABASE or not server_ip or not server_ip.is_loopback:
        connection.close()
        pytest.fail("Safety stop: nonlocal/nonfixture PostgreSQL server")
    return connection


def _text(value):
    return value.decode("ascii") if isinstance(value, bytes) else value


def test_real_postgres_migration_constraints_rls_concurrency_and_fencing():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.errors import UniqueViolation

    clinic_id = uuid.uuid4().int % 1_000_000_000 + 8_000_000_000
    clinic_uuid = str(uuid.uuid4())
    operation_key = "phase-d-" + str(uuid.uuid4())
    campaign = "phase-d-concurrency"
    gas_case = "phase-d-case-" + str(uuid.uuid4())
    barrier = Barrier(2)
    owners = [str(uuid.uuid4()), str(uuid.uuid4())]

    with _connect(psycopg) as setup:
        setup.execute("INSERT INTO public.clinics(id,uuid) VALUES (%s,%s)", (clinic_id, clinic_uuid))
        setup.commit()

    def acquire(owner):
        with _connect(psycopg) as connection:
            connection.execute("SET ROLE clinic_outreach_runtime")
            barrier.wait(timeout=5)
            result = connection.execute(
                "SELECT acquired,status,fencing_token FROM outreach.acquire_shared_draft_lease(%s,%s,%s,%s,%s,%s::uuid,%s)",
                (operation_key, clinic_uuid, clinic_id, campaign, gas_case, owner, 60),
            ).fetchone()
            connection.commit()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(acquire, owners))
        assert sum(bool(result[0]) for result in results) == 1
        assert sum(_text(result[1]) == "LEASED" for result in results) == 1
        acquired = next(result for result in results if result[0])
        fence = int(acquired[2])
        owner = owners[results.index(acquired)]

        with _connect(psycopg) as conflicting_key:
            conflicting_key.execute("SET ROLE clinic_outreach_runtime")
            conflict = conflicting_key.execute(
                "SELECT acquired,status FROM outreach.acquire_shared_draft_lease(%s,%s,%s,%s,%s,%s::uuid,%s)",
                (operation_key + "-other-key", clinic_uuid, clinic_id, campaign, "", str(uuid.uuid4()), 60),
            ).fetchone()
            assert conflict[0] is False and _text(conflict[1]) == "OPERATION_KEY_CONFLICT"
            conflicting_key.commit()

        # Direct write is denied; only the execute-granted functions can mutate.
        with _connect(psycopg) as runtime:
            runtime.execute("SET ROLE clinic_outreach_runtime")
            assert runtime.execute("SELECT has_table_privilege(current_user,'outreach.shared_draft_operations','SELECT'),has_table_privilege(current_user,'outreach.shared_draft_operations','INSERT'),has_function_privilege(current_user,'outreach.consume_shared_draft_api_rate(text,timestamptz,integer)','EXECUTE')").fetchone() == (True, False, True)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.execute("UPDATE outreach.shared_draft_operations SET status='HOLD' WHERE operation_key=%s", (operation_key,))
            runtime.rollback()
        with _connect(psycopg) as privilege_check:
            assert privilege_check.execute("SELECT has_function_privilege('anon','outreach.acquire_shared_draft_lease(text,text,bigint,text,text,uuid,integer)','EXECUTE'),has_function_privilege('clinic_runtime','outreach.acquire_shared_draft_lease(text,text,bigint,text,text,uuid,integer)','EXECUTE')").fetchone() == (False, False)
            assert privilege_check.execute("SELECT has_schema_privilege('clinic_outreach_runtime','outreach','USAGE'),has_schema_privilege('anon','outreach','USAGE')").fetchone() == (True, False)

        # A distinct operation cannot reserve the same clinic/campaign row.
        with _connect(psycopg) as duplicate:
            with pytest.raises(UniqueViolation):
                duplicate.execute(
                    "INSERT INTO outreach.shared_draft_operations(operation_key,clinic_id,clinic_uuid,campaign_id) VALUES (%s,%s,%s,%s)",
                    (operation_key + "-duplicate", clinic_id, clinic_uuid, campaign),
                )
            duplicate.rollback()

        # Expiration fences the original worker and prevents silent lease takeover.
        with _connect(psycopg) as admin:
            admin.execute("UPDATE outreach.shared_draft_operations SET lease_expires_at=now()-interval '1 second' WHERE operation_key=%s", (operation_key,))
            admin.commit()
            stale_complete = admin.execute(
                "SELECT outreach.complete_shared_draft_operation(%s,%s::uuid,%s,%s,%s,%s)",
                (operation_key, owner, fence, "stale-draft", "stale-message", "stale-thread"),
            ).fetchone()[0]
            assert stale_complete is False
            admin.execute("SELECT * FROM outreach.acquire_shared_draft_lease(%s,%s,%s,%s,%s,%s::uuid,%s)",
                          (operation_key, clinic_uuid, clinic_id, campaign, gas_case, str(uuid.uuid4()), 60))
            state = admin.execute("SELECT status,fencing_token,lease_owner FROM outreach.shared_draft_operations WHERE operation_key=%s", (operation_key,)).fetchone()
            assert _text(state[0]) == "UNKNOWN" and state[1] > fence and state[2] is None
            admin.commit()
    finally:
        with _connect(psycopg) as cleanup:
            cleanup.execute("DELETE FROM outreach.gas_case_identity_links WHERE operation_key=%s", (operation_key,))
            cleanup.execute("DELETE FROM outreach.shared_draft_operation_events WHERE operation_key=%s", (operation_key,))
            cleanup.execute("DELETE FROM outreach.shared_draft_operations WHERE operation_key=%s", (operation_key,))
            cleanup.execute("DELETE FROM outreach.clinic_draft_guard WHERE clinic_id=%s", (clinic_id,))
            cleanup.execute("DELETE FROM public.clinics WHERE id=%s", (clinic_id,))
            cleanup.commit()

