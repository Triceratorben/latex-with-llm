#!/usr/bin/env python3
"""Group 3: Preamble ownership — the tool may only remove what it injected.

Purpose:
When the last draft is resolved, latex-draft strips the "changes"/"todonotes"
preamble it injected. The stripping is pattern-based, not provenance-based:
it removes *any* line that looks like an injected line — including lines the
user wrote themselves (their own todonotes load, their own
\\definechangesauthor definitions, even their own changes package load), and
it mis-handles a UTF-8 BOM in front of \\documentclass. These tests pin down:
only lines the tool itself added may ever disappear from the preamble.

Run:  python3 tests/additional/test_preamble_ownership.py
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

    def first_draft(self):
        return next(iter(ld.load_state(self.tex)["drafts"]))


@test
def user_todonotes_survives_strip():
    """The user's own \\usepackage{todonotes} must never be stripped.

    The strip step removes a todonotes line when no \\todo command remains in
    the document. That heuristic also deletes a todonotes package the user
    loaded themselves (e.g. for their own todo workflow elsewhere).
    """
    doc = Doc(text="\\documentclass{article}\n"
                   "\\usepackage{todonotes}\n"
                   "\\begin{document}\nBody text.\n\\end{document}\n")
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "Draft.")[0] == 0
    did = doc.first_draft()
    assert doc.run("reject", "--id", did)[0] == 0
    assert "\\usepackage{todonotes}" in doc.text(), (
        "the user's own todonotes package was deleted by the preamble strip; "
        "only tool-injected lines may be removed")


@test
def user_author_definition_survives_strip():
    """The user's own \\definechangesauthor must survive the strip."""
    doc = Doc(text="\\documentclass{article}\n"
                   "\\usepackage{changes}\n"
                   "\\definechangesauthor[name=Sam]{Sam}\n"
                   "\\begin{document}\nBody text.\n\\end{document}\n")
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "Draft.")[0] == 0
    did = doc.first_draft()
    assert doc.run("reject", "--id", did)[0] == 0
    assert "\\definechangesauthor[name=Sam]{Sam}" in doc.text(), (
        "the user's own author definition was deleted by the preamble strip")


@test
def user_changes_package_survives_strip():
    """A changes package the user loaded before ever drafting must survive.

    The strip step matches the exact injected line \\usepackage[draft]{changes}
    and removes every occurrence once no drafts are pending — including one
    the user wrote themselves before the tool was ever used.
    """
    doc = Doc(text="\\documentclass{article}\n"
                   "\\usepackage[draft]{changes}\n"
                   "\\begin{document}\nBody text.\n\\end{document}\n")
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "Draft.")[0] == 0
    did = doc.first_draft()
    assert doc.run("reject", "--id", did)[0] == 0
    assert "\\usepackage[draft]{changes}" in doc.text(), (
        "the user's own changes package line was deleted by the preamble strip")


@test
def bom_documentclass_still_first():
    """A UTF-8 BOM must not push injected lines in front of \\documentclass.

    With a BOM, the \\documentclass line no longer startswith('\\documentclass'),
    so the changes package gets inserted at line 0 — before the document class,
    which breaks compilation.
    """
    bom = "﻿"
    doc = Doc(text=bom + "\\documentclass{article}\n"
                        "\\begin{document}\nBody text.\n\\end{document}\n")
    rc, _, _ = doc.run("draft-insert", "--after", "Body text.", "--text", "Draft.")
    assert rc == 0, f"draft-insert failed on a BOM file: rc={rc}"
    text = doc.text()
    assert text.index("\\documentclass") < text.index("changes}"), (
        "the changes package was injected before \\documentclass on a "
        "BOM-prefixed file, which breaks LaTeX")


@test
def injected_lines_removed_only_after_resolution():
    """Guard: the tool's own injected lines ARE present while pending and
    ARE removed once the last draft is resolved."""
    doc = Doc()
    assert doc.run("draft-insert", "--after", "Body text.", "--text", "Draft.")[0] == 0
    assert "\\usepackage[draft]{changes}" in doc.text()
    did = doc.first_draft()
    assert doc.run("reject", "--id", did)[0] == 0
    text = doc.text()
    assert "\\usepackage[draft]{changes}" not in text
    assert "\\definechangesauthor" not in text
    assert "latex-draft" not in text


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
