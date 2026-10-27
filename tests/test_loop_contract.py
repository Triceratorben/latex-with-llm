#!/usr/bin/env python3
"""The loop's contract: draft, review, accept or reject — no surprises.

The skill exists so a user can see a drafted change in the PDF and then
either keep it or revert it. These tests pin the guarantees that make that
loop trustworthy, beyond the per-command behaviour covered elsewhere:

- every draft is placed where TeX will typeset it, in every supported line
  ending style, so the review PDF really shows the change;
- the document's line endings stay uniform through draft -> accept, and
  reject restores the exact bytes;
- a draft that would corrupt another draft's block is refused, not written;
- the revert path survives the user deleting a file, and only recreates it
  on request;
- repairing a multi-file paper fixes the chapter the log points at;
- a nested chapter survives a snapshot -> restore round trip.

Run:  python3 -m unittest discover -s tests
"""

import contextlib
import io
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.latex_draft import (
    BLOCK_RE,
    backups_dir,
    list_snapshots,
    load_state,
    main,
    read_changelog,
    restore_snapshot,
    snapshot,
    tex_inputs,
)

LF_DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "Small problems are worth studying.\n"
    "\\end{document}\n"
)

# Same document, one line ending style per test.
CRLF_DOC = LF_DOC.replace("\n", "\r\n")
CR_DOC = LF_DOC.replace("\n", "\r")


def _eol_uniform(data: bytes, eol: bytes) -> bool:
    """True when every line break in `data` is exactly `eol`."""
    if eol == b"\n":
        return b"\r" not in data
    if eol == b"\r\n":
        return b"\r" not in data.replace(b"\r\n", b"")
    return b"\n" not in data and b"\r\n" not in data


def _kpse(name):
    exe = shutil.which("kpsewhich")
    return bool(
        exe
        and subprocess.run([exe, name], capture_output=True, text=True).stdout.strip()
    )


HAVE_TEX = (
    bool(shutil.which("latexmk") or shutil.which("pdflatex"))
    and bool(shutil.which("pdftotext"))
    and all(_kpse(f) for f in ("changes.sty", "ulem.sty", "todonotes.sty"))
)


