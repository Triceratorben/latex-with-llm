#!/usr/bin/env python3
"""Group 8: The pre-submission check must not cry wolf.

Purpose:
`check` is the gate the agent runs before submission. It currently counts
EVERY `% latex-draft:` marker in the document — including the markers of the
drafts it itself lists as pending — so a document with one normal pending
draft reports two problems: "1 pending draft" AND "2 stray marker lines".
That makes the check output ambiguous (is there a REAL orphaned marker the
tool does not know about, or is it just the pending draft?) and trains the
user to ignore "stray marker" warnings. These tests pin down: pending drafts
are reported once, only markers the tool does not know about count as stray,
and a fully resolved document (multi-file included) checks clean.

Run:  python3 tests/additional/test_check_report.py
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
    def __init__(self, text=None, files=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(text if text is not None else
                            "\\documentclass{article}\n"
                            "\\begin{document}\nBody text.\n\\end{document}\n")
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


@test
def pending_draft_is_not_reported_as_stray():
    """A single, perfectly normal pending draft must produce ONE problem.

    The markers of a pending draft block belong to a draft the tool knows
    about; reporting them as 'stray marker lines' as well is a false alarm.
    """
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "Draft.")[0] == 0
    rc, out, err = doc.run("check")
    assert rc == 1, "check must fail while a draft is pending"
    assert "pending" in out, "the pending draft must be reported"
    assert "stray" not in out, (
        "check reported the pending draft's own markers as stray; only "
        "markers that belong to no known draft are stray")


@test
def unknown_marker_is_reported_as_stray():
    """Guard: a marker the tool does not know about must still be flagged."""
    doc = Doc()
    doc.run("list")  # initialise state/backups
    doc.tex.write_text(doc.text() + "% latex-draft:deadbeef\n"
                       "\\added{orphan}\n% latex-draft-end:deadbeef\n")
    rc, out, err = doc.run("check")
    assert rc == 1, "check must fail on an unknown draft marker"
    assert "stray" in out or "deadbeef" in out


@test
def clean_after_accept_all_multifile():
    """Guard: after resolving every draft of a multi-file document, check
    passes — no pending drafts, no markers, no leftover preamble."""
    doc = Doc(files={"chapter.tex": "Chapter body.\n"},
              text="\\documentclass{article}\n"
                   "\\begin{document}\nMain body.\n\\input{chapter}\n\\end{document}\n")
    assert doc.run("draft-insert", "--after", "Main body.", "--text", "Main draft.")[0] == 0
    assert doc.run("draft-insert", "--file", "chapter.tex",
                   "--after", "Chapter body.", "--text", "Chapter draft.")[0] == 0
    rc, _, err = doc.run("accept", "--all")
    assert rc == 0, f"accept --all failed: rc={rc}\n{err}"
    rc, out, err = doc.run("check")
    assert rc == 0, f"check must pass after resolving everything; output:\n{out}"
    assert "OK" in out


@test
def rejected_everything_checks_clean():
    """Guard: reject --all of insert + comment + preamble drafts checks clean."""
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "Draft.")[0] == 0
    assert doc.run("draft-comment", "--after", "Body text.", "--text", "Note.")[0] == 0
    assert doc.run("draft-preamble", "--end", "--text", r"\setlength{\parindent}{0pt}")[0] == 0
    rc, _, err = doc.run("reject", "--all")
    assert rc == 0, f"reject --all failed: rc={rc}\n{err}"
    rc, out, err = doc.run("check")
    assert rc == 0, f"check must pass after rejecting everything; output:\n{out}"


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
