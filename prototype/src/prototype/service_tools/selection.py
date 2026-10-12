"""Trusted, bounded tool selection for TZ v3 4.1.7.3; no model calls here."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from uuid import uuid4


def select_backend(intent="quick"):
    try:
        return {"quick": "schemathesis", "deep": "evomaster", "mock": "microcks", "multi": "microcks"}[intent]
    except KeyError as exc:
        raise ValueError("intent must be quick, deep, mock or multi") from exc


@dataclass(frozen=True)
class ToolSettings:
    contract_path: Path | str
    base_url: str | None = None
    intent: str = "quick"
    output_dir: Path | str = ".tool-runs"
    timeout_seconds: float = 30
    network: str | None = None
    schemathesis_url: str = "http://127.0.0.1:8080"
    microcks_url: str = "http://127.0.0.1:8585"
    microcks_service_id: str | None = None
    microcks_runner: str = "OPEN_API_SCHEMA"
    evomaster_image: str = "webfuzzing/evomaster:v6.1.1"

    def __post_init__(self):
        select_backend(self.intent)
        for name in ("contract_path", "output_dir"):
            value = getattr(self, name)
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError(f"{name} must be a non-empty path")
            object.__setattr__(self, name, Path(value))
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 600):
            raise ValueError("tool timeout must be finite, positive and <=600 seconds")
        if self.intent != "mock" and not self.base_url:
            raise ValueError("selected test requires --base-url")
        if self.intent == "deep" and not self.network:
            raise ValueError("deep search requires a trusted Docker --network")
        from .runner.contracts import _validate_base_url
        for value in (self.schemathesis_url, self.microcks_url):
            _validate_base_url(value)
        if self.base_url:
            # Microcks uses http(s) URI syntax even for a gRPC target, to
            # distinguish plaintext transport from TLS.
            _validate_base_url(self.base_url)


def _clip(value, depth=0):
    if depth >= 4:
        return "(details in report)"
    if isinstance(value, str):
        return value[:350]
    if isinstance(value, dict):
        return {str(k)[:80]: _clip(v, depth+1) for k, v in list(value.items())[:10]}
    if isinstance(value, (list, tuple)):
        return [_clip(v, depth+1) for v in value[:3]]
    return value


def compact_result(payload):
    """Actual verdict/counts + a few diagnostics, never the full test/code corpus."""
    result = {key: payload[key] for key in ("backend", "status", "success", "summary", "duration_seconds", "report_paths") if key in payload}
    for key in ("message", "failures", "recommendations", "mock_url", "service_id"):
        if key in payload:
            result[key] = _clip(payload[key])
    if len(json.dumps(result, ensure_ascii=False)) > 6000:
        for key in ("failures", "recommendations"):
            result.pop(key, None)
    result["details_in_report"] = True
    return result


def execute_selected(settings):
    from dataclasses import replace
    from .backends import run_schemathesis, run_evomaster, run_microcks
    from ..reports.recommendations import build_recommendations
    directory = settings.output_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    settings = replace(settings, output_dir=directory)
    backend = select_backend(settings.intent)
    try:
        payload = {"schemathesis": run_schemathesis, "evomaster": run_evomaster, "microcks": run_microcks}[backend](settings)
    except Exception as exc:
        from ..llm.diagnostics import describe_error
        message = describe_error(exc)
        token = os.getenv("MICROCKS_TOKEN")
        if token:
            message = message.replace(token, "[REDACTED]")
        payload = {"backend": backend, "status": "input_error" if isinstance(exc, ValueError) else "infrastructure_error", "message": message}
    payload["intent"] = settings.intent
    paths = {"json": str(directory.resolve() / "tool-report.json"), "markdown": str(directory.resolve() / "tool-report.md")}
    payload["report_paths"] = paths
    # The backend report may not provide enough evidence for response coverage.
    # Preserve that uncertainty; coverage percentages belong to evaluate.
    try:
        from ..parser.contract import _load_contract
        schema = _load_contract(settings.contract_path)
    except (OSError, ValueError):
        schema = {}
    defects = [{"nodeid": x.get("operation"), "reason": x.get("title", x.get("message"))}
               for x in payload.get("failures", ()) if isinstance(x, dict)]
    payload["recommendations"] = build_recommendations(schema, payload if backend == "evomaster" else None,
        scope="Фактические coveredHttpStatus из отчёта EvoMaster." if backend == "evomaster" else "Факты внешнего инструмента; HTTP-статусы покрыты только при наличии явных наблюдений.",
        suspected_defects=defects,
        environmental=({"nodeid": backend},) if payload["status"] in {"infrastructure_error", "timeout"} else ())
    if payload["status"] == "input_error":
        payload["recommendations"]["items"].insert(0, {"kind": "contract_clarification",
            "evidence": payload.get("message"), "action": "Проверить контракт и настройки инструмента, включая название и версию API; не менять тесты для обхода ошибки входа."})
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    Path(paths["json"]).write_text(text, encoding="utf-8")
    import html
    Path(paths["markdown"]).write_text("# Результат инструмента и рекомендации\n\n<pre>" + html.escape(text) + "</pre>\n", encoding="utf-8")
    return payload


def create_selected_tool(settings, *, execute_fn=None):
    """No addresses/limits in LLM arguments; repeated calls replay the same facts."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, ConfigDict
    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")
    class Bound(StructuredTool):
        def _to_args_and_kwargs(self, tool_input, tool_call_id):
            if not isinstance(tool_input, dict) or tool_input:
                raise ValueError("Selected tool accepts no configuration arguments")
            return (), {}
    cached = []
    from threading import Lock
    lock = Lock()
    backend = select_backend(settings.intent)
    def invoke_selected():
        with lock:
            if not cached:
                data = (execute_fn or execute_selected)(settings)
                cached.append({"tool": backend + "_tool", **compact_result(data)})
        return cached[0]
    descriptions = {"schemathesis": "Quick contract check requested by the user.",
                    "evomaster": "Deep black-box search explicitly requested by the user.",
                    "microcks": "API mocks or multi-protocol checks requested by the user."}
    return Bound.from_function(invoke_selected, name=backend + "_tool", description=descriptions[backend], args_schema=Input)


def main():
    """Direct tool execution without LLM; non-OpenAPI Microcks services use their ID."""
    import argparse
    parser = argparse.ArgumentParser(description="Проверка выбранным инструментом без LLM.")
    parser.add_argument("contract", type=Path)
    parser.add_argument("--intent", choices=("quick", "deep", "mock", "multi"), default="quick")
    parser.add_argument("--base-url")
    parser.add_argument("--network")
    parser.add_argument("--output-dir", type=Path, default=Path(".tool-runs"))
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--schemathesis-url", default="http://127.0.0.1:8080")
    parser.add_argument("--microcks-url", default="http://127.0.0.1:8585")
    parser.add_argument("--microcks-service-id")
    parser.add_argument("--microcks-runner", default="OPEN_API_SCHEMA")
    parser.add_argument("--evomaster-image", default="webfuzzing/evomaster:v6.1.1")
    args = parser.parse_args()
    try:
        settings = ToolSettings(args.contract, args.base_url, args.intent, args.output_dir, args.timeout, args.network,
            args.schemathesis_url, args.microcks_url, args.microcks_service_id, args.microcks_runner, args.evomaster_image)
        result = execute_selected(settings)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(compact_result(result), ensure_ascii=False, indent=2))
    if result["status"] != "completed" or result.get("success") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
