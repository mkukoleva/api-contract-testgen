"""
Тесты для модуля мутационного тестирования API-контрактов (задача 2.2.9).
"""

import json
from pathlib import Path
import pytest
import yaml

from prototype.evaluate.mutation import (
    Mutant,
    MutationScoreResult,
    MutationType,
    _set_by_path,
    calculate_mutation_score,
    count_mutants_by_operator,
    generate_mutants,
    mutate_remove_required,
    mutate_status_code_swap,
    mutate_type_change,
)


@pytest.fixture
def sample_contract() -> dict:
    """Типичный контракт OpenAPI 3.0 с параметрами, схемами и статус-кодами."""
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Test Store API",
            "version": "1.0.0",
        },
        "paths": {
            "/items": {
                "get": {
                    "summary": "List items",
                    "responses": {
                        "200": {
                            "description": "Success",
                        },
                        "500": {
                            "description": "Server error",
                        },
                    },
                },
                "post": {
                    "summary": "Create item",
                    "responses": {
                        "201": {
                            "description": "Created",
                        },
                        "400": {
                            "description": "Invalid input",
                        },
                    },
                },
            },
            "/items/{id}": {
                "get": {
                    "summary": "Get item by ID",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {
                                "type": "string",
                            },
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Found",
                        },
                        "404": {
                            "description": "Not found",
                        },
                    },
                },
            },
        },
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "required": ["id", "name"],
                    "properties": {
                        "id": {
                            "type": "string",
                        },
                        "count": {
                            "type": "integer",
                        },
                        "is_active": {
                            "type": "boolean",
                        },
                        "tags": {
                            "type": "array",
                            "items": {
                                "type": "string",
                            },
                        },
                    },
                },
            },
        },
    }


# ============================================================================
# Тесты оператора: type_change
# ============================================================================


def test_mutate_type_change_generates_mutants(sample_contract):
    """Проверяет генерацию мутантов изменения типов данных."""
    mutants = mutate_type_change(sample_contract)
    assert len(mutants) > 0

    for m in mutants:
        assert m.operator == MutationType.TYPE_CHANGE
        assert "type" in m.target_path
        assert m.original_value != m.mutated_value
        assert m.mutant_id.startswith("MUT-TYPE-")


def test_mutate_type_change_replaces_types_correctly(sample_contract):
    """Проверяет корректность правил подмены типов."""
    mutants = mutate_type_change(sample_contract)
    types_found = {m.original_value: m.mutated_value for m in mutants}

    # string -> integer
    assert types_found.get("string") == "integer"
    # integer -> string
    assert types_found.get("integer") == "string"
    # boolean -> string
    assert types_found.get("boolean") == "string"
    # object -> string
    assert types_found.get("object") == "string"
    # array -> object
    assert types_found.get("array") == "object"


def test_mutate_type_change_does_not_modify_original_contract(sample_contract):
    """Проверяет, что исходный контракт остаётся неизменным."""
    orig_type = sample_contract["components"]["schemas"]["Item"]["properties"]["count"]["type"]
    assert orig_type == "integer"

    _ = mutate_type_change(sample_contract)

    assert sample_contract["components"]["schemas"]["Item"]["properties"]["count"]["type"] == "integer"


# ============================================================================
# Тесты оператора: remove_required
# ============================================================================


def test_mutate_remove_required_from_schemas(sample_contract):
    """Проверяет удаление полей из списка required в схемах."""
    mutants = mutate_remove_required(sample_contract)
    schema_req_mutants = [m for m in mutants if "[" in m.target_path]

    assert len(schema_req_mutants) == 2  # 'id' и 'name'

    # Проверяем удаление 'id'
    id_mutant = next(m for m in schema_req_mutants if "id" in m.target_path)
    assert id_mutant.operator == MutationType.REMOVE_REQUIRED
    assert "name" in id_mutant.mutated_value
    assert "id" not in id_mutant.mutated_value

    # Проверяем удаление 'name'
    name_mutant = next(m for m in schema_req_mutants if "name" in m.target_path)
    assert "id" in name_mutant.mutated_value
    assert "name" not in name_mutant.mutated_value


