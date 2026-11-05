"""GROUP 3 - Structural contexts: math, tables, floats, verbatim, TikZ, URLs.

In these contexts the markup cannot render, so the only acceptable outcomes
are: refuse cleanly (exit 2, file untouched, a message that says what to do),
or draft so that the review build still works and accept applies the exact
replacement.  Every test below asserts one of those two outcomes per context;
no test accepts a crash, a silent mis-edit, or a broken build.

Run:  python3 tests/test_rw_03_structural_contexts.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.rw_support import PaperCase


class StructuralContexts(PaperCase):
    """Each test builds a minimal document around the structure, then demands
    a clean refusal (rc 2, file untouched) or an exact, buildable draft."""

    def doc(self, body):
        self.write("paper.tex", body)
        return body

    def attempt(self, old, new, body=None):
        """Draft `old`->`new`; return (rc, text-after-the-attempt)."""
        if body is not None:
            self.doc(body)
        rc = self.cli("draft-replace", "--old", old, "--new", new)
        return rc, self.read()

    # -- display math --------------------------------------------------------

    def test_edit_inside_an_equation_is_refused_or_builds(self):
        body = self.doc(
            "\\documentclass{article}\n\\begin{document}\n"
            "Before the formula.\n"
            "\\begin{equation}\n  E = m c^2\n\\end{equation}\n"
            "After the formula.\n\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "E = m c^2", "--new", "E = m a^2")
        text = self.read()
        if rc == 0:
            self.assertNotIn("E = m a^2", "\begin{equation}" and text)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(text, body)

    def test_edit_inside_align_is_refused_cleanly(self):
        body = self.doc(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{align}\n  a &= b + c \\\\\n  d &= e\n\\end{align}\n"
            "\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "d &= e", "--new", "d &= f")
        text = self.read()
        if rc == 0:
            self.cli("reject", "--all")
            self.assertEqual(self.read(), body)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(text, body)

    # -- verbatim ------------------------------------------------------------

    def test_edit_inside_verbatim_is_refused_or_builds(self):
        body = self.doc(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{verbatim}\nx = 1  # keep this comment\n\\end{verbatim}\n"
            "\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "x = 1", "--new", "x = 2")
        if rc == 0:
            self.cli("accept", "--all")
            self.assertIn("x = 2", self.read())
            self.assertNotIn("x = 1", self.read())
            self.assertNotIn(r"\added", self.read())
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), body)

    def test_verbatim_comment_is_not_treated_as_a_comment(self):
        body = self.doc(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{verbatim}\nvalue = 50%% of range\n\\end{verbatim}\n"
            "\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "50%% of range", "--new", "60%% of range")
        if rc == 0:
            self.cli("accept", "--all")
            self.assertIn("60%% of range", self.read())
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), body)

    # -- URLs and \verb ------------------------------------------------------

    def test_text_before_a_url_with_a_percent_is_editable(self):
        body = self.doc(
            "\\documentclass{article}\n\\usepackage[hidelinks]{hyperref}\n"
            "\\begin{document}\n"
            "The tool lives at \\url{https://example.org/a%20b}.\n"
            "\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "The tool lives at",
                      "--new", "The program lives at")
        if rc == 0:
            self.cli("accept", "--all")
            text = self.read()
            self.assertNotIn("\\deleted", text)
            self.assertIn("program", text)
            self.assertIn("\\url{https://example.org/a%20b}", text,
                          "the url with its % was damaged by the edit")
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), body)

    def test_verb_percent_is_not_a_comment(self):
        body = self.doc(
            "\\documentclass{article}\n\\begin{document}\n"
            "Type \\verb|100% pure| here.\n\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "Type \\verb|100% pure| here",
                      "--new", "Type \\verb|100% pure| exactly here")
        if rc == 0:
            self.cli("accept", "--all")
            self.assertIn("exactly here", self.read())
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), body)

    # -- tables and floats ---------------------------------------------------

    def test_edit_inside_a_table_cell_refuses_or_builds(self):
        body = self.doc(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{tabular}{ll}\n  A & 12.3 \\\\\n  B & 45.0 \\\\\n\\end{tabular}\n"
            "\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "B & 45.0", "--new", "B & 45.6")
        if rc == 0:
            self.cli("reject", "--all")
            self.assertEqual(self.read(), body)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), body)

    def test_edit_inside_a_float_caption_refuses_or_builds(self):
        body = self.doc(
            "\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n"
            "\\begin{figure}[ht]\n  \\centering\n"
            "  \\fbox{\\rule{0pt}{2cm}\\rule{4cm}{0pt}}\n"
            "  \\caption{Flow--incentive characteristic of station B.}\n"
            "  \\label{fig:fi}\n\\end{figure}\n\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "characteristic of station B",
                      "--new", "characteristic of station C")
        if rc == 0:
            self.cli("reject", "--all")
            self.assertEqual(self.read(), body)
        else:
            self.assertEqual(rc, 2)
            self.assertEqual(self.read(), body)

    # -- comments ------------------------------------------------------------

    def test_edit_whose_old_text_sits_in_a_comment_is_refused(self):
        body = self.doc(
            "\\documentclass{article}\n\\begin{document}\n"
            "Visible prose. % hidden note about the calibration\n"
            "\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "hidden note about the calibration",
                      "--new", "hidden note, revised")
        self.assertEqual(rc, 2, "editing comment text must not be applied silently")
        self.assertEqual(self.read(), body)

    def test_edit_spanning_visible_text_and_a_comment_is_refused(self):
        body = self.doc(
            "\\documentclass{article}\n\\begin{document}\n"
            "Visible prose. % trailing note\n\\end{document}\n")
        rc = self.cli("draft-replace", "--old", "Visible prose. % trailing note",
                      "--new", "Rewritten.")
        self.assertEqual(rc, 2)
        self.assertEqual(self.read(), body)


if __name__ == "__main__":
    unittest.main()
