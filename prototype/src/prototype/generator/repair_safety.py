"""Compare code repairs without executing generated Python (TZ v3 M3).

Only recoverable header punctuation, known module imports and consistent
fixture aliases are normalized. Unknown repairs require review; assertions,
decorators, control flow and other tests remain structurally identical.
"""

import ast
import copy
import io
import re
import tokenize


_IMPORTS = {"pytest": "pytest", "requests": "requests",
            "pytes": "pytest", "request": "requests"}
_FIXTURES = {"client": "api_client", "http_client": "api_client",
             "session": "api_client", "baseurl": "base_url", "api_url": "base_url"}


def syntax_reference(source: str) -> ast.Module:
    """Recover missing colons in function/class headers, preserving all code.

    A malformed expression or ambiguous indentation has no trusted reference.
    It is rejected instead of trusting the model to invent the original check.
    """
    lines = source.splitlines(keepends=True)
    for _ in range(16):
        try:
            return ast.parse("".join(lines))
        except SyntaxError as exc:
            if exc.msg != "expected ':'" or not exc.lineno:
                raise ValueError("repair requires review: syntax cannot be recovered safely") from exc
            line = lines[exc.lineno - 1]
            if not re.match(r"\s*(?:async\s+)?(?:def|class)\s+\w+", line):
                raise ValueError("repair requires review: unsupported syntax repair") from exc
            # Tokenization distinguishes a comment from '#' inside a string.
            tokens = list(tokenize.generate_tokens(io.StringIO(line).readline))
            end = next((t.start[1] for t in tokens if t.type == tokenize.COMMENT), len(line.rstrip()))
            head = line[:end].rstrip()
            lines[exc.lineno - 1] = head + ":" + line[len(head):]
    raise ValueError("repair requires review: too many syntax errors")


def _imports(tree: ast.Module, *, allow_typos: bool) -> tuple[ast.Module, set[str]]:
    tree = copy.deepcopy(tree)
    recognized = set()
    for node in tree.body:
        if isinstance(node, ast.Import) and len(node.names) == 1:
            alias = node.names[0]
            module = _IMPORTS.get(alias.name)
            if module and (alias.asname or alias.name) == module and (allow_typos or alias.name == module):
                recognized.add(module)
                alias.name, alias.asname = module, None
    return tree, recognized


def _binding_names(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            yield node.id
        elif isinstance(node, ast.arg):
            yield node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for item in node.names:
                yield item.asname or item.name.split(".")[0]
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name


def validate_repair(before: ast.Module, after: ast.Module, *, function_path=(), message=""):
    from .repair import find_test_node

    if ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False):
        return  # A no-op is safe; the repeated-error gate stops the next round.
    before, old_imports = _imports(before, allow_typos=True)
    after, new_imports = _imports(after, allow_typos=False)
    if not old_imports <= new_imports:
        raise ValueError("repair requires review: existing imports were removed")
    loaded = {node.id for node in ast.walk(before) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    bound = set(_binding_names(before))
    if any(name not in loaded or name in bound for name in new_imports - old_imports):
        raise ValueError("repair requires review: import is unused or shadows a binding")
    # Preserve import order, docstring and future directives. Only new known
    # imports in the leading import block may be removed for AST comparison.
    prefix = []
    for index, node in enumerate(before.body):
        if (index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)) or (
                    isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            prefix.append(node)
        else:
            break
    if ast.dump(ast.Module(body=prefix, type_ignores=[]), include_attributes=False) != ast.dump(
        ast.Module(body=after.body[:len(prefix)], type_ignores=[]), include_attributes=False
    ):
        raise ValueError("repair requires review: module preamble changed")
    added = new_imports - old_imports
    seen = set()
    keep = []
    in_header = True
    for index, node in enumerate(after.body):
        if (isinstance(node, ast.Import) and len(node.names) == 1
                and node.names[0].name in added and node.names[0].asname is None):
            name = node.names[0].name
            if not in_header or name in seen or index < len(prefix):
                raise ValueError("repair requires review: new import is outside the import block")
            seen.add(name)
            continue
        if not isinstance(node, (ast.Import, ast.ImportFrom)) and not (
            index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            in_header = False
        keep.append(node)
    after.body = keep
    match = re.search(r"fixture ['\"]([^'\"]+)['\"] not found", message or "")
    if match and function_path:
        old = match.group(1)
        new = _FIXTURES.get(old)
        node = find_test_node(before, function_path)
        if new and node:
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if any(arg.arg == old for arg in args):
                body = ast.Module(body=node.body, type_ignores=[])
                if new in set(_binding_names(node)) or any(
                    isinstance(n, ast.Name) and n.id == new for n in ast.walk(node)
                ) or old in set(_binding_names(body)) or any(
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
                    for n in ast.walk(body)
                ):
                    raise ValueError("repair requires review: fixture alias has conflicting bindings")
                for arg in args:
                    if arg.arg == old:
                        arg.arg = new
                for child in ast.walk(body):
                    if isinstance(child, ast.Name) and child.id == old:
                        child.id = new
    if ast.dump(before, include_attributes=False) != ast.dump(after, include_attributes=False):
        raise ValueError("repair requires review: checks or execution structure changed")
