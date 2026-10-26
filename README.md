# latex-with-llm

Simple text input (e.g. from voice) drives an LLM agent that edits a LaTeX
document. Every edit is **drafted first**: the change is inserted into the
`.tex` file clearly marked with the LaTeX
[`changes`](https://ctan.org/pkg/changes) package, so the recompiled PDF
shows exactly what would change. The user inspects the PDF, picks the draft
(or one of several options) to keep, and the tool merges it into the document
without markings — or rejects it. All `.tex` files are backed up
automatically (before every change, plus at most every 5 minutes) and can be
undone.

The skill has two modes, declared at the start of a conversation (see
`SKILL.md`): **Writer** (drafts actual text changes) and **Commenter** (only
adds drafted footnotes — never alters the body text). In Writer mode the
agent follows scientific-writing practices: it defaults to drafting several
versions, avoids unsubstantiated claims, and pushes back when an instruction
seems wrong.

## Layout

- `SKILL.md` — the skill guide: the turn protocol an LLM agent follows.
- `tools/latex_draft.py` — the single editing tool the skill relies on.
- `examples/paper.tex` — a minimal example document.
- `tests/` — test suite for the tool (`python3 -m unittest discover -s tests`).

## Quick start

```sh
cp examples/paper.tex my-paper.tex

# Turn 1: user says "add a sentence about motivation after the abstract"
python3 tools/latex_draft.py my-paper.tex draft-insert \
  --after '\abstract{' --text 'Motivation is the key to great papers.'
python3 tools/latex_draft.py my-paper.tex compile   # PDF shows the addition

# Commenter mode: annotate without touching the text
python3 tools/latex_draft.py my-paper.tex draft-comment \
  --after 'We study a small problem.' --text 'TODO: cite a source here.'
python3 tools/latex_draft.py my-paper.tex compile

# User looks at the PDF, then:
python3 tools/latex_draft.py my-paper.tex list
python3 tools/latex_draft.py my-paper.tex accept --id <id>   # merge cleanly
python3 tools/latex_draft.py my-paper.tex compile

# Safety net:
python3 tools/latex_draft.py my-paper.tex backups
python3 tools/latex_draft.py my-paper.tex undo --n 1
```

Requires Python 3.10+ (standard library only). Compiling requires
`latexmk` or `pdflatex` with the `changes` package; without a LaTeX engine
the tool still drafts/backs up and skips compilation with a warning.
