"""Tests for diffing against a git ref.

Two questions here. Does a ref actually get scanned as it was — the right
tree, the right commit sha, tags and shas and HEAD all resolving? And is the
throwaway checkout genuinely throwaway: no worktree left registered in the
user's repo, and above all no cache written over the one the working tree's
scans depend on.

That last one is the reason this is not simply `build_graph(some_other_dir)`.
The cache keys on the repo's HEAD and on file mtimes, so a scan of a
historical commit has neither, and letting it save would poison the real
cache with facts belonging to a different tree.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from repokg import refs
from repokg.cli import main

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

BASE = {
    ".gitignore": ".repokg/\n",
    "app/__init__.py": "",
    "app/main.py": "from lib import helper\n",
    "lib/__init__.py": "",
    "lib/helper.py": "def helper(): pass\n",
}

# The second commit adds a module and an import edge into it, so a diff
# between the two commits has a shape change to find rather than only drift.
SECOND = {
    "billing/__init__.py": "",
    "billing/charge.py": "def charge(): pass\n",
    "app/main.py": "from lib import helper\nfrom billing import charge\n",
}


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, env=ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def git_out(repo, *args):
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                       text=True, env=ENV)
    return p.stdout.strip()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def copy_repo(src, dst):
    """Copy a git repo, skipping git's transient lock files — background
    maintenance can drop a .lock between copytree listing a directory and
    reading it, which fails the whole copy."""
    shutil.copytree(src, dst, ignore=lambda d, names: [
        n for n in names if n.endswith(".lock")])


class RefCase(unittest.TestCase):
    """One two-commit repo built per class and copied per test.

    `first` is the commit with app and lib; `second`, tagged v1 with an
    annotated tag, adds billing and an edge into it.
    """

    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.mkdtemp()
        for rel, text in BASE.items():
            write(cls.template, rel, text)
        git(cls.template, "init", "-q", "-b", "main", ".")
        git(cls.template, "config", "maintenance.auto", "false")
        git(cls.template, "config", "gc.auto", "0")
        git(cls.template, "add", "-A")
        git(cls.template, "commit", "-qm", "first")
        cls.first = git_out(cls.template, "rev-parse", "HEAD")
        for rel, text in SECOND.items():
            write(cls.template, rel, text)
        git(cls.template, "add", "-A")
        git(cls.template, "commit", "-qm", "second")
        # annotated, so resolving it has to peel a tag object to a commit
        git(cls.template, "tag", "-a", "v1", "-m", "release one")
        cls.second = git_out(cls.template, "rev-parse", "HEAD")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.template, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        copy_repo(self.template, self.repo)
        self.out = os.path.join(self.repo, ".repokg")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        """(exit code, stdout, stderr) for one repokg invocation."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def diff(self, *extra):
        return self.run_cli("diff", self.repo, "--no-github", *extra)

    def seed(self):
        rc, _, _ = self.run_cli("scan", self.repo, "--no-github")
        self.assertEqual(rc, 0)

    def worktrees(self):
        """Paths git has registered as worktrees, the main one excluded."""
        lines = git_out(self.repo, "worktree", "list", "--porcelain")
        paths = [line.split(" ", 1)[1] for line in lines.splitlines()
                 if line.startswith("worktree ")]
        return [p for p in paths
                if os.path.realpath(p) != os.path.realpath(self.repo)]


class TestTwoRefs(RefCase):
    """Both sides scanned the same way, which is the comparable case."""

    def test_it_finds_the_module_and_edge_the_second_commit_added(self):
        rc, stdout, _ = self.diff("--from-ref", self.first,
                                  "--to-ref", self.second)
        self.assertEqual(rc, 1)
        self.assertIn("+ billing", stdout)
        self.assertIn("app -> billing", stdout)
        self.assertIn("shape changed", stdout)

    def test_reversing_the_refs_reverses_the_finding(self):
        rc, stdout, _ = self.diff("--from-ref", self.second,
                                  "--to-ref", self.first)
        self.assertEqual(rc, 1)
        self.assertIn("- billing", stdout)

    def test_a_ref_against_itself_is_no_change(self):
        rc, stdout, _ = self.diff("--from-ref", self.second,
                                  "--to-ref", self.second)
        self.assertEqual(rc, 0)
        self.assertIn("no change", stdout)

    def test_the_provenance_line_carries_each_commit_sha(self):
        """The reason this uses a worktree and not `git archive`: a scan of a
        directory that is not a checkout could not report these."""
        rc, stdout, _ = self.diff("--from-ref", self.first,
                                  "--to-ref", self.second, "--format", "json")
        delta = json.loads(stdout)
        self.assertEqual(delta["old"]["head"], self.first)
        self.assertEqual(delta["new"]["head"], self.second)

    def test_two_refs_need_no_baseline_on_disk(self):
        """Naming both sides means never reading <out>/kg.json, so this works
        in a repo that has never been scanned."""
        self.assertFalse(os.path.exists(self.out))
        rc, _, _ = self.diff("--from-ref", self.first, "--to-ref", self.second)
        self.assertEqual(rc, 1)


