#!/usr/bin/env python3
"""Bugs from bug-report-6 (latex-draft point-edit defects).

Each test pins one reported defect; every example is invented text about
the Roman Empire, no content from any real document:

1 (high)   a word move welded words on accept and on reject; reject did
           not restore the original line;
2 (high)   an anchor ending inside a pending \\deleted{...} span raised an
           unhandled StopIteration (bare traceback, exit 1);
3 (high)   drafting inside a \\replaced{...}{...} or \\highlight{...}
           argument was allowed, producing markup the changes package
           cannot nest;
4 (medium) an anchor straddling accepted \\added{...} reported the bare
           "--old text not found in the document", which is false;
5 (medium) the documented --unmarked whole-command anchor failed while the
           inner-word anchor worked -- inverted relative to the skill text;
6 (medium) --unmarked was refused on a line that also carries a pending
           block, blocking a legitimate direct fix;
7 (medium) a composed block could not be partially resolved and the
           documented reject-and-redraft workaround was lossy (bug 1);
8 (low)    list omitted the file, so a block number could not be mapped to
           its file in a multi-file document.

Run:  python3 -m unittest discover -s tests
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tools.latex_draft import (
    BLOCK_RE,
    _accepted_markup_straddle,
    _inline_diff_exact,
    _provenance_span,
    _word_diff_verified,
    load_state,
    main,
)

DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "The senate debated the law in the forum.\n"
    "\\end{document}\n"
)


class BugReport6Case(unittest.TestCase):
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

    def body_line(self):
        text = self.tex.read_text()
        m = BLOCK_RE.search(text)
        assert m is not None, "no draft block in document"
        return m.group(2).rstrip("\r\n")

    def accepted_view(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.tex), "show", "--accepted"]), 0)
        return next(
            line.rstrip("\r\n")
            for line in out.getvalue().splitlines()
            if "senate" in line
        )


class TestWordMoveResolvesExactly(BugReport6Case):
    """Bug 1: a word move welded words on accept and on reject."""

    ORIG = (
        "The senate debated the law extensively today and the consul watched."
    )
    NEW = (
        "The senate extensively debated the law today and the consuls watched."
    )

    def _draft_move(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            + self.ORIG + "\n\\end{document}\n"
        )
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "the consul watched",
                "--new", "the consuls watched",
            ),
            0,
        )
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "debated the law extensively",
                "--new", "extensively debated the law",
            ),
            0,
        )

    def test_accept_resolves_the_new_text_exactly(self):
        self._draft_move()
        self.assertEqual(self.run_cli("accept", "--all"), 0)
        self.assertEqual(self.accepted_view(), self.NEW)

    def test_reject_restores_the_original_line_exactly(self):
        self._draft_move()
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        self.assertIn(self.ORIG, self.tex.read_text())
        self.assertNotIn("latex-draft", self.tex.read_text())

    def test_verified_word_diff_resolves_both_views(self):
        cand = _word_diff_verified(
            "The senate ", "debated the law extensively",
            " today and the ", "extensively debated the law", author="AI",
        )
        self.assertIsNotNone(cand)
        self.assertTrue(
            _inline_diff_exact(
                "The senate ", "debated the law extensively",
                " today and the ", "extensively debated the law", cand,
            )
        )


class TestStopIterationOnPendingDeletedSpan(BugReport6Case):
    """Bug 2: an anchor ending inside a pending \\deleted span crashed."""

    def test_anchor_ending_in_pending_deleted_gets_the_overlap_error(self):
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "the forum", "--new", "the market"
            ),
            0,
        )
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "the forum", "--new", "ZZ"
        )
        self.assertEqual(rc, 2)
        self.assertIn("accept or reject that draft first", err)
        self.assertNotIn("StopIteration", err)

    def test_control_anchor_starting_before_the_marker_still_refuses(self):
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "the forum", "--new", "the market"
            ),
            0,
        )
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "in the forum", "--new", "in the market"
        )
        self.assertEqual(rc, 2)
        self.assertIn("accept or reject that draft first", err)


class TestDraftInsideReplacedArgumentRefused(BugReport6Case):
    """Bug 3: drafting inside \\replaced/\\highlight arguments was allowed."""

    def _write(self, *body_lines):
        self.tex.write_text(
            "\\documentclass{article}\n\\usepackage[draft]{changes}\n"
            "\\definechangesauthor[color=blue,name=ABL]{ABL}\n"
            "\\definechangesauthor[color=green!60!black]{AI}\n"
            "\\begin{document}\n" + "\n".join(body_lines) + "\n\\end{document}\n"
        )

    def test_anchor_inside_multiline_replaced_argument_is_refused(self):
        self._write(
            "\\replaced[id=ABL]{The senate debated the law.",
            "}{The senate debated the law at length, and the consul listened "
            "to the debate in the forum while the province paid its taxes.}",
        )
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "in the forum while",
            "--new", "in the forum, while",
        )
        self.assertEqual(rc, 2)
        self.assertIn("accepted changes markup", err)
        self.assertNotIn("drafted", err)

    def test_anchor_inside_single_line_replaced_argument_is_refused(self):
        self._write(
            "\\replaced[id=ABL]{The senate debated the law.}"
            "{The senate debated the law in the forum while the province "
            "paid its taxes.}",
        )
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "in the forum while",
            "--new", "in the forum, while",
        )
        self.assertEqual(rc, 2)
        self.assertIn("accepted changes markup", err)

    def test_anchor_inside_highlight_argument_is_refused(self):
        self._write(
            "\\highlight[id=ABL]{The senate debated the law in the forum "
            "while the province paid its taxes.}",
        )
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "in the forum while",
            "--new", "in the forum, while",
        )
        self.assertEqual(rc, 2)
        self.assertIn("accepted changes markup", err)

    def test_provenance_span_covers_both_replaced_arguments(self):
        text = (
            "The senate debated the law.\n"
            "\\replaced[id=ABL]{The senate debated the law."
            "}{The senate debated the law in the forum while the province "
            "paid its taxes.}\n"
        )
        idx = text.index("in the forum while")
        span = _provenance_span(text, idx)
        self.assertIsNotNone(span)
        self.assertEqual(text[span[0]:span[1]].count("replaced"), 1)

    def test_control_plain_text_still_drafts(self):
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "in the forum",
                "--new", "in the market",
            ),
            0,
        )


class TestStraddleReportsTheMarkup(BugReport6Case):
    """Bug 4: an anchor straddling accepted markup reported "not found"."""

    def test_straddling_anchor_names_the_accepted_markup(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "The senate \\added[id=AI]{is} debating the law.\n"
            "\\end{document}\n"
        )
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "is debating the law",
            "--new", "was debating the statute",
        )
        self.assertEqual(rc, 2)
        self.assertIn("crosses accepted", err)
        self.assertIn("\\added[id=AI]{is}", err)
        self.assertNotIn("not found", err)

    def test_control_non_straddling_anchor_still_drafts(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "The senate \\added[id=AI]{is} debating the law.\n"
            "\\end{document}\n"
        )
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "debating the law",
                "--new", "debating the statute",
            ),
            0,
        )

    def test_control_genuinely_missing_text_stays_not_found(self):
        rc, err = self.run_cli_err(
            "draft-replace", "--old", "no such phrase", "--new", "x"
        )
        self.assertEqual(rc, 2)
        self.assertIn("not found", err)


class TestUnmarkedAnchors(BugReport6Case):
    """Bug 5: the documented --unmarked whole-command anchor failed."""

    def test_inner_word_anchor_replaces_and_keeps_the_wrapper(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "The senate \\added[id=AI]{debated} the law.\n"
            "\\end{document}\n"
        )
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--unmarked", "--old", "debated",
                "--new", "argued",
            ),
            0,
        )
        self.assertIn(r"\added[id=AI]{argued}", self.tex.read_text())

    def test_whole_command_anchor_replaces_and_keeps_the_wrapper(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "The senate \\added[id=AI]{debated} the law.\n"
            "\\end{document}\n"
        )
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--unmarked",
                "--old", "\\added[id=AI]{debated}",
                "--new", "\\added[id=AI]{argued}",
            ),
            0,
        )
        self.assertIn(r"\added[id=AI]{argued}", self.tex.read_text())


class TestUnmarkedOnPendingBlockLine(BugReport6Case):
    """Bug 6: --unmarked was refused on a line carrying a pending block."""

    def test_unmarked_direct_fix_on_a_pending_block_line(self):
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "the forum", "--new", "the market"
            ),
            0,
        )
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--unmarked", "--old", "The senate debated",
                "--new", "The Senate debated",
            ),
            0,
        )
        self.assertIn("The Senate debated", self.tex.read_text())


class TestComposedBlockPartialResolve(BugReport6Case):
    """Bug 7: the reject-and-redraft workaround for a composed block was
    lossy because rejecting it triggered bug 1."""

    def test_reject_of_composed_block_restores_the_original_line(self):
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "the forum", "--new", "the market"
            ),
            0,
        )
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "debated the law",
                "--new", "debated the statute",
            ),
            0,
        )
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        self.assertIn("The senate debated the law in the forum.", self.tex.read_text())


class TestListShowsFile(BugReport6Case):
    """Bug 8: list omitted the file column."""

    def test_list_line_includes_the_file_name(self):
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "the forum", "--new", "the market"
            ),
            0,
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.tex), "list"]), 0)
        pending_line = next(
            line for line in out.getvalue().splitlines() if "  pending  " in line
        )
        self.assertIn("paper.tex", pending_line)


if __name__ == "__main__":
    unittest.main()
