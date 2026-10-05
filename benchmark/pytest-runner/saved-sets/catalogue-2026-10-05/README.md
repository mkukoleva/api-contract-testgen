# Сохранённый набор: catalogue (LLM-генерация от 2026-10-05)

Снимок набора pytest-тестов, сгенерированных реальной LLM-моделью по контракту
Catalogue. Каталог зафиксирован в репозитории, чтобы CI и `tools/verify.py`
могли прогонять результат реальной генерации **без вызова LLM** (без ключей,
токенов и платной генерации): сам прогон использует только изолированный
pytest-runner и живой стенд Catalogue.

- Модель: `deepseek-ai/DeepSeek-V4-Flash`
- Контракт: `benchmark/catalogue/catalogue.swagger.json`
  (SHA-256 `c52810441d6f1f5530f41d83dcc47b0bae2d3be494923aab649741a081b12672`)
- Дата генерации: 2026-10-05, версия `2026-10-05_163817`
  (`prototype/generated/catalogue-resources/2026-10-05_163817/`)
- Файлы: `tests/test_catalogue.py` и `tests/test_tags.py` (5 тестов: `/catalogue`,
  `/catalogue/{id}` c динамическим id, `/catalogue/size`, `/tags`).

Почему эта версия, а не `2026-10-05_163902`: более поздняя версия жёстко
проверяет `GET /catalogue/1`, а живой сервис на числовой id отдаёт
недокументированный 500 (известное расхождение сервиса с контрактом).
Рабочие id товаров — UUID, поэтому снапшот берёт первый id из `/catalogue`
и проверяет `GET /catalogue/{uuid}` (200). Так CI прогоняет сохранённый
результат реальной генерации без вызовов LLM и без падений на дефекте сервиса.

Отклонения снимка от сырого вывода генератора (сырьё лежит в
`prototype/generated/catalogue-resources/2026-10-05_163817/`, не в git):

- по контракту `/catalogue/size` возвращает объект `{"size": int}`,
  а `/tags` — объект `{"tags": [string]}`; сгенерированные тесты ошибочно
  проверяли скаляр `int` и список напрямую. В снимке проверки приведены
  к документированной форме (`response.json()["size"]`, `response.json()["tags"]`)
  — тот же стиль, который применяет ограниченный self-repair (ADR 0004);
- `test_catalogue_size_matches_list_length` получил тот же фикс
  (`size_response.json()["size"]`).

Остальной код снимка — дословный вывод генератора. Сама возможность
зафиксировать проходящий набор без вызова LLM — цель этого каталога.

Прогон (service-режим, из корня репозитория):

```bash
uv run --project prototype --locked --no-sync python -m prototype.service_tools.runner \
  benchmark/pytest-runner/saved-sets/catalogue-2026-10-05/tests \
  --output-dir .runner-results --timeout 60 \
  --base-url http://catalogue:8080 \
  --network pytest-runner-catalogue_runner \
  --blocked-network pytest-runner-catalogue_database
```

Перед прогоном должен быть поднят стенд (`docker compose -f benchmark/catalogue/compose.yaml up -d --wait`)
и собран образ runner (`docker build -t api-contract-pytest-runner:step5 prototype/src/prototype/service_tools/runner`).
Обычно всё это делает команда целиком: `tools/verify.py`.
