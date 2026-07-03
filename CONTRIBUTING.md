# Contributing to repokg

## Workflow (required)

**All work lands through pull requests. Nothing is committed directly to `main`.**

1. **Claim an issue.** Every change starts from a GitHub issue — comment on it so
   work isn't duplicated. No issue for what you want to do? Open one first.
2. **Branch off `main`**, named for the issue:

   ```
   issue-<N>/<short-description>
   # e.g. issue-3/tsconfig-paths-resolution
   ```

3. **Keep PRs small.** Roadmap issues include a *Stacked PRs* plan — follow it:
   each PR in the stack must land green and reviewable on its own. Prefer three
   100-line PRs over one 300-line PR.
4. **Open a PR to `main`** using the PR template (What / Why / Testing, linked
   issue with `Resolves #N`).
5. **Review + green CI are required to merge.** A maintainer reviews every PR;
   CI (tests on Python 3.9/3.12/3.13 + smoke run) must pass. PRs are
   squash-merged, so the PR title becomes the commit message — write it well.

## Ground rules for every PR

- **Zero runtime dependencies** — stdlib only. This is the project's identity
  and is non-negotiable. Test-time/tooling deps are also avoided (unittest, not
  pytest).
- **No guessed facts.** Anything heuristic must register a finding with
  confidence + evidence (see `findings.py`) so `repokg audit` surfaces it.
- **No silent coverage loss.** If your change excludes, caps, or degrades
  anything, it must say so (scan output line or uncertainty note).
- **Reversibility.** Any new artifact repokg writes must be removed by
  `repokg clean`, and `clean` must never touch user-authored content
  (ownership markers — see `inject.py`).
- **Tests required.** New behavior needs unit tests; bug fixes need a
  regression test that fails without the fix.
- Python ≥ 3.9 compatibility (no `match`, no `X | Y` type syntax in runtime code).

## Development setup

```sh
git clone https://github.com/NehharShah/repokg
cd repokg
pip install -e .
python -m unittest discover -s tests -v   # all tests
repokg generate . --no-github             # dogfood on this repo
```

## Releases (maintainers)

Bump `version` in `pyproject.toml` + `__version__` in `src/repokg/__init__.py`,
merge, then tag `vX.Y.Z` and push the tag. The release workflow publishes to
PyPI via trusted publishing after maintainer approval of the `pypi` environment.
