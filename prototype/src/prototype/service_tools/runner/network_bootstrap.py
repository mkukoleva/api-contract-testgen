"""Trusted image entry point; never imports or reads files from /tests.

Service mode installs a firewall in this container's network namespace, then
execs pytest as nobody with all capability sets cleared. No host firewall is
changed. Offline mode already has --network none and starts unprivileged.
"""

import ipaddress
import os
import subprocess
import sys
from urllib.parse import urlsplit


WORKER = "/opt/runner/pytest_worker.py"


def install_policy(address: str, port: int) -> None:
    target = ipaddress.IPv4Address(address)
    if target.is_loopback or target.is_multicast or target.is_unspecified or not 1 <= port <= 65535:
        raise ValueError("Invalid API destination")
    # Loopback TCP is local to this container (used by temporary test servers).
    # Do not allow all of 127/8: Docker DNS at 127.0.0.11 must remain blocked,
    # including its DNAT'ed destination port. All UDP is denied.
    ipv4 = f"""*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A INPUT -i lo -s 127.0.0.1/32 -d 127.0.0.1/32 -p tcp -j ACCEPT
-A OUTPUT -o lo -s 127.0.0.1/32 -d 127.0.0.1/32 -p tcp -j ACCEPT
-A OUTPUT -d {target}/32 -p tcp --dport {port} -j ACCEPT
-A OUTPUT -j REJECT
COMMIT
"""
    ipv6 = """*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A OUTPUT -j REJECT
COMMIT
"""
    for command, rules in (("iptables-restore", ipv4), ("ip6tables-restore", ipv6)):
        subprocess.run([command, "--wait", "2"], input=rules, text=True,
                       check=True, timeout=5)


def main() -> None:
    args = sys.argv[1:]
    if len(args) not in (3, 5):
        raise ValueError("Invalid worker arguments")
    # Docker client configuration can inject proxies independently of shell
    # environment. The worker must not inherit them in either mode.
    for key in list(os.environ):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            os.environ.pop(key)
    command = [sys.executable, "-I", "-B", WORKER, *args]
    if len(args) == 5:
        if os.geteuid() != 0:
            raise RuntimeError("Service policy bootstrap must start as root")
        url = urlsplit(os.environ["RUNNER_BASE_URL"])
        if url.scheme not in {"http", "https"}:
            raise ValueError("Expected HTTP(S) API")
        install_policy(os.environ["RUNNER_API_IP"], url.port or (443 if url.scheme == "https" else 80))
        command = [
            "/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups",
            "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
            "--no-new-privs", *command,
        ]
    elif os.geteuid() == 0:
        raise RuntimeError("Offline worker must start unprivileged")
    os.chdir("/opt/runner")
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
