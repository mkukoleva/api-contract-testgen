"""
Микросервис-обёртка над Schemathesis.

Запускает тестирование API по OpenAPI-контракту in-process
(через Python API `schemathesis.engine`) и возвращает JSON-сводку —
минимально жизнеспособный результат (MVP) по контракту.

HTTP-сервер целиком на стандартной библиотеке Python (`http.server`),
без Flask/FastAPI.

Эндпоинты:
- GET  /health — проверка живости сервиса;
- POST /test   — принимает JSON-объект
      {"schema_url" | "schema": "...", "base_url": "...", "max_time": N}
      и возвращает JSON-сводку прогона Schemathesis.
      Контракт можно передать тремя способами:
      * schema_url   — http(s) URL (или путь при ALLOW_LOCAL_SCHEMAS=1);
      * schema       — inline-текст YAML/JSON или объект JSON;
      * сырое тело   — Content-Type: application/yaml,
        тогда base_url/max_time передаются query-параметрами:
        POST /test?base_url=...&max_time=...

Безопасность: schema_url принимается только как http(s) URL.
Локальные пути к файлам разрешены лишь при ALLOW_LOCAL_SCHEMAS=1
(для локальной разработки); inline-передача контракта не открывает
доступа к файловой системе.

Пример запуска локально:
    PORT=8080 python3 main.py

Пример запуска в Docker:
    docker build -f DockerFile -t schemathesis-svc .
    docker run --rm -p 8080:8080 schemathesis-svc
"""

import json
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import schemathesis
from schemathesis import engine as schemathesis_engine
from schemathesis.engine import Status

# По умолчанию контракт принимается только как http(s) URL —
# так отключён доступ к произвольным файлам по schema_url.
# Локальные пути разрешены только при ALLOW_LOCAL_SCHEMAS=1
# (для локальной разработки).
ALLOW_LOCAL_SCHEMAS = os.environ.get("ALLOW_LOCAL_SCHEMAS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Внутренняя ошибка Schemathesis при выполнении теста.
class SchemathesisRunError(RuntimeError):
    """Возникает при фатальной ошибке движка во время прогона."""


# Ошибка загрузки контракта (недоступен URL / невалидный файл).
class SchemaLoadError(RuntimeError):
    """Возникает, если контракт не удалось прочитать по schema_url."""


HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))


def load_schema(schema_url: str, base_url: str | None = None):
    """
    Загрузить OpenAPI-контракт и при необходимости переопределить base_url.

    Args:
        schema_url: URL до OpenAPI-спецификации (http/https) либо локальный путь.
        base_url: Адрес тестируемого API. Если не передан, Schemathesis
            выводит его сам (обычно из location схемы).

    Returns:
        Загруженный объект схемы Schemathesis.

    Raises:
        SchemaLoadError: если контракт не удалось загрузить или распарсить.
    """

    parsed = urlparse(schema_url)

    try:
        if parsed.scheme in ("http", "https"):
            # kwargs (`timeout`) уходят в requests.get внутри Schemathesis.
            schema = schemathesis.openapi.from_url(schema_url, timeout=15)
        elif ALLOW_LOCAL_SCHEMAS:
            schema = schemathesis.openapi.from_path(schema_url)
        else:
            raise SchemaLoadError(
                "schema_url должен быть http(s) URL; локальные пути "
                "разрешены только при ALLOW_LOCAL_SCHEMAS=1."
            )

        if base_url:
            schema.config.update(base_url=base_url)

        return schema

    except SchemaLoadError:
        raise
    except Exception as exc:
        raise SchemaLoadError(f"Не удалось загрузить контракт: {exc}") from exc


def load_inline_schema(schema_source, base_url: str | None = None):
    """
    Загрузить OpenAPI-контракт, переданный inline (текст или словарь).

    Args:
        schema_source: Текст контракта (YAML/JSON строка) либо уже
            разобранный JSON-объект (dict).
        base_url: Адрес тестируемого API (опционально).

    Returns:
        Загруженный объект схемы Schemathesis.

    Raises:
        SchemaLoadError: если контракт не удалось распарсить.
    """
    try:
        if isinstance(schema_source, str):
            schema = schemathesis.openapi.from_file(schema_source)
        elif isinstance(schema_source, dict):
            schema = schemathesis.openapi.from_dict(schema_source)
        else:
            raise SchemaLoadError(
                "Поле schema должно быть текстом YAML/JSON или объектом JSON."
            )

        if base_url:
            schema.config.update(base_url=base_url)

        return schema

    except SchemaLoadError:
        raise
    except Exception as exc:
        raise SchemaLoadError(f"Не удалось разобрать inline-контракт: {exc}") from exc


