import os
import tempfile
import unittest

from repokg.inject import clean, run


class TestClean(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.out = os.path.join(self.repo, ".repokg")
        self.md = os.path.join(self.repo, "KNOWLEDGE_GRAPH.md")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path) or self.repo, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_full_roundtrip(self):
        self._write("CLAUDE.md", "# Mine\n\nMy rules.\n")
        os.makedirs(self.out)
        self._write(".repokg/kg.json", "{}")
        self._write("KNOWLEDGE_GRAPH.md",
                    "# x — Codebase Knowledge Graph\n\n> Machine-generated knowledge "
                    "graph (by [repokg](https://github.com/NehharShah/repokg)) on x.\n")
        run(self.repo, self.md)
        self.assertIn("repokg:begin", open(os.path.join(self.repo, "CLAUDE.md")).read())

        actions = clean(self.repo, self.out, self.md)
        self.assertEqual(actions["CLAUDE.md"], "block stripped")
        text = open(os.path.join(self.repo, "CLAUDE.md")).read()
        self.assertIn("My rules.", text)
        self.assertNotIn("repokg:begin", text)
        self.assertFalse(os.path.exists(self.md))
        self.assertFalse(os.path.exists(self.out))

    def test_never_deletes_foreign_knowledge_graph(self):
        self._write("KNOWLEDGE_GRAPH.md", "# Hand-written by a human\n")
        actions = clean(self.repo, self.out, self.md)
        self.assertIn("SKIPPED", actions["KNOWLEDGE_GRAPH.md"])
        self.assertTrue(os.path.exists(self.md))

    def test_deletes_agents_md_it_authored(self):
        run(self.repo, self.md)  # creates AGENTS.md containing only our block
        actions = clean(self.repo, self.out, self.md)
        self.assertEqual(actions["AGENTS.md"], "deleted (was repokg-authored)")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "AGENTS.md")))

    def test_dry_run_touches_nothing(self):
        run(self.repo, self.md)
        actions = clean(self.repo, self.out, self.md, write=False)
        self.assertTrue(actions)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "AGENTS.md")))


class TestInjectDryRun(unittest.TestCase):
    def test_write_false_leaves_disk_untouched(self):
        with tempfile.TemporaryDirectory() as repo:
            md = os.path.join(repo, "KNOWLEDGE_GRAPH.md")
            results = run(repo, md, write=False)
            status, old, new = results["AGENTS.md"]
            self.assertEqual(status, "created")
            self.assertIn("repokg:begin", new)
            self.assertFalse(os.path.exists(os.path.join(repo, "AGENTS.md")))


if __name__ == "__main__":
    unittest.main()
