import socket

import pytest

from scripts.launch_v2 import acquire_windows_instance_guard


def _free_local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_single_instance_guard_rejects_second_owner():
    port = _free_local_port()
    first = acquire_windows_instance_guard(port)
    try:
        with pytest.raises(RuntimeError, match="既に起動中"):
            acquire_windows_instance_guard(port)
    finally:
        first.close()


def test_single_instance_guard_can_be_reacquired_after_close():
    port = _free_local_port()
    first = acquire_windows_instance_guard(port)
    first.close()

    second = acquire_windows_instance_guard(port)
    second.close()
