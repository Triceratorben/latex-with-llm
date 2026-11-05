"""GROUP 8 - A thesis-sized, multi-file project.

Six chapters (~120 hard-wrapped sentences each) in `chapters/`, pulled in with
`\\input` and `\\include`, a shared .bib, boilerplate sentences repeated across
chapters, thirty drafts spread over the files in one turn, a hand edit in one
chapter between turns. Checks exact per-file results (accept / reject / undo),
that identical sentences in different chapters never cross over, that the tool
stays responsive at this size, and that `finalize` keeps the relative paths.

Run:  python3 tests/test_rw_08_thesis_project.py
"""

from __future__ import annotations

import re
import sys
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.rw_support import PaperCase, needs_tex, plain_view  # noqa: E402


N_CHAPTERS = 6
THESIS_BIB = (
    "@article{demaio2009,\n  author = {DeMaio, Paul},\n"
    "  title = {Bike-sharing: History, impacts, models of provision},\n  journal = {Journal of Public Transportation},\n"
    "  year = {2009}, volume = {12}, pages = {41--56}\n}\n")
BOILERPLATE = "This chapter is organized as follows."


def chapter(c):
    paragraphs = []
    for p in range(20):
        sents = []
        for k in range(6):
            tag = f"{c}.{p}.{k}"
            if k == 1:
                sents.append(f"Claim {tag} holds for the network under study~\\cite{{demaio2009}}.")
            elif k == 4:
                sents.append(f"Claim {tag} gives $x_{{{c}}} = {p}{k}$ in the light-traffic limit.")
            else:
                sents.append(f"Claim {tag} holds for the network under study, as shown by measurement {tag}.")
        if p == 0:
            sents.insert(0, BOILERPLATE)
        paragraphs.append(textwrap.fill(" ".join(sents), width=72))
    return f"\\section{{Chapter {c}}}\n\\label{{ch:{c}}}\n\n" + "\n\n".join(paragraphs) + "\n"


def thesis_files():
    body = "".join(f"\\input{{chapters/ch{c}}}\n" if c <= 4 else f"\\include{{chapters/ch{c}}}\n"
                   for c in range(1, N_CHAPTERS + 1))
    main = ("\\documentclass[11pt]{article}\n\\usepackage[numbers]{natbib}\n"
            "\\begin{document}\n" + body +
            "\\bibliographystyle{plainnat}\n\\bibliography{refs}\n\\end{document}\n")
    files = {"paper.tex": main, "refs.bib": THESIS_BIB}
    for c in range(1, N_CHAPTERS + 1):
        files[f"chapters/ch{c}.tex"] = chapter(c)
    return files


def edit(c, p):
    """A unique point edit in chapter c, paragraph p."""
    return (f"chapters/ch{c}.tex", f"Claim {c}.{p}.2 holds", f"Claim {c}.{p}.2 remains valid")


EDITS = [edit(c, p) for c in range(1, N_CHAPTERS + 1) for p in (2, 5, 8, 11, 14)]  # 30 edits


