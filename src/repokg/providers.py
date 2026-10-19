"""PR providers: where branch x PR data comes from.

Branch classification (`github.classify`) only needs a normalized PR list, so
everything host-specific lives behind one method:

    fetch_prs(repo, limit) -> (prs, note)

`prs` is a list of dicts with keys number/title/state/head/author/created/
merged/closed/draft, where `state` is one of OPEN / MERGED / CLOSED and the
dates are YYYY-MM-DD strings ("" when unknown). `note` is "" on success, or a
one-line reason the PR layer is missing or partial; a provider never raises
for an unavailable CLI, network or credentials — it degrades through `note`.

To add a host: subclass `PRProvider`, set `name`, optionally teach `detect` its
hostname, and register the class in `PROVIDERS`. The classifier is untouched.
"""

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import gitinfo

GH_FIELDS = "number,title,state,headRefName,author,createdAt,mergedAt,closedAt,isDraft"


class PRProvider(object):
    name = ""

    def fetch_prs(self, repo, limit):
        raise NotImplementedError


class GitHubProvider(PRProvider):
    name = "github"

    def fetch_prs(self, repo, limit):
        if not shutil.which("gh"):
            return [], "gh CLI not found; PR data skipped"
        p = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--limit", str(limit),
             "--json", GH_FIELDS],
            cwd=repo, capture_output=True, text=True)
        if p.returncode != 0:
            return [], "gh failed: " + p.stderr.strip()[:200]
        prs = []
        for x in json.loads(p.stdout or "[]"):
            prs.append({
                "number": x["number"],
                "title": x.get("title", "").strip(),
                "state": x.get("state", ""),
                "head": x.get("headRefName", ""),
                "author": (x.get("author") or {}).get("login", ""),
                "created": (x.get("createdAt") or "")[:10],
                "merged": (x.get("mergedAt") or "")[:10],
                "closed": (x.get("closedAt") or "")[:10],
                "draft": bool(x.get("isDraft")),
            })
        prs.sort(key=lambda pr: pr["number"])
        return prs, ""


class GitLabProvider(PRProvider):
    """Merge requests via `glab api`, which resolves the host and project from
    the repo's remote (so self-hosted GitLab works) and handles auth."""
    name = "gitlab"
    PAGE = 100  # GitLab's per_page ceiling
    STATES = {"opened": "OPEN", "locked": "OPEN", "merged": "MERGED",
              "closed": "CLOSED"}

    def fetch_prs(self, repo, limit):
        if not shutil.which("glab"):
            return [], "glab CLI not found; PR data skipped"
        prs, page = [], 1
        while len(prs) < limit:
            p = subprocess.run(
                ["glab", "api", "projects/:id/merge_requests?state=all"
                 "&order_by=created_at&sort=desc&per_page=%d&page=%d" %
                 (self.PAGE, page)],
                cwd=repo, capture_output=True, text=True)
            if p.returncode != 0:
                note = "glab failed: " + (p.stderr or p.stdout).strip()[:200]
                if prs:
                    note += " (partial: %d MRs fetched)" % len(prs)
                return self._finish(prs, limit), note
            try:
                batch = json.loads(p.stdout or "[]")
            except ValueError:
                return self._finish(prs, limit), "glab returned invalid JSON"
            if not isinstance(batch, list):
                return self._finish(prs, limit), "glab returned unexpected output"
            prs.extend(self._normalize(x) for x in batch)
            if len(batch) < self.PAGE:
                break
            page += 1
        return self._finish(prs, limit), ""

    @staticmethod
    def _finish(prs, limit):
        prs = prs[:limit]
        prs.sort(key=lambda pr: pr["number"])
        return prs

    def _normalize(self, x):
        return {
            "number": x["iid"],
            "title": (x.get("title") or "").strip(),
            "state": self.STATES.get(x.get("state"), (x.get("state") or "").upper()),
            "head": x.get("source_branch") or "",
            "author": (x.get("author") or {}).get("username", ""),
            "created": (x.get("created_at") or "")[:10],
            "merged": (x.get("merged_at") or "")[:10],
            "closed": (x.get("closed_at") or "")[:10],
            "draft": bool(x.get("draft") or x.get("work_in_progress")),
        }


