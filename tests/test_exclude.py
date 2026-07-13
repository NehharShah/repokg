import os
import tempfile
import unittest

from repokg.code import walk
from repokg.findings import build


def write(root, rel, text="x = 1\n"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestWalkExclude(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        write(self.repo, "src/app.py")
        write(self.repo, "src/fixtures/big.py")
        write(self.repo, "fixtures/data.py")
        write(self.repo, "docs/gen/index.html")

    def tearDown(self):
        self.tmp.cleanup()

    def dirs(self, exclude=None, stats=None):
        return {rel for rel, _ in walk(self.repo, exclude, stats)}

    def test_no_exclude_walks_everything(self):
        self.assertEqual(
            self.dirs(),
            {"", "src", "src/fixtures", "fixtures", "docs", "docs/gen"})

    def test_root_level_pattern_only_matches_root(self):
        seen = self.dirs(exclude=["fixtures"])
        self.assertNotIn("fixtures", seen)
        self.assertIn("src/fixtures", seen)

    def test_star_crosses_slashes(self):
        seen = self.dirs(exclude=["*fixtures"])
        self.assertNotIn("fixtures", seen)
        self.assertNotIn("src/fixtures", seen)
        self.assertIn("src", seen)

    def test_nested_pattern(self):
        seen = self.dirs(exclude=["docs/gen"])
        self.assertIn("docs", seen)
        self.assertNotIn("docs/gen", seen)

    def test_pruned_dir_excludes_whole_tree(self):
        write(self.repo, "docs/gen/deep/inner.py")
        seen = self.dirs(exclude=["docs"])
        self.assertNotIn("docs", seen)
        self.assertNotIn("docs/gen", seen)
        self.assertNotIn("docs/gen/deep", seen)

    def test_file_patterns_drop_matching_files(self):
        write(self.repo, "src/schema.sql")
        tree = dict(walk(self.repo, ["*.sql"]))
        self.assertIn("app.py", tree["src"])
        self.assertNotIn("schema.sql", tree["src"])

    def test_stats_counts(self):
        stats = {}
        write(self.repo, "src/a.snap")
        write(self.repo, "src/b.snap")
        self.dirs(exclude=["*fixtures", "*.snap"], stats=stats)
        self.assertEqual(stats["excluded_dirs"], 2)
        self.assertEqual(stats["excluded_files"], 2)

    def test_stats_untouched_when_nothing_matches(self):
        stats = {}
        self.dirs(exclude=["nomatch*"], stats=stats)
        self.assertEqual(stats, {})


class TestExcludeFinding(unittest.TestCase):
    def kg(self, **exclude):
        return {"repo": {"trunk": "main", "trunk_method": "origin/HEAD symref"},
                "exclude": exclude}

    def test_note_emitted_when_dirs_excluded(self):
        _, notes = build(self.kg(patterns=["*fixtures"], dirs=3, files=0))
        note = [n for n in notes if "excluded" in n]
        self.assertEqual(len(note), 1)
        self.assertIn("3 dirs", note[0])
        self.assertIn("*fixtures", note[0])

    def test_no_note_when_nothing_excluded(self):
        _, notes = build(self.kg(patterns=["*fixtures"], dirs=0, files=0))
        self.assertFalse([n for n in notes if "invisible" in n])

    def test_no_note_without_exclude_key(self):
        _, notes = build({"repo": {}})
        self.assertFalse([n for n in notes if "invisible" in n])


if __name__ == "__main__":
    unittest.main()
