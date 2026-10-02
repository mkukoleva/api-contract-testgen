"""Wait for the local Catalogue stand without LLM or third-party packages.

This is a trusted stand probe, not the sandbox for generated pytest code.
"""

import argparse
from http.client import HTTPException
import json
import math
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_json(url: str, timeout: float):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=timeout) as response:
        return json.load(response)


def _check_health(payload) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("health"), list):
        raise ValueError("health: invalid response")
    services = {}
    for entry in payload["health"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("service"), str):
            raise ValueError("health: invalid service entry")
        services[entry["service"]] = entry.get("status")
    for name in ("catalogue", "catalogue-db"):
        if services.get(name) != "OK":
            raise ValueError(f"health: {name} is not healthy")


def _check_products(payload) -> int:
    if not isinstance(payload, list) or not payload:
        raise ValueError("catalogue: demo products are missing")
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
           or not item["id"].strip() for item in payload):
        raise ValueError("catalogue: invalid product data")
    return len(payload)


def wait_until_ready(base_url: str, timeout_seconds: float = 120) -> dict:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be positive and finite")
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.query or parsed.fragment):
        raise ValueError("base URL must be HTTP(S), without credentials, query or fragment")
    base_url = base_url.rstrip("/")
    started = time.monotonic()
    deadline = started + timeout_seconds
    last_error = "no response received"

    def read(path):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("readiness deadline reached")
        return _read_json(base_url + path, timeout=min(2.0, remaining))

    while time.monotonic() < deadline:
        try:
            _check_health(read("/health"))
            count = _check_products(read("/catalogue"))
            if time.monotonic() >= deadline:
                raise TimeoutError("readiness deadline reached")
            return {
                "status": "ready",
                "base_url": base_url,
                "product_count": count,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        except (OSError, ValueError, HTTPException) as exc:
            last_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1.0, remaining))
    raise TimeoutError(f"Catalogue was not ready within {timeout_seconds:g}s: {last_error}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9911")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    try:
        result = wait_until_ready(args.base_url, args.timeout)
    except (TimeoutError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
