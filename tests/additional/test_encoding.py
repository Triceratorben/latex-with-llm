#!/usr/bin/env python3
"""Group 9: Encoding robustness.

Purpose:
The tool hard-reads every file as UTF-8 (`read_text(encoding="utf-8")`) and
lets UnicodeDecodeError escape as a raw traceback. A latin-1/cp1252 document
or chapter is not exotic on real projects. These tests pin down: (1) any
command against a non-UTF-8 file must exit with a clean nonzero error
message, not a traceback; (2) UTF-8 content (accented text, CJK, emoji) must
roundtrip through draft/accept/reject byte-identically; (3) a UTF-8 BOM must
survive a draft cycle.

Run:  python3 tests/additional/test_encoding.py
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

    def __init__(self, binary=None, text=None, files=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        if binary is not None:
            self.tex.write_bytes(binary)
        else:
            self.tex.write_text(text if text is not None else self.MAIN)
        for name, content in (files or {}).items():
            (self.dir / name).write_bytes(content if isinstance(content, bytes) else content)

    def run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = ld.main([str(self.tex), *argv])
            except Exception as exc:
                rc = f"EXCEPTION {type(exc).__name__}: {exc}"
        return rc, out.getvalue(), err.getvalue()

    def bytes(self, name="paper.tex"):
        return (self.dir / name).read_bytes()

    def first_draft(self):
        return next(iter(ld.load_state(self.tex)["drafts"]))


@test
def non_utf8_main_file_fails_cleanly():
    """A latin-1 document must produce a clean error, not a traceback.

    The CLI must return a nonzero exit code and print a message; an
    unhandled UnicodeDecodeError breaks any agent that shells out to the
    tool.
    """
    latin1 = (b"\\documentclass{article}\n"
              b"\\begin{document}\n"
              b"caf\xe9 body\n"
              b"\\end{document}\n")
    doc = Doc(binary=latin1)
    rc, out, err = doc.run("list")
    assert not str(rc).startswith("EXCEPTION"), (
        f"the tool crashed with a traceback on a latin-1 file: {rc}")
    assert rc != 0, "a non-UTF-8 file must be rejected with an error"
    assert "latex-draft" in err or "latex-draft" in out, (
        "the failure must be reported as a latex-draft error message")


@test
def non_utf8_chapter_fails_cleanly():
    """A latin-1 \\input chapter must not crash the main-file commands either.

    The dependency scanner reads every input file; a decode error anywhere
    in the document currently surfaces as a traceback from commands that
    touch the main file only.
    """
    doc = Doc(files={"chapter.tex": b"chapter caf\xe9\n"},
              text="\\documentclass{article}\n"
                   "\\begin{document}\n\\input{chapter}\n\\end{document}\n")
    rc, out, err = doc.run("list")
    assert not str(rc).startswith("EXCEPTION"), (
        f"the tool crashed because an input file is not UTF-8: {rc}")
    assert rc != 0, "a non-UTF-8 input file must be rejected with an error"


@test
def utf8_text_roundtrips():
    """Guard: non-ASCII drafted text must survive accept byte-identically."""
    doc = Doc()
    text = "Naïve café study: 结果. 🧪 \\emph{Wörter}"
    assert doc.run("draft-insert", "--after", "Body text.", "--text", text)[0] == 0
    did = doc.first_draft()
    rc, _, err = doc.run("accept", "--id", did)
    assert rc == 0, f"accept failed: rc={rc}\n{err}"
    assert text in doc.bytes().decode("utf-8"), "non-ASCII drafted text was altered"


@test
def utf8_reject_restores_bytes():
    """Guard: reject restores the exact original bytes for a UTF-8 document."""
    original_bytes = ("\\documentclass{article}\n"
                      "\\begin{document}\n"
                      "Größe und Wirkung — 结果 ok.\n"
                      "\\end{document}\n").encode("utf-8")
    doc = Doc(binary=original_bytes)
    assert doc.run("draft-insert", "--after", "Wirkung", "--text", "Draft äöü.")[0] == 0
    did = doc.first_draft()
    rc, _, err = doc.run("reject", "--id", did)
    assert rc == 0, f"reject failed: rc={rc}\n{err}"
    assert doc.bytes() == original_bytes, "reject did not restore the original bytes"


@test
def bom_survives_draft_cycle():
    """A UTF-8 BOM must still be present after a draft + reject cycle."""
    bom = b"\xef\xbb\xbf"
    original = bom + ("\\documentclass{article}\n"
                      "\\begin{document}\n"
                      "Body text.\n"
                      "\\end{document}\n").encode("utf-8")
    doc = Doc(binary=original)
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "Draft.")[0] == 0
    did = doc.first_draft()
    rc, _, err = doc.run("reject", "--id", did)
    assert rc == 0, f"reject failed: rc={rc}\n{err}"
    assert doc.bytes() == original, (
        "the UTF-8 BOM was dropped or the bytes changed across the cycle")


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
