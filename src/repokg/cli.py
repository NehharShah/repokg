"""repokg CLI: scan | prompts | render | generate | inject | audit | clean |
check | diff."""

import argparse
import datetime
import difflib
import json
import os
import sys

from . import (__version__, cache, code, deps, diff, facts, findings, github,
               gitinfo, inject, markdown, ops, prompts, validate)


def build_graph(repo, out, no_github, pr_limit, exclude=(), use_cache=True,
                stream=None):
    """Assemble the knowledge graph and return it, without writing kg.json.

    `scan` writes what this returns. `diff` cannot: the document already in
    <out> is the baseline it compares against, so writing over it would
    destroy the answer to the next question asked.

    Progress lines go to `stream` (stdout by default). `diff` sends them to
    stderr instead, so that `--format json` stays pipeable while the scan
    still says out loud what it excluded — coverage loss has to be visible
    wherever the scan happens.
    """
    stream = stream if stream is not None else sys.stdout
    info, branches = gitinfo.collect(repo)
    if no_github:
        prs, note = [], "GitHub lookup disabled (--no-github)"
    else:
        prs, note = github.collect(repo, pr_limit)
    github.classify(branches, prs, info["trunk"], info["integration"])
    walk_stats = {}
    # single filesystem walk, shared by all collectors — exclusions inherit
    tree = dict(code.walk(repo, exclude, walk_stats))
    # single read per file too: the store memoizes extracted facts across
    # collectors that would otherwise each open the same file, and replays
    # them from the cache for files that have not changed since the last scan
    scan_cache, cache_note = cache.open_(repo, out, info["head"], use_cache)
    store = facts.Store(repo, scan_cache)
    languages, modules = code.collect(repo, tree, store)
    edge_stats = {}
    kg = {
        "repokg_version": 1,
        "generated_at": datetime.date.today().isoformat(),
        "repo": info,
        "languages": languages,
        "modules": modules,
        "edges": deps.collect(repo, tree, edge_stats, store),
        "edge_stats": edge_stats,
        "exclude": {
            "patterns": sorted(exclude),
            "dirs": walk_stats.get("excluded_dirs", 0),
            "files": walk_stats.get("excluded_files", 0),
        },
        "branches": branches,
        "prs": prs,
        "github_note": note,
        "ops": ops.collect(repo, tree),
    }
    kg["findings"], kg["uncertainty"] = findings.build(kg)
    if scan_cache is not None:
        scan_cache.save(out, info["head"])
    excl = kg["exclude"]
    if excl["dirs"] or excl["files"]:
        print("excluded %d dirs, %d files (%d exclude patterns)" %
              (excl["dirs"], excl["files"], len(excl["patterns"])),
              file=stream)
    print(_cache_line(scan_cache, cache_note, store), file=stream)
    return kg


def scan(repo, out, no_github, pr_limit, exclude=(), use_cache=True):
    kg = build_graph(repo, out, no_github, pr_limit, exclude, use_cache)
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "kg.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(kg, f, indent=1)
    print("wrote %s (%d modules, %d edges, %d branches, %d PRs)" %
          (path, len(kg["modules"]), len(kg["edges"]), len(kg["branches"]),
           len(kg["prs"])))
    return kg


def _cache_line(scan_cache, note, store):
    """How much of this scan was replayed rather than parsed.

    Printed, never stored: a warm scan and a cold scan must write identical
    kg.json, and this is exactly the number that differs between them.
    """
    if scan_cache is None:
        return "cache: %s — parsed %d files" % (note, store.parses)
    if note != "warm":
        return "cache: cold (%s) — parsed %d files" % (note, store.parses)
    return ("cache: replayed %d of %d files, parsed %d"
            % (scan_cache.hits, scan_cache.hits + store.parses, store.parses))


def write_prompts(repo, out, md):
    pdir = os.path.join(out, "prompts")
    os.makedirs(pdir, exist_ok=True)
    path = os.path.join(pdir, "enrich.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(prompts.render(repo, out, md))
    print("wrote %s (hand this to your AI agent)" % path)


def render(out, md):
    with open(os.path.join(out, "kg.json"), encoding="utf-8") as f:
        kg = json.load(f)
    narratives = {}
    npath = os.path.join(out, "narratives.json")
    if os.path.isfile(npath):
        with open(npath, encoding="utf-8") as f:
            try:
                narratives = json.load(f)
            except json.JSONDecodeError as e:
                print("error: %s is not valid JSON: %s" % (npath, e), file=sys.stderr)
                return 1
        errs = validate.narratives(narratives)
        if errs:
            print("error: %s failed schema validation:" % npath, file=sys.stderr)
            for e in errs:
                print("  - %s" % e, file=sys.stderr)
            print("fix the file (schema in .repokg/prompts/enrich.md) and re-run "
                  "`repokg render`", file=sys.stderr)
            return 1
    doc = markdown.render(kg, narratives)
    with open(md, "w", encoding="utf-8") as f:
        f.write(doc)
    state = "enriched" if narratives else "structure-only; run .repokg/prompts/enrich.md to enrich"
    print("wrote %s (%s)" % (md, state))
    return 0


def do_inject(repo, md, diff=False):
    for path, (status, old, new) in inject.run(repo, md, write=not diff).items():
        print("%s: %s%s" % (path, status, " (dry run)" if diff and status != "unchanged" else ""))
        if diff and status != "unchanged":
            sys.stdout.writelines(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile="a/" + path, tofile="b/" + path))
            print()


