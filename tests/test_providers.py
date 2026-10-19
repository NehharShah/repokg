import json
import subprocess
import unittest
from unittest import mock

from repokg import providers
from repokg.github import classify


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestDetect(unittest.TestCase):
    def test_remote_host_forms(self):
        for url, host in [
            ("https://github.com/o/r.git", "github.com"),
            ("https://user:tok@GitLab.com/g/s/r.git", "gitlab.com"),
            ("git@gitlab.example.org:g/r.git", "gitlab.example.org"),
            ("ssh://git@bitbucket.org:7999/ws/r.git", "bitbucket.org"),
            ("", ""),
            ("/local/path/repo", ""),
        ]:
            self.assertEqual(providers.remote_host(url), host, url)

    def test_detect(self):
        self.assertEqual(providers.detect("git@github.com:o/r.git"), "github")
        self.assertEqual(providers.detect("https://gitlab.com/g/r.git"), "gitlab")
        self.assertEqual(providers.detect("git@gitlab.corp.io:g/r.git"), "gitlab")

    def test_unrecognized_host_keeps_the_github_default(self):
        # GitHub Enterprise hosts have arbitrary names; `gh` handles them.
        self.assertEqual(providers.detect("git@git.corp.example:o/r.git"), "github")
        self.assertEqual(providers.detect(""), "github")

    def test_collect_auto_uses_the_origin_url(self):
        calls = []

        class Fake(providers.PRProvider):
            name = "gitlab"

            def fetch_prs(self, repo, limit):
                calls.append((repo, limit))
                return [], ""

        with mock.patch.dict(providers.PROVIDERS, {"gitlab": Fake}), \
                mock.patch.object(providers.gitinfo, "try_run",
                                  return_value="git@gitlab.com:g/r.git"):
            providers.collect("/repo", 7)
        self.assertEqual(calls, [("/repo", 7)])

    def test_explicit_provider_overrides_detection(self):
        with mock.patch.object(providers.gitinfo, "try_run",
                               return_value="git@gitlab.com:g/r.git") as t, \
                mock.patch("shutil.which", return_value=None):
            _, note = providers.collect("/repo", 5, "github")
        t.assert_not_called()
        self.assertIn("gh CLI not found", note)

    def test_unknown_provider_is_an_error(self):
        with self.assertRaises(RuntimeError):
            providers.collect("/repo", 5, "svn")


class TestGitHubProvider(unittest.TestCase):
    def test_missing_cli_degrades_with_a_note(self):
        with mock.patch("shutil.which", return_value=None):
            prs, note = providers.GitHubProvider().fetch_prs("/r", 10)
        self.assertEqual(prs, [])
        self.assertEqual(note, "gh CLI not found; PR data skipped")

    def test_cli_failure_degrades_with_a_note(self):
        with mock.patch("shutil.which", return_value="/bin/gh"), \
                mock.patch("subprocess.run",
                           return_value=completed(returncode=1, stderr="no auth")):
            prs, note = providers.GitHubProvider().fetch_prs("/r", 10)
        self.assertEqual((prs, note), ([], "gh failed: no auth"))

    def test_normalizes_and_sorts(self):
        payload = [
            {"number": 9, "title": " b ", "state": "MERGED", "headRefName": "f/b",
             "author": {"login": "bo"}, "createdAt": "2024-02-01T10:00:00Z",
             "mergedAt": "2024-02-03T10:00:00Z", "closedAt": "2024-02-03T10:00:00Z",
             "isDraft": False},
            {"number": 2, "title": "a", "state": "OPEN", "headRefName": "f/a",
             "author": None, "createdAt": "2024-01-01T00:00:00Z", "mergedAt": None,
             "closedAt": None, "isDraft": True},
        ]
        with mock.patch("shutil.which", return_value="/bin/gh"), \
                mock.patch("subprocess.run",
                           return_value=completed(json.dumps(payload))):
            prs, note = providers.GitHubProvider().fetch_prs("/r", 10)
        self.assertEqual(note, "")
        self.assertEqual([p["number"] for p in prs], [2, 9])
        self.assertEqual(prs[0], {
            "number": 2, "title": "a", "state": "OPEN", "head": "f/a",
            "author": "", "created": "2024-01-01", "merged": "", "closed": "",
            "draft": True})
        self.assertEqual(prs[1]["title"], "b")
        self.assertEqual(prs[1]["merged"], "2024-02-03")


def mr(iid, state, branch, **kw):
    x = {"iid": iid, "title": "mr %d" % iid, "state": state,
         "source_branch": branch, "author": {"username": "dev"},
         "created_at": "2024-03-01T08:00:00.000Z", "merged_at": None,
         "closed_at": None, "draft": False}
    x.update(kw)
    return x


