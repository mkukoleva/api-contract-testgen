"""4.1.7.2 recommendations from run evidence. No metrics or model calls."""

METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def build_recommendations(contract, run=None, *, suspected_defects=(), environmental=(), review_required=(),
                          scope="Ответы доверенного api_client; другие клиенты не отслеживаются. Это рекомендации, не расчёт метрик."):
    # generator.__init__ exports pipeline, which imports this report module.
    # Defer the matching helper until both modules have finished importing.
    from ..generator.context import match_contract_path
    operations = {}
    for path, item in contract.get("paths", {}).items():
        if not path.startswith("/") or not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() in METHODS and isinstance(operation, dict):
                responses = operation.get("responses", {})
                operations[(method.upper(), path)] = {str(code).upper() for code in responses
                    if str(code).lower() == "default" or (
                        len(str(code)) == 3 and str(code)[0] in "12345"
                        and (str(code)[1:].isdigit() or str(code)[1:].upper() == "XX"))} if isinstance(responses, dict) else set()
    items = []
    if (run or {}).get("status") in {"infrastructure_error", "timeout", "interrupted"} and not environmental:
        items.append({"kind": "environment", "action": "Проверить Docker, готовность стенда и лимит времени; повторить сохранённые тесты без изменения assert и генерации."})
    if (run or {}).get("status") == "generation_error":
        items.append({"kind": "generation", "action": "Проверить диагностику генерации, формат ответа модели и лимит токенов; результаты API ещё не получены."})
    for defect in suspected_defects:
        items.append({"kind": "suspected_discrepancy", "nodeid": defect.get("nodeid"),
            "evidence": defect.get("reason"), "action": "Сопоставить фактический ответ с контрактом и проверить сервис; не ослаблять assert."})
    for error in environmental:
        items.append({"kind": "environment", "nodeid": error.get("nodeid"),
            "action": "Восстановить доступность стенда и повторить сохранённые тесты без генерации."})
    for target in review_required:
        items.append({"kind": "review_required", "target": target,
            "action": "Проверить неоднозначную ошибку тестового кода вручную; автоматическое изменение отклонено."})
    observations = (run or {}).get("http_observations")
    observed_operations, observed_codes = set(), set()
    for observation in observations or ():
        method, path, status = observation["method"].upper(), observation["path"], str(observation["status_code"])
        matches = [key for key in operations if key[0] == method and match_contract_path(key[1], path)]
        exact = next((key for key in matches if key[1] == path), None)
        key = exact or (matches[0] if len(matches) == 1 else None)
        if key is None:
            items.append({"kind": "contract_clarification", "operation": f"{method} {path}",
                "action": "Проверить, должна ли операция присутствовать в контракте; не добавлять её автоматически."})
            continue
        observed_operations.add(key)
        codes = operations[key]
        matched = {code for code in codes if code == status or code == status[0] + "XX"}
        if not matched and "DEFAULT" in codes:
            matched = {"DEFAULT"}
        observed_codes.update((key, code) for code in matched)
        if not matched:
            items.append({"kind": "contract_clarification", "operation": f"{key[0]} {key[1]}",
                "status_code": status, "action": "Подтвердить причину недокументированного ответа; уточнить контракт только если поведение сервиса признано правильным."})
    complete = (run or {}).get("status") == "completed" and (run or {}).get("http_observations_complete", True)
    available = observations is not None and complete
    missing_operations = [f"{m} {p}" for m, p in sorted(operations) if (m, p) not in observed_operations] if available else None
    missing_responses = [{"operation": f"{m} {p}", "status_code": code}
        for m, p in sorted(operations) for code in sorted(operations[(m, p)])
        if ((m, p), code) not in observed_codes] if available else None
    if available and (missing_operations or missing_responses):
        items.append({"kind": "add_scenarios", "action": "Дополнить сценарии для операций и ответов, не встречавшихся в записанных вызовах api_client."})
    if not available:
        items.append({"kind": "insufficient_evidence", "action": "Для списка непроверенных операций нужны завершённый прогон и HTTP-наблюдения; наличие кода теста этого не подтверждает."})
    return {"basis": "observed_http" if available else "unavailable",
        "scope": scope,
        "uncovered_operations": missing_operations, "uncovered_responses": missing_responses, "items": items}


def compact_recommendations(result):
    if not result:
        return {}
    value = {key: result[key] for key in ("basis", "scope") if key in result}
    for key in ("uncovered_operations", "uncovered_responses", "items"):
        entries = result.get(key)
        value[key] = entries[:5] if isinstance(entries, list) else entries
        value[key + "_total"] = len(entries) if isinstance(entries, list) else None
    value["details_in_report"] = True
    return value
