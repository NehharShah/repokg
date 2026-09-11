"""Tests for the bits of action.yml the GitHub Marketplace validates.

These are not tests of behaviour. They exist because the Marketplace refuses
a listing over metadata, and it refuses it at publish time — after the tag
has been cut, which is the one moment the fix is most awkward, since the
listing is built from the tagged commit and a tag should not be moved.

The description length is here because it has already happened once: the
sentence that described both modes came to 177 characters against a limit of
125, and the release had to be redrafted around it.

Read by hand rather than with a YAML parser. repokg has no dependencies, and
taking one on to measure the length of a string would be a poor trade for a
project whose whole install story is that there is nothing to install.
"""

import os
import shutil
import tempfile
import unittest

ACTION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "action.yml")

# What the Marketplace form reports as "must be less than 125 characters".
DESCRIPTION_LIMIT = 125


def folded(path, key):
    """The value of a top-level `key: >-` block, joined as YAML would join it.

    Enough of a YAML reader for one field and no more: the opening line, then
    the indented lines under it, folded on whitespace. It stops at the first
    line that is neither blank nor indented, which is where the block ends.
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if not line.startswith(key + ":"):
            continue
        rest = line[len(key) + 1:].strip()
        if rest not in (">-", ">", "|", "|-"):
            return rest.strip('"\'')
        body = []
        for nxt in lines[i + 1:]:
            if nxt.strip() and not nxt.startswith(" "):
                break
            body.append(nxt.strip())
        return " ".join(" ".join(body).split())
    raise AssertionError("no '%s:' in %s" % (key, path))


class TestMarketplaceMetadata(unittest.TestCase):
    def test_the_description_fits_the_marketplace_limit(self):
        description = folded(ACTION, "description")
        self.assertLess(
            len(description), DESCRIPTION_LIMIT,
            "action.yml's description is %d characters; the Marketplace "
            "rejects a listing at %d or more, and it does so at publish "
            "time, after the tag exists.\n\n%s"
            % (len(description), DESCRIPTION_LIMIT, description))

    def test_the_description_says_what_the_action_does(self):
        """A length limit is easy to satisfy by saying nothing."""
        description = folded(ACTION, "description")
        self.assertGreater(len(description), 40)
        self.assertTrue(description[0].isupper(), description)

    def test_the_metadata_the_marketplace_requires_is_present(self):
        """Absent any of these, the listing is refused rather than degraded."""
        with open(ACTION, encoding="utf-8") as fh:
            text = fh.read()
        for key in ("name:", "description:", "branding:", "icon:", "color:"):
            self.assertIn(key, text, "action.yml has no %s" % key)


class TestFolded(unittest.TestCase):
    """The hand-rolled reader, since a wrong one would pass the test above
    by reading the wrong thing."""

    def read(self, text, key="description"):
        path = os.path.join(self.tmp, "a.yml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return folded(path, key)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_folded_block_is_joined_on_whitespace(self):
        self.assertEqual(self.read("description: >-\n  one two\n  three\n"),
                         "one two three")

    def test_a_plain_value_is_taken_as_is(self):
        self.assertEqual(self.read("description: one two\n"), "one two")

    def test_a_quoted_value_loses_its_quotes(self):
        self.assertEqual(self.read('description: "one two"\n'), "one two")

    def test_the_block_stops_at_the_next_top_level_key(self):
        self.assertEqual(
            self.read("description: >-\n  one\nauthor: someone\n  indented\n"),
            "one")

    def test_a_missing_key_is_a_failure_rather_than_an_empty_string(self):
        with self.assertRaises(AssertionError):
            self.read("name: x\n", key="description")


if __name__ == "__main__":
    unittest.main()
