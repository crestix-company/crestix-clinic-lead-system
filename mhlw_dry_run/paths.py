"""Portable paths shared by the reproducible MHLW build phases."""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
SOURCE_DIR = Path(os.environ.get("MHLW_SOURCE_DIR", Path.home() / "Downloads"))


def source(filename):
    return SOURCE_DIR / filename
