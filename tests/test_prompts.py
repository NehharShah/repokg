"""The enrichment prompt has to carry the targeted re-run protocol.

The prompt is the only channel to the agent: if the instructions for
re-verifying a flagged module drift out of it, nothing else in repokg can
bring them back. These tests pin the commands it names to commands that exist,
so the prompt and the CLI cannot disagree without a test failing.
"""

import contextlib
import io
import re
import unittest

from repokg import cli, prompts

REPO, OUT, MD = "/work/proj", "/work/proj/.repokg", "/work/proj/KNOWLEDGE_GRAPH.md"


def rendered():
    return prompts.render(REPO, OUT, MD)


def rerun_section(text):
    start = text.index("## Is this a re-run?")
    return text[start:text.index("## Steps")]


class TestRerunProtocol(unittest.TestCase):
    def test_the_rerun_section_comes_before_the_full_steps(self):
        text = rendered()
        self.assertLess(text.index("## Is this a re-run?"), text.index("## Steps"))

    def test_it_says_when_it_applies(self):
        self.assertIn("%s/narratives.json` already exists" % OUT,
                      rerun_section(rendered()))

    def test_it_finds_flagged_modules_with_check_and_audit(self):
        sec = rerun_section(rendered())
        self.assertIn("repokg check %s" % REPO, sec)
        self.assertIn("narrative stale: <module>", sec)
        self.assertIn("repokg audit %s --json" % REPO, sec)
        self.assertIn('"kind": "narrative"', sec)

    def test_it_limits_the_work_to_flagged_modules(self):
        sec = rerun_section(rendered())
        self.assertIn("only those", sec)
        self.assertIn("Do not re-read, re-word or re-verify modules that were "
                      "not\n   flagged", sec)

    def test_it_covers_both_outcomes(self):
        sec = rerun_section(rendered())
        self.assertIn("now wrong or incomplete: rewrite that one line", sec)
        self.assertIn("still holds: leave the text alone", sec)

    def test_it_names_the_restamp_command(self):
        self.assertIn("repokg render %s --restamp path/of/module "
                      "--restamp another/module" % REPO, rerun_section(rendered()))

    def test_it_forbids_restamping_unverified_modules(self):
        self.assertIn("Never use `--restamp` on a module you have not actually "
                      "re-read", rerun_section(rendered()))

    def test_it_tells_the_agent_to_confirm_with_check(self):
        sec = rerun_section(rendered())
        self.assertIn("Run `repokg check %s` again" % REPO, sec)

    def test_it_covers_nothing_flagged_and_missing_lock(self):
        sec = rerun_section(rendered())
        self.assertIn("nothing to do", sec)
        self.assertIn("no lock", sec)

    def test_it_mentions_the_threshold_flag(self):
        self.assertIn("--stale-threshold PERCENT", rerun_section(rendered()))

    def test_the_full_run_still_ends_in_render_and_inject(self):
        text = rendered()
        self.assertIn("repokg render %s\nrepokg inject %s" % (REPO, REPO), text)
        self.assertIn("records each module's fingerprint", text)


class TestPromptStaysUsable(unittest.TestCase):
    def test_every_placeholder_is_filled(self):
        text = rendered()
        self.assertIsNone(re.search(r"\{(repo|out|md)\}", text))

    def test_the_schema_example_is_still_literal_json_braces(self):
        self.assertIn('"modules": {\n', rendered())

    def test_the_flags_it_names_exist_on_the_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["--help"])
        for flag in ("--restamp", "--stale-threshold"):
            self.assertIn(flag, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