class TestResolution(RefCase):
    """Which spellings of a ref work.

    Against `resolve` rather than through the CLI: what is under test is one
    `git rev-parse`, and routing each spelling through two checkouts and two
    cold scans would test the diff pipeline four more times over instead.
    """

    def test_every_spelling_git_accepts(self):
        for ref, want in (("v1", self.second),          # annotated tag
                          ("main", self.second),        # branch
                          ("HEAD", self.second),
                          ("HEAD~1", self.first),       # relative
                          (self.first, self.first),     # full sha
                          (self.first[:8], self.first)):  # abbreviated sha
            self.assertEqual(refs.resolve(self.repo, ref), want, ref)

    def test_an_annotated_tag_resolves_to_its_commit_not_the_tag_object(self):
        """`^{commit}` matters here: a tag object's own sha is not a tree
        anything can be checked out from."""
        tag_object = git_out(self.repo, "rev-parse", "v1")
        self.assertNotEqual(tag_object, self.second)  # it is annotated
        self.assertEqual(refs.resolve(self.repo, "v1"), self.second)


class TestOneRef(RefCase):
    """A ref against the working tree: the asymmetric case, which says so."""

    def test_it_compares_the_commit_against_the_working_tree(self):
        write(self.repo, "extra/__init__.py", "")
        write(self.repo, "extra/thing.py", "x = 1\n")
        rc, stdout, _ = self.diff("--from-ref", self.second)
        self.assertEqual(rc, 1)
        self.assertIn("+ extra", stdout)

    def test_mixing_a_ref_with_a_working_tree_scan_is_disclosed(self):
        rc, stdout, _ = self.diff("--from-ref", self.second)
        self.assertIn("holds files no commit does", stdout)

    def test_two_refs_carry_no_such_note(self):
        """Both sides built the same way, so there is no asymmetry to warn
        about and warning anyway would be noise."""
        rc, stdout, _ = self.diff("--from-ref", self.first,
                                  "--to-ref", self.second)
        self.assertNotIn("holds files no commit does", stdout)

    def test_a_ref_against_a_saved_graph_carries_no_such_note(self):
        """How a stored document was produced is not knowable from the
        document, so claiming the comparison is asymmetric would be a guess.
        """
        self.seed()
        saved = os.path.join(self.tmp, "saved.json")
        shutil.copy(os.path.join(self.out, "kg.json"), saved)
        rc, stdout, _ = self.diff("--from-ref", self.first, "--to", saved)
        self.assertNotIn("holds files no commit does", stdout)

    def test_the_note_lands_in_the_delta_itself(self):
        """So it reaches every format and any consumer of `--format json`,
        rather than being decoration the text renderer adds."""
        rc, stdout, _ = self.diff("--from-ref", self.second, "--format",
                                  "json")
        notes = json.loads(stdout)["notes"]
        self.assertTrue(any("holds files no commit does" in n for n in notes),
                        notes)


