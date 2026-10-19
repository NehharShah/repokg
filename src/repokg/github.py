"""Branch x PR classification. Provider-agnostic: it only reads the normalized
PR shape that `providers` produces, so it works for any host."""

# Branch statuses:
#   trunk / integration          - the long-lived branches
#   active                       - has an open PR
#   merged                       - reachable from the default base (merge-commit merge)
#   squash-merged                - not an ancestor, but its PR was merged (squash/rebase)
#   abandoned                    - only closed-without-merge PRs
#   stale                        - no PR ever opened


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
