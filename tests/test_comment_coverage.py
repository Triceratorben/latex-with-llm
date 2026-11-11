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

And, from the suggested-tests review of the commenting feature:

* reviewer-style free text: bare %, #, _ (outside math), ^ (outside math),
  an odd unpaired $ and non-ASCII symbols (→, ≤, ×) are escaped or spelled
  so they stay visible and the build survives; already-escaped forms and
  $...$ math pass through; & is refused, never written;
* anchors in different parts of a paper: prose/\item/theorem/abstract lines
  work, a \section heading keeps the footnote in the body, and captions,
  table rows and equation interiors are refused cleanly;
* a six-comment review pass resolved out of drafting order: accepted
  footnotes are exactly \footnote{<text>} with no block-number prefix or
  marker lines, rejected ones leave no trace, and finalize keeps accepted
  footnotes while dropping the \todo and todonotes;
* a comment and an edit on the same sentence, in both orders, resolve
  independently (and the Order-B refusal points at a working --at-line);
* hard-wrapped sentences, multi-line comment text and unsafe --author
  labels.

Run:  python3 -m unittest discover -s tests
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import rw_support  # noqa: E402

from tools.latex_draft import BLOCK_RE, load_state, main  # noqa: E402

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


PAPER = (
    "\\documentclass{article}\n"
    "\\usepackage{amsthm}\n"
    "\\newtheorem{theorem}{Theorem}\n"
    "\\begin{document}\n"
    "\\begin{abstract}\n"
    "This paper studies a waiting line.\n"
    "\\end{abstract}\n"
    "\\section{Introduction}\n"
    "We study a queue at a bakery counter.\n"
    "\\begin{itemize}\n"
    "\\item Arrivals are counted by hand.\n"
    "\\end{itemize}\n"
    "\\begin{theorem}\n"
    "Every queue shortens eventually.\n"
    "\\end{theorem}\n"
    "\\begin{equation}\n"
    "q = 2 b + 1\n"
    "\\end{equation}\n"
    "\\begin{table}\n"
    "\\begin{tabular}{ll}\n"
    "Mon & 3 \\\\\n"
    "Tue & 5 \\\\\n"
    "\\end{tabular}\n"
    "\\end{table}\n"
    "\\begin{figure}\n"
    "\\centering\n"
    "\\fbox{placeholder}\n"
    "\\caption{A placeholder figure.}\n"
    "\\end{figure}\n"
    "The closing paragraph sums up the study.\n"
    "\\end{document}\n"
)


class PaperCase(CommentCase):
    """A realistic paper with abstract, theorem, equation, table, figure."""

    def setUp(self):
        super().setUp()
        self.write_doc(PAPER)


