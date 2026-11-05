import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

from tools.latex_draft import (
    BLOCK_RE,
    build_block,
    ensure_changes_package,
    extract_command_content,
    list_snapshots,
    load_state,
    main,
    resolve_drafts,
    snapshot,
)


def _note_of(snap):
    note_file = snap.with_suffix(snap.suffix + ".note")
    return note_file.read_text().strip() if note_file.exists() else ""

PAPER = """\\documentclass{article}
\\title{A Small Paper}
\\begin{document}
\\section{Introduction}
Small problems are worth studying.
\\end{document}
"""


class LatexDraftCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.tex = self.dir / "paper.tex"
        self.tex.write_text(PAPER)
        (self.dir / ".latex-draft.cfg").write_text("[accept]\nkeep_markup = false\n")

    def run_cli(self, *argv):
        return main([str(self.tex), *argv])

    def state(self):
        return load_state(self.tex)

    def draft_ids(self):
        # Drafts in document order (the state file is saved with sort_keys,
        # so dict order is alphabetical by id, not creation order). After a
        # resolve, blocks are gone from the document, so fall back to
        # state-file order for resolved drafts and append them at the end.
        text = self.tex.read_text()
        doc_order = [m.group(1) for m in BLOCK_RE.finditer(text)]
        state = self.state()["drafts"]
        ordered = [d for d in doc_order if d in state]
        ordered += [d for d in state if d not in doc_order]
        return ordered

    def strip_block_markers(self, text: str) -> str:
        """Remove \\textsuperscript{N} block-number markers for assertions."""
        return re.sub(r"\\textsuperscript\{\d+\} ", "", text)


class TestChangesPackage(LatexDraftCase):
    def test_injects_package_after_documentclass(self):
        out = ensure_changes_package(PAPER)
        self.assertIn("\\usepackage[draft,authormarkup=brackets,authormarkupposition=right,deletedmarkup=sout]{changes}", out)
        self.assertLess(
            out.index("\\documentclass"), out.index("\\usepackage[draft,authormarkup=brackets,authormarkupposition=right,deletedmarkup=sout]{changes}")
        )

    def test_idempotent(self):
        once = ensure_changes_package(PAPER)
        self.assertEqual(ensure_changes_package(once), once)


class TestInsert(LatexDraftCase):
    def test_insert_after_anchor_marks_block(self):
        rc = self.run_cli(
            "draft-insert",
            "--after",
            "\\section{Introduction}",
            "--text",
            "New sentence.",
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        did = self.draft_ids()[0]
        self.assertIn(f"% latex-draft:{did}", text)
        self.assertIn(r"\added[id=AI]{New sentence.}", self.strip_block_markers(text))
        self.assertIn(f"% latex-draft-end:{did}", text)
        self.assertIn("\\usepackage[draft,authormarkup=brackets,authormarkupposition=right,deletedmarkup=sout]{changes}", text)

    def test_ambiguous_anchor_fails(self):
        self.tex.write_text(PAPER + "\\section{Introduction}\n")
        rc = self.run_cli(
            "draft-insert", "--after", "\\section{Introduction}", "--text", "x"
        )
        self.assertEqual(rc, 2)
        self.assertNotIn("latex-draft:", self.tex.read_text())

    def test_missing_file_fails(self):
        rc = main([str(self.dir / "nope.tex"), "list"])
        self.assertEqual(rc, 2)


class TestReplaceDelete(LatexDraftCase):
    def test_replace_marks_old_and_new(self):
        rc = self.run_cli(
            "draft-replace",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "Big problems are worth studying.",
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertIn(r"\deleted[id=AI]{Small problems are worth studying.}", self.strip_block_markers(text))
        self.assertIn(r"\added[id=AI]{Big problems are worth studying.}", self.strip_block_markers(text))

    def test_replace_requires_unique_old(self):
        rc = self.run_cli("draft-replace", "--old", "o", "--new", "x")
        self.assertEqual(rc, 2)

    def test_delete_marks_only_old(self):
        rc = self.run_cli("draft-delete", "--old", "Small problems are worth studying.")
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertIn(r"\deleted[id=AI]{Small problems are worth studying.}", self.strip_block_markers(text))
        self.assertNotIn(r"\added", text)


class TestComment(LatexDraftCase):
    def test_comment_inserts_footnote_without_touching_text(self):
        body_before = [
            l for l in self.tex.read_text().split("\n") if "latex-draft" not in l
        ]
        rc = self.run_cli(
            "draft-comment",
            "--after",
            "Small problems are worth studying.",
            "--text",
            "TODO: cite a source here.",
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertIn(r"\footnote{TODO: cite a source here.}", self.strip_block_markers(text))
        self.assertNotIn(r"\added", text)
        self.assertNotIn(r"\deleted[", self.strip_block_markers(text))
        did = self.draft_ids()[0]
        self.assertEqual(self.state()["drafts"][did]["op"], "comment")

    def test_accept_comment_keeps_footnote(self):
        self.run_cli(
            "draft-comment",
            "--after",
            "Small problems are worth studying.",
            "--text",
            "See related work.",
        )
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        text = self.tex.read_text()
        self.assertIn(r"\footnote{See related work.}", self.strip_block_markers(text))
        self.assertNotIn("latex-draft:", text)

    def test_reject_comment_removes_footnote(self):
        original = self.tex.read_text()
        self.run_cli(
            "draft-comment",
            "--after",
            "Small problems are worth studying.",
            "--text",
            "See related work.",
        )
        did = self.draft_ids()[0]
        self.run_cli("reject", "--id", did)
        self.assertEqual(self.tex.read_text(), original)

    def test_comment_ambiguous_anchor_fails(self):
        self.tex.write_text(PAPER + "Small problems are worth studying.\n")
        rc = self.run_cli(
            "draft-comment",
            "--after",
            "Small problems are worth studying.",
            "--text",
            "x",
        )
        self.assertEqual(rc, 2)


class TestResolve(LatexDraftCase):
    def test_accept_merges_without_markup(self):
        self.run_cli(
            "draft-replace",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "Big problems are worth studying.",
        )
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        text = self.tex.read_text()
        self.assertIn("Big problems are worth studying.", text)
        self.assertNotIn("Small problems are worth studying.", text)
        self.assertNotIn("latex-draft:", text)
        self.assertNotIn(r"\added", text)
        self.assertEqual(self.state()["drafts"][did]["status"], "accepted")

    def test_reject_restores_original(self):
        original = self.tex.read_text()
        self.run_cli("draft-delete", "--old", "Small problems are worth studying.")
        did = self.draft_ids()[0]
        self.run_cli("reject", "--id", did)
        self.assertEqual(self.tex.read_text(), original)
        self.assertEqual(self.state()["drafts"][did]["status"], "rejected")

    def test_accept_all(self):
        self.run_cli("draft-insert", "--at-line", "1", "--text", "one.")
        self.run_cli("draft-insert", "--at-line", "2", "--text", "two.")
        rc = self.run_cli("accept", "--all")
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertIn("one.", text)
        self.assertIn("two.", text)
        self.assertEqual(text.count("% latex-draft:"), 0)

    def test_accept_unknown_id_fails(self):
        self.assertEqual(self.run_cli("accept", "--id", "deadbeef"), 2)

    def test_overlapping_alternatives_resolve_together(self):
        # two alternative drafts for the same replacement: accepting one
        # consumes both blocks; the sibling is resolved as rejected
        rc = self.run_cli(
            "draft-replace",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "Version A.",
        )
        self.assertEqual(rc, 0)
        id_a = self.draft_ids()[0]
        rc = self.run_cli(
            "draft-replace",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "Version B.",
        )
        self.assertEqual(rc, 0, "drafting an alternative over a pending draft must work")
        id_b = [d for d in self.draft_ids() if d != id_a][0]
        rc = self.run_cli("accept", "--id", id_a)
        self.assertEqual(rc, 0)
        self.assertIn("Version A.", self.tex.read_text())
        self.assertNotIn("Version B.", self.tex.read_text())
        self.assertEqual(self.state()["drafts"][id_a]["status"], "accepted")
        self.assertEqual(self.state()["drafts"][id_b]["status"], "rejected")
        # rejecting the sibling afterwards is a clean error (already resolved)
        self.assertEqual(self.run_cli("reject", "--id", id_b), 2)

    def test_draft_over_pending_block_refused(self):
        self.run_cli(
            "draft-replace",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "Version A.",
        )
        # inserting into the middle of a pending draft block is refused
        rc = self.run_cli(
            "draft-insert",
            "--after",
            "Small problems are worth studying.",
            "--text",
            "Side note.",
        )
        self.assertEqual(rc, 2)

    def test_replace_old_must_be_unique_outside_drafts(self):
        rc = self.run_cli("draft-replace", "--old", "o", "--new", "x")
        self.assertEqual(rc, 2)

    def test_double_resolve_fails(self):
        self.run_cli("draft-insert", "--at-line", "1", "--text", "one.")
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        self.assertEqual(self.run_cli("accept", "--id", did), 2)

    def test_nested_braces_in_content(self):
        self.run_cli(
            "draft-insert",
            "--at-line",
            "1",
            "--text",
            r"Text with \textbf{bold {nested}} braces.",
        )
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        self.assertIn(r"Text with \textbf{bold {nested}} braces.", self.tex.read_text())


class TestBlockParsing(unittest.TestCase):
    def test_extract_command_content(self):
        block = build_block("abcd1234", old="old {x}", new="new {y}")
        self.assertEqual(extract_command_content(block, "added"), "new {y}")
        self.assertEqual(extract_command_content(block, "deleted"), "old {x}")
        self.assertIsNone(extract_command_content(block.replace("\\added", "\\zzz"), "added") or None)

    def test_resolve_roundtrip(self):
        block = build_block("abcd1234", new="hello")
        text = "A\n" + block + "\nB"
        out, found, spliced = resolve_drafts(text, {"abcd1234"}, accept=True)
        self.assertEqual(out, "A\nhello\nB")
        self.assertEqual(found, {"abcd1234"})
        self.assertEqual(spliced, set())  # pure insert: no deleted span

    def test_block_re_matches(self):
        block = build_block("abcd1234", new="x")
        m = BLOCK_RE.search(block)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "abcd1234")


class TestBackups(LatexDraftCase):
    def test_every_mutation_snapshots(self):
        self.run_cli("draft-insert", "--at-line", "1", "--text", "one.")
        self.run_cli("draft-insert", "--at-line", "1", "--text", "two.")
        # 1 full initial backup + 1 per mutation (first draft = draft + setup)
        self.assertEqual(len(list_snapshots(self.tex)), 4)

    def test_compile_takes_per_turn_backup(self):
        self.run_cli("status")  # first use: full initial backup
        before = len(list_snapshots(self.tex))
        self.run_cli("compile")
        self.assertEqual(len(list_snapshots(self.tex)), before + 1)
        self.assertEqual(self.state()["turn"], 1)
        self.run_cli("compile")
        self.assertEqual(len(list_snapshots(self.tex)), before + 2)
        self.assertEqual(self.state()["turn"], 2)

    def test_full_initial_backup_includes_inputs(self):
        chapter = self.dir / "chapter1.tex"
        chapter.write_text("\\section{One}\n")
        self.tex.write_text(PAPER.replace("\\end{document}", "\\input{chapter1}\n\\end{document}"))
        self.run_cli("list")  # first use triggers the full initial backup
        snaps = list_snapshots(self.tex)
        self.assertEqual(len(snaps), 1)  # exactly the full initial backup
        deps = list((self.dir / ".latex-draft-backups" / self.tex.name).glob(f"{snaps[0].stem}__*.tex"))
        self.assertEqual([d.name.split("__", 1)[1] for d in deps], ["chapter1.tex"])

    def test_undo_restores(self):
        original = self.tex.read_text()
        self.run_cli("draft-insert", "--at-line", "1", "--text", "one.")
        self.run_cli("draft-insert", "--at-line", "1", "--text", "two.")
        rc = self.run_cli("undo")
        self.assertEqual(rc, 0)
        self.assertNotIn("two.", self.tex.read_text())
        self.assertIn("one.", self.tex.read_text())
        self.run_cli("undo", "--n", "5")
        self.assertEqual(self.tex.read_text(), original)

    def test_undo_without_changes_fails(self):
        self.assertEqual(self.run_cli("undo"), 1)

    def test_manual_backup_with_note(self):
        rc = self.run_cli("backup", "--note", "before big edit")
        self.assertEqual(rc, 0)
        all_snaps = list_snapshots(self.tex)
        self.assertEqual(len(all_snaps), 2)  # full initial + manual
        self.assertEqual(_note_of(all_snaps[0]), "before big edit")
        self.assertIn("initial backup", _note_of(all_snaps[1]))


class TestChangelog(LatexDraftCase):
    def test_changes_are_recorded(self):
        from tools.latex_draft import read_changelog

        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.run_cli("accept", "--all")
        entries = [e for e in read_changelog(self.tex) if e["kind"] in ("change", "setup")]
        # draft insert, profile setup, accept, and the preamble strip
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0]["turn"], 0)
        self.assertIn("draft insert", entries[0]["note"])
        self.assertIn("strip", entries[3]["note"])

    def test_undo_turn_reverts_whole_turn(self):
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.run_cli("draft-insert", "--at-line", "5", "--text", "two.")
        self.run_cli("compile")  # ends turn 1
        self.assertEqual(self.state()["turn"], 1)
        self.run_cli("draft-insert", "--at-line", "5", "--text", "three.")
        rc = self.run_cli("undo", "--turn")
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertNotIn("three.", text)
        self.assertIn("one.", text)
        self.assertIn("two.", text)

    def test_undo_turn_without_changes(self):
        # no changes recorded at all -> error
        self.assertEqual(self.run_cli("undo", "--turn"), 1)

    def test_undo_turn_after_compile_reverts_last_active_turn(self):
        # changes in turn 0, compile ends the turn; `undo --turn` should still
        # revert that turn (the common "undo my last turn" case)
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.run_cli("compile")
        self.assertEqual(self.state()["turn"], 1)
        rc = self.run_cli("undo", "--turn")
        self.assertEqual(rc, 0)
        self.assertNotIn("one.", self.tex.read_text())

    def test_changelog_command_lists_entries(self):
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.assertEqual(self.run_cli("changelog"), 0)

    def test_undo_records_undo_entry(self):
        from tools.latex_draft import read_changelog

        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.run_cli("undo")
        kinds = [e["kind"] for e in read_changelog(self.tex)]
        self.assertEqual(kinds, ["change", "setup", "undo"])


