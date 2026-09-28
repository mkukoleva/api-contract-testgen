"""Storage of generated pytest suites as immutable, versioned directories (ТЗ 2.1.6)."""

from .contracts import GeneratedFile, SaveRequest, SavedSuite
from .store import save_test_suite

__all__ = ["GeneratedFile", "SaveRequest", "SavedSuite", "save_test_suite"]
