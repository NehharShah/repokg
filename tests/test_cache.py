import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest

from repokg import cache
from repokg.cli import main
from repokg.inject import clean

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, env=ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class CacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        write(self.repo, ".gitignore", "gen/\n.repokg/\n")
        write(self.repo, "pkg/__init__.py", "")
        write(self.repo, "pkg/a.py", "import os\n")
        write(self.repo, "pkg/b.py", "from pkg import a\n")
        write(self.repo, "web/index.ts", "import {x} from './util';\n")
        write(self.repo, "web/util.ts", "export const x = 1;\n")
        git(self.repo, "init", "-q", "-b", "main", ".")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, *extra):
        """Run a scan, returning its cache report line."""
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = main(["scan", self.repo, "--no-github"] + list(extra))
        self.assertEqual(rc, 0, buf.getvalue())
        lines = [ln for ln in buf.getvalue().splitlines()
                 if ln.startswith("cache:")]
        self.assertEqual(len(lines), 1, buf.getvalue())
        return lines[0]

    def kg(self):
        with open(os.path.join(self.repo, ".repokg", "kg.json"),
                  encoding="utf-8") as f:
            return f.read()

    def doc(self):
        with open(os.path.join(self.repo, ".repokg", cache.CACHE_FILE),
                  encoding="utf-8") as f:
            return json.load(f)

    def write_doc(self, doc):
        with open(os.path.join(self.repo, ".repokg", cache.CACHE_FILE),
                  "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def backdate(self, rel, seconds=3600):
        """Change a file's mtime without touching its content."""
        path = os.path.join(self.repo, rel)
        t = time.time() - seconds
        os.utime(path, (t, t))


class TestWarmScan(CacheCase):
    def test_first_scan_is_cold_and_seeds_the_cache(self):
        self.assertIn("cold (no cache yet)", self.scan())
        self.assertTrue(self.doc()["files"])

    def test_untouched_repo_parses_nothing(self):
        self.scan()
        self.assertIn("parsed 0", self.scan())

    def test_warm_output_matches_cold_output(self):
        self.scan()
        cold = self.kg()
        self.scan("--no-cache")
        self.assertEqual(self.kg(), cold)
        warm = self.scan()
        self.assertIn("parsed 0", warm)
        self.assertEqual(self.kg(), cold)

    def test_document_records_identity_and_facts(self):
        self.scan()
        entry = self.doc()["files"]["pkg/a.py"]
        self.assertEqual(entry["facts"],
                         {"lang": "Python", "loc": 1,
                          "py_imports": [["os", 0]]})
        self.assertEqual(entry["size"], len("import os\n"))
        self.assertIsInstance(entry["mtime_ns"], int)
        self.assertEqual(len(entry["blob"]), 40)

    def test_head_is_stored_for_the_next_diff(self):
        self.scan()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(self.doc()["head"], head)


class TestInvalidation(CacheCase):
    def assert_warm_matches_cold(self, expect_parsed):
        line = self.scan()
        warm = self.kg()
        self.assertIn("parsed %d" % expect_parsed, line)
        self.scan("--no-cache")
        self.assertEqual(warm, self.kg(), line)

    def test_dirty_tracked_file_is_reparsed(self):
        self.scan()
        write(self.repo, "pkg/a.py", "import os\nfrom pkg import b\n")
        self.assert_warm_matches_cold(1)

    def test_new_untracked_file_is_parsed(self):
        self.scan()
        write(self.repo, "pkg/c.py", "from pkg import a\n")
        self.assert_warm_matches_cold(1)

    def test_committed_change_is_reparsed(self):
        self.scan()
        write(self.repo, "pkg/a.py", "from pkg import b\n")
        git(self.repo, "commit", "-qam", "change")
        self.assert_warm_matches_cold(1)

    def test_rename_reparses_both_sides(self):
        self.scan()
        git(self.repo, "mv", "pkg/a.py", "pkg/moved.py")
        git(self.repo, "commit", "-qm", "move")
        self.assert_warm_matches_cold(1)  # old path is gone, new one is parsed

    def test_deleted_file_leaves_the_document(self):
        self.scan()
        os.remove(os.path.join(self.repo, "pkg/a.py"))
        git(self.repo, "commit", "-qam", "delete")
        self.assert_warm_matches_cold(0)
        self.assertNotIn("pkg/a.py", self.doc()["files"])

    def test_gitignored_file_is_caught_by_the_stat_gate(self):
        """repokg walks paths git ignores, so git alone cannot invalidate."""
        write(self.repo, "gen/x.py", "import os\n")
        self.scan()
        self.assertIn("gen/x.py", self.doc()["files"])
        write(self.repo, "gen/x.py", "import os\nimport sys\nimport json\n")
        self.assert_warm_matches_cold(1)
        self.assertEqual(self.doc()["files"]["gen/x.py"]["facts"]["loc"], 3)

    def test_excluded_files_drop_out_and_return(self):
        """An excluded file is never asked for, so it leaves the cache and
        costs one parse when the exclusion is lifted."""
        self.scan()
        self.scan("--exclude", "web")
        self.assertEqual([p for p in self.doc()["files"] if p.startswith("web/")],
                         [])
        self.assertIn("parsed 2", self.scan())  # both web/*.ts come back


class TestBlobGate(CacheCase):
    def test_tracked_file_with_new_mtime_but_same_content_is_replayed(self):
        self.scan()
        self.backdate("pkg/a.py")
        self.assertIn("parsed 0", self.scan())

    def test_stat_gate_alone_would_have_reparsed_it(self):
        """Guards the blob gate itself: with no blob recorded, the same file
        falls through to size/mtime and is re-parsed."""
        self.scan()
        doc = self.doc()
        doc["files"]["pkg/a.py"]["blob"] = None
        self.write_doc(doc)
        self.backdate("pkg/a.py")
        self.assertIn("parsed 1", self.scan())

    def test_untracked_file_with_new_mtime_is_reparsed(self):
        write(self.repo, "gen/x.py", "import os\n")
        self.scan()
        self.backdate("gen/x.py")
        self.assertIn("parsed 1", self.scan())


class TestDegradation(CacheCase):
    def test_truncated_cache_degrades_quietly(self):
        self.scan()
        with open(os.path.join(self.repo, ".repokg", cache.CACHE_FILE),
                  "w", encoding="utf-8") as f:
            f.write('{"files": {"a.py": tru')
        self.assertIn("cold (cache unreadable)", self.scan())

    def test_wrong_shape_degrades(self):
        self.scan()
        self.write_doc({"cache_version": cache.CACHE_VERSION, "files": []})
        self.assertIn("cold (cache malformed)", self.scan())

    def test_stale_facts_version_degrades(self):
        self.scan()
        doc = self.doc()
        doc["facts_version"] = doc["facts_version"] + 1
        self.write_doc(doc)
        self.assertIn("different repokg", self.scan())

    def test_stale_cache_version_degrades(self):
        self.scan()
        doc = self.doc()
        doc["cache_version"] = doc["cache_version"] + 1
        self.write_doc(doc)
        self.assertIn("different repokg", self.scan())

    def test_unknown_cached_head_degrades(self):
        self.scan()
        doc = self.doc()
        doc["head"] = "0" * 40
        self.write_doc(doc)
        self.assertIn("git cannot bound what changed", self.scan())

    def test_degraded_scan_reseeds_a_usable_cache(self):
        self.scan()
        self.write_doc({"nonsense": True})
        self.scan()
        self.assertIn("parsed 0", self.scan())

    def test_non_git_directory_still_scans(self):
        plain = tempfile.mkdtemp()
        try:
            write(plain, "a.py", "import os\n")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main(["scan", plain, "--no-github"])
            self.assertEqual(rc, 1)  # gitinfo needs a repo; cache stays out of it
        finally:
            import shutil
            shutil.rmtree(plain)

    def test_unwritable_out_dir_does_not_fail_the_scan(self):
        self.scan()
        out = os.path.join(self.repo, ".repokg")
        path = os.path.join(out, cache.CACHE_FILE)
        os.remove(path)
        os.mkdir(path)  # a directory where the document should go
        try:
            self.assertIn("cache:", self.scan())
        finally:
            os.rmdir(path)


class TestNoCacheFlag(CacheCase):
    def test_no_cache_parses_everything(self):
        self.scan()
        self.assertIn("disabled (--no-cache)", self.scan("--no-cache"))

    def test_no_cache_leaves_the_document_alone(self):
        self.scan()
        before = self.doc()
        self.scan("--no-cache")
        self.assertEqual(self.doc(), before)

    def test_generate_honours_no_cache(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = main(["generate", self.repo, "--no-github", "--no-cache"])
        self.assertEqual(rc, 0)
        self.assertIn("disabled (--no-cache)", buf.getvalue())


class TestReversibility(CacheCase):
    def test_clean_removes_the_cache(self):
        self.scan()
        out = os.path.join(self.repo, ".repokg")
        self.assertTrue(os.path.isfile(os.path.join(out, cache.CACHE_FILE)))
        clean(self.repo, out, os.path.join(self.repo, "KNOWLEDGE_GRAPH.md"))
        self.assertFalse(os.path.exists(out))

    def test_no_temporary_file_is_left_behind(self):
        self.scan()
        out = os.path.join(self.repo, ".repokg")
        self.assertEqual([f for f in os.listdir(out) if f.endswith(".tmp")], [])


class TestGitParsing(unittest.TestCase):
    def test_diff_name_status(self):
        self.assertEqual(
            cache._parse_diff("M\0a.py\0A\0b/c.ts\0D\0old.go\0"),
            {"a.py", "b/c.ts", "old.go"})

    def test_diff_rename_counts_both_sides(self):
        self.assertEqual(
            cache._parse_diff("R100\0from.py\0to.py\0M\0x.py\0"),
            {"from.py", "to.py", "x.py"})

    def test_diff_copy_counts_both_sides(self):
        self.assertEqual(cache._parse_diff("C75\0src.py\0copy.py\0"),
                         {"src.py", "copy.py"})

    def test_status_entries(self):
        self.assertEqual(
            cache._parse_status(" M a.py\0?? new.ts\0A  staged.go\0"),
            {"a.py", "new.ts", "staged.go"})

    def test_status_rename_pulls_in_the_following_path(self):
        self.assertEqual(cache._parse_status("R  new.py\0old.py\0 M x.py\0"),
                         {"new.py", "old.py", "x.py"})

    def test_status_path_with_spaces(self):
        self.assertEqual(cache._parse_status("?? a dir/my file.py\0"),
                         {"a dir/my file.py"})

    def test_empty_output(self):
        self.assertEqual(cache._parse_status(""), set())
        self.assertEqual(cache._parse_diff(""), set())


if __name__ == "__main__":
    unittest.main()
