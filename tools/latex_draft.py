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
import configparser
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

# Per-document configuration, next to the main file.
CONFIG_NAME = ".latex-draft.cfg"
ACCEPT_STYLES = ("added", "highlight")
DEFAULT_CONFIG = {"keep_markup": False, "color": None, "style": "added"}
# xcolor knows "gray", not "grey": accept the common spellings so a config
# cannot break every compile with an undefined color.
COLOR_ALIASES = {"grey": "gray", "lightgrey": "lightgray", "darkgrey": "darkgray"}
# Accepted LLM text, kept marked so the review PDF still shows what the model
# wrote; the changes package prints it plainly in final mode.
PROVENANCE_CMD_RE = re.compile(r"\\(?:added|highlight)(?:\[[^\]]*\])?\{")
# Content that cannot sit inside \added{...}/\deleted{...}: the markup is
# ulem text markup. Environment boundaries and alignment tabs are compile
# errors ("Not in outer par mode", "Missing } inserted") that would otherwise
# surface only after drafting reported success. A plain \\ line break is
# fine inside the markup; a row break inside a table is not (see
# _alignment_depth).
UNMARKABLE_RE = re.compile(r"\\(?:begin|end)\{[^}]*\}|(?<!\\)&")
ALIGNMENT_ENVIRONMENTS = (
    "tabular", "tabular*", "tabularx", "tabulary", "array", "longtable",
)
ALIGN_BEGIN_RE = re.compile(
    r"\\begin\{(" + "|".join(map(re.escape, ALIGNMENT_ENVIRONMENTS)) + r")\}"
)
ALIGN_END_RE = re.compile(
    r"\\end\{(" + "|".join(map(re.escape, ALIGNMENT_ENVIRONMENTS)) + r")\}"
)

BLOCK_RE = re.compile(
    r"% latex-draft:([0-9a-f]{8})(?:\r\n|\r|\n)(.*?)"
    r"% latex-draft-end:\1(?:\r\n|\r|\n)?",
    re.DOTALL,
)
SNAP_RE = re.compile(r"^(\d{8})-(\d{6})-")
INPUT_RE = re.compile(r"\\(?:input|include)(?:\[[^\]]*\])?\{([^}]+)\}")
SUBFILE_RE = re.compile(r"\\subfile\{([^}]+)\}")
IMPORT_RE = re.compile(r"\\import\{([^}]+)\}\{([^}]+)\}")
EOL_RE = re.compile(r"\r\n|\r|\n")
# A marker line on its own (BLOCK_RE matches the whole block instead).
BLOCK_BEGIN_LINE_RE = re.compile(r"% latex-draft:[0-9a-f]{8}")
BLOCK_END_LINE_RE = re.compile(r"% latex-draft-end:[0-9a-f]{8}")


class DraftError(Exception):
    pass


def _fail(message: object, rc: int = 2) -> int:
    """Report a user-facing error on stderr and return the exit code."""
    print(f"latex-draft: {message}", file=sys.stderr)
    return rc


def load_config(tex: Path) -> dict:
    """Read `.latex-draft.cfg` from the main file's directory.

    ```
    [accept]
    keep_markup = true        # keep changes markup on accepted LLM text
    color = grey              # xcolor name for the LLM author
    style = highlight         # added (colored underline) or highlight
    ```

    Missing keys keep their defaults; a malformed value is a clean error, not
    a traceback (a bad color would otherwise break every compile).
    """
    config = dict(DEFAULT_CONFIG)
    path = tex.parent / CONFIG_NAME
    if not path.is_file():
        return config
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        if parser.has_section("accept"):
            section = parser["accept"]
            config["keep_markup"] = section.getboolean("keep_markup", fallback=False)
            config["color"] = section.get("color", fallback="").strip() or None
            config["style"] = section.get("style", fallback="added").strip().lower()
    except (configparser.Error, ValueError) as e:
        raise DraftError(f"{CONFIG_NAME} is invalid: {e}") from e
    if config["style"] not in ACCEPT_STYLES:
        raise DraftError(
            f"{CONFIG_NAME}: [accept] style must be one of "
            f"{', '.join(ACCEPT_STYLES)}, got {config['style']!r}"
        )
    if config["color"] and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", config["color"]):
        raise DraftError(
            f"{CONFIG_NAME}: [accept] color must be an xcolor name "
            f"(green, gray, blue, ...), got {config['color']!r}"
        )
    if config["color"]:
        config["color"] = COLOR_ALIASES.get(config["color"], config["color"])
    return config


# ---------------------------------------------------------------- state


def state_path(tex: Path) -> Path:
    return tex.parent / (tex.name + ".latex-draft.json")


def changelog_path(tex: Path) -> Path:
    return tex.parent / (tex.name + ".latex-draft-changes.jsonl")


def backups_dir(tex: Path) -> Path:
    return tex.parent / ".latex-draft-backups" / tex.name


def load_state(tex: Path) -> dict:
    path = state_path(tex)
    corrupt = False
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("state file is not a JSON object")
            if "last_backup" in state:  # migrate from the 5-minute-backup era
                del state["last_backup"]
            state.setdefault("drafts", {})
            state.setdefault("file_hashes", {})
            state.setdefault("turn", 0)
            state.setdefault("full_backup_done", False)
            state.setdefault("injected_preamble", [])
            # Hashes used to be keyed by absolute path; a copied or moved
            # project then looked hand-edited. Re-key them relative to the
            # main file's directory.
            state["file_hashes"] = {
                _rel_key(tex, Path(p)): h
                for p, h in (state.get("file_hashes") or {}).items()
            }
            return state
        except (json.JSONDecodeError, ValueError):
            corrupt = True
    state = {
        "drafts": {},
        "file_hashes": {},
        "turn": 0,
        "full_backup_done": False,
        "injected_preamble": [],
    }
    if corrupt:
        # The document still carries its draft blocks; recover them from the
        # markers instead of silently forgetting pending drafts.
        state["_recovered"] = _recover_pending_drafts(tex, state)
    return state


