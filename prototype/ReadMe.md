### Установка и запуск агента

Проект использует Python 3.14 (см. `.python-version`) и `uv`.
Из корня репозитория:

```bash
cd prototype
touch .env
uv sync --locked
uv run --locked tester src/prototype/tests/fixtures/demo_openapi.yaml
```

`uv sync --locked` создаёт `.venv` и устанавливает зависимости из `uv.lock`
без его обновления. Активация окружения для `uv run` не требуется.
Перед запуском агента заполните `.env` по шаблону ниже и подготовьте
тестируемый API и сервис Schemathesis.

Для PowerShell, если `uv` не установлен или не найден в `PATH`, можно
использовать установленный Python 3.12. Из каталога `prototype`:

```powershell
py -3.12 -m pip install --user uv
py -3.12 -m uv sync --locked
```

Python 3.12 здесь запускает только `uv`; окружение проекта использует Python
3.14. Создайте `.env` в каталоге `prototype` через редактор. Для запуска агента:

```powershell
py -3.12 -m uv run --locked tester src/prototype/tests/fixtures/demo_openapi.yaml
```

### env-шаблон
```
DEEPCODE_API_KEY=<api-key>
DEEPCODE_BASE_URL=https://deepcode.ci.nsu.ru/api/v1
DEEPCODE_MODEL=deepseek-ai/DeepSeek-V4-Flash-0731

```
### запуск микросервисов для тестирования 
```bash
cd prototype/src/prototype/service_tools
docker compose up -d
```
### Цепочка вызова инструментов
- schemathesis_tool
- demo_api_test_tool
- generate_user_story_tool
- verify_user_story_tool
- написать отчёт по результатам в prototype/src/prototype/reports

`demo_api_test_tool` и `verify_user_story_tool` пока являются заглушками:
их статус успеха не подтверждает выполнение HTTP-проверок.

### Проверки самого прототипа

Из каталога `prototype`:

```bash
uv run --locked python -m pytest src/prototype/tests/test_runner_contracts.py -v
uv run --locked python -m pytest -q
uv run --locked tester --help
```

Эквивалент для PowerShell, если `uv` установлен через Python 3.12:

```powershell
py -3.12 -m uv run --locked python -m pytest -q
py -3.12 -m uv run --locked tester --help
```

После установки зависимостей текущим проверкам не нужны `.env`, ключи LLM,
доступ к внешней сети или работающий Docker. Проверка стенда использует временный
HTTP-сервер на loopback. На этапе 1 проверено **38 тестов**
с Python 3.14.7 и pytest 9.1.1: 37 проверок интерфейса runner и совместимости,
один существующий smoke-тест. Сборка агента проверяется с подменой внешних
зависимостей; это не сквозной прогон с настоящим LLM или Schemathesis.

### Интерфейс pytest-runner — этап 1

В `prototype.service_tools.runner.contracts` доступны:

- `RunConfig` — каталог тестов, каталог отчётов, адрес сервиса и таймаут.
- `TestResult`, `TestOutcome` — результат отдельного тестового случая.
- `RunResult`, `RunStatus` — результат прогона и метод `to_dict()` для JSON.

Структуры используют только стандартную библиотеку. Их импорт не загружает
настройки LLM. `completed` означает завершённый прогон, а не отсутствие
падающих тестов. Проверка синтаксиса URL не ограничивает сетевые соединения.

Runner реализован на этапах 3–5 ниже: доступны доверенные HTTP-фикстуры,
сетевые ограничения и проверка совместимости. Подключение к генератору впереди.
Форматы и ответственность модулей описаны в
[ADR 0002](../docs/adr/0002-pytest-runner-contract.md).

Для работы в PyCharm выберите интерпретатор `.venv/Scripts/python.exe`
(Windows). Если IDE не видит локальный импорт `prototype.service_tools.runner.contracts`,
отметьте каталог `src` как **Mark Directory as → Sources Root**.
Устанавливать сторонние пакеты для этого импорта не требуется.

### Стенд для pytest-runner — этап 2

Подготовлен отдельный [стенд Catalogue](../benchmark/catalogue/README.md):
API, база с демонстрационными данными, полный Swagger-контракт и скрипт
проверки готовности. Существующий Compose с Schemathesis не изменён.
Из каталога `prototype`, после запуска Docker Desktop:

```powershell
docker compose -f ../benchmark/catalogue/compose.yaml up -d --wait --wait-timeout 120
py -3.12 -m uv run --locked python ../benchmark/catalogue/check_ready.py --timeout 120
```

Скрипт должен вернуть `status: ready`. При завершении этапа 2 прошли **59 тестов**
прототипа. Проверки этапа 3 описаны ниже.

### Минимальный pytest-runner — этап 3

Runner принимает готовый каталог тестов и возвращает `RunResult`. Сбор и запуск
происходят в новом Docker-контейнере без ключей LLM. В offline-режиме
`RunConfig.base_url=None` используется `--network none`. Для HTTP-тестов
доступен service-режим этапа 4, описанный ниже.

Из каталога `prototype`, при работающем Docker Desktop (Linux containers):

```powershell
# Один раз собрать образ; повторить после изменения файлов runner.
docker build -t api-contract-pytest-runner:step5 src/prototype/service_tools/runner

# Выполнить сохранённый пример: ожидаются completed, exit_code: 0, passed: 3.
py -3.12 -m uv run --locked python -m prototype.service_tools.runner ../benchmark/pytest-runner/example --output-dir .runner-results --timeout 30
```

Для своего набора замените путь примера каталогом с `test_*.py`. Внутри него
могут находиться `conftest.py` и вспомогательные модули. Монтируется только этот
каталог, поэтому не передавайте корень проекта с `.env` и другими секретами.
Тесты доступны только для чтения; временные файлы создавайте через `tmp_path`.
Каталог результатов должен быть отдельным, без вложенности в каталог тестов
или наоборот. Пути с пробелами поддерживаются.

В образе заранее установлены Python 3.14.7, pytest 9.1.1 с зависимостями и
`requests` (для фикстур `api_client`). Другие библиотеки не включены. Установка
пакетов и скачивание образа во время прогона отключены; отсутствие образа, Docker
или нужного импорта возвращается как диагностируемая ошибка. Сеть нужна для
сборки образа, но не для повторного запуска сохранённых тестов. LLM-токены не
расходуются.

CLI печатает JSON и возвращает код `0` только для завершённого прогона с кодом
pytest `0`; падения, пустой набор, прерывание и ошибки возвращают код CLI `1`.
В JSON `exit_code` — исходный код **pytest**, а не Docker/CLI. `completed` может
содержать `failed` или `error`. В API:

```python
from prototype.service_tools.runner.contracts import RunConfig
from prototype.service_tools.runner.docker_runner import run_tests

result = run_tests(RunConfig(
    tests_dir="../benchmark/pytest-runner/example",
    output_dir=".runner-results",
    timeout_seconds=30,
))
print(result.to_dict())
```

Каждый прогон сохраняет `pytest.log` и поток событий `events.jsonl` в отдельном
подкаталоге `.runner-results/pytest-runner-...`; пути находятся в `report_paths`.
С этапа 6 здесь же автоматически сохраняются `report.json` и `report.md`.
Метрики качества runner не рассчитывает.
После прогона контейнер удаляется. Общий таймаут включает создание контейнера,
импорт, сбор и выполнение; очистка может занять ещё до 10 секунд. При таймауте
сохраняются результаты тестов, уже завершивших teardown. Незавершённый тест не
получает выдуманный результат `passed`.

Проверки этапа 3 (из `prototype`):

```powershell
# Быстрые проверки без Docker; контейнерные сценарии будут skipped.
py -3.12 -m uv run --locked python -m pytest src/prototype/tests/test_pytest_runner.py -q

# Полная проверка runner с заранее собранным образом.
$env:RUN_RUNNER_DOCKER_TESTS = "1"
py -3.12 -m uv run --locked python -m pytest src/prototype/tests/test_pytest_runner.py -q
Remove-Item Env:RUN_RUNNER_DOCKER_TESTS
```

Контрольные сценарии проверяют passed/failed/skipped, ошибки setup/teardown,
ошибки импорта и синтаксиса, пустой набор, прерывание, аварийный выход, таймаут
импорта и зависание с дочерним процессом. Обычные тесты запускают на хосте только
заранее заданные контрольные строки Python; публичный runner запускает наборы
исключительно в Docker. Команды не вызывают генератор и не меняют стенд Catalogue.