class TestExternalEdits(LatexDraftCase):
    def test_hand_edit_detected_and_adopted_at_turn_start(self):
        from tools.latex_draft import read_changelog

        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        # simulate the user editing the file by hand between turns
        text = self.tex.read_text()
        self.tex.write_text(text.replace("worth studying", "worth studying deeply"))
        self.run_cli("status")  # turn start: should warn and adopt
        entries = [e for e in read_changelog(self.tex) if e["kind"] == "external"]
        self.assertEqual(len(entries), 1)
        self.assertIn("changed outside latex-draft", entries[0]["note"])
        # after adoption, a new change proceeds without aborting
        rc = self.run_cli("draft-insert", "--at-line", "5", "--text", "two.")
        self.assertEqual(rc, 0)
        self.assertIn("worth studying deeply", self.tex.read_text())

    def test_hand_edit_during_mutation_aborts(self):
        from tools import latex_draft

        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        original = self.tex.read_text()

        def sneaky_fn(text):
            # simulate a hand edit landing while the change is computed
            self.tex.write_text(text + "\n% hand edit\n")
            return text + "\n% drafted by tool\n"

        state = self.state()
        with self.assertRaises(latex_draft.DraftError):
            latex_draft.mutate(self.tex, state, "test", sneaky_fn)
        # file content is whatever the hand edit left; tool did not clobber
        self.assertIn("hand edit", self.tex.read_text())
        self.assertNotEqual(self.tex.read_text(), original + "\n% drafted by tool\n")

    def test_hand_edit_of_input_file_detected(self):
        chapter = self.dir / "chapter1.tex"
        chapter.write_text("\\section{One}\n")
        self.tex.write_text(PAPER.replace("\\end{document}", "\\input{chapter1}\n\\end{document}"))
        self.run_cli("status")  # establishes hashes
        chapter.write_text("\\section{One, edited by hand}\n")
        from tools.latex_draft import check_external_changes

        state = self.state()
        warning = check_external_changes(self.tex, state)
        self.assertIsNotNone(warning)
        self.assertIn("chapter1.tex", warning)


class TestMultiFile(LatexDraftCase):
    """Multi-file documents: paper.tex stitched together from chapters."""

    def setUp(self):
        super().setUp()
        self.chapter = self.dir / "chapter1.tex"
        self.chapter.write_text("\\section{One}\nChapter body text.\n")
        self.tex.write_text(
            PAPER.replace(
                "\\end{document}", "\\input{chapter1}\n\\end{document}"
            )
        )

    def run_cli(self, *argv):
        return main([str(self.tex), *argv])

    def test_status_lists_inputs(self):
        self.run_cli("status")
        # smoke: no crash, chapter is an input
        from tools.latex_draft import tex_inputs

        self.assertEqual([d.name for d in tex_inputs(self.tex)], ["chapter1.tex"])

    def test_draft_insert_into_chapter(self):
        rc = self.run_cli(
            "draft-insert",
            "--file",
            "chapter1.tex",
            "--after",
            "Chapter body text.",
            "--text",
            "Drafted chapter sentence.",
        )
        self.assertEqual(rc, 0)
        chapter = self.chapter.read_text()
        main_tex = self.tex.read_text()
        did = self.draft_ids()[0]
        self.assertIn(f"% latex-draft:{did}", chapter)
        self.assertIn(r"\added[id=AI]{Drafted chapter sentence.}", self.strip_block_markers(chapter))
        # the main file gets the changes preamble (included files cannot
        # carry it) but no draft markers of its own
        self.assertNotIn("latex-draft:", main_tex)
        self.assertIn("{changes}", main_tex)
        self.assertRegex(main_tex, r"\\usepackage\[draft[,\]]")
        self.assertEqual(self.state()["drafts"][did]["file"], "chapter1.tex")

    def test_draft_replace_in_chapter(self):
        rc = self.run_cli(
            "draft-replace",
            "--file",
            "chapter1.tex",
            "--old",
            "Chapter body text.",
            "--new",
            "Rewritten chapter body.",
        )
        self.assertEqual(rc, 0)
        self.assertIn(r"\deleted[id=AI]{Chapter body text.}", self.strip_block_markers(self.chapter.read_text()))
        self.assertIn(r"\added[id=AI]{Rewritten chapter body.}", self.strip_block_markers(self.chapter.read_text()))

    def test_accept_draft_in_chapter(self):
        self.run_cli(
            "draft-replace",
            "--file",
            "chapter1.tex",
            "--old",
            "Chapter body text.",
            "--new",
            "Rewritten chapter body.",
        )
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        chapter = self.chapter.read_text()
        self.assertIn("Rewritten chapter body.", chapter)
        self.assertNotIn("latex-draft:", chapter)
        self.assertNotIn(r"\added", chapter)

    def test_reject_draft_in_chapter_restores(self):
        original = self.chapter.read_text()
        self.run_cli(
            "draft-delete", "--file", "chapter1.tex", "--old", "Chapter body text."
        )
        did = self.draft_ids()[0]
        self.run_cli("reject", "--id", did)
        self.assertEqual(self.chapter.read_text(), original)

    def test_draft_comment_in_chapter(self):
        rc = self.run_cli(
            "draft-comment",
            "--file",
            "chapter1.tex",
            "--after",
            "Chapter body text.",
            "--text",
            "TODO: expand.",
        )
        self.assertEqual(rc, 0)
        self.assertIn(r"\footnote{TODO: expand.}", self.strip_block_markers(self.chapter.read_text()))

    def test_file_must_be_an_input(self):
        stranger = self.dir / "stranger.tex"
        stranger.write_text("not part of the document\n")
        rc = self.run_cli(
            "draft-insert",
            "--file",
            "stranger.tex",
            "--at-line",
            "1",
            "--text",
            "x.",
        )
        self.assertEqual(rc, 2)
        self.assertEqual(stranger.read_text(), "not part of the document\n")

    def test_file_missing_fails(self):
        rc = self.run_cli(
            "draft-insert", "--file", "nope.tex", "--at-line", "1", "--text", "x."
        )
        self.assertEqual(rc, 2)

    def test_anchor_searched_only_within_target_file(self):
        # the anchor exists only in the chapter: anchoring without --file
        # (main file) must fail, anchoring with --file must succeed
        rc = self.run_cli("draft-insert", "--after", "Chapter body text.", "--text", "x.")
        self.assertEqual(rc, 2)
        rc = self.run_cli(
            "draft-insert",
            "--file",
            "chapter1.tex",
            "--after",
            "Chapter body text.",
            "--text",
            "x.",
        )
        self.assertEqual(rc, 0)

    def test_anchor_must_be_unique_within_its_file(self):
        # the same line twice in the chapter makes the anchor ambiguous there
        self.chapter.write_text("\\section{One}\nSame line.\nSame line.\n")
        rc = self.run_cli(
            "draft-insert",
            "--file",
            "chapter1.tex",
            "--after",
            "Same line.",
            "--text",
            "x.",
        )
        self.assertEqual(rc, 2)

    def test_changelog_records_file(self):
        from tools.latex_draft import read_changelog

        self.run_cli(
            "draft-insert",
            "--file",
            "chapter1.tex",
            "--at-line",
            "1",
            "--text",
            "x.",
        )
        entries = [e for e in read_changelog(self.tex) if e["kind"] == "change"]
        self.assertEqual(entries[-1]["file"], "chapter1.tex")

    def test_undo_restores_change_in_chapter(self):
        original = self.chapter.read_text()
        self.run_cli(
            "draft-insert",
            "--file",
            "chapter1.tex",
            "--at-line",
            "1",
            "--text",
            "x.",
        )
        self.assertNotEqual(self.chapter.read_text(), original)
        self.run_cli("undo", "--n", "2")  # profile setup + chapter draft
        self.assertEqual(self.chapter.read_text(), original)

    def test_undo_turn_spans_multiple_files(self):
        main_original = self.tex.read_text()
        chapter_original = self.chapter.read_text()
        self.run_cli("draft-insert", "--at-line", "5", "--text", "in main.")
        self.run_cli(
            "draft-insert", "--file", "chapter1.tex", "--at-line", "1", "--text", "in chapter."
        )
        self.run_cli("compile")  # ends the turn
        self.run_cli("undo", "--turn")
        self.assertEqual(self.tex.read_text(), main_original)
        self.assertEqual(self.chapter.read_text(), chapter_original)

    def test_resolve_across_files_in_one_accept_all(self):
        self.run_cli("draft-insert", "--at-line", "5", "--text", "in main.")
        self.run_cli(
            "draft-insert", "--file", "chapter1.tex", "--at-line", "1", "--text", "in chapter."
        )
        rc = self.run_cli("accept", "--all")
        self.assertEqual(rc, 0)
        self.assertIn("in main.", self.tex.read_text())
        self.assertIn("in chapter.", self.chapter.read_text())
        self.assertNotIn("latex-draft:", self.tex.read_text())
        self.assertNotIn("latex-draft:", self.chapter.read_text())

    def test_changes_preamble_stripped_only_from_main(self):
        # after resolving everything, the main file loses the injected
        # preamble even if a chapter draft remains pending
        self.run_cli("draft-insert", "--at-line", "5", "--text", "in main.")
        self.run_cli(
            "draft-insert", "--file", "chapter1.tex", "--at-line", "1", "--text", "x."
        )
        self.run_cli("accept", "--all")
        self.assertNotIn("usepackage[draft]{changes}", self.tex.read_text())


