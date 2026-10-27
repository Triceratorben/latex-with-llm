#!/usr/bin/env python3
"""Group 2: Byte fidelity of line endings.

Purpose:
latex-draft reads the document with text-mode I/O and writes it back, so a
CRLF (Windows) or CR-only (classic Mac) document can be silently rewritten
with LF endings — every line of the user's file, not just the drafted one.
Tests in this group pin down two things: (1) a draft must only ever add or
remove its own lines, never convert the line endings of the user's existing
lines, and (2) `reject` must restore the document byte-for-byte, which is
impossible if the endings were converted anywhere along the way.

Run:  python3 tests/additional/test_line_endings.py
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
    MAIN_BYTES = (b"\\documentclass{article}\r\n"
                  b"\\begin{document}\r\n"
                  b"Small problems are worth studying.\r\n"
                  b"\\end{document}\r\n")

    def __init__(self, binary=None, files=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_bytes(binary if binary is not None else self.MAIN_BYTES)
        for name, content in (files or {}).items():
            (self.dir / name).write_bytes(content)

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
def crlf_survives_draft_insert():
    """Drafting must not convert the user's existing CRLF lines to LF."""
    doc = Doc()
    rc, _, _ = doc.run("draft-insert", "--after", "Small problems", "--text", "New sentence.")
    assert rc == 0, f"draft-insert failed on a CRLF file: rc={rc}"
    data = doc.bytes()
    assert b"\r\n" in data, (
        "the whole document was silently rewritten with LF line endings; "
        "only the drafted lines may change")
    assert b"studying.\r\n" in data, "an untouched line lost its CRLF ending"


@test
def crlf_reject_restores_bytes():
    """reject must give back the exact bytes the user wrote (CRLF included)."""
    doc = Doc()
    original = doc.bytes()
    assert doc.run("draft-insert", "--after", "Small problems", "--text", "New sentence.")[0] == 0
    did = doc.first_draft()
    rc, _, _ = doc.run("reject", "--id", did)
    assert rc == 0, f"reject failed: rc={rc}"
    assert doc.bytes() == original, (
        "reject did not restore the original bytes; the CRLF line endings "
        "of the user's file were destroyed")


@test
def crlf_accept_keeps_untouched_lines():
    """accept may merge text but must not re-encode the untouched lines."""
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Small problems", "--text", "New sentence.")[0] == 0
    did = doc.first_draft()
    rc, _, _ = doc.run("accept", "--id", did)
    assert rc == 0, f"accept failed: rc={rc}"
    data = doc.bytes()
    assert b"studying.\r\n" in data, "accept re-wrote an untouched line with LF endings"
    assert b"New sentence." in data


@test
def cronly_file_roundtrips():
    """A CR-only (classic Mac) document must roundtrip, not be re-encoded."""
    cr = (b"\\documentclass{article}\r"
          b"\\begin{document}\r"
          b"Body.\r"
          b"\\end{document}\r")
    doc = Doc(binary=cr)
    original = doc.bytes()
    assert doc.run("draft-insert", "--after", "Body.", "--text", "New.")[0] == 0
    did = doc.first_draft()
    rc, _, _ = doc.run("reject", "--id", did)
    assert rc == 0, f"reject failed: rc={rc}"
    assert doc.bytes() == original, (
        "reject did not restore the original CR-only bytes")


@test
def normalization_must_not_trip_hand_edit_detector():
    """Guard: whatever the tool writes, the hash bookkeeping must stay sound.

    If line-ending conversion ever happens, the tool must at least adopt the
    result so `status` does not warn about the file it just rewrote itself.
    """
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Small problems", "--text", "New sentence.")[0] == 0
    rc, out, err = doc.run("status")
    assert rc == 0
    assert "WARNING" not in out and "WARNING" not in err, (
        "status warned about an 'external change' caused by the tool itself")


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
