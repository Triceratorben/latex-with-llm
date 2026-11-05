# Tests

The suite has three parts, all runnable from the repo root:

```bash
python3 -m unittest discover -s tests          # 348 tests: main + loop + provenance + finalize + author-profiles + session-report + extended
                                                       # (needs pdflatex+pdftotext on PATH for the PDF tests)
python3 tests/additional/test_*.py             # reviewer 1: 50 assertions, standalone scripts
```

| Path | What it covers |
|---|---|
| `test_latex_draft.py` | per-command behaviour (drafts, resolve, undo, repair, backups) |
| `test_loop_contract.py` | the loop: drafts visible in the PDF, uniform endings, byte-exact reject, undo after deleted files, batches settled over several rounds, unmarked equation labels and their notes, structural edits refused |
| `test_accept_provenance.py` | accepted text stays marked by default (dark green) until final mode; opt-out, config validation, nesting guard |
| `test_finalize.py` | the clean copy: corrections materialized, wrappers/notes/markers/injected preamble gone, original untouched, pending drafts refused, assets copied, output compiles |
| `test_author_profiles.py` | the first-turn profile setup: `\author{...}` parsing, AI symbol-mark hook, one `setup` changelog entry, idempotence, strip on reject-all, CRLF preservation, undo of draft+setup |
| `test_session_report_bugs.py` | session-report regressions: pending-insertion lines still compose, refusals name the block, sectioning titles refused with the `--unmarked` pointer, preview isolation, reject preserves accepted markup and restores the original, inline pure deletes, no empty `\added` |
| `extended/` | reviewer 2's extended suite (placement, atomicity, state consistency, discovery, backups, real-LaTeX end-to-end) |
| `additional/` | reviewer 1's scripts; each file is self-contained with its own tiny runner |

## Parked: shared fixture for the test suites

Not done; noted here so it is not rediscovered.

Every suite defines its own temp-project fixture, and the copies are large
and mostly identical: **657 lines** in total.

- `extended/test_ext_*.py` — ten copies of the same 33-line `Base`
  (`setUp`, `populate`, `reset`, `cli`, `text`, `draft_id`, and the identical
  `DOC`/`SENTENCE`/`INTRO` constants). ~330 lines, byte-identical.
- `additional/test_*.py` — ten `Doc` classes (20–40 lines each) with the same
  `run()`/`text()`/`path()` shape but different constructors.
- `test_latex_draft.py` (`LatexDraftCase`, 25) and `test_loop_contract.py`
  (`LoopCase`, 36) overlap but differ in helpers and error capture.

The obvious refactor is `tests/support.py` with one `TempProject` case class
plus the shared document constants, imported by every suite; for the
`additional/` scripts a superset `Doc` would need care because their
constructors differ.

Parked because `extended/` and `additional/` are reviewer artifacts imported
as delivered: rewriting them makes a future reviewer drop conflict line by
line, and the duplication costs only test lines, not test reliability.
