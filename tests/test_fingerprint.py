"""Fingerprints and the narratives lock written by `repokg render`."""

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
from repokg.inject import clean

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

FIXTURE = {
    ".gitignore": ".repokg/\n",
    "app/__init__.py": "",
    "app/main.py": "from lib import helper\n",
    "lib/__init__.py": "",
    "lib/helper.py": "def helper(): pass\n",
    "lib/notes.txt": "not code\n",
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
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = main(list(argv))
    return code, buf.getvalue()


class TestFingerprint(unittest.TestCase):
    def test_order_independent_and_content_sensitive(self):
        a = fingerprint.fingerprint({"a.py": "1", "b.py": "2"})
        self.assertEqual(a, fingerprint.fingerprint({"b.py": "2", "a.py": "1"}))
        self.assertNotEqual(a, fingerprint.fingerprint({"a.py": "1", "b.py": "3"}))

    def test_rename_changes_fingerprint(self):
        self.assertNotEqual(fingerprint.fingerprint({"a.py": "1"}),
                            fingerprint.fingerprint({"c.py": "1"}))

    def test_name_and_sha_cannot_be_confused(self):
        self.assertNotEqual(fingerprint.fingerprint({"a": "bc"}),
                            fingerprint.fingerprint({"ab": "c"}))


class LockCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = self.tmp
        self.out = os.path.join(self.repo, ".repokg")
        for rel, text in FIXTURE.items():
            write(self.repo, rel, text)
        git(self.repo, "init", "-q", "-b", "main", ".")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.enrich(NARRATIVES)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def enrich(self, narratives):
        write(self.repo, ".repokg/narratives.json", json.dumps(narratives))

    def generate(self):
        return cli("generate", self.repo, "--no-github", "--no-cache")

    def lock(self):
        with open(os.path.join(self.out, fingerprint.LOCK_NAME)) as f:
            return json.load(f)

    def modules(self):
        return self.lock()["modules"]


class TestLockWritten(LockCase):
    def test_stamps_each_enriched_module(self):
        code, out = self.generate()
        self.assertEqual(code, 0, out)
        mods = self.modules()
        self.assertEqual(sorted(mods), ["app", "lib"])
        self.assertIn("2 modules stamped", out)

    def test_files_are_code_files_with_git_blob_shas(self):
        self.generate()
        files = self.modules()["lib"]["files"]
        self.assertEqual(sorted(files), ["__init__.py", "helper.py"])
        want = subprocess.run(
            ["git", "rev-parse", "HEAD:lib/helper.py"], cwd=self.repo,
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(files["helper.py"], want)
        self.assertEqual(self.modules()["lib"]["fingerprint"],
                         fingerprint.fingerprint(files))

    def test_only_enriched_modules_are_stamped(self):
        self.enrich({"overview": "demo", "modules": {"app": "Entry point."}})
        self.generate()
        self.assertEqual(sorted(self.modules()), ["app"])

    def test_no_lock_without_narratives(self):
        os.remove(os.path.join(self.out, "narratives.json"))
        code, _ = self.generate()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(
            os.path.join(self.out, fingerprint.LOCK_NAME)))

    def test_lock_is_deterministic(self):
        self.generate()
        with open(os.path.join(self.out, fingerprint.LOCK_NAME)) as f:
            first = f.read()
        self.generate()
        with open(os.path.join(self.out, fingerprint.LOCK_NAME)) as f:
            self.assertEqual(f.read(), first)

    def test_uncommitted_edit_changes_fingerprint(self):
        self.generate()
        before = self.modules()["lib"]["fingerprint"]
        write(self.repo, "lib/helper.py", "def helper(): return 1\n")
        self.assertNotEqual(
            fingerprint.snapshot(self.repo, ["lib"])["lib"]["fingerprint"],
            before)

    def test_untouched_module_fingerprint_survives_other_edits(self):
        self.generate()
        before = self.modules()["app"]["fingerprint"]
        write(self.repo, "lib/helper.py", "def helper(): return 1\n")
        self.assertEqual(
            fingerprint.snapshot(self.repo, ["app"])["app"]["fingerprint"],
            before)

    def test_non_code_file_does_not_move_fingerprint(self):
        self.generate()
        before = self.modules()["lib"]["fingerprint"]
        write(self.repo, "lib/notes.txt", "edited prose\n")
        self.assertEqual(
            fingerprint.snapshot(self.repo, ["lib"])["lib"]["fingerprint"],
            before)

    def test_root_module(self):
        write(self.repo, "setup.py", "x = 1\n")
        self.enrich({"modules": {"(root)": "Packaging."}})
        self.generate()
        self.assertEqual(list(self.modules()["(root)"]["files"]), ["setup.py"])

    def test_excluded_files_are_not_fingerprinted(self):
        write(self.repo, "lib/gen.py", "x = 1\n")
        code, _ = cli("generate", self.repo, "--no-github", "--no-cache",
                      "--exclude", "lib/gen.py")
        self.assertEqual(code, 0)
        self.assertNotIn("gen.py", self.modules()["lib"]["files"])


class TestRestamping(LockCase):
    def test_rerender_keeps_stamp_when_code_drifted(self):
        self.generate()
        old = self.modules()["lib"]
        write(self.repo, "lib/helper.py", "def helper(): return 1\n")
        self.generate()
        self.assertEqual(self.modules()["lib"], old)

    def test_rerender_reports_kept_modules(self):
        self.generate()
        _, out = self.generate()
        self.assertIn("0 modules stamped, 2 unchanged", out)

    def test_rewritten_narrative_is_restamped(self):
        self.generate()
        old = self.modules()
        write(self.repo, "lib/helper.py", "def helper(): return 1\n")
        self.enrich({"modules": {"app": "Entry point.",
                                 "lib": "Helpers, now returning one."}})
        self.generate()
        new = self.modules()
        self.assertNotEqual(new["lib"]["fingerprint"], old["lib"]["fingerprint"])
        self.assertEqual(new["app"], old["app"])

    def test_dropped_narrative_leaves_the_lock(self):
        self.generate()
        self.enrich({"modules": {"app": "Entry point."}})
        self.generate()
        self.assertEqual(sorted(self.modules()), ["app"])

    def test_corrupt_lock_is_rewritten_not_fatal(self):
        self.generate()
        write(self.repo, ".repokg/" + fingerprint.LOCK_NAME, "{not json")
        code, out = self.generate()
        self.assertEqual(code, 0, out)
        self.assertIn("unreadable", out)
        self.assertEqual(sorted(self.modules()), ["app", "lib"])

    def test_unknown_lock_version_is_rewritten(self):
        self.generate()
        write(self.repo, ".repokg/" + fingerprint.LOCK_NAME,
              json.dumps({"lockfile_version": 99, "modules": {}}))
        code, out = self.generate()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lock()["lockfile_version"], 1)


