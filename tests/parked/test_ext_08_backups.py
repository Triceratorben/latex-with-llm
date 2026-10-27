"""GROUP 8 - Backup guarantees.

Question answered: the README promises an initial full backup, "a snapshot
before every change" and automatic pruning. Do the snapshots actually hold the
content a user would need to recover, and does pruning protect the one backup
that must never be deleted?

Run:  python3 tests/test_ext_08_backups.py
"""

import contextlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
from tools.latex_draft import load_state, main, read_changelog  # noqa: E402,F401

SENTENCE = "Small problems are worth studying."
INTRO = "\\section{Introduction}"
DOC = (
    "\\documentclass{article}\n"
    "\\title{A Small Paper}\n"
    "\\begin{document}\n"
    f"{INTRO}\n"
    f"{SENTENCE}\n"
    "\\end{document}\n"
)


class Base(unittest.TestCase):
    """Fresh temp project per test; cli() runs the tool in-process."""

    doc = DOC

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tex = self.dir / "paper.tex"
        self.populate()

    def populate(self):
        self.tex.write_bytes(self.doc.encode("utf-8"))  # exact bytes

    def reset(self):
        for p in self.dir.iterdir():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        self.populate()

    def cli(self, *argv, tex=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([str(tex or self.tex), *argv])
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def text(self, path=None):
        return (path or self.tex).read_bytes().decode("utf-8")

    def draft_id(self):
        """Id of the draft created by the last draft-* command."""
        return re.search(r"drafted .*?\b([0-9a-f]{8})\b", self.out).group(1)


import time
from tools.latex_draft import backups_dir, list_snapshots, prune, restore_snapshot, snapshot  # noqa: E402

CHAPTER_DOC = "\\documentclass{article}\n\\begin{document}\n\\input{ch1}\n\\end{document}\n"


class SnapshotContent(Base):
    def test_per_change_snapshot_holds_the_pre_change_main_file(self):
        self.cli("draft-insert", "--after", INTRO, "--text", "Hello.")
        newest = list_snapshots(self.tex)[0]
        self.assertEqual(newest.read_bytes().decode(), DOC)

    def test_per_change_snapshot_of_a_chapter_edit_holds_the_chapters_pre_change_text(self):
        """README: 'a snapshot before every change'. For a chapter edit the
        snapshot only copies the main file, so the chapter text exists only in
        the initial backup and the changelog."""
        self.tex.write_bytes(CHAPTER_DOC.encode())
        (self.dir / "ch1.tex").write_text("Chapter one.\n")
        self.cli("status")  # full initial backup
        self.cli("draft-insert", "--file", "ch1.tex", "--after", "Chapter one.", "--text", "More.")
        newest = list_snapshots(self.tex)[0]
        copy = backups_dir(self.tex) / f"{newest.stem}__ch1.tex"
        self.assertTrue(copy.exists(), "no copy of ch1.tex was taken before the chapter edit")
        self.assertEqual(copy.read_text(), "Chapter one.\n")


class RestoreSnapshot(Base):
    def setUp(self):
        super().setUp()
        self.tex.write_bytes(CHAPTER_DOC.encode())
        (self.dir / "ch1.tex").write_text("Chapter one.\n")

    def test_restores_main_file_and_chapters_to_the_snapshotted_content(self):
        snap = snapshot(self.tex, "full", full=True)
        self.tex.write_text("scribble\n")
        (self.dir / "ch1.tex").write_text("scribble\n")
        restore_snapshot(self.tex, snap)
        self.assertEqual(self.text(), CHAPTER_DOC)
        self.assertEqual((self.dir / "ch1.tex").read_text(), "Chapter one.\n")

    def test_restores_a_chapter_the_user_has_since_deleted(self):
        snap = snapshot(self.tex, "full", full=True)
        (self.dir / "ch1.tex").unlink()
        restore_snapshot(self.tex, snap)
        self.assertTrue((self.dir / "ch1.tex").is_file(), "deleted chapter was not recreated")


class Prune(Base):
    def make_snap(self, age_seconds, seq, note="", deps=()):
        bdir = backups_dir(self.tex)
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - age_seconds))
        stem = f"{stamp}-{seq:06d}"
        (bdir / f"{stem}.tex").write_text("x")
        if note:
            (bdir / f"{stem}.tex.note").write_text(note + "\n")
        for d in deps:
            (bdir / f"{stem}__{d}").write_text("dep")
        return bdir, stem

    def test_initial_full_backup_is_kept_forever_with_its_dependency_copies(self):
        bdir, stem = self.make_snap(90 * 86400, 1, "full initial backup (main file + inputs)", ["ch1.tex"])
        prune(self.tex)
        self.assertTrue((bdir / f"{stem}.tex").exists())
        self.assertTrue((bdir / f"{stem}__ch1.tex").exists())
        self.assertTrue((bdir / f"{stem}.tex.note").exists())

    def test_expired_snapshot_is_deleted_together_with_note_and_dependency_copies(self):
        self.make_snap(90 * 86400, 1, "full initial backup (main file + inputs)")
        bdir, stem = self.make_snap(40 * 86400, 2, "end of turn 3", ["ch1.tex"])
        prune(self.tex)
        self.assertEqual([p for p in bdir.iterdir() if p.name.startswith(stem)], [])

    def test_everything_from_the_last_hour_is_kept(self):
        self.make_snap(90 * 86400, 1, "full initial backup (main file + inputs)")
        for i in range(8):
            self.make_snap(60 + i * 30, 10 + i)
        prune(self.tex)
        self.assertEqual(len(list_snapshots(self.tex)), 9)


if __name__ == "__main__":
    unittest.main()
