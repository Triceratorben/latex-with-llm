#!/usr/bin/env python3
"""latex-draft: turn-based, draft-marked editing for LaTeX documents.

Drafts are inserted into the .tex file wrapped in `changes`-package markup
(\\added / \\deleted) and bracketed by marker comments, so a PDF viewer shows
exactly what would change. Drafts are later accepted (merged without markup)
or rejected (original text restored).

Every mutation is preceded by a timestamped backup snapshot. A snapshot is
also taken automatically at most every 5 minutes. `undo` restores the most
recent snapshot(s). Every turn should end with `compile` so the PDF updates.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

AUTHOR = "LLM"
CHANGES_PACKAGE = r"\usepackage[draft]{changes}"
CHANGES_AUTHOR = rf"\definechangesauthor[name={AUTHOR}]{{{AUTHOR}}}"
BACKUP_INTERVAL_S = 300  # automatic snapshot at most every 5 minutes

BLOCK_RE = re.compile(
    r"% latex-draft:([0-9a-f]{8})\n(.*?)% latex-draft-end:\1\n?", re.DOTALL
)
SNAP_RE = re.compile(r"^(\d{8})-(\d{6})-")


class DraftError(Exception):
    pass


# ---------------------------------------------------------------- state


def state_path(tex: Path) -> Path:
    return tex.parent / (tex.name + ".latex-draft.json")


def backups_dir(tex: Path) -> Path:
    return tex.parent / ".latex-draft-backups" / tex.name


def load_state(tex: Path) -> dict:
    path = state_path(tex)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"drafts": {}, "last_backup": time.time()}


def save_state(tex: Path, state: dict) -> None:
    state_path(tex).write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def new_id(state: dict) -> str:
    while True:
        did = uuid.uuid4().hex[:8]
        if did not in state["drafts"]:
            return did


# ---------------------------------------------------------------- backups


def list_snapshots(tex: Path) -> list[Path]:
    bdir = backups_dir(tex)
    if not bdir.is_dir():
        return []
    return sorted(bdir.glob("*.tex"), reverse=True)  # newest first


def snapshot(tex: Path, note: str = "") -> Path:
    bdir = backups_dir(tex)
    bdir.mkdir(parents=True, exist_ok=True)
    seq = 0
    for existing in bdir.glob("*-*.tex"):
        m = re.search(r"-(\d+)\.tex$", existing.name)
        if m:
            seq = max(seq, int(m.group(1)))
    dest = bdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{seq + 1:06d}.tex"
    shutil.copy2(tex, dest)
    if note:
        dest.with_suffix(dest.suffix + ".note").write_text(note + "\n")
    prune(tex)
    return dest


def prune(tex: Path) -> None:
    """Keep all snapshots <1h old, one per hour for 24h, one per day for 30d."""
    snaps = list_snapshots(tex)
    now = time.time()
    keep: set[Path] = set()
    hours: set[str] = set()
    days: set[str] = set()
    for s in snaps:  # newest first
        m = SNAP_RE.match(s.name)
        if not m:
            keep.add(s)
            continue
        t = time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
        age = now - t
        if age < 3600:
            keep.add(s)
            continue
        hb = time.strftime("%Y%m%d-%H", time.localtime(t))
        db = time.strftime("%Y%m%d", time.localtime(t))
        if age < 86400 and hb not in hours:
            hours.add(hb)
            keep.add(s)
            continue
        if age < 30 * 86400 and db not in days:
            days.add(db)
            keep.add(s)
    for s in snaps:
        if s not in keep:
            s.unlink()
            note = s.with_suffix(s.suffix + ".note")
            if note.exists():
                note.unlink()


def maybe_periodic_backup(tex: Path, state: dict) -> None:
    if time.time() - state.get("last_backup", 0.0) >= BACKUP_INTERVAL_S:
        snapshot(tex, "automatic 5-minute backup")
        state["last_backup"] = time.time()
        save_state(tex, state)


# ---------------------------------------------------------------- drafts


def ensure_changes_package(text: str) -> str:
    if re.search(r"\\usepackage(\[[^\]]*\])?\{changes\}", text):
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("\\documentclass"):
            lines.insert(i + 1, CHANGES_PACKAGE)
            lines.insert(i + 2, CHANGES_AUTHOR)
            return "\n".join(lines)
    return CHANGES_PACKAGE + "\n" + CHANGES_AUTHOR + "\n" + text


def build_block(did: str, old: str | None = None, new: str | None = None) -> str:
    parts = [f"% latex-draft:{did}"]
    if old is not None:
        parts.append(rf"\deleted[author={AUTHOR}]{{{old}}}")
    if new is not None:
        parts.append(rf"\added[author={AUTHOR}]{{{new}}}")
    parts.append(f"% latex-draft-end:{did}")
    return "\n".join(parts)


def build_comment_block(did: str, text: str) -> str:
    """A commenter-mode draft: a footnote that never alters the body text."""
    return "\n".join(
        [f"% latex-draft:{did}", rf"\footnote{{{text}}}", f"% latex-draft-end:{did}"]
    )


def insert_block(
    text: str,
    block: str,
    after: str | None = None,
    before: str | None = None,
    at_line: int | None = None,
) -> str:
    lines = text.split("\n")
    if at_line is not None:
        # The user numbers lines of their own document. Skip the auto-injected
        # changes preamble and any pending draft blocks, which are not part of
        # the visible document yet: pos ends up just before the at_line-th
        # visible line.
        pos = 0
        visible = 0
        in_block = False
        while pos < len(lines) and visible < at_line - 1:
            line = lines[pos]
            if in_block:
                if re.fullmatch(r"% latex-draft-end:[0-9a-f]{8}", line):
                    in_block = False
                pos += 1
                continue
            if line in (CHANGES_PACKAGE, CHANGES_AUTHOR) or re.fullmatch(
                r"% latex-draft:[0-9a-f]{8}", line
            ):
                if re.fullmatch(r"% latex-draft:[0-9a-f]{8}", line):
                    in_block = True
                pos += 1
                continue
            visible += 1
            pos += 1
        pos = min(pos, len(lines))
        # never insert before \documentclass or split the injected preamble
        dc = max(
            (i for i, l in enumerate(lines) if l.startswith("\\documentclass")),
            default=-1,
        )
        pos = max(pos, dc + 1)
        while pos < len(lines) and lines[pos] in (CHANGES_PACKAGE, CHANGES_AUTHOR):
            pos += 1
    else:
        needle = after if after is not None else before
        hits = [i for i, line in enumerate(lines) if needle in line]
        if len(hits) != 1:
            raise DraftError(
                f"anchor {needle!r} matched {len(hits)} lines (need exactly 1)"
            )
        pos = hits[0] + 1 if after is not None else hits[0]
    block_start: int | None = None
    for i, line in enumerate(lines):
        if block_start is None and re.fullmatch(r"% latex-draft:[0-9a-f]{8}", line):
            block_start = i
        elif block_start is not None and re.fullmatch(
            r"% latex-draft-end:[0-9a-f]{8}", line
        ):
            if block_start < pos <= i:
                raise DraftError(
                    f"insertion point is inside existing draft block "
                    f"(lines {block_start + 1}-{i + 1})"
                )
            block_start = None
    lines[pos:pos] = block.split("\n")
    return "\n".join(lines)


def extract_command_content(block: str, cmd: str) -> str | None:
    m = re.search(rf"\\{cmd}(?:\[[^\]]*\])?\{{", block)
    if not m:
        return None
    depth = 1
    i = m.end()
    out: list[str] = []
    while i < len(block):
        ch = block[i]
        if ch == "\\" and i + 1 < len(block):
            out.append(ch)
            out.append(block[i + 1])
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(out)
        out.append(ch)
        i += 1
    raise DraftError(f"unbalanced braces in \\{cmd} block")


def strip_changes_package_if_unused(text: str) -> str:
    """Remove the auto-injected preamble lines once no draft markers remain."""
    if "% latex-draft:" in text:
        return text
    lines = [
        line
        for line in text.split("\n")
        if line not in (CHANGES_PACKAGE, CHANGES_AUTHOR)
    ]
    return "\n".join(lines)


def resolve_drafts(text: str, ids: set[str], accept: bool) -> tuple[str, set[str]]:
    found: set[str] = set()

    def repl(m: re.Match) -> str:
        did = m.group(1)
        if did not in ids:
            return m.group(0)
        found.add(did)
        had_nl = m.group(0).endswith("\n")
        content = extract_command_content(m.group(2), "added" if accept else "deleted")
        if content is not None:
            if content:
                return content + ("\n" if had_nl else "")
            return ""
        content = extract_command_content(m.group(2), "footnote")
        if content is not None:
            if not accept:
                return ""
            out = rf"\footnote{{{content}}}"
            return out + ("\n" if had_nl else "")
        return ""

    return BLOCK_RE.subn(repl, text)[0], found


def draft_record(did: str, op: str, preview_text: str) -> dict:
    preview = " ".join(preview_text.split())[:120]
    return {
        "id": did,
        "op": op,
        "status": "pending",
        "created": datetime.now().isoformat(timespec="seconds"),
        "preview": preview,
    }


def mutate(tex: Path, state: dict, note: str, fn) -> Path:
    maybe_periodic_backup(tex, state)
    snap = snapshot(tex, note)
    text = tex.read_text(encoding="utf-8")
    lines = fn(text).split("\n")
    if lines and lines[0] in (CHANGES_PACKAGE, CHANGES_AUTHOR):
        raise DraftError("insertion landed before \\documentclass")
    tex.write_text("\n".join(lines), encoding="utf-8")
    return snap


# ---------------------------------------------------------------- commands


def cmd_draft_insert(tex: Path, state: dict, args: argparse.Namespace) -> int:
    did = new_id(state)

    def fn(text: str) -> str:
        text = ensure_changes_package(text)
        return insert_block(
            text,
            build_block(did, new=args.text),
            after=args.after,
            before=args.before,
            at_line=args.at_line,
        )

    try:
        mutate(tex, state, f"draft insert {did}", fn)
    except DraftError as e:
        print(f"latex-draft: {e}", file=sys.stderr)
        return 2
    state["drafts"][did] = draft_record(did, "insert", args.text)
    save_state(tex, state)
    print(f"latex-draft: drafted insert {did} in {tex.name}")
    return 0


def _replace_or_delete(tex: Path, state: dict, args: argparse.Namespace, op: str) -> int:
    did = new_id(state)
    old = args.old
    new = getattr(args, "new", None)
    text = tex.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(
            f"latex-draft: --old text matched {text.count(old)} times (need exactly 1)",
            file=sys.stderr,
        )
        return 2

    def fn(text: str) -> str:
        text = ensure_changes_package(text)
        return text.replace(old, build_block(did, old=old, new=new))

    mutate(tex, state, f"draft {op} {did}", fn)
    preview = new if op == "replace" else old
    state["drafts"][did] = draft_record(did, op, preview)
    save_state(tex, state)
    print(f"latex-draft: drafted {op} {did} in {tex.name}")
    return 0


def cmd_draft_replace(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _replace_or_delete(tex, state, args, "replace")


def cmd_draft_delete(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _replace_or_delete(tex, state, args, "delete")


def cmd_draft_comment(tex: Path, state: dict, args: argparse.Namespace) -> int:
    did = new_id(state)

    def fn(text: str) -> str:
        text = ensure_changes_package(text)
        return insert_block(
            text,
            build_comment_block(did, args.text),
            after=args.after,
            before=args.before,
            at_line=args.at_line,
        )

    try:
        mutate(tex, state, f"draft comment {did}", fn)
    except DraftError as e:
        print(f"latex-draft: {e}", file=sys.stderr)
        return 2
    state["drafts"][did] = draft_record(did, "comment", args.text)
    save_state(tex, state)
    print(f"latex-draft: drafted comment {did} in {tex.name}")
    return 0


def cmd_list(tex: Path, state: dict, args: argparse.Namespace) -> int:
    drafts = state["drafts"]
    if not drafts:
        print("latex-draft: no drafts")
        return 0
    for did, rec in sorted(drafts.items(), key=lambda kv: kv[1]["created"]):
        print(
            f"{did}  {rec['status']:<8}  {rec['op']:<7}  "
            f"{rec['created']}  {rec['preview']}"
        )
    return 0


def _resolve(tex: Path, state: dict, args: argparse.Namespace, accept: bool) -> int:
    verb = "accept" if accept else "reject"
    if args.all:
        ids = [d for d, r in state["drafts"].items() if r["status"] == "pending"]
        if not ids:
            print("latex-draft: no pending drafts")
            return 0
    else:
        ids = [args.id]
        for did in ids:
            rec = state["drafts"].get(did)
            if rec is None:
                print(f"latex-draft: unknown draft id {did}", file=sys.stderr)
                return 2
            if rec["status"] != "pending":
                print(
                    f"latex-draft: draft {did} is already {rec['status']}",
                    file=sys.stderr,
                )
                return 2

    def fn(text: str) -> str:
        new_text, found = resolve_drafts(text, set(ids), accept)
        missing = set(ids) - found
        if missing:
            raise DraftError(
                "draft(s) not found in document: " + ", ".join(sorted(missing))
            )
        return strip_changes_package_if_unused(new_text)

    try:
        mutate(tex, state, f"{verb} {','.join(ids)}", fn)
    except DraftError as e:
        print(f"latex-draft: {e}", file=sys.stderr)
        return 2
    for did in ids:
        state["drafts"][did]["status"] = verb + "ed"
        state["drafts"][did]["resolved"] = datetime.now().isoformat(timespec="seconds")
    save_state(tex, state)
    print(f"latex-draft: {verb}ed {len(ids)} draft(s): {', '.join(ids)}")
    return 0


def cmd_accept(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _resolve(tex, state, args, accept=True)


def cmd_reject(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _resolve(tex, state, args, accept=False)


def cmd_compile(tex: Path, state: dict, args: argparse.Namespace) -> int:
    maybe_periodic_backup(tex, state)
    save_state(tex, state)
    latexmk = shutil.which("latexmk")
    pdflatex = shutil.which("pdflatex")
    if latexmk:
        cmd = [latexmk, "-pdf", "-interaction=nonstopmode", "-halt-on-error", tex.name]
        runs = 1
    elif pdflatex:
        cmd = [pdflatex, "-interaction=nonstopmode", tex.name]
        runs = 2
    else:
        print(
            "latex-draft: no LaTeX engine found (latexmk/pdflatex); "
            "skipping compile",
            file=sys.stderr,
        )
        return 0
    proc = None
    for _ in range(runs):
        proc = subprocess.run(cmd, cwd=tex.parent, capture_output=True, text=True)
    if proc is not None and proc.returncode != 0:
        log = tex.parent / (tex.stem + ".log")
        print(
            f"latex-draft: compile failed (exit {proc.returncode}); "
            f"last log lines from {log.name}:",
            file=sys.stderr,
        )
        if log.exists():
            tail = log.read_text(errors="replace").splitlines()[-30:]
            print("\n".join(tail), file=sys.stderr)
        return 1
    pdf = tex.parent / (tex.stem + ".pdf")
    print(f"latex-draft: compiled {pdf.name}")
    return 0


def cmd_backup(tex: Path, state: dict, args: argparse.Namespace) -> int:
    snap = snapshot(tex, args.note or "manual backup")
    print(f"latex-draft: backup -> {snap}")
    return 0


def cmd_undo(tex: Path, state: dict, args: argparse.Namespace) -> int:
    snaps = list_snapshots(tex)
    if not snaps:
        print("latex-draft: no backups to restore", file=sys.stderr)
        return 1
    n = max(1, min(args.n, len(snaps)))
    target = snaps[n - 1]
    shutil.copy2(target, tex)
    for s in snaps[:n]:
        s.unlink()
    print(
        f"latex-draft: restored {tex.name} from {target.name} "
        f"(undid {n} step{'s' if n > 1 else ''})"
    )
    return 0


def cmd_backups(tex: Path, state: dict, args: argparse.Namespace) -> int:
    snaps = list_snapshots(tex)
    if not snaps:
        print("latex-draft: no backups")
        return 0
    for s in snaps:
        note_file = s.with_suffix(s.suffix + ".note")
        note = note_file.read_text().strip() if note_file.exists() else ""
        mtime = datetime.fromtimestamp(s.stat().st_mtime).isoformat(timespec="seconds")
        print(f"{s.name}  {mtime}  {note}")
    return 0


def cmd_status(tex: Path, state: dict, args: argparse.Namespace) -> int:
    maybe_periodic_backup(tex, state)
    save_state(tex, state)
    drafts = state["drafts"]
    pending = sum(1 for r in drafts.values() if r["status"] == "pending")
    snaps = list_snapshots(tex)
    pdf = tex.parent / (tex.stem + ".pdf")
    print(f"file:    {tex}")
    print(f"drafts:  {len(drafts)} total, {pending} pending")
    print(f"backups: {len(snaps)}" + (f", latest {snaps[0].name}" if snaps else ""))
    print(f"pdf:     {pdf.name if pdf.exists() else 'not built'}")
    return 0


HANDLERS = {
    "draft-insert": cmd_draft_insert,
    "draft-replace": cmd_draft_replace,
    "draft-delete": cmd_draft_delete,
    "draft-comment": cmd_draft_comment,
    "list": cmd_list,
    "accept": cmd_accept,
    "reject": cmd_reject,
    "compile": cmd_compile,
    "backup": cmd_backup,
    "undo": cmd_undo,
    "backups": cmd_backups,
    "status": cmd_status,
}


# ---------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latex-draft",
        description="Turn-based, draft-marked editing of LaTeX documents.",
    )
    parser.add_argument("tex", type=Path, help="path to the .tex file")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("draft-insert", help="insert a drafted text at an anchor")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--after", help="unique line content to insert after")
    g.add_argument("--before", help="unique line content to insert before")
    g.add_argument("--at-line", type=int, help="1-based line number to insert before")
    p.add_argument("--text", required=True, help="LaTeX text to insert (drafted)")

    p = sub.add_parser("draft-replace", help="draft replacing existing text")
    p.add_argument("--old", required=True, help="text to replace (must occur exactly once)")
    p.add_argument("--new", required=True, help="replacement LaTeX text")

    p = sub.add_parser("draft-delete", help="draft deleting existing text")
    p.add_argument("--old", required=True, help="text to delete (must occur exactly once)")

    p = sub.add_parser(
        "draft-comment",
        help="commenter mode: add a drafted footnote at an anchor "
        "(never alters the body text)",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--after", help="unique line content to attach the comment to")
    g.add_argument("--before", help="unique line content to attach the comment to")
    g.add_argument("--at-line", type=int, help="1-based line number to attach to")
    p.add_argument("--text", required=True, help="LaTeX comment text")

    sub.add_parser("list", help="list drafts and their status")

    for name, help_text in [
        ("accept", "merge draft(s) into the document, removing markup"),
        ("reject", "discard draft(s), restoring the original text"),
    ]:
        p = sub.add_parser(name, help=help_text)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--id", help="draft id (see `list`)")
        g.add_argument("--all", action="store_true", help="all pending drafts")

    sub.add_parser("compile", help="recompile the PDF (end every turn with this)")

    p = sub.add_parser("backup", help="take a manual backup snapshot")
    p.add_argument("--note", default="", help="note stored with the snapshot")

    p = sub.add_parser("undo", help="restore previous backup snapshot(s)")
    p.add_argument("--n", type=int, default=1, help="how many steps to undo")

    sub.add_parser("backups", help="list backup snapshots")
    sub.add_parser("status", help="show document, draft and backup status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tex = args.tex.expanduser().resolve()
    if not tex.is_file():
        print(f"latex-draft: no such file: {tex}", file=sys.stderr)
        return 2
    state = load_state(tex)
    return HANDLERS[args.command](tex, state, args)


if __name__ == "__main__":
    sys.exit(main())