class Thesis(PaperCase):
    files = thesis_files()

    def snapshot(self):
        return {name: self.read(name) for name in self.files}

    def draft_all(self):
        for path, old, new in EDITS:
            self.assertEqual(self.cli("draft-replace", "--file", path, "--old", old, "--new", new),
                             0, self.err)

    def expected(self, original, edits=EDITS):
        want = dict(original)
        for path, old, new in edits:
            want[path] = want[path].replace(old, new, 1)
        return want

    def test_status_lists_every_chapter_and_nothing_else(self):
        self.write("chapters/notes-to-self.tex", "scratch\n")
        self.cli("status")
        self.assertEqual(self.cli("check"), 0, self.out)
        self.assertEqual(self.cli("draft-replace", "--file", "chapters/notes-to-self.tex",
                                  "--old", "scratch", "--new", "x"), 2)

    def test_thirty_edits_across_six_files_accept_exactly(self):
        original = self.snapshot()
        self.draft_all()
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        want = self.expected(original)
        got = {name: plain_view(text) for name, text in self.snapshot().items()}
        self.assertEqual(got, want)

    def test_thirty_edits_reject_and_undo_restore_every_file_byte_for_byte(self):
        original = self.snapshot()
        self.draft_all()
        self.assertEqual(self.cli("reject", "--all"), 0, self.err)
        self.assertEqual(self.snapshot(), original)
        self.reset()
        self.draft_all()
        self.assertEqual(self.cli("undo", "--turn"), 0, self.err)
        self.assertEqual(self.snapshot(), original)

    def test_the_same_boilerplate_sentence_in_every_chapter_never_crosses_over(self):
        original = self.snapshot()
        for c in range(1, N_CHAPTERS + 1):
            self.assertEqual(original[f"chapters/ch{c}.tex"].count(BOILERPLATE), 1)
        self.assertEqual(self.cli("draft-replace", "--file", "chapters/ch3.tex", "--old", BOILERPLATE,
                                  "--new", "This chapter proceeds as follows."), 0, self.err)
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        want = dict(original)
        want["chapters/ch3.tex"] = want["chapters/ch3.tex"].replace(
            BOILERPLATE, "This chapter proceeds as follows.", 1)
        got = {name: plain_view(text) for name, text in self.snapshot().items()}
        self.assertEqual(got, want)

    def test_a_boilerplate_sentence_without_a_file_is_refused_not_applied_to_the_wrong_chapter(self):
        """In the main file the sentence does not occur at all."""
        original = self.snapshot()
        rc = self.cli("draft-replace", "--old", BOILERPLATE, "--new", "x")
        self.assertEqual(rc, 2)
        self.assertEqual(self.snapshot(), original)

    def test_hand_edit_in_one_chapter_survives_resolving_a_draft_in_another(self):
        original = self.snapshot()
        path, old, new = edit(2, 2)
        hand_old, hand_new = "Claim 3.0.0 holds", "Claim 3.0.0 is established"
        for verb in ("accept", "reject"):
            with self.subTest(verb):
                self.reset()
                self.cli("draft-replace", "--file", path, "--old", old, "--new", new)
                self.write("chapters/ch3.tex",
                           self.read("chapters/ch3.tex").replace(hand_old, hand_new, 1))
                self.cli("status")
                self.assertEqual(self.cli(verb, "--all"), 0, self.err)
                want = original[path].replace(old, new, 1) if verb == "accept" else original[path]
                self.assertEqual(plain_view(self.read(path)), want)
                self.assertIn(hand_new, self.read("chapters/ch3.tex"), "the user's own edit was lost")
                for other in ("chapters/ch1.tex", "chapters/ch4.tex", "chapters/ch5.tex"):
                    self.assertEqual(self.read(other), original[other])
                self.assertEqual(self.cli("check"), 0, self.out)

    def test_every_command_stays_responsive(self):
        slowest = (0.0, "")
        for path, old, new in EDITS:
            t = time.time()
            self.cli("draft-replace", "--file", path, "--old", old, "--new", new)
            slowest = max(slowest, (time.time() - t, f"draft-replace {path}"))
        for argv in (["status"], ["list"], ["check"], ["accept", "--all"]):
            t = time.time()
            self.cli(*argv)
            slowest = max(slowest, (time.time() - t, " ".join(argv)))
        self.assertLess(slowest[0], 5.0, f"slowest command: {slowest[1]} took {slowest[0]:.1f}s")

    @needs_tex
    def test_review_and_accepted_pdfs_of_the_whole_thesis(self):
        for path, old, new in [edit(2, 2), edit(5, 8), edit(6, 14)]:  # ch5/ch6 are \include'd
            self.assertEqual(self.cli("draft-replace", "--file", path, "--old", old, "--new", new),
                             0, self.err)
        review = self.compile()
        self.assertEqual(review.count("remains valid"), 3)
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        self.assertEqual(self.compile().count("remains valid"), 3)

    def test_finalize_keeps_relative_paths_and_copies_the_bib(self):
        self.draft_all()
        self.cli("accept", "--all")
        out = self.dir / "thesis-final"
        self.assertEqual(self.cli("finalize", "--out", str(out)), 0, self.err)
        for c in range(1, N_CHAPTERS + 1):
            self.assertEqual((out / "chapters" / f"ch{c}.tex").read_text(encoding="utf-8"),
                             plain_view(self.read(f"chapters/ch{c}.tex")))
        self.assertTrue((out / "refs.bib").exists())
        self.assertFalse(list(out.rglob("*latex-draft*")))


if __name__ == "__main__":
    unittest.main()
