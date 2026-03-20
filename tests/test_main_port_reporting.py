import subprocess
import sys
import os
import time


def test_proxy_prints_port_on_startup():
    """Proxy must print PROXY_PORT=<n> to stdout within 5 seconds of startup."""
    env = os.environ.copy()
    env["PORT"] = "0"
    env["LOG_FILE"] = "/tmp/test_proxy_port.jsonl"
    env["API_BASE_URL"] = "http://localhost:9999"  # dummy upstream

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    try:
        port_line = None
        deadline = time.time() + 5
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("PROXY_PORT="):
                port_line = line.strip()
                break

        assert port_line is not None, "Proxy never printed PROXY_PORT="
        port_str = port_line.split("=")[1]
        assert port_str.isdigit(), f"Port is not a number: {port_str!r}"
        port = int(port_str)
        assert 1024 <= port <= 65535, f"Port out of range: {port}"
    finally:
        proc.terminate()
        proc.wait()
