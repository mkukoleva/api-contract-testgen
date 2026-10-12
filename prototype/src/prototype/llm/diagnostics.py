"""Provider diagnostics for host-side reports, without configured API keys."""

import os


def describe_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    key = os.getenv("DEEPCODE_API_KEY")
    if key:
        message = message.replace(key, "[REDACTED]")
    return message
