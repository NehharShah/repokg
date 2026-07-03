import os
import tempfile
import unittest

from repokg.inject import BEGIN, END, run


class TestInject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.md = os.path.join(self.repo, "KNOWLEDGE_GRAPH.md")

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self, rel):
        with open(os.path.join(self.repo, rel), encoding="utf-8") as f:
            return f.read()

    def test_creates_agents_md_when_nothing_exists(self):
        results = {k: v[0] for k, v in run(self.repo, self.md).items()}
        self.assertEqual(results, {"AGENTS.md": "created"})
        text = self._read("AGENTS.md")
        self.assertIn(BEGIN, text)
        self.assertIn("KNOWLEDGE_GRAPH.md", text)

    def test_updates_existing_claude_md_preserving_content(self):
        path = os.path.join(self.repo, "CLAUDE.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# My project\n\nHand-written rules.\n")
        results = run(self.repo, self.md)
        self.assertEqual(results["CLAUDE.md"][0], "updated")
        text = self._read("CLAUDE.md")
        self.assertIn("Hand-written rules.", text)
        self.assertIn(BEGIN, text)
        # AGENTS.md must NOT be created when CLAUDE.md exists
        self.assertNotIn("AGENTS.md", results)

    def test_idempotent(self):
        run(self.repo, self.md)
        results = {k: v[0] for k, v in run(self.repo, self.md).items()}
        self.assertEqual(results, {"AGENTS.md": "unchanged"})
        self.assertEqual(self._read("AGENTS.md").count(BEGIN), 1)
        self.assertEqual(self._read("AGENTS.md").count(END), 1)

    def test_block_replaced_not_duplicated_after_edit(self):
        run(self.repo, self.md)
        path = os.path.join(self.repo, "AGENTS.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\nUser section below the block.\n")
        run(self.repo, self.md)
        text = self._read("AGENTS.md")
        self.assertEqual(text.count(BEGIN), 1)
        self.assertIn("User section below the block.", text)

    def test_cursor_rules_dir(self):
        os.makedirs(os.path.join(self.repo, ".cursor", "rules"))
        results = run(self.repo, self.md)
        self.assertEqual(results[".cursor/rules/repokg.mdc"][0], "created")
        text = self._read(".cursor/rules/repokg.mdc")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("alwaysApply: true", text)
        self.assertIn(BEGIN, text)

    def test_kg_outside_repo_uses_absolute_path(self):
        outside = os.path.join(tempfile.gettempdir(), "elsewhere", "KNOWLEDGE_GRAPH.md")
        run(self.repo, outside)
        self.assertIn(outside, self._read("AGENTS.md"))


if __name__ == "__main__":
    unittest.main()
