"""Trusted fixtures for both the policy suite and generated tests.

This module is copied into the runner image and registered as a pytest
plugin. The `base_url` and `api_client` fixtures are what generated tests are
allowed to rely on: the client never inherits proxies from the environment,
carries an explicit HTTP timeout, and does not follow redirects by default.
No network policy is chosen here; the container network already limits what
is reachable, and the policy suite verifies that before generated tests run.
"""

import os

import pytest


@pytest.fixture
def base_url() -> str:
    """Base URL of the target API as seen from inside the isolation."""
    url = os.environ.get("RUNNER_BASE_URL", "")
    if not url:
        raise RuntimeError(
            "base_url fixture requires RUNNER_BASE_URL (service mode)")
    return url


@pytest.fixture
def api_client(base_url):
    """requests.Session bound to the target, without proxies or redirects.

    Redirect responses are returned unchanged, including when a caller asks
    to follow them. The kernel firewall also protects clients created by tests.
    """
    import requests

    timeout = float(os.environ.get("RUNNER_TIMEOUT_SECONDS", "60"))

    class _Client(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs.setdefault("timeout", (min(timeout, 10.0), timeout))
            # Session.get supplies True before calling request(), so setdefault
            # is insufficient here. max_redirects=0 also breaks normal 302s.
            kwargs["allow_redirects"] = False
            return super().request(method, url, **kwargs)

        def send(self, request, **kwargs):
            kwargs["allow_redirects"] = False
            kwargs.setdefault("timeout", (min(timeout, 10.0), timeout))
            return super().send(request, **kwargs)

    session = _Client()
    # Never inherit HTTP(S)_PROXY / NO_PROXY from the environment; the
    # sandbox must not route requests through a host proxy.
    session.trust_env = False
    try:
        yield session
    finally:
        session.close()
