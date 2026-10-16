import stat
import subprocess
from pathlib import Path

import pytest

from src.outreach.secure_artifacts import ensure_private_directory, write_private_bytes


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_private_artifact_directory_and_file_modes(tmp_path):
    output = ensure_private_directory(tmp_path / "artifacts" / "outreach_phase12_test")
    artifact = write_private_bytes(output / "report.json", b"{}")

    assert _mode(output) == 0o700
    assert _mode(artifact) == 0o600


def test_private_artifact_writer_refuses_to_overwrite(tmp_path):
    output = ensure_private_directory(tmp_path / "outreach_phase12_test")
    artifact = write_private_bytes(output / "phase2_previews.csv", b"original")

    with pytest.raises(FileExistsError):
        write_private_bytes(artifact, b"replacement")

    assert artifact.read_bytes() == b"original"


def test_phase12_artifacts_are_git_ignored():
    root = Path(__file__).resolve().parents[1]
    candidate = "artifacts/outreach_phase12_ignore_test/phase2_previews.csv"
    checked = subprocess.run(
        ["git", "check-ignore", "--quiet", candidate], cwd=root, check=False,
    )
    assert checked.returncode == 0
