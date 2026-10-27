#!/usr/bin/env python3
"""Group 5: Input discovery must follow real inclusion, not raw text.

Purpose:
\\input/\\include dependencies are discovered with a plain regex over the
source text, so a file mentioned inside a LaTeX comment or a verbatim
environment is treated as part of the document. Consequences: status lists
phantom inputs, the full backup copies unrelated files, and — worst — editing
such a file triggers a false "edited outside latex-draft" warning that is then
written into the changelog as an external change. These tests pin down that
only files the document really pulls in become watched dependencies.

Run:  python3 tests/additional/test_input_discovery.py
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
    def __init__(self, text, files=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(text)
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

    def path(self, name):
        return self.dir / name


@test
def commented_input_not_watched():
    """A \\input inside a LaTeX comment must not become a dependency."""
    doc = Doc("\\documentclass{article}\n"
              "\\begin{document}\n"
              "Body.\n"
              "% \\input{unused}\n"
              "\\end{document}\n",
              files={"unused.tex": "unused\n"})
    rc, out, err = doc.run("status")
    assert rc == 0
    assert "unused.tex" not in out, (
        "a commented-out \\input was treated as a real dependency")

    # editing the non-included file must not raise an external-change warning
    doc.path("unused.tex").write_text("unused, edited\n")
    rc, out, err = doc.run("status")
    assert rc == 0
    assert "WARNING" not in out and "WARNING" not in err, (
        "editing a file that is only referenced from a comment produced a "
        "false hand-edit warning")


@test
def verbatim_input_not_watched():
    """A \\input inside a verbatim environment is literal text, not an include."""
    doc = Doc("\\documentclass{article}\n"
              "\\begin{document}\n"
              "Body.\n"
              "\\begin{verbatim}\n"
              "\\input{ghost}\n"
              "\\end{verbatim}\n"
              "\\end{document}\n",
              files={"ghost.tex": "ghost\n"})
    rc, out, err = doc.run("status")
    assert rc == 0
    assert "ghost.tex" not in out, (
        "a \\input inside verbatim was treated as a real dependency")


@test
def transitive_inputs_are_watched():
    """Guard: real transitive inclusion still works."""
    doc = Doc("\\documentclass{article}\n"
              "\\begin{document}\n"
              "\\input{chapter}\n"
              "\\end{document}\n",
              files={"chapter.tex": "\\input{sub}\n",
                     "sub.tex": "Sub content.\n"})
    rc, out, _ = doc.run("status")
    assert rc == 0
    assert "chapter.tex" in out and "sub.tex" in out, (
        "transitive inputs were not discovered")


@test
def unrelated_file_not_watched():
    """Guard: a file that is not referenced at all is never watched."""
    doc = Doc("\\documentclass{article}\n"
              "\\begin{document}\n"
              "Body.\n"
              "\\end{document}\n",
              files={"bystander.tex": "nothing to do with this paper\n"})
    doc.run("list")
    doc.path("bystander.tex").write_text("edited\n")
    rc, out, err = doc.run("status")
    assert rc == 0
    assert "WARNING" not in out and "WARNING" not in err


@test
def same_file_via_include_and_input_watched_once():
    """Guard: \\include{chapter} and \\input{chapter.tex} resolve to one file."""
    doc = Doc("\\documentclass{article}\n"
              "\\begin{document}\n"
              "\\include{chapter}\n"
              "\\input{chapter}\n"
              "\\end{document}\n",
              files={"chapter.tex": "Chapter.\n"})
    rc, out, _ = doc.run("status")
    assert rc == 0
    assert out.count("chapter.tex") == 1, (
        "the same dependency was listed more than once")


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
