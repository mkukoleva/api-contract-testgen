# Отчёт по выбору инструментов API/contract testing для ИИ-агента

## 1. Окружение экспериментов

Эксперименты проводились на одном семействе тестовых микросервисов Sock Shop, но Microcks и связка Schemathesis/EvoMaster проверялись в немного разных локальных конфигурациях. Это важно: результаты Microcks подтверждают его возможности, но не являются прямым benchmark по скорости или числу найденных дефектов против Schemathesis/EvoMaster.

| Компонент | Schemathesis / EvoMaster | Microcks |
|---|---|---|
| ОС | macOS, Apple Silicon (ARM64) | macOS, Apple Silicon (ARM64) |
| Python | 3.12.10 | 3.12.10 |
| Python environment | Virtualenv (`.venv`) | Virtualenv (`.venv`) |
| Docker | 29.4.0 | 29.4.0 |
| Docker Compose | v5.1.1 | v5.1.1 |
| Тестируемая система | Sock Shop | Sock Shop |
| Микросервис | Catalogue | Catalogue |
| Контракт | Swagger 2.0, готовый `api-spec/catalogue.json` | OpenAPI 3.0, созданный для эксперимента на основе реальных ответов |
| Endpoint набора | `/catalogue`, `/catalogue/{id}`, `/catalogue/size`, `/tags` | `/catalogue`, `/catalogue/{id}`, `/tags`, `/health` |
| Адрес Catalogue с хоста | `http://localhost:8080` | `http://localhost:8082` |
| Проверенная версия инструмента | Schemathesis 4.24.3; EvoMaster 6.1.1 | Microcks 1.11.0 nightly |
| IDE из исходных отчётов | PyCharm | VS Code |

---

## 2. Цель исследования

Задача исследования — выбрать инструменты, которые можно подключить к ИИ-агенту для автоматического тестирования микросервисов при минимальном ручном вводе и при минимальном расходе LLM-токенов.

Основной вход для API/integration-части:

```text
API-контракт
+ адрес работающего сервиса
+ по возможности минимум дополнительной информации
```

Рассмотренные кандидаты:

- **Schemathesis**
- **Microcks**
- **EvoMaster**
- **RESTler**
- **Pact**

Важно разделять две вещи:

1. **Инструмент как tool агента** — агент вызывает готовый движок через CLI/API, получает структурированный результат и принимает следующее решение.
2. **Инструмент как архитектурный шаблон** — мы можем не использовать его непосредственно, но перенять полезную идею: dependency graph, replay, разделение fault/success tests, LLM-augmentation и т. п.

Также важно, что эти пять инструментов в основном закрывают **API/system/integration/contract testing**. Они не заменяют полноценный source-level unit testing и mutation testing. Настоящие unit-тесты требуют информации о внутренних единицах кода, а mutation testing требует исходников, сборки и существующего test runner.

---

## 3. Что представляет собой каждый кандидат и почему он был замечен

| Инструмент | Что это | Как работает в базовом сценарии | Почему попал в кандидаты |
|---|---|---|---|
| **Schemathesis** | Генератор property-based API-тестов из OpenAPI/GraphQL | Читает схему, генерирует запросы, выполняет Examples/Coverage/Fuzzing/Stateful и проверяет ответы | Очень маленький вход: контракт + URL; быстрый запуск; хорошо подходит под tool ИИ-агента |
| **Microcks** | Платформа API mocking + contract/conformance testing | Импортирует API-артефакты, создаёт mock endpoints и проверяет реализацию по контракту | Уникален поддержкой REST, AsyncAPI/event-driven, gRPC/Protobuf, GraphQL, SOAP и наличием AI Copilot |
| **EvoMaster** | Search-based / evolutionary system-level API test generator | Генерирует и эволюционно улучшает тесты, ищет faults, минимизирует suite и сохраняет executable test code | Похож на Schemathesis по входу, но умеет создавать готовые Python/JS/JUnit test suites |
| **RESTler** | Stateful REST API fuzzer | Компилирует OpenAPI в grammar, выводит producer-consumer зависимости и исследует последовательности запросов | Сильная специализация на глубоких stateful цепочках и security/reliability fuzzing |
| **Pact** | Consumer-driven contract testing framework | Consumer формулирует реальные ожидания, Pact создаёт контракт, затем provider проверяется по нему | Интересен для межсервисной совместимости, но требует consumer expectations, которых в текущем минимальном входе нет |

---

## 4. Где инструменты пересекаются, а где уникальны

### 4.1. Матрица возможностей

