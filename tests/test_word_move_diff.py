"""A word moved across a bracketed span must diff cleanly.

Moving a word across a parenthetical ("recedes (shown in App.~A)" ->
"(shown in App.~A) recedes") used to produce a diff that struck out the
parenthetical and re-emitted it (\deleted{App.~A)}\added{App.~A) recedes}),
so the reviewer could not read the change, and reject lost the separator
space. Tokens now match on whitespace-normalized cores, the moved word
diffs as delete-at-the-old-spot + insert-at-the-new-spot, and every inline
diff is verified byte-exactly against both resolve views before it is used.

Run:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.latex_draft import (  # noqa: E402
    _inline_views,
    build_inline_block,
)


def _body(before, old, after, new):
    return build_inline_block("deadbeef", before, old, after, new).split("\n")[1]


class TestWordMoveAcrossBracket(unittest.TestCase):
    OLD = "recedes (shown in App.~A)"
    NEW = "(shown in App.~A) recedes"
    BEFORE = "The value "
    AFTER = " identically."

    def test_bracketed_span_stays_plain(self):
        body = _body(self.BEFORE, self.OLD, self.AFTER, self.NEW)
        # the parenthetical is unchanged: it must not be struck or re-added
        self.assertNotIn("\\deleted[id=AI]{App", body)
        self.assertNotIn("\\added[id=AI]{(shown", body)
        self.assertIn("(shown in App.~A)", body)
        self.assertNotIn("\\deleted[id=AI]{App.~A)}", body)
        self.assertNotIn("App.~A) recedes}", body)

    def test_moved_word_is_struck_at_the_old_spot_and_added_at_the_new(self):
        body = _body(self.BEFORE, self.OLD, self.AFTER, self.NEW)
        self.assertIn("\\deleted[id=AI]{recedes}", body)
        self.assertIn("\\added[id=AI]{recedes}", body)
        # the struck copy comes before the plain parenthetical, the added
        # copy after it
        self.assertLess(
            body.index("\\deleted[id=AI]{recedes}"),
            body.index("(shown in App.~A)"),
        )
        self.assertGreater(
            body.index("\\added[id=AI]{recedes}"),
            body.index("(shown in App.~A)"),
        )

    def test_accept_and_reject_resolve_byte_exactly(self):
        body = _body(self.BEFORE, self.OLD, self.AFTER, self.NEW)
        acc, rej = _inline_views(body)
        self.assertEqual(acc, self.BEFORE + self.NEW + self.AFTER)
        self.assertEqual(rej, self.BEFORE + self.OLD + self.AFTER)

    def test_move_in_both_directions(self):
        for old, new in (
            ("alpha (bracketed) beta", "(bracketed) alpha beta"),
            ("(bracketed) beta alpha", "alpha (bracketed) beta"),
        ):
            with self.subTest(old=old):
                body = _body("Word move: ", old, " end.", new)
                acc, rej = _inline_views(body)
                self.assertEqual(acc, "Word move: " + new + " end.")
                self.assertEqual(rej, "Word move: " + old + " end.")


class TestInlineDiffByteExactness(unittest.TestCase):
    """Every inline diff must resolve byte-exactly, whatever the whitespace
    around the changed words does."""

    def test_replace_before_a_space_starting_tail_keeps_the_separator(self):
        # regression: reject used to eat the tail's leading space after the
        # dropped \added ("value recedeshere.")
        body = _body("See the ", "value recedes", " here.", "value grows")
        acc, rej = _inline_views(body)
        self.assertEqual(acc, "See the value grows here.")
        self.assertEqual(rej, "See the value recedes here.")

    def test_replace_before_punctuation_gets_no_separator(self):
        body = _body("The value ", "recedes", ".", "grows")
        acc, rej = _inline_views(body)
        self.assertEqual(acc, "The value grows.")
        self.assertEqual(rej, "The value recedes.")

    def test_swap_of_two_words(self):
        body = _body("", "alpha beta", "", "beta alpha")
        acc, rej = _inline_views(body)
        self.assertEqual(acc, "beta alpha")
        self.assertEqual(rej, "alpha beta")

    def test_move_inside_a_word_run(self):
        body = _body("", "one two three four", "", "one three two four")
        acc, rej = _inline_views(body)
        self.assertEqual(acc, "one three two four")
        self.assertEqual(rej, "one two three four")

    def test_ordinary_replacements_stay_byte_exact(self):
        cases = [
            ("The value ", "scales quadratically", "", "grows quadratically"),
            ("A ", "big, red car", " parked.", "small, red car"),
            (
                "",
                "rely on the method of \\cite{doe2020}",
                "",
                "rest on the method of \\cite{doe2020}",
            ),
        ]
        for before, old, after, new in cases:
            with self.subTest(old=old):
                body = _body(before, old, after, new)
                acc, rej = _inline_views(body)
                self.assertEqual(acc, before + new + after)
                self.assertEqual(rej, before + old + after)


if __name__ == "__main__":
    unittest.main()
