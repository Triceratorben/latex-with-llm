"""GROUP 4 - New material: sentences, subsections, list items, equations,
figures, cites, and text containing `%` or `&`.

Adding content is the other half of reviewing. Every chain below ends in the
same oracle: either the tool refuses cleanly (exit 2, file untouched, a
message that says what to do), or the draft is applied so that the review
build works, and `accept --all` lands exactly the requested text while
`reject --all` restores the original bytes.

Run:  python3 tests/test_rw_04_new_material.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.rw_support import PaperCase


BASE = (
    "\\documentclass{article}\n\\begin{document}\n"
    "\\section{Intro}\n"
    "The station is stocked to \\SI{90}{\\percent}.\n"
    "\\begin{itemize}\n"
    "  \\item a general expression for the rebalanced flow;\n"
    "\\end{itemize}\n"
    "\\section{Theory}\n"
    "\\label{sec:theory}\n"
    "We consider a station coupled to two demand centres.\n"
    "\\end{document}\n"
)


class NewMaterial(PaperCase):
    files = {"paper.tex": BASE}

    def attempt(self, *argv):
        """Draft; return (rc, text-after-the-attempt)."""
        rc = self.cli(*argv)
        return rc, self.read()

    # -- sentences and cites -------------------------------------------------

    def test_new_sentence_after_an_anchor(self):
        original = self.read()
        rc = self.cli("draft-insert", "--after", "The station is stocked to \\SI{90}{\\percent}.",
                      "--text", "The stock is swept from empty to the threshold capacity.")
        text = self.read()
        if rc == 0:
            self.cli("accept", "--all")
            self.assertIn("The stock is swept from empty to the threshold capacity.", self.read())
            self.reset()
            self.cli("draft-insert", "--after", "The station is stocked to \\SI{90}{\\percent}.",
                     "--text", "The stock is swept from empty to the threshold capacity.")
            self.assertEqual(self.cli("reject", "--all"), 0, self.err)
            self.assertEqual(self.read(), original)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(text, original)

    def test_add_a_citation_to_a_sentence(self):
        original = self.read()
        rc = self.cli("draft-replace", "--old", "two demand centres.",
                      "--new", "two demand centres~\\cite{demaio2009}.")
        if rc == 0:
            self.cli("accept", "--all")
            self.assertIn("\\cite{demaio2009}", self.read())
            self.assertNotIn("\\deleted", self.read())
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), original)

    # -- structure -----------------------------------------------------------

    def test_new_subsection_with_label(self):
        original = self.read()
        block = ("\\subsection{Light-traffic limit}\n\\label{sec:lighttraffic}\n"
                 "The light-traffic limit applies when the arrival rate is low.\n")
        rc = self.cli("draft-insert", "--before", "\\section{Theory}", "--text", block)
        if rc == 0:
            self.cli("accept", "--all")
            text = self.read()
            self.assertIn("\\label{sec:lighttraffic}", text)
            self.assertIn("The light-traffic limit applies", text)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), original)

    def test_new_list_item_keeps_the_indentation(self):
        original = self.read()
        rc = self.cli("draft-insert", "--after",
                      "  \\item a general expression for the rebalanced flow;",
                      "--text", "  \\item an efficient implementation in \\ours{};")
        if rc == 0:
            self.cli("accept", "--all")
            text = self.read()
            self.assertIn("  \\item an efficient implementation", text)
            self.assertIn("  \\item a general expression", text)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), original)

    def test_new_display_equation_is_refused_or_builds(self):
        original = self.read()
        block = ("\\begin{equation}\n  F_{\\mathrm{net}} = \\frac{1}{\\tau} T(\\lambda_0)\n"
                 "\\end{equation}\n")
        rc = self.cli("draft-insert", "--after", "We consider a station coupled to two demand centres.",
                      "--text", block)
        if rc == 0:
            self.cli("reject", "--all")
            self.assertEqual(self.read(), original)
        else:
            self.assertEqual(rc, 2, self.err)
            self.assertEqual(self.read(), original)

    def test_new_figure_float_is_refused_or_builds(self):
        original = self.read()
        block = ("\\begin{figure}[ht]\n  \\centering\n"
                 "  \\fbox{\\rule{0pt}{2cm}\\rule{4cm}{0pt}}\n"
                 "  \\caption{Flow--incentive characteristic.}\n"
                 "  \\label{fig:fi}\n\\end{figure}\n")
        rc = self.cli("draft-insert", "--after", "\\section{Theory}", "--text", block)
        if rc == 0:
            self.cli("reject", "--all")
            self.assertEqual(self.read(), original)
        else:
            self.assertEqual(rc, 2, self.err)
            self.assertEqual(self.read(), original)

    def test_new_label_via_unmarked_insert(self):
        """An --unmarked insert lands immediately (nothing pending), so the
        way back is `undo --turn`, not `reject --all`."""
        original = self.read()
        rc = self.cli("draft-insert", "--after", "\\section{Theory}",
                      "--text", "\\label{sec:theory-alt}", "--unmarked")
        if rc == 0:
            self.assertIn("\\label{sec:theory-alt}", self.read())
            self.assertEqual(self.cli("undo", "--turn"), 0, self.err)
            self.assertEqual(self.read(), original)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), original)

    # -- special characters ---------------------------------------------------

    def test_inserted_text_with_a_percent_sign_is_refused_or_escaped(self):
        original = self.read()
        rc = self.cli("draft-insert", "--after", "\\section{Intro}",
                      "--text", "Half of the stock is lost, a 50% overhead.")
        if rc == 0:
            self.cli("reject", "--all")
            self.assertEqual(self.read(), original)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), original)

    def test_inserted_text_with_an_ampersand_is_refused_or_escaped(self):
        original = self.read()
        rc = self.cli("draft-insert", "--after", "\\section{Intro}",
                      "--text", "The R&D division funded the work.")
        if rc == 0:
            self.cli("reject", "--all")
            self.assertEqual(self.read(), original)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), original)

    def test_escaped_percent_and_ampersand_are_inserted_verbatim(self):
        original = self.read()
        rc = self.cli("draft-insert", "--after", "\\section{Intro}",
                      "--text", "Losses reach 50\\% in R\\&D samples.")
        if rc == 0:
            self.cli("accept", "--all")
            self.assertIn("50\\% in R\\&D samples", self.read())
            self.reset()
            self.cli("draft-insert", "--after", "\\section{Intro}",
                     "--text", "Losses reach 50\\% in R\\&D samples.")
            self.assertEqual(self.cli("reject", "--all"), 0, self.err)
            self.assertEqual(self.read(), original)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), original)

    def test_insert_before_an_anchor(self):
        rc = self.cli("draft-insert", "--before", "We consider a station",
                      "--text", "The theory section starts here.")
        if rc == 0:
            self.cli("accept", "--all")
            text = self.read()
            self.assertIn("The theory section starts here.", text)
            self.assertLess(text.index("The theory section starts here."),
                            text.index("We consider a station"))
        else:
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
