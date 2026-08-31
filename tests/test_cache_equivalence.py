"""The cache's one hard invariant: a warm scan writes what a cold scan writes.

Everything else about the cache is a performance claim, and a wrong performance
claim costs time. This is the claim that costs correctness, so it is checked
against a repo carrying every language repokg understands, after each step of a
long mutation sequence rather than from a clean start each time — a cache that
is right once but accumulates drift over successive scans fails here and passes
the per-scenario tests in test_cache.py.

Most of the sequence runs at the cache layer. Languages, modules and edges are
the whole of what the cache can reach: branches, contributors and the ops
surface never ask the store for anything. That claim is worth more than the
assumption, so one test does compare complete kg.json documents.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from repokg import cache, code, deps, facts, gitinfo
from repokg.cli import main

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

# One repo, every extractor: Go module, Python src layout, TS with tsconfig
# aliases and npm workspaces, Rust workspace, Maven/Gradle Java + Kotlin.
FIXTURE = {
    ".gitignore": "gen/\n.repokg/\n",
    "go.mod": "module acme/svc\n",
    "cmd/api/main.go": ('package main\nimport (\n\t"fmt"\n'
                        '\t"acme/svc/internal/store"\n)\n'
                        "func main() { fmt.Println(store.X) }\n"),
    "internal/store/store.go": ('package store\n'
                                'import "acme/svc/internal/util"\n'
                                "var X = util.Y\n"),
    "internal/util/util.go": "package util\n\nvar Y = 1\n",
    "pyproject.toml": '[project]\nname = "acme"\n',
    "src/acme/__init__.py": "",
    "src/acme/app.py": "import os\nfrom . import models\n",
    "src/acme/models.py": "from acme import app\n",
    "tsconfig.json": ('{"compilerOptions": {"baseUrl": "./src",'
                      ' "paths": {"@/*": ["./*"]}}}'),
    "package.json": '{"name": "root", "workspaces": ["packages/*"]}',
    "src/web/index.ts": ("import {u} from './util';\n"
                         "import {v} from '@/web/util';\n"),
    "src/web/util.ts": "export const u = 1;\nexport const v = 2;\n",
    "packages/core/package.json": '{"name": "@acme/core"}',
    "packages/core/index.js": "const ui = require('@acme/ui');\n",
    "packages/ui/package.json": '{"name": "@acme/ui"}',
    "packages/ui/index.js": "module.exports = 1;\n",
    "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
    "crates/engine/Cargo.toml": '[package]\nname = "acme-engine"\n',
    "crates/engine/src/lib.rs": ("pub mod parts;\nuse crate::parts::gear;\n"
                                 "use acme_util::helper;\n"),
    "crates/engine/src/parts/mod.rs": "pub mod gear;\n",
    "crates/engine/src/parts/gear.rs": "pub fn spin() {}\n",
    "crates/util/Cargo.toml": '[package]\nname = "acme-util"\n',
    "crates/util/src/lib.rs": "pub fn helper() {}\n",
    "pom.xml": "<project/>\n",
    "core/pom.xml": "<project/>\n",
    "core/src/main/java/com/acme/core/Engine.java":
        "package com.acme.core;\nimport com.acme.core.util.Str;\n"
        "public class Engine {}\n",
    "core/src/main/java/com/acme/core/util/Str.java":
        "package com.acme.core.util;\npublic class Str {}\n",
    "api/build.gradle": "",
    "api/src/main/kotlin/Handlers.kt":
        "package com.acme.api\nimport com.acme.core.Engine\nfun handle() {}\n",
    "Makefile": "build:\n\tgo build ./...\n",
    "docs/design.md": "# Design\n",
    "configs/dev.yaml": "a: 1\n",
    "gen/ignored.py": "import os\n",
}


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, env=ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def commit(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


def copy_repo(src, dst):
    """Copy a git repo, skipping git's transient lock files.

    Background maintenance can create and remove .git/objects/*.lock while the
    copy is in flight, and a name that disappears between copytree listing a
    directory and reading it fails the whole copy.
    """
    shutil.copytree(src, dst, ignore=lambda d, names: [
        n for n in names if n.endswith(".lock")])


class EquivalenceCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.mkdtemp()
        for rel, text in FIXTURE.items():
            write(cls.template, rel, text)
        git(cls.template, "init", "-q", "-b", "main", ".")
        # no background repacking, so the template stops changing under the
        # per-test copies once it is built
        git(cls.template, "config", "maintenance.auto", "false")
        git(cls.template, "config", "gc.auto", "0")
        git(cls.template, "add", "-A")
        git(cls.template, "commit", "-qm", "base")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.template, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        copy_repo(self.template, self.repo)
        self.out = os.path.join(self.repo, ".repokg")
        # cold output goes outside the repo so it can never be scanned itself
        self.cold_out = os.path.join(self.tmp, "cold")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, no_cache=False):
        """Everything the cache can influence, without the git metadata and
        ops collection that never consult the store."""
        head = gitinfo.try_run(self.repo, "rev-parse", "HEAD")
        c, note = cache.open_(self.repo, self.out, head, not no_cache)
        store = facts.Store(self.repo, c)
        tree = dict(code.walk(self.repo))
        languages, modules = code.collect(self.repo, tree, store)
        edges = deps.collect(self.repo, tree, None, store)
        if c is not None:
            c.save(self.out, head)
        graph = json.dumps([languages, modules, edges], sort_keys=True,
                           indent=1)
        return note, store, c, graph

    def assert_equivalent(self, label):
        """A warm scan and a cold scan of the same tree agree exactly."""
        _, store, _, warm = self.scan()
        _, _, _, cold = self.scan(no_cache=True)
        self.assertEqual(warm, cold, "warm != cold after: %s" % label)
        return store.parses

    def cli(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = main(["scan", self.repo, "--no-github"] + list(extra))
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def kg(self, out):
        with open(os.path.join(out, "kg.json"), encoding="utf-8") as f:
            doc = json.load(f)
        # generated_at is a date, so it only differs if a run straddles
        # midnight; normalising it keeps that from looking like a cache bug.
        doc["generated_at"] = "(normalised)"
        return json.dumps(doc, indent=1, sort_keys=True)

    def doc(self):
        with open(os.path.join(self.out, cache.CACHE_FILE),
                  encoding="utf-8") as f:
            return json.load(f)


class TestFullDocumentEquivalence(EquivalenceCase):
    """The complete artifact, not just the part the cache is known to reach.

    Everything else here trusts that the cache cannot influence branches,
    contributors or the ops surface. These two tests are what earn that trust,
    and would fail if some future collector started reading through the store
    without the cache being taught about it.
    """

    def test_untouched_repo_matches_cold_byte_for_byte(self):
        self.cli()
        report = self.cli()
        self.assertIn("parsed 0", report)
        warm = self.kg(self.out)
        self.cli("--no-cache", "--out", self.cold_out)
        self.assertEqual(warm, self.kg(self.cold_out))

    def test_mutated_repo_matches_cold_byte_for_byte(self):
        self.cli()
        write(self.repo, "src/acme/extra.py", "from acme import models\n")
        git(self.repo, "mv", "internal/util/util.go",
            "internal/util/helper.go")
        os.remove(os.path.join(self.repo, "packages/ui/index.js"))
        self.cli()
        warm = self.kg(self.out)
        self.cli("--no-cache", "--out", self.cold_out)
        self.assertEqual(warm, self.kg(self.cold_out))


class TestColdWarmEquivalence(EquivalenceCase):
    def test_untouched_repo_parses_nothing_and_matches_cold(self):
        self.scan()
        self.assertEqual(self.assert_equivalent("no change"), 0)

    def test_repeated_warm_scans_are_stable(self):
        _, _, _, first = self.scan()
        for i in range(5):
            _, store, _, graph = self.scan()
            self.assertEqual(store.parses, 0, "scan %d re-parsed" % i)
            self.assertEqual(graph, first)

    def test_warm_scan_parses_zero_files(self):
        """The issue's acceptance bar, asserted on the store rather than on
        the report line: a warm scan of an untouched repo extracts nothing."""
        self.scan()
        note, store, c, _ = self.scan()
        self.assertEqual(note, "warm")
        self.assertEqual(store.parses, 0)
        self.assertGreater(c.hits, 0)
        # metadata (go.mod, Cargo.toml, tsconfig.json, package.json, ...) is
        # not cached, so a warm scan still reads those and only those
        self.assertGreater(store.reads, 0)

    def test_cache_document_tracks_the_scanned_set(self):
        self.scan()
        before = set(self.doc()["files"])
        write(self.repo, "src/acme/added.py", "import os\n")
        os.remove(os.path.join(self.repo, "src/acme/models.py"))
        self.scan()
        after = set(self.doc()["files"])
        self.assertEqual(after - before, {"src/acme/added.py"})
        self.assertEqual(before - after, {"src/acme/models.py"})

    def test_equivalence_holds_across_a_cumulative_mutation_sequence(self):
        """Each step builds on the last, so the cache is never re-seeded from
        clean — drift accumulated over successive scans shows up here."""
        self.scan()

        steps = [
            ("edit a Go file in place",
             lambda: write(self.repo, "internal/store/store.go",
                           'package store\nimport "acme/svc/internal/util"\n'
                           "var X = util.Y\nvar Z = 2\n")),
            ("add an untracked Python module",
             lambda: write(self.repo, "src/acme/extra.py",
                           "from acme import models\n")),
            ("commit everything",
             lambda: commit(self.repo, "s2")),
            ("break a Python file's syntax",
             lambda: write(self.repo, "src/acme/extra.py", "def (:\n")),
            ("repair it with a different import",
             lambda: write(self.repo, "src/acme/extra.py",
                           "from acme import app\n")),
            ("retarget a TS alias import",
             lambda: write(self.repo, "src/web/index.ts",
                           "import {u} from '@/web/util';\n")),
            ("rename a Rust module file",
             lambda: (git(self.repo, "mv", "crates/engine/src/parts/gear.rs",
                               "crates/engine/src/parts/cog.rs"),
                      write(self.repo, "crates/engine/src/parts/mod.rs",
                            "pub mod cog;\n"),
                      write(self.repo, "crates/engine/src/lib.rs",
                            "pub mod parts;\nuse crate::parts::cog;\n"))),
            ("add a Java class in a new package",
             lambda: write(self.repo,
                           "core/src/main/java/com/acme/core/db/Repo.java",
                           "package com.acme.core.db;\n"
                           "import com.acme.core.Engine;\n"
                           "public class Repo {}\n")),
            ("import it from Kotlin",
             lambda: write(self.repo, "api/src/main/kotlin/Handlers.kt",
                           "package com.acme.api\n"
                           "import com.acme.core.db.Repo\nfun handle() {}\n")),
            ("commit the rename and the new package",
             lambda: commit(self.repo, "s3")),
            ("delete a workspace package",
             lambda: shutil.rmtree(os.path.join(self.repo, "packages/ui"))),
            ("change a gitignored file git cannot see",
             lambda: write(self.repo, "gen/ignored.py",
                           "import os\nimport sys\n")),
            ("add a whole new Go package",
             lambda: (write(self.repo, "internal/api/api.go",
                            'package api\nimport "acme/svc/internal/store"\n'
                            "var A = store.X\n"),
                      write(self.repo, "cmd/api/main.go",
                            'package main\nimport "acme/svc/internal/api"\n'
                            "func main() { _ = api.A }\n"))),
            ("commit, then move HEAD back one commit",
             lambda: (commit(self.repo, "s4"),
                      git(self.repo, "reset", "-q", "--hard", "HEAD~1"))),
        ]

        for label, mutate in steps:
            mutate()
            self.assert_equivalent(label)


if __name__ == "__main__":
    unittest.main()
