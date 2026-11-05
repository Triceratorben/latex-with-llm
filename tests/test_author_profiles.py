"""Regression tests for the changes-author profiles and the agent's symbol mark.

On the first successful body draft the tool defines one changes profile per
paper author (parsed from \\author{...}) plus its own AI profile, each with
its own color. The agent's mark in the PDF is a filled circle (\\bullet), not
a text tag: the AI profile redefines \\Changes@output@author so the AI id
renders as the circle while every other author keeps the ordinary (Name)
tag. The setup is a "setup" changelog entry, so undo replays it and reject
strips every injected line again.
"""

import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from tools.latex_draft import (
    AGENT_AUTHOR,
    AUTHOR_COLORS,
    BLOCK_RE,
    CHANGES_PACKAGE_LINE,
    author_line,
    load_state,
    main,
    paper_author_ids,
    read_changelog,
)

PAPER = """\\documentclass{article}
\\author{Alice Smith \\and Bob Jones}
\\title{A Small Paper}
\\begin{document}
\\section{Introduction}
Small problems are worth studying.
\\end{document}
"""


class ProfileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(PAPER)

    def run_cli(self, *argv):
        return main([str(self.tex), *argv])

    def state(self):
        return load_state(self.tex)

    def text(self):
        return self.tex.read_text()

    def strip_block_markers(self, text: str) -> str:
        return re.sub(r"\\textsuperscript\{\d+\} ", "", text)


class TestPaperAuthorIds(unittest.TestCase):
    def test_and_separated_names(self):
        self.assertEqual(
            paper_author_ids(r"\author{Alice Smith \and Bob Jones}"),
            ["AliceSmith", "BobJones"],
        )

    def test_thanks_and_titles_are_stripped(self):
        ids = paper_author_ids(
            r"\author{Dr. Carol Danvers\thanks{Dept. of Urban Planning}}"
        )
        self.assertEqual(ids, ["DrCarolDanvers"])

    def test_comma_separated(self):
        self.assertEqual(
            paper_author_ids(r"\author{Alice Smith, Bob Jones}"),
            ["AliceSmith", "BobJones"],
        )

    def test_no_author_command(self):
        self.assertEqual(paper_author_ids(r"\documentclass{article}"), [])

    def test_duplicates_removed(self):
        self.assertEqual(
            paper_author_ids(r"\author{Alice Smith \and Alice Smith}"),
            ["AliceSmith"],
        )


class TestAuthorLine(unittest.TestCase):
    def test_agent_profile_has_symbol_hook(self):
        line = author_line(AGENT_AUTHOR, "red")
        self.assertIn(r"\definechangesauthor[color=red]{AI}", line)
        self.assertIn(r"\makeatletter", line)
        # The hook sits on \Changes@output@author (which receives the bare
        # author id), not on the protected position wrapper.
        self.assertIn(r"\def\Changes@output@author#1{", line)
        self.assertIn(r"\pdfstrcmp{#1}{AI}=0", line)
        self.assertIn(r"\ensuremath{\bullet}", line)
        self.assertIn(r"\let\latexdraft@orig@out\Changes@output@author", line)

    def test_human_profile_has_name_and_color(self):
        line = author_line("AliceSmith", "blue")
        self.assertEqual(
            line, r"\definechangesauthor[color=blue,name=AliceSmith]{AliceSmith}"
        )

    def test_agent_gets_first_palette_color(self):
        self.assertIn(f"color={AUTHOR_COLORS[0]}", author_line(AGENT_AUTHOR, AUTHOR_COLORS[0]))


