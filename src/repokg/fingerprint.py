"""Content fingerprints for enriched modules: the lock file behind staleness.

Structure refreshes on every scan; a module's purpose line does not. To tell
when a narrative has outlived the code it describes, `render` records what each
enriched module contained at the moment its narrative was written, in
`<out>/narratives.lock.json`. The lock is kept apart from `narratives.json` so
the agent-authored file stays agent-authored and the lock never needs a human.

A module's files are the code files directly inside its directory — the same
set `code.collect` counts as the module's `files` — each identified by its git
blob sha (`git hash-object`, so the working tree is measured, uncommitted edits
included, and line-ending filters apply the way they do for a commit). The
per-file map is stored next to the fingerprint because a bare hash can say
*that* a module changed but not *how much*, and "12 of 40 files" is what a
threshold has to be applied to.

Re-rendering must not launder staleness. `generate` runs render on every
refresh, so a lock that was simply re-stamped each time would report every
narrative as fresh the moment anyone refreshed the graph. A module's stamp is
therefore only replaced when its narrative text changed — the agent rewrote
it, which is the act the stamp certifies — or when it has none yet.
"""

import hashlib
import json
import os

from . import code, facts, gitinfo

LOCK_NAME = "narratives.lock.json"
LOCK_VERSION = 1


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(files):
    """sha256 over a module's {file name: blob sha}, independent of order."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(("%s\0%s\n" % (name, files[name])).encode("utf-8"))
    return h.hexdigest()


def module_files(repo, mod_path, exclude=()):
    """Names of the code files `code.collect` counts for this module."""
    rel = "" if mod_path == "(root)" else mod_path
    try:
        entries = sorted(os.listdir(os.path.join(repo, rel)))
    except OSError:
        return []
    names = []
    for name in entries:
        key = (rel + "/" + name) if rel else name
        if not os.path.isfile(os.path.join(repo, key)):
            continue
        lang = facts.LANG_BY_EXT.get(os.path.splitext(name)[1].lower())
        if lang not in code.MODULE_LANGS or code._excluded(key, exclude):
            continue
        names.append(name)
    return names


def blob_shas(repo, keys):
    """{repo-relative path: git blob sha of its working-tree content}."""
    if not keys:
        return {}
    plain = [k for k in keys if "\n" not in k]
    shas = {}
    if plain:
        out = gitinfo.run(repo, "hash-object", "--stdin-paths",
                          stdin="\n".join(plain) + "\n")
        shas.update(zip(plain, out.splitlines()))
    # --stdin-paths is line-delimited, so a path containing a newline cannot
    # go through it; it is hashed alone rather than left out of the lock
    for k in keys:
        if "\n" in k:
            shas[k] = gitinfo.run(repo, "hash-object", "--", k)
    return shas


def snapshot(repo, mod_paths, exclude=()):
    """{module path: {"fingerprint", "files": {name: blob sha}}} for each path
    that is a directory with at least one code file."""
    listing = {}
    for path in mod_paths:
        rel = "" if path == "(root)" else path
        names = module_files(repo, path, exclude)
        if names:
            listing[path] = [(n, (rel + "/" + n) if rel else n) for n in names]
    shas = blob_shas(repo, [k for pairs in listing.values() for _, k in pairs])
    snap = {}
    for path, pairs in listing.items():
        files = {n: shas[k] for n, k in pairs}
        snap[path] = {"fingerprint": fingerprint(files), "files": files}
    return snap


def read_lock(out):
    """The lock's {module: entry} map, or ({}, reason) if absent or unusable.

    An unreadable lock is treated as no lock rather than an error: it is
    machine-written, so the fix is to write it again, not to ask anyone to
    repair it.
    """
    path = os.path.join(out, LOCK_NAME)
    if not os.path.isfile(path):
        return {}, ""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        return {}, "%s unreadable (%s); rewriting it" % (LOCK_NAME, e)
    mods = doc.get("modules") if isinstance(doc, dict) else None
    if (not isinstance(mods, dict)
            or doc.get("lockfile_version") != LOCK_VERSION):
        return {}, "%s not in a format this repokg reads; rewriting it" % LOCK_NAME
    return {k: v for k, v in mods.items() if isinstance(v, dict)}, ""


def stamp(repo, out, kg, narratives):
    """Write <out>/narratives.lock.json for the modules `narratives` describes.

    Returns (summary, notes): `summary` is {"stamped": [...], "kept": [...],
    "untracked": [...]} and `notes` are lines for the caller to print.
    Nothing is written when no narrative describes a module.
    """
    described = narratives.get("modules") or {}
    summary = {"stamped": [], "kept": [], "untracked": []}
    notes = []
    if not described:
        return summary, notes

    known = {m["path"] for m in kg.get("modules", [])}
    exclude = (kg.get("exclude") or {}).get("patterns") or ()
    snap = snapshot(repo, sorted(p for p in described if p in known), exclude)
    summary["untracked"] = sorted(p for p in described if p not in snap)

    previous, note = read_lock(out)
    if note:
        notes.append(note)
    entries = {}
    for path, now in sorted(snap.items()):
        text_sha = _sha256(described[path])
        old = previous.get(path)
        if old and old.get("narrative") == text_sha:
            entries[path] = old
            summary["kept"].append(path)
        else:
            entries[path] = dict(now, narrative=text_sha)
            summary["stamped"].append(path)

    doc = json.dumps({"lockfile_version": LOCK_VERSION, "modules": entries},
                     indent=1, sort_keys=True) + "\n"
    os.makedirs(out, exist_ok=True)
    lock = os.path.join(out, LOCK_NAME)
    try:
        with open(lock, encoding="utf-8") as f:
            unchanged = f.read() == doc
    except OSError:
        unchanged = False
    if not unchanged:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(doc)
    return summary, notes
