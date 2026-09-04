"""Tests for the `repokg diff` command and its two reports.

The engine's own invariants live in test_diff.py. What matters here is the
wiring: that the exit code says what it claims, that a diff never writes over
the baseline it is comparing against, and that stdout carries only the report
so `--format json` can be piped.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from repokg import diff
from repokg.cli import main

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

FIXTURE = {
    ".gitignore": ".repokg/\n",
    "app/__init__.py": "",
    "app/main.py": "from lib import helper\n",
    "lib/__init__.py": "",
    "lib/helper.py": "def helper(): pass\n",
}


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, env=ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def copy_repo(src, dst):
    """Copy a git repo, skipping git's transient lock files — background
    maintenance can drop a .lock between copytree listing a directory and
    reading it, which fails the whole copy."""
    shutil.copytree(src, dst, ignore=lambda d, names: [
        n for n in names if n.endswith(".lock")])


class DiffCase(unittest.TestCase):
    """One git repo built per class and copied per test."""

    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.mkdtemp()
        for rel, text in FIXTURE.items():
            write(cls.template, rel, text)
        git(cls.template, "init", "-q", "-b", "main", ".")
        git(cls.template, "config", "maintenance.auto", "false")
        git(cls.template, "config", "gc.auto", "0")
        git(cls.template, "add", "-A")
        git(cls.template, "commit", "-qm", "base")

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

    def seed(self):
        rc, _, _ = self.run_cli("scan", self.repo, "--no-github")
        self.assertEqual(rc, 0)

    def diff(self, *extra):
        return self.run_cli("diff", self.repo, "--no-github", *extra)

    def baseline_modules(self):
        with open(os.path.join(self.out, "kg.json"), encoding="utf-8") as f:
            return sorted(m["path"] for m in json.load(f)["modules"])


class TestExitCodes(DiffCase):
    """0 = shape unchanged, 1 = shape changed, 2 = error.

    The convention `diff(1)` and `git diff --exit-code` use, and the one
    `repokg check` already follows. Errors are 2 and not 1 so a CI job cannot
    read a mistyped path as an architectural change.
    """

    def test_unchanged_repo_exits_zero(self):
        self.seed()
        rc, stdout, _ = self.diff()
        self.assertEqual(rc, 0)
        self.assertIn("no change", stdout)

    def test_a_new_module_and_edge_exits_one(self):
        self.seed()
        write(self.repo, "billing/__init__.py", "")
        write(self.repo, "billing/api.py", "def charge(): pass\n")
        write(self.repo, "app/main.py",
              "from lib import helper\nfrom billing import api\n")
        rc, stdout, _ = self.diff()
        self.assertEqual(rc, 1)
        self.assertIn("+ billing", stdout)
        self.assertIn("app -> billing", stdout)
        self.assertIn("shape changed", stdout)

    def test_loc_drift_alone_exits_zero(self):
        """Reported, but not a shape change. LOC moves on every commit, so a
        gate keyed on it would be switched off within a week."""
        self.seed()
        write(self.repo, "lib/helper.py",
              "def helper(): pass\n# pad\n# pad\n# pad\n")
        rc, stdout, _ = self.diff()
        self.assertEqual(rc, 0)
        self.assertIn("loc 1 -> 4", stdout)
        self.assertIn("measurements moved but the shape did not", stdout)

    def test_a_missing_baseline_exits_two(self):
        rc, _, stderr = self.diff()
        self.assertEqual(rc, 2)
        self.assertIn("no baseline graph", stderr)
        self.assertIn("repokg scan", stderr)

    def test_a_missing_explicit_graph_exits_two(self):
        self.seed()
        rc, _, stderr = self.diff("--to", os.path.join(self.tmp, "nope.json"))
        self.assertEqual(rc, 2)
        self.assertIn("no comparison graph", stderr)
        # the hint to run a scan belongs to the default baseline only
        self.assertNotIn("repokg scan", stderr)

    def test_an_unparseable_graph_exits_two(self):
        self.seed()
        bad = os.path.join(self.tmp, "bad.json")
        write(self.tmp, "bad.json", "{ not json")
        rc, _, stderr = self.diff("--from", bad)
        self.assertEqual(rc, 2)
        self.assertIn("not a readable knowledge graph", stderr)

    def test_a_json_document_that_is_not_a_graph_exits_two(self):
        self.seed()
        write(self.tmp, "list.json", "[1, 2, 3]")
        rc, _, stderr = self.diff("--from", os.path.join(self.tmp,
                                                         "list.json"))
        self.assertEqual(rc, 2)
        self.assertIn("not a knowledge graph document", stderr)


class TestNonDestructive(DiffCase):
    def test_diff_does_not_write_over_the_baseline(self):
        """The document in <out> *is* the baseline. A diff that scanned over
        it would answer once and then have nothing left to compare against."""
        self.seed()
        before = self.baseline_modules()
        write(self.repo, "billing/__init__.py", "")
        write(self.repo, "billing/api.py", "def charge(): pass\n")
        rc, _, _ = self.diff()
        self.assertEqual(rc, 1)
        self.assertEqual(self.baseline_modules(), before)
        self.assertNotIn("billing", self.baseline_modules())

    def test_repeated_diffs_report_the_same_thing(self):
        """Follows from the above, and is the property a user would notice
        breaking: the baseline must not drift under repeated queries."""
        self.seed()
        write(self.repo, "billing/__init__.py", "")
        write(self.repo, "billing/api.py", "def charge(): pass\n")
        first = self.diff("--format", "json")
        second = self.diff("--format", "json")
        self.assertEqual(first[0], 1)
        self.assertEqual(json.loads(first[1]), json.loads(second[1]))

    def test_diff_writes_no_new_artifact(self):
        """Nothing to teach `repokg clean` about, which is the reversibility
        rule met by writing nothing in the first place."""
        self.seed()
        before = sorted(os.listdir(self.out))
        self.diff()
        self.assertEqual(sorted(os.listdir(self.out)), before)

    def test_two_explicit_graphs_do_not_scan_at_all(self):
        self.seed()
        old = os.path.join(self.tmp, "old.json")
        shutil.copy(os.path.join(self.out, "kg.json"), old)
        rc, _, stderr = self.diff("--from", old, "--to", old)
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, "")  # a scan would have reported its cache


class TestStreams(DiffCase):
    def test_json_output_is_alone_on_stdout(self):
        """A scan's progress lines would corrupt `repokg diff --format json >
        delta.json`, so they go to stderr — silenced nowhere, since coverage
        loss has to stay visible."""
        self.seed()
        rc, stdout, stderr = self.diff("--format", "json")
        self.assertEqual(rc, 0)
        json.loads(stdout)
        self.assertIn("cache:", stderr)

    def test_exclusions_are_still_reported_when_scanning_for_a_diff(self):
        self.seed()
        rc, stdout, stderr = self.diff("--exclude", "lib", "--format", "json")
        self.assertIn("excluded", stderr)
        json.loads(stdout)

    def test_json_flag_is_shorthand_for_format_json(self):
        self.seed()
        _, with_flag, _ = self.diff("--json")
        _, with_format, _ = self.diff("--format", "json")
        self.assertEqual(json.loads(with_flag), json.loads(with_format))

    def test_format_wins_over_the_json_flag(self):
        self.seed()
        _, stdout, _ = self.diff("--json", "--format", "text")
        self.assertIn("no change", stdout)


class TestReports(unittest.TestCase):
    """Rendering, driven off a delta rather than a repo."""

    def delta(self):
        old = {"repokg_version": 1, "generated_at": "2026-01-01",
               "repo": {"head": "a" * 40},
               "modules": [{"path": "app", "lang": "Python", "files": 1,
                            "loc": 10},
                           {"path": "old", "lang": "Go", "files": 2,
                            "loc": 20}],
               "edges": [], "languages": [], "branches": [], "prs": [],
               "ops": {"workflows": []}}
        new = {"repokg_version": 1, "generated_at": "2026-06-01",
               "repo": {"head": "b" * 40},
               "modules": [{"path": "app", "lang": "Python", "files": 1,
                            "loc": 99}],
               "edges": [{"from": "app", "to": "lib", "lang": "Python",
                          "count": 3}],
               "languages": [], "branches": [], "prs": [],
               "ops": {"workflows": [{"file": ".github/workflows/ci.yml",
                                      "name": "CI"}]}}
        return diff.build(old, new)

    def test_text_report_names_both_graphs_and_the_verdict(self):
        text = diff.render_text(self.delta())
        self.assertIn("comparing aaaaaaaaaaaa (2026-01-01) -> "
                      "bbbbbbbbbbbb (2026-06-01)", text)
        self.assertIn("shape changed: modules, edges, ops (exit 1)", text)

    def test_text_report_marks_additions_removals_and_movement(self):
        text = diff.render_text(self.delta())
        self.assertIn("+ app -> lib (Python)", text)
        self.assertIn("- old", text)
        self.assertIn("~ app", text)
        self.assertIn("loc 10 -> 99", text)

    def test_a_shared_head_is_stated_rather_than_warned_about(self):
        """A stored graph against a fresh scan of an uncommitted tree is the
        ordinary case, not a problem."""
        same = {"repo": {"head": "c" * 40}, "modules": []}
        text = diff.render_text(diff.build(same, same))
        self.assertIn("two graphs of cccccccccccc (the same commit)", text)
        self.assertIn("no change (exit 0)", text)

    def test_markdown_report_is_paste_ready(self):
        md = diff.render_markdown(self.delta())
        self.assertTrue(md.startswith("## Knowledge graph diff"))
        self.assertIn("### modules -1 ~1", md)  # zero counts are not printed
        self.assertIn("- **+** `app -> lib (Python)` — 3 imports", md)
        self.assertIn("**shape changed", md)
        self.assertTrue(md.endswith("\n"))

    def test_notes_are_carried_into_both_reports(self):
        """A skipped section is the one thing a reader must not miss."""
        d = diff.build({"modules": []}, {})
        self.assertTrue(d["notes"])
        self.assertIn("Notes:", diff.render_text(d))
        self.assertIn("> ", diff.render_markdown(d))

    def test_long_sections_are_capped_and_say_so(self):
        """Moving a top-level directory relocates every module under it; a
        thousand-line wall is not a report, and a silent truncation is worse."""
        many = [{"path": "m%03d" % i, "lang": "Python", "files": 1, "loc": 1}
                for i in range(diff.MAX_ROWS + 25)]
        d = diff.build({"modules": many}, {"modules": []})
        text = diff.render_text(d)
        self.assertIn("... and 25 more", text)
        self.assertEqual(text.count("  - m"), diff.MAX_ROWS)
        self.assertIn("… and 25 more", diff.render_markdown(d))

    def test_json_output_is_never_capped(self):
        many = [{"path": "m%03d" % i, "lang": "Python", "files": 1, "loc": 1}
                for i in range(diff.MAX_ROWS + 25)]
        d = diff.build({"modules": many}, {"modules": []})
        self.assertEqual(len(d["modules"]["removed"]), diff.MAX_ROWS + 25)

    def test_a_config_dir_listing_is_cut_short_in_the_report(self):
        """`entries` runs to thirty names and would swamp the line."""
        entries = ["f%02d.yaml" % i for i in range(30)]
        d = diff.build({"ops": {"config_dirs": [{"dir": "configs",
                                                 "entries": []}]}},
                       {"ops": {"config_dirs": [{"dir": "configs",
                                                 "entries": entries}]}})
        text = diff.render_text(d)
        self.assertIn("+25 more", text)
        self.assertNotIn("f29.yaml", text)


class TestCheckStillWorks(DiffCase):
    """`diff` split `scan` into assemble-then-write; the commands built on the
    written document must be unaffected."""

    def test_scan_still_writes_the_document_and_reports_it(self):
        rc, stdout, _ = self.run_cli("scan", self.repo, "--no-github")
        self.assertEqual(rc, 0)
        self.assertIn("wrote", stdout)
        self.assertIn("2 modules", stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "kg.json")))

    def test_generate_and_check_agree_after_a_diff(self):
        rc, _, _ = self.run_cli("generate", self.repo, "--no-github")
        self.assertEqual(rc, 0)
        self.assertEqual(self.diff()[0], 0)
        rc, stdout, _ = self.run_cli("check", self.repo)
        self.assertEqual(rc, 0, stdout)
        self.assertIn("fresh", stdout)


if __name__ == "__main__":
    unittest.main()
