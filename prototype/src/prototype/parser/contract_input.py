"""Contract locations and immutable input bytes. No third-party imports or I/O on import."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


class ContractInputError(ValueError):
    """An invalid contract location, encoding, syntax or structure."""


def normalize_location(value: Path | str) -> Path | str:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ContractInputError("contract_path: укажите путь или HTTP(S) URL контракта.")
    if isinstance(value, Path):
        return value
    # Windows drive letters are paths, not URL schemes.
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return Path(value)
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme.lower() in {"http", "https"} and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.fragment and parsed.port != 0
                     and not any(c.isspace() or ord(c) < 32 for c in value))
        except ValueError:
            valid = False
        if not valid:
            raise ContractInputError("URL контракта должен быть HTTP(S), без учётных данных и фрагмента.")
        return value
    return Path(value)


@dataclass(frozen=True)
class ContractDocument:
    source: str
    raw: bytes
    data: dict[str, Any]
    resolved_url: str | None = None
    # Resolved URLs of external $ref documents fetched at load time, in order.
    external_refs: tuple[str, ...] = ()
