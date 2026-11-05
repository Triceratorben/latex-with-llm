# latex-with-llm

Simple text input (e.g. from voice) drives an LLM agent that edits a LaTeX
document. Every edit is **drafted first**: the change is inserted into the
`.tex` file clearly marked with the LaTeX
[`changes`](https://ctan.org/pkg/changes) package, so the recompiled PDF
shows exactly what would change. The user inspects the PDF, picks the draft
(or one of several options) to keep, and the tool merges it into the document
without markings — or rejects it. Acceptance keeps the agent's edits marked (dark green) in the review PDF until you switch the package to final mode; a config can opt out (see *Accepted text stays marked*).

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
carry a preamble) as `\usepackage[draft,authormarkup=brackets,authormarkupposition=right,deletedmarkup=sout]{changes}`
plus one `\definechangesauthor` per profile — the agent's own `AI` profile and one
for every author in the paper's `\author{...}`, each with its own color — and
stripped again once no drafts remain pending, only the lines the tool
injected are removed, never your own.

**Alternatives are distinguishable**: pass `--author A`, `--author B`, ...
when drafting several versions — each author gets a distinct color in the
PDF. The label marks *which alternative* a draft is (not agent-vs-user
provenance; every draft is the agent's proposal, and the user's own edits
are detected as external changes). The default label is `AI`. The agent's own
mark in the review PDF is a filled circle ● in its color (not a text tag);
human profiles render as `(Name)` in their color. Alternatives
are sibling drafts over the same original text: accepting one removes the
others' markup and marks them rejected (`orphaned_by_overlap`). Siblings
work at whole-line/whole-sentence granularity; a second alternative over a
*point* edit (inline block mid-line) is refused until the first is resolved. **Comments**
default to footnotes; use `draft-comment --margin` for a todonotes margin
note at a title or inside a table. `--margin` is refused inside a float
(`figure`/`table`, captions included) and in math — a `\marginpar` there
silently drops later floats — so anchor before/after the float instead.
Prefix accepted footnote comments with `FOOTNOTE: ` so that endnote-style
classes (revtex `apsrev` lists footnotes as numbered references) keep them
identifiable in the bibliography. **`undo` is
refuse-on-conflict** (a change is skipped if the file changed after it;
`--force` overrides). **`latex-draft <file> check`** is the pre-submission
check: no pending drafts, no stray `latex-draft:` markers, and no leftover
auto-injected preamble (which some classes reject in final mode; the tool
strips it automatically once no drafts remain pending anywhere in the
document — unless accepted text kept marked or a label note still needs the
package). **Preamble changes** (`\renewcommand{\thesection}{\Roman{section}}`,
`\setlength`, `\captionsetup`, font/geometry) cannot go through the `changes`
package, so `draft-preamble` drafts them as raw LaTeX between markers in the
main file's preamble: the PDF shows the effect live, and `accept` merges the
command verbatim, `reject` removes it.

The skill has two modes, declared at the start of a conversation (see
`SKILL.md`): **Writer** (drafts actual text changes) and **Commenter** (only
adds drafted footnotes — never alters the body text). Writer has three edit
depths, also declared up front and widened only on request: **point edits**
(words and phrasing inside a sentence, the default — rendered as an inline
word-level diff: only the changed words are struck out in the review PDF, not
the whole source line; several point edits on the same line compose into
one numbered draft block), **sentence-level**, and
**paragraph-level**. In Writer mode the agent follows scientific-writing
practices: it defaults to drafting several versions, keeps the diff minimal
and the author's voice, uses the paper's own terminology, avoids
unsubstantiated claims and invented content, pushes back when an instruction
seems wrong, and asks 2–4 concrete clarifying questions instead of drafting
when an instruction is too vague (e.g. "make the paper").

At **session end** the agent runs a reference check: it spawns a subagent to
validate that every `\cite{key}` has a matching bibliography entry (or checks
them itself if a subagent is unavailable), fixes or flags problems as drafts,
and finishes with a final `compile` and `status`. When the user wants files
to hand over or submit, it runs `finalize` (see *Finalizing* below) rather
than editing the markup out of the working copy.

## How it fits together (for an agent picking this up)

