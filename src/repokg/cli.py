"""repokg CLI: scan | prompts | render | generate."""

import argparse
import datetime
import json
import os
import sys

from . import __version__, code, deps, github, gitinfo, inject, markdown, ops, prompts


def scan(repo, out, no_github, pr_limit):
    info, branches = gitinfo.collect(repo)
    if no_github:
        prs, note = [], "GitHub lookup disabled (--no-github)"
    else:
        prs, note = github.collect(repo, pr_limit)
    github.classify(branches, prs, info["trunk"], info["integration"])
    tree = dict(code.walk(repo))  # single filesystem walk, shared by all collectors
    languages, modules = code.collect(repo, tree)
    kg = {
        "repokg_version": 1,
        "generated_at": datetime.date.today().isoformat(),
        "repo": info,
        "languages": languages,
        "modules": modules,
        "edges": deps.collect(repo, tree),
        "branches": branches,
        "prs": prs,
        "github_note": note,
        "ops": ops.collect(repo, tree),
    }
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "kg.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(kg, f, indent=1)
    print("wrote %s (%d modules, %d edges, %d branches, %d PRs)" %
          (path, len(modules), len(kg["edges"]), len(branches), len(prs)))
    return kg


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
            narratives = json.load(f)
    doc = markdown.render(kg, narratives)
    with open(md, "w", encoding="utf-8") as f:
        f.write(doc)
    state = "enriched" if narratives else "structure-only; run .repokg/prompts/enrich.md to enrich"
    print("wrote %s (%s)" % (md, state))


def do_inject(repo, md):
    for path, status in inject.run(repo, md).items():
        print("%s: %s" % (path, status))


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


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="repokg",
        description="Generate an AI-ready knowledge graph of a codebase.")
    ap.add_argument("command", nargs="?", default="generate",
                    choices=["scan", "prompts", "render", "generate", "inject",
                             "check", "version"],
                    help="scan: extract structure to .repokg/kg.json | "
                         "prompts: write the AI enrichment prompt | "
                         "render: kg.json (+narratives.json) -> KNOWLEDGE_GRAPH.md | "
                         "generate: scan + prompts + render (default) | "
                         "inject: add knowledge-graph pointer to CLAUDE.md/AGENTS.md/cursor rules | "
                         "check: exit 1 if knowledge graph is stale vs HEAD")
    ap.add_argument("path", nargs="?", default=".", help="repository path (default: .)")
    ap.add_argument("--out", default=None, help="output dir (default: <repo>/.repokg)")
    ap.add_argument("--md", default=None, help="markdown output (default: <repo>/KNOWLEDGE_GRAPH.md)")
    ap.add_argument("--no-github", action="store_true", help="skip gh PR lookup")
    ap.add_argument("--pr-limit", type=int, default=1000, help="max PRs to fetch (default 1000)")
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

    try:
        if args.command == "scan":
            scan(repo, out, args.no_github, args.pr_limit)
        elif args.command == "prompts":
            write_prompts(repo, out, md)
        elif args.command == "render":
            render(out, md)
        elif args.command == "inject":
            do_inject(repo, md)
        elif args.command == "check":
            return check(repo, out, md)
        else:  # generate
            scan(repo, out, args.no_github, args.pr_limit)
            write_prompts(repo, out, md)
            render(out, md)
    except RuntimeError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print("error: %s (run `repokg scan` first?)" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
