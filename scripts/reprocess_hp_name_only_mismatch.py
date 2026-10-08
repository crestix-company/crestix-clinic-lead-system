#!/usr/bin/env python3
"""One-time backfill for legacy HP REVIEW rows covered by the new identity rule.

Default is DRY RUN. This does not create Human Review labels.
Use --apply to fetch/crawl the saved candidate URL and replace the legacy canonical result.
Content-fetch failure remains AUTO VERIFIED and is recorded as a fetch/content failure.
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

    rows = repo.legacy_name_only_mismatch_candidates(limit=max(1, int(args.limit)))
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

    done, content_failed, failed = [], [], []
    for row in rows:
        cid = int(row["clinic_id"])
        url = str(row["snapshot"].get("best_candidate_url") or "")
        try:
            result = reanalyze_auto_verified_hp(
                store,
                clinic_id=cid,
                selected_url=url,
            )
            if result.get("status") == "CONTENT_FAILED":
                content_failed.append(result)
            else:
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
        "content_failed": len(content_failed),
        "failed": len(failed),
        "done_rows": done,
        "content_failed_rows": content_failed,
        "failed_rows": failed,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
