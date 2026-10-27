# latex-draft — voice-to-LaTeX editing skill

Edit a LaTeX document from simple text input (e.g. voice transcription),
turn-based, with drafts that are visible in the compiled PDF before being
merged.

## Modes (declare one at the start of the conversation)

State the active mode in your first reply and stay in it unless the user
switches. The mode is a promise about *how* you may use the tool.

- **Writer** — may use `draft-insert`, `draft-replace`, `draft-delete`
  (and `draft-comment`). Responsible for actually writing the text.
- **Commenter** — may use **only** `draft-comment`. Adds footnotes that mark,
  question, or annotate the text. Never inserts, replaces, or deletes body
  text, and never restructures sections. If a change would require editing
  the text, propose it in a comment instead ("suggest rewriting this
  sentence as ...") and let the user ask a Writer.

## Writer best practices (scientific writing)

- **Default to several versions.** Unless the user asks for exactly one,
  draft 2–3 alternative wordings/structures (e.g. two `draft-replace` drafts
  with different `--new`, or two inserts). Say which you recommend and why.
- **Be critical; no unsubstantiated claims.** Do not draft claims that the
  document does not support (no invented results, citations, or superlatives
  like "state-of-the-art" / "novel" without evidence). If the user asks for
  one, draft it but add a `draft-comment` flagging what needs a citation or
  verification.
- **Push back.** If an instruction is factually wrong, inconsistent with the
  rest of the document, or would damage the argument, say so plainly *before*
  drafting, and offer the corrected alternative. Never silently comply.
- **Ask instead of guessing.** If the instruction is vague ("make the
  paper", "improve this"), do not draft anything — ask 2–4 concrete
  questions first (see "Clarify before acting").

## The one tool this skill relies on

`tools/latex_draft.py` (CLI: `python3 tools/latex_draft.py <file.tex> <command>`).
It is the only supported way to modify `.tex` files. Never edit the document
by hand; every change goes through the tool so it is marked, backed up and
reversible.

Drafts are wrapped in the LaTeX [`changes`](https://ctan.org/pkg/changes)
package markup (`\added` / `\deleted`) plus `% latex-draft:<id>` marker
comments. The document preamble is auto-augmented with
`\usepackage[draft]{changes}`, so recompiling shows additions in green and
deletions struck through — the user inspects the PDF and decides.

**Multi-file documents.** If the paper is stitched together from several
files (`\input`/`\include` in the main file), pass `--file <name.tex>` to any
`draft-*` command to draft directly into that part (e.g.
`draft-insert --file chapter1.tex --after "..." --text "..."`). The file
must be one of the main file's `\input`/`\include` dependencies. Anchors are
searched only within the target file, the `changes` package is only injected
into the main file, and the changelog records which file each change
touched. Everything else — accept/reject, undo, backups, hand-edit detection
— works across all files of the document automatically. (`status` lists the
detected input files.)

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
4. Only when the user's answer makes the request specific, start the turn
   protocol below.

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

- `latex-draft <file> status` — takes the full initial backup on first use,
  confirms document state.
- `latex-draft <file> backup --note "session start"` — explicit named
  restore point (recommended when the file has no tool state yet).

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
`backups` — plus reading the `.tex` file. **Never** `draft-*`, `accept`,
`reject`, `undo`, or `compile` in IDLE.

### State 2: TURN (drafting a change)

One user request = one turn. Execute in this order:

1. **Turn-start safety check.** The first tool call of the turn verifies
   that the `.tex` file (and every `\input`/`\include` dependency) has not
   been edited by hand since the last turn. If the tool prints a `WARNING`,
   **read it carefully** and re-read the file (`status`, `changelog`,
   `cat`) before drafting — your anchors may have moved.
2. **Read the document** (`cat` the `.tex` file) to find anchors.
3. **Draft** the change. Always draft first; never write final text
   directly. (Mode rules apply: Writer may draft anything, Commenter only
   `draft-comment`.)
   - Insert: `latex-draft draft-insert --after "<unique line>" --text "..."`
     (or `--before` / `--at-line N`; the anchor must match exactly one line
     in the target file)
   - Replace: `latex-draft draft-replace --old "..." --new "..."`
     (`--old` must occur exactly once outside draft blocks)
   - Delete: `latex-draft draft-delete --old "..."`
   - Comment (Commenter mode, or to flag something in Writer mode):
     `latex-draft draft-comment --after "<unique line>" --text "..."`
   - All four accept `--file <name.tex>` to edit an `\input`/`\include`
     part of a multi-file document instead of the main file.
   - If the instruction is ambiguous (several options), draft **several
     options** (Writer mode: this is the default, see best practices).
4. **Compile** (mandatory end of turn): `latex-draft compile`.
   The PDF viewer shows the updated PDF automatically. Compiling also ends
   the turn: it takes the per-turn backup and bumps the turn counter.
   **If the compile fails**, run `latex-draft repair` (see "Repair mode"
   below), then compile again.
5. **Report** draft ids and previews, and ask the user which to accept.

Allowed in TURN: `draft-insert`, `draft-replace`, `draft-delete`,
`draft-comment`, `compile`, `repair`, plus read-only commands (`list`,
`status`, `changelog`). **Never** `accept`, `reject`, or `undo` in TURN —
resolving happens in RESOLVE.

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
   `latex-draft undo --turn`.
4. **Compile** (mandatory end of turn): `latex-draft compile`.
5. Report what was merged/restored and return to IDLE.

Allowed in RESOLVE: `accept`, `reject`, `undo`, `compile`, `list`,
`status`. **Never** `draft-*` in RESOLVE — if the user asks for a new change
while resolving, finish the resolution first (steps 1–4), then start a new
TURN for the new request.

### Loop summary table

| State | Trigger | Allowed tool commands | Forbidden |
|---|---|---|---|
| SESSION START | conversation begins | `status`, `backup` | everything else |
| IDLE | user sends instruction | `status`, `list`, `changelog`, `backups` | `draft-*`, `accept`, `reject`, `undo`, `compile` |
| TURN | specific editing request | `draft-*`, `compile`, `repair`, read-only | `accept`, `reject`, `undo` |
| REVIEW | PDF is open for inspection | none (wait) | everything |
| RESOLVE | user decides on drafts | `accept`, `reject`, `undo`, `compile`, `list`, `status` | `draft-*` |

Every turn — TURN or RESOLVE — **must end with exactly one `compile`**.
A turn that drafted but did not compile is an error: fix it by compiling
before doing anything else.

## Command reference

All commands: `python3 tools/latex_draft.py <main.tex> <command> [options]`.
`<main.tex>` is always the **main** file of the document, even when editing
a chapter (use `--file` for that).

| Command | Purpose | Key options |
|---|---|---|
| `draft-insert` | draft inserting text at an anchor | `--after` / `--before` / `--at-line N` (exactly one), `--text`, `--file` |
| `draft-replace` | draft replacing existing text | `--old` (unique), `--new`, `--file` |
| `draft-delete` | draft deleting existing text | `--old` (unique), `--file` |
| `draft-comment` | draft a footnote at an anchor (Commenter mode) | `--after` / `--before` / `--at-line N`, `--text`, `--file` |
| `list` | list drafts: id, status, op, preview | — |
| `accept` | merge draft(s) into the file, removing markup | `--id <id>` or `--all` |
| `reject` | discard draft(s), restoring original text | `--id <id>` or `--all` |
| `compile` | recompile the PDF; ends the turn (per-turn backup) | — |
| `repair` | after a failed compile: analyze the log, apply safe fixes as drafts, suggest the rest | — |
| `backup` | take a manual snapshot | `--note "..."` |
| `undo` | revert recorded changes | `--n K` or `--turn` |
| `backups` | list snapshots | — |
| `changelog` | show the change log | `--last N` |
| `status` | summary: turn, drafts, changes, backups, pdf, inputs, warnings | — |

Exit codes: `0` success, `1` nothing to do / runtime failure, `2` usage error
(unknown draft id, ambiguous anchor, text not found, file not an input).

## Repair mode (when `compile` fails)

`latex-draft <file> repair` analyzes the last `.log` and:

- **Auto-fixes safely**, as normal *drafted* changes (marked with the
  `changes` package, backed up, changelogged, reviewable in the PDF — nothing
  is applied silently):
  - `\begin{X} ended by \end{Y}` — corrects the wrong `\end{Y}` to `\end{X}`
    (located in the main file or any `\input`/`\include` file).
  - undefined `\added`/`\deleted`/`\footnote` with no `changes` package —
    injects the preamble into the main file.
- **Prints `SUGGESTION`s** for everything else — undefined control
  sequences (typo? missing `\usepackage`?), undefined environments, missing
  files, runaway arguments (unbalanced braces) — with the log line number.
- Recompiles afterwards and reports whether the document builds again.

After `repair`, treat its fixes like any other drafts: `list`, review in the
PDF, `accept`/`reject`, then `compile`.

## Change log and undo

Every change the tool makes is appended to an append-only changelog
(`<file.tex>.latex-draft-changes.jsonl`) with the full content before and
after, so **any change can be undone individually** — even ones made turns
ago — and the whole history is inspectable.

- `latex-draft changelog [--last N]` — show the change log.
- `latex-draft undo --n K` — revert the K most recent changes (replays the
  recorded before-content).
- `latex-draft undo --turn` — revert **all** changes of the current turn.
- `latex-draft status` — summary; also warns if the file was edited by hand.

## Backups (automatic)

- On **first use** on a file, a full initial backup is taken: the main file
  plus every file it pulls in via `\input`/`\include` (kept forever).
- A snapshot is taken before **every** mutation (draft, accept, reject) and
  **once per turn** when the turn ends with `compile`.
- Retention: all snapshots < 1 h, one per hour for 24 h, one per day for 30 d
  (the full initial backup is never pruned).
- `latex-draft backup --note "..."` — manual snapshot.
- `latex-draft backups` — list snapshots.
- Per-file state (draft ids, statuses, file hashes, turn counter) lives in
  `<file.tex>.latex-draft.json`.

## Robustness against hand edits

The user may edit the `.tex` file directly, even mid-turn:

- **Between turns**: detected automatically at the start of the next tool
  call (content hashes of the main file and its `\input`/`\include`
  dependencies). The edit is recorded in the changelog as an `external`
  entry, the new state is adopted, and the agent is warned to re-read the
  file.
- **During a tool call**: a hash re-check happens immediately before every
  write; if the file changed while the change was being prepared, the write
  is **aborted** and the user's edit is never clobbered.

## Rules

- One turn = user input → turn-start check → drafted change(s) via the
  tool → `compile`. Follow the state machine above: know your state
  (IDLE / TURN / REVIEW / RESOLVE) before every tool call and only use the
  commands that state allows.
- Anchors (`--after`/`--before`/`--old`) must be unique in the file; if a
  match is not unique, re-read the file and use a longer anchor or
  `--at-line`.
- Keep drafted LaTeX idiomatic; escape `%`, `&`, `#`, `_` etc. as needed.
- Do not commit backups, state files, changelogs, or build artifacts (see
  `.gitignore`).
