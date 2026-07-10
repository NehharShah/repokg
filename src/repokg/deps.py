"""Dependency-edge extraction: internal import graphs for Go, Python, JS/TS, Rust.

Edges are directory -> directory, deduplicated with counts. Only imports that
resolve inside the repo are kept (external/third-party imports are ignored).
"""

import ast
import fnmatch
import json
import os
import re
from collections import Counter

from .code import walk

GO_BLOCK_RE = re.compile(r"^import\s*\(\s*(.*?)\s*\)", re.S | re.M)
GO_SINGLE_RE = re.compile(r'^import\s+(?:\w+\s+)?"([^"]+)"', re.M)
GO_QUOTED_RE = re.compile(r'"([^"]+)"')
GO_MODULE_RE = re.compile(r"^module\s+(\S+)", re.M)
JS_IMPORT_RE = re.compile(
    r"""(?:from\s+|require\(\s*|import\(\s*|^\s*import\s+)['"]([^'"]+)['"]""",
    re.M)
JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs")
# tsconfig wins over jsconfig when a dir carries both (jsconfig is the
# JS-only subset of the same format).
JS_CONFIG_FILES = ("tsconfig.json", "jsconfig.json")
# `- 'packages/*'` list item under the packages: key of pnpm-workspace.yaml
# (flat list of quoted-or-bare globs; a full YAML parser is not needed).
PNPM_ITEM_RE = re.compile(r"^\s*-\s*['\"]?([^'\"#\n]+?)['\"]?\s*$")
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
# JVM: a Maven/Gradle module is any dir carrying its own build file.
JVM_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
# `package a.b.c;` (Java) / `package a.b.c` (Kotlin, no semicolon).
JVM_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;?\s*$", re.M)
# `import a.b.C;` / `import static a.b.C.m;` / `import a.b.*;` (captured with a
# trailing dot, stripped in code) / Kotlin `import a.b.C as D`.
JVM_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([A-Za-z_][\w.]*)", re.M)
JVM_EXTS = (".java", ".kt")


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
    _jvm_edges(repo, tree, counter)

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
    """Relative imports resolve directly; non-relative specifiers go through
    the nearest tsconfig/jsconfig's `paths` aliases and `baseUrl`, then
    workspace package names (bare third-party imports match nothing in
    either and drop out)."""
    configs = _js_configs(repo, tree)
    workspaces = _js_workspaces(repo, tree)
    for rel, files in tree.items():
        cfg = _owning_root(rel, configs) if configs else None
        for f in files:
            if not f.endswith(JS_EXTS) or f.endswith(".d.ts"):
                continue
            for imp in JS_IMPORT_RE.findall(_read(repo, rel, f)):
                if imp.startswith("."):
                    target = _existing_dir(_norm(os.path.join(rel, imp)), dirs)
                elif imp.startswith("/"):
                    target = None
                else:
                    target = (_js_alias_resolve(imp, configs[cfg], dirs)
                              if cfg is not None else None)
                    if target is None and workspaces:
                        target = _js_workspace_resolve(imp, workspaces, dirs)
                if target is not None and target != rel:
                    counter[(rel, target, "JS/TS")] += 1


def _js_configs(repo, tree):
    """{config dir: (paths_base, patterns, bare_base)} from tsconfig/jsconfig.

    paths_base: dir that `paths` values resolve against — baseUrl when set,
    else the config's own dir (TS 4.4 semantics). patterns: (pattern, values)
    sorted most-specific-first (longest literal prefix before `*`), matching
    tsconfig's own tie-break. bare_base: baseUrl dir when explicitly set —
    bare specifiers additionally resolve against it, after `paths`.
    Configs defining neither baseUrl nor paths are skipped so an
    extends-only leaf config does not shadow the root's aliases
    (`extends` chains themselves are not followed).
    """
    configs = {}
    for rel, files in tree.items():
        name = next((c for c in JS_CONFIG_FILES if c in files), None)
        if name is None:
            continue
        data = _jsonc_loads(_read(repo, rel, name))
        opts = data.get("compilerOptions") if isinstance(data, dict) else None
        if not isinstance(opts, dict):
            continue
        base = opts.get("baseUrl")
        bare_base = _norm(os.path.join(rel, base)) if isinstance(base, str) else None
        patterns = []
        paths = opts.get("paths")
        if isinstance(paths, dict):
            for pat, vals in paths.items():
                if not isinstance(pat, str) or not isinstance(vals, list):
                    continue
                vals = [v for v in vals if isinstance(v, str)]
                if vals:
                    patterns.append((pat, vals))
        patterns.sort(  # exact patterns beat wildcards; longer prefix wins
            key=lambda pv: ("*" in pv[0], -pv[0].find("*"), pv[0]))
        if bare_base is None and not patterns:
            continue
        configs[rel] = (bare_base if bare_base is not None else rel,
                        patterns, bare_base)
    return configs


