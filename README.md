# repokg

[![CI](https://github.com/NehharShah/repokg/actions/workflows/ci.yml/badge.svg)](https://github.com/NehharShah/repokg/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/repokg.svg)](https://pypi.org/project/repokg/)
[![Python versions](https://img.shields.io/pypi/pyversions/repokg.svg)](https://pypi.org/project/repokg/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Generate an **AI-ready knowledge graph** of any codebase — so an AI agent (or a new
developer) can read one file and start building immediately.

`repokg` extracts everything that can be known *deterministically* about a repo —
module inventory, internal import graph, every branch classified against every PR
(merged / squash-merged / abandoned / stale), contributor stats, CI/Docker/Helm/Make
surface — and renders it as:

- **`KNOWLEDGE_GRAPH.md`** — a single human/AI-readable document with a mermaid architecture
  graph, module tables, branch & PR catalog, timeline, and ops inventory.
- **`.repokg/kg.json`** — the same graph, machine-readable.

The semantic layer (module purposes, data-flow narratives, project eras, gotchas)
can't be produced by static analysis without guessing — so repokg is
**agent-first**: it emits `.repokg/prompts/enrich.md`, a rigorous prompt any AI coding
agent (Claude Code, Cursor, Copilot Workspace…) executes to verify-and-fill the
narrative sections, writing `.repokg/narratives.json`. Re-render and the knowledge graph is
complete. No API keys, no LLM dependency in the tool itself.

## Install

```sh
pipx install repokg        # or: pip install repokg
# from source:
pipx install git+https://github.com/NehharShah/repokg
```

Requirements: Python ≥ 3.9, `git`. Optional: [`gh`](https://cli.github.com) (logged
in) for the PR/branch cross-reference — without it the knowledge graph still builds, minus PR data.

## Usage

```sh
cd your-repo
repokg                      # = generate: scan + prompts + render
```

Output:

```
.repokg/kg.json               # machine-readable knowledge graph
.repokg/prompts/enrich.md        # hand this to your AI agent
KNOWLEDGE_GRAPH.md                        # the knowledge graph document
```

Then, in your AI agent of choice:

> Follow the instructions in .repokg/prompts/enrich.md

The agent explores the code, writes `.repokg/narratives.json`, and runs
`repokg render` — KNOWLEDGE_GRAPH.md now carries verified purposes, data flows,
timeline eras, and gotchas alongside the deterministic structure.

### Commands

| Command | Effect |
|---|---|
| `repokg scan [path]` | Extract structure → `.repokg/kg.json` |
| `repokg prompts [path]` | Write the enrichment prompt |
| `repokg render [path]` | `kg.json` (+ `narratives.json`) → `KNOWLEDGE_GRAPH.md` |
| `repokg generate [path]` | All three (default) |
| `repokg inject [path]` | Wire the knowledge graph into `CLAUDE.md` / `AGENTS.md` / Cursor rules (`--diff` for dry run) |
| `repokg audit [path]` | Show every *inferred* conclusion with confidence + evidence (`--json` for machines) |
| `repokg clean [path]` | Remove everything repokg authored — never touches your content (`--diff` for dry run) |
| `repokg check [path]` | Exit 1 if the knowledge graph is stale vs `HEAD` (CI-friendly) |

Flags: `--out DIR` (default `<repo>/.repokg`), `--md FILE` (default `<repo>/KNOWLEDGE_GRAPH.md`),
`--no-github`, `--pr-limit N`, `--diff`, `--json`.

## Honesty layer

Most of the graph is measured fact. The parts that are *heuristics* are labeled
as findings with confidence and evidence, surfaced by `repokg audit`:

```
[git]
  trunk = master          high    detected via origin/HEAD symref
  integration = staging   medium  matched a well-known integration branch name
[modules]
  4 flagged generated     low     path-name heuristic; verify before excluding
```

Agent-written `narratives.json` is schema-validated before rendering — malformed
enrichment fails loudly with errors precise enough for the agent to self-correct.
(Findings/confidence design inspired by [RepoCanon](https://github.com/NehharShah/repocanon).)

## Agent integration

`repokg inject` adds a **managed block** (delimited by
`<!-- repokg:begin/end -->`, idempotent, never touches your hand-written
content) pointing agents at KNOWLEDGE_GRAPH.md:

- **`CLAUDE.md`** (Claude Code) — updated if present
- **`AGENTS.md`** (the cross-tool agent standard) — updated if present, created if
  no agent file exists at all
- **`.github/copilot-instructions.md`** (Copilot) — updated if present
- **`.cursor/rules/repokg.mdc`** (Cursor, with `alwaysApply: true`) — created
  if `.cursor/rules/` exists; falls back to legacy `.cursorrules`

Keep it fresh in CI:

```yaml
- run: pipx run repokg check . || echo "::warning::KNOWLEDGE_GRAPH.md is stale"
```

KNOWLEDGE_GRAPH.md itself also lists any agent-context files it found, so an agent landing
on the knowledge graph discovers your rules — and vice versa.

## What gets extracted (all verified, never guessed)

| Area | How |
|---|---|
| Branch classification | `git for-each-ref` + `--merged` ancestry vs the integration branch (auto-detects `staging`/`develop`), cross-referenced with every PR's head ref via `gh` — distinguishes true merges from squash-merges from abandoned work |
| PR catalog | `gh pr list --state all` — open / merged / closed-unmerged, full appendix table |
| Module inventory | Filesystem walk with LOC per directory, language detection, generated-code flagging |
| Import graph | Go: `import` blocks resolved against `go.mod` module paths · Python: stdlib `ast` incl. relative imports · JS/TS: relative `import`/`require` resolution · Rust: `use` declarations resolved against Cargo crate names (cross-crate) and `src/` module trees (intra-crate). Directory→directory edges with counts |
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
- **Rust**: `use` declarations only — macro-generated imports, re-export
  chains, and `[dependencies] path = …` (non-workspace) crates are not
  resolved; `crate::` paths ground only in module dirs/files that exist.
- Branch `ahead` counts use one batched git call on git ≥ 2.41, with a
  per-branch fallback on older git.

## Roadmap

- [x] Rust import graph
- [ ] Java / Kotlin import graphs
- [ ] `--exclude` glob patterns
- [ ] `llms.txt` emission alongside KNOWLEDGE_GRAPH.md
- [ ] tsconfig `paths` alias resolution
- [ ] PyPI release + prebuilt GitHub Action

## Development

```sh
pip install -e .
python -m unittest discover -s tests -v
```

No runtime dependencies — stdlib only.

## Contributing

All work goes through issue → branch (`issue-<N>/<desc>`) → PR → review → squash-merge to `main`.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and the ground rules
(zero deps, findings for heuristics, `clean` reversibility).

## License

MIT
