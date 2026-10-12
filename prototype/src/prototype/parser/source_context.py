"""Bounded, static Python/FastAPI context. Never import or execute service code."""

import ast
import hashlib
from pathlib import Path
import tokenize
import io

from .contract_input import ContractInputError

MAX_FILES = 100
MAX_FILE_BYTES = 256 * 1024
MAX_CONTEXT_CHARS = 6000
_SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist", ".pytest_cache"}
_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def read_source_context(path: Path | str | None, contract: dict) -> dict:
    """Return context text and metadata; unavailable optional context never executes code.

    Literal FastAPI/APIRouter decorators select routes; local functions/classes
    referenced by those handlers supply additional context. Dynamic routing is
    reported as a limitation, rather than guessed or executed.
    """
    if path is None:
        return {"text": "", "metadata": {"provided": False, "files": [], "warnings": []}}
    root = Path(path).resolve()
    if not root.exists() or not (root.is_dir() or root.suffix.lower() == ".py"):
        raise ContractInputError("Исходники: укажите существующий Python-файл или каталог.")
    base = root if root.is_dir() else root.parent
    warnings, sources = [], []
    truncated = False
    # Prune ignored directories before recursion, not after traversing a venv.
    def files(directory):
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            if child.is_symlink() or child.name in _SKIP or child.name.startswith("."):
                continue
            if child.is_dir():
                yield from files(child)
            elif child.suffix.lower() == ".py":
                yield child
    candidates = files(root) if root.is_dir() else iter([root])
    operations = {(method.upper(), route) for route, item in contract.get("paths", {}).items()
                  if isinstance(item, dict) for method in item if method.lower() in _METHODS}
    definitions = {}
    handlers = []
    framework = "Python"
    for index, file in enumerate(candidates):
        if index >= MAX_FILES:
            truncated = True
            warnings.append("Число Python-файлов превышает лимит 100.")
            break
        relative = file.relative_to(base).as_posix()
        try:
            if file.stat().st_size > MAX_FILE_BYTES:
                warnings.append(f"{relative}: превышен лимит 256 KiB; файл пропущен.")
                continue
            raw = file.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                continue
            # Respect Python encoding declarations without importing the module.
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            text = raw.decode(encoding)
            tree = ast.parse(text)
        except (OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
            warnings.append(f"{relative}: не удалось статически разобрать Python; файл пропущен.")
            continue
        source = {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "text": text}
        sources.append(source)
        prefixes = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
                call = node.value
                name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
                if name in {"FastAPI", "APIRouter"}:
                    framework = "Python/FastAPI"
                    prefix = next((kw.value.value for kw in call.keywords if kw.arg == "prefix"
                                   and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)), "")
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            prefixes[target.id] = prefix
        mounts = []
        for node in tree.body:
            call = node.value if isinstance(node, ast.Expr) else None
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "include_router":
                parent = call.func.value.id if isinstance(call.func.value, ast.Name) else None
                router_node = call.args[0] if call.args else next((kw.value for kw in call.keywords if kw.arg == "router"), None)
                prefix_node = next((kw.value for kw in call.keywords if kw.arg == "prefix"), ast.Constant(value=""))
                if parent in prefixes and isinstance(router_node, ast.Name) and router_node.id in prefixes \
                        and isinstance(prefix_node, ast.Constant) and isinstance(prefix_node.value, str):
                    mounts.append((parent, router_node.id, prefix_node.value))
                else:
                    warnings.append(f"{relative}: include_router требует межмодульного или динамического разрешения.")
        if len(mounts) > 64:
            mounts = mounts[:64]
            warnings.append(f"{relative}: превышен лимит 64 include_router.")
            truncated = True
        effective = {}
        def route_prefixes(name, active=()):
            nonlocal truncated
            if name in active or len(active) > 16:
                warnings.append(f"{relative}: цикл или слишком глубокая цепочка include_router.")
                return set()
            if name in effective:
                return effective[name]
            result = {prefixes[name]}
            for parent, router, extra in mounts:
                if router == name:
                    for parent_prefix in sorted(route_prefixes(parent, (*active, name))):
                        if len(result) >= 32:
                            truncated = True
                            warnings.append(f"{relative}: число префиксов маршрута превышает лимит 32.")
                            break
                        result.add(parent_prefix + extra + prefixes[name])
            effective[name] = result
            return result
        for name in prefixes:
            route_prefixes(name)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            key = (relative, node.name)
            definitions[key] = (source, node)
            if isinstance(node, ast.ClassDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                method = decorator.func.attr
                route_node = decorator.args[0] if decorator.args else next(
                    (kw.value for kw in decorator.keywords if kw.arg == "path"), None)
                if method not in _METHODS or not isinstance(route_node, ast.Constant) or not isinstance(route_node.value, str):
                    continue
                framework = "Python/FastAPI"
                owner = decorator.func.value.id if isinstance(decorator.func.value, ast.Name) else ""
                routes = {prefix + route_node.value for prefix in effective.get(owner, {""})}
                if any((method.upper(), route) in operations for route in routes):
                    handlers.append(key)
    selected = list(dict.fromkeys(handlers))
    if not selected:
        warnings.append("Обработчики операций контракта не найдены; переданы только сигнатуры Python.")
        # Plain Python and dynamic routes still provide useful bounded context.
        fallback = True
        selected = list(definitions)
    else:
        fallback = False
        # Resolve only unambiguous local definitions; do not pull unrelated routes.
        position = 0
        while position < len(selected):
            key = selected[position]
            source, node = definitions[key]
            referenced = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            for name in sorted(referenced):
                same_file = (key[0], name)
                matches = [k for k in definitions if k[1] == name]
                candidate = same_file if same_file in definitions else matches[0] if len(matches) == 1 else None
                if candidate is not None and candidate not in selected:
                    selected.append(candidate)
            position += 1
    fragments, used_files, size = [], {}, 0
    for key in selected:
        source, node = definitions[key]
        start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
        lines = source["text"].splitlines()
        if fallback:
            fragment = f"{type(node).__name__} {node.name} (line {node.lineno})"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fragment += f"({ast.unparse(node.args)})"
        else:
            fragment = "\n".join(lines[start - 1:node.end_lineno])
        entry = f"# {source['path']}:{start}\n{fragment}\n\n"
        remaining = MAX_CONTEXT_CHARS - size
        if len(entry) > remaining:
            truncated = True
            if remaining > 100:
                fragments.append(entry[:remaining - 24] + "\n# (фрагмент обрезан)\n")
                used_files[source["path"]] = {k: source[k] for k in ("path", "sha256")}
            break
        fragments.append(entry)
        size += len(entry)
        used_files[source["path"]] = {k: source[k] for k in ("path", "sha256")}
    return {"text": "".join(fragments)[:MAX_CONTEXT_CHARS], "metadata": {
        "provided": True, "path": str(root), "framework": framework,
        "files": list(used_files.values()), "scanned_files": len(sources),
        "matched_handlers": len(handlers), "truncated": truncated, "warnings": warnings}}
