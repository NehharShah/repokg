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
from collections import Counter

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

# Renderers cap each section. Moving a top-level directory relocates every
# module under it, and a thousand-line wall in a terminal or a PR comment is
# not a report — the tail is always counted out loud rather than dropped, and
# `--format json` is never capped.
MAX_ROWS = 40

# Order the report reads in: what the architecture is, then what surrounds it.
ORDER = ("modules", "edges", "languages", "ops", "branches", "prs")

# Rename detection is quadratic in the candidates, and a repo-wide
# reorganisation is both the case that blows the pair count up and the case
# where pairing is least trustworthy. Past this many pairs it is skipped and
# said so, rather than run slowly to produce guesses.
MAX_RENAME_PAIRS = 10000

# Confidence tiers for a rename, strongest first. Each is a rule, not a score:
# a tuned similarity threshold is not something anyone could defend in review,
# and these can each be stated in a sentence.
RENAME_TIERS = ("high", "medium", "low")

_DIGITS_RE = re.compile(r"(\d+)")


def build(old, new, detect_renames=True):
    """Return the delta taking knowledge graph `old` to `new`.

    Both arguments are kg.json documents as dicts. Neither is mutated, and
    nothing outside them is consulted.

    Top-level sections are always present even when empty, so a consumer can
    index them without guarding. `ops` is the exception: its keys vary by repo
    and most are empty in any given one, so only those that moved appear.

    `detect_renames` pairs removed modules with added ones — see `_renames`.
    It only ever adds a `modules.renamed` list; the paired modules stay in
    `added` and `removed`, so turning it off changes what the report says and
    nothing about what the diff found.
    """
    notes = []
    delta = {"old": _provenance(old), "new": _provenance(new), "notes": notes}
    for name, identity, fields in SECTIONS:
        delta[name] = _section(old, new, name, identity, fields, notes)
    delta["ops"] = _ops(old, new, notes)
    delta["modules"]["renamed"] = \
        _renames(old, new, delta, notes) if detect_renames else []
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
    """True if the graph's shape moved, not merely its measurements."""
    return bool(shape_reasons(delta))


def shape_reasons(delta):
    """The sections that moved the graph's shape, in report order.

    Membership of the SHAPE sections, plus the field movements named in
    SHAPE_FIELDS. LOC drift, edge weight, branch tips and PR states are all
    excluded: they move on essentially every commit, so an exit code that
    tracked them would carry no information. Returned rather than reduced to a
    boolean so the report can say *why* it is exiting non-zero.
    """
    reasons = []
    for name in SHAPE:
        section = delta.get(name) or {}
        if name == "ops":
            if any(sub.get("added") or sub.get("removed")
                   for sub in section.values()):
                reasons.append(name)
            continue
        watched = SHAPE_FIELDS.get(name, ())
        if (section.get("added") or section.get("removed")
                or any(f in entry["before"]
                       for entry in section.get("changed", ())
                       for f in watched)):
            reasons.append(name)
    return [name for name in ORDER if name in reasons]


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


# -- rename detection --------------------------------------------------------
#
# The one heuristic in the diff, and the only thing here that can be wrong
# rather than merely incomplete. A module is keyed by its path, so moving one
# reads as a removal plus an addition; recovering the pairing is guesswork and
# is treated as such — every rename carries the confidence tier it was matched
# at and the evidence for it, and an ambiguous pairing is not reported at all.
#
# The evidence is the *import neighbourhood*, not size similarity. A renamed
# module keeps its dependencies: everything it imported still imports, and
# everything that imported it still does. Two modules having a similar line
# count is a coincidence waiting to happen, and pairing on it would be exactly
# the sort of guessed fact this project refuses to emit.

