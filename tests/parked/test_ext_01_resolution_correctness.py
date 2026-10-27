"""GROUP 1 - Resolution correctness.

Question answered: after any mix of accept/reject, in any order, is the .tex
file exactly what an independent model of the user's intent says it should be?

Run:  python3 tests/test_ext_01_resolution_correctness.py
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


import random

N = 10
HEAD = "\\documentclass{article}\n\\begin{document}\n"
TAIL = "\\end{document}\n"


def line(k):
    return f"Sentence number{k:02d} here."


class RandomResolution(Base):
    """Model-based: draft a random mix of insert/replace/delete on distinct
    lines, resolve them randomly, compare to an independently computed file."""

    doc = HEAD + "".join(line(k) + "\n" for k in range(N)) + TAIL

    def draft_random(self, rnd):
        ops = {
            k: rnd.choice(("insert", "replace", "delete"))
            for k in rnd.sample(range(N), rnd.randint(1, N))
        }
        order = list(ops)
        rnd.shuffle(order)
        ids = {}
        for k in order:
            if ops[k] == "insert":
                argv = ["draft-insert", "--after", f"number{k:02d}", "--text", f"Inserted{k:02d}."]
            elif ops[k] == "replace":
                argv = ["draft-replace", "--old", line(k), "--new", f"Replaced{k:02d}."]
            else:
                argv = ["draft-delete", "--old", line(k)]
            self.assertEqual(self.cli(*argv), 0, self.err)
            ids[k] = self.draft_id()
        return ops, ids

    @staticmethod
    def expected(ops, accepted):
        out = []
        for k in range(N):
            kind, acc = ops.get(k), k in accepted
            if kind == "replace":
                out.append(f"Replaced{k:02d}." if acc else line(k))
            elif kind == "delete":
                if not acc:
                    out.append(line(k))
            else:
                out.append(line(k))
                if kind == "insert" and acc:
                    out.append(f"Inserted{k:02d}.")
        return HEAD + "".join(l + "\n" for l in out) + TAIL

    def test_any_accept_reject_mix_in_any_order_matches_model(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                self.reset()
                rnd = random.Random(seed)
                ops, ids = self.draft_random(rnd)
                accepted = {k for k in ops if rnd.random() < 0.5}
                order = list(ops)
                rnd.shuffle(order)
                for k in order:
                    verb = "accept" if k in accepted else "reject"
                    self.assertEqual(self.cli(verb, "--id", ids[k]), 0, self.err)
                self.assertEqual(self.text(), self.expected(ops, accepted))
                self.assertEqual(self.cli("check"), 0, self.out)  # no markup left

    def test_accept_all_matches_model(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                self.reset()
                ops, _ = self.draft_random(random.Random(seed))
                self.assertEqual(self.cli("accept", "--all"), 0, self.err)
                self.assertEqual(self.text(), self.expected(ops, set(ops)))

    def test_reject_all_is_byte_identical_to_the_original(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                self.reset()
                self.draft_random(random.Random(seed))
                self.assertEqual(self.cli("reject", "--all"), 0, self.err)
                self.assertEqual(self.text(), self.doc)

    def test_changelog_is_an_unbroken_chain_and_full_undo_restores_original(self):
        for seed in range(15):
            with self.subTest(seed=seed):
                self.reset()
                rnd = random.Random(seed)
                ops, ids = self.draft_random(rnd)
                for k in ops:
                    self.cli(rnd.choice(("accept", "reject")), "--id", ids[k])
                entries = [e for e in read_changelog(self.tex) if e["kind"] == "change"]
                self.assertEqual(entries[0]["before"], self.doc)
                for a, b in zip(entries, entries[1:]):
                    self.assertEqual(a["after_sha256"], b["before_sha256"])
                self.assertEqual(entries[-1]["after"], self.text())
                self.assertEqual(self.cli("undo", "--n", "10000"), 0, self.err)
                self.assertEqual(self.text(), self.doc)


class Alternatives(Base):
    """N alternatives drafted over the same sentence (one author each)."""

    def draft_alternatives(self, n):
        authors = "ABCDEFGH"[:n]
        ids = {}
        for a in authors:
            self.assertEqual(
                self.cli("draft-replace", "--author", a, "--old", SENTENCE, "--new", f"Variant {a}."),
                0, self.err)
            ids[a] = self.draft_id()
        return authors, ids

    def test_accepting_any_one_of_n_alternatives_leaves_exactly_that_text(self):
        """Every alternative is tried as the accepted one: a bug that always
        merges the first block's text is invisible if only the first is picked."""
        for n in (2, 3, 5):
            for pick in "ABCDEFGH"[:n]:
                with self.subTest(alternatives=n, accepted=pick):
                    self.reset()
                    authors, ids = self.draft_alternatives(n)
                    self.assertEqual(self.cli("accept", "--id", ids[pick]), 0, self.err)
                    drafts = load_state(self.tex)["drafts"]
                    self.assertEqual(drafts[ids[pick]]["status"], "accepted")
                    self.assertEqual(
                        self.text(), DOC.replace(SENTENCE, f"Variant {pick}."),
                        f"registry says {pick} was accepted but the file holds another alternative")
                    for a in authors:
                        if a != pick:
                            self.assertEqual(drafts[ids[a]]["status"], "rejected")
                            self.assertTrue(drafts[ids[a]].get("orphaned_by_overlap"))

    def test_rejecting_all_n_alternatives_is_byte_identical(self):
        for n in range(2, 9):
            with self.subTest(alternatives=n):
                self.reset()
                self.draft_alternatives(n)
                self.assertEqual(self.cli("reject", "--all"), 0, self.err)
                self.assertEqual(self.text(), DOC)


class AcceptedComments(Base):
    """Commenter mode: an accepted comment must stay a real LaTeX command.

    The existing suite checks `assertIn(r"\\footnote{...}", text)`, which is
    also true of the broken `\\\\footnote{...}` (a line break followed by the
    literal word "footnote"), so exact whole-file equality is used here.
    """

    TEXTS = ["Cite a source.", r"See \emph{Smith} and 50\% of cases."]

    def check_accept(self, margin):
        cmd = "todo" if margin else "footnote"
        for text in self.TEXTS:
            with self.subTest(text=text):
                self.reset()
                argv = ["draft-comment", "--after", SENTENCE, "--text", text]
                self.assertEqual(self.cli(*argv, *(["--margin"] if margin else [])), 0, self.err)
                self.assertEqual(self.cli("accept", "--all"), 0, self.err)
                want = DOC.replace(SENTENCE + "\n", SENTENCE + "\n" + f"\\{cmd}{{{text}}}\n")
                if margin:  # the accepted \todo still needs its package
                    want = want.replace("\\documentclass{article}\n",
                                        "\\documentclass{article}\n\\usepackage{todonotes}\n")
                self.assertEqual(self.text(), want)

    def test_accepted_footnote_comment_is_a_single_backslash_command(self):
        self.check_accept(margin=False)

    def test_accepted_margin_comment_is_a_single_backslash_command(self):
        self.check_accept(margin=True)


if __name__ == "__main__":
    unittest.main()
