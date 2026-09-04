"""Tests for the structural diff engine.

The engine is a pure function of two dicts, so almost everything here is an
invariant rather than a scenario: diffing a graph against itself is empty,
diffing in the other direction swaps additions and removals, and neither the
order of the input lists nor anything outside the compared fields may reach
the result.
"""

import copy
import json
import unittest

from repokg import diff


def kg(**over):
    """A minimal but well-formed knowledge graph, with sections replaced
    wholesale by keyword."""
    doc = {
        "repokg_version": 1,
        "generated_at": "2026-01-01",
        "repo": {"head": "a" * 40},
        "languages": [{"lang": "Python", "files": 2, "loc": 10}],
        "modules": [{"path": "app", "lang": "Python", "files": 2, "loc": 10,
                     "root": False, "generated": False}],
        "edges": [],
        "branches": [],
        "prs": [],
        "ops": {"workflows": [], "test_dirs": [], "dockerfiles": []},
    }
    doc.update(over)
    return doc


def module(path, **over):
    m = {"path": path, "lang": "Python", "files": 1, "loc": 5, "root": False,
         "generated": False}
    m.update(over)
    return m


def edge(src, dst, lang="Python", count=1):
    return {"from": src, "to": dst, "lang": lang, "count": count}


def branch(name, status="active", **over):
    b = {"name": name, "status": status, "date": "2026-01-01", "ahead": 1,
         "subject": "wip", "merged_ancestry": False, "prs": []}
    b.update(over)
    return b


class TestInvariants(unittest.TestCase):
    def test_a_graph_against_itself_is_empty(self):
        d = diff.build(kg(), kg())
        self.assertFalse(diff.any_change(d))
        self.assertFalse(d["shape_changed"])
        self.assertEqual(diff.counts(d), {})
        self.assertEqual(d["notes"], [])

    def test_reversing_the_diff_swaps_additions_and_removals(self):
        a = kg(modules=[module("app")], edges=[edge("app", "lib")])
        b = kg(modules=[module("app"), module("lib")], edges=[])
        forward = diff.build(a, b)
        back = diff.build(b, a)
        self.assertEqual(forward["modules"]["added"],
                         back["modules"]["removed"])
        self.assertEqual(forward["edges"]["removed"], back["edges"]["added"])

    def test_input_order_does_not_reach_the_result(self):
        """Collectors sort their output, but a hand-written or older document
        need not, and a diff that changed with list order could not be
        compared across runs."""
        mods = [module("a"), module("b"), module("c")]
        one = diff.build(kg(modules=mods), kg(modules=[]))
        other = diff.build(kg(modules=list(reversed(mods))), kg(modules=[]))
        self.assertEqual(one, other)
        self.assertEqual([m["path"] for m in one["modules"]["removed"]],
                         ["a", "b", "c"])

    def test_neither_argument_is_mutated(self):
        a, b = kg(modules=[module("a")]), kg(modules=[module("b")])
        before = copy.deepcopy(a), copy.deepcopy(b)
        diff.build(a, b)
        self.assertEqual((a, b), before)

    def test_the_delta_is_json_serialisable(self):
        """--json output and any downstream consumer depend on it, and a tuple
        or a set leaking into a record would only fail at the boundary."""
        d = diff.build(kg(modules=[module("a")]),
                       kg(modules=[module("a", loc=9)], prs=[
                           {"number": 1, "state": "OPEN", "title": "t",
                            "head": "h"}]))
        json.dumps(d)

    def test_repeated_builds_agree(self):
        a = kg(modules=[module("a")], branches=[branch("main")])
        b = kg(modules=[module("b")], branches=[branch("main", "merged")])
        self.assertEqual(json.dumps(diff.build(a, b), sort_keys=True),
                         json.dumps(diff.build(a, b), sort_keys=True))