def _js_alias_resolve(imp, cfg, dirs):
    """Resolve a non-relative specifier through `paths` patterns (first
    existing substitution wins), then baseUrl-relative lookup; None when
    nothing grounds in the repo tree."""
    paths_base, patterns, bare_base = cfg
    for pat, vals in patterns:
        star = pat.find("*")
        if star == -1:
            if imp != pat:
                continue
            stem = ""
        else:
            pre, suf = pat[:star], pat[star + 1:]
            if not (len(imp) >= len(pre) + len(suf)
                    and imp.startswith(pre) and imp.endswith(suf)):
                continue
            stem = imp[len(pre):len(imp) - len(suf)]
        for val in vals:
            target = _existing_dir(
                _norm(os.path.join(paths_base, val.replace("*", stem, 1))),
                dirs)
            if target is not None:
                return target
    if bare_base is not None:
        target = _existing_dir(_norm(os.path.join(bare_base, imp)), dirs)
        # _existing_dir's parent fallback would resolve any bare specifier
        # whose first segment is missing ('react', 'lodash') to bare_base
        # itself — those are third-party packages, not internal edges.
        if target is not None and target != bare_base:
            return target
    return None


def _js_workspaces(repo, tree):
    """{package name: workspace dir} for monorepo workspaces.

    Globs come from package.json `workspaces` (npm/yarn; array or
    {packages: [...]}) and pnpm-workspace.yaml `packages:` lists — any dir
    may declare them, so nested workspace roots work too. A matched dir
    counts only if its own package.json carries a `name`; that name is what
    `import '@scope/pkg'` specifiers resolve against.
    """
    names = {}
    for rel, files in tree.items():
        globs = []
        if "package.json" in files:
            data = _jsonc_loads(_read(repo, rel, "package.json"))
            ws = data.get("workspaces") if isinstance(data, dict) else None
            if isinstance(ws, dict):
                ws = ws.get("packages")
            if isinstance(ws, list):
                globs.extend(g for g in ws if isinstance(g, str))
        if "pnpm-workspace.yaml" in files:
            globs.extend(_pnpm_globs(_read(repo, rel, "pnpm-workspace.yaml")))
        for g in globs:
            if g.startswith("!"):  # negation globs: rare, not modeled
                continue
            pat = _norm(os.path.join(rel, g)).split("/")
            for wdir, wfiles in tree.items():
                if not wdir or "package.json" not in wfiles:
                    continue
                if not _segments_match(pat, wdir.split("/")):
                    continue
                pkg = _jsonc_loads(_read(repo, wdir, "package.json"))
                name = pkg.get("name") if isinstance(pkg, dict) else None
                if isinstance(name, str) and name:
                    names[name] = wdir
    return names


def _pnpm_globs(text):
    """Glob items of the top-level packages: list in pnpm-workspace.yaml."""
    globs, in_packages = [], False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace() and line[0] != "-":
            in_packages = line.split(":")[0].strip() == "packages"
            continue
        if in_packages:
            m = PNPM_ITEM_RE.match(line)
            if m:
                globs.append(m.group(1))
    return globs


def _segments_match(pat, segs):
    """Segment-wise glob match: `*` spans one path segment (unlike fnmatch
    on the whole string), `**` spans any number."""
    if not pat:
        return not segs
    if pat[0] == "**":
        return any(_segments_match(pat[1:], segs[i:])
                   for i in range(len(segs) + 1))
    return (bool(segs) and fnmatch.fnmatchcase(segs[0], pat[0])
            and _segments_match(pat[1:], segs[1:]))


