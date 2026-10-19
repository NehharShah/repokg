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


class TestProviderAgnosticClassification(unittest.TestCase):
    """The same classifier gives the same answer whichever host the PRs came from."""

    def test_squash_merged_branch(self):
        branches = [{"name": "feat/x", "ref": "origin/feat/x"}]
        prs = [{"number": 1, "head": "feat/x", "state": "MERGED"}]
        classify(branches, prs, trunk="main", integration="")
        self.assertEqual(branches[0]["status"], "squash-merged")


if __name__ == "__main__":
    unittest.main()
