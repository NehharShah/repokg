import unittest

from repokg.github import classify


def b(name, merged_ancestry=False):
    return {"name": name, "ref": "origin/" + name, "merged_ancestry": merged_ancestry}


def pr(number, head, state):
    return {"number": number, "head": head, "state": state}


class TestClassify(unittest.TestCase):
    def test_statuses(self):
        branches = [
            b("master"), b("staging"),
            b("feat/open-work"),
            b("feat/merged-ancestry", merged_ancestry=True),
            b("feat/squashed"),
            b("feat/abandoned"),
            b("feat/no-pr"),
        ]
        prs = [
            pr(1, "feat/open-work", "OPEN"),
            pr(2, "feat/squashed", "MERGED"),
            pr(3, "feat/abandoned", "CLOSED"),
        ]
        classify(branches, prs, trunk="master", integration="staging")
        got = {x["name"]: x["status"] for x in branches}
        self.assertEqual(got["master"], "trunk")
        self.assertEqual(got["staging"], "integration")
        self.assertEqual(got["feat/open-work"], "active")
        self.assertEqual(got["feat/merged-ancestry"], "merged")
        self.assertEqual(got["feat/squashed"], "squash-merged")
        self.assertEqual(got["feat/abandoned"], "abandoned")
        self.assertEqual(got["feat/no-pr"], "stale")

    def test_open_pr_wins_over_ancestry(self):
        branches = [b("feat/reopened", merged_ancestry=True)]
        classify(branches, [pr(9, "feat/reopened", "OPEN")], "main", "")
        self.assertEqual(branches[0]["status"], "active")

    def test_pr_numbers_attached(self):
        branches = [b("feat/x")]
        classify(branches, [pr(4, "feat/x", "MERGED"), pr(7, "feat/x", "CLOSED")],
                 "main", "")
        self.assertEqual(branches[0]["prs"], [4, 7])


if __name__ == "__main__":
    unittest.main()