def _renames(old, new, delta, notes):
    """Pair removed modules with added ones where the evidence supports it.

    Returns [{from, to, confidence, evidence}], sorted by source path.

    A pairing is accepted only when it is unambiguous: the two must be each
    other's sole candidate at their tier. A module split in two, or two
    modules that both look like the same move, produce no rename rather than
    an arbitrary pick — a wrong pairing would claim a dependency survived a
    refactor when it did not, which is worse than saying nothing.
    """
    removed = [m for m in delta["modules"]["removed"] if isinstance(m, dict)]
    added = [m for m in delta["modules"]["added"] if isinstance(m, dict)]
    if not removed or not added:
        return []
    if len(removed) * len(added) > MAX_RENAME_PAIRS:
        notes.append(
            "%d removed and %d added modules is too many to pair renames "
            "across (%d combinations, limit %d), so they are reported as "
            "plain additions and removals; a reorganisation this large is "
            "also where pairing is least trustworthy."
            % (len(removed), len(added), len(removed) * len(added),
               MAX_RENAME_PAIRS))
        return []

    stable = _module_paths(old) & _module_paths(new)
    old_nbrs = _neighbours(old, stable)
    new_nbrs = _neighbours(new, stable)

    # tier -> [(removed module, added module, evidence)]
    by_tier = {tier: [] for tier in RENAME_TIERS}
    for r in removed:
        for a in added:
            tier, evidence = _rename_tier(
                r, a, old_nbrs.get(r.get("path"), frozenset()),
                new_nbrs.get(a.get("path"), frozenset()))
            if tier:
                by_tier[tier].append((r, a, evidence))

    out, taken_from, taken_to = [], set(), set()
    for tier in RENAME_TIERS:  # strongest evidence claims its pairs first
        pairs = [(r, a, ev) for r, a, ev in by_tier[tier]
                 if r["path"] not in taken_from and a["path"] not in taken_to]
        froms = Counter(r["path"] for r, _, _ in pairs)
        tos = Counter(a["path"] for _, a, _ in pairs)
        for r, a, evidence in pairs:
            # sole candidate in both directions, or it stays unreported
            if froms[r["path"]] == 1 and tos[a["path"]] == 1:
                out.append({"from": r["path"], "to": a["path"],
                            "confidence": tier, "evidence": evidence})
                taken_from.add(r["path"])
                taken_to.add(a["path"])
    return sorted(out, key=lambda p: _sort_key((p["from"],)))


def _rename_tier(r, a, old_nbrs, new_nbrs):
    """(tier, evidence) for pairing removed module `r` with added `a`.

    Same language throughout: a Python package does not become a Go one by
    being moved, and allowing it would open the door to pairing on size alone.
    """
    if r.get("lang") != a.get("lang"):
        return None, []
    same_name = _basename(r.get("path")) == _basename(a.get("path"))
    shared, union = len(old_nbrs & new_nbrs), len(old_nbrs | new_nbrs)
    kept = ("%d of %d imports across unmoved modules preserved"
            % (shared, union)) if union else None

    if union and shared == union:
        # every dependency to and from the unmoved parts of the graph
        # survived: this is the signature of a move, not a coincidence
        return "high", _evidence(kept, same_name and "same directory name")
    if same_name and (not union or shared):
        return "medium", _evidence("same directory name", kept)
    if union and shared * 2 >= union:
        return "medium", _evidence(kept)
    if same_name:
        # nothing but the name, which two unrelated packages can share
        return "low", _evidence("same directory name",
                                "no shared imports to corroborate it")
    return None, []


def _evidence(*parts):
    return [p for p in parts if p]


def _neighbours(kg, stable):
    """{module: {(direction, other module, lang)}}, over unmoved modules only.

    Restricted to the stable core deliberately. A candidate's neighbours may
    themselves be candidates, and resolving those would require the answer
    this is being used to compute.
    """
    nbrs = {}
    for e in kg.get("edges") or ():
        if not isinstance(e, dict):
            continue
        src, dst, lang = e.get("from"), e.get("to"), e.get("lang")
        if src is None or dst is None:
            continue
        if dst in stable:
            nbrs.setdefault(src, set()).add(("out", dst, lang))
        if src in stable:
            nbrs.setdefault(dst, set()).add(("in", src, lang))
    return nbrs


