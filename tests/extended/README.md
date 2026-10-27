# Extended tests (reviewer 2)

These 10 test scripts come from a second review agent's report
(`latex-with-llm-extended-tests.md`). They were parked while the defects they
expose were open; they are now part of the default suite.

## Status

| File | Tests | Notes |
|---|---|---|
| `test_ext_01_resolution_correctness.py` | 8 | accept/reject splice for siblings, comments |
| `test_ext_02_state_consistency.py` | 7 | undo vs. draft registry, corrupt state file, moved project |
| `test_ext_03_failure_atomicity.py` | 3 | failed commands leave file, state and changelog untouched |
| `test_ext_04_draft_placement.py` | 8 | body drafts stay in the body, mid-line edits, editing pending text |
| `test_ext_05_markup_ownership.py` | 5 | only tool-injected preamble lines are stripped |
| `test_ext_06_file_fidelity.py` | 6 | CRLF/byte fidelity, encoding |
| `test_ext_07_input_discovery.py` | 6 | nested `\input` resolution, commented-out inclusions |
| `test_ext_08_backups.py` | 7 | per-change snapshots of chapters, restore of deleted chapters |
| `test_ext_09_cli_contract.py` | 6 | exit codes and output contract |
| `test_ext_10_end_to_end.py` | 12 | real LaTeX engine (skipped when unavailable) |

## Defects these tests pinned down, and the fixes

- `undo` left phantom pending drafts after undoing a draft or an accept. Undo
  now re-derives the registry from the document, and the auto-injected
  preamble provenance is recovered from the changelog, so a draft whose block
  an undo restores is pending (and acceptable/rejectable) again.
- A corrupt state file silently forgot pending drafts. It now warns and
  recovers them from the marker blocks in the document.
- Hashes were keyed by absolute path, so a copied/moved project looked
  hand-edited. Keys are now relative to the main file's directory.
- A failed draft into a chapter left the main file's preamble modified. The
  draft is validated against its target before the preamble is prepared.
- Body drafts could be written into the preamble or after `\end{document}`.
  Inserts/comments are clamped to the body, replace/delete in the preamble is
  refused with a pointer to `draft-preamble`, and nothing lands after
  `\end{document}`.
- Mid-line replace/delete commented out the rest of the line in the review
  PDF. A draft block now always occupies whole lines; accept/reject still
  merge or restore exactly the intended span.
- `draft-replace` on text inside a pending insert duplicated the text. It is
  now refused (accept or reject the pending draft first).
- Nested `\input` paths were resolved relative to the including file. They
  are resolved relative to the main directory, like TeX.
- Per-change snapshots did not copy chapter files, and `restore_snapshot`
  skipped deleted chapters. Both are fixed.
- A compile after rejecting the last margin note died on stale
  `\pgfsyspdfmark` entries in the `.aux`; stale auxiliary data is dropped
  before compiling when the packages are no longer loaded.

## How to run them

```bash
python3 -m unittest discover -s tests                      # everything
python3 -m unittest discover -s tests/extended             # just these
```
