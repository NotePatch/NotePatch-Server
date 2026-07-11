from __future__ import annotations

import ast
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "notepatch"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_platform_does_not_depend_on_domain_modules():
    violations = []
    for path in (SOURCE_ROOT / "platform").rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith("notepatch.modules"):
                violations.append(f"{path.name}: {imported}")
    assert violations == []


def test_api_routes_do_not_import_infrastructure_clients_directly():
    forbidden = ("boto3", "botocore", "redis", "docker")
    violations = []
    for path in (SOURCE_ROOT / "modules").glob("*/api/*.py"):
        for imported in _imports(path):
            if imported == forbidden or imported.startswith(tuple(f"{name}." for name in forbidden)):
                violations.append(f"{path}: {imported}")
    assert violations == []


def test_legacy_app_package_is_not_referenced():
    violations = []
    for path in SOURCE_ROOT.rglob("*.py"):
        for imported in _imports(path):
            if imported == "app" or imported.startswith("app."):
                violations.append(f"{path}: {imported}")
    assert violations == []


def test_large_runtime_and_executor_modules_stay_split():
    limits = {
        SOURCE_ROOT / "modules/tasks/services/executor.py": 200,
        SOURCE_ROOT / "modules/ai/services/runtime.py": 150,
        SOURCE_ROOT / "modules/learning/services/workflow.py": 550,
    }
    for path, limit in limits.items():
        assert len(path.read_text(encoding="utf-8").splitlines()) <= limit
