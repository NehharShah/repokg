"""Wire KNOWLEDGE_GRAPH.md into agent context files: CLAUDE.md, AGENTS.md, Cursor rules,
Copilot instructions.

A single managed block (delimited by markers) is inserted or replaced, so
re-running `repokg inject` is idempotent and never touches surrounding
hand-written content.
"""

import os

BEGIN = "<!-- repokg:begin -->"
END = "<!-- repokg:end -->"

TEMPLATE = """{begin}
## Codebase knowledge graph

This repository has a generated knowledge graph: **{md}** (read this FIRST,
before exploring the codebase) and `.repokg/kg.json` (machine-readable).
It contains the module inventory, internal import graph, branch/PR state,
timeline, and ops surface — extracted from source, not guessed.

- Regenerate after structural changes: `repokg generate .`
- Check freshness (safe for CI): `repokg check .`
- Improve the narrative layer: follow `.repokg/prompts/enrich.md`
{end}"""

# Files that, when present, receive the block. If none exist, AGENTS.md is created.
AGENT_FILES = ("CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md")
CURSOR_RULES_DIR = ".cursor/rules"
CURSOR_FRONTMATTER = (
    "---\ndescription: Generated codebase knowledge graph — read before exploring\n"
    "alwaysApply: true\n---\n\n")


def block(md_rel):
    return TEMPLATE.format(begin=BEGIN, end=END, md=md_rel)


def upsert(path, content, prefix=""):
    """Insert or replace the managed block in path.
    Returns 'created' | 'updated' | 'unchanged'."""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if BEGIN in text and END in text:
            pre, rest = text.split(BEGIN, 1)
            _, post = rest.split(END, 1)
            new = pre + content + post
        else:
            new = text.rstrip("\n") + "\n\n" + content + "\n"
        if new == text:
            return "unchanged"
        status = "updated"
    else:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        new = prefix + content + "\n"
        status = "created"
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    return status


def run(repo, md):
    """Inject the knowledge-graph pointer into every agent-context surface present.
    Returns {relative_path: status}."""
    md_rel = os.path.relpath(md, repo)
    if md_rel.startswith(".."):
        md_rel = md  # knowledge graph rendered outside the repo; keep absolute
    b = block(md_rel)
    results = {}

    existing = [f for f in AGENT_FILES if os.path.isfile(os.path.join(repo, f))]
    for f in existing or ["AGENTS.md"]:
        results[f] = upsert(os.path.join(repo, f), b)

    if os.path.isdir(os.path.join(repo, CURSOR_RULES_DIR)):
        rel = CURSOR_RULES_DIR + "/repokg.mdc"
        results[rel] = upsert(os.path.join(repo, rel), b, prefix=CURSOR_FRONTMATTER)
    elif os.path.isfile(os.path.join(repo, ".cursorrules")):
        results[".cursorrules"] = upsert(os.path.join(repo, ".cursorrules"), b)

    return results