class TestCommentSpecialCharacters(CommentCase):
    """Reviewer-style free text: the special-character rows must either be
    escaped so they stay visible (and the build survives) or be refused
    cleanly with the file untouched. Probes the rows of the suggested-tests
    table one by one; the control rows must pass unchanged."""

    def draft(self, text):
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "Second line follows.", "--text", text
        )
        return rc, out, err

    def drafted_body(self):
        text = self.tex.read_text()
        blocks = self.blocks(text)
        self.assertEqual(len(blocks), 1)
        return blocks[0][1]

    def test_percent_is_escaped(self):
        rc, out, err = self.draft("Is 50% the right figure here?")
        self.assertEqual(rc, 0, err)
        body = self.drafted_body()
        self.assertIn(r"Is 50\% the right figure here?", body)
        self.assertNotIn("50%}", body)  # not truncated at the raw %

    def test_hash_is_escaped(self):
        rc, out, err = self.draft("See issue #3 and item #2.")
        self.assertEqual(rc, 0, err)
        self.assertIn(r"issue \#3 and item \#2.", self.drafted_body())

    def test_underscore_outside_math_is_escaped(self):
        rc, out, err = self.draft("The variable E_F is not defined before use.")
        self.assertEqual(rc, 0, err)
        self.assertIn(r"The variable E\_F is", self.drafted_body())

    def test_caret_outside_math_is_escaped(self):
        rc, out, err = self.draft("Check the scaling x^2 here.")
        self.assertEqual(rc, 0, err)
        self.assertIn(r"scaling x\^{}2 here.", self.drafted_body())

    def test_lone_dollar_is_escaped(self):
        rc, out, err = self.draft("The fee is $5 per ride.")
        self.assertEqual(rc, 0, err)
        body = self.drafted_body()
        self.assertIn(r"The fee is \$5 per ride.", body)
        # the escaped dollar must not open math that swallows the rest:
        # no unescaped $ may remain anywhere in the note
        self.assertNotRegex(body, r"(?<!\\)\$")

    def test_paired_dollars_keep_their_math_verbatim(self):
        rc, out, err = self.draft("The energy is $E_mc^2$ per unit.")
        self.assertEqual(rc, 0, err)
        # an even number of $ pairs up; _ and ^ inside math stay verbatim
        self.assertIn(r"The energy is $E_mc^2$ per unit.", self.drafted_body())

    def test_unicode_symbols_are_spelled_in_latex(self):
        rc, out, err = self.draft("The relation a \u2192 b is unclear; also \u2264 and \u00d7.")
        self.assertEqual(rc, 0, err)
        body = self.drafted_body()
        self.assertIn("a $\\rightarrow$ b", body)
        self.assertIn("$\\leq$", body)
        self.assertIn("$\\times$", body)
        self.assertNotIn("\u2192", body)

    def test_url_special_characters_are_escaped(self):
        rc, out, err = self.draft("See https://example.org/a_b?x=50%25#c")
        self.assertEqual(rc, 0, err)
        body = self.drafted_body()
        self.assertIn(r"a\_b?x=50\%25\#c", body)

    def test_ampersand_is_refused_not_written(self):
        original = self.tex.read_bytes()
        rc, out, err = self.draft("Use R&D consistently.")
        self.assertEqual(rc, 2)
        self.assertEqual(self.tex.read_bytes(), original)
        self.assertTrue(out.strip() or err.strip())  # the refusal says why

    def test_mathrm_control_row_passes_through(self):
        rc, out, err = self.draft(
            "The term \\mathrm{d}x and \\cite{smith2020} [3]."
        )
        self.assertEqual(rc, 0, err)
        body = self.drafted_body()
        # math-only command is spelled out visibly; \cite is text-safe
        self.assertIn(r"\textbackslash{}mathrm{d}x", body)
        self.assertIn(r"\cite{smith2020}", body)

    def test_special_characters_round_trip_on_accept_and_reject(self):
        text = "Is 50% the right figure here?"
        rc, out, err = self.draft(text)
        self.assertEqual(rc, 0, err)
        did = self.pending_ids()[0]
        self.assertEqual(self.run_cli("accept", "--id", did), 0)
        self.assertIn(
            r"\footnote{Is 50\% the right figure here?}", self.tex.read_text()
        )
        # a fresh draft: reject must restore the original bytes exactly
        before = self.tex.read_bytes()
        self.assertEqual(self.draft(text)[0], 0)
        did = self.pending_ids()[0]
        self.assertEqual(self.run_cli("reject", "--id", did), 0)
        self.assertEqual(self.tex.read_bytes(), before)


@rw_support.needs_tex
class TestCommentSpecialCharactersCompile(PaperCase):
    """The escaped comment text must still compile (needs a TeX engine)."""

    def test_escaped_rows_compile(self):
        rows = [
            "Is 50% the right figure here?",
            "See issue #3 and item #2.",
            "The variable E_F is not defined before use.",
            "Check the scaling x^2 here.",
            "The fee is $5 per ride.",
        ]
        for row in rows:
            with self.subTest(row=row):
                rc, out, err = self.run_cli_err(
                    "draft-comment", "--after", "We study a queue",
                    "--text", row,
                )
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.run_cli("accept", "--all"), 0)
                # the accepted note must not break the build
                self.assertEqual(self.run_cli("compile"), 0)