class TestFirstDraftProfileSetup(ProfileCase):
    def test_profiles_defined_once_on_first_draft(self):
        self.assertEqual(self.run_cli("draft-insert", "--after", "studying.", "--text", " More."), 0)
        self.assertTrue(self.state().get("profiles_defined"))
        # second draft must not redefine anything
        self.assertEqual(self.run_cli("draft-insert", "--after", "studying.", "--text", " Even more."), 0)
        text = self.text()
        self.assertEqual(text.count(r"\definechangesauthor[color=green!60!black]{AI}"), 1)
        self.assertEqual(text.count(r"\definechangesauthor[color=blue,name=AliceSmith]{AliceSmith}"), 1)
        self.assertEqual(text.count(r"\definechangesauthor[color=orange,name=BobJones]{BobJones}"), 1)
        self.assertEqual(text.count(r"\makeatletter"), 1)
        setups = [e for e in read_changelog(self.tex) if e.get("kind") == "setup"]
        self.assertEqual(len(setups), 1)

    def test_agent_block_right_after_package(self):
        self.run_cli("draft-insert", "--after", "studying.", "--text", " More.")
        text = self.text()
        pkg = text.index(CHANGES_PACKAGE_LINE)
        agent = text.index(r"\definechangesauthor[color=green!60!black]{AI}")
        self.assertEqual(agent, pkg + len(CHANGES_PACKAGE_LINE) + 1)

    def test_wrapper_only_when_define_already_present(self):
        # pre-seed the document the way the draft handler leaves it before
        # the profile hook runs: package + plain agent define, no wrapper
        text = PAPER.replace(
            "\\begin{document}",
            CHANGES_PACKAGE_LINE + "\n" + r"\definechangesauthor[color=green!60!black]{AI}" + "\n\\begin{document}",
        )
        self.tex.write_text(text)
        self.assertEqual(
            self.run_cli("draft-insert", "--after", "studying.", "--text", " More."), 0
        )
        out = self.text()
        self.assertEqual(out.count(r"\definechangesauthor[color=green!60!black]{AI}"), 1)
        self.assertIn(r"\def\Changes@output@author#1{", out)
        self.assertIn(r"\definechangesauthor[color=blue,name=AliceSmith]{AliceSmith}", out)

    def test_failed_draft_defines_no_profiles(self):
        rc = self.run_cli("draft-replace", "--old", "no such text", "--new", "x")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("definechangesauthor", self.text())
        self.assertFalse(self.state().get("profiles_defined"))

    def test_deleted_uses_plain_id_tag(self):
        self.run_cli("draft-replace", "--old", "Small problems", "--new", "Big problems")
        block = self.strip_block_markers(BLOCK_RE.search(self.text()).group(2))
        self.assertIn(r"\deleted[id=AI]{Small}", block)
        self.assertIn(r"\added[id=AI]{Big}", block)
        self.assertNotIn("comment=", block)

    def test_reject_all_strips_every_profile_line(self):
        self.run_cli("draft-replace", "--old", "Small problems", "--new", "Big problems")
        self.assertIn("definechangesauthor", self.text())
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        text = self.text()
        self.assertNotIn("definechangesauthor", text)
        self.assertNotIn("{changes}", text)
        self.assertNotIn(r"\makeatletter", text)
        self.assertNotIn(r"\latexdraft@", text)
        self.assertEqual(text, PAPER)
        self.assertEqual(self.run_cli("check"), 0)

    def test_crlf_document_keeps_crlf(self):
        self.tex.write_text(PAPER.replace("\n", "\r\n"))
        self.run_cli("draft-insert", "--after", "studying.", "--text", " More.")
        text = self.tex.read_bytes().decode()
        self.assertIn("\r\n", text)
        self.assertEqual(text.count("\n"), text.count("\r\n"))  # no bare-LF line

    def test_undo_of_draft_and_setup_restores_original(self):
        self.run_cli("draft-replace", "--old", "Small problems", "--new", "Big problems")
        kinds = [e.get("kind") for e in read_changelog(self.tex)]
        self.assertIn("setup", kinds)
        self.assertEqual(self.run_cli("undo", "--n", "2"), 0)
        self.assertEqual(self.text(), PAPER)

    def test_human_author_draft_uses_human_profile(self):
        self.run_cli(
            "draft-replace", "--old", "Small problems", "--new", "Big problems", "--author", "AliceSmith"
        )
        block = self.strip_block_markers(BLOCK_RE.search(self.text()).group(2))
        self.assertIn(r"\added[id=AliceSmith]{Big}", block)
        # the AI profile still exists exactly once, plus the paper authors
        text = self.text()
        self.assertEqual(text.count(r"\definechangesauthor[color=green!60!black]{AI}"), 1)
        self.assertIn(r"\definechangesauthor[color=blue,name=AliceSmith]{AliceSmith}", text)


if __name__ == "__main__":
    unittest.main()
