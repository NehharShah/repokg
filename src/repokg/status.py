"""Enrichment coverage and dead narratives: how much of the graph has been
described, and how much of what was described no longer exists.

Pure functions over a knowledge graph document and a narratives document, so
the numbers can be tested without a repository and reused by whatever prints
them. Nothing here reads a file, runs git or decides an exit code.

Both inputs are taken as they are on disk, malformed or not. `render` is the
place that rejects a bad `narratives.json`; a health report has to describe a
broken one rather than raise, so an entry that does not count as a description
(a blank or non-string purpose, a flow with no steps) is simply not counted.

A narrative key is *dead* when it names no module in the current scan. That is
exactly the test `render` applies when it looks up a module's purpose, so a
dead key is one the rendered document silently never shows — whether the
directory was deleted, renamed, or never held code files directly. Keys are
compared as written: `src/app/` does not match module `src/app`, and the
renderer would not match it either.
"""


def _purposes(narratives):
    """{module path: purpose} for the entries that actually describe a module."""
    mods = narratives.get("modules") if isinstance(narratives, dict) else None
    if not isinstance(mods, dict):
        return {}
    return {k: v for k, v in mods.items()
            if isinstance(v, str) and v.strip()}


def _percent(part, whole):
    """Share as a percentage to one decimal, or None when there is nothing to
    take a share of — an empty graph is not 100% covered, it is unmeasured."""
    return round(100.0 * part / whole, 1) if whole else None


def _modules(kg):
    mods = kg.get("modules") if isinstance(kg, dict) else None
    return mods if isinstance(mods, list) else []


def coverage(kg, narratives):
    """How much of the graph has a purpose line, and which narrative sections exist.

    Returns:
      modules:  {"total", "covered", "percent", "missing"} — `missing` is the
                undescribed module paths, largest LOC first, since the biggest
                gap is the one to close first
      loc:      {"total", "covered", "percent"} — the same split weighted by
                lines of code, so a repo whose three biggest modules are
                described is not reported as a third done
      sections: {"overview": bool, "flows", "timeline", "gotchas", "sections"}
                — the last four are counts of well-formed entries
    """
    described = _purposes(narratives)
    mods = _modules(kg)
    total_loc = sum(m.get("loc", 0) for m in mods)
    covered = [m for m in mods if m["path"] in described]
    covered_loc = sum(m.get("loc", 0) for m in covered)
    missing = sorted((m for m in mods if m["path"] not in described),
                     key=lambda m: (-m.get("loc", 0), m["path"]))
    return {
        "modules": {
            "total": len(mods),
            "covered": len(covered),
            "percent": _percent(len(covered), len(mods)),
            "missing": [m["path"] for m in missing],
        },
        "loc": {
            "total": total_loc,
            "covered": covered_loc,
            "percent": _percent(covered_loc, total_loc),
        },
        "sections": _sections(narratives),
    }


def _sections(narratives):
    n = narratives if isinstance(narratives, dict) else {}

    def entries(key, ok):
        items = n.get(key)
        return sum(1 for i in items if ok(i)) if isinstance(items, list) else 0

    def text(v):
        return isinstance(v, str) and bool(v.strip())

    return {
        "overview": text(n.get("overview")),
        "flows": entries("flows", lambda f: isinstance(f, dict)
                         and text(f.get("name"))
                         and isinstance(f.get("steps"), list)
                         and bool(f["steps"])),
        "timeline": entries("timeline", lambda t: isinstance(t, dict)
                            and text(t.get("period")) and text(t.get("theme"))),
        "gotchas": entries("gotchas", text),
        "sections": entries("sections", lambda s: isinstance(s, dict)
                            and text(s.get("title")) and text(s.get("body"))),
    }


def dead_narratives(kg, narratives):
    """Sorted narrative module keys that name no module in the current scan.

    Every key under `modules` is judged, including ones whose value is blank:
    the key is stale regardless of what it says.
    """
    mods = narratives.get("modules") if isinstance(narratives, dict) else None
    if not isinstance(mods, dict):
        return []
    known = {m["path"] for m in _modules(kg)}
    return sorted(k for k in mods if k not in known)


def compute(kg, narratives):
    """The whole of what can be said from the two documents alone:
    {"coverage": ..., "dead_narratives": [...]}."""
    return {"coverage": coverage(kg, narratives),
            "dead_narratives": dead_narratives(kg, narratives)}
