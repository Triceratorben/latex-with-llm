#!/usr/bin/env python3
"""Commenter-mode (draft-comment) coverage parity with the draft modes.

draft-comment shares its plumbing with draft-insert (anchors, placement,
line endings, multi-file targets, undo) but only a slice of the scenarios
explored for the draft modes is pinned for comments. This file pins the
rest, mirroring cases the draft tests already cover:

* --before and --at-line anchors (only ever tested for draft-insert);
* --at-line beyond the end of the file is refused;
* CRLF and CR-only documents keep their line endings in the comment block
  and reject restores the original bytes (rw_01 covers this for drafts);
* --file targets an included file: draft, list's file column, accept,
  reject (rw_06 covers this for drafts);
* an unbalanced '}' in the comment text is escaped, and accept is
  verbatim (test_comment_fidelity pins accept; not the escaping);
* placement refusals: a footnote inside display math, a --margin note
  inside math and inside a float (ext_04 covers section titles only);
* two comments on the same anchor become two numbered blocks and
  reject --all removes both byte-exactly;
* accept --all with a comment and a replace pending resolves both;
* undo restores the pre-draft bytes;
* show --accepted previews a pending comment's footnote;
* an anchor that also occurs in a % comment still matches the real line.

Run:  python3 -m unittest discover -s tests
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tools.latex_draft import BLOCK_RE, load_state, main

DOC = (
    "\\documentclass{article}\n"
    "\\usepackage[draft]{changes}\n"
    "\\definechangesauthor[color=green!60!black]{AI}\n"
    "\\begin{document}\n"
    "First line stands alone.\n"
    "Second line follows.\n"
    "Third line ends here.\n"
    "\\end{document}\n"
)


class CommentCase(unittest.TestCase):
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
        return rc, out.getvalue(), err.getvalue()

    def write_doc(self, text):
        self.tex.write_text(text)

    def state(self):
        return load_state(self.tex)

    def pending_ids(self):
        return [
            did
            for did, rec in self.state()["drafts"].items()
            if rec["status"] == "pending"
        ]

    def blocks(self, text=None):
        text = text if text is not None else self.tex.read_text()
        return BLOCK_RE.findall(text)


class TestCommentAnchors(CommentCase):
    """--before and --at-line anchors, tested for draft-insert only so far."""

    def test_before_anchor_places_the_block_before_the_line(self):
        rc, out, err = self.run_cli_err(
            "draft-comment", "--before", "Third line", "--text", "note before"
        )
        self.assertEqual(rc, 0, err)
        lines = self.tex.read_text().splitlines()
        block_at = next(
            i for i, l in enumerate(lines) if l.startswith("% latex-draft:")
        )
        anchor_at = next(
            i for i, l in enumerate(lines) if l.startswith("Third line")
        )
        self.assertLess(block_at, anchor_at)
        self.assertIn(r"\footnote{", "\n".join(lines))

    def test_at_line_places_the_block_before_raw_line_n(self):
        # raw lines: 1 documentclass, 2 usepackage, 3 definechangesauthor,
        # 4 begin{document}, 5 First line, 6 Second line, 7 Third line
        rc, out, err = self.run_cli_err(
            "draft-comment", "--at-line", "6", "--text", "at line six"
        )
        self.assertEqual(rc, 0, err)
        lines = self.tex.read_text().splitlines()
        block_at = next(
            i for i, l in enumerate(lines) if l.startswith("% latex-draft:")
        )
        self.assertEqual(lines[block_at + 3], "Second line follows.")

    def test_at_line_beyond_the_end_is_refused(self):
        rc, out, err = self.run_cli_err(
            "draft-comment", "--at-line", "999", "--text", "x"
        )
        self.assertEqual(rc, 2)
        self.assertIn("beyond the end of the file", err)
        self.assertNotIn("drafted", out)

    def test_comment_on_the_last_body_line(self):
        rc, out, err = self.run_cli_err(
            "draft-comment",
            "--after", "Third line ends here.",
            "--text", "end note",
        )
        self.assertEqual(rc, 0, err)
        text = self.tex.read_text()
        self.assertIn(r"\footnote{\textsuperscript{1} end note}", text)
        # the block must stay inside the document environment
        self.assertLess(
            text.index("% latex-draft-end:"), text.index("\\end{document}")
        )

    def test_anchor_ignores_commented_duplicate(self):
        self.write_doc(
            "\\documentclass{article}\n"
            "\\usepackage[draft]{changes}\n"
            "\\definechangesauthor[color=green!60!black]{AI}\n"
            "\\begin{document}\n"
            "% First line stands alone. commented out\n"
            "First line stands alone.\n"
            "\\end{document}\n"
        )
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "First line stands alone.",
            "--text", "x",
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.blocks()), 1)


class TestCommentNewlineKinds(CommentCase):
    """rw_01 pins line endings for drafts; comments must behave the same."""

    def test_crlf_comment_block_uses_crlf_and_reject_restores_bytes(self):
        original = (
            "\\documentclass{article}\r\n"
            "\\usepackage[draft]{changes}\r\n"
            "\\definechangesauthor[color=green!60!black]{AI}\r\n"
            "\\begin{document}\r\n"
            "Body line one.\r\n"
            "Body line two.\r\n"
            "\\end{document}\r\n"
        )
        self.tex.write_bytes(original.encode())
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "Body line one.", "--text", "crlf note"
        )
        self.assertEqual(rc, 0, err)
        raw = self.tex.read_bytes()
        self.assertIn(b"\\footnote{\\textsuperscript{1} crlf note}\r\n", raw)
        self.assertNotIn(b"}\n", raw.replace(b"\r\n", b""))  # no bare-LF lines
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        self.assertEqual(self.tex.read_bytes(), original.encode())

    def test_cr_only_comment_block_uses_cr_and_reject_restores_bytes(self):
        original = (
            "\\documentclass{article}\r"
            "\\usepackage[draft]{changes}\r"
            "\\definechangesauthor[color=green!60!black]{AI}\r"
            "\\begin{document}\r"
            "Body line one.\r"
            "\\end{document}\r"
        )
        self.tex.write_bytes(original.encode())
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "Body line one.", "--text", "cr note"
        )
        self.assertEqual(rc, 0, err)
        raw = self.tex.read_bytes()
        self.assertIn(b"\\footnote{\\textsuperscript{1} cr note}\r", raw)
        self.assertNotIn(b"\n", raw)  # still CR-only
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        self.assertEqual(self.tex.read_bytes(), original.encode())


class TestCommentIncludedFile(CommentCase):
    """rw_06 pins --file for drafts; comments must behave the same."""

    def setUp(self):
        super().setUp()
        (self.dir / "parts").mkdir()
        self.intro = self.dir / "parts" / "intro.tex"
        self.intro.write_text("The intro line stands alone.\n")
        self.write_doc(
            "\\documentclass{article}\n"
            "\\usepackage[draft]{changes}\n"
            "\\definechangesauthor[color=green!60!black]{AI}\n"
            "\\begin{document}\n"
            "\\input{parts/intro}\n"
            "\\end{document}\n"
        )

    def test_comment_lands_in_the_included_file(self):
        rc, out, err = self.run_cli_err(
            "draft-comment", "--file", "parts/intro.tex",
            "--after", "The intro line", "--text", "note in included file",
        )
        self.assertEqual(rc, 0, err)
        self.assertIn(
            r"\footnote{\textsuperscript{1} note in included file}",
            self.intro.read_text(),
        )
        self.assertNotIn("footnote", self.tex.read_text())

    def test_list_names_the_included_file(self):
        self.run_cli(
            "draft-comment", "--file", "parts/intro.tex",
            "--after", "The intro line", "--text", "note",
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.tex), "list"]), 0)
        pending_line = next(
            line for line in out.getvalue().splitlines() if "  pending  " in line
        )
        self.assertIn("parts/intro.tex", pending_line)

    def test_accept_keeps_and_reject_removes_in_the_included_file(self):
        self.run_cli(
            "draft-comment", "--file", "parts/intro.tex",
            "--after", "The intro line", "--text", "note in included file",
        )
        did = self.pending_ids()[0]
        self.assertEqual(self.run_cli("accept", "--id", did), 0)
        self.assertIn(r"\footnote{", self.intro.read_text())
        accepted = self.intro.read_bytes()

        self.run_cli(
            "draft-comment", "--file", "parts/intro.tex",
            "--after", "The intro line", "--text", "second note",
        )
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        self.assertEqual(self.intro.read_bytes(), accepted)


class TestCommentTextSafety(CommentCase):
    """The sanitizer is pinned in test_latex_draft; pin the brace escaping."""

    def test_unbalanced_closing_brace_is_escaped(self):
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "First line", "--text", "prices } rise"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn(r"\footnote{\textsuperscript{1} prices \} rise}",
                      self.tex.read_text())

    def test_unbalanced_closing_brace_accepts_verbatim(self):
        self.run_cli(
            "draft-comment", "--after", "First line", "--text", "prices } rise"
        )
        did = self.pending_ids()[0]
        self.assertEqual(self.run_cli("accept", "--id", did), 0)
        self.assertIn(r"\footnote{prices \} rise}", self.tex.read_text())
        self.assertIn("rise", self.tex.read_text())


class TestCommentPlacementRefusals(CommentCase):
    """ext_04 pins section-title refusals; pin the math/float ones."""

    def _math_doc(self):
        self.write_doc(
            "\\documentclass{article}\n"
            "\\usepackage[draft]{changes}\n"
            "\\definechangesauthor[color=green!60!black]{AI}\n"
            "\\begin{document}\n"
            "\\begin{equation}\n"
            "E = mc^2\n"
            "\\end{equation}\n"
            "\\end{document}\n"
        )

    def test_footnote_inside_display_math_is_refused(self):
        self._math_doc()
        before = self.tex.read_bytes()
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "E = mc^2", "--text", "check this"
        )
        self.assertEqual(rc, 2)
        self.assertIn("display math", err)
        self.assertEqual(self.tex.read_bytes(), before)

    def test_margin_note_inside_math_is_refused(self):
        self._math_doc()
        before = self.tex.read_bytes()
        rc, out, err = self.run_cli_err(
            "draft-comment", "--margin", "--after", "E = mc^2",
            "--text", "check this",
        )
        self.assertEqual(rc, 2)
        self.assertIn("marginpar", err)
        self.assertEqual(self.tex.read_bytes(), before)

    def test_margin_note_inside_a_float_is_refused(self):
        self.write_doc(
            "\\documentclass{article}\n"
            "\\usepackage[draft]{changes}\n"
            "\\definechangesauthor[color=green!60!black]{AI}\n"
            "\\usepackage{graphicx}\n"
            "\\begin{document}\n"
            "\\begin{figure}\n"
            "\\caption{The caption line stands alone.}\n"
            "\\end{figure}\n"
            "\\end{document}\n"
        )
        before = self.tex.read_bytes()
        rc, out, err = self.run_cli_err(
            "draft-comment", "--margin", "--after", "The caption line",
            "--text", "caption note",
        )
        self.assertEqual(rc, 2)
        self.assertIn("float", err)
        self.assertEqual(self.tex.read_bytes(), before)


class TestCommentResolution(CommentCase):
    """Resolution scenarios pinned for drafts, now for comments too."""

    def test_two_comments_on_one_anchor_reject_all_restores_bytes(self):
        original = self.tex.read_bytes()
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "Second line", "--text", "first note"
            ),
            0,
        )
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "Second line", "--text", "second note"
            ),
            0,
        )
        self.assertEqual(len(self.pending_ids()), 2)
        # the two blocks carry consecutive block numbers
        text = self.tex.read_text()
        self.assertIn(r"\textsuperscript{1} first note", text)
        self.assertIn(r"\textsuperscript{2} second note", text)
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        self.assertEqual(self.tex.read_bytes(), original)
        self.assertEqual(self.pending_ids(), [])

    def test_accept_all_resolves_comment_and_replace_together(self):
        self.assertEqual(
            self.run_cli(
                "draft-replace", "--old", "Second line follows.",
                "--new", "Second line arrives.",
            ),
            0,
        )
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "Third line", "--text", "why changed"
            ),
            0,
        )
        self.assertEqual(len(self.pending_ids()), 2)
        self.assertEqual(self.run_cli("accept", "--all"), 0)
        text = self.tex.read_text()
        self.assertNotIn("latex-draft:", text)
        self.assertIn("Second line arrives.", text)
        self.assertNotIn("Second line follows.", text)
        self.assertIn(r"\footnote{why changed}", text)

    def test_undo_restores_the_pre_draft_bytes(self):
        original = self.tex.read_bytes()
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "Second line", "--text", "undo me"
            ),
            0,
        )
        # the draft may record a second changelog entry (author-mark setup);
        # undo --turn reverts every change of the turn, then the bytes must
        # match exactly
        rc, out, err = self.run_cli_err("undo", "--turn")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tex.read_bytes(), original)
        self.assertEqual(self.pending_ids(), [])

    def test_show_accepted_previews_a_pending_comment(self):
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "Second line",
                "--text", "pending note",
            ),
            0,
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.tex), "show", "--accepted"]), 0)
        self.assertIn(r"\footnote{pending note}", out.getvalue())
        # the body text is untouched
        self.assertIn("Second line follows.", out.getvalue())


if __name__ == "__main__":
    unittest.main()
