"""``find_fences``: which lines open a fenced block, and which close one.

The subject here is the markdown layer alone — no Gherkin, no spec tree. It
gets its own file because the defect it pins (waviisoft/vellum#10) was neither
lint's nor extraction's: both read ``SpecFile.fences``, so a fence the *parser*
missed was missing from both at once, and asserting it through either command
would have located it in the wrong place.
"""

import unittest

from vellum.specfile import find_fences

#: The issue's repro, at the layer the defect lived in. The first block's info
#: string carries an attribute; the second is ordinary. Before the fix the
#: first line matched nothing, so the first block's *closing* line opened a
#: phantom fence that ran to the second block's opening line — one fence with
#: no language where there should be two gherkin ones, and both blocks gone
#: from lint and from the suite without a word.
DESYNC = """\
```gherkin title=demo
Feature: First
```

Prose between the blocks.

```gherkin
Feature: Second
```
""".split("\n")


def summary(lines):
    """``(language, start_line, end_line)`` per fence — what pairing looks like."""
    return [(f.language, f.start_line, f.end_line) for f in find_fences(lines)]


class TestInfoStrings(unittest.TestCase):
    """The info string is free text and its first word is the language."""

    def test_a_two_word_info_string_opens_a_fence(self):
        fences = find_fences(["```gherkin title=demo", "Feature: X", "```"])
        self.assertEqual(len(fences), 1)
        self.assertEqual(fences[0].language, "gherkin")
        self.assertEqual(fences[0].body, "Feature: X")

    def test_the_attributes_are_kept_on_info_and_read_by_nothing(self):
        # Decided and recorded: trailing attributes are ignored. They stay on
        # `info` so a report can quote the line as written, and `language` is
        # what anything acts on.
        fence = find_fences(["~~~gherkin title=demo hl_lines='1 2'", "x", "~~~"])[0]
        self.assertEqual(fence.info, "gherkin title=demo hl_lines='1 2'")
        self.assertEqual(fence.language, "gherkin")

    def test_the_language_is_lowercased_and_a_bare_fence_has_none(self):
        self.assertEqual(find_fences(["```GHERKIN", "x", "```"])[0].language, "gherkin")
        self.assertEqual(find_fences(["```", "x", "```"])[0].language, "")

    def test_a_backtick_fence_whose_info_holds_a_backtick_is_not_a_fence(self):
        # CommonMark §4.5: a backtick fence's info string may not contain a
        # backtick, or an inline code span opening a line would open a block.
        self.assertEqual(find_fences(["```` `code` ````", "x"]), [])

    def test_a_tilde_fences_info_string_may_hold_a_backtick(self):
        fence = find_fences(["~~~text `x`", "body", "~~~"])[0]
        self.assertEqual(fence.language, "text")
        self.assertEqual(fence.body, "body")


class TestOpeningAndClosingStayInStep(unittest.TestCase):
    """waviisoft/vellum#10: the desync, and the properties that prevent it.

    A line rejected as an opening must not be accepted as a closing. The two
    halves of that rule are asserted separately, because the failure needed
    only one of them to slip.
    """

    def test_an_attributed_block_does_not_swallow_the_next_one(self):
        self.assertEqual(
            summary(DESYNC),
            [("gherkin", 1, 3), ("gherkin", 7, 9)],
        )

    def test_a_closing_fence_carries_no_info_string(self):
        # The other half: an info string is what an *opening* may have. If a
        # line carrying one could also close, the first block below would end
        # at line 4 and the second would never be seen.
        self.assertEqual(
            summary(["```gherkin", "a", "```json x", "b", "```"]),
            [("gherkin", 1, 5)],
        )

    def test_a_non_gherkin_fence_with_attributes_is_skipped_without_desync(self):
        # The blast radius of the old rule was never limited to gherkin: any
        # attributed info string desynced the whole file. A python block with
        # one is now an ordinary fence that pairs with its own closing line and
        # leaves the gherkin block after it exactly where it is.
        lines = (
            "```python foo=bar",
            "print('hi')",
            "```",
            "",
            "```gherkin",
            "Feature: Still here",
            "```",
        )
        self.assertEqual(
            summary(list(lines)), [("python", 1, 3), ("gherkin", 5, 7)]
        )


class TestMarkerPairing(unittest.TestCase):
    """Length and character, as CommonMark has it — unchanged by the fix."""

    def test_a_tilde_fence_is_not_closed_by_backticks(self):
        fence = find_fences(["~~~gherkin", "a", "```", "b", "~~~"])[0]
        self.assertEqual((fence.language, fence.start_line, fence.end_line), ("gherkin", 1, 5))
        self.assertEqual(fence.body, "a\n```\nb")

    def test_a_longer_fence_is_closed_only_by_one_at_least_as_long(self):
        fence = find_fences(["````gherkin", "a", "```", "b", "````"])[0]
        self.assertEqual((fence.start_line, fence.end_line), (1, 5))
        self.assertEqual(fence.body, "a\n```\nb")

    def test_a_shorter_opening_is_closed_by_a_longer_fence(self):
        self.assertEqual(summary(["```gherkin", "a", "`````"]), [("gherkin", 1, 3)])

    def test_two_tilde_fences_in_a_row_pair_one_to_one(self):
        self.assertEqual(
            summary(["~~~a", "1", "~~~", "", "~~~b", "2", "~~~"]),
            [("a", 1, 3), ("b", 5, 7)],
        )

    def test_an_unclosed_fence_runs_to_the_end_of_the_file(self):
        self.assertEqual(summary(["```gherkin", "a", "b"]), [("gherkin", 1, 3)])

    def test_an_indented_fence_body_is_dedented_by_its_own_indent(self):
        fence = find_fences(["  ```gherkin title=x", "  Feature: X", "  ```"])[0]
        self.assertEqual(fence.body, "Feature: X")
        self.assertEqual(fence.language, "gherkin")


if __name__ == "__main__":
    unittest.main()
