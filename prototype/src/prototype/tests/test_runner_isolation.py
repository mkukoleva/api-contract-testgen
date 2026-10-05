"""Real network boundaries, including live forbidden listeners (no LLM).

RUN_RUNNER_DOCKER_TESTS=1 requires the built step5 image. Catalogue checks
additionally require RUN_RUNNER_CATALOGUE_TESTS=1 and the running demo stand.
"""

import json
import os
from pathlib import Path
import subprocess
import textwrap
import time
from uuid import uuid4

import pytest

from prototype.service_tools.runner import docker_runner as runner
from prototype.service_tools.runner.contracts import RunConfig


DOCKER = pytest.mark.skipif(os.environ.get("RUN_RUNNER_DOCKER_TESTS") != "1",
                            reason="requires the built runner Docker image")
CATALOGUE = pytest.mark.skipif(os.environ.get("RUN_RUNNER_CATALOGUE_TESTS") != "1",
                               reason="requires the running Catalogue stand")


def docker(*args, timeout=20, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=check)


# Each helper has two live TCP ports and matching UDP echo sockets. A blocked
# connection is meaningful only after the unrestricted control reaches it.
SERVER = """
import json, socket, socketserver, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        target = parse_qs(urlsplit(self.path).query).get('to')
        self.send_response(302 if target else 200)
        if target: self.send_header('Location', target[0])
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, *args): pass
class Server(ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = 'control'
        self.server_port = self.server_address[1]
def echo(sock):
    while True:
        data, address = sock.recvfrom(1024)
        sock.sendto(data, address)
ports = []
for _ in range(2):
    server = Server(('0.0.0.0', 0), Handler)
    ports.append(server.server_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', server.server_port))
    threading.Thread(target=echo, args=(sock,), daemon=True).start()
print(json.dumps(ports), flush=True)
while True: time.sleep(60)
"""


