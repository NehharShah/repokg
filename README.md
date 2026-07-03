# repo-atlas

Generate an **AI-ready knowledge graph** of any codebase — so an AI agent (or a new
developer) can read one file and start building immediately.

`repo-atlas` extracts everything that can be known *deterministically* about a repo —
module inventory, internal import graph, every branch classified against every PR
(merged / squash-merged / abandoned / stale), contributor stats, CI/Docker/Helm/Make
surface — and renders it as:

- **`ATLAS.md`** — a single human/AI-readable document with a mermaid architecture
  graph, module tables, branch & PR catalog, timeline, and ops inventory.
- **`.atlas/atlas.json`** — the same graph, machine-readable.

The semantic layer (module purposes, data-flow narratives, project eras, gotchas)
can't be produced by static analysis without guessing — so repo-atlas is
**agent-first**: it emits `.atlas/prompts/enrich.md`, a rigorous prompt any AI coding
agent (Claude Code, Cursor, Copilot Workspace…) executes to verify-and-fill the
narrative sections, writing `.atlas/narratives.json`. Re-render and the atlas is
complete. No API keys, no LLM dependency in the tool itself.

## Install

```sh
pipx install repo-atlas        # or: pip install repo-atlas
# from source:
pipx install git+https://github.com/NehharShah/repo-atlas
```

Requirements: Python ≥ 3.9, `git`. Optional: [`gh`](https://cli.github.com) (logged
in) for the PR/branch cross-reference — without it the atlas still builds, minus PR data.

## Usage

```sh
cd your-repo
repo-atlas                      # = generate: scan + prompts + render
```

Output:

```
.atlas/atlas.json               # machine-readable knowledge graph
.atlas/prompts/enrich.md        # hand this to your AI agent
ATLAS.md                        # the knowledge graph document
```

Then, in your AI agent of choice:

> Follow the instructions in .atlas/prompts/enrich.md

The agent explores the code, writes `.atlas/narratives.json`, and runs
`repo-atlas render` — ATLAS.md now carries verified purposes, data flows,
timeline eras, and gotchas alongside the deterministic structure.

### Commands

| Command | Effect |
|---|---|
| `repo-atlas scan [path]` | Extract structure → `.atlas/atlas.json` |
| `repo-atlas prompts [path]` | Write the enrichment prompt |
| `repo-atlas render [path]` | `atlas.json` (+ `narratives.json`) → `ATLAS.md` |
| `repo-atlas generate [path]` | All three (default) |
| `repo-atlas inject [path]` | Wire the atlas into `CLAUDE.md` / `AGENTS.md` / Cursor rules |
| `repo-atlas check [path]` | Exit 1 if the atlas is stale vs `HEAD` (CI-friendly) |

Flags: `--out DIR` (default `<repo>/.atlas`), `--md FILE` (default `<repo>/ATLAS.md`),
`--no-github`, `--pr-limit N`.

## Agent integration

`repo-atlas inject` adds a **managed block** (delimited by
`<!-- repo-atlas:begin/end -->`, idempotent, never touches your hand-written
content) pointing agents at ATLAS.md:

- **`CLAUDE.md`** (Claude Code) — updated if present
- **`AGENTS.md`** (the cross-tool agent standard) — updated if present, created if
  no agent file exists at all
- **`.github/copilot-instructions.md`** (Copilot) — updated if present
- **`.cursor/rules/repo-atlas.mdc`** (Cursor, with `alwaysApply: true`) — created
  if `.cursor/rules/` exists; falls back to legacy `.cursorrules`

Keep it fresh in CI:

```yaml
- run: pipx run repo-atlas check . || echo "::warning::ATLAS.md is stale"
```

ATLAS.md itself also lists any agent-context files it found, so an agent landing
on the atlas discovers your rules — and vice versa.

## What gets extracted (all verified, never guessed)

| Area | How |
|---|---|
| Branch classification | `git for-each-ref` + `--merged` ancestry vs the integration branch (auto-detects `staging`/`develop`), cross-referenced with every PR's head ref via `gh` — distinguishes true merges from squash-merges from abandoned work |
| PR catalog | `gh pr list --state all` — open / merged / closed-unmerged, full appendix table |
| Module inventory | Filesystem walk with LOC per directory, language detection, generated-code flagging |
| Import graph | Go: `import` blocks resolved against `go.mod` module paths · Python: stdlib `ast` incl. relative imports · JS/TS: relative `import`/`require` resolution. Directory→directory edges with counts |
| Ops surface | CI workflow names, Dockerfiles, compose files, Helm charts, Makefile targets, config/docs/test/migration dirs |
| Timeline | Merged PRs grouped by month with conventional-commit scope frequencies (replaced by agent-written eras after enrichment) |

## Why agent-first instead of calling an LLM API?

Because the enrichment quality depends on *reading the code*, and your coding agent
already has the repo open, tools to search it, and your permission model. A prompt it
can execute beats a second LLM integration with its own keys, costs, and context limits.
The contract between tool and agent is one JSON file (`narratives.json`) with a fixed
schema — everything else stays deterministic and reproducible.

## Known limitations

- **JS/TS**: only relative imports are resolved; alias imports (`@/…`,
  tsconfig `paths`) are ignored.
- **Fork PRs**: a fork PR whose head branch name matches a local branch will be
  linked to it (GitHub's API reports bare head refs).
- **Python**: packages are discovered at the repo root and under `src/`;
  deeper monorepo layouts (`packages/*/src/…`) get file-level edges only.
- Branch `ahead` counts use one batched git call on git ≥ 2.41, with a
  per-branch fallback on older git.

## Roadmap

- [ ] Rust / Java / Kotlin import graphs
- [ ] `--exclude` glob patterns
- [ ] `llms.txt` emission alongside ATLAS.md
- [ ] tsconfig `paths` alias resolution
- [ ] PyPI release + prebuilt GitHub Action

## Development

```sh
pip install -e .
python -m unittest discover -s tests -v
```

No runtime dependencies — stdlib only.

## License

MIT
