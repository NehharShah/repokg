"""Inference audit: what repokg *inferred* (vs. measured), with confidence.

Most of the knowledge graph is deterministic fact (LOC, import statements,
PR states). But several conclusions are heuristics, and pretending otherwise
would be dishonest. Each heuristic conclusion becomes a finding:

    {kind, subject, rationale, evidence, confidence}

surfaced by `repokg audit` so users (and AI agents) can see exactly what was
guessed and why. Inspired by RepoCanon's Finding/Confidence model.
"""

from collections import Counter

CONFIDENCE_SCORE = {"high": 1.0, "medium": 0.6, "low": 0.3}

_EDGE_METHODS = {
    "Go": ("high", "imports resolved against go.mod module paths (exact)"),
    "Python": ("high", "stdlib ast parse; note: package discovery covers repo "
                       "root and src/ only"),
    "JS/TS": ("medium", "regex import extraction; relative imports, "
                        "tsconfig/jsconfig paths + baseUrl aliases and "
                        "workspace package names all resolved; `extends` "
                        "chains and package `exports` maps are not"),
    "Rust": ("medium", "regex `use` parsing resolved against Cargo [package] "
                       "names and src/ module dirs; macros, re-exports and "
                       "path-dependencies are not resolved"),
    "Java": ("high", "imports resolved by longest prefix against package "
                     "declarations (language ground truth); test source roots "
                     "excluded as targets when a main root declares the package"),
    "Kotlin": ("high", "same package-declaration resolution as Java, incl. "
                       "Kotlin's collapsed directory convention; `as` aliases "
                       "handled, extension-function call sites are not imports"),
}


def _f(kind, subject, rationale, evidence, confidence):
    return {"kind": kind, "subject": subject, "rationale": rationale,
            "evidence": evidence[:5], "confidence": confidence}


def build(kg):
    """Return (findings, uncertainty_notes) for an assembled knowledge graph."""
    findings, notes = [], []
    repo = kg.get("repo", {})

    method = repo.get("trunk_method", "")
    findings.append(_f(
        "git", "trunk = %s" % (repo.get("trunk") or "?"),
        "detected via %s" % (method or "unknown"), [],
        "high" if method == "origin/HEAD symref" else "medium"))

    if repo.get("integration"):
        findings.append(_f(
            "git", "integration branch = %s" % repo["integration"],
            "matched a well-known integration branch name "
            "(staging/develop/dev/next/canary); branch merge state is "
            "computed against it", [], "medium"))

    squash = [b for b in kg.get("branches", []) if b.get("status") == "squash-merged"]
    if squash:
        findings.append(_f(
            "branches", "%d squash-merged branches" % len(squash),
            "each had a MERGED PR for its exact head ref but is not an "
            "ancestor of the base — the squash/rebase signature",
            [b["name"] for b in squash], "high"))

    stale = [b for b in kg.get("branches", []) if b.get("status") == "stale"]
    if stale:
        findings.append(_f(
            "branches", "%d stale branches" % len(stale),
            "no PR ever referenced these head refs and they are not merged",
            [b["name"] for b in stale], "high"))

    edge_langs = Counter(e["lang"] for e in kg.get("edges", []))
    for lang, count in sorted(edge_langs.items()):
        conf, how = _EDGE_METHODS.get(lang, ("medium", "heuristic extraction"))
        findings.append(_f(
            "imports", "%d %s edges" % (count, lang), how, [], conf))

    gen = [m for m in kg.get("modules", []) if m.get("generated")]
    if gen:
        findings.append(_f(
            "modules", "%d modules flagged generated" % len(gen),
            "path-name heuristic (generated/sqlcgen/pb/bindings/...); "
            "verify before excluding from review",
            [m["path"] for m in gen], "low"))

    excl = kg.get("exclude", {})
    if excl.get("dirs") or excl.get("files"):
        notes.append("%d dirs and %d files excluded by configured patterns "
                     "(%s); everything under them is invisible to this graph."
                     % (excl.get("dirs", 0), excl.get("files", 0),
                        ", ".join(excl.get("patterns", [])) or "?"))

    unresolved = kg.get("edge_stats", {}).get("js_alias_unresolved", 0)
    if unresolved:
        notes.append("%d JS/TS alias imports matched a tsconfig/jsconfig "
                     "paths pattern but their targets exist nowhere in the "
                     "tree; those edges were dropped." % unresolved)

    if kg.get("github_note"):
        notes.append("PR layer incomplete: %s — branch statuses degrade to "
                     "merged/stale only." % kg["github_note"])
    elif kg.get("prs"):
        notes.append("Fork PRs share bare head-ref names; a same-named local "
                     "branch would be linked to the fork's PR.")
    if not repo.get("integration"):
        notes.append("No integration branch detected; merge state is "
                     "computed directly against trunk.")

    return findings, notes


def render_text(findings, notes):
    """Plain-text audit table (stdlib only, no rich)."""
    out = []
    by_kind = {}
    for f in findings:
        by_kind.setdefault(f["kind"], []).append(f)
    for kind in sorted(by_kind):
        out.append("[%s]" % kind)
        for f in by_kind[kind]:
            out.append("  %-38s %-6s  %s" %
                       (f["subject"], f["confidence"], f["rationale"]))
            if f["evidence"]:
                out.append("      evidence: %s" % ", ".join(f["evidence"]))
        out.append("")
    if notes:
        out.append("Uncertainty notes:")
        for n in notes:
            out.append("  - %s" % n)
    low = [f for f in findings if f["confidence"] == "low"]
    if low:
        out.append("")
        out.append("Needs manual review: %s" %
                   ", ".join(f["subject"] for f in low))
    return "\n".join(out).rstrip()
