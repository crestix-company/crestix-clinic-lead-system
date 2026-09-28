"""HP Rank Phase2.2 frozen experimental candidate.

This module is deliberately disconnected from the production scoring path.  It
provides a small, dependency-free hierarchical logistic model for experiments
on already-reviewed Ground Truth rows.  A/B vs C/D is learned first, followed
by A vs B or C vs D inside the predicted group.

The feature set, weights learned by ``fit_phase22``, and decision thresholds
are frozen after the Phase2.2 experiment.  Do not tune them again on the same
300 Ground Truth rows.  Any production decision requires fresh external
validation data.
"""

from dataclasses import dataclass
from math import exp, log1p


MODEL_VERSION = "hp-content-2.2-hierarchical-logistic-experimental"
CANDIDATE_STATUS = "experimental-frozen"
RANKS = ("A", "B", "C", "D")
# Frozen experimental thresholds.  Changes require a new model version and
# fresh validation; do not optimize these against the existing 300-row GT.
AB_THRESHOLD = 0.60
A_THRESHOLD = 0.50
D_ADD_THRESHOLD = 0.95

HTML_BOOL_FEATURES = (
    "viewport_meta", "responsive_indicator", "table_layout_indicator",
    "video_present", "sns_link_present", "web_reservation_cta", "contact_cta",
    "phone_cta", "doctor_photo_candidate", "clinic_photo_many",
    "copyright_year_recent", "copyright_year_old", "legacy_html_indicator",
    "https_status", "mobile_usability_proxy",
)
HTML_NUM_FEATURES = (
    "html_byte_size", "visible_text_length", "image_count", "internal_link_count",
    "iframe_count", "table_count", "inline_style_ratio",
)
CACHE_BOOL_FEATURES = (
    "thin_single_page", "has_copyright_notice", "copyright_year_recent",
    "copyright_year_old", "video_signal",
)
CACHE_NUM_FEATURES = (
    "total_text_len", "home_text_len", "page_count", "heading_count_proxy",
)


def phase21_baseline_rank(row):
    """Apply the Phase2.1 D detector only to a Phase1 C prediction."""
    rank = row["p1_rank"]
    html = row.get("html_features")
    if rank != "C" or not html:
        return rank
    votes = sum((
        not html["clinic_photo_many"],
        not html["doctor_photo_candidate"],
        html["html_byte_size"] < 60000,
        not html["phone_cta"],
    ))
    return "D" if votes >= 3 else "C"


def feature_vector(row, base_feature_names):
    """Create a stable numeric vector without using human or split fields."""
    base = row.get("features") or {}
    html = row.get("html_features") or {}
    cached = row.get("new_features") or {}
    values = [float(bool(base.get(name))) for name in base_feature_names]
    values.extend(float(bool(html.get(name))) for name in HTML_BOOL_FEATURES)
    values.extend(log1p(max(0.0, float(html.get(name, 0) or 0))) for name in HTML_NUM_FEATURES)
    values.extend(float(bool(cached.get(name))) for name in CACHE_BOOL_FEATURES)
    values.extend(log1p(max(0.0, float(cached.get(name, 0) or 0))) for name in CACHE_NUM_FEATURES)
    values.extend((float(row.get("p1_score", 0) or 0), float(row.get("candidate_score", 0) or 0)))
    values.append(float(not bool(row.get("html_features"))))
    return values


@dataclass(frozen=True)
class BinaryLogisticModel:
    weights: tuple
    means: tuple
    scales: tuple

    def probability(self, values):
        score = self.weights[0]
        for weight, value, mean, scale in zip(self.weights[1:], values, self.means, self.scales):
            score += weight * ((value - mean) / scale)
        score = max(-30.0, min(30.0, score))
        return 1.0 / (1.0 + exp(-score))


def fit_binary(vectors, targets, l2=1.0, steps=2000, learning_rate=0.05):
    """Fit class-balanced binary logistic regression deterministically."""
    if not vectors or len(set(targets)) != 2:
        raise ValueError("binary training requires both classes")
    width = len(vectors[0])
    means = [sum(row[j] for row in vectors) / len(vectors) for j in range(width)]
    scales = []
    for j, mean in enumerate(means):
        variance = sum((row[j] - mean) ** 2 for row in vectors) / len(vectors)
        scale = variance ** 0.5
        scales.append(scale if scale >= 1e-8 else 1.0)
    normalized = [[1.0] + [(v - means[j]) / scales[j] for j, v in enumerate(row)] for row in vectors]
    positives = sum(targets)
    negatives = len(targets) - positives
    sample_weights = [len(targets) / (2 * (positives if target else negatives)) for target in targets]
    weights = [0.0] * (width + 1)
    for _ in range(steps):
        gradients = [0.0] * len(weights)
        for row, target, sample_weight in zip(normalized, targets, sample_weights):
            score = max(-30.0, min(30.0, sum(w * x for w, x in zip(weights, row))))
            error = ((1.0 / (1.0 + exp(-score))) - target) * sample_weight
            for j, value in enumerate(row):
                gradients[j] += error * value
        for j in range(1, len(weights)):
            gradients[j] += l2 * weights[j]
        for j in range(len(weights)):
            weights[j] -= learning_rate * gradients[j] / len(vectors)
    return BinaryLogisticModel(tuple(weights), tuple(means), tuple(scales))


@dataclass(frozen=True)
class Phase22Model:
    base_feature_names: tuple
    ab_model: BinaryLogisticModel
    a_model: BinaryLogisticModel
    d_model: BinaryLogisticModel
    version: str = MODEL_VERSION

    def predict(self, row):
        vector = feature_vector(row, self.base_feature_names)
        # Preserve every Phase2.1 D decision.  The learned D model may only add
        # a D at high confidence; it cannot erase the existing detector signal.
        if phase21_baseline_rank(row) == "D":
            return "D"
        if self.ab_model.probability(vector) >= AB_THRESHOLD:
            return "A" if self.a_model.probability(vector) >= A_THRESHOLD else "B"
        return "D" if self.d_model.probability(vector) >= D_ADD_THRESHOLD else "C"


def fit_phase22(rows):
    """Fit the experimental hierarchy from labeled training rows only."""
    if not rows:
        raise ValueError("training rows are required")
    base_feature_names = tuple((rows[0].get("features") or {}).keys())
    vectors = [feature_vector(row, base_feature_names) for row in rows]
    labels = [row["human_rank"] for row in rows]
    ab_targets = [int(label in ("A", "B")) for label in labels]
    ab_indices = [i for i, label in enumerate(labels) if label in ("A", "B")]
    cd_indices = [i for i, label in enumerate(labels) if label in ("C", "D")]
    return Phase22Model(
        base_feature_names=base_feature_names,
        ab_model=fit_binary(vectors, ab_targets),
        a_model=fit_binary([vectors[i] for i in ab_indices], [int(labels[i] == "A") for i in ab_indices]),
        d_model=fit_binary([vectors[i] for i in cd_indices], [int(labels[i] == "D") for i in cd_indices]),
    )
