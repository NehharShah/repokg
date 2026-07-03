"""GitHub collectors (via `gh` CLI) and branch x PR classification."""

import json
import shutil
import subprocess

FIELDS = "number,title,state,headRefName,author,createdAt,mergedAt,closedAt,isDraft"

# Branch statuses:
#   trunk / integration          - the long-lived branches
#   active                       - has an open PR
#   merged                       - reachable from the default base (merge-commit merge)
#   squash-merged                - not an ancestor, but its PR was merged (squash/rebase)
#   abandoned                    - only closed-without-merge PRs
#   stale                        - no PR ever opened


def collect(repo, limit=1000):
    """Return (prs, note). Degrades gracefully when gh is unavailable."""
    if not shutil.which("gh"):
        return [], "gh CLI not found; PR data skipped"
    p = subprocess.run(
        ["gh", "pr", "list", "--state", "all", "--limit", str(limit), "--json", FIELDS],
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


def classify(branches, prs, trunk, integration):
    """Set status + prs on every branch by cross-referencing PR head refs."""
    by_head = {}
    for pr in prs:
        by_head.setdefault(pr["head"], []).append(pr)
    for b in branches:
        linked = by_head.get(b["name"], [])
        b["prs"] = [pr["number"] for pr in linked]
        states = {pr["state"] for pr in linked}
        if b["name"] == trunk:
            b["status"] = "trunk"
        elif integration and b["name"] == integration:
            b["status"] = "integration"
        elif "OPEN" in states:
            b["status"] = "active"
        elif b.get("merged_ancestry"):
            b["status"] = "merged"
        elif "MERGED" in states:
            b["status"] = "squash-merged"
        elif "CLOSED" in states:
            b["status"] = "abandoned"
        else:
            b["status"] = "stale"
