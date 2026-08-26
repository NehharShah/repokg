import os
import tempfile
import unittest

from repokg.code import collect as code_collect
from repokg.code import walk
from repokg.deps import _js_workspaces
from repokg.deps import collect as deps_collect
from repokg.facts import MAX_FILE_BYTES, Store


def write(root, rel, text, mode="w"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        f.write(text)


class FactsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.store = Store(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def facts(self, rel, text, mode="w"):
        write(self.repo, rel, text, mode)
        head, _, name = rel.rpartition("/")
        return self.store.facts(head, name)


class TestExtractors(FactsCase):
    def test_go_single_and_block_imports(self):
        rec = self.facts("main.go",
                         'package main\n'
                         'import "fmt"\n'
                         'import (\n\t"os"\n\t_ "acme/x/y"\n)\n')
        self.assertEqual(rec["lang"], "Go")
        self.assertEqual(rec["go_imports"], ["fmt", "os", "acme/x/y"])

    def test_python_absolute_and_relative_imports(self):
        rec = self.facts("app.py",
                         "import os, sys\n"
                         "from . import sibling\n"
                         "from ..pkg.mod import thing\n")
        self.assertEqual(rec["py_imports"],
                         [["os", 0], ["sys", 0], ["", 1], ["pkg.mod", 2]])

    def test_python_syntax_error_yields_no_imports(self):
        rec = self.facts("broken.py", "def (:\n")
        self.assertEqual(rec["lang"], "Python")
        self.assertNotIn("py_imports", rec)

    def test_js_import_forms(self):
        rec = self.facts("a.ts",
                         "import x from './x';\n"
                         "const y = require('../y');\n"
                         "await import('@acme/z');\n")
        self.assertEqual(rec["js_imports"], ["./x", "../y", "@acme/z"])

    def test_rust_use_roots_and_paths(self):
        rec = self.facts("lib.rs",
                         "use std::fmt;\n"
                         "pub use crate::a::b;\n"
                         "use crate::{c::d, e};\n")
        self.assertEqual(rec["rust_roots"], ["std", "crate", "crate"])
        self.assertEqual(rec["rust_paths"],
                         [["std::fmt", ""], ["crate::a::b", ""],
                          ["crate::", "c::d, e"]])

    def test_jvm_package_and_imports(self):
        rec = self.facts("A.java",
                         "package com.acme.core;\n"
                         "import java.util.List;\n"
                         "import static com.acme.x.Y.z;\n")
        self.assertEqual(rec["jvm_package"], "com.acme.core")
        self.assertEqual(rec["jvm_imports"], ["java.util.List", "com.acme.x.Y.z"])

    def test_jvm_default_package_is_none(self):
        rec = self.facts("A.java", "class A {}\n")
        self.assertIsNone(rec["jvm_package"])

    def test_unknown_extension_has_no_record(self):
        self.assertEqual(self.facts("notes.txt", "hello\n"), {})
        self.assertEqual(self.store.reads, 0)

    def test_language_without_extractor_gets_loc_only(self):
        rec = self.facts("schema.sql", "select 1;\nselect 2;\n")
        self.assertEqual(rec, {"lang": "SQL", "loc": 2})

    def test_records_are_json_serializable(self):
        import json
        rec = self.facts("app.py", "from . import x\n")
        self.assertEqual(json.loads(json.dumps(rec)), rec)


class TestLoc(FactsCase):
    def test_trailing_newline_not_double_counted(self):
        self.assertEqual(self.facts("a.py", "x = 1\ny = 2\n")["loc"], 2)

    def test_missing_trailing_newline_still_counts(self):
        self.assertEqual(self.facts("b.py", "x = 1\ny = 2")["loc"], 2)

    def test_empty_file_is_zero(self):
        self.assertEqual(self.facts("c.py", "")["loc"], 0)

    def test_binary_file_is_zero(self):
        self.assertEqual(self.facts("d.py", b"\x00\x01\n\n", mode="wb")["loc"], 0)

    def test_oversized_parseable_file_is_zero_loc_but_still_parsed(self):
        rec = self.facts("e.py", "import os\n" + "#\n" * MAX_FILE_BYTES)
        self.assertEqual(rec["loc"], 0)
        self.assertEqual(rec["py_imports"], [["os", 0]])

    def test_oversized_file_without_extractor_is_never_opened(self):
        rec = self.facts("e.sql", "select 1;\n" * (MAX_FILE_BYTES // 5))
        self.assertEqual(rec["loc"], 0)
        self.assertEqual(self.store.reads, 0)

    def test_crlf_normalized_for_extraction(self):
        rec = self.facts("f.py", "import os\r\nimport sys\r\n")
        self.assertEqual(rec["py_imports"], [["os", 0], ["sys", 0]])
        self.assertEqual(rec["loc"], 2)


class TestSingleRead(unittest.TestCase):
    """Each file is opened once per scan. Before the store, LOC and the dep
    collectors each opened their own handle — Java/Kotlin files three times."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        write(self.repo, "core/src/main/java/com/acme/core/Engine.java",
              "package com.acme.core;\npublic class Engine {}\n")
        write(self.repo, "api/src/main/java/com/acme/api/H.java",
              "package com.acme.api;\nimport com.acme.core.Engine;\nclass H {}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def tree(self):
        return dict(walk(self.repo))

    def test_jvm_files_read_once_across_both_collectors(self):
        store = Store(self.repo)
        tree = self.tree()
        code_collect(self.repo, tree, store)
        edges = deps_collect(self.repo, tree, None, store)
        self.assertEqual(store.reads, 2)  # two .java files, one read each
        self.assertEqual(
            [(e["from"], e["to"], e["count"]) for e in edges],
            [("api/src/main/java/com/acme/api",
              "core/src/main/java/com/acme/core", 1)])

    def test_repeated_facts_calls_do_not_reread(self):
        store = Store(self.repo)
        for _ in range(3):
            store.facts("core/src/main/java/com/acme/core", "Engine.java")
        self.assertEqual(store.reads, 1)

    def test_sharing_a_store_does_not_change_edges(self):
        tree = self.tree()
        shared = Store(self.repo)
        code_collect(self.repo, tree, shared)
        self.assertEqual(deps_collect(self.repo, tree, None, shared),
                         deps_collect(self.repo, tree))

    def test_overlapping_workspace_globs_read_manifests_once(self):
        write(self.repo, "package.json",
              '{"workspaces": ["packages/*", "packages/**"]}')
        write(self.repo, "packages/core/package.json", '{"name": "core"}')
        store = Store(self.repo)
        self.assertEqual(_js_workspaces(store, self.tree()),
                         {"core": "packages/core"})
        self.assertEqual(store.reads, 2)  # root manifest + the one workspace


if __name__ == "__main__":
    unittest.main()
