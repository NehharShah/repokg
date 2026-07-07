import os
import tempfile
import unittest

from repokg.deps import collect


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestRustEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def rust_edges(self):
        return {(e["from"], e["to"]): e["count"]
                for e in collect(self.repo) if e["lang"] == "Rust"}

    def test_workspace_cross_crate_edges(self):
        write(self.repo, "Cargo.toml",
              '[workspace]\nmembers = ["crates/core", "crates/api"]\n')
        write(self.repo, "crates/core/Cargo.toml",
              '[package]\nname = "my-core"\nversion = "0.1.0"\n')
        write(self.repo, "crates/core/src/lib.rs", "pub fn f() {}\n")
        write(self.repo, "crates/api/Cargo.toml",
              '[package]\nname = "api"\nversion = "0.1.0"\n')
        write(self.repo, "crates/api/src/main.rs",
              "use my_core::f;\npub use my_core::f as g;\nuse std::io;\n"
              "use serde::Serialize;\nfn main() { f() }\n")
        edges = self.rust_edges()
        # hyphenated crate name resolves via underscore normalization;
        # std/serde (external) produce nothing
        self.assertEqual(edges, {("crates/api/src", "crates/core"): 2})

    def test_intra_crate_and_self_use_produce_no_edges(self):
        write(self.repo, "Cargo.toml",
              '[package]\nname = "solo"\nversion = "0.1.0"\n')
        write(self.repo, "src/lib.rs", "pub mod a;\nuse crate::a::x;\n")
        write(self.repo, "src/a.rs", "use super::other;\npub fn x() {}\n")
        # tests/ referencing the lib by its crate name stays intra-crate
        write(self.repo, "tests/it.rs", "use solo::a::x;\n")
        self.assertEqual(self.rust_edges(), {})

    def test_bin_section_name_not_mistaken_for_crate(self):
        write(self.repo, "Cargo.toml",
              '[package]\nname = "realname"\nversion = "0.1.0"\n\n'
              '[[bin]]\nname = "cli-alias"\npath = "src/main.rs"\n')
        write(self.repo, "src/main.rs", "fn main() {}\n")
        write(self.repo, "other/Cargo.toml",
              '[package]\nname = "other"\nversion = "0.1.0"\n')
        write(self.repo, "other/src/lib.rs", "use cli_alias::x;\nuse realname::y;\n")
        # cli_alias is a [[bin]] name, not a crate -> no edge; realname resolves
        self.assertEqual(self.rust_edges(), {("other/src", "(root)"): 1})

    def test_virtual_workspace_manifest_ignored(self):
        write(self.repo, "Cargo.toml", '[workspace]\nmembers = ["a"]\n')
        write(self.repo, "a/Cargo.toml", '[package]\nname = "a"\nversion = "0.1.0"\n')
        write(self.repo, "a/src/lib.rs", "pub fn f() {}\n")
        self.assertEqual(self.rust_edges(), {})


if __name__ == "__main__":
    unittest.main()


class TestRustIntraCrateResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        write(self.repo, "Cargo.toml", '[package]\nname = "app"\nversion = "0.1.0"\n')
        write(self.repo, "src/lib.rs", "pub mod engine;\npub mod util;\n")
        write(self.repo, "src/util.rs", "pub fn helper() {}\npub mod fmt {}\n")
        write(self.repo, "src/engine/mod.rs", "pub mod planner;\n")
        write(self.repo, "src/engine/planner.rs", "pub struct Plan;\n")

    def tearDown(self):
        self.tmp.cleanup()

    def rust_edges(self):
        return {(e["from"], e["to"]): e["count"]
                for e in collect(self.repo) if e["lang"] == "Rust"}

    def test_dir_module_and_file_module(self):
        # dir module resolves to its dir; deeper file module stops at owning dir
        write(self.repo, "src/api.rs",
              "use crate::engine::planner::Plan;\nuse crate::util::helper;\n")
        # api.rs lives in src/ -> engine edge crosses dirs; util.rs is in src/ too (self)
        self.assertEqual(self.rust_edges(), {("src", "src/engine"): 1})

    def test_deep_file_uses_root_file_module(self):
        write(self.repo, "src/engine/exec.rs", "use crate::util::helper;\n")
        # util is a file module in src/ -> edge src/engine -> src
        edges = self.rust_edges()
        self.assertEqual(edges.get(("src/engine", "src")), 1)

    def test_brace_group_expands(self):
        write(self.repo, "src/engine/exec.rs",
              "use crate::{util::helper, engine::planner::Plan};\n")
        edges = self.rust_edges()
        self.assertEqual(edges.get(("src/engine", "src")), 1)      # util file module
        self.assertNotIn(("src/engine", "src/engine"), edges)      # self-edge dropped

    def test_unknown_module_path_ignored(self):
        write(self.repo, "src/engine/exec.rs", "use crate::generated::Thing;\n")
        self.assertEqual(self.rust_edges(), {})

    def test_tests_dir_crate_paths_skipped(self):
        # in tests/, `crate::` is the test crate itself, not the lib
        write(self.repo, "tests/it.rs", "use crate::common::x;\n")
        write(self.repo, "tests/common/mod.rs", "pub fn x() {}\n")
        self.assertEqual(self.rust_edges(), {})
