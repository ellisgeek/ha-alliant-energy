"""The backend client must stay movable to its own project."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).parent.parent / "custom_components" / "alliant_energy"
# The client: the backend's request format (api.py) and each utility's
# deployment values (utility.py). Everything else is the integration.
CLIENT = {"api", "utility"}


def _imports(module: str) -> set[str]:
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def test_client_imports_neither_home_assistant_nor_the_integration() -> None:
    for module in CLIENT:
        imports = _imports(module)
        assert not {name for name in imports if name.startswith("homeassistant")}, module
        relative = {name.lstrip(".") for name in imports if name.startswith(".")}
        assert relative <= CLIENT, f"{module} imports {relative - CLIENT}"
