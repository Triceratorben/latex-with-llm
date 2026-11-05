#!/usr/bin/env python3
"""Bug: accepting a draft-delete of a \\footnote restored the footnote.

A whole-line ``draft-delete`` of ``\\footnote{...}`` drafts
``\\deleted[id=AI]{\\mbox{\\footnote{...}}}``. The accept resolver's
comment fallback (which exists for draft-comment blocks, whose body is a
bare ``\\footnote{...}``/``\\todo{...}``) searched the whole block body,
found the footnote nested inside ``\\mbox{...}`` inside ``\\deleted{...}``,
and re-emitted it on accept - silently inverting the delete (exit 0,
"accepted" message, text back in the source).

The fix gates the comment fallback to bodies that start with the bare
command: a delete block starts with ``\\deleted`` and can never be a
comment. These tests pin the reported delete cases and the neighbouring
comment cases that must keep working.

Run:  python3 -m unittest discover -s tests
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tools.latex_draft import load_state, main

DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "The fox crossed the pond.\n"
    "\\footnote{A note about the tide.}\n"
    "The mouse watched.\n"
    "\\end{document}\n"
)


class AcceptDeleteFootnoteCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(DOC)

    def run_cli_err(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([str(self.tex), *argv])
        return rc, err.getvalue()

    def write(self, text):
        self.tex.write_text(text)


class TestDeleteFootnoteIsRemoved(AcceptDeleteFootnoteCase):
    def test_whole_line_footnote_delete_accept_removes_the_text(self):
        rc, err = self.run_cli_err(
            "draft-delete", "--old", "\\footnote{A note about the tide.}"
        )
        self.assertEqual(rc, 0, err)
        rc, err = self.run_cli_err("accept", "--all")
        self.assertEqual(rc, 0, err)
        text = self.tex.read_text()
        self.assertNotIn("A note about the tide", text)
        self.assertNotIn("\\footnote", text)
        self.assertIn("The fox crossed the pond.", text)
        self.assertIn("The mouse watched.", text)

    def test_whole_line_footnote_delete_reject_restores_the_text(self):
        rc, err = self.run_cli_err(
            "draft-delete", "--old", "\\footnote{A note about the tide.}"
        )
        self.assertEqual(rc, 0, err)
        rc, err = self.run_cli_err("reject", "--all")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tex.read_text(), DOC)

    def test_delete_of_a_line_containing_a_footnote_removes_the_whole_line(self):
        doc = (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "Keep me.\n"
            "A line with a note\\footnote{the note} here.\n"
            "Keep me too.\n"
            "\\end{document}\n"
        )
        self.write(doc)
        rc, err = self.run_cli_err(
            "draft-delete", "--old", "A line with a note\\footnote{the note} here."
        )
        self.assertEqual(rc, 0, err)
        rc, err = self.run_cli_err("accept", "--all")
        self.assertEqual(rc, 0, err)
        text = self.tex.read_text()
        self.assertNotIn("the note", text)
        self.assertNotIn("A line with a note", text)
        self.assertIn("Keep me.", text)
        self.assertIn("Keep me too.", text)

    def test_delete_of_a_todo_line_removes_the_text(self):
        doc = (
            "\\documentclass{article}\n"
            "\\usepackage{todonotes}\n"
            "\\begin{document}\n"
            "First.\n"
            "\\todo{fix this later}\n"
            "Last.\n"
            "\\end{document}\n"
        )
        self.write(doc)
        rc, err = self.run_cli_err("draft-delete", "--old", "\\todo{fix this later}")
        self.assertEqual(rc, 0, err)
        rc, err = self.run_cli_err("accept", "--all")
        self.assertEqual(rc, 0, err)
        text = self.tex.read_text()
        self.assertNotIn("fix this later", text)
        self.assertIn("First.", text)
        self.assertIn("Last.", text)


class TestCommentBlocksStillWork(AcceptDeleteFootnoteCase):
    DOC_PLAIN = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "First.\n"
        "Second.\n"
        "\\end{document}\n"
    )

    def setUp(self):
        super().setUp()
        self.write(self.DOC_PLAIN)

    def test_comment_footnote_is_kept_on_accept(self):
        rc, err = self.run_cli_err(
            "draft-comment", "--after", "First.", "--text", "a reviewer question"
        )
        self.assertEqual(rc, 0, err)
        rc, err = self.run_cli_err("accept", "--all")
        self.assertEqual(rc, 0, err)
        text = self.tex.read_text()
        self.assertIn("a reviewer question", text)
        self.assertIn("\\footnote", text)
        self.assertIn("First.", text)
        self.assertIn("Second.", text)

    def test_comment_footnote_is_removed_on_reject(self):
        rc, err = self.run_cli_err(
            "draft-comment", "--after", "First.", "--text", "a reviewer question"
        )
        self.assertEqual(rc, 0, err)
        rc, err = self.run_cli_err("reject", "--all")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tex.read_text(), self.DOC_PLAIN)

    def test_margin_comment_todo_is_kept_on_accept(self):
        rc, err = self.run_cli_err(
            "draft-comment",
            "--after",
            "First.",
            "--margin",
            "--text",
            "margin question",
        )
        self.assertEqual(rc, 0, err)
        rc, err = self.run_cli_err("accept", "--all")
        self.assertEqual(rc, 0, err)
        text = self.tex.read_text()
        self.assertIn("margin question", text)
        self.assertIn("\\todo", text)
        self.assertIn("First.", text)


class TestDeleteFootnoteState(AcceptDeleteFootnoteCase):
    def test_no_pending_drafts_remain_after_accept(self):
        rc, err = self.run_cli_err(
            "draft-delete", "--old", "\\footnote{A note about the tide.}"
        )
        self.assertEqual(rc, 0, err)
        rc, err = self.run_cli_err("accept", "--all")
        self.assertEqual(rc, 0, err)
        state = load_state(self.tex)
        pending = [
            did
            for did, rec in state["drafts"].items()
            if rec["status"] == "pending"
        ]
        self.assertEqual(pending, [])


if __name__ == "__main__":
    unittest.main()
