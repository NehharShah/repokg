"""Dependency-edge extraction: internal import graphs for Go, Python, JS/TS, Rust.

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
# [package] section of a Cargo.toml, up to the next table header.
CARGO_PACKAGE_RE = re.compile(r"^\[package\]\s*$(.*?)(?=^\[|\Z)", re.M | re.S)
CARGO_NAME_RE = re.compile(r'^\s*name\s*=\s*"([^"]+)"', re.M)
# First path segment of a `use` declaration (also `pub use`, `pub(crate) use`).
RUST_USE_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+(?:::)?([A-Za-z_][A-Za-z0-9_]*)", re.M)
# Full `use` path with an optional one-level brace group:
# `use crate::a::b;` / `use crate::{a::b, c};` -> ("crate::a::b", None) / ("crate::", "a::b, c")
RUST_USE_PATH_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+(?:::)?([A-Za-z_][\w:]*)(?:\{([^}]*)\})?",
    re.M)
# Built-in path roots that can never be workspace crates. `self`/`super` are
# relative module paths; `crate::` paths are resolved dir-level below.
RUST_SKIP = {"std", "core", "alloc", "crate", "self", "super"}


def collect(repo, tree=None):
    """Return [{"from": dir, "to": dir, "lang": lang, "count": n}] sorted by count.

    tree: optional pre-built {rel_dir: filenames} to avoid re-walking the repo.
    """
    counter = Counter()
    if tree is None:
        tree = {rel: files for rel, files in walk(repo)}
    dirs = set(tree)

    _go_edges(repo, tree, counter)
    _py_edges(repo, tree, dirs, counter)
    _js_edges(repo, tree, dirs, counter)
    _rust_edges(repo, tree, counter)

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
            except (SyntaxError, ValueError):  # ValueError: null bytes on py<=3.11
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


# -- Rust --------------------------------------------------------------------

def _rust_edges(repo, tree, counter):
    """Crate-level edges from `use <crate>::...` declarations.

    Crates are discovered from every Cargo.toml carrying a [package] name —
    workspace members each have their own Cargo.toml, so walking them covers
    workspaces and single-crate repos without parsing [workspace] members.
    Cargo crate names use `-`, Rust paths use `_`; names are normalized.
    `use crate::...` paths additionally resolve dir-level inside the owning
    crate's src/ tree (module dirs and `mod.rs`-style file modules).
    """
    crates = {}  # normalized crate name -> crate dir
    for rel, files in tree.items():
        if "Cargo.toml" not in files:
            continue
        pkg = CARGO_PACKAGE_RE.search(_read(repo, rel, "Cargo.toml"))
        if not pkg:
            continue  # virtual workspace manifest (no [package])
        name = CARGO_NAME_RE.search(pkg.group(1))
        if name:
            crates[name.group(1).replace("-", "_")] = rel
    if not crates:
        return
    # Longest crate dir owning a given file dir (handles nested crates).
    crate_dirs = sorted(crates.values(), key=len, reverse=True)
    for rel, files in tree.items():
        owner = next((c for c in crate_dirs
                      if rel == c or c == "" or rel.startswith(c + "/")), None)
        for f in files:
            if not f.endswith(".rs"):
                continue
            src = _read(repo, rel, f)
            for seg in RUST_USE_RE.findall(src):
                if seg in RUST_SKIP:
                    continue
                target = crates.get(seg)
                if target is not None and target != owner and target != rel:
                    counter[(rel, target, "Rust")] += 1
            if owner is not None:
                _rust_crate_paths(rel, owner, src, tree, counter)


def _rust_crate_paths(rel, owner, src, tree, counter):
    """Dir-level edges for `use crate::...` inside the owning crate's src/ tree.

    Only files under <crate>/src use `crate::` to mean the lib/bin root —
    files under tests/ or benches/ are their own crates, where `crate::`
    refers to themselves, so they are skipped for accuracy.
    """
    src_root = _norm(os.path.join(owner, "src"))
    if src_root not in tree:
        return
    if rel != src_root and not rel.startswith(src_root + "/"):
        return
    for path, group in RUST_USE_PATH_RE.findall(src):
        segments = [s for s in path.split("::") if s]
        if not segments or segments[0] != "crate":
            continue
        # Expand a one-level brace group into one path per item.
        if group:
            paths = [segments[1:] + [s for s in item.strip().split("::") if s]
                     for item in group.split(",") if item.strip()]
        else:
            paths = [segments[1:]]
        for segs in paths:
            target = _rust_resolve(src_root, segs, tree)
            if target is not None and target != rel:
                counter[(rel, target, "Rust")] += 1


def _rust_resolve(src_root, segments, tree):
    """Walk path segments as directories under src/; a segment that exists as
    `<seg>.rs` (file module) resolves to the directory holding it."""
    target = src_root
    for seg in segments:
        nxt = _norm(os.path.join(target, seg))
        if nxt in tree:
            target = nxt
        else:
            if seg + ".rs" not in tree.get(target, ()):
                return None  # not a module path we can ground in the tree
            break
    return target


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