| Возможность | Schemathesis | Microcks | EvoMaster | RESTler | Pact |
|---|---:|---:|---:|---:|---:|
| OpenAPI как вход | ✅ | ✅ | ✅ | ✅ | ⚠️ не основной вход |
| Автогенерация входных данных | ✅ | ⚠️ в основном examples / mocks | ✅ | ✅ | ❌ ожидания задаёт consumer |
| Contract/schema conformance | ✅ | ✅ | ✅ | Частично, не главная цель | Другой тип контракта: consumer-provider |
| Boundary / fuzz testing | ✅ | ❌ не основная задача | ✅ | ✅ | ❌ |
| Stateful sequences | ✅ | ❌ не основная задача | ✅ через system-level search | ✅ ключевая специализация | ❌ взаимодействия изолированы |
| Генерация executable test source | Не основной CLI-результат | ❌ | ✅ | ❌ обычных regression-файлов | Consumer tests пишутся в коде вручную/агентом |
| Mock generation | ❌ | ✅ | ❌ | ❌ | ✅ mock provider, но в рамках CDC |
| AsyncAPI / event-driven | ❌ | ✅ | ❌ как основной путь | ❌ | Возможны message pacts, но это другой workflow |
| gRPC/Protobuf | ❌ | ✅ | ⚠️ RPC поддержка требует отдельного подхода/driver | ❌ | Через plugins/реализации, не наш текущий сценарий |
| Машиночитаемый результат для агента | ✅ NDJSON/JUnit/HAR/VCR | ✅ API/CLI results | ✅ `report.json` | ✅ bug buckets/replay logs | ✅ Pact JSON/Broker |
| LLM в основном рабочем цикле | ❌ | Опциональный AI Copilot | Основной search без LLM; в 6.1.1 есть experimental LLM flags | Не является частью документированного core workflow | Не является частью core workflow |

### 4.2. Главные группы пересечения

**Schemathesis, EvoMaster и RESTler** — самая близкая группа. Все три могут получать OpenAPI и автоматически исследовать REST API без ручного написания обычных тест-кейсов.

Однако акцент разный:

```text
Schemathesis
→ schema/property-based проверки
→ быстрый contract + fuzzing + stateful
→ компактные воспроизводимые failures

EvoMaster
→ search-based/evolutionary exploration
→ оптимизация найденного suite
→ executable test source + report.json

RESTler
→ dependency-aware stateful fuzzing
→ глубокие request sequences
→ security/reliability bug hunting
```

**Microcks** пересекается со Schemathesis только в части проверки OpenAPI-conformance. Его основная ценность другая: mocks, multi-protocol и API lifecycle.

**Pact** находится в ещё более отдельной категории: он проверяет не «соответствует ли API OpenAPI», а «не нарушил ли provider конкретные ожидания конкретного consumer».

---

## 5. Pact: почему не включён в текущий pipeline

### 5.1. Что делает Pact

Pact — **code-first consumer-driven contract testing**. Consumer описывает конкретное взаимодействие:

```text
consumer
  ↓
"я отправляю такой запрос
и ожидаю такой ответ"
  ↓
Pact contract
  ↓
provider verification
```

Это полезно, когда есть реальные межсервисные отношения, например:

```text
orders → payment
```

и нужно гарантировать, что `payment` не сломал то, что реально требуется `orders`.

### 5.2. Почему он не подходит текущему входу

Наш текущий целевой сценарий:

```text
OpenAPI + URL + минимум кода
→ автоматически найти тесты
```

Pact требует другой исходной информации:

- реального consumer или его клиентского кода;
- конкретных consumer expectations;
- набора взаимодействий, которые являются значимыми для consumer.

Если этих данных нет, агенту пришлось бы **самостоятельно придумывать consumer expectations**. Это уже не автоматическая проверка API по имеющемуся контракту, а создание нового контракта на предположениях LLM. Для первой версии это лишняя неопределённость и лишние токены.

### 5.3. Можно ли сделать Pact tool для агента

**Да, технически можно**, но не сейчас как core tool.

Имеет смысл вернуть Pact, когда агент получит:

```text
consumer code
+ provider code/API
+ реальные межсервисные вызовы
```

Тогда агент сможет извлекать взаимодействия из кода и использовать Pact для provider verification.

### 5.4. Что можно взять из Pact как шаблон

Даже без включения Pact в текущий pipeline полезен принцип:

```text
реальное ожидание consumer
→ формальный контракт
→ независимая проверка provider
```

Это хороший шаблон для будущего слоя межсервисных тестов.

### Решение

**Pact не включать в текущий базовый API-generation pipeline. Вернуться к нему, когда появятся реальные consumer-provider связи.**

---

## 6. RESTler: почему не включён в базовый pipeline

### 6.1. Что делает RESTler

RESTler — специализированный stateful REST API fuzzer. Он:

1. анализирует OpenAPI;
2. выводит producer-consumer зависимости;
3. строит RESTler grammar;
4. выполняет smoke/test;
5. запускает более глубокий fuzzing;
6. использует ответы предыдущих запросов для исследования последующих состояний.

