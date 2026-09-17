"""Targeted reads and Python AST symbols without putting whole files in context."""
import ast
from pathlib import Path


def read_file(path: Path, start: int = 1, end: int | None = None, symbol: str | None = None) -> str:
    if start < 1 or (end is not None and end < start):
        raise ValueError("Invalid line range")
    if symbol:
        if path.suffix != ".py":
            raise ValueError("Symbol extraction currently supports Python; use --start/--end for other files")
        source = path.read_text()
        nodes = [node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol]
        if not nodes:
            raise ValueError(f"Symbol {symbol!r} not found")
        if len(nodes) > 1:
            raise ValueError(f"Symbol {symbol!r} is ambiguous; use --start/--end")
        node = nodes[0]
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno
    end = end or start + 199
    result = []
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if number > end:
                break
            if number >= start:
                result.append(f"{number}: {line}")
    return "".join(result)


def symbols(path: Path) -> str:
    tree = ast.parse(path.read_text())
    return "\n".join(f"{node.lineno}-{node.end_lineno}: {type(node).__name__} {node.name}"
                     for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