class LoopCase(unittest.TestCase):
    """Fresh temp project per test; the CLI runs in-process."""

    doc = LF_DOC

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tex = self.dir / "paper.tex"
        self.populate()

    def populate(self):
        self.tex.write_bytes(self.doc.encode("utf-8"))

    def cli(self, *argv):
        return main([str(self.tex), *argv])

    def cli_out(self, *argv):
        """Run a command and capture what the user would read."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([str(self.tex), *argv])
        return rc, out.getvalue(), err.getvalue()

    def bytes(self, name="paper.tex"):
        return (self.dir / name).read_bytes()

    def text(self, name="paper.tex"):
        return self.bytes(name).decode("utf-8")

    def draft_ids(self):
        return [m.group(1) for m in BLOCK_RE.finditer(self.text())]

    def write(self, name, content):
        (self.dir / name).write_bytes(content.encode("utf-8"))


class TestDraftIsWhereTexTypesetsIt(LoopCase):
    """An invisible draft is worse than a refused one."""

    def test_cr_only_draft_lands_inside_the_body(self):
        self.doc = CR_DOC
        self.populate()
        self.assertEqual(self.cli("draft-insert", "--after", "Small problems", "--text", "Drafted."), 0)
        body = self.text()
        self.assertLess(body.index("\\begin{document}"), body.index("% latex-draft:"),
                        "the draft block was written outside the document environment")
        self.assertLess(body.index("% latex-draft:"), body.index("\\end{document}"))
        # ... and the preamble that makes \added work is loaded before it
        self.assertLess(body.index("\\usepackage[draft]{changes}"), body.index("\\begin{document}"))

    def test_cr_only_reject_restores_the_cr_bytes(self):
        self.doc = CR_DOC
        self.populate()
        original = self.bytes()
        self.assertEqual(self.cli("draft-insert", "--after", "Small problems", "--text", "Drafted."), 0)
        self.assertEqual(self.cli("reject", "--all"), 0)
        self.assertEqual(self.bytes(), original)

    def test_crlf_draft_block_stays_between_begin_and_end(self):
        self.doc = CRLF_DOC
        self.populate()
        self.assertEqual(self.cli("draft-insert", "--after", "Small problems", "--text", "Drafted."), 0)
        body = self.text()
        self.assertLess(body.index("\\begin{document}"), body.index("% latex-draft:"))
        self.assertLess(body.index("% latex-draft:"), body.index("\\end{document}"))

    def test_at_line_targets_the_same_visible_line_in_crlf(self):
        self.doc = LF_DOC.replace(
            "Small problems are worth studying.\n", "Line one.\nLine two.\n"
        ).replace("\n", "\r\n")
        self.populate()
        # visible line 4 is "Line two." (1 documentclass, 2 begin{document},
        # 3 "Line one."); the injected preamble and pending blocks are not
        # counted, so a second draft still sees the user's numbering.
        self.assertEqual(self.cli("draft-insert", "--at-line", "4", "--text", "Drafted."), 0)
        self.assertEqual(self.cli("accept", "--all"), 0)
        body = self.text()
        self.assertLess(body.index("Line one."), body.index("Drafted."))
        self.assertLess(body.index("Drafted."), body.index("Line two."))


class TestLineEndingsStayUniform(LoopCase):
    """A draft must not mix LF into a CRLF/CR document."""

    def test_crlf_accept_keeps_crlf_everywhere(self):
        self.doc = CRLF_DOC
        self.populate()
        self.assertEqual(
            self.cli("draft-insert", "--after", "Small problems", "--text", "One.\n\nTwo."), 0
        )
        self.assertEqual(self.cli("accept", "--all"), 0)
        self.assertTrue(
            _eol_uniform(self.bytes(), b"\r\n"),
            "accept left LF-only or bare-CR lines behind:\n%r" % self.bytes(),
        )

    def test_cr_accept_keeps_cr_everywhere(self):
        self.doc = CR_DOC
        self.populate()
        self.assertEqual(
            self.cli("draft-insert", "--after", "Small problems", "--text", "One.\n\nTwo."), 0
        )
        self.assertEqual(self.cli("accept", "--all"), 0)
        self.assertTrue(
            _eol_uniform(self.bytes(), b"\r"),
            "accept left LF or CRLF lines behind:\n%r" % self.bytes(),
        )

    def test_crlf_review_file_has_no_lone_lf_while_drafting(self):
        self.doc = CRLF_DOC
        self.populate()
        self.assertEqual(self.cli("draft-insert", "--after", "Small problems", "--text", "Drafted."), 0)
        self.assertTrue(
            _eol_uniform(self.bytes(), b"\r\n"),
            "the marked-up file the user reviews mixes line endings:\n%r" % self.bytes(),
        )

    def test_lf_documents_are_untouched_by_the_newline_handling(self):
        self.assertEqual(self.cli("draft-insert", "--after", "Small problems", "--text", "Drafted."), 0)
        self.assertEqual(self.cli("accept", "--all"), 0)
        self.assertTrue(_eol_uniform(self.bytes(), b"\n"))
        self.assertIn(b"Small problems are worth studying.\nDrafted.\n", self.bytes())


class TestNoSilentBlockCorruption(LoopCase):
    def test_replace_over_a_block_boundary_is_refused(self):
        """--old may not start outside a pending block and end inside it."""
        self.assertEqual(self.cli("draft-insert", "--at-line", "3", "--text", "Inserted."), 0)
        before = self.bytes()
        body = self.text()
        marker = re.search(r"% latex-draft-end:[0-9a-f]{8}", body).group(0)
        # starts in visible text, ends inside the block's end-marker line
        old = "Small problems are worth studying.\n" + marker[:14]
        rc = self.cli("draft-replace", "--old", old, "--new", "X")
        self.assertEqual(rc, 2)
        self.assertEqual(self.bytes(), before, "the refused draft still rewrote the file")
        rc = self.cli("accept", "--all")
        self.assertEqual(rc, 0, "the surviving draft became unresolvable")

    def test_adjacent_drafts_resolve_independently(self):
        """Accepting one draft must not swallow its neighbour's block."""
        self.doc = "\\documentclass{article}\n\\begin{document}\nAlpha.\nBeta.\n\\end{document}\n"
        self.populate()
        self.assertEqual(self.cli("draft-replace", "--old", "Alpha.", "--new", "Alpha v2."), 0)
        first = self.draft_ids()[0]
        self.assertEqual(self.cli("draft-replace", "--old", "Beta.", "--new", "Beta v2."), 0)
        second = [d for d in self.draft_ids() if d != first][0]

        self.assertEqual(self.cli("accept", "--id", first), 0)
        self.assertIn(f"% latex-draft:{second}", self.text(),
                      "the neighbour's block disappeared with the accepted draft")
        self.assertEqual(load_state(self.tex)["drafts"][second]["status"], "pending")

        self.assertEqual(self.cli("accept", "--id", second), 0)
        self.assertEqual(
            self.text(),
            "\\documentclass{article}\n\\begin{document}\nAlpha v2.\nBeta v2.\n\\end{document}\n",
        )

    def test_editing_text_a_pending_draft_adds_is_refused(self):
        self.assertEqual(self.cli("draft-insert", "--after", "Small problems", "--text", "Hello world."), 0)
        before = self.bytes()
        rc = self.cli("draft-replace", "--old", "Hello", "--new", "Goodbye")
        self.assertEqual(rc, 2, "editing added text would duplicate it on accept")
        self.assertEqual(self.bytes(), before)