Типичный сценарий:

```text
POST /users
   ↓ id
GET /users/{id}
   ↓
POST /users/{id}/orders
   ↓ orderId
GET /orders/{orderId}
```

Сильная сторона RESTler — именно такие зависимые цепочки.

### 6.2. Почему он оказался избыточным для MVP

Schemathesis уже имеет Stateful phase и умеет находить связи через:

- анализ OpenAPI;
- `Location` headers;
- OpenAPI Links;
- dependency analysis.

То есть для текущего MVP появляется большой overlap:

```text
Schemathesis Stateful
       ↕
RESTler dependency/state exploration
```

При этом RESTler требует более тяжёлого pipeline:

```text
Compile → Test → Fuzz-lean → Fuzz
```

и иногда требует дополнительного словаря или prerequisite values, если OpenAPI не содержит достаточной информации.

Для нашего критерия «минимум ручного ввода» Schemathesis проще.

### 6.3. Что RESTler умеет лучше

RESTler не признан ненужным вообще. Его специализация может стать полезна, если:

- API содержит длинные resource-dependent цепочки;
- Schemathesis Stateful не достигает нужных состояний;
- требуется именно security/reliability bug hunting с RESTler checkers;
- требуется агрессивное исследование state space.

### 6.4. Можно ли сделать RESTler tool для агента

**Да**, wrapper может скрыть стадии Compile/Test/Fuzz и возвращать:

```json
{
  "bug_buckets": [],
  "covered_requests": [],
  "replay_logs": []
}
```

Но это отдельный сложный tool, который сейчас дублирует уже выбранную способность.

### 6.5. Что можно взять как шаблон

Из RESTler полезно перенять:

- построение **producer-consumer dependency graph**;
- разделение быстрого smoke-test и глубокого fuzzing;
- bug buckets;
- replay logs.

### Решение

**RESTler не включать в базовый pipeline. Оставить резервным кандидатом для сложного stateful fuzzing.**

---

## 7. Microcks: почему его сразу оставили

### 7.1. Что делает Microcks

Microcks — не просто REST fuzzer. Это платформа, которая использует API-спецификации для:

- создания mocks;
- contract/conformance testing;
- управления несколькими API-протоколами;
- автоматизации API testing через API/CLI.

По официальной документации Microcks работает с OpenAPI, AsyncAPI, gRPC/Protobuf, GraphQL и SOAP.

Для OpenAPI runner `OPEN_API_SCHEMA` выполняет example requests и проверяет:

- HTTP status;
- соответствие response payload схеме OpenAPI.

### 7.2. Почему он не дублирует Schemathesis/EvoMaster

Schemathesis и EvoMaster нужны, когда надо **автоматически исследовать REST API и генерировать тестовые значения**.

Microcks нужен, когда надо:

```text
спецификация
→ mock API / broker behavior
→ contract conformance
→ multi-protocol testing
```

Это отдельная роль.

В проведённом Microcks-эксперименте первый contract run выявил:

```text
GET /tags
→ фактически err: null
→ в контракте err: string
→ FAIL: null found, string expected
```

После исправления OpenAPI (`nullable: true`) все четыре операции прошли проверку.

Эксперимент показал, что Microcks хорошо выполняет именно **syntactic/schema conformance**, но не заменяет fuzzing:

- он использует examples;
- сам по себе не создаёт широкий набор boundary/negative значений как Schemathesis;
- не создаёт обычные pytest/JUnit source tests;
- не решает mutation testing.

### 7.3. Уникальная ценность: mocks и multi-protocol

Это основная причина оставить Microcks.

Если агент получает:

```text
OpenAPI
AsyncAPI
Protobuf
GraphQL schema
```

он может иметь единый Microcks adapter для mock/contract слоя.

Особенно это важно для будущих микросервисов с event-driven API: Schemathesis не заменяет AsyncAPI mocking/testing.

### 7.4. LLM в Microcks

Microcks имеет **AI Copilot**.

Его роль ограничена и архитектурно полезна:

```text
API specification
→ не хватает meaningful examples
→ LLM генерирует sample data
→ Microcks использует samples для mocks
```

Это хороший пример того, как LLM следует использовать в нашем проекте: **не заменять специализированный test engine, а заполнять семантический пробел**.

AI Copilot должен быть отключён по умолчанию и вызываться только при необходимости, иначе мы будем тратить токены там, где обычный deterministic engine уже справляется.

### 7.5. Можно ли сделать Microcks tool для агента

**Да.**

У Microcks есть собственный API для управления import jobs, mocks и тестами, а также `microcks-cli`.

Рекомендуемый agent interface:

```text
microcks.import(spec)
microcks.create_mock(service)
microcks.run_contract_test(service, target)
microcks.get_result(test_id)
```

Агенту не нужно читать HTML UI.

