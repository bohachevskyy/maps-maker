"""render.py must not import anything that does I/O.

The three seams only stay separate if the boundary is checked mechanically:
render takes a manifest and an already-harvested result and returns a string.
The moment it can fetch or read on its own, callers stop having to be honest
about where the data came from.
"""

import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "mapsvc"

FORBIDDEN_MODULES = {
    "urllib", "http", "socket", "ssl", "ftplib", "requests", "httpx",
    "pathlib", "shutil", "tempfile", "subprocess", "sqlite3", "pickle",
    "os", "io",
}
FORBIDDEN_CALLS = {"open"}

# The seams that do touch the outside world.
FORBIDDEN_FIRST_PARTY = {"harvest", "api"}


def _imports(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
            if node.module.startswith("mapsvc"):
                found.update(f"mapsvc.{a.name}" for a in node.names)
    return found


def _first_party(tree: ast.AST) -> set[str]:
    """Names of sibling mapsvc modules this module pulls in."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "mapsvc":
                found.update(a.name for a in node.names)
            elif node.module.startswith("mapsvc."):
                found.add(node.module.split(".", 1)[1].split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mapsvc."):
                    found.add(alias.name.split(".", 1)[1].split(".")[0])
    return found


def _reachable_from(start: str) -> set[str]:
    """Every mapsvc module reachable from `start`, transitively."""
    seen: set[str] = set()
    queue = [start]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = PACKAGE / f"{name}.py"
        if not path.exists():
            continue
        queue.extend(_first_party(ast.parse(path.read_text())))
    return seen


def test_render_reaches_no_io_performing_module():
    modules = _reachable_from("render")
    assert not modules & FORBIDDEN_FIRST_PARTY, (
        f"render reaches I/O modules: {sorted(modules & FORBIDDEN_FIRST_PARTY)}"
    )

    for name in sorted(modules):
        path = PACKAGE / f"{name}.py"
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())

        offending = _imports(tree) & FORBIDDEN_MODULES
        assert not offending, f"mapsvc/{name}.py imports {sorted(offending)}"

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, (
                    f"mapsvc/{name}.py calls {node.func.id}()"
                )


def test_the_check_would_catch_a_violation():
    """Guard the guard: harvest.py does do I/O, so it must trip the same rules."""
    path = PACKAGE / "harvest.py"
    if not path.exists():
        return  # harvest lands in step 3
    tree = ast.parse(path.read_text())
    assert _imports(tree) & FORBIDDEN_MODULES, (
        "harvest.py performs no recognised I/O, so this test proves nothing"
    )


def test_package_init_stays_import_free():
    """A package-level import would sneak I/O into render behind the AST check."""
    tree = ast.parse((PACKAGE / "__init__.py").read_text())
    assert not _imports(tree), "mapsvc/__init__.py must not import anything"
