"""Idempotently import the connector READ ONLY Phase I snapshot into local Phase J PostgreSQL."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.outreach.phase_j_local_db import import_phase_i_snapshot


if __name__ == "__main__":
    print(json.dumps(import_phase_i_snapshot(), ensure_ascii=False, indent=2))
