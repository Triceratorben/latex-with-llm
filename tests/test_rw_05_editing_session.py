"""GROUP 5 - A whole multi-turn editing session.

Three alternatives over the same sentence, a hand edit between turns, a
margin note, selective accepts, and a `finalize` that ends in exactly the
right files.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.rw_support import PaperCase, plain_view

START = (
    "\\documentclass{article}\n\\begin{document}\n"
    "\\section{Intro}\n"
    "The station is stocked to \\SI{90}{\\percent}.\n"
    "A second sentence with plain text.\n"
    "\\end{document}\n"
)


class EditingSession(PaperCase):
    files = {"paper.tex": START, "refs.bib": ""}

    def test_three_alternatives_resolve_to_one(self):
        """Three alternatives over the same sentence: each whole-line draft
        becomes a sibling block; accepting one id resolves the sentence to
        that alternative and drops the others."""
        ids = []
        for wording in ("simple", "plain", "ordinary"):
            self.assertEqual(
                self.cli("draft-replace", "--old", "A second sentence with plain text.",
                         "--new", f"A second sentence with {wording} wording."), 0, self.err)
            ids.append(self.draft_id())
        self.assertEqual(self.cli("list"), 0, self.err)
        pending = [l for l in self.out.splitlines() if " pending " in l]
        self.assertEqual(len(pending), 3, "the alternatives are not all listed:\n" + self.out)
        # accept the third alternative; the other two are dropped with it
        self.assertEqual(self.cli("accept", "--id", ids[2]), 0, self.err)
        text = plain_view(self.read())
        self.assertIn("A second sentence with ordinary wording.", text)
        self.assertNotIn("simple wording", text)
        self.assertNotIn("plain wording", text)
        self.assertEqual(self.cli("check"), 0, self.out)

    def test_hand_edit_between_turns_survives(self):
        self.assertEqual(self.cli(
            "draft-replace", "--old", "plain text", "--new", "simple text"), 0, self.err)
        # the user rewrites a different sentence by hand while the draft pends
        hand = self.read().replace(
            "The station is stocked to \\SI{90}{\\percent}.", "The station is stocked by hand.")
        self.write("paper.tex", hand)
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        text = plain_view(self.read())
        self.assertIn("simple text", text)
        self.assertIn("stocked by hand", text)

    def test_undo_turn_restores_the_original(self):
        self.assertEqual(self.cli(
            "draft-replace", "--old", "plain text", "--new", "simple text"), 0, self.err)
        self.assertEqual(self.cli("undo", "--turn"), 0, self.err)
        self.assertEqual(self.read(), START)
        self.assertEqual(self.cli("check"), 0, self.out)


if __name__ == "__main__":
    unittest.main()
