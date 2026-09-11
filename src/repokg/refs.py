"""Lay out the repository as it was at a git ref, so it can be scanned.

`repokg diff` compares two knowledge graphs. Producing one for a commit means
producing the *tree* for that commit, and the honest way to do that is to let
git lay it out: `git worktree add --detach` into a throwaway directory, scan
it, remove it.

The lighter alternative — `git archive` piped into a temp dir — was rejected.
It produces a directory that is not a git repository, and a repokg scan
collects git facts as well as file facts. A worktree shares the repo's .git,
so a scan inside one reports the ref's own commit sha as `head` and still
resolves the trunk through origin/HEAD. That sha is the entire provenance line
of a diff, so losing it to save a checkout is a bad trade.

Two things a caller could get wrong are therefore not left to the caller.

The checkout is removed even when the scan raises. A leaked worktree is not
merely a stray temp directory: it stays registered in .git and shows up in
every later `git worktree list`, so the failure would be visible in the user's
repo long after the command that caused it.

And nothing here decides what to scan or how. This module hands back a path;
the caller scans it. That keeps the git plumbing testable on its own, and
keeps this module's imports down to gitinfo — which owns every other git
subprocess in the codebase, and whose error convention this follows.
"""

import contextlib
import os
import shutil
import tempfile

from . import gitinfo


def resolve(repo, ref):
    """The commit sha `ref` names, or raise RuntimeError saying it does not.

    Peeled to a commit with `^{commit}`, so an annotated tag resolves to what
    it points at rather than to the tag object, whose tree cannot be checked
    out.
    """
    sha = gitinfo.try_run(repo, "rev-parse", "--verify", "-q",
                          "%s^{commit}" % ref)
    if sha:
        return sha
    raise RuntimeError("'%s' is not a commit in this repository%s"
                       % (ref, _missing_hint(repo)))


@contextlib.contextmanager
def checkout(repo, ref):
    """Yield a path holding `repo`'s tree at `ref`, removed on the way out.

    Detached, so checking out a ref that is also a branch does not contend
    with that branch being checked out in the main working tree — which is
    the common case, since the ref a diff is usually against is the trunk.
    """
    sha = resolve(repo, ref)
    parent = tempfile.mkdtemp(prefix="repokg-ref-")
    # named for the repo so that a repo with no remote, whose name the scan
    # falls back to the directory for, does not report itself as a temp dir
    tree = os.path.join(parent, os.path.basename(os.path.abspath(repo))
                        or "repo")
    try:
        gitinfo.run(repo, "worktree", "add", "--detach", "-q", tree, sha)
    except (RuntimeError, OSError):
        shutil.rmtree(parent, ignore_errors=True)
        raise
    try:
        yield tree
    finally:
        # `remove` unregisters and deletes; the rmtree covers the temp parent
        # it does not own, and the prune covers a `remove` that itself failed
        # and would otherwise leave a registration behind in the user's repo.
        gitinfo.try_run(repo, "worktree", "remove", "--force", tree)
        shutil.rmtree(parent, ignore_errors=True)
        gitinfo.try_run(repo, "worktree", "prune")


def _missing_hint(repo):
    """Why a ref might be absent, but only when it demonstrably might be.

    A shallow clone is the case that matters, because it is the default:
    `actions/checkout` fetches depth 1, so the base branch a CI diff wants to
    compare against is often not in the clone at all. The hint is conditional
    on the repository actually being shallow rather than printed always —
    guessing at the cause of an error is how a message ends up sending someone
    after the wrong thing.
    """
    if gitinfo.try_run(repo, "rev-parse", "--is-shallow-repository") == "true":
        return (" — this clone is shallow, so most history is absent from it "
                "(actions/checkout needs fetch-depth: 0)")
    return ""
