"""render.py must not reach anything that does I/O.

The three seams only stay separate if the boundary is checked mechanically:
render takes a manifest and an already-harvested result and returns a string.
The moment it can fetch or read on its own, callers stop having to be honest
about where the data came from.

The check walks every first-party module render can reach -- packages
included, since `from mapsvc import statistics` loads statistics/__init__.py and
whatever that pulls in -- and fails on an I/O import, an I/O builtin, or a
dynamic import, which would hide the rest of the call graph from the walk.
An earlier version only followed `<name>.py` files, so render calling
statistics.describe() (a cache read for OWID) went unnoticed.
"""

import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "mapsvc"

FORBIDDEN_MODULES = {
    "urllib", "http", "socket", "ssl", "ftplib", "requests", "httpx",
    "pathlib", "shutil", "tempfile", "subprocess", "sqlite3", "pickle",
    "os", "io", "glob", "zipfile", "gzip", "csv",
    # A dynamic import makes the rest of the graph invisible to this walk.
    "importlib",
}
FORBIDDEN_CALLS = {"open", "__import__", "exec", "eval"}

# First-party modules that touch the outside world. Reaching any of them is a
# violation even where the stdlib check would also catch it.
FORBIDDEN_FIRST_PARTY = {"harvest", "api", "fetch", "export", "pipeline", "agent",
                         "statistics", "cartography"}


def _source(module: str) -> pathlib.Path | None:
    """mapsvc.<module> -> its file: a module, or a package's __init__.py."""
    if not module:
        return PACKAGE / "__init__.py"
    parts = module.split(".")
    as_file = PACKAGE.joinpath(*parts).with_suffix(".py")
    if as_file.exists():
        return as_file
    as_package = PACKAGE.joinpath(*parts, "__init__.py")
    return as_package if as_package.exists() else None


def _first_party(tree: ast.AST) -> set[str]:
    """Dotted names, relative to mapsvc, of the first-party modules imported.

    `from mapsvc import statistics` names a package; `from mapsvc.statistics
    import owid` names a module inside one. Both are followed.
    """
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                # Not used in this codebase; resolving it is not worth the
                # code, and silently skipping it would open a hole.
                found.add("<relative import>")
                continue
            base = node.module or ""
            if base == "mapsvc":
                prefix = ""
            elif base.startswith("mapsvc."):
                prefix = base[len("mapsvc."):]
                found.add(prefix)
            else:
                continue
            for alias in node.names:
                candidate = f"{prefix}.{alias.name}" if prefix else alias.name
                if _source(candidate):
                    found.add(candidate)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mapsvc."):
                    found.add(alias.name[len("mapsvc."):])
    return found


def _stdlib_imports(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module.split(".")[0])
    return found


def _reachable_from(start: str) -> set[str]:
    """Every mapsvc module reachable from `start`, transitively, packages included."""
    seen: set[str] = set()
    queue = [start]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _source(name)
        if path is None:
            continue
        # Importing a.b runs a/__init__.py first.
        parent = name.rpartition(".")[0]
        if parent:
            queue.append(parent)
        queue.extend(_first_party(ast.parse(path.read_text())))
    return seen


def _violations(start: str) -> list[str]:
    modules = _reachable_from(start)
    problems = [f"reaches mapsvc.{m}" for m in sorted(modules)
                if m.split(".")[0] in FORBIDDEN_FIRST_PARTY or m == "<relative import>"]
    for name in sorted(modules):
        path = _source(name)
        if path is None:
            continue
        tree = ast.parse(path.read_text())
        offending = _stdlib_imports(tree) & FORBIDDEN_MODULES
        if offending:
            problems.append(f"mapsvc.{name} imports {sorted(offending)}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in FORBIDDEN_CALLS:
                problems.append(f"mapsvc.{name} calls {node.func.id}()")
    return problems


def test_render_reaches_no_io_performing_module():
    assert _violations("render") == []


def test_the_check_would_catch_a_violation():
    """Guard the guard: a module known to do I/O must trip the same rules."""
    assert _violations("harvest"), "harvest.py does I/O, so this test proves nothing"


def test_the_check_follows_packages():
    """`from mapsvc import statistics` must be walked into, not skipped.

    manifest.py imports the statistics and cartography packages, whose
    providers fetch over the network. The original check only looked for
    `<name>.py`, so a package import was invisible -- which is how render came
    to call statistics.describe().
    """
    reached = _reachable_from("manifest")
    assert {"statistics", "cartography"} <= reached
    problems = _violations("manifest")
    assert any("statistics" in p for p in problems)
    # statistics/__init__ loads its providers with importlib, which the walk
    # cannot follow, so the dynamic import itself is the violation.
    assert any("importlib" in p for p in problems)


def test_package_init_stays_import_free():
    """A package-level import would sneak I/O into render behind the AST check."""
    tree = ast.parse((PACKAGE / "__init__.py").read_text())
    assert not _stdlib_imports(tree) and not _first_party(tree), (
        "mapsvc/__init__.py must not import anything")
