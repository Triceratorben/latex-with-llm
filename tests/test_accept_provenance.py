#!/usr/bin/env python3
"""Accepted LLM text stays marked (opt-in via .latex-draft.cfg).

The loop is the same, but `accept` no longer throws the provenance away: the
text stays wrapped in changes-package markup, so the review PDF still shows
what the model wrote. In final mode the package prints that text plainly,
which is what a production build wants.

Run:  python3 -m unittest discover -s tests
"""

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.latex_draft import load_state, main

DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "Small problems are worth studying.\n"
    "\\end{document}\n"
)

MARKED_DOC = (
    "\\documentclass{article}\n"
    "\\usepackage[draft]{changes}\n"
    "\\definechangesauthor[name=LLM]{LLM}\n"
    "\\begin{document}\n"
    "Small problems are worth studying.\n"
    "\\end{document}\n"
)


def _kpse(name):
    exe = shutil.which("kpsewhich")
    return bool(
        exe
        and subprocess.run([exe, name], capture_output=True, text=True).stdout.strip()
    )


HAVE_TEX = (
    bool(shutil.which("latexmk") or shutil.which("pdflatex"))
    and bool(shutil.which("pdftotext"))
    and all(_kpse(f) for f in ("changes.sty", "ulem.sty"))
)


class ProvenanceCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(DOC)

    def configure(self, text):
        (self.dir / ".latex-draft.cfg").write_text(text)

    def cli(self, *argv):
        return main([str(self.tex), *argv])

    def cli_out(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([str(self.tex), *argv])
        return rc, out.getvalue(), err.getvalue()

    def text(self):
        return self.tex.read_text()

    def draft_and_accept(self, *draft_args):
        self.assertEqual(self.cli(*draft_args), 0)
        did = next(
            d for d, r in load_state(self.tex)["drafts"].items() if r["status"] == "pending"
        )
        self.assertEqual(self.cli("accept", "--id", did), 0)
        return did


class TestDefaultBehaviour(ProvenanceCase):
    def test_without_config_accept_is_plain(self):
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        self.assertIn("Drafted.", self.text())
        self.assertNotIn("\\added", self.text())
        self.assertNotIn("changes", self.text())

    def test_explicit_false_is_the_same(self):
        self.configure("[accept]\nkeep_markup = false\n")
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        self.assertNotIn("\\added", self.text())


class TestKeepMarkup(ProvenanceCase):
    def setUp(self):
        super().setUp()
        self.configure("[accept]\nkeep_markup = true\n")

    def test_accepted_insert_stays_marked(self):
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        self.assertIn("\\added[id=LLM]{Drafted.}", self.text())
        # the package must stay loaded, or the kept markup cannot compile
        self.assertIn("\\usepackage[draft]{changes}", self.text())
        self.assertIn("\\definechangesauthor", self.text())

    def test_accepted_replace_keeps_only_the_new_text_marked(self):
        self.draft_and_accept(
            "draft-replace", "--old", "Small problems", "--new", "Big problems"
        )
        body = self.text()
        self.assertIn("\\added[id=LLM]{Big problems are worth studying.}", body)
        self.assertNotIn("Small problems", body)
        self.assertNotIn("\\deleted", body)

    def test_accepted_delete_leaves_nothing_to_mark(self):
        self.draft_and_accept("draft-delete", "--old", "Small problems are worth studying.")
        self.assertNotIn("Small problems", self.text())
        self.assertNotIn("\\added", self.text())

    def test_multi_paragraph_accept_marks_one_command_per_paragraph(self):
        self.draft_and_accept(
            "draft-insert", "--after", "Small problems", "--text", "One.\n\nTwo."
        )
        body = self.text()
        self.assertIn("\\added[id=LLM]{One.}", body)
        self.assertIn("\\added[id=LLM]{Two.}", body)
        self.assertIn("\\added[id=LLM]{One.}\n\n\\added[id=LLM]{Two.}", body)

    def test_alternatives_keep_their_own_author(self):
        self.assertEqual(
            self.cli("draft-replace", "--author", "A",
                     "--old", "Small problems are worth studying.", "--new", "Version A."), 0)
        ids = {d for d, r in load_state(self.tex)["drafts"].items() if r["status"] == "pending"}
        did_a = next(d for d in ids if load_state(self.tex)["drafts"][d]["author"] == "A")
        self.assertEqual(self.cli("accept", "--id", did_a), 0)
        self.assertIn("\\added[id=A]{Version A.}", self.text())

    def test_check_passes_and_points_at_final_mode(self):
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        rc, out, err = self.cli_out("check")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("final", out + err)

    def test_check_flags_preamble_once_the_markup_is_gone(self):
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        # user removes the kept markup by hand: now the injected preamble is
        # an actual leftover again
        self.tex.write_text(
            self.tex.read_text().replace("\\added[id=LLM]{Drafted.}", "Drafted.")
        )
        rc, out, err = self.cli_out("check")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("leftover", out + err)

    def test_check_does_not_nag_when_already_final(self):
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        self.tex.write_text(self.tex.read_text().replace("[draft]{changes}", "[final]{changes}"))
        rc, out, err = self.cli_out("check")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("keep_markup", out + err)

    def test_drafting_over_marked_text_is_refused(self):
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        before = self.text()
        rc, out, err = self.cli_out("draft-replace", "--old", "Drafted.", "--new", "Other.")
        self.assertEqual(rc, 2)
        self.assertIn("cannot nest", err)
        self.assertEqual(self.text(), before)


class TestConfig(ProvenanceCase):
    def test_style_and_color_are_applied(self):
        self.configure("[accept]\nkeep_markup = true\nstyle = highlight\ncolor = grey\n")
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        body = self.text()
        self.assertIn("\\highlight[id=LLM]{Drafted.}", body)
        # xcolor has "gray", not "grey": the alias must be applied, or every
        # compile dies with "Undefined color"
        self.assertIn("\\definechangesauthor[color=gray,name=LLM]{LLM}", body)

    def test_invalid_style_is_a_clean_error(self):
        self.configure("[accept]\nkeep_markup = true\nstyle = glowing\n")
        rc, out, err = self.cli_out("draft-insert", "--after", "Small problems", "--text", "X.")
        self.assertEqual(rc, 2)
        self.assertIn("style", err)
        self.assertEqual(self.text(), DOC, "a bad config must not modify the document")

    def test_invalid_color_is_a_clean_error(self):
        self.configure("[accept]\ncolor = not a color\n")
        rc, out, err = self.cli_out("draft-insert", "--after", "Small problems", "--text", "X.")
        self.assertEqual(rc, 2)
        self.assertIn("xcolor", err)

    def test_broken_config_syntax_is_a_clean_error(self):
        self.configure("[accept\nkeep_markup = true\n")
        rc, out, err = self.cli_out("draft-insert", "--after", "Small problems", "--text", "X.")
        self.assertEqual(rc, 2)
        self.assertIn(".latex-draft.cfg", err)
        self.assertEqual(self.text(), DOC)


class TestRepairInjection(ProvenanceCase):
    """repair injects the same preamble, with the configured author color."""

    def populate(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n\\added{Orphan.}\n\\end{document}\n"
        )

    def log(self):
        (self.dir / "paper.log").write_text(
            "This is pdfTeX\n! Undefined control sequence.\n<recently read> \\added \n"
        )

    def test_repair_injects_the_changes_preamble(self):
        self.populate()
        self.log()
        self.assertEqual(self.cli("repair"), 0)
        self.assertIn("\\usepackage[draft]{changes}", self.text())

    def test_repair_uses_the_configured_color(self):
        self.populate()
        self.configure("[accept]\ncolor = grey\n")
        self.log()
        self.assertEqual(self.cli("repair"), 0)
        self.assertIn("color=gray", self.text())


class TestLineEndings(ProvenanceCase):
    def test_kept_markup_uses_the_documents_separator(self):
        self.configure("[accept]\nkeep_markup = true\n")
        self.tex.write_bytes(DOC.replace("\n", "\r\n").encode())
        self.assertEqual(
            self.cli("draft-insert", "--after", "Small problems", "--text", "One.\n\nTwo."), 0
        )
        self.assertEqual(self.cli("accept", "--all"), 0)
        data = self.tex.read_bytes()
        rest = data.replace(b"\r\n", b"")
        self.assertNotIn(b"\n", rest, "accept-kept markup introduced lone LF lines")
        self.assertEqual(self.cli("check"), 0)

    def test_reject_after_turning_the_option_off_is_still_exact(self):
        # The option only affects accept; drafting/rejecting stays byte-exact.
        original = self.tex.read_text()
        self.assertEqual(self.cli("draft-insert", "--after", "Small problems", "--text", "X."), 0)
        self.assertEqual(self.cli("reject", "--all"), 0)
        self.assertEqual(self.tex.read_text(), original)


@unittest.skipUnless(HAVE_TEX, "needs latexmk/pdflatex, pdftotext, changes.sty")
class TestCompile(ProvenanceCase):
    def setUp(self):
        super().setUp()
        self.configure("[accept]\nkeep_markup = true\n")

    def pdf_text(self, tex=None):
        tex = tex or self.tex
        out = subprocess.run(
            ["pdftotext", str(tex.with_suffix(".pdf")), "-"], capture_output=True, text=True
        ).stdout
        return " ".join(out.split())

    def test_marked_text_compiles_and_survives_final_mode(self):
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        self.assertEqual(self.cli("compile"), 0)
        review = self.pdf_text()
        self.assertIn("Drafted.", review)
        self.assertEqual(review.count("Drafted."), 1, "accepted text was duplicated")

        # production: the same document with the package in final mode
        final = self.dir / "final.tex"
        final.write_text(
            self.tex.read_text().replace(
                "\\usepackage[draft]{changes}", "\\usepackage[final]{changes}"
            )
        )
        self.assertEqual(main([str(final), "compile"]), 0)
        self.assertIn("Drafted.", self.pdf_text(final))
        self.assertIn("Small problems are worth studying.", self.pdf_text(final))

    def test_grey_alias_compiles(self):
        """xcolor has no `grey`; the alias must keep the document buildable."""
        self.configure("[accept]\nkeep_markup = true\nstyle = highlight\ncolor = grey\n")
        self.draft_and_accept("draft-insert", "--after", "Small problems", "--text", "Drafted.")
        self.assertEqual(self.cli("compile"), 0, "a 'grey' color broke the compile")
        self.assertIn("Drafted.", self.pdf_text())


if __name__ == "__main__":
    unittest.main()