### 7.6. Что взять как шаблон

Microcks даёт два полезных архитектурных шаблона:

1. **Specification → deterministic engine**.
2. **LLM только для enrichment**, когда спецификации не хватает примеров.

### Решение

**Microcks оставить как отдельный multi-protocol mocking/contract tool, а не как основной REST fuzzer.**

---

## 8. Schemathesis и EvoMaster: почему нельзя было выбрать один теоретически

До эксперимента оба выглядели почти одинаково:

```text
OpenAPI + URL
      ↓
автоматическая генерация API-тестов
      ↓
поиск failures
```

Если смотреть только на описание, можно решить, что один инструмент полностью заменяет другой.

На практике у них разные цели и разные выходные артефакты.

### 8.1. Schemathesis

Schemathesis — property-based API testing engine.

В актуальной документации основной CLI запускает фазы:

- Examples;
- Coverage;
- Fuzzing;
- Stateful.

Он проверяет, в частности:

- server errors;
- status-code conformance;
- response/schema conformance;
- Content-Type;
- unsupported methods;
- автоматически сгенерированные и граничные значения;
- связанные последовательности, если удаётся вывести зависимости.

Для интеграции с внешним tooling CLI умеет создавать NDJSON events, JUnit, HAR и VCR reports.

### 8.2. EvoMaster

EvoMaster — search-based/evolutionary generator системных API-тестов.

Его базовый black-box режим тоже принимает OpenAPI и URL, но дальше он:

```text
создаёт кандидатов
→ выполняет
→ оценивает fitness / coverage / faults
→ мутирует и улучшает кандидатов
→ минимизирует suite
→ сохраняет executable tests
```

Основной алгоритмический AI в EvoMaster — **эволюционный поиск и программный анализ**, а не LLM.

В проверенной версии 6.1.1 CLI также содержит experimental LLM options (`--llm`, provider/model/API key и др.), но они **не нужны для основного search workflow** и в нашем эксперименте не использовались.

### 8.3. Почему требовался реальный A/B эксперимент

Нужно было узнать не «кто заявляет больше функций», а:

- кто быстрее даёт полезный сигнал;
- находят ли они одни и те же проблемы;
- что именно агент получает на выходе;
- создаётся ли executable test code;
- насколько результаты пригодны для автоматического дальнейшего анализа;
- есть ли смысл платить вычислительной сложностью EvoMaster за дополнительные возможности.

---

## 9. Результаты Schemathesis на Catalogue

Использовался готовый Swagger 2.0:

```text
api-spec/catalogue.json
```

Операции:

```text
GET /catalogue
GET /catalogue/{id}
GET /catalogue/size
GET /tags
```

### 9.1. Полный запуск

Результаты **Schemathesis**: 

| Фаза | Результат | Смысл |
|---|---:|---|
| Examples | 1 passed, 3 skipped | В контракте был применимый example только для одной операции |
| Coverage | 4 failed | `TRACE` для существующих путей возвращал 404 вместо ожидаемого 405 |
| Fuzzing | 3 passed, 1 failed | Невалидный/несуществующий `id` приводил к 500 |
| Stateful | not applicable | Контракт не дал применимых связей для stateful chain |

Общий итог:

```text
15 generated
5 failing cases
7 unique failures
```

### 9.2. Ключевая найденная проблема

Schemathesis сгенерировал, например:

```text
GET /catalogue/0
```

и получил:

```text
500 Internal Server Error
Content-Type: text/plain
```

при контракте, документирующем `200` и JSON.

Один запрос дал три диагностических нарушения:

1. server error;
2. undocumented HTTP status;
3. undocumented Content-Type.

Дополнительно Coverage выявил неправильную обработку unsupported method:

```text
TRACE /catalogue
→ 404 Not Found
```

вместо:

```text
405 Method Not Allowed
```

### 9.3. Отдельный fuzzing run

Отдельный fuzzing-прогон занял около `0.19 s`:

```text
4 generated
3 passed
1 failed
3 unique failures
```

### 9.4. Что это показало

Schemathesis очень быстро даёт **диагностический сигнал** и практически не требует ручного определения сценариев.

Главная ценность для агента:

```text
контракт + URL
→ один tool call
→ failures + reproduction
```

---

## 10. Результаты EvoMaster на тех же данных

Для честного сравнения EvoMaster запускался на том же Catalogue и том же Swagger-контракте.

### 10.1. Особенность Swagger-файла

Исходный `catalogue.json` содержал TAB-форматирование, которое Schemathesis принял, а parser EvoMaster отверг.

Была создана семантически эквивалентная отформатированная копия:

```text
api-spec/catalogue-evomaster.json
```

Состав API и ограничения не менялись.

### 10.2. 30-секундный black-box search

Итог:

