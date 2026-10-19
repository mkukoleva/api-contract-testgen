"""Launch parameters for reproducibility (ТЗ v3 4.1.8.3), from an explicit whitelist."""

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_GENERATION_FIELDS = ("base_url", "network", "blocked_networks", "runner_timeout_seconds",
                      "max_repair_attempts", "repair_token_budget", "output_root")
_TESTING_FIELDS = ("intent", "timeout_seconds", "base_url", "network", "schemathesis_url",
                   "microcks_url", "microcks_service_id", "microcks_runner", "evomaster_image")
_URL_FIELDS = {"base_url", "schemathesis_url", "microcks_url"}


def strip_credentials(url: str) -> str:
    """Drop `user:password@` from a URL; anything that is not a URL is returned as is."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def _plain(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _fields(settings: Any, names: tuple[str, ...]) -> dict[str, Any]:
    result = {}
    for name in names:
        if not hasattr(settings, name):
            continue
        value = _plain(getattr(settings, name))
        if name in _URL_FIELDS and isinstance(value, str):
            value = strip_credentials(value)
        result[name] = value
    return result


def launch_parameters(*, mode: str, contract: str, report_dir, source_code,
                      generation_settings=None, testing_settings=None) -> dict[str, Any]:
    launch = {
        "mode": mode,
        "contract": strip_credentials(str(contract)),
        "report_dir": None if report_dir is None else str(report_dir),
        "source_code": None if source_code is None else str(source_code),
    }
    if generation_settings is not None:
        launch.update(_fields(generation_settings, _GENERATION_FIELDS))
    if testing_settings is not None:
        launch.update(_fields(testing_settings, _TESTING_FIELDS))
    return launch
