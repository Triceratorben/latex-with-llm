import tempfile
import time
import unittest
from pathlib import Path

from tools.latex_draft import (
    BACKUP_INTERVAL_S,
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
        return list(self.state()["drafts"])


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
        self.assertIn(r"\added[author=LLM]{New sentence.}", text)
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
        self.assertIn(r"\deleted[author=LLM]{Small problems are worth studying.}", text)
        self.assertIn(r"\added[author=LLM]{Big problems are worth studying.}", text)

    def test_replace_requires_unique_old(self):
        rc = self.run_cli("draft-replace", "--old", "o", "--new", "x")
        self.assertEqual(rc, 2)

    def test_delete_marks_only_old(self):
        rc = self.run_cli("draft-delete", "--old", "Small problems are worth studying.")
        self.assertEqual(rc, 0)
        text = self.tex.read_text()
        self.assertIn(r"\deleted[author=LLM]{Small problems are worth studying.}", text)
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
        out, found = resolve_drafts(text, {"abcd1234"}, accept=True)
        self.assertEqual(out, "A\nhello\nB")
        self.assertEqual(found, {"abcd1234"})

    def test_block_re_matches(self):
        block = build_block("abcd1234", new="x")
        m = BLOCK_RE.search(block)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "abcd1234")


class TestBackups(LatexDraftCase):
    def test_every_mutation_snapshots(self):
        self.run_cli("draft-insert", "--at-line", "1", "--text", "one.")
        self.run_cli("draft-insert", "--at-line", "1", "--text", "two.")
        self.assertEqual(len(list_snapshots(self.tex)), 2)

    def test_periodic_backup_only_after_interval(self):
        self.tex.write_text(PAPER)
        snapshot(self.tex)
        state = self.state()
        state["last_backup"] = time.time()
        from tools.latex_draft import save_state

        save_state(self.tex, state)
        before = len(list_snapshots(self.tex))
        self.run_cli("draft-insert", "--at-line", "1", "--text", "x.")
        # manual mutation snapshot only; periodic one suppressed
        self.assertEqual(len(list_snapshots(self.tex)), before + 1)
        state["last_backup"] = time.time() - BACKUP_INTERVAL_S - 1
        save_state(self.tex, state)
        self.run_cli("draft-insert", "--at-line", "1", "--text", "y.")
        self.assertEqual(len(list_snapshots(self.tex)), before + 3)

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

    def test_undo_without_backups_fails(self):
        self.assertEqual(self.run_cli("undo"), 1)

    def test_manual_backup_with_note(self):
        rc = self.run_cli("backup", "--note", "before big edit")
        self.assertEqual(rc, 0)
        snaps = list_snapshots(self.tex)
        self.assertEqual(len(snaps), 1)
        note = snaps[0].with_suffix(snaps[0].suffix + ".note")
        self.assertEqual(note.read_text().strip(), "before big edit")


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


if __name__ == "__main__":
    unittest.main()