```text
4 usable REST endpoints
30 s search
32 390 evaluated tests
39 710 evaluated actions
20 covered targets
2 potential faults
9 output tests
4/4 endpoints получили успешный 2xx
```

После поиска EvoMaster выполнил minimization и security phases.

### 10.3. Что было сгенерировано

Итоговый suite:

| Файл | Кол-во | Назначение |
|---|---:|---|
| `EvoMaster_faults_Test.py` | 1 | Воспроизведение найденного fault |
| `EvoMaster_successes_Test.py` | 4 | Успешные GET-сценарии |
| `EvoMaster_others_Test.py` | 4 | Дополнительные неуспешные calls (`OPTIONS → 404`), не классифицированные как fault |
| **Итого** | **9** | Минимизированный executable suite |

Также был создан:

```text
report.json
```

Он связывает:

- faults;
- endpoint;
- HTTP status;
- generated test case;
- количество evaluated HTTP calls;
- итоговые test files.

### 10.4. Какая ошибка найдена

EvoMaster сгенерировал:

```text
GET /catalogue/Ikuskl3kg
```

и получил тот же дефект:

```text
500 Internal Server Error
```

Он зарегистрировал два potential faults:

- HTTP 500;
- status `500` не описан для `/catalogue/{id}`.

То есть основную серверную проблему EvoMaster и Schemathesis нашли одинаково.

### 10.5. Что Schemathesis нашёл дополнительно

EvoMaster отправлял:

```text
OPTIONS /catalogue
OPTIONS /tags
OPTIONS /catalogue/{id}
OPTIONS /catalogue/size
```

и видел `404`, но поместил их в `others` как failed calls, **not indicative of faults**.

Schemathesis, напротив, целенаправленно проверил unsupported methods через `TRACE` и отметил `404` вместо `405` как нарушение.

На этом API Schemathesis дал более сильную диагностику HTTP-contract поведения.

### 10.6. Что EvoMaster дал дополнительно

Главное преимущество EvoMaster — **готовый executable test source**.

Он сгенерировал Python `unittest` code с HTTP-вызовами и assertions.

Важно: значение `PYTHON_UNITTEST` в `--outputFormat` означает **framework `unittest`**, а не то, что это настоящие unit-тесты. Они обращаются к работающему HTTP API и по уровню являются system/integration/API tests.

### 10.7. Ограничение сгенерированных тестов

Часть successful tests фиксирует текущее содержимое базы:

```text
len(catalogue) == 9
name == "Holy"
price == 99.99
...
```

Такие assertions могут стать brittle, если легитимно изменятся данные, хотя API останется корректным по контракту.

Fault test также ожидает:

```text
status == 500
```

То есть это **reproducer/characterization test найденной ошибки**, а не декларация правильного поведения. Если дефект исправить на `404`, такой тест должен измениться.

Следовательно, агент **не должен автоматически коммитить весь EvoMaster output без анализа**.

---

## 11. Итог сравнения Schemathesis и EvoMaster

| Критерий | Schemathesis | EvoMaster |
|---|---|---|
| Минимальный вход | OpenAPI + URL | OpenAPI + URL |
| Black-box без исходников | ✅ | ✅ |
| Скорость первого сигнала | **Очень высокая** | Ниже: search budget |
| Property/schema fuzzing | **Сильная сторона** | Есть, но через search-based подход |
| Unsupported HTTP method diagnostics | В нашем тесте нашёл | В нашем тесте не классифицировал как fault |
| Stateful | Есть | Есть через sequences/search |
| Evolutionary feedback search | ❌ | **✅** |
| Готовые source test files | Не основной результат | **✅** |
| Машиночитаемый результат | **NDJSON/JUnit/HAR/VCR** | **report.json** |
| Reproduction | `curl`, `st replay` | generated test source |
| Риск brittle assertions | Низкий для contract checks | Выше у сохранённых snapshot-like assertions |
| Runtime cost | Низкий | Выше |
| LLM обязателен | Нет | Нет |
| Лучший use case | Fast API/contract fuzzing | Deep/on-demand search + generation of persistent tests |

### 11.1. Почему не надо выбирать только один

Теоретически инструменты сильно пересекаются, но эксперимент показал разные выходы.

**Schemathesis** лучше подходит как первая линия:

```text
быстро
→ дёшево
→ компактно
→ хороший contract signal
```

**EvoMaster** полезен как вторая линия:

```text
больше search budget
→ поиск/минимизация
→ executable test suite
```

При отсутствии ограничения на число инструментов нет смысла удалять уникальную способность EvoMaster только из-за пересечения.

При этом **не нужно запускать оба всегда**.

---

## 12. Стратегия минимизации LLM-токенов

Количество HTTP-запросов, которые делает fuzzer, само по себе **не равно расходу LLM-токенов**. Schemathesis, RESTler и базовый EvoMaster выполняют генерацию локальными алгоритмами.