def collect_summary(schema, stream, meta: dict) -> dict:
    """
    Пройти по потоку событий движка Schemathesis и собрать JSON-сводку.

    Args:
        schema: Загруженная схема (нужна для заголовка и числа операций).
        stream: Синхронный итератор событий (EventStream из engine).
        meta: Метаданные запроса (schema_url, base_url).

    Returns:
        Словарь-сводка в формате ответа /test.

    Raises:
        SchemathesisRunError: при фатальной ошибке движка (FatalError).
    """

    # Соответствие значений Status счётчикам в сводке.
    # Status.SUCCESS.value == "success", FAILURE == "failure",
    # ERROR == "error", SKIP == "skip", INTERRUPTED == "interrupted".
    STATUS_TO_COUNTER = {
        "success": "passed",
        "failure": "failed",
        "error": "errors",
        "skip": "skipped",
        "interrupted": "skipped",
    }

    counts = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    nonfatal_errors = 0
    failures: list[dict] = []
    # Дедупликация: один и тот же дефект не должен повторяться
    # по разным сценариям/проверкам.
    seen_failures: set[tuple] = set()
    stop_reason = None
    running_time = None
    fatal: Exception | None = None

    def record_failure(entry: dict) -> None:
        key = (entry["operation"], entry["check"], entry["title"])
        if key not in seen_failures:
            seen_failures.add(key)
            failures.append(entry)

    for event in stream:
        name = type(event).__name__

        if name == "ScenarioFinished":
            counts["total"] += 1
            counts[STATUS_TO_COUNTER.get(event.status.value, "skipped")] += 1

            # Фрагменты неудачных проверок берём из recorder проверенной
            # структуры: checks[case_id] -> list[CheckNode].
            for nodes in event.recorder.checks.values():
                for node in nodes:
                    if node.status == Status.FAILURE and node.failure_info is not None:
                        failure = node.failure_info.failure
                        record_failure(
                            {
                                "operation": event.recorder.label,
                                "check": node.name,
                                "title": failure.title,
                                "message": failure.message or "",
                                "code_sample": node.failure_info.code_sample,
                            }
                        )

        elif name == "NonFatalError":
            # Не прибавляем к scenario-счётчику errors: недоступный target
            # порождает десятки ConnectionError, которые раздували бы `errors`
            # в разы относительно числа ERROR-сценариев.
            nonfatal_errors += 1

        elif name == "FatalError":
            fatal = event.exception

        elif name == "EngineFinished":
            running_time = round(event.running_time, 3)
            stop_reason = event.stop_reason.value if event.stop_reason else None
            # Классовые проверки after_run могут добавить свои failures.
            for failure in event.failures:
                record_failure(
                    {
                        "operation": getattr(failure, "operation", "") or "",
                        "check": "after_run",
                        "title": failure.title,
                        "message": failure.message or "",
                        "code_sample": "",
                    }
                )

    if fatal is not None:
        raise SchemathesisRunError(f"Фатальная ошибка движка Schemathesis: {fatal}")

    info = schema.raw_schema.get("info") or {}
    title = info.get("title") or ""
    operations = len(schema)

    return {
        "service": "schemathesis",
        "status": "success",
        "schema_title": title,
        "schema_url": meta.get("schema_url", ""),
        "base_url": meta.get("base_url", ""),
        "operations": operations,
        "scenarios": counts,
        "nonfatal_errors": nonfatal_errors,
        "failures": failures,
        "stop_reason": stop_reason,
        "running_time": running_time,
        "message": "Schemathesis завершил тестирование контракта.",
    }


def run_test(
    base_url: str | None = None,
    max_time: float | None = None,
    schema_url: str | None = None,
    schema_inline: str | dict | None = None,
) -> dict:
    """
    Запустить Schemathesis против заданного контракта и вернуть сводку.

    Контракт задаётся одним из двух способов:
    - schema_url: http(s) URL (или, при ALLOW_LOCAL_SCHEMAS=1, локальный путь);
    - schema_inline: текст YAML/JSON либо разобранный JSON-объект.

    Args:
        base_url: Адрес тестируемого API (опционально).
        max_time: Лимит времени выполнения в секундах (опционально).
        schema_url: URL или путь до OpenAPI-контракта.
        schema_inline: Контракт, переданный в теле запроса.

    Returns:
        JSON-сводка прогона (тело ответа /test).

    Raises:
        SchemaLoadError: если ни один источник не задан или контракт невалиден.
    """

    if schema_inline is not None:
        schema = load_inline_schema(schema_inline, base_url)
    elif schema_url:
        schema = load_schema(schema_url, base_url)
    else:
        raise SchemaLoadError("Не задан контракт: укажите schema_url или schema.")

    stream = schemathesis_engine.from_schema(schema).execute()

    # Watchdog: по истечении лимита останавливаем поток событий.
    # После stop() движок корректно завершится событием EngineFinished.
    timer = None
    if max_time is not None and max_time > 0:
        timer = threading.Timer(max_time, stream.stop)
        timer.daemon = True
        timer.start()

    try:
        return collect_summary(
            schema,
            stream,
            meta={
                "schema_url": schema_url or "",
                "base_url": base_url or "",
            },
        )
    finally:
        if timer is not None:
            timer.cancel()


