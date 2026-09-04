"""Structural diff between two knowledge graphs.

`repokg check` answers "is the graph stale?". This answers the question after
it — what actually changed — which is the primitive that PR review and release
notes both want. "This adds an import edge from api/ to billing/" is exactly
the signal a reviewer misses in a raw file diff.

The comparison is a keyed set difference and nothing cleverer. Every section
of kg.json is a list of records with a natural identity: a module is its path,
an import edge is its (from, to, lang) triple, a branch is its name. So each
section reduces to what was added, what was removed, and which fields moved on
the records present in both. Identity is never guessed — a module that moved
reads as a removal plus an addition, and recovering the rename from that is a
heuristic that has to register a finding, so it lives elsewhere.

Two rules keep the answer honest rather than merely complete.

*Not everything that differs is worth reporting.* A branch's tip, date and
`ahead` count move whenever anyone pushes, so comparing them would bury the
transitions that matter — active becoming merged, a branch going stale — under
noise from unrelated work. Only the fields listed per section are compared,
and the omissions are the point.

*A section missing from one side is skipped, with a note.* A document written
before `languages` existed does not mean every language was just added, and
reporting it that way would turn a version gap into a fake architectural
event.

Separately, `shape_changed` marks the subset of changes that alter the graph's
shape rather than its measurements — see SHAPE. It is what the CLI's exit code
keys off, and the distinction exists because a gate that tripped on every
commit would be switched off within a week.

Nothing here touches the filesystem or git: `build` is a pure function of two
dicts, so a diff stays reproducible from two saved documents forever.
"""

import re

# Sections that diff as keyed record lists: the fields identifying a record,
# then the fields whose movement is reported. Anything unlisted is ignored on
# purpose — see the module docstring.
SECTIONS = (
    ("modules", ("path",), ("lang", "files", "loc", "root", "generated")),
    ("edges", ("from", "to", "lang"), ("count",)),
    ("languages", ("lang",), ("files", "loc")),
    ("branches", ("name",), ("status",)),
    ("prs", ("number",), ("state",)),
)

# Sections whose membership *is* the graph's shape. Gaining a module or an
# import edge is an architectural event; a branch moving is not, and nor is a
# module gaining twenty lines. Both are still reported, neither counts here.
SHAPE = ("modules", "edges", "languages", "ops")

# Field movements that count as shape as well: a module switching primary
# language is a real architectural event, its LOC drifting is not. Ops records
# are deliberately absent — which workflows and charts exist is surface, the
# `entries` listing inside a config dir is detail.
SHAPE_FIELDS = {"modules": ("lang",)}

# An ops record is a bare path under most keys, and a dict under workflows
# ({file, name}) and config_dirs ({dir, entries}). Whichever of these a record
# carries identifies it, and everything else it carries is compared.
OPS_IDENTITY = ("file", "dir", "path", "name")

_DIGITS_RE = re.compile(r"(\d+)")


def build(old, new):
    """Return the delta taking knowledge graph `old` to `new`.

    Both arguments are kg.json documents as dicts. Neither is mutated, and
    nothing outside them is consulted.

    Top-level sections are always present even when empty, so a consumer can
    index them without guarding. `ops` is the exception: its keys vary by repo
    and most are empty in any given one, so only those that moved appear.
    """
    notes = []
    delta = {"old": _provenance(old), "new": _provenance(new), "notes": notes}
    for name, identity, fields in SECTIONS:
        delta[name] = _section(old, new, name, identity, fields, notes)
    delta["ops"] = _ops(old, new, notes)
    ov, nv = old.get("repokg_version"), new.get("repokg_version")
    if ov != nv:
        notes.append(
            "documents were written against different graph schemas (%s vs "
            "%s); a section whose record shape changed between them diffs as "
            "a wholesale replacement rather than as field movement."
            % (ov, nv))
    delta["shape_changed"] = shape_changed(delta)
    return delta


def shape_changed(delta):
    """True if the graph's shape moved, not merely its measurements.

    Membership of the SHAPE sections, plus the field movements named in
    SHAPE_FIELDS. LOC drift, edge weight, branch tips and PR states are all
    excluded: they move on essentially every commit, so an exit code that
    tracked them would carry no information.
    """
    for name in SHAPE:
        section = delta.get(name) or {}
        if name == "ops":
            if any(sub.get("added") or sub.get("removed")
                   for sub in section.values()):
                return True
            continue
        if section.get("added") or section.get("removed"):
            return True
        watched = SHAPE_FIELDS.get(name, ())
        for entry in section.get("changed", ()):
            if any(f in entry["before"] for f in watched):
                return True
    return False


def any_change(delta):
    """True if anything at all differs, shape or measurement.

    What decides whether a diff has something to print, as against
    `shape_changed`, which decides the exit code.
    """
    for name, _, _ in SECTIONS:
        section = delta.get(name) or {}
        if (section.get("added") or section.get("removed")
                or section.get("changed")):
            return True
    return bool(delta.get("ops"))


