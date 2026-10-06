"""Explicit, serial pytest observer; no mutation plan is exposed to tests."""
import json
import os
from pathlib import Path
import pytest

_collected = []
_cases = {}
_diagnostics = []


def write_json(path, value):
    target = Path(path)
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.replace(target)


def context(nodeid='', phase=''):
    write_json(os.environ['MUTATION_CONTEXT'], {'nodeid': nodeid, 'phase': phase})


def pytest_collection_finish(session):
    _collected.extend(item.nodeid for item in session.items)


def pytest_collectreport(report):
    if report.failed:
        _diagnostics.append(str(report.longrepr))


def pytest_internalerror(excrepr, excinfo):
    _diagnostics.append(str(excrepr))


def pytest_runtest_logstart(nodeid, location):
    context(nodeid, 'setup')


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    record = {'nodeid': item.nodeid, 'outcome': report.outcome, 'phase': report.when}
    if report.failed:
        record['message'] = str(report.longrepr)
        assertion = call.excinfo is not None and call.excinfo.errisinstance(AssertionError)
        record['failure_kind'] = 'assertion' if assertion else 'other'
        if call.excinfo is not None:
            entry = call.excinfo.traceback[-1]
            record['failure_signature'] = f'{entry.path}:{entry.lineno}:{call.excinfo.typename}'
    previous = _cases.get(item.nodeid)
    if report.when == 'call' and (previous is None or previous['outcome'] == 'passed'):
        _cases[item.nodeid] = record
    elif report.when != 'call' and report.outcome != 'passed':
        _cases[item.nodeid] = record
    if report.when == 'setup':
        context(item.nodeid, 'call')
    elif report.when == 'call':
        context(item.nodeid, 'teardown')


def pytest_runtest_logfinish(nodeid, location):
    context()


def pytest_sessionfinish(session, exitstatus):
    context()
    write_json(os.environ['MUTATION_OBSERVATION'], {
        'status': 'completed', 'exit_code': int(exitstatus),
        'collected_nodeids': _collected, 'cases': list(_cases.values()),
        'diagnostics': _diagnostics,
    })


"""Overlay only: keep the existing worker and firewall lifecycle."""
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location('base_worker', '/opt/runner/base_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
# Its child phase launches must return through this overlay.
worker.__file__ = __file__
if sys.argv[1:3] == ['--phase', 'run']:
    sys.path.insert(0, '/opt/mutation')
    import requests
    import json
    os.environ['MUTATION_CONTEXT'] = '/results/mutation-context.json'
    os.environ['MUTATION_OBSERVATION'] = '/results/mutation-observation.json'
    original_send = requests.Session.send
    def send(session, request, **kwargs):
        try:
            context = json.loads(Path(os.environ['MUTATION_CONTEXT']).read_text())
        except (OSError, ValueError):
            context = {}
        request.headers['X-Mutation-Node'] = context.get('nodeid', '')
        request.headers['X-Mutation-Phase'] = context.get('phase', '')
        return original_send(session, request, **kwargs)
    requests.Session.send = send
    original_plugins = worker._policy_plugins
    worker._policy_plugins = lambda: [*original_plugins(), sys.modules[__name__]]
raise SystemExit(worker.main())