class TestGitLabProvider(unittest.TestCase):
    def fetch(self, pages, limit=1000, which="/bin/glab"):
        """Run fetch_prs with `glab api` answering one JSON page per call."""
        outs = [completed(p if isinstance(p, str) else json.dumps(p))
                if not isinstance(p, subprocess.CompletedProcess) else p
                for p in pages]
        with mock.patch("shutil.which", return_value=which), \
                mock.patch("subprocess.run", side_effect=outs) as run:
            res = providers.GitLabProvider().fetch_prs("/r", limit)
        return res, run

    def test_missing_cli_degrades_with_a_note(self):
        (prs, note), run = self.fetch([], which=None)
        self.assertEqual(prs, [])
        self.assertEqual(note, "glab CLI not found; PR data skipped")
        run.assert_not_called()

    def test_normalizes_states_and_fields(self):
        (prs, note), run = self.fetch([[
            mr(3, "merged", "feat/c", merged_at="2024-03-05T01:00:00Z"),
            mr(2, "closed", "feat/b", closed_at="2024-03-04T01:00:00Z"),
            mr(1, "opened", "feat/a", draft=True),
            mr(4, "locked", "feat/d"),
        ]])
        self.assertEqual(note, "")
        self.assertEqual([p["number"] for p in prs], [1, 2, 3, 4])
        self.assertEqual([p["state"] for p in prs],
                         ["OPEN", "CLOSED", "MERGED", "OPEN"])
        self.assertEqual(prs[2], {
            "number": 3, "title": "mr 3", "state": "MERGED", "head": "feat/c",
            "author": "dev", "created": "2024-03-01", "merged": "2024-03-05",
            "closed": "", "draft": False})
        self.assertTrue(prs[0]["draft"])
        args = run.call_args[0][0]
        self.assertEqual(args[:2], ["glab", "api"])
        self.assertIn("state=all", args[2])

    def test_paginates_up_to_the_limit(self):
        page1 = [mr(i, "merged", "b%d" % i) for i in range(300, 200, -1)]
        page2 = [mr(i, "merged", "b%d" % i) for i in range(200, 100, -1)]
        (prs, note), run = self.fetch([page1, page2], limit=150)
        self.assertEqual(note, "")
        self.assertEqual(len(prs), 150)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(max(p["number"] for p in prs), 300)  # newest kept

    def test_failure_degrades_with_a_note(self):
        (prs, note), _ = self.fetch([completed(returncode=1, stderr="401")])
        self.assertEqual((prs, note), ([], "glab failed: 401"))

    def test_failure_on_a_later_page_keeps_what_was_fetched(self):
        page1 = [mr(i, "opened", "b%d" % i) for i in range(100)]
        (prs, note), _ = self.fetch([page1, completed(returncode=1, stderr="boom")])
        self.assertEqual(len(prs), 100)
        self.assertIn("partial: 100 MRs fetched", note)

    def test_invalid_output_degrades_with_a_note(self):
        (prs, note), _ = self.fetch(["not json"])
        self.assertEqual((prs, note), ([], "glab returned invalid JSON"))
        (prs, note), _ = self.fetch(['{"message": "404"}'])
        self.assertEqual((prs, note), ([], "glab returned unexpected output"))


class TestProviderAgnosticClassification(unittest.TestCase):
    """The same classifier gives the same answer whichever host the PRs came from."""

    def test_gitlab_classifies_like_github(self):
        gl = [mr(1, "merged", "feat/squashed", merged_at="2024-03-02T00:00:00Z"),
              mr(2, "closed", "feat/abandoned"),
              mr(3, "opened", "feat/active")]
        gh = [{"number": 1, "headRefName": "feat/squashed", "state": "MERGED"},
              {"number": 2, "headRefName": "feat/abandoned", "state": "CLOSED"},
              {"number": 3, "headRefName": "feat/active", "state": "OPEN"}]

        def classified(prs):
            branches = [{"name": n, "ref": "origin/" + n} for n in
                        ("main", "feat/squashed", "feat/abandoned",
                         "feat/active", "feat/none")]
            classify(branches, prs, trunk="main", integration="")
            return {b["name"]: b["status"] for b in branches}

        with mock.patch("shutil.which", return_value="/bin/x"), \
                mock.patch("subprocess.run",
                           return_value=completed(json.dumps(gl))):
            gl_prs, _ = providers.GitLabProvider().fetch_prs("/r", 10)
        with mock.patch("shutil.which", return_value="/bin/x"), \
                mock.patch("subprocess.run",
                           return_value=completed(json.dumps(gh))):
            gh_prs, _ = providers.GitHubProvider().fetch_prs("/r", 10)
        self.assertEqual(classified(gl_prs), classified(gh_prs))
        self.assertEqual(classified(gl_prs)["feat/squashed"], "squash-merged")


if __name__ == "__main__":
    unittest.main()
