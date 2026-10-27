#!/usr/bin/env python3
"""Group 4: Fidelity of drafted text through accept.

Purpose:
`accept` does not keep the drafted line verbatim — it re-emits the content it
parses back out of the \\added/\\deleted/\\footnote/\\todo commands. That
round-trip is only safe while the braces in the drafted text are balanced.
These tests pin down: (1) text that parses cleanly must come back
byte-identical, and (2) text with an unbalanced brace must never be silently
truncated, and the tool must never create a draft it can no longer resolve.

Run:  python3 tests/additional/test_comment_fidelity.py
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
    def __init__(self, text=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(text if text is not None else
                            "\\documentclass{article}\n"
                            "\\begin{document}\nBody text.\n\\end{document}\n")

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

    def first_draft(self):
        return next(iter(ld.load_state(self.tex)["drafts"]))


@test
def comment_accept_is_verbatim():
    """A well-formed comment must survive accept byte-identically."""
    tricky = r"50\% of \textbf{cases} involve $\{x\}$ sets"
    doc = Doc()
    assert doc.run("draft-comment", "--after", "Body text.", "--text", tricky)[0] == 0
    did = doc.first_draft()
    rc, _, _ = doc.run("accept", "--id", did)
    assert rc == 0, f"accept failed: rc={rc}"
    assert rf"\footnote{{{tricky}}}" in doc.text(), (
        "the accepted comment differs from the drafted text")


@test
def comment_accept_never_truncates():
    """Text after an unbalanced '}' must not be silently dropped by accept.

    accept re-emits \\footnote{<parsed content>}; a literal '}' in the drafted
    text ends the argument early and everything after it is lost without any
    warning or nonzero exit code.
    """
    doc = Doc()
    assert doc.run("draft-comment", "--after", "Body text.",
                   "--text", "prices } rise")[0] == 0
    did = doc.first_draft()
    rc, _, _ = doc.run("accept", "--id", did)
    assert rc == 0, f"accept failed: rc={rc}"
    assert "rise" in doc.text(), (
        "accept silently truncated the drafted text at an unbalanced brace")


@test
def unbalanced_brace_draft_stays_resolvable():
    """The tool must never create a draft it can neither accept nor reject.

    With an unbalanced '{' in the text, both accept and reject abort with an
    'unbalanced braces' error — the draft is stuck in the file forever.
    Either the draft command should have refused such text, or resolve must
    handle it; here we only demand: one of accept/reject must succeed.
    """
    doc = Doc()
    assert doc.run("draft-comment", "--after", "Body text.",
                   "--text", "press { now")[0] == 0
    did = doc.first_draft()
    rc_accept, _, _ = doc.run("accept", "--id", did)
    if rc_accept != 0:
        rc_reject, _, _ = doc.run("reject", "--id", did)
        assert rc_reject == 0, (
            f"draft {did} can neither be accepted (rc={rc_accept}) nor "
            f"rejected (rc={rc_reject}); the tool created an unresolvable draft")


@test
def inserted_text_accept_is_verbatim():
    """draft-insert text with LaTeX must survive accept byte-identically."""
    tricky = r"\emph{Stress} $x^2$ \cite{ref} 100\% done"
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Body text.", "--text", tricky)[0] == 0
    did = doc.first_draft()
    rc, _, _ = doc.run("accept", "--id", did)
    assert rc == 0, f"accept failed: rc={rc}"
    assert tricky in doc.text(), "the accepted insert differs from the drafted text"


@test
def inserted_text_accept_never_truncates():
    """Same truncation risk as comments, for \\added blocks."""
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Body text.",
                   "--text", "say } what")[0] == 0
    did = doc.first_draft()
    rc, _, _ = doc.run("accept", "--id", did)
    assert rc == 0, f"accept failed: rc={rc}"
    assert "what" in doc.text(), (
        "accept silently truncated the inserted text at an unbalanced brace")


@test
def margin_comment_accept_keeps_todo():
    """A margin comment (--margin) must keep its exact \\todo{...} on accept."""
    doc = Doc()
    assert doc.run("draft-comment", "--after", "Body text.",
                   "--margin", "--text", "Check the proof.")[0] == 0
    did = doc.first_draft()
    rc, _, _ = doc.run("accept", "--id", did)
    assert rc == 0, f"accept failed: rc={rc}"
    assert "\\todo{Check the proof.}" in doc.text()
    assert "\\usepackage{todonotes}" in doc.text(), (
        "the todonotes package must remain while an accepted \\todo is in the document")


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