class TestModules(unittest.TestCase):
    def test_added_and_removed_carry_the_whole_record(self):
        """A renderer reports "app (Python, 120 LOC) removed", so the record
        has to survive the diff rather than just its path."""
        d = diff.build(kg(modules=[module("gone", loc=120)]),
                       kg(modules=[module("fresh", loc=7)]))
        self.assertEqual(d["modules"]["removed"], [module("gone", loc=120)])
        self.assertEqual(d["modules"]["added"], [module("fresh", loc=7)])

    def test_loc_movement_reports_only_the_field_that_moved(self):
        d = diff.build(kg(modules=[module("app", loc=10, files=1)]),
                       kg(modules=[module("app", loc=40, files=1)]))
        self.assertEqual(d["modules"]["changed"],
                         [{"id": ["app"], "before": {"loc": 10},
                           "after": {"loc": 40}}])

    def test_a_module_is_keyed_on_path_alone(self):
        """Everything else about a module can change without it becoming a
        different module."""
        d = diff.build(kg(modules=[module("app", lang="Python", loc=1)]),
                       kg(modules=[module("app", lang="Go", loc=900)]))
        self.assertEqual(d["modules"]["added"], [])
        self.assertEqual(d["modules"]["removed"], [])
        self.assertEqual(d["modules"]["changed"][0]["before"],
                         {"lang": "Python", "loc": 1})

    def test_a_move_reads_as_a_removal_and_an_addition(self):
        """Rename recovery is a heuristic and deliberately not done here."""
        d = diff.build(kg(modules=[module("old/path")]),
                       kg(modules=[module("new/path")]))
        self.assertEqual([m["path"] for m in d["modules"]["removed"]],
                         ["old/path"])
        self.assertEqual([m["path"] for m in d["modules"]["added"]],
                         ["new/path"])
        self.assertEqual(d["modules"]["changed"], [])


class TestEdges(unittest.TestCase):
    def test_edges_are_keyed_on_language_as_well_as_endpoints(self):
        """A module pair can be joined in two languages at once, and collapsing
        them would hide one of the two edges."""
        d = diff.build(kg(edges=[edge("a", "b", "Python")]),
                       kg(edges=[edge("a", "b", "Python"),
                                 edge("a", "b", "TypeScript")]))
        self.assertEqual(d["edges"]["added"], [edge("a", "b", "TypeScript")])
        self.assertEqual(d["edges"]["changed"], [])

    def test_a_new_dependency_is_reported(self):
        d = diff.build(kg(edges=[]), kg(edges=[edge("api", "billing")]))
        self.assertEqual(d["edges"]["added"], [edge("api", "billing")])

    def test_import_count_movement_is_reported(self):
        d = diff.build(kg(edges=[edge("api", "billing", count=4)]),
                       kg(edges=[edge("api", "billing", count=11)]))
        self.assertEqual(d["edges"]["changed"],
                         [{"id": ["api", "billing", "Python"],
                           "before": {"count": 4}, "after": {"count": 11}}])


class TestNoiseIsNotReported(unittest.TestCase):
    """Fields left out of SECTIONS, which move on unrelated work."""

    def test_a_branch_tip_moving_is_not_a_change(self):
        d = diff.build(
            kg(branches=[branch("main", date="2026-01-01", ahead=1,
                                subject="old")]),
            kg(branches=[branch("main", date="2026-06-01", ahead=9,
                                subject="new")]))
        self.assertEqual(d["branches"]["changed"], [])
        self.assertFalse(diff.any_change(d))

    def test_a_branch_status_transition_is_a_change(self):
        d = diff.build(kg(branches=[branch("x", "active")]),
                       kg(branches=[branch("x", "merged")]))
        self.assertEqual(d["branches"]["changed"],
                         [{"id": ["x"], "before": {"status": "active"},
                           "after": {"status": "merged"}}])

    def test_the_head_and_the_scan_date_are_provenance_not_changes(self):
        d = diff.build(kg(), kg(generated_at="2027-09-09",
                                repo={"head": "b" * 40}))
        self.assertFalse(diff.any_change(d))
        self.assertEqual(d["old"]["head"], "a" * 40)
        self.assertEqual(d["new"]["generated_at"], "2027-09-09")

    def test_a_pr_title_edit_is_not_a_change_but_its_state_is(self):
        old = [{"number": 7, "state": "OPEN", "title": "first", "head": "h"}]
        new = [{"number": 7, "state": "OPEN", "title": "reworded",
                "head": "h"}]
        self.assertEqual(diff.build(kg(prs=old), kg(prs=new))["prs"]["changed"],
                         [])
        new[0]["state"] = "MERGED"
        self.assertEqual(
            diff.build(kg(prs=old), kg(prs=new))["prs"]["changed"],
            [{"id": ["7"], "before": {"state": "OPEN"},
              "after": {"state": "MERGED"}}])

    def test_prs_sort_numerically(self):
        """String order would put 10 before 9 and make the report look
        shuffled."""
        prs = [{"number": n, "state": "OPEN"} for n in (2, 9, 10, 100)]
        d = diff.build(kg(prs=[]), kg(prs=prs))
        self.assertEqual([p["number"] for p in d["prs"]["added"]],
                         [2, 9, 10, 100])