def counts(delta):
    """{section: (added, removed, changed)} for the sections that moved.

    Ops is summed across its keys, since "3 ops entries added" is the right
    granularity for a summary line and the detail is in the delta already.
    """
    out = {}
    for name, _, _ in SECTIONS:
        section = delta.get(name) or {}
        triple = (len(section.get("added", ())),
                  len(section.get("removed", ())),
                  len(section.get("changed", ())))
        if any(triple):
            out[name] = triple
    ops = delta.get("ops") or {}
    if ops:
        out["ops"] = tuple(sum(len(sub.get(k, ())) for sub in ops.values())
                           for k in ("added", "removed", "changed"))
    return out


# -- sections ----------------------------------------------------------------

def _provenance(kg):
    """Which graph this side of the diff was, so a renderer can say so without
    being handed the documents as well."""
    repo = kg.get("repo") if isinstance(kg.get("repo"), dict) else {}
    return {"head": repo.get("head", ""),
            "generated_at": kg.get("generated_at", ""),
            "repokg_version": kg.get("repokg_version")}


def _section(old, new, name, identity, fields, notes):
    o, n = old.get(name), new.get(name)
    if not isinstance(o, list) or not isinstance(n, list):
        if isinstance(o, list) or isinstance(n, list):
            notes.append(
                "`%s` is recorded in only one of the two documents, so it was "
                "not compared; diffing it would report the whole section as "
                "added or removed, which is a version gap and not a change."
                % name)
        return _empty()
    return _keyed(o, n, _sectioned(identity, fields), notes, name,
                  ", ".join(identity))


def _ops(old, new, notes):
    o, n = old.get("ops"), new.get("ops")
    if not isinstance(o, dict) or not isinstance(n, dict):
        if isinstance(o, dict) or isinstance(n, dict):
            notes.append("`ops` is recorded in only one of the two documents, "
                         "so the ops surface was not compared.")
        return {}
    out, skipped = {}, []
    for key in sorted(set(o) | set(n)):
        ol, nl = o.get(key), n.get(key)
        if not isinstance(ol, list) or not isinstance(nl, list):
            skipped.append(key)
            continue
        sub = _keyed(ol, nl, _ops_record, notes, "ops." + key,
                     " or ".join(OPS_IDENTITY))
        if sub["added"] or sub["removed"] or sub["changed"]:
            out[key] = sub
    if skipped:
        notes.append("ops keys recorded by only one of the two documents were "
                     "not compared: %s." % ", ".join(skipped))
    return out


# -- keyed set difference ----------------------------------------------------

def _sectioned(identity, fields):
    """Record normaliser for a section: identity is every listed field, and
    the compared fields are fixed."""
    def norm(item):
        if not isinstance(item, dict) or any(f not in item for f in identity):
            return None
        return (tuple(str(item[f]) for f in identity), item,
                {f: item.get(f) for f in fields})
    return norm


def _ops_record(item):
    """Record normaliser for the ops surface.

    Identity is whichever of OPS_IDENTITY the record carries first, and every
    other field is compared — including `name`, which identifies some shapes
    but is a compared attribute of a workflow once `file` has identified it.
    """
    if not isinstance(item, dict):
        return (str(item),), item, {}  # bare path: it is its own identity
    for field in OPS_IDENTITY:
        if field in item:
            return ((str(item[field]),), item,
                    {k: v for k, v in item.items() if k != field})
    return None


def _keyed(old_items, new_items, norm, notes, label, identity_desc):
    """added/removed/changed for two lists of records sharing an identity."""
    o, o_dropped = _index(old_items, norm)
    n, n_dropped = _index(new_items, norm)
    if o_dropped or n_dropped:
        notes.append("%d %s record(s) could not be identified and were "
                     "skipped, having none of the fields the diff keys on "
                     "(%s)." % (o_dropped + n_dropped, label, identity_desc))
    added = [n[k][0] for k in sorted(set(n) - set(o), key=_sort_key)]
    removed = [o[k][0] for k in sorted(set(o) - set(n), key=_sort_key)]
    changed = []
    for k in sorted(set(o) & set(n), key=_sort_key):
        before, after = _compare(o[k][1], n[k][1])
        if before or after:
            changed.append({"id": list(k), "before": before, "after": after})
    return {"added": added, "removed": removed, "changed": changed}


def _index(items, norm):
    """{identity: (record, compared fields)}, and how many had no identity.

    Identity values are stringified so one section always sorts against one
    type, whatever a hand-edited or foreign document put in the field.
    """
    index, dropped = {}, 0
    for item in items if isinstance(items, list) else ():
        entry = norm(item)
        if entry is None:
            dropped += 1
        else:
            index[entry[0]] = (entry[1], entry[2])
    return index, dropped


def _compare(before_map, after_map):
    """The compared fields that differ, as ({field: before}, {field: after}).

    A field on one side only reports as None on the other, which is the
    truthful rendering of "that record did not carry this fact".
    """
    before, after = {}, {}
    for field in sorted(set(before_map) | set(after_map)):
        o, n = before_map.get(field), after_map.get(field)
        if o != n:
            before[field], after[field] = o, n
    return before, after


def _empty():
    return {"added": [], "removed": [], "changed": []}


def _sort_key(key):
    """Total order over identity tuples, with digit runs compared as numbers
    so pr 9 precedes pr 10 rather than following it."""
    return tuple(_natural(part) for part in key)


def _natural(text):
    return tuple((int(p), "") if p.isdigit() else (-1, p)
                 for p in _DIGITS_RE.split(text) if p != "")
