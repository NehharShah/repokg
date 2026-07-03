"""repo-atlas CLI: scan | prompts | render | generate."""

import argparse
import datetime
import json
import os
import sys

from . import __version__, code, deps, github, gitinfo, markdown, ops, prompts


def scan(repo, out, no_github, pr_limit):
    info, branches = gitinfo.collect(repo)
    if no_github:
        prs, note = [], "GitHub lookup disabled (--no-github)"
    else:
        prs, note = github.collect(repo, pr_limit)
    github.classify(branches, prs, info["trunk"], info["integration"])
    languages, modules = code.collect(repo)
    atlas = {
        "atlas_version": 1,
        "generated_at": datetime.date.today().isoformat(),
        "repo": info,
        "languages": languages,
        "modules": modules,
        "edges": deps.collect(repo),
        "branches": branches,
        "prs": prs,
        "github_note": note,
        "ops": ops.collect(repo),
    }
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "atlas.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(atlas, f, indent=1)
    print("wrote %s (%d modules, %d edges, %d branches, %d PRs)" %
          (path, len(modules), len(atlas["edges"]), len(branches), len(prs)))
    return atlas


def write_prompts(repo, out, md):
    pdir = os.path.join(out, "prompts")
    os.makedirs(pdir, exist_ok=True)
    path = os.path.join(pdir, "enrich.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(prompts.render(repo, out, md))
    print("wrote %s (hand this to your AI agent)" % path)


def render(out, md):
    with open(os.path.join(out, "atlas.json"), encoding="utf-8") as f:
        atlas = json.load(f)
    narratives = {}
    npath = os.path.join(out, "narratives.json")
    if os.path.isfile(npath):
        with open(npath, encoding="utf-8") as f:
            narratives = json.load(f)
    doc = markdown.render(atlas, narratives)
    with open(md, "w", encoding="utf-8") as f:
        f.write(doc)
    state = "enriched" if narratives else "structure-only; run .atlas/prompts/enrich.md to enrich"
    print("wrote %s (%s)" % (md, state))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="repo-atlas",
        description="Generate an AI-ready knowledge graph of a codebase.")
    ap.add_argument("command", nargs="?", default="generate",
                    choices=["scan", "prompts", "render", "generate", "version"],
                    help="scan: extract structure to .atlas/atlas.json | "
                         "prompts: write the AI enrichment prompt | "
                         "render: atlas.json (+narratives.json) -> ATLAS.md | "
                         "generate: scan + prompts + render (default)")
    ap.add_argument("path", nargs="?", default=".", help="repository path (default: .)")
    ap.add_argument("--out", default=None, help="output dir (default: <repo>/.atlas)")
    ap.add_argument("--md", default=None, help="markdown output (default: <repo>/ATLAS.md)")
    ap.add_argument("--no-github", action="store_true", help="skip gh PR lookup")
    ap.add_argument("--pr-limit", type=int, default=1000, help="max PRs to fetch (default 1000)")
    args = ap.parse_args(argv)

    if args.command == "version":
        print("repo-atlas %s" % __version__)
        return 0

    repo = os.path.abspath(args.path)
    if not os.path.isdir(repo):
        print("error: %s is not a directory" % repo, file=sys.stderr)
        return 2
    out = args.out or os.path.join(repo, ".atlas")
    md = args.md or os.path.join(repo, "ATLAS.md")

    try:
        if args.command == "scan":
            scan(repo, out, args.no_github, args.pr_limit)
        elif args.command == "prompts":
            write_prompts(repo, out, md)
        elif args.command == "render":
            render(out, md)
        else:  # generate
            scan(repo, out, args.no_github, args.pr_limit)
            write_prompts(repo, out, md)
            render(out, md)
    except RuntimeError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print("error: %s (run `repo-atlas scan` first?)" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
