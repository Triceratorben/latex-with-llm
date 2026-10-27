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

DEFAULT_AUTHOR = "LLM"
CHANGES_PACKAGE = r"\usepackage[draft]{changes}"
CHANGES_AUTHOR_RE = re.compile(r"\\definechangesauthor(\[[^\]]*\])?\{[^}]*\}")
# colors from the changes package palette, cycled per non-default author
AUTHOR_COLORS = ["blue", "orange", "purple", "brown", "teal", "magenta"]

BLOCK_RE = re.compile(
    r"% latex-draft:([0-9a-f]{8})\n(.*?)% latex-draft-end:\1\n?", re.DOTALL
)
SNAP_RE = re.compile(r"^(\d{8})-(\d{6})-")
INPUT_RE = re.compile(r"\\(?:input|include)(?:\[[^\]]*\])?\{([^}]+)\}")
SUBFILE_RE = re.compile(r"\\subfile\{([^}]+)\}")
IMPORT_RE = re.compile(r"\\import\{([^}]+)\}\{([^}]+)\}")


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
    """Files pulled in via \\input/\\include, resolved transitively (a chapter
    that itself \\input's a subsection is followed), relative to the main
    file. Cycles are tolerated."""
    deps: list[Path] = []
    seen: set[Path] = {tex.resolve()}
    queue = [tex]
    while queue:
        current = queue.pop(0)
        try:
            text = current.read_text(encoding="utf-8")
        except OSError:
            continue
        names: list[tuple[Path, str]] = []
        for match in INPUT_RE.finditer(text):
            names.append((current.parent, match.group(1).strip()))
        for match in SUBFILE_RE.finditer(text):
            names.append((current.parent, match.group(1).strip()))
        for match in IMPORT_RE.finditer(text):
            # \import{dir}{file}: the file lives in dir, inputs relative to it
            names.append((current.parent / match.group(1).strip(), match.group(2).strip()))
        for base, name in names:
            candidate = base / name
            if not candidate.suffix:
                candidate = candidate.with_suffix(".tex")
            resolved = candidate.resolve()
            if not candidate.is_file() or resolved in seen:
                continue
            seen.add(resolved)
            deps.append(candidate)
            queue.append(candidate)
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


def author_line(author: str, color: str | None = None) -> str:
    if color:
        return rf"\definechangesauthor[color={color},name={author}]{{{author}}}"
    return rf"\definechangesauthor[name={author}]{{{author}}}"


def ensure_changes_package(text: str, author: str = DEFAULT_AUTHOR) -> str:
    """Inject \\usepackage[draft]{changes} plus a \\definechangesauthor for the
    given author (colored when it is not the default, so alternatives are
    distinguishable in the PDF). Idempotent."""
    lines = text.split("\n")
    have_pkg = any(
        re.search(r"\\usepackage(\[[^\]]*\])?\{changes\}", line) for line in lines
    )
    have_author = any(
        re.search(rf"\\definechangesauthor(\[[^\]]*\])?\{{{re.escape(author)}}}", line)
        for line in lines
    )
    if have_pkg and have_author:
        return text
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("\\documentclass"):
            insert_at = i + 1
            break
    if not have_pkg:
        # The changes package must be loaded before any \definechangesauthor.
        lines[insert_at:insert_at] = [CHANGES_PACKAGE]
        insert_at += 1
    if not have_author:
        # Author definitions go after the changes package, never before it.
        for i, line in enumerate(lines):
            if re.search(r"\\usepackage(\[[^\]]*\])?\{changes\}", line):
                insert_at = i + 1
                break
        lines.insert(insert_at, author_line(author, _author_color(text, author)))
    return "\n".join(lines)


def ensure_document_preamble(tex: Path, state: dict, author: str, margin: bool = False) -> None:
    """Make sure the *main* file loads the changes package (draft mode) and
    defines the author, so that drafts written into any \\input/\\include
    file can use \\added/\\deleted/\\definechangesauthor commands.

    Included files cannot carry the preamble, so this runs before a draft is
    placed in a non-main file. Idempotent; recorded through the changelog.
    """
    text = tex.read_text(encoding="utf-8")
    new_text = ensure_changes_package(text, author)
    if margin:
        new_text = ensure_todonotes_package(new_text)
    if new_text == text:
        return
    mutate(tex, state, f"ensure preamble for drafts (author {author})", lambda t: new_text)


def _author_color(text: str, author: str) -> str | None:
    """A stable color per non-default author, based on how many other
    non-default authors are already defined (cycles through the palette)."""
    if author == DEFAULT_AUTHOR:
        return None
    others = [
        m.group(1)
        for m in re.finditer(
            r"\\definechangesauthor(?:\[[^\]]*\])?\{([^}]+)\}", text
        )
        if m.group(1) != author
    ]
    return AUTHOR_COLORS[len(others) % len(AUTHOR_COLORS)]


def split_paragraphs(text: str) -> list[str]:
    r"""Split drafted text into paragraphs at blank lines.

    The `changes` package calls \addcontentsline with the \added/\deleted
    argument, and that breaks on a blank line (or a \par token) inside the
    argument: "Paragraph ended before \addcontentsline was complete".
    Multi-paragraph drafts are therefore emitted as one \added/\deleted
    command per paragraph, separated by a blank line (valid between
    commands, invalid inside one).
    """
    return re.split(r"\n[ \t]*\n", text)


def build_block(
    did: str,
    old: str | None = None,
    new: str | None = None,
    author: str = DEFAULT_AUTHOR,
) -> str:
    parts = [f"% latex-draft:{did}"]
    if old is not None:
        for para in split_paragraphs(old):
            parts.append(rf"\deleted[id={author}]{{{para}}}")
            parts.append("")
    if new is not None:
        for para in split_paragraphs(new):
            parts.append(rf"\added[id={author}]{{{para}}}")
            parts.append("")
    parts.append(f"% latex-draft-end:{did}")
    return "\n".join(parts)