Токены начинают расходоваться, когда агент отправляет в LLM:

- полный stdout;
- тысячи HTTP exchanges;
- большие HTML reports;
- весь generated source suite;
- все успешные cases, даже если они не требуют решения.

Поэтому архитектура должна быть построена вокруг **сжатых machine-readable результатов**.

### 12.1. Правила

1. **Schemathesis запускать по умолчанию для REST/OpenAPI.**
   - Wrapper сохраняет NDJSON.
   - В LLM передаются только summary и unique failures.

2. **EvoMaster запускать условно.**
   - Если нужен persistent executable suite.
   - Если нужен более глубокий search.
   - Если быстрый Schemathesis не дал достаточного покрытия.
   - Агент сначала читает `report.json`, а не все `.py`.

3. **Microcks запускать только по своей роли.**
   - mocks;
   - multi-protocol;
   - contract conformance.
   - AI Copilot включать только когда отсутствуют полезные examples.

4. **Не передавать LLM successful details без необходимости.**
   - Для успешных endpoint достаточно `{endpoint, status, schema_ok}`.

5. **LLM использовать для семантики, а не для случайной генерации.**
   - cross-field constraints;
   - meaningful examples;
   - интерпретация faults;
   - решение, какой tool запустить следующим;
   - преобразование reproducer в устойчивый regression test.

### 12.2. Рекомендуемый компактный ответ tool wrapper

```json
{
  "tool": "schemathesis",
  "operations": 4,
  "failures": [
    {
      "operation": "GET /catalogue/{id}",
      "categories": [
        "server_error",
        "undocumented_status",
        "undocumented_content_type"
      ],
      "reproducer": "GET /catalogue/0"
    }
  ],
  "artifacts": {
    "raw_report": "events.ndjson"
  }
}
```

Для EvoMaster аналогично:

```json
{
  "tool": "evomaster",
  "evaluated_calls": 39710,
  "output_tests": 9,
  "faults": 2,
  "fault_operations": ["GET /catalogue/{id}"],
  "artifacts": {
    "report": "report.json",
    "tests": "generated_tests_evomaster/"
  }
}
```

LLM не требуется видеть 39 710 действий — достаточно итогового summary.

---

## 13. Рекомендуемая архитектура ИИ-агента

```text
                         ┌──────────────────────────────┐
                         │           ИИ-АГЕНТ           │
                         │                              │
                         │  LLM: routing + semantics    │
                         │  НЕ генерирует bulk fuzz     │
                         └──────────────┬───────────────┘
                                        │
                   ┌────────────────────┼────────────────────┐
                   │                    │                    │
                   ▼                    ▼                    ▼
        ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
        │   Schemathesis   │  │     Microcks     │  │    EvoMaster     │
        │  DEFAULT REST    │  │ MULTI-PROTOCOL   │  │    ON-DEMAND     │
        │                  │  │                  │  │                  │
        │ OpenAPI + URL    │  │ API artifacts    │  │ OpenAPI + URL    │
        │       ↓          │  │       ↓          │  │       ↓          │
        │ fast fuzzing     │  │ mocks            │  │ deep search      │
        │ contract checks  │  │ conformance      │  │ minimization     │
        │ stateful         │  │ AsyncAPI/gRPC    │  │ source tests     │
        │       ↓          │  │       ↓          │  │       ↓          │
        │ NDJSON/failures  │  │ API/CLI result   │  │ report.json      │
        └────────┬─────────┘  └────────┬─────────┘  │ generated tests  │
                 │                     │            └────────┬─────────┘
                 └─────────────────────┼─────────────────────┘
                                       ▼
                         ┌──────────────────────────┐
                         │ Compact normalized result│
                         │ faults / coverage / refs │
                         └─────────────┬────────────┘
                                       ▼
                              ┌────────────────┐
                              │ LLM interprets │
                              │ only useful    │
                              │ differences    │
                              └────────────────┘
```

### Условная маршрутизация

```text
REST + OpenAPI
→ Schemathesis первым

Нужен mock / AsyncAPI / gRPC / multi-protocol conformance
→ Microcks

Нужны готовые test-source файлы или более глубокий search
→ EvoMaster

Schemathesis Stateful не достигает сложных состояний
→ можно вернуть RESTler как специализированный fallback

Появились реальные consumer-provider связи и consumer code
→ можно вернуть Pact
```

---

## 14. Что брать как tool, а что как шаблон