def do_clean(repo, out, md, diff=False):
    actions = inject.clean(repo, out, md, write=not diff)
    if not actions:
        print("nothing to clean")
        return
    for path, action in actions.items():
        print("%s: %s%s" % (path, action,
                            " (dry run)" if diff and "SKIPPED" not in action else ""))


def audit(out, as_json=False):
    with open(os.path.join(out, "kg.json"), encoding="utf-8") as f:
        kg = json.load(f)
    found = kg.get("findings", [])
    notes = kg.get("uncertainty", [])
    if not found and not notes:
        print("no findings recorded (re-run `repokg scan` with repokg >= 0.2)")
        return
    if as_json:
        print(json.dumps({"findings": found, "uncertainty": notes}, indent=1))
    else:
        print(findings.render_text(found, notes))


def check(repo, out, md):
    """Exit 0 if the knowledge graph matches HEAD, 1 if stale/missing. CI-friendly."""
    apath = os.path.join(out, "kg.json")
    if not os.path.isfile(apath) or not os.path.isfile(md):
        print("stale: knowledge graph not generated (run `repokg generate`)")
        return 1
    with open(apath, encoding="utf-8") as f:
        stored = json.load(f).get("repo", {}).get("head", "")
    head = gitinfo.try_run(repo, "rev-parse", "HEAD")
    if stored and head and stored != head:
        print("stale: knowledge graph at %s, HEAD is %s (run `repokg generate`)"
              % (stored[:12], head[:12]))
        return 1
    print("fresh: knowledge graph matches HEAD %s" % (head[:12] or "(unknown)"))
    return 0


def do_diff(repo, out, from_graph, to_graph, fmt, no_github, pr_limit,
            exclude=(), use_cache=True):
    """Report what changed between two knowledge graphs.

    Exit 0 when the graph's shape is unchanged, 1 when it changed, 2 on
    error — the `diff(1)` and `git diff --exit-code` convention, and the one
    `repokg check` already follows by returning 1 for a stale graph. Errors
    are 2 rather than 1 so that a CI job cannot read a mistyped path as an
    architectural change.

    Shape means the module, edge, language and ops-surface membership of the
    graph. LOC drift, edge weight and branch churn are all reported but none
    of them move the exit code: they change on essentially every commit, and
    a gate that fired every time would be switched off within a week.

    The graph is never written. The baseline is the document in <out>, so a
    scan that saved over it would leave the next run with nothing to compare
    against. <out>/cache.json is still updated when a scan runs — it records
    what each file contained, not what the graph concluded, and is what keeps
    the scan that feeds this fast.
    """
    if from_graph:
        old = _load_graph(from_graph, "baseline", "")
    else:
        old = _load_graph(os.path.join(out, "kg.json"), "baseline",
                          " (run `repokg scan` first, or point --from at one)")
    if old is None:
        return 2
    if to_graph:
        new = _load_graph(to_graph, "comparison", "")
        if new is None:
            return 2
    else:
        try:
            # progress lines to stderr: stdout is the report, and may be piped
            new = build_graph(repo, out, no_github, pr_limit, exclude,
                              use_cache, stream=sys.stderr)
        except (RuntimeError, OSError) as e:
            # A scan that cannot run is an error, not an architectural change.
            # main() maps these to 1, and 1 is precisely what a CI job reads
            # as "the shape moved" — the collision the three codes exist to
            # avoid, so it has to be caught here rather than there.
            print("error: cannot scan %s to compare against: %s" % (repo, e),
                  file=sys.stderr)
            return 2
    delta = diff.build(old, new)
    if fmt == "json":
        print(json.dumps(delta, indent=1))
    elif fmt == "md":
        sys.stdout.write(diff.render_markdown(delta))
    else:
        print(diff.render_text(delta))
    return 1 if delta["shape_changed"] else 0


