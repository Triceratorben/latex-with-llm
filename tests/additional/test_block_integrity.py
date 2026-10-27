#!/usr/bin/env python3
"""Group 1: Draft-block integrity and state/file consistency.

Purpose:
Every pending draft must be discoverable in the .tex file as a
`% latex-draft:<id> ... % latex-draft-end:<id>` block, and every draft whose
status is "pending" must actually have its block in its file. This must hold
after any sequence of commands, including commands that FAIL halfway through
(a draft block hand-deleted by the user, an undo that resurrects a block, a
failed command). The tool records draft status in a state file; if that state
ever disagrees with the document, drafts become permanently unresolvable
("draft not found") or invisible ("stray markers"), and the user loses the
safety net the tool promises.

Run:  python3 tests/additional/test_block_integrity.py   (from anywhere;
      $LATEX_DRAFT_REPO overrides the checkout location).
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
    """A fresh single- or multi-file document in its own temp directory."""

    MAIN = ("\\documentclass{article}\n"
            "\\begin{document}\n"
            "Small problems are worth studying.\n"
            "\\end{document}\n")

    def __init__(self, text=None, files=None, binary=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        if binary is not None:
            self.tex.write_bytes(binary)
        else:
            self.tex.write_text(text if text is not None else self.MAIN)
        for name, content in (files or {}).items():
            (self.dir / name).write_text(content)

    def run(self, *argv):
        """Run a latex-draft command; returns (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = ld.main([str(self.tex), *argv])
            except Exception as exc:  # a traceback is a failure of the CLI contract
                rc = f"EXCEPTION {type(exc).__name__}: {exc}"
        return rc, out.getvalue(), err.getvalue()

    def path(self, name="paper.tex"):
        return self.dir / name

    def text(self, name="paper.tex"):
        return (self.dir / name).read_text()

    def state(self):
        return ld.load_state(self.tex)

    def first_draft(self):
        return next(iter(self.state()["drafts"]))


def assert_pending_blocks_exist(doc):
    """Invariant: every draft with status 'pending' has its block in its file."""
    for did, rec in doc.state()["drafts"].items():
        if rec["status"] != "pending":
            continue
        fname = rec.get("file", "paper.tex")
        assert f"% latex-draft:{did}" in doc.text(fname), (
            f"draft {did} is 'pending' but its block is missing from {fname}; "
            f"the state file and the document disagree")


@test
def fragment_old_roundtrips():
    """A --old that starts/ends mid-line must stay resolvable."""
    doc = Doc()
    rc, _, _ = doc.run("draft-replace", "--old", "Small problems",
                       "--new", "Big problems")
    assert rc == 0, f"fragment --old draft failed: rc={rc}"
    did = doc.first_draft()
    rc, _, _ = doc.run("accept", "--id", did)
    assert rc == 0, f"accept of a fragment draft failed: rc={rc}"
    assert "Big problems are worth studying." in doc.text()
    assert "latex-draft" not in doc.text()

    doc = Doc()
    original = doc.text()
    doc.run("draft-replace", "--old", "Small problems", "--new", "Big problems")
    did = doc.first_draft()
    rc, _, _ = doc.run("reject", "--id", did)
    assert rc == 0, f"reject of a fragment draft failed: rc={rc}"
    assert doc.text() == original, "reject did not restore the original bytes"


@test
def accept_all_partial_failure_is_consistent():
    """accept --all must resolve what still exists and close what vanished.

    Scenario: drafts in the main file and in a chapter; the user hand-deletes
    the chapter's block. A missing block cannot be resolved, but it must not
    block the drafts that are still there: the command resolves them and
    closes the vanished record (with a warning), so the state never claims a
    draft is 'pending' when its block is gone.
    """
    doc = Doc(files={"chapter.tex": "Chapter body.\n"},
              text="\\documentclass{article}\n"
                   "\\begin{document}\nMain body.\n\\input{chapter}\n\\end{document}\n")
    assert doc.run("draft-insert", "--after", "Main body.", "--text", "Main draft.")[0] == 0
    assert doc.run("draft-insert", "--file", "chapter.tex",
                   "--after", "Chapter body.", "--text", "Chapter draft.")[0] == 0
    import re
    ch = doc.text("chapter.tex")
    ch = re.sub(r"% latex-draft:[0-9a-f]{8}\n.*?% latex-draft-end:[0-9a-f]{8}\n?",
                "", ch, flags=re.DOTALL)
    doc.path("chapter.tex").write_text(ch)

    rc, out, err = doc.run("accept", "--all")
    assert rc == 0, (
        "a hand-deleted draft block must not abort accept --all for the "
        f"drafts that still exist: rc={rc}")
    assert "marked as rejected" in out + err, (
        "the vanished draft was closed without telling the user")
    assert "Main draft." in doc.text(), "the surviving draft was not accepted"
    assert_pending_blocks_exist(doc)


@test
def undo_resurrects_pending_status():
    """Undo that brings a draft block back must also reset its status.

    reject a draft, then undo: the block returns to the file. If the state
    still says 'rejected', the block can never be resolved again (accept /
    reject refuse) and `check` reports it as a stray marker.
    """
    doc = Doc()
    doc.run("draft-insert", "--after", "Small problems", "--text", "New sentence.")
    did = doc.first_draft()
    assert doc.run("reject", "--id", did)[0] == 0
    rc, _, _ = doc.run("undo", "--n", "2")  # undo the strip + the reject
    assert rc == 0, f"undo --n 2 failed: rc={rc}"
    if f"% latex-draft:{did}" in doc.text():
        status = doc.state()["drafts"][did]["status"]
        assert status == "pending", (
            f"undo resurrected draft {did} but its status is '{status}'; "
            f"it can no longer be accepted or rejected")
        rc, _, _ = doc.run("reject", "--id", did)
        assert rc == 0, "resurrected draft could not be resolved again"


@test
def failed_commands_leave_file_untouched():
    """A refused command must not modify the document (only backups may appear)."""
    doc = Doc(text="\\documentclass{article}\n"
                   "\\begin{document}\nTwin.\nTwin.\n\\end{document}\n")
    original = doc.text()
    cases = [
        ("draft-insert", "--after", "Twin.", "--text", "x"),          # ambiguous anchor
        ("draft-replace", "--old", "absent", "--new", "y"),           # no match
        ("accept", "--id", "deadbeef"),                                # unknown id
    ]
    for case in cases:
        rc, _, _ = doc.run(*case)
        assert rc != 0, f"command {case} should have been refused"
        assert doc.text() == original, f"command {case} modified the file although it failed"

    # a document without \begin{document} must refuse a preamble draft, not crash
    doc2 = Doc(text="\\documentclass{article}\nno document environment\n")
    rc, _, _ = doc2.run("draft-preamble", "--start", "--text", r"\setlength{\parindent}{0pt}")
    assert rc not in (0,) and not str(rc).startswith("EXCEPTION"), \
        f"draft-preamble without \\begin{{document}} must fail cleanly, got {rc}"


@test
def every_pending_draft_is_findable():
    """After a mixed drafting session each pending draft must be a real block."""
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Small problems", "--text", "Inserted.")[0] == 0
    assert doc.run("draft-comment", "--after", "Small problems", "--text", "Check this.")[0] == 0
    assert doc.run("draft-preamble", "--end", "--text", r"\setlength{\parindent}{0pt}")[0] == 0
    assert doc.run("draft-replace", "--old", "Small problems",
                  "--new", "Big problems")[0] == 0
    assert_pending_blocks_exist(doc)


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