| Инструмент | Как tool агента | Что взять как архитектурный шаблон |
|---|---|---|
| **Schemathesis** | **Да, основной REST tool** | Минимальный interface `schema + URL → structured failures`; replay; фазовое тестирование |
| **Microcks** | **Да, отдельный mock/contract tool** | LLM-augmentation только при нехватке examples; единый multi-protocol слой |
| **EvoMaster** | **Да, условный deep-generation tool** | Feedback/search loop; minimization; разделение `faults/successes/others`; machine-readable `report.json` |
| **RESTler** | Пока нет в core | Producer-consumer dependency graph; bug buckets; replay; progressive fuzz depth |
| **Pact** | Пока нет в core | Consumer expectation → contract → provider verification для будущих межсервисных тестов |

---

## 15. Итоговое решение

### 15.1. Что оставляем

**1. Schemathesis — основной инструмент REST/OpenAPI integration testing.**

Причины:

- минимальный вход;
- быстрый результат;
- property-based fuzzing;
- contract checks;
- stateful phase;
- хорошая диагностика;
- воспроизведение failures;
- NDJSON для tool integration;
- не требует LLM.

**2. Microcks — отдельный инструмент mocks + multi-protocol contract testing.**

Причины:

- не дублирует основную роль Schemathesis;
- OpenAPI + AsyncAPI + gRPC/Protobuf + GraphQL + SOAP;
- mock generation;
- conformance testing;
- API/CLI automation;
- AI Copilot как пример контролируемого LLM-enrichment.

**3. EvoMaster — условный инструмент для глубокого поиска и генерации executable tests.**

Причины:

- на том же API подтвердил найденный 500;
- создаёт реальные test source files;
- имеет evolutionary feedback search;
- `report.json` удобен для агента;
- не должен запускаться на каждый запрос, иначе будет дублировать Schemathesis и тратить вычислительное время.

### 15.2. Что не включаем сейчас

| Инструмент | Статус               | Короткая причина                                                                                                                |
|---|----------------------|---------------------------------------------------------------------------------------------------------------------------------|
| **Pact** | ⏸ Не включать сейчас | Требует реальных consumer expectations; OpenAPI + URL недостаточно; появится смысл при анализе межсервисного consumer code      |
| **RESTler** | ❌ Не включать в core | Сильно пересекается со Stateful Schemathesis и требует более тяжёлого workflow; оставить fallback для глубоких зависимых цепочек |
| **Schemathesis** | ✅ Взять              | Самый простой и быстрый default REST/OpenAPI tool                                                                               |
| **Microcks** | ✅ Взять              | Уникальная роль: mocks + multi-protocol + contract conformance + опциональный AI enrichment                                     |
| **EvoMaster** | ✅ Взять условно      | Уникальный результат: search-based minimization + executable test source; запускать on-demand                                   |

### 15.3. Главный вывод

Нет необходимости выбирать один «универсальный» инструмент.

Для минимизации LLM-токенов лучше не пытаться заставить LLM самостоятельно генерировать весь объём тестовых данных. Агент должен **маршрутизировать специализированные локальные инструменты и читать только компактный результат**.

Рекомендуемая базовая стратегия:

```text
Schemathesis = default REST/OpenAPI tester
Microcks     = mocks + multi-protocol contract layer
EvoMaster    = on-demand deep search + executable regression artifacts

Pact         = future consumer-provider layer
RESTler      = future deep-stateful fallback
```

Это устраняет ненужное дублирование: Schemathesis и EvoMaster не запускаются одновременно по умолчанию, а LLM получает только результаты, требующие семантического решения.

---

## Финальная формула выбора

```text
Если нужен быстрый автоматический REST test из OpenAPI:
→ Schemathesis

Если нужны mocks, AsyncAPI/gRPC или единый contract layer:
→ Microcks

Если нужен более дорогой search и готовый executable test suite:
→ EvoMaster

Если позже нужен глубокий stateful dependency fuzzing:
→ RESTler

Если позже есть реальные consumer-provider expectations:
→ Pact
```

# Приложение A. Проверенные официальные источники

Документация была перепроверена перед формированием общего вывода.

## Schemathesis

- Официальная документация: https://schemathesis.readthedocs.io/en/stable/
- CLI, phases и machine-readable reports: https://schemathesis.readthedocs.io/en/stable/reference/cli/
- Stateful testing: https://schemathesis.readthedocs.io/en/stable/explanations/stateful/
- Data generation: https://schemathesis.readthedocs.io/en/latest/explanations/data-generation/

## Microcks

- Project goals / protocols: https://microcks.io/documentation/overview/project-goals/
- Test runners (`OPEN_API_SCHEMA`, `ASYNC_API_SCHEMA`): https://microcks.io/documentation/references/test-endpoints/
- OpenAPI examples conventions: https://microcks.io/documentation/references/artifacts/openapi-conventions/
- Microcks API: https://microcks.io/documentation/references/apis/open-api/
- AI Copilot: https://microcks.io/documentation/guides/integration/ai-copilot/

## EvoMaster

