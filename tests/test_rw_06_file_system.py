"""GROUP 6 - File-system realities: odd paths, symlinks, CRLF, BOM, leftovers.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.rw_support import PaperCase, plain_view

BASE = (
    "\\documentclass{article}\n\\begin{document}\n"
    "A first sentence with plain text.\n"
    "\\end{document}\n"
)


class OddPaths(PaperCase):
    files = {"paper.tex": BASE}

    def test_path_with_spaces(self):
        nested = self.dir / "my papers"
        nested.mkdir()
        (nested / "paper.tex").write_text(BASE, encoding="utf-8")
        self.assertEqual(self.cli(
            "draft-replace", "--old", "plain text", "--new", "simple text",
            main_file="my papers/paper.tex"), 0, self.err)
        self.assertEqual(self.cli("accept", "--all", main_file="my papers/paper.tex"), 0, self.err)
        self.assertIn("simple text", plain_view((nested / "paper.tex").read_text(encoding="utf-8")))

    def test_symlinked_main_file(self):
        link = self.dir.parent / "linked.tex"
        os.symlink(self.dir / "paper.tex", link)
        self.assertEqual(self.cli(
            "draft-replace", "--old", "plain text", "--new", "simple text",
            main_file=str(link)), 0, self.err)
        self.assertEqual(self.cli("accept", "--all", main_file=str(link)), 0, self.err)
        self.assertTrue(link.is_symlink(), "the symlink was replaced by a regular file")
        self.assertIn("simple text", plain_view(self.read()))

    def test_crlf_file(self):
        self.write("paper.tex", BASE.replace("\n", "\r\n"))
        rc = self.cli("draft-replace", "--old", "plain text", "--new", "simple text")
        self.assertEqual(rc, 0, "a CRLF file must be editable")
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        self.assertIn("simple text", plain_view(self.read()))

    def test_utf8_bom_file(self):
        bom = b"\xef\xbb\xbf"
        self.write("paper.tex", bom + BASE.encode("utf-8"))
        rc = self.cli("draft-replace", "--old", "plain text", "--new", "simple text")
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        self.assertIn("simple text", plain_view(self.read()))
        self.assertTrue(self.read("paper.tex").startswith("\ufeff"))

    def test_editor_leftovers_are_ignored(self):
        for name in ("paper.tex~", ".paper.tex.swp", "#paper.tex#", "paper.tex.bak"):
            self.write(name, "stale editor leftovers\n")
        self.assertEqual(self.cli("check"), 0, self.out)
        self.assertEqual(self.cli(
            "draft-replace", "--old", "plain text", "--new", "simple text"), 0, self.err)
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        self.assertIn("simple text", plain_view(self.read()))
        self.assertEqual(self.read("paper.tex~"), "stale editor leftovers\n")


if __name__ == "__main__":
    unittest.main()