def test_mutate_remove_required_from_parameter(sample_contract):
    """Проверяет снятие флага required: true у параметров."""
    mutants = mutate_remove_required(sample_contract)
    param_mutants = [m for m in mutants if m.original_value is True and m.mutated_value is False]

    assert len(param_mutants) >= 1
    p_mutant = param_mutants[0]
    assert p_mutant.operator == MutationType.REMOVE_REQUIRED
    assert "required" in p_mutant.target_path
    assert "id" in p_mutant.description


def test_mutate_remove_required_empty_contract():
    """Проверяет контракт без обязательных полей."""
    contract = {"openapi": "3.0.0", "paths": {}}
    mutants = mutate_remove_required(contract)
    assert mutants == []


# ============================================================================
# Тесты оператора: status_code_swap
# ============================================================================


def test_mutate_status_code_swap(sample_contract):
    """Проверяет подмену статус-кодов ответов."""
    mutants = mutate_status_code_swap(sample_contract)
    assert len(mutants) > 0

    codes_swapped = {(m.original_value, m.mutated_value) for m in mutants}

    # 200 -> 500
    assert ("200", "500") in codes_swapped
    # 201 -> 400
    assert ("201", "400") in codes_swapped
    # 400 -> 200
    assert ("400", "200") in codes_swapped
    # 404 -> 200
    assert ("404", "200") in codes_swapped
    # 500 -> 200
    assert ("500", "200") in codes_swapped


def test_mutate_status_code_swap_integer_keys():
    """Проверяет работу с целочисленными ключами статус-кодов."""
    contract = {
        "paths": {
            "/test": {
                "get": {
                    "responses": {
                        200: {"description": "OK"},
                    }
                }
            }
        }
    }
    mutants = mutate_status_code_swap(contract)
    assert len(mutants) == 1
    m = mutants[0]
    assert m.original_value == "200"
    assert m.mutated_value == "500"
    assert "500" in m.mutated_contract["paths"]["/test"]["get"]["responses"]


# ============================================================================
# Тесты генератора мутантов: generate_mutants
# ============================================================================


def test_generate_mutants_all_operators(sample_contract):
    """Проверяет генерацию со всеми операторами по умолчанию."""
    mutants = generate_mutants(sample_contract)
    operators_present = {m.operator for m in mutants}

    assert operators_present == {
        MutationType.TYPE_CHANGE,
        MutationType.REMOVE_REQUIRED,
        MutationType.STATUS_CODE_SWAP,
    }


def test_generate_mutants_filtered_operators(sample_contract):
    """Проверяет запуск только выбранных операторов."""
    mutants = generate_mutants(
        sample_contract,
        operators=[MutationType.STATUS_CODE_SWAP],
    )
    for m in mutants:
        assert m.operator == MutationType.STATUS_CODE_SWAP


def test_generate_mutants_on_demo_fixture():
    """Проверяет запуск генератора на реальном файле фикстуры demo_openapi.yaml."""
    fixture_path = Path(__file__).parent / "fixtures" / "demo_openapi.yaml"
    assert fixture_path.exists()

    contract = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    mutants = generate_mutants(contract)

    assert len(mutants) > 0
    counts = count_mutants_by_operator(mutants)
    assert counts[MutationType.TYPE_CHANGE.value] > 0
    assert counts[MutationType.REMOVE_REQUIRED.value] > 0
    assert counts[MutationType.STATUS_CODE_SWAP.value] > 0


# ============================================================================
# Тесты расчёта Mutation Score
# ============================================================================


def test_calculate_mutation_score_normal():
    """Проверяет вычисление процента устойчивости тестов."""
    res = calculate_mutation_score(total_mutants=10, killed_mutants=7)
    assert res.total_mutants == 10
    assert res.killed_mutants == 7
    assert res.survived_mutants == 3
    assert res.mutation_score_percent == 70.0


