"""`repokg check` judging narratives against the lock, and `--restamp`."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from repokg import fingerprint
from repokg.cli import main

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

FIXTURE = {
    ".gitignore": ".repokg/\n",
    "app/__init__.py": "",
    "app/main.py": "from lib import helper\n",
    "lib/__init__.py": "",
    "lib/a.py": "A = 1\n",
    "lib/b.py": "B = 1\n",
    "lib/c.py": "C = 1\n",
    "lib/d.py": "D = 1\n",
}

NARRATIVES = {"overview": "demo", "modules": {
    "app": "Entry point.", "lib": "Helpers."}}


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, env=ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CheckCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.out = os.path.join(self.repo, ".repokg")
        for rel, text in FIXTURE.items():
            write(self.repo, rel, text)
        git(self.repo, "init", "-q", "-b", "main", ".")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.enrich(NARRATIVES)
        self.generate()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def enrich(self, narratives):
        write(self.repo, ".repokg/narratives.json", json.dumps(narratives))

    def generate(self, *extra):
        rc, out, err = cli("generate", self.repo, "--no-github", "--no-cache",
                           *extra)
        self.assertEqual(rc, 0, out + err)
        return out

    def check(self, *extra):
        return cli("check", self.repo, *extra)

    def lock(self):
        with open(os.path.join(self.out, fingerprint.LOCK_NAME)) as f:
            return json.load(f)["modules"]

    def edit(self, rel="lib/a.py", text="A = 2\n"):
        write(self.repo, rel, text)


class TestCheckNarratives(CheckCase):
    def test_nothing_changed_is_fresh(self):
        rc, out, _ = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn("fresh", out)
        self.assertIn("2 narratives current", out)

    def test_edited_module_is_flagged_and_untouched_stays_green(self):
        self.edit()
        rc, out, _ = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("narrative stale: lib (1 of 5 files changed since "
                      "enrichment)", out)
        self.assertNotIn("narrative stale: app", out)
        self.assertNotIn("fresh", out)

    def test_names_the_way_out(self):
        self.edit()
        _, out, _ = self.check()
        self.assertIn("--restamp", out)

    def test_added_file_counts_as_changed(self):
        self.edit("lib/e.py", "E = 1\n")
        rc, out, _ = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("lib (1 of 6 files", out)

    def test_removed_file_counts_as_changed(self):
        os.remove(os.path.join(self.repo, "lib/d.py"))
        rc, out, _ = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("lib (1 of 5 files", out)

    def test_module_with_no_code_left_is_fully_changed(self):
        shutil.rmtree(os.path.join(self.repo, "app"))
        rc, out, _ = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("narrative stale: app (2 of 2 files", out)

    def test_non_code_edit_is_not_drift(self):
        self.edit("lib/notes.txt", "prose\n")
        self.assertEqual(self.check()[0], 0)

    def test_committed_edit_is_flagged_too(self):
        self.edit()
        git(self.repo, "commit", "-qam", "edit")
        rc, out, _ = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("narrative stale: lib", out)
        self.assertIn("stale: knowledge graph at", out)

    def test_graph_staleness_is_still_reported_alone(self):
        self.edit("README.md", "docs\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "docs only")
        rc, out, _ = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("stale: knowledge graph at", out)
        self.assertNotIn("narrative stale", out)

    def test_survives_rerender(self):
        self.edit()
        self.generate()
        rc, out, _ = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("narrative stale: lib", out)


class TestThreshold(CheckCase):
    def test_below_threshold_is_not_stale(self):
        self.edit()  # 1 of 5 files = 20%
        self.assertEqual(self.check("--stale-threshold", "25")[0], 0)

    def test_above_threshold_is_stale(self):
        self.edit()
        self.edit("lib/b.py", "B = 2\n")  # 2 of 5 = 40%
        rc, out, _ = self.check("--stale-threshold", "25")
        self.assertEqual(rc, 1)
        self.assertIn("2 of 5 files", out)

    def test_threshold_is_strictly_greater_than(self):
        self.edit()  # exactly 20%
        self.assertEqual(self.check("--stale-threshold", "20")[0], 0)
        self.assertEqual(self.check("--stale-threshold", "19.9")[0], 1)

    def test_out_of_range_is_a_usage_error(self):
        for bad in ("-1", "101"):
            rc, _, err = self.check("--stale-threshold", bad)
            self.assertEqual(rc, 2)
            self.assertIn("between 0 and 100", err)


class TestNothingToJudge(CheckCase):
    def test_narratives_without_a_lock_are_said_not_passed(self):
        os.remove(os.path.join(self.out, fingerprint.LOCK_NAME))
        rc, out, _ = self.check()
        self.assertEqual(rc, 0)
        self.assertIn("narratives.lock.json does not", out)

    def test_structure_only_repo_is_unchanged(self):
        os.remove(os.path.join(self.out, fingerprint.LOCK_NAME))
        os.remove(os.path.join(self.out, "narratives.json"))
        rc, out, _ = self.check()
        self.assertEqual(rc, 0)
        self.assertNotIn("note", out)
        self.assertTrue(out.startswith("fresh: knowledge graph matches HEAD"))

    def test_unreadable_lock_is_a_note_not_a_crash(self):
        write(self.repo, ".repokg/" + fingerprint.LOCK_NAME, "{nope")
        rc, out, _ = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn("unreadable", out)

    def test_git_failure_is_said_and_does_not_fail_the_check(self):
        err = RuntimeError("boom")
        with mock.patch.object(fingerprint.gitinfo, "run", side_effect=err):
            rc, out, _ = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn("narrative staleness not checked", out)


class TestRestamp(CheckCase):
    def test_restamp_clears_one_module_and_leaves_the_rest(self):
        self.edit()
        self.edit("app/main.py", "x = 1\n")
        self.generate("--restamp", "lib")
        rc, out, _ = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("narrative stale: app", out)
        self.assertNotIn("narrative stale: lib", out)

    def test_restamp_all_flagged_goes_green(self):
        self.edit()
        out = self.generate("--restamp", "lib", "--restamp", "app")
        self.assertIn("2 restamped", out)
        self.assertEqual(self.check()[0], 0)

    def test_restamp_leaves_the_narrative_alone(self):
        self.edit()
        self.generate("--restamp", "lib")
        with open(os.path.join(self.out, "narratives.json")) as f:
            self.assertEqual(json.load(f), NARRATIVES)

    def test_restamp_tracks_later_drift(self):
        self.edit()
        self.generate("--restamp", "lib")
        self.edit("lib/b.py", "B = 2\n")
        rc, out, _ = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("lib (1 of 5 files", out)

    def test_restamp_works_with_render_alone(self):
        self.edit()
        rc, out, err = cli("render", self.repo, "--restamp", "lib")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.check()[0], 0)

    def test_unknown_module_is_an_error_and_writes_nothing(self):
        self.edit()
        before = self.lock()
        md = os.path.join(self.repo, "KNOWLEDGE_GRAPH.md")
        mtime = os.path.getmtime(md)
        rc, _, err = cli("render", self.repo, "--restamp", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("cannot restamp nope", err)
        self.assertEqual(self.lock(), before)
        self.assertEqual(os.path.getmtime(md), mtime)

    def test_restamp_a_module_that_was_never_stamped_is_an_error(self):
        rc, _, err = cli("render", self.repo, "--restamp", "other")
        self.assertEqual(rc, 1)
        self.assertIn("other", err)


class TestAuditFinding(CheckCase):
    def audit(self, *extra):
        return cli("audit", self.repo, "--json", *extra)

    def test_stale_narrative_is_a_finding_with_evidence(self):
        self.edit()
        rc, out, _ = self.audit()
        self.assertEqual(rc, 0)
        found = [f for f in json.loads(out)["findings"]
                 if f["kind"] == "narrative"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["subject"], "lib: narrative may be stale")
        self.assertEqual(found[0]["evidence"], ["a.py"])
        self.assertEqual(found[0]["confidence"], "medium")

    def test_fresh_narratives_add_no_finding(self):
        _, out, _ = self.audit()
        self.assertEqual([f for f in json.loads(out)["findings"]
                          if f["kind"] == "narrative"], [])

    def test_text_report_includes_it(self):
        self.edit()
        _, out, _ = cli("audit", self.repo)
        self.assertIn("[narrative]", out)

    def test_threshold_applies_to_audit(self):
        self.edit()
        _, out, _ = self.audit("--stale-threshold", "50")
        self.assertEqual([f for f in json.loads(out)["findings"]
                          if f["kind"] == "narrative"], [])

    def test_scan_output_does_not_contain_it(self):
        self.edit()
        self.generate()
        with open(os.path.join(self.out, "kg.json")) as f:
            kinds = {x["kind"] for x in json.load(f)["findings"]}
        self.assertNotIn("narrative", kinds)


class TestDrift(unittest.TestCase):
    def test_describe_singular(self):
        self.assertEqual(
            fingerprint.describe({"module": "m", "changed": ["a"], "total": 1}),
            "narrative stale: m (1 of 1 file changed since enrichment)")

    def test_stamp_rejects_unknown_restamp(self):
        with self.assertRaises(ValueError):
            fingerprint.stamp("/nonexistent", "/nonexistent", {},
                              {"modules": {"a": "x"}}, restamp=["b"])


if __name__ == "__main__":
    unittest.main()