class TestShapeChanged(unittest.TestCase):
    """What the CLI's exit code keys off: shape, not measurement."""

    def test_a_new_module_changes_shape(self):
        self.assertTrue(diff.build(kg(modules=[]),
                                   kg(modules=[module("a")]))["shape_changed"])

    def test_a_new_edge_changes_shape(self):
        self.assertTrue(diff.build(
            kg(edges=[]), kg(edges=[edge("a", "b")]))["shape_changed"])

    def test_a_new_language_changes_shape(self):
        self.assertTrue(diff.build(kg(), kg(languages=[
            {"lang": "Python", "files": 2, "loc": 10},
            {"lang": "Go", "files": 1, "loc": 3}]))["shape_changed"])

    def test_a_module_switching_language_changes_shape(self):
        self.assertTrue(diff.build(
            kg(modules=[module("a", lang="Python")]),
            kg(modules=[module("a", lang="Go")]))["shape_changed"])

    def test_loc_drift_does_not_change_shape(self):
        """The whole point of the distinction: LOC moves on every commit, so a
        CI gate keyed on it would carry no information."""
        d = diff.build(kg(modules=[module("a", loc=10)]),
                       kg(modules=[module("a", loc=4000)]))
        self.assertTrue(diff.any_change(d))
        self.assertFalse(d["shape_changed"])

    def test_edge_weight_does_not_change_shape(self):
        d = diff.build(kg(edges=[edge("a", "b", count=1)]),
                       kg(edges=[edge("a", "b", count=90)]))
        self.assertTrue(diff.any_change(d))
        self.assertFalse(d["shape_changed"])

    def test_branch_and_pr_churn_does_not_change_shape(self):
        d = diff.build(
            kg(branches=[branch("x", "active")], prs=[]),
            kg(branches=[branch("x", "stale"), branch("y")],
               prs=[{"number": 1, "state": "OPEN"}]))
        self.assertTrue(diff.any_change(d))
        self.assertFalse(d["shape_changed"])

    def test_a_new_workflow_changes_shape(self):
        d = diff.build(kg(), kg(ops={
            "workflows": [{"file": ".github/workflows/ci.yml", "name": "CI"}],
            "test_dirs": [], "dockerfiles": []}))
        self.assertTrue(d["shape_changed"])

    def test_renaming_a_workflow_does_not_change_shape(self):
        """Which workflows exist is the surface; what they are called is
        detail."""
        wf = ".github/workflows/ci.yml"
        d = diff.build(
            kg(ops={"workflows": [{"file": wf, "name": "CI"}]}),
            kg(ops={"workflows": [{"file": wf, "name": "Build"}]}))
        self.assertTrue(diff.any_change(d))
        self.assertFalse(d["shape_changed"])


