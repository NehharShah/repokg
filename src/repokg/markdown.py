"""Render KNOWLEDGE_GRAPH.md — the AI-ready knowledge graph document."""

import re
from collections import Counter, OrderedDict

CONVENTIONAL_RE = re.compile(r"^(\w+)(?:\(([^)]+)\))?\s*[:!]")
MAX_MODULE_ROWS = 150
MAX_EDGE_ROWS = 120
MAX_LIST = 30
MERMAID_MAX_NODES = 28


def render(kg, narratives=None):
    n = narratives or {}
    out = []
    w = out.append

    repo = kg.get("repo", {})
    prs = kg.get("prs", [])
    branches = kg.get("branches", [])
    modules = kg.get("modules", [])
    edges = kg.get("edges", [])
    enriched = bool(n.get("modules") or n.get("overview"))

    w("# %s — Codebase Knowledge Graph" % repo.get("name", "repository"))
    w("")
    w("> Machine-generated knowledge graph (by [repokg](https://github.com/NehharShah/repokg)) "
      "on %s. Structure is extracted deterministically from git/GitHub/source;" % kg.get("generated_at", ""))
    if enriched:
        w("> narrative sections were enriched by an AI agent that verified claims against the code.")
    else:
        w("> narrative sections are **not yet enriched** — run the prompt in `.repokg/prompts/enrich.md` "
          "with an AI agent to fill them in.")
    w("")
    w("## For AI agents — start here")
    w("")
    w("- **§1 Repo facts** and **§2 Languages** tell you what this project is and how big.")
    w("- **§3 Architecture graph** + **§4 Module inventory** tell you where code lives "
      "and what depends on what — trust these edges, they are extracted from imports.")
    w("- **§6 Branches & PRs** tells you what is in flight (`active`), what merged, and what was abandoned.")
    w("- **§7 Timeline** shows how the codebase evolved; **§8 Ops** shows how it builds/deploys.")
    w("- Machine-readable version of everything: `.repokg/kg.json`.")
    w("")

    _repo_section(w, kg, repo, prs, branches, n)
    _languages_section(w, kg)
    _graph_section(w, edges)
    _modules_section(w, modules, n)
    _edges_section(w, edges)
    _flows_section(w, n)
    _branches_section(w, repo, branches, prs, kg)
    _timeline_section(w, prs, n)
    _ops_section(w, kg.get("ops", {}))
    _gotchas_section(w, n)
    _pr_appendix(w, prs)

    return "\n".join(out).rstrip() + "\n"


def _repo_section(w, kg, repo, prs, branches, n):
    w("## 1. Repo at a glance")
    w("")
    if n.get("overview"):
        w(n["overview"].strip())
        w("")
    contribs = ", ".join("%s (%d)" % (c["name"], c["commits"])
                         for c in repo.get("contributors", [])[:8])
    rows = [
        ("Remote", repo.get("remote") or "(none)"),
        ("Trunk", "`%s`" % repo.get("trunk", "?")),
        ("Integration branch", "`%s`" % repo["integration"] if repo.get("integration") else "(none)"),
        ("First commit", repo.get("first_commit", "")),
        ("Commits", str(repo.get("commit_count", 0))),
        ("Contributors", contribs or "(unknown)"),
        ("Branches", str(len(branches))),
        ("Pull requests", _pr_totals(prs, kg)),
    ]
    w("| Fact | Value |")
    w("|---|---|")
    for k, v in rows:
        w("| %s | %s |" % (k, v))
    w("")


def _pr_totals(prs, kg):
    if not prs:
        return kg.get("github_note") or "0"
    c = Counter(p["state"] for p in prs)
    return "%d total — %d merged / %d open / %d closed-unmerged" % (
        len(prs), c.get("MERGED", 0), c.get("OPEN", 0), c.get("CLOSED", 0))


def _languages_section(w, kg):
    w("## 2. Languages")
    w("")
    w("| Language | Files | LOC |")
    w("|---|---:|---:|")
    for l in kg.get("languages", [])[:15]:
        w("| %s | %d | %d |" % (l["lang"], l["files"], l["loc"]))
    w("")


def mermaid_id(path):
    return re.sub(r"\W+", "_", path) or "root"


