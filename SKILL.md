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

## Modes (declare one at the start of the conversation)

State the active mode in your first reply and stay in it unless the user
switches. The mode is a promise about *how* you may use the tool.
**Default: if the user has not picked a mode, act as Writer** (the common
case for "write/edit my paper" requests); say which mode you assumed.

- **Writer** — may use `draft-insert`, `draft-replace`, `draft-delete`,
  `draft-preamble` (and `draft-comment`). Responsible for making the
  concrete formulation and phrasing from the input of the user —
  crystallizing the ideas, so to speak: turning rough spoken intent into
  precise, idiomatic scientific prose.
- **Commenter** — may use **only** `draft-comment`. Adds comments that mark,
  question, or annotate the text. Never inserts, replaces, or deletes body
  text, never restructures sections, and never changes the preamble
  (`draft-preamble` is a Writer capability). If a change would require
  editing the text or the preamble, propose it in a comment instead
  ("suggest rewriting this sentence as ...") and let the user ask a Writer.

The tool does not enforce the mode — it is a promise you make. Stay in it.

## Writer best practices (scientific writing)

- **Default to several versions.** Unless the user asks for exactly one,
  draft 2–3 alternative wordings/structures (e.g. two `draft-replace` drafts
  with different `--new`, over the same `--old`). Label them with
  `--author A`, `--author B`, ... so each alternative gets a distinct color
  in the PDF and can be told apart. Say which you recommend and why.
  Alternatives are **sibling drafts**: draft each one over the same original
  text (`--old` is still found inside the first pending draft). Accepting one
  automatically removes the other alternatives' markup and marks them
  rejected (`orphaned_by_overlap` in `list` output) — the discarded versions
  disappear from the PDF together with the one you kept.
  **`--author` labels alternatives, not provenance.** It is *not* "agent vs
  user": every draft is your proposal, and the user's own edits are detected
  as external changes, never as drafts. Use one label per alternative version
  (A, B, C, ...); the default label is `LLM`. You may also use it to mark a
  draft as coming from a specific person the user names ("label this one
  'Alice's suggestion'" → `--author Alice`), but do not invent labels for
  the user's own text.
- **Be critical; no unsubstantiated claims.** Do not draft claims that the
  document does not support (no invented results, citations, or superlatives
  like "state-of-the-art" / "novel" without evidence). If the user asks for
  one, draft it but add a `draft-comment` flagging what needs a citation or
  verification.
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
`\usepackage[draft]{changes}` and a `\definechangesauthor` per author, so
recompiling shows additions in the author's color and deletions struck
through — the user inspects the PDF and decides.

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
`--old` may span several lines but must occur exactly once (outside draft
blocks). Keep **one sentence per line** in the source — anchoring into a
paragraph that is one long line is awkward. If a match is not unique,
re-read the file and use a longer anchor or `--at-line`.

**Draft placement.** `\added`/`\deleted`/`\footnote`/`\todo` are body
commands: in the preamble they abort compilation with "Missing
`\begin{document}`", which is why preamble changes have their own command.
A body draft whose anchor, line number or `--old` lands in the preamble is
therefore placed at the top of the body (insert/comment) or refused with a
pointer to `draft-preamble` (replace/delete), and nothing is ever written
after `\end{document}`, where TeX would ignore it. In the file, a draft
block always occupies whole lines — a mid-line `--old` widens to its
line(s) in the marked-up source, so the marker comments cannot swallow the
rest of the line in the review PDF; accept/reject still merge/restore
exactly the intended text.

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
     Use `--margin` for a todonotes margin note when the anchor is inside a
     caption, section title, table, or math (a footnote breaks there and
     shifts pagination).
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
   - If the instruction is ambiguous (several options), draft **several
     options** (Writer mode: this is the default).
4. **Compile** (mandatory end of turn): `latex-draft compile`.
   The PDF viewer shows the updated PDF automatically. Compiling also ends
   the turn: it takes the per-turn backup and bumps the turn counter.
   **If the compile fails**, run `latex-draft repair` (see "Repair mode"
   below) and compile again — **a turn must not end with a broken build**:
   either fix it (repair or a corrective draft) or `undo` the change that
   broke it, then compile successfully.
5. **Report** draft ids and previews, and ask the user which to accept.

Allowed in TURN: `draft-insert`, `draft-replace`, `draft-delete`,
`draft-preamble`, `draft-comment`, `compile`, `repair`, plus read-only
commands (`list`, `status`, `changelog`, `check`). **Never** `accept`,
`reject`, or `undo` in TURN — resolving happens in RESOLVE.

**Turns with no edits** (the user only asked a question, or you only read
back text) make no tool changes and need **no `compile`** — compiling would
bump the turn counter and take a backup for nothing. End such turns with a
plain reply.

### State 3: REVIEW (the user looks at the PDF)

The compiled PDF is open in front of the user. You do nothing; the user
inspects the marked-up drafts. Your previous message already listed the
draft ids. Wait for the user's verdict.

### State 4: RESOLVE (the user decides on drafts)

The user's message names which drafts to keep or discard. Execute:

1. `latex-draft list` — confirm the current pending drafts and their ids.
2. For each draft the user named:
   - keep → `latex-draft accept --id <id>` (or `accept --all`)
   - discard → `latex-draft reject --id <id>` (or `reject --all`)
3. If the user asks to undo something instead: `latex-draft undo --n K` or
   `latex-draft undo --turn`. Undo is **refuse-on-conflict**: a change is
   skipped (with a warning) if the file changed after it; use `undo --force`
   to restore the recorded content anyway.
4. **Compile** (mandatory end of turn): `latex-draft compile`.
5. Report what was merged/restored and return to IDLE.

Allowed in RESOLVE: `accept`, `reject`, `undo`, `compile`, `list`,
`status`. **Never** `draft-*` in RESOLVE — if the user asks for a new change
while resolving, finish the resolution first (steps 1–4), then start a new
TURN for the new request.

### Comment lifecycle

A `draft-comment` is a draft like any other: it appears as a footnote (or
margin note with `--margin`) in the PDF, and is resolved the same way —
`accept` keeps it (as a real footnote/margin note in the document),
`reject` removes it (dismisses the comment). Accepted comments stay in the
final document; run `latex-draft check` before submission and reject the
comments you do not want to keep.

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

| Command | Purpose | Key options |
|---|---|---|
| `draft-insert` | draft inserting text at an anchor | `--after` / `--before` / `--at-line N` (exactly one), `--text`, `--file`, `--author` |
| `draft-replace` | draft replacing existing text | `--old` (unique), `--new`, `--file`, `--author` |
| `draft-delete` | draft deleting existing text | `--old` (unique), `--file`, `--author` |
| `draft-comment` | draft a comment at an anchor (Commenter mode) | `--after` / `--before` / `--at-line N`, `--text`, `--file`, `--author`, `--margin` |
| `draft-preamble` | draft a preamble change (raw LaTeX, not changes-marked; for `\renewcommand`, `\setlength`, `\captionsetup`, font/geometry) | `--start` / `--end` / `--after <unique preamble line>` (exactly one), `--text` |
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

After `repair`, the document is already patched; run `compile` to confirm
and review the fixed source. Fixes are undoable: `latex-draft undo --n K`.

## Pre-submission check (cleanup)

Before the document leaves draft mode (submission, sharing, session end),
run `latex-draft check`. It fails when:

- drafts are still pending (accept or reject them first);
- stray `% latex-draft:` markers remain in any file;
- the auto-injected preamble (`\usepackage[draft]{changes}`,
  `\definechangesauthor`, `\usepackage{todonotes}`) is left over after
  everything was resolved — **this matters**: some classes (revtex, achemso)
  reject the `changes` package in final mode. (The tool strips these
  automatically when the last draft is resolved; `check` catches leftovers
  from hand edits or crashes.)

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
  lines the tool itself injected (`\usepackage[draft]{changes}`,
  `\definechangesauthor`, `\usepackage{todonotes}`). A `\usepackage{changes}`
  or `\definechangesauthor` you wrote yourself is never touched.
- **Accepted-text provenance (optional)**: with
  `[accept] keep_markup = true` in `.latex-draft.cfg` (next to the main
  file), `accept` leaves the accepted insert/replace text wrapped in
  `\added[id=LLM]{...}` (or `\highlight`, and in the configured `color`), so
  the review PDF still shows what the agent wrote. The injected preamble is
  kept for that, and `check` reports the document clean with a reminder to
  build production with `\usepackage[final]{changes}`, where the marks
  vanish and the text prints plainly. Drafting over text that is still
  marked is refused — the changes package cannot nest `\added`.
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

## Rules

- One turn = user input → turn-start check → drafted change(s) via the
  tool → `compile`. Follow the state machine above: know your state
  (IDLE / TURN / REVIEW / RESOLVE) before every tool call and only use the
  commands that state allows.
- Anchors (`--after`/`--before`/`--old`) must be unique in the target file;
  keep one sentence per line in the source.
- Keep drafted LaTeX idiomatic; escape `%`, `&`, `#`, `_` etc. as needed.
- Do not commit backups, state files, changelogs, or build artifacts (see
  `.gitignore`).

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
