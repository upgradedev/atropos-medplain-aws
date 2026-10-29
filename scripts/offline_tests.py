"""Run pytest with network sockets refused, proving fixture tests need no cloud."""

from __future__ import annotations

import os
import socket
import sys


class OfflineNetwork:
    def pytest_sessionstart(self, session):
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def refuse_internet(sock, address):
            if sock.family not in {socket.AF_UNIX}:
                raise RuntimeError(f"Offline test attempted network access to {address!r}")
            return original_connect(sock, address)

        socket.socket.connect = refuse_internet
        socket.socket.connect_ex = refuse_internet
        self.original_connect = original_connect
        self.original_connect_ex = original_connect_ex

    def pytest_sessionfinish(self, session, exitstatus):
        socket.socket.connect = self.original_connect
        socket.socket.connect_ex = self.original_connect_ex


if __name__ == "__main__":
    os.environ["MEDPLAIN_MODEL_MODE"] = "fixture"
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["OTEL_SDK_DISABLED"] = "true"
    import pytest

    sys.exit(pytest.main(sys.argv[1:] or ["-q"], plugins=[OfflineNetwork()]))
