"""Wire KNOWLEDGE_GRAPH.md into agent context files: CLAUDE.md, AGENTS.md,
Cursor rules, Copilot instructions.

A single managed block (delimited by markers) is inserted or replaced, so
re-running `repokg inject` is idempotent and never touches surrounding
hand-written content. `clean` reverses everything, deleting only what repokg
authored outright and stripping only its own block from shared files.
"""

import os
import re
import shutil

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

# Ownership marker: KNOWLEDGE_GRAPH.md is only deleted by `clean` if it
# carries the generated-by line from our renderer.
OWNERSHIP_MARKER = "(by [repokg]"


def block(md_rel):
    return TEMPLATE.format(begin=BEGIN, end=END, md=md_rel)


def upsert(path, content, prefix="", write=True):
    """Insert or replace the managed block in path.

    Returns (status, old_text, new_text) where status is
    'created' | 'updated' | 'unchanged'. With write=False nothing touches disk.
    """
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
        if BEGIN in old and END in old:
            pre, rest = old.split(BEGIN, 1)
            _, post = rest.split(END, 1)
            new = pre + content + post
        else:
            new = old.rstrip("\n") + "\n\n" + content + "\n"
        if new == old:
            return "unchanged", old, old
        status = "updated"
    else:
        old = ""
        new = prefix + content + "\n"
        status = "created"
    if write:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    return status, old, new


def run(repo, md, write=True):
    """Inject the knowledge-graph pointer into every agent-context surface
    present. Returns {relative_path: (status, old, new)}."""
    md_rel = os.path.relpath(md, repo)
    if md_rel.startswith(".."):
        md_rel = md  # knowledge graph rendered outside the repo; keep absolute
    b = block(md_rel)
    results = {}

    existing = [f for f in AGENT_FILES if os.path.isfile(os.path.join(repo, f))]
    for f in existing or ["AGENTS.md"]:
        results[f] = upsert(os.path.join(repo, f), b, write=write)

    if os.path.isdir(os.path.join(repo, CURSOR_RULES_DIR)):
        rel = CURSOR_RULES_DIR + "/repokg.mdc"
        results[rel] = upsert(os.path.join(repo, rel), b,
                              prefix=CURSOR_FRONTMATTER, write=write)
    elif os.path.isfile(os.path.join(repo, ".cursorrules")):
        results[".cursorrules"] = upsert(
            os.path.join(repo, ".cursorrules"), b, write=write)

    return results


def clean(repo, out, md, write=True):
    """Remove everything repokg authored. Returns {relative_path: action}.

    Ownership rules (never destroy user content):
    - `.repokg/` and `.cursor/rules/repokg.mdc` are wholly ours -> deleted.
    - KNOWLEDGE_GRAPH.md is deleted only if it carries our generated-by
      marker; otherwise skipped with a warning action.
    - Agent files (CLAUDE.md etc.): only our marker block is stripped; if the
      file becomes empty afterwards we authored it, so it is deleted.
    """
    actions = {}

    def rel(p):
        r = os.path.relpath(p, repo)
        return p if r.startswith("..") else r

    for target in list(AGENT_FILES) + [".cursorrules"]:
        path = os.path.join(repo, target)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if BEGIN not in text or END not in text:
            continue
        pre, rest = text.split(BEGIN, 1)
        _, post = rest.split(END, 1)
        stripped = re.sub(r"\n{3,}", "\n\n", pre + post).strip()
        if stripped:
            if write:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(stripped + "\n")
            actions[target] = "block stripped"
        else:
            if write:
                os.remove(path)
            actions[target] = "deleted (was repokg-authored)"

    mdc = os.path.join(repo, CURSOR_RULES_DIR, "repokg.mdc")
    if os.path.isfile(mdc):
        if write:
            os.remove(mdc)
        actions[CURSOR_RULES_DIR + "/repokg.mdc"] = "deleted"

    if os.path.isfile(md):
        with open(md, encoding="utf-8") as f:
            head = f.read(2048)
        if OWNERSHIP_MARKER in head:
            if write:
                os.remove(md)
            actions[rel(md)] = "deleted"
        else:
            actions[rel(md)] = "SKIPPED (no repokg generated-by marker; not ours)"

    if os.path.isdir(out):
        if write:
            shutil.rmtree(out)
        actions[rel(out) + "/"] = "deleted"

    return actions
