"""Dependency-edge extraction: internal import graphs for Go, Python, JS/TS.

Edges are directory -> directory, deduplicated with counts. Only imports that
resolve inside the repo are kept (external/third-party imports are ignored).
"""

import ast
import os
import re
from collections import Counter

from .code import walk

GO_BLOCK_RE = re.compile(r"^import\s*\(\s*(.*?)\s*\)", re.S | re.M)
GO_SINGLE_RE = re.compile(r'^import\s+(?:\w+\s+)?"([^"]+)"', re.M)
GO_QUOTED_RE = re.compile(r'"([^"]+)"')
GO_MODULE_RE = re.compile(r"^module\s+(\S+)", re.M)
JS_IMPORT_RE = re.compile(
    r"""(?:from\s+|require\(\s*|import\(\s*|^\s*import\s+)['"](\.{1,2}/[^'"]+)['"]""",
    re.M)
JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs")


def collect(repo):
    """Return [{"from": dir, "to": dir, "lang": lang, "count": n}] sorted by count."""
    counter = Counter()
    tree = {rel: files for rel, files in walk(repo)}
    dirs = set(tree)

    _go_edges(repo, tree, counter)
    _py_edges(repo, tree, dirs, counter)
    _js_edges(repo, tree, dirs, counter)

    edges = [{"from": f or "(root)", "to": t or "(root)", "lang": lang, "count": n}
             for (f, t, lang), n in counter.items()]
    edges.sort(key=lambda e: -e["count"])
    return edges


def _read(repo, rel, name):
    try:
        with open(os.path.join(repo, rel, name), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# -- Go ----------------------------------------------------------------------

def _go_edges(repo, tree, counter):
    roots = {}  # rel dir of go.mod -> module path
    for rel, files in tree.items():
        if "go.mod" in files:
            m = GO_MODULE_RE.search(_read(repo, rel, "go.mod"))
            if m:
                roots[rel] = m.group(1)
    if not roots:
        return
    for rel, files in tree.items():
        root = _owning_root(rel, roots)
        if root is None:
            continue
        modpath = roots[root]
        for f in files:
            if not f.endswith(".go") or f.endswith("_test.go"):
                continue
            src = _read(repo, rel, f)
            imports = GO_SINGLE_RE.findall(src)
            for block in GO_BLOCK_RE.findall(src):
                imports.extend(GO_QUOTED_RE.findall(block))
            for imp in imports:
                if imp != modpath and not imp.startswith(modpath + "/"):
                    continue
                sub = imp[len(modpath):].lstrip("/")
                target = _norm(os.path.join(root, sub) if root else sub)
                if target != rel and target in tree:
                    counter[(rel, target, "Go")] += 1


def _owning_root(rel, roots):
    best = None
    for root in roots:
        if rel == root or root == "" or rel.startswith(root + "/"):
            if best is None or len(root) > len(best):
                best = root
    return best


# -- Python ------------------------------------------------------------------

def _py_edges(repo, tree, dirs, counter):
    # Internal top-level packages: dirs at repo root or under src/ holding .py files.
    pkg_map = {}
    for rel in dirs:
        parts = rel.split("/") if rel else []
        if not parts:
            continue
        if len(parts) == 1 or (len(parts) == 2 and parts[0] == "src"):
            if any(f.endswith(".py") for f in tree.get(rel, ())):
                pkg_map[parts[-1]] = rel
    for rel, files in tree.items():
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                node = ast.parse(_read(repo, rel, f))
            except SyntaxError:
                continue
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Import):
                    for alias in stmt.names:
                        _py_edge(rel, alias.name, 0, pkg_map, dirs, counter)
                elif isinstance(stmt, ast.ImportFrom):
                    _py_edge(rel, stmt.module or "", stmt.level, pkg_map, dirs, counter)


def _py_edge(rel, module, level, pkg_map, dirs, counter):
    if level:  # relative import
        base = rel
        for _ in range(level - 1):
            base = os.path.dirname(base)
        target = _norm(os.path.join(base, *module.split("."))) if module else base
    else:
        head = module.split(".")[0]
        if head not in pkg_map:
            return
        target = _norm(os.path.join(os.path.dirname(pkg_map[head]),
                                    *module.split(".")))
    target = _existing_dir(target, dirs)
    if target is not None and target != rel:
        counter[(rel, target, "Python")] += 1


# -- JS / TS -----------------------------------------------------------------

def _js_edges(repo, tree, dirs, counter):
    for rel, files in tree.items():
        for f in files:
            if not f.endswith(JS_EXTS) or f.endswith(".d.ts"):
                continue
            for imp in JS_IMPORT_RE.findall(_read(repo, rel, f)):
                target = _existing_dir(_norm(os.path.join(rel, imp)), dirs)
                if target is not None and target != rel:
                    counter[(rel, target, "JS/TS")] += 1


# -- helpers -----------------------------------------------------------------

def _norm(p):
    p = os.path.normpath(p).replace(os.sep, "/")
    return "" if p == "." else p


def _existing_dir(path, dirs):
    """Resolve an import target to a known repo dir (itself, or its parent
    when the import points at a file)."""
    if path in dirs:
        return path
    parent = _norm(os.path.dirname(path))
    if parent in dirs:
        return parent
    return None
