"""Behavioral checks for the standalone verification command; no Docker needed."""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def verify(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[4] / "tools" / "verify.py"
    spec = importlib.util.spec_from_file_location("verify_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "PROTOTYPE_DIR", tmp_path / "prototype")
    monkeypatch.setattr(module, "DEFAULT_SNAPSHOT", tmp_path / "saved")
    monkeypatch.setattr(module.sys, "version_info", module.TARGET_VERSION)
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    return module


def subprocess_double(monkeypatch, verify, *, existing=False, failure=None):
    calls = []

    def execute(cmd, **kwargs):
        calls.append(cmd)
        stage = ("ps" if "ps" in cmd else "up" if "up" in cmd else
                 "down" if "down" in cmd else "pytest" if "pytest" in cmd else
                 "readiness" if str(verify.CHECK_READY) in cmd else "preflight")
        stdout = ""
        if stage == "ps":
            stdout = "existing-container\n" if existing else ""
        elif stage == "readiness":
            stdout = json.dumps({"status": "ready", "base_url": "http://localhost", "product_count": 1})
        elif stage == "pytest":
            stdout = "1 passed"
        return subprocess.CompletedProcess(cmd, 1 if failure == stage else 0, stdout, "injected failure" if failure == stage else "")

    monkeypatch.setattr(verify.subprocess, "run", execute)
    return calls


def test_prepared_environment_does_not_require_uv_on_path(verify, monkeypatch):
    monkeypatch.setattr(verify.shutil, "which", lambda name: None)
    assert "ok" in verify.env_check_common()


@pytest.mark.parametrize("failure", ["preflight", "up", "readiness"])
def test_failed_prerequisite_skips_dependent_commands(verify, monkeypatch, failure):
    calls = subprocess_double(monkeypatch, verify, failure=failure)
    assert verify.main(["--skip-runner-set"]) == 1
    assert not any("pytest" in cmd for cmd in calls)
    if failure == "preflight":
        assert not any("up" in cmd or "down" in cmd for cmd in calls)
    if failure == "up":
        assert not any(str(verify.CHECK_READY) in cmd for cmd in calls)


@pytest.mark.parametrize("failure", [None, "pytest", "up"])
def test_preexisting_stand_is_not_torn_down(verify, monkeypatch, failure):
    calls = subprocess_double(monkeypatch, verify, existing=True, failure=failure)
    assert verify.main(["--skip-runner-set"]) == (1 if failure else 0)
    assert not any("down" in cmd for cmd in calls)


@pytest.mark.parametrize("failure", ["up", "readiness", "pytest"])
def test_new_stand_cleaned_after_failure_without_deleting_volumes(verify, monkeypatch, failure):
    calls = subprocess_double(monkeypatch, verify, failure=failure)
    assert verify.main(["--skip-runner-set"]) == 1
    downs = [cmd for cmd in calls if "down" in cmd]
    assert len(downs) == 1
    assert "--volumes" not in downs[0]


def test_keep_stand_preserves_new_stand_on_failure(verify, monkeypatch):
    calls = subprocess_double(monkeypatch, verify, failure="pytest")
    assert verify.main(["--skip-runner-set", "--keep-stand"]) == 1
    assert not any("down" in cmd for cmd in calls)


def test_failed_ownership_probe_does_not_start_or_remove_stand(verify, monkeypatch):
    calls = subprocess_double(monkeypatch, verify, failure="ps")
    assert verify.main(["--skip-runner-set"]) == 1
    assert not any("up" in cmd or "down" in cmd or "pytest" in cmd for cmd in calls)


def test_interruption_still_cleans_new_stand(verify, monkeypatch):
    calls = subprocess_double(monkeypatch, verify)
    execute = verify.subprocess.run

    def interrupted(cmd, **kwargs):
        if "pytest" in cmd:
            raise KeyboardInterrupt
        return execute(cmd, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        verify.main(["--skip-runner-set"])
    assert sum("down" in cmd for cmd in calls) == 1


def test_reports_cannot_collide_at_same_timestamp(verify, monkeypatch):
    class FrozenDatetime:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 5, tzinfo=timezone.utc)

    monkeypatch.setattr(verify, "datetime", FrozenDatetime)
    args = argparse.Namespace(report_root="reports")
    first, second = verify.Verify(args), verify.Verify(args)
    first.write_summary("offline")
    second.write_summary("full")
    assert first.report_dir != second.report_dir
    assert json.loads((first.report_dir / "summary.json").read_text())["mode"] == "offline"


def test_cache_cleanup_rejects_symlink(verify, monkeypatch):
    cache = verify.PROTOTYPE_DIR / ".pytest_cache"
    cache.mkdir(parents=True)
    marker = cache / "marker"
    marker.write_text("keep")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == cache or original(path))
    with pytest.raises(verify.Failure):
        verify.clean_temp()
    assert marker.read_text() == "keep"


def test_cache_cleanup_removes_only_cache(verify):
    cache = verify.PROTOTYPE_DIR / ".pytest_cache"
    cache.mkdir(parents=True)
    (cache / "entry").write_text("temporary")
    retained = verify.PROTOTYPE_DIR / "retained"
    retained.write_text("keep")
    verify.clean_temp()
    assert not cache.exists()
    assert retained.read_text() == "keep"
