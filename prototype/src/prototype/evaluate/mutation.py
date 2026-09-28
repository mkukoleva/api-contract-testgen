"""
Модуль мутационного тестирования API-контрактов (OpenAPI/Swagger).

Реализация задачи 2.2.9:
Базовые операторы мутации данных на уровне API-контракта:
1. type_change — изменение типов данных в схемах (string -> integer, integer -> string, etc.).
2. remove_required — удаление обязательных полей (required в схемах объектов и required в параметрах).
3. status_code_swap — подмена статус-кодов ответов (200 -> 500, 201 -> 400, 404 -> 200, etc.).

Модуль позволяет генерировать мутантов контракта, сохранять их в YAML/JSON и
рассчитывать Mutation Score (метрику устойчивости тестов).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


class MutationType(str, Enum):
    """Типы операторов мутации API-контракта (задача 2.2.9)."""

    TYPE_CHANGE = "type_change"
    REMOVE_REQUIRED = "remove_required"
    STATUS_CODE_SWAP = "status_code_swap"


@dataclass(frozen=True)
class Mutant:
    """
    Описание одного сгенерированного мутанта API-контракта.

    Attributes:
        mutant_id: Уникальный идентификатор мутанта (например, MUT-TYPE-001).
        operator: Тип применённого оператора мутации.
        target_path: Точный путь к изменяемому узлу в дереве OpenAPI.
        description: Понятное описание мутации.
        original_value: Исходное значение узла.
        mutated_value: Новое (мутированное) значение узла.
        mutated_contract: Полный мутированный словарь спецификации контракта.
    """

    mutant_id: str
    operator: MutationType
    target_path: str
    description: str
    original_value: Any
    mutated_value: Any
    mutated_contract: dict[str, Any] = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Преобразовать метаданные мутанта в словарь (без тяжёлого тела контракта)."""
        return {
            "mutant_id": self.mutant_id,
            "operator": self.operator.value,
            "target_path": self.target_path,
            "description": self.description,
            "original_value": self.original_value,
            "mutated_value": self.mutated_value,
        }

    def dump_yaml(self) -> str:
        """Экспортировать мутированный контракт в формат YAML."""
        if yaml is None:
            raise RuntimeError("Пакет pyyaml не установлен для экспорта в YAML.")
        return yaml.safe_dump(
            self.mutated_contract,
            sort_keys=False,
            allow_unicode=True,
        )

    def dump_json(self, indent: int = 2) -> str:
        """Экспортировать мутированный контракт в формат JSON."""
        return json.dumps(
            self.mutated_contract,
            indent=indent,
            ensure_ascii=False,
        )

    def save_to_file(self, file_path: str | Path) -> Path:
        """Сохранить мутированный контракт в файл (.yaml/.yml/.json)."""
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in {".yaml", ".yml"}:
            path.write_text(self.dump_yaml(), encoding="utf-8")
        else:
            path.write_text(self.dump_json(), encoding="utf-8")
        return path


