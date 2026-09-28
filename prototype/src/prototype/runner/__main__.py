"""Independent CLI: python -m prototype.runner TESTS_DIR --output-dir DIR."""

import argparse
import json

from .contracts import RunConfig, RunStatus
from .docker_runner import run_tests


def main():
    parser = argparse.ArgumentParser(description="Run saved pytest tests in an offline Docker container")
    parser.add_argument("tests_dir")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    try:
        config = RunConfig(args.tests_dir, args.output_dir, timeout_seconds=args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    result = run_tests(config)
    print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2, allow_nan=False))
    # Lifecycle errors and empty suites must never look successful to a shell/CI.
    return 0 if result.status == RunStatus.COMPLETED and result.exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
