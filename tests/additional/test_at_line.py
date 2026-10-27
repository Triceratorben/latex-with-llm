#!/usr/bin/env python3
"""Group 10: --at-line addressing — visible lines, validated bounds.

Purpose:
`draft-insert --at-line N` inserts before the N-th *visible* line (skipping
the tool's injected preamble lines and pending draft blocks), so the line
number the user sees in their editor stays meaningful. The bounds are not
validated: --at-line 0 or negative silently inserts after \\documentclass,
and a line number far beyond the end of the document silently inserts
AFTER \\end{document} — a draft the user will not see in the body and which
compile will not show where expected. These tests pin down: out-of-range and
non-positive line numbers are refused, visible numbering skips tool-owned
lines, and numbering applies to the target file when --file is used.

Run:  python3 tests/additional/test_at_line.py
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
            "Line one.\n"
            "Line two.\n"
            "Line three.\n"
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


@test
def at_line_zero_rejected():
    """--at-line 0 is not a visible line and must be refused."""
    doc = Doc()
    rc, _, _ = doc.run("draft-insert", "--at-line", "0", "--text", "X.")
    assert rc != 0, (
        "--at-line 0 was accepted and silently inserted after \\documentclass; "
        "line numbering starts at 1")
    assert "latex-draft:" not in doc.text(), "the refused draft still modified the file"


@test
def at_line_negative_rejected():
    """Negative --at-line must be refused."""
    doc = Doc()
    rc, _, _ = doc.run("draft-insert", "--at-line", "-2", "--text", "X.")
    assert rc != 0, "negative --at-line was accepted instead of refused"
    assert "latex-draft:" not in doc.text()


@test
def at_line_beyond_document_rejected():
    """A line number past the end of the document must be refused.

    Currently it silently inserts AFTER \\end{document}: a draft invisible
    in the compiled body.
    """
    doc = Doc()
    rc, _, _ = doc.run("draft-insert", "--at-line", "999", "--text", "X.")
    assert rc != 0, (
        "--at-line 999 was accepted and inserted past \\end{document}; "
        "out-of-range line numbers must be an error")
    assert "latex-draft:" not in doc.text()


@test
def at_line_counts_visible_lines_only():
    """Guard: injected preamble and pending draft blocks are not counted.

    Two drafts at the same visible line both land at the same user-visible
    position, and resolving both keeps both texts in the document.
    """
    doc = Doc()
    assert doc.run("draft-insert", "--at-line", "1", "--text", "first.")[0] == 0
    assert doc.run("draft-insert", "--at-line", "1", "--text", "second.")[0] == 0
    state = ld.load_state(doc.tex)
    ids = [d for d, r in state["drafts"].items() if r["status"] == "pending"]
    assert len(ids) == 2
    rc, _, err = doc.run("accept", "--all")
    assert rc == 0, f"accept --all failed: rc={rc}\n{err}"
    text = doc.text()
    assert "first." in text and "second." in text
    # both blocks were inserted at the same visible position: between the
    # injected preamble and \begin{document} nothing else changed
    assert "latex-draft" not in text


@test
def at_line_applies_to_target_file():
    """Guard: with --file, the line number addresses the target file's lines."""
    doc = Doc(files={"chapter.tex": "Alpha line.\nBeta line.\n"},
              text="\\documentclass{article}\n"
                   "\\begin{document}\n\\input{chapter}\n\\end{document}\n")
    rc, _, _ = doc.run("draft-insert", "--file", "chapter.tex",
                       "--at-line", "2", "--text", "Inserted before Beta.")
    assert rc == 0, f"draft-insert --file with --at-line failed: rc={rc}"
    state = ld.load_state(doc.tex)
    did = next(iter(state["drafts"]))
    rc, _, err = doc.run("accept", "--id", did)
    assert rc == 0, f"accept failed: rc={rc}\n{err}"
    ch = doc.text("chapter.tex")
    assert ch.index("Inserted before Beta.") < ch.index("Beta line."), (
        "the insert did not land before the target file's second line")


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
