"""Incremental scan cache: replay per-file facts instead of re-parsing them.

The cache is an optimization and nothing else. A missing, unreadable, or
unusable cache degrades to a cold scan, `--no-cache` forces one, and a warm
scan's output is byte-identical to a cold scan's — so nothing here may
influence what lands in kg.json, only how much work it took to get there.

A cached record is replayed only when two independent gates agree:

1. **git has not flagged the path.** `git diff` between the cached commit and
   HEAD says what changed in history; `git status` says what is dirty,
   staged, or untracked right now. Neither alone is enough.
2. **the file still looks the same.** Its index blob sha matches, or failing
   that its size and mtime match what was recorded. This gate is what covers
   the files git cannot speak for — anything inside `.gitignore` that repokg
   still walks, submodule contents, a repo that is not a git checkout at all.

Both gates fail open (towards re-parsing), so the ways this can go wrong all
cost time rather than correctness. The one exception is inherited from every
stat-based build tool: a file rewritten with its size and mtime preserved is
invisible to git and to the stat gate alike. `--no-cache` is the escape hatch.
"""

import json
import os
import subprocess

from .facts import FACTS_VERSION

CACHE_FILE = "cache.json"
# Bumped when the document layout changes; older documents are then discarded.
CACHE_VERSION = 1


def open_(repo, out, head, enabled=True):
    """Return (Cache or None, reason). None only for `--no-cache`.

    A cache that cannot be replayed still collects this scan's facts so the
    next one can be warm — it just starts empty. `reason` is a short phrase
    that `scan` prints, so a cold scan never happens silently.
    """
    if not enabled:
        return None, "disabled (--no-cache)"
    doc, reason = _load(os.path.join(out, CACHE_FILE))
    changed = None
    if doc is not None:
        changed = _changed(repo, doc.get("head") or "", head)
        if changed is None:
            reason = ("git cannot bound what changed since %s"
                      % _short(doc.get("head") or ""))
    if changed is None:
        return Cache(repo, {}, frozenset(), _blobs(repo)), reason
    return Cache(repo, doc["files"], changed, _blobs(repo)), "warm"


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None, "no cache yet"
    except (OSError, ValueError):
        return None, "cache unreadable"
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), dict):
        return None, "cache malformed"
    if (doc.get("cache_version") != CACHE_VERSION
            or doc.get("facts_version") != FACTS_VERSION):
        return None, "cache written by a different repokg"
    return doc, "warm"


class Cache:
    """Facts from the previous scan, replayed for files that have not moved.

    Entries are carried into a fresh document as they are used, so files that
    were deleted or excluded since the last scan drop out on their own.
    """

    def __init__(self, repo, old, changed, blobs):
        self.repo = repo
        self._old = old
        self._changed = changed
        self._blobs = blobs
        self._new = {}
        self.hits = 0
        self.misses = 0

    def replay(self, key):
        """The cached record for `key`, or None if it must be re-extracted."""
        entry = self._old.get(key)
        if not isinstance(entry, dict) or key in self._changed:
            return None
        st = self._stat(key)
        if st is None:
            return None
        rec = entry.get("facts")
        blob = self._blobs.get(key)
        same = ((blob is not None and entry.get("blob") == blob)
                or (entry.get("size") == st[0]
                    and entry.get("mtime_ns") == st[1]))
        if not same or not isinstance(rec, dict):
            return None
        self._new[key] = _entry(st, blob, rec)
        self.hits += 1
        return rec

    def record(self, key, rec):
        """Store a freshly extracted record for the next scan."""
        self.misses += 1
        st = self._stat(key)
        if st is not None:
            self._new[key] = _entry(st, self._blobs.get(key), rec)

    def save(self, out, head):
        """Write the document. Failure to write is not a scan failure."""
        doc = {"cache_version": CACHE_VERSION, "facts_version": FACTS_VERSION,
               "head": head, "files": self._new}
        return _save(out, doc)

    def _stat(self, key):
        try:
            st = os.stat(os.path.join(self.repo, key.replace("/", os.sep)))
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns


def _entry(st, blob, rec):
    return {"size": st[0], "mtime_ns": st[1], "blob": blob, "facts": rec}


def _save(out, doc):
    """Write `doc` to <out>/cache.json, replacing any previous one.

    Written compactly and via a temporary file: cache.json is machine-only and
    the largest thing repokg writes, and a scan interrupted mid-write must not
    leave a half-document behind for the next one to trip over.
    """
    path = os.path.join(out, CACHE_FILE)
    tmp = path + ".tmp"
    try:
        os.makedirs(out, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


# -- git plumbing ------------------------------------------------------------

def _git(repo, *args):
    """stdout of a git command as text, or None if git could not answer.

    Output is read as bytes and decoded with surrogateescape, because `-z`
    output is NUL-separated and paths need not be valid UTF-8; this matches
    how os.walk hands us the same names.
    """
    try:
        p = subprocess.run(["git", *args], cwd=repo, capture_output=True)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "surrogateescape")


def _changed(repo, cached_head, head):
    """Repo-relative paths that may differ from the cached scan, or None if
    git could not answer (the caller then scans cold).

    Renames and copies contribute both sides. Adding a path that did not
    really change only costs a re-parse, so every ambiguity resolves that way.
    """
    changed = set()
    if cached_head != head:
        # One side without a commit (a repo that just got its first, or lost
        # the cached one to a rebase) leaves history unbounded: scan cold.
        if not cached_head or not head:
            return None
        out = _git(repo, "diff", "--name-status", "-z", cached_head, head)
        if out is None:
            return None
        changed |= _parse_diff(out)
    # -uall lists untracked files individually; without it an untracked
    # directory is reported as one entry and the files inside it are invisible.
    out = _git(repo, "status", "--porcelain", "-z", "-uall")
    if out is None:
        return None
    return changed | _parse_status(out)


def _parse_diff(out):
    """Paths out of `git diff --name-status -z`: a status token followed by
    one path, or two for a rename/copy."""
    paths = set()
    toks = out.split("\0")
    i = 0
    while i < len(toks):
        status = toks[i]
        i += 1
        if not status:
            continue
        n = 2 if status[0] in "RC" else 1
        paths.update(p for p in toks[i:i + n] if p)
        i += n
    return paths


def _parse_status(out):
    """Paths out of `git status --porcelain -z`: 'XY path' entries, with the
    other side of a rename or copy following as its own record."""
    paths = set()
    toks = [t for t in out.split("\0") if t]
    i = 0
    while i < len(toks):
        entry = toks[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if set(entry[:2]) & set("RC") and i < len(toks):
            paths.add(toks[i])
            i += 1
    return paths


def _blobs(repo):
    """{path: index blob sha} from one index dump, or {} if git cannot answer.

    Lets a file whose mtime moved but whose content did not — a branch switch
    and back, a stash round trip — stay a cache hit. Only trustworthy in
    combination with `git status`, which is what flags the paths whose working
    tree has drifted from the index.
    """
    out = _git(repo, "ls-files", "-s", "-z")
    if out is None:
        return {}
    blobs = {}
    for entry in out.split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split(" ")
        if path and len(parts) == 3:
            blobs[path] = parts[1]
    return blobs


def _short(sha):
    return sha[:12] if sha else "(unknown)"
