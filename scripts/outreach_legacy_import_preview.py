"""Read-only preview of the explicitly selected historical outreach CSV."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.outreach.legacy_import_preview import parse_legacy_import_csv, preview_legacy_import


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="The one explicitly selected, unmodified source CSV")
    args = parser.parse_args(argv)
    data = args.input.read_bytes()
    preview = preview_legacy_import(parse_legacy_import_csv(data))
    print(json.dumps({
        "source_file": args.input.name,
        "total_rows": preview.total_rows,
        "unique_clinic_ids": preview.unique_clinic_ids,
        "unique_clinic_uuids": preview.unique_clinic_uuids,
        "duplicate_clinic_ids": preview.duplicate_clinic_ids,
        "duplicate_clinic_uuids": preview.duplicate_clinic_uuids,
        "incomplete_identity_rows": preview.incomplete_identity_rows,
        "listing_status_counts": preview.listing_status_counts,
        "gmail_state_counts": preview.gmail_state_counts,
        "expected_328_rows": preview.expected_count_matches,
        "expected_listing_distribution": preview.status_count_matches,
        "database_written": False,
    }, ensure_ascii=False, indent=2))
    return 0 if preview.expected_count_matches and preview.status_count_matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
