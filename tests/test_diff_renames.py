"""Tests for rename detection — the one heuristic in the diff.

Everything else the diff reports is a fact about two documents. A rename is a
guess about which removal and which addition are the same module, so what
matters here is as much what is *refused* as what is found: an ambiguous
pairing, a language change, or a name match with no imports to corroborate it
must not be reported as a confident move.

Each accepted pairing carries the tier it matched at and the evidence for it,
which is what the "no guessed facts" rule asks of a heuristic.
"""

import unittest

from repokg import diff


def graph(modules, edges=()):
    """A graph carrying only what rename detection reads."""
    return {
        "repokg_version": 1,
        "generated_at": "2026-01-01",
        "repo": {"head": "a" * 40},
        "modules": [dict(m) for m in modules],
        "edges": [dict(e) for e in edges],
        "languages": [], "branches": [], "prs": [], "ops": {},
    }


def mod(path, lang="Python", files=1, loc=5):
    return {"path": path, "lang": lang, "files": files, "loc": loc,
            "root": False, "generated": False}


def dep(src, dst, lang="Python"):
    return {"from": src, "to": dst, "lang": lang, "count": 1}


def renames(old, new, **kw):
    return diff.build(old, new, **kw)["modules"]["renamed"]


class TestDetected(unittest.TestCase):
    def test_preserved_imports_are_high_confidence(self):
        """Every dependency to and from the unmoved parts of the graph
        survived, which is the signature of a move."""
        old = graph([mod("app"), mod("lib")], [dep("app", "lib")])
        new = graph([mod("app"), mod("shared")], [dep("app", "shared")])
        self.assertEqual(renames(old, new), [{
            "from": "lib", "to": "shared", "confidence": "high",
            "evidence": ["1 of 1 imports across unmoved modules preserved"]}])

    def test_a_leaf_module_keeping_its_name_is_medium(self):
        """No imports either way, so the directory name is all there is —
        enough to report, not enough to be sure of."""
        old = graph([mod("app"), mod("api/billing")])
        new = graph([mod("app"), mod("core/billing")])
        self.assertEqual(renames(old, new), [{
            "from": "api/billing", "to": "core/billing",
            "confidence": "medium", "evidence": ["same directory name"]}])

    def test_partly_preserved_imports_are_medium(self):
        """Half the neighbourhood survived: consistent with a move that also
        dropped a dependency, which is a normal refactor."""
        old = graph([mod("app"), mod("web"), mod("lib")],
                    [dep("app", "lib"), dep("web", "lib")])
        new = graph([mod("app"), mod("web"), mod("shared")],
                    [dep("app", "shared")])
        [pair] = renames(old, new)
        self.assertEqual((pair["from"], pair["to"], pair["confidence"]),
                         ("lib", "shared", "medium"))
        self.assertEqual(pair["evidence"],
                         ["1 of 2 imports across unmoved modules preserved"])

    def test_a_name_match_the_imports_contradict_is_low(self):
        """Two unrelated packages can share a directory name, so a match with
        no shared imports says so rather than claiming a move."""
        old = graph([mod("app"), mod("web"), mod("x/thing")],
                    [dep("app", "x/thing")])
        new = graph([mod("app"), mod("web"), mod("y/thing")],
                    [dep("web", "y/thing")])
        [pair] = renames(old, new)
        self.assertEqual(pair["confidence"], "low")
        self.assertIn("no shared imports to corroborate it", pair["evidence"])

    def test_both_signals_are_recorded_as_evidence(self):
        old = graph([mod("app"), mod("a/core")], [dep("app", "a/core")])
        new = graph([mod("app"), mod("b/core")], [dep("app", "b/core")])
        [pair] = renames(old, new)
        self.assertEqual(pair["confidence"], "high")
        self.assertEqual(pair["evidence"],
                         ["1 of 1 imports across unmoved modules preserved",
                          "same directory name"])

    def test_two_independent_moves_are_both_found(self):
        old = graph([mod("app"), mod("one"), mod("two")],
                    [dep("app", "one"), dep("two", "app")])
        new = graph([mod("app"), mod("x/one"), mod("y/two")],
                    [dep("app", "x/one"), dep("y/two", "app")])
        self.assertEqual([(p["from"], p["to"]) for p in renames(old, new)],
                         [("one", "x/one"), ("two", "y/two")])

    def test_renames_are_sorted_by_source_path(self):
        old = graph([mod("app"), mod("zeta"), mod("alpha")])
        new = graph([mod("app"), mod("n/zeta"), mod("n/alpha")])
        self.assertEqual([p["from"] for p in renames(old, new)],
                         ["alpha", "zeta"])


