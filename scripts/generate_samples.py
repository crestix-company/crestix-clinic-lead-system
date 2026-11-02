"""Compatibility entry point; implementation lives under scripts/maintenance/generate_samples.py."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "maintenance/generate_samples.py"), run_name="__main__")
