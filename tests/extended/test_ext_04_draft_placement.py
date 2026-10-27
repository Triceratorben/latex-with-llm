"""GROUP 4 - Draft placement and review fidelity.

Question answered: is every draft placed where TeX will actually typeset it,
and does the marked-up file read the same as the final document (plus the
drafted change)? A draft in the preamble, after \\end{document}, or one that
comments out the rest of its line makes the PDF the user reviews misleading
or uncompilable even though accept/reject would later produce the right text.

Run:  python3 tests/test_ext_04_draft_placement.py
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



TITLE = "\\title{A Small Paper}"


def strip_markup(text):
    """The document with every pending draft block and injected line removed."""
    text = re.sub(r"% latex-draft:([0-9a-f]{8})\n.*?% latex-draft-end:\1\n?", "", text, flags=re.S)
    return "\n".join(
        l for l in text.split("\n")
        if not l.startswith(("\\usepackage[draft]{changes}", "\\definechangesauthor")))


class BodyDraftsStayInTheBody(Base):
    """\\added/\\deleted/\\footnote/\\todo are body commands; in the preamble
    they abort compilation with "Missing \\begin{document}". The tool has a
    separate draft-preamble command for that region, so body drafts aimed at
    the preamble should be refused (or at least placed after \\begin{document})."""

    CASES = {
        "insert --at-line 2": ["draft-insert", "--at-line", "2", "--text", "Oops."],
        "insert --after \\title": ["draft-insert", "--after", TITLE, "--text", "Oops."],
        "replace \\title": ["draft-replace", "--old", TITLE, "--new", "\\title{Other}"],
        "delete \\title": ["draft-delete", "--old", TITLE],
        "comment --after \\title": ["draft-comment", "--after", TITLE, "--text", "Oops."],
        "margin comment --after \\title": ["draft-comment", "--margin", "--after", TITLE, "--text", "Oops."],
    }

    def test_body_drafts_are_refused_or_placed_after_begin_document(self):
        for name, argv in self.CASES.items():
            with self.subTest(name):
                self.reset()
                rc = self.cli(*argv)
                if rc != 0:
                    self.assertEqual(rc, 2, self.err)
                    continue
                text = self.text()
                self.assertGreater(
                    text.index("% latex-draft:"), text.index("\\begin{document}"),
                    "draft markup was written into the preamble:\n" + text)


class AtLine(Base):
    def test_at_line_past_end_of_file_stays_before_end_document(self):
        rc = self.cli("draft-insert", "--at-line", "999", "--text", "Late text.")
        if rc != 0:
            self.assertEqual(rc, 2, self.err)
            return
        text = self.text()
        self.assertLess(text.index("Late text."), text.index("\\end{document}"),
                        "text after \\end{document} is ignored by TeX, so the draft is invisible")

    def test_at_line_numbers_visible_lines_not_injected_preamble_or_drafts(self):
        # line 5 of DOC is SENTENCE. The first draft injects two preamble
        # lines and a block; the second --at-line 5 must still mean SENTENCE.
        self.cli("draft-insert", "--at-line", "5", "--text", "First.")
        self.cli("draft-insert", "--at-line", "5", "--text", "Second.")
        text = self.text()
        self.assertEqual(strip_markup(text), DOC)
        for word in ("First.", "Second."):
            self.assertTrue(text.index(INTRO) < text.index(word) < text.index(SENTENCE), word)


class AnchorDirection(Base):
    def test_before_anchor_lands_directly_above_the_anchor_line(self):
        self.assertEqual(self.cli("draft-insert", "--before", SENTENCE, "--text", "Opening."), 0, self.err)
        self.cli("accept", "--all")
        self.assertEqual(self.text(), DOC.replace(SENTENCE, "Opening.\n" + SENTENCE))

    def test_after_anchor_matches_a_substring_of_the_line(self):
        self.assertEqual(self.cli("draft-insert", "--after", "worth studying", "--text", "Next."), 0, self.err)
        self.cli("accept", "--all")
        self.assertEqual(self.text(), DOC.replace(SENTENCE, SENTENCE + "\nNext."))


class MidLineEdits(Base):
    """--old need not be a whole line. The block markers are %-comments, so
    anything that follows the end marker on the same line is commented out in
    the review PDF."""

    CASES = {
        "replace": (["draft-replace", "--old", "worth", "--new", "well worth"],
                    SENTENCE.replace("worth", "well worth")),
        "delete": (["draft-delete", "--old", "worth "], SENTENCE.replace("worth ", "")),
    }

    def test_nothing_follows_an_end_marker_on_its_line(self):
        for name, (argv, _) in self.CASES.items():
            with self.subTest(name):
                self.reset()
                self.assertEqual(self.cli(*argv), 0, self.err)
                trailing = re.findall(r"% latex-draft-end:[0-9a-f]{8}([^\n]*)", self.text())
                self.assertEqual(
                    trailing, [""],
                    f"text {trailing!r} sits after the end marker, so TeX treats it as a comment")

    def test_mid_line_edits_still_resolve_correctly(self):
        for name, (argv, accepted_sentence) in self.CASES.items():
            with self.subTest(name + " / accept"):
                self.reset()
                self.cli(*argv)
                self.cli("accept", "--all")
                self.assertEqual(self.text(), DOC.replace(SENTENCE, accepted_sentence))
            with self.subTest(name + " / reject"):
                self.reset()
                self.cli(*argv)
                self.cli("reject", "--all")
                self.assertEqual(self.text(), DOC)


class EditingPendingText(Base):
    def test_replacing_words_inside_a_pending_insert_edits_that_insert(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Hello world.")
        rc = self.cli("draft-replace", "--old", "Hello", "--new", "Goodbye")
        if rc != 0:  # refusing is acceptable; silently duplicating text is not
            self.assertEqual(rc, 2, self.err)
            return
        self.cli("accept", "--all")
        body = self.text()
        self.assertIn("Goodbye world.", body)
        self.assertNotIn("Hello", body, "the original insert survived next to its own replacement")


if __name__ == "__main__":
    unittest.main()