def aggregate(path, depth):
    parts = path.split("/")
    return "/".join(parts[:depth])


def _graph_section(w, edges):
    w("## 3. Architecture graph (imports)")
    w("")
    if not edges:
        w("_No internal import edges detected._")
        w("")
        return
    for depth in (2, 1):
        agg = Counter()
        for e in edges:
            f, t = aggregate(e["from"], depth), aggregate(e["to"], depth)
            if f != t:
                agg[(f, t)] += e["count"]
        nodes = {x for pair in agg for x in pair}
        if len(nodes) <= MERMAID_MAX_NODES or depth == 1:
            break
    w("```mermaid")
    w("flowchart LR")
    for node in sorted(nodes):
        w('  %s["%s"]' % (mermaid_id(node), node))
    for (f, t), c in sorted(agg.items(), key=lambda kv: -kv[1]):
        w("  %s --> %s" % (mermaid_id(f), mermaid_id(t)))
    w("```")
    w("")
    w("_Arrows point from importer to imported. Aggregated to path depth %d._" % depth)
    w("")


def _modules_section(w, modules, n):
    w("## 4. Module inventory")
    w("")
    purposes = n.get("modules", {})
    w("| Module | Lang | Files | LOC | Purpose |")
    w("|---|---|---:|---:|---|")
    shown = modules[:MAX_MODULE_ROWS]
    for m in shown:
        flags = " `generated`" if m.get("generated") else ""
        flags += " `root`" if m.get("root") else ""
        purpose = purposes.get(m["path"], "") or "_(pending enrichment)_"
        w("| `%s`%s | %s | %d | %d | %s |" %
          (m["path"], flags, m["lang"], m["files"], m["loc"], purpose))
    if len(modules) > len(shown):
        w("")
        w("_… %d smaller modules omitted (see kg.json)._" % (len(modules) - len(shown)))
    w("")


def _edges_section(w, edges):
    w("## 5. Dependency edges")
    w("")
    if not edges:
        w("_None detected._")
        w("")
        return
    shown = edges[:MAX_EDGE_ROWS]
    for e in shown:
        w("- `%s` → `%s` (%s, ×%d)" % (e["from"], e["to"], e["lang"], e["count"]))
    if len(edges) > len(shown):
        w("- _… %d more edges in kg.json_" % (len(edges) - len(shown)))
    w("")


def _flows_section(w, n):
    flows = n.get("flows") or []
    if not flows:
        return
    w("## 5b. Data flows")
    w("")
    for fl in flows:
        w("### %s" % fl.get("name", "flow"))
        for i, step in enumerate(fl.get("steps", []), 1):
            w("%d. %s" % (i, step))
        w("")


def _branches_section(w, repo, branches, prs, kg):
    w("## 6. Branches & pull requests")
    w("")
    base = repo.get("default_base", "")
    w("Branch merge state is computed against `%s`. Statuses: `active` (open PR), "
      "`merged` (ancestry), `squash-merged` (PR merged, no ancestry), "
      "`abandoned` (PR closed unmerged), `stale` (no PR)." % base)
    w("")
    by_status = {}
    for b in branches:
        by_status.setdefault(b.get("status", "unknown"), []).append(b)
    counts = ", ".join("%d %s" % (len(v), k) for k, v in
                       sorted(by_status.items(), key=lambda kv: -len(kv[1])))
    w("**%d branches**: %s." % (len(branches), counts))
    w("")

    open_prs = [p for p in prs if p["state"] == "OPEN"]
    if open_prs:
        w("### Open PRs")
        w("")
        w("| PR | Since | Author | Branch | Title |")
        w("|---|---|---|---|---|")
        for p in sorted(open_prs, key=lambda x: -x["number"]):
            draft = " (draft)" if p.get("draft") else ""
            w("| #%d%s | %s | %s | `%s` | %s |" %
              (p["number"], draft, p["created"], p["author"], p["head"],
               p["title"].replace("|", "\\|")))
        w("")

    for status in ("active", "abandoned", "stale", "squash-merged", "merged"):
        items = by_status.get(status, [])
        if not items:
            continue
        if status == "active" and open_prs:
            continue  # already shown as the open-PR table
        w("### %s (%d)" % (status, len(items)))
        w("")
        for b in sorted(items, key=lambda x: x.get("date", ""), reverse=True)[:MAX_LIST]:
            pr_ref = (" → PR " + ", ".join("#%d" % x for x in b["prs"])) if b.get("prs") else ""
            ahead = (" [+%d]" % b["ahead"]) if b.get("ahead") else ""
            w("- `%s` (%s)%s%s" % (b["name"], b.get("date", ""), ahead, pr_ref))
        if len(items) > MAX_LIST:
            w("- _… %d more in kg.json_" % (len(items) - MAX_LIST))
        w("")


