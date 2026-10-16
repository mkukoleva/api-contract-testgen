"""Run journal: one JSON event per line, written as it happens (ТЗ v3 4.1.8.2).

Events emitted before the run directory is known are buffered and written on
attach(). A journal I/O failure never breaks the run: the journal warns once on
stderr and disables itself.
"""

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


JSONL_NAME = "journal.jsonl"
MARKDOWN_NAME = "journal.md"
MAX_TEXT = 2000
_TRUNCATED = "…[обрезано]"
_SECRET_VARIABLES = ("DEEPCODE_API_KEY", "OPENAI_API_KEY")


def redact(value: Any) -> Any:
    """Replace configured API keys and truncate long strings, recursively."""
    if isinstance(value, str):
        for name in _SECRET_VARIABLES:
            secret = os.environ.get(name)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + _TRUNCATED
    if isinstance(value, dict):
        return {str(key): redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


class Journal:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._clock = clock
        self._now = now
        self._started = clock()
        self._events: list[dict] = []
        self._directory: Path | None = None
        self._file = None
        self._disabled = False
        self._closed = False

    @property
    def events(self) -> list[dict]:
        return [dict(event) for event in self._events]

    @property
    def directory(self) -> Path | None:
        return self._directory

    @property
    def disabled(self) -> bool:
        return self._disabled

    def emit(self, event: str, status: str = "info", **data: Any) -> None:
        record = {
            "seq": len(self._events) + 1,
            "ts": self._now().isoformat(timespec="milliseconds"),
            "elapsed_s": round(self._clock() - self._started, 3),
            "event": event,
            "status": status,
            "data": redact(data),
        }
        self._events.append(record)
        if self._file is not None:
            self._write(record)

    def attach(self, directory: Path | str) -> None:
        directory = Path(directory)
        if self._directory is not None:
            if directory != self._directory:
                raise ValueError(f"journal is already attached to {self._directory}")
            return
        self._directory = directory
        if self._disabled:
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._file = (directory / JSONL_NAME).open("a", encoding="utf-8")
        except OSError as exc:
            self._fail(exc)
            return
        for record in self._events:
            self._write(record)

    def close(self, render: Callable[[list[dict]], str] | None = None) -> None:
        if render is None:
            from .render import render_markdown as render
        if self._closed:
            return
        self._closed = True
        if self._file is not None:
            try:
                self._file.close()
            except OSError as exc:
                self._fail(exc)
            self._file = None
        if self._directory is None or self._disabled:
            return
        try:
            (self._directory / MARKDOWN_NAME).write_text(render(self.events), encoding="utf-8")
        except OSError as exc:
            self._fail(exc)

    def _write(self, record: dict) -> None:
        try:
            self._file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            self._file.flush()
        except (OSError, ValueError) as exc:
            self._fail(exc)

    def _fail(self, exc: BaseException) -> None:
        if not self._disabled:
            print(f"[JOURNAL] Журнал отключён: {type(exc).__name__}: {redact(str(exc))}", file=sys.stderr)
        self._disabled = True
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
