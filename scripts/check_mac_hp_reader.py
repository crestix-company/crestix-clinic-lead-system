"""Read-only acceptance check for the Mac HP Reader before starting Streamlit."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.launch_v2_mac_reader import (
    NORMAL_HP_REQUIRED_COLUMNS,
    NORMAL_HP_REQUIRED_TABLES,
    NORMAL_READER_FILE,
    load_reader_dsn,
    verify_reader,
)


def check_reader(*, reader_file=None, connect=None):
    dsn = load_reader_dsn(reader_file or NORMAL_READER_FILE)
    role, clinic_count = verify_reader(
        dsn,
        required_tables=NORMAL_HP_REQUIRED_TABLES,
        required_columns=NORMAL_HP_REQUIRED_COLUMNS,
        count_relation="public.clinic_cloudrun_readonly",
        connect=connect,
    )
    if connect is None:
        import psycopg
        connect = psycopg.connect
    with connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                "SELECT count(*) FROM research.research_jobs "
                "WHERE kind='hp' AND status<>'RESET'"
            )
            hp_job_count = int(cur.fetchone()[0])
            cur.execute(
                "SELECT count(*) FROM research.research_job_items i "
                "JOIN research.research_jobs j ON j.id=i.job_id "
                "WHERE j.kind='hp' AND j.status<>'RESET'"
            )
            hp_item_count = int(cur.fetchone()[0])
        conn.rollback()
    return {
        "role": role,
        "visible_clinics": clinic_count,
        "visible_hp_jobs": hp_job_count,
        "visible_hp_job_items": hp_item_count,
    }


def main():
    result = check_reader()
    print(
        "HP Reader受入検査OK: "
        f"role={result['role']}, 医院={result['visible_clinics']:,}件, "
        f"HP Job={result['visible_hp_jobs']:,}件, "
        f"対象item={result['visible_hp_job_items']:,}件"
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError) as exc:
        print(f"HP Reader検査を中止しました: {exc}", file=sys.stderr)
        sys.exit(1)