@DOCKER
def test_firewall_blocks_live_destinations_before_import(tmp_path):
    prefix = "runner-isolation-" + uuid4().hex
    networks, containers = [], []
    image = runner.DEFAULT_IMAGE

    def server(suffix, network):
        name = prefix + suffix
        containers.append(name)
        docker("run", "-d", "--name", name, "--network", network,
               "--cap-drop", "ALL", "--user", "65534:65534", "--read-only",
               "--security-opt", "no-new-privileges:true", "--entrypoint", "python",
               image, "-I", "-B", "-c", SERVER)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            output = docker("logs", name).stdout.strip()
            if output:
                return name, json.loads(output.splitlines()[0])
            time.sleep(0.1)
        pytest.fail(f"helper did not start: {name}")

    def address(name, network):
        info = json.loads(docker("inspect", name).stdout)[0]
        return info["NetworkSettings"]["Networks"][network]["IPAddress"]

    try:
        for suffix in ("-api-net", "-db-net"):
            name = prefix + suffix
            docker("network", "create", "--internal", name)
            networks.append(name)
        api, api_ports = server("-api", networks[0])
        peer, peer_ports = server("-peer", networks[0])
        database, db_ports = server("-db", networks[1])
        host, host_ports = server("-host-listener", "host")
        api_ip = address(api, networks[0])
        gateway = json.loads(docker("network", "inspect", networks[0]).stdout)[0]["IPAM"]["Config"][0]["Gateway"]
        forbidden = [(api_ip, api_ports[1]), (address(peer, networks[0]), peer_ports[0]),
                     (address(database, networks[1]), db_ports[0]), (gateway, host_ports[0])]
        endpoints = [(api_ip, api_ports[0]), *forbidden]
        control = f"""
import socket
for host, port in {endpoints!r}:
    with socket.create_connection((host, port), timeout=2): pass
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(2); s.sendto(b'control', (host, port))
        assert s.recv(100) == b'control'
print('all listeners reachable')
"""
        # This control intentionally has access to both demo networks. The
        # tested runner will have only the API network and its own firewall.
        control_name = prefix + "-control"
        containers.append(control_name)
        baseline = docker("run", "--rm", "--name", control_name,
                          "--network", networks[0], "--network", networks[1],
                          "--cap-drop", "ALL", "--entrypoint", "python", image,
                          "-I", "-c", control)
        assert "all listeners reachable" in baseline.stdout

        suite = tmp_path / "suite"; suite.mkdir()
        output = tmp_path / "results"; output.mkdir(); output.chmod(0o777)
        code = f"""
import os, socket, subprocess, sys
from pathlib import Path
import pytest, requests
FORBIDDEN = {forbidden!r}
# These assertions execute during collect-only, before any test body.
assert os.getuid() == 65534
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
assert all(int(status[key].strip(), 16) == 0 for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))
assert status['NoNewPrivs'].strip() == '1'
for host, port in FORBIDDEN:
    with pytest.raises(OSError): socket.create_connection((host, port), timeout=0.3)
for host, port in {endpoints!r}:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(0.3)
        with pytest.raises(OSError):
            s.connect((host, port)); s.send(b'blocked'); s.recv(100)
with pytest.raises(OSError): socket.getaddrinfo('example.com', 443)
with pytest.raises(OSError): socket.create_connection(('2606:4700:4700::1111', 443), timeout=0.3)
with pytest.raises(OSError): socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
def test_service_and_redirect(base_url, api_client):
    assert api_client.get(base_url + '/health').status_code == 200
    url = base_url + '/redirect?to=http://{gateway}:{host_ports[0]}/'
    assert api_client.get(url).status_code == 302
    with requests.Session() as client:
        client.trust_env = False
        with pytest.raises(requests.RequestException): client.get(url, timeout=0.3, allow_redirects=True)
def test_cannot_remove_policy():
    for tool in ('iptables', 'ip6tables'):
        p = subprocess.run([tool, '-F'], capture_output=True, timeout=3)
        assert p.returncode != 0
    with pytest.raises(OSError): os.setuid(0)
    for host, port in FORBIDDEN:
        with pytest.raises(OSError): socket.create_connection((host, port), timeout=0.3)
def test_child_process():
    script = "import socket; socket.create_connection(({gateway!r}, {host_ports[0]}), timeout=0.3)"
    p = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=3)
    assert p.returncode != 0
"""
        (suite / "test_live_policy.py").write_text(textwrap.dedent(code), encoding="utf-8")
        name = prefix + "-runner"; containers.append(name)
        # Include a peer in the same network to prove the firewall itself,
        # beyond the launcher's separate single-API topology precheck.
        args = runner._docker_create_args(
            config=RunConfig(suite, output, base_url=f"http://api:{api_ports[0]}", timeout_seconds=60),
            image=image, name=name, network=networks[0], target_ip=api_ip,
            service_hostname="api", gateway=gateway,
            unreachable=[gateway, forbidden[1][0], forbidden[2][0]], tests_dir=suite, run_dir=output)
        docker(*args[1:])
        executed = docker("start", "--attach", name, timeout=70, check=False)
        assert executed.returncode == 0, executed.stdout + executed.stderr
        assert runner.read_policy_result(output / "policy_events.jsonl")[0]
        result = runner.read_result(output / "events.jsonl", 0)
        assert result.to_dict()["summary"]["passed"] == 3, result.to_dict()
    finally:
        for name in reversed(containers):
            docker("rm", "--force", name, check=False)
        for name in reversed(networks):
            docker("network", "rm", name)


@CATALOGUE
def test_catalogue_service_mode(tmp_path):
    suite = tmp_path / "suite"; suite.mkdir()
    (suite / "test_catalogue_api.py").write_text(
        "def test_catalogue(base_url, api_client):\n"
        "    response = api_client.get(base_url + '/catalogue')\n"
        "    assert response.status_code == 200\n"
        "    assert len(response.json()) > 0\n", encoding="utf-8")
    result = runner.run_tests(
        RunConfig(suite, tmp_path / "out", base_url="http://catalogue:8080", timeout_seconds=60),
        network="pytest-runner-catalogue_runner",
        blocked_networks=("pytest-runner-catalogue_database",))
    assert result.status == "completed", result.to_dict()
    assert result.exit_code == 0, result.to_dict()
    assert result.to_dict()["summary"]["passed"] == 1