def test_calculate_mutation_score_zero_mutants():
    """Проверяет граничный случай при отсутствии мутантов."""
    res = calculate_mutation_score(total_mutants=0, killed_mutants=0)
    assert res.mutation_score_percent == 100.0
    assert res.survived_mutants == 0


def test_calculate_mutation_score_all_killed():
    """Проверяет 100% уничтожение мутантов."""
    res = calculate_mutation_score(total_mutants=5, killed_mutants=5)
    assert res.mutation_score_percent == 100.0
    assert res.survived_mutants == 0


def test_calculate_mutation_score_none_killed():
    """Проверяет 0% уничтожение мутантов."""
    res = calculate_mutation_score(total_mutants=5, killed_mutants=0)
    assert res.mutation_score_percent == 0.0
    assert res.survived_mutants == 5


def test_calculate_mutation_score_invalid_values():
    """Проверяет валидацию некорректных аргументов."""
    with pytest.raises(ValueError, match="не может быть отрицательным"):
        calculate_mutation_score(total_mutants=-1, killed_mutants=0)

    with pytest.raises(ValueError, match="не может превышать"):
        calculate_mutation_score(total_mutants=5, killed_mutants=6)


# ============================================================================
# Тесты сериализации и сохранения мутантов
# ============================================================================


def test_mutant_export_and_save(sample_contract, tmp_path):
    """Проверяет экспорт мутанта в JSON, YAML и сохранение в файл."""
    mutants = mutate_type_change(sample_contract)
    mutant = mutants[0]

    d = mutant.to_dict()
    assert d["mutant_id"] == mutant.mutant_id
    assert d["operator"] == MutationType.TYPE_CHANGE.value

    json_str = mutant.dump_json()
    assert "openapi" in json_str

    yaml_str = mutant.dump_yaml()
    assert "openapi: 3.0.3" in yaml_str

    # Сохранение во временный файл YAML
    yaml_file = tmp_path / "mutant.yaml"
    saved = mutant.save_to_file(yaml_file)
    assert saved.exists()
    loaded = yaml.safe_load(saved.read_text(encoding="utf-8"))
    assert loaded["openapi"] == "3.0.3"

    # Сохранение во временный файл JSON
    json_file = tmp_path / "mutant.json"
    saved_json = mutant.save_to_file(json_file)
    assert saved_json.exists()
    assert '"openapi": "3.0.3"' in saved_json.read_text(encoding="utf-8")


def test_mutation_score_result_to_dict():
    """Проверяет сериализацию MutationScoreResult в словарь."""
    res = calculate_mutation_score(
        total_mutants=10,
        killed_mutants=8,
        mutants_by_operator={"type_change": 5, "status_code_swap": 5},
        killed_by_operator={"type_change": 4, "status_code_swap": 4},
    )
    d = res.to_dict()
    assert d["total_mutants"] == 10
    assert d["killed_mutants"] == 8
    assert d["survived_mutants"] == 2
    assert d["mutation_score_percent"] == 80.0
    assert d["mutants_by_operator"]["type_change"] == 5


def test_mutate_status_code_swap_custom_codes():
    """Проверяет подмену нестандартных статус-кодов (fallback ветки)."""
    contract = {
        "paths": {
            "/custom": {
                "get": {
                    "responses": {
                        "299": {"description": "Custom 2xx"},
                        "499": {"description": "Custom 4xx"},
                    }
                }
            }
        }
    }
    mutants = mutate_status_code_swap(contract)
    assert len(mutants) == 2
    swaps = {m.original_value: m.mutated_value for m in mutants}
    assert swaps["299"] == "500"
    assert swaps["499"] == "200"


# ============================================================================
# Тесты интеграции с модулем metrics и пакетом evaluate
# ============================================================================


def test_package_exports():
    """Проверяет экспорт сущностей из пакета prototype.evaluate."""
    import prototype.evaluate as ev

    assert hasattr(ev, "Mutant")
    assert hasattr(ev, "MutationType")
    assert hasattr(ev, "generate_mutants")
    assert hasattr(ev, "calculate_mutation_score")
    assert hasattr(ev, "calculate_metrics")


