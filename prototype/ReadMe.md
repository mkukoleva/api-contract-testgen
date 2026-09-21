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
сетевые запросы или работающий Docker. На этапе 1 проверено **38 тестов**
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

Сам runner, доверенные фикстуры, Docker-изоляция и подключение к генератору
относятся к следующим этапам. Форматы и ответственность модулей описаны в
[ADR 0002](../docs/adr/0002-pytest-runner-contract.md).

Для работы в PyCharm выберите интерпретатор `.venv/Scripts/python.exe`
(Windows). Если IDE не видит локальный импорт `prototype.runner.contracts`,
отметьте каталог `src` как **Mark Directory as → Sources Root**.
Устанавливать сторонние пакеты для этого импорта не требуется.
