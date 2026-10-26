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

## Turn protocol (strictly one per user request)

1. **Receive** the user's instruction (text; voice is transcribed upstream —
   irrelevant to the tool).
2. **Read the document** (`cat` the `.tex` file) to find anchors.
3. **Draft** the change. Always draft first; never write final text directly.
   - Insert: `latex-draft draft-insert --after "<unique line>" --text "..."`
     (or `--before` / `--at-line N`; the anchor must match exactly one line)
   - Replace: `latex-draft draft-replace --old "..." --new "..."`
     (`--old` must occur exactly once)
   - Delete: `latex-draft draft-delete --old "..."`
   - If the instruction is ambiguous (several options), draft **several
     options** (e.g. two inserts, or two replaces with different `--new`);
     the user picks one to accept and rejects the rest. (Writer mode: this is
     the default, see above.)
- In Commenter mode use `draft-comment` instead: attach a footnote at the
  relevant anchor (`--after "<unique line>"`), e.g. "TODO: needs citation",
  "This claim contradicts Section 2", "Consider splitting this paragraph".
4. **Compile** (mandatory end of turn): `latex-draft compile`.
   A PDF inspector/viewer shows the updated PDF automatically.
5. **Report** draft ids and previews, and ask the user which to accept.

## Resolving drafts (next turn, after the user looks at the PDF)

- `latex-draft list` — show pending/resolved drafts with previews.
- `latex-draft accept --id <id>` or `--all` — merge into the `.tex` **without**
  any `changes` markup or marker comments.
- `latex-draft reject --id <id>` or `--all` — restore the original text.
- End the turn with `latex-draft compile` again.

## Backups and undo (automatic)

- A timestamped snapshot is taken before **every** mutation (draft, accept,
  reject) in `.latex-draft-backups/<file.tex>/`.
- An automatic snapshot is taken at most every 5 minutes while editing.
- Retention: all snapshots < 1 h, one per hour for 24 h, one per day for 30 d.
- `latex-draft backup --note "..."` — manual snapshot.
- `latex-draft undo --n K` — restore the K-th most recent snapshot.
- `latex-draft backups` / `latex-draft status` — inspect snapshots and state.
- Per-file state (draft ids, statuses) lives in `<file.tex>.latex-draft.json`.

## Rules

- One turn = user input → drafted change(s) via the tool → `compile`.
- Anchors (`--after`/`--before`/`--old`) must be unique in the file; if a
  match is not unique, re-read the file and use a longer anchor or
  `--at-line`.
- Keep drafted LaTeX idiomatic; escape `%`, `&`, `#`, `_` etc. as needed.
- Do not commit backups, state files, or build artifacts (see `.gitignore`).