class TestCoverageIsSaid(LockCase):
    def test_entry_matching_no_module_is_reported(self):
        self.enrich({"modules": {"app": "Entry.", "docs/guide": "Prose.",
                                 "lib/notes": "Nope."}})
        code, out = self.generate()
        self.assertEqual(code, 0, out)
        self.assertIn("2 narrative entries match no scanned module", out)
        self.assertIn("docs/guide", out)
        self.assertEqual(sorted(self.modules()), ["app"])

    def test_git_failure_degrades_to_a_note(self):
        from repokg import cli as cli_mod
        kg = {"modules": [{"path": "app"}]}
        err = RuntimeError("git hash-object: boom")
        with mock.patch.object(fingerprint.gitinfo, "run", side_effect=err), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            cli_mod._stamp_narratives(self.repo, self.out, kg,
                                      {"modules": {"app": "x"}})
        self.assertIn("not written", buf.getvalue())
        self.assertFalse(os.path.exists(
            os.path.join(self.out, fingerprint.LOCK_NAME)))

    def test_render_still_succeeds_when_stamping_fails(self):
        self.generate()
        err = RuntimeError("boom")
        with mock.patch.object(fingerprint.gitinfo, "run", side_effect=err):
            code, out = cli("render", self.repo)
        self.assertEqual(code, 0, out)
        self.assertIn("not written", out)


class TestReversible(LockCase):
    def test_clean_removes_the_lock(self):
        self.generate()
        lock = os.path.join(self.out, fingerprint.LOCK_NAME)
        self.assertTrue(os.path.exists(lock))
        clean(self.repo, self.out, os.path.join(self.repo, "KNOWLEDGE_GRAPH.md"))
        self.assertFalse(os.path.exists(lock))


class TestFilenames(LockCase):
    def test_path_with_newline_is_hashed_alone(self):
        name = "odd\nname.py"
        try:
            write(self.repo, "lib/" + name, "x = 1\n")
        except OSError:
            self.skipTest("filesystem rejects newlines in names")
        shas = fingerprint.blob_shas(self.repo, ["lib/helper.py", "lib/" + name])
        self.assertEqual(len(set(shas.values())), 2)
        self.assertEqual(len(shas), 2)


if __name__ == "__main__":
    unittest.main()
