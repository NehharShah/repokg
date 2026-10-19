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
import re
import shutil
import subprocess

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


PROVIDERS = {cls.name: cls for cls in (GitHubProvider,)}

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
