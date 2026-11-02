"""Compatibility entry point; implementation lives under scripts/import_tokyo.py."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "import_tokyo.py"), run_name="__main__")
