import unittest

from repokg.validate import narratives


class TestValidate(unittest.TestCase):
    def test_valid_full(self):
        n = {"overview": "x", "modules": {"a": "b"},
             "flows": [{"name": "f", "steps": ["s1"]}],
             "timeline": [{"period": "2026-01", "theme": "t"}],
             "gotchas": ["g"]}
        self.assertEqual(narratives(n), [])

    def test_valid_partial(self):
        self.assertEqual(narratives({"modules": {"a": "b"}}), [])

    def test_not_object(self):
        self.assertTrue(narratives([1, 2])[0].startswith("narratives.json must be"))

    def test_unknown_key(self):
        errs = narratives({"summary": "x"})
        self.assertIn("unknown key 'summary'", errs[0])

    def test_bad_module_value(self):
        errs = narratives({"modules": {"a": 42}})
        self.assertIn("modules['a']", errs[0])

    def test_bad_flow_shape(self):
        errs = narratives({"flows": [{"steps": ["x"]}]})
        self.assertIn("flows[0]", errs[0])
        errs = narratives({"flows": [{"name": "f", "steps": [1]}]})
        self.assertIn("flows[0].steps", errs[0])

    def test_sections_valid(self):
        n = {"sections": [{"title": "Environments", "body": "| a | b |"}]}
        self.assertEqual(narratives(n), [])

    def test_sections_invalid(self):
        self.assertIn("sections must be a list", narratives({"sections": {}})[0])
        errs = narratives({"sections": [{"title": "x"}]})
        self.assertIn("sections[0]", errs[0])

    def test_bad_timeline_and_gotchas(self):
        self.assertIn("timeline[0]", narratives({"timeline": [{"period": "x"}]})[0])
        self.assertIn("gotchas", narratives({"gotchas": "not a list"})[0])


class TestFindings(unittest.TestCase):
    def test_build_and_render(self):
        from repokg.findings import build, render_text
        kg = {
            "repo": {"trunk": "main", "trunk_method": "well-known name",
                     "integration": "staging"},
            "branches": [
                {"name": "b1", "status": "squash-merged"},
                {"name": "b2", "status": "stale"},
            ],
            "edges": [{"from": "a", "to": "b", "lang": "JS/TS", "count": 1},
                      {"from": "a", "to": "b", "lang": "Go", "count": 2}],
            "modules": [{"path": "gen/pb", "generated": True}],
            "prs": [{"number": 1}],
            "github_note": "",
        }
        found, notes = build(kg)
        subjects = {f["subject"] for f in found}
        self.assertIn("trunk = main", subjects)
        self.assertIn("1 squash-merged branches", subjects)
        confs = {f["subject"]: f["confidence"] for f in found}
        self.assertEqual(confs["trunk = main"], "medium")  # name-guess, not symref
        self.assertEqual(confs["1 Go edges"], "high")
        self.assertEqual(confs["1 JS/TS edges"], "medium")
        self.assertEqual(confs["1 modules flagged generated"], "low")
        self.assertTrue(any("Fork PRs" in n for n in notes))
        text = render_text(found, notes)
        self.assertIn("[git]", text)
        self.assertIn("Needs manual review", text)


if __name__ == "__main__":
    unittest.main()
