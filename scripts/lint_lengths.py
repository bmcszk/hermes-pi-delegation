"""AST-based length limits: every function <=100 lines, every file <=1000 lines."""

import argparse
import ast
import sys
from pathlib import Path

MAX_FUNCTION_LINES = 100
MAX_FILE_LINES = 1000
SKIP_DIRS = {".git", "__pycache__", ".hermes"}


def _node_lines(node: ast.AST) -> int:
    return node.end_lineno - node.lineno + 1


def find_violations(paths: list[Path]) -> list[str]:
    violations = []
    roots = paths
    targets: list[Path] = []
    for root in roots:
        if root.is_dir():
            targets.extend(_py_files_under(root))
        else:
            targets.append(root)
    for path in targets:
        violations.extend(_check_file(path))
    return sorted(violations)


def _py_files_under(root: Path) -> list[Path]:
    files: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for child in sorted(current.iterdir()):
            if child.is_dir():
                if child.name not in SKIP_DIRS:
                    stack.append(child)
            elif child.suffix == ".py":
                files.append(child)
    return files


def _check_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.count("\n") + (0 if source.endswith("\n") or not source else 1)
    violations = []
    if lines > MAX_FILE_LINES:
        violations.append(f"{path}:1: <file> - {lines} lines")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = _node_lines(node)
            if length > MAX_FUNCTION_LINES:
                violations.append(f"{path}:{node.lineno}: {node.name} - {length} lines")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["."], type=Path)
    args = parser.parse_args()
    roots = [Path(p) for p in args.paths]
    violations = find_violations(roots)
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
