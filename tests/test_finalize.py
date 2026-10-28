#!/usr/bin/env python3
"""`finalize`: a clean copy of the sources with every tool trace removed.

The review loop leaves markup in the document on purpose. `finalize` writes
a second, human-readable copy in which the corrections are materialized:
accepted text stays, wrappers/notes/markers/injected preamble go, and the
copy builds the same PDF as `changes` in final mode. The original document
and its history are untouched.

Run:  python3 -m unittest discover -s tests
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.latex_draft import main

DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "Alpha sentence.\n"
    "Beta sentence.\n"
    "Gamma sentence.\n"
    "\\end{document}\n"
)


class FinalizeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(DOC)
        self.out = self.dir / "out"

    def cli(self, *argv):
        return main([str(self.tex), *argv])

    def finalize(self, *extra):
        rc = self.cli("finalize", "--out", str(self.out), *extra)
        self.assertEqual(rc, 0)
        return self.out / "paper.tex"

    def text(self, path=None):
        return (path or self.tex).read_text()

    def accepted_edits(self):
        """One accepted insert, replace and delete."""
        for argv in (
            ["draft-insert", "--after", "Alpha sentence.", "--text", "Inserted."],
            ["draft-replace", "--old", "Beta sentence.", "--new", "Beta revised."],
            ["draft-delete", "--old", "Gamma sentence."],
        ):
            self.assertEqual(self.cli(*argv), 0)
        self.assertEqual(self.cli("accept", "--all"), 0)


class TestCleanCopy(FinalizeCase):
    def test_markup_is_gone_and_the_corrections_are_kept(self):
        self.accepted_edits()
        clean = self.finalize().read_text()
        self.assertIn("Inserted.", clean)
        self.assertIn("Beta revised.", clean)
        self.assertNotIn("Gamma sentence.", clean)
        for trace in ("\\added", "\\deleted", "\\comment", "% latex-draft",
                      "\\usepackage[draft]{changes}", "\\definechangesauthor"):
            self.assertNotIn(trace, clean, trace)

    def test_the_original_document_is_untouched(self):
        self.accepted_edits()
        before = self.tex.read_bytes()
        self.finalize()
        self.assertEqual(self.tex.read_bytes(), before)
        self.assertTrue((self.dir / "paper.tex.latex-draft.json").exists())

    def test_pending_drafts_block_finalizing(self):
        self.assertEqual(
            self.cli("draft-insert", "--after", "Alpha sentence.", "--text", "Pending."), 0
        )
        rc = self.cli("finalize", "--out", str(self.out))
        self.assertEqual(rc, 1)
        self.assertFalse((self.out / "paper.tex").exists())
        self.assertEqual(self.cli("accept", "--all"), 0)
        self.assertEqual(self.cli("finalize", "--out", str(self.out)), 0)

    def test_kept_markup_is_unwrapped(self):
        (self.dir / ".latex-draft.cfg").write_text("[accept]\nkeep_markup = true\n")
        self.assertEqual(
            self.cli("draft-insert", "--after", "Alpha sentence.", "--text", "Marked."), 0
        )
        self.assertEqual(self.cli("accept", "--all"), 0)
        self.assertIn("\\added[id=LLM]{Marked.}", self.text())
        clean = self.finalize().read_text()
        self.assertIn("Alpha sentence.\nMarked.\n", clean)
        self.assertNotIn("\\added", clean)
        self.assertNotIn("\\usepackage[draft]{changes}", clean)

    def test_a_label_note_is_removed_but_the_label_stays(self):
        rc = self.cli(
            "draft-insert", "--unmarked", "--after", "Alpha sentence.",
            "--text", "\\label{sec:alpha}",
        )
        self.assertEqual(rc, 0)
        clean = self.finalize().read_text()
        self.assertIn("\\label{sec:alpha}", clean)
        self.assertNotIn("labelled by", clean)
        self.assertNotIn("\\comment", clean)

    def test_multi_file_sources_keep_their_paths_and_assets_are_copied(self):
        (self.dir / "figures").mkdir()
        (self.dir / "figures" / "plot.pdf").write_bytes(b"%PDF-1.4 fake\n")
        (self.dir / "refs.bib").write_text("@article{x, title={X}}\n")
        (self.dir / "ch1.tex").write_text("Chapter body. \\label{ch:one}\n")
        self.tex.write_text(
            "\\documentclass{article}\n\\usepackage{graphicx}\n"
            "\\bibliography{refs}\n\\begin{document}\n\\input{ch1}\n"
            "\\begin{figure}[h]\n\\includegraphics[width=0.5\\textwidth]{figures/plot}\n"
            "\\caption{A plot.}\n\\end{figure}\n\\end{document}\n"
        )
        self.assertEqual(
            self.cli("draft-insert", "--file", "ch1.tex", "--after", "Chapter body.",
                     "--text", "Drafted."), 0
        )
        self.assertEqual(self.cli("accept", "--all"), 0)
        self.finalize()
        self.assertIn("Drafted.", (self.out / "ch1.tex").read_text())
        self.assertNotIn("\\added", (self.out / "ch1.tex").read_text())
        self.assertIn("\\input{ch1}", (self.out / "paper.tex").read_text(),
                      "finalize is not a flattener")
        self.assertTrue((self.out / "figures" / "plot.pdf").is_file())
        self.assertTrue((self.out / "refs.bib").is_file())

    def test_finalizing_twice_gives_the_same_copy(self):
        self.accepted_edits()
        first = self.finalize().read_bytes()
        second = self.finalize().read_bytes()
        self.assertEqual(first, second)

    def test_nested_and_two_argument_markup(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\added[id=LLM]{Outer \\added[id=A]{inner} text.}\n"
            "\\replaced[id=LLM]{New text.}{Old text.}\n"
            "\\todo{Fix this.} \\comment[id=LLM]{note}\n"
            "\\end{document}\n"
        )
        clean = self.finalize().read_text()
        self.assertIn("Outer inner text.", clean)
        self.assertIn("New text.", clean)
        self.assertNotIn("Old text.", clean)
        self.assertNotIn("Fix this.", clean)
        self.assertNotIn("note", clean)

    def test_report_counts_what_it_removed(self):
        # Traces survive accept in three ways: kept markup, a label note, an
        # accepted margin comment (which stays a \todo).
        (self.dir / ".latex-draft.cfg").write_text("[accept]\nkeep_markup = true\n")
        self.assertEqual(
            self.cli("draft-insert", "--after", "Alpha sentence.", "--text", "Marked."), 0
        )
        self.assertEqual(
            self.cli("draft-comment", "--margin", "--after", "Beta sentence.",
                     "--text", "Note."), 0
        )
        self.assertEqual(self.cli("accept", "--all"), 0)
        self.assertEqual(
            self.cli("draft-insert", "--unmarked", "--after", "Alpha sentence.",
                     "--text", "\\label{sec:alpha}"), 0
        )
        rc, out = self._cli_capture("finalize", "--out", str(self.out))
        self.assertEqual(rc, 0)
        for word in ("added", "todo", "comment", "file(s)"):
            self.assertIn(word, out)
        clean = (self.out / "paper.tex").read_text()
        self.assertIn("Marked.", clean)
        self.assertIn("\\label{sec:alpha}", clean)
        self.assertNotIn("\\todo", clean)
        self.assertNotIn("labelled by", clean)

    def _cli_capture(self, *argv):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([str(self.tex), *argv])
        return rc, out.getvalue() + err.getvalue()


@unittest.skipUnless(
    shutil.which("latexmk") or shutil.which("pdflatex"),
    "needs a LaTeX engine",
)
class TestCleanCopyCompiles(FinalizeCase):
    def compile_pdf(self, tex):
        engine = shutil.which("latexmk")
        cmd = (
            [engine, "-pdf", "-interaction=nonstopmode", tex.name]
            if engine
            else ["pdflatex", "-interaction=nonstopmode", tex.name]
        )
        for _ in range(2):
            proc = subprocess.run(cmd, cwd=tex.parent, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout[-1500:])
        out = subprocess.run(
            ["pdftotext", str(tex.with_suffix(".pdf")), "-"],
            capture_output=True,
            text=True,
        ).stdout
        return " ".join(out.split())

    def test_the_clean_copy_builds_the_corrected_document(self):
        self.accepted_edits()
        clean = self.finalize()
        text = self.compile_pdf(clean)
        self.assertIn("Inserted.", text)
        self.assertIn("Beta revised.", text)
        self.assertNotIn("Gamma sentence.", text)
        self.assertNotIn("labelled by", text)


if __name__ == "__main__":
    unittest.main()