class TestCommentPaperParts(PaperCase):
    """Comments anchored in different parts of a paper (suggested test 2)."""

    def test_prose_item_theorem_and_abstract_lines_take_comments(self):
        for anchor in (
            "We study a queue at a bakery counter.",
            "\\item Arrivals are counted by hand.",
            "Every queue shortens eventually.",
            "This paper studies a waiting line.",
        ):
            with self.subTest(anchor=anchor):
                rc, out, err = self.run_cli_err(
                    "draft-comment", "--after", anchor, "--text", "note"
                )
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.run_cli("reject", "--all"), 0)

    def test_section_heading_comment_sits_in_the_body(self):
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "\\section{Introduction}",
            "--text", "Section reads well.",
        )
        self.assertEqual(rc, 0, err)
        text = self.tex.read_text()
        # the title itself is untouched: the footnote is a whole line after it
        self.assertIn("\\section{Introduction}\n% latex-draft:", text)
        self.assertNotIn("\\section{Introduction\\footnote", text)
        self.assertLess(
            text.index("\\footnote"), text.index("We study a queue")
        )
        # ... and a --margin note works there too (the docs recommend it)
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "\\section{Introduction}",
            "--text", "Margin on the heading.", "--margin",
        )
        self.assertEqual(rc, 0, err)

    def test_caption_footnote_is_refused(self):
        original = self.tex.read_bytes()
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "\\caption{A placeholder figure.}",
            "--text", "Is this figure needed?",
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.tex.read_bytes(), original)
        # the refusal says what to do instead
        self.assertIn("float", out + err)
        # --margin is refused inside a float too (documented)
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "\\caption{A placeholder figure.}",
            "--text", "Is this figure needed?", "--margin",
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.tex.read_bytes(), original)

    def test_table_row_comment_is_refused(self):
        original = self.tex.read_bytes()
        for extra in ([], ["--margin"]):
            with self.subTest(margin=bool(extra)):
                rc, out, err = self.run_cli_err(
                    "draft-comment", "--after", "Mon & 3 \\\\",
                    "--text", "Check this row.", *extra,
                )
                self.assertEqual(rc, 2)
                self.assertEqual(self.tex.read_bytes(), original)

    def test_equation_interior_comment_is_refused(self):
        original = self.tex.read_bytes()
        for extra in ([], ["--margin"]):
            with self.subTest(margin=bool(extra)):
                rc, out, err = self.run_cli_err(
                    "draft-comment", "--after", "q = 2 b + 1",
                    "--text", "Check this formula.", *extra,
                )
                self.assertEqual(rc, 2)
                self.assertEqual(self.tex.read_bytes(), original)


class TestCommentMixedResolution(PaperCase):
    """A normal review pass resolved in a mixed order (suggested test 3)."""

    def setUp(self):
        super().setUp()
        # six paragraphs, no hand-written changes preamble: the tool injects
        # what it needs, so `check`/`finalize` see a realistic state
        self.write_doc(
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "One: the first paragraph stands alone here.\n"
            "Two: the second paragraph follows the first.\n"
            "Three: the third paragraph is shorter still.\n"
            "Four: the fourth paragraph mentions the queue.\n"
            "Five: the fifth paragraph closes the argument.\n"
            "Six: the sixth paragraph is the last one here.\n"
            "\\end{document}\n"
        )

    def draft_six(self):
        notes = [
            ("One: the first", "FOOTNOTE: Cite the source here.", False),
            ("Two: the second", "Tighten this sentence.", False),
            ("Three: the third", "FOOTNOTE: Define the queue length.", False),
            ("Four: the fourth", "Check the number.", False),
            ("Five: the fifth", "Margin: rewrite later.", True),
            ("Six: the sixth", "Margin: add a citation.", True),
        ]
        for anchor, text, margin in notes:
            argv = ["draft-comment", "--after", anchor, "--text", text]
            if margin:
                argv.append("--margin")
            self.assertEqual(self.run_cli(*argv), 0)

    def test_mixed_order_resolution(self):
        self.draft_six()
        # resolve out of drafting order: accept 5, 1, 3; reject 6, 2, 4
        for block, want in ((6, "reject"), (5, "accept"), (2, "reject"),
                            (1, "accept"), (4, "reject"), (3, "accept")):
            self.assertEqual(self.run_cli(want, "--block", str(block)), 0)
        text = self.tex.read_text()
        # accepted footnotes: exactly the original --text, no block-number
        # prefix, no marker lines, on the line after the anchor
        self.assertIn(
            "One: the first paragraph stands alone here.\n"
            "\\footnote{FOOTNOTE: Cite the source here.}\n",
            text,
        )
        self.assertIn(
            "Three: the third paragraph is shorter still.\n"
            "\\footnote{FOOTNOTE: Define the queue length.}\n",
            text,
        )
        self.assertNotIn("\\textsuperscript", text)
        self.assertNotIn("% latex-draft:", text)
        # rejected comments leave no trace
        self.assertNotIn("Tighten this sentence.", text)
        self.assertNotIn("Check the number.", text)
        # the accepted margin note is still there, and todonotes with it
        self.assertIn("\\todo{Margin: rewrite later.}", text)
        self.assertIn("\\usepackage{todonotes}", text)
        self.assertEqual(self.run_cli("check"), 0)
        # finalize: the todo goes, todonotes goes, the footnotes stay; the
        # working copy is untouched
        self.assertEqual(self.run_cli("finalize", "--out", str(self.dir / "clean")), 0)
        clean = (self.dir / "clean" / "paper.tex").read_text()
        self.assertNotIn("\\todo", clean)
        self.assertNotIn("todonotes", clean)
        self.assertIn("\\footnote{FOOTNOTE: Cite the source here.}", clean)
        self.assertIn("\\footnote{FOOTNOTE: Define the queue length.}", clean)
        self.assertIn("\\todo{Margin: rewrite later.}", self.tex.read_text())


