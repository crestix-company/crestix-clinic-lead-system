"""SQLite implementation of TreatmentWriteRepository -- thin delegation to the existing,
unmodified scripts.research_worker.write_clinic_atomic(). Zero behavior change: same
connection object the worker already manages (one long-lived connection per run, not
per-call connect() like ClinicStore), same SQL, same single-transaction atomicity.
"""


class SqliteTreatmentWriteRepository:
    def __init__(self, final_db_conn):
        self._conn = final_db_conn

    def write_clinic_result(self, clinic_id, rows, status_row):
        from scripts.research_worker import write_clinic_atomic
        write_clinic_atomic(self._conn, clinic_id, rows, status_row)
