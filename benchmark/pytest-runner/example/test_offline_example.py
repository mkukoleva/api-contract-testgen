"""Small saved suite for trying the runner without Catalogue or an LLM."""

import pytest


@pytest.mark.parametrize("status_code", [200, 201, 204])
def test_success_status_range(status_code):
    assert 200 <= status_code < 300