class TestNestedFiles(LatexDraftCase):
    """Nested documents: paper.tex -> chapter1.tex -> sub.tex."""

    def setUp(self):
        super().setUp()
        self.sub = self.dir / "sub.tex"
        self.sub.write_text("\\section{Sub}\nSub body.\n")
        self.chapter = self.dir / "chapter1.tex"
        self.chapter.write_text(
            "\\section{One}\nChapter body.\n\\input{sub}\n"
        )
        self.tex.write_text(
            PAPER.replace(
                "\\end{document}", "\\input{chapter1}\n\\end{document}"
            )
        )

    def run_cli(self, *argv):
        return main([str(self.tex), *argv])

    def test_inputs_discovered_transitively(self):
        from tools.latex_draft import tex_inputs

        self.assertEqual(
            [d.name for d in tex_inputs(self.tex)], ["chapter1.tex", "sub.tex"]
        )

    def test_status_lists_nested_inputs(self):
        self.run_cli("status")
        from tools.latex_draft import tex_inputs

        self.assertIn("sub.tex", [d.name for d in tex_inputs(self.tex)])

    def test_full_backup_includes_nested_inputs(self):
        self.run_cli("list")  # first use triggers the full initial backup
        snaps = list_snapshots(self.tex)
        self.assertEqual(len(snaps), 1)
        bdir = self.dir / ".latex-draft-backups" / self.tex.name
        deps = sorted(
            d.name.split("__", 1)[1] for d in bdir.glob(f"{snaps[0].stem}__*.tex")
        )
        self.assertEqual(deps, ["chapter1.tex", "sub.tex"])

    def test_draft_into_nested_file(self):
        rc = self.run_cli(
            "draft-insert",
            "--file",
            "sub.tex",
            "--after",
            "Sub body.",
            "--text",
            "Nested draft.",
        )
        self.assertEqual(rc, 0)
        sub = self.sub.read_text()
        self.assertIn("latex-draft:", sub)
        self.assertIn(r"\added[id=AI]{Nested draft.}", self.strip_block_markers(sub))
        did = self.draft_ids()[0]
        self.assertEqual(self.state()["drafts"][did]["file"], "sub.tex")
        # intermediate and main files untouched
        self.assertNotIn("latex-draft:", self.chapter.read_text())
        self.assertNotIn("latex-draft:", self.tex.read_text())

    def test_accept_and_undo_in_nested_file(self):
        original = self.sub.read_text()
        self.run_cli(
            "draft-replace",
            "--file",
            "sub.tex",
            "--old",
            "Sub body.",
            "--new",
            "Rewritten sub body.",
        )
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        self.assertIn("Rewritten sub body.", self.sub.read_text())
        # undo the preamble strip, the accept, the profile setup, and the draft
        self.run_cli("undo", "--n", "4")
        self.assertEqual(self.sub.read_text(), original)

    def test_hand_edit_of_nested_file_detected(self):
        self.run_cli("status")  # establish hashes
        self.sub.write_text("\\section{Sub}\nSub body, hand-edited.\n")
        from tools.latex_draft import check_external_changes

        warning = check_external_changes(self.tex, self.state())
        self.assertIsNotNone(warning)
        self.assertIn("sub.tex", warning)

    def test_cycle_tolerated(self):
        # chapter1 inputs sub, sub inputs chapter1: no infinite loop
        self.sub.write_text("\\section{Sub}\nSub body.\n\\input{chapter1}\n")
        from tools.latex_draft import tex_inputs

        names = [d.name for d in tex_inputs(self.tex)]
        self.assertEqual(sorted(names), ["chapter1.tex", "sub.tex"])

    def test_changelog_attributes_nested_file(self):
        from tools.latex_draft import read_changelog

        self.run_cli(
            "draft-insert",
            "--file",
            "sub.tex",
            "--at-line",
            "1",
            "--text",
            "x.",
        )
        entries = [e for e in read_changelog(self.tex) if e["kind"] == "change"]
        self.assertEqual(entries[-1]["file"], "sub.tex")


class TestAuthorLabeling(LatexDraftCase):
    def test_alternatives_get_distinct_authors_and_colors(self):
        self.run_cli(
            "draft-replace",
            "--author",
            "A",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "Version A.",
        )
        self.run_cli(
            "draft-replace",
            "--author",
            "B",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "Version B.",
        )
        text = self.tex.read_text()
        self.assertIn(r"\added[id=A]{Version A.}", self.strip_block_markers(text))
        self.assertIn(r"\added[id=B]{Version B.}", self.strip_block_markers(text))
        self.assertIn(r"\definechangesauthor[color=blue,name=A]{A}", text)
        self.assertIn(r"\definechangesauthor[color=orange,name=B]{B}", text)
        # draft records carry the author
        self.assertEqual(
            sorted(rec["author"] for rec in self.state()["drafts"].values()),
            ["A", "B"],
        )

    def test_ensure_changes_package_idempotent_per_author(self):
        from tools.latex_draft import ensure_changes_package

        once = ensure_changes_package(PAPER, "A")
        twice = ensure_changes_package(once, "A")
        self.assertEqual(once, twice)
        with_b = ensure_changes_package(once, "B")
        self.assertIn(r"\definechangesauthor[color=orange,name=B]{B}", with_b)

    def test_strip_removes_all_author_lines(self):
        from tools.latex_draft import ensure_changes_package, strip_changes_package_if_unused

        text = ensure_changes_package(PAPER, "A")
        text = ensure_changes_package(text, "B")
        stripped = strip_changes_package_if_unused(text)
        self.assertNotIn("definechangesauthor", stripped)
        self.assertNotIn("usepackage[draft]{changes}", stripped)
        self.assertEqual(stripped, PAPER)


