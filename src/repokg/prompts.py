"""Emit the enrichment prompt that any AI coding agent can execute."""

PROMPT = """\
# repokg enrichment task

You are an AI coding agent working inside this repository. `repokg` has already
extracted the deterministic structure of this codebase into `{out}/kg.json` and
rendered it as `{md}`. Your job is to add the semantic layer that static analysis
cannot produce — and to verify every claim by actually reading the code.

## Is this a re-run?

If `{out}/narratives.json` already exists, do **not** start over. Structure
refreshes on every scan, but narratives rot silently: a module can be rewritten
while its purpose line stays frozen. `repokg` records what each enriched module
contained when its purpose line was written (`{out}/narratives.lock.json`) and
can tell you exactly which ones have drifted. Re-verify those and nothing else.
Skip to "Steps" below only if `narratives.json` does not exist yet.

1. Find the flagged modules:

```sh
repokg check {repo}
```

   Each `narrative stale: <module> (N of M files changed since enrichment)` line
   is one module to re-verify. `repokg audit {repo} --json` lists the same
   modules as findings with `"kind": "narrative"`; the subject reads
   `<module>: narrative may be stale` and `evidence` names the changed files.
   By default any changed file flags a module; if this repo's CI passes
   `--stale-threshold PERCENT` to `check`, pass the same value here.
2. For each flagged module, and only those, re-read its code, starting with the
   changed files. Compare what it does now with its current line in
   `narratives.json`. Do not re-read, re-word or re-verify modules that were not
   flagged: their stamps say nothing has changed.
3. Decide per module:
   - The purpose line is now wrong or incomplete: rewrite that one line in
     `{out}/narratives.json`. Rewriting it re-stamps the module on the next render.
   - The purpose line still holds: leave the text alone and re-stamp it with
     `--restamp` in the next step.
4. Re-render, passing `--restamp <module>` (repeatable) for each module that
   still holds. Rewritten modules do not need it:

```sh
repokg render {repo} --restamp path/of/module --restamp another/module
```

5. Run `repokg check {repo}` again. It should no longer report a stale
   narrative. If it still flags a module you did not touch, it changed while you
   were working; handle it the same way.

Rules for a re-run:

- Never use `--restamp` on a module you have not actually re-read. It certifies
  "I verified this against the current code"; using it to silence a flag
  defeats the check.
- Touch other parts of `narratives.json` (overview, flows, timeline, gotchas)
  only if what you read in a flagged module shows them to be false.
- If `repokg check` reports nothing stale, there is nothing to do. If it says
  narratives exist but there is no lock, run `repokg render {repo}` once to
  start tracking and stop.

## Steps

1. Read `{out}/kg.json`. Note the modules (sorted by LOC), the dependency edges,
   the branch/PR inventory, and the ops surface.
2. Explore the code. Prioritize: entry points (main/cli files), the largest
   non-generated modules, README/docs, and config files. For each module in the
   inventory, determine its real purpose from its code — do not guess from its name.
3. Reconstruct the data flow(s): how does input enter the system, what transforms
   it, where does it exit? Follow the dependency edges in kg.json.
4. Reconstruct the project timeline from the PR catalog: group PRs into eras/epics
   with a theme each (e.g. "Feb: FIX adapter built", "May: relayer fleet").
5. Collect gotchas: non-obvious conventions, footguns, deliberate quirks
   (check CLAUDE.md / CONTRIBUTING / lint configs for hints, then verify).
6. Write your findings to `{out}/narratives.json` with EXACTLY this schema
   (it is validated on render; unknown keys or wrong types fail loudly):

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
  "gotchas": ["Non-obvious fact an AI or new dev must know before editing."],
  "sections": [
    {{"title": "Environments & deploys",
      "body": "Free-form **markdown** for depth the fixed fields can't hold:\nenvironment tables, revert sagas, architecture narrative, mermaid diagrams."}}
  ]
}}
```

7. Re-render the final document (this also records each module's fingerprint,
   which is what lets a later run find out which narratives went stale), and wire it into this repo's agent context files
   (CLAUDE.md / AGENTS.md / Cursor rules) so future agents find it:

```sh
repokg render {repo}
repokg inject {repo}
```

## Quality bar

- Every module purpose must come from reading that module's code, not its name.
- Every flow step should name real types/functions/files.
- If you cannot verify a claim, leave it out rather than guessing.
- Cover at minimum every module above the median LOC in the inventory.
"""


def render(repo, out, md):
    return PROMPT.format(repo=repo, out=out, md=md)
