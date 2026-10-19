import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock

from repokg import providers
from repokg.cli import main
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
        self.assertEqual(providers.detect("https://bitbucket.org/w/r.git"),
                         "bitbucket")

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


BB = "https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests"


def bb(id_, state, branch, **kw):
    x = {"id": id_, "title": "pr %d" % id_, "state": state,
         "source": {"branch": {"name": branch}},
         "author": {"nickname": "dev", "display_name": "Dev Eloper"},
         "created_on": "2024-04-01T09:00:00.000000+00:00",
         "updated_on": "2024-04-03T09:00:00.000000+00:00"}
    x.update(kw)
    return x


class TestBitbucketProvider(unittest.TestCase):
    ORIGIN = "git@bitbucket.org:ws/repo.git"

    def fetch(self, pages=(), limit=1000, origin=None, token=None):
        """Run fetch_prs with `_get_json` answering one page per call."""
        env = {"BITBUCKET_TOKEN": token} if token else {}
        answers = list(pages)
        with mock.patch.dict("os.environ", env, clear=False), \
                mock.patch.object(providers.gitinfo, "try_run",
                                  return_value=origin or self.ORIGIN), \
                mock.patch.object(providers.BitbucketProvider, "_get_json",
                                  side_effect=answers) as get:
            res = providers.BitbucketProvider().fetch_prs("/r", limit)
        return res, get

    def test_slug_forms(self):
        slug = providers.BitbucketProvider._slug
        for url in ("git@bitbucket.org:ws/repo.git",
                    "https://user@bitbucket.org/ws/repo.git",
                    "https://bitbucket.org/ws/repo",
                    "ssh://git@bitbucket.org:7999/ws/repo.git"):
            self.assertEqual(slug(url), "ws/repo", url)
        self.assertEqual(slug("git@github.com:ws/repo.git"), "")
        self.assertEqual(slug("https://bitbucket.org/ws"), "")

    def test_unrecognized_remote_degrades_with_a_note(self):
        (prs, note), get = self.fetch(origin="git@github.com:a/b.git")
        self.assertEqual(prs, [])
        self.assertIn("not recognized", note)
        get.assert_not_called()

    def test_normalizes_states_and_fields(self):
        (prs, note), get = self.fetch([{"values": [
            bb(3, "MERGED", "feat/c"), bb(2, "DECLINED", "feat/b"),
            bb(1, "OPEN", "feat/a"), bb(4, "SUPERSEDED", "feat/d")]}])
        self.assertEqual(note, "")
        self.assertEqual([p["state"] for p in prs],
                         ["OPEN", "CLOSED", "MERGED", "CLOSED"])
        self.assertEqual(prs[2], {
            "number": 3, "title": "pr 3", "state": "MERGED", "head": "feat/c",
            "author": "dev", "created": "2024-04-01", "merged": "2024-04-03",
            "closed": "", "draft": False})
        self.assertEqual(prs[0]["merged"], "")
        self.assertEqual(prs[0]["closed"], "")
        self.assertEqual(prs[1]["closed"], "2024-04-03")
        url = get.call_args[0][0]
        self.assertTrue(url.startswith(BB + "?"))
        for st in ("OPEN", "MERGED", "DECLINED", "SUPERSEDED"):
            self.assertIn("state=" + st, url)

    def test_follows_next_links_up_to_the_limit(self):
        p1 = {"values": [bb(i, "MERGED", "b%d" % i) for i in range(50, 40, -1)],
              "next": BB + "?page=2"}
        p2 = {"values": [bb(i, "MERGED", "b%d" % i) for i in range(40, 30, -1)]}
        (prs, note), get = self.fetch([p1, p2], limit=15)
        self.assertEqual(note, "")
        self.assertEqual(len(prs), 15)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(max(p["number"] for p in prs), 50)

    def test_does_not_follow_next_off_the_api_host(self):
        p1 = {"values": [bb(1, "OPEN", "a")], "next": "https://evil.example/x"}
        (prs, _), get = self.fetch([p1, {"values": []}])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(prs), 1)

    def test_http_error_degrades_and_hints_at_the_token(self):
        err = urllib.error.HTTPError(BB, 403, "Forbidden", {}, None)
        (prs, note), _ = self.fetch([err])
        self.assertEqual(prs, [])
        self.assertIn("HTTP 403", note)
        self.assertIn("BITBUCKET_TOKEN", note)

    def test_http_error_with_a_token_does_not_hint(self):
        err = urllib.error.HTTPError(BB, 401, "Unauthorized", {}, None)
        (_, note), _ = self.fetch([err], token="t")
        self.assertIn("HTTP 401", note)
        self.assertNotIn("BITBUCKET_TOKEN", note)

    def test_network_error_degrades_with_a_note(self):
        (prs, note), _ = self.fetch([urllib.error.URLError("no route")])
        self.assertEqual(prs, [])
        self.assertIn("bitbucket API failed", note)

    def test_failure_on_a_later_page_keeps_what_was_fetched(self):
        p1 = {"values": [bb(1, "OPEN", "a")], "next": BB + "?page=2"}
        (prs, note), _ = self.fetch([p1, ValueError("bad json")])
        self.assertEqual(len(prs), 1)
        self.assertIn("partial: 1 PRs fetched", note)

    def test_token_is_sent_as_a_bearer_header(self):
        captured = {}

        class Resp(object):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"values": []}'

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            providers.BitbucketProvider()._get_json(BB, "sekrit")
            self.assertEqual(captured["auth"], "Bearer sekrit")
            providers.BitbucketProvider()._get_json(BB, "")
            self.assertIsNone(captured["auth"])


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


class TestScanWiring(unittest.TestCase):
    """`scan` picks the provider from origin, and `--provider` overrides it."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        for args in (["init", "-q", "-b", "main", "."],
                     ["remote", "add", "origin", "git@gitlab.com:g/r.git"],
                     ["commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write(".repokg/\n")
        self.addCleanup(shutil.rmtree, self.repo, True)

    def scan(self, *extra):
        seen = []

        def fake(name):
            class P(providers.PRProvider):
                def fetch_prs(self, repo, limit):
                    seen.append(name)
                    return [], "%s note" % name
            P.name = name
            return P

        patched = {n: fake(n) for n in providers.PROVIDERS}
        with mock.patch.dict(providers.PROVIDERS, patched), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = main(["scan", self.repo, *extra])
        self.assertEqual(rc, 0)
        with open(os.path.join(self.repo, ".repokg", "kg.json")) as f:
            return seen, json.load(f)

    def test_auto_detects_from_origin(self):
        seen, kg = self.scan()
        self.assertEqual(seen, ["gitlab"])
        self.assertEqual(kg["github_note"], "gitlab note")

    def test_flag_overrides_detection(self):
        seen, _ = self.scan("--provider", "bitbucket")
        self.assertEqual(seen, ["bitbucket"])

    def test_no_github_skips_every_provider(self):
        seen, kg = self.scan("--no-github")
        self.assertEqual(seen, [])
        self.assertIn("--no-github", kg["github_note"])


if __name__ == "__main__":
    unittest.main()
