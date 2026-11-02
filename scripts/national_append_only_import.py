"""Compatibility entry point; implementation lives under scripts/national_append_only_import.py."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "national_append_only_import.py"), run_name="__main__")
