"""Fail-closed shadow adapter for the frozen Phase2.2 artifact.

This module is experimental-only and is not imported by rank_hp(), app_v2.py,
or any production scoring entry point.
"""

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from src.enrichment.hp_analysis import Page
from src.scoring.hp_rank_html_features import extract_html_features
from src.scoring.hp_rank_phase22 import (
    CACHE_BOOL_FEATURES,
    CACHE_NUM_FEATURES,
    HTML_BOOL_FEATURES,
    HTML_NUM_FEATURES,
    MODEL_VERSION,
    RANKS,
    feature_vector,
    phase21_baseline_rank,
)
from src.scoring.hp_rank_phase22_artifact import load_artifact
from src.scoring.research_scoring import hp_rank_from_score, hp_rank_score
from src.utils.config import read_config


PHASE21_CONFIG_VERSION = "hp-content-2-candidate"
YEAR_RE = re.compile(r"(19|20)\d{2}")
COPYRIGHT_RE = re.compile(r"©|copyright|all rights reserved", re.I)


class ShadowValidationError(ValueError):
    """Input is unsafe to score; callers must emit REVIEW without a rank."""


@dataclass(frozen=True)
class ShadowModel:
    model: object
    artifact_sha: str


def load_model(artifact_path):
    path = Path(artifact_path)
    model = load_artifact(path)
    return ShadowModel(model=model, artifact_sha=hashlib.sha256(path.read_bytes()).hexdigest())


def _require_bool_map(values, names, section):
    if not isinstance(values, dict):
        raise ShadowValidationError(f"{section} must be an object")
    for name in names:
        if name not in values:
            raise ShadowValidationError(f"missing {section} feature: {name}")
        if type(values[name]) is not bool:
            raise ShadowValidationError(f"invalid {section} boolean: {name}")


def _require_numeric_map(values, names, section):
    if not isinstance(values, dict):
        raise ShadowValidationError(f"{section} must be an object")
    for name in names:
        if name not in values:
            raise ShadowValidationError(f"missing {section} feature: {name}")
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ShadowValidationError(f"invalid {section} numeric: {name}")


def _cache_features(html, html_features, url):
    page = Page(url, html)
    soup = BeautifulSoup(html, "html.parser")
    text = page.main_text or ""
    years = [int(match.group(0)) for match in YEAR_RE.finditer(text)]
    max_year = max(years) if years else None
    headings = " ".join(node.get_text(" ", strip=True) for node in soup.select("h1,h2,h3,h4,h5,h6"))
    return {
        "total_text_len": len(text),
        "home_text_len": len(text),
        "page_count": 1,
        "thin_single_page": len(text) < 800,
        "has_copyright_notice": bool(COPYRIGHT_RE.search(text)),
        "copyright_year_recent": bool(max_year and max_year >= 2024),
        "copyright_year_old": bool(max_year and max_year <= 2019),
        "video_signal": bool(html_features["video_present"]),
        "heading_count_proxy": len([part for part in headings.split(" ") if part.strip()]),
    }