Итоговая проверка этапов 1–5 и команды для её повторения приведены ниже.

### Автоматическая проверка совместимости pytest — этап 5

Перед каждым запуском runner проверяет, что набор действительно совместим
с pytest, тремя стадиями:

1. **Проверка синтаксиса без исполнения кода** — на хосте, до Docker.
   Каждый `*.py` в каталоге тестов разбирается через `ast.parse`;
   `tokenize.open` учитывает BOM и объявленную кодировку Python.
   Контейнер при ошибке не создаётся. Точная причина — `path:line:column`.
   Это жёсткий барьер: `collection_error` + структурированные
   `compatibility_issues` + артефакт `precheck.json`.
2. **Сбор в изоляции** — worker запускает `pytest --collect-only` внутри
   того же контейнера и сетевой политики (в service-режиме после policy-
   набора). Сбор импортирует Python-модули, поэтому выполняется только
   в изоляции; ошибки импорта записываются в `collect_events.jsonl`.
3. **Запуск** — тела тестов выполняются только после успешного сбора.

Каждая фаза запускается в отдельном процессе Python внутри одного контейнера:
имена модулей policy-набора не конфликтуют с пользовательскими тестами.
При сборе и выполнении модули импортируются заново; побочные эффекты импорта
могут выполняться дважды, всегда под одними сетевыми ограничениями.
Незавершённый сбор, включая `os._exit(0)` и `pytest.exit(returncode=0)`,
блокирует фазу выполнения.

Очевидные исправления (`auto_fixable=True` в `compatibility_issues`)
помечаются для будущего модуля постобработки; сам модуль и применение фиксов —
следующий этап. Корректные файлы (в том числе
`../benchmark/pytest-runner/example`) проходят конвейер без ручных правок;
некорректные дают понятный результат (`error`-статус, точная причина).

Образ пересобирается после изменения worker:

```bash
docker build -t api-contract-pytest-runner:step5 src/prototype/service_tools/runner
```

Проверки этапа 5 (из `prototype`), как и раньше:

```bash
.venv/bin/python -m pytest src/prototype/tests/test_pytest_compat.py -q
.venv/bin/python -m pytest src/prototype/tests/test_pytest_runner.py -q
# Полная проверка runner с заранее собранным образом step5:
RUN_RUNNER_DOCKER_TESTS=1 .venv/bin/python -m pytest src/prototype/tests/test_pytest_runner.py -q
```

### Сетевая изоляция до API — этап 4

Этап 4 разрешает тестам доступ только к адресу и порту Catalogue. Стенд
предоставляет отдельную internal-сеть `runner` (имя
`pytest-runner-catalogue_runner`), в которой находится ровно один контейнер —
Catalogue. Запуск против стенда происходит так:

```powershell
# Стенд должен быть поднят (см. benchmark/catalogue/README.md), образ собран:
docker build -t api-contract-pytest-runner:step5 src/prototype/service_tools/runner

py -3.12 -m uv run --locked python -m prototype.service_tools.runner <tests_dir> `
  --output-dir .runner-results --timeout 60 `
  --base-url http://catalogue:8080 `
  --network pytest-runner-catalogue_runner `
  --blocked-network pytest-runner-catalogue_database
```

- Preflight через `docker network inspect` подтверждает, что в сети runner ровно
  один контейнер; иначе запуск не выполняется. Имя `catalogue` фиксируется в
  hosts контейнера на фактический IPv4 (`--add-host`), поэтому `base_url` должен
  быть обычным именем, а не IP.
- `--blocked-network` (повторяемый) собирает IPv4 всех контейнеров сети —
  обычно сети `database` — и вместе с gateway сети runner передаёт их в env
  контейнера `RUNNER_UNREACHABLE`. Другие переменные: `RUNNER_BASE_URL`,
  `RUNNER_API_IP`, `RUNNER_GATEWAY_IP`, `RUNNER_TIMEOUT_SECONDS`.
- Worker сначала исполняет доверенный policy-набор `/opt/runner/policy_tests`
  (health Catalogue, DNS pinned, недоступность интернета/host/базы/IPv6,
  отказ внешнего DNS, запрет raw-socket, наследование ограничений дочерними
  процессами, отказ следовать редиректам, отсутствие HTTP-proxy из среды).
  Если policy-набор не подтвердил изоляцию, сгенерированные тесты
  даже не импортируются, а результат — `infrastructure_error`. При исчерпании
  лимита времени сохраняется статус `timeout`; выполнение также прекращается.