class SchemathesisHandler(BaseHTTPRequestHandler):
    """Обработчик HTTP-запросов микросервиса."""

    server_version = "SchemathesisService/0.1"

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            print("[SCH] GET /health -> 200")
            self._send_json(200, {"status": "ok", "service": "schemathesis"})
        else:
            self._send_json(404, {"status": "error", "message": "Not found."})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/test":
            self._send_json(404, {"status": "error", "message": "Not found."})
            return

        # base_url / max_time могут приходить и query-параметрами —
        # это нужно, когда схема идёт сырым телом (Content-Type: application/yaml).
        query = parse_qs(parsed.query)
        query_base_url = query.get("base_url", [None])[0]
        query_max_time = query.get("max_time", [None])[0]

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0

        raw = self.rfile.read(length) if length > 0 else b""

        # Режим 1: схема передана сырым телом (текст YAML/JSON).
        content_type = (
            self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        )
        if content_type in {
            "application/yaml",
            "text/yaml",
            "application/x-yaml",
            "application/yml",
        }:
            schema_inline = raw.decode("utf-8", "replace")
            if not schema_inline.strip():
                self._send_json(400, {"status": "error", "message": "Тело запроса пусто."})
                return

            max_time = query_max_time
            if max_time is not None:
                try:
                    max_time = float(max_time)
                except (TypeError, ValueError):
                    self._send_json(
                        400,
                        {"status": "error", "message": "Параметр max_time должен быть числом (секунды)."},
                    )
                    return

            self._finish_test(
                schema_inline=schema_inline,
                schema_url=None,
                base_url=query_base_url or None,
                max_time=max_time,
            )
            return

        # Режим 2: JSON-объект с полями schema_url / schema / base_url / max_time.
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            print("[SCH] POST /test -> 400 (неразборчивый JSON)")
            self._send_json(
                400,
                {"status": "error", "message": f"Неразборчивый JSON: {exc}"},
            )
            return

        if not isinstance(payload, dict):
            self._send_json(
                400,
                {"status": "error", "message": "Тело запроса должно быть JSON-объектом."},
            )
            return

        schema_url = payload.get("schema_url")
        schema_inline = payload.get("schema")
        base_url = payload.get("base_url") or query_base_url
        max_time = payload.get("max_time") or query_max_time

        if schema_url and schema_inline is not None:
            self._send_json(
                400,
                {"status": "error", "message": "Задайте одно из полей: schema_url или schema."},
            )
            return

        if (not isinstance(schema_url, str) or not schema_url) and schema_inline is None:
            self._send_json(
                400,
                {"status": "error", "message": "Обязателен schema_url или schema."},
            )
            return

        if max_time is not None:
            try:
                max_time = float(max_time)
            except (TypeError, ValueError):
                self._send_json(
                    400,
                    {"status": "error", "message": "Параметр max_time должен быть числом (секунды)."},
                )
                return

        print(
            f"[SCH] POST /test schema_url={schema_url} "
            f"schema_inline={'да' if schema_inline is not None else 'нет'} "
            f"base_url={base_url} max_time={max_time} -> running..."
        )

        self._finish_test(
            schema_inline=schema_inline,
            schema_url=schema_url or None,
            base_url=base_url if isinstance(base_url, str) and base_url else None,
            max_time=max_time,
        )

    def _finish_test(self, *, schema_inline, schema_url, base_url, max_time) -> None:
        """Запустить прогон и отправить ответ /test."""
        try:
            result = run_test(
                base_url=base_url,
                max_time=max_time,
                schema_url=schema_url,
                schema_inline=schema_inline,
            )
        except SchemaLoadError as exc:
            self._send_json(400, {"status": "error", "message": str(exc)})
            return
        except SchemathesisRunError as exc:
            traceback.print_exc()
            self._send_json(500, {"status": "error", "message": str(exc)})
            return
        except Exception as exc:
            traceback.print_exc()
            self._send_json(500, {"status": "error", "message": f"Внутренняя ошибка: {exc}"})
            return

        self._send_json(200, result)


def main() -> None:
    """Запустить HTTP-сервер микросервиса."""
    server = ThreadingHTTPServer((HOST, PORT), SchemathesisHandler)
    print(f"[SCH] Сервис Schemathesis слушает http://{HOST}:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SCH] Завершение по Ctrl-C.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