def test_metrics_integration_with_mutation_score():
    """Проверяет передачу Mutation Score в calculate_metrics."""
    from prototype.evaluate.metrics import calculate_metrics, format_metrics_markdown

    agent_result = {
        "messages": [],
        "mutation_score_percent": 85.5,
    }
    contract_summary = {"operation_count": 3}

    # 1. Из agent_result
    metrics = calculate_metrics(
        agent_result=agent_result,
        contract_summary=contract_summary,
        generation_time_seconds=1.23,
    )
    assert metrics.mutation_score_percent == 85.5
    md = format_metrics_markdown(metrics)
    assert "85.5 %" in md
    assert "N/A — mutation testing runner" not in md

    # 2. Явная передача параметра
    metrics_explicit = calculate_metrics(
        agent_result={},
        contract_summary=contract_summary,
        generation_time_seconds=1.0,
        mutation_score_percent=92.0,
    )
    assert metrics_explicit.mutation_score_percent == 92.0


# ============================================================================
# Стресс-тесты, фаззинг и граничные случаи (Hardcore testing)
# ============================================================================


def test_dump_yaml_no_pyyaml(monkeypatch, sample_contract):
    """Проверяет обработку отсутствия pyyaml при вызове dump_yaml."""
    import prototype.evaluate.mutation as mut_module

    mutants = mutate_type_change(sample_contract)
    monkeypatch.setattr(mut_module, "yaml", None)
    with pytest.raises(RuntimeError, match="Пакет pyyaml не установлен"):
        mutants[0].dump_yaml()


def test_set_by_path_list_and_scalar_edge_cases():
    """Проверяет установку значений по путям со списками и некорректными узлами."""
    # 1. Замена элемента в списке
    data = {"items": ["a", "b", "c"]}
    _set_by_path(data, ["items", "1"], "mutated")
    assert data["items"][1] == "mutated"

    # 2. Попытка прохода через скалярное значение (должна безопасно завершиться)
    scalar_data = {"key": "scalar_value"}
    _set_by_path(scalar_data, ["key", "sub_key", "target"], "new_val")
    assert scalar_data == {"key": "scalar_value"}


def test_mutate_status_code_swap_malformed_and_edge_paths():
    """Проверяет устойчивость status_code_swap к нестандартным или повреждённым структурам paths."""
    # paths не словарь
    assert mutate_status_code_swap({"paths": "invalid"}) == []
    assert mutate_status_code_swap({"paths": None}) == []

    # path_item не словарь или содержит служебные поля OpenAPI (summary, parameters, servers)
    contract_with_meta = {
        "paths": {
            "/meta": "not-a-dict",
            "/items": {
                "summary": "Path summary",
                "description": "Path description",
                "servers": [{"url": "http://example.com"}],
                "parameters": [{"name": "filter", "in": "query", "schema": {"type": "string"}}],
                "get": "not-an-operation-dict",
                "post": {
                    "summary": "Create",
                    "responses": "not-a-responses-dict",
                },
                "delete": {
                    "summary": "Delete",
                    "responses": {
                        "default": {"description": "Default error"},
                        "500": {"description": "Server error"},
                    },
                },
            },
        }
    }
    mutants = mutate_status_code_swap(contract_with_meta)
    assert len(mutants) == 2
    ops = {m.original_value: m.mutated_value for m in mutants}
    # default -> 200 (так как не начинается с 2)
    assert ops["default"] == "200"
    # 500 -> 200
    assert ops["500"] == "200"