class TestCommentWithEdit(CommentCase):
    """A comment and an edit on the same sentence (suggested test 4)."""

    SENTENCE = "The fee is five euros per ride and riders complain."
    NEW = "The fee is six euros per ride and riders complain."

    def doc(self):
        return (
            "\\documentclass{article}\n"
            "\\usepackage[draft]{changes}\n"
            "\\definechangesauthor[color=green!60!black]{AI}\n"
            "\\begin{document}\n"
            + self.SENTENCE + "\n"
            "\\end{document}\n"
        )

    def ids_by_op(self):
        out = {}
        for did, rec in self.state()["drafts"].items():
            if rec["status"] == "pending":
                out[rec["op"]] = did
        return out

    def test_order_a_comment_then_edit(self):
        self.write_doc(self.doc())
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "The fee is five euros",
                "--text", "Why five?",
            ),
            0,
        )
        self.assertEqual(
            self.run_cli("draft-replace", "--old", self.SENTENCE,
                         "--new", self.NEW),
            0,
        )
        # both drafts stay pending
        self.assertEqual(sorted(self.ids_by_op()), ["comment", "replace"])
        # accept the comment, reject the edit: original sentence + footnote
        ids = self.ids_by_op()
        self.assertEqual(self.run_cli("accept", "--id", ids["comment"]), 0)
        self.assertEqual(self.run_cli("reject", "--id", ids["replace"]), 0)
        text = self.tex.read_text()
        self.assertIn(self.SENTENCE + "\n\\footnote{Why five?}", text)
        self.assertNotIn(self.NEW, text)

    def test_order_a_edit_accepted_comment_rejected(self):
        self.write_doc(self.doc())
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "The fee is five euros",
                "--text", "Why five?",
            ),
            0,
        )
        self.assertEqual(
            self.run_cli("draft-replace", "--old", self.SENTENCE,
                         "--new", self.NEW),
            0,
        )
        ids = self.ids_by_op()
        self.assertEqual(self.run_cli("accept", "--id", ids["replace"]), 0)
        self.assertEqual(self.run_cli("reject", "--id", ids["comment"]), 0)
        text = self.tex.read_text()
        self.assertIn(self.NEW, text)
        self.assertNotIn("\\footnote", text)
        self.assertNotIn(self.SENTENCE, text)

    def test_order_b_edit_then_comment_refuses_with_a_working_fallback(self):
        self.write_doc(self.doc())
        self.assertEqual(
            self.run_cli("draft-replace", "--old", self.SENTENCE,
                         "--new", self.NEW),
            0,
        )
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "The fee is five euros",
            "--text", "Why five?",
        )
        # refused: the anchor only exists inside the pending draft block, and
        # the message must lead somewhere (resolve, another anchor, --at-line)
        self.assertEqual(rc, 2)
        message = out + err
        self.assertIn("pending draft", message)
        self.assertIn("--at-line", message)
        self.assertEqual(self.tex.read_text().count("\\footnote"), 0)
        # the --at-line fallback: the first line after the pending block
        lines = self.tex.read_text().splitlines()
        end = max(
            i for i, ln in enumerate(lines) if "latex-draft-end:" in ln
        )
        rc, out, err = self.run_cli_err(
            "draft-comment", "--at-line", str(end + 2),
            "--text", "Why five?",
        )
        self.assertEqual(rc, 0, err)
        text = self.tex.read_text()
        # the comment must not land INSIDE the pending replace block (right
        # after its opening marker line)
        # the comment block sits OUTSIDE the pending replace block
        comment_pos = text.index("\\footnote{")
        block_end = text.index("% latex-draft-end:")
        self.assertGreater(comment_pos, block_end)
        # accepting the edit keeps the comment (still pending) next to the
        # edited sentence
        ids = self.ids_by_op()
        self.assertEqual(self.run_cli("accept", "--id", ids["replace"]), 0)
        text = self.tex.read_text()
        self.assertIn(self.NEW, text)
        self.assertIn("Why five?", text)
        # accepting the comment too leaves the final footnote form
        self.assertEqual(self.run_cli("accept", "--all"), 0)
        self.assertIn("\\footnote{Why five?}", self.tex.read_text())


