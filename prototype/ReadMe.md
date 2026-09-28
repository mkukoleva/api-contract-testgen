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
DEEPCODE_MODEL=deepseek-ai/DeepSeek-V4-Flash

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

В `prototype.runner.contracts` доступны:

- `RunConfig` — каталог тестов, каталог отчётов, адрес сервиса и таймаут.
- `TestResult`, `TestOutcome` — результат отдельного тестового случая.
- `RunResult`, `RunStatus` — результат прогона и метод `to_dict()` для JSON.

Структуры используют только стандартную библиотеку. Их импорт не загружает
настройки LLM. `completed` означает завершённый прогон, а не отсутствие
падающих тестов. Проверка синтаксиса URL не ограничивает сетевые соединения.

Минимальный runner реализован на этапе 3 ниже. Доверенные HTTP-фикстуры,
доступ только к API сервиса и подключение к генератору — следующие этапы.
Форматы и ответственность модулей описаны в
[ADR 0002](../docs/adr/0002-pytest-runner-contract.md).

Для работы в PyCharm выберите интерпретатор `.venv/Scripts/python.exe`
(Windows). Если IDE не видит локальный импорт `prototype.runner.contracts`,
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
происходят в новом Docker-контейнере с `--network none`, без ключей LLM.
Текущий режим — контрольные unit-тесты: Catalogue ему пока недоступен.
`RunConfig.base_url` должен быть `None`; другой режим отклоняется до запуска.

Из каталога `prototype`, при работающем Docker Desktop (Linux containers):

```powershell
# Один раз собрать образ; повторить после изменения Dockerfile или pytest_worker.py.
docker build -t api-contract-pytest-runner:step3 src/prototype/runner

# Выполнить сохранённый пример: ожидаются completed, exit_code: 0, passed: 3.
py -3.12 -m uv run --locked python -m prototype.runner ../benchmark/pytest-runner/example --output-dir .runner-results --timeout 30
```

Для своего набора замените путь примера каталогом с `test_*.py`. Внутри него
могут находиться `conftest.py` и вспомогательные модули. Монтируется только этот
каталог, поэтому не передавайте корень проекта с `.env` и другими секретами.
Тесты доступны только для чтения; временные файлы создавайте через `tmp_path`.
Каталог результатов должен быть отдельным, без вложенности в каталог тестов
или наоборот. Пути с пробелами поддерживаются.

В образе заранее установлены Python 3.14.7, pytest 9.1.1 и зависимости pytest.
Другие библиотеки, включая `requests`, пока не включены. Установка пакетов и
скачивание образа во время прогона отключены; отсутствие образа, Docker или
нужного импорта возвращается как диагностируемая ошибка. Сеть нужна для сборки
образа, но не для повторного запуска сохранённых тестов. LLM-токены не расходуются.

CLI печатает JSON и возвращает код `0` только для завершённого прогона с кодом
pytest `0`; падения, пустой набор, прерывание и ошибки возвращают код CLI `1`.
В JSON `exit_code` — исходный код **pytest**, а не Docker/CLI. `completed` может
содержать `failed` или `error`. В API:

```python
from prototype.runner.contracts import RunConfig
from prototype.runner.docker_runner import run_tests

result = run_tests(RunConfig(
    tests_dir="../benchmark/pytest-runner/example",
    output_dir=".runner-results",
    timeout_seconds=30,
))
print(result.to_dict())
```

Каждый прогон сохраняет `pytest.log` и поток событий `events.jsonl` в отдельном
подкаталоге `.runner-results/pytest-runner-...`; пути находятся в `report_paths`.
Полный JSON/Markdown-отчёт и метрика запускаемости относятся к этапу 6.
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

Проверено 2026-09-29: все **27 проверок runner прошли**, включая 8 сценариев
с реальным Docker. Пример через CLI вернул `completed`, код 0 и 3 passed.
Общий набор: 115 passed, 1 failed — существующая проверка производительности
`test_stress_large_contract_performance` в `test_mutation.py` превысила лимит
2 секунды. Она падала и до изменений этапа 3; модуль мутаций не изменялся.

На этапе 4 предстоит разрешить только адрес и порт Catalogue и проверить все
оговорённые обходы сетевых ограничений. Полностью пункт 2.2.6 ещё не закрыт.