class TestThrowaway(RefCase):
    """The checkout has to leave nothing behind, in the filesystem or in
    .git."""

    def test_no_worktree_stays_registered(self):
        """A leaked worktree is not just a stray temp dir — it stays in .git
        and shows up in every later `git worktree list`."""
        self.assertEqual(self.worktrees(), [])
        rc, _, _ = self.diff("--from-ref", self.first, "--to-ref", self.second)
        self.assertEqual(rc, 1)
        self.assertEqual(self.worktrees(), [])

    def test_a_ref_scan_leaves_both_files_in_dot_repokg_untouched(self):
        """The trap this module exists to avoid. The cache keys on HEAD and
        on mtimes, so a historical scan saving into it would replay facts
        from a different tree on the next real scan; and the graph is the
        baseline, so writing over it would destroy the comparison."""
        self.seed()
        before = {name: read_bytes(os.path.join(self.out, name))
                  for name in ("cache.json", "kg.json")}
        rc, _, _ = self.diff("--from-ref", self.first, "--to-ref", self.second)
        self.assertEqual(rc, 1)
        for name, was in before.items():
            self.assertEqual(read_bytes(os.path.join(self.out, name)), was,
                             name)

    def test_a_ref_scan_writes_no_cache_of_its_own(self):
        self.assertFalse(os.path.exists(self.out))
        rc, _, _ = self.diff("--from-ref", self.first, "--to-ref", self.second)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.out))

    def test_the_cache_line_does_not_blame_a_flag_nobody_passed(self):
        """The cache is off here by policy. Reporting it as `--no-cache`
        would send someone looking for a flag they never used."""
        rc, _, stderr = self.diff("--from-ref", self.first)
        self.assertIn("off for a ref scan", stderr)
        self.assertNotIn("--no-cache", stderr)

    def test_a_working_tree_scan_still_blames_the_flag_when_it_is_the_cause(
            self):
        rc, _, stderr = self.diff("--from-ref", self.first, "--no-cache")
        self.assertIn("disabled (--no-cache)", stderr)

    def test_the_checkout_is_removed_even_when_the_scan_raises(self):
        """Cleanup on the happy path is not the interesting case."""
        captured = []
        with self.assertRaises(ValueError):
            with refs.checkout(self.repo, self.first) as tree:
                captured.append(tree)
                self.assertTrue(os.path.isfile(os.path.join(tree,
                                                            "app/main.py")))
                raise ValueError("scan blew up")
        self.assertFalse(os.path.exists(captured[0]))
        self.assertEqual(self.worktrees(), [])

    def test_the_checkout_holds_that_commit_and_not_another(self):
        with refs.checkout(self.repo, self.first) as tree:
            self.assertFalse(os.path.exists(os.path.join(tree, "billing")))
            self.assertEqual(git_out(tree, "rev-parse", "HEAD"), self.first)
        with refs.checkout(self.repo, self.second) as tree:
            self.assertTrue(os.path.isdir(os.path.join(tree, "billing")))

    def test_checking_out_a_ref_that_is_the_checked_out_branch_works(self):
        """Detached for this reason: the trunk is both the usual ref to diff
        against and the branch most likely already checked out."""
        self.assertEqual(git_out(self.repo, "rev-parse", "--abbrev-ref",
                                 "HEAD"), "main")
        with refs.checkout(self.repo, "main") as tree:
            self.assertEqual(git_out(tree, "rev-parse", "HEAD"), self.second)


class TestBadRefs(RefCase):
    """A ref that cannot be scanned is an error, never a shape change."""

    def test_an_unknown_ref_exits_two_with_nothing_on_stdout(self):
        """2 and not 1, or a CI job reads a typo as an architectural change."""
        rc, stdout, stderr = self.diff("--from-ref", "no-such-ref")
        self.assertEqual(rc, 2)
        self.assertEqual(stdout, "")
        self.assertIn("not a commit in this repository", stderr)

    def test_an_unknown_ref_on_either_side_exits_two(self):
        for args in (("--from-ref", "nope"),
                     ("--from-ref", self.first, "--to-ref", "nope")):
            rc, stdout, _ = self.diff(*args)
            self.assertEqual(rc, 2, args)
            self.assertEqual(stdout, "")

    def test_a_ref_and_a_saved_graph_for_one_side_is_refused(self):
        """Two ways to name one side, so neither silently wins."""
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("diff", self.repo, "--from", "a.json",
                         "--from-ref", "main")
        self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(SystemExit):
            self.run_cli("diff", self.repo, "--to", "a.json",
                         "--to-ref", "main")

    def test_an_empty_ref_is_not_taken_for_no_ref_at_all(self):
        """`--from-ref ""` is a mistake, and falling back to the default
        baseline would answer a question nobody asked."""
        rc, _, stderr = self.diff("--from-ref", "")
        self.assertEqual(rc, 2)
        self.assertIn("not a commit", stderr)

    def test_resolve_reports_the_ref_it_could_not_find(self):
        with self.assertRaises(RuntimeError) as caught:
            refs.resolve(self.repo, "wat")
        self.assertIn("wat", str(caught.exception))

    def test_a_shallow_clone_says_so(self):
        """The CI default: actions/checkout fetches depth 1, so the base
        branch a diff wants is often not in the clone at all. The hint is
        conditional on the clone actually being shallow — a message that
        guesses at the cause sends people after the wrong thing.
        """
        shallow = os.path.join(self.tmp, "shallow")
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        "file://" + self.repo, shallow], check=True, env=ENV,
                       capture_output=True)
        self.assertEqual(git_out(shallow, "rev-parse",
                                 "--is-shallow-repository"), "true")
        with self.assertRaises(RuntimeError) as caught:
            refs.resolve(shallow, self.first)
        self.assertIn("fetch-depth: 0", str(caught.exception))

    def test_a_full_clone_does_not_mention_shallowness(self):
        with self.assertRaises(RuntimeError) as caught:
            refs.resolve(self.repo, "definitely-not-a-ref")
        self.assertNotIn("shallow", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
