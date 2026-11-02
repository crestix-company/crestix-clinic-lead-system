"""Stable paths rooted at the repository checkout."""
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def repo_path(*parts):
    """Return a path under the repository without depending on the current directory."""
    return REPOSITORY_ROOT.joinpath(*parts)
