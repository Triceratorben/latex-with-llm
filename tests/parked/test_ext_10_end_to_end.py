"""GROUP 10 - End-to-end with a real LaTeX engine.

Question answered: do the drafts the tool writes actually compile, does the
PDF the user reviews show what the tool claims, and does the final PDF equal
what a hand-edited document would produce? Everything else in the suite checks
source text; only TeX can say whether that source typesets correctly.

Skipped automatically unless latexmk/pdflatex, pdftotext and the LaTeX
packages changes, ulem and todonotes are installed.

Run:  python3 tests/test_ext_10_real_tex.py
"""

import contextlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
from tools.latex_draft import load_state, main, read_changelog  # noqa: E402,F401

SENTENCE = "Small problems are worth studying."
INTRO = "\\section{Introduction}"
DOC = (
    "\\documentclass{article}\n"
    "\\title{A Small Paper}\n"
    "\\begin{document}\n"
    f"{INTRO}\n"
    f"{SENTENCE}\n"
    "\\end{document}\n"
)


class Base(unittest.TestCase):
    """Fresh temp project per test; cli() runs the tool in-process."""

    doc = DOC

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tex = self.dir / "paper.tex"
        self.populate()

    def populate(self):
        self.tex.write_bytes(self.doc.encode("utf-8"))  # exact bytes

    def reset(self):
        for p in self.dir.iterdir():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        self.populate()

    def cli(self, *argv, tex=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([str(tex or self.tex), *argv])
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def text(self, path=None):
        return (path or self.tex).read_bytes().decode("utf-8")

    def draft_id(self):
        """Id of the draft created by the last draft-* command."""
        return re.search(r"drafted .*?\b([0-9a-f]{8})\b", self.out).group(1)


import subprocess


def _kpse(name):
    exe = shutil.which("kpsewhich")
    return bool(exe and subprocess.run([exe, name], capture_output=True, text=True).stdout.strip())


HAVE_TEX = (
    bool(shutil.which("latexmk") or shutil.which("pdflatex"))
    and bool(shutil.which("pdftotext"))
    and all(_kpse(f) for f in ("changes.sty", "ulem.sty", "todonotes.sty"))
)


@unittest.skipUnless(HAVE_TEX, "needs latexmk/pdflatex, pdftotext, changes.sty, ulem.sty, todonotes.sty")
class RealTeX(Base):
    def compile(self, tex=None):
        """Compile with the tool's own `compile`; return the PDF text."""
        tex = tex or self.tex
        rc = self.cli("compile", tex=tex)
        self.assertEqual(rc, 0, "compile failed:\n" + self.err[-1500:])
        out = subprocess.run(["pdftotext", str(tex.with_suffix(".pdf")), "-"],
                             capture_output=True, text=True).stdout
        return " ".join(out.split())

    # -- do drafts compile? -------------------------------------------------

    def test_every_kind_of_draft_compiles(self):
        kinds = {
            "insert": ["draft-insert", "--after", INTRO, "--text", "Hello."],
            "replace": ["draft-replace", "--old", SENTENCE, "--new", "Big problems are worth studying."],
            "delete": ["draft-delete", "--old", SENTENCE],
            "multi-paragraph insert": ["draft-insert", "--after", INTRO, "--text", "Para one.\n\nPara two."],
            "multi-paragraph replace": ["draft-replace", "--old", SENTENCE, "--new", "Para A.\n\nPara B."],
            "footnote comment": ["draft-comment", "--after", SENTENCE, "--text", "Cite."],
            "margin comment": ["draft-comment", "--margin", "--after", SENTENCE, "--text", "Cite."],
            "preamble": ["draft-preamble", "--end", "--text", "\\renewcommand{\\thesection}{\\Roman{section}}"],
            "special characters": ["draft-insert", "--after", INTRO, "--text", r"50\% of $x^2$ \& more."],
        }
        for name, argv in kinds.items():
            with self.subTest(name):
                self.reset()
                self.assertEqual(self.cli(*argv), 0, self.err)
                self.compile()

    def test_eight_simultaneous_alternatives_compile(self):
        for a in "ABCDEFGH":
            self.cli("draft-replace", "--author", a, "--old", SENTENCE, "--new", f"Variant {a}.")
        text = self.compile()
        for a in "ABCDEFGH":
            self.assertIn(f"Variant {a}.", text)

    def test_chapter_draft_compiles_then_accept_leaves_a_clean_compiling_paper(self):
        self.tex.write_bytes(
            b"\\documentclass{article}\n\\begin{document}\n\\input{ch1}\n\\end{document}\n")
        (self.dir / "ch1.tex").write_text("Chapter text.\n")
        self.cli("draft-insert", "--file", "ch1.tex", "--after", "Chapter text.", "--text", "Drafted.")
        self.assertIn("Drafted.", self.compile())
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        self.assertEqual(self.cli("check"), 0, self.out)
        self.assertIn("Drafted.", self.compile())
        self.assertNotIn("changes", self.text())

    # -- does the review PDF show what was drafted? -------------------------

    def test_review_pdf_shows_old_and_new_text_of_a_replace(self):
        self.cli("draft-replace", "--old", SENTENCE, "--new", "Big problems are worth studying.")
        text = self.compile()
        self.assertIn(SENTENCE, text)
        self.assertIn("Big problems are worth studying.", text)

    def test_review_pdf_keeps_the_rest_of_the_line_after_a_mid_line_replace(self):
        self.cli("draft-replace", "--old", "worth", "--new", "well worth")
        self.assertIn("studying", self.compile(),
                      "the text after the replaced span vanished from the review PDF")

    def test_review_pdf_shows_a_footnote_comment_without_changing_the_body(self):
        self.cli("draft-comment", "--after", SENTENCE, "--text", "Cite a source.")
        text = self.compile()
        self.assertIn("Cite a source.", text)
        self.assertIn(SENTENCE, text)

    # -- does resolving give the right PDF? ---------------------------------

    def test_reject_all_gives_the_original_pdf(self):
        original = self.compile()
        self.cli("draft-replace", "--old", SENTENCE, "--new", "Other.")
        self.cli("draft-insert", "--after", INTRO, "--text", "Inserted.")
        self.cli("draft-comment", "--after", INTRO, "--text", "Note.")
        self.cli("draft-preamble", "--end", "--text", "\\renewcommand{\\thesection}{\\Roman{section}}")
        self.compile()
        self.assertEqual(self.cli("reject", "--all"), 0, self.err)
        self.assertEqual(self.compile(), original)

    def test_compile_after_rejecting_a_margin_comment_still_succeeds(self):
        """The review compile leaves todonotes data (\\pgfsyspdfmark) in the .aux
        file; once reject strips the package the next compile dies on it."""
        self.cli("draft-comment", "--margin", "--after", SENTENCE, "--text", "Note.")
        self.compile()
        self.assertEqual(self.cli("reject", "--all"), 0, self.err)
        self.assertIn(SENTENCE, self.compile())

    def test_accept_all_gives_the_same_pdf_as_the_hand_edited_document(self):
        wrap = lambda body: ("\\documentclass{article}\n\\begin{document}\n" + body + "\\end{document}\n")
        self.tex.write_text(wrap("Alpha sentence.\nBeta sentence.\nGamma sentence.\n"))
        self.cli("draft-replace", "--old", "Alpha sentence.", "--new", "Alpha revised.")
        self.cli("draft-insert", "--after", "Beta sentence.", "--text", "Beta addendum.")
        self.cli("draft-delete", "--old", "Gamma sentence.")
        self.compile()
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        got = self.compile()
        hand = self.dir / "hand.tex"
        hand.write_text(wrap("Alpha revised.\nBeta sentence.\nBeta addendum.\n"))
        self.assertEqual(got, self.compile(hand))

    def test_accepted_footnote_renders_as_a_footnote_not_as_literal_text(self):
        self.cli("draft-comment", "--after", SENTENCE, "--text", "Cite a source.")
        self.cli("accept", "--all")
        text = self.compile()
        self.assertIn("Cite a source.", text)
        self.assertNotIn("footnote", text, "the word 'footnote' is typeset: the command was corrupted")

    def test_preamble_draft_shows_its_effect_and_reject_removes_it(self):
        self.cli("draft-preamble", "--end", "--text", "\\renewcommand{\\thesection}{\\Roman{section}}")
        self.assertIn("I Introduction", self.compile())
        self.assertEqual(self.cli("reject", "--all"), 0, self.err)
        text = self.compile()
        self.assertIn("1 Introduction", text)
        self.assertNotIn("I Introduction", text)

    # -- repair against a real log ------------------------------------------

    def test_repair_fixes_an_environment_mismatch_reported_by_real_pdflatex(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{itemize}\n\\item one\n\\end{enumerate}\n\\end{document}\n")
        self.assertNotEqual(self.cli("compile"), 0)
        self.assertEqual(self.cli("repair"), 0, self.out + self.err)
        self.assertIn("\\end{itemize}", self.text())
        self.compile()


if __name__ == "__main__":
    unittest.main()