def _module_paths(kg):
    return set(m["path"] for m in (kg.get("modules") or ())
               if isinstance(m, dict) and "path" in m)


def _basename(path):
    return str(path or "").rstrip("/").rsplit("/", 1)[-1]


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


# -- reports -----------------------------------------------------------------
#
# Two renderings of one row set. Added, removed and changed rows are built
# from the same identity the diff keyed on, so a module names itself the same
# way whichever of the three it lands in.

_NORMALISERS = {name: _sectioned(identity, fields)
                for name, identity, fields in SECTIONS}

# How a section's identity tuple reads as a subject.
_SUBJECT = {
    "edges": lambda i: "%s -> %s (%s)" % (i[0], i[1], i[2]) if len(i) > 2
    else " -> ".join(i),
    "prs": lambda i: "#" + i[0],
}

_MARKS = (("+", "added"), ("-", "removed"), ("~", "changed"))
_MD_MARKS = {"R": "**R**", "+": "**+**", "-": "**−**", "~": "**~**"}
_MARK_ORDER = ("R", "+", "-", "~")


def render_text(delta):
    """Plain-text report, in the shape `repokg audit` already prints."""
    out = []
    w = out.append
    w(_header(delta, "->"))
    w("")
    for name, section in _report_sections(delta):
        rows = _rows(name, section, "->")
        w("%s  %s" % (name, _tally(rows)))
        for mark, subject, detail in rows[:MAX_ROWS]:
            w(("  %s %-44s %s" % (mark, subject, detail)).rstrip())
        if len(rows) > MAX_ROWS:
            w("  ... and %d more (--format json for all of them)"
              % (len(rows) - MAX_ROWS))
        w("")
    w(_verdict(delta))
    if delta.get("notes"):
        w("")
        w("Notes:")
        for note in delta["notes"]:
            w("  - %s" % note)
    return "\n".join(out).rstrip()


def render_markdown(delta):
    """Markdown for a PR comment, which is the form the GitHub Action in #7
    would post and the reason `--format md` exists at all."""
    out = []
    w = out.append
    w("## Knowledge graph diff")
    w("")
    w(_header(delta, "→"))
    w("")
    for name, section in _report_sections(delta):
        rows = _rows(name, section, "→")
        w("### %s %s" % (name, _tally(rows)))
        w("")
        for mark, subject, detail in rows[:MAX_ROWS]:
            w("- %s `%s`%s" % (_MD_MARKS[mark], subject,
                               (" — " + detail) if detail else ""))
        if len(rows) > MAX_ROWS:
            w("- … and %d more" % (len(rows) - MAX_ROWS))
        w("")
    w("**%s**" % _verdict(delta))
    if delta.get("notes"):
        w("")
        for note in delta["notes"]:
            w("> %s" % note)
    return "\n".join(out).rstrip() + "\n"


def _header(delta, arrow):
    """Which two graphs these were.

    Stated as fact and nothing more. Two graphs of the same commit is the
    ordinary case — a stored graph against a fresh scan of an uncommitted
    working tree — so it is not worth a warning.
    """
    old, new = delta.get("old") or {}, delta.get("new") or {}
    if old.get("head") and old.get("head") == new.get("head"):
        return "comparing two graphs of %s (the same commit)" \
            % _short(old.get("head"))
    return "comparing %s (%s) %s %s (%s)" % (
        _short(old.get("head")), old.get("generated_at") or "?", arrow,
        _short(new.get("head")), new.get("generated_at") or "?")


def _verdict(delta):
    """The bottom line, naming the exit code so it is discoverable without
    reading the docs."""
    if not any_change(delta):
        return "no change (exit 0)"
    reasons = shape_reasons(delta)
    if reasons:
        return "shape changed: %s (exit 1)" % ", ".join(reasons)
    return ("measurements moved but the shape did not: %s (exit 0)"
            % ", ".join(sorted(counts(delta))))


