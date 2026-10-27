import hashlib
from pathlib import Path
import tempfile

import pytest
from filelock import FileLock


def test_mac_runtime_accepts_requested_strict_port(monkeypatch, tmp_path):
    from scripts import launch_v2

    calls = []
    monkeypatch.setattr(launch_v2, "load_runtime_env", lambda root: True)
    monkeypatch.setattr(launch_v2.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(launch_v2, "choose_port", lambda preferred: calls.append(preferred) or preferred)
    monkeypatch.setattr(launch_v2.subprocess, "call", lambda command, cwd: 0)
    monkeypatch.setattr(launch_v2.sys, "argv", ["launch_v2.py", "--port", "8502", "--strict-port"])
    assert launch_v2.main() == 0
    assert calls == [8502]


def test_mac_wrapper_separates_normal_and_preview_ports():
    source = Path("scripts/launch_v2_mac.sh").read_text(encoding="utf-8")
    assert 'DEFAULT_PORT=8501' in source
    assert '[ "$MODE" = "preview" ] && DEFAULT_PORT=8502' in source
    assert 'launch_v2_mac_reader.py" --mode "$MODE"' in source


def test_second_streamlit_start_is_rejected_before_port_or_process(monkeypatch, tmp_path):
    from scripts import launch_v2

    root = Path(launch_v2.__file__).resolve().parents[1]
    monkeypatch.setattr(launch_v2.tempfile, "gettempdir", lambda: str(tmp_path))
    name = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    lock = FileLock(str(Path(tempfile.gettempdir()) / f"clinic-lead-runtime-{name}.lock"))
    monkeypatch.setattr(launch_v2, "load_runtime_env", lambda _root: True)
    monkeypatch.setattr(launch_v2, "choose_port", lambda _preferred: pytest.fail("must not choose a second port"))
    monkeypatch.setattr(launch_v2.sys, "argv", ["launch_v2.py"])
    with lock:
        with pytest.raises(RuntimeError, match="二重起動"):
            launch_v2.main()
