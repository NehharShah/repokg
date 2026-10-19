"""What the action's check step tells you to do about what repokg reported.

`repokg generate` fixes a graph that is behind HEAD. It does nothing for a
narrative that has drifted from its code, so advising it there sends someone
round a loop with no exit. The step is a shell script, so these tests run it:
the real `run:` block, extracted from action.yml, against a stand-in `repokg`
that prints what a given situation prints.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ACTION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "action.yml")

GRAPH = ("stale: knowledge graph at aaaaaaaaaaaa, HEAD is bbbbbbbbbbbb "
         "(run `repokg generate`)")
NARR = "narrative stale: lib (1 of 5 files changed since enrichment)"
NARR2 = "narrative stale: app (2 of 2 files changed since enrichment)"
HINT = ("re-verify the flagged modules, then `repokg render --restamp MODULE` "
        "for each one that still holds")


def check_script():
    """The body of the step with `id: check`, dedented."""
    with open(ACTION, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "id: check")
    run = next(i for i in range(start, len(lines))
               if lines[i].strip() == "run: |")
    body = []
    for ln in lines[run + 1:]:
        if ln.strip() and not ln.startswith("        "):
            break
        body.append(ln[8:])
    return "\n".join(body) + "\n"


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestCheckStepAdvice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.script = os.path.join(self.tmp, "step.sh")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write(check_script())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_step(self, lines, rc=1, strict="true"):
        stub = os.path.join(self.tmp, "repokg")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\nexit %d\n"
                     % ("\n".join(lines), rc))
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        gh_out = os.path.join(self.tmp, "output")
        env = dict(os.environ, PATH=self.tmp + os.pathsep + os.environ["PATH"],
                   GITHUB_OUTPUT=gh_out, INPUT_PATH=".", INPUT_MODE="check",
                   INPUT_STRICT=strict, INPUT_OUT="", INPUT_MD="")
        p = subprocess.run(["bash", self.script], env=env, cwd=self.tmp,
                           capture_output=True, text=True)
        with open(gh_out, encoding="utf-8") as fh:
            return p, fh.read()

    def annotation(self, p, level="error"):
        found = [ln for ln in p.stdout.splitlines()
                 if ln.startswith("::%s::" % level)]
        self.assertEqual(len(found), 1, p.stdout)
        return found[0]

    def test_graph_only_still_says_generate(self):
        p, out = self.run_step([GRAPH])
        self.assertEqual(p.returncode, 1)
        msg = self.annotation(p)
        self.assertIn("run `repokg generate` and commit the result", msg)
        self.assertNotIn("--restamp", msg)
        self.assertIn("stale=true", out)

    def test_narrative_only_does_not_say_generate(self):
        p, _ = self.run_step([NARR, HINT])
        self.assertEqual(p.returncode, 1)
        msg = self.annotation(p)
        self.assertNotIn("repokg generate", msg)
        self.assertIn("re-verify the flagged modules", msg)
        self.assertIn("repokg render --restamp MODULE", msg)

    def test_both_gives_both_pieces_of_advice(self):
        p, _ = self.run_step([GRAPH, NARR, HINT])
        msg = self.annotation(p)
        self.assertIn("repokg generate", msg)
        self.assertIn("--restamp MODULE", msg)

    def test_the_annotation_is_one_line_and_names_every_module(self):
        p, _ = self.run_step([NARR, NARR2, HINT])
        msg = self.annotation(p)
        self.assertIn("narrative stale: lib", msg)
        self.assertIn("narrative stale: app", msg)
        self.assertEqual(len(msg.splitlines()), 1)
        self.assertEqual(msg.count("--restamp"), 1)

    def test_notes_are_kept_out_of_the_annotation(self):
        p, _ = self.run_step([NARR, HINT, "note: something chatty"])
        self.assertNotIn("chatty", self.annotation(p))
        self.assertIn("chatty", p.stdout.split("::error::")[0])

    def test_non_strict_is_a_warning_with_the_same_advice(self):
        p, _ = self.run_step([NARR, HINT], strict="false")
        self.assertEqual(p.returncode, 0)
        self.assertIn("--restamp MODULE", self.annotation(p, "warning"))

    def test_fresh_is_silent(self):
        p, out = self.run_step(["fresh: knowledge graph matches HEAD abc"], rc=0)
        self.assertEqual(p.returncode, 0)
        self.assertIn("stale=false", out)
        self.assertNotIn("::error::", p.stdout)

    def test_a_crash_is_still_not_reported_as_staleness(self):
        p, out = self.run_step(["Traceback"], rc=2)
        self.assertEqual(p.returncode, 2)
        self.assertIn("stale=false", out)
        self.assertIn("could not run", p.stdout)


class TestExtraction(unittest.TestCase):
    def test_the_extracted_step_is_the_check_step(self):
        script = check_script()
        self.assertIn("repokg check", script)
        self.assertNotIn("repokg diff", script)
        self.assertTrue(re.search(r"INPUT_STRICT", script))


if __name__ == "__main__":
    unittest.main()
