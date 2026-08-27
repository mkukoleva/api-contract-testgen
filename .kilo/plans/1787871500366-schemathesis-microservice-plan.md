# План: микросервис-обёртка над schemathesis (MVP)

## Цель
В папке `prototype/service tools/schematesis/` собрать MVP-микросервис, который принимает на порту минимальный HTTP-запрос, запускает **schemathesis** in-process и возвращает JSON-сводку тестирования («MVP результат по контракту»). HTTP-сервер — только стандартная библиотека Python (`http.server`), без Flask/FastAPI. Рядом — `DockerFile`, который запускает этот сервис.

Сейчас в папке только заглушки:
- `prototype/service tools/schematesis/main.py` — пустая `main()`.
- `prototype/service tools/schematesis/DockerFile` — одна строка-комментарий про порт.

## Согласованные решения
1. Эндпоинт: `POST /test` (JSON) → JSON-сводка. `GET /health` → `{"status": "ok"}`.
2. Schemathesis запускается **in-process** через Python API (`schemathesis.engine`), версия зафиксирована: **`schemathesis==4.25.2`** (уже установлена в `prototype/.venv`, её API проверен ниже).
3. Порт берётся из переменной окружения `PORT` (default `8080`), в образе `EXPOSE 8080`.
4. Контракт передаётся параметром `schema_url` (URL, где лежит openapi.json); целевой API — параметром `base_url` (опционально; если не передан, schemathesis выводит его сам из location схемы).

## Файлы для создания/изменения
1. `prototype/service tools/schematesis/main.py` — заменить заглушку на готовый сервис.
2. `prototype/service tools/schematesis/requirements.txt` — новый, `schemathesis==4.25.2`.
3. `prototype/service tools/schematesis/DockerFile` — перезаписать рабочим Dockerfile.

Стиль: докстринги/комментарии на русском, как в остальном репозитории.

## main.py — структура и логика

### Константы
- `HOST = "0.0.0.0"`, `PORT = int(os.environ.get("PORT", "8080"))`.

### Загрузка схемы
`load_schema(schema_url, base_url=None) -> (schema, meta)`:
- если `schema_url` начинается с `http://`/`https://` → `schemathesis.openapi.from_url(schema_url, timeout=15)` (там всё равно берётся request-клиент, можно через `kwargs` передать `timeout`);
- иначе (локальный файл) → `schemathesis.openapi.from_path(schema_url)`;
- если передан `base_url` → `schema.config.update(base_url=base_url)` (поле `base_url` у `ProjectConfig`, см. `schemathesis/config/_projects.py`; читается в `BaseSchema.get_base_url()`);
- `meta`: `title` из `schema.raw_schema["info"]["title"]`, `operations = len(schema)` (равен `schema.statistic.operations.total`).
- Обернуть исключения загрузки (`LoaderError`, сетевые, `ValueError`) в понятное сообщение.

### Агрегация событий (проверенный API 4.25.2)
`collect_summary(schema, stream, meta) -> dict` — обычный синхронный `for event in stream:` (внимание: `EventStream` — синхронный итератор, `__next__`/`__iter__` в `schemathesis/engine/core.py:210`, asyncio НЕ нужен). Типы и поля событий из `schemathesis/engine/events.py`, `recorder.py`, `core/failures.py`:

- `ScenarioFinished` (событие) — счётчики `total`; по `event.status` (`from schemathesis.engine import Status`):
  - `Status.SUCCESS` → passed, `Status.FAILURE` → failed, `Status.ERROR` → errors, иначе skipped.
  - при failure/error из `event.recorder.checks` (dict `case_id -> list[CheckNode]`): для каждого `CheckNode` c `node.status == Status.FAILURE` собрать
    `{"operation": recorder.label, "check": node.name, "title": node.failure_info.failure.title, "message": node.failure_info.failure.message, "code_sample": node.failure_info.code_sample}`.
    `failure` — экземпляр `Failure(AssertionError)` со слотами `title`, `message` (`schemathesis/core/failures.py:56`).
- `NonFatalError` — посчитать в `errors`.
- `FatalError` — пометить прогон как прерванный (в `status:"error"`), сохранить `end2.exception`.
- `EngineFinished` — `running_time`, `stop_reason` (`"completed"` | `"interrupted"` | `"failure_limit"` | `"max_time"`), `failures` из event.
- При пустом потоке/отсутствии `EngineFinished` — вернуть partial-сводку.

### Запуск теста
`run_test(schema_url, base_url=None, max_time=None) -> dict`:
- `schema, meta = load_schema(...)`;
- `stream = schemathesis.engine.from_schema(schema).execute()`;
- опциональный watchdog: `threading.Timer(max_time, stream.stop)` (после `stop()` следующий event = `EngineFinished`, `EventStream.stop()` в `engine/core.py:225`);
- `summary = collect_summary(...)`; оформить итоговый JSON (пример ниже).