class TestRefused(unittest.TestCase):
    def test_a_language_change_is_never_a_rename(self):
        """A Python package does not become a Go one by being moved, and
        allowing it would open the door to pairing on size alone."""
        old = graph([mod("app"), mod("a/lib", lang="Python")])
        new = graph([mod("app"), mod("b/lib", lang="Go")])
        self.assertEqual(renames(old, new), [])

    def test_a_module_split_in_two_is_not_a_rename(self):
        """Both halves look equally like the original, so picking one would be
        arbitrary — and would claim a dependency survived intact."""
        old = graph([mod("app"), mod("lib")], [dep("app", "lib")])
        new = graph([mod("app"), mod("part1"), mod("part2")],
                    [dep("app", "part1"), dep("app", "part2")])
        self.assertEqual(renames(old, new), [])

    def test_two_candidates_for_one_addition_is_not_a_rename(self):
        old = graph([mod("app"), mod("one"), mod("two")],
                    [dep("app", "one"), dep("app", "two")])
        new = graph([mod("app"), mod("merged")], [dep("app", "merged")])
        self.assertEqual(renames(old, new), [])

    def test_an_unrelated_deletion_and_addition_is_not_a_rename(self):
        old = graph([mod("app"), mod("gone")])
        new = graph([mod("app"), mod("fresh")])
        self.assertEqual(renames(old, new), [])

    def test_no_shared_imports_and_no_shared_name_is_not_a_rename(self):
        old = graph([mod("app"), mod("web"), mod("gone")],
                    [dep("app", "gone")])
        new = graph([mod("app"), mod("web"), mod("fresh")],
                    [dep("web", "fresh")])
        self.assertEqual(renames(old, new), [])

    def test_an_addition_with_nothing_removed_is_not_a_rename(self):
        old = graph([mod("app")])
        new = graph([mod("app"), mod("app2")])
        self.assertEqual(renames(old, new), [])

    def test_too_many_candidates_skips_detection_and_says_so(self):
        """A repo-wide reorganisation is both what blows the pair count up and
        where pairing is least trustworthy."""
        n = 101  # 101 * 101 = 10201 pairs, over the limit
        old = graph([mod("app")] + [mod("old%d" % i) for i in range(n)])
        new = graph([mod("app")] + [mod("new%d" % i) for i in range(n)])
        d = diff.build(old, new)
        self.assertEqual(d["modules"]["renamed"], [])
        self.assertTrue(any("too many to pair renames" in note
                            for note in d["notes"]))
        self.assertEqual(len(d["modules"]["added"]), n)


class TestItOnlyAddsInformation(unittest.TestCase):
    """The heuristic may not change what the diff found, only how it reads."""

    def test_the_pair_stays_in_added_and_removed(self):
        """A consumer that distrusts the pairing can ignore `renamed` and see
        exactly what it would have seen without it."""
        old = graph([mod("app"), mod("lib")], [dep("app", "lib")])
        new = graph([mod("app"), mod("shared")], [dep("app", "shared")])
        d = diff.build(old, new)
        self.assertEqual([m["path"] for m in d["modules"]["removed"]], ["lib"])
        self.assertEqual([m["path"] for m in d["modules"]["added"]],
                         ["shared"])
        self.assertEqual(len(d["modules"]["renamed"]), 1)

    def test_disabling_detection_changes_nothing_else(self):
        old = graph([mod("app"), mod("lib")], [dep("app", "lib")])
        new = graph([mod("app"), mod("shared")], [dep("app", "shared")])
        on, off = diff.build(old, new), diff.build(old, new,
                                                   detect_renames=False)
        self.assertEqual(off["modules"]["renamed"], [])
        on["modules"]["renamed"] = []
        self.assertEqual(on, off)

    def test_a_rename_is_still_a_shape_change(self):
        """The module set did move. Every path an agent, a doc or a CODEOWNERS
        entry referenced has changed, which is worth a non-zero exit even
        though no dependency did."""
        old = graph([mod("app"), mod("lib")], [dep("app", "lib")])
        new = graph([mod("app"), mod("shared")], [dep("app", "shared")])
        self.assertTrue(diff.build(old, new)["shape_changed"])

    def test_the_key_is_always_present(self):
        d = diff.build(graph([mod("app")]), graph([mod("app")]))
        self.assertEqual(d["modules"]["renamed"], [])
        self.assertFalse(diff.any_change(d))

    def test_detection_reads_a_document_with_no_edges_section(self):
        d = diff.build({"modules": [mod("a/lib")]}, {"modules": [mod("b/lib")]})
        self.assertEqual([p["confidence"] for p in d["modules"]["renamed"]],
                         ["medium"])


class TestReports(unittest.TestCase):
    def delta(self):
        old = graph([mod("app"), mod("lib")], [dep("app", "lib")])
        new = graph([mod("app"), mod("shared")], [dep("app", "shared")])
        return diff.build(old, new)

    def test_a_rename_prints_once_instead_of_three_times(self):
        text = diff.render_text(self.delta())
        self.assertIn("R lib -> shared", text)
        self.assertNotIn("+ shared", text)
        self.assertNotIn("- lib ", text)
        self.assertIn("modules  R1", text)  # not +1 -1

    def test_the_row_carries_confidence_and_evidence(self):
        text = diff.render_text(self.delta())
        self.assertIn("high, 1 of 1 imports across unmoved modules preserved",
                      text)

    def test_markdown_marks_renames_too(self):
        md = diff.render_markdown(self.delta())
        self.assertIn("- **R** `lib → shared` — high, 1 of 1 imports", md)

    def test_the_edge_churn_a_rename_causes_is_still_shown(self):
        """Suppressing it would mean trusting the pairing enough to hide a real
        dependency change that happened alongside the move."""
        text = diff.render_text(self.delta())
        self.assertIn("+ app -> shared (Python)", text)
        self.assertIn("- app -> lib (Python)", text)

    def test_without_detection_the_pair_prints_as_two_rows(self):
        old = graph([mod("app"), mod("lib")], [dep("app", "lib")])
        new = graph([mod("app"), mod("shared")], [dep("app", "shared")])
        text = diff.render_text(diff.build(old, new, detect_renames=False))
        self.assertIn("+ shared", text)
        self.assertIn("- lib", text)
        self.assertNotIn(" R ", text)


if __name__ == "__main__":
    unittest.main()
