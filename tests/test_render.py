import unittest

from repo_atlas.markdown import aggregate, mermaid_id, render

ATLAS = {
    "generated_at": "2026-07-03",
    "repo": {
        "name": "demo", "remote": "git@github.com:x/demo.git", "trunk": "main",
        "integration": "staging", "default_base": "staging",
        "first_commit": "2026-01-01", "commit_count": 42,
        "contributors": [{"name": "A", "commits": 40}],
    },
    "languages": [{"lang": "Python", "files": 3, "loc": 300}],
    "modules": [
        {"path": "src/app", "lang": "Python", "files": 2, "loc": 250,
         "root": True, "generated": False},
        {"path": "src/gen", "lang": "Python", "files": 1, "loc": 50,
         "root": False, "generated": True},
    ],
    "edges": [{"from": "src/app", "to": "src/gen", "lang": "Python", "count": 3}],
    "branches": [
        {"name": "main", "date": "2026-07-01", "status": "trunk", "prs": [], "ahead": 0},
        {"name": "feat/x", "date": "2026-07-02", "status": "active", "prs": [2], "ahead": 1},
    ],
    "prs": [
        {"number": 1, "title": "feat(app): initial", "state": "MERGED", "head": "feat/init",
         "author": "a", "created": "2026-01-02", "merged": "2026-01-03", "closed": "",
         "draft": False},
        {"number": 2, "title": "fix(app): bug | pipe", "state": "OPEN", "head": "feat/x",
         "author": "a", "created": "2026-07-02", "merged": "", "closed": "", "draft": False},
    ],
    "github_note": "",
    "ops": {"workflows": [{"file": ".github/workflows/ci.yml", "name": "CI"}],
            "dockerfiles": ["Dockerfile"], "compose": [], "helm_charts": [],
            "makefile_targets": ["test"], "config_dirs": [], "docs": [],
            "test_dirs": ["tests"], "migration_dirs": [], "proto_dirs": []},
}


class TestRender(unittest.TestCase):
    def test_mermaid_id(self):
        self.assertEqual(mermaid_id("internal/hedging"), "internal_hedging")
        self.assertEqual(mermaid_id(""), "root")

    def test_aggregate(self):
        self.assertEqual(aggregate("a/b/c", 2), "a/b")
        self.assertEqual(aggregate("a", 2), "a")

    def test_render_structure_only(self):
        doc = render(ATLAS, {})
        for needle in ("# demo — Codebase Atlas", "## 1. Repo at a glance",
                       "```mermaid", "src/app", "pending enrichment",
                       "Open PRs", "Appendix A", "bug \\| pipe", "## 7. Timeline"):
            self.assertIn(needle, doc)

    def test_render_enriched(self):
        n = {"overview": "Demo overview.",
             "modules": {"src/app": "The app."},
             "flows": [{"name": "Main flow", "steps": ["in", "out"]}],
             "timeline": [{"period": "2026-01", "theme": "built it"}],
             "gotchas": ["watch out"]}
        doc = render(ATLAS, n)
        for needle in ("Demo overview.", "The app.", "Main flow",
                       "built it", "watch out"):
            self.assertIn(needle, doc)
        self.assertNotIn("pending enrichment", doc.split("src/app")[1].split("\n")[0])


if __name__ == "__main__":
    unittest.main()
