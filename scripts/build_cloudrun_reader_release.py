"""Build manual DBA SQL; never connect to or apply changes to a database."""
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'docs/cloudrun_reader_release'


def transaction_body(sql):
    assert sql.count('\nBEGIN;\n') == 1 and sql.count('\nCOMMIT;') == 1
    return sql.split('\nBEGIN;\n', 1)[1].rsplit('\nCOMMIT;', 1)[0]


def main():
    projection = (PACKAGE / 'projection_85aabbec.sql').read_text()
    rollback = (PACKAGE / 'projection_rollback_85aabbec.sql').read_text()
    header = ('-- REVIEW PROPOSAL ONLY. Do not apply without explicit production approval.\n'
              '-- Pinned PR #122 head: 85aabbec218b1e73124bc5143a3d5dee351fe3d3\n'
              f'-- Projection SHA256: {hashlib.sha256(projection.encode()).hexdigest()}\n'
              '-- Apply this combined file ONCE; do not separately apply the pinned migration.\n')
    (PACKAGE / 'apply.sql').write_text(
        header + 'BEGIN;\n' + (PACKAGE / 'preflight_guard.sql').read_text()
        + "\n-- Temporary creator membership for ownership transfer under non-superuser postgres.\n"
        + "SET LOCAL createrole_self_grant = 'SET, INHERIT';\n"
        + '\n-- BEGIN PINNED PROJECTION BODY\n' + transaction_body(projection)
        + '\n-- END PINNED PROJECTION BODY\n' + (PACKAGE / 'reader_grants.sql').read_text()
        + '\nCOMMIT;\n')
    (PACKAGE / 'rollback.sql').write_text(
        header + 'BEGIN;\n'
        + 'GRANT clinic_cloudrun_projection_owner TO postgres WITH INHERIT TRUE, SET TRUE;\n'
        + (PACKAGE / 'reader_rollback.sql').read_text()
        + '\n' + transaction_body(rollback) + '\nCOMMIT;\n')


if __name__ == '__main__':
    main()
