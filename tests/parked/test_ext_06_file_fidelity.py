"""GROUP 6 - File fidelity.

Question answered: is the user's file treated as bytes they own? Line endings,
encodings, missing trailing newlines, trailing whitespace and LaTeX-special
characters must survive a draft -> accept/reject round trip untouched.

Run:  python3 tests/test_ext_06_file_fidelity.py
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



SNIPPETS = [
    r"50\% of cases",
    r"Tom \& Jerry",
    r"$E=mc^2$ and \(a_b\)",
    r"Line one \\ line two.",
    r"\emph{a} \textbf{b} \#1 \~{}",
    r"stray \{ brace",
    "Na\u00efve caf\u00e9 \u2014 \u00e5ngstr\u00f6m \u2713 \u65e5\u672c\u8a9e",
]


class SpecialCharacters(Base):
    def test_inserted_text_survives_accept_verbatim_and_reject_leaves_no_trace(self):
        for s in SNIPPETS:
            with self.subTest(s):
                self.reset()
                self.cli("draft-insert", "--after", INTRO, "--text", s)
                self.cli("accept", "--all")
                self.assertEqual(self.text(), DOC.replace(INTRO + "\n", INTRO + "\n" + s + "\n"))
                self.reset()
                self.cli("draft-insert", "--after", INTRO, "--text", s)
                self.cli("reject", "--all")
                self.assertEqual(self.text(), DOC)

    def test_replacement_text_survives_accept_verbatim(self):
        for s in SNIPPETS:
            with self.subTest(s):
                self.reset()
                self.cli("draft-replace", "--old", SENTENCE, "--new", s)
                self.cli("accept", "--all")
                self.assertEqual(self.text(), DOC.replace(SENTENCE, s))


class LineEndingsAndEncoding(Base):
    def test_crlf_file_is_still_crlf_after_draft_and_reject(self):
        crlf = DOC.replace("\n", "\r\n").encode("utf-8")
        self.tex.write_bytes(crlf)
        self.cli("draft-insert", "--after", INTRO, "--text", "Hello.")
        self.cli("reject", "--all")
        self.assertEqual(self.tex.read_bytes(), crlf, "line endings were rewritten")

    def test_non_utf8_file_gives_a_clean_error_not_a_traceback(self):
        self.tex.write_bytes(DOC.replace("Small", "Sm\u00e5l").encode("latin-1"))
        try:
            rc = self.cli("status")
        except UnicodeDecodeError as exc:
            self.fail(f"uncaught UnicodeDecodeError instead of a clean error: {exc}")
        self.assertIn(rc, (1, 2))


class WhitespaceIsPreserved(Base):
    def test_file_without_trailing_newline_round_trips(self):
        no_nl = DOC.rstrip("\n")
        for name, argv in {
            "insert after": ["draft-insert", "--after", SENTENCE, "--text", "X."],
            "insert before \\end": ["draft-insert", "--before", "\\end{document}", "--text", "X."],
            "replace": ["draft-replace", "--old", SENTENCE, "--new", "X."],
            "replace last line": ["draft-replace", "--old", "\\end{document}", "--new", "\\end{document}"],
        }.items():
            with self.subTest(name):
                self.tex.write_bytes(no_nl.encode())
                self.cli(*argv)
                self.assertEqual(self.cli("reject", "--all"), 0, self.err)
                self.assertEqual(self.text(), no_nl)

    def test_trailing_spaces_tabs_and_blank_lines_are_untouched(self):
        messy = DOC.replace(SENTENCE + "\n", SENTENCE + "  \t\n\n\n   \n")
        self.tex.write_bytes(messy.encode())
        self.cli("draft-replace", "--old", SENTENCE, "--new", "Other.")
        self.cli("draft-insert", "--after", INTRO, "--text", "Hello.")
        self.assertEqual(self.cli("reject", "--all"), 0, self.err)
        self.assertEqual(self.text(), messy)


if __name__ == "__main__":
    unittest.main()
