"""Readiness checks for the runner's stand; no Docker or external network."""

import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from urllib.error import HTTPError

import pytest


STAND_DIR = Path(__file__).resolve().parents[4] / "benchmark" / "catalogue"


@pytest.fixture
def stand():
    spec = importlib.util.spec_from_file_location("catalogue_readiness", STAND_DIR / "check_ready.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ready_health():
    return {"health": [
        {"service": "catalogue", "status": "OK"},
        {"service": "catalogue-db", "status": "OK"},
    ]}


def clock(monkeypatch, stand):
    now = [0.0]
    monkeypatch.setattr(stand, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ))
    return now


def test_readiness_requires_healthy_api_database_and_products(stand, monkeypatch):
    calls = []

    def read_json(url, timeout):
        calls.append(url)
        return ready_health() if url.endswith("/health") else [{"id": "sock-1"}]

    monkeypatch.setattr(stand, "_read_json", read_json)
    result = stand.wait_until_ready("http://127.0.0.1:9911", timeout_seconds=3)
    assert result["status"] == "ready"
    assert result["product_count"] == 1
    assert calls == ["http://127.0.0.1:9911/health", "http://127.0.0.1:9911/catalogue"]


@pytest.mark.parametrize("health", [
    {"health": [{"service": "catalogue", "status": "OK"}]},
    {"health": [{"service": "catalogue", "status": "OK"}, {"service": "catalogue-db", "status": "err"}]},
    {"health": []}, {}, [], {"health": [None]},
])
def test_http_200_without_healthy_database_is_not_ready(stand, monkeypatch, health):
    now = clock(monkeypatch, stand)
    monkeypatch.setattr(stand, "_read_json", lambda url, timeout: health)
    with pytest.raises(TimeoutError, match="health"):
        stand.wait_until_ready("http://catalogue:8080", timeout_seconds=2)
    assert now[0] == 2


@pytest.mark.parametrize("products", [[], {}, None, [None], [{"id": ""}]])
def test_empty_or_invalid_seed_data_is_not_ready(stand, monkeypatch, products):
    clock(monkeypatch, stand)
    monkeypatch.setattr(stand, "_read_json", lambda url, timeout: ready_health() if url.endswith("/health") else products)
    with pytest.raises(TimeoutError, match="catalogue"):
        stand.wait_until_ready("http://catalogue:8080", timeout_seconds=2)


def test_transient_connection_failure_is_retried(stand, monkeypatch):
    clock(monkeypatch, stand)
    attempts = []

    def read_json(url, timeout):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("connection refused")
        return ready_health() if url.endswith("/health") else [{"id": "sock-1"}]

    monkeypatch.setattr(stand, "_read_json", read_json)
    assert stand.wait_until_ready("http://catalogue:8080", timeout_seconds=3)["status"] == "ready"
    assert len(attempts) == 3


def test_total_deadline_limits_each_request(stand, monkeypatch):
    now = clock(monkeypatch, stand)
    budgets = []

    def read_json(url, timeout):
        budgets.append(timeout)
        now[0] += timeout
        return ready_health()

    monkeypatch.setattr(stand, "_read_json", read_json)
    with pytest.raises(TimeoutError):
        stand.wait_until_ready("http://catalogue:8080", timeout_seconds=0.25)
    assert budgets == [0.25]
    assert now[0] == 0.25


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected_before_network(stand, monkeypatch, timeout):
    def unexpected(*args, **kwargs):
        pytest.fail("A network request must not be attempted")

    monkeypatch.setattr(stand, "_read_json", unexpected)
    with pytest.raises(ValueError, match="timeout"):
        stand.wait_until_ready("http://catalogue:8080", timeout_seconds=timeout)


def test_cli_failure_is_machine_readable_and_nonzero(stand, monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise TimeoutError("catalogue-db is not healthy")

    monkeypatch.setattr(stand, "wait_until_ready", unavailable)
    assert stand.main([]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "catalogue-db" in payload["message"]


def test_http_probe_ignores_proxy_and_refuses_redirects(stand, monkeypatch):
    visited = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/trap")
                self.end_headers()
            else:
                body = json.dumps(ready_health()).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        assert stand._read_json(base_url + "/health", timeout=2) == ready_health()
        with pytest.raises(HTTPError) as error:
            stand._read_json(base_url + "/redirect", timeout=2)
        assert error.value.code == 302
        assert visited == ["/health", "/redirect"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_saved_contract_has_the_four_real_operations():
    contract = json.loads((STAND_DIR / "catalogue.swagger.json").read_text(encoding="utf-8"))
    operations = {(method, path) for path, item in contract["paths"].items() for method in item}
    assert contract["swagger"] == "2.0"
    assert operations == {("get", "/catalogue"), ("get", "/catalogue/{id}"), ("get", "/catalogue/size"), ("get", "/tags")}
