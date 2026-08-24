# Бенчмарки и метрики

> Команда: Соколов Богдан, Бычинская Дарья, Крист Милена

Данный раздел посвящен оценке качества автоматической генерации тестов в рамках проекта `api-contract-testgen`.

## Используемые метрики

Для сравнения различных подходов (детерминированных, LLM и гибридных) используется следующий набор метрик:

|#|Метрика|Что измеряет|Приоритет|Инструменты измерения|
|---|---|---|---|---|
|1|**Test Suite Size**|Объём тестового набора|Средний|CLI вывод, report.json|
|2|**Generation Time**|Скорость работы генератора|Высокий|`time`, timestamp|
|3|**Line Coverage**|Доля выполненных строк кода|Средний|`coverage.py`, `pytest-cov`|
|4|**Branch Coverage**|Доля пройденных ветвей логики|Высокий|`coverage.py`, `pytest-cov`|
|5|**Input Diversity**|Широта входных значений|Средний|Анализ логов, энтропия строк|
|6|**Mutation Score**|Способность ловить мутантов|**Критический**|API Mutation testing (Schemathesis / кастомные скрипты)|
|7|**Fault Detection Rate**|Обнаружение известных дефектов|Высокий|Seeded defects|
|8|**Contract Coverage**|Покрытие элементов OpenAPI контракта|**Критический**|Microcks (Conformance Score)|
|9|**Runnability**|Доля тестов без ручной правки|**Критический**|Запуск в изолированном окружении (Docker)|
|10|**Generation Cost**|Токены / время / $ на генерацию|Высокий|Обёртка над LLM API (tiktoken)|

> Подробное описание каждой метрики и формулы расчета находятся в [docs/report_metrics.md](../docs/report_metrics.md).

## Автоматический сбор метрик

Для сбора метрик из отчётов инструментов используется скрипт:

```bash
python benchmark/collect_metrics.py --tool schemathesis --report report.ndjson
```

Подробнее см. [benchmark/README.md](README.md).

## Предварительные результаты (Baseline)

Ниже представлены предварительные результаты замеров для существующих решений на тестовом микросервисе `catalogue` (Sock Shop).

|Метрика|Schemathesis|Microcks|EvoMaster|APITestGenie (LLM)|
|---|---|---|---|---|
|**Test Suite Size**|15 тестов|4 операции|9 тестов|_В процессе_|
|**Generation Time**|~0.19 с|~0.05 с|30 с|_В процессе_|
|**Line Coverage**|Н/Д (black-box)|Н/Д (black-box)|Н/Д (black-box)|Н/Д (black-box)|
|**Branch Coverage**|Н/Д (black-box)|Н/Д (black-box)|Н/Д (black-box)|Н/Д (black-box)|
|**Input Diversity**|Высокая (hypothesis)|Низкая (examples)|Средняя (search)|_В процессе_|
|**Mutation Score**|_В процессе_|_В процессе_|_В процессе_|_В процессе_|
|**Fault Detection Rate**|100% (500 err)|100%|100%|_В процессе_|
|**Contract Coverage**|Частично|100% (4/4 PASS)|Частично|_В процессе_|
|**Runnability**|100%|100%|~90%|_В процессе_|
|**Generation Cost**|$0, 0.19 с|$0, ~0.05 с|$0, 30 с|_В процессе_|

## Структура результатов

Все сырые данные и сводные таблицы находятся в папке `benchmark/results/`:

```
benchmark/results/
├── schemathesis/
│   └── schemathesis_YYYYMMDD.json
├── microcks/
│   └── microcks_YYYYMMDD.json
├── evomaster/
│   └── evomaster_YYYYMMDD.json
└── summary.json
```
