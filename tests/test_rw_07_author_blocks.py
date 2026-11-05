"""GROUP 7 - Real `\\author` blocks.

The tool derives change-tracking author profiles from the document, and every
real `\\author{...}` is messier than "Jane Doe": TeX accents, UTF-8 names,
apostrophes and particles, `\\thanks`, affiliations after `\\\\`, `$^{1,*}$`
superscripts, `%` comments inside the block, a commented-out older `\\author`,
an empty one, none at all, thirty authors, duplicates, `authblk` syntax, and
names that look like the agent's own labels.

For each block the same promises must hold:
  * drafting works and the review source defines each change-author id once,
    with an id that is safe to use in `\\added[id=...]`
  * the review PDF compiles (skipped without a TeX engine)
  * accept-all leaves the `\\author{...}` block byte-identical and no
    `\\definechangesauthor` behind; reject-all and `undo --turn` restore the
    original bytes

Run:  python3 tests/test_rw_07_author_blocks.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.rw_support import HAVE_TEX, PaperCase, needs_tex, plain_view  # noqa: E402


FIRST = "First sentence of the paper."
SECOND = "Second sentence of the paper."


def doc(author_block, extra_preamble=""):
    return ("\\documentclass{article}\n\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n"
            + extra_preamble + "\n\\title{A Title}\n" + author_block + "\n\\date{}\n"
            "\\begin{document}\n\\maketitle\n" + FIRST + "\n" + SECOND + "\n\\end{document}\n")


AUTHORS = {
    "single plain name": "\\author{Jane Doe}",
    "utf-8 names": "\\author{Jos\u00e9 N\u00fa\u00f1ez \\and Anna M\u00fcller \\and \u00d8yvind \u00c5berg}",
    "tex accent macros": "\\author{Jos'e N'u~nez \\and Anna M\"uller \\and \\c{C}etin \\O{}stergaard \\and \\v{S}koda}",
    "apostrophe hyphen particles": "\\author{Conor O'Brien \\and Jean-Luc Picard \\and Ludwig van der Waals \\and J. R. R. Tolkien}",
    "thanks and affiliation": ("\\author{Jane Doe\\thanks{Corresponding author: jane@example.org} \\and\n"
                               "  John Smith\\\\ Department of Urban Planning, Example University}"),
    "superscript affiliations": ("\\author{A. Author$^{1,*}$ \\and B. Author$^{2}$\\\\"
                                 "\n  {\\small $^1$Univ A, $^2$Univ B}}"),
    "comments inside the block": "\\author{Jane Doe % first author\n  \\and John Smith % second\n}",
    "commented-out old author": "%\\author{Old Name}\n\\author{New Name}",
    "last name first with textsc": "\\author{\\textsc{Doe}, Jane \\and \\textsc{Smith}, John}",
    "empty author": "\\author{}",
    "no author command": "",
    "duplicate names": "\\author{Jane Doe \\and Jane Doe}",
    "names that resemble agent labels": "\\author{AI Researcher \\and LLM Smith \\and Claude Shannon}",
    "thirty authors": "\\author{" + " \\and ".join(f"Author {chr(65 + i % 26)}{i}" for i in range(30)) + "}",
    "single-letter initials": "\\author{A. B. \\and C. D. \\and E.}",
}

DEFINE = re.compile(r"\\definechangesauthor\[(.*?)\]\{(.*?)\}")


class AuthorBlocks(PaperCase):
    files = {"paper.tex": doc(AUTHORS["single plain name"])}

    def use(self, text):
        self.files = {"paper.tex": text}
        self.reset()

    def draft(self):
        return self.cli("draft-replace", "--old", FIRST, "--new", "First sentence of this paper.")

    def test_review_source_defines_each_author_id_once_and_safely(self):
        for name, block in AUTHORS.items():
            with self.subTest(name):
                self.use(doc(block))
                self.assertEqual(self.draft(), 0, self.err)
                ids = [m.group(2) for m in DEFINE.finditer(self.read())]
                self.assertEqual(len(ids), len(set(ids)), f"duplicate change-author ids: {ids}")
                for i in ids:
                    self.assertRegex(i, r"^[A-Za-z0-9_-]+$", "id is not safe inside \\added[id=...]")
                self.assertTrue(ids, "no change author was defined")

    @needs_tex
    def test_review_pdf_compiles_for_every_author_block(self):
        for name, block in AUTHORS.items():
            with self.subTest(name):
                self.use(doc(block))
                self.draft()
                self.compile()

    def test_accept_all_leaves_the_author_block_untouched(self):
        for name, block in AUTHORS.items():
            with self.subTest(name):
                original = doc(block)
                self.use(original)
                self.draft()
                self.assertEqual(self.cli("accept", "--all"), 0, self.err)
                accepted = self.read()
                self.assertEqual(plain_view(accepted),
                                 original.replace(FIRST, "First sentence of this paper.", 1))
                # keep_markup keeps the changes preamble until finalize; the
                # finalize view must carry no \definechangesauthor.
                self.assertNotIn("\\definechangesauthor", plain_view(accepted))
                self.assertEqual(self.cli("check"), 0, self.out)

    def test_reject_all_and_undo_restore_the_original_bytes(self):
        for name, block in AUTHORS.items():
            with self.subTest(name):
                original = doc(block)
                self.use(original)
                self.draft()
                self.cli("draft-insert", "--after", SECOND, "--text", "A third sentence.")
                self.assertEqual(self.cli("reject", "--all"), 0, self.err)
                self.assertEqual(self.read(), original)
                self.use(original)
                self.draft()
                self.assertEqual(self.cli("undo", "--turn"), 0, self.err)
                self.assertEqual(self.read(), original)

    def test_several_authors_drafting_in_one_turn_keep_distinct_ids(self):
        for name in ("utf-8 names", "thirty authors", "duplicate names"):
            with self.subTest(name):
                self.use(doc(AUTHORS[name]))
                for label, text in (("Ann", "First sentence, version A."),
                                    ("Bob", "First sentence, version B.")):
                    self.assertEqual(self.cli("draft-replace", "--author", label, "--old", FIRST,
                                              "--new", text), 0, self.err)
                ids = [m.group(2) for m in DEFINE.finditer(self.read())]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(self.cli("reject", "--all"), 0, self.err)

    def test_explicit_author_labels_are_refused_or_safe(self):
        """--author is free text: a label with spaces, accents, `&`, `%` or an
        apostrophe must be refused cleanly or turned into a safe id."""
        for label in ("Reviewer2", "J. Smith", "Dr M\u00fcller", "O'Brien",
                      "A&B", "50%", "x y z", "\\evil"):
            with self.subTest(label):
                self.use(doc(AUTHORS["single plain name"]))
                original = self.read()
                rc = self.cli("draft-replace", "--author", label, "--old", FIRST,
                              "--new", "First sentence of this paper.")
                if rc != 0:
                    self.assertEqual(rc, 2, self.err)
                    self.assertEqual(self.read(), original)
                    continue
                ids = [m.group(2) for m in DEFINE.finditer(self.read())]
                for i in ids:
                    self.assertNotRegex(i, r"[%&#$^~\\{}]", "TeX-special character in a change-author id")
                if HAVE_TEX:
                    self.compile()
                self.assertEqual(self.cli("reject", "--all"), 0, self.err)
                self.assertEqual(self.read(), original)

    def test_authblk_style_author_and_affil_commands(self):
        try:
            import shutil
            import subprocess
            kpse = shutil.which("kpsewhich")
            have_authblk = bool(kpse and subprocess.run(
                [kpse, "authblk.sty"], capture_output=True, text=True).stdout.strip())
        except Exception:
            have_authblk = False
        if not have_authblk:
            self.skipTest("authblk.sty not installed")
        block = ("\\author[1]{Jane Doe}\n\\author[2]{John Smith}\n"
                 "\\affil[1]{Department A}\n\\affil[2]{Department B}")
        original = doc(block, "\\usepackage{authblk}\n")
        self.use(original)
        self.assertEqual(self.draft(), 0, self.err)
        if HAVE_TEX:
            self.compile()
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        self.assertEqual(plain_view(self.read()),
                         original.replace(FIRST, "First sentence of this paper.", 1))


if __name__ == "__main__":
    unittest.main()