class TestMarginComments(LatexDraftCase):
    def test_margin_comment_uses_todo(self):
        rc = self.run_cli(
            "draft-comment",
            "--margin",
            "--after",
            "Small problems are worth studying.",
            "--text",
            "Check this.",
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertIn(r"\todo{Check this.}", self.strip_block_markers(text))
        self.assertIn(r"\usepackage{todonotes}", text)
        self.assertNotIn(r"\footnote", text)

    def test_accept_margin_comment_keeps_todo(self):
        self.run_cli(
            "draft-comment",
            "--margin",
            "--after",
            "Small problems are worth studying.",
            "--text",
            "Check this.",
        )
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        self.assertIn(r"\todo{Check this.}", self.strip_block_markers(self.tex.read_text()))
        self.assertNotIn("latex-draft:", self.tex.read_text())

    def test_reject_margin_comment_removes_it(self):
        original = self.tex.read_text()
        self.run_cli(
            "draft-comment",
            "--margin",
            "--after",
            "Small problems are worth studying.",
            "--text",
            "Check this.",
        )
        did = self.draft_ids()[0]
        self.run_cli("reject", "--id", did)
        self.assertEqual(self.tex.read_text(), original)


class TestUndoConflict(LatexDraftCase):
    def test_undo_refuses_on_conflict(self):
        # a hand edit (adopted at the next command start) changes the file
        # after a change was recorded
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.tex.write_text(self.tex.read_text() + "\n% hand edit\n")
        self.run_cli("status")  # turn start: adopts the hand edit
        # undoing the recorded change now conflicts: the file no longer
        # matches its recorded after-state -> refuse instead of clobbering
        rc = self.run_cli("undo")
        self.assertEqual(rc, 1)
        self.assertIn("% hand edit", self.tex.read_text())
        # --force restores the recorded content anyway
        rc = self.run_cli("undo", "--force")
        self.assertEqual(rc, 0)
        self.assertNotIn("% hand edit", self.tex.read_text())

    def test_undo_back_to_back_reverts_all(self):
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.run_cli("draft-insert", "--at-line", "5", "--text", "two.")
        original = PAPER
        rc = self.run_cli("undo", "--n", "3")  # 2 drafts + 1 profile setup
        self.assertEqual(rc, 0)
        self.assertEqual(self.tex.read_text(), original)

    def test_undo_force_restores_anyway(self):
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.run_cli("draft-insert", "--at-line", "5", "--text", "two.")
        rc = self.run_cli("undo", "--n", "3", "--force")  # 2 drafts + 1 profile setup
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        # both changes reverted, original restored
        self.assertNotIn("one.", text)
        self.assertNotIn("two.", text)

    def test_undo_all_conflicting_fails(self):
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        # hand edit after the change (adopted at next command start)
        self.tex.write_text(self.tex.read_text() + "\n% hand edit\n")
        self.run_cli("status")
        rc = self.run_cli("undo")
        self.assertEqual(rc, 1)


class TestCheck(LatexDraftCase):
    def test_check_ok_when_clean(self):
        self.assertEqual(self.run_cli("check"), 0)

    def test_check_fails_with_pending_draft(self):
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.assertEqual(self.run_cli("check"), 1)

    def test_check_fails_with_stray_marker(self):
        self.tex.write_text(self.tex.read_text() + "\n% latex-draft:deadbeef\n")
        self.assertEqual(self.run_cli("check"), 1)

    def test_check_fails_with_leftover_preamble(self):
        # simulate a leftover injected preamble with no pending drafts
        from tools.latex_draft import ensure_changes_package

        self.tex.write_text(ensure_changes_package(PAPER))
        self.assertEqual(self.run_cli("check"), 1)

    def test_check_ok_after_resolving_all(self):
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.run_cli("accept", "--all")
        self.assertEqual(self.run_cli("check"), 0)


class TestExtendedInputs(LatexDraftCase):
    def test_subfile_discovered(self):
        sub = self.dir / "sub.tex"
        sub.write_text("\\section{Sub}\n")
        self.tex.write_text(PAPER.replace("\\end{document}", "\\subfile{sub}\n\\end{document}"))
        from tools.latex_draft import tex_inputs

        self.assertEqual([d.name for d in tex_inputs(self.tex)], ["sub.tex"])
        rc = self.run_cli(
            "draft-insert", "--file", "sub.tex", "--at-line", "1", "--text", "x."
        )
        self.assertEqual(rc, 0)
        self.assertIn("latex-draft:", sub.read_text())

    def test_import_discovered(self):
        chapter_dir = self.dir / "chapters"
        chapter_dir.mkdir()
        ch = chapter_dir / "one.tex"
        ch.write_text("\\section{One}\n")
        self.tex.write_text(
            PAPER.replace("\\end{document}", "\\import{chapters/}{one}\n\\end{document}")
        )
        from tools.latex_draft import tex_inputs

        self.assertEqual([d.name for d in tex_inputs(self.tex)], ["one.tex"])


class TestCompileAndStatus(LatexDraftCase):
    def test_compile_without_engine_warns_but_succeeds(self):
        # latexmk/pdflatex are not installed in this environment
        rc = self.run_cli("compile")
        self.assertEqual(rc, 0)

    def test_status(self):
        self.run_cli("draft-insert", "--at-line", "1", "--text", "one.")
        rc = self.run_cli("status")
        self.assertEqual(rc, 0)

    def test_list_empty(self):
        self.assertEqual(self.run_cli("list"), 0)


SAMPLE_LOG = """This is pdfTeX, Version 3.14159265
\\openout1 = `paper.log'.
(./paper.tex
! Undefined control sequence.
l.6 \\badcommand
                  {x}
! LaTeX Error: Environment figuree undefined.
! LaTeX Error: File `missing-fig.pdf' not found.
! LaTeX Error: \\begin{document} ended by \\end{documnt}.
! Emergency stop.
"""

MISMATCH_LOG = """This is pdfTeX
! LaTeX Error: \\begin{document} on input line 4 ended by \\end{documnt}.
! Emergency stop.
"""


class TestRepair(LatexDraftCase):
    def write_log(self, content):
        (self.dir / (self.tex.stem + ".log")).write_text(content)

    def test_analyze_log_extracts_known_errors(self):
        from tools.latex_draft import analyze_log

        self.write_log(SAMPLE_LOG)
        problems = analyze_log(self.tex)
        kinds = [p["kind"] for p in problems]
        self.assertIn("undefined-control", kinds)
        self.assertIn("undefined-environment", kinds)
        self.assertIn("missing-file", kinds)
        self.assertIn("begin-end-mismatch", kinds)
        uc = next(p for p in problems if p["kind"] == "undefined-control")
        self.assertEqual(uc["line"], 6)

    def test_undefined_control_takes_last_command_on_error_line(self):
        # regression: TeX truncates the log line at the error point and the
        # undefined control is "at the end of the top line", i.e. the LAST
        # \cmd on the line, not the first (\section{...}\boguscmd)
        from tools.latex_draft import analyze_log

        self.write_log(
            "! Undefined control sequence.\n"
            "l.1 " + "\\" + "section{Conclusion}" + "\\" + "boguscmd\n"
            "                                 {\n"
            "The control sequence at the end of the top line\n"
        )
        problems = analyze_log(self.tex)
        uc = [p for p in problems if p["kind"] == "undefined-control"]
        self.assertEqual(len(uc), 1)
        self.assertEqual(uc[0]["command"], "boguscmd")
        self.assertEqual(uc[0]["line"], 1)

    def test_analyze_log_empty_when_no_log(self):
        from tools.latex_draft import analyze_log

        self.assertEqual(analyze_log(self.tex), [])

    def test_repair_fixes_begin_end_mismatch_directly(self):
        self.tex.write_text(PAPER.replace("\\end{document}", "\\end{documnt}"))
        self.write_log(MISMATCH_LOG)
        # no LaTeX engine here, so run_latex inside repair returns 0
        rc = self.run_cli("repair")
        self.assertEqual(rc, 0)
        # the fix patches the source directly (a repair must compile on its
        # own, without the changes package), and is changelogged (undoable)
        text = self.tex.read_text()
        self.assertNotIn("documnt", text)
        self.assertIn("\\end{document}", text)
        self.assertNotIn("latex-draft:", text)
        self.assertEqual(self.draft_ids(), [])
        # the repair is reversible via the changelog
        self.run_cli("undo", "--n", "1")
        self.assertIn("documnt", self.tex.read_text())

    def test_repair_suggests_for_unfixable_errors(self):
        self.write_log(SAMPLE_LOG)
        rc = self.run_cli("repair")
        # undefined-control of an unknown command is not auto-fixed
        self.assertEqual(rc, 1)
        self.assertEqual(self.draft_ids(), [])

    def test_repair_compiles_clean_says_nothing_to_do(self):
        # no log + no engine -> run_latex returns 0 -> "nothing to repair"
        rc = self.run_cli("repair")
        self.assertEqual(rc, 0)

    def test_repair_records_changes_in_changelog(self):
        from tools.latex_draft import read_changelog

        self.tex.write_text(PAPER.replace("\\end{document}", "\\end{documnt}"))
        self.write_log(MISMATCH_LOG)
        self.run_cli("repair")
        entries = [e for e in read_changelog(self.tex) if e["kind"] in ("change", "setup")]
        self.assertTrue(any("repair" in e["note"] for e in entries))


class TestPrune(LatexDraftCase):
    def test_prune_retention(self):
        from tools.latex_draft import backups_dir, prune

        bdir = backups_dir(self.tex)
        bdir.mkdir(parents=True)
        now = time.time()
        # 2 recent, 2 in last day (different hours), 2 old (different days)
        stamps = [now - 60, now - 120, now - 7200, now - 10800,
                  now - 90000, now - 200000]
        for i, t in enumerate(stamps):
            name = time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"-{i:06d}.tex"
            (bdir / name).write_text("x")
        prune(self.tex)
        remaining = list_snapshots(self.tex)
        # 2 recent + 2 hourly + 2 daily = 6
        self.assertEqual(len(remaining), 6)


class TestSiblingGroupResolution(LatexDraftCase):
    """Regression tests for the sibling-duplication bug: rejecting or
    accepting a group of alternatives over the same --old must splice the
    result exactly once, not once per sibling."""

    def _draft_three_alternatives(self):
        self.run_cli(
            "draft-replace", "--author", "A",
            "--old", "Small problems are worth studying.",
            "--new", "Small problems are worth studying. Variant A.",
        )
        self.run_cli(
            "draft-replace", "--author", "B",
            "--old", "Small problems are worth studying.",
            "--new", "Small problems are worth studying. Variant B.",
        )
        self.run_cli(
            "draft-replace", "--author", "C",
            "--old", "Small problems are worth studying.",
            "--new", "Small problems are worth studying. Variant C.",
        )

    def test_reject_all_siblings_restores_once(self):
        original = self.tex.read_text()
        self._draft_three_alternatives()
        rc = self.run_cli("reject", "--all")
        self.assertEqual(rc, 0)
        # byte-identical to the pre-draft state
        self.assertEqual(self.tex.read_text(), original)

    def test_accept_one_sibling_merges_once(self):
        self._draft_three_alternatives()
        id_a = self.draft_ids()[0]
        rc = self.run_cli("accept", "--id", id_a)
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        # the accepted version appears exactly once
        self.assertEqual(text.count("Variant A."), 1)
        # the rejected siblings' versions are gone
        self.assertNotIn("Variant B.", text)
        self.assertNotIn("Variant C.", text)
        # the original text is gone (replaced, not duplicated)
        self.assertNotIn("Small problems are worth studying.\n", text)
        # siblings are marked rejected (orphaned): every draft except the
        # accepted one
        for did in self.state()["drafts"]:
            if did == id_a:
                continue
            self.assertEqual(self.state()["drafts"][did]["status"], "rejected")
            self.assertTrue(self.state()["drafts"][did].get("orphaned_by_overlap"))

    def test_reject_all_at_eof_preserves_end_document(self):
        # the draft block at EOF must not glue its end marker to \end{document}
        self.tex.write_text(
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "Final sentence.\n"
            "\\end{document}\n"
        )
        original = self.tex.read_text()
        self.run_cli(
            "draft-replace", "--author", "A",
            "--old", "Final sentence.",
            "--new", "Final sentence. Variant A.",
        )
        self.run_cli(
            "draft-replace", "--author", "B",
            "--old", "Final sentence.",
            "--new", "Final sentence. Variant B.",
        )
        # the end marker must be on its own line, not glued to \end{document}
        drafted = self.tex.read_text()
        self.assertIn("\n% latex-draft-end:", drafted)  # end marker on its own line
        rc = self.run_cli("reject", "--all")
        self.assertEqual(rc, 0)
        self.assertEqual(self.tex.read_text(), original)

    def test_two_independent_replaces_both_resolve(self):
        # two unrelated drafts (different --old) must both resolve
        self.run_cli(
            "draft-replace",
            "--old", "Small problems are worth studying.",
            "--new", "Replaced one.",
        )
        self.run_cli(
            "draft-insert",
            "--at-line", "1",
            "--text", "Inserted.",
        )
        rc = self.run_cli("accept", "--all")
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertIn("Replaced one.", text)
        self.assertIn("Inserted.", text)


class TestMultiParagraphDrafts(LatexDraftCase):
    r"""The changes package cannot take a blank line inside \added/\deleted
    ("Paragraph ended before \addcontentsline was complete"). Multi-paragraph
    drafts are split into one command per paragraph and reassembled on
    accept/reject."""

    def test_insert_multi_paragraph_block(self):
        from tools.latex_draft import build_block

        block = build_block("aaaaaaaa", new="Para one.\n\nPara two.", author="AI")
        self.assertIn(r"\added[id=AI]{Para one.}", self.strip_block_markers(block))
        self.assertIn(r"\added[id=AI]{Para two.}", self.strip_block_markers(block))

    def test_replace_multi_paragraph_roundtrip(self):
        self.run_cli(
            "draft-replace",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "Para one.\n\nPara two.",
        )
        did = self.draft_ids()[0]
        text = self.tex.read_text()
        # two \added commands, one per paragraph
        self.assertEqual(self.strip_block_markers(text).count(r"\added[id=AI]{"), 2)
        # accept reassembles the paragraphs with a blank line
        self.run_cli("accept", "--id", did)
        text = self.tex.read_text()
        self.assertIn("Para one.\n\nPara two.", text)
        self.assertNotIn("latex-draft:", text)

    def test_reject_multi_paragraph_restores_original(self):
        original = self.tex.read_text()
        self.run_cli(
            "draft-replace",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "Para one.\n\nPara two.",
        )
        did = self.draft_ids()[0]
        self.run_cli("reject", "--id", did)
        self.assertEqual(self.tex.read_text(), original)

    def test_delete_multi_paragraph(self):
        self.tex.write_text(
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "Delete para one.\n"
            "\n"
            "Delete para two.\n"
            "Keep this.\n"
            "\\end{document}\n"
        )
        rc = self.run_cli(
            "draft-delete",
            "--old",
            "Delete para one.\n\nDelete para two.",
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertEqual(self.strip_block_markers(text).count(r"\deleted[id=AI]{"), 2)
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        text = self.tex.read_text()
        self.assertNotIn("Delete para one.", text)
        self.assertNotIn("Delete para two.", text)
        self.assertIn("Keep this.", text)

    def test_extract_command_contents_multiple(self):
        from tools.latex_draft import extract_command_content

        block = r"\added[id=AI]{First.}" + "\n\n" + r"\added[id=AI]{Second.}"
        self.assertEqual(
            extract_command_content(block, "added"), "First.\n\nSecond."
        )

    def test_single_paragraph_unchanged(self):
        self.run_cli(
            "draft-replace",
            "--old",
            "Small problems are worth studying.",
            "--new",
            "One paragraph only.",
        )
        text = self.tex.read_text()
        self.assertEqual(self.strip_block_markers(text).count(r"\added[id=AI]{"), 1)


class TestDraftPreamble(LatexDraftCase):
    r"""Preamble drafts: raw LaTeX between markers, NOT wrapped in
    \added/\deleted (the changes package cannot typeset in the preamble).
    Accept merges verbatim, reject removes byte-identically."""

    def test_end_position_raw_text_no_changes_package(self):
        rc = self.run_cli(
            "draft-preamble",
            "--end",
            "--text",
            r"\renewcommand{\thesection}{\Roman{section}}",
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        did = self.draft_ids()[0]
        self.assertIn(f"% latex-draft:{did}\n", text)
        self.assertIn(r"\renewcommand{\thesection}{\Roman{section}}", text)
        self.assertIn(f"% latex-draft-end:{did}", text)
        # raw text: no changes-package markup, and the package is NOT injected
        self.assertNotIn(r"\added", text)
        self.assertNotIn(r"\deleted[", self.strip_block_markers(text))
        self.assertNotIn("changes", text)
        self.assertEqual(self.state()["drafts"][did]["op"], "preamble")
        self.assertTrue(self.state()["drafts"][did]["preamble"])
        # the raw text sits before \begin{document}
        self.assertLess(
            text.index(r"\renewcommand{\thesection}"),
            text.index("\\begin{document}"),
        )

    def test_start_position_after_documentclass(self):
        rc = self.run_cli(
            "draft-preamble", "--start", "--text", r"\usepackage{amsmath}"
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertLess(
            text.index("\\documentclass"),
            text.index(r"\usepackage{amsmath}"),
        )
        self.assertLess(
            text.index(r"\usepackage{amsmath}"), text.index("\\title")
        )

    def test_after_anchor_preamble_line(self):
        rc = self.run_cli(
            "draft-preamble", "--after", "\\title{A Small Paper}", "--text", r"\setlength{\parskip}{1em}"
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertLess(
            text.index("\\title{A Small Paper}"),
            text.index(r"\setlength{\parskip}{1em}"),
        )
        self.assertLess(
            text.index(r"\setlength{\parskip}{1em}"),
            text.index("\\begin{document}"),
        )

    def test_anchor_searches_preamble_only(self):
        # The anchor text also appears in the body; only the preamble
        # occurrence may count.
        self.tex.write_text(
            "\\documentclass{article}\n"
            "\\usepackage{geometry}\n"
            "\\begin{document}\n"
            "\\usepackage{geometry}\n"
            "\\end{document}\n"
        )
        rc = self.run_cli(
            "draft-preamble", "--after", "\\usepackage{geometry}", "--text", "X"
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        lines = text.split("\n")
        self.assertEqual(lines[1], "\\usepackage{geometry}")
        self.assertIn("% latex-draft:", lines[2])

    def test_ambiguous_anchor_fails(self):
        self.tex.write_text(
            "\\documentclass{article}\n"
            "\\usepackage{amsmath}\n"
            "\\usepackage{amsmath}\n"
            "\\begin{document}\n"
            "x\n"
            "\\end{document}\n"
        )
        original = self.tex.read_text()
        rc = self.run_cli(
            "draft-preamble", "--after", "\\usepackage{amsmath}", "--text", "X"
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.tex.read_text(), original)
        self.assertEqual(self.draft_ids(), [])

    def test_anchor_skips_pending_draft_blocks(self):
        self.run_cli("draft-preamble", "--end", "--text", r"\captionsetup{font=small}")
        original = self.tex.read_text()
        # The anchored text exists only inside the pending draft block.
        rc = self.run_cli(
            "draft-preamble", "--after", r"\captionsetup{font=small}", "--text", "X"
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.tex.read_text(), original)

    def test_accept_merges_verbatim(self):
        rc = self.run_cli(
            "draft-preamble",
            "--end",
            "--text",
            r"\renewcommand{\thesection}{\Roman{section}}",
        )
        self.assertEqual(rc, 0)
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        text = self.tex.read_text()
        self.assertIn(r"\renewcommand{\thesection}{\Roman{section}}", text)
        self.assertNotIn("latex-draft:", text)
        self.assertEqual(self.run_cli("check"), 0)

    def test_reject_removes_byte_identical(self):
        original = self.tex.read_text()
        self.run_cli(
            "draft-preamble", "--end", "--text", r"\setlength{\parskip}{1em}"
        )
        did = self.draft_ids()[0]
        self.run_cli("reject", "--id", did)
        self.assertEqual(self.tex.read_text(), original)

    def test_reject_all_byte_identical(self):
        original = self.tex.read_text()
        self.run_cli("draft-preamble", "--start", "--text", r"\usepackage{amsmath}")
        self.run_cli("draft-preamble", "--end", "--text", r"\setlength{\parskip}{1em}")
        self.run_cli("reject", "--all")
        self.assertEqual(self.tex.read_text(), original)

    def test_multi_line_text_preserved_verbatim(self):
        text_arg = r"\setlength{\parskip}{1em}" + "\n" + r"\setlength{\parindent}{0pt}"
        self.run_cli("draft-preamble", "--end", "--text", text_arg)
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        text = self.tex.read_text()
        self.assertIn(text_arg, text)
        self.assertNotIn("latex-draft:", text)

    def test_requires_position(self):
        with self.assertRaises(SystemExit):
            self.run_cli("draft-preamble", "--text", "x")

    def test_no_begin_document_fails(self):
        self.tex.write_text("\\documentclass{article}\nno document env\n")
        rc = self.run_cli("draft-preamble", "--end", "--text", "x")
        self.assertEqual(rc, 2)
        self.assertEqual(self.draft_ids(), [])


if __name__ == "__main__":
    unittest.main()


class TestInlinePointEdits(LatexDraftCase):
    """Mid-line --old must produce an inline block: only the changed words are
    wrapped in \\deleted/\\added, the surrounding line text stays plain."""

    def setUp(self):
        super().setUp()
        long_line = (
            "This whole paragraph is one single source line in the tex file. "
            "The quick brown fox jumps over the lazy dog and then keeps running for a while."
        )
        self.tex.write_text(
            "\\documentclass{article}" + chr(10)
            + "\\begin{document}" + chr(10)
            + long_line + chr(10)
            + "\\end{document}" + chr(10)
        )

    def test_mid_line_replace_marks_only_the_phrase(self):
        self.run_cli(
            "draft-replace", "--old", "quick brown fox", "--new", "slow red hare"
        )
        text = self.tex.read_text()
        self.assertIn(r"\deleted[id=AI]{quick brown fox}", text)
        self.assertIn(r"\added[id=AI]{slow red hare}", text)
        # the surrounding sentence text is NOT inside the markup
        self.assertIn("This whole paragraph", text)
        self.assertNotIn(r"\deleted[id=AI]{This whole paragraph", text)

    def test_inline_accept_merges_surrounding_text(self):
        self.run_cli(
            "draft-replace", "--old", "quick brown fox", "--new", "slow red hare"
        )
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        text = self.tex.read_text()
        self.assertIn(
            "The slow red hare jumps over the lazy dog", text
        )
        self.assertNotIn("quick brown fox", text)
        self.assertNotIn("latex-draft:", text)

    def test_inline_reject_restores_byte_identical(self):
        original = self.tex.read_text()
        self.run_cli(
            "draft-replace", "--old", "quick brown fox", "--new", "slow red hare"
        )
        did = self.draft_ids()[0]
        self.run_cli("reject", "--id", did)
        self.assertEqual(self.tex.read_text(), original)

    def test_point_edit_abutting_punctuation_keeps_spacing_exact(self):
        # regression: build_inline_block used to insert separator spaces around
        # the diff, turning "carefully." into "rigorously ." on accept
        original = self.tex.read_text()
        # 'while' abuts the line's final period: "... running for a while."
        self.run_cli("draft-replace", "--old", "while", "--new", "moment")
        text = self.tex.read_text()
        self.assertIn("\\added[id=AI]{moment}.", text)
        self.run_cli("reject", "--all")
        self.assertEqual(self.tex.read_text(), original)
        self.run_cli("draft-replace", "--old", "while", "--new", "moment")
        self.run_cli("accept", "--all")
        self.assertIn("running for a moment.", self.tex.read_text())
        self.assertNotIn("moment .", self.tex.read_text())

    def test_two_point_edits_on_one_line_compose_without_tab_corruption(self):
        # regression: composing a second point edit into the host block used
        # a string repl in re.sub, so \textsuperscript became a literal TAB
        original = self.tex.read_text()
        self.run_cli(
            "draft-replace", "--old", "quick brown fox", "--new", "slow red hare"
        )
        self.run_cli(
            "draft-replace", "--old", "lazy dog", "--new", "sleepy cat"
        )
        text = self.tex.read_text()
        self.assertNotIn(chr(9), text, "a TAB leaked into the tex source")
        self.assertIn(chr(92) + "textsuperscript{", text)
        # exactly one pending block hosts both edits (the first draft is
        # superseded by the composed one)
        pending = [d for d, r in self.state()["drafts"].items() if r["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.run_cli("reject", "--all")
        self.assertEqual(self.tex.read_text(), original)
        self.run_cli(
            "draft-replace", "--old", "quick brown fox", "--new", "slow red hare"
        )
        self.run_cli(
            "draft-replace", "--old", "lazy dog", "--new", "sleepy cat"
        )
        self.run_cli("accept", "--all")
        merged = self.tex.read_text()
        self.assertIn("The slow red hare jumps over the sleepy cat", merged)
        self.assertNotIn("latex-draft:", merged)

    def test_partial_word_change_marks_only_changed_word(self):
        self.run_cli("draft-replace", "--old", "quick", "--new", "very quick")
        text = self.tex.read_text()
        self.assertIn(r"\added[id=AI]{very} quick", text)
        self.run_cli("accept", "--all")
        self.assertIn("The very quick brown fox", self.tex.read_text())

    def test_whole_line_replace_still_uses_line_block(self):
        # when --old IS the whole line, keep the whole-line block
        whole = ("This whole paragraph is one single source line in the tex file. "
                 "The quick brown fox jumps over the lazy dog and then keeps running for a while.")
        self.run_cli("draft-replace", "--old", whole, "--new", "New sentence.")
        text = self.tex.read_text()
        self.assertIn(r"\deleted[id=AI]{" + whole + "}", text)

    def test_no_inline_diff_inside_command_brackets(self):
        # --old inside \includegraphics[width=...] must NOT use inline diff
        self.tex.write_text(
            "\\documentclass{article}\n\\usepackage{graphicx}\n"
            "\\begin{document}\n\\begin{figure}[h]\n"
            "\\includegraphics[width=0.5\\textwidth]{example-image}\n"
            "\\end{figure}\n\\end{document}\n"
        )
        rc = self.run_cli(
            "draft-replace", "--old", "width=0.5\\textwidth",
            "--new", "width=0.9\\textwidth",
        )
        # inside a float interior: refused (exit 2), no markup written
        self.assertEqual(rc, 2)


class TestSessionLockAndAtomicWrite(LatexDraftCase):
    """Issue 5: concurrent invocations serialize; temp files are per-pid."""

    def test_lock_file_created_next_to_state(self):
        self.run_cli("list")
        self.assertTrue((self.dir / "paper.tex.latex-draft.lock").exists())

    def test_rapid_successive_drafts_all_land(self):
        import subprocess
        # three disjoint, non-overlapping point edits fired in parallel;
        # the session lock must serialize them so none races or crashes
        jobs = [
            ("Small problems", "Big problems", []),
            # different source line than the first job: same-line point edits
            # now compose into one block (bug-report-2 bugs 1/4), so the lock
            # test must use disjoint lines to expect 3 pending drafts.
            # Sectioning titles take --unmarked: marked drafts inside them
            # break the build ("Not allowed in LR mode", session report bug 2).
            ("\\section{Introduction}", "\\section{Overview}", ["--unmarked"]),
            ("\\title{A Small Paper}", "\\title{A Paper}", ["--unmarked"]),
        ]
        procs = [
            subprocess.Popen(
                [sys.executable, str(ROOT / "tools" / "latex_draft.py"), str(self.tex),
                 "draft-replace", "--old", old, "--new", new, *extra],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for old, new, extra in jobs
        ]
        for p in procs:
            p.wait()
        for p in procs:
            self.assertEqual(p.returncode, 0, p.communicate()[1])
        pending = [d for d, r in self.state()["drafts"].items() if r["status"] == "pending"]
        self.assertEqual(len(pending), 3)
        self.run_cli("reject", "--all")
        self.assertNotIn("latex-draft:", self.tex.read_text())

    def test_no_stray_tmp_files_left_behind(self):
        self.run_cli("draft-insert", "--at-line", "3", "--text", "x.")
        leftovers = [p for p in self.dir.iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])


class TestRejectUnmarkedAndHandDeleted(LatexDraftCase):
    """Issue 6: unmarked drafts invert via reject; vanished blocks close cleanly."""

    MATH_PAPER = (
        "\\documentclass{article}\n\\begin{document}\n"
        "\\begin{equation}\ndt' = x\n\\end{equation}\n\\end{document}\n"
    )

    def setUp(self):
        super().setUp()
        self.tex.write_text(self.MATH_PAPER)

    def test_reject_unmarked_reverts_direct_patch(self):
        rc = self.run_cli(
            "draft-replace", "--unmarked", "--old", "dt'", "--new", "\\mathrm{d}t"
        )
        self.assertEqual(rc, 0)
        self.assertIn("\\mathrm{d}t", self.tex.read_text())
        did = self.draft_ids()[0]
        self.assertEqual(self.run_cli("reject", "--id", did), 0)
        self.assertIn("dt'", self.tex.read_text())
        self.assertNotIn("mathrm", self.tex.read_text())
        self.assertEqual(self.state()["drafts"][did]["status"], "rejected")

    def test_accept_unmarked_keeps_direct_patch(self):
        self.run_cli("draft-replace", "--unmarked", "--old", "dt'", "--new", "\\mathrm{d}t")
        self.assertEqual(self.run_cli("accept", "--all"), 0)
        self.assertIn("\\mathrm{d}t", self.tex.read_text())

    def test_reject_id_of_hand_deleted_block_closes_without_error(self):
        # a marked draft in the body, then hand-delete its block
        rc = self.run_cli(
            "draft-replace", "--old", "dt'", "--new", "y", "--unmarked"
        )
        self.assertEqual(rc, 0)
        rc = self.run_cli(
            "draft-replace", "--old", "= x", "--new", "= y", "--unmarked"
        )
        self.assertEqual(rc, 0)
        self.run_cli("draft-insert", "--after", "\\end{equation}", "--text", "Body note.")
        did = [d for d in self.draft_ids()
               if not self.state()["drafts"][d].get("unmarked")][0]
        self.tex.write_text(BLOCK_RE.sub("", self.tex.read_text()))
        rc = self.run_cli("reject", "--id", did)
        # turn-start reconcile already closed it; reject then reports cleanly
        self.assertEqual(self.state()["drafts"][did]["status"], "rejected")
        self.assertIn(rc, (0, 2))


class TestReconcileOnHandRestore(LatexDraftCase):
    """Issue 7: hand-restoring the files closes phantom pending drafts."""

    def test_phantom_pending_closed_at_turn_start(self):
        original = self.tex.read_text()
        self.run_cli("draft-replace", "--old", "Small problems", "--new", "Big problems")
        self.run_cli("draft-insert", "--after", "Introduction", "--text", "x.")
        self.tex.write_text(original)  # hand restore
        self.run_cli("status")
        pending = [d for d, r in self.state()["drafts"].items() if r["status"] == "pending"]
        self.assertEqual(pending, [])
        self.assertEqual(self.run_cli("check"), 0)

    def test_block_brought_back_reopens_as_pending(self):
        self.run_cli("draft-replace", "--old", "Small problems", "--new", "Big problems")
        did = self.draft_ids()[0]
        self.run_cli("reject", "--id", did)
        self.assertEqual(self.state()["drafts"][did]["status"], "rejected")
        self.run_cli("undo", "--n", "2")  # undo reject + draft: block returns
        self.assertEqual(self.state()["drafts"][did]["status"], "pending")


class TestCompileLogParsing(LatexDraftCase):
    """Issue 8: the log, not the exit code, decides failure."""

    def _fake_engine(self, script: str):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        eng = bin_dir / "latexmk"
        eng.write_text(script)
        eng.chmod(0o755)
        return bin_dir

    def test_exit_nonzero_without_pdf_is_a_hard_failure(self):
        # Bug report 2026-11-03 (compile false negative): a non-zero exit with
        # a clean-looking log must NOT be reported as "the PDF is complete" —
        # error cascades can land in later passes while the last log scan
        # raced them. Without a PDF there is nothing to trust: fail the turn.
        self._fake_engine(
            "#!/bin/sh\necho 'no errors' > paper.log\nexit 12\n"
        )
        with mock.patch.dict(os.environ, {"PATH": str(self.dir / "bin") + os.pathsep + os.environ["PATH"]}):
            rc = self.run_cli("compile")
        self.assertEqual(rc, 1)

    def test_exit_nonzero_with_clean_log_and_pdf_is_bibtex_warning(self):
        # Bug report 2026-11-04 (session report, bug 3): latexmk exits non-zero
        # on a .bib defect while the document itself compiles — clean log,
        # PDF written. That is a warning (the .blg), not a broken build:
        # failing the turn made the skill's end-of-turn rule unsatisfiable.
        self._fake_engine(
            "#!/bin/sh\necho 'no errors' > paper.log\necho pdf > paper.pdf\n"
            "echo 'Warning--missing comma' > paper.blg\nexit 12\n"
        )
        with mock.patch.dict(os.environ, {"PATH": str(self.dir / "bin") + os.pathsep + os.environ["PATH"]}):
            rc = self.run_cli("compile")
        self.assertEqual(rc, 0)

    def test_log_error_line_is_a_failure_even_with_pdf(self):
        self._fake_engine(
            "#!/bin/sh\necho '! Undefined control sequence.' > paper.log\n"
            "echo 'l.3 \\badcmd' >> paper.log\necho pdf > paper.pdf\nexit 1\n"
        )
        with mock.patch.dict(os.environ, {"PATH": str(self.dir / "bin") + os.pathsep + os.environ["PATH"]}):
            rc = self.run_cli("compile")
        self.assertEqual(rc, 1)


class TestCommentSanitizer(LatexDraftCase):
    """Issue 9: comment text cannot kill the build with math-only commands."""

    def test_mathrm_outside_math_is_escaped(self):
        self.run_cli(
            "draft-comment", "--after", "Small problems",
            "--text", r"the term is \mathrm{d}x here",
        )
        self.assertIn(r"\textbackslash{}mathrm{d}x", self.tex.read_text())

    def test_math_delimiters_keep_commands_live(self):
        self.run_cli(
            "draft-comment", "--after", "Small problems",
            "--text", r"math $\mathrm{d}x$ stays",
        )
        self.assertIn(r"$\mathrm{d}x$", self.tex.read_text())

    def test_text_safe_commands_stay_live(self):
        self.run_cli(
            "draft-comment", "--after", "Small problems",
            "--text", r"see \emph{this} and \cite{smith2020}",
        )
        text = self.tex.read_text()
        self.assertIn(r"\emph{this}", text)
        self.assertIn(r"\cite{smith2020}", text)


class TestAnchorMasking(LatexDraftCase):
    """Issue 10: anchors and --old never match inside % comments or draft blocks."""

    def test_anchor_ignores_commented_duplicate(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "% \\label{fig:fig3} commented out\n"
            "Real text \\label{fig:fig3} here.\n"
            "\\end{document}\n"
        )
        rc = self.run_cli(
            "draft-insert", "--after", "\\label{fig:fig3}", "--text", "X."
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.tex.read_text().count("X."), 1)

    def test_anchor_only_inside_draft_block_is_refused(self):
        self.run_cli(
            "draft-replace", "--old", "worth studying", "--new", "deeply studying"
        )
        rc = self.run_cli("draft-insert", "--after", "worth studying", "--text", "X.")
        self.assertEqual(rc, 2)

    def test_old_inside_comment_does_not_count(self):
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "% old phrase commented\nReal text.\n\\end{document}\n"
        )
        rc = self.run_cli("draft-replace", "--old", "old phrase", "--new", "X")
        self.assertEqual(rc, 2)


class TestAtLineRawNumbering(LatexDraftCase):
    """Issue 11: --at-line is the raw 1-based file line, as editors show it."""

    def test_at_line_counts_raw_lines_including_injected(self):
        # PAPER lines: 1 documentclass, 2 title, 3 begin{document}, 4 section,
        # 5 sentence, 6 end{document. The first call injects 2 preamble lines
        # but numbering stays raw.
        rc = self.run_cli("draft-insert", "--at-line", "5", "--text", "X.")
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        # raw line 5 (0-based 4) is now the section line (preamble shifted it)
        # -> block lands before the section line, inside the body
        self.assertLess(text.index("\\begin{document}"), text.index("% latex-draft:"))

    def test_at_line_beyond_file_refused(self):
        rc = self.run_cli("draft-insert", "--at-line", "999", "--text", "X.")
        self.assertEqual(rc, 2)


class TestComposedPointEdits(LatexDraftCase):
    """Bug report 2, bugs 1+4: a second point edit on a line that already has
    a pending inline draft must compose into ONE coherent block (not a
    fragment sibling with the wrong diff), and accepting must merge both
    fixes while rejecting restores the original byte-identically."""

    LINE = (
        "In the quick field probe the fox jumps over the lazy dog while the "
        "probe $a+\\frac{b}{2}$ streams \\cite{doe2020} beside the slow river, "
        "and the cat naps near the warm hat in the cool morning shade."
    )

    def setUp(self):
        super().setUp()
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            + self.LINE
            + "\n\\end{document}\n"
        )

    def test_second_point_edit_composes_into_one_block(self):
        rc = self.run_cli(
            "draft-replace", "--old", "the fox jumps over the lazy dog",
            "--new", "the fox leaps over the sleepy dog",
        )
        self.assertEqual(rc, 0)
        rc = self.run_cli(
            "draft-replace", "--old", "near the warm hat", "--new", "near the warm mat"
        )
        self.assertEqual(rc, 0, "second same-line point edit must not fail")
        text = self.tex.read_text()
        # ONE block, containing BOTH edits, no blank lines inside the body
        self.assertEqual(text.count("% latex-draft:"), 1)
        self.assertIn(r"\added[id=AI]{mat}", text)
        self.assertIn(r"\added[id=AI]{leaps}", text)
        body = BLOCK_RE.search(text).group(2)
        self.assertNotIn("\n\n", body, "no paragraph breaks inside a draft block")
        # the host draft is closed as superseded, the new one is pending
        statuses = {d: r["status"] for d, r in self.state()["drafts"].items()}
        self.assertEqual(sorted(statuses.values()), ["pending", "rejected"])
        host = [d for d, s in statuses.items() if s == "rejected"][0]
        self.assertEqual(self.state()["drafts"][host].get("superseded_by"),
                         [d for d, s in statuses.items() if s == "pending"][0])

    def test_accept_composed_block_merges_both_fixes(self):
        self.run_cli(
            "draft-replace", "--old", "the fox jumps over the lazy dog",
            "--new", "the fox leaps over the sleepy dog",
        )
        self.run_cli(
            "draft-replace", "--old", "near the warm hat", "--new", "near the warm mat"
        )
        pending = [d for d, r in self.state()["drafts"].items() if r["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.run_cli("accept", "--id", pending[0]), 0)
        text = self.tex.read_text()
        self.assertIn("the fox leaps over the sleepy dog", text)
        self.assertIn("near the warm mat", text)
        self.assertNotIn("latex-draft:", text)
        self.assertNotIn("textsuperscript", text)

    def test_reject_composed_block_is_byte_identical(self):
        original = self.tex.read_text()
        self.run_cli(
            "draft-replace", "--old", "the fox jumps over the lazy dog",
            "--new", "the fox leaps over the sleepy dog",
        )
        self.run_cli(
            "draft-replace", "--old", "near the warm hat", "--new", "near the warm mat"
        )
        self.assertEqual(self.run_cli("reject", "--all"), 0)
        self.assertEqual(self.tex.read_text(), original)

    def test_overlapping_point_edit_is_refused(self):
        self.run_cli(
            "draft-replace", "--old", "jumps", "--new", "leaps"
        )
        before = self.tex.read_text()
        rc = self.run_cli("draft-replace", "--old", "jumps", "--new", "bounds")
        self.assertEqual(rc, 2, "editing the words a pending point edit deletes must be refused")
        self.assertEqual(self.tex.read_text(), before)

    def test_editing_pending_insert_text_still_refused(self):
        self.run_cli("draft-insert", "--after", "probe", "--text", "Hello world.")
        before = self.tex.read_text()
        rc = self.run_cli("draft-replace", "--old", "Hello", "--new", "Goodbye")
        self.assertEqual(rc, 2)
        self.assertEqual(self.tex.read_text(), before)

    def test_list_preview_comes_from_block_content(self):
        import io
        from contextlib import redirect_stdout
        self.run_cli(
            "draft-replace", "--old", "jumps over", "--new", "leaps over"
        )
        self.run_cli(
            "draft-replace", "--old", "warm hat", "--new", "warm mat"
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.run_cli("list")
        out = buf.getvalue()
        pending_line = [l for l in out.splitlines() if "pending" in l][0]
        # the pending block contains BOTH edits; the preview must show them
        self.assertIn("leaps", pending_line)
        self.assertIn("mat", pending_line)


class TestSubdirectoryInputPaths(LatexDraftCase):
    """Bug report 2, bug 2: drafts in subdirectory \\input files must be
    recorded and resolved by their relative path (parts/intro.tex), and
    recovery must never create a spurious file at the main-dir basename."""

    def setUp(self):
        super().setUp()
        (self.dir / "parts").mkdir()
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\input{parts/intro}\n\\end{document}\n"
        )
        self.chapter = self.dir / "parts" / "intro.tex"
        self.chapter.write_text("The probing fox naps by the quiet pond.\n")

    def test_record_stores_relative_path(self):
        rc = self.run_cli(
            "draft-replace", "--file", "parts/intro.tex",
            "--old", "quiet pond", "--new", "still pond",
        )
        self.assertEqual(rc, 0)
        did = self.draft_ids()[0]
        self.assertEqual(self.state()["drafts"][did]["file"], "parts/intro.tex")

    def test_reject_resolves_the_real_input_path(self):
        self.run_cli(
            "draft-replace", "--file", "parts/intro.tex",
            "--old", "quiet pond", "--new", "still pond",
        )
        did = self.draft_ids()[0]
        rc = self.run_cli("reject", "--id", did)
        self.assertEqual(rc, 0, "reject must find the block in parts/intro.tex")
        self.assertNotIn("latex-draft:", self.chapter.read_text())
        self.assertIn("quiet pond", self.chapter.read_text())
        self.assertEqual(self.state()["drafts"][did]["status"], "rejected")
        self.assertFalse(self.state()["drafts"][did].get("hand_deleted", False))

    def test_accept_resolves_the_real_input_path(self):
        self.run_cli(
            "draft-replace", "--file", "parts/intro.tex",
            "--old", "quiet pond", "--new", "still pond",
        )
        did = self.draft_ids()[0]
        self.assertEqual(self.run_cli("accept", "--id", did), 0)
        self.assertIn("still pond", self.chapter.read_text())
        self.assertNotIn("latex-draft:", self.chapter.read_text())

    def test_undo_force_restores_input_path_no_spurious_file(self):
        from tools.latex_draft import read_changelog
        self.run_cli(
            "draft-replace", "--file", "parts/intro.tex",
            "--old", "quiet pond", "--new", "still pond",
        )
        entries = [e for e in read_changelog(self.tex) if e.get("kind") == "change"]
        self.assertTrue(any(e.get("file") == "parts/intro.tex" for e in entries))
        rc = self.run_cli("undo", "--n", "2", "--force")  # profile setup + chapter draft
        self.assertEqual(rc, 0)
        self.assertIn("quiet pond", self.chapter.read_text())
        self.assertFalse((self.dir / "intro.tex").exists(),
                         "no spurious file at the main-dir basename")

    def test_legacy_basename_record_is_migrated(self):
        # a state written by the old version records only "intro.tex"
        self.run_cli(
            "draft-replace", "--file", "parts/intro.tex",
            "--old", "quiet pond", "--new", "still pond",
        )
        state = self.state()
        for rec in state["drafts"].values():
            rec["file"] = "intro.tex"  # simulate the legacy basename record
        from tools.latex_draft import save_state
        save_state(self.tex, state)
        did = self.draft_ids()[0]
        rc = self.run_cli("reject", "--id", did)
        self.assertEqual(rc, 0, "legacy basename record must migrate and resolve")
        self.assertNotIn("latex-draft:", self.chapter.read_text())
        self.assertEqual(self.state()["drafts"][did]["file"], "parts/intro.tex")

    def test_status_lists_relative_input_paths(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.run_cli("status")
        self.assertIn("parts/intro.tex", buf.getvalue())


class TestMissingInputFileIsHardError(LatexDraftCase):
    """Bug report 2, bug 3: an unreadable/missing input during resolve must be
    a hard error naming the path, with a suspect flag surfaced by status and
    check -- never a silent hand_deleted classification."""

    def setUp(self):
        super().setUp()
        (self.dir / "parts").mkdir()
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\input{parts/intro}\n\\end{document}\n"
        )
        self.chapter = self.dir / "parts" / "intro.tex"
        self.chapter.write_text("The probing fox naps by the quiet pond.\n")
        self.run_cli(
            "draft-replace", "--file", "parts/intro.tex",
            "--old", "quiet pond", "--new", "still pond",
        )
        self.did = self.draft_ids()[0]
        self.chapter.unlink()  # the input file goes missing

    def test_reject_is_hard_error_naming_path(self):
        rc = self.run_cli("reject", "--id", self.did)
        self.assertEqual(rc, 2)
        rec = self.state()["drafts"][self.did]
        self.assertEqual(rec["status"], "pending", "must stay pending, not hand_deleted")
        self.assertTrue(rec.get("suspect"))

    def test_accept_all_refuses_without_traceback(self):
        rc = self.run_cli("accept", "--all")
        self.assertNotEqual(rc, 0)
        self.assertIn(rc, (1, 2))

    def test_status_and_check_surface_suspect(self):
        import io
        from contextlib import redirect_stdout
        self.run_cli("status")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.run_cli("status")
        self.assertIn("SUSPECT", buf.getvalue())
        self.assertIn(self.did, buf.getvalue())
        rc = self.run_cli("check")
        self.assertEqual(rc, 1)

    def test_genuine_hand_deletion_still_closes_cleanly(self):
        # restore the file but strip the block by hand: that IS hand-deletion.
        # The turn-start external-edit reconcile may already close the draft
        # (rejected + hand_deleted) before reject --id runs, in which case
        # reject reports rc 2 "already rejected"; either way the draft ends
        # closed as hand-deleted and the file stays clean.
        self.chapter.write_text("The probing fox naps by the quiet pond.\n")
        rc = self.run_cli("reject", "--id", self.did)
        self.assertIn(rc, (0, 2))
        rec = self.state()["drafts"][self.did]
        self.assertEqual(rec["status"], "rejected")
        self.assertTrue(rec.get("hand_deleted"))
        self.assertNotIn("latex-draft:", self.chapter.read_text())


class TestCiteAndMathPointEdits(LatexDraftCase):
    r"""Bug report 2026-11-03: lines containing \cite/$...$ used to fall back to
    chained whole-line blocks (duplicated PDF text, \cite inside \deleted
    breaks ulem). They must compose inline and compile."""

    LINE = ("We rely on the method of " + chr(92) + "cite{doe2020} and the response "
            "$F(" + chr(92) + "omega)$ is measured in the regime.")

    def setUp(self):
        super().setUp()
        self.tex.write_text(
            chr(92)*2 + "documentclass{article}\n" + chr(92)*2 + "begin{document}\n"
            + self.LINE + "\n" + chr(92)*2 + "end{document}\n"
        )

    def test_cite_math_line_uses_inline_diff_not_whole_line(self):
        rc = self.run_cli(
            "draft-replace",
            "--old", "rely on the method of " + chr(92) + "cite{doe2020}",
            "--new", "rest on the method of " + chr(92) + "cite{doe2020}",
        )
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        # inline: the cite token is unchanged, so it stays OUTSIDE the markup
        self.assertIn(chr(92) + "deleted[id=AI]{rely}", text)
        self.assertIn(chr(92) + "cite{doe2020}", text)
        self.assertNotIn(chr(92) + "deleted[id=AI]{" + self.LINE, text)

    def test_three_edits_on_cite_math_line_compose_into_one_block(self):
        self.run_cli("draft-replace", "--old", "rely on the method", "--new", "rest on the method")
        self.run_cli("draft-replace", "--old", "$F(" + chr(92) + "omega)$", "--new", "$F(" + chr(92) + "omega)^{2}$")
        self.run_cli("draft-replace", "--old", "is measured", "--new", "was measured")
        pending = [d for d, r in self.state()["drafts"].items() if r["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        text = self.tex.read_text()
        # no full-line deleted copy of the cite line
        self.assertNotIn(chr(92) + "deleted[id=AI]{" + self.LINE, text)

    def test_whole_line_replace_protects_cite_in_deleted(self):
        rc = self.run_cli("draft-replace", "--old", self.LINE, "--new", "A new line.")
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        # the cite inside \deleted is \mbox-wrapped so ulem can strike it
        self.assertIn(chr(92) + "mbox{" + chr(92) + "cite{doe2020}}", text)

    def test_reject_after_whole_line_cite_replace_is_byte_identical(self):
        original = self.tex.read_text()
        self.run_cli("draft-replace", "--old", self.LINE, "--new", "A new line.")
        self.run_cli("reject", "--all")
        self.assertEqual(self.tex.read_text(), original)

    def test_accept_whole_line_cite_replace_merges_new_text(self):
        self.run_cli("draft-replace", "--old", self.LINE, "--new", "A new line.")
        self.run_cli("accept", "--all")
        text = self.tex.read_text()
        self.assertIn("A new line.", text)
        self.assertNotIn("latex-draft:", text)
        self.assertNotIn(chr(92) + "deleted", text)


class TestNestedBraceInlineAccept(LatexDraftCase):
    """Accept of an inline block must unwrap nested braces ($x^{2}$),
    not leave the markup in the file."""

    def test_accept_unwraps_nested_braces(self):
        self.tex.write_text(
            chr(92)*2 + "documentclass{article}\n" + chr(92)*2 + "begin{document}\n"
            "The value $x^{2}$ is small.\n" + chr(92)*2 + "end{document}\n"
        )
        self.run_cli("draft-replace", "--old", "$x^{2}$", "--new", "$x^{3}$")
        self.run_cli("accept", "--all")
        text = self.tex.read_text()
        self.assertIn("The value $x^{3}$ is small.", text)
        self.assertNotIn(chr(92) + "added", text)
        self.assertNotIn(chr(92) + "deleted", text)

    def test_reject_unwraps_nested_braces_byte_identical(self):
        self.tex.write_text(
            chr(92)*2 + "documentclass{article}\n" + chr(92)*2 + "begin{document}\n"
            "The value $x^{2}$ is small.\n" + chr(92)*2 + "end{document}\n"
        )
        original = self.tex.read_text()
        self.run_cli("draft-replace", "--old", "$x^{2}$", "--new", "$x^{3}$")
        self.run_cli("reject", "--all")
        self.assertEqual(self.tex.read_text(), original)


class TestOldSpanningPendingPointEdit(LatexDraftCase):
    """Bug hunt: an --old that spans the words a pending inline point edit
    deletes plus surrounding plain text must get the clear 'overlaps a
    pending point-edit draft' refusal, not a misleading '--old not found'."""

    def test_spanning_old_gets_clear_refusal(self):
        import io
        from contextlib import redirect_stderr
        self.tex.write_text(
            "\\" + "documentclass{article}\n" + "\\" + "begin{document}\n"
            "The quick brown fox is a classic test sentence.\n"
            + "\\" + "end{document}\n"
        )
        self.run_cli("draft-replace", "--old", "quick brown fox", "--new", "red fox")
        buf = io.StringIO()
        with redirect_stderr(buf):
            # spans the deleted words ("quick brown") plus plain text
            rc = self.run_cli(
                "draft-replace", "--old", "quick brown fox is a classic", "--new", "x"
            )
        self.assertEqual(rc, 2)
        self.assertIn("point-edit draft", buf.getvalue())
        self.assertNotIn("not found", buf.getvalue())

    def test_disjoint_old_on_same_line_still_composes(self):
        self.tex.write_text(
            "\\" + "documentclass{article}\n" + "\\" + "begin{document}\n"
            "The quick brown fox is a classic test sentence.\n"
            + "\\" + "end{document}\n"
        )
        self.run_cli("draft-replace", "--old", "quick brown fox", "--new", "red fox")
        rc = self.run_cli("draft-replace", "--old", "classic test", "--new", "standard example")
        self.assertEqual(rc, 0)
        pending = [d for d, r in self.state()["drafts"].items() if r["status"] == "pending"]
        self.assertEqual(len(pending), 1)


class TestAnchorMessageNearPendingDrafts(LatexDraftCase):
    """Bug report 2, bug 5: an anchor that only matches inside pending draft
    blocks must name those draft ids and say how to proceed."""

    def setUp(self):
        super().setUp()
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "The quick brown fox jumps, as field surveyors have documented, "
            "while the cat naps.\n\\end{document}\n"
        )
        self.run_cli("draft-replace", "--old", "quick brown", "--new", "quick red")

    def test_anchor_error_names_pending_draft_and_suggests(self):
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = self.run_cli("draft-comment", "--after", "have documented", "--text", "x")
        self.assertEqual(rc, 2)
        err = buf.getvalue()
        did = self.draft_ids()[0]
        self.assertIn(did, err)
        self.assertIn("pending draft", err)
        self.assertIn("--at-line", err)


class TestMarginRefusal(LatexDraftCase):
    r"""Usage notes hiccup 1: a --margin note (\todo -> \marginpar) inside a
    float environment or in math is illegal and fails SILENTLY there (later
    same-class floats get dropped with no '!' error line) -- refuse at draft
    time instead of letting it poison the compile at accept time."""

    def setUp(self):
        super().setUp()
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "Text before the figure.\n"
            "\\begin{figure}[ht]\n\\centering\n"
            "\\caption{A dummy figure.}\n\\label{fig:dummy}\n"
            "\\end{figure}\n"
            "Text after the figure.\n"
            "\\begin{equation}\nx = 1\n\\end{equation}\n"
            "\\end{document}\n"
        )

    def _margin(self, *anchor_args):
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = self.run_cli(
                "draft-comment", "--margin", *anchor_args, "--text", "A note."
            )
        return rc, buf.getvalue()

    def test_margin_inside_figure_is_refused(self):
        before = self.tex.read_text()
        rc, err = self._margin("--after", "\\caption{A dummy figure.}")
        self.assertEqual(rc, 2)
        self.assertIn("float", err)
        # a refused draft must not touch the file
        self.assertEqual(self.tex.read_text(), before)

    def test_margin_inside_math_is_refused(self):
        before = self.tex.read_text()
        rc, err = self._margin("--after", "x = 1")
        self.assertEqual(rc, 2)
        self.assertIn("margin note cannot be placed", err)
        self.assertEqual(self.tex.read_text(), before)

    def test_margin_outside_float_still_works(self):
        rc, err = self._margin("--after", "Text after the figure.")
        self.assertEqual(rc, 0, err)
        self.assertIn(r"\todo{A note.}", self.strip_block_markers(self.tex.read_text()))


class TestTodonotesStripDocumentWide(LatexDraftCase):
    r"""Usage notes hiccup 2: the auto-injected preamble strip must keep
    \usepackage{todonotes} while an ACCEPTED \todo remains in any \input file
    -- a main-file-only scan removed it and broke the build with
    '! Undefined control sequence \todo'."""

    def setUp(self):
        super().setUp()
        self.tex.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\input{chapter}\n\\end{document}\n"
        )
        self.chapter = self.dir / "chapter.tex"
        self.chapter.write_text("A chapter sentence worth checking.\n")

    def _draft_and_accept_margin_note(self):
        rc = self.run_cli(
            "draft-comment", "--file", "chapter.tex", "--margin",
            "--after", "A chapter sentence worth checking.",
            "--text", "Verify the claim.",
        )
        self.assertEqual(rc, 0)
        pending = [
            d for d, r in self.state()["drafts"].items() if r["status"] == "pending"
        ]
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.run_cli("accept", "--id", pending[0]), 0)

    def test_accepted_todo_in_input_file_keeps_todonotes(self):
        self._draft_and_accept_margin_note()
        self.assertIn(r"\todo{Verify the claim.}", self.chapter.read_text())
        main_text = self.tex.read_text()
        self.assertNotIn("latex-draft:", main_text)
        # the strip ran (no markers left) but must have kept todonotes:
        # the accepted \todo lives in chapter.tex, not in main.tex
        self.assertIn(r"\usepackage{todonotes}", main_text)

    def test_todonotes_removed_once_no_todo_remains_anywhere(self):
        self._draft_and_accept_margin_note()
        # hand-remove the accepted note, adopt the edit, resolve one more
        # draft: the strip must now find no \todo in any input file
        self.chapter.write_text("A chapter sentence worth checking.\n")
        self.run_cli("status")  # adopts the hand edit
        rc = self.run_cli(
            "draft-comment", "--after", "\\input{chapter}", "--text", "Plain note."
        )
        self.assertEqual(rc, 0)
        pending = [
            d for d, r in self.state()["drafts"].items() if r["status"] == "pending"
        ]
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.run_cli("accept", "--id", pending[0]), 0)
        main_text = self.tex.read_text()
        self.assertNotIn(r"\usepackage{todonotes}", main_text)
        self.assertNotIn(r"\usepackage[draft,authormarkup=brackets,authormarkupposition=right,deletedmarkup=sout]{changes}", main_text)


class TestBlgSurfacing(LatexDraftCase):
    r"""Usage notes hiccup 6: BibTeX errors (duplicate keys, malformed
    entries) live in the .blg, never as '!' lines in the .log -- a failed
    compile must surface the .blg problems, not just the log tail."""

    def test_blg_error_lines_helper(self):
        from tools.latex_draft import _blg_error_lines
        (self.dir / "paper.blg").write_text(
            "This is BibTeX, Version 0.99d\n"
            "Warning--I didn't find a database entry for \"x\"\n"
            "I'm skipping whatever remains of this entry\n"
            "(There were 2 error messages)\n"
        )
        lines = _blg_error_lines(self.tex)
        self.assertTrue(any("database entry" in line for line in lines))
        self.assertTrue(any("error message" in line for line in lines))
        self.assertFalse(any("Version 0.99d" in line for line in lines))

    def test_compile_failure_prints_blg_problems(self):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        eng = bin_dir / "latexmk"
        eng.write_text(
            "#!/bin/sh\n"
            "echo 'all fine' > paper.log\n"
            "printf 'Warning--duplicate entry key: asadi2020td\\n' > paper.blg\n"
            "printf '(There were 1 error messages)\\n' >> paper.blg\n"
            "exit 1\n"
        )
        eng.chmod(0o755)
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with mock.patch.dict(
            os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        ):
            with redirect_stderr(buf):
                rc = self.run_cli("compile")
        self.assertEqual(rc, 1)
        err = buf.getvalue()
        self.assertIn("paper.blg", err)
        self.assertIn("duplicate entry key", err)
