# HP Rank Phase2.2 frozen experiment

## Status

- Model version: `hp-content-2.2-hierarchical-logistic-experimental`
- Candidate status: `experimental-frozen`
- Production scoring path: not connected
- Production readiness: not established
- Further tuning on the same 300 Ground Truth rows: stopped

The candidate is frozen at the result below. Its feature set, weights, and
thresholds must not be tuned further against this dataset. A production
adoption decision requires fresh external validation.

## Dataset and evaluation

The experiment used only the 300 existing human-reviewed Ground Truth rows:

| Human rank | Rows |
| --- | ---: |
| A | 7 |
| B | 83 |
| C | 170 |
| D | 40 |
| Total | 300 |

These rows were already used or inspected during Phase1 through Phase2.1 and
were also used to design Phase2.2. They are not a fresh holdout. No row from
the separately prepared 300-row Human Review Package was used.

The comparison used fixed stratified 5-fold cross-validation. Each Phase2.2
prediction is out-of-fold: the model for a row was fitted on the other four
folds. This reduces direct train/evaluation overlap inside a fold, but it does
not make the overall dataset fresh because candidate design decisions were
made using the same 300-row corpus.

## Phase2.1 baseline

| Metric | Result |
| --- | ---: |
| Macro F1 | 37.50% |
| A precision / recall | 9.38% / 42.86% |
| B precision / recall | 40.35% / 27.71% |
| C precision / recall | 61.88% / 58.24% |
| D precision / recall | 37.25% / 47.50% |
| A/B vs C/D accuracy | 70.33% |
| Severe errors | 17 |

Confusion matrix (rows are human rank; columns are predicted rank):

| Human \ Predicted | A | B | C | D |
| --- | ---: | ---: | ---: | ---: |
| A | 3 | 2 | 1 | 1 |
| B | 17 | 23 | 40 | 3 |
| C | 11 | 32 | 99 | 28 |
| D | 1 | 0 | 20 | 19 |

## Phase2.2 candidate

| Metric | Result |
| --- | ---: |
| Macro F1 | 45.98% |
| A precision / recall | 25.00% / 42.86% |
| B precision / recall | 48.39% / 54.22% |
| C precision / recall | 66.19% / 54.12% |
| D precision / recall | 35.71% / 50.00% |
| A/B vs C/D accuracy | 71.67% |
| Severe errors | 12 |

Confusion matrix (rows are human rank; columns are predicted rank):

| Human \ Predicted | A | B | C | D |
| --- | ---: | ---: | ---: | ---: |
| A | 3 | 3 | 0 | 1 |
| B | 4 | 45 | 31 | 3 |
| C | 4 | 42 | 92 | 32 |
| D | 1 | 3 | 16 | 20 |

## Interpretation and limitations

Macro F1 increased from 37.50% to 45.98%, A/B vs C/D accuracy increased from
70.33% to 71.67%, and severe errors fell from 17 to 12. A precision and B
precision/recall improved.

C recall regressed from 58.24% to 54.12%. D recall increased from 47.50% to
50.00%, but this is only one additional correct D row (19 to 20 out of 40).
D precision decreased from 37.25% to 35.71%, and D F1 was effectively flat.
The small A support (7) and D support (40) prohibit strong conclusions from
percentages alone.

This result does not demonstrate production accuracy or generalization to
unknown clinics. The candidate remains disconnected from production. The next
step is fresh external validation using newly reviewed Ground Truth or genuine
operational data, followed by a separate production adoption decision.