def _timeline_section(w, prs, n):
    w("## 7. Timeline")
    w("")
    eras = n.get("timeline") or []
    if eras:
        w("| Period | Theme |")
        w("|---|---|")
        for e in eras:
            w("| %s | %s |" % (e.get("period", ""), e.get("theme", "")))
        w("")
        return
    merged = [p for p in prs if p["state"] == "MERGED" and p["merged"]]
    if not merged:
        w("_No merged-PR history available (GitHub data missing)._")
        w("")
        return
    months = OrderedDict()
    for p in sorted(merged, key=lambda x: x["merged"]):
        months.setdefault(p["merged"][:7], []).append(p)
    w("| Month | Merged PRs | Top scopes | Sample |")
    w("|---|---:|---|---|")
    for month, items in months.items():
        scopes = Counter()
        for p in items:
            m = CONVENTIONAL_RE.match(p["title"])
            if m and m.group(2):
                scopes[m.group(2)] += 1
        top = ", ".join("%s (%d)" % (s, c) for s, c in scopes.most_common(3)) or "—"
        sample = "; ".join(p["title"][:60].replace("|", "\\|") for p in items[:2])
        w("| %s | %d | %s | %s |" % (month, len(items), top, sample))
    w("")


def _ops_section(w, ops):
    w("## 8. Ops surface")
    w("")
    if ops.get("agent_context"):
        w("**Agent context files (read and respect these):** " +
          ", ".join("`%s`" % x for x in ops["agent_context"]))
        w("")
    if ops.get("workflows"):
        w("**CI workflows:** " + "; ".join(
            "%s (`%s`)" % (x["name"], x["file"]) for x in ops["workflows"]))
        w("")
    for key, label in (("dockerfiles", "Dockerfiles"), ("compose", "Compose files"),
                       ("helm_charts", "Helm charts"), ("test_dirs", "Test dirs"),
                       ("migration_dirs", "Migration dirs"), ("proto_dirs", "Proto dirs")):
        if ops.get(key):
            w("**%s:** %s" % (label, ", ".join("`%s`" % x for x in ops[key][:20])))
            w("")
    if ops.get("makefile_targets"):
        w("**Make targets:** " + ", ".join("`%s`" % t for t in ops["makefile_targets"][:30]))
        w("")
    for cd in ops.get("config_dirs", []):
        w("**`%s/`:** %s" % (cd["dir"], ", ".join(cd["entries"][:15])))
        w("")
    if ops.get("docs"):
        w("**Docs:** " + ", ".join("`%s`" % d for d in ops["docs"][:40]))
        w("")


def _gotchas_section(w, n):
    gotchas = n.get("gotchas") or []
    if not gotchas:
        return
    w("## 9. Gotchas (verified by enrichment agent)")
    w("")
    for g in gotchas:
        w("- %s" % g)
    w("")


def _pr_appendix(w, prs):
    if not prs:
        return
    w("## Appendix A — Complete PR catalog (%d)" % len(prs))
    w("")
    w("| # | State | Date | Author | Branch | Title |")
    w("|---|---|---|---|---|---|")
    for p in prs:
        date = p["merged"] or p["closed"] or p["created"]
        state = {"MERGED": "merged", "OPEN": "**open**", "CLOSED": "closed"}.get(
            p["state"], p["state"].lower())
        w("| %d | %s | %s | %s | `%s` | %s |" %
          (p["number"], state, date, p["author"], p["head"],
           p["title"].replace("|", "\\|")))
    w("")
