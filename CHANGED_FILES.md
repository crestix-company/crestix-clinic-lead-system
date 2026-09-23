# Changed files — 2.1.0

- `src/master/google_maps.py` — Maps queue/export/import/matching/idempotency
- `src/master/store.py` — schema v4, Maps storage/projected fields, metrics, queue/full CSV APIs
- `src/master/filters.py` — Google Maps掲載確認 filter
- `src/master/fixed_export.py` — 28-column contract, existing URL protection, blank URL Maps supplementation, excluded hospital/center filtering
- `src/enrichment/researcher.py` — Maps website first source and no HP-discovery search API when Maps website exists
- `app_v2.py` — Maps queue download/import/full CSV UI, Maps filter/display
- `tests/test_google_maps_integration.py` — new regression tests
- `README.md`, `README_V2.md`, `CODEX_HANDOFF_MINIMAL.md` — current two-app workflow
- `VERSION` — 2.1.0
