# Стенд для проверки pytest-runner: Catalogue

Это второй шаг работы над пунктом 2.2.6 ТЗ: отдельный тестируемый API и база
для будущего runner. Существующий Compose с Schemathesis не изменяется.
Здесь ещё нет запуска сгенерированных тестов или доказанной сетевой изоляции
runner. Скрипт готовности выполняется на хосте и проверяет только этот стенд.

## Состав

- Catalogue `0.3.5` и база с демонстрационными данными `0.3.0`.
- Образы зафиксированы по digest, архитектура — `linux/amd64`.
- API на хосте: `http://127.0.0.1:9911`, в сети Compose: `http://catalogue:8080`.
- Порт базы не публикуется. База подключена только к сети `database`;
  будущий runner должен подключаться только к `api`.
- Сети `api` и `database` имеют `internal: true`. Catalogue дополнительно
  подключён к обычной сети `host-access`, чтобы Docker публиковал порт на
  `127.0.0.1`. Эта сеть даёт самому сервису исходящий доступ; будущий runner
  к ней не подключается. Политика «тестам разрешён только адрес и порт API»
  и проверки обходов относятся к следующему этапу.
- У Catalogue оставлена только capability `NET_BIND_SERVICE`: в готовом образе
  файл `/app` имеет `cap_net_bind_service+ep`. Если убрать capability полностью,
  Linux запрещает выполнение файла (`exec /app: operation not permitted`), даже
  при выбранном порте 8080. `read_only` и `no-new-privileges` сохранены.
- База содержит только синтетические данные Sock Shop. Используется штатная
  демонстрационная инициализация образа, без продуктивных ключей и учётных данных.

## Запуск

Нужен работающий Docker Engine с Linux-контейнерами, Docker Compose с поддержкой
`--wait` и Python 3.11+. На Windows сначала откройте Docker Desktop и дождитесь
готовности Engine. Загрузка образов требует сети; внутри стенда LLM не используется.

Из корня репозитория:

```powershell
docker compose -f benchmark/catalogue/compose.yaml up -d --wait --wait-timeout 120
python benchmark/catalogue/check_ready.py --timeout 120
```

Если терминал уже открыт в каталоге `prototype`, используйте:

```powershell
docker compose -f ../benchmark/catalogue/compose.yaml up -d --wait --wait-timeout 120
py -3.12 -m uv run --locked python ../benchmark/catalogue/check_ready.py --timeout 120
```

Выполняйте вторую команду после успешного завершения первой. `--wait` проверяет
состояние контейнеров и healthcheck MySQL. Вторая команда дополнительно проверяет
реальную связь API с базой и наличие товаров. При временной ошибке она повторяет
проверку в пределах заданного срока; при неготовности возвращает JSON с причиной
и ненулевой код завершения. Redirect не выполняется, HTTP-proxy из окружения
не используется.

Ожидаемый вид успешного ответа (число товаров и время могут отличаться):

```json
{"status": "ready", "base_url": "http://127.0.0.1:9911", "product_count": 9, "elapsed_seconds": 0.02}
```

Скрипт требует `OK` для `catalogue` и `catalogue-db` в `/health`, а также
непустой список товаров с идентификаторами в `/catalogue`. HTTP 200 без этих
данных не считается готовностью. Это проверка стенда, а не покрытие контракта.

Если порт 9911 занят, задайте `CATALOGUE_PORT` и тот же порт в скрипте:

```powershell
$env:CATALOGUE_PORT = "9912"
docker compose -f benchmark/catalogue/compose.yaml up -d --wait --wait-timeout 120
python benchmark/catalogue/check_ready.py --base-url http://127.0.0.1:9912
```

## Диагностика и остановка

Из корня репозитория:

```powershell
docker compose -f benchmark/catalogue/compose.yaml ps
docker compose -f benchmark/catalogue/compose.yaml logs --tail 100 catalogue catalogue-db
docker compose -f benchmark/catalogue/compose.yaml down
```

Именованный том сохраняет базу между запусками. Для сброса **данных этого
демонстрационного стенда** используйте `down --volumes` вместо `down`:
следующий запуск заново загрузит исходные товары.

## Контракт и происхождение

`catalogue.swagger.json` — неизменённый полный контракт Swagger 2.0 с четырьмя
операциями: `GET /catalogue`, `GET /catalogue/{id}`, `GET /catalogue/size`,
`GET /tags`. Контракт сохранён локально, при проверке его не нужно скачивать.
Это дополнительный демонстрационный контракт; он не заменяет требование ТЗ
поддерживать OpenAPI 3.x. Известные несоответствия сервиса контракту не исправлялись.

- Источник: [microservices-demo/catalogue](https://github.com/microservices-demo/catalogue).
- Ревизия: `925e08ee17c28b94b87160e1ae5e03da7f61cf84`.
- [Исходный контракт](https://github.com/microservices-demo/catalogue/blob/925e08ee17c28b94b87160e1ae5e03da7f61cf84/api-spec/catalogue.json).
- SHA-256 контракта: `c52810441d6f1f5530f41d83dcc47b0bae2d3be494923aab649741a081b12672`.
- Лицензия исходного репозитория сохранена в `LICENSE.catalogue` (Apache 2.0).

Собственный `compose.yaml` запускает API на порту 8080 внутри контейнера,
выделяет сети и ожидание MySQL. Полный Sock Shop, LLM и Schemathesis для этого
стенда не требуются.

## Проверки без Docker

Из каталога `prototype`:

```powershell
py -3.12 -m uv run --locked python -m pytest src/prototype/tests/test_catalogue_stand.py -q
py -3.12 -m uv run --locked python -m pytest -q
```

Проверяются неготовая/отсутствующая база, пустые и повреждённые данные,
повтор после ошибки соединения, ограничение времени, JSON-диагностика,
запрет редиректов и игнорирование proxy. Один тест поднимает временный HTTP-сервер
только на loopback; внешние сервисы и LLM не используются.

На 2026-09-28: 21 проверка стенда и 38 прежних тестов прошли; Compose проходит
`docker compose config --quiet`. Подтверждён живой запуск на Docker Desktop:
база здорова, Catalogue доступен на `127.0.0.1:9911`, скрипт вернул
`status: ready` и `product_count: 9`. Это подтверждает второй шаг — готовность
стенда; выполнение pytest и изоляция будущего runner пока не реализованы.
