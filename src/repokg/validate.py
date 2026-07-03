"""Schema validation for .repokg/narratives.json (agent-written enrichment).

Agents occasionally emit shape-mismatched JSON; rendering it silently produces
a half-broken document. Validate loudly instead, with errors precise enough
for the agent to self-correct.
"""

ALLOWED_KEYS = {"overview", "modules", "flows", "timeline", "gotchas", "sections"}


def narratives(n):
    """Return a list of error strings; empty list means valid."""
    errs = []
    if not isinstance(n, dict):
        return ["narratives.json must be a JSON object, got %s" % type(n).__name__]

    for key in n:
        if key not in ALLOWED_KEYS:
            errs.append("unknown key %r (allowed: %s)"
                        % (key, ", ".join(sorted(ALLOWED_KEYS))))

    if "overview" in n and not isinstance(n["overview"], str):
        errs.append("overview must be a string")

    mods = n.get("modules", {})
    if not isinstance(mods, dict):
        errs.append("modules must be an object of {path: purpose}")
    else:
        for k, v in mods.items():
            if not isinstance(v, str) or not v.strip():
                errs.append("modules[%r] must be a non-empty string" % k)

    flows = n.get("flows", [])
    if not isinstance(flows, list):
        errs.append("flows must be a list")
    else:
        for i, fl in enumerate(flows):
            if not isinstance(fl, dict) or not isinstance(fl.get("name"), str):
                errs.append("flows[%d] must be {name: str, steps: [str]}" % i)
            elif not isinstance(fl.get("steps"), list) or \
                    not all(isinstance(s, str) for s in fl["steps"]):
                errs.append("flows[%d].steps must be a list of strings" % i)

    timeline = n.get("timeline", [])
    if not isinstance(timeline, list):
        errs.append("timeline must be a list")
    else:
        for i, era in enumerate(timeline):
            if not isinstance(era, dict) or not isinstance(era.get("period"), str) \
                    or not isinstance(era.get("theme"), str):
                errs.append("timeline[%d] must be {period: str, theme: str}" % i)

    gotchas = n.get("gotchas", [])
    if not isinstance(gotchas, list) or not all(isinstance(g, str) for g in gotchas):
        errs.append("gotchas must be a list of strings")

    sections = n.get("sections", [])
    if not isinstance(sections, list):
        errs.append("sections must be a list")
    else:
        for i, s in enumerate(sections):
            if not isinstance(s, dict) or not isinstance(s.get("title"), str) \
                    or not isinstance(s.get("body"), str):
                errs.append("sections[%d] must be {title: str, body: str (markdown)}" % i)

    return errs
