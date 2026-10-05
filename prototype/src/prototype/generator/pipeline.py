"""Generation pipeline: generate -> post-process -> run -> limited self-repair.

Only errors attributable to the generated code are repaired. Infrastructure
failures, timeouts, interrupted runs and suspected service defects never call
the LLM (ADR 0004). Each attempt is stored as an immutable version through
prototype.storage (collect=False: collection belongs to the isolated runner).

The runner receives the pipeline's own attempt directory (all post-processed
files, including syntax-error files) so precheck/collection errors stay
visible to the repair loop instead of being hidden by storage's rejected/.
"""

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..evaluate.runnability import compute_runnability
from ..postprocess.fixes import (
    PreparedFile,
    as_generated_files,
    prepare_file,
    write_attempt_dir,
)
from ..reports.generation_report import save_generation_report
from ..service_tools.runner.contracts import (
    RunConfig,
    RunResult,
    RunStatus,
    TestOutcome,
)
from .context import load_contract
from .contracts import GenerationRun, GenerationSettings, RepairAttempt
from .generate import generate_suite
from .repair import apply_repair, classify_result, repair_call

_TERMINAL_INFRA_STATUSES = (
    RunStatus.INFRASTRUCTURE_ERROR,
    RunStatus.TIMEOUT,
    RunStatus.INTERRUPTED,
    RunStatus.NO_TESTS,
)


def _derive_status(run: RunResult) -> str:
    if run.status != RunStatus.COMPLETED:
        return run.status.value
    bad = any(
        test.outcome in (TestOutcome.FAILED, TestOutcome.ERROR) for test in run.tests
    )
    return "completed_with_failures" if bad else "success"


def _run_dir(settings: GenerationSettings) -> Path:
    if settings.output_dir is not None:
        return settings.output_dir
    return Path.cwd() / ".pipeline-runs"


def _expected_cases(files_map: dict[str, str]) -> tuple[str, ...]:
    """Static pytest nodeids of the current suite (runnability denominator).

    Computed from the final prepared files, so repairs that keep a test
    function (the repair contract preserves the name) do not change the set.
    Files with a syntax error contribute no nodeids: their tests are unknowable
    and surface separately through collection/compatibility diagnostics.
    """
    cases: list[str] = []
    for name in sorted(files_map):
        cases.extend(prepare_file(name, files_map[name]).test_cases)
    return tuple(cases)


def _save_report(run: GenerationRun, settings: GenerationSettings) -> GenerationRun:
    """Write the JSON + Markdown generation report; never abort the result.

    A failed write is recorded as a generator error instead of being raised,
    keeping the pipeline's verdict usable while staying honest about the audit.
    """
    try:
        paths = save_generation_report(run, _run_dir(settings))
    except Exception as exc:  # report is auxiliary; the run result must survive
        return replace(
            run,
            generator_errors=(
                *run.generator_errors,
                f"saving the generation report failed: {exc}",
            ),
        )
    return replace(
        run,
        report_paths={key: str(path) for key, path in paths.items()},
    )


def _repair_log_from(log: list[dict[str, Any]]) -> tuple[RepairAttempt, ...]:
    return tuple(
        RepairAttempt(
            index=entry["index"],
            target=entry["identifier"],
            kind=entry["kind"],
            error_signature=entry["signature"],
            outcome=entry["outcome"],
            payload_chars=entry["payload_chars"],
            tokens_output=entry["tokens_output"],
            message=entry["message"],
        )
        for entry in log
    )


