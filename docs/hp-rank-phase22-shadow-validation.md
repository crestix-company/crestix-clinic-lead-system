# HP Rank Phase2.2 bounded Shadow Validation

## Purpose

This is a **Shadow Validation**, not an accuracy evaluation. It compares the
current Production `hp_rank` value (read-only, from `clinics.sqlite3`) against
what the frozen Phase2.2 candidate model (see
[`hp-rank-phase22-experiment.md`](hp-rank-phase22-experiment.md)) would have
produced for the same 300 rows, using pre-extracted features that already
exist for those rows (no HTML re-fetch, no network calls).

**This is not a Ground Truth accuracy check.** There is no human label in this
run. A disagreement between current rank and shadow rank means the two
scoring generations produced different output for the same clinic — it says
nothing about which one is closer to a human reviewer's judgement.
`accuracy` / `precision` / `recall` are not computed here and must not be
inferred from the agreement/disagreement counts below.

## Frozen artifact usage

The run loads the already-frozen artifact
`artifacts/hp_rank/hp-content-2.2-hierarchical-logistic-experimental.json`
through `load_artifact()` / `load_model()` (`src/scoring/hp_rank_phase22_artifact.py`,
`src/scoring/hp_rank_phase22_shadow.py`). No re-fit, no threshold/weight
change, no feature addition/removal/reordering happens in this experiment.
Verified against the frozen commit (`606d7f4`, packaged at `d943caf`):

| Field | Value |
| --- | --- |
| `model_version` | `hp-content-2.2-hierarchical-logistic-experimental` |
| Artifact file SHA-256 | `bb530750a9d22c81a11e89e58eb42dc5de7df6ba14cbed05a88306f6e86b9d68` |
| `training_dataset_fingerprint` | `b8343a8a1a3bafa6724a9efd56cd2f5d8b847c5e20e6f7edceb53eb2ce8a44ff` |
| Tracked artifact file diff vs. `d943caf` | none (clean) |

## Scope and inputs

- Source: the existing 300-row Phase2.1/2.2 dataset (same 300 clinics used to
  design/freeze the candidate; not a fresh holdout — see the interpretation
  caveats in `hp-rank-phase22-experiment.md`). Each row already carries
  pre-extracted `features` / `html_features` / `new_features` from earlier
  work; **no HTML is fetched and no network call is made** in this script.
- `current_rank` is read **read-only** from Production
  (`~/CrestixData/clinic-lead/clinics.sqlite3`, opened with
  `mode=ro` + `PRAGMA query_only=ON`) via `id`/`medical_key`/`hp_rank`.
  Rows are only kept if `hp_rank` is one of `A/B/C/D` and `medical_key` is
  non-empty.
- `shadow_rank` / `shadow_score` come from the frozen Phase2.2 model,
  computed via `predict_with_metadata()`. The model input row is built
  through an explicit allowlist (`p1_rank`, `p1_score`, `candidate_score`,
  `features`, `html_features`, `new_features`) — `human_rank` and `split` are
  never passed to the model or used to select/weight the sample.
- Sampling is deterministic: `deterministic_sample()` sorts by
  `sha256(medical_key)` and is order-independent (see
  `tests/test_hp_rank_phase22_bounded_shadow.py`).

## Reproduced result (2026-09-28)

Re-run against the same 300-row dataset and the current, unmodified frozen
artifact reproduced Codex's numbers byte-for-byte (predictions CSV, review
queue CSV, and summary JSON are all identical to the pre-handoff run):

| Metric | Value |
| --- | ---: |
| eligible | 300 |
| evaluated | 299 |
| skipped | 1 |
| feature_missing | 1 |
| network_calls | 0 |
| source_rows | 300 |
| sample_limit | 500 |

The one skipped row failed model-input validation (safe fail-closed skip via
`ShadowValidationError`/`KeyError`/`TypeError`/`ValueError`/`OverflowError`)
and is marked `REVIEW:FEATURE_MISSING`, not scored with an invented rank.

