"""Enrichment coverage and dead narratives (pure computation)."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from repokg import status
from repokg.cli import main

KG = {"modules": [
    {"path": "big", "loc": 800},
    {"path": "mid", "loc": 150},
    {"path": "small", "loc": 50},
]}


def narr(**modules):
    return {"modules": modules}


class TestModuleCoverage(unittest.TestCase):
    def test_all_described(self):
        c = status.coverage(KG, narr(big="a", mid="b", small="c"))
        self.assertEqual(c["modules"], {"total": 3, "covered": 3,
                                        "percent": 100.0, "missing": []})

    def test_none_described(self):
        c = status.coverage(KG, {})
        self.assertEqual(c["modules"]["covered"], 0)
        self.assertEqual(c["modules"]["percent"], 0.0)
        self.assertEqual(c["loc"]["percent"], 0.0)

    def test_missing_is_largest_first(self):
        c = status.coverage(KG, narr(mid="b"))
        self.assertEqual(c["modules"]["missing"], ["big", "small"])

    def test_missing_ties_break_by_path(self):
        kg = {"modules": [{"path": "b", "loc": 5}, {"path": "a", "loc": 5}]}
        self.assertEqual(status.coverage(kg, {})["modules"]["missing"],
                         ["a", "b"])

    def test_percent_is_one_decimal(self):
        c = status.coverage(KG, narr(big="a"))
        self.assertEqual(c["modules"]["percent"], 33.3)

    def test_blank_and_non_string_purposes_do_not_count(self):
        c = status.coverage(KG, narr(big="", mid="   ", small=7))
        self.assertEqual(c["modules"]["covered"], 0)
        self.assertEqual(c["modules"]["missing"], ["big", "mid", "small"])

    def test_empty_graph_is_unmeasured_not_complete(self):
        c = status.coverage({"modules": []}, narr(x="y"))
        self.assertEqual(c["modules"]["total"], 0)
        self.assertIsNone(c["modules"]["percent"])
        self.assertIsNone(c["loc"]["percent"])

    def test_modules_with_no_loc_leave_loc_unmeasured(self):
        kg = {"modules": [{"path": "a", "loc": 0}]}
        c = status.coverage(kg, narr(a="x"))
        self.assertEqual(c["modules"]["percent"], 100.0)
        self.assertIsNone(c["loc"]["percent"])


class TestLocWeighting(unittest.TestCase):
    def test_loc_weighting_differs_from_module_count(self):
        c = status.coverage(KG, narr(big="a"))
        self.assertEqual(c["modules"]["percent"], 33.3)
        self.assertEqual(c["loc"], {"total": 1000, "covered": 800,
                                    "percent": 80.0})

    def test_small_modules_alone_barely_move_loc(self):
        c = status.coverage(KG, narr(mid="b", small="c"))
        self.assertEqual(c["modules"]["percent"], 66.7)
        self.assertEqual(c["loc"]["percent"], 20.0)

    def test_dead_keys_do_not_inflate_coverage(self):
        c = status.coverage(KG, narr(gone="x", big="a"))
        self.assertEqual(c["modules"]["covered"], 1)
        self.assertEqual(c["loc"]["covered"], 800)


class TestSections(unittest.TestCase):
    def sections(self, n):
        return status.coverage(KG, n)["sections"]

    def test_absent(self):
        self.assertEqual(self.sections({}), {
            "overview": False, "flows": 0, "timeline": 0, "gotchas": 0,
            "sections": 0})

    def test_present(self):
        n = {"overview": "x",
             "flows": [{"name": "f", "steps": ["s"]}],
             "timeline": [{"period": "2026-01", "theme": "t"}],
             "gotchas": ["g", "h"],
             "sections": [{"title": "T", "body": "B"}]}
        self.assertEqual(self.sections(n), {
            "overview": True, "flows": 1, "timeline": 1, "gotchas": 2,
            "sections": 1})

    def test_malformed_entries_are_not_counted(self):
        n = {"overview": "  ",
             "flows": [{"name": "f", "steps": []}, {"steps": ["s"]}, "x"],
             "timeline": [{"period": "p"}, None],
             "gotchas": ["", 3, "ok"],
             "sections": [{"title": "t"}, []]}
        self.assertEqual(self.sections(n), {
            "overview": False, "flows": 0, "timeline": 0, "gotchas": 1,
            "sections": 0})

    def test_wrong_container_types_are_zero_not_a_crash(self):
        n = {"flows": {}, "timeline": "x", "gotchas": 1, "sections": None}
        self.assertEqual(self.sections(n)["flows"], 0)


class TestDeadNarratives(unittest.TestCase):
    def test_none_when_every_key_is_a_module(self):
        self.assertEqual(status.dead_narratives(KG, narr(big="a", mid="b")), [])

    def test_keys_missing_from_the_scan_are_dead_and_sorted(self):
        n = narr(zzz="x", big="a", old="y")
        self.assertEqual(status.dead_narratives(KG, n), ["old", "zzz"])

    def test_a_blank_value_does_not_hide_a_dead_key(self):
        self.assertEqual(status.dead_narratives(KG, narr(gone="")), ["gone"])

    def test_keys_match_as_written(self):
        n = narr(**{"big/": "a", "BIG": "b"})
        self.assertEqual(status.dead_narratives(KG, n), ["BIG", "big/"])

    def test_no_modules_key_or_wrong_type(self):
        for n in ({}, {"modules": []}, {"modules": None}, [], None):
            self.assertEqual(status.dead_narratives(KG, n), [])

    def test_empty_scan_makes_every_key_dead(self):
        self.assertEqual(
            status.dead_narratives({"modules": []}, narr(a="x")), ["a"])

    def test_root_module_key(self):
        kg = {"modules": [{"path": "(root)", "loc": 3}]}
        self.assertEqual(status.dead_narratives(kg, narr(**{"(root)": "x"})), [])


class TestCompute(unittest.TestCase):
    def test_shape_and_inputs_untouched(self):
        n = narr(big="a", gone="b")
        before = json.dumps([KG, n], sort_keys=True)
        out = status.compute(KG, n)
        self.assertEqual(sorted(out), ["coverage", "dead_narratives"])
        self.assertEqual(out["dead_narratives"], ["gone"])
        self.assertEqual(out["coverage"]["modules"]["covered"], 1)
        self.assertEqual(json.dumps([KG, n], sort_keys=True), before)

    def test_result_is_json_serializable(self):
        json.dumps(status.compute(KG, narr(big="a")))


ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestAgainstARealScan(unittest.TestCase):
    """The acceptance case: rename a module and its old key goes dead."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        for rel, text in {".gitignore": ".repokg/\n", "app/main.py": "x = 1\n",
                          "lib/helper.py": "y = 2\n"}.items():
            _write(self.repo, rel, text)
        for args in (["init", "-q", "-b", "main", "."], ["add", "-A"],
                     ["commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, env=ENV,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def scan(self):
        with contextlib.redirect_stdout(io.StringIO()):
            main(["scan", self.repo, "--no-github", "--no-cache"])
        with open(os.path.join(self.repo, ".repokg", "kg.json")) as f:
            return json.load(f)

    def test_fully_enriched_then_renamed(self):
        n = narr(app="Entry.", lib="Helpers.")
        kg = self.scan()
        out = status.compute(kg, n)
        self.assertEqual(out["coverage"]["modules"]["covered"], 2)
        self.assertEqual(out["coverage"]["modules"]["total"], 2)
        self.assertEqual(out["dead_narratives"], [])

        os.rename(os.path.join(self.repo, "lib"),
                  os.path.join(self.repo, "helpers"))
        out = status.compute(self.scan(), n)
        self.assertEqual(out["dead_narratives"], ["lib"])
        self.assertEqual(out["coverage"]["modules"]["missing"], ["helpers"])
        self.assertEqual(out["coverage"]["modules"]["covered"], 1)


if __name__ == "__main__":
    unittest.main()
