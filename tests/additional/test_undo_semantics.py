#!/usr/bin/env python3
"""Group 7: Undo semantics — idempotence, validation, and conflicts.

Purpose:
`undo` reverts recorded changes and is the tool's safety net, so its own
behaviour at the edges matters: (1) repeating `undo --turn` must be a clean
no-op ("already undone"), not a pile of conflict errors — otherwise the user
cannot tell an already-empty history from a real conflict; (2) an invalid
--n (0, negative) must be refused instead of silently undoing one change;
(3) undos must restore exact bytes and must keep the recorded draft statuses
in sync with the resurrected/removed blocks; (4) when one file of the
document conflicts, the other files' changes must still be undone.

Run:  python3 tests/additional/test_undo_semantics.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path


def _repo_root() -> Path:
    env = os.environ.get("LATEX_DRAFT_REPO")
    here = Path(__file__).resolve()
    for cand in ([Path(env)] if env else []) + [here.parent, here.parent.parent, Path.cwd()]:
        if (cand / "tools" / "latex_draft.py").is_file():
            return cand
    sys.exit("cannot locate the latex-with-llm checkout; set LATEX_DRAFT_REPO")


sys.path.insert(0, str(_repo_root()))
from tools import latex_draft as ld  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


class Doc:
    MAIN = ("\\documentclass{article}\n"
            "\\begin{document}\n"
            "Body text.\n"
            "\\end{document}\n")

    def __init__(self, text=None, files=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(text if text is not None else self.MAIN)
        for name, content in (files or {}).items():
            (self.dir / name).write_text(content)

    def run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = ld.main([str(self.tex), *argv])
            except Exception as exc:
                rc = f"EXCEPTION {type(exc).__name__}: {exc}"
        return rc, out.getvalue(), err.getvalue()

    def text(self, name="paper.tex"):
        return (self.dir / name).read_text()

    def state(self):
        return ld.load_state(self.tex)


@test
def undo_turn_twice_is_clean_noop():
    """A repeated `undo --turn` must recognize the turn is already undone.

    After the first undo undid every change of the turn, the second call
    reports the leftover changes as 'conflicting' (rc 1) — indistinguishable
    from a real conflict, and it nags the user with --force suggestions.
    """
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "One.")[0] == 0
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "Two.")[0] == 0
    doc.run("compile")
    rc, out, err = doc.run("undo", "--turn")
    assert rc == 0, f"first undo --turn failed: rc={rc}\n{err}"
    content = doc.text()
    rc, out, err = doc.run("undo", "--turn")
    assert rc == 0, (
        f"repeated undo --turn returned rc={rc} with conflict warnings "
        f"instead of recognizing an already-undone turn; stderr: {err}")
    assert "conflict" not in err.lower()
    assert doc.text() == content, "the second undo --turn changed the file"


@test
def undo_turn_restores_exact_bytes():
    """Guard: undo --turn restores the pre-turn document byte-identically."""
    doc = Doc()
    original = doc.text()
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "One.")[0] == 0
    assert doc.run("draft-comment", "--after", "Body text.", "--text", "Note.")[0] == 0
    doc.run("compile")
    rc, _, err = doc.run("undo", "--turn")
    assert rc == 0, f"undo --turn failed: rc={rc}\n{err}"
    assert doc.text() == original, "undo --turn did not restore the exact pre-turn bytes"


@test
def undo_n_beyond_history_is_consistent():
    """Guard: undo --n larger than the history reverts everything AND the
    draft statuses must not claim 'pending' for blocks that are no longer
    in the file (undo does not reconcile the state with the document)."""
    doc = Doc()
    original = doc.text()
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "One.")[0] == 0
    rc, _, err = doc.run("undo", "--n", "100")
    assert rc == 0, f"undo --n 100 failed: rc={rc}\n{err}"
    assert doc.text() == original, "undo --n 100 did not restore the original document"
    state = doc.state()
    for did, rec in state["drafts"].items():
        if rec["status"] == "pending":
            assert f"% latex-draft:{did}" in doc.text(), (
                f"undo left draft {did} marked 'pending' but its block is gone "
                f"from the document; accept would now fail with 'not found'")


@test
def undo_invalid_n_rejected():
    """undo --n 0 or negative must be refused, not silently undo one change."""
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "One.")[0] == 0
    drafted = doc.text()
    rc, out, err = doc.run("undo", "--n", "0")
    assert rc != 0, "undo --n 0 must be rejected (there is no 0th change to undo)"
    assert doc.text() == drafted, "the refused undo --n 0 changed the file"
    rc, out, err = doc.run("undo", "--n", "-3")
    assert rc != 0, "undo --n -3 must be rejected instead of silently undoing one change"
    assert doc.text() == drafted, "the refused undo --n -3 changed the file"


@test
def undo_skips_only_conflicting_file():
    """Guard: a hand-edited file is skipped, the other file is still undone."""
    doc = Doc(files={"chapter.tex": "Chapter body.\n"},
              text="\\documentclass{article}\n"
                   "\\begin{document}\nMain body.\n\\input{chapter}\n\\end{document}\n")
    original_main = doc.text()
    assert doc.run("draft-insert", "--after", "Main body.", "--text", "Main draft.")[0] == 0
    assert doc.run("draft-insert", "--file", "chapter.tex",
                   "--after", "Chapter body.", "--text", "Chapter draft.")[0] == 0
    doc.run("compile")
    # hand-edit the chapter (adopted with a warning at the next command)
    (doc.dir / "chapter.tex").write_text("Chapter body.\nHand-written line.\n")
    doc.run("compile")
    rc, out, err = doc.run("undo", "--turn")
    assert rc == 0, f"undo --turn failed: rc={rc}\n{err}"
    assert doc.text() == original_main, (
        "the main file's change was not undone although only the chapter conflicted")
    assert "Hand-written line." in doc.text("chapter.tex"), (
        "the hand-edited chapter must be left untouched (refuse-on-conflict)")


def main() -> int:
    failed = []
    for fn in TESTS:
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                fn()
        except Exception as exc:  # noqa: BLE001
            failed.append(fn.__name__)
            print(f"FAIL  {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"pass  {fn.__name__}")
    print(f"\n{len(TESTS) - len(failed)}/{len(TESTS)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