class TestRevertSafetyNet(LoopCase):
    """The undo half of the loop must not crash or surprise."""

    def setUp(self):
        super().setUp()
        self.doc = "\\documentclass{article}\n\\begin{document}\n\\input{ch1}\n\\end{document}\n"
        self.populate()
        self.write("ch1.tex", "Chapter body.\n")

    def _draft_into_chapter(self):
        self.assertEqual(
            self.cli("draft-insert", "--file", "ch1.tex", "--after", "Chapter body.", "--text", "More."),
            0,
        )

    def test_undo_reports_a_deleted_chapter_instead_of_crashing(self):
        self._draft_into_chapter()
        (self.dir / "ch1.tex").unlink()
        self.assertEqual(self.cli("undo"), 1, "undo must report, not raise, on a deleted file")
        self.assertFalse((self.dir / "ch1.tex").exists(),
                         "undo silently resurrected a file the user deleted")

    def test_undo_force_recreates_a_deleted_chapter(self):
        self._draft_into_chapter()
        (self.dir / "ch1.tex").unlink()
        self.assertEqual(self.cli("undo", "--force"), 0)
        self.assertEqual((self.dir / "ch1.tex").read_text(), "Chapter body.\n")

    def test_status_and_check_survive_a_deleted_chapter(self):
        self._draft_into_chapter()
        (self.dir / "ch1.tex").unlink()
        self.assertEqual(self.cli("status"), 0)
        self.assertIn(self.cli("check"), (0, 1), "check must report, not raise")


class TestBackupsForMultiFilePapers(LoopCase):
    def setUp(self):
        super().setUp()
        self.doc = "\\documentclass{article}\n\\begin{document}\n\\input{chapters/ch1}\n\\end{document}\n"
        self.populate()
        (self.dir / "chapters").mkdir()
        self.write("chapters/ch1.tex", "Chapter one.\nChapter two.\n")

    def test_nested_chapter_is_discovered_from_the_main_directory(self):
        self.assertEqual(
            [p.relative_to(self.dir).as_posix() for p in tex_inputs(self.tex)],
            ["chapters/ch1.tex"],
        )

    def test_nested_chapter_roundtrips_through_a_full_snapshot(self):
        snap = snapshot(self.tex, "full", full=True)
        self.write("chapters/ch1.tex", "scribble\n")
        self.tex.write_text("scribble\n")
        restore_snapshot(self.tex, snap)
        self.assertEqual(self.text(), self.doc)
        self.assertEqual(self.text("chapters/ch1.tex"), "Chapter one.\nChapter two.\n")

    def test_per_change_snapshot_of_a_nested_chapter_holds_its_text(self):
        self.cli("status")  # full initial backup
        self.assertEqual(
            self.cli("draft-insert", "--file", "chapters/ch1.tex",
                     "--after", "Chapter one.", "--text", "More."),
            0,
        )
        newest = list_snapshots(self.tex)[0]
        copy = backups_dir(self.tex) / f"{newest.stem}__chapters__ch1.tex"
        self.assertTrue(copy.exists(), "no pre-change copy of the nested chapter")
        self.assertEqual(copy.read_text(), "Chapter one.\nChapter two.\n")

    def test_restore_recreates_a_nested_chapter_deleted_since(self):
        snap = snapshot(self.tex, "full", full=True)
        (self.dir / "chapters" / "ch1.tex").unlink()
        restore_snapshot(self.tex, snap)
        self.assertTrue((self.dir / "chapters" / "ch1.tex").is_file())


