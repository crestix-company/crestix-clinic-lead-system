"""Compatibility re-export for protected MHLW dry-run tools."""
from src.research.safe_web import SafeFetcher, WebError, crawl

__all__ = ["SafeFetcher", "WebError", "crawl"]
