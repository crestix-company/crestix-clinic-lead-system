# Test results — 2026-09-15

- Python syntax/compile: PASS (`python -m compileall -q .`)
- Python tests executable in this environment: **265 passed**
  - command: `pytest -q --ignore=tests/test_app.py --ignore=tests/test_v2_app.py`
- Full pytest collection: NOT COMPLETED in this container because `streamlit` is not installed and network access is unavailable, so the two Streamlit AppTest files cannot be imported here.
- New Google Maps integration tests: included in the 265 passed.
- Real Chrome / real Google Maps test: **not performed in this environment**. Use 5–10 clinics first as described in the extension README.