- В образ включаются `requests` и фикстуры `base_url`/`api_client` из
  `fixtures.py` (`requests.Session` без proxy из среды, с HTTP-таймаутом и
  отключённым следованием редиректам).
- Offline-режим (`base_url=None`) не изменился и policy-набор не исполняет.

Проверки этапа 4 (из `prototype`), без Docker дополнительно покрываются
юнит-тестами парсинга сети, формирования аргументов `docker create` и чтения
policy-событий; сценарии с реальным Docker — флагом `RUN_RUNNER_DOCKER_TESTS=1`.

До запуска worker доверенный `network_bootstrap.py` устанавливает правила
`iptables`/`ip6tables` в сетевом пространстве контейнера. Разрешён только TCP
к фиксированному IP и порту API и локальный TCP `127.0.0.1` внутри runner.
Другие порты API, интернет, хост/gateway, база, соседние контейнеры, UDP,
внешний DNS (включая Docker DNS) и IPv6 блокируются ядром. Ограничение
действует для прямых сокетов, редиректов и дочерних процессов.

Только bootstrap кратковременно работает от root с capabilities для установки
правил и смены пользователя. До импорта pytest и пользовательских файлов
`setpriv` переключает процесс на UID/GID 65534, очищает все capabilities,
включая bounding set, и запрещает получение новых привилегий.
Ошибка настройки останавливает запуск. Firewall хоста не изменяется.
Нужен Docker с Linux-контейнерами и поддержкой этих операций; запрещённый
внешней политикой `NET_ADMIN` приведёт к ошибке окружения, а не обходу защиты.

Policy-набор проверяет окружение при каждом service-прогоне. Любой skip,
xfail, ошибка или прерывание не допускает пользовательские импорты.
Фикстуры загружаются по доверенному пути даже при `python -I`; `api_client`
возвращает исходный HTTP 3xx, включая вызов с `allow_redirects=True`.
Самостоятельный HTTP-клиент также ограничен firewall.

### Полная проверка одной командой

Все проверки собираются в одну команду из корня репозитория. Скрипт
использует только стандартную библиотеку и никогда не вызывает LLM:

```bash
# Полный цикл: окружение -> стенд -> готовность -> pytest -> сохранённые
# наборы через runner -> отчёты -> удаление стенда и временных ресурсов.
uv run --project prototype --locked --no-sync python tools/verify.py

# Быстрый режим без стенда и Docker (юнит-тесты + offline-пример runner):
uv run --project prototype --locked --no-sync python tools/verify.py --offline
```

Разовые действия до первого запуска (повторные запуски их не требуют):

```bash
cd prototype && uv sync --locked   # один раз создать окружение (Python 3.14.7)
docker build -t api-contract-pytest-runner:step5 prototype/src/prototype/service_tools/runner  # один раз собрать образ
```

Повторные запуски не требуют правки файлов, `.env`/ключей LLM и установки
зависимостей: тестовые шаги идут с `--no-sync` (окружение только
синхронизируется отдельным шагом), образ и стенд не пересобираются.

Что делает команда:

1. **Проверка окружения**: интерпретатор `3.14.7` (по `prototype/.python-version`),
   наличие `uv`, для полного режима — Docker, валидность
   `benchmark/catalogue/compose.yaml` и наличие образа
   `api-contract-pytest-runner:step5`.
2. **Стенд**: `docker compose -f benchmark/catalogue/compose.yaml up -d --wait`
   и ожидание готовности через `benchmark/catalogue/check_ready.py`
   (health catalogue и базы, непустой список товаров).
3. **Тесты**: `pytest -q` с флагами `RUN_RUNNER_DOCKER_TESTS=1
   RUN_RUNNER_CATALOGUE_TESTS=1` и `--junitxml` в каталог прогона.
4. **Сохранённые наборы через runner**: offline-пример
   `benchmark/pytest-runner/example` и коммитный снимок реальной генерации
   `benchmark/pytest-runner/saved-sets/catalogue-2026-10-05` в service-режиме
   (`--base-url http://catalogue:8080 --network ... --blocked-network ...`).
   Платная генерация не выполняется.