### Current (Production) rank distribution

| Rank | Count |
| --- | ---: |
| A | 99 |
| B | 200 |
| C | 0 |
| D | 0 |

**Constraint:** the current Production `hp_rank` values sampled here are
entirely A/B. This is a generation/definition difference in the Production
rank source for this particular 300-row set, not a claim that Phase2.2 is
better or worse — see "Interpretation" below.

### Shadow (Phase2.2 candidate) rank distribution

| Rank | Count |
| --- | ---: |
| A | 14 |
| B | 93 |
| C | 141 |
| D | 51 |

### Agreement / disagreement (evaluated = 299)

| Bucket | Count |
| --- | ---: |
| same rank | 55 |
| 1-step difference | 153 |
| 2-step difference | 86 |
| 3-step difference | 5 |
| A→B/B→A, C→D/D→C, etc. (not itemized further) | — |
| AB → CD | 192 |
| CD → AB | 0 |
| A → D | 5 |
| D → A | 0 |

CD → AB and D → A are both 0 only because the current-rank sample contains
zero C/D rows (see distribution above) — there is nothing in the sample for
those transitions to originate from. This is a property of this 300-row
current-rank sample, not a finding about Phase2.2.

## Interpretation and limitations

- Do **not** read "AB → CD: 192" as "Phase2.2 downgraded 192 clinics" in a
  quality sense. It reflects a difference between two rank-generation
  schemes evaluated on the same clinics, where the current side is
  concentrated in A/B by construction of this sample.
- Do **not** treat `same` / `one_step_difference` / etc. as accuracy,
  precision, or recall. There is no Ground Truth label in this comparison.
- This run does not establish, and is not evidence toward, a production
  adoption decision for Phase2.2.

## Production protection

- Production DB access is **read-only** (`sqlite3.connect(..., uri=True)`
  with `mode=ro`, plus `PRAGMA query_only=ON`); only a single `SELECT` is
  issued. No `INSERT`/`UPDATE`/`DELETE`/DDL.
- SHA-256, mtime, `clinics` row count, non-empty-`medical_key` duplicate
  count, and `PRAGMA integrity_check` were confirmed identical before and
  after the run.
- No network calls (`network_calls: 0` in every summary; no HTTP client is
  imported anywhere in this code path).
- Production scoring (`app_v2.py`, `rank_hp()`) does not import any
  `hp_rank_phase22*` module — this experiment is fully disconnected from the
  production scoring path.

## Review queue

Up to 50 rows, deterministically ordered by:

1. `A_D_transition` — current/shadow ranks are exactly `{A, D}`
2. `AB_CD_transition` — current and shadow disagree on the A/B-vs-C/D split
3. `high_confidence_disagreement` — ranks differ and shadow confidence ≥ 0.8
4. `feature_missing` — prediction skipped (safe fail-closed)

within each priority, by descending shadow score, then by `medical_key` for a
stable deterministic order. This run produced a full 50-row queue dominated
by `AB_CD_transition` (matching the large AB→CD count above) and 5
`A_D_transition` rows.

Outputs containing real clinic names / `medical_key` (`bounded_phase22_shadow_predictions.csv`,
`bounded_phase22_review_queue.csv`) are **not committed to git** — they stay
local/untracked. Only the aggregate `bounded_phase22_shadow_summary.json`
(counts only, no per-clinic identifiers) is committed.

## Dataset provenance caveat

The 300-row input dataset used above is the same pre-existing, pre-extracted
Ground Truth corpus referenced in the Phase2.1/2.2 experiment docs. It is not
checked into this repository (it contains human labels, clinic names, and
addresses) and must be sourced from the same experiment lineage that produced
`hp-rank-phase22-experiment.md`. Re-running this script requires that dataset
file plus read-only access to Production; no part of this script regenerates
it from HTML or the network.