MISMATCH_LOG = """This is pdfTeX
! LaTeX Error: \\begin{itemize} on input line 2 ended by \\end{enumerate}.
! Emergency stop.
"""


class TestRepairInAChapter(LoopCase):
    """A multi-file paper is repaired in the file the log points at."""

    def setUp(self):
        super().setUp()
        self.doc = "\\documentclass{article}\n\\begin{document}\n\\input{ch1}\n\\end{document}\n"
        self.populate()
        self.write(
            "ch1.tex",
            "\\begin{itemize}\n\\item one\n\\end{enumerate}\n",
        )
        (self.dir / "paper.log").write_text(MISMATCH_LOG)

    def test_repair_fixes_the_mismatch_in_the_chapter(self):
        self.assertEqual(self.cli("repair"), 0)
        self.assertIn("\\end{itemize}", self.text("ch1.tex"))
        self.assertNotIn("enumerate", self.text("ch1.tex"))
        self.assertNotIn("\\end{itemize}", self.text(), "the main file was rewritten")

    def test_repair_changelog_attributes_the_chapter(self):
        self.cli("repair")
        entries = [e for e in read_changelog(self.tex) if e["kind"] == "change"]
        self.assertTrue(any(e.get("file") == "ch1.tex" for e in entries), entries)

    def test_repair_does_not_look_like_a_hand_edit_afterwards(self):
        """The next command must not warn about the change repair just made."""
        self.cli("repair")
        rc, out, err = self.cli_out("status")
        self.assertEqual(rc, 0)
        self.assertNotIn("WARNING", out + err,
                         "the tool reported its own repair as an external edit")

    def test_repair_is_undoable(self):
        self.cli("repair")
        self.assertEqual(self.cli("undo"), 0)
        self.assertIn("\\end{enumerate}", self.text("ch1.tex"))


@unittest.skipUnless(HAVE_TEX, "needs latexmk/pdflatex, pdftotext and the LaTeX packages")
class TestReviewPdfShowsTheDraft(LoopCase):
    """Only the compiled PDF can confirm the user really sees the change."""

    def compile_pdf(self):
        self.assertEqual(self.cli("compile"), 0)
        out = subprocess.run(
            ["pdftotext", str(self.tex.with_suffix(".pdf")), "-"],
            capture_output=True,
            text=True,
        ).stdout
        return " ".join(out.split())

    def test_cr_only_draft_is_visible_after_compile(self):
        self.doc = CR_DOC
        self.populate()
        self.assertEqual(self.cli("draft-insert", "--after", "Small problems", "--text", "Drafted."), 0)
        self.assertIn("Drafted.", self.compile_pdf())

    def test_crlf_draft_is_visible_after_compile(self):
        self.doc = CRLF_DOC
        self.populate()
        self.assertEqual(self.cli("draft-insert", "--after", "Small problems", "--text", "Drafted."), 0)
        self.assertIn("Drafted.", self.compile_pdf())

    def test_reject_after_a_crlf_draft_gives_back_the_original_pdf(self):
        self.doc = CRLF_DOC
        self.populate()
        original = self.compile_pdf()
        self.assertEqual(self.cli("draft-replace", "--old", "worth", "--new", "well worth"), 0)
        self.compile_pdf()
        self.assertEqual(self.cli("reject", "--all"), 0)
        self.assertEqual(self.compile_pdf(), original)


if __name__ == "__main__":
    unittest.main()
