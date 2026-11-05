#!/usr/bin/env python3
"""Bugs from the 2026-11-04 REVTeX session report.

Each test pins one reported defect:

1 (high)   a pure-insertion draft froze the whole source line and the
           refusal misstated the cause;
2 (high)   a draft inside a sectioning title was accepted silently and
           broke the build ("Not allowed in LR mode");
3 (medium) a .bib defect was reported as a compile failure although the
           PDF was correct (covered in test_latex_draft.py);
5 (low)    list previews showed tokens from earlier accepted drafts kept
           marked on the same line;
6 (low)    deleting a token emitted an empty \\added[id=AI]{} no-op;
plus a regression found while reproducing bug 5: rejecting a draft that
was composed over kept accepted markup deleted the accepted text.

Run:  python3 -m unittest discover -s tests
"""

import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path

from tools.latex_draft import (
    BLOCK_RE,
    build_inline_block,
    load_state,
    main,
)

DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "The fox crossed the pond and the mouse watched the tide.\n"
    "\\end{document}\n"
)


class SessionReportCase(unittest.TestCase):
    """Default configuration (keep_markup on, no .latex-draft.cfg)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(DOC)

    def run_cli(self, *argv):
        return main([str(self.tex), *argv])

    def run_cli_err(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([str(self.tex), *argv])
        return rc, err.getvalue()

    def state(self):
        return load_state(self.tex)

    def pending_ids(self):
        return [
            did
            for did, rec in self.state()["drafts"].items()
            if rec["status"] == "pending"
        ]

    def body_line(self):
        text = self.tex.read_text()
        m = BLOCK_RE.search(text)
        assert m is not None, "no draft block in document"
        return m.group(2).rstrip("\r\n")


class TestPureInsertFreeze(SessionReportCase):
    """Bug 1: a pure-insertion draft froze the whole line."""

    def draft_insert(self):
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "the pond", "--new", "the wide pond"
        )
        self.assertEqual(rc, 0, err)

    def test_anchor_before_the_insertion_composes(self):
        self.draft_insert()
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "The fox", "--new", "The red fox"
        )
        self.assertEqual(rc, 0, err)
        line = self.body_line()
        self.assertIn(r"\added[id=AI]{red}", line)
        self.assertIn(r"\added[id=AI]{wide}", line)

    def test_anchor_after_the_insertion_composes(self):
        self.draft_insert()
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "the tide", "--new", "the rising tide"
        )
        self.assertEqual(rc, 0, err)
        line = self.body_line()
        self.assertIn(r"\added[id=AI]{rising}", line)
        self.assertIn(r"\added[id=AI]{wide}", line)

    def test_anchor_inside_the_insertion_is_refused_and_names_the_block(self):
        self.draft_insert()
        pending = self.pending_ids()
        self.assertEqual(len(pending), 1)
        rc, err = self.run_cli_err("draft-replace", "--old", "wide", "--new", "broad")
        self.assertEqual(rc, 2)
        self.assertIn(pending[0], err)
        self.assertIn("adds", err)
        self.assertIn("accept or reject", err)


class TestSectionTitleDraft(SessionReportCase):
    """Bug 2: a draft inside a sectioning title broke the build."""

    def section_doc(self, command):
        self.tex.write_text(
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            f"{command}{{Notes on the pond}}\n"
            "The fox crossed the pond.\n"
            "\\end{document}\n"
        )

    def test_section_title_is_refused_with_unmarked_pointer(self):
        self.section_doc("\\section")
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "Notes on the pond", "--new", "Remarks on the pond"
        )
        self.assertEqual(rc, 2)
        self.assertIn("sectioning title", err)
        self.assertIn("--unmarked", err)
        self.assertNotIn("drafted", err)

    def test_starred_subsection_title_is_refused(self):
        self.section_doc("\\subsection*")
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "Notes on the pond", "--new", "Remarks on the pond"
        )
        self.assertEqual(rc, 2)
        self.assertIn("sectioning title", err)

    def test_optional_argument_title_is_refused(self):
        self.tex.write_text(
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "\\section[short]{Notes on the pond}\n"
            "The fox crossed the pond.\n"
            "\\end{document}\n"
        )
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "Notes on the pond", "--new", "Remarks on the pond"
        )
        self.assertEqual(rc, 2)
        self.assertIn("sectioning title", err)


class TestPreviewMixing(SessionReportCase):
    """Bug 5: previews showed tokens from earlier accepted drafts."""

    def test_preview_shows_only_the_pending_drafts_tokens(self):
        self.run_cli("draft-replace", "--old", "the pond", "--new", "the lake")
        self.run_cli("draft-replace", "--old", "the mouse", "--new", "the owl")
        self.assertEqual(self.run_cli("accept", "--all"), 0)
        self.assertEqual(
            self.run_cli("draft-replace", "--old", "watched", "--new", "observed"), 0
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(self.run_cli("list"), 0)
        pending_line = next(
            line for line in out.getvalue().splitlines() if "  pending  " in line
        )
        self.assertIn("observed", pending_line)
        self.assertNotIn("lake", pending_line)
        self.assertNotIn("owl", pending_line)

    def test_composed_preview_shows_both_pending_edits(self):
        self.run_cli("draft-replace", "--old", "the pond", "--new", "the lake")
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "the mouse", "--new", "the owl"
        )
        self.assertEqual(rc, 0, err)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_cli("list"), 0)
        pending_line = next(
            line for line in out.getvalue().splitlines() if "  pending  " in line
        )
        self.assertIn("lake", pending_line)
        self.assertIn("owl", pending_line)


class TestRejectPreservesAcceptedMarkup(SessionReportCase):
    """Regression found while reproducing bug 5: reject deleted accepted text."""

    def test_reject_keeps_accepted_text_and_restores_the_pending_edit(self):
        self.run_cli("draft-replace", "--old", "the pond", "--new", "the lake")
        self.run_cli("draft-replace", "--old", "the mouse", "--new", "the owl")
        self.assertEqual(self.run_cli("accept", "--all"), 0)
        self.assertEqual(
            self.run_cli("draft-replace", "--old", "watched", "--new", "observed"), 0
        )
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        line = next(
            line for line in self.tex.read_text().splitlines() if "fox" in line
        )
        self.assertIn(r"\added[id=AI]{lake}", line)
        self.assertIn(r"\added[id=AI]{owl}", line)
        self.assertIn("watched", line)
        self.assertNotIn("observed", line)
        self.assertNotIn("pond", line)
        self.assertNotIn("mouse", line)

    def test_reject_of_composed_block_restores_the_original_line(self):
        self.run_cli("draft-replace", "--old", "the pond", "--new", "the lake")
        self.run_cli("draft-replace", "--old", "the mouse", "--new", "the owl")
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        line = next(
            line for line in self.tex.read_text().splitlines() if "fox" in line
        )
        self.assertEqual(line, DOC.splitlines()[2])


class TestEmptyAdded(SessionReportCase):
    """Bug 6: deleting a token emitted an empty \\added[id=AI]{}."""

    def test_inline_pure_delete_emits_no_added(self):
        raw = build_inline_block(
            "0eb7fdf0", "The constant is ", r"\AA", " case.", "",
            author="AI", block_number=1,
        )
        body = BLOCK_RE.search(raw).group(2)
        self.assertIn(r"\deleted[id=AI]{\AA}", body)
        self.assertNotIn(r"\added", body)
        self.assertIn("The constant is", body)
        self.assertIn("case.", body)

    def test_mid_line_plain_word_delete_stays_inline(self):
        rc, err = self.run_cli_err("draft-delete", "--old", "the mouse")
        self.assertEqual(rc, 0, err)
        line = self.body_line()
        self.assertIn(r"\deleted[id=AI]{the mouse}", line)
        self.assertNotIn(r"\added", line)
        self.assertIn("The fox crossed the pond and", line)
        self.assertIn("watched the tide.", line)


if __name__ == "__main__":
    unittest.main()