def test_deeply_nested_and_complex_schemas():
    """Проверяет работу операторов на сложных схемах с allOf, anyOf, oneOf и массивами массивов."""
    complex_contract = {
        "openapi": "3.1.0",
        "paths": {
            "/complex": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "allOf": [
                                        {
                                            "type": "object",
                                            "required": ["base_id"],
                                            "properties": {
                                                "base_id": {"type": "string"}
                                            },
                                        },
                                        {
                                            "type": "object",
                                            "required": ["details"],
                                            "properties": {
                                                "matrix": {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "array",
                                                        "items": {
                                                            "type": "integer"
                                                        },
                                                    },
                                                }
                                            },
                                        },
                                    ]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            }
        }
    }
    mutants = generate_mutants(complex_contract)
    assert len(mutants) >= 6

    # Проверяем наличие мутаций глубоко вложенных типов (matrix items integer -> string)
    type_mutants = [m for m in mutants if m.operator == MutationType.TYPE_CHANGE]
    int_mutant = next(m for m in type_mutants if m.original_value == "integer")
    assert int_mutant.mutated_value == "string"
    assert "matrix" in int_mutant.target_path

    # Проверяем удаление required из allOf
    req_mutants = [m for m in mutants if m.operator == MutationType.REMOVE_REQUIRED]
    assert any("base_id" in m.target_path for m in req_mutants)
    assert any("details" in m.target_path for m in req_mutants)


def test_fuzzing_with_random_unusual_types():
    """Фаззинг-проверка: структуры с None, пустыми словарями, списками и нетипичными типами."""
    fuzz_contract = {
        "openapi": "3.0.3",
        "info": None,
        "paths": {
            "/fuzz": {
                "get": {
                    "parameters": [
                        None,
                        {},
                        {"name": "broken_param", "required": "not-a-bool"},
                        {"name": "valid_param", "required": True, "in": "query", "schema": None},
                    ],
                    "responses": {
                        "200": None,
                        "": {"description": "Empty code"},
                    },
                }
            },
            "": {},
            "/empty": None,
        },
        "components": {
            "schemas": {
                "Empty": {},
                "NullType": {"type": None},
                "IntType": {"type": 123},
                "EmptyRequired": {"required": []},
                "InvalidRequired": {"required": "not-a-list"},
            }
        },
    }
    # Функция должна безопасно отработать, не упав с TypeError/AttributeError
    mutants = generate_mutants(fuzz_contract)
    assert isinstance(mutants, list)
    # Параметр valid_param должен быть мутирован
    assert any(m.operator == MutationType.REMOVE_REQUIRED for m in mutants)


