# Сервис Schemathesis

Обёртка принимает контракт в теле HTTP-запроса, выполняет Schemathesis и
возвращает фактические сценарии, ошибки и время. Она не генерирует pytest-код
через LLM. Локальные пути по `schema_url` по умолчанию запрещены.

Из корня репозитория, при работающем Docker Desktop (Linux containers):

```powershell
docker compose -f benchmark/catalogue/compose.yaml up -d --wait --wait-timeout 120
docker compose -f prototype/src/prototype/service_tools/docker-compose.yml up -d --build --wait
docker network connect pytest-runner-catalogue_api schemathesis-svc
```

Подключение к API-сети выполните один раз после создания контейнера Schemathesis.
Если контейнер уже подключён, повторять команду не нужно. Runner-сеть не используйте:
в ней должен оставаться только Catalogue. `base_url` разрешается внутри контейнера
Schemathesis; адрес Catalogue там — `http://catalogue:8080`.

Проверка без LLM из корня:

```powershell
py -3.12 -m uv run --project prototype --locked --no-sync python -m prototype.service_tools.selection benchmark/catalogue/catalogue.swagger.json --base-url http://catalogue:8080 --timeout 30 --output-dir "$env:TEMP/api-contract-tools"
```

API обёртки: `GET /health`, `POST /test`. Пример прямого запроса из корня
для Bash; в PowerShell используйте `curl.exe` с параметрами в одной строке:

```bash
curl -X POST 'http://127.0.0.1:8080/test?base_url=http://catalogue:8080&max_time=30' \
  -H 'Content-Type: application/yaml' \
  --data-binary @benchmark/catalogue/catalogue.swagger.json
```

HTTP 200 и `status: success` означают завершение движка. Проверяйте
`scenarios.failed`, `scenarios.errors` и `failures`: API может нарушать контракт.
Адаптер прототипа отдельно сообщает фактический итог и сохраняет JSON/Markdown.

Для остановки только обёртки:

```powershell
docker compose -f prototype/src/prototype/service_tools/docker-compose.yml down
```

Стенд Catalogue и его данные эта команда не удаляет.
