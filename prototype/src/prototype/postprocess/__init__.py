"""Deterministic post-processing of generated test modules (before running)."""

from .fixes import (
    PreparedFile,
    as_generated_files,
    prepare_file,
    prepare_files,
    write_attempt_dir,
)

__all__ = [
    "PreparedFile",
    "as_generated_files",
    "prepare_file",
    "prepare_files",
    "write_attempt_dir",
]
