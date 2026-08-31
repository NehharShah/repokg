import collections
import contextlib
import hashlib
import inspect
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from repokg import cache, code, deps, facts, gitinfo
from repokg.cli import main
from repokg.facts import FACTS_VERSION
from repokg.inject import clean

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

FIXTURE = {
    ".gitignore": "gen/\n.repokg/\n",
    "pkg/__init__.py": "",
    "pkg/a.py": "import os\n",
    "pkg/b.py": "from pkg import a\n",
    "web/index.ts": "import {x} from './util';\n",
    "web/util.ts": "export const x = 1;\n",
}

Result = collections.namedtuple("Result", "note parses hits graph")


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, env=ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def copy_repo(src, dst):
    """Copy a git repo, skipping git's transient lock files.

    Background maintenance can create and remove .git/objects/*.lock while the
    copy is in flight, and a name that disappears between copytree listing a
    directory and reading it fails the whole copy.
    """
    shutil.copytree(src, dst, ignore=lambda d, names: [
        n for n in names if n.endswith(".lock")])


class CacheCase(unittest.TestCase):
    """One git repo built per class and copied per test.

    `scan` drives the cache layer directly rather than the CLI. Everything the
    cache can influence is here — languages, modules, edges all come through
    the store — while branches, PRs and the ops surface do not touch it, and
    collecting them costs a dozen git subprocesses per scan. The CLI's own
    wiring is covered separately in TestCliReporting.
    """

    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.mkdtemp()
        for rel, text in FIXTURE.items():
            write(cls.template, rel, text)
        git(cls.template, "init", "-q", "-b", "main", ".")
        # no background repacking, so the template stops changing under the
        # per-test copies once it is built
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

    def scan(self, no_cache=False, exclude=(), store_cls=facts.Store):
        head = gitinfo.try_run(self.repo, "rev-parse", "HEAD")
        c, note = cache.open_(self.repo, self.out, head, not no_cache)
        store = store_cls(self.repo, c)
        tree = dict(code.walk(self.repo, exclude))
        languages, modules = code.collect(self.repo, tree, store)
        edges = deps.collect(self.repo, tree, None, store)
        if c is not None:
            c.save(self.out, head)
        return Result(note, store.parses, c.hits if c else 0,
                      (languages, modules, edges))

    def doc(self):
        with open(os.path.join(self.out, cache.CACHE_FILE),
                  encoding="utf-8") as f:
            return json.load(f)

    def write_doc(self, doc):
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, cache.CACHE_FILE), "w",
                  encoding="utf-8") as f:
            json.dump(doc, f)

    def backdate(self, rel, seconds=3600):
        """Change a file's mtime without touching its content."""
        t = time.time() - seconds
        os.utime(os.path.join(self.repo, rel), (t, t))

    def assert_warm_matches_cold(self, expect_parsed, label=""):
        warm = self.scan()
        cold = self.scan(no_cache=True)
        self.assertEqual(warm.graph, cold.graph, label)
        self.assertEqual(warm.parses, expect_parsed, label)


class TestWarmScan(CacheCase):
    def test_first_scan_is_cold_and_seeds_the_cache(self):
        r = self.scan()
        self.assertEqual(r.note, "no cache yet")
        self.assertTrue(self.doc()["files"])

    def test_untouched_repo_parses_nothing(self):
        self.scan()
        r = self.scan()
        self.assertEqual(r.note, "warm")
        self.assertEqual(r.parses, 0)
        self.assertEqual(r.hits, len(self.doc()["files"]))

    def test_warm_graph_matches_cold_graph(self):
        self.scan()
        self.assert_warm_matches_cold(0)

    def test_document_records_identity_and_facts(self):
        self.scan()
        entry = self.doc()["files"]["pkg/a.py"]
        self.assertEqual(entry["facts"],
                         {"lang": "Python", "loc": 1,
                          "py_imports": [["os", 0]]})
        self.assertEqual(entry["size"], len("import os\n"))
        self.assertIsInstance(entry["mtime_ns"], int)

    def test_head_is_stored_for_the_next_diff(self):
        self.scan()
        self.assertEqual(self.doc()["head"],
                         gitinfo.try_run(self.repo, "rev-parse", "HEAD"))

    def test_unchanged_document_is_not_rewritten(self):
        """A fully warm scan re-serialising the whole cache costs more than
        every replay in it, so it is skipped when nothing moved."""
        self.scan()
        path = os.path.join(self.out, cache.CACHE_FILE)
        before = os.stat(path).st_mtime_ns
        self.scan()
        self.assertEqual(os.stat(path).st_mtime_ns, before)
        write(self.repo, "pkg/c.py", "import os\n")
        self.scan()
        self.assertNotEqual(os.stat(path).st_mtime_ns, before)


