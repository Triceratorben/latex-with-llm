"""GROUP 3 - Failure atomicity.

Question answered: when a command fails (bad anchor, unknown id, ...) does it
leave the document, the draft registry and the changelog exactly as they were?
A failed command with side effects leaves the user in a state they never asked
for and cannot see.

Run:  python3 tests/test_ext_03_failure_atomicity.py
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




class SingleFileFailures(Base):
    def world(self):
        return (self.tex.read_bytes(), load_state(self.tex)["drafts"], len(read_changelog(self.tex)))

    def test_failing_commands_change_nothing(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Seed.")
        seed = self.draft_id()
        self.cli("draft-insert", "--at-line", "5", "--text", "Done.")
        self.cli("accept", "--id", self.draft_id())
        cases = {
            "insert: missing anchor": ["draft-insert", "--after", "NOPE", "--text", "x"],
            "insert: missing --before anchor": ["draft-insert", "--before", "NOPE", "--text", "x"],
            "insert: point inside a pending block": ["draft-insert", "--after", "{Seed.}", "--text", "x"],
            "replace: old text missing": ["draft-replace", "--old", "NOPE", "--new", "x"],
            "delete: old text missing": ["draft-delete", "--old", "NOPE"],
            "comment: missing anchor": ["draft-comment", "--after", "NOPE", "--text", "x"],
            "comment --margin: missing anchor": ["draft-comment", "--margin", "--after", "NOPE", "--text", "x"],
            "preamble: missing anchor": ["draft-preamble", "--after", "NOPE", "--text", "x"],
            "accept: unknown id": ["accept", "--id", "deadbeef"],
            "reject: unknown id": ["reject", "--id", "deadbeef"],
        }
        for name, argv in cases.items():
            with self.subTest(name):
                before = self.world()
                self.assertEqual(self.cli(*argv), 2, self.err)
                self.assertEqual(self.world(), before)
        self.assertEqual(self.cli("reject", "--id", seed), 0, self.err)  # still resolvable


class MultiFileFailures(Base):
    """A draft aimed at a chapter first prepares the MAIN file's preamble.
    If the chapter-level step then fails, that preparation must not stick."""

    doc = "\\documentclass{article}\n\\begin{document}\n\\input{ch1}\n\\end{document}\n"

    def populate(self):
        super().populate()
        (self.dir / "ch1.tex").write_bytes(b"Chapter one.\n")

    CASES = {
        "insert": ["draft-insert", "--file", "ch1.tex", "--after", "NOPE", "--text", "x"],
        "replace": ["draft-replace", "--file", "ch1.tex", "--old", "NOPE", "--new", "x"],
        "delete": ["draft-delete", "--file", "ch1.tex", "--old", "NOPE"],
        "comment": ["draft-comment", "--file", "ch1.tex", "--after", "NOPE", "--text", "x"],
        "margin comment": ["draft-comment", "--margin", "--file", "ch1.tex", "--after", "NOPE", "--text", "x"],
    }

    def test_failed_chapter_draft_leaves_the_main_file_untouched(self):
        for name, argv in self.CASES.items():
            with self.subTest(name):
                self.reset()
                before = self.tex.read_bytes()
                self.assertEqual(self.cli(*argv), 2, self.err)
                self.assertEqual(
                    self.tex.read_bytes(), before,
                    "a failed draft modified the main file:\n" + self.text())

    def test_check_still_passes_after_a_failed_chapter_draft(self):
        for name, argv in self.CASES.items():
            with self.subTest(name):
                self.reset()
                self.cli(*argv)
                self.assertEqual(
                    self.cli("check"), 0,
                    "check fails with nothing pending and nothing the tool could clean up:\n" + self.out)


if __name__ == "__main__":
    unittest.main()