def test_stress_large_contract_performance():
    """Стресс-тест: генерация мутантов для масштабного контракта (100 эндпоинтов)."""
    import time

    large_contract = {
        "openapi": "3.0.3",
        "paths": {},
        "components": {"schemas": {}},
    }

    for i in range(100):
        path = f"/api/v1/resource_{i}"
        large_contract["paths"][path] = {
            "get": {
                "parameters": [
                    {
                        "name": f"param_{i}",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {"description": "Success"},
                    "404": {"description": "Not Found"},
                },
            },
            "post": {
                "responses": {
                    "201": {"description": "Created"},
                    "400": {"description": "Bad Request"},
                }
            },
        }
        large_contract["components"]["schemas"][f"Model_{i}"] = {
            "type": "object",
            "required": [f"field_{i}_a", f"field_{i}_b"],
            "properties": {
                f"field_{i}_a": {"type": "integer"},
                f"field_{i}_b": {"type": "boolean"},
            },
        }

    start = time.perf_counter()
    mutants = generate_mutants(large_contract)
    duration = time.perf_counter() - start

    assert len(mutants) >= 1000
    # Каждый мутант — полная копия контракта (deepcopy), поэтому на 100 эндпоинтах
    # генерация занимает ~3.5 с и на GitHub, и локально. Лимит ловит деградацию
    # на порядок, а не колебания скорости машины.
    assert duration < 10.0, f"Генерация мутантов слишком медленная: {duration:.2f}с"

    counts = count_mutants_by_operator(mutants)
    assert counts[MutationType.TYPE_CHANGE.value] >= 400
    assert counts[MutationType.REMOVE_REQUIRED.value] >= 300
    assert counts[MutationType.STATUS_CODE_SWAP.value] >= 400


def test_every_mutant_produces_valid_syntax(sample_contract):
    """Жёсткая проверка синтаксиса: каждый мутант обязан сериализоваться в валидные JSON и YAML."""
    mutants = generate_mutants(sample_contract)
    assert len(mutants) > 0

    for m in mutants:
        # 1. Валидный JSON
        json_repr = m.dump_json()
        loaded_json = json.loads(json_repr)
        assert isinstance(loaded_json, dict)
        assert loaded_json["openapi"] == sample_contract["openapi"]

        # 2. Валидный YAML
        yaml_repr = m.dump_yaml()
        loaded_yaml = yaml.safe_load(yaml_repr)
        assert isinstance(loaded_yaml, dict)
        assert loaded_yaml["openapi"] == sample_contract["openapi"]


def test_mutation_killing_simulation(sample_contract):
    """
    Симуляция уничтожения мутантов (Mutation Testing Kill-Rate).
    Проверяет, как валидатор данных/ответов ловит каждый тип мутации.
    """
    mutants = generate_mutants(sample_contract)
    killed = 0
    survived = 0

    for m in mutants:
        c = m.mutated_contract
        # Тестовая проверка 1: проверка типа поля count в схеме Item (должен быть int)
        item_schema = c.get("components", {}).get("schemas", {}).get("Item", {})
        count_type = item_schema.get("properties", {}).get("count", {}).get("type")

        # Тестовая проверка 2: обязательность поля id в схеме Item
        item_req = item_schema.get("required", [])

        # Тестовая проверка 3: статус-код ответа GET /items должен быть 200
        get_items_resp = c.get("paths", {}).get("/items", {}).get("get", {}).get("responses", {})

        is_killed = False

        if m.operator == MutationType.TYPE_CHANGE and count_type != "integer":
            is_killed = True
        elif m.operator == MutationType.REMOVE_REQUIRED and "id" not in item_req:
            is_killed = True
        elif m.operator == MutationType.STATUS_CODE_SWAP and "200" not in get_items_resp:
            is_killed = True

        if is_killed:
            killed += 1
        else:
            survived += 1

    assert killed > 0
    score_res = calculate_mutation_score(total_mutants=len(mutants), killed_mutants=killed)
    assert 0.0 < score_res.mutation_score_percent <= 100.0
    assert score_res.killed_mutants == killed
    assert score_res.survived_mutants == survived


def test_branch_coverage_edges(monkeypatch):
    """Покрывает оставшиеся ветки условий для 100% надёжности."""
    import prototype.evaluate.mutation as mut_module

    # 1. Схема с неизвестным типом данных (orig_type not in TYPE_REPLACEMENTS)
    contract_unknown_type = {"components": {"schemas": {"Test": {"type": "custom_geo_point"}}}}
    assert mutate_type_change(contract_unknown_type) == []

    # 2. Узел required: True, но не параметр (нет "in" и "name")
    contract_non_param_req = {"components": {"schemas": {"Test": {"required": True}}}}
    assert mutate_remove_required(contract_non_param_req) == []

    # 3. Status code swap, где swapped_code совпадает с code_str
    monkeypatch.setattr(mut_module, "STATUS_CODE_SWAPS", {"777": "777", "500": "500"})
    contract_same_code = {
        "paths": {
            "/p": {
                "get": {
                    "responses": {"777": {"description": "Lucky"}, "500": {"description": "Err"}}
                }
            }
        }
    }
    muts = mutate_status_code_swap(contract_same_code)
    assert len(muts) == 2
    swapped = {m.original_value: m.mutated_value for m in muts}
    assert swapped["777"] == "500"
    assert swapped["500"] == "200"

    # 4. Передача неизвестного оператора в generate_mutants
    res = generate_mutants(contract_unknown_type, operators=["non_existent_operator"])  # type: ignore
    assert res == []

    # 5. _set_by_path, когда исходный корень не словарь и не список на последнем шаге
    scalar = 42
    _set_by_path(scalar, ["0"], "value")  # не падает
