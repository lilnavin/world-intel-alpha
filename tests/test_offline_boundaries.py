import socket
import subprocess
import sys

import pytest
from conftest import NetworkBlocked


def test_dns_and_connect_are_blocked():
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("example.org", 443)
    with socket.socket() as connection:
        with pytest.raises(NetworkBlocked):
            connection.connect(("127.0.0.1", 9))


def test_live_model_subprocess_is_blocked():
    with pytest.raises(NetworkBlocked):
        subprocess.run(["codex", "exec", "never send"])


def test_unrelated_codex_path_is_not_a_model_command():
    result = subprocess.run([sys.executable, "-c", "print('codex-tmp')"],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "codex-tmp"
