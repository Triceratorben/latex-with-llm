"""GROUP 1 - Source whitespace fidelity.

Real papers are hard-wrapped at 70-80 columns, indented inside lists and
environments, carry trailing `%` comments and stray trailing spaces, and are
sometimes written one paragraph per line. An agent edits them by quoting a
phrase - which may cross a wrap, or come back with the wrap collapsed to a
space. Oracle: after `accept --all` the file equals the original with exactly
`--old` replaced by `--new`; after `reject --all` it equals the original
byte for byte.

Run:  python3 tests/test_rw_01_source_whitespace.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.rw_support import BIB, PAPER, PaperCase, needs_tex, plain_view  # noqa: E402

WRAP_OLD = "Our results show that\nthe rebalanced flow scales quadratically"
WRAP_NEW = "Our results show that\nthe rebalanced flow grows quadratically"


class WhitespaceFidelity(PaperCase):
    def test_edit_spanning_a_hard_wrap_keeps_the_wrap(self):
        self.round_trip(WRAP_OLD, WRAP_NEW)

    def test_old_with_the_wrap_collapsed_to_a_space_is_matched_or_explained(self):
        old = "Our results show that the rebalanced flow scales quadratically"
        new = "Our results show that the rebalanced flow grows quadratically"
        original = self.read()
        rc = self.cli("draft-replace", "--old", old, "--new", new)
        if rc == 0:
            self.assertEqual(self.cli("accept", "--all"), 0, self.err)
            self.assertIn("grows quadratically", self.read())
            self.assertNotIn("scales quadratically", self.read())
        else:
            self.assertEqual(rc, 2)
            self.assertTrue(
                any(w in self.err.lower() for w in ("line break", "newline", "whitespace", "wrap")),
                "refusal does not mention that only whitespace/line breaks differ:\n" + self.err)
            self.assertEqual(self.read(), original)

    def test_old_with_extra_spaces_or_a_trailing_space_is_not_silently_accepted_wrongly(self):
        original = self.read()
        for old in ("net dc  flow", "net dc flow "):
            with self.subTest(old=old):
                rc = self.cli("draft-replace", "--old", old, "--new", "net dock-to-dock flow")
                if rc == 0:
                    self.cli("accept", "--all")
                    self.assertEqual(self.read().count("net dock-to-dock flow"), 1)
                else:
                    self.assertEqual(rc, 2)
                    self.assertEqual(self.read(), original)

    def test_edit_inside_an_indented_item_keeps_the_indentation(self):
        self.round_trip(
            "a general expression for the rebalanced flow in the light-traffic limit;",
            "a closed-form expression for the rebalanced flow in the light-traffic limit;")

    def test_edit_on_an_indented_continuation_line_keeps_the_indentation(self):
        self.round_trip("the number of docking stations;", "the number of active docking stations;")

    def test_replacing_a_whole_indented_line_including_its_indent(self):
        self.round_trip(
            "  \\item a comparison with field data at \\SI{20}{\\celsius}.",
            "  \\item a comparison with field data at \\SI{20}{\\celsius} and \\SI{5}{\\celsius}.")

    def test_odd_spacing_before_a_trailing_comment_is_preserved(self):
        doc = PAPER.replace("even with balanced demand. % TODO: cite the Shaheen reference",
                            "even with balanced demand.   %TODO:  cite the Shaheen reference  ")
        self.write(self.main_name, doc)
        self.round_trip("even with balanced demand.", "even with nearly balanced demand.")

    def test_trailing_blanks_on_the_edited_line_survive(self):
        doc = PAPER.replace("drive a net dc flow\n", "drive a net dc flow \t \n")
        self.write(self.main_name, doc)
        self.round_trip("net dc flow", "net dock-to-dock flow")

    def test_lines_ending_in_a_forced_break_or_a_tilde(self):
        note = "\\begin{center}\nFirst line of a note\\\\\nsecond line of the note~\n\\end{center}\n\n"
        doc = PAPER.replace("\\section{Conclusion}", note + "\\section{Conclusion}")
        self.write(self.main_name, doc)
        self.round_trip("First line of a note", "Opening line of a note")
        self.round_trip("second line of the note", "closing line of the note")

    def test_crlf_hard_wrapped_source_with_a_phrase_spanning_the_wrap(self):
        doc = PAPER.replace("\n", "\r\n")
        self.round_trip(WRAP_OLD, WRAP_NEW, doc, eol="\r\n")

    # -- paragraph-per-line source --------------------------------------------

    def paragraph_per_line_doc(self):
        start = PAPER.index("Bike-share systems are")
        end = PAPER.index("\n\nOur contributions")
        para = ("Bike-share systems are a promising platform for sustainable urban "
                "mobility~\\cite{demaio2009,fishman2014}. Early pilots reported "
                "rebalancing ratios of about 10 at \\SI{1}{\\per\\hour} demand "
                "imbalance~\\cite{fishman2014}, while later work achieved ratios above "
                "$10^3$~\\citep{raviv2013}. "
                "In this work we show that incentive pricing can drive a net dc flow "
                "even with balanced demand.")
        return PAPER[:start] + para + PAPER[end:]

    def test_several_edits_on_one_long_line_resolve_together(self):
        self.use(self.paragraph_per_line_doc())
        original = self.read()
        for old, new in self.EDITS:
            self.assertEqual(self.cli("draft-replace", "--old", old, "--new", new), 0, self.err)
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        want = original
        for old, new in self.EDITS:
            want = want.replace(old, new, 1)
        self.assertEqual(plain_view(self.read()), want)

    def test_several_edits_on_one_line_form_one_numbered_block_resolved_as_a_unit(self):
        self.use(self.paragraph_per_line_doc())
        original = self.read()
        last = None
        for old, new in self.EDITS:
            self.assertEqual(self.cli("draft-replace", "--old", old, "--new", new), 0, self.err)
            last = self.draft_id()
        self.cli("list")
        pending = [l for l in self.out.splitlines() if " pending " in l]
        self.assertEqual(len(pending), 1, "edits on one line did not compose into one block:\n" + self.out)
        self.assertEqual(self.cli("accept", "--id", last), 0, self.err)
        want = original
        for old, new in self.EDITS:
            want = want.replace(old, new, 1)
        self.assertEqual(plain_view(self.read()), want)

    def test_second_edit_on_a_line_after_a_pure_word_insertion(self):
        """Dictation: 'add "big" before three, and also change seven eight in
        the same sentence'. A replace followed by an edit on the same line
        works; a pure insertion followed by an edit is refused."""
        doc = ("\\documentclass{article}\n\\begin{document}\n"
               "One two three four five six seven eight nine ten.\n\\end{document}\n")
        self.use(doc)
        self.assertEqual(self.cli("draft-replace", "--old", "two three", "--new", "two big three"), 0, self.err)
        rc = self.cli("draft-replace", "--old", "seven eight", "--new", "seven and eight")
        self.assertEqual(rc, 0, "second edit refused after a pure insertion on the same line:\n" + self.err)
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        self.assertEqual(plain_view(self.read()), doc.replace("two three", "two big three").replace(
            "seven eight", "seven and eight"))

    @needs_tex
    def test_review_pdf_of_a_long_line_with_four_edits_compiles(self):
        self.use(self.paragraph_per_line_doc())
        for old, new in self.EDITS:
            self.cli("draft-replace", "--old", old, "--new", new)
        text = self.compile()
        for word in ("versatile", "roughly"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