5. **Отчёт**: `.verify-runs/<UTC-идентификатор>/` — `summary.json`,
   `junit.xml` и отчёты runner (`report.json`, `report.md`).
6. **Очистка**: `docker compose ... down --volumes` (стенд + именованный том)
   и `.pytest_cache`; отчёты остаются. `--keep-stand` оставляет стенд.

Флаги: `--offline`, `--skip-runner-set` (только юнит-тесты, без Docker),
`--keep-stand`, `--saved-set PATH`, `--readiness-timeout`, `--runner-timeout`.
Код возврата `0` — только если все шаги, включая очистку, прошли.

Ограничение окружения: полный режим требует, чтобы хост разрешал новые
соединения из Docker-мостов на адреса шлюзов (штатное поведение Docker
Desktop и GitHub-hosted runner). На хостах с включённым брандмауэром (например
`ufw` с политикой `DROP` на вход) интеграционный тест
`test_firewall_blocks_live_destinations_before_import` не сможет на контрольном
шаге дотянуться до слушателя хоста через gateway — этому окружению нужна
разрешающая запись для мостов Docker в `ufw`.

### Проверка этапов 1–5: детали

В полном режиме команда выполняет тот же состав, что и ручной процесс ниже
(сохранён для справки и Windows):

```powershell
cd prototype
# Один раз: py -3.12 -m uv sync --locked (uv из Python 3.12)
docker build -t api-contract-pytest-runner:step5 src/prototype/service_tools/runner
docker compose -f ../benchmark/catalogue/compose.yaml up -d --wait --wait-timeout 120
py -3.12 -m uv run --locked python ../benchmark/catalogue/check_ready.py --timeout 120
$env:RUN_RUNNER_DOCKER_TESTS = "1"
$env:RUN_RUNNER_CATALOGUE_TESTS = "1"
py -3.12 -m uv run --locked python -m pytest -q
Remove-Item Env:RUN_RUNNER_DOCKER_TESTS,Env:RUN_RUNNER_CATALOGUE_TESTS
```

Выполняйте команды последовательно после успешного завершения предыдущей.
`test_runner_isolation.py` проверяет реальный HTTP-запрос к Catalogue и
блокировку работающих TCP/UDP-серверов на запрещённых адресах. Контрольный
контейнер сначала подтверждает доступность этих серверов без firewall:
закрытый порт не выдаётся за доказательство изоляции. Проверяются ограничения
уже при импорте, отсутствие capabilities, запрет изменения firewall и
наследование ограничений дочерним процессом. Временные контейнеры и сети
удаляются; стенд Catalogue остаётся до его явной остановки.

13 policy-тестов пропускаются при общем запуске на хосте намеренно: они
выполняются внутри runner-контейнера при каждом service-прогоне. Без флагов
дополнительно пропускаются Docker- и Catalogue-сценарии. Все проверки
используют сохранённый код; LLM не вызывается.

Проверено 2026-10-05 на Windows / Docker Desktop (Linux containers),
Python 3.14.7 и pytest 9.1.1: **165 passed, 13 skipped**, без ошибок,
с обоими флагами интеграционных проверок. Policy-набор прошёл внутри
контейнеров. Стенд вернул `status: ready`, 9 товаров; сохранённый пример
через CLI — `completed`, `exit_code: 0`, 3 passed. Это проверка этапов 1–5
на готовых pytest-файлах, без генерации и self-repair.

### CI и сохранённые наборы (без платной генерации)

Обычный CI (`.github/workflows/ci.yml`, PR/push) использует сохранённые
наборы и никогда не вызывает генерацию через LLM (`.env` и ключи не нужны):

- job `unit` — `tools/verify.py --offline --skip-runner-set`: юнит-тесты
  проекта без Docker и стенда (на 2026-10-05 — 339 passed, 12 skipped);
- job `saved-sets` — сборка образа runner, стенд Catalogue и полный
  `tools/verify.py` с интеграционными флагами и прогоном сохранённых наборов.

Полный прогон на `main` и по `workflow_dispatch` — `.github/workflows/verify.yml`.

