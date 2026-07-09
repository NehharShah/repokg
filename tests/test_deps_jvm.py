import os
import tempfile
import unittest

from repokg.code import walk
from repokg.deps import _jvm_modules, _jvm_package_index


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestJVMDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def tree(self):
        return dict(walk(self.repo))

    def test_maven_and_gradle_modules(self):
        write(self.repo, "pom.xml", "<project/>")
        write(self.repo, "core/pom.xml", "<project/>")
        write(self.repo, "api/build.gradle", "")
        write(self.repo, "app/build.gradle.kts", "")
        write(self.repo, "docs/readme.txt", "")
        self.assertEqual(_jvm_modules(self.tree()), ["", "api", "app", "core"])

    def test_package_index_java_and_kotlin(self):
        write(self.repo, "core/src/main/java/com/acme/core/Engine.java",
              "// (c) acme\npackage com.acme.core;\n\npublic class Engine {}\n")
        write(self.repo, "core/src/main/java/com/acme/core/util/Str.java",
              "package com.acme.core.util;\nclass Str {}\n")
        # Kotlin: no semicolon, collapsed dir convention (dir != package path)
        write(self.repo, "api/src/main/kotlin/Handlers.kt",
              "package com.acme.api\n\nfun handle() {}\n")
        idx = _jvm_package_index(self.repo, self.tree())
        self.assertEqual(idx["com.acme.core"],
                         {"core/src/main/java/com/acme/core"})
        self.assertEqual(idx["com.acme.core.util"],
                         {"core/src/main/java/com/acme/core/util"})
        self.assertEqual(idx["com.acme.api"], {"api/src/main/kotlin"})

    def test_javadoc_and_comments_not_mistaken_for_declarations(self):
        write(self.repo, "src/A.java",
              "/**\n * package fake.name\n */\npackage real.pkg;\nclass A {}\n")
        write(self.repo, "src/B.java", "// package another.fake\nclass B {}\n")
        idx = _jvm_package_index(self.repo, self.tree())
        self.assertEqual(set(idx), {"real.pkg"})

    def test_default_package_files_ignored(self):
        write(self.repo, "src/Main.java", "public class Main {}\n")
        self.assertEqual(_jvm_package_index(self.repo, self.tree()), {})


if __name__ == "__main__":
    unittest.main()