class TestOps(unittest.TestCase):
    def test_bare_path_entries_are_their_own_identity(self):
        d = diff.build(kg(ops={"test_dirs": ["tests"]}),
                       kg(ops={"test_dirs": ["tests", "e2e"]}))
        self.assertEqual(d["ops"]["test_dirs"]["added"], ["e2e"])
        self.assertEqual(d["ops"]["test_dirs"]["removed"], [])

    def test_a_workflow_is_identified_by_file_and_compared_on_name(self):
        """`name` is an identity candidate for other ops shapes, so it must not
        be swallowed as one here."""
        wf = ".github/workflows/ci.yml"
        d = diff.build(kg(ops={"workflows": [{"file": wf, "name": "CI"}]}),
                       kg(ops={"workflows": [{"file": wf, "name": "Tests"}]}))
        self.assertEqual(d["ops"]["workflows"]["changed"],
                         [{"id": [wf], "before": {"name": "CI"},
                           "after": {"name": "Tests"}}])

    def test_config_dir_entries_are_compared(self):
        d = diff.build(kg(ops={"config_dirs": [{"dir": "configs",
                                                "entries": ["a.yaml"]}]}),
                       kg(ops={"config_dirs": [{"dir": "configs",
                                                "entries": ["a.yaml",
                                                            "b.yaml"]}]}))
        self.assertEqual(d["ops"]["config_dirs"]["changed"][0]["after"],
                         {"entries": ["a.yaml", "b.yaml"]})

    def test_unchanged_ops_keys_are_pruned(self):
        """Eleven keys are collected and most are empty in any given repo, so
        carrying them all would drown the delta."""
        d = diff.build(kg(ops={"test_dirs": ["tests"], "docs": [],
                               "helm_charts": []}),
                       kg(ops={"test_dirs": ["tests", "e2e"], "docs": [],
                               "helm_charts": []}))
        self.assertEqual(list(d["ops"]), ["test_dirs"])


class TestMissingAndMalformed(unittest.TestCase):
    def test_a_section_only_one_side_has_is_skipped_with_a_note(self):
        """An older document that predates a section must not read as though
        the whole section had just been added."""
        old = kg()
        del old["languages"]
        d = diff.build(old, kg())
        self.assertEqual(d["languages"], {"added": [], "removed": [],
                                          "changed": []})
        self.assertTrue(any("languages" in n for n in d["notes"]))
        self.assertFalse(d["shape_changed"])

    def test_a_section_missing_from_both_sides_is_silent(self):
        old, new = kg(), kg()
        del old["prs"], new["prs"]
        d = diff.build(old, new)
        self.assertEqual(d["notes"], [])
        self.assertEqual(d["prs"], {"added": [], "removed": [],
                                    "changed": []})

    def test_records_without_an_identity_are_skipped_with_a_note(self):
        d = diff.build(kg(modules=[module("a"), {"lang": "Go"}, "junk"]),
                       kg(modules=[module("a")]))
        self.assertEqual(d["modules"]["removed"], [])
        self.assertTrue(any("could not be identified" in n
                            for n in d["notes"]))

    def test_a_section_of_the_wrong_type_is_skipped(self):
        d = diff.build(kg(edges={"not": "a list"}), kg(edges=[edge("a", "b")]))
        self.assertEqual(d["edges"]["added"], [])
        self.assertTrue(any("edges" in n for n in d["notes"]))

    def test_an_empty_document_diffs_without_raising(self):
        d = diff.build({}, {})
        self.assertFalse(diff.any_change(d))
        self.assertFalse(d["shape_changed"])

    def test_an_empty_document_against_a_real_one_reports_no_phantom_changes(self):
        """Every section is missing on one side, so all of them are skipped."""
        d = diff.build({}, kg(modules=[module("a")]))
        self.assertFalse(d["shape_changed"])
        self.assertEqual(d["modules"]["added"], [])
        self.assertTrue(len(d["notes"]) >= 5)

    def test_a_schema_version_gap_is_noted(self):
        d = diff.build(kg(repokg_version=1), kg(repokg_version=2))
        self.assertTrue(any("different graph schemas" in n
                            for n in d["notes"]))

    def test_an_ops_key_only_one_side_collected_is_noted(self):
        d = diff.build(kg(ops={"workflows": []}),
                       kg(ops={"workflows": [], "sboms": ["x"]}))
        self.assertEqual(d["ops"], {})
        self.assertTrue(any("sboms" in n for n in d["notes"]))


class TestCounts(unittest.TestCase):
    def test_counts_report_only_the_sections_that_moved(self):
        d = diff.build(kg(modules=[module("a"), module("b")]),
                       kg(modules=[module("a", loc=99), module("c")]))
        self.assertEqual(diff.counts(d), {"modules": (1, 1, 1)})

    def test_ops_counts_are_summed_across_keys(self):
        d = diff.build(kg(ops={"test_dirs": [], "docs": []}),
                       kg(ops={"test_dirs": ["tests"],
                               "docs": ["docs/a.md", "docs/b.md"]}))
        self.assertEqual(diff.counts(d)["ops"], (3, 0, 0))


if __name__ == "__main__":
    unittest.main()