Сохранённые наборы живут в `benchmark/pytest-runner/saved-sets/`
(подробности — в `README.md` каталога): это коммитный снимок версии реальной
генерации `2026-10-05_163817`, приведённый к документированной форме контракта
(`/catalogue/size` → `{"size": int}`, `/tags` → `{"tags": [string]}`). Сырой
вывод генерации остаётся в `prototype/generated/` (в gitignore).

Версии Python согласованы: `prototype/.python-version` = `3.14.7`,
`actions/setup-python` читает этот файл, образ runner — `python:3.14.7-slim`
(фиксированный digest). Пути тестов задаются один раз в `tools/verify.py`
и переиспользуются CI из корня репозитория.

### Сохранение отчётов — этап 6

Этот этап реализует сохранение результатов runner в рамках 2.1.10 и 2.2.10 ТЗ.
Он не рассчитывает метрики качества: `evaluate/metrics.py` принадлежит
отдельному модулю команды и не вызывается. Поле `metrics` в JSON равно `null`,
в Markdown явно указано «Метрики не предоставлены». Это не нулевые значения.
Имеющаяся сводка passed/failed/error/skipped — результаты pytest, а не оценка
качества генератора. Self-repair и вызовы LLM не добавлены.

Команда запуска прежняя, из каталога `prototype`:

```powershell
py -3.12 -m uv run --locked python -m prototype.service_tools.runner ../benchmark/pytest-runner/example --output-dir .runner-results --timeout 30
```

В `.runner-results/pytest-runner-<UTC-дата-и-время>-<уникальный-ID>/` появляются:

- `report.json` — машинный формат, `schema_version: 1`.
- `report.md` — читаемый отчёт с результатами и диагностикой.
- Имеющиеся `pytest.log`, `events.jsonl`, `collect_events.jsonl`,
  `policy_events.jsonl` или `precheck.json` — когда соответствующая фаза состоялась.

Пути отчётов возвращаются в прежнем `RunResult.report_paths` под ключами `json`
и `markdown`, поэтому одинаково доступны через CLI и Python API. Старые
прогоны не перезаписываются. Сборка нового Docker-образа для этапа 6 не нужна:
отчёты формируются на хосте после завершения запуска и очистки контейнера.

JSON содержит `run_id`, `started_at`, `finished_at` (UTC), параметры запуска
`input`, список входных Python-файлов `input_files`, прежний `RunResult` в
`result` и заглушку `metrics`. Содержимое тестов и переменные окружения не
копируются. Имена файлов не подменяют полный перечень тестовых случаев:
при прерванном сборе/таймауте результаты могут быть неполными. Падение assert
сохраняется как `failed`; отчёт не объявляет его автоматически дефектом сервиса
или ошибкой генерации. Структурированные ошибки совместимости сохраняются отдельно.

Отчёты сохраняются и при синтаксической ошибке, отсутствии Docker/каталога
тестов, ошибке импорта, пустом наборе и таймауте. Если output-путь недопустим
(например, вложен в тесты) или запись невозможна, runner не обходит ограничение:
возвращает ошибку. Сбой записи отчёта даёт `infrastructure_error`, сохраняя
полученные результаты и диагностику. Каждый файл публикуется атомарно;
при сбое диска может остаться только один из двух файлов. Успех записи пары
подтверждается обоими ключами в возвращённом `report_paths`.

Проверки без Docker:

```powershell
py -3.12 -m uv run --locked python -m pytest src/prototype/tests/test_runner_reports.py -q
```

Полный набор с Docker запускается командами раздела проверки этапов 1–5 выше;
он дополнительно проверяет отчёты успешных/падающих тестов, ошибок сбора,
пустого набора и таймаутов. Генерация и метрики не запускаются.

Проверено 2026-10-05 после добавления отчётов: **171 passed, 13 skipped**
с включёнными Docker- и Catalogue-проверками. 13 пропусков относятся к
policy-набору на хосте; внутри runner он прошёл. Сохранённый CLI-пример
вернул 3 passed и создал оба отчёта. Повторные запуски, сбой записи отчёта
и сохранение диагностики ранних ошибок покрыты отдельными проверками.

### Дальнейшая интеграция

Для приёмки 2.2.6 остаётся передать runner реальный набор от генератора
и подтвердить его выполнение в проверенной изоляции. Полный отчёт о прогоне
генерации по 2.1.10 затем должен объединить эти результаты с данными генератора
и доступными метриками от ответственного модуля команды. Текущий отчёт описывает
запуск уже готовых pytest-файлов; ошибки самой генерации runner не наблюдает.
Self-repair не является требованием 2.2.6 и не входит в этот этап.