def build_features(candidate, html, phase21_config):
    """Build the frozen model row from fetched HTML and machine-safe metadata."""
    if not isinstance(html, str) or not html.strip():
        raise ShadowValidationError("missing HTML")
    if phase21_config.get("version") != PHASE21_CONFIG_VERSION:
        raise ShadowValidationError("Phase2.1 config version mismatch")
    base = candidate.get("features")
    _require_bool_map(base, tuple(phase21_config["weights"]), "base")
    url = candidate.get("use_url") or candidate.get("hp_url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ShadowValidationError("missing or invalid URL")
    try:
        html_features = extract_html_features(url, html)
        score, _ = hp_rank_score(base, phase21_config["weights"])
        p1_rank = hp_rank_from_score(score, phase21_config["thresholds"])
        cache_features = _cache_features(html, html_features, url)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ShadowValidationError(f"feature generation failed: {exc}") from exc
    return {
        "p1_rank": p1_rank,
        "p1_score": score,
        "candidate_score": score,
        "features": dict(base),
        "html_features": html_features,
        "new_features": cache_features,
    }


def _validate_model_row(row, base_feature_names):
    _require_bool_map(row.get("features"), base_feature_names, "base")
    _require_bool_map(row.get("html_features"), HTML_BOOL_FEATURES, "html")
    _require_numeric_map(row.get("html_features"), HTML_NUM_FEATURES, "html")
    _require_bool_map(row.get("new_features"), CACHE_BOOL_FEATURES, "cache")
    _require_numeric_map(row.get("new_features"), CACHE_NUM_FEATURES, "cache")
    for name in ("p1_score", "candidate_score"):
        value = row.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ShadowValidationError(f"invalid score: {name}")
    if row.get("p1_rank") not in RANKS:
        raise ShadowValidationError("unknown Phase2.1 rank")
    vector = feature_vector(row, base_feature_names)
    if any(not math.isfinite(value) for value in vector):
        raise ShadowValidationError("model input contains NaN or infinity")


def predict(model, row):
    _validate_model_row(row, model.base_feature_names)
    predicted = model.predict(row)
    if predicted not in RANKS:
        raise ShadowValidationError("model returned unknown rank")
    return predicted


def predict_with_metadata(shadow_model, row):
    """Return ranks and probabilities, or raise without inventing a fallback."""
    model = shadow_model.model
    _validate_model_row(row, model.base_feature_names)
    vector = feature_vector(row, model.base_feature_names)
    ab_probability = model.ab_model.probability(vector)
    a_probability = model.a_model.probability(vector)
    d_probability = model.d_model.probability(vector)
    if any(not math.isfinite(value) for value in (ab_probability, a_probability, d_probability)):
        raise ShadowValidationError("model probability contains NaN or infinity")
    phase21_rank = phase21_baseline_rank(row)
    phase22_rank = predict(model, row)
    if phase22_rank == "A":
        score = ab_probability * a_probability
    elif phase22_rank == "B":
        score = ab_probability * (1.0 - a_probability)
    elif phase22_rank == "C":
        score = (1.0 - ab_probability) * (1.0 - d_probability)
    else:
        score = (1.0 - ab_probability) * d_probability
    return {
        "phase21_rank": phase21_rank,
        "phase21_score": row["p1_score"],
        "phase22_rank": phase22_rank,
        "phase22_score": score,
        "model_version": MODEL_VERSION,
        "artifact_sha": shadow_model.artifact_sha,
        "prediction_status": "OK",
    }


def aggregate_transitions(predictions):
    valid = [row for row in predictions if row.get("prediction_status") == "OK"]
    transitions = {source: {target: 0 for target in RANKS} for source in RANKS}
    for row in valid:
        source = row.get("phase21_rank")
        target = row.get("phase22_rank")
        if source not in RANKS or target not in RANKS:
            raise ShadowValidationError("transition contains unknown rank")
        transitions[source][target] += 1
    phase21_distribution = Counter(row["phase21_rank"] for row in valid)
    phase22_distribution = Counter(row["phase22_rank"] for row in valid)
    denominator = len(valid) or 1
    share_delta = {
        rank: ((phase22_distribution[rank] - phase21_distribution[rank]) / denominator) * 100.0
        for rank in RANKS
    }
    changed = sum(count for source in RANKS for target, count in transitions[source].items() if source != target)
    return {
        "rows": len(predictions),
        "scored": len(valid),
        "review": len(predictions) - len(valid),
        "changed": changed,
        "unchanged": sum(transitions[rank][rank] for rank in RANKS),
        "changed_rate": changed / denominator,
        "phase21_distribution": {rank: phase21_distribution[rank] for rank in RANKS},
        "phase22_distribution": {rank: phase22_distribution[rank] for rank in RANKS},
        "distribution_share_delta_percentage_points": share_delta,
        "max_absolute_share_delta_percentage_points": max((abs(value) for value in share_delta.values()), default=0.0),
        "transition_matrix": transitions,
    }


def load_phase21_config(path):
    config = read_config(path)
    if config.get("version") != PHASE21_CONFIG_VERSION:
        raise ShadowValidationError("Phase2.1 config version mismatch")
    return config