def save_state(tex: Path, state: dict) -> None:
    state_path(tex).write_text(
        json.dumps(
            {k: v for k, v in state.items() if not k.startswith("_")},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _recover_pending_drafts(tex: Path, state: dict) -> int:
    """Rebuild pending draft records from the marker blocks in the document.

    Used when the state file is unreadable: the blocks are the source of
    truth, so they can still be listed, accepted or rejected.
    """
    recovered = 0
    for f in watched_files(tex):
        try:
            text = read_tex(f)
        except (OSError, DraftError):
            continue
        for m in BLOCK_RE.finditer(text):
            did = m.group(1)
            if did in state["drafts"]:
                continue
            body = m.group(2)
            preview = (
                extract_command_content(body, "added")
                or extract_command_content(body, "deleted")
                or body
            )
            rec = draft_record(did, "unknown", preview)
            rec["file"] = f.name
            rec["recovered"] = True
            state["drafts"][did] = rec
            recovered += 1
    return recovered


def new_id(state: dict) -> str:
    while True:
        did = uuid.uuid4().hex[:8]
        if did not in state["drafts"]:
            return did


# ------------------------------------------------------- changelog & hashes


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_tex(path: Path) -> str:
    """Read a .tex file exactly as stored: no universal-newline translation
    (CRLF/CR endings must survive a draft -> reject cycle byte-identically)
    and a clean DraftError instead of a traceback on non-UTF-8 content."""
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except UnicodeDecodeError as e:
        raise DraftError(
            f"{path.name} is not valid UTF-8 ({e}); latex-draft only edits "
            "UTF-8 documents"
        ) from e


def write_tex(path: Path, text: str) -> None:
    """Write a .tex file without newline translation (see read_tex)."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` through a temp sibling in the same directory.

    os.replace is atomic, so a crash mid-write cannot leave the user's file
    half written.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    write_tex(tmp, text)
    os.replace(tmp, path)


def _read_or_none(path: Path) -> str | None:
    """Text of a file, or None when it is missing, unreadable or not UTF-8."""
    try:
        return read_tex(path)
    except (OSError, DraftError):
        return None


def newline_of(text: str) -> str:
    """The document's dominant line separator: "\\r\\n", "\\r" or "\\n".

    Drafts are written with the document's own separator, so a CRLF or
    CR-only document does not end up with mixed line endings after a
    draft -> accept cycle.
    """
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    if crlf and crlf >= cr and crlf >= lf:
        return "\r\n"
    if cr > lf:
        return "\r"
    return "\n"


def split_lines(text: str) -> list[str]:
    """Split into lines on any line separator (no terminators kept)."""
    return EOL_RE.split(text)


def split_eol(text: str) -> tuple[list[str], list[str]]:
    """Split into lines and their terminators.

    `len(lines) == len(eols) + 1`; eols[i] terminates lines[i]. Rebuilding
    with `join_eol` reproduces the input byte-for-byte, so a mixed-EOL file
    keeps every untouched line exactly as the user wrote it.
    """
    parts = re.split(r"(\r\n|\r|\n)", text)
    return parts[0::2], parts[1::2]


def join_eol(lines: list[str], eols: list[str]) -> str:
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        if i < len(eols):
            out.append(eols[i])
    return "".join(out)


def insert_lines_at(
    text: str, pos: int, new_lines: list[str], newline: str | None = None
) -> str:
    """Insert whole lines before line `pos`, terminated like the document.

    Untouched lines keep their own terminators byte-for-byte, so a CRLF or
    CR-only document stays that way and a mixed one is not re-encoded.
    """
    lines, eols = split_eol(text)
    nl = newline or newline_of(text)
    pos = max(0, min(pos, len(lines)))
    new_lines = list(new_lines)
    lines[pos:pos] = new_lines
    eols[pos:pos] = [nl] * len(new_lines)
    return join_eol(lines, eols)


def to_newline(text: str, newline: str) -> str:
    """Rewrite a fragment's line separators to `newline`."""
    if newline == "\n":
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


VERBATIM_BEGIN_RE = re.compile(
    r"\\begin\{(verbatim\*?|lstlisting|Verbatim|alltt)\}"
)
VERBATIM_END_RE = re.compile(
    r"\\end\{(verbatim\*?|lstlisting|Verbatim|alltt)\}"
)


def _include_relevant_text(text: str) -> str:
    """The part of a document that can pull files in: comment text stripped,
    verbatim bodies skipped. A \\input in a comment or a verbatim example is
    literal text, not an inclusion."""
    out = []
    in_verb = False
    for line in split_lines(text):
        begins = VERBATIM_BEGIN_RE.search(line)
        ends = VERBATIM_END_RE.search(line)
        if in_verb:
            if ends:
                in_verb = False
            continue
        if begins:
            in_verb = not (ends and ends.start() > begins.start())
            continue
        cut = None
        for m in re.finditer("%", line):
            nbs = 0
            j = m.start() - 1
            while j >= 0 and line[j] == "\\":
                nbs += 1
                j -= 1
            if nbs % 2 == 0:
                cut = m.start()
                break
        out.append(line[:cut] if cut is not None else line)
    return "\n".join(out)


def tex_inputs(tex: Path) -> list[Path]:
    r"""Files pulled in via \\input/\\include, resolved transitively (a chapter
    that itself \\input's a subsection is followed).

    TeX resolves those paths relative to the directory pdflatex runs in —
    the main file's directory — not relative to the including file, so a
    nested ``\input{chapters/sec}`` is looked up in ``<main dir>/chapters``.
    Cycles are tolerated.
    """
    deps: list[Path] = []
    seen: set[Path] = {tex.resolve()}
    queue = [tex]
    while queue:
        current = queue.pop(0)
        try:
            text = _include_relevant_text(read_tex(current))
        except (OSError, DraftError):
            continue
        names: list[tuple[Path, str]] = []
        for match in INPUT_RE.finditer(text):
            names.append((tex.parent, match.group(1).strip()))
        for match in SUBFILE_RE.finditer(text):
            names.append((tex.parent, match.group(1).strip()))
        for match in IMPORT_RE.finditer(text):
            # \import{dir}{file}: the file lives in dir, inputs relative to it
            names.append((tex.parent / match.group(1).strip(), match.group(2).strip()))
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


def _rel_key(tex: Path, path: Path) -> str:
    """Key a watched file by its path relative to the main file's directory.

    Absolute keys would make a copied or renamed project look hand-edited.
    """
    try:
        return path.resolve().relative_to(tex.parent.resolve()).as_posix()
    except ValueError:
        return str(path)


def current_hashes(tex: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for f in watched_files(tex):
        try:
            hashes[_rel_key(tex, f)] = sha256(read_tex(f))
        except OSError:
            hashes[_rel_key(tex, f)] = "<unreadable>"
    return hashes


def now() -> str:
    """Timestamp for changelog entries and draft records (second precision)."""
    return datetime.now().isoformat(timespec="seconds")


def append_changelog(tex: Path, entry: dict) -> None:
    entry = {"time": now(), **entry}
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


def _dep_copy_name(tex: Path, dep: Path) -> str:
    """Name component for a dependency copy inside the backup directory.

    Paths below the main file's directory keep their structure with "__" as
    the separator, so a nested chapter is restored to its own directory.
    """
    try:
        rel = dep.resolve().relative_to(tex.parent.resolve())
    except ValueError:
        return dep.name
    return "__".join(rel.parts)


def _note_path(snap: Path) -> Path:
    """Sidecar file holding a snapshot's note."""
    return snap.with_suffix(snap.suffix + ".note")


def snapshot(
    tex: Path, note: str = "", full: bool = False, files: list[Path] | None = None
) -> Path:
    """Copy the main file (plus `files`, or every input when `full`) into the
    backup directory.

    `files` is the per-change case: the file that is about to be edited is
    snapshotted as it was before the change, so a chapter edit is recoverable
    from its own snapshot and not only from the initial full backup.
    """
    bdir = backups_dir(tex)
    bdir.mkdir(parents=True, exist_ok=True)
    seq = 0
    for existing in bdir.glob("*-*.tex"):
        m = re.search(r"-(\d+)\.tex$", existing.name)
        if m:
            seq = max(seq, int(m.group(1)))
    dest = bdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{seq + 1:06d}.tex"
    shutil.copy2(tex, dest)
    deps = tex_inputs(tex) if full else list(files or [])
    for dep in deps:
        if not dep.is_file() or _same_file(dep, tex):
            continue
        shutil.copy2(dep, bdir / f"{dest.stem}__{_dep_copy_name(tex, dep)}")
    if note:
        _note_path(dest).write_text(note + "\n")
    if not full:
        prune(tex)
    return dest


def restore_snapshot(tex: Path, snap: Path) -> None:
    """Restore a snapshot; also restores any dependency files stored in it,
    recreating chapters the user deleted since."""
    bdir = backups_dir(tex)
    shutil.copy2(snap, tex)
    for dep_snap in bdir.glob(f"{snap.stem}__*.tex"):
        name = dep_snap.name[len(snap.stem) + 2 :]
        target = tex.parent.joinpath(*name.split("__"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dep_snap, target)


def prune(tex: Path) -> None:
    """Keep the full initial backup forever; for the rest: all snapshots <1h
    old, one per hour for 24h, one per day for 30d."""
    bdir = backups_dir(tex)
    snaps = list_snapshots(tex)
    reference = time.time()
    keep: set[Path] = set()
    hours: set[str] = set()
    days: set[str] = set()
    for s in snaps:  # newest first
        m = SNAP_RE.match(s.name)
        note = _note_path(s)
        if not m or (note.exists() and "initial backup" in note.read_text()):
            keep.add(s)  # unparseable or the initial full backup: never prune
            continue
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        age = reference - dt.timestamp()
        hour, day = dt.strftime("%Y%m%d-%H"), dt.strftime("%Y%m%d")
        if age < 3600:
            keep.add(s)
        elif age < 86400 and hour not in hours:
            hours.add(hour)
            keep.add(s)
        elif age < 30 * 86400 and day not in days:
            days.add(day)
            keep.add(s)
    for s in snaps:
        if s in keep:
            continue
        s.unlink()
        for extra in (_note_path(s), *bdir.glob(f"{s.stem}__*.tex")):
            if extra.exists():
                extra.unlink()


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


def ensure_changes_package(
    text: str, author: str = DEFAULT_AUTHOR, color: str | None = None
) -> str:
    """Inject \\usepackage[draft]{changes} plus a \\definechangesauthor for the
    given author (colored when it is not the default or `color` is given, so
    alternatives and accepted-text provenance are distinguishable in the
    PDF). Idempotent."""
    lines = split_lines(text)
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
        if line.lstrip("\ufeff").startswith("\\documentclass"):
            insert_at = i + 1
            break
    if not have_pkg:
        # The changes package must be loaded before any \definechangesauthor.
        text = insert_lines_at(text, insert_at, [CHANGES_PACKAGE])
        insert_at += 1
    if not have_author:
        # Author definitions go after the changes package, never before it.
        for i, line in enumerate(split_lines(text)):
            if re.search(r"\\usepackage(\[[^\]]*\])?\{changes\}", line):
                insert_at = i + 1
                break
        text = insert_lines_at(
            text, insert_at, [author_line(author, color or _author_color(text, author))]
        )
    return text


def ensure_document_preamble(tex: Path, state: dict, author: str, margin: bool = False) -> None:
    """Make sure the *main* file loads the changes package (draft mode) and
    defines the author, so that drafts written into any \\input/\\include
    file can use \\added/\\deleted/\\definechangesauthor commands.

    Included files cannot carry the preamble, so this runs before a draft is
    placed in a non-main file. Idempotent; recorded through the changelog.
    """
    config = load_config(tex)
    text = read_tex(tex)
    color = config["color"] if author == DEFAULT_AUTHOR else None
    new_text = ensure_changes_package(text, author, color)
    if margin:
        new_text = ensure_todonotes_package(new_text)
    if new_text == text:
        return
    _record_injected(state, text, new_text)
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


def _escape_unbalanced_braces(text: str) -> str:
    r"""Escape braces that would end the \added/\deleted/\footnote argument
    early (a literal unmatched "}") or never close (an unmatched "{"), so a
    drafted block stays parseable and accept never truncates the text."""
    escape: set[int] = set()
    opens: list[int] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            opens.append(i)
        elif ch == "}":
            if opens:
                opens.pop()
            else:
                escape.add(i)
        i += 1
    escape.update(opens)
    if not escape:
        return text
    return "".join("\\" + ch if i in escape else ch for i, ch in enumerate(text))


INJECTED_LINE_RE = re.compile(
    r"\\usepackage(\[[^\]]*\])?\{(changes|todonotes)\}"
    r"|\\definechangesauthor(\[[^\]]*\])?\{[^}]*\}"
)


def _record_injected(state: dict, old: str, new: str) -> None:
    """Remember which preamble lines latex-draft itself added, so the strip
    step can later remove exactly those — never a line the user wrote."""
    if old == new:
        return
    old_lines = set(split_lines(old))
    recorded = state.setdefault("injected_preamble", [])
    for line in split_lines(new):
        if line in old_lines or line in recorded:
            continue
        if INJECTED_LINE_RE.fullmatch(line.strip()):
            recorded.append(line)


def _injected_lines(tex: Path, state: dict) -> list[str]:
    """Preamble lines this tool added, from the state and from the changelog.

    Undoing an accept restores the file content (including the injected
    preamble) but the state's provenance list was emptied by the strip, so
    the changelog diffs are the persistent record of what the tool wrote.
    """
    lines = list(state.get("injected_preamble") or [])
    for entry in read_changelog(tex):
        if entry.get("kind") != "change":
            continue
        before = entry.get("before") or ""
        after = entry.get("after") or ""
        if not before or not after:
            continue
        old_lines = set(split_lines(before))
        # A drafted preamble command (draft-preamble) is also "a line the
        # change added", but it is the user's text, not an injection: skip
        # anything inside a draft block.
        block_spans = [m.span() for m in BLOCK_RE.finditer(after)]
        pos = 0
        after_lines, after_eols = split_eol(after)
        for i, line in enumerate(after_lines):
            start = pos
            pos = start + len(line) + (len(after_eols[i]) if i < len(after_eols) else 0)
            if line in old_lines or line in lines:
                continue
            if any(a <= start < b for a, b in block_spans):
                continue
            if INJECTED_LINE_RE.fullmatch(line.strip()):
                lines.append(line)
    return lines


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


def _prepare_body_text(
    text: str, state: dict, author: str, tex: Path, margin: bool = False
) -> str:
    """Add the changes package (and todonotes) to a main file's preamble.

    The injected lines are recorded in the state, so the strip at resolve time
    removes exactly those and never a line the user wrote. `margin` also
    loads todonotes, for a margin-note comment; the configured author color
    applies to the default (LLM) author only, so alternatives keep their own.
    """
    color = load_config(tex)["color"] if author == DEFAULT_AUTHOR else None
    prepared = ensure_changes_package(text, author, color)
    _record_injected(state, text, prepared)
    if margin:
        with_todo = ensure_todonotes_package(prepared)
        _record_injected(state, prepared, with_todo)
        prepared = with_todo
    return prepared


def _marker_block(did: str, body: str) -> str:
    """Wrap a draft body in its `% latex-draft` marker comments."""
    return "\n".join([f"% latex-draft:{did}", body, f"% latex-draft-end:{did}"])


def _provenance(text: str, style: str, attrs: str, newline: str) -> str:
    """Accepted LLM text, kept marked with changes markup.

    In draft mode the text shows the author's color (underline for `added`,
    highlight for `highlight`); in final mode the changes package prints it
    plainly, which is what production builds want. One command per paragraph,
    like the drafted blocks (a blank line cannot sit inside one argument).
    """
    flat = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = [
        f"\\{style}{attrs}{{{_escape_unbalanced_braces(para)}}}"
        for para in split_paragraphs(flat)
    ]
    return (newline * 2).join(parts)


def _has_provenance(tex: Path) -> bool:
    """Whether any accepted (kept-marked) LLM text is still in the document."""
    for f in watched_files(tex):
        text = _read_or_none(f)
        if text and PROVENANCE_CMD_RE.search(text):
            return True
    return False


def _provenance_span(text: str, index: int) -> tuple[int, int] | None:
    """Span of the `\\added{...}`/`\\highlight{...}` command containing `index`.

    The changes package cannot nest its commands, so a draft must not be built
    inside accepted provenance markup.
    """
    for m in PROVENANCE_CMD_RE.finditer(text):
        if m.start() > index:
            break
        depth, i = 1, m.end()
        while i < len(text) and depth:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if m.start() <= index < i:
            return m.start(), i
    return None


def build_block(
    did: str,
    old: str | None = None,
    new: str | None = None,
    author: str = DEFAULT_AUTHOR,
) -> str:
    parts = [f"% latex-draft:{did}"]
    if old is not None:
        for para in split_paragraphs(old):
            parts.append(
                rf"\deleted[id={author}]{{{_escape_unbalanced_braces(para)}}}"
            )
            parts.append("")
    if new is not None:
        for para in split_paragraphs(new):
            parts.append(
                rf"\added[id={author}]{{{_escape_unbalanced_braces(para)}}}"
            )
            parts.append("")
    parts.append(f"% latex-draft-end:{did}")
    return "\n".join(parts)


def build_comment_block(did: str, text: str, margin: bool = False) -> str:
    """A commenter-mode draft that never alters the body text.

    Default is a footnote; with margin=True a todonotes margin note, which is
    safe inside captions, section titles, tables and math (a footnote is not)
    and does not shift pagination.
    """
    safe = _escape_unbalanced_braces(text)
    cmd = "\\todo" if margin else "\\footnote"
    return _marker_block(did, f"{cmd}{{{safe}}}")


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
    return _marker_block(did, text)


def block_for(text: str, block: str) -> str:
    """Write a draft block with the target file's own line separator.

    Blocks are built with "\\n"; a CRLF or CR-only document must not end up
    with LF-only lines in the middle of it (mixed endings in the file the
    user reviews, and in the diff of their VCS).
    """
    return to_newline(block, newline_of(text))


def document_bounds(lines: list[str]) -> tuple[int, int] | None:
    r"""Line indices of \begin{document} and \end{document} in a main file.

    Returns None for an included file, which has no document environment of
    its own.
    """
    begin = end = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if begin is None and stripped.startswith("\\begin{document}"):
            begin = i
        elif begin is not None and stripped.startswith("\\end{document}"):
            end = i
            break
    if begin is None:
        return None
    return begin, end if end is not None else len(lines)


def find_preamble_end(text: str) -> int:
    """Line index (0-based) of \\begin{document} — the end of the preamble.
    Raises DraftError when the document has no \\begin{document}."""
    bounds = document_bounds(split_lines(text))
    if bounds is None:
        raise DraftError("no \\begin{document} found — cannot locate the preamble")
    return bounds[0]


def visible_line_indices(lines: list[str], skip_injected: bool = True) -> list[int]:
    """Indices of the lines the user sees in their own numbering.

    Pending draft blocks are not part of the document yet, so --at-line must
    skip them; two drafts at the same visible line then land at the same
    place. `skip_injected` also hides the auto-injected changes preamble.
    """
    out: list[int] = []
    in_block = False
    for i, line in enumerate(lines):
        if in_block:
            in_block = not BLOCK_END_LINE_RE.fullmatch(line)
        elif BLOCK_BEGIN_LINE_RE.fullmatch(line):
            in_block = True
        elif not (skip_injected and injected_preamble_line(line)):
            out.append(i)
    return out


def insert_block(
    text: str,
    block: str,
    after: str | None = None,
    before: str | None = None,
    at_line: int | None = None,
) -> str:
    lines = split_lines(text)
    if at_line is not None:
        if at_line < 1:
            raise DraftError(f"--at-line must be >= 1, got {at_line}")
        visible = visible_line_indices(lines)
        if at_line > len(visible):
            raise DraftError(
                f"--at-line {at_line} is beyond the end of the document "
                f"({len(visible)} visible lines)"
            )
        pos = visible[at_line - 1]
        # never insert before \documentclass
        dc = max(
            (i for i, line in enumerate(lines) if line.startswith("\\documentclass")),
            default=-1,
        )
        pos = max(pos, dc + 1)
    else:
        needle = after if after is not None else before
        hits = [i for i, line in enumerate(lines) if needle in line]
        if len(hits) != 1:
            raise DraftError(
                f"anchor {needle!r} matched {len(hits)} lines (need exactly 1)"
            )
        pos = hits[0] + 1 if after is not None else hits[0]
    bounds = document_bounds(lines)
    if bounds is not None:
        begin, end = bounds
        if pos <= begin:
            # \added/\deleted are body commands: in the preamble they abort
            # compilation with "Missing \begin{document}". draft-preamble is
            # the command for that region, so a body draft aimed at the
            # preamble is placed at the top of the body instead.
            pos = begin + 1
        if pos > end:
            raise DraftError(
                "insertion point is after \\end{document}; TeX would ignore "
                "the draft"
            )
    block_start: int | None = None
    for i, line in enumerate(lines):
        if block_start is None:
            if BLOCK_BEGIN_LINE_RE.fullmatch(line):
                block_start = i
        elif BLOCK_END_LINE_RE.fullmatch(line):
            if block_start < pos <= i:
                raise DraftError(
                    f"insertion point is inside existing draft block "
                    f"(lines {block_start + 1}-{i + 1})"
                )
            block_start = None
    # A drafted line inside a table would be a table row: the row break cannot
    # be wrapped in \added/\deleted, so refuse instead of writing a broken
    # document.
    if _alignment_depth(lines, pos):
        raise DraftError(
            "insertion point is inside a table or array; a drafted line would "
            "be a table row and cannot be wrapped in \\added/\\deleted. Edit "
            "the cell by hand (the tool records and adopts hand edits)"
        )
    # A draft block always occupies whole lines; insert_lines_at terminates
    # every inserted line with the document's own separator, so the end
    # marker lands on its own line even at EOF.
    return insert_lines_at(text, pos, split_lines(block))


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


def _drop_lines(text: str, drop) -> str:
    """Remove the lines `drop` selects, keeping every other byte intact.

    A removed line takes its own terminator with it; untouched lines keep
    their original endings (a mixed-EOL file stays mixed), and a document
    that did not end with a newline still does not.
    """
    lines, eols = split_eol(text)
    keep = [i for i, line in enumerate(lines) if not drop(line)]
    result = join_eol([lines[i] for i in keep], [eols[i] for i in keep if i < len(eols)])
    if not text.endswith(("\n", "\r")):
        result = re.sub(r"(\r\n|\r|\n)$", "", result)
    return result


def strip_changes_package_if_unused(
    text: str, injected: list[str] | None = None
) -> str:
    """Remove the auto-injected preamble lines once no draft markers remain,
    so the document is clean for submission (some classes reject the changes
    package in final mode).

    With `injected` (the lines latex-draft recorded adding to this state),
    exactly those lines are removed — a user-authored todonotes load, changes
    package or author definition is never touched. Without provenance the
    old pattern-based behaviour applies."""
    if "% latex-draft:" in text:
        return text
    if injected is None:
        keep_todo = bool(re.search(r"\\todo\b", text))

        def drop(line: str) -> bool:
            if injected_preamble_line(line):
                return True
            return bool(
                not keep_todo
                and re.fullmatch(
                    r"\\usepackage(\[[^\]]*\])?\{todonotes\}", line.strip()
                )
            )

        return _drop_lines(text, drop)
    keep_todo = bool(re.search(r"\\todo\b", text))
    injected_set = set(injected)

    def drop_recorded(line: str) -> bool:
        if line not in injected_set:
            return False
        return not (
            keep_todo
            and re.fullmatch(r"\\usepackage(\[[^\]]*\])?\{todonotes\}", line.strip())
        )

    return _drop_lines(text, drop_recorded)


def resolve_drafts(
    text: str,
    ids: set[str],
    accept: bool,
    accepted_id: str | None = None,
    keep_markup: bool = False,
    style: str = "added",
) -> tuple[str, set[str], set[str]]:
    """Resolve the draft blocks whose ids are in `ids`.

    Returns (new_text, found, spliced). `found` is the set of draft ids whose
    block was present and resolved; `spliced` is the set of ids whose block
    actually performed the splice for a shared deleted span.

    `accepted_id` names the one draft the user accepted. On accept of a
    sibling group, that block (not the first in document order) performs the
    splice, so the text that lands is the one the user picked. On reject,
    the first block of the group splices the original text back.

    With `keep_markup`, accepted text stays wrapped in the changes markup
    (`style` = added/highlight) so the review PDF still shows the LLM wrote
    it; final mode prints it plainly.
    """
    found: set[str] = set()
    spliced: set[str] = set()
    # Sibling alternatives draft over the same original text: each sibling
    # block repeats the same \\deleted{...} span. Resolving must splice the
    # result in only ONCE for the whole group, not once per sibling —
    # otherwise rejecting duplicates the restored text and accepting leaks
    # the other alternatives' additions. Group siblings by their deleted
    # span: the accepted block of a group performs the splice, the rest
    # collapse to nothing.
    done_groups: set[str] = set()
    # Pre-scan: map each deleted span to the id that should splice it.
    # On accept that is the accepted sibling; on reject it is the first
    # block of the group in document order.
    splice_winner: dict[str, str] = {}
    for m in BLOCK_RE.finditer(text):
        did = m.group(1)
        if did not in ids:
            continue
        if re.search(r"\\(added|deleted|footnote|todo)\b", m.group(2)):
            deleted = extract_command_content(m.group(2), "deleted")
            if deleted is not None:
                key = "del:" + deleted
                if key not in splice_winner:
                    if accept and accepted_id is not None:
                        # find which id in this group is the accepted one
                        splice_winner[key] = accepted_id
                    else:
                        splice_winner[key] = did

    def repl(m: re.Match) -> str:
        did = m.group(1)
        if did not in ids:
            return m.group(0)
        found.add(did)
        # BLOCK_RE consumes the line break after the end marker. When the
        # match is immediately followed by another block (a sibling), the
        # next block starts right after; when it is followed by a blank line
        # (the original paragraph break that was behind the replaced text),
        # that blank line survives. Re-attach the break only when the
        # following text does not start with one, so a reject restores the
        # original line structure — using the document's own separator.
        had_nl = m.group(0).endswith(("\n", "\r"))
        nl = newline_of(text)
        # A preamble draft block: raw text between the markers, no
        # \\added/\\deleted. The block occupies whole lines, so reject
        # removes the match entirely (like a pure insert draft); accept
        # merges the raw text verbatim, keeping the end-marker's break.
        if not re.search(r"\\(added|deleted|footnote|todo)\b", m.group(2)):
            if not accept:
                return ""
            return m.group(2).strip("\r\n") + (nl if had_nl else "")
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
                # Only the winner block splices; the rest collapse.
                winner = splice_winner.get(key)
                if accept and winner is not None and did != winner:
                    return ""
                done_groups.add(key)
                spliced.add(did)
            if content:
                spliced_text = to_newline(content, nl)
                if accept and keep_markup:
                    attrs_match = re.search(r"\\added(\[[^\]]*\])?", m.group(2))
                    attrs = (attrs_match.group(1) or "") if attrs_match else ""
                    spliced_text = _provenance(spliced_text, style, attrs, nl)
                return spliced_text + (nl if had_nl else "")
            return ""
        for cmd in ("footnote", "todo"):
            content = extract_command_content(m.group(2), cmd)
            if content is not None:
                if not accept:
                    return ""
                out = "\\" + cmd + "{" + to_newline(content, nl) + "}"
                return out + (nl if had_nl else "")
        return ""

    return BLOCK_RE.subn(repl, text)[0], found, spliced


def _file_of(tex: Path, fname: str) -> Path:
    """Path of a file of the document from the name recorded in a draft."""
    return tex if fname == tex.name else tex.parent / fname


def _same_file(a: Path, b: Path) -> bool:
    """Whether two paths name the same file (the main file is resolved once)."""
    return a.resolve() == b.resolve()


def _blocks_by_id(text: str) -> dict[str, re.Match]:
    """The draft blocks in `text`, first block per draft id."""
    blocks: dict[str, re.Match] = {}
    for m in BLOCK_RE.finditer(text):
        blocks.setdefault(m.group(1), m)
    return blocks


def draft_record(did: str, op: str, preview_text: str) -> dict:
    preview = " ".join(preview_text.split())[:120]
    return {
        "id": did,
        "op": op,
        "status": "pending",
        "created": now(),
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
    before_text = read_tex(target)
    snap = snapshot(
        tex, note, files=None if _same_file(target, tex) else [target]
    )
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
    _atomic_write(target, new_text)
    append_changelog(tex, entry)
    state["file_hashes"] = current_hashes(tex)
    return snap


# ---------------------------------------------------------------- commands


def _validate_target(fn, target: Path, tex: Path) -> str | None:
    """Run a draft's text transformation without writing anything.

    For a draft aimed at a chapter, the main file's preamble is prepared
    first (included files cannot load the changes package), so the draft
    itself must be validated before that happens: a bad anchor otherwise
    leaves the preamble of the main file modified. The main file needs no
    dry run — its preamble injection and the block are written in one step.
    """
    if _same_file(target, tex):
        return None
    try:
        fn(read_tex(target))
    except DraftError as e:
        return str(e)
    return None


def _apply_draft(
    tex: Path,
    state: dict,
    target: Path,
    fn,
    did: str,
    op: str,
    preview: str,
    author: str,
    preamble_margin: bool = False,
    fields: dict | None = None,
) -> int:
    """Validate, prepare the main preamble, write, and register one draft.

    The validation must run before `ensure_document_preamble`: a draft aimed
    at a chapter needs the main file's preamble, and a refused draft must
    leave the main file untouched. `preamble_margin` loads todonotes for a
    margin-note comment; `fields` are extra draft-record keys.
    """
    error = _validate_target(fn, target, tex)
    if error:
        return _fail(error)
    if not _same_file(target, tex):
        ensure_document_preamble(tex, state, author, margin=preamble_margin)
    try:
        mutate(tex, state, f"draft {op} {did}", fn, target=target)
    except DraftError as e:
        return _fail(e)
    record = draft_record(did, op, preview)
    record.update({"file": target.name, "author": author, **(fields or {})})
    state["drafts"][did] = record
    save_state(tex, state)
    print(f"latex-draft: drafted {op} {did} in {target.name}")
    return 0


def _insert_draft(
    tex: Path,
    state: dict,
    args: argparse.Namespace,
    op: str,
    make_block,
    margin: bool = False,
    fields: dict | None = None,
) -> int:
    """Place a drafted block at an anchor: insert and comment share this.

    They differ only in the block they build (and a margin comment also needs
    todonotes in the preamble).
    """
    did = new_id(state)
    author = getattr(args, "author", None) or DEFAULT_AUTHOR
    target = resolve_target(tex, getattr(args, "file", None))
    if target is None:
        return 2

    def fn(text: str) -> str:
        _assert_markable(args.text)
        if _same_file(target, tex):
            text = _prepare_body_text(text, state, author, tex, margin)
        return insert_block(
            text,
            block_for(text, make_block(did, author)),
            after=args.after,
            before=args.before,
            at_line=args.at_line,
        )

    return _apply_draft(
        tex, state, target, fn, did, op, args.text, author,
        preamble_margin=margin, fields=fields,
    )


def cmd_draft_insert(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _insert_draft(
        tex, state, args, "insert",
        lambda did, author: build_block(did, new=args.text, author=author),
    )


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
    if _same_file(candidate, tex):
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


def _visible_span(text: str, old: str) -> tuple[int, int] | None:
    """Character span of `old` outside every pending draft block.

    A match that merely overlaps a block (starts outside, ends inside, or
    the other way round) is not a visible occurrence: drafting over it would
    put another draft's marker lines inside \\deleted{...}.
    """
    spans = [m.span() for m in BLOCK_RE.finditer(text)]
    start = 0
    while True:
        i = text.find(old, start)
        if i < 0:
            return None
        stop = i + len(old)
        if not any(i < b and a < stop for a, b in spans):
            return i, stop
        start = i + 1


def _line_span(text: str, span: tuple[int, int]) -> tuple[int, int]:
    """Widen a character span to whole lines.

    A draft block always occupies whole lines: the marker comments swallow
    anything that follows them on the same line, so text left after an end
    marker would vanish from the review PDF. Works for LF, CRLF and CR.
    """
    start = max(text.rfind("\n", 0, span[0]), text.rfind("\r", 0, span[0])) + 1
    m = EOL_RE.search(text, span[1])
    return start, m.start() if m else len(text)


def _line_of(text: str, index: int) -> int:
    """Line index (0-based) of a character offset."""
    return len(split_lines(text[:index])) - 1


def _alignment_depth(lines: list[str], line_index: int) -> int:
    """Table/array nesting depth at a line.

    A line inside one is a table row: wrapping it in \\deleted/\\added would
    put the row break inside the argument, which TeX rejects.
    """
    depth = 0
    for i, line in enumerate(lines):
        if i == line_index:
            return depth
        depth += len(ALIGN_BEGIN_RE.findall(line)) - len(ALIGN_END_RE.findall(line))
    return depth


def _assert_markable(
    text: str, span: tuple[int, int] | None = None, what: str = "the drafted text"
) -> None:
    r"""Refuse content the changes markup cannot wrap.

    \added/\deleted (and \footnote/\todo) are text markup: an environment
    boundary or an alignment tab inside the argument is a compile error, and
    so is a table row. Structural edits belong in draft-preamble (preamble)
    or in a hand edit, which the tool adopts and records.

    `span` narrows the check to part of `text` and enables the table check
    (callers pass the character span of the line(s) about to be wrapped).
    """
    probe = text[span[0] : span[1]] if span is not None else text
    m = UNMARKABLE_RE.search(probe)
    if m:
        raise DraftError(
            f"{what} contains LaTeX structure ({m.group(0)!r}) that "
            "\\added/\\deleted cannot wrap; change only the argument inside "
            "the line, or make this edit by hand"
        )
    if span is not None and _alignment_depth(split_lines(text), _line_of(text, span[0])):
        raise DraftError(
            "the text is inside a table or array; a row cannot be wrapped in "
            "\\added/\\deleted. Edit the cell by hand (the tool records and "
            "adopts hand edits), or draft the change outside the table"
        )


def _assert_body_text(text: str, index: int) -> None:
    r"""Refuse a body draft that lands in the preamble.

    \added/\deleted (and \footnote) cannot be typeset before
    \begin{document}; draft-preamble is the command for that region.
    """
    m = re.search(r"^[ \t]*\\begin\{document\}", text, re.MULTILINE)
    if m and index < m.start():
        raise DraftError(
            "the text is in the preamble, where \\added/\\deleted cannot be "
            "typeset; use draft-preamble for preamble changes"
        )


def _replace_or_delete(tex: Path, state: dict, args: argparse.Namespace, op: str) -> int:
    did = new_id(state)
    old = args.old
    new = getattr(args, "new", None)
    author = getattr(args, "author", None) or DEFAULT_AUTHOR
    target = resolve_target(tex, getattr(args, "file", None))
    if target is None:
        return 2
    text = target.read_text(encoding="utf-8")

    def replacement(removed: str) -> str:
        """The line(s) in their accepted form."""
        return removed.replace(old, "" if new is None else new, 1)

    def fn(text: str) -> str:
        if _same_file(target, tex):
            text = _prepare_body_text(text, state, author, tex)
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
            deleted = extract_command_content(m.group(2), "deleted")
            if deleted is not None and old in deleted:
                host = m  # innermost pending block that deletes the old text
        if host is None and any(old in m.group(2) for m in matches):
            # The text exists only inside a pending draft's \added content.
            # Editing it here would duplicate it: the original insert would
            # still be spliced in on accept.
            raise DraftError(
                "--old text is inside the text a pending draft adds; "
                "accept or reject that draft before editing it"
            )
        if host is not None:
            removed = extract_command_content(host.group(2), "deleted")
            kept = replacement(removed)
            _assert_markable(removed, what="the replaced text")
            _assert_markable(kept, what="the new text")
            block = block_for(
                text, build_block(did, old=removed, new=kept or None, author=author)
            )
            # BLOCK_RE consumes the trailing newline after the host's end
            # marker, so host.end() is already at a line boundary. A blank
            # line right after the host (the original paragraph break that
            # was displaced when the first block replaced text inline) must
            # stay AFTER all sibling blocks, not be split by them: skip it
            # when placing the sibling.
            insert_at = host.end()
            rest = text[insert_at:]
            nl = newline_of(text)
            m_blank = re.match(rf"{nl}(?:[ \t]*{nl})?", rest)
            blank = m_blank.group(0) if m_blank else ""
            insert_at += len(blank)
            rest = text[insert_at:]
            # The end marker must never glue to a following line (e.g.
            # \end{document} at EOF): ensure a line break after the block
            # when the rest does not start with one.
            end_sep = "" if (rest.startswith(("\n", "\r")) or not rest) else nl
            return text[:insert_at] + block + end_sep + rest
        span = _visible_span(text, old)
        if span is None:
            raise DraftError(
                "--old text matched 0 times outside pending draft blocks "
                "(a match that overlaps a block cannot be drafted over; "
                "resolve that draft first)"
            )
        if _provenance_span(text, span[0]) is not None:
            raise DraftError(
                "the text is inside accepted LLM markup (\\added/\\highlight), "
                "which the changes package cannot nest; edit the whole "
                "\\added{...} command or remove that markup first"
            )
        start, stop = _line_span(text, span)
        _assert_body_text(text, start)
        removed = text[start:stop]
        kept = replacement(removed)
        _assert_markable(text, (start, stop), "the replaced text")
        _assert_markable(kept, what="the new text")
        block = block_for(
            text, build_block(did, old=removed, new=kept or None, author=author)
        )
        return text[:start] + block + text[stop:]

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
    preview = new if op == "replace" else old
    return _apply_draft(tex, state, target, fn, did, op, preview, author)


def cmd_draft_replace(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _replace_or_delete(tex, state, args, "replace")


def cmd_draft_delete(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _replace_or_delete(tex, state, args, "delete")


def cmd_draft_comment(tex: Path, state: dict, args: argparse.Namespace) -> int:
    margin = getattr(args, "margin", False)
    return _insert_draft(
        tex, state, args, "comment",
        lambda did, author: build_comment_block(did, args.text, margin=margin),
        margin=margin, fields={"margin": margin},
    )


def ensure_todonotes_package(text: str) -> str:
    """Inject \\usepackage{todonotes} if missing (for margin-note comments).

    Must run *after* the changes package is in place: todonotes is loaded by
    changes in draft mode, and a duplicate or earlier load breaks the author
    definitions that follow."""
    if re.search(r"\\usepackage(\[[^\]]*\])?\{todonotes\}", text):
        return text
    lines = split_lines(text)
    insert_at = None
    for i, line in enumerate(lines):
        if re.search(r"\\usepackage(\[[^\]]*\])?\{changes\}", line):
            insert_at = i + 1
            break
    if insert_at is None:
        raise DraftError(
            "todonotes needs the changes package; add \\usepackage[draft]{changes} first"
        )
    return insert_lines_at(text, insert_at, [r"\usepackage{todonotes}"])


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
        lines = split_lines(text)
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
            hits = [
                i
                for i in visible_line_indices(lines, skip_injected=False)
                if i < bd and position in lines[i]
            ]
            if len(hits) != 1:
                raise DraftError(
                    f"anchor {position!r} matched {len(hits)} preamble lines "
                    "(need exactly 1)"
                )
            pos = hits[0] + 1
        block = block_for(text, build_preamble_block(did, text_arg))
        return insert_lines_at(text, pos, split_lines(block))

    return _apply_draft(
        tex, state, tex, fn, did, "preamble", text_arg, DEFAULT_AUTHOR,
        fields={"preamble": True},
    )


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


def _sibling_ids(
    tex: Path, state: dict, by_file: dict[str, list[str]], ids: list[str]
) -> set[str]:
    """Pending drafts that are alternatives to the drafts being accepted.

    Siblings share one \\deleted span: they describe changes to the same
    original text, so resolving the accepted one orphans the rest.
    """
    siblings: set[str] = set()
    for fname, file_ids in by_file.items():
        blocks = _blocks_by_id(read_tex(_file_of(tex, fname)))
        accepted_old: set[str] = set()
        for did in file_ids:
            block = blocks.get(did)
            if block is None:
                continue
            deleted = extract_command_content(block.group(2), "deleted")
            if deleted is not None:
                accepted_old.add(deleted)
        for did, rec in state["drafts"].items():
            if (
                did in ids
                or rec["status"] != "pending"
                or rec.get("file", tex.name) != fname
            ):
                continue
            block = blocks.get(did)
            if block is None:
                continue
            deleted = extract_command_content(block.group(2), "deleted")
            if deleted is not None and deleted in accepted_old:
                siblings.add(did)
    return siblings


def _close_missing_blocks(
    tex: Path, state: dict, ids: list[str], by_file: dict[str, list[str]], all_drafts: bool
) -> tuple[list[str], dict[str, list[str]], int | None]:
    """Handle drafts whose block was hand-deleted from the document.

    A vanished block cannot be resolved. With `--all` its record is closed and
    the surviving drafts are resolved; for one specific id the command refuses
    up front, so no file is left resolved while the state claims it pending.
    """
    missing: dict[str, list[str]] = {}
    for fname, file_ids in by_file.items():
        try:
            file_text = read_tex(_file_of(tex, fname))
        except (OSError, DraftError):
            file_text = ""
        gone = [did for did in file_ids if did not in _blocks_by_id(file_text)]
        if gone:
            missing[fname] = gone
    if not missing:
        return ids, by_file, None
    for fname, gone in missing.items():
        where = ", ".join(sorted(gone))
        if all_drafts:
            print(
                f"latex-draft: draft(s) no longer in {fname}, marked as "
                f"rejected: {where}",
                file=sys.stderr,
            )
        else:
            print(f"latex-draft: draft(s) not found in {fname}: {where}",
                  file=sys.stderr)
        for did in gone:
            rec = state["drafts"][did]
            rec["status"] = "rejected"
            rec["resolved"] = now()
            rec["hand_deleted"] = True
    save_state(tex, state)
    if not all_drafts:
        return ids, by_file, 2
    for fname, gone in missing.items():
        ids = [did for did in ids if did not in gone]
        by_file[fname] = [d for d in by_file[fname] if d not in gone]
    return ids, by_file, None


def _strip_unused_preamble(tex: Path, state: dict, injected: list[str]) -> None:
    """Remove the auto-injected preamble once no draft is pending anywhere."""
    mutate(
        tex,
        state,
        "strip unused changes preamble",
        lambda text: strip_changes_package_if_unused(text, injected),
    )
    state["injected_preamble"] = []


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
    # other pending drafts (in any file) still need the changes preamble
    others_pending = any(
        did not in ids and rec["status"] == "pending"
        for did, rec in state["drafts"].items()
    )
    sibling_ids = _sibling_ids(tex, state, by_file, ids) if accept else set()

    ids, by_file, error = _close_missing_blocks(tex, state, ids, by_file, args.all)
    if error is not None:
        return error
    # The provenance list is emptied once the preamble is stripped; an undo
    # of that strip brings the lines back, so recover them from the changelog
    # rather than leaving them in the document forever.
    injected = _injected_lines(tex, state)
    config = load_config(tex)
    for fname, file_ids in by_file.items():
        if not file_ids:
            continue
        target = _file_of(tex, fname)
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

        spliced_ids: set[str] = set()
        # The accepted draft id (single-id accept) tells resolve_drafts
        # which sibling of a group performs the splice.
        accepted_id = ids[0] if accept and not args.all and len(ids) == 1 else None

        def fn(text: str, file_ids=all_ids) -> str:
            nonlocal spliced_ids
            new_text, found, spliced = resolve_drafts(
                text,
                file_ids,
                accept,
                accepted_id=accepted_id,
                keep_markup=config["keep_markup"],
                style=config["style"],
            )
            spliced_ids |= spliced
            missing = file_ids - found
            if missing:
                raise DraftError(
                    f"draft(s) not found in {fname}: " + ", ".join(sorted(missing))
                )
            if _same_file(target, tex) and not others_pending:
                if config["keep_markup"] and PROVENANCE_CMD_RE.search(new_text):
                    # The kept markup needs the package to compile.
                    return new_text
                return strip_changes_package_if_unused(new_text, injected)
            return new_text

        try:
            mutate(tex, state, f"{verb} {','.join(file_ids)}", fn, target=target)
        except DraftError as e:
            return _fail(e)
    # On accept, exactly one sibling per shared deleted span performs the
    # splice — that is the alternative whose text lands in the document.
    # The others are orphaned by the overlap. (On reject the first block of
    # the group splices the original text back; no draft is accepted.)
    accepted_ids = set(ids) if accept else set()
    if accept and spliced_ids:
        accepted_ids = {
            did for did in ids if did in spliced_ids or did not in sibling_ids
        }
    stamp = now()
    for did in ids:
        state["drafts"][did]["status"] = (
            "accepted" if did in accepted_ids else "rejected"
        )
        state["drafts"][did]["resolved"] = stamp
    for did in sibling_ids:
        rec = state["drafts"][did]
        rec["status"] = "rejected"
        rec["resolved"] = stamp
        rec["orphaned_by_overlap"] = True
    # Once no draft markers remain anywhere in the document, strip the
    # auto-injected preamble from the main file (some classes reject the
    # changes package in final mode) — unless accepted text is still kept
    # marked, which needs the package to compile.
    if not any(rec["status"] == "pending" for rec in state["drafts"].values()):
        if not (config["keep_markup"] and _has_provenance(tex)):
            _strip_unused_preamble(tex, state, injected)
    save_state(tex, state)
    note = ""
    if sibling_ids:
        note = (
            f"; overlapping draft(s) {', '.join(sorted(sibling_ids))} "
            "resolved as rejected"
        )
    if ids:
        print(f"latex-draft: {verb}ed {len(ids)} draft(s): {', '.join(ids)}{note}")
    return 0


def cmd_accept(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _resolve(tex, state, args, accept=True)


def cmd_reject(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _resolve(tex, state, args, accept=False)


PGFSYS_MARK_RE = re.compile(r"\\pgfsyspdfmark\b")
PGFSYS_PACKAGE_RE = re.compile(
    r"\\usepackage(?:\[[^\]]*\])?\{(?:tikz|pgf|todonotes|changes)\}"
)


def _clear_stale_aux(tex: Path) -> None:
    r"""Drop auxiliary data a previous draft compile left behind.

    todonotes/pgf write ``\pgfsyspdfmark`` into the .aux; once the packages
    are stripped after rejecting the last draft, the next compile dies on the
    undefined command. The .aux is regenerated anyway, so removing it is safe.
    """
    aux = tex.parent / (tex.stem + ".aux")
    if not aux.is_file():
        return
    try:
        if not PGFSYS_MARK_RE.search(aux.read_text(errors="replace")):
            return
        source = "\n".join(read_tex(f) for f in watched_files(tex))
    except (OSError, DraftError):
        return
    if not PGFSYS_PACKAGE_RE.search(source):
        aux.unlink()


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
    _clear_stale_aux(tex)
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


def _first_unpaired_end(text: str, env: str) -> int | None:
    r"""1-based line number of the first \end{env} that no \begin{env}
    before it matches — the mismatched one to repair."""
    depth = 0
    for i, line in enumerate(text.split("\n"), start=1):
        for tok in re.finditer(r"\\(begin|end)\{([^}]+)\}", line):
            if tok.group(2) != env:
                continue
            if tok.group(1) == "begin":
                depth += 1
            elif depth == 0:
                return i
            else:
                depth -= 1
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
            if shutil.which("latexmk") or shutil.which("pdflatex"):
                print(f"latex-draft: nothing to repair; {tex.stem}.pdf compiles")
            else:
                print(
                    "latex-draft: nothing to repair (no LaTeX engine found; "
                    "compile not verified)"
                )
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
            # The mismatched \end is the first \end{X} that no \begin{X}
            # before it matches. Pairing by counts alone would rewrite a
            # healthy environment of the same name instead.
            target = None
            lineno = None
            for f in watched_files(tex):
                try:
                    f_text = read_tex(f)
                except (OSError, DraftError):
                    continue
                unpaired = _first_unpaired_end(f_text, p["end"])
                if unpaired is not None:
                    target, lineno = f, unpaired
                    break
            if target is not None:
                wrong = "\\end{" + p["end"] + "}"
                right = "\\end{" + p["begin"] + "}"
                # A repair must compile on its own, so it may not rely on the
                # changes package: patch the source directly. The change is
                # still snapshotted and changelogged (so it can be undone).
                def _fix_end(
                    text: str, _line=lineno, _wrong=wrong, _right=right
                ) -> str:
                    lines, eols = split_eol(text)
                    idx = _line - 1
                    if 0 <= idx < len(lines) and _wrong in lines[idx]:
                        lines[idx] = lines[idx].replace(_wrong, _right, 1)
                        return join_eol(lines, eols)
                    return text.replace(_wrong, _right, 1)

                mutate(
                    tex,
                    state,
                    f"repair: {wrong} -> {right} "
                    f"in {target.name}:{lineno}",
                    _fix_end,
                    target=target,
                )
                fixed += 1
                continue
            suggestions.append(f"fix the mismatched \\end{{{p['end']}}} by hand")
        elif p["kind"] == "undefined-control" and p.get("command") in (
            "added", "deleted", "todo", "definechangesauthor",
        ):
            # A changes-package command without the package (a draft block in
            # a document that lost the preamble, or an author definition
            # above the package load): inject the preamble into the main file
            # — included files cannot carry it.
            mutate(
                tex,
                state,
                "repair: inject changes package preamble",
                lambda text: _prepare_body_text(text, state, DEFAULT_AUTHOR, tex),
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
            if shutil.which("latexmk") or shutil.which("pdflatex"):
                print(f"latex-draft: repair applied; compiled {pdf.name}")
            else:
                print(
                    "latex-draft: repair applied (no LaTeX engine found; "
                    "compile not verified)"
                )
            return 0
        print(
            "latex-draft: repair applied but compile still fails; "
            "remaining problems:",
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


def _reconcile_draft_status(tex: Path, state: dict) -> None:
    """After an undo, draft records must match the document again: a block
    the undo brought back is pending once more; a pending record whose block
    is gone (the change that created it was undone) is closed."""
    for did, rec in state["drafts"].items():
        try:
            text = read_tex(_file_of(tex, rec.get("file", tex.name)))
        except (OSError, DraftError):
            continue
        has_block = did in _blocks_by_id(text)
        if has_block and rec["status"] != "pending":
            rec["status"] = "pending"
            rec.pop("resolved", None)
            rec.pop("orphaned_by_overlap", None)
        elif not has_block and rec["status"] == "pending":
            rec["status"] = "rejected"
            rec["resolved"] = now()


def _undo_targets(
    tex: Path, state: dict, args: argparse.Namespace
) -> tuple[list[dict], int | None]:
    """The changelog entries an undo reverts, plus an exit code when there is
    nothing to revert.

    `--turn` reverts the current turn, or the most recent turn that has
    changes (the common case: `undo` after `compile`). Entries a trailing run
    of undos already reverted are skipped, so `undo --turn` cannot loop.
    """
    entries = [e for e in read_changelog(tex) if e.get("kind") == "change"]
    if not entries:
        print("latex-draft: changelog is empty, nothing to undo", file=sys.stderr)
        return [], 1
    if not args.turn:
        if args.n < 1:
            print("latex-draft: --n must be >= 1", file=sys.stderr)
            return [], 2
        return entries[-args.n :], None
    turn = state.get("turn", 0)
    targets = [e for e in entries if e.get("turn", 0) == turn]
    if not targets:
        turn = max(e.get("turn", 0) for e in entries)
        targets = [e for e in entries if e.get("turn", 0) == turn]
    undone: set[str] = set()
    for e in reversed(read_changelog(tex)):
        if e.get("kind") != "undo":
            break
        undone.add(e.get("undid_backup"))
    targets = [e for e in targets if e.get("backup") not in undone]
    if not targets:
        print(f"latex-draft: turn {turn} is already undone (last action was "
              "`undo`)")
        return [], 0
    return targets, None


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
    targets, error = _undo_targets(tex, state, args)
    if error is not None:
        return error
    skipped: list[str] = []
    done_entries: list[dict] = []
    # replaying before-content is a whole-file restore, so undoing several
    # changes only works back-to-back without intervening edits: track the
    # expected content as we go (each undone change must match the
    # before-state of the change undone right after it)
    expected: dict[str, str] = {}
    for entry in reversed(targets):
        fname = entry.get("file", tex.name)
        target = _file_of(tex, fname)
        try:
            current = read_tex(target)
        except (OSError, DraftError):
            # The file was deleted (or moved) by hand. Replaying the recorded
            # before-content can recreate it, but only on request: silently
            # resurrecting a file the user deleted would be a surprise.
            if not force:
                skipped.append(fname)
                print(
                    f"latex-draft: skipped '{entry.get('note', '?')}' in "
                    f"{fname}: the file is gone; use --force to restore the "
                    "recorded content",
                    file=sys.stderr,
                )
                continue
            current = None
            print(
                f"latex-draft: restoring deleted {fname} from the changelog "
                "(--force)",
                file=sys.stderr,
            )
        if current is not None:
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
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, entry["before"])
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
    _reconcile_draft_status(tex, state)
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
        note_file = _note_path(s)
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


def _changes_mode(text: str) -> str:
    """How the changes package is loaded: missing, draft (default) or final."""
    m = re.search(r"\\usepackage(\[[^\]]*\])?\{changes\}", text)
    if not m:
        return "missing"
    return "final" if "final" in (m.group(1) or "") else "draft"


def cmd_check(tex: Path, state: dict, args: argparse.Namespace) -> int:
    """Pre-submission check: the document must be clean of draft markup.

    Reports, across the main file and all input files:
    - pending drafts (must be accepted or rejected first)
    - stray `% latex-draft:` markers (a draft block the tool no longer knows)
    - leftover auto-injected preamble lines (changes/todonotes packages and
      \\definechangesauthor) once nothing is pending — some classes (revtex,
      achemso) reject them in final mode

    Accepted LLM text kept marked (`keep_markup` in the config) is not a
    leftover: the package must stay loaded for it to compile. `check` then
    reminds the user to build production with the final mode, where the
    package prints that text plainly.

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
    pending_by_file: dict[str, set[str]] = {}
    for did, rec in state["drafts"].items():
        if rec["status"] == "pending":
            pending_by_file.setdefault(rec.get("file", tex.name), set()).add(did)
    for f in watched_files(tex):
        text = _read_or_none(f)
        if text is None:
            continue
        # the markers of the pending drafts the tool knows about are not
        # stray; only unknown markers are
        scrubbed = text
        for m in _blocks_by_id(text).values():
            if m.group(1) in pending_by_file.get(f.name, set()):
                scrubbed = scrubbed.replace(m.group(0), "")
        stray = len(re.findall(r"% latex-draft(-end)?:[0-9a-f]{8}", scrubbed))
        if stray:
            problems.append(f"{f.name}: {stray} stray latex-draft marker line(s)")
    marked = load_config(tex)["keep_markup"] and _has_provenance(tex)
    if not pending:
        main_text = read_tex(tex)
        leftovers = [
            line.strip()
            for line in split_lines(main_text)
            if injected_preamble_line(line)
        ]
        if leftovers and not marked:
            problems.append(
                f"{tex.name}: leftover auto-injected preamble line(s): "
                + "; ".join(leftovers)
            )
    if problems:
        print(f"latex-draft: check FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("latex-draft: check OK — no pending drafts, no stray markers, "
          "no leftover preamble")
    if marked and _changes_mode(read_tex(tex)) != "final":
        print(
            "latex-draft: accepted LLM text is still marked (keep_markup); "
            "the review PDF shows it. For production, build with "
            "\\usepackage[final]{changes} so the marks are printed plainly."
        )
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
    if "_recovered" in state:
        recovered = state.pop("_recovered")
        print(
            "latex-draft: WARNING: the state file was unreadable; "
            f"recovered {recovered} pending draft(s) from the document markers",
            file=sys.stderr,
        )
    # Turn-start safety: make sure a full backup exists, and check whether the
    # user edited the file (or its inputs) by hand since the last tool action.
    try:
        ensure_full_backup(tex, state)
        check_and_adopt_external(tex, state)
        rc = HANDLERS[args.command](tex, state, args)
    except DraftError as e:
        return _fail(e)
    if rc == 0 and args.command in (
        "draft-insert",
        "draft-replace",
        "draft-delete",
        "draft-comment",
        "draft-preamble",
        "accept",
        "reject",
        "undo",
        "repair",
    ):
        state["file_hashes"] = current_hashes(tex)
        save_state(tex, state)
    return rc


if __name__ == "__main__":
    sys.exit(main())
