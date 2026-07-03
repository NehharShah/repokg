"""Ops-surface collectors: CI workflows, Docker, Helm, Makefile, configs, docs, tests."""

import os
import re

from .code import walk

WORKFLOW_NAME_RE = re.compile(r"^name:\s*['\"]?(.+?)['\"]?\s*$", re.M)
MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9][\w./-]*)\s*:(?!=)", re.M)


def collect(repo):
    ops = {
        "workflows": [], "dockerfiles": [], "compose": [], "helm_charts": [],
        "makefile_targets": [], "config_dirs": [], "docs": [], "test_dirs": [],
        "migration_dirs": [], "proto_dirs": [],
    }

    wf_dir = os.path.join(repo, ".github", "workflows")
    if os.path.isdir(wf_dir):
        for f in sorted(os.listdir(wf_dir)):
            if f.endswith((".yml", ".yaml")):
                m = WORKFLOW_NAME_RE.search(_read(os.path.join(wf_dir, f)))
                ops["workflows"].append(
                    {"file": ".github/workflows/" + f,
                     "name": m.group(1) if m else f})

    for rel, files in walk(repo):
        base = os.path.basename(rel)
        for f in files:
            p = (rel + "/" + f) if rel else f
            if f.startswith("Dockerfile"):
                ops["dockerfiles"].append(p)
            elif re.match(r"(docker-)?compose[^/]*\.ya?ml$", f):
                ops["compose"].append(p)
            elif f == "Chart.yaml":
                ops["helm_charts"].append(rel)
            elif f.endswith(".md") and (rel == "docs" or rel.startswith("docs/")):
                ops["docs"].append(p)
        if base in ("tests", "test", "e2e", "integration") and rel.count("/") <= 2:
            ops["test_dirs"].append(rel)
        if base == "migrations":
            ops["migration_dirs"].append(rel)
        if base in ("proto", "protos") and rel:
            ops["proto_dirs"].append(rel)

    mk = os.path.join(repo, "Makefile")
    if os.path.isfile(mk):
        seen = []
        for t in MAKE_TARGET_RE.findall(_read(mk)):
            if t not in seen and not t.startswith("."):
                seen.append(t)
        ops["makefile_targets"] = seen

    for d in ("configs", "config", "deploy", "deployments", "charts", "infra"):
        full = os.path.join(repo, d)
        if os.path.isdir(full):
            entries = sorted(os.listdir(full))[:30]
            ops["config_dirs"].append({"dir": d, "entries": entries})

    for k in ("dockerfiles", "compose", "helm_charts", "docs",
              "test_dirs", "migration_dirs", "proto_dirs"):
        ops[k] = sorted(set(ops[k]))
    return ops


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""