**Intent**: the user talks to their paper (plain text, e.g. voice
transcription). An LLM agent follows the protocol in `SKILL.md` and carries
out each request as a *draft*: the change is written into the `.tex` source
marked with the `changes` package, the PDF is recompiled, and only after the
user has inspected the marked PDF does the tool merge (`accept`) or discard
(`reject`) the draft. Nothing is ever written into the document silently.

**Structure**: there is exactly **one tool that may modify `.tex` files** —
`tools/latex_draft.py` (Python 3.10+, standard library only;
`pip install .` also installs a `latex-draft` command). The agent never edits
`.tex` by hand; it only calls the tool. The tool keeps its own bookkeeping
next to the main file (all git-ignored):

| Sidecar file | Contents |
|---|---|
| `<main>.latex-draft.json` | state: draft registry, per-file hashes, turn counter |
| `<main>.latex-draft-changes.jsonl` | append-only changelog: full before/after content of every change (draft, accept, reject, repair, hand edit) — the basis of `undo` |
| `<main>.latex-draft.lock` | flock serializing concurrent invocations |
| `.latex-draft-backups/` | snapshots: full initial backup on first use, one before every mutation, one per turn at `compile` |

```
user text -> agent (SKILL.md protocol) -> latex-draft CLI -> .tex (draft marked)
          -> compile -> review PDF -> user verdict -> accept/reject -> compile
```

