"""Per-file fact extraction: one read per file, memoized and cacheable.

Collectors ask a Store for facts instead of opening files themselves. That
buys two things. Every source file is read and parsed exactly once per scan —
Java/Kotlin files used to be read three times (LOC, package index, imports)
and Python twice. And the records are JSON-serializable by construction, which
is what lets a warm scan replay them instead of re-parsing.

A record holds only *raw* per-file facts: language, LOC, import specifiers,
package declarations. Resolving those specifiers to repo directories stays
out on purpose — resolution depends on repo-wide state that no single file
owns (tsconfig `paths`, workspace package names, crate names, the JVM package
index, the directory set itself), so a cached edge would go stale the moment
an unrelated file moved, while a cached specifier stays true.
"""

import ast
import os
import re

# Bumped whenever an extractor's output shape or semantics change, so records
# written by an older repokg are discarded rather than replayed.
FACTS_VERSION = 1

LANG_BY_EXT = {
    ".go": "Go", ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby",
    ".php": "PHP", ".cs": "C#", ".c": "C", ".cpp": "C++", ".cc": "C++",
    ".h": "C/C++", ".hpp": "C++", ".sol": "Solidity", ".swift": "Swift",
    ".scala": "Scala", ".ex": "Elixir", ".exs": "Elixir", ".zig": "Zig",
    ".lua": "Lua", ".dart": "Dart", ".vue": "Vue", ".svelte": "Svelte",
    ".sql": "SQL", ".sh": "Shell", ".proto": "Protobuf", ".tf": "Terraform",
    ".yaml": "YAML", ".yml": "YAML", ".html": "HTML", ".css": "CSS",
}

MAX_FILE_BYTES = 2_000_000

GO_BLOCK_RE = re.compile(r"^import\s*\(\s*(.*?)\s*\)", re.S | re.M)
GO_SINGLE_RE = re.compile(r'^import\s+(?:\w+\s+)?"([^"]+)"', re.M)
GO_QUOTED_RE = re.compile(r'"([^"]+)"')
JS_IMPORT_RE = re.compile(
    r"""(?:from\s+|require\(\s*|import\(\s*|^\s*import\s+)['"]([^'"]+)['"]""",
    re.M)
# First path segment of a `use` declaration (also `pub use`, `pub(crate) use`).
RUST_USE_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+(?:::)?([A-Za-z_][A-Za-z0-9_]*)", re.M)
# Full `use` path with an optional one-level brace group:
# `use crate::a::b;` / `use crate::{a::b, c};` -> ("crate::a::b", None) / ("crate::", "a::b, c")
RUST_USE_PATH_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+(?:::)?([A-Za-z_][\w:]*)(?:\{([^}]*)\})?",
    re.M)
# `package a.b.c;` (Java) / `package a.b.c` (Kotlin, no semicolon).
JVM_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;?\s*$", re.M)
# `import a.b.C;` / `import static a.b.C.m;` / `import a.b.*;` (captured with a
# trailing dot, stripped by the consumer) / Kotlin `import a.b.C as D`.
JVM_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([A-Za-z_][\w.]*)", re.M)

JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs")
JVM_EXTS = (".java", ".kt")


def _go(text):
    imports = GO_SINGLE_RE.findall(text)
    for block in GO_BLOCK_RE.findall(text):
        imports.extend(GO_QUOTED_RE.findall(block))
    return {"go_imports": imports}


def _py(text):
    try:
        node = ast.parse(text)
    except (SyntaxError, ValueError):  # ValueError: null bytes on py<=3.11
        return {}
    imports = []
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Import):
            imports.extend([alias.name, 0] for alias in stmt.names)
        elif isinstance(stmt, ast.ImportFrom):
            imports.append([stmt.module or "", stmt.level])
    return {"py_imports": imports}


def _js(text):
    return {"js_imports": JS_IMPORT_RE.findall(text)}


def _rust(text):
    return {"rust_roots": RUST_USE_RE.findall(text),
            "rust_paths": [list(m) for m in RUST_USE_PATH_RE.findall(text)]}


def _jvm(text):
    m = JVM_PACKAGE_RE.search(text)
    return {"jvm_package": m.group(1) if m else None,
            "jvm_imports": JVM_IMPORT_RE.findall(text)}


_EXTRACTORS = {".go": _go, ".py": _py, ".rs": _rust}
_EXTRACTORS.update((ext, _js) for ext in JS_EXTS)
_EXTRACTORS.update((ext, _jvm) for ext in JVM_EXTS)


def _lines(data):
    """Physical lines, or 0 for binary content."""
    if b"\0" in data[:1024]:  # binary
        return 0
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _decode(data):
    """utf-8 with replacement and universal newlines — byte-for-byte what the
    collectors used to see when they opened files in text mode themselves."""
    text = data.decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


class Store:
    """Memoized per-file facts for a single scan of `repo`.

    `facts()` returns a cacheable record; `text()` returns raw content for the
    handful of metadata files (go.mod, Cargo.toml, tsconfig.json,
    package.json, pnpm-workspace.yaml) whose contents describe the repo rather
    than one file, and which several collectors would otherwise re-read.

    The two are memoized separately, so a path asked for both ways is opened
    twice. Only `.yaml`/`.yml` metadata (pnpm-workspace.yaml) is in both sets,
    which is one extra read per repo — not worth a shared byte cache, since
    that would mean holding every source file in memory for the whole scan.

    An optional `cache` supplies records from a previous scan for files that
    have not changed, in which case those files are never opened. `parses`
    counts the files this scan actually extracted facts from, and is what a
    warm scan of an untouched repo has to drive to zero; `reads` counts every
    open, including the metadata files that are always re-read.
    """

    def __init__(self, repo, cache=None):
        self.repo = repo
        self.cache = cache
        self.reads = 0
        self.parses = 0
        self._facts = {}
        self._text = {}

    def facts(self, rel, name):
        key = (rel + "/" + name) if rel else name
        rec = self._facts.get(key)
        if rec is not None:
            return rec
        rec = self.cache.replay(key) if self.cache is not None else None
        if rec is None:
            rec = self._extract(key)
            if rec and self.cache is not None:
                self.cache.record(key, rec)
        if rec:  # only files with a known language or extractor are kept
            self._facts[key] = rec
        return rec

    def text(self, rel, name):
        key = (rel + "/" + name) if rel else name
        if key not in self._text:
            self._text[key] = _decode(self._read(key))
        return self._text[key]

    def _extract(self, key):
        ext = os.path.splitext(key)[1].lower()
        lang = LANG_BY_EXT.get(ext)
        parse = _EXTRACTORS.get(ext)
        if lang is None and parse is None:
            return {}
        self.parses += 1
        rec = {"lang": lang} if lang else {}
        if parse is None:
            rec["loc"] = self._loc_only(key)
            return rec
        data = self._read(key)
        # An extractor needs the whole file regardless, so the size cap can
        # only be applied after the read here. The resulting LOC is the same.
        rec["loc"] = 0 if len(data) > MAX_FILE_BYTES else _lines(data)
        rec.update(parse(_decode(data)))
        return rec

    def _loc_only(self, key):
        """LOC for a language with no import extractor. Oversized files are
        skipped on their stat, without ever being opened."""
        try:
            if os.path.getsize(self._path(key)) > MAX_FILE_BYTES:
                return 0
        except OSError:
            return 0
        return _lines(self._read(key))

    def _path(self, key):
        return os.path.join(self.repo, key.replace("/", os.sep))

    def _read(self, key):
        self.reads += 1
        try:
            with open(self._path(key), "rb") as f:
                return f.read()
        except OSError:
            return b""
