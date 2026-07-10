import os
import tempfile
import unittest

from repokg.code import walk
from repokg.deps import _js_configs, _js_workspaces, _jsonc_loads, _pnpm_globs, collect


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestJsoncLoads(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_jsonc_loads('{"a": [1, 2]}'), {"a": [1, 2]})

    def test_comments_stripped(self):
        self.assertEqual(_jsonc_loads(
            '{\n'
            '  // line comment\n'
            '  "a": 1, /* block\n'
            '  comment */ "b": 2\n'
            '}'), {"a": 1, "b": 2})

    def test_trailing_commas_stripped(self):
        self.assertEqual(_jsonc_loads('{"a": [1, 2, ], "b": {"c": 3,},}'),
                         {"a": [1, 2], "b": {"c": 3}})

    def test_comment_lookalikes_inside_strings_survive(self):
        self.assertEqual(_jsonc_loads(
            '{"url": "https://x.dev", "glob": "src/**/*", "csv": "a,}"}'),
            {"url": "https://x.dev", "glob": "src/**/*", "csv": "a,}"})

    def test_escaped_quote_inside_string(self):
        self.assertEqual(_jsonc_loads('{"a": "say \\"hi\\" // ok",}'),
                         {"a": 'say "hi" // ok'})

    def test_invalid_returns_none(self):
        self.assertIsNone(_jsonc_loads("{not json"))
        self.assertIsNone(_jsonc_loads(""))


class TestJsConfigs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def tree(self):
        return dict(walk(self.repo))

    def test_baseurl_and_paths(self):
        write(self.repo, "tsconfig.json",
              '{"compilerOptions": {"baseUrl": "./src",'
              ' "paths": {"@/*": ["./*"]}}}')
        write(self.repo, "src/app.ts", "")
        configs = _js_configs(self.repo, self.tree())
        self.assertEqual(configs, {"": ("src", [("@/*", ["./*"])], "src")})

    def test_paths_without_baseurl_resolve_from_config_dir(self):
        write(self.repo, "web/tsconfig.json",
              '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}')
        configs = _js_configs(self.repo, self.tree())
        self.assertEqual(configs["web"], ("web", [("@/*", ["./src/*"])], None))

    def test_jsconfig_supported_tsconfig_preferred(self):
        write(self.repo, "a/jsconfig.json",
              '{"compilerOptions": {"baseUrl": "."}}')
        write(self.repo, "b/jsconfig.json",
              '{"compilerOptions": {"baseUrl": "./x"}}')
        write(self.repo, "b/tsconfig.json",
              '{"compilerOptions": {"baseUrl": "./y"}}')
        configs = _js_configs(self.repo, self.tree())
        self.assertEqual(configs["a"], ("a", [], "a"))
        self.assertEqual(configs["b"], ("b/y", [], "b/y"))

    def test_config_without_aliases_ignored(self):
        write(self.repo, "tsconfig.json",
              '{"extends": "./tsconfig.base.json",'
              ' "compilerOptions": {"strict": true}}')
        self.assertEqual(_js_configs(self.repo, self.tree()), {})

    def test_most_specific_pattern_first(self):
        write(self.repo, "tsconfig.json",
              '{"compilerOptions": {"paths": {'
              '"@/*": ["./src/*"], "@/lib/*": ["./lib/*"], "@cfg": ["./cfg.ts"]}}}')
        _, patterns, _ = _js_configs(self.repo, self.tree())[""]
        self.assertEqual([p for p, _ in patterns], ["@cfg", "@/lib/*", "@/*"])


class TestJsAliasEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def edges(self):
        return {(e["from"], e["to"]): e["count"]
                for e in collect(self.repo) if e["lang"] == "JS/TS"}

    def test_alias_import_matches_equivalent_relative_import(self):
        write(self.repo, "tsconfig.json",
              '{"compilerOptions": {"baseUrl": ".",'
              ' "paths": {"@/*": ["./src/*"]}}}')
        write(self.repo, "src/components/button.tsx", "export const B = 1\n")
        write(self.repo, "pages/index.tsx",
              "import { B } from '@/components/button'\n"
              "import { B as B2 } from '../src/components/button'\n")
        self.assertEqual(self.edges(),
                         {("pages", "src/components"): 2})

    def test_bare_third_party_imports_drop(self):
        write(self.repo, "tsconfig.json",
              '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}')
        write(self.repo, "src/util.ts", "export const u = 1\n")
        write(self.repo, "app/main.ts",
              "import React from 'react'\n"
              "import fs from 'node:fs'\n"
              "const x = require('lodash/get')\n")
        self.assertEqual(self.edges(), {})

    def test_exact_alias_to_file_resolves_to_parent_dir(self):
        write(self.repo, "tsconfig.json",
              '{"compilerOptions": {"paths": {"@config": ["./src/config.ts"]}}}')
        write(self.repo, "src/config.ts", "export const c = 1\n")
        write(self.repo, "app/main.ts", "import { c } from '@config'\n")
        self.assertEqual(self.edges(), {("app", "src"): 1})

    def test_baseurl_bare_import(self):
        write(self.repo, "jsconfig.json",
              '{"compilerOptions": {"baseUrl": "."}}')
        write(self.repo, "lib/util.js", "module.exports = {}\n")
        write(self.repo, "app/main.js", "const u = require('lib/util')\n")
        self.assertEqual(self.edges(), {("app", "lib"): 1})

    def test_baseurl_does_not_claim_third_party_packages(self):
        # 'react' joined to baseUrl has no first segment in the tree; the
        # parent fallback must not resolve it to the baseUrl dir itself.
        write(self.repo, "src/tsconfig.json",
              '{"compilerOptions": {"baseUrl": "."}}')
        write(self.repo, "src/lib/a.ts", "export const a = 1\n")
        write(self.repo, "src/app/main.ts",
              "import React from 'react'\n"
              "import { a } from 'lib/a'\n")
        self.assertEqual(self.edges(), {("src/app", "src/lib"): 1})

    def test_nearest_config_wins_and_shadows_root(self):
        write(self.repo, "tsconfig.json",
              '{"compilerOptions": {"paths": {"~/*": ["./shared/*"]}}}')
        write(self.repo, "shared/log.ts", "export const l = 1\n")
        write(self.repo, "packages/app/tsconfig.json",
              '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}')
        write(self.repo, "packages/app/src/db.ts", "export const d = 1\n")
        write(self.repo, "packages/app/main.ts",
              "import { d } from '@/db'\n"     # nearest config's alias
              "import { l } from '~/log'\n")   # root alias: shadowed, drops
        write(self.repo, "tools/gen.ts",
              "import { l } from '~/log'\n")   # root config applies here
        self.assertEqual(self.edges(), {
            ("packages/app", "packages/app/src"): 1,
            ("tools", "shared"): 1,
        })

    def test_unresolved_alias_target_drops(self):
        write(self.repo, "tsconfig.json",
              '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}')
        write(self.repo, "src/a.ts", "export const a = 1\n")
        write(self.repo, "app/main.ts", "import { g } from '@/gone/deep/x'\n")
        self.assertEqual(self.edges(), {})

    def test_first_existing_paths_value_wins(self):
        write(self.repo, "tsconfig.json",
              '{"compilerOptions": {"paths": {"@/*": ["./missing/*", "./src/*"]}}}')
        write(self.repo, "src/a.ts", "export const a = 1\n")
        write(self.repo, "app/main.ts", "import { a } from '@/a'\n")
        self.assertEqual(self.edges(), {("app", "src"): 1})

    def test_relative_imports_unchanged_without_configs(self):
        write(self.repo, "src/a.ts", "export const a = 1\n")
        write(self.repo, "app/main.ts",
              "import { a } from '../src/a'\n"
              "import missing from '@/nope'\n")
        self.assertEqual(self.edges(), {("app", "src"): 1})

    def test_jsonc_config_with_comments_and_trailing_commas(self):
        write(self.repo, "tsconfig.json",
              '{\n'
              '  // aliases\n'
              '  "compilerOptions": {\n'
              '    "baseUrl": ".", /* root */\n'
              '    "paths": {\n'
              '      "@/*": ["./src/*"],\n'
              '    },\n'
              '  },\n'
              '}\n')
        write(self.repo, "src/a.ts", "export const a = 1\n")
        write(self.repo, "app/main.ts", "import { a } from '@/a'\n")
        self.assertEqual(self.edges(), {("app", "src"): 1})


class TestJsWorkspaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def tree(self):
        return dict(walk(self.repo))

    def test_npm_array_and_scoped_names(self):
        write(self.repo, "package.json",
              '{"name": "root", "workspaces": ["packages/*"]}')
        write(self.repo, "packages/core/package.json",
              '{"name": "@acme/core"}')
        write(self.repo, "packages/ui/package.json",
              '{"name": "@acme/ui"}')
        self.assertEqual(_js_workspaces(self.repo, self.tree()),
                         {"@acme/core": "packages/core",
                          "@acme/ui": "packages/ui"})

    def test_yarn_object_form(self):
        write(self.repo, "package.json",
              '{"workspaces": {"packages": ["libs/*"], "nohoist": ["**"]}}')
        write(self.repo, "libs/log/package.json", '{"name": "log"}')
        self.assertEqual(_js_workspaces(self.repo, self.tree()),
                         {"log": "libs/log"})

    def test_pnpm_workspace_yaml(self):
        write(self.repo, "pnpm-workspace.yaml",
              "# workspace layout\n"
              "packages:\n"
              "  - 'packages/*'\n"
              '  - "apps/*"\n'
              "  - tools\n"
              "catalog:\n"
              "  - not-a-glob\n")
        write(self.repo, "packages/core/package.json", '{"name": "@acme/core"}')
        write(self.repo, "apps/web/package.json", '{"name": "web"}')
        write(self.repo, "tools/package.json", '{"name": "tools"}')
        self.assertEqual(_js_workspaces(self.repo, self.tree()),
                         {"@acme/core": "packages/core", "web": "apps/web",
                          "tools": "tools"})

    def test_pnpm_globs_parser(self):
        self.assertEqual(
            _pnpm_globs("packages:\n  - 'a/*'\n  - b\nother:\n  - c\n"),
            ["a/*", "b"])
        self.assertEqual(_pnpm_globs("onlyOther:\n  - c\n"), [])

    def test_star_glob_does_not_cross_segments(self):
        write(self.repo, "package.json", '{"workspaces": ["packages/*"]}')
        write(self.repo, "packages/core/package.json", '{"name": "core"}')
        write(self.repo, "packages/core/examples/demo/package.json",
              '{"name": "demo"}')
        self.assertEqual(_js_workspaces(self.repo, self.tree()),
                         {"core": "packages/core"})

    def test_double_star_glob_crosses_segments(self):
        write(self.repo, "package.json", '{"workspaces": ["libs/**"]}')
        write(self.repo, "libs/a/deep/pkg/package.json", '{"name": "deep"}')
        self.assertEqual(_js_workspaces(self.repo, self.tree()),
                         {"deep": "libs/a/deep/pkg"})

    def test_workspace_without_name_ignored(self):
        write(self.repo, "package.json", '{"workspaces": ["packages/*"]}')
        write(self.repo, "packages/anon/package.json", '{"private": true}')
        self.assertEqual(_js_workspaces(self.repo, self.tree()), {})


class TestJsWorkspaceEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        write(self.repo, "package.json", '{"workspaces": ["packages/*"]}')
        write(self.repo, "packages/core/package.json", '{"name": "@acme/core"}')
        write(self.repo, "packages/core/src/index.ts", "export const c = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def edges(self):
        return {(e["from"], e["to"]): e["count"]
                for e in collect(self.repo) if e["lang"] == "JS/TS"}

    def test_workspace_name_import(self):
        write(self.repo, "packages/app/package.json", '{"name": "@acme/app"}')
        write(self.repo, "packages/app/main.ts",
              "import { c } from '@acme/core'\n"
              "import React from 'react'\n")
        self.assertEqual(self.edges(),
                         {("packages/app", "packages/core"): 1})

    def test_workspace_subpath_grounds_or_falls_back(self):
        write(self.repo, "packages/app/package.json", '{"name": "@acme/app"}')
        write(self.repo, "packages/app/main.ts",
              "import { c } from '@acme/core/src/index'\n"   # grounds in src/
              "import { d } from '@acme/core/dist/util'\n")  # falls back to pkg dir
        self.assertEqual(self.edges(), {
            ("packages/app", "packages/core/src"): 1,
            ("packages/app", "packages/core"): 1,
        })

    def test_paths_alias_takes_precedence_over_workspace(self):
        write(self.repo, "shim/core.ts", "export const c = 2\n")
        write(self.repo, "packages/app/tsconfig.json",
              '{"compilerOptions": {"paths": {"@acme/core": ["../../shim/core.ts"]}}}')
        write(self.repo, "packages/app/package.json", '{"name": "@acme/app"}')
        write(self.repo, "packages/app/main.ts",
              "import { c } from '@acme/core'\n")
        self.assertEqual(self.edges(), {("packages/app", "shim"): 1})


if __name__ == "__main__":
    unittest.main()
