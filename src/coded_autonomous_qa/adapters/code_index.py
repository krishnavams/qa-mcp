"""Static code analysis with ``ast``: existing POM classes and the Python import graph.

This *reads* the existing framework; it never generates or rewrites POMs.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILES = 5000
MAX_BYTES = 1_000_000
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "build", "dist", ".mypy_cache"}
_TEST_FILE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|conftest\.py)$")
_LOCATOR_HINT = re.compile(
    r"(locator|selector|xpath|css|_btn|_button|_input|_field|_link|_txt|_dd|_lbl|^[A-Z0-9_]+$)", re.IGNORECASE
)


def iter_python_files(root: Path, limit: int = MAX_FILES) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        out.append(path)
        if len(out) >= limit:
            break
    return out


def is_test_file(rel: str) -> bool:
    return bool(_TEST_FILE.search(rel.replace("\\", "/")))


def _parse(path: Path) -> ast.Module | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except (SyntaxError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# POM classes
# ---------------------------------------------------------------------------
@dataclass
class PomClass:
    class_name: str
    file_path: str
    methods: list[str] = field(default_factory=list)
    locators: dict[str, str] = field(default_factory=dict)


def _literal(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:  # (By.ID, "x")
            strategy = ast.unparse(node.elts[0])
            second = _literal(node.elts[1])
            return f"{strategy}={second}" if second else None
        if isinstance(node, ast.Call) and node.args:  # page.locator("css") / By(...)
            first = _literal(node.args[0])
            return f"{ast.unparse(node.func)}({first})" if first else None
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, tuple) and len(value) == 2 and all(isinstance(v, str) for v in value):
        return f"{value[0]}={value[1]}"
    return None


def looks_like_pom(cls: ast.ClassDef, rel: str) -> bool:
    name = cls.name.lower()
    return (
        name.endswith(("page", "screen", "pageobject", "component", "dialog", "modal", "form"))
        or "/pages/" in f"/{rel}"
        or "/pom" in f"/{rel}"
        or "page_object" in rel
    )


def scan_pom_classes(root: Path, base: Path | None = None) -> list[PomClass]:
    base = base or root
    found: list[PomClass] = []
    for path in iter_python_files(root):
        rel = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
        tree = _parse(path)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not looks_like_pom(node, rel):
                continue
            pom = PomClass(class_name=node.name, file_path=str(path))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not item.name.startswith("__"):
                        pom.methods.append(item.name)
                    for sub in ast.walk(item):  # self.x = page.locator("...") in __init__/methods
                        if isinstance(sub, ast.Assign):
                            for tgt in sub.targets:
                                if (
                                    isinstance(tgt, ast.Attribute)
                                    and isinstance(tgt.value, ast.Name)
                                    and tgt.value.id == "self"
                                ):
                                    lit = _literal(sub.value)
                                    if lit:
                                        pom.locators[tgt.attr] = lit
                elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                    for tgt in targets:
                        if isinstance(tgt, ast.Name) and item.value is not None and _LOCATOR_HINT.search(tgt.id):
                            lit = _literal(item.value)
                            if lit:
                                pom.locators[tgt.id] = lit
            found.append(pom)
    return found


def _tokens(text: str) -> set[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    words = re.split(r"[^A-Za-z0-9]+", text.lower())
    stop = {"page", "screen", "pageobject", "the", "html", "index", "php", "aspx", "www", "com", "http", "https"}
    return {w for w in words if len(w) > 1 and w not in stop}


def name_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# Import graph
# ---------------------------------------------------------------------------
@dataclass
class ImportGraph:
    imports: dict[str, set[str]] = field(default_factory=dict)  # file -> repo files it imports
    names: dict[str, set[str]] = field(default_factory=dict)  # file -> imported names (classes etc.)
    truncated: bool = False

    @property
    def dependents(self) -> dict[str, set[str]]:
        rev: dict[str, set[str]] = defaultdict(set)
        for src, targets in self.imports.items():
            for t in targets:
                rev[t].add(src)
        return rev

    def impact(self, changed: list[str], max_depth: int = 6) -> dict[str, int]:
        """Files depending on changed files, with their distance (0 = changed)."""
        rev = self.dependents
        seen: dict[str, int] = {}
        queue: deque[str] = deque()
        for path in changed:
            seen[path] = 0
            queue.append(path)
        while queue:
            cur = queue.popleft()
            if seen[cur] >= max_depth:
                continue
            for dep in sorted(rev.get(cur, ())):
                if dep not in seen:
                    seen[dep] = seen[cur] + 1
                    queue.append(dep)
        return seen

    def transitive_imports(self, path: str, max_depth: int = 8) -> dict[str, int]:
        seen: dict[str, int] = {path: 0}
        queue: deque[str] = deque([path])
        while queue:
            cur = queue.popleft()
            if seen[cur] >= max_depth:
                continue
            for dep in sorted(self.imports.get(cur, ())):
                if dep not in seen:
                    seen[dep] = seen[cur] + 1
                    queue.append(dep)
        seen.pop(path)
        return seen


def _module_index(files: list[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for rel in files:
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        for prefix in ("", "src."):
            if prefix and mod.startswith(prefix):
                index.setdefault(mod[len(prefix) :], rel)
        index.setdefault(mod, rel)
    return index


def _resolve(name: str, index: dict[str, str]) -> str | None:
    parts = name.split(".")
    while parts:
        hit = index.get(".".join(parts))
        if hit:
            return hit
        parts.pop()
    return None


def build_import_graph(root: Path) -> ImportGraph:
    paths = iter_python_files(root)
    graph = ImportGraph(truncated=len(paths) >= MAX_FILES)
    files = [str(p.relative_to(root)).replace("\\", "/") for p in paths]
    index = _module_index(files)
    for path, rel in zip(paths, files, strict=True):
        tree = _parse(path)
        local: set[str] = set()
        names: set[str] = set()
        if tree is not None:
            pkg = rel.rsplit("/", 1)[0].replace("/", ".") if "/" in rel else ""
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if hit := _resolve(alias.name, index):
                            local.add(hit)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        base = pkg.split(".") if pkg else []
                        base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                        module = ".".join([*base, module] if module else base)
                    for alias in node.names:
                        names.add(alias.name)
                        hit = _resolve(f"{module}.{alias.name}", index) or _resolve(module, index)
                        if hit:
                            local.add(hit)
        local.discard(rel)
        graph.imports[rel] = local
        graph.names[rel] = names
    return graph