**The conversation loop** (full rules in `SKILL.md` → "The conversation
loop"): each state has its own allowed commands — know your state before
every call.

| State | When | Allowed commands | Forbidden |
|---|---|---|---|
| SESSION START | conversation begins | `status`, `backup` | everything else |
| IDLE | waiting for the user | read-only: `status`, `list`, `changelog`, `backups`, `check` | `draft-*`, `accept`, `reject`, `undo`, `compile`, `repair` |
| TURN | a specific edit was requested | `draft-insert`, `draft-replace`, `draft-delete`, `draft-comment`, `draft-preamble`, then mandatory `compile` (`repair` if it fails), plus read-only | `accept`, `reject`, `undo` |
| REVIEW | user inspects the PDF | none — wait | everything |
| RESOLVE | user decides on drafts | `accept`, `reject`, `undo`, mandatory `compile`, `list`, `status` | `draft-*` |

Every turn that changed something must end with exactly one successful
`compile`.

## Layout

- `SKILL.md` — the skill guide an LLM agent follows: modes (Writer /
  Commenter), edit depths, the conversation state machine with per-state
  allowed commands, the full command reference, repair, backups, undo,
  hand-edit robustness, session end.
- `tools/latex_draft.py` — the single editing tool the skill relies on
  (all mutations of `.tex` files go through it). See *How it fits together*
  above for the sidecar state files it creates next to the main file.
- `pyproject.toml` — packaging metadata; `pip install .` installs the
  `latex-draft` console command.
- `examples/paper.tex` — a minimal example document for the quick start.
- `.latex-draft.cfg` (optional, next to the main file) — accept behaviour:
  keep markup, author color, label style.
- `plans/` — hand-off plans for other agents; delete a plan when its change lands.
- `tests/README.md` — what each suite covers and the commands to run them;
  `tests/test_*.py` is the main suite, `tests/parked/extended/` reviewer 2's (parked for now — not part of the default suite),
  `tests/additional/` reviewer 1's standalone scripts, `tests/parked/`
  parked ideas. Run from the repo root:

  ```sh
  python3 -m unittest discover -s tests          # main suite (needs pdflatex+pdftotext for the PDF tests)
  python3 tests/additional/test_*.py             # reviewer 1's standalone scripts
  ```

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

# Hand over or submit: a clean copy, corrections kept, no tool traces
python3 tools/latex_draft.py my-paper.tex finalize      # -> my-paper-final/
```

Requires Python 3.10+ (standard library only). Compiling requires
`latexmk` or `pdflatex` with the `changes` package; without a LaTeX engine
the tool still drafts/backs up and skips compilation with a warning.

**Shell note (zsh)**: don't store the invocation in a variable —
`LD="python3 tools/latex_draft.py my-paper.tex"; $LD draft-insert ...` fails
in zsh, which does not word-split variables (`$LD` becomes one word).
Use a shell **function** or `pip install .` (the `latex-draft` command):

```sh
ld() { python3 tools/latex_draft.py my-paper.tex "$@"; }   # then: ld draft-insert ...
# or: pip install .  ->  latex-draft my-paper.tex draft-insert ...
```

**Repair mode**: when `compile` fails, `latex-draft <file> repair` analyzes
the `.log`, auto-fixes what is safe (a wrong `\end{...}` from a
`\begin{...}/\end{...}` mismatch; a missing `changes` preamble) directly in
the source — snapshotted and changelogged, so undoable with `undo` — and
prints suggestions for everything else (undefined commands/environments,
missing files, unbalanced braces). When `repair` reports "no known error
pattern was found", the usual cause in a draft session is markup where
`\deleted`/`\added` cannot be typeset: reject the offending draft and redo
the edit with `--unmarked`. A failed `compile` also prints problems
from `<stem>.blg`: BibTeX errors (duplicate entry keys, malformed author
lines) never show up as `!` lines in the `.log`. And one failure has no log
evidence at all — a `\marginpar`/`\todo` inside a float silently drops later
same-class floats (tell-tale: the graphic is "used on input line N" in the
`.log` but never page-shipped, and its `\label` is missing from the `.aux`).
See `SKILL.md` → "Repair mode".

## Command reference

All commands: `python3 tools/latex_draft.py <main.tex> <command> [options]`.

| Command | Purpose | Key options |
|---|---|---|
| `draft-insert` | draft inserting text at an anchor | `--after` / `--before` / `--at-line N` (exactly one), `--text`, `--file`, `--author`, `--unmarked` |
| `draft-replace` | draft replacing existing text | `--old` (unique), `--new`, `--file`, `--author`, `--unmarked` (no markup: display math, captions, float interiors) |
| `draft-delete` | draft deleting existing text | `--old` (unique), `--file`, `--author`, `--unmarked` (same) |
| `draft-comment` | draft a comment at an anchor (Commenter mode) | `--after` / `--before` / `--at-line N`, `--text`, `--file`, `--author`, `--margin` |
| `draft-preamble` | draft a preamble change (raw LaTeX between markers, not `\added`/`\deleted` — the changes package cannot typeset in the preamble; use for `\renewcommand`, `\setlength`, `\captionsetup`, font/geometry) | `--start` / `--end` / `--after <unique preamble line>` (exactly one), `--text` |
| `list` | list drafts: block #, id, status, op, preview — each draft block is numbered `^N` in the PDF, matching its `#N` in `list` | — |
| `accept` | merge draft(s) into the file, removing markup (and the `^N` marker) | `--id <id>`, `--block N`, or `--all` |
| `reject` | discard draft(s), restoring original text | `--id <id>`, `--block N`, or `--all` |
| `compile` | recompile the PDF; ends the turn (per-turn backup) | — |
| `repair` | after a failed compile: analyze the log, apply safe fixes, suggest the rest | — |
| `backup` | take a manual snapshot | `--note "..."` |
| `undo` | revert recorded changes (refuse-on-conflict) | `--n K` or `--turn`, `--force` |
| `backups` | list snapshots | — |
| `changelog` | show the change log | `--last N` |
| `status` | summary: turn, drafts, changes, backups, pdf, inputs, warnings | — |
| `check` | pre-submission check: no pending drafts, no stray markers, no leftover preamble; prints non-failing `note:` lines (e.g. an abbreviation glued to a reference) | — |
| `show` | print a file the way `accept --all` would leave it (markup unwrapped, `^N` markers removed) — the grep-able way to verify the running text | `--accepted` (default), `--file` |
| `finalize` | write a clean copy of the sources: corrections kept, markup/notes/markers removed; original untouched | `--out DIR` |

**Citations and math in point edits are safe.** Lines containing `\cite{...}`
or `$...$` math use the same inline word-level diff as plain prose (math spans
and `\cmd{...}` tokens are kept atomic, and unchanged citations stay outside
the markup). If a `\cite`/`\footnote`/`\url` itself must be struck out (e.g. a
whole-line replace), the tool wraps it in `\mbox{...}` inside `\deleted` —
ulem's strike-through cannot carry those commands otherwise — and `reject`
removes the box again, restoring the exact original bytes. (`\ref`/`\eqref`
need no boxing — they compile fine inside `\deleted`. For deleting a
`\footnote`, prefer `draft-delete --unmarked`: the marked form is fragile on
accept.) Several point edits
on one line always compose into a single numbered block, whole-line fallback
included — no chained full-line copies that re-render the whole paragraph.
A `list` preview of a composed block mixes tokens from the other edits on
that line, so read the block in the source rather than quoting the preview.
**Word moves diff cleanly**: moving a word across a bracketed span strikes it
at its old spot and adds it at the new one, leaving the crossed span plain —
no `--unmarked` needed, and every inline diff is verified byte-exactly
against accept and reject before it is written (falling back to the
whole-span form if it would not round-trip). A composed block resolves as a
unit: to drop one edit of several, reject the block and re-draft the rest.

**Reading the running text**: the review PDF is for layout and markup. To
read or grep what the document actually says — including pending drafts in
their accepted form — use `latex-draft show --accepted` (`--file <input>` for
another file): markup unwrapped, `\deleted` dropped, markers and the injected
preamble removed.

**Structural edits are refused, not miscompiled.** `\added`/`\deleted` are
text markup: an environment boundary (`\begin{figure}`…), an unescaped `&`,
a line inside a `tabular`/`array`, or a line inside display math
(`equation`, `align`, `\[ … \]`) cannot go inside the argument — TeX reports
"Not in outer par mode", "Missing } inserted" or "Missing $ inserted". Such
drafts exit `2` and write nothing, instead of producing a document that will
not build. Also refused: a `\caption` line, a sectioning title (`\section`,
`\subsection`, `\subsubsection`, `\chapter`, `\part` — starred or
optional-argument forms included, where the markup breaks the build with
`! LaTeX Error: Not allowed in LR mode`) and anything inside a float
environment (`figure`, `table`, … — `width=0.5\textwidth` → `width=0.9\textwidth`
included), where the markup breaks the build (`! You can't use \hrule here`).
For display math, captions, sectioning titles and float interiors,
`draft-replace --unmarked` / `draft-delete --unmarked` patch the source
directly — no review markup, but still snapshotted, changelogged and undoable.
Editing over **accepted markup** (text inside an `\added{...}` from an
earlier accept) is refused for marked drafts; the route is
`draft-replace --unmarked --old "<the whole \\added{...} command>"`, which
drops the provenance mark. Other edits *inside* one line
still work, covering most non-prose changes: `\vspace{...}`, math inside a
sentence, escaped `\&`. Preamble-level changes use `draft-preamble` (the
injected `changes` preamble is placed after a multi-line `\documentclass[...]`
line correctly);
content the markup cannot carry at all (a float environment, a label) can be
applied with `draft-insert --unmarked`, which leaves no markup but is still
snapshotted, changelogged and undoable. Everything else structural is a hand
edit (detected, recorded in the changelog, and backed up). Two caveats for
the review PDF: a change with no visible text effect (spacing, a size) shows
only the author mark, and a graphic inside `\deleted` is shown twice without
a strike-through — use `changelog` or the source when the PDF is
inconclusive.

**Labels are the exception with no review surface of their own.** A `\label`
on an equation cannot be marked: inside the environment `\added` fails to
compile, and after `\end{equation}` it compiles but captures an empty number.
Use `draft-insert --unmarked --after 'y = mx^2' --text '\label{eq:fit}'`:
the label is applied with no markup, still snapshotted, changelogged and
revertible with `undo`. Next to it the tool adds a draft-only note
(`\comment[id=AI]{labelled by AI}`, after the environment, since a margin
note cannot live inside a float or in math), so the review PDF says that
something changed; the note is dropped in final mode and removed with the
label by `undo`.

**Concurrency.** Invocations on the same document are serialized with a lock
file (`<main>.latex-draft.lock`), so rapid successive or parallel `draft-*`
calls queue instead of racing; each still gets its own snapshot and changelog
entry. `--at-line N` counts raw file lines (1-based, as your editor shows
them), including draft-marker and injected-preamble lines — so a line
number read before drafting drifts once a draft block adds lines; use
`--at-line` only on a file with no pending blocks, or re-read first.
`--after`/`--before` are line-level: the insert becomes its own block on the
neighbouring line, not mid-sentence (inline placement is a `draft-replace`).
Anchors
(`--after`/`--before`) and `--old` never match inside `%` comments or pending
draft blocks. Comment text with math-only commands outside `$...$` (e.g.
`\mathrm{d}x`) is rendered literally, so it cannot break the build.
`compile` gates on the final `.log` first: any `!` error line fails the turn.
A non-zero latexmk/pdflatex exit fails it too — unless the final `.log` has
no `!` lines and the PDF was written, in which case the compile succeeded
(likely a BibTeX/`.bib` issue, which this tool does not manage) and any
`.blg` problems are reported as a warning; a non-zero exit without a PDF is
still a hard failure.

Exit codes: `0` success, `1` nothing to do / runtime failure / check failed,
`2` usage error (unknown draft id, ambiguous anchor, text not found, file
not an input).
See `SKILL.md` for the full guide: modes, the conversation state machine,
and the per-state command rules.

## Accepted text stays marked (dark green) until final mode

By default `accept` keeps the agent's edits visible: accepted insert/replace
text stays wrapped in the changes markup (`\\added[id=AI]{...}`, or
`\\highlight[...]` with `style = highlight`), in dark green (the default
agent color), and the auto-injected `\\usepackage[draft]{changes}` line
stays so it still compiles. Pending drafts are marked the same way. To merge
accepted text plainly instead, put a `.latex-draft.cfg` next to the main
file:

```ini
[accept]
keep_markup = false   # merge accepted text plain (default: keep it marked)
color = blue          # agent color override (default: darkgreen)
style = highlight     # "highlight" instead of "added"
```

- **Review**: build normally (`latex-draft compile`) — accepted text stays
  marked, in its color, with the author mark the package adds (● for `AI`).
- **Production**: switch the package line to `\\usepackage[final]{changes}` —
  the changes package prints added text plainly and drops deleted text, so
  the marks disappear from the PDF without touching the prose. `latex-draft
  check` reports a document with kept marks as clean and reminds you of
  this; it does not call the kept preamble a leftover.

For a background-highlighted look rather than an underline, load the package
as `\\usepackage[draft,highlightmarkup=background]{changes}` or add
`\\sethighlightmarkup{background}` after it. Drafting over text that is
still marked is refused — the changes package cannot nest `\\added{...}`
— so edit the whole `\\added{...}` command instead. `reject` stays
byte-exact, and `finalize` still writes a clean copy without any markup.

## Finalizing: clean sources for submission

`latex-draft <main.tex> finalize [--out DIR]` writes a second copy of the
document (default `<main>-final/` next to the main file) in which the
corrections are materialized and every trace of the tool is gone:

| In the working copy | In the clean copy |
|---|---|
| `\added[id=AI]{text}`, `\highlight{text}` | `text` |
| `\replaced[id=AI]{new}{old}` | `new` |
| `\deleted{text}`, `\comment{...}`, `\todo{...}` | removed |
| `% latex-draft:<id>` marker lines | removed |
| injected `\usepackage[draft,...]{changes}`, `\definechangesauthor` lines, agent symbol-mark block, `\usepackage{todonotes}` | removed |

Input files keep their relative paths, and referenced graphics and `.bib`
files are copied, so the output builds as-is. It refuses while drafts are
pending, and it never touches the working copy, its state, changelog or
backups — the review loop continues there. Because the copy has no markup,
it builds the same PDF that `changes` in final mode would, but without the
package. The default output directory (`<main>-final/`) is git-ignored.

`finalize` is deliberately not a flattener and does not prune unused files.
For an arXiv bundle, run [`latexpand`](https://ctan.org/pkg/latexpand) or
[arxivable](https://github.com/maximelucas/arxivable) on the finalized
directory: they handle single-file flattening, `\includegraphics`
normalization and unused-file pruning, but they do not understand the
`changes` markup, so they cannot materialize the corrections themselves.

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
If you wrap the tool in a shell function (recommended under zsh — variables
are not word-split there), allowlist the wrapper name too, e.g. `"ld *"`.
Alternatives: run Vibe with `--agent auto-approve` (approves *everything* —
use only in a sandbox), or press `2` at an approval prompt to allow a command
for the rest of the session.

Note: the `allowed-tools` field in a skill's `SKILL.md` frontmatter is read
by Vibe but **not enforced** — it does not grant auto-approval. The bash
allowlist above is the mechanism that skips the prompts.