def run_generation_pipeline(
    settings: GenerationSettings,
    *,
    model: Any = None,
    run_tests_fn: Callable[[RunConfig], RunResult] | None = None,
    save_fn: Callable[..., Any] | None = None,
) -> GenerationRun:
    """Run the full pipeline and return a structured GenerationRun.

    ``run_tests_fn`` and ``save_fn`` default to the isolated Docker runner and
    prototype.storage; tests inject fakes for both.
    """
    from ..service_tools.runner.docker_runner import run_tests as default_run_tests
    from ..storage import SaveRequest, save_test_suite as default_save

    # output_dir is a root, not a reusable attempt directory. Allocate even
    # for a failed generation so every report remains an immutable run record.
    run_dir = _run_dir(settings) / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex)
    run_dir.mkdir(parents=True, exist_ok=False)
    settings = replace(settings, output_dir=run_dir)

    # One model instance is shared by generation and every repair call, so a
    # caller can pass a model override or let the pipeline build the default.
    if model is None:
        from ..llm.model import build_model

        model = build_model()

    if run_tests_fn is None:
        # Service mode needs the runner network (and blocked networks) which
        # live outside RunConfig: the default wrapper closes over settings.
        def run_tests_with_network(config: RunConfig) -> RunResult:
            return default_run_tests(
                config,
                network=settings.network,
                blocked_networks=settings.blocked_networks,
            )

        run_tests_fn = run_tests_with_network
    save_fn = save_fn or default_save

    contract = load_contract(settings.contract_path)
    generated = generate_suite(contract, settings, model=model)
    gen_input, gen_output, gen_total = generated["tokens"]
    generator_errors = list(generated["errors"])
    prepared: list[PreparedFile] = list(generated["prepared"])

    if not prepared:
        status = "generation_error" if generator_errors else "no_tests"
        return _save_report(
            GenerationRun(
                status=status,
                contract_path=settings.contract_path,
                model=settings.model,
                attempts=0,
                input_tokens=gen_input,
                output_tokens=gen_output,
                total_tokens=gen_total,
                generator_errors=tuple(generator_errors),
                test_cases=(),
                runnability=compute_runnability((), None).to_dict(),
            ),
            settings,
        )

    files_map: dict[str, str] = {item.name: item.code for item in prepared}
    run_dir = _run_dir(settings)
    saved_versions: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    defects: list[dict[str, Any]] = []
    environmental: list[dict[str, Any]] = []

    def save_attempt(index: int, files: dict[str, str], extra: dict[str, Any] | None = None) -> None:
        try:
            generated_files = as_generated_files(
                tuple(prepare_file(name, code) for name, code in files.items())
            )
            saved = save_fn(
                SaveRequest(
                    contract_path=settings.contract_path,
                    files=generated_files,
                    model=settings.model,
                    generator_meta={"attempt": index, **(extra or {})},
                    output_root=settings.output_root,
                    collect=False,
                )
            )
            saved_versions.append(
                {"attempt": index, "run_id": saved.run_id, "tests_dir": str(saved.tests_dir)}
            )
        except Exception as exc:  # versioning must not kill the run
            generator_errors.append(f"save attempt {index} failed: {exc}")

    save_attempt(0, files_map)

    attempt_index = 0
    attempts_used = 0
    used_output = 0
    repair_input = 0
    repair_total = 0
    final_run: RunResult | None = None
    status = "completed_with_failures"
    repeat_key: tuple[str, str] | None = None

    while True:
        attempt_dir = run_dir / f"attempt-{attempt_index}"
        tests_dir = write_attempt_dir(
            tuple(prepare_file(name, code) for name, code in files_map.items()),
            attempt_dir / "tests",
        )
        try:
            run = run_tests_fn(
                RunConfig(
                    tests_dir=tests_dir,
                    output_dir=attempt_dir / "results",
                    base_url=settings.base_url,
                    timeout_seconds=settings.runner_timeout_seconds,
                )
            )
        except Exception as exc:
            final_run = None
            status = "infrastructure_error"
            generator_errors.append(f"runner failed: {exc}")
            break
        final_run = run

        if run.status in _TERMINAL_INFRA_STATUSES:
            status = run.status.value
            break

        plan = classify_result(contract, files_map, run, settings)
        defects.extend(plan.suspected_defects)
        environmental.extend(plan.environmental)
        repairable = [target for target in plan.targets if target.file in files_map]

        if not repairable:
            status = _derive_status(run)
            break

        if repeat_key is not None and any(
            target.identifier() == repeat_key[0] and target.signature == repeat_key[1]
            for target in repairable
        ):
            if log:
                log[-1]["outcome"] = "stuck"
            generator_errors.append(
                "self-repair stopped: the same error repeated after repair"
            )
            status = _derive_status(run)
            break

        if attempts_used >= settings.max_repair_attempts:
            generator_errors.append(
                f"self-repair stopped: max_repair_attempts={settings.max_repair_attempts} reached"
            )
            status = _derive_status(run)
            break

        if used_output >= settings.repair_token_budget:
            generator_errors.append(
                f"self-repair stopped: output token budget={settings.repair_token_budget} reached"
            )
            status = _derive_status(run)
            break

        target = repairable[0]
        call = repair_call(model, settings, contract, target, files_map[target.file])
        attempts_used += 1
        used_output += call.tokens[1]
        repair_input += call.tokens[0]
        repair_total += call.tokens[2]

        entry: dict[str, Any] = {
            "index": attempts_used,
            "identifier": target.identifier(),
            "kind": target.kind,
            "signature": target.signature,
            "payload_chars": call.payload_chars,
            "tokens_output": call.tokens[1],
            "outcome": "repaired",
            "message": None,
        }
        log.append(entry)
        repeat_key = (target.identifier(), target.signature)

        if not call.ok:
            entry["outcome"] = "error"
            entry["message"] = call.error or "repair call failed"
            status = _derive_status(run)
            break

        try:
            files_map = apply_repair(files_map, target, call.code)
            save_attempt(attempts_used, files_map, {"repaired": target.identifier()})
        except Exception as exc:
            entry["outcome"] = "error"
            entry["message"] = str(exc)
            status = _derive_status(run)
            break

        attempt_index += 1
        continue

    expected = _expected_cases(files_map)
    run_result_dict = final_run.to_dict() if final_run is not None else None
    return _save_report(
        GenerationRun(
            status=status,
            contract_path=settings.contract_path,
            model=settings.model,
            attempts=attempts_used,
            input_tokens=gen_input + repair_input,
            output_tokens=gen_output + used_output,
            total_tokens=gen_total + repair_total,
            tests=run_result_dict["tests"] if run_result_dict is not None else (),
            repair_log=_repair_log_from(log),
            suspected_defects=tuple(defects),
            environmental=tuple(environmental),
            saved_versions=tuple(saved_versions),
            generator_errors=tuple(generator_errors),
            run_result=run_result_dict,
            test_cases=expected,
            runnability=compute_runnability(expected, run_result_dict).to_dict(),
        ),
        settings,
    )