class TestInvalidation(CacheCase):
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

    def test_rename_reparses_the_new_path(self):
        self.scan()
        git(self.repo, "mv", "pkg/a.py", "pkg/moved.py")
        git(self.repo, "commit", "-qm", "move")
        self.assert_warm_matches_cold(1)

    def test_deleted_file_leaves_the_document(self):
        self.scan()
        os.remove(os.path.join(self.repo, "pkg/a.py"))
        git(self.repo, "commit", "-qam", "delete")
        self.assert_warm_matches_cold(0)
        self.assertNotIn("pkg/a.py", self.doc()["files"])

    def test_mtime_change_alone_reparses(self):
        """No content check beyond size and mtime, so a touched file is
        re-parsed rather than trusted."""
        self.scan()
        self.backdate("pkg/a.py")
        self.assert_warm_matches_cold(1)

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
        costs a parse when the exclusion is lifted."""
        self.scan()
        self.scan(exclude=["web"])
        self.assertEqual(
            [p for p in self.doc()["files"] if p.startswith("web/")], [])
        self.assertEqual(self.scan().parses, 2)  # both web/*.ts come back


class TestRecordOrdering(CacheCase):
    """A record's identity must be captured no later than its content.

    If a file changes between being read and being stat'd, storing the new
    identity beside the old facts claims the stale facts are current. Storing
    the older identity is safe: the next scan sees a mismatch and re-parses.
    """

    def racing_store(self, target, new_text):
        repo = self.repo

        class RacingStore(facts.Store):
            """Rewrites `target` in the window between read and stat."""

            def _extract(self, key):
                rec = facts.Store._extract(self, key)
                if key == target:
                    write(repo, target, new_text)
                return rec

        return RacingStore

    def test_file_changed_between_read_and_stat_is_reparsed(self):
        """The gitignored case: git cannot flag it, so ordering is all that
        stands between the cache and a stale record.

        The target must be absent from the seed scan — the window only exists
        for a file being extracted, and a file already in the cache is
        replayed without ever being opened.
        """
        self.scan()
        write(self.repo, "gen/x.py", "import os\n")
        racing = self.racing_store("gen/x.py",
                                   "import os\nimport sys\nimport json\n")
        self.scan(store_cls=racing)
        self.assertEqual(self.doc()["files"]["gen/x.py"]["facts"]["loc"], 1)
        self.assert_warm_matches_cold(1)
        self.assertEqual(self.doc()["files"]["gen/x.py"]["facts"]["loc"], 3)


class TestDegradation(CacheCase):
    def test_truncated_cache_degrades_quietly(self):
        self.scan()
        with open(os.path.join(self.out, cache.CACHE_FILE), "w",
                  encoding="utf-8") as f:
            f.write('{"files": {"a.py": tru')
        self.assertEqual(self.scan().note, "cache unreadable")

    def test_wrong_shape_degrades(self):
        self.write_doc({"cache_version": cache.CACHE_VERSION, "files": []})
        self.assertEqual(self.scan().note, "cache malformed")

    def test_stale_facts_version_degrades(self):
        self.scan()
        doc = self.doc()
        doc["facts_version"] = doc["facts_version"] + 1
        self.write_doc(doc)
        self.assertEqual(self.scan().note,
                         "cache written by a different repokg")

    def test_stale_cache_version_degrades(self):
        self.scan()
        doc = self.doc()
        doc["cache_version"] = doc["cache_version"] + 1
        self.write_doc(doc)
        self.assertEqual(self.scan().note,
                         "cache written by a different repokg")

    def test_unknown_cached_head_degrades(self):
        self.scan()
        doc = self.doc()
        doc["head"] = "0" * 40
        self.write_doc(doc)
        self.assertIn("git cannot bound what changed", self.scan().note)

    def test_cache_without_git_to_validate_it_degrades(self):
        """A document is only trustworthy in combination with git. With no
        checkout to ask, neither gate can speak for any path, so nothing may
        be replayed however well the recorded sizes and mtimes match."""
        self.scan()
        shutil.rmtree(os.path.join(self.repo, ".git"))
        r = self.scan()
        self.assertEqual(r.note, "git cannot list working-tree changes")
        self.assertGreater(r.parses, 0)
        self.assertEqual(r.hits, 0)

    def test_degraded_scan_reseeds_a_usable_cache(self):
        self.write_doc({"nonsense": True})
        self.scan()
        self.assertEqual(self.scan().parses, 0)

    def test_unwritable_document_path_does_not_fail_the_scan(self):
        os.makedirs(os.path.join(self.out, cache.CACHE_FILE))
        r = self.scan()
        self.assertEqual(r.note, "cache unreadable")
        self.assertGreater(r.parses, 0)


class TestNoCacheFlag(CacheCase):
    def test_no_cache_parses_everything(self):
        seeded = self.scan().parses
        r = self.scan(no_cache=True)
        self.assertEqual(r.note, "disabled (--no-cache)")
        self.assertEqual(r.parses, seeded)

    def test_no_cache_leaves_the_document_alone(self):
        self.scan()
        before = self.doc()
        self.scan(no_cache=True)
        self.assertEqual(self.doc(), before)


class TestCliReporting(CacheCase):
    """The CLI wiring and its report line, which the cache-layer scans skip."""

    def cli(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = main(["scan", self.repo, "--no-github"] + list(extra))
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def test_cold_then_warm_report_lines(self):
        self.assertIn("cache: cold (no cache yet) — parsed", self.cli())
        self.assertIn("cache: replayed 5 of 5 files, parsed 0", self.cli())

    def test_no_cache_report_line(self):
        self.cli()
        self.assertIn("cache: disabled (--no-cache) — parsed 5 files",
                      self.cli("--no-cache"))

    def test_report_is_not_stored_in_the_graph(self):
        """Warm and cold must write identical kg.json, so the one number that
        differs between them may never enter it."""
        self.cli()
        with open(os.path.join(self.out, "kg.json"), encoding="utf-8") as f:
            kg = f.read()
        self.assertNotIn("cache", kg.lower().replace("repokg", ""))

    def test_generate_honours_no_cache(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = main(["generate", self.repo, "--no-github", "--no-cache"])
        self.assertEqual(rc, 0)
        self.assertIn("disabled (--no-cache)", buf.getvalue())

    def test_clean_removes_the_cache(self):
        self.cli()
        self.assertTrue(os.path.isfile(os.path.join(self.out,
                                                    cache.CACHE_FILE)))
        clean(self.repo, self.out,
              os.path.join(self.repo, "KNOWLEDGE_GRAPH.md"))
        self.assertFalse(os.path.exists(self.out))

    def test_no_temporary_file_is_left_behind(self):
        self.cli()
        self.assertEqual(
            [f for f in os.listdir(self.out) if f.endswith(".tmp")], [])


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

    def test_history_is_empty_when_head_has_not_moved(self):
        self.assertEqual(cache._history(".", "abc", "abc"), set())

    def test_history_is_unbounded_without_a_commit_on_either_side(self):
        self.assertIsNone(cache._history(".", "", "abc"))
        self.assertIsNone(cache._history(".", "abc", ""))


# Everything that decides what a cached record contains. Compared against a
# recorded value so that changing extraction without bumping FACTS_VERSION is
# a failing test rather than a silently stale cache in someone's repo.
FINGERPRINTS = {
    1: "35497aa5e0f9fce9",
}


def _extractor_fingerprint():
    parts = [repr(sorted(facts.LANG_BY_EXT.items())),
             repr(facts.MAX_FILE_BYTES),
             repr(sorted(facts.JS_EXTS)),
             repr(sorted(facts.JVM_EXTS)),
             repr(sorted((ext, fn.__name__)
                         for ext, fn in facts._EXTRACTORS.items()))]
    for name in sorted(dir(facts)):
        if name.endswith("_RE"):
            parts.append(name + "=" + getattr(facts, name).pattern)
    for fn in (facts._go, facts._py, facts._js, facts._rust, facts._jvm,
               facts._lines, facts._decode,
               facts.Store._extract, facts.Store._loc_only):
        parts.append(inspect.getsource(fn))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


class TestFactsVersionDiscipline(unittest.TestCase):
    def test_extraction_changes_require_a_version_bump(self):
        expected = FINGERPRINTS.get(FACTS_VERSION)
        self.assertIsNotNone(
            expected,
            "FACTS_VERSION is %d but tests/test_cache.py has no fingerprint "
            "for it — add one." % FACTS_VERSION)
        self.assertEqual(
            _extractor_fingerprint(), expected,
            "\n\nExtraction changed but FACTS_VERSION is still %d.\n"
            "A cache written by the old code would be replayed as if it were "
            "current, which is a wrong graph and not a failing build.\n"
            "Bump FACTS_VERSION in src/repokg/facts.py and add its "
            "fingerprint to FINGERPRINTS in this file." % FACTS_VERSION)


if __name__ == "__main__":
    unittest.main()
