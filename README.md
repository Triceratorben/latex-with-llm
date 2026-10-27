# latex-with-llm

Simple text input (e.g. from voice) drives an LLM agent that edits a LaTeX
document. Every edit is **drafted first**: the change is inserted into the
`.tex` file clearly marked with the LaTeX
[`changes`](https://ctan.org/pkg/changes) package, so the recompiled PDF
shows exactly what would change. The user inspects the PDF, picks the draft
(or one of several options) to keep, and the tool merges it into the document
without markings — or rejects it.

Every change is recorded in an append-only **changelog** (full content before
and after, plus which file it touched), so any change can be undone
individually (`undo --n K`) or per turn (`undo --turn`). `.tex` files are
backed up automatically: a full initial backup (main file plus
`\input`/`\include` dependencies) on first use, a snapshot of the file about to
change before every edit, and one per turn at `compile`. The tool detects hand
edits — between turns (with a warning, recorded in the changelog) and even
mid-write (the change is aborted, never clobbered) — and the content hashes are
keyed relative to the document, so copying or moving the project is not
mistaken for a hand edit.

**File fidelity**: LF, CRLF and CR-only documents all work, and a draft
inherits the document's own line separators — the marked-up file the user
reviews is not a mix of endings, and reject restores the exact bytes. A
document that mixes endings keeps each untouched line as it was. Documents
must be UTF-8 (a non-UTF-8 file gets a clean error, not a traceback);
unbalanced braces in drafted text are escaped so accept never truncates; and
the auto-strip at resolve time removes only the preamble lines the tool itself
injected — your own `\usepackage{changes}` or `\definechangesauthor` lines are
never touched.

**Multi-file papers** (a `paper.tex` stitched together from chapters) are
fully supported: pass `--file chapter1.tex` to any `draft-*` command to edit
that part directly. Input files are discovered **transitively** via
`\input`/`\include`/`\subfile`/`\import`, so nested structures
(`paper.tex` → `chapter1.tex` → `sub.tex`) work the same way. Commented-out
(`% \input{...}`) and verbatim-environment `\input`s are not real inclusions
and are ignored. Anchors are matched within the target file, drafts are
resolved per file, and undo/changelog/hand-edit detection span the whole
document. Draft markup lives in the target file, but the `changes` package is
loaded in the **main file's** preamble automatically (included files cannot
carry a preamble), and stripped again once no drafts remain pending — only
the lines the tool injected are removed, never your own.

**Alternatives are distinguishable**: pass `--author A`, `--author B`, ...
when drafting several versions — each author gets a distinct color in the
PDF. The label marks *which alternative* a draft is (not agent-vs-user
provenance; every draft is the agent's proposal, and the user's own edits
are detected as external changes). The default label is `LLM`. Alternatives
are sibling drafts over the same original text: accepting one removes the
others' markup and marks them rejected (`orphaned_by_overlap`). **Comments**
default to footnotes; use `draft-comment --margin` for a todonotes margin
note inside captions, titles, tables, or math. **`undo` is
refuse-on-conflict** (a change is skipped if the file changed after it;
`--force` overrides). **`latex-draft <file> check`** is the pre-submission
check: no pending drafts, no stray `latex-draft:` markers, and no leftover
auto-injected preamble (which some classes reject in final mode; the tool
strips it automatically once no drafts remain pending anywhere in the
document). **Preamble changes** (`\renewcommand{\thesection}{\Roman{section}}`,
`\setlength`, `\captionsetup`, font/geometry) cannot go through the `changes`
package, so `draft-preamble` drafts them as raw LaTeX between markers in the
main file's preamble: the PDF shows the effect live, and `accept` merges the
command verbatim, `reject` removes it.

The skill has two modes, declared at the start of a conversation (see
`SKILL.md`): **Writer** (drafts actual text changes) and **Commenter** (only
adds drafted footnotes — never alters the body text). In Writer mode the
agent follows scientific-writing practices: it defaults to drafting several
versions, avoids unsubstantiated claims, pushes back when an instruction
seems wrong, and asks 2–4 concrete clarifying questions instead of drafting
when an instruction is too vague (e.g. "make the paper").

At **session end** the agent runs a reference check: it spawns a subagent to
validate that every `\cite{key}` has a matching bibliography entry (or checks
them itself if a subagent is unavailable), fixes or flags problems as drafts,
and finishes with a final `compile` and `status`.

## Layout

- `SKILL.md` — the skill guide: the turn protocol an LLM agent follows.
- `tools/latex_draft.py` — the single editing tool the skill relies on.
- `examples/paper.tex` — a minimal example document.
- `tests/` — test suite for the tool (`python3 -m unittest discover -s tests`,
  plus the standalone reviewer suites: `python3 tests/additional/test_*.py`).
  `tests/extended/` holds the extended (reviewer 2) suite; it is run by the
  same discovery command and skips its real-LaTeX tests when no engine or
  LaTeX packages are installed.

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

