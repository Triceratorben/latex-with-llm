---
name: latex-draft
description: >-
  Turn-based, draft-marked editing of a LaTeX document from simple text input
  (e.g. voice transcription). Drafts changes with the `changes` package so the
  user inspects them in the compiled PDF before they are merged. Supports
  Writer and Commenter modes, multi-file documents, changelog-based undo,
  hand-edit detection, backups, and repair of failed compiles. Use when the
  user wants to write, edit, review, or annotate a LaTeX paper by talking to
  it.
---

# latex-draft — voice-to-LaTeX editing skill

Edit a LaTeX document from simple text input (e.g. voice transcription),
turn-based, with drafts that are visible in the compiled PDF before being
merged.

## Invocation (use exactly this form)

Every tool call is:

```sh
python3 tools/latex_draft.py <main.tex> <command> [options]
```

`<main.tex>` is always the **main** file of the document (e.g.
`paper.tex`), even when editing a chapter — use `--file` for that. In this
guide, `latex-draft` is shorthand for `python3 tools/latex_draft.py <main.tex>`.
(If you install the package with `pip install .`, the `latex-draft` command
becomes available directly.)

Shell note: under **zsh**, do not put the invocation in a variable
(`LD="python3 tools/latex_draft.py main.tex"; $LD ...` — zsh does not
word-split variables, so `$LD` is one word and the command is not found).
Use a shell function (`ld() { python3 tools/latex_draft.py main.tex "$@"; }`)
or `pip install .` for the `latex-draft` command.

## Modes (declare one at the start of the conversation)

State the active mode in your first reply and stay in it unless the user
switches. For Writer, state the edit submode as well ("Writer, point edits";
see below). The mode is a promise about *how* you may use the tool.
**Default: if the user has not picked a mode, act as Writer** (the common
case for "write/edit my paper" requests); say which mode you assumed.

- **Writer** — may use `draft-insert`, `draft-replace`, `draft-delete`,
  `draft-preamble` (and `draft-comment`). Responsible for making the
  concrete formulation and phrasing from the input of the user —
  laying the mosaic stones, so to speak: turning rough spoken intent into
  precise, idiomatic scientific prose.
- **Commenter** — may use **only** `draft-comment`. Adds comments that mark,
  question, or annotate the text. Never inserts, replaces, or deletes body
  text, never restructures sections, and never changes the preamble
  (`draft-preamble` is a Writer capability). If a change would require
  editing the text or the preamble, propose it in a comment instead
  ("suggest rewriting this sentence as ...") and let the user ask a Writer.

The tool does not enforce the mode — it is a promise you make. Stay in it.

### Writer submodes: point, sentence, paragraph

Within Writer mode, *how much* of the text you may change is a second
promise. Declare it with the mode ("Writer, point edits") and stay in it
until the user asks for more.

1. **Point edits (default).** Only words and phrasing *within* a sentence.
   Sentence and paragraph structure stays untouched: no sentence is added,
   removed, split, merged, or reordered, and no paragraph is added, removed,
   or restructured. This is the default because it is the least destructive
   way to improve a paper that already exists.
2. **Sentence-level edits.** Everything in 1, plus whole sentences may be
   rewritten, inserted, or deleted. Paragraph structure still holds: the
   paragraphs stay where they are; none is added, removed, or reordered.
3. **Paragraph-level edits.** Everything in 2, plus whole paragraphs may be
   drafted, rewritten, or deleted.

