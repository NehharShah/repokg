"""Code collectors: language breakdown, module discovery, LOC."""

import fnmatch
import os
import re

from .facts import Store

SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "out", "target", ".next",
    ".venv", "venv", "__pycache__", ".idea", ".vscode", ".repokg", "coverage",
    ".terraform", "third_party", ".tox", ".mypy_cache", ".ruff_cache",
    "site-packages", ".pytest_cache", ".cache",
}

# Languages whose presence makes a directory a "module" worth graphing.
MODULE_LANGS = {
    "Go", "Python", "TypeScript", "JavaScript", "Rust", "Java", "Kotlin",
    "Ruby", "PHP", "C#", "C", "C++", "Solidity", "Swift", "Scala", "Elixir",
    "Zig", "Lua", "Dart", "Vue", "Svelte",
}

ROOT_MARKERS = {
    "go.mod", "package.json", "pyproject.toml", "setup.py", "Cargo.toml",
    "pom.xml", "build.gradle", "composer.json", "Gemfile",
}

GENERATED_RE = re.compile(r"(generated|sqlcgen|_pb2|\.pb\.|/pb$|/pb/|bindings)")

IGNORE_FILE = ".repokgignore"


def load_ignore(repo):
    """Patterns from <repo>/.repokgignore: one glob per line, # comments.

    Same semantics as --exclude; committed so the whole team shares it.
    """
    try:
        with open(os.path.join(repo, IGNORE_FILE), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    stripped = (ln.strip() for ln in lines)
    return [ln for ln in stripped if ln and not ln.startswith("#")]


def _excluded(rel, exclude):
    return any(fnmatch.fnmatch(rel, pat) for pat in exclude)


def walk(repo, exclude=None, stats=None):
    """os.walk with skip-dir pruning; yields (rel_dir, filenames).

    exclude: glob patterns fnmatch'd against repo-relative paths ("docs",
    "*/fixtures", "packages/*/gen"). Matching dirs are pruned wholesale
    (fnmatch's `*` crosses `/`, so "*fixtures" matches at any depth);
    matching files are dropped. stats: optional dict that receives
    "excluded_dirs"/"excluded_files" counts.
    """
    exclude = exclude or ()
    for dirpath, dirnames, filenames in os.walk(repo):
        rel = os.path.relpath(dirpath, repo)
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        keep = []
        for d in sorted(dirnames):
            if d in SKIP_DIRS or (d.startswith(".") and d != ".github"):
                continue
            if _excluded(rel + "/" + d if rel else d, exclude):
                if stats is not None:
                    stats["excluded_dirs"] = stats.get("excluded_dirs", 0) + 1
                continue
            keep.append(d)
        dirnames[:] = keep
        if exclude:
            kept, dropped = [], 0
            for f in filenames:
                if _excluded(rel + "/" + f if rel else f, exclude):
                    dropped += 1
                else:
                    kept.append(f)
            filenames = kept
            if dropped and stats is not None:
                stats["excluded_files"] = stats.get("excluded_files", 0) + dropped
        yield rel, filenames


def collect(repo, tree=None, store=None):
    """Return (languages, modules).

    languages: [{lang, files, loc}] sorted by loc desc
    modules:   [{path, lang, files, loc, root, generated}] one per dir containing code
    tree: optional pre-built {rel_dir: filenames} to avoid re-walking the repo.
    store: optional facts.Store shared with the other collectors, so each file
    is read once per scan rather than once per collector.
    """
    if store is None:
        store = Store(repo)
    lang_stats = {}
    modules = []
    for rel, files in (tree.items() if tree is not None else walk(repo)):
        if rel.startswith(".github"):
            continue
        per_lang = {}
        is_root = any(f in ROOT_MARKERS for f in files)
        for f in files:
            rec = store.facts(rel, f)
            lang = rec.get("lang")
            if not lang:
                continue
            loc = rec["loc"]
            ls = lang_stats.setdefault(lang, {"lang": lang, "files": 0, "loc": 0})
            ls["files"] += 1
            ls["loc"] += loc
            if lang in MODULE_LANGS:
                pl = per_lang.setdefault(lang, [0, 0])
                pl[0] += 1
                pl[1] += loc
        if per_lang:
            dominant = max(per_lang.items(), key=lambda kv: kv[1][1])
            modules.append({
                "path": rel or "(root)",
                "lang": dominant[0],
                "files": sum(v[0] for v in per_lang.values()),
                "loc": sum(v[1] for v in per_lang.values()),
                "root": is_root,
                "generated": bool(GENERATED_RE.search("/" + rel)),
            })
    languages = sorted(lang_stats.values(), key=lambda x: -x["loc"])
    modules.sort(key=lambda m: -m["loc"])
    return languages, modules