def build_comment_block(did: str, text: str, margin: bool = False) -> str:
    """A commenter-mode draft that never alters the body text.

    Default is a footnote; with margin=True a todonotes margin note, which is
    safe inside captions, section titles, tables and math (a footnote is not)
    and does not shift pagination.
    """
    cmd = rf"\todo{{{text}}}" if margin else rf"\footnote{{{text}}}"
    return "\n".join([f"% latex-draft:{did}", cmd, f"% latex-draft-end:{did}"])


def build_preamble_block(did: str, text: str) -> str:
    """A preamble draft block: raw LaTeX between marker comments, NOT
    wrapped in \\added/\\deleted.

    Preamble commands (\\renewcommand, \\setlength, \\captionsetup, ...)
    cannot go through the changes package: \\added typesets its argument
    immediately (\"Missing \\begin{document}\" in the preamble), expands it
    with \\protected@edef (freezing counter references like \\Roman{section}
    at definition time), and opens a group (so body-level \\def is discarded).
    The raw text is still marked, backed up, changelogged, and tracked as a
    draft — accepting merges it verbatim, rejecting removes it. The PDF
    review shows the effect (e.g. Roman numeral section headings).
    """
    return "\n".join([f"% latex-draft:{did}", text, f"% latex-draft-end:{did}"])


