"""GROUP 5 - Markup ownership.

Question answered: the tool injects \\usepackage[draft]{changes} and
\\definechangesauthor lines and strips them again when nothing is pending.
Does it only ever strip what it injected itself, and is the injection
idempotent and cleanly reversible?

Run:  python3 tests/test_ext_05_markup_ownership.py
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



BOB = "\\definechangesauthor[name=Bob,color=red]{Bob}"
BOBS_TEXT = "\\added[id=Bob]{Bob's own tracked sentence.}"


def user_doc(package_line):
    return (
        "\\documentclass{article}\n"
        f"{package_line}\n"
        f"{BOB}\n"
        "\\begin{document}\n"
        f"{BOBS_TEXT}\n\n"
        "Body text.\n"
        "\\end{document}\n"
    )


class UsersOwnChangesPackage(Base):
    """A document that already uses `changes` (e.g. tracked edits from a
    co-author) must come out of draft/resolve with those lines intact."""

    VARIANTS = {
        "[draft] option": "\\usepackage[draft]{changes}",
        "no option": "\\usepackage{changes}",
    }

    def test_accept_keeps_the_users_package_and_author_definitions(self):
        for name, pkg in self.VARIANTS.items():
            with self.subTest(name):
                self.doc = user_doc(pkg)
                self.reset()
                self.cli("draft-insert", "--after", "Body text.", "--text", "LLM sentence.")
                self.assertEqual(self.cli("accept", "--all"), 0, self.err)
                self.assertEqual(self.text(), self.doc.replace("Body text.\n", "Body text.\nLLM sentence.\n"))

    def test_reject_restores_the_users_document_byte_for_byte(self):
        for name, pkg in self.VARIANTS.items():
            with self.subTest(name):
                self.doc = user_doc(pkg)
                self.reset()
                self.cli("draft-insert", "--after", "Body text.", "--text", "LLM sentence.")
                self.assertEqual(self.cli("reject", "--all"), 0, self.err)
                self.assertEqual(self.text(), self.doc)


class InjectionLifecycle(Base):
    def test_package_and_author_lines_are_injected_exactly_once(self):
        for i in range(3):
            self.cli("draft-insert", "--after", INTRO, "--text", f"Draft {i}.")
        text = self.text()
        self.assertEqual(text.count("\\usepackage[draft]{changes}"), 1)
        self.assertEqual(text.count("\\definechangesauthor[name=LLM]{LLM}"), 1)

    def test_preamble_stays_while_any_draft_is_pending_and_goes_with_the_last(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Keep.")
        keep = self.draft_id()
        self.cli("draft-insert", "--after", SENTENCE, "--text", "Drop.")
        drop = self.draft_id()
        self.assertEqual(self.cli("accept", "--id", keep), 0, self.err)
        self.assertIn("\\usepackage[draft]{changes}", self.text(), "removed while a draft is still pending")
        self.assertEqual(self.cli("reject", "--id", drop), 0, self.err)
        self.assertEqual(self.text(), DOC.replace(INTRO + "\n", INTRO + "\nKeep.\n"))

    def test_each_author_is_defined_once_with_its_own_color(self):
        for a in "ABC":
            self.cli("draft-insert", "--author", a, "--after", INTRO, "--text", f"From {a}.")
            self.cli("draft-insert", "--author", a, "--after", SENTENCE, "--text", f"Again {a}.")
        defs = re.findall(r"\\definechangesauthor\[color=(\w+),name=(\w)\]", self.text())
        self.assertEqual(sorted(a for _, a in defs), ["A", "B", "C"])
        self.assertEqual(len({c for c, _ in defs}), 3)


if __name__ == "__main__":
    unittest.main()