- Официальный репозиторий и current documentation index: https://github.com/WebFuzzing/EvoMaster
- Black-box documentation: https://github.com/WebFuzzing/EvoMaster/blob/master/docs/blackbox.md
- В эксперименте дополнительно проверен фактический `evomaster --help` версии 6.1.1, включая `--schema`, `--base`, output options и experimental LLM options.

## RESTler

- Официальный репозиторий Microsoft RESTler: https://github.com/microsoft/restler-fuzzer
- README описывает producer-consumer dependency inference, Compile/Test/Fuzz-lean/Fuzz, checkers, bug buckets и replay.

## Pact

- Официальная документация: https://docs.pact.io/
- Consumer-driven contract model: https://docs.pact.io/consumer
- Provider verification: https://docs.pact.io/implementation_guides/python/docs/provider

---

# Приложение B. Как запускались оставленные инструменты и что было изменено

Этот раздел вынесен после итогов специально, чтобы эксплуатационные детали не смешивались с анализом выбора.

## B.1. Schemathesis

### Что изменили в тестовом Catalogue

Исходный код Catalogue и `api-spec/catalogue.json` **не изменялись**.

Для запуска старого Sock Shop на Apple Silicon были сделаны инфраструктурные изменения:

```yaml
catalogue:
  platform: linux/amd64
  image: weaveworksdemos/catalogue:0.3.5
  ports:
    - "8080:80"

catalogue-db:
  platform: linux/amd64
  image: weaveworksdemos/catalogue-db:0.3.0
```

Причина:

- старые образы рассчитаны на AMD64;
- Catalogue должен быть доступен Schemathesis, работающему на macOS;
- `8080:80` публикует внутренний порт `80` контейнера как `localhost:8080`.

Swagger содержит внутренний Docker host:

```json
"host": "catalogue"
```

поэтому с хоста адрес переопределялся через `--url`.

### Основной запуск

```bash
schemathesis run api-spec/catalogue.json --url http://localhost:8080
```

### Отдельный fuzzing

```bash
schemathesis run api-spec/catalogue.json \
  --url http://localhost:8080 \
  --phases fuzzing
```

### Что важно для будущего tool

Для agent wrapper лучше использовать machine-readable report, например NDJSON, а не передавать LLM весь terminal output.

---

## B.2. EvoMaster

### Что изменили

Catalogue и его исходный API не менялись.

Первый запуск EvoMaster на исходном `catalogue.json` завершился ошибкой parser из-за TAB-форматирования. Поэтому была создана форматированная семантически эквивалентная копия:

```bash
python -m json.tool api-spec/catalogue.json \
  > api-spec/catalogue-evomaster.json
```

Это изменение форматирования, а не API-содержания.

EvoMaster также автоматически создал локальный конфигурационный файл:

```text
em.yaml
```

### Запуск black-box эксперимента

```bash
evomaster \
  --blackBox true \
  --schema api-spec/catalogue-evomaster.json \
  --base http://localhost:8080 \
  --maxTime 30s \
  --outputFormat PYTHON_UNITTEST \
  --outputFolder generated_tests_evomaster
```

### Полученные артефакты

```text
generated_tests_evomaster/
├── EvoMaster_faults_Test.py
├── EvoMaster_successes_Test.py
├── EvoMaster_others_Test.py
├── em_test_utils.py
├── report.json
├── index.html
├── low-code-index.html
├── webreport.py
└── assets/...
```

Для ИИ-агента основной вход после выполнения — **`report.json`**. Generated `.py` следует читать только тогда, когда требуется анализ или сохранение конкретного regression/reproducer test.

---

## B.3. Microcks

### Что изменили в тестовой среде

В Microcks-эксперименте использовалась отдельная конфигурация Sock Shop.

К сервису Catalogue был добавлен host port:

```yaml
ports:
  - "8082:80"
```

Catalogue стал доступен на:

```text
http://localhost:8082
```

Так как Microcks работает внутри Docker, target endpoint для contract test был:

```text
http://host.docker.internal:8082
```

### Контракт

Для этого эксперимента использовался OpenAPI 3.0:

```text
sockshop-catalogue-openapi.yaml
```

Он был создан на основе реальных ответов и содержал examples для операций.

При первом contract test Microcks обнаружил:

```text
/tags: null found, string expected
```

После этого в контракт было внесено:

```yaml
err:
  type: string
  nullable: true
```

Это было исправление **контракта**, а не исходного кода Catalogue.

### Как запускалась проверка

В Microcks импортировался OpenAPI и создавался test:

```text
Target:
http://host.docker.internal:8082

Runner:
OPEN_API_SCHEMA
```

После исправления `nullable`:

```text
GET /catalogue       PASS
GET /catalogue/{id}  PASS
GET /tags            PASS
GET /health          PASS
```

Для будущего агента UI не нужен: управление следует делать через Microcks API/CLI.

---
