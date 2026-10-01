"""Trusted isolation suite. Runs inside the same network policy as the
generated tests, BEFORE them. Every test here must pass, otherwise the runner
reports an infrastructure error and never executes the generated tests.

Only this pinned setup is allowed:
- the API is reachable at RUNNER_BASE_URL and resolves to RUNNER_API_IP;
- everything else (internet, host/gateway, database and every address in
  RUNNER_UNREACHABLE, IPv6, external DNS) must be unreachable;
- raw sockets and privileged capabilities must be denied, and the same
  restrictions must hold for child processes.

This module is part of the image and cannot be replaced by generated tests.
"""

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

PROBE_PORTS = (
    1,     # tcpmux: almost certainly closed
    22,    # ssh on the host/gateway
    80,    # http on the host/gateway
    3306,  # mysql
    5432,  # postgres
    8080,  # common app port
)

# This suite is meaningful only inside the runner image, where the host injects
# the pinned network policy as environment variables and provides the trusted
# fixtures through the runner_fixtures plugin. Outside that context (host-side
# unit runs and editor collection) it is skipped as a whole: collection of it
# must not fail when there is no RUNNER_BASE_URL.
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUNNER_BASE_URL"),
    reason="network policy suite runs inside the runner image (RUNNER_BASE_URL set)",
)


def _env(name: str) -> list[str]:
    value = os.environ.get(name, "")
    return [item for item in value.split(",") if item]


def _host_of_base_url() -> str:
    return urlsplit(os.environ["RUNNER_BASE_URL"]).hostname or ""


def _connect_blocked(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=1.0)
    except OSError:
        return False
    return True


def _assert_unreachable(host: str, ports: tuple[int, ...]):
    for port in ports:
        assert not _connect_blocked(host, port), (
            f"{host}:{port} unexpectedly reachable")


# ---------------------------------------------------------------- API access

def test_catalogue_health_reachable(api_client, base_url):
    response = api_client.get(base_url.rstrip("/") + "/health", timeout=5)
    assert response.status_code < 500, response.text


def test_base_url_pinned_to_single_api_address():
    ip = socket.gethostbyname(_host_of_base_url())
    assert ip == os.environ["RUNNER_API_IP"]


# ------------------------------------------------------- nothing else works

def test_external_internet_unreachable():
    for host in ("1.1.1.1", "8.8.8.8"):
        _assert_unreachable(host, (53, 443))


def test_host_gateway_unreachable():
    gateway = os.environ.get("RUNNER_GATEWAY_IP", "")
    assert gateway, "RUNNER_GATEWAY_IP is missing"
    _assert_unreachable(gateway, PROBE_PORTS)


def test_database_and_stray_containers_unreachable():
    addresses = _env("RUNNER_UNREACHABLE")
    assert addresses, "RUNNER_UNREACHABLE is empty"
    for address in addresses:
        _assert_unreachable(address, PROBE_PORTS)


def test_external_dns_not_resolved():
    with pytest.raises(OSError):
        socket.getaddrinfo("example.com", 443)


def test_ipv6_unreachable():
    assert not _connect_blocked("2606:4700:4700::1111", 443), "global IPv6 reachable"


def test_raw_sockets_denied():
    with pytest.raises(OSError):
        socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)


# ------------------------------------------------- child processes inherit

def test_child_process_shares_restrictions():
    script = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=2)\n"
        "    raise SystemExit(0)\n"
        "except OSError:\n"
        "    raise SystemExit(7)\n"
    )
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, timeout=10)
    assert proc.returncode == 7, proc.stderr


# ------------------------------------------------------------- client control

def test_client_does_not_follow_redirects(api_client):
    # A redirect towards an external host must never be followed by default.
    assert api_client.max_redirects == 0
    assert api_client.trust_env is False


def test_redirect_to_external_host_is_not_followed(api_client):
    # Local loopback server that answers 302 to the external internet. The
    # policy client defaults to allow_redirects=False, so the redirect is
    # returned to the caller and no external connection is attempted.
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://1.1.1.1:443/leak")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = api_client.get(
            f"http://127.0.0.1:{server.server_port}/", timeout=5)
        assert response.status_code == 302
        assert response.headers["Location"] == "http://1.1.1.1:443/leak"
        # Even a caller that explicitly asks to follow redirects must fail
        # locally (max_redirects=0) instead of connecting to the target.
        raised = None
        try:
            api_client.get(
                f"http://127.0.0.1:{server.server_port}/",
                timeout=5, allow_redirects=True)
        except Exception as exc:  # too many redirects / connection error
            raised = exc
        assert raised is not None, "client followed the external redirect"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_no_proxy_environment_leaks(api_client):
    # trust_env=False means no proxy from environment; also assert the surprise
    # proxy variables are not present at all.
    assert "HTTP_PROXY" not in os.environ
    assert "http_proxy" not in os.environ
    assert api_client.trust_env is False


# ------------------------------------------------------------------ sanity

def test_policy_env_is_self_consistent():
    payload = {
        "base_url": os.environ["RUNNER_BASE_URL"],
        "api_ip": os.environ["RUNNER_API_IP"],
        "gateway": os.environ.get("RUNNER_GATEWAY_IP", ""),
        "unreachable": _env("RUNNER_UNREACHABLE"),
    }
    assert payload["base_url"]
    assert payload["api_ip"]
    assert payload["gateway"]
    assert payload["unreachable"]
    print(json.dumps(payload))