**Change the submode only on request.** If an instruction cannot be carried
out at the current level ("rewrite this paragraph" while doing point edits),
that instruction *is* the request: name the level it needs ("that is a
paragraph-level edit") and work at that level for the turn. Never widen
silently, and never present a wider change as if it were a point edit. If the
user sets a level for the rest of the session ("let's rewrite section 3"),
keep it until they say otherwise, and say when you return to the default.

**What this means for drafts.** Point edits are `draft-replace` over the
phrase, inside one line. Sentence-level edits also use
`draft-insert`/`draft-delete` for whole sentences. Paragraph-level edits
draft whole paragraphs, including multi-paragraph text — the tool splits it
into one `\added`/`\deleted` per paragraph. `draft-preamble` is independent
of the submode, and Commenter mode is unaffected: it never edits text.

## Writer best practices (scientific writing)

- **Default to several versions.** Unless the user asks for exactly one,
  draft 2–3 alternative wordings (or, at sentence/paragraph level,
  alternative structures) — e.g. two `draft-replace` drafts with different
  `--new`, over the same `--old`. Label them with
  `--author A`, `--author B`, ... so each alternative gets a distinct color
  in the PDF and can be told apart. Say which you recommend and why.
  Alternatives are **sibling drafts**: draft each one over the same original
  text (`--old` is still found inside the first pending draft). This works
  when the drafts are **whole-line/whole-sentence drafts**. For a *point*
  edit (an inline block mid-line) the second alternative is **refused**
  ("--old text overlaps the words a pending point-edit draft changes"):
  either draft one wording for point edits, resolve the first draft before
  drafting the next, or draft the alternatives at whole-line granularity.
  Accepting one sibling automatically removes the other alternatives' markup
  and marks them rejected (`orphaned_by_overlap` in `list` output) — the
  discarded versions disappear from the PDF together with the one you kept.
  **`--author` labels alternatives, not provenance.** It is *not* "agent vs
  user": every draft is your proposal, and the user's own edits are detected
  as external changes, never as drafts. Use one label per alternative version
  (A, B, C, ...); the default label is `AI`. You may also use it to mark a
  draft as coming from a specific person the user names ("label this one
  'Alice's suggestion'" → `--author Alice`), but do not invent labels for
  the user's own text.
- **Be critical; no unsubstantiated or invented content.** Do not draft
  claims the document does not support (no invented results, numbers,
  examples, or superlatives like "state-of-the-art" / "novel" without
  evidence), and do not add content the user did not supply: a fact that is
  needed but missing becomes `TODO` plus a `draft-comment` naming what is
  needed. If the user asks for an unsupported claim, draft it but flag it in
  a `draft-comment`.
- **Never invent `\cite` keys or bibliography entries.** If a claim needs a
  reference and you do not know a real key, write `\cite{TODO}` plus a
  `draft-comment` ("needs a real citation"). The session-end check validates
  all references.
- **Check `\ref`/`\eqref` targets exist** before drafting a reference to a
  label that may not be there; if unsure, use `\ref{TODO}` plus a comment.
  Do not touch equations (renumbering, reordering) unless the user asked.
- **Confirm spoken math instead of guessing.** Dictated math is ambiguous
  ("x squared plus y" vs "x to the y"); restate your reading and confirm
  before drafting it.
- **Push back.** If an instruction is factually wrong, inconsistent with the
  rest of the document, or would damage the argument, say so plainly *before*
  drafting, and offer the corrected alternative. Never silently comply.
- **Ask instead of guessing.** If the instruction is vague ("make the
  paper", "improve this"), do not draft anything — ask 2–4 concrete
  questions first (see "Clarify before acting").

### Editing discipline (every submode)

The submode says how far a change may reach; these say how to make it.

- **Keep the diff minimal and the voice the author's.** Change the words that
  are wrong, not the ones you would have written differently. At point level
  prefer a word or clause over a rewritten sentence; do not raise or lower
  the register, and do not polish text the user did not ask about — note it
  in a `draft-comment` instead.
- **Use the paper's terminology, notation and units.** Grep the file for the
  term before choosing one ("method" vs "approach", "dataset" vs "data set")
  and reuse it; keep symbols, number formats (`0.5`, not `.5`) and unit
  spacing as they are; never rename a defined term or symbol.
- **Check what is pending before drafting over text.** Run
  `latex-draft list`. A new draft whose `--old` is text a pending draft
  already replaces becomes an *alternative* to it (a sibling): only one of
  the two can be accepted, and accepting one rejects the other. To build on
  a pending draft, accept or reject it first — drafting inside text a
  pending draft adds is refused outright.
- **Change only the region you were asked about.** One request, one region;
  anything else you notice goes into a `draft-comment`, not into the draft.
- **Settle house style once, then hold it.** Infer language, person ("we" vs
  passive), tense, serial comma and compound hyphenation from the document;
  ask about at most 2–3 genuinely ambiguous points at session start, and
  keep the answers for the session instead of deciding again each turn.

### Dictation conventions

Expect these spoken commands and map them deliberately (confirm if unsure):
"new paragraph" → blank line; "open quote"/"close quote" → ` `` ` / ` '' `;
"backslash X" → `\X`; "section X" → `\section{X}`. Match the document's
spelling variant (US/UK) unless the user says otherwise.

## The one tool this skill relies on

`tools/latex_draft.py`. It is the only supported way to modify `.tex` files.
Never edit the document by hand; every change goes through the tool so it is
marked, backed up and reversible.

Drafts are wrapped in the LaTeX [`changes`](https://ctan.org/pkg/changes)
package markup (`\added` / `\deleted`) plus `% latex-draft:<id>` marker
comments. The main file's preamble is auto-augmented with
`\usepackage[draft,authormarkup=brackets,authormarkupposition=right,deletedmarkup=sout]{changes}`
and one `\definechangesauthor` per profile, so recompiling shows additions
in the author's color and deletions struck through — the user inspects the
PDF and decides. **Profiles**: on the first successful body draft the tool
defines a profile for itself (`AI`, first palette color) and one for every
author parsed from the paper's `\author{...}` (each with its own color),
recorded as one `setup` changelog entry (undoable like any other change; a
full undo restores the original document). The agent drafts only under its
own `AI` profile; its mark in the PDF is a filled circle ● in its color, not a
text tag (the AI profile redefines `\Changes@output@author` to print the
circle for `AI` and delegate to the ordinary `(Name)` tag for everyone else),
while human profiles render as `(AliceSmith)` etc.

**Multi-file documents.** If the paper is stitched together from several
files, pass `--file <name.tex>` to any `draft-*` command to draft directly
into that part (e.g. `draft-insert --file chapter1.tex --after "..." --text "..."`).
Input files are discovered **transitively** via `\input`, `\include`,
`\subfile`, and `\import` — a chapter that itself `\input`s a subsection
works too (`--file sub.tex`). Like pdflatex, the tool resolves those paths
relative to the **main file's directory**, so `\input{chapters/sec}` inside
`chapters/ch1.tex` means `<main dir>/chapters/sec.tex`. Bibliography
(`.bib`) files are *not* tracked or edited. Anchors are searched only within
the target file. The `changes`
package (and `todonotes` for `--margin` comments) is injected into the **main
file's** preamble automatically when the first draft goes into a non-main
file — included files cannot carry a preamble, so draft markup in them
relies on the main file loading the package. Once no drafts remain pending
anywhere in the document, the auto-injected preamble is stripped again (some
classes reject the `changes` package in final mode). The changelog records
which file each change touched. Everything else — accept/reject, undo,
backups, hand-edit detection — works across all files of the document
automatically. (`status` lists the detected input files.)

**Anchors.** `--after`/`--before` match a substring of exactly one line;
`--old` may span several lines but must occur exactly once. Matches inside
`%` comments and inside pending draft blocks do not count (a commented-out
duplicate or a draft's own `\added` text is not a real occurrence), so an
anchor that "matched 2 lines" usually has one commented copy. `--at-line N`
is the **raw file line N, 1-based, exactly as your editor numbers lines** —
draft marker lines and the auto-injected preamble count too. **Line numbers
drift once drafts exist:** a draft block adds lines to the file, so a line
number read before drafting is wrong afterwards — `--at-line` is only safe
on a file with no pending blocks, or after re-reading the file. Keep **one
sentence per line** in the source — anchoring into a paragraph that is one
long line is awkward. If a match is not unique, re-read the file and use a
longer anchor or `--at-line`.

**`draft-insert --after`/`--before` are line-level, not inline.** The
inserted text becomes its own draft block on the line after (before) the
anchor's whole line — it cannot be placed mid-sentence. "Define an acronym
at first use" or any inline placement is a `draft-replace` job: replace a
unique phrase with itself plus the insertion.

**Draft placement.** `\added`/`\deleted`/`\footnote`/`\todo` are body
commands: in the preamble they abort compilation with "Missing
`\begin{document}`", which is why preamble changes have their own command.
A body draft whose anchor, line number or `--old` lands in the preamble is
therefore placed at the top of the body (insert/comment) or refused with a
pointer to `draft-preamble` (replace/delete), and nothing is ever written
after `\end{document}`, where TeX would ignore it. (The auto-injected `changes`
preamble is placed after the `\documentclass` line even when its options span
several lines — `\documentclass[\n  12pt,\n]{article}` is safe.) A mid-line `--old` (a point edit inside a longer
source line) is **not** widened to the whole line any more: the tool emits an
*inline* block — the unchanged text around the edit stays plain, and only the
changed words are wrapped in `\deleted{...}`/`\added{...}` (a difflib
word-level diff), so the review PDF strikes out just the changed words.
Inline diff also covers lines with `\cite{...}` or `$...$` math: math spans
and `\cmd{...}` arguments are atomic diff tokens, text-mode commands
(`\cite`, `\ref`, `\emph`, `\vspace`, ...) are allowed, and an unchanged
`\cite` stays outside the markup. If a `\cite`/`\footnote`/`\url` must be
struck out itself, the tool boxes it (`\mbox{...}`) inside `\deleted` — ulem
cannot strike those commands directly — and `reject` unboxes it, restoring the
exact original bytes. `\ref`/`\eqref` need no boxing: they compile fine
inside `\deleted`, unlike `\cite`/`\footnote`/`\url`. For **deleting a
`\footnote`** prefer `draft-delete --unmarked`: the marked form boxes the
footnote and is fragile on accept (bug-report-5); the unmarked delete removes
it directly and is still undoable. Text inside `[...]`/`{...}` of a command on the same
line, alignment cells (`&`), or multi-line spans still use the whole-line
block form — and a disjoint point edit on a line already covered by a
whole-line block composes into that block instead of chaining a second
full-line copy. In the file, a whole-line draft block occupies
whole lines, so the marker comments cannot swallow the rest of the line in the
review PDF; accept/reject merge/restore exactly the intended text.

**Word moves diff cleanly.** Moving a word across a bracketed span
(`recedes (shown in App.~A)` → `(shown in App.~A) recedes`) is a normal
copy-edit: the diff strikes the word at its old spot, adds it at the new
one, and leaves the span it crossed plain — the parenthesis is neither
struck out nor re-emitted, so the review PDF reads the move directly.
There is no need for `--unmarked` on a word move. Every inline diff is
verified before it is written: accept must give exactly the new text and
reject exactly the original bytes, or the tool falls back to the
whole-span form.

Several point edits on the *same* source line compose into a single
numbered block: the second edit is folded into the pending block's own line
(sibling `\deleted`/`\added` pairs, never nested), the block number bumps,
and the earlier draft is marked superseded — accept/reject of the one
remaining block settles all of them at once. (`list` previews of a composed
block mix tokens from the other edits on that line — a block whose only
change is `dog ran → dog walked` can preview as `lay walked` — so do not
quote a preview as if it were the diff; read the block in the source.)
**A composed block resolves as a unit** — there is currently no way to
accept or reject a single edit inside it. To drop one edit of many, reject
the block and re-draft the edits you want to keep (they are small, and the
changelog/`undo` still has them); to keep all but unsure about one, accept
and hand-edit. Mention this when the user reviews a block that composes
several dictated edits.

**What cannot be drafted.** `\added`/`\deleted` are *text* markup, so the
tool refuses (exit 2, nothing written) any draft that would put LaTeX
structure inside them: an environment boundary (`\begin{...}`/`\end{...}`,
e.g. moving a figure's `[h]` to `[htbp]`, deleting a whole figure, inserting
an `itemize`), an unescaped alignment `&`, a line inside a `tabular`/
`array` (a row break cannot be wrapped), a line inside display math
(`equation`, `align`, `\[ ... \]`), where the markup cannot be typeset at
all, a `\caption` line, a sectioning title (`\section`, `\subsection`,
`\subsubsection`, `\chapter`, `\part` — starred or optional-argument forms
included), or anything inside a float environment (`figure`,
`table`, `algorithm`, `listing` — a figure's `width=0.5\textwidth` included),
where the markup breaks the build (`! You can't use \hrule here` in captions,
`! LaTeX Error: Not allowed in LR mode` in titles).
For display math, captions, sectioning titles and float interiors use
`draft-replace --unmarked`
/ `draft-delete --unmarked`: the change is applied directly with no review
markup, but it is snapshotted, changelogged and `undo`-able — say so when you
report such a change. **Editing over accepted markup** (text already inside
an `\added{...}`/`\highlight{...}`/`\replaced{...}{...}` from an earlier
accept or from a co-author's tracked edits) is refused for marked drafts —
the changes package cannot nest. The route is `draft-replace --unmarked`,
and both anchors work: matching the inner words alone edits inside the
wrapper and keeps it (and its author mark) as-is, and matching the whole
`\added[id=...]{...}` command also works when you quote the exact command
text in `--old` (re-emit the wrapper in `--new` if the mark must survive).
An anchor that merely *straddles* the markup (starts outside, ends inside)
is refused with a message naming the accepted command — never re-draft over
a partial span. Without `--unmarked` the edit is refused; say so when you
report the change. Other edits *inside* one line are fine and are the
intended route for non-prose text: `\vspace{...}`, math such as `$x^2$` inside
a sentence, or an escaped `\&`. Preamble-level
changes go through `draft-preamble`. Content the markup cannot carry at all
— a whole float environment, a label — goes through `draft-insert --unmarked`: it
is applied with no markup, snapshot, changelog entry and `undo` intact, and
the reviewer only sees the note the tool adds. Anything else structural is a
hand edit, which the tool detects, records and backs up.

**Labels on equations.** `\label{eq:...}` cannot be drafted: inside the
environment `\added` fails to compile, and after `\end{equation}` it
compiles but captures an *empty* number (the counter lives inside the
environment's group), so references print nothing. Use
`draft-insert --unmarked`: it applies the text with no markup, snapshotted,
changelogged and undoable. For a label it also adds a draft-only note next
to the change — `\comment[id=AI]{labelled by AI}`, placed after the
environment (a margin note cannot live inside a float or in math) — so the
review PDF says something happened. The note shows in draft mode, disappears
in final mode, and `undo` removes it together with the label. Say so when you
report the change ("labelled unmarked; the note marks it in the PDF; `undo`
reverts both").

Note also that changes with no visible text effect (spacing, sizes) show only
the author mark in the review PDF, and that a graphic inside `\deleted` is
shown but not struck through — inspect the `changelog` or the source when the
PDF is inconclusive. The `changes` package renders the author mark directly
after the `\added`/`\deleted` argument, so next to closing punctuation the
mark can sit just inside it (`App.~C(•)`) — cosmetic, package-controlled,
and gone in final mode; do not "fix" it by editing the text.

**Verifying what the text actually says.** The review PDF is for layout and
markup; to read or grep the running text, use `latex-draft show --accepted`
(`--file <input>` for another file): it prints the file the way
`accept --all` would leave it — markup unwrapped, `\deleted` dropped, `^N`
markers and the injected preamble removed. Pending drafts appear in their
accepted form, so a reviewer's question like "what does that sentence say
now?" is a `show` + quote, not a PDF parse. For a submission copy use
`finalize --out`.

**Multi-paragraph drafts.** Drafted text may contain blank lines
(paragraph breaks) — the tool splits them into one `\added`/`\deleted`
command per paragraph automatically (the `changes` package cannot take a
blank line inside a single command argument). On accept/reject the
paragraphs are reassembled with blank lines, so the merged text is exactly
what was drafted.

## Clarify before acting (vague instructions)

**Do not carry out a turn — do not call any mutating tool command and do not
`compile` — if the instruction is not detailed enough to act on.** Vague
requests such as "make the paper", "write something", "improve this section",
or "fix it" must trigger clarification instead of drafting.

When the instruction is vague:

1. **Stop.** Make no tool calls that change anything. (Read-only calls like
   `status`, `list`, `changelog`, or `cat`-ing the file are fine and often
   help you ask better questions.)
2. **Ask 2–4 concrete questions** that turn the vague request into an
   actionable one. Good questions name the *what*, *where*, and *how*:
   - *What*: "Should I add a motivation paragraph, a methods section, or
     both?"
   - *Where*: "After the abstract, or at the end of the introduction?"
   - *How*: "Formal survey tone, or conversational? Roughly how long?"
   - *Scope*: "Just this section, or the whole paper?"
3. **Offer a concrete proposal** the user can approve with one word, e.g.
   "Say the word and I'll draft 2 versions of a 3-sentence motivation
   paragraph after the abstract."
4. Only when the user's answer makes the request specific, start the
   conversation loop below.

A request is specific enough when you can name: the exact text or location to
change, what the new text should say (or the concrete options to draft), and
the scope. "Add a sentence about X after the abstract" is specific; "make the
paper better" is not.

## The conversation loop (state machine)

The whole conversation is a loop over four states. **Know which state you
are in before every tool call — each state has its own allowed commands.**

```
SESSION START ──► IDLE ──► TURN ──► REVIEW ──► RESOLVE ──► IDLE ──► ...
                     ▲          │                     │
                     │          │ user says "undo"    │
                     └──────────┴─────────────────────┘
```

### State 0: SESSION START (once per conversation)

Run before the first edit:

- `latex-draft status` — takes the full initial backup on first use,
  confirms document state.
- `latex-draft backup --note "session start"` — explicit named restore point
  (recommended when the file has no tool state yet).

Allowed: `status`, `backup`. Nothing else is needed here.

### State 1: IDLE (waiting for the user)

The user sends an instruction. Decide first:

- **Vague?** → follow "Clarify before acting": ask 2–4 questions, offer a
  proposal, end the turn. **No drafting, no `compile`.** Stay in IDLE.
- **About pending drafts** ("keep the first one", "accept all",
  "reject that", "undo")? → go to RESOLVE.
- **A specific editing request?** → go to TURN.
- **A question about the document or the tool state** ("what changed?",
  "show my drafts")? → answer with read-only commands and stay in IDLE.

Allowed in IDLE: read-only commands only — `status`, `list`, `changelog`,
`backups`, `check` — plus reading the `.tex` files. **Never** `draft-*`,
`accept`, `reject`, `undo`, `compile`, or `repair` in IDLE.

### State 2: TURN (drafting a change)

One user request = one turn. Execute in this order:

1. **Turn-start safety check.** The first tool call of the turn verifies
   that the `.tex` files (the main file and every input dependency) have not
   been edited by hand since the last turn. If the tool prints a `WARNING`,
   **read it carefully** and re-read the file (`status`, `changelog`,
   `cat`) before drafting — your anchors may have moved.
2. **Read the document** (`cat` the target `.tex` file) to find anchors.
3. **Draft** the change. Always draft first; never write final text
   directly. (Mode rules apply: Writer may draft anything, Commenter only
   `draft-comment`.)
   - Insert: `latex-draft draft-insert --after "<unique line>" --text "..."`
     (or `--before` / `--at-line N`)
   - Replace: `latex-draft draft-replace --old "..." --new "..."`
   - Delete: `latex-draft draft-delete --old "..."`
   - Comment (Commenter mode, or to flag something in Writer mode):
     `latex-draft draft-comment --after "<unique line>" --text "..."`
     Comment text is typeset in the PDF: commands valid only in math mode
     (`\mathrm{d}x`) are rendered literally outside `$...$`, so a dictated
     command cannot break the build — but write math as `$...$` to keep it
     live.
     Use `--margin` for a todonotes margin note when the anchor is a section
     title or inside a table (a footnote breaks there and shifts pagination).
     `--margin` is **refused** inside a float environment (`figure`, `table`,
     ... — a `\caption` line is inside one) and inside math: `\todo` becomes
     `\marginpar`, which is illegal there, and inside a float it fails
     *silently* — later floats of the same class are dropped with no `!`
     error line. Anchor before/after the float instead, or put the reminder
     in a plain `%` comment in the source.
   - Preamble change (Writer mode): `latex-draft draft-preamble --text "..."`
     with exactly one of `--start` (after `\documentclass`), `--end` (before
     `\begin{document}`), or `--after "<unique preamble line>"`. Use it for
     preamble-only commands: `\renewcommand{\thesection}{\Roman{section}}`,
     `\setlength`, `\captionsetup`, font/geometry changes. These cannot go
     through `\added`/`\deleted` (the changes package typesets immediately,
     expands with `\protected@edef`, and opens a group — all wrong in a
     preamble), so a preamble draft is raw LaTeX between the markers. It
     compiles live, so the user sees the effect (e.g. Roman numeral
     headings) in the PDF; `accept` merges it verbatim, `reject` removes it.
   - All four body-text drafts accept `--file <name.tex>` (multi-file
     documents) and `--author <label>` (distinct color per alternative; see
     best practices). `draft-preamble` always edits the main file.
   - A label on an equation/figure/table, or content the markup cannot carry
     (a float environment): `draft-insert --unmarked --after "<anchor>"
     --text "\label{...}"`. It is applied at once with no markup and reports
     that `undo` reverts it; for a label it also adds the note that makes the
     change visible in the PDF (see "Labels on equations").
   - Display math, `\caption` lines, sectioning titles and float interiors
     cannot be marked either: use `draft-replace --unmarked --old "..." --new "..."`
     (or `draft-delete --unmarked`). The edit goes straight into the source —
     snapshotted, changelogged, `undo`-able, and `reject` inverts it — but
     the review PDF does not strike anything out; say so when reporting.
   - If the instruction is ambiguous (several options), draft **several
     options** (Writer mode: this is the default).
4. **Compile** (mandatory end of turn): `latex-draft compile`.
   The PDF viewer shows the updated PDF automatically. Compiling also ends
   the turn: it takes the per-turn backup and bumps the turn counter. A failed
   compile reports the `!` error lines of the `.log`. `.blg` problems
   (BibTeX duplicate entry keys, malformed entries — they never appear as
   `!` lines) are reported as a warning and do **not** by themselves fail
   the compile when the `.log` is clean and the PDF was written.
   **If the compile fails**, run `latex-draft repair` (see "Repair mode"
   below) and compile again — **a turn must not end with a broken build**:
   either fix it (repair or a corrective draft) or `undo` the change that
   broke it, then compile successfully.
5. **Report** draft ids and previews, plus the *effective* change in words:
   state `before → after` for anything the PDF cannot show (a figure size, a
   spacing value). Then ask the user which to accept.

Allowed in TURN: `draft-insert`, `draft-replace`, `draft-delete`,
`draft-preamble`, `draft-comment`, `compile`, `repair`, plus read-only
commands (`list`, `status`, `changelog`, `check`). **Never** `accept`,
`reject`, or `undo` in TURN — resolving happens in RESOLVE.
  *Exception:* when a `draft-*` command refuses with a message naming a
  pending draft ("...accept or reject that draft first"), the refusal is
  cleared by rejecting **that** draft — `reject --id <id>` in TURN is allowed
  for exactly that draft, then re-draft the change. Do not use this to
  resolve drafts the user has not seen.

**Turns with no edits** (the user only asked a question, or you only read
back text) make no tool changes and need **no `compile`** — compiling would
bump the turn counter and take a backup for nothing. End such turns with a
plain reply.

#### Chunking: one page at a time (default)

**Unless the user explicitly asks for a whole-document pass** ("go through
the whole paper", "do a full sweep", "review everything"), edit in **small
chunks: about one page of text at a time** — one section, one paragraph
block, or one screenful, whichever is smaller. Draft that chunk, `compile`,
report its draft ids and previews, and **stop**: let the user accept or
reject the chunk (REVIEW → RESOLVE) **before drafting the next one**. A page
of pending markup is reviewable in the PDF; ten pages are not, and the user
decides per chunk, not per file. Do not draft the entire document in one
turn to save round-trips. Only when the user prompts for it should you do a
whole-document pass — and even then, report the drafts chunk by chunk so
the user can resolve them page by page.

### State 3: REVIEW (the user looks at the PDF)

The compiled PDF is open in front of the user. You do nothing; the user
inspects the marked-up drafts. Your previous message already listed the
draft ids. Wait for the user's verdict.

### State 4: RESOLVE (the user decides on drafts)

The user's message names which drafts to keep or discard. Execute:

1. `latex-draft list` — confirm the current pending drafts and their ids.
2. For each draft the user named:
   - keep → `latex-draft accept --id <id>` or `accept --block N` (or `accept --all`)
   - discard → `latex-draft reject --id <id>` or `reject --block N` (or `reject --all`)
   (use `list` to see each draft's `#N` block number — the same number appears as a superscript at the start of the drafted text in the PDF, e.g. "keep block 2, reject block 1". The superscript sits at the **start of the drafted block**, which for whole-line blocks means the start of the source line: in one-paragraph-per-line sources it can be a sentence away from the changed words. `^58` in the PDF margin, `#58` in `list` output and the superscript `⁵⁸` are the same id in three notations)
3. If the user asks to undo something instead: `latex-draft undo --n K` or
   `latex-draft undo --turn`. Undo is **refuse-on-conflict**: a change is
   skipped (with a warning) if the file changed after it; use `undo --force`
   to restore the recorded content anyway.
4. **Compile** (mandatory end of turn): `latex-draft compile`.
5. Report what was merged/restored and return to IDLE.

Allowed in RESOLVE: `accept`, `reject`, `undo`, `compile`, `list`,
`status`. **Never** `draft-*` in RESOLVE — if the user asks for a new change
while resolving, finish the resolution first (steps 1–4), then start a new
TURN for the new request. (`accept --all` also resolves **unmarked**
draft records, reporting `accepted unmarked draft <id> (change kept in
<file>)` — so it is not purely "merge the marked drafts"; that is expected.)

### Comment lifecycle

A `draft-comment` is a draft like any other: it appears as a footnote (or
margin note with `--margin`) in the PDF, and is resolved the same way —
`accept` keeps it (as a real footnote/margin note in the document),
`reject` removes it (dismisses the comment). Accepted comments stay in the
final document; run `latex-draft check` before submission and reject the
comments you do not want to keep.

- **Prefix footnotes with `FOOTNOTE: `** when the comment is meant to be
  accepted as a footnote (e.g. `FOOTNOTE: Acronym check: ...`). Endnote-style
  classes (revtex `apsrev`) turn footnotes into numbered entries *inside the
  reference list*, so accepted comments silently change the visible reference
  count; the prefix makes them identifiable there and flags them for removal
  before submission.
- Accepted `--margin` notes keep the injected `\usepackage{todonotes}` alive:
  the preamble strip scans the main file **and every `\input`/`\include`
  file** and removes `todonotes` only once no `\todo` remains anywhere in the
  document.

### Loop summary table

| State | Trigger | Allowed tool commands | Forbidden |
|---|---|---|---|
| SESSION START | conversation begins | `status`, `backup` | everything else |
| IDLE | user sends instruction | `status`, `list`, `changelog`, `backups`, `check` | `draft-*`, `accept`, `reject`, `undo`, `compile`, `repair` |
| TURN | specific editing request | `draft-*`, `compile`, `repair`, read-only | `accept`, `reject`, `undo` |
| REVIEW | PDF is open for inspection | none (wait) | everything |
| RESOLVE | user decides on drafts | `accept`, `reject`, `undo`, `compile`, `list`, `status` | `draft-*` |

Every turn that changed something — TURN or RESOLVE — **must end with
exactly one successful `compile`**. A turn that drafted but did not compile
(or ended with a failed build) is an error: fix it by compiling before doing
anything else.

## Command reference

All commands: `python3 tools/latex_draft.py <main.tex> <command> [options]`.

| Command | Purpose | Key options | Used in state |
|---|---|---|---|
| `draft-insert` | draft inserting text at an anchor | `--after` / `--before` / `--at-line N` (exactly one), `--text`, `--file`, `--author`, `--unmarked` (no markup: for labels or content the PDF cannot show) | TURN |
| `draft-replace` | draft replacing existing text | `--old` (unique), `--new`, `--file`, `--author`, `--unmarked` (no markup: display math, captions, section titles, float interiors) | TURN |
| `draft-delete` | draft deleting existing text | `--old` (unique), `--file`, `--author`, `--unmarked` (same) | TURN |
| `draft-comment` | draft a comment at an anchor (Commenter mode) | `--after` / `--before` / `--at-line N`, `--text`, `--file`, `--author`, `--margin` | TURN |
| `draft-preamble` | draft a preamble change (raw LaTeX, not changes-marked; for `\renewcommand`, `\setlength`, `\captionsetup`, font/geometry) | `--start` / `--end` / `--after <unique preamble line>` (exactly one), `--text` | TURN |
| `list` | list drafts: block #, id, status, op, preview — each draft block shows a `^N` superscript in the PDF matching its `#N` | — | read-only: IDLE, TURN, RESOLVE |
| `accept` | merge draft(s) into the file, removing markup (and the `^N` marker) | `--id <id>`, `--block N`, or `--all` | RESOLVE |
| `reject` | discard draft(s), restoring original text | `--id <id>`, `--block N`, or `--all` | RESOLVE |
| `compile` | recompile the PDF; ends the turn (per-turn backup) — mandatory at the end of every TURN and RESOLVE that changed something | — | TURN, RESOLVE |
| `repair` | after a failed compile: analyze the log, apply safe fixes, suggest the rest | — | TURN |
| `backup` | take a manual snapshot | `--note "..."` | SESSION START (or anytime) |
| `undo` | revert recorded changes (refuse-on-conflict) | `--n K` or `--turn`, `--force` | RESOLVE |
| `backups` | list snapshots | — | read-only: any state |
| `changelog` | show the change log | `--last N` | read-only: any state |
| `status` | summary: turn, drafts, changes, backups, pdf, inputs, warnings | — | read-only: any state (SESSION START) |
| `check` | pre-submission check: no pending drafts, no stray markers, no leftover preamble; also prints non-failing `note:` lines (e.g. an abbreviation glued to a reference, `App.\ref{app:C}`, which typesets as one word) | — | read-only: any state (before submission / session end) |
| `show` | print a file the way `accept --all` would leave it (markup unwrapped, `\deleted` dropped, `^N` markers and the injected preamble removed) — the grep-able way to verify what the running text says | `--accepted` (default), `--file` | read-only: any state |
| `finalize` | write a clean copy of the sources: corrections kept, markup/notes/markers removed; original untouched | `--out DIR` | session end; any state (never edits the document) |

Exit codes: `0` success, `1` nothing to do / runtime failure / check failed,
`2` usage error (unknown draft id, ambiguous anchor, text not found, file
not an input).

## Repair mode (when `compile` fails)

`latex-draft repair` analyzes the last `.log` and:

- **Auto-fixes safely**, applied directly to the source (a repair must
  compile on its own, so it cannot rely on the `changes` package). Every
  fix is snapshotted and recorded in the changelog, so it can be undone
  with `undo` like any other change — nothing is applied silently:
  - a wrong `\end{Y}` from a `\begin{X}`/`\end{Y}` mismatch (located in the
    main file or any input file);
  - a missing `changes` preamble when a draft block uses
    `\added`/`\deleted`/`\todo`, or a `\definechangesauthor` placed before
    the package load.
- **Prints `SUGGESTION`s** for everything else — undefined control
  sequences (typo? missing `\usepackage`?), undefined environments, missing
  files, runaway arguments (unbalanced braces), `\footnote` in a context
  where it is not allowed (use `draft-comment --margin` there) — with the
  log line number.
- Recompiles afterwards and reports whether the document builds again.

If `repair` answers "no known error pattern was found" and dumps the log
tail, the usual cause in a draft session is markup landed where
`\deleted`/`\added` cannot be typeset (a sectioning title, a caption,
display math, a float interior, verbatim). The fix is not a repair: find the
offending draft (`list`, then locate its block in the source), `reject --id
<id>` it, and redo the edit with `--unmarked`.

After `repair`, the document is already patched; run `compile` to confirm
and review the fixed source. Fixes are undoable: `latex-draft undo --n K`.

**Silent failures `compile` cannot see**: a `\marginpar`/`\todo` placed
inside a float compiles with no `!` line but drops later floats of the same
class — the tell-tale is a graphic loaded in the `.log` ("used on input line
N") that never appears in a page-ship bracket, with its `\label` missing
from the `.aux` (the reference then prints "??"). That is why `--margin` is
refused inside floats and math at draft time.

## Pre-submission check (cleanup)

Before the document leaves draft mode (submission, sharing, session end),
run `latex-draft check`. It fails when:

- drafts are still pending (accept or reject them first);
- stray `% latex-draft:` markers remain in any file;
- the auto-injected preamble (`\usepackage[draft,...]{changes}`,
  `\definechangesauthor` lines, the agent's `\makeatletter` symbol-mark
  block, `\usepackage{todonotes}`) is left over after
  everything was resolved — **this matters**: some classes (revtex, achemso)
  reject the `changes` package in final mode. (The tool strips these
  automatically when the last draft is resolved; `check` catches leftovers
  from hand edits or crashes.)

`check` can also print `note:` lines that do **not** fail the run (exit 0).
One it looks for is an abbreviation glued to a reference: `App.\ref{app:C}`
typesets as a single word ("App.C") because there is no space between the
abbreviation's period and the reference — the fix in the source is
`App.~\ref{app:C}`. Quote the note to the user, name the source line, and
let them decide; the tool does not edit on its own.

When the user wants files to hand over or submit — readable without this
tool — run `latex-draft finalize [--out DIR]`. It writes a clean copy
(default `<main>-final/`): accepted corrections materialized (`\added` /
`\highlight` unwrapped, `\replaced` keeps the new text), `\deleted`,
`\comment` and `\todo` dropped, markers and the injected preamble removed,
input files kept at their relative paths, referenced graphics and `.bib`
copied. It refuses while drafts are pending, and it never touches the
original or its history — the review loop stays intact on the working copy.
It does not flatten `\input` into one file and does not prune unused files:
`latexpand` or `arxivable` do that if the user is preparing an arXiv bundle.
Say what the copy is and where it is; do not replace the working document
with it unless the user asks. `finalize` is allowed in either mode: it does
not change the document, only writes a copy.

## Change log and undo

Every change the tool makes is appended to an append-only changelog
(`<main.tex>.latex-draft-changes.jsonl`) with the full content before and
after, so **any change can be undone individually** — even ones made turns
ago — and the whole history is inspectable.

- `latex-draft changelog [--last N]` — show the change log.
- `latex-draft undo --n K` — revert the K most recent changes (replays the
  recorded before-content; **refuse-on-conflict**, see RESOLVE).
- `latex-draft undo --turn` — revert **all** changes of the current turn.
- `latex-draft status` — summary; also warns if a file was edited by hand.

Undo re-derives the draft registry from the document: a draft whose block an
undo brings back is pending again (and can be accepted or rejected), and a
record whose block is gone is closed instead of lingering as a phantom
pending draft. If a draft block was hand-deleted from the file,
`accept --all` / `reject --all` resolve the drafts that still exist and warn
about the vanished ones; asking for that specific id is an error (exit 2).
If the user deleted a whole chapter, `undo` reports it and restores nothing
(exit 1) — `undo --force` recreates the file from the changelog.

## Backups (automatic)

- On **first use** on a file, a full initial backup is taken: the main file
  plus every input file (kept forever).
- A snapshot is taken before **every** mutation (draft, accept, reject) and
  **once per turn** when the turn ends with `compile`. The snapshot holds the
  file that is about to change, so a chapter edit is recoverable from its own
  snapshot, and restoring recreates a chapter the user deleted since.
- Retention: all snapshots < 1 h, one per hour for 24 h, one per day for 30 d
  (the full initial backup is never pruned).
- `latex-draft backup --note "..."` — manual snapshot.
- `latex-draft backups` — list snapshots.
- Per-file state (draft ids, statuses, file hashes, turn counter) lives in
  `<main.tex>.latex-draft.json`.

## Robustness against hand edits

The user may edit the `.tex` files directly, even mid-turn:

- **Between turns**: detected automatically at the start of the next tool
  call (content hashes of the main file and all input dependencies). The
  edit is recorded in the changelog as an `external` entry, the new state is
  adopted, and the agent is warned to re-read the files.
- **During a tool call**: a hash re-check happens immediately before every
  write; if a file changed while the change was being prepared, the write is
  **aborted** and the user's edit is never clobbered.

## File fidelity

The tool treats `.tex` files as bytes the user owns:

- **Line endings are preserved.** LF, CRLF (Windows) and CR-only (classic
  Mac) documents all work. A draft is written with the document's own
  separator, so the marked-up file the user reviews has no mixed endings;
  reject restores the exact bytes, and a mixed-EOL file keeps every untouched
  line as it was. A draft always lands between `\begin{document}` and
  `\end{document}`, where TeX actually typesets it.
- **Encoding is UTF-8.** A non-UTF-8 file (e.g. latin-1) is refused with a
  clean error message (`latex-draft: <file> is not valid UTF-8 ...`), not a
  traceback. A UTF-8 BOM is tolerated.
- **Unbalanced braces in drafted text** (e.g. `if x} else`) are escaped so
  the `\added`/`\deleted`/`\footnote` argument stays parseable and accept
  reproduces the full text — no silent truncation.
- **Preamble ownership**: the auto-strip at resolve time removes only the
  lines the tool itself injected (`\usepackage[draft,...]{changes}`,
  `\definechangesauthor`, the agent's `\makeatletter`/`\let\latexdraft@...`/
  `\def\Changes@output@author...` symbol-mark block, `\usepackage{todonotes}`).
  A `\usepackage{changes}`
  or `\definechangesauthor` you wrote yourself is never touched. The strip is
  skipped while accepted text is kept marked or a label note is present,
  since that markup needs the package; `todonotes` is likewise kept while
  any accepted `\todo` remains in any input file. `finalize` removes both
  (and the injected lines) from its clean copy.
- **Accepted-text provenance (default)**: `accept` keeps the agent's edits
  wrapped in `\\added[id=AI]{...}` (dark green by default) so the review
  PDF still shows what the agent wrote; the injected preamble is kept while
  any accepted text is still marked, and `check` reports the document clean
  with a reminder to build production with `\\usepackage[final]{changes}`,
  where the marks vanish and the text prints plainly. Opt out with
  `[accept] keep_markup = false` in `.latex-draft.cfg` (next to the main
  file). Drafting over text that is still marked is refused — the changes
  package cannot nest `\\added`.
- **Hashes are relative**: state records content hashes relative to the main
  file's directory, so copying, renaming or moving the project does not look
  like a hand edit. A corrupt state file is not fatal: the tool says so and
  recovers the pending drafts from the marker blocks in the document.

## Session end (when the user is done editing)

Before closing a session, finish with a **reference check**:

1. `latex-draft changelog` — review what changed this session. If any turn
   added citations (`\cite{...}`, `\bibliography`, `\addbibresource`,
   `thebibliography`), validate them:
2. **Spawn a subagent** to check the validity of the references — every
   `\cite{key}` must have a matching bibliography entry, and every entry
   should be cited. Ask the subagent to report dangling `\cite`s and unused
   entries; fix them as new drafts (Writer) or flag them with
   `draft-comment` (Commenter).
3. **If a subagent is unavailable, check yourself**: `grep` the `.tex`
   files for `\cite{...}` keys and for the corresponding
   `\bibitem{...}` entries / `.bib` keys, and compare the two sets.
4. Run `latex-draft check` (clean pre-submission state), then a final
   `latex-draft compile` so the PDF is up to date, and `latex-draft status`
   to confirm.
5. If the user wants the files themselves to hand over or submit, run
   `latex-draft finalize` (see "Pre-submission check"): it writes a clean
   copy with the corrections materialized and no trace of the tool. The
   working copy keeps its history and stays the one you keep editing.

## Rules

- One turn = user input → turn-start check → drafted change(s) via the
  tool → `compile`. Follow the state machine above: know your state
  (IDLE / TURN / REVIEW / RESOLVE) before every tool call and only use the
  commands that state allows.
- Edit in chunks by default: roughly **one page of text per turn**, then
  wait for the user's accept/reject before drafting the next chunk. A
  whole-document pass only when the user asks for one.
- Anchors (`--after`/`--before`/`--old`) must be unique in the target file;
  keep one sentence per line in the source.
- Keep drafted LaTeX idiomatic; escape `%`, `&`, `#`, `_` etc. as needed.
- Do not commit backups, state files, changelogs, `finalize` output
  (`*-final/`) or build artifacts (see `.gitignore`).

## Reducing approval prompts (Mistral Vibe)

This skill's only side-effecting mechanism is one shell command,
`python3 tools/latex_draft.py <main.tex> <command>`. If you run Vibe's
`smart-approve` agent, every such call may trigger an approval prompt.
The user can clear them by allowlisting the command in Vibe's
configuration (`~/.vibe/config.toml` or a trusted project's
`./.vibe/config.toml`):

```toml
[tools.bash]
permission = "ask"
allowlist = [
  "python3 tools/latex_draft.py *",
  "python3 */tools/latex_draft.py *",
  "latex-draft *",
  "latexmk *",
  "pdflatex *",
]
```

Allowlisted commands skip the prompt even under `smart-approve`
(permission rules are applied before the classifier). The `allowed-tools`
field in a skill's `SKILL.md` frontmatter does **not** grant auto-approval —
the bash allowlist is the mechanism. See `README.md` → "Reducing approval
prompts in Mistral Vibe" for the full snippet.
