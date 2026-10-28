# Tests

The suite has three parts, all runnable from the repo root:

```bash
python3 -m unittest discover -s tests          # 230 tests: main + loop + provenance + extended
python3 tests/additional/test_*.py             # reviewer 1: 50 assertions, standalone scripts
```

| Path | What it covers |
|---|---|
| `test_latex_draft.py` | per-command behaviour (drafts, resolve, undo, repair, backups) |
| `test_loop_contract.py` | the draft → review → accept/reject loop: drafts are visible in the PDF, endings stay uniform, reject restores bytes, undo survives deleted chapters |
| `test_accept_provenance.py` | opt-in `keep_markup`: accepted text stays marked in draft mode, prints plainly in final mode, config validation, nesting guard |
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