def find_preamble_end(text: str) -> int:
    """Line index (0-based) of \\begin{document} — the end of the preamble.
    Raises DraftError when the document has no \\begin{document}."""
    for i, line in enumerate(text.split("\n")):
        if line.strip().startswith("\\begin{document}"):
            return i
    raise DraftError("no \\begin{document} found — cannot locate the preamble")


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
            if injected_preamble_line(line) or re.fullmatch(
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
        while pos < len(lines) and injected_preamble_line(lines[pos]):
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
    # A draft block always occupies whole lines: ensure the line
    # before the block ends with a newline (it does, since we split
    # on "\n"), and ensure the line after the block starts fresh.
    block_lines = block.split("\n")
    # When inserting at EOF (pos == len(lines)), the block is the
    # last content; make sure it ends with a newline so the end
    # marker is on its own line.
    if pos >= len(lines) and block_lines and block_lines[-1] != "":
        block_lines.append("")
    lines[pos:pos] = block_lines
    return "\n".join(lines)


def extract_command_contents(block: str, cmd: str) -> list[str]:
    r"""Contents of every \cmd{...} in a block.

    A multi-paragraph draft emits one \added/\deleted command per
    paragraph (the changes package cannot take a blank line inside one
    argument); this returns them all so they can be rejoined.
    """
    contents: list[str] = []
    pos = 0
    while True:
        m = re.search(rf"\\{cmd}(?:\[[^\]]*\])?\{{", block[pos:])
        if not m:
            return contents
        depth = 1
        i = pos + m.end()
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
                    contents.append("".join(out))
                    pos = i + 1
                    break
            out.append(ch)
            i += 1
        else:
            raise DraftError(f"unbalanced braces in \\{cmd} block")


def extract_command_content(block: str, cmd: str) -> str | None:
    r"""All \cmd{...} contents joined with blank lines (multi-paragraph
    drafts are split into one command per paragraph; this reassembles
    them). None when the block has no such command."""
    contents = extract_command_contents(block, cmd)
    if not contents:
        return None
    return "\n\n".join(contents)


def injected_preamble_line(line: str) -> bool:
    """Whether a preamble line was auto-injected by latex-draft (the changes
    package in draft mode, or any \\definechangesauthor)."""
    stripped = line.strip()
    if stripped == CHANGES_PACKAGE:
        return True
    return bool(CHANGES_AUTHOR_RE.fullmatch(stripped))


def strip_changes_package_if_unused(text: str) -> str:
    """Remove the auto-injected preamble lines once no draft markers remain,
    so the document is clean for submission (some classes reject the changes
    package in final mode)."""
    if "% latex-draft:" in text:
        return text
    lines = [
        line
        for line in text.split("\n")
        if not injected_preamble_line(line)
        and not (
            re.fullmatch(r"\\usepackage(\[[^\]]*\])?\{todonotes\}", line.strip())
            and not re.search(r"\\todo\b", text)
        )
    ]
    return "\n".join(lines)


def resolve_drafts(text: str, ids: set[str], accept: bool) -> tuple[str, set[str]]:
    found: set[str] = set()
    # Sibling alternatives draft over the same original text: each sibling
    # block repeats the same \\deleted{...} span. Resolving must splice the
    # result in only ONCE for the whole group, not once per sibling —
    # otherwise rejecting duplicates the restored text and accepting leaks
    # the other alternatives' additions. Group siblings by their deleted
    # span: the first block of a group performs the splice, the rest
    # collapse to nothing.
    done_groups: set[str] = set()

    def repl(m: re.Match) -> str:
        did = m.group(1)
        if did not in ids:
            return m.group(0)
        found.add(did)
        # BLOCK_RE consumes the trailing newline after the end marker
        # (the \n? in the pattern). When the match is immediately followed
        # by another block (a sibling), the next block starts right after;
        # when it is followed by a blank line (the original paragraph break
        # that was behind the replaced text), that blank line survives.
        # Re-attach the newline only when the following text does not start
        # with one, so a reject restores the original line structure.
        had_nl = m.group(0).endswith("\n")
        # A preamble draft block: raw text between the markers, no
        # \\added/\\deleted. The block occupies whole lines, so reject
        # removes the match entirely (like a pure insert draft); accept
        # merges the raw text verbatim, keeping the end-marker's newline.
        if not re.search(r"\\(added|deleted|footnote|todo)\b", m.group(2)):
            if not accept:
                return ""
            return m.group(2).strip("\n") + ("\n" if had_nl else "")
        content = extract_command_content(m.group(2), "added" if accept else "deleted")
        if content is not None:
            deleted = extract_command_content(m.group(2), "deleted")
            if deleted is not None:
                # A replace/delete draft: siblings share the deleted span.
                # Splice only for the first sibling of the group; siblings
                # collapse to nothing. When a sibling collapses, also drop
                # a single blank line that separated it from the previous
                # block, so the original paragraph structure is restored.
                key = "del:" + deleted
                if key in done_groups:
                    return ""
                done_groups.add(key)
            if content:
                return content + ("\n" if had_nl else "")
            return ""
        for cmd in ("footnote", "todo"):
            content = extract_command_content(m.group(2), cmd)
            if content is not None:
                if not accept:
                    return ""
                out = rf"\\{cmd}{{{content}}}"
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


def mutate(tex: Path, state: dict, note: str, fn, target: Path | None = None) -> Path:
    """Apply fn to a file of the document: snapshot, changelog, verify, write.

    `target` is the file being edited (defaults to the main file `tex`).
    The changelog always belongs to the main file. The hash re-check
    immediately before the write guards against the user editing any file of
    the document while this change was being prepared (mid-turn race).
    """
    target = target or tex
    before_text = target.read_text(encoding="utf-8")
    snap = snapshot(tex, note)
    new_text = fn(before_text)
    assert_no_external_change(tex, state)
    entry = {
        "kind": "change",
        "note": note,
        "turn": state.get("turn", 0),
        "backup": snap.name,
        "file": target.name,
        "before_sha256": sha256(before_text),
        "after_sha256": sha256(new_text),
        "before": before_text,
        "after": new_text,
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)
    append_changelog(tex, entry)
    state["file_hashes"] = current_hashes(tex)
    return snap


# ---------------------------------------------------------------- commands


def cmd_draft_insert(tex: Path, state: dict, args: argparse.Namespace) -> int:
    did = new_id(state)
    author = getattr(args, "author", None) or DEFAULT_AUTHOR
    target = resolve_target(tex, getattr(args, "file", None))
    if target is None:
        return 2
    if target != tex:
        ensure_document_preamble(tex, state, author)

    def fn(text: str) -> str:
        if target == tex:
            text = ensure_changes_package(text, author)
        return insert_block(
            text,
            build_block(did, new=args.text, author=author),
            after=args.after,
            before=args.before,
            at_line=args.at_line,
        )

    try:
        mutate(tex, state, f"draft insert {did}", fn, target=target)
    except DraftError as e:
        print(f"latex-draft: {e}", file=sys.stderr)
        return 2
    state["drafts"][did] = draft_record(did, "insert", args.text)
    state["drafts"][did]["file"] = target.name
    state["drafts"][did]["author"] = author
    save_state(tex, state)
    print(f"latex-draft: drafted insert {did} in {target.name}")
    return 0


def resolve_target(tex: Path, file_arg: str | None) -> Path | None:
    """Resolve the --file option to an editable file of the document.

    Without --file the main file is edited. With --file, the file must be
    one of the main file's \\input/\\include dependencies (so anchors and
    uniqueness checks stay within a real part of the document).
    """
    if not file_arg:
        return tex
    candidate = (tex.parent / file_arg).resolve()
    if not candidate.is_file():
        print(f"latex-draft: no such file: {candidate}", file=sys.stderr)
        return None
    if candidate == tex:
        return tex
    deps = {d.resolve() for d in tex_inputs(tex)}
    if candidate not in deps:
        print(
            f"latex-draft: {file_arg} is not the main file and not one of its "
            f"\\input/\\include dependencies ({', '.join(sorted(d.name for d in tex_inputs(tex))) or 'none'})",
            file=sys.stderr,
        )
        return None
    return candidate


def _replace_or_delete(tex: Path, state: dict, args: argparse.Namespace, op: str) -> int:
    did = new_id(state)
    old = args.old
    new = getattr(args, "new", None)
    author = getattr(args, "author", None) or DEFAULT_AUTHOR
    target = resolve_target(tex, getattr(args, "file", None))
    if target is None:
        return 2
    if target != tex:
        ensure_document_preamble(tex, state, author)
    text = target.read_text(encoding="utf-8")

    def fn(text: str) -> str:
        if target == tex:
            text = ensure_changes_package(text, author)
        # Draft over the visible occurrence of the old text. If a pending
        # draft already replaced it, draft an *alternative* as a sibling
        # block right after the existing one — not nested inside it: the
        # changes package cannot compile nested \added/\deleted blocks
        # (brace mismatch), and nesting would make the alternatives
        # indistinguishable in the PDF. The host check must come first:
        # the old text still occurs exactly once, inside the pending block.
        matches = list(BLOCK_RE.finditer(text))
        host = None
        for m in matches:
            if old in m.group(2):
                host = m  # innermost pending block containing the old text
        if host is not None:
            block = build_block(did, old=old, new=new, author=author)
            # BLOCK_RE consumes the trailing newline after the host's end
            # marker, so host.end() is already at a line boundary. A blank
            # line right after the host (the original paragraph break that
            # was displaced when the first block replaced text inline) must
            # stay AFTER all sibling blocks, not be split by them: skip it
            # when placing the sibling.
            insert_at = host.end()
            rest = text[insert_at:]
            m_blank = re.match(r"\n(?:[ \t]*\n)?", rest)
            blank = m_blank.group(0) if m_blank else ""
            insert_at += len(blank)
            rest = text[insert_at:]
            # The end marker must never glue to a following line (e.g.
            # \end{document} at EOF): ensure a newline after the block
            # when the rest does not start with one.
            end_sep = "" if (rest.startswith("\n") or not rest) else "\n"
            return text[:insert_at] + block + end_sep + rest
        if text.count(old) == 1:
            return text.replace(old, build_block(did, old=old, new=new, author=author))
        raise DraftError(
            "--old text matched 0 times (not in the document, not in any "
            "pending draft block)"
        )

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
        mutate(tex, state, f"draft {op} {did}", fn, target=target)
    except DraftError as e:
        print(f"latex-draft: {e}", file=sys.stderr)
        return 2
    preview = new if op == "replace" else old
    state["drafts"][did] = draft_record(did, op, preview)
    state["drafts"][did]["file"] = target.name
    state["drafts"][did]["author"] = author
    save_state(tex, state)
    print(f"latex-draft: drafted {op} {did} in {target.name}")
    return 0


def cmd_draft_replace(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _replace_or_delete(tex, state, args, "replace")


def cmd_draft_delete(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _replace_or_delete(tex, state, args, "delete")


def cmd_draft_comment(tex: Path, state: dict, args: argparse.Namespace) -> int:
    did = new_id(state)
    author = getattr(args, "author", None) or DEFAULT_AUTHOR
    margin = getattr(args, "margin", False)
    target = resolve_target(tex, getattr(args, "file", None))
    if target is None:
        return 2
    if target != tex:
        ensure_document_preamble(tex, state, author, margin=margin)

    def fn(text: str) -> str:
        if target == tex:
            text = ensure_changes_package(text, author)
            if margin:
                text = ensure_todonotes_package(text)
        return insert_block(
            text,
            build_comment_block(did, args.text, margin=margin),
            after=args.after,
            before=args.before,
            at_line=args.at_line,
        )

    try:
        mutate(tex, state, f"draft comment {did}", fn, target=target)
    except DraftError as e:
        print(f"latex-draft: {e}", file=sys.stderr)
        return 2
    state["drafts"][did] = draft_record(did, "comment", args.text)
    state["drafts"][did]["file"] = target.name
    state["drafts"][did]["author"] = author
    state["drafts"][did]["margin"] = margin
    save_state(tex, state)
    print(f"latex-draft: drafted comment {did} in {target.name}")
    return 0


def ensure_todonotes_package(text: str) -> str:
    """Inject \\usepackage{todonotes} if missing (for margin-note comments).

    Must run *after* the changes package is in place: todonotes is loaded by
    changes in draft mode, and a duplicate or earlier load breaks the author
    definitions that follow."""
    if re.search(r"\\usepackage(\[[^\]]*\])?\{todonotes\}", text):
        return text
    lines = text.split("\n")
    insert_at = None
    for i, line in enumerate(lines):
        if re.search(r"\\usepackage(\[[^\]]*\])?\{changes\}", line):
            insert_at = i + 1
            break
    if insert_at is None:
        raise DraftError(
            "todonotes needs the changes package; add \\usepackage[draft]{changes} first"
        )
    lines.insert(insert_at, r"\usepackage{todonotes}")
    return "\n".join(lines)


def cmd_draft_preamble(tex: Path, state: dict, args: argparse.Namespace) -> int:
    r"""Draft a preamble change: raw LaTeX inserted into the preamble of the
    main file, between marker comments but NOT wrapped in \added/\deleted.

    Preamble commands (\renewcommand, \setlength, \captionsetup, font or
    geometry changes, ...) cannot go through the changes package: \added
    typesets its argument immediately ("Missing \begin{document}" in the
    preamble), expands it with \protected@edef (freezing counter references
    like \Roman{section} at definition time), and opens a group (so
    body-level \def is discarded). The raw text is still marked, backed up,
    changelogged, and tracked as a draft — accepting merges it verbatim,
    rejecting removes it. The PDF review shows the effect (e.g. Roman numeral
    section headings).
    """
    did = new_id(state)
    position = args.position
    text_arg = args.text

    def fn(text: str) -> str:
        lines = text.split("\n")
        bd = find_preamble_end(text)
        if position == "start":
            pos = 0
            for i, line in enumerate(lines):
                if line.startswith("\\documentclass"):
                    pos = i + 1
                    break
        elif position == "end":
            pos = bd
        else:
            # Search preamble lines only (before \begin{document}), skipping
            # lines inside pending draft blocks: an anchor must refer to a
            # line of the user's own preamble.
            hits = []
            in_block = False
            for i, line in enumerate(lines):
                if i >= bd:
                    break
                if re.fullmatch(r"% latex-draft:[0-9a-f]{8}", line):
                    in_block = True
                if in_block:
                    if re.fullmatch(r"% latex-draft-end:[0-9a-f]{8}", line):
                        in_block = False
                    continue
                if position in line:
                    hits.append(i)
            if len(hits) != 1:
                raise DraftError(
                    f"anchor {position!r} matched {len(hits)} preamble lines "
                    "(need exactly 1)"
                )
            pos = hits[0] + 1
        block_lines = build_preamble_block(did, text_arg).split("\n")
        lines[pos:pos] = block_lines
        return "\n".join(lines)

    try:
        mutate(tex, state, f"draft preamble {did}", fn)
    except DraftError as e:
        print(f"latex-draft: {e}", file=sys.stderr)
        return 2
    state["drafts"][did] = draft_record(did, "preamble", text_arg)
    state["drafts"][did]["file"] = tex.name
    state["drafts"][did]["preamble"] = True
    save_state(tex, state)
    print(f"latex-draft: drafted preamble change {did} in {tex.name}")
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

    # Drafts may live in different files of the document; resolve each group
    # in its own file. The changes package is only auto-injected into (and
    # stripped from) the main file.
    by_file: dict[str, list[str]] = {}
    for did in ids:
        fname = state["drafts"][did].get("file", tex.name)
        by_file.setdefault(fname, []).append(did)

    # Alternatives are sibling blocks over the same original text. Accepting
    # one resolves its siblings: they describe a change to text that no
    # longer exists, so remove their blocks and mark them rejected (the user
    # sees what was discarded; rejecting them afterwards is a clean error).
    # All siblings (accepted and orphaned) go into the same resolve_drafts
    # call so the group-dedup logic in resolve_drafts collapses the shared
    # span correctly.
    sibling_ids: set[str] = set()
    if accept:
        for fname, file_ids in by_file.items():
            target = tex if fname == tex.name else tex.parent / fname
            sibling_text = target.read_text(encoding="utf-8")
            accepted_old: set[str] = set()
            for did in file_ids:
                for m in BLOCK_RE.finditer(sibling_text):
                    if m.group(1) == did:
                        deleted = extract_command_content(m.group(2), "deleted")
                        if deleted is not None:
                            accepted_old.add(deleted)
            for did, rec in state["drafts"].items():
                if (
                    did in ids
                    or rec["status"] != "pending"
                    or rec.get("file", tex.name) != fname
                ):
                    continue
                m = None
                for cand in BLOCK_RE.finditer(sibling_text):
                    if cand.group(1) == did:
                        m = cand
                        break
                if m is None:
                    continue
                deleted = extract_command_content(m.group(2), "deleted")
                if deleted is not None and deleted in accepted_old:
                    sibling_ids.add(did)

    for fname, file_ids in by_file.items():
        target = tex if fname == tex.name else tex.parent / fname
        # Resolve the requested drafts and their orphaned siblings in one
        # pass: resolve_drafts deduplicates sibling groups by their shared
        # deleted span, so the original text is restored/merged exactly once.
        all_ids = set(file_ids)
        if accept:
            all_ids |= {
                d
                for d in sibling_ids
                if state["drafts"][d].get("file", tex.name) == fname
            }

        def fn(text: str, file_ids=all_ids) -> str:
            new_text, found = resolve_drafts(text, file_ids, accept)
            missing = file_ids - found
            if missing:
                raise DraftError(
                    f"draft(s) not found in {fname}: " + ", ".join(sorted(missing))
                )
            if target == tex:
                return strip_changes_package_if_unused(new_text)
            return new_text

        try:
            mutate(tex, state, f"{verb} {','.join(file_ids)}", fn, target=target)
        except DraftError as e:
            print(f"latex-draft: {e}", file=sys.stderr)
            return 2
    for did in ids:
        state["drafts"][did]["status"] = verb + "ed"
        state["drafts"][did]["resolved"] = datetime.now().isoformat(timespec="seconds")
    now = datetime.now().isoformat(timespec="seconds")
    for did in sibling_ids:
        rec = state["drafts"][did]
        rec["status"] = "rejected"
        rec["resolved"] = now
        rec["orphaned_by_overlap"] = True
    # Once no draft markers remain anywhere in the document, strip the
    # auto-injected preamble from the main file (some classes reject the
    # changes package in final mode).
    if not any(rec["status"] == "pending" for rec in state["drafts"].values()):
        def strip_fn(text: str) -> str:
            return strip_changes_package_if_unused(text)

        mutate(tex, state, "strip unused changes preamble", strip_fn)
    save_state(tex, state)
    note = ""
    if sibling_ids:
        note = (
            f"; overlapping draft(s) {', '.join(sorted(sibling_ids))} "
            "resolved as rejected"
        )
    print(f"latex-draft: {verb}ed {len(ids)} draft(s): {', '.join(ids)}{note}")
    return 0


def cmd_accept(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _resolve(tex, state, args, accept=True)


def cmd_reject(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _resolve(tex, state, args, accept=False)


def run_latex(tex: Path) -> tuple[int, str]:
    """Compile the document. Returns (exit_code, log_tail)."""
    latexmk = shutil.which("latexmk")
    pdflatex = shutil.which("pdflatex")
    if latexmk:
        cmd = [latexmk, "-pdf", "-interaction=nonstopmode", "-halt-on-error", tex.name]
        runs = 1
    elif pdflatex:
        cmd = [pdflatex, "-interaction=nonstopmode", tex.name]
        runs = 2
    else:
        return 0, ""
    proc = None
    for _ in range(runs):
        proc = subprocess.run(cmd, cwd=tex.parent, capture_output=True, text=True)
    log = tex.parent / (tex.stem + ".log")
    tail = ""
    if log.exists():
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-30:])
    return (proc.returncode if proc else 1), tail


def cmd_compile(tex: Path, state: dict, args: argparse.Namespace) -> int:
    # End of turn: take the per-turn backup and advance the turn counter.
    snapshot(tex, f"end of turn {state.get('turn', 0) + 1}")
    state["turn"] = state.get("turn", 0) + 1
    state["file_hashes"] = current_hashes(tex)
    save_state(tex, state)
    if not (shutil.which("latexmk") or shutil.which("pdflatex")):
        print(
            "latex-draft: no LaTeX engine found (latexmk/pdflatex); "
            "skipping compile",
            file=sys.stderr,
        )
        return 0
    code, tail = run_latex(tex)
    if code != 0:
        print(
            f"latex-draft: compile failed (exit {code}); "
            f"last log lines from {tex.stem}.log:",
            file=sys.stderr,
        )
        print(tail, file=sys.stderr)
        print(
            "latex-draft: run `repair` to analyze the errors and fix them",
            file=sys.stderr,
        )
        return 1
    pdf = tex.parent / (tex.stem + ".pdf")
    print(f"latex-draft: compiled {pdf.name}")
    return 0


# ---------------------------------------------------------------- repair

UNDEFINED_CONTROL_RE = re.compile(
    # TeX reports "! Undefined control sequence." then either
    # "<recently read> \cmd" or "l.NN <source line with \cmd>" — the command
    # may sit anywhere on that line, not necessarily at the start.
    r"! Undefined control sequence\.\s*\n"
    r"(?:<recently read>\s*)?\n?"
    r"(?:l\.(\d+)\s*)?"
    r"[^\n]*?(\\[a-zA-Z@]+)"
)
UNDEFINED_ENV_RE = re.compile(r"! LaTeX Error: Environment (\S+) undefined\.")
MISSING_FILE_RE = re.compile(r"! LaTeX Error: File `([^']+)' not found\.")
MISSING_BEGEND_RE = re.compile(
    r"! LaTeX Error: \\begin\{([^}]+)\}(?: on input line \d+)? ended by \\end\{([^}]+)\}\."
)  # modern LaTeX adds "on input line N"; older formats do not
RUNAWAY_RE = re.compile(r"! File ended while scanning use of \\([a-zA-Z]+)\.")
EMERGENCY_STOP_RE = re.compile(r"! Emergency stop\.")


def analyze_log(tex: Path) -> list[dict]:
    """Extract actionable errors from the LaTeX log."""
    log = tex.parent / (tex.stem + ".log")
    if not log.exists():
        return []
    text = log.read_text(errors="replace")
    problems: list[dict] = []
    for m in UNDEFINED_CONTROL_RE.finditer(text):
        command = (m.group(2) or "").lstrip("\\")
        line = int(m.group(1)) if m.group(1) else None
        problems.append({
            "kind": "undefined-control",
            "command": command,
            "line": line,
            "detail": m.group(0).splitlines()[0],
        })
    for m in UNDEFINED_ENV_RE.finditer(text):
        problems.append({
            "kind": "undefined-environment",
            "environment": m.group(1),
            "detail": m.group(0).splitlines()[0],
        })
    for m in MISSING_FILE_RE.finditer(text):
        problems.append({
            "kind": "missing-file",
            "file": m.group(1),
            "detail": m.group(0).splitlines()[0],
        })
    for m in MISSING_BEGEND_RE.finditer(text):
        problems.append({
            "kind": "begin-end-mismatch",
            "begin": m.group(1),
            "end": m.group(2),
            "detail": m.group(0).splitlines()[0],
        })
    for m in RUNAWAY_RE.finditer(text):
        problems.append({
            "kind": "runaway-argument",
            "command": m.group(1),
            "detail": m.group(0).splitlines()[0],
        })
    if EMERGENCY_STOP_RE.search(text) and not problems:
        problems.append({"kind": "emergency-stop", "detail": "! Emergency stop."})
    return problems


def find_line_in_files(tex: Path, needle: str) -> tuple[Path, int] | None:
    """Find the first file (main or input) containing `needle`; returns
    (file, 1-based line number)."""
    for f in watched_files(tex):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            if needle in line:
                return f, i
    return None


def cmd_repair(tex: Path, state: dict, args: argparse.Namespace) -> int:
    r"""Repair mode: analyze the last failed compile and fix what can be
    fixed safely.

    Repairs are applied as normal drafted changes (marked, backed up,
    changelogged) so the user reviews them in the PDF. Only safe, mechanical
    fixes are attempted:
    - an \end{X} closed by \begin{Y} (or vice versa) -> correct the \end.
    - undefined control sequence from a draft block (the changes package is
      missing) -> ensure the changes preamble in the main file.
    Everything else is reported as a suggestion for the agent/user.
    """
    problems = analyze_log(tex)
    if not problems:
        code, tail = run_latex(tex)
        if code == 0:
            print(f"latex-draft: nothing to repair; {tex.stem}.pdf compiles")
            return 0
        problems = analyze_log(tex)
        if not problems:
            print(
                "latex-draft: compile failed but no known error pattern was "
                f"found in {tex.stem}.log; last log lines:",
                file=sys.stderr,
            )
            print(tail, file=sys.stderr)
            return 1
    print(f"latex-draft: found {len(problems)} problem(s) in {tex.stem}.log:")
    for p in problems:
        print(f"  - [{p['kind']}] {p['detail']}")
    fixed = 0
    suggestions: list[str] = []
    for p in problems:
        if p["kind"] == "begin-end-mismatch":
            # TeX reports "\begin{X} ended by \end{Y}": the \end is wrong,
            # but a document may contain several \end{Y}; fix the one whose
            # line number is closest to (and not before) the position TeX
            # stopped at, preferring the last unmatched \begin{X}.
            begin_hits = [
                (f, i)
                for f in watched_files(tex)
                for i, line in enumerate(
                    f.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                )
                if rf"\begin{{{p['begin']}}}" in line
            ]
            end_hits = [
                (f, i)
                for f in watched_files(tex)
                for i, line in enumerate(
                    f.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                )
                if rf"\end{{{p['end']}}}" in line
            ]
            if begin_hits and end_hits:
                # pair them up in order; the mismatch is the first unpaired \end
                target, lineno = end_hits[len(begin_hits) - 1] if len(end_hits) >= len(begin_hits) else end_hits[-1]
                wrong = "\\end{" + p["end"] + "}"
                right = "\\end{" + p["begin"] + "}"
                # A repair must compile on its own, so it may not rely on the
                # changes package: patch the source directly. The change is
                # still snapshotted and changelogged (so it can be undone).
                mutate(
                    tex,
                    state,
                    f"repair: {wrong} -> {right} "
                    f"in {target.name}:{lineno}",
                    lambda text: text.replace(wrong, right, 1),
                    target=target,
                )
                fixed += 1
                continue
            suggestions.append(f"fix the mismatched \\end{{{p['end']}}} by hand")
        elif p["kind"] == "undefined-control" and p.get("command") in (
            "added", "deleted", "todo",
        ):
            # A changes-package command without the package: inject the
            # preamble into the main file (included files cannot carry it).
            mutate(
                tex,
                state,
                "repair: inject changes package preamble",
                ensure_changes_package,
            )
            fixed += 1
            continue
        elif p["kind"] == "undefined-control" and p.get("command") == "footnote":
            # \footnote outside a float/paragraph context, or a draft comment
            # left behind after the preamble was stripped: the command itself
            # is standard LaTeX, so this is a real error — suggest, don't fix.
            suggestions.append(
                "\\footnote is undefined here (it is not allowed in this "
                "context, e.g. inside a caption or a section title); move it "
                "or use a margin note (draft-comment --margin)"
            )
        elif p["kind"] == "undefined-control" and p.get("command") in (
            "definechangesauthor",
        ):
            # An author definition before the changes package: move the
            # package load above it.
            mutate(
                tex,
                state,
                "repair: load changes package before \\definechangesauthor",
                ensure_changes_package,
            )
            fixed += 1
            continue
        elif p["kind"] == "undefined-control":
            suggestions.append(
                f"undefined \\{p.get('command', '?')} (log line {p.get('line', '?')}): "
                "check for a typo or a missing \\usepackage"
            )
        elif p["kind"] == "undefined-environment":
            suggestions.append(
                f"undefined environment '{p['environment']}': check for a typo "
                "or a missing \\usepackage / \\newenvironment"
            )
        elif p["kind"] == "missing-file":
            suggestions.append(
                f"missing file '{p['file']}': create it, fix the path, or "
                "remove the \\input/\\include"
            )
        elif p["kind"] == "runaway-argument":
            suggestions.append(
                f"runaway argument of \\{p['command']}: an opening brace is "
                "missing its closing brace"
            )
        else:
            suggestions.append(p["detail"])
    for s in suggestions:
        print(f"latex-draft: SUGGESTION: {s}")
    if fixed:
        code, tail = run_latex(tex)
        if code == 0:
            pdf = tex.parent / (tex.stem + ".pdf")
            print(f"latex-draft: repair applied; compiled {pdf.name}")
            return 0
        print(
            f"latex-draft: repair applied but compile still fails; "
            f"remaining problems:",
            file=sys.stderr,
        )
        for p in analyze_log(tex):
            print(f"  - [{p['kind']}] {p['detail']}", file=sys.stderr)
        return 1
    print("latex-draft: no automatic repair available; see suggestions above")
    return 1


def cmd_backup(tex: Path, state: dict, args: argparse.Namespace) -> int:
    snap = snapshot(tex, args.note or "manual backup")
    state["file_hashes"] = current_hashes(tex)
    save_state(tex, state)
    print(f"latex-draft: backup -> {snap}")
    return 0


def cmd_undo(tex: Path, state: dict, args: argparse.Namespace) -> int:
    r"""Undo changes using the changelog.

    `undo --n K` reverts the K most recent changes by replaying their
    recorded before-content. `undo --turn` reverts every change made in the
    current turn (the turn is ended by `compile`, which bumps the counter).

    Undo is refuse-on-conflict: if a file no longer matches the state after
    the change being undone (because of later edits or hand edits), that
    change is skipped with a warning instead of overwriting newer work.
    `--force` restores the recorded content anyway.
    """
    force = getattr(args, "force", False)
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
    skipped: list[str] = []
    done_entries: list[dict] = []
    # replaying before-content is a whole-file restore, so undoing several
    # changes only works back-to-back without intervening edits: track the
    # expected content as we go (each undone change must match the
    # before-state of the change undone right after it)
    expected: dict[str, str] = {}
    for entry in reversed(targets):
        fname = entry.get("file", tex.name)
        target = tex if fname == tex.name else tex.parent / fname
        current = target.read_text(encoding="utf-8")
        want = expected.get(fname, entry.get("after_sha256"))
        if sha256(current) != want:
            if not force:
                skipped.append(fname)
                print(
                    f"latex-draft: skipped '{entry.get('note', '?')}' in "
                    f"{fname}: the file changed after it (conflict); "
                    "use --force to restore the recorded content anyway",
                    file=sys.stderr,
                )
                continue
            print(
                f"latex-draft: WARNING: {fname} no longer matches the "
                f"state after '{entry.get('note', '?')}' (hand edits?); "
                "restoring recorded content anyway (--force)",
                file=sys.stderr,
            )
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(entry["before"], encoding="utf-8")
        os.replace(tmp, target)
        done_entries.append(entry)
        expected[fname] = entry.get("before_sha256", "")
        append_changelog(
            tex,
            {
                "kind": "undo",
                "note": f"undo: {entry.get('note', '?')}",
                "turn": state.get("turn", 0),
                "undid_backup": entry.get("backup", ""),
                "file": fname,
                "before_sha256": entry.get("after_sha256", ""),
                "after_sha256": entry.get("before_sha256", ""),
            },
        )
    state["file_hashes"] = current_hashes(tex)
    save_state(tex, state)
    if not done_entries:
        print("latex-draft: nothing undone (all changes conflicted)",
              file=sys.stderr)
        return 1
    what = f"turn {state.get('turn', 0)}" if args.turn else f"{len(done_entries)} change(s)"
    files = sorted({e.get("file", tex.name) for e in done_entries})
    msg = f"latex-draft: undid {what}; restored {', '.join(files)}"
    if skipped:
        msg += f"; skipped {len(skipped)} conflicting change(s)"
    print(msg)
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
        fname = e.get("file", "")
        where = f"  [{fname}]" if fname and fname != tex.name else ""
        print(f"{i:>4}  turn {turn:<3} {e.get('kind', '?'):<7} {note}{where}")
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
    inputs = tex_inputs(tex)
    if inputs:
        print(f"inputs:  {', '.join(d.name for d in inputs)}")
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


def cmd_check(tex: Path, state: dict, args: argparse.Namespace) -> int:
    """Pre-submission check: the document must be clean of draft markup.

    Reports, across the main file and all input files:
    - pending drafts (must be accepted or rejected first)
    - stray `% latex-draft:` markers (a draft block the tool no longer knows)
    - leftover auto-injected preamble lines (changes/todonotes packages and
      \\definechangesauthor) once nothing is pending — some classes (revtex,
      achemso) reject them in final mode
    Exit code is 0 when clean, 1 when problems were found.
    """
    problems: list[str] = []
    pending = [
        did for did, rec in state["drafts"].items() if rec["status"] == "pending"
    ]
    if pending:
        problems.append(
            f"{len(pending)} pending draft(s): {', '.join(sorted(pending))} "
            "(accept or reject them first)"
        )
    for f in watched_files(tex):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        stray = len(re.findall(r"% latex-draft(-end)?:[0-9a-f]{8}", text))
        if stray:
            problems.append(f"{f.name}: {stray} stray latex-draft marker line(s)")
    if not pending:
        main_text = tex.read_text(encoding="utf-8")
        leftovers = [
            line.strip()
            for line in main_text.split("\n")
            if injected_preamble_line(line)
        ]
        if leftovers:
            problems.append(
                f"{tex.name}: leftover auto-injected preamble line(s): "
                + "; ".join(leftovers)
            )
    if problems:
        print(f"latex-draft: check FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"latex-draft: check OK — no pending drafts, no stray markers, "
          "no leftover preamble")
    return 0


HANDLERS = {
    "draft-insert": cmd_draft_insert,
    "draft-replace": cmd_draft_replace,
    "draft-delete": cmd_draft_delete,
    "draft-comment": cmd_draft_comment,
    "draft-preamble": cmd_draft_preamble,
    "list": cmd_list,
    "accept": cmd_accept,
    "reject": cmd_reject,
    "compile": cmd_compile,
    "repair": cmd_repair,
    "backup": cmd_backup,
    "undo": cmd_undo,
    "backups": cmd_backups,
    "changelog": cmd_changelog,
    "status": cmd_status,
    "check": cmd_check,
}


# ---------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latex-draft",
        description="Turn-based, draft-marked editing of LaTeX documents.",
    )
    parser.add_argument("tex", type=Path, help="path to the main .tex file")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_file_option(p, parser):
        parser.add_argument(
            "--file",
            default=None,
            help="edit this \\input/\\include/\\subfile/\\import file of the "
            "document instead of the main file (path relative to the main file)",
        )

    def add_author_option(p, parser):
        parser.add_argument(
            "--author",
            default=None,
            help="changes-package author label for this draft (default: LLM); "
            "non-default authors get a distinct color so alternatives are "
            "distinguishable in the PDF",
        )

    p = sub.add_parser("draft-insert", help="insert a drafted text at an anchor")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--after", help="unique line content to insert after")
    g.add_argument("--before", help="unique line content to insert before")
    g.add_argument("--at-line", type=int, help="1-based line number to insert before")
    p.add_argument("--text", required=True, help="LaTeX text to insert (drafted)")
    add_file_option(p, p)
    add_author_option(p, p)

    p = sub.add_parser("draft-replace", help="draft replacing existing text")
    p.add_argument("--old", required=True, help="text to replace (must occur exactly once)")
    p.add_argument("--new", required=True, help="replacement LaTeX text")
    add_file_option(p, p)
    add_author_option(p, p)

    p = sub.add_parser("draft-delete", help="draft deleting existing text")
    p.add_argument("--old", required=True, help="text to delete (must occur exactly once)")
    add_file_option(p, p)
    add_author_option(p, p)

    p = sub.add_parser(
        "draft-comment",
        help="commenter mode: add a drafted comment at an anchor "
        "(never alters the body text)",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--after", help="unique line content to attach the comment to")
    g.add_argument("--before", help="unique line content to attach the comment to")
    g.add_argument("--at-line", type=int, help="1-based line number to attach to")
    p.add_argument("--text", required=True, help="LaTeX comment text")
    p.add_argument(
        "--margin",
        action="store_true",
        help="use a todonotes margin note instead of a footnote (safe inside "
        "captions, section titles, tables and math; no pagination shift)",
    )
    add_file_option(p, p)
    add_author_option(p, p)

    p = sub.add_parser(
        "draft-preamble",
        help="draft a preamble change (raw LaTeX, not changes-marked; "
        "for \\renewcommand, \\setlength, \\captionsetup, font/geometry, ...)",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--start",
        action="store_const",
        const="start",
        dest="position",
        help="insert right after \\documentclass (start of preamble)",
    )
    g.add_argument(
        "--end",
        action="store_const",
        const="end",
        dest="position",
        help="insert right before \\begin{document} (end of preamble)",
    )
    g.add_argument(
        "--after",
        dest="position",
        help="unique preamble line content to insert after",
    )
    p.add_argument("--text", required=True, help="LaTeX preamble code to insert (drafted)")

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

    sub.add_parser(
        "repair",
        help="after a failed compile: analyze the log, apply safe fixes as "
        "drafted changes, and report suggestions for the rest",
    )

    p = sub.add_parser("backup", help="take a manual backup snapshot")
    p.add_argument("--note", default="", help="note stored with the snapshot")

    p = sub.add_parser("undo", help="undo changes, recorded in the changelog")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--n", type=int, default=1, help="how many recent changes to undo")
    g.add_argument(
        "--turn", action="store_true", help="undo all changes of the current turn"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="restore recorded content even when the file changed after the "
        "change (default: refuse on conflict)",
    )

    sub.add_parser("backups", help="list backup snapshots")

    p = sub.add_parser("changelog", help="show the change log")
    p.add_argument("--last", type=int, default=20, help="show only the last N entries")

    sub.add_parser("status", help="show document, draft and backup status")

    sub.add_parser(
        "check",
        help="pre-submission check: no pending drafts, no stray markers, "
        "no leftover changes/todonotes preamble",
    )
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
        "draft-preamble",
        "accept",
        "reject",
        "undo",
    ):
        state["file_hashes"] = current_hashes(tex)
        save_state(tex, state)
    return rc


if __name__ == "__main__":
    sys.exit(main())