class BitbucketProvider(PRProvider):
    """Bitbucket Cloud pull requests via the REST API, stdlib only.

    Public repositories work anonymously; private ones need a repository or
    workspace access token in $BITBUCKET_TOKEN (sent as a Bearer token).
    """
    name = "bitbucket"
    API = "https://api.bitbucket.org/2.0"
    PAGELEN = 50  # Bitbucket's pagelen ceiling for pull requests
    TIMEOUT = 30
    STATES = {"OPEN": "OPEN", "MERGED": "MERGED", "DECLINED": "CLOSED",
              "SUPERSEDED": "CLOSED"}

    def fetch_prs(self, repo, limit):
        slug = self._slug(gitinfo.try_run(repo, "remote", "get-url", "origin"))
        if not slug:
            return [], "bitbucket remote not recognized; PR data skipped"
        token = os.environ.get("BITBUCKET_TOKEN", "")
        query = urllib.parse.urlencode(
            [("state", s) for s in self.STATES] +
            [("pagelen", min(self.PAGELEN, limit)), ("sort", "-created_on")])
        url = "%s/repositories/%s/pullrequests?%s" % (self.API, slug, query)
        prs = []
        while url and len(prs) < limit:
            try:
                page = self._get_json(url, token)
            except urllib.error.HTTPError as e:
                hint = ("" if token else "; set BITBUCKET_TOKEN for private "
                        "repositories") if e.code in (401, 403, 404) else ""
                return self._finish(prs, limit), self._fail(
                    "bitbucket API returned HTTP %d%s" % (e.code, hint), prs)
            except (urllib.error.URLError, OSError, ValueError) as e:
                return self._finish(prs, limit), self._fail(
                    "bitbucket API failed: %s" % str(e)[:200], prs)
            prs.extend(self._normalize(x) for x in page.get("values", []))
            url = page.get("next", "")
            if not self._trusted(url):
                break
        return self._finish(prs, limit), ""

    @staticmethod
    def _fail(note, prs):
        return note + (" (partial: %d PRs fetched)" % len(prs) if prs else "")

    @staticmethod
    def _finish(prs, limit):
        prs = prs[:limit]
        prs.sort(key=lambda pr: pr["number"])
        return prs

    def _trusted(self, url):
        # `next` links are followed with the token attached, so they must
        # stay on the API host.
        return bool(url) and url.startswith(self.API + "/")

    def _get_json(self, url, token):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _slug(remote_url):
        """`workspace/repo` from a Bitbucket Cloud remote, or ""."""
        if remote_host(remote_url) != "bitbucket.org":
            return ""
        path = re.split(r"bitbucket\.org[:/]", remote_url, maxsplit=1)[-1]
        path = re.sub(r"^\d+/", "", path)  # ssh://host:7999/ws/repo
        parts = path.strip("/").split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return ""
        repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
        return "%s/%s" % (parts[0], repo)

    def _normalize(self, x):
        state = self.STATES.get(x.get("state"), (x.get("state") or "").upper())
        # The list endpoint has no merge/close timestamp; `updated_on` is the
        # closest it offers and is exact unless the PR is touched afterwards.
        ended = (x.get("updated_on") or "")[:10] if state != "OPEN" else ""
        return {
            "number": x["id"],
            "title": (x.get("title") or "").strip(),
            "state": state,
            "head": ((x.get("source") or {}).get("branch") or {}).get("name", ""),
            "author": (x.get("author") or {}).get("nickname")
            or (x.get("author") or {}).get("display_name", ""),
            "created": (x.get("created_on") or "")[:10],
            "merged": ended if state == "MERGED" else "",
            "closed": ended if state == "CLOSED" else "",
            "draft": bool(x.get("draft")),
        }


PROVIDERS = {cls.name: cls for cls in (GitHubProvider, GitLabProvider,
                                       BitbucketProvider)}

# Hostname fragments that identify a host. Anything unrecognized falls back to
# GitHub, which is what every scan did before providers existed: `gh` resolves
# GitHub Enterprise hosts itself, so guessing "unknown" would be a regression.
HOST_HINTS = (("gitlab", "gitlab"), ("bitbucket", "bitbucket"))
DEFAULT_PROVIDER = "github"


def remote_host(url):
    """Hostname of a git remote URL (https, ssh://, or scp-style), lowercased."""
    m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)", url or "", re.I)
    if not m:
        m = re.match(r"^(?:[^@/]+@)?([^:/]+):", url or "")
    return m.group(1).lower() if m else ""


def detect(remote_url):
    """Provider name for an origin URL; DEFAULT_PROVIDER when unrecognized."""
    host = remote_host(remote_url)
    for hint, name in HOST_HINTS:
        if hint in host and name in PROVIDERS:
            return name
    return DEFAULT_PROVIDER


def collect(repo, limit=1000, provider="auto"):
    """Return (prs, note) from the chosen or auto-detected provider."""
    if provider == "auto":
        provider = detect(gitinfo.try_run(repo, "remote", "get-url", "origin"))
    if provider not in PROVIDERS:
        raise RuntimeError("unknown PR provider %r (choose from: auto, %s)" %
                           (provider, ", ".join(sorted(PROVIDERS))))
    return PROVIDERS[provider]().fetch_prs(repo, limit)
