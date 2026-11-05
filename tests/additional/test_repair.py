#!/usr/bin/env python3
"""Group 6: Repair must fix the broken environment, not a healthy one.

Purpose:
`repair` pairs \\begin{X}/\\end{Y} mismatches by counting matches across the
document and replacing the first textual occurrence — without line numbers.
When the document contains a legitimate environment of the same name as the
wrong \\end, the repair can rewrite the healthy environment and leave the
broken one untouched, while reporting success. These tests use synthetic
.log files (no LaTeX installation needed) and pin down: the actually
mismatched line is fixed, legitimate environments are untouched, and the
tool never claims a compile it did not perform.

Run:  python3 tests/additional/test_repair.py
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
    """Document plus an optional synthetic paper.log."""

    def __init__(self, text, log=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(text)
        if log is not None:
            self.tex.with_suffix(".log").write_text(log)

    def run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = ld.main([str(self.tex), *argv])
            except Exception as exc:
                rc = f"EXCEPTION {type(exc).__name__}: {exc}"
        return rc, out.getvalue(), err.getvalue()

    def text(self):
        return self.tex.read_text()


@test
def repair_keeps_legit_env_before_mismatch():
    """A legitimate \\end{center} before the mismatched one must survive.

    Document: a healthy center environment, then an itemize closed with
    \\end{center}. The pairing logic picks the *first* \\end{center} — the
    healthy one — so the repair corrupts the healthy environment and leaves
    the broken line as it was.
    """
    paper = ("\\documentclass{article}\n"
             "\\begin{document}\n"
             "\\begin{center}\n"
             "legit center text\n"
             "\\end{center}\n"
             "\\begin{itemize}\n"
             "\\item a\n"
             "\\end{center}\n"
             "\\end{document}\n")
    log = ("! LaTeX Error: \\begin{itemize} on input line 6 ended by \\end{center}.\n")
    doc = Doc(paper, log=log)
    rc, out, err = doc.run("repair")
    assert rc == 0, f"repair failed: rc={rc}\n{err}"
    text = doc.text()
    assert text.count("\\end{center}") == 1, (
        "the mismatched \\end{center} must have been rewritten")
    assert text.count("\\end{itemize}") == 1
    assert "\\begin{center}\nlegit center text\n\\end{center}" in text, (
        "repair rewrote the healthy \\end{center} and left the broken one: "
        "the document is now MORE broken than before")


@test
def repair_two_mismatches_both_fixed():
    """Guard: two mismatches in one document are both repaired correctly."""
    paper = ("\\documentclass{article}\n"
             "\\begin{document}\n"
             "\\begin{itemize}\n\\item a\n\\end{center}\n"
             "\\begin{enumerate}\n\\item b\n\\end{center}\n"
             "\\end{document}\n")
    log = ("! LaTeX Error: \\begin{itemize} on input line 3 ended by \\end{center}.\n"
           "! LaTeX Error: \\begin{enumerate} on input line 6 ended by \\end{center}.\n")
    doc = Doc(paper, log=log)
    rc, out, err = doc.run("repair")
    assert rc == 0, f"repair failed: rc={rc}\n{err}"
    text = doc.text()
    assert "\\end{itemize}" in text and "\\end{enumerate}" in text, (
        "the two mismatches were not both repaired")
    assert text.count("\\end{center}") == 0


@test
def repair_mismatch_first_is_fixed():
    """Guard: mismatched \\end before a legitimate same-name \\end is the
    one rewritten."""
    paper = ("\\documentclass{article}\n"
             "\\begin{document}\n"
             "\\begin{itemize}\n\\item a\n\\end{center}\n"
             "\\begin{center}\nlegit\n\\end{center}\n"
             "\\end{document}\n")
    log = "! LaTeX Error: \\begin{itemize} on input line 3 ended by \\end{center}.\n"
    doc = Doc(paper, log=log)
    rc, out, err = doc.run("repair")
    assert rc == 0, f"repair failed: rc={rc}\n{err}"
    text = doc.text()
    assert text.count("\\end{center}") == 1, "the legit \\end{center} must survive"
    assert text.count("\\end{itemize}") == 1, "the mismatched end must be fixed"


@test
def repair_never_claims_compile_without_engine():
    """With no LaTeX engine installed, repair must not claim it compiled.

    run_latex returns exit code 0 when no engine exists, so repair reports
    'nothing to repair; paper.pdf compiles' / 'repair applied; compiled
    paper.pdf' — a verification that never happened.
    """
    import shutil
    if shutil.which("latexmk") or shutil.which("pdflatex"):
        return  # an engine is installed; the honesty problem does not apply

    # clean document, no log: repair claims the PDF compiles
    doc = Doc("\\documentclass{article}\n"
              "\\begin{document}\nBody.\n\\end{document}\n")
    rc, out, err = doc.run("repair")
    assert "compiles" not in out.lower(), (
        "repair claimed the document compiles without ever running LaTeX")


@test
def repair_is_undoable():
    """Guard: an applied repair must be revertible via undo, byte-identical."""
    paper = ("\\documentclass{article}\n"
             "\\begin{document}\n"
             "\\begin{itemize}\n\\item a\n\\end{center}\n"
             "\\end{document}\n")
    log = "! LaTeX Error: \\begin{itemize} on input line 3 ended by \\end{center}.\n"
    doc = Doc(paper, log=log)
    rc, out, err = doc.run("repair")
    assert rc == 0, f"repair failed: rc={rc}\n{err}"
    rc, out, err = doc.run("undo", "--n", "1")
    assert rc == 0, f"undo of a repair failed: rc={rc}\n{err}"
    assert doc.text() == paper, "undo did not restore the pre-repair document"


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