class TestCommentAlsoWorthAdding(CommentCase):
    """The cheap extras from the suggested-tests list."""

    def test_hard_wrapped_sentence_block_never_splits_a_line(self):
        self.write_doc(
            "\\documentclass{article}\n"
            "\\usepackage[draft]{changes}\n"
            "\\definechangesauthor[color=green!60!black]{AI}\n"
            "\\begin{document}\n"
            "The queue grows on Mondays as the table shows, see\n"
            "\\cite{smith2020} for the numbers and the method.\n"
            "\\end{document}\n"
        )
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "The queue grows on Mondays",
                "--text", "Split sentence: merge the lines?",
            ),
            0,
        )
        text = self.tex.read_text()
        # the block is whole lines directly after the anchored line; the
        # anchored line (and the \cite on the next one) are untouched
        lines = text.splitlines()
        anchor = next(
            i for i, ln in enumerate(lines)
            if "The queue grows on Mondays" in ln
        )
        self.assertTrue(lines[anchor + 1].startswith("% latex-draft:"))
        self.assertTrue(lines[anchor + 2].startswith("\\footnote{"))
        self.assertIn("\\cite{smith2020} for the numbers", text)

    def test_multiline_comment_text_survives_accept_and_reject(self):
        long_text = (
            "First line of the note.\n\n"
            "Second paragraph continues. And a very long tail that keeps "
            "going and going and going to make sure nothing is truncated "
            "anywhere in the pipeline at all."
        )
        original = self.tex.read_bytes()
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "First line",
                "--text", long_text,
            ),
            0,
        )
        did = self.pending_ids()[0]
        self.assertEqual(self.run_cli("reject", "--id", did), 0)
        self.assertEqual(self.tex.read_bytes(), original)
        self.assertEqual(
            self.run_cli(
                "draft-comment", "--after", "First line",
                "--text", long_text,
            ),
            0,
        )
        did = self.pending_ids()[0]
        self.assertEqual(self.run_cli("accept", "--id", did), 0)
        self.assertIn(long_text, self.tex.read_text())

    def test_author_labels_with_special_characters_are_made_safe(self):
        rc, out, err = self.run_cli_err(
            "draft-comment", "--after", "First line",
            "--text", "Note.", "--author", "R&D 50%",
        )
        self.assertEqual(rc, 0, err)
        text = self.tex.read_text()
        # the unsafe characters are stripped from the id (RD-50), which the
        # preamble records and the block uses
        self.assertIn("RD-50", text)
        did = self.pending_ids()[0]
        self.assertEqual(self.run_cli("accept", "--id", did), 0)
        self.assertIn("\\footnote{Note.}", self.tex.read_text())


if __name__ == "__main__":
    unittest.main()