def _report_sections(delta):
    """(label, section) for everything that moved, ops flattened per key."""
    for name in ORDER:
        if name == "ops":
            for key, sub in sorted((delta.get("ops") or {}).items()):
                yield "ops." + key, sub
            continue
        section = delta.get(name) or {}
        if section.get("added") or section.get("removed") \
                or section.get("changed"):
            yield name, section


def _tally(rows):
    """Counts derived from the rows actually printed, so the two agree even
    though a rename suppresses the addition and removal it is made of."""
    counts = Counter(mark for mark, _, _ in rows)
    return " ".join("%s%d" % (mark, counts[mark])
                    for mark in _MARK_ORDER if counts[mark])


def _rows(name, section, arrow):
    """(mark, subject, detail) per moved record, renames first.

    A renamed module prints once, as a rename; the addition and removal it is
    made of are suppressed. The delta keeps all three, so a consumer that
    distrusts the heuristic can ignore `renamed` and see exactly what it would
    have seen without it — but printing one move three ways would obscure it.
    """
    rows, paired = [], {"+": set(), "-": set()}
    for pair in section.get("renamed", ()):
        paired["-"].add(pair["from"])
        paired["+"].add(pair["to"])
        rows.append(("R", "%s %s %s" % (pair["from"], arrow, pair["to"]),
                     _bits(pair["confidence"],
                           "; ".join(pair.get("evidence") or ()))))
    for mark, key in _MARKS:
        for item in section.get(key, ()):
            if mark == "~":
                rows.append((mark, _subject(name, item["id"]),
                             _movement(item, arrow)))
                continue
            identity = _identity(name, item)
            if identity and identity[0] in paired[mark]:
                continue
            rows.append((mark, _subject(name, identity),
                         _detail(name, item)))
    return rows


def _identity(name, record):
    norm = _NORMALISERS.get(name, _ops_record)
    entry = norm(record)
    return entry[0] if entry else ("?",)


def _subject(name, identity):
    identity = [str(part) for part in identity] or ["?"]
    return _SUBJECT.get(name, lambda i: i[0])(identity)


def _detail(name, record):
    """The measurements worth showing beside an added or removed record."""
    if not isinstance(record, dict):
        return ""
    if name == "modules":
        return _bits(record.get("lang"), _count(record.get("files"), "file"),
                     _count(record.get("loc"), "line"),
                     "generated" if record.get("generated") else None)
    if name == "edges":
        return _bits(_count(record.get("count"), "import"))
    if name == "languages":
        return _bits(_count(record.get("files"), "file"),
                     _count(record.get("loc"), "line"))
    if name == "branches":
        return _bits(record.get("status"))
    if name == "prs":
        return _bits(record.get("state"), record.get("title"))
    entry = _ops_record(record)  # ops.*, whose extra fields vary by key
    return _bits(*[_value(v) for v in (entry[2] if entry else {}).values()])


def _movement(entry, arrow):
    fields = sorted(set(entry.get("before") or {}) | set(entry.get("after")
                                                         or {}))
    return ", ".join(
        "%s %s %s %s" % (field, _value((entry.get("before") or {}).get(field)),
                         arrow, _value((entry.get("after") or {}).get(field)))
        for field in fields)


def _bits(*parts):
    return ", ".join(str(p) for p in parts if p)


def _count(n, noun):
    if not isinstance(n, int) or isinstance(n, bool):
        return None
    return "%d %s%s" % (n, noun, "" if n == 1 else "s")


def _value(v):
    """A field value as report text, with long lists cut short — a config
    dir's `entries` runs to thirty names and would swamp the line."""
    if v is None:
        return "(none)"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        if not v:
            return "(empty)"
        head = ", ".join(str(x) for x in v[:5])
        return head if len(v) <= 5 else "%s +%d more" % (head, len(v) - 5)
    return str(v)


def _short(sha):
    return (sha or "")[:12] or "(unknown)"
