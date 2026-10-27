"""GROUP 7 - Dependency discovery for multi-file papers.

Question answered: does tex_inputs() find exactly the files pdflatex would
read? Everything the tool does for chapters (backups, hand-edit detection,
--file drafting, undo) depends on this list; a file missing from it is
invisible to all of those, and a file wrongly included is tracked for nothing.

TeX resolves \\input/\\include paths relative to the directory pdflatex is run
in (the main file's directory), not relative to the including file.

Run:  python3 tests/test_ext_07_input_discovery.py
"""

import contextlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
from tools.latex_draft import load_state, main, read_changelog  # noqa: E402,F401

SENTENCE = "Small problems are worth studying."
INTRO = "\\section{Introduction}"
DOC = (
    "\\documentclass{article}\n"
    "\\title{A Small Paper}\n"
    "\\begin{document}\n"
    f"{INTRO}\n"
    f"{SENTENCE}\n"
    "\\end{document}\n"
)


class Base(unittest.TestCase):
    """Fresh temp project per test; cli() runs the tool in-process."""

    doc = DOC

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tex = self.dir / "paper.tex"
        self.populate()

    def populate(self):
        self.tex.write_bytes(self.doc.encode("utf-8"))  # exact bytes

    def reset(self):
        for p in self.dir.iterdir():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        self.populate()

    def cli(self, *argv, tex=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([str(tex or self.tex), *argv])
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def text(self, path=None):
        return (path or self.tex).read_bytes().decode("utf-8")

    def draft_id(self):
        """Id of the draft created by the last draft-* command."""
        return re.search(r"drafted .*?\b([0-9a-f]{8})\b", self.out).group(1)


from tools.latex_draft import tex_inputs  # noqa: E402


class Discovery(Base):
    def make(self, main_body, files):
        self.tex.write_bytes(
            ("\\documentclass{article}\n\\begin{document}\n" + main_body + "\\end{document}\n").encode())
        for name, content in files.items():
            p = self.dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

    def found(self):
        return sorted(str(p.relative_to(self.dir)) for p in tex_inputs(self.tex))

    def test_input_with_extension_and_include_without(self):
        self.make("\\input{ch1.tex}\n\\include{ch2}\n", {"ch1.tex": "a\n", "ch2.tex": "b\n"})
        self.assertEqual(self.found(), ["ch1.tex", "ch2.tex"])

    def test_missing_files_are_ignored(self):
        self.make("\\input{nope}\n\\input{ch1}\n", {"ch1.tex": "a\n"})
        self.assertEqual(self.found(), ["ch1.tex"])

    def test_file_included_twice_is_listed_once(self):
        self.make("\\input{ch1}\n\\input{ch1}\n", {"ch1.tex": "a\n"})
        self.assertEqual(self.found(), ["ch1.tex"])

    def test_nested_input_path_is_resolved_from_the_main_directory_like_tex_does(self):
        self.make("\\input{chapters/ch1}\n", {
            "chapters/ch1.tex": "One.\n\\input{chapters/sec}\n",  # correct for pdflatex run in the main dir
            "chapters/sec.tex": "Section text.\n",
        })
        self.assertEqual(self.found(), ["chapters/ch1.tex", "chapters/sec.tex"])

    def test_drafting_into_a_file_nested_two_levels_deep(self):
        self.make("\\input{chapters/ch1}\n", {
            "chapters/ch1.tex": "One.\n\\input{chapters/sec}\n",
            "chapters/sec.tex": "Section text.\n",
        })
        rc = self.cli("draft-insert", "--file", "chapters/sec.tex", "--after", "Section text.", "--text", "Drafted.")
        self.assertEqual(rc, 0, self.err)

    def test_commented_out_input_is_not_a_dependency(self):
        self.make("% \\input{old}\n\\input{ch1} % \\input{older}\n", {
            "ch1.tex": "a\n", "old.tex": "x\n", "older.tex": "y\n"})
        self.assertEqual(self.found(), ["ch1.tex"])


if __name__ == "__main__":
    unittest.main()
