"""GROUP 9 - CLI contract.

Question answered: the README documents exit codes (0 success; 1 nothing to
do / runtime failure / check failed; 2 usage error) and an LLM agent branches
on them. Do the commands honor that contract, including argparse-level misuse?

Run:  python3 tests/test_ext_09_cli_contract.py
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




class ExitCodes(Base):
    def test_usage_errors_exit_2(self):
        (self.dir / "other.tex").write_text("unrelated\n")
        self.tex.write_bytes((DOC + "\\section{Introduction}\n").encode())  # anchor now ambiguous
        cases = {
            "ambiguous anchor": ["draft-insert", "--after", INTRO, "--text", "x"],
            "replace: text not found": ["draft-replace", "--old", "NOPE", "--new", "x"],
            "unknown draft id": ["accept", "--id", "deadbeef"],
            "--file that is not an input": ["draft-insert", "--file", "other.tex", "--after", "unrelated", "--text", "x"],
            "--file that does not exist": ["draft-insert", "--file", "missing.tex", "--after", "x", "--text", "x"],
        }
        for name, argv in cases.items():
            with self.subTest(name):
                self.assertEqual(self.cli(*argv), 2, self.err)

    def test_argparse_misuse_exits_2(self):
        cases = {
            "--after and --before together": ["draft-insert", "--after", "a", "--before", "b", "--text", "x"],
            "no anchor": ["draft-insert", "--text", "x"],
            "no --text": ["draft-insert", "--after", "a"],
            "accept without --id/--all": ["accept"],
            "undo --n with --turn": ["undo", "--n", "2", "--turn"],
            "preamble without position": ["draft-preamble", "--text", "x"],
            "unknown command": ["frobnicate"],
        }
        for name, argv in cases.items():
            with self.subTest(name):
                with self.assertRaises(SystemExit) as cm:
                    self.cli(*argv)
                self.assertEqual(cm.exception.code, 2)

    def test_nothing_to_undo_and_failed_check_exit_1(self):
        self.assertEqual(self.cli("undo"), 1)
        self.assertEqual(self.cli("undo", "--turn"), 1)
        self.cli("draft-insert", "--after", INTRO, "--text", "x")
        self.assertEqual(self.cli("check"), 1)

    def test_resolving_nothing_is_not_an_error(self):
        self.assertEqual(self.cli("accept", "--all"), 0)
        self.assertEqual(self.cli("reject", "--all"), 0)


class ReportingCommands(Base):
    def test_list_status_and_changelog_agree_with_what_happened(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "One.")
        one = self.draft_id()
        self.cli("draft-insert", "--after", SENTENCE, "--text", "Two.")
        self.cli("accept", "--id", one)
        self.cli("list")
        rows = [l.split() for l in self.out.strip().splitlines()]
        # list output starts with the block number (#N), then the draft id
        self.assertEqual(sorted(r[2] for r in rows), ["accepted", "pending"])
        self.cli("status")
        self.assertIn("2 total, 1 pending", self.out)
        self.cli("changelog", "--last", "1")
        self.assertEqual(len(self.out.strip().splitlines()), 1)

    def test_changelog_last_zero_prints_nothing_but_succeeds(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "One.")
        self.assertEqual(self.cli("changelog", "--last", "0"), 0)
        self.assertEqual(self.out.strip(), "")


if __name__ == "__main__":
    unittest.main()
