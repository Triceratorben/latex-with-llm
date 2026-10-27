"""GROUP 2 - File/state consistency.

Question answered: the tool keeps three records of the document (the .tex
file, <file>.latex-draft.json, the changelog). When one of them is changed
by something other than the tool (undo, a hand edit, a corrupted or moved
project), do the others stay usable instead of going silently inconsistent?

Run:  python3 tests/test_ext_02_state_consistency.py
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


import tempfile


def block_re():
    return re.compile(r"% latex-draft:([0-9a-f]{8})\n.*?% latex-draft-end:\1\n?", re.S)


class UndoVsDraftState(Base):
    def n_changes(self):
        return len([e for e in read_changelog(self.tex) if e["kind"] == "change"])

    def test_undoing_a_draft_leaves_no_phantom_pending_draft(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Hello.")
        self.assertEqual(self.cli("undo"), 0, self.err)
        self.assertEqual(self.text(), DOC)  # file is really back to the original
        self.assertEqual(
            self.cli("check"), 0,
            "check still reports a pending draft whose block no longer exists:\n" + self.out)

    def test_undoing_an_accept_makes_the_draft_pending_again(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Hello.")
        did = self.draft_id()
        before = self.n_changes()
        self.cli("accept", "--id", did)
        self.assertEqual(self.cli("undo", "--n", str(self.n_changes() - before)), 0, self.err)
        self.assertIn(f"% latex-draft:{did}", self.text())  # block is back in the file
        self.assertEqual(
            self.cli("reject", "--id", did), 0,
            "draft block is back in the file but the tool cannot resolve it: " + self.err)
        self.assertEqual(self.text(), DOC)

    def test_a_phantom_draft_does_not_block_accept_all_for_real_drafts(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Phantom.")
        self.cli("undo")
        self.assertEqual(self.cli("draft-insert", "--after", SENTENCE, "--text", "Real."), 0, self.err)
        rc = self.cli("accept", "--all")
        self.assertEqual(rc, 0, "accept --all aborted entirely: " + self.err)
        self.assertIn("Real.", self.text())
        self.assertNotIn("% latex-draft", self.text())

    def test_a_hand_deleted_block_does_not_block_other_drafts(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Gone.")
        self.tex.write_bytes(block_re().sub("", self.text()).encode("utf-8"))  # user deletes it
        self.assertEqual(self.cli("draft-insert", "--after", SENTENCE, "--text", "Real."), 0, self.err)
        rc = self.cli("accept", "--all")
        self.assertEqual(rc, 0, "accept --all aborted entirely: " + self.err)
        self.assertIn("Real.", self.text())


class StateFileRobustness(Base):
    def test_corrupt_state_file_does_not_silently_orphan_pending_drafts(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Hello.")
        (self.dir / "paper.tex.latex-draft.json").write_text("{not json")
        rc = self.cli("reject", "--all")
        # Either fail loudly, or recover the drafts from the markers in the
        # file. What must not happen: rc 0 ("no pending drafts") while the
        # markup is still in the document.
        self.assertTrue(
            rc != 0 or "% latex-draft" not in self.text(),
            f"rc={rc} out={self.out.strip()!r} but draft markup remains in the file")

    def test_moving_the_project_directory_is_not_reported_as_a_hand_edit(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Hi.")
        parent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, parent, True)
        moved = parent / "moved"
        shutil.copytree(self.dir, moved)
        self.cli("status", tex=moved / "paper.tex")
        self.assertNotIn("WARNING", self.err + self.out,
                         "state stores absolute paths, so a copied/renamed project looks hand-edited")
        external = [e for e in read_changelog(moved / "paper.tex") if e["kind"] == "external"]
        self.assertEqual(external, [])

    def test_torn_final_changelog_line_is_tolerated(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Hello.")
        with (self.dir / "paper.tex.latex-draft-changes.jsonl").open("a") as fh:
            fh.write('{"kind": "change", "no')  # crash mid-write
        self.assertEqual(self.cli("changelog"), 0, self.err)
        self.assertEqual(self.cli("undo"), 0, self.err)
        self.assertEqual(self.text(), DOC)


if __name__ == "__main__":
    unittest.main()
