"""Independent CLI: python -m prototype.service_tools.runner TESTS_DIR --output-dir DIR.

Offline run (no network): leave --base-url unset.
Service run (network isolation): pass --base-url and the runner network name,
e.g. --base-url http://catalogue:8080 --network pytest-runner-catalogue_runner
--blocked-network pytest-runner-catalogue_database.
"""

import argparse
import json

from .contracts import RunConfig, RunStatus
from .docker_runner import run_tests


def main():
    parser = argparse.ArgumentParser(description="Run saved pytest tests in an isolated Docker container")
    parser.add_argument("tests_dir")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--base-url", default=None,
                        help="HTTP(S) URL of the target API (service mode)")
    parser.add_argument("--network", default=None,
                        help="runner Docker network containing exactly one reachable container")
    parser.add_argument("--blocked-network", action="append", default=[],
                        help="network whose container addresses must stay unreachable (repeatable)")
    args = parser.parse_args()
    try:
        config = RunConfig(args.tests_dir, args.output_dir,
                           base_url=args.base_url, timeout_seconds=args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    result = run_tests(config, network=args.network,
                       blocked_networks=tuple(args.blocked_network))
    print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2, allow_nan=False))
    # Lifecycle errors and empty suites must never look successful to a shell/CI.
    return 0 if result.status == RunStatus.COMPLETED and result.exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