def _load_graph(path, role, hint):
    """A kg.json document, or None having said on stderr why not."""
    try:
        with open(path, encoding="utf-8") as f:
            kg = json.load(f)
    except FileNotFoundError:
        print("error: no %s graph at %s%s" % (role, path, hint),
              file=sys.stderr)
        return None
    except (OSError, ValueError) as e:
        print("error: %s is not a readable knowledge graph: %s" % (path, e),
              file=sys.stderr)
        return None
    if not isinstance(kg, dict):
        print("error: %s is not a knowledge graph document" % path,
              file=sys.stderr)
        return None
    return kg


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="repokg",
        description="Generate an AI-ready knowledge graph of a codebase.")
    ap.add_argument("command", nargs="?", default="generate",
                    choices=["scan", "prompts", "render", "generate", "inject",
                             "audit", "clean", "check", "diff", "version"],
                    help="scan: extract structure to .repokg/kg.json | "
                         "prompts: write the AI enrichment prompt | "
                         "render: kg.json (+narratives.json) -> KNOWLEDGE_GRAPH.md | "
                         "generate: scan + prompts + render (default) | "
                         "inject: add knowledge-graph pointer to CLAUDE.md/AGENTS.md/cursor rules | "
                         "audit: show inferred conclusions with confidence + evidence | "
                         "clean: remove everything repokg authored | "
                         "check: exit 1 if knowledge graph is stale vs HEAD | "
                         "diff: report what changed between two graphs, "
                         "exit 1 if the shape changed")
    ap.add_argument("path", nargs="?", default=".", help="repository path (default: .)")
    ap.add_argument("--out", default=None, help="output dir (default: <repo>/.repokg)")
    ap.add_argument("--md", default=None, help="markdown output (default: <repo>/KNOWLEDGE_GRAPH.md)")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help="glob matched against repo-relative paths; matching "
                         "dirs are pruned, matching files dropped (repeatable; "
                         "`*` crosses `/`, so '*fixtures' matches any depth). "
                         "Unioned with <repo>/.repokgignore (one glob per "
                         "line, # comments)")
    ap.add_argument("--no-github", action="store_true", help="skip gh PR lookup")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore <out>/cache.json and re-parse every file; "
                         "the cache is an optimization only, so this changes "
                         "how long a scan takes and nothing it produces")
    ap.add_argument("--pr-limit", type=int, default=1000, help="max PRs to fetch (default 1000)")
    ap.add_argument("--diff", action="store_true",
                    help="inject/clean: dry run, print what would change")
    ap.add_argument("--json", action="store_true", help="audit/diff: machine-readable output")
    ap.add_argument("--from", dest="from_graph", metavar="KG.JSON",
                    help="diff: baseline graph (default: <out>/kg.json, i.e. "
                         "what the last scan left there)")
    ap.add_argument("--to", dest="to_graph", metavar="KG.JSON",
                    help="diff: graph to compare against (default: a fresh "
                         "scan, which is not written to disk)")
    ap.add_argument("--format", dest="fmt", choices=["text", "json", "md"],
                    default=None,
                    help="diff: report format (default text; md is ready to "
                         "paste into a PR comment). `--md` is already the "
                         "markdown *output path*, hence --format")
    args = ap.parse_args(argv)

    if args.command == "version":
        print("repokg %s" % __version__)
        return 0

    repo = os.path.abspath(args.path)
    if not os.path.isdir(repo):
        print("error: %s is not a directory" % repo, file=sys.stderr)
        return 2
    out = args.out or os.path.join(repo, ".repokg")
    md = args.md or os.path.join(repo, "KNOWLEDGE_GRAPH.md")
    # union of CLI flags and the committed ignore file, deduped
    exclude = list(dict.fromkeys(args.exclude + code.load_ignore(repo)))

    try:
        if args.command == "scan":
            scan(repo, out, args.no_github, args.pr_limit, exclude,
                 not args.no_cache)
        elif args.command == "prompts":
            write_prompts(repo, out, md)
        elif args.command == "render":
            return render(out, md)
        elif args.command == "inject":
            do_inject(repo, md, diff=args.diff)
        elif args.command == "audit":
            audit(out, as_json=args.json)
        elif args.command == "clean":
            do_clean(repo, out, md, diff=args.diff)
        elif args.command == "check":
            return check(repo, out, md)
        elif args.command == "diff":
            return do_diff(repo, out, args.from_graph, args.to_graph,
                           args.fmt or ("json" if args.json else "text"),
                           args.no_github, args.pr_limit, exclude,
                           not args.no_cache)
        else:  # generate
            scan(repo, out, args.no_github, args.pr_limit, exclude,
                 not args.no_cache)
            write_prompts(repo, out, md)
            return render(out, md)
    except RuntimeError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print("error: %s (run `repokg scan` first?)" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