# Multi-file paper: draft directly into a chapter
python3 tools/latex_draft.py paper.tex draft-insert --file intro.tex \
  --after 'Intro body.' --text 'A drafted intro sentence.'
python3 tools/latex_draft.py paper.tex compile   # changes markup lives in intro.tex; preamble in paper.tex

# User looks at the PDF, then:
python3 tools/latex_draft.py my-paper.tex list
python3 tools/latex_draft.py my-paper.tex accept --id <id>   # merge cleanly
python3 tools/latex_draft.py my-paper.tex compile

# Safety net:
python3 tools/latex_draft.py my-paper.tex changelog      # what changed, when
python3 tools/latex_draft.py my-paper.tex undo --n 1     # revert last change
python3 tools/latex_draft.py my-paper.tex undo --turn    # revert this turn
python3 tools/latex_draft.py my-paper.tex backups       # list snapshots
```

Requires Python 3.10+ (standard library only). Compiling requires
`latexmk` or `pdflatex` with the `changes` package; without a LaTeX engine
the tool still drafts/backs up and skips compilation with a warning.

**Repair mode**: when `compile` fails, `latex-draft <file> repair` analyzes
the `.log`, auto-fixes what is safe (a wrong `\end{...}` from a
`\begin{...}/\end{...}` mismatch; a missing `changes` preamble) directly in
the source — snapshotted and changelogged, so undoable with `undo` — and
prints suggestions for everything else (undefined commands/environments,
missing files, unbalanced braces). See `SKILL.md` → "Repair mode".

## Command reference

All commands: `python3 tools/latex_draft.py <main.tex> <command> [options]`.

| Command | Purpose | Key options |
|---|---|---|
| `draft-insert` | draft inserting text at an anchor | `--after` / `--before` / `--at-line N` (exactly one), `--text`, `--file`, `--author` |
| `draft-replace` | draft replacing existing text | `--old` (unique), `--new`, `--file`, `--author` |
| `draft-delete` | draft deleting existing text | `--old` (unique), `--file`, `--author` |
| `draft-comment` | draft a comment at an anchor (Commenter mode) | `--after` / `--before` / `--at-line N`, `--text`, `--file`, `--author`, `--margin` |
| `draft-preamble` | draft a preamble change (raw LaTeX between markers, not `\added`/`\deleted` — the changes package cannot typeset in the preamble; use for `\renewcommand`, `\setlength`, `\captionsetup`, font/geometry) | `--start` / `--end` / `--after <unique preamble line>` (exactly one), `--text` |
| `list` | list drafts: id, status, op, preview | — |
| `accept` | merge draft(s) into the file, removing markup | `--id <id>` or `--all` |
| `reject` | discard draft(s), restoring original text | `--id <id>` or `--all` |
| `compile` | recompile the PDF; ends the turn (per-turn backup) | — |
| `repair` | after a failed compile: analyze the log, apply safe fixes, suggest the rest | — |
| `backup` | take a manual snapshot | `--note "..."` |
| `undo` | revert recorded changes (refuse-on-conflict) | `--n K` or `--turn`, `--force` |
| `backups` | list snapshots | — |
| `changelog` | show the change log | `--last N` |
| `status` | summary: turn, drafts, changes, backups, pdf, inputs, warnings | — |
| `check` | pre-submission check: no pending drafts, no stray markers, no leftover preamble | — |

Exit codes: `0` success, `1` nothing to do / runtime failure / check failed,
`2` usage error (unknown draft id, ambiguous anchor, text not found, file
not an input).
See `SKILL.md` for the full guide: modes, the conversation state machine,
and the per-state command rules.

## Reducing approval prompts in Mistral Vibe

This skill runs entirely through one shell command —
`python3 tools/latex_draft.py <main.tex> <command>` — plus `compile`, which
runs `latexmk`/`pdflatex` itself. Under Vibe's `smart-approve` agent every
tool call can still trigger an approval prompt. To clear the skill's
commands for automatic use, allowlist them in your Vibe configuration
(`~/.vibe/config.toml`, or `./.vibe/config.toml` in a trusted project):

```toml
[tools.bash]
permission = "ask"
allowlist = [
  "python3 tools/latex_draft.py *",
  "python3 */tools/latex_draft.py *",
  "latex-draft *",
  "latexmk *",
  "pdflatex *",
  "pdftotext *",
  "grep *",
  "cat *",
  "ls *",
  "git status",
  "git diff *",
  "git log *",
]
```

Allowlisted commands run without a prompt even under `smart-approve` — the
permission rules are applied before the classifier. Adjust the path patterns
to where you installed the skill (e.g. `~/.vibe/skills/latex-with-llm`).
Alternatives: run Vibe with `--agent auto-approve` (approves *everything* —
use only in a sandbox), or press `2` at an approval prompt to allow a command
for the rest of the session.

Note: the `allowed-tools` field in a skill's `SKILL.md` frontmatter is read
by Vibe but **not enforced** — it does not grant auto-approval. The bash
allowlist above is the mechanism that skips the prompts.
