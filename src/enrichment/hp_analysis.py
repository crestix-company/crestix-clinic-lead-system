"""Compatibility re-export for protected MHLW dry-run tools."""
from src.research.hp_analysis import (
    NON_OFFICIAL, Page, canonical_page, host, identity, is_official_candidate, page_soup,
)

__all__ = ["NON_OFFICIAL", "Page", "canonical_page", "host", "identity", "is_official_candidate", "page_soup"]
