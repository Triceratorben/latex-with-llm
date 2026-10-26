#!/usr/bin/env python3
"""latex-draft: turn-based, draft-marked editing for LaTeX documents.

Drafts are inserted into the .tex file wrapped in `changes`-package markup
(\\added / \\deleted) and bracketed by marker comments, so a PDF viewer shows
exactly what would change. Drafts are later accepted (merged without markup)
or rejected (original text restored).

Every change is recorded in an append-only changelog (JSONL) with the content
before and after, so any change can be undone individually (`undo`,
`undo --turn`) or inspected (`changelog`). A full backup snapshot — the main
file plus every file it pulls in via \\input/\\include — is taken on first use,
and a per-turn snapshot is taken when a turn ends with `compile`.

The tool is robust against the user editing the .tex file by hand: every
command first checks whether the file (or any of its inputs) changed on disk
since the last tool action, and every mutation re-checks right before writing
and aborts on a conflict instead of clobbering the user's edits.

Every turn should end with `compile` so the PDF updates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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

BLOCK_RE = re.compile(
    r"% latex-draft:([0-9a-f]{8})\n(.*?)% latex-draft-end:\1\n?", re.DOTALL
)
SNAP_RE = re.compile(r"^(\d{8})-(\d{6})-")
INPUT_RE = re.compile(r"\\(?:input|include)(?:\[[^\]]*\])?\{([^}]+)\}")


class DraftError(Exception):
    pass


# ---------------------------------------------------------------- state


def state_path(tex: Path) -> Path:
    return tex.parent / (tex.name + ".latex-draft.json")


def changelog_path(tex: Path) -> Path:
    return tex.parent / (tex.name + ".latex-draft-changes.jsonl")


def backups_dir(tex: Path) -> Path:
    return tex.parent / ".latex-draft-backups" / tex.name


def load_state(tex: Path) -> dict:
    path = state_path(tex)
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if "last_backup" in state:  # migrate from the 5-minute-backup era
                del state["last_backup"]
            state.setdefault("drafts", {})
            state.setdefault("file_hashes", {})
            state.setdefault("turn", 0)
            state.setdefault("full_backup_done", False)
            return state
        except json.JSONDecodeError:
            pass
    return {
        "drafts": {},
        "file_hashes": {},
        "turn": 0,
        "full_backup_done": False,
    }


def save_state(tex: Path, state: dict) -> None:
    state_path(tex).write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def new_id(state: dict) -> str:
    while True:
        did = uuid.uuid4().hex[:8]
        if did not in state["drafts"]:
            return did


# ------------------------------------------------------- changelog & hashes


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tex_inputs(tex: Path) -> list[Path]:
    """Files pulled in via \\input/\\include (relative to the main file)."""
    deps: list[Path] = []
    try:
        text = tex.read_text(encoding="utf-8")
    except OSError:
        return deps
    for match in INPUT_RE.finditer(text):
        name = match.group(1).strip()
        candidate = tex.parent / name
        if not candidate.suffix:
            candidate = candidate.with_suffix(".tex")
        if candidate.is_file() and candidate.resolve() != tex.resolve():
            deps.append(candidate)
    return deps


def watched_files(tex: Path) -> list[Path]:
    return [tex, *tex_inputs(tex)]


def current_hashes(tex: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for f in watched_files(tex):
        try:
            hashes[str(f)] = sha256(f.read_text(encoding="utf-8"))
        except OSError:
            hashes[str(f)] = "<unreadable>"
    return hashes


def append_changelog(tex: Path, entry: dict) -> None:
    entry = {"time": datetime.now().isoformat(timespec="seconds"), **entry}
    with changelog_path(tex).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def read_changelog(tex: Path) -> list[dict]:
    path = changelog_path(tex)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # tolerate a torn final line
    return entries


def check_external_changes(tex: Path, state: dict) -> str | None:
    """Return a warning string if any watched file changed on disk since the
    last tool action (i.e. the user edited it by hand between turns)."""
    recorded = state.get("file_hashes") or {}
    current = current_hashes(tex)
    changed = []
    for path, digest in sorted(current.items()):
        if path not in recorded:
            changed.append(Path(path).name + " (new input file)")
        elif recorded[path] != digest:
            changed.append(Path(path).name)
    for path in sorted(recorded):
        if path not in current:
            changed.append(Path(path).name + " (input file removed)")
    if changed:
        return "file(s) changed outside latex-draft: " + ", ".join(changed)
    return None


def check_and_adopt_external(tex: Path, state: dict) -> str | None:
    """Turn-start check: detect hand edits made since the last tool action.

    Warns, records the external change in the changelog, and adopts the new
    on-disk state so the tool continues from reality instead of failing. The
    write-time re-check in `mutate` still guards the compute window, so an
    edit made *while* a change is being prepared aborts that change.
    """
    if not state.get("file_hashes"):
        state["file_hashes"] = current_hashes(tex)
        save_state(tex, state)
        return None
    warning = check_external_changes(tex, state)
    if not warning:
        return None
    append_changelog(
        tex,
        {
            "kind": "external",
            "note": warning,
            "turn": state.get("turn", 0),
            "before_sha256": "",
            "after_sha256": "",
        },
    )
    state["file_hashes"] = current_hashes(tex)
    save_state(tex, state)
    print(f"latex-draft: WARNING: {warning}", file=sys.stderr)
    print(
        "latex-draft: the file was edited by hand; it was recorded in the "
        "changelog and adopted. Re-read the file before drafting further "
        "changes (`status`, `changelog`).",
        file=sys.stderr,
    )
    return warning


def assert_no_external_change(tex: Path, state: dict) -> None:
    """Re-verify hashes right before writing, so a hand edit made while the
    tool is computing a change is never clobbered."""
    warning = check_external_changes(tex, state)
    if warning:
        raise DraftError(
            f"aborting, {warning}; the file changed while this change was "
            "being prepared — re-run the command"
        )


# ---------------------------------------------------------------- backups


def list_snapshots(tex: Path) -> list[Path]:
    bdir = backups_dir(tex)
    if not bdir.is_dir():
        return []
    # newest first; skip dependency copies of full backups (name__dep.tex)
    snaps = [p for p in bdir.glob("*.tex") if "__" not in p.stem]
    return sorted(snaps, reverse=True)


def snapshot(tex: Path, note: str = "", full: bool = False) -> Path:
    bdir = backups_dir(tex)
    bdir.mkdir(parents=True, exist_ok=True)
    seq = 0
    for existing in bdir.glob("*-*.tex"):
        m = re.search(r"-(\d+)\.tex$", existing.name)
        if m:
            seq = max(seq, int(m.group(1)))
    dest = bdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{seq + 1:06d}.tex"
    shutil.copy2(tex, dest)
    if full:
        for dep in tex_inputs(tex):
            dep_dest = bdir / f"{dest.stem}__{dep.name}"
            shutil.copy2(dep, dep_dest)
    if note:
        dest.with_suffix(dest.suffix + ".note").write_text(note + "\n")
    if not full:
        prune(tex)
    return dest


def restore_snapshot(tex: Path, snap: Path) -> None:
    """Restore a snapshot; also restores any dependency files stored in it."""
    bdir = backups_dir(tex)
    shutil.copy2(snap, tex)
    for dep_snap in bdir.glob(f"{snap.stem}__*.tex"):
        name = dep_snap.name[len(snap.stem) + 2 :]
        target = tex.parent / name
        if target.is_file():
            shutil.copy2(dep_snap, target)


def prune(tex: Path) -> None:
    """Keep the full initial backup forever; for the rest: all snapshots <1h
    old, one per hour for 24h, one per day for 30d."""
    bdir = backups_dir(tex)
    snaps = list_snapshots(tex)
    now = time.time()
    keep: set[Path] = set()
    hours: set[str] = set()
    days: set[str] = set()
    for s in snaps:  # newest first
        note_file = s.with_suffix(s.suffix + ".note")
        if note_file.exists() and "initial backup" in note_file.read_text():
            keep.add(s)
            continue
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
            for dep_snap in bdir.glob(f"{s.stem}__*.tex"):
                dep_snap.unlink()


def ensure_full_backup(tex: Path, state: dict) -> None:
    """On first use of the tool on a file, back up the main file and every
    file it pulls in via \\input/\\include, so a full restore is possible."""
    if state.get("full_backup_done"):
        return
    snapshot(tex, "full initial backup (main file + inputs)", full=True)
    state["full_backup_done"] = True
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


def extract_top_command_contents(block: str, cmd: str) -> list[str]:
    """Top-level \\cmd{...} contents in a block. Alternatives drafted over the
    same text nest, so only the outermost commands are real alternatives."""
    matches = list(re.finditer(rf"\\{cmd}(?:\[[^\]]*\])?\{{", block))
    spans = [(m.start(), m.end()) for m in matches]
    contents: list[str] = []
    for (s, e) in spans:
        depth = 1
        i = e
        start = i
        while i < len(block):
            ch = block[i]
            if ch == "\\" and i + 1 < len(block):
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        else:
            raise DraftError(f"unbalanced braces in \\{cmd} block")
        contents.append(block[start:i])
    return [
        content
        for (s, e), content in zip(spans, contents)
        if not any(s > s2 and e < e2 for s2, e2 in spans)
    ]


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


def find_orphaned_drafts(text: str, state: dict) -> list[str]:
    """Pending drafts whose marker block no longer exists in the document.

    This happens when two drafts overlap (e.g. two alternative versions of
    the same replacement) and one is accepted: the accepted block consumed
    both, so the sibling draft has nothing left to resolve.
    """
    present = {m.group(1) for m in BLOCK_RE.finditer(text)}
    return [
        did
        for did, rec in state["drafts"].items()
        if rec["status"] == "pending" and did not in present
    ]


def _added_for_draft(block: str) -> str | None:
    """The \\added text belonging to the outer draft of a (possibly nested)
    block: a top-level \\added that is not part of a nested draft block."""
    for m in re.finditer(r"\\added(?:\[[^\]]*\])?\{", block):
        nested_before = block.count("% latex-draft:", 0, m.start())
        nested_after = block.count("% latex-draft-end:", 0, m.start())
        if nested_before > nested_after:
            continue  # inside a nested draft block
        depth = 1
        i = m.end()
        start = i
        while i < len(block):
            ch = block[i]
            if ch == "\\" and i + 1 < len(block):
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return block[start:i]
            i += 1
        raise DraftError("unbalanced braces in \\added block")
    return None


def resolve_drafts(text: str, ids: set[str], accept: bool) -> tuple[str, set[str]]:
    found: set[str] = set()

    def repl(m: re.Match) -> str:
        did = m.group(1)
        if did not in ids:
            return m.group(0)
        found.add(did)
        had_nl = m.group(0).endswith("\n")
        if accept:
            # An accept merges the whole block. Alternatives drafted over the
            # same text nest inside each other; accepting one alternative
            # means keeping its own added text and dropping the others.
            tops = extract_top_command_contents(m.group(2), "added")
            if len(tops) == 1:
                return tops[0] + ("\n" if had_nl else "")
            if len(tops) > 1:
                own = _added_for_draft(m.group(2))
                if own is not None:
                    return own + ("\n" if had_nl else "")
                return ""
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
    """Apply fn to the document: snapshot, changelog, verify, write.

    The hash re-check immediately before the write guards against the user
    editing the file while this change was being prepared (mid-turn race).
    """
    before_text = tex.read_text(encoding="utf-8")
    snap = snapshot(tex, note)
    new_text = fn(before_text)
    lines = new_text.split("\n")
    if lines and lines[0] in (CHANGES_PACKAGE, CHANGES_AUTHOR):
        raise DraftError("insertion landed before \\documentclass")
    assert_no_external_change(tex, state)
    entry = {
        "kind": "change",
        "note": note,
        "turn": state.get("turn", 0),
        "backup": snap.name,
        "before_sha256": sha256(before_text),
        "after_sha256": sha256(new_text),
        "before": before_text,
        "after": new_text,
    }
    tmp = tex.with_suffix(tex.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, tex)
    append_changelog(tex, entry)
    state["file_hashes"] = current_hashes(tex)
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

    def fn(text: str) -> str:
        text = ensure_changes_package(text)
        # Draft over the visible occurrence of the old text. If a pending
        # draft already replaced it, the visible text no longer contains it;
        # draft over the original inside the outermost pending draft block.
        if text.count(old) == 1:
            return text.replace(old, build_block(did, old=old, new=new))
        matches = list(BLOCK_RE.finditer(text))
        # innermost pending block containing the old text (a block drafted
        # over an already-drafted replacement nests inside the outer block)
        host = None
        for m in matches:
            if old in m.group(2):
                host = m  # later matches are nested inside earlier ones
        if host is None:
            raise DraftError(
                "--old text matched 0 times (not in the document, not in any "
                "pending draft block)"
            )
        inner = host.group(2).replace(old, build_block(did, old=old, new=new), 1)
        return text[: host.start(2)] + inner + text[host.end(2) :]

    visible = BLOCK_RE.sub("", text)
    if visible.count(old) > 1:
        print(
            f"latex-draft: --old text matched {visible.count(old)} times "
            "outside draft blocks (need exactly 1)",
            file=sys.stderr,
        )
        return 2
    if visible.count(old) == 0 and text.count(old) == 0:
        print(
            "latex-draft: --old text not found in the document",
            file=sys.stderr,
        )
        return 2

    try:
        mutate(tex, state, f"draft {op} {did}", fn)
    except DraftError as e:
        print(f"latex-draft: {e}", file=sys.stderr)
        return 2
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
    # A draft whose block was consumed by resolving an overlapping draft is
    # resolved as well: its text is already gone (reject) or merged (accept).
    if accept:
        orphans = find_orphaned_drafts(tex.read_text(encoding="utf-8"), state)
        orphans = [d for d in orphans if d not in ids]
        for did in orphans:
            state["drafts"][did]["status"] = "rejected"
            state["drafts"][did]["resolved"] = datetime.now().isoformat(
                timespec="seconds"
            )
            state["drafts"][did]["orphaned_by_overlap"] = True
    for did in ids:
        state["drafts"][did]["status"] = verb + "ed"
        state["drafts"][did]["resolved"] = datetime.now().isoformat(timespec="seconds")
    save_state(tex, state)
    note = ""
    if accept and orphans:
        note = f"; overlapping draft(s) {', '.join(orphans)} resolved as rejected"
    print(f"latex-draft: {verb}ed {len(ids)} draft(s): {', '.join(ids)}{note}")
    return 0


def cmd_accept(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _resolve(tex, state, args, accept=True)


def cmd_reject(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _resolve(tex, state, args, accept=False)


def cmd_compile(tex: Path, state: dict, args: argparse.Namespace) -> int:
    # End of turn: take the per-turn backup and advance the turn counter.
    snapshot(tex, f"end of turn {state.get('turn', 0) + 1}")
    state["turn"] = state.get("turn", 0) + 1
    state["file_hashes"] = current_hashes(tex)
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
    state["file_hashes"] = current_hashes(tex)
    save_state(tex, state)
    print(f"latex-draft: backup -> {snap}")
    return 0


def cmd_undo(tex: Path, state: dict, args: argparse.Namespace) -> int:
    """Undo changes using the changelog.

    `undo --n K` reverts the K most recent changes by replaying their
    recorded before-content. `undo --turn` reverts every change made in the
    current turn (the turn is ended by `compile`, which bumps the counter).
    """
    entries = [e for e in read_changelog(tex) if e.get("kind") == "change"]
    if args.turn:
        current_turn = state.get("turn", 0)
        # undo the current turn if it has changes, otherwise the most recent
        # turn that does (the common case: user says "undo" after compile)
        targets = [e for e in entries if e.get("turn", 0) == current_turn]
        if not targets:
            done = sorted({e.get("turn", 0) for e in entries})
            if not done:
                print("latex-draft: changelog is empty, nothing to undo",
                      file=sys.stderr)
                return 1
            targets = [e for e in entries if e.get("turn", 0) == done[-1]]
        # an undo already performed reverts its target; skip entries whose
        # effect is already undone so `undo --turn` cannot loop
        last_undo = next(
            (e for e in reversed(read_changelog(tex)) if e.get("kind") == "undo"),
            None,
        )
        if last_undo is not None:
            undid = last_undo.get("undid_backup")
            targets = [e for e in targets if e.get("backup") != undid]
            if not targets:
                print(
                    f"latex-draft: turn {done[-1]} is already undone "
                    "(last action was `undo`)"
                )
                return 0
        n = len(targets)
    else:
        if not entries:
            print("latex-draft: changelog is empty, nothing to undo",
                  file=sys.stderr)
            return 1
        n = max(1, min(args.n, len(entries)))
        targets = entries[-n:]
    for entry in reversed(targets):
        current = tex.read_text(encoding="utf-8")
        if sha256(current) != entry.get("after_sha256"):
            print(
                f"latex-draft: WARNING: {tex.name} no longer matches the "
                f"state after '{entry.get('note', '?')}' (hand edits?); "
                "restoring recorded content anyway",
                file=sys.stderr,
            )
        tmp = tex.with_suffix(tex.suffix + ".tmp")
        tmp.write_text(entry["before"], encoding="utf-8")
        os.replace(tmp, tex)
        append_changelog(
            tex,
            {
                "kind": "undo",
                "note": f"undo: {entry.get('note', '?')}",
                "turn": state.get("turn", 0),
                "undid_backup": entry.get("backup", ""),
                "before_sha256": entry.get("after_sha256", ""),
                "after_sha256": entry.get("before_sha256", ""),
            },
        )
    state["file_hashes"] = current_hashes(tex)
    save_state(tex, state)
    what = f"turn {state.get('turn', 0)}" if args.turn else f"{n} change(s)"
    print(f"latex-draft: undid {what}; {tex.name} restored")
    return 0


def cmd_changelog(tex: Path, state: dict, args: argparse.Namespace) -> int:
    entries = read_changelog(tex)
    if not entries:
        print("latex-draft: changelog is empty")
        return 0
    start = max(0, len(entries) - args.last)
    for i, e in enumerate(entries[start:], start=start + 1):
        note = e.get("note", "?")
        turn = e.get("turn", 0)
        print(f"{i:>4}  turn {turn:<3} {e.get('kind', '?'):<7} {note}")
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
    drafts = state["drafts"]
    pending = sum(1 for r in drafts.values() if r["status"] == "pending")
    snaps = list_snapshots(tex)
    changes = len([e for e in read_changelog(tex) if e.get("kind") == "change"])
    pdf = tex.parent / (tex.stem + ".pdf")
    print(f"file:    {tex}")
    print(f"turn:    {state.get('turn', 0)}")
    print(f"drafts:  {len(drafts)} total, {pending} pending")
    print(f"changes: {changes} recorded in changelog")
    print(f"backups: {len(snaps)}" + (f", latest {snaps[0].name}" if snaps else ""))
    print(f"pdf:     {pdf.name if pdf.exists() else 'not built'}")
    warning = check_external_changes(tex, state)
    if warning:
        print(f"WARNING: {warning}")
        print("         the file was edited outside latex-draft")
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
    "changelog": cmd_changelog,
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

    p = sub.add_parser("undo", help="undo changes, recorded in the changelog")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--n", type=int, default=1, help="how many recent changes to undo")
    g.add_argument(
        "--turn", action="store_true", help="undo all changes of the current turn"
    )

    sub.add_parser("backups", help="list backup snapshots")

    p = sub.add_parser("changelog", help="show the change log")
    p.add_argument("--last", type=int, default=20, help="show only the last N entries")

    sub.add_parser("status", help="show document, draft and backup status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tex = args.tex.expanduser().resolve()
    if not tex.is_file():
        print(f"latex-draft: no such file: {tex}", file=sys.stderr)
        return 2
    state = load_state(tex)
    # Turn-start safety: make sure a full backup exists, and check whether the
    # user edited the file (or its inputs) by hand since the last tool action.
    ensure_full_backup(tex, state)
    check_and_adopt_external(tex, state)
    rc = HANDLERS[args.command](tex, state, args)
    if rc == 0 and args.command in (
        "draft-insert",
        "draft-replace",
        "draft-delete",
        "draft-comment",
        "accept",
        "reject",
        "undo",
    ):
        state["file_hashes"] = current_hashes(tex)
        save_state(tex, state)
    return rc


if __name__ == "__main__":
    sys.exit(main())
