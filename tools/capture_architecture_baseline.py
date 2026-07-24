from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys


CENTRAL_MODULES = (
    "src/aetherstate/state.py",
    "src/aetherstate/pipeline.py",
    "src/aetherstate/control.py",
    "src/aetherstate/store.py",
)
WORKFLOW_PATH = ".github/workflows/ci.yml"
ROUTE_METHODS = {"get", "post", "put", "patch", "delete"}


def git_text(ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def tracked_python(ref: str) -> list[str]:
    output = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "src/aetherstate"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return sorted(path for path in output.splitlines() if path.endswith(".py"))


def git_value(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def physical_lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def is_broad_exception(handler: ast.ExceptHandler) -> str | None:
    if handler.type is None:
        return "bare"
    if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
        return "exception"
    if isinstance(handler.type, ast.Name) and handler.type.id == "BaseException":
        return "base_exception"
    return None


def module_counts(text: str) -> dict[str, int]:
    tree = ast.parse(text)
    functions = sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body
    )
    methods = sum(
        isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for member in node.body
    )
    return {
        "physical_lines": physical_lines(text),
        "functions": functions,
        "methods": methods,
    }


def route_decorator_count(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            receiver = decorator.func.value
            if (
                decorator.func.attr in ROUTE_METHODS
                and isinstance(receiver, ast.Name)
                and receiver.id == "router"
            ):
                count += 1
    return count


def state_dispatch_count(tree: ast.AST) -> int:
    apply_op = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_apply_op"
        ),
        None,
    )
    if apply_op is None:
        raise ValueError("state.py has no _apply_op dispatcher")
    return sum(
        isinstance(comparison.left, ast.Name)
        and comparison.left.id == "kind"
        and len(comparison.ops) == 1
        and isinstance(comparison.ops[0], ast.Eq)
        and len(comparison.comparators) == 1
        and isinstance(comparison.comparators[0], ast.Constant)
        and isinstance(comparison.comparators[0].value, str)
        for comparison in ast.walk(apply_op)
        if isinstance(comparison, ast.Compare)
    )


def linux_python_lanes(workflow: str) -> list[str]:
    for line in workflow.splitlines():
        stripped = line.strip()
        if stripped.startswith("matrix:") and "python:" in stripped:
            values = stripped.split("python:", 1)[1].split("[", 1)[1].split("]", 1)[0]
            return [value.strip().strip('"\'') for value in values.split(",")]
    raise ValueError("workflow has no Python matrix")


def capture(ref: str) -> dict[str, object]:
    commit = git_value("rev-parse", ref)
    tree = git_value("rev-parse", f"{commit}^{{tree}}")
    python_paths = tracked_python(commit)
    module_texts = {path: git_text(commit, path) for path in python_paths}
    parsed_modules = {path: ast.parse(text) for path, text in module_texts.items()}
    exception_counts = {"bare": 0, "exception": 0, "base_exception": 0}
    for parsed in parsed_modules.values():
        for node in ast.walk(parsed):
            if isinstance(node, ast.ExceptHandler):
                category = is_broad_exception(node)
                if category is not None:
                    exception_counts[category] += 1
    workflow = git_text(commit, WORKFLOW_PATH)
    state_tree = parsed_modules["src/aetherstate/state.py"]
    return {
        "schema": "aetherstate-architecture-baseline/1",
        "source": {
            "commit": commit,
            "tree": tree,
            "workflow": {
                "path": WORKFLOW_PATH,
                "sha256": hashlib.sha256(workflow.encode("utf-8")).hexdigest(),
                "linux_python_lanes": linux_python_lanes(workflow),
            },
        },
        "central_modules": list(CENTRAL_MODULES),
        "central_module_metrics": {
            path: module_counts(module_texts[path]) for path in CENTRAL_MODULES
        },
        "metrics": {
            "python_modules": len(python_paths),
            "python_physical_lines": sum(physical_lines(text) for text in module_texts.values()),
            "broad_exception_handlers": sum(exception_counts.values()),
            "bare_except_handlers": exception_counts["bare"],
            "exception_handlers": exception_counts["exception"],
            "base_exception_handlers": exception_counts["base_exception"],
            "route_decorators": sum(route_decorator_count(parsed) for parsed in parsed_modules.values()),
            "state_dispatch_branches": state_dispatch_count(state_tree),
        },
        "thresholds": {},
    }


def rendered_baseline(ref: str) -> str:
    return json.dumps(capture(ref), indent=2, sort_keys=True) + "\n"


def main(arguments: list[str]) -> int:
    if len(arguments) != 4 or arguments[0] != "--git-ref":
        raise SystemExit("usage: capture_architecture_baseline.py --git-ref REF (--output PATH | --check PATH)")
    ref = arguments[1]
    mode = arguments[2]
    path = Path(arguments[3])
    if mode == "--output":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered_baseline(ref), encoding="utf-8")
        return 0
    if mode == "--check":
        expected = rendered_baseline(ref)
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            raise SystemExit(f"FAIL architecture-baseline {git_value('rev-parse', ref)[:8]}")
        print(f"PASS architecture-baseline {git_value('rev-parse', ref)[:8]}")
        return 0
    raise SystemExit("usage: capture_architecture_baseline.py --git-ref REF (--output PATH | --check PATH)")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