### HTTP-обработчик
`Handler(BaseHTTPRequestHandler)` из `http.server`:
- `do_GET`: `/health` → 200 `{"status": "ok", "service": "schemathesis"}`; иначе 404.
- `do_POST`: только путь `/test`; читать `Content-Length`, парсить `json.loads`; валидация:
  - обязательный `schema_url`; необязательные `base_url`, `max_time`;
  - неразборчивый JSON / отсутствие `schema_url` → 400 `{"status":"error","message":...}`;
  - ошибка загрузки схемы (сеть/неверный файл) → 400 с сообщением;
  - `FatalError`/внутренняя ошибка → 500 `{"status":"error",...}`;
  - успех → 200 со сводкой.
- Ответ всегда JSON, `Content-Type: application/json`, `send_response` + `end_headers`.
- Логировать поступающие запросы (`[SCH] POST /test ...`). (`ThreadingHTTPServer` обрабатывает запросы в отдельных потоках — каждый `/test` не блокирует `/health`.)

### Точка входа
`main()`: `ThreadingHTTPServer((HOST, PORT), Handler)`, `serve_forever()`; на старте вывести `[SCH] Слушаю 0.0.0.0:PORT`, обработка `KeyboardInterrupt` → чистое завершение.

### Формат ответа `/test`
```json
{
  "service": "schemathesis",
  "status": "success",
  "schema_title": "Demo Catalogue API",
  "schema_url": "https://.../openapi.json",
  "base_url": "http://target.local:8000",
  "operations": 3,
  "scenarios": {"total": 10, "passed": 5, "failed": 2, "errors": 3, "skipped": 0},
  "failures": [
    {"operation": "GET /catalogue/{id}", "check": "negative_data_rejection",
     "title": "Server error", "message": "...", "code_sample": "curl ..."}
  ],
  "stop_reason": "completed",
  "running_time": 1.23,
  "message": "Schemathesis завершил тестирование контракта."
}
```
Ключи — на английском (как в `prototype/runner/tools.py`), сообщения на русском.

## requirements.txt
```
schemathesis==4.25.2
```
(фиксация версии важна: план опирается на внутренний engine-API 4.25.2).

## DockerFile
```dockerfile
# MVP-микросервис поверх schemathesis.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "main.py"]
```
- `python:3.11-slim` — совпадает с версией в CI (`.github/workflows/ci.yml` использует 3.11; schemathesis требует `>=3.10`).
- Локальный venv — python 3.14; для контейнера 3.11 достаточно (schemathesis поддерживает 3.10–3.14).
- Опционально (рекомендуется): переименовать `DockerFile` → `Dockerfile` (стандарт, `docker build` найдёт сам); если имя оставляем — собирать через `docker build -f DockerFile .`.

## Порядок работы (задачи исполнителя)
1. Написать `requirements.txt`.
2. Написать `main.py` по схеме выше.
3. Перед финализацией агрегации прогнать probe-скрипт: запустить `engine` на тестовой схеме и распечатать типы/поля реальных событий — сверить `recorder.label`, `CheckNode.name/failure_info.code_sample/failure.title/message`, `EngineFinished.running_time/stop_reason` (структура подтверждена выше, но double-check на живой схеме).
4. Перезаписать `DockerFile` (и, при согласии, переименовать в `Dockerfile`).

## Проверка (validation)
- Локально (из `prototype/service tools/schematesis`, используя уже готовый venv):
  - `../../.venv/bin/python main.py` → `curl localhost:8080/health`.
  - `curl -X POST localhost:8080/test -H 'Content-Type: application/json' -d '{"schema_url": "<url>", "base_url": "<url>"}'` — против доступного target API (например, поднятого рядом демо-API; можно и публичный пример `example.schemathesis.io` при наличии интернета).
  - Негативные кейсы: битый JSON → 400; недоступный `schema_url` → 400; отсутствие `schema_url` → 400.
- Docker: `docker build -f DockerFile -t schemathesis-svc . && docker run --rm -p 8080:8080 schemathesis-svc`, повторить `/health` и `/test`.
- НЕ добавлять в CI-тесты `prototype/tests`, зависящие от schemathesis: CI ставит только `pytest` (см. `ci.yml`), падение будет ложным.

## Риски и границы
- Внутренний engine-API схематезиса может меняться между версиями → версия зафиксирована в `requirements.txt`.
- Для реального прогона нужен доступный target API; иначе результат будет с `errors` (сетевые), но сервис отработает корректно.
- `from_url` делает сетевой запрос за схемой — задаём таймаут; недоступная схема → 400.
- `max_time` — сигнал `stream.stop()` из watchdog-потока (прерывание между сценариями, без форс-килла).
- Вне скоупа: UI, авторизация, генерация pytest-файлов из результатов (другие service tools), CI-джоб для сервиса.
