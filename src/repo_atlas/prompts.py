"""Emit the enrichment prompt that any AI coding agent can execute."""

PROMPT = """\
# repo-atlas enrichment task

You are an AI coding agent working inside this repository. `repo-atlas` has already
extracted the deterministic structure of this codebase into `{out}/atlas.json` and
rendered it as `{md}`. Your job is to add the semantic layer that static analysis
cannot produce — and to verify every claim by actually reading the code.

## Steps

1. Read `{out}/atlas.json`. Note the modules (sorted by LOC), the dependency edges,
   the branch/PR inventory, and the ops surface.
2. Explore the code. Prioritize: entry points (main/cli files), the largest
   non-generated modules, README/docs, and config files. For each module in the
   inventory, determine its real purpose from its code — do not guess from its name.
3. Reconstruct the data flow(s): how does input enter the system, what transforms
   it, where does it exit? Follow the dependency edges in atlas.json.
4. Reconstruct the project timeline from the PR catalog: group PRs into eras/epics
   with a theme each (e.g. "Feb: FIX adapter built", "May: relayer fleet").
5. Collect gotchas: non-obvious conventions, footguns, deliberate quirks
   (check CLAUDE.md / CONTRIBUTING / lint configs for hints, then verify).
6. Write your findings to `{out}/narratives.json` with EXACTLY this schema:

```json
{{
  "overview": "2-4 sentence description of what this project is and does.",
  "modules": {{
    "path/relative/to/repo": "One-line purpose, verified from code.",
    "another/module": "..."
  }},
  "flows": [
    {{"name": "Request lifecycle", "steps": ["step 1 ...", "step 2 ..."]}}
  ],
  "timeline": [
    {{"period": "2026-01", "theme": "What happened in this era, with PR numbers."}}
  ],
  "gotchas": ["Non-obvious fact an AI or new dev must know before editing."]
}}
```

7. Re-render the final document:

```sh
repo-atlas render {repo}
```

## Quality bar

- Every module purpose must come from reading that module's code, not its name.
- Every flow step should name real types/functions/files.
- If you cannot verify a claim, leave it out rather than guessing.
- Cover at minimum every module above the median LOC in the inventory.
"""


def render(repo, out, md):
    return PROMPT.format(repo=repo, out=out, md=md)