def _js_workspace_resolve(imp, workspaces, dirs):
    """Workspace package name -> its dir; a subpath import grounds inside
    the package dir when it exists there, else falls back to the dir itself
    (real subpaths map through package `exports`, which is not modeled)."""
    for name, wdir in workspaces.items():
        if imp == name:
            return wdir
        if imp.startswith(name + "/"):
            sub = imp[len(name) + 1:]
            target = _existing_dir(_norm(os.path.join(wdir, sub)), dirs)
            return target if target is not None else wdir
    return None


def _jsonc_loads(text):
    """json.loads for the JSONC dialect tsconfig uses: // and /* */ comments
    and trailing commas are stripped (string-aware, so comment-lookalikes
    inside string values survive). Returns None when still not valid JSON."""
    out, i, n = [], 0, len(text)
    while i < n:  # pass 1: strip comments
        c = text[i]
        if c == '"':
            j = _jsonc_string_end(text, i)
            out.append(text[i:j])
            i = j
        elif text[i:i + 2] == "//":
            while i < n and text[i] != "\n":
                i += 1
        elif text[i:i + 2] == "/*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(c)
            i += 1
    text = "".join(out)
    out, i, n = [], 0, len(text)
    while i < n:  # pass 2: drop trailing commas
        c = text[i]
        if c == '"':
            j = _jsonc_string_end(text, i)
            out.append(text[i:j])
            i = j
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # trailing comma: skip it
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    try:
        return json.loads("".join(out))
    except ValueError:
        return None


def _jsonc_string_end(text, i):
    """Index just past the string literal opening at text[i]."""
    j, n = i + 1, len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
        elif text[j] == '"':
            return j + 1
        else:
            j += 1
    return n


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


# -- Java / Kotlin -----------------------------------------------------------

def _jvm_modules(tree):
    """Dirs that are Maven/Gradle modules (each submodule carries its own
    build file, so no settings.gradle / <modules> parsing is needed)."""
    return sorted(rel for rel, files in tree.items()
                  if any(b in files for b in JVM_BUILD_FILES))


def _jvm_edges(repo, tree, counter):
    """Java/Kotlin edges: imports resolved against the package-declaration
    index by longest package prefix. Externals (java.*, kotlin.*, third-party)
    are never in the index, so they drop out naturally."""
    index = _jvm_package_index(repo, tree)
    if not index:
        return
    for rel, files in tree.items():
        for f in files:
            if not f.endswith(JVM_EXTS):
                continue
            lang = "Kotlin" if f.endswith(".kt") else "Java"
            for path in JVM_IMPORT_RE.findall(_read(repo, rel, f)):
                dirs = _jvm_resolve(path.rstrip("."), index)
                for target in _jvm_prefer_main(dirs):
                    if target != rel:
                        counter[(rel, target, lang)] += 1


def _jvm_resolve(path, index):
    """Longest package prefix of an import path present in the index.

    Full path first (wildcard imports name the package itself), then
    successively dropping trailing segments (class name, nested classes)."""
    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        dirs = index.get(".".join(parts[:i]))
        if dirs:
            return dirs
    return ()


def _jvm_prefer_main(dirs):
    """A package usually exists in both main and test source roots; edges
    into test dirs from an import would be fabricated, so prefer non-test
    dirs when the package is declared in several places."""
    if len(dirs) <= 1:
        return sorted(dirs)
    main = [d for d in dirs
            if not {"test", "tests"} & set(d.split("/"))]
    return sorted(main) or sorted(dirs)


def _jvm_package_index(repo, tree):
    """{package name -> set of dirs declaring it}, from `package` declarations.

    Declarations are language-level ground truth: they work for standard
    src/main/java layouts, Kotlin's collapsed directory convention, and plain
    src/ repos with no build files at all. Imports (follow-up PR) resolve
    against this index by longest package prefix, which makes external
    imports (java.*, kotlin.*, third-party) drop out naturally.
    """
    index = {}
    for rel, files in tree.items():
        for f in files:
            if not f.endswith(JVM_EXTS):
                continue
            m = JVM_PACKAGE_RE.search(_read(repo, rel, f))
            if m:
                index.setdefault(m.group(1), set()).add(rel)
    return index


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
