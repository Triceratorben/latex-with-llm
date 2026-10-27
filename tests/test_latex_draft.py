import tempfile
import time
import unittest
from pathlib import Path

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


class TestChangesPackage(LatexDraftCase):
    def test_injects_package_after_documentclass(self):
        out = ensure_changes_package(PAPER)
        self.assertIn("\\usepackage[draft]{changes}", out)
        self.assertLess(
            out.index("\\documentclass"), out.index("\\usepackage[draft]{changes}")
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
        self.assertIn(r"\added[id=LLM]{New sentence.}", text)
        self.assertIn(f"% latex-draft-end:{did}", text)
        self.assertIn("\\usepackage[draft]{changes}", text)

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
        self.assertIn(r"\deleted[id=LLM]{Small problems are worth studying.}", text)
        self.assertIn(r"\added[id=LLM]{Big problems are worth studying.}", text)

    def test_replace_requires_unique_old(self):
        rc = self.run_cli("draft-replace", "--old", "o", "--new", "x")
        self.assertEqual(rc, 2)

    def test_delete_marks_only_old(self):
        rc = self.run_cli("draft-delete", "--old", "Small problems are worth studying.")
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertIn(r"\deleted[id=LLM]{Small problems are worth studying.}", text)
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
        self.assertIn(r"\footnote{TODO: cite a source here.}", text)
        self.assertNotIn(r"\added", text)
        self.assertNotIn(r"\deleted", text)
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
        self.assertIn(r"\footnote{See related work.}", text)
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
        # 1 full initial backup + 1 per mutation
        self.assertEqual(len(list_snapshots(self.tex)), 3)

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
        entries = [e for e in read_changelog(self.tex) if e["kind"] == "change"]
        # draft insert, accept, and the preamble strip that follows the accept
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["turn"], 0)
        self.assertIn("draft insert", entries[0]["note"])
        self.assertIn("strip", entries[2]["note"])

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
        self.assertEqual(kinds, ["change", "undo"])


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
        self.assertIn(r"\added[id=LLM]{Drafted chapter sentence.}", chapter)
        # the main file gets the changes preamble (included files cannot
        # carry it) but no draft markers of its own
        self.assertNotIn("latex-draft:", main_tex)
        self.assertIn("usepackage[draft]{changes}", main_tex)
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
        self.assertIn(r"\deleted[id=LLM]{Chapter body text.}", self.chapter.read_text())
        self.assertIn(r"\added[id=LLM]{Rewritten chapter body.}", self.chapter.read_text())

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
        self.assertIn(r"\footnote{TODO: expand.}", self.chapter.read_text())

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
        self.run_cli("undo")
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
        self.assertIn(r"\added[id=LLM]{Nested draft.}", sub)
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
        # undo the preamble strip, the accept, and the draft
        self.run_cli("undo", "--n", "3")
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
        self.assertIn(r"\added[id=A]{Version A.}", text)
        self.assertIn(r"\added[id=B]{Version B.}", text)
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
        self.assertIn(r"\todo{Check this.}", text)
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
        self.assertIn(r"\todo{Check this.}", self.tex.read_text())
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
        rc = self.run_cli("undo", "--n", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(self.tex.read_text(), original)

    def test_undo_force_restores_anyway(self):
        self.run_cli("draft-insert", "--at-line", "5", "--text", "one.")
        self.run_cli("draft-insert", "--at-line", "5", "--text", "two.")
        rc = self.run_cli("undo", "--n", "2", "--force")
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
        entries = [e for e in read_changelog(self.tex) if e["kind"] == "change"]
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

        block = build_block("aaaaaaaa", new="Para one.\n\nPara two.", author="LLM")
        self.assertIn(r"\added[id=LLM]{Para one.}", block)
        self.assertIn(r"\added[id=LLM]{Para two.}", block)

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
        self.assertEqual(text.count(r"\added[id=LLM]{"), 2)
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
        self.assertEqual(text.count(r"\deleted[id=LLM]{"), 2)
        did = self.draft_ids()[0]
        self.run_cli("accept", "--id", did)
        text = self.tex.read_text()
        self.assertNotIn("Delete para one.", text)
        self.assertNotIn("Delete para two.", text)
        self.assertIn("Keep this.", text)

    def test_extract_command_contents_multiple(self):
        from tools.latex_draft import extract_command_content

        block = r"\added[id=LLM]{First.}" + "\n\n" + r"\added[id=LLM]{Second.}"
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
        self.assertEqual(text.count(r"\added[id=LLM]{"), 1)


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
        self.assertNotIn(r"\deleted", text)
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
