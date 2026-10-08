#!/usr/bin/env python3
"""Reprocess legacy HP REVIEW rows now covered by the name-only-mismatch auto-verify rule.

Default is DRY RUN. This does not create Human Review labels.
Use --apply to fetch/crawl the already saved candidate URL and refresh the canonical HP result.
"""
from __future__ import annotations

import argparse
import json

from src.master.hp_human_review import reanalyze_auto_verified_hp
from src.repository.runtime_store import SupabaseRuntimeStore
from src.repository.write_backend import write_repositories_for


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args(argv)

    store = SupabaseRuntimeStore()
    repo = write_repositories_for(store).hp_human_review
    if repo is None or not repo.available():
        raise RuntimeError("HP Human Review repository is unavailable")

    rows = repo.auto_promotable(limit=max(1, int(args.limit)))
    preview = [
        {
            "clinic_id": row["clinic_id"],
            "clinic_name": row["clinic_name"],
            "score": row["snapshot"].get("score"),
            "url": row["snapshot"].get("best_candidate_url"),
            "reason": row["snapshot"].get("auto_accept_reason"),
        }
        for row in rows
    ]

    if not args.apply:
        print(json.dumps({
            "mode": "DRY_RUN",
            "count": len(rows),
            "rows": preview,
        }, ensure_ascii=False, indent=2))
        return

    done, failed = [], []
    for row in rows:
        cid = int(row["clinic_id"])
        url = str(row["snapshot"].get("best_candidate_url") or "")
        try:
            result = reanalyze_auto_verified_hp(
                store,
                clinic_id=cid,
                selected_url=url,
            )
            done.append(result)
        except Exception as exc:
            failed.append({
                "clinic_id": cid,
                "clinic_name": row["clinic_name"],
                "url": url,
                "error": str(exc),
            })

    print(json.dumps({
        "mode": "APPLY",
        "targeted": len(rows),
        "done": len(done),
        "failed": len(failed),
        "done_rows": done,
        "failed_rows": failed,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
