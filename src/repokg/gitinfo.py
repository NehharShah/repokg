"""Git collectors: branches, trunk/integration detection, merge classification, contributors."""

import os
import subprocess

INTEGRATION_CANDIDATES = ("staging", "develop", "dev", "next", "canary")


def run(repo, *args):
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def try_run(repo, *args):
    try:
        return run(repo, *args)
    except (RuntimeError, OSError):
        return ""


def collect(repo):
    """Return (repo_info, branches). Each branch dict carries merged_ancestry/ahead
    against the default base (integration branch if present, else trunk)."""
    top = run(repo, "rev-parse", "--show-toplevel")
    remote = try_run(repo, "remote", "get-url", "origin")

    fmt = "%(refname:short)%09%(committerdate:short)%09%(authorname)%09%(contents:subject)"
    raw = try_run(repo, "for-each-ref", "refs/remotes/origin", "--format=" + fmt)
    use_remote = bool(raw)
    if not use_remote:
        raw = try_run(repo, "for-each-ref", "refs/heads", "--format=" + fmt)

    branches = []
    for line in raw.splitlines():
        parts = line.split("\t", 3)
        while len(parts) < 4:
            parts.append("")
        ref, date, author, subject = parts
        name = ref[len("origin/"):] if use_remote and ref.startswith("origin/") else ref
        if name in ("HEAD", "") or ref == "origin":
            continue
        branches.append({
            "name": name, "ref": ref, "date": date,
            "author": author, "subject": subject,
        })
    names = {b["name"] for b in branches}

    trunk, trunk_method = _detect_trunk(repo, names, use_remote)
    integration = next((c for c in INTEGRATION_CANDIDATES if c in names and c != trunk), "")
    base = integration or trunk
    base_ref = ("origin/" + base) if (use_remote and base) else base
    prefix = "refs/remotes/origin" if use_remote else "refs/heads"

    merged_refs, ahead_map = set(), {}
    if base_ref:
        out = try_run(repo, "for-each-ref", prefix, "--merged=" + base_ref,
                      "--format=%(refname:short)")
        merged_refs = set(out.splitlines())
        # One call for all ahead counts (git >= 2.41); fall back per-branch below.
        out = try_run(repo, "for-each-ref", prefix,
                      "--format=%(refname:short)%09%(ahead-behind:" + base_ref + ")")
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                nums = parts[1].split()
                if len(nums) == 2 and nums[0].isdigit():
                    ahead_map[parts[0]] = int(nums[0])

    for b in branches:
        b["merged_ancestry"] = b["ref"] in merged_refs
        if b["name"] in (trunk, integration) or b["merged_ancestry"] or not base_ref:
            b["ahead"] = 0
        elif b["ref"] in ahead_map:
            b["ahead"] = ahead_map[b["ref"]]
        else:
            n = try_run(repo, "rev-list", "--count", "%s..%s" % (base_ref, b["ref"]))
            b["ahead"] = int(n) if n.isdigit() else 0

    trunk_ref = ("origin/" + trunk) if use_remote and trunk in names else trunk
    contributors = []
    for line in try_run(repo, "shortlog", "-sn", "--no-merges", trunk_ref).splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2 and parts[0].strip().isdigit():
            contributors.append({"name": parts[1].strip(), "commits": int(parts[0])})

    count = try_run(repo, "rev-list", "--count", trunk_ref)
    roots = try_run(repo, "log", "--max-parents=0", "--format=%cs", trunk_ref).splitlines()

    info = {
        "path": top,
        "name": _repo_name(remote, top),
        "remote": remote,
        "head": try_run(repo, "rev-parse", "HEAD"),
        "trunk": trunk,
        "trunk_method": trunk_method,
        "integration": integration,
        "default_base": base,
        "first_commit": roots[-1] if roots else "",
        "commit_count": int(count) if count.isdigit() else 0,
        "contributors": contributors,
    }
    return info, branches


def _detect_trunk(repo, names, use_remote):
    """Return (trunk_name, detection_method)."""
    if use_remote:
        head = try_run(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
        if head:
            return head.rsplit("/", 1)[-1], "origin/HEAD symref"
    for cand in ("main", "master"):
        if cand in names:
            return cand, "well-known name"
    cur = try_run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if cur and cur != "HEAD":
        return cur, "current branch"
    return (sorted(names)[0], "first branch alphabetically") if names else ("", "none")


def _repo_name(remote, top):
    if remote:
        tail = remote.rstrip("/").rsplit("/", 1)[-1]
        return tail[:-4] if tail.endswith(".git") else tail
    return os.path.basename(top)