@dataclass
class MutationScoreResult:
    """
    Результат расчёта Mutation Score.

    Attributes:
        total_mutants: Всего сгенерировано мутантов.
        killed_mutants: Количество уничтоженных (killed) мутантов (тесты зафиксировали ошибку).
        survived_mutants: Количество выживших (survived) мутантов.
        mutation_score_percent: Процент уничтоженных мутантов (0.0 - 100.0%).
        mutants_by_operator: Число мутантов по каждому оператору.
        killed_by_operator: Число уничтоженных мутантов по каждому оператору.
    """

    total_mutants: int
    killed_mutants: int
    survived_mutants: int
    mutation_score_percent: float
    mutants_by_operator: dict[str, int] = field(default_factory=dict)
    killed_by_operator: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Преобразовать результат в JSON-совместимый словарь."""
        return {
            "total_mutants": self.total_mutants,
            "killed_mutants": self.killed_mutants,
            "survived_mutants": self.survived_mutants,
            "mutation_score_percent": self.mutation_score_percent,
            "mutants_by_operator": self.mutants_by_operator,
            "killed_by_operator": self.killed_by_operator,
        }


# Таблица несовместимых подмен типов данных
TYPE_REPLACEMENTS: dict[str, str] = {
    "string": "integer",
    "integer": "string",
    "number": "string",
    "boolean": "string",
    "array": "object",
    "object": "string",
}

# Таблица подмены статус-кодов ответов
STATUS_CODE_SWAPS: dict[str, str] = {
    "200": "500",
    "201": "400",
    "202": "500",
    "204": "500",
    "400": "200",
    "401": "200",
    "403": "200",
    "404": "200",
    "409": "200",
    "422": "200",
    "500": "200",
    "502": "200",
    "503": "200",
}


def mutate_type_change(contract: dict[str, Any]) -> list[Mutant]:
    """
    Оператор: изменение типов данных (type_change).

    Находит в контракте OpenAPI объявления `type` (в схемах моделей, параметров,
    свойств, элементов массивов) и подменяет их на несовместимые типы данных.
    """
    mutants: list[Mutant] = []
    counter = 1

    def _traverse(node: Any, path: list[str]) -> None:
        nonlocal counter
        if isinstance(node, dict):
            # Проверяем поле 'type'
            if "type" in node and isinstance(node["type"], str):
                orig_type = node["type"].lower()
                if orig_type in TYPE_REPLACEMENTS:
                    new_type = TYPE_REPLACEMENTS[orig_type]
                    target_path = ".".join(path + ["type"])
                    mutated = copy.deepcopy(contract)
                    _set_by_path(mutated, path + ["type"], new_type)

                    mutants.append(
                        Mutant(
                            mutant_id=f"MUT-TYPE-{counter:03d}",
                            operator=MutationType.TYPE_CHANGE,
                            target_path=target_path,
                            description=f"Изменение типа данных '{orig_type}' -> '{new_type}' в {target_path}",
                            original_value=orig_type,
                            mutated_value=new_type,
                            mutated_contract=mutated,
                        )
                    )
                    counter += 1

            for k, v in node.items():
                _traverse(v, path + [k])
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                _traverse(item, path + [str(idx)])

    _traverse(contract, [])
    return mutants


def mutate_remove_required(contract: dict[str, Any]) -> list[Mutant]:
    """
    Оператор: удаление обязательных полей (remove_required).

    1. В схемах объектов: удаляет каждое обязательное поле из списка `required`.
    2. В параметрах операций: подменяет `required: true` на `required: false`.
    """
    mutants: list[Mutant] = []
    counter = 1

    def _traverse(node: Any, path: list[str]) -> None:
        nonlocal counter
        if isinstance(node, dict):
            # 1. Схема объекта: список required
            if "required" in node and isinstance(node["required"], list) and len(node["required"]) > 0:
                req_list = node["required"]
                for item in req_list:
                    target_path = ".".join(path + ["required"])
                    mutated = copy.deepcopy(contract)
                    new_req = [x for x in req_list if x != item]
                    _set_by_path(mutated, path + ["required"], new_req)

                    mutants.append(
                        Mutant(
                            mutant_id=f"MUT-REQ-{counter:03d}",
                            operator=MutationType.REMOVE_REQUIRED,
                            target_path=f"{target_path}[{item}]",
                            description=f"Удаление обязательного поля '{item}' из {target_path}",
                            original_value=req_list,
                            mutated_value=new_req,
                            mutated_contract=mutated,
                        )
                    )
                    counter += 1

            # 2. Параметр запроса: флаг required: True
            if "required" in node and isinstance(node["required"], bool) and node["required"] is True:
                # Проверяем, является ли узел параметром (наличие 'in' и 'name')
                if "in" in node and "name" in node:
                    param_name = node.get("name", "unknown")
                    target_path = ".".join(path + ["required"])
                    mutated = copy.deepcopy(contract)
                    _set_by_path(mutated, path + ["required"], False)

                    mutants.append(
                        Mutant(
                            mutant_id=f"MUT-REQ-{counter:03d}",
                            operator=MutationType.REMOVE_REQUIRED,
                            target_path=target_path,
                            description=f"Снятие флага обязательности (required: true -> false) для параметра '{param_name}' в {target_path}",
                            original_value=True,
                            mutated_value=False,
                            mutated_contract=mutated,
                        )
                    )
                    counter += 1

            for k, v in node.items():
                _traverse(v, path + [k])
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                _traverse(item, path + [str(idx)])

    _traverse(contract, [])
    return mutants


def mutate_status_code_swap(contract: dict[str, Any]) -> list[Mutant]:
    """
    Оператор: подмена статус-кодов ответов (status_code_swap).

    Находит секции responses во всех операциях контракта и подменяет объявленные
    коды ответов (успешные на ошибочные, ошибочные на успешные).
    """
    mutants: list[Mutant] = []
    counter = 1

    paths = contract.get("paths", {})
    if not isinstance(paths, dict):
        return mutants

    for path_str, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "delete", "patch", "options", "head"}:
                continue
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if not isinstance(responses, dict):
                continue

            for status_code, resp_content in responses.items():
                code_str = str(status_code)
                swapped_code = STATUS_CODE_SWAPS.get(code_str)
                if not swapped_code:
                    swapped_code = "500" if code_str.startswith("2") else "200"

                if swapped_code == code_str:
                    swapped_code = "500" if code_str != "500" else "200"

                target_path = f"paths.{path_str}.{method}.responses.{code_str}"
                mutated = copy.deepcopy(contract)

                # Заменяем статус-код в объекте responses
                mut_responses = copy.deepcopy(responses)
                val = mut_responses.pop(status_code)
                mut_responses[swapped_code] = val
                _set_by_path(
                    mutated,
                    ["paths", path_str, method, "responses"],
                    mut_responses,
                )

                mutants.append(
                    Mutant(
                        mutant_id=f"MUT-STATUS-{counter:03d}",
                        operator=MutationType.STATUS_CODE_SWAP,
                        target_path=target_path,
                        description=(
                            f"Подмена статус-кода ответа {code_str} -> {swapped_code} "
                            f"в {method.upper()} {path_str}"
                        ),
                        original_value=code_str,
                        mutated_value=swapped_code,
                        mutated_contract=mutated,
                    )
                )
                counter += 1

    return mutants


def generate_mutants(
    contract: dict[str, Any],
    operators: list[MutationType] | None = None,
) -> list[Mutant]:
    """
    Сгенерировать мутантов API-контракта с использованием заданных операторов.

    Args:
        contract: Словарь со структурой OpenAPI-контракта.
        operators: Список операторов (по умолчанию применяются все три).

    Returns:
        Список объектов Mutant.
    """
    if operators is None:
        operators = list(MutationType)

    mutants: list[Mutant] = []

    operator_dispatch: dict[MutationType, Callable[[dict[str, Any]], list[Mutant]]] = {
        MutationType.TYPE_CHANGE: mutate_type_change,
        MutationType.REMOVE_REQUIRED: mutate_remove_required,
        MutationType.STATUS_CODE_SWAP: mutate_status_code_swap,
    }

    for op in operators:
        fn = operator_dispatch.get(op)
        if fn:
            mutants.extend(fn(contract))

    return mutants


def count_mutants_by_operator(mutants: list[Mutant]) -> dict[str, int]:
    """Подсчитать количество сгенерированных мутантов по каждому типу оператора."""
    counts: dict[str, int] = {op.value: 0 for op in MutationType}
    for m in mutants:
        counts[m.operator.value] = counts.get(m.operator.value, 0) + 1
    return counts


def calculate_mutation_score(
    total_mutants: int,
    killed_mutants: int,
    mutants_by_operator: dict[str, int] | None = None,
    killed_by_operator: dict[str, int] | None = None,
) -> MutationScoreResult:
    """
    Рассчитать Mutation Score по количеству уничтоженных мутантов.

    Score = (Killed Mutants / Total Mutants) * 100%

    Args:
        total_mutants: Всего сгенерированных мутантов.
        killed_mutants: Уничтоженные мутанты (тесты упали).
        mutants_by_operator: Статистика распределения всех мутантов по операторам.
        killed_by_operator: Статистика уничтоженных мутантов по операторам.

    Returns:
        MutationScoreResult.
    """
    if total_mutants < 0 or killed_mutants < 0:
        raise ValueError("Количество мутантов не может быть отрицательным.")

    if killed_mutants > total_mutants:
        raise ValueError(
            f"Количество уничтоженных мутантов ({killed_mutants}) "
            f"не может превышать общее ({total_mutants})."
        )

    if total_mutants == 0:
        score = 100.0
        survived = 0
    else:
        score = round((killed_mutants / total_mutants) * 100.0, 2)
        survived = total_mutants - killed_mutants

    return MutationScoreResult(
        total_mutants=total_mutants,
        killed_mutants=killed_mutants,
        survived_mutants=survived,
        mutation_score_percent=score,
        mutants_by_operator=mutants_by_operator or {},
        killed_by_operator=killed_by_operator or {},
    )


def _set_by_path(d: dict[str, Any], path: list[str], val: Any) -> None:
    """Установить значение по заданному пути в словаре/списке."""
    curr: Any = d
    for step in path[:-1]:
        if isinstance(curr, dict):
            curr = curr[step]
        elif isinstance(curr, list):
            curr = curr[int(step)]
        else:
            return

    last_step = path[-1]
    if isinstance(curr, dict):
        curr[last_step] = val
    elif isinstance(curr, list):
        curr[int(last_step)] = val