### Сохранение сгенерированных тестов (п. 2.1.6 ТЗ)

Модуль `prototype.storage` сохраняет pytest-код генератора как
неизменяемую версию с `manifest.json`:

```python
from prototype.storage import GeneratedFile, SaveRequest, save_test_suite

saved = save_test_suite(SaveRequest(
    contract_path="src/prototype/tests/fixtures/demo_openapi.yaml",
    files=(GeneratedFile("test_catalogue.py", code),),
    model="deepseek-ai/DeepSeek-V4-Flash",
))
print(saved.tests_dir)   # generated/demo-catalogue-api/2026-09-28_153012/tests
```

Файлы с синтаксической ошибкой попадают в `rejected/`, остальные
проверяются `pytest --collect-only`. Каталог по умолчанию —
`prototype/generated`, переопределяется `TESTGEN_OUTPUT_DIR`. Для агента
есть tool `save_tests_tool`; к `build_agent` он будет подключён вместе
с генератором тестов. Сбор выполняет код верхнего уровня тестовых модулей
и не является изоляцией. Подробности — в
[ADR 0003](../docs/adr/0003-generated-tests-storage.md).

### Генерация тестов и ограниченный self-repair (пп. 2.1.5, 2.1.2 ТЗ)

Модуль `prototype.generator` реализует конвейер «реальная генерация +
ограниченный self-repair»: один структурированный LLM-вызов генерирует набор
pytest-файлов по контракту; постобработка нормализует код и статически
проверяет его (`ast`, без исполнения); готовый набор прогоняется через
изолированный pytest-runner; упавшие `test_*`-функции (или файлы при ошибках
сбора) могут быть ограниченно отремонтированы. Программный API:

```python
from prototype.generator import GenerationSettings, run_generation_pipeline

run = run_generation_pipeline(GenerationSettings(
    contract_path="../benchmark/catalogue/catalogue.swagger.json",
    base_url="http://catalogue:8080",
    network="pytest-runner-catalogue_runner",
    blocked_networks=("pytest-runner-catalogue_database",),
    output_dir=".pipeline-runs",
))
print(run.status, run.attempts, run.to_dict()["tokens"])
```

Границы (ADR 0004):

- ремонтируется только код генератора: синтаксис, импорты, пути/методы,
  выбор входных данных; успешные тесты повторно не генерируются — чинится
  только упавшая функция;
- недоступность Docker/API, таймаут и прерывание не вызывают LLM;
- фактический ответ, противоречащий контракту (недокументированный статус),
  — это `suspected_defect`: assert не ослабляется, ремонт не выполняется;
- лимиты: до 3 попыток ремонта (`TESTGEN_MAX_REPAIR_ATTEMPTS`) и 30 000
  выходных токенов (`TESTGEN_REPAIR_TOKEN_BUDGET`); повтор одинаковой ошибки
  останавливает цикл;
- пейлоад ремонта компактен: функция ≤ 30 строк, фрагмент контракта ≤ 1500
  символов, хвост диагностики ≤ 2000 символов.

Каждая попытка сохраняется отдельной неизменяемой версией через
`save_test_suite(..., collect=False)` (сбор — за runner). Для агента добавлен
тонкий tool `generate_tests_tool`; в `build_agent` он будет подключён отдельно.
Offline-тесты конвейера используют подменённые LLM и runner; живой прогон с
настоящим стендом — см. раздел «Полная проверка этапов 1–5». Подробности —
в [ADR 0004](../docs/adr/0004-generation-self-repair.md).

Живая проверка 2026-10-05 (стенд Catalogue + образ `api-contract-pytest-runner:step5`
+ реальная модель `deepseek-ai/DeepSeek-V4-Flash-0731`): генерация и изолированный
прогон в service-режиме прошли; недокументированный 500 на `/catalogue/1`
корректно классифицирован `suspected_defect` без вызова LLM; направленный ремонт
упавшей функции выполнен реальной моделью (assert не ослаблен) и подтверждён
повторным прогоном. Оффлайн-набор: 339 passed, 12 skipped.
