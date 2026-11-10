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
import contextlib
import difflib
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

try:
    import fcntl
except ImportError:  # non-POSIX platform: fall back to hash checks only
    fcntl = None

DEFAULT_AUTHOR = "AI"
AGENT_AUTHOR = "AI"

# A changes-package author id lands bare inside \added[id=...] and
# \definechangesauthor{id}, so it must be TeX-safe.
_AUTHOR_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")


def _safe_author_id(label: str) -> str:
    """Turn a free-text --author label into a TeX-safe change-author id.

    Labels like `A&B`, `50%` or `\evil` would otherwise end up unescaped in
    \definechangesauthor and break the build. Runs of spaces become `-` so
    multi-word labels stay readable; an empty result falls back to the
    default author.
    """
    ident = _AUTHOR_ID_BAD.sub("", label.strip().replace(" ", "-"))
    return ident or DEFAULT_AUTHOR
# The agent's mark in the review PDF is a symbol, not a text tag: a filled
# circle in the agent's color (installed per-profile via author_line).
# Struck-out text keeps the package default and shows the ordinary tag.
AGENT_MARK = r"\ensuremath{\bullet}"
CHANGES_PACKAGE = r"\usepackage[draft]{changes}"
# Package options used when the tool injects the package for the agent:
# brackets markup for human authors, right position, struck-out deletions.
CHANGES_OPTIONS = "authormarkup=brackets,authormarkupposition=right,deletedmarkup=sout"
CHANGES_PACKAGE_LINE = r"\usepackage[draft," + CHANGES_OPTIONS + "]{changes}"
CHANGES_AUTHOR_RE = re.compile(r"\\definechangesauthor(\[[^\]]*\])?\{[^}]*\}")
# Palette: the agent gets the first entry, human authors (parsed from
# \author{...}) cycle through the rest.
AUTHOR_COLORS = ["red", "blue", "orange", "purple", "brown", "teal", "magenta"]

# Per-document configuration, next to the main file.
CONFIG_NAME = ".latex-draft.cfg"
ACCEPT_STYLES = ("added", "highlight")
DEFAULT_CONFIG = {"keep_markup": True, "color": "darkgreen", "style": "added"}
# xcolor knows "gray", not "grey": accept the common spellings so a config
# cannot break every compile with an undefined color.
COLOR_ALIASES = {"grey": "gray", "lightgrey": "lightgray", "darkgrey": "darkgray", "darkgreen": "green!60!black"}
# Accepted LLM text, kept marked so the review PDF still shows what the model
# wrote; the changes package prints it plainly in final mode. \replaced is
# included because its arguments are equally un-nestable: a draft built
# inside \replaced{...}{...} produces markup the changes package cannot
# compile. Group 1 is the command name.
PROVENANCE_CMD_RE = re.compile(r"\\(added|highlight|replaced)(?:\[[^\]]*\])?\{")
# Content that cannot sit inside \added{...}/\deleted{...}: the markup is
# ulem text markup. Environment boundaries and alignment tabs are compile
# errors ("Not in outer par mode", "Missing } inserted") that would otherwise
# surface only after drafting reported success. A plain \\ line break is
# fine inside the markup; a row break inside a table is not (see
# _alignment_depth).
UNMARKABLE_RE = re.compile(r"\\(?:begin|end)\{[^}]*\}|(?<!\\)&")
# Sectioning titles: moving arguments typeset in restricted horizontal mode
# (LR), where \added/\deleted cannot run ("Not allowed in LR mode").
SECTIONING_RE = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph)"
    r"\*?(?:\[[^\]]*\])?\s*\{"
)
ALIGNMENT_ENVIRONMENTS = (
    "tabular", "tabular*", "tabularx", "tabulary", "array", "longtable",
)
ALIGN_BEGIN_RE = re.compile(
    r"\\begin\{(" + "|".join(map(re.escape, ALIGNMENT_ENVIRONMENTS)) + r")\}"
)
ALIGN_END_RE = re.compile(
    r"\\end\{(" + "|".join(map(re.escape, ALIGNMENT_ENVIRONMENTS)) + r")\}"
)
# Display math: \added/\deleted are text markup, so they cannot be typeset in
# math mode ("Missing $ inserted"). A label or a formula edit therefore has to
# happen outside the environment, or by hand.
MATH_ENVIRONMENTS = (
    "equation", "equation*", "align", "align*", "alignat", "alignat*",
    "flalign", "flalign*", "gather", "gather*", "multline", "multline*",
    "eqnarray", "eqnarray*", "displaymath", "math",
)
MATH_BEGIN_RE = re.compile(
    r"\\begin\{(" + "|".join(map(re.escape, MATH_ENVIRONMENTS)) + r")\}"
)
MATH_END_RE = re.compile(
    r"\\end\{(" + "|".join(map(re.escape, MATH_ENVIRONMENTS)) + r")\}"
)
# \[ ... \] on its own; \\[2mm] (a line break with spacing) must not count.
MATH_OPEN_RE = re.compile(r"(?<!\\)\\\[")
MATH_CLOSE_RE = re.compile(r"\\\]")
# Floats: a margin note cannot go inside one either ("Float(s) lost"), so a
# note about a change to a float is placed after the float's end.
FLOAT_ENVIRONMENTS = (
    "figure", "figure*", "table", "table*", "wrapfigure", "wraptable",
    "subfigure", "sidewaysfigure", "sidewaystable",
)
FLOAT_BEGIN_RE = re.compile(
    r"\\begin\{(" + "|".join(map(re.escape, FLOAT_ENVIRONMENTS)) + r")\}"
)
FLOAT_END_RE = re.compile(
    r"\\end\{(" + "|".join(map(re.escape, FLOAT_ENVIRONMENTS)) + r")\}"
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
    keep_markup = false        # opt out: merge accepted text plain (default keeps it marked)
    color = blue              # xcolor name for the agent (default: dark green)
    style = highlight         # added (colored underline) or highlight
    ```

    Missing keys keep their defaults; a malformed value is a clean error, not
    a traceback (a bad color would otherwise break every compile).
    """
    config = dict(DEFAULT_CONFIG)
    config["color"] = COLOR_ALIASES.get(config["color"], config["color"])
    path = tex.parent / CONFIG_NAME
    if not path.is_file():
        return config
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        if parser.has_section("accept"):
            section = parser["accept"]
            config["keep_markup"] = section.getboolean("keep_markup", fallback=DEFAULT_CONFIG["keep_markup"])
            config["color"] = section.get("color", fallback=DEFAULT_CONFIG["color"]).strip() or None
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


def lock_path(tex: Path) -> Path:
    return tex.parent / (tex.name + ".latex-draft.lock")


@contextlib.contextmanager
def _session_lock(tex: Path):
    """Serialize concurrent latex-draft invocations on the same document.

    Rapid successive `draft-*` calls (one shell line, or parallel agent
    calls) otherwise race on the state file and the on-disk files: the loser
    aborts with "file changed while this change was being prepared" or
    crashes on the shared temp file. An exclusive flock on
    `<main>.latex-draft.lock` makes them queue instead — each call then sees
    the state the previous one saved. Where flock is unavailable, fall back
    to the hash re-checks alone.
    """
    lock = None
    if fcntl is not None:
        try:
            lock = open(lock_path(tex), "a+")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except OSError:
            if lock is not None:
                lock.close()
            lock = None
    try:
        yield
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()


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
            state.setdefault("label_notes", [])
            # Hashes used to be keyed by absolute path; a copied or moved
            # project then looked hand-edited. Re-key them relative to the
            # main file's directory.
            state["file_hashes"] = {
                _rel_key(tex, Path(p)): h
                for p, h in (state.get("file_hashes") or {}).items()
            }
            # Draft records used to store only the basename of the target
            # file; migrate them to the path relative to the main file's
            # directory, resolved through the input/include graph.
            _migrate_record_files(tex, state)
            return state
        except (json.JSONDecodeError, ValueError):
            corrupt = True
    state = {
        "drafts": {},
        "file_hashes": {},
        "turn": 0,
        "full_backup_done": False,
        "profiles_defined": False,
        "injected_preamble": [],
        "label_notes": [],
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


def _migrate_record_files(tex: Path, state: dict) -> None:
    r"""Rewrite legacy basename `file` fields in draft records to the path
    relative to the main file's directory (``parts/intro.tex``), resolved
    through the input/include graph so subdirectory inputs keep working.
    Already-relative records are left untouched."""
    deps = {d.name: d for d in tex_inputs(tex)}
    for rec in state.get("drafts", {}).values():
        fname = rec.get("file")
        if not fname or fname == tex.name or "/" in fname:
            continue
        dep = deps.get(fname)
        if dep is not None:
            rec["file"] = _rel_key(tex, dep)


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
            rec = draft_record(
                did, "unknown", preview, block_number=_next_block_number(state)
            )
            rec["file"] = _rel_key(tex, f)
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
    half written. The temp name carries the pid: two concurrent invocations
    must not share one temp file (the second os.replace would then fail with
    FileNotFoundError and lose its change).
    """
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        write_tex(tmp, text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


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
VERBATIM_REGION_RE = re.compile(
    r"\\begin\{(verbatim\*?|lstlisting|Verbatim|alltt)\}.*?\\end\{\1\}",
    re.DOTALL,
)


def _in_verbatim(text: str, index: int) -> bool:
    """Whether the character offset sits inside a verbatim-like environment.

    Verbatim bodies are typeset literally: \added/\deleted there would be
    printed as markup in the review PDF, so drafts must refuse them.
    """
    return any(m.start() <= index < m.end() for m in VERBATIM_REGION_RE.finditer(text))


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
    # Re-derive the draft registry from the markers now in the document: a
    # hand restore (e.g. `cp` from a backup) can remove blocks the registry
    # still calls pending — close those instead of leaving phantom drafts.
    closed = _reconcile_draft_status(tex, state)
    save_state(tex, state)
    print(f"latex-draft: WARNING: {warning}", file=sys.stderr)
    print(
        "latex-draft: the file was edited by hand; it was recorded in the "
        "changelog and adopted. Re-read the file before drafting further "
        "changes (`status`, `changelog`).",
        file=sys.stderr,
    )
    if closed:
        print(
            f"latex-draft: {closed} pending draft(s) no longer in the document "
            "(their blocks were removed by the hand edit); marked as rejected "
            "— see `list`",
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
    # One snapshot per invocation at most: two mutations within the same
    # second (a draft plus the one-time profile setup) share the timestamp,
    # so bump the sequence when the candidate name already exists.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{stamp}-{seq + 1:06d}.tex"
    while dest.exists():
        seq += 1
        dest = bdir / f"{stamp}-{seq + 1:06d}.tex"
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
    # The agent's mark in the PDF is a symbol (a filled circle in its color),
    # not a text tag. The changes package has no per-author markup
    # (\setauthormarkup is global and would restyle every profile), so the
    # agent's profile redefines \Changes@output@author instead: that is the
    # command which receives the bare author id (as \Changes@added@id, an
    # expandable macro holding the id), so a \pdfstrcmp on its argument
    # reliably matches. For the AI profile it emits the circle; every other
    # author is delegated to the original definition, which prints the id
    # (wrapped in brackets by authormarkup=brackets). \pdfstrcmp is a
    # pdftex/luatex primitive, so this works under pdflatex and lualatex.
    if author == AGENT_AUTHOR:
        setup = "\n".join([
            r"\makeatletter",
            r"\let\latexdraft@orig@out\Changes@output@author",
            r"\def\Changes@output@author#1{\ifnum\pdfstrcmp{#1}{" + AGENT_AUTHOR + r"}=0" + AGENT_MARK + r"\else\latexdraft@orig@out{#1}\fi}",
            r"\makeatother",
        ])
        if color:
            return rf"\definechangesauthor[color={color}]{{{author}}}" + "\n" + setup
        return rf"\definechangesauthor{{{author}}}" + "\n" + setup
    if color:
        return rf"\definechangesauthor[color={color},name={author}]{{{author}}}"
    return rf"\definechangesauthor[name={author}]{{{author}}}"


def _documentclass_end(lines: list[str]) -> int:
    r"""Line index just past the full \documentclass statement.

    Handles a multi-line options bracket: \documentclass[\n  12pt,\n]{article}
    — returns the index after the line containing the closing ]{...}.
    """
    for i, line in enumerate(lines):
        if line.lstrip("\ufeff").startswith("\\documentclass"):
            # Track bracket balance from this line onward
            depth = line.count("[") - line.count("]")
            j = i
            while depth > 0 and j + 1 < len(lines):
                j += 1
                depth += lines[j].count("[") - lines[j].count("]")
            # Now find the {class} after the closing ]
            rest = lines[j]
            brace = rest.find("]")
            if brace != -1:
                after = rest[brace + 1:]
                if "{" in after:
                    # check the class brace closes on this line
                    bdepth = after.count("{") - after.count("}")
                    while bdepth > 0 and j + 1 < len(lines):
                        j += 1
                        after = lines[j]
                        bdepth += after.count("{") - after.count("}")
            return j + 1
    return 0


def ensure_changes_package(
    text: str,
    author: str = DEFAULT_AUTHOR,
    color: str | None = None,
    with_agent_setup: bool = True,
) -> str:
    r"""Inject the changes package (draft mode) plus a \definechangesauthor for
    the given author. Idempotent per author. For the agent, author_line also
    installs the symbol-mark wrapper; pass with_agent_setup=False to inject
    only the package and the plain \definechangesauthor (used while drafting,
    before ensure_author_profiles has run)."""
    lines = split_lines(text)
    pkg_re = re.compile(r"\\usepackage(\[[^\]]*\])?\{changes\}")
    author_re = re.compile(
        r"\\definechangesauthor(\[[^\]]*\])?\{" + re.escape(author) + r"\}"
    )
    have_pkg = any(pkg_re.search(line) for line in lines)
    have_author = any(author_re.search(line) for line in lines)
    if have_pkg and have_author:
        return text
    insert_at = _documentclass_end(lines)
    if not have_pkg:
        # The changes package must be loaded before any \definechangesauthor.
        # The agent's profile injects the optioned package line; a human
        # author gets the plain draft line.
        pkg_line = CHANGES_PACKAGE_LINE if author == AGENT_AUTHOR else CHANGES_PACKAGE
        text = insert_lines_at(text, insert_at, [pkg_line])
        insert_at += 1
    if not have_author:
        # Author definitions go after the changes package, never before it.
        for i, line in enumerate(split_lines(text)):
            if pkg_re.search(line):
                insert_at = i + 1
                break
        if author == AGENT_AUTHOR and not with_agent_setup:
            line = rf"\definechangesauthor[color={color or _author_color(text, author)}]{{{author}}}"
        else:
            line = author_line(author, color or _author_color(text, author))
        text = insert_lines_at(text, insert_at, split_lines(line))
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
    new_text = ensure_changes_package(text, author, color, with_agent_setup=False)
    if margin:
        new_text = ensure_todonotes_package(new_text)
    if new_text == text:
        return
    _record_injected(state, text, new_text)
    mutate(tex, state, f"ensure preamble for drafts (author {author})", lambda t: new_text)


def _author_color(text: str, author: str) -> str | None:
    """A stable color per author. The agent always gets the first palette
    entry (its own profile); human authors and --author alternatives cycle
    through the rest by definition order."""
    if author == AGENT_AUTHOR:
        return AUTHOR_COLORS[0]
    others = [
        m.group(1)
        for m in re.finditer(
            r"\\definechangesauthor(?:\[[^\]]*\])?\{([^}]+)\}", text
        )
        if m.group(1) not in (author, AGENT_AUTHOR)
    ]
    return AUTHOR_COLORS[1 + len(others) % (len(AUTHOR_COLORS) - 1)]


_AUTHOR_CMD_RE = re.compile(r"\\author\{((?:[^{}]|\{[^{}]*\})*)\}")
_AUTHOR_SEP_RE = re.compile(r"\\and|,")


def paper_author_ids(text: str) -> list[str]:
    r"""Author profile ids parsed from the paper's \author{...} command(s).

    `\author{Alice Smith \and Bob Jones}` yields ["AliceSmith", "BobJones"]
    (name with spaces/TeX markup stripped -- a valid changes-author id that
    stays distinguishable in the PDF). [] when there is no usable \author."""
    ids: list[str] = []
    for m in _AUTHOR_CMD_RE.finditer(text):
        for part in _AUTHOR_SEP_RE.split(m.group(1)):
            name = re.sub(r"\\[a-zA-Z@]+\*?(?:\[^\]]*\])?\{[^{}]*\}", "", part)
            name = re.sub(r"\\[a-zA-Z@]+", "", name)
            ident = re.sub(r"[^0-9A-Za-z]", "", name)
            if ident and ident not in ids:
                ids.append(ident)
    return ids


def ensure_author_profiles(tex: Path, state: dict) -> None:
    r"""One-time first-draft setup: define a changes profile (id + own color)
    for the agent and for every author named in the paper's \author{...}.

    The agent (AI, circle mark, first palette color) is defined first; human
    authors follow in paper order, each with its own color. The agent drafts
    only under its own AI profile; the human profiles exist so text the
    humans write and `--author <name>` drafts render in their own color.
    Idempotent per author (a profile that already exists is never redefined);
    recorded through the changelog like every other mutation. The full agent
    block (definechangesauthor + symbol-mark wrapper) is inserted right after
    the changes package, AI first; a human profile is a single author line.
    """
    text = read_tex(tex)
    config = load_config(tex)
    agent_color = config["color"] or AUTHOR_COLORS[0]
    ids = paper_author_ids(text)
    lines, eols = split_eol(text)
    nl = newline_of(text)
    ins = _documentclass_end(lines)
    pkg_re = re.compile(r"\\usepackage(\[[^\]]*\])?\{changes\}")
    author_re = re.compile(
        r"\\definechangesauthor(?:\[[^\]]*\])?\{([^}]*)\}"
    )
    have_pkg = any(pkg_re.search(l) for l in lines)
    defined = [m.group(1) for m in author_re.finditer(text)]
    agent_setup_done = any(l.strip() == r"\makeatletter" for l in lines)
    agent_lines = split_lines(author_line(AGENT_AUTHOR, agent_color))
    new_lines: list[str] = []
    if not have_pkg:
        new_lines.append(CHANGES_PACKAGE_LINE)
    if AGENT_AUTHOR not in defined:
        new_lines += agent_lines
    elif not agent_setup_done:
        # package + definechangesauthor were injected by the draft handler;
        # add only the symbol-mark wrapper (agent block minus its first line)
        new_lines += agent_lines[1:]
    n_human = sum(1 for d in defined if d != AGENT_AUTHOR)
    for author_id in ids:
        if author_id == AGENT_AUTHOR or author_id in defined:
            continue
        color = AUTHOR_COLORS[1 + n_human % (len(AUTHOR_COLORS) - 1)]
        n_human += 1
        new_lines += split_lines(author_line(author_id, color))
    if not new_lines:
        return
    if not have_pkg:
        lines[ins:ins] = new_lines
        eols[ins:ins] = [nl] * len(new_lines)
    else:
        pkg_idx = next((i for i, l in enumerate(lines) if pkg_re.search(l)), None)
        if pkg_idx is None:
            return
        # If the agent definechangesauthor already exists (draft handler put
        # it right after the package), insert the wrapper after it; human
        # profiles go right after the package line.
        insert_at = pkg_idx + 1
        for i in range(pkg_idx + 1, len(lines)):
            if author_re.search(lines[i]):
                insert_at = i + 1
                break
        lines[insert_at:insert_at] = new_lines
        eols[insert_at:insert_at] = [eols[pkg_idx]] * len(new_lines)
    block_text = join_eol(lines, eols)
    if block_text == text:
        return
    _record_injected(state, text, block_text)
    mutate(
        tex, state, "define changes profiles (AI + paper authors)",
        lambda t: block_text, setup=True,
    )


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
    r"|\\setauthormarkup\\{.*}"
    r"|\\makeat(?:letter|other)"
    r"|\\let\\latexdraft@.*"
    r"|\\def\\Changes@output@author#1\{.*}"
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
        if entry.get("kind") not in ("change", "setup"):
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
    r"""Add the changes package (and todonotes) to a main file's preamble.

    The injected lines are recorded in the state, so the strip at resolve time
    removes exactly those and never a line the user wrote. `margin` also
    loads todonotes, for a margin-note comment. The agent profile (AI +
    symbol-mark wrapper) is added separately, exactly once, by
    ensure_author_profiles after the first successful body draft; only the
    package and the per-command author line are ensured here.
    """
    color = load_config(tex)["color"] if author == DEFAULT_AUTHOR else None
    prepared = ensure_changes_package(text, author, color, with_agent_setup=False)
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
    """Accepted agent text, kept marked with changes markup.

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
    # Paragraph interiors were flattened for splitting; write them back in
    # the document's own separator so a CRLF file keeps CRLF everywhere.
    return to_newline((newline * 2).join(parts), newline)


def _has_provenance(tex: Path) -> bool:
    """Whether any accepted (kept-marked) LLM text is still in the document."""
    for f in watched_files(tex):
        text = _read_or_none(f)
        if text and PROVENANCE_CMD_RE.search(text):
            return True
    return False


def _has_label_notes(tex: Path, state: dict) -> bool:
    """Whether a note this tool added next to an unmarked label is still there.

    Only notes the tool recorded in the state count: a document that already
    uses the changes package itself (a co-author's tracked edits) must not
    keep the injected preamble alive.
    """
    notes = state.get("label_notes") or []
    if not notes:
        return False
    for f in watched_files(tex):
        text = _read_or_none(f)
        if text and any(note in text for note in notes):
            return True
    return False


def _provenance_span(text: str, index: int) -> tuple[int, int] | None:
    """Span of the `\\added{...}`/`\\highlight{...}`/`\\replaced{...}{...}`
    command containing `index`.

    The changes package cannot nest its commands, so a draft must not be built
    inside accepted provenance markup. For `\\replaced[...]{new}{old}` both
    arguments are covered (the whitespace between them may be a line break).
    """
    for m in PROVENANCE_CMD_RE.finditer(text):
        if m.start() > index:
            break

        def arg_end(j: int) -> int | None:
            depth = 1
            while j < len(text) and depth:
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                j += 1
            return j if depth == 0 else None

        end = arg_end(m.end())
        if end is None:
            continue
        if m.group(1) == "replaced":
            k = end
            while k < len(text) and text[k] in " \t\r\n":
                k += 1
            if k < len(text) and text[k] == "{":
                end2 = arg_end(k + 1)
                if end2 is not None:
                    end = end2
        if m.start() <= index < end:
            return m.start(), end
    return None


def _block_sup(n: int) -> str:
    """A superscript block number for the start of drafted text."""
    return f"\\textsuperscript{{{n}}}"


BLOCK_SUP_RE = re.compile(r"^\\textsuperscript\{[0-9]+\} ")
BLOCK_SUP_UNANCHORED_RE = re.compile(BLOCK_SUP_RE.pattern[1:])  # same, without the ^ anchor



def _strip_block_sup(text: str) -> str:
    """Remove the leading \\textsuperscript{N} block marker from accepted text."""
    return BLOCK_SUP_RE.sub("", text, count=1)


_WORD_RE = re.compile(r"\$[^$]*\$|\\[a-zA-Z@]+(?:\[[^\]]*\])?\{[^{}]*\}|\S+\s*|\s+")  # math spans and \cmd{...} stay one token


FRAGILE_IN_DELETED_RE = re.compile(
    r"\\(cite|footnote|url|marginpar)(?:\[[^\]]*\])?\{"
)


def _protect_fragile_in_deleted(content: str) -> str:
    r"""Wrap fragile commands in \mbox{...} so they survive inside
    \deleted{...}: ulem's strike-through (\sout) expands its argument in a
    context where the @ catcode is lost, so \cite/\footnote/\url/\marginpar
    fail there with "Undefined control sequence" (\mbox boxes them first).
    \added needs no protection (plain coloured text)."""
    out: list[str] = []
    i = 0
    for m in FRAGILE_IN_DELETED_RE.finditer(content):
        depth = 1
        j = m.end()
        while j < len(content) and depth:
            ch = content[j]
            if ch == "\\" and j + 1 < len(content):
                j += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        if depth:  # unbalanced brace: leave it (brace escaping handles it)
            continue
        out.append(content[i:m.start()])
        out.append("\\mbox{" + content[m.start():j] + "}")
        i = j
    out.append(content[i:])
    return "".join(out)


def _unprotect_fragile(content: str) -> str:
    r"""Reverse _protect_fragile_in_deleted: \mbox{\cite{...}} -> \cite{...},
    so a reject restores the original bytes (never leaves the \mbox behind)."""
    return re.sub(
        r"\\mbox\{(\\(?:cite|footnote|url|marginpar)(?:\[[^\]]*\])?\{[^{}]*\})\}",
        r"\1",
        content,
    )


SIBLING_WRAPPER_RE = re.compile(
    r"\\(?:added|deleted|highlight|footnote|todo)(?:\[[^\]]*\])?\{"
)


def _brace_matching_sub(text: str, cmd: str, keep: bool, eat_space: bool = False) -> str:
    r"""Replace every \cmd[..]{balanced} in text by its inner content
    (keep=True) or by "" (keep=False), with real brace matching so nested
    braces (\emph{...}, $x^{2}$) unwrap correctly. Flat-regex substitution
    cannot handle those and used to leave markup in the file on accept.

    With eat_space=True and keep=False, one space directly after a removed
    command is removed with it: the word diff writes `\deleted{x} \added{y}`
    with a separator space, and dropping only the command would leave a
    doubled space (or a stray leading one at a line start). Spaces that were
    in the document before the draft (comment alignment, indentation) are
    never adjacent to a wrapper and so are not touched. The heuristic is
    exact only for diffs verified byte-exact by _inline_diff_exact — draft
    paths that cannot verify fall back to the whole-span form, whose views
    resolve exactly under this rule."""
    pat = re.compile(rf"\\{cmd}(?:\[[^\]]*\])?\{{")
    out: list[str] = []
    i = 0
    while True:
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:m.start()])
        depth = 1
        j = m.end()
        start = j
        while j < len(text) and depth:
            ch = text[j]
            if ch == "\\" and j + 1 < len(text):
                j += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        if depth:  # unbalanced: leave the command untouched
            out.append(text[m.start():m.end()])
            i = m.end()
            continue
        out.append(text[start:j - 1] if keep else "")
        i = j
        if eat_space and not keep and i < len(text) and text[i] == " ":
            i += 1


def _inline_math_masked(text: str) -> str:
    r"""Copy of text with $...$ and \(...\) spans blanked, so command-safety
    checks only see text-mode commands (math content compiles fine inside
    \deleted/\added)."""
    res = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "$":
            j = text.find("$", i + 1)
            if j < 0:
                res.append(text[i:])
                break
            res.append(" " * (j + 1 - i))
            i = j + 1
        else:
            res.append(text[i])
            i += 1
    s = "".join(res)
    return re.sub(r"\\\([^)]*\\\)", lambda m: " " * len(m.group(0)), s)


INLINE_SAFE_CMDS = {
    "cite", "ref", "eqref", "pageref", "cref", "Cref", "label",
    "emph", "textit", "textbf", "textsl", "textsc", "textup", "underline",
    "text", "textrm", "textsf", "texttt", "textmd", "textnormal",
    "textsuperscript", "mbox", "hbox", "vspace", "hspace", "quad", "qquad",
    "enspace", "thinspace", "negthinspace", "noindent", "linebreak", "newline",
    "today", "TeX", "LaTeX", "LaTeXe",
}


def _inline_text_safe(text: str) -> bool:
    r"""Whether every text-mode command in text is safe for an inline point
    edit. Structural commands (\section, \begin, \includegraphics, ...) are
    not: those still use the whole-line form."""
    masked = _inline_math_masked(text)
    return all(
        m.group(1) in INLINE_SAFE_CMDS
        for m in re.finditer(r"\\([a-zA-Z@]+)", masked)
    )


def _deleted_opts(author: str) -> str:
    r"""Optional-argument string for \deleted: the author id. Struck-out
    text renders with the package default markup (struck through, in the
    author's color, with the ordinary (Name) tag)."""
    return f"[id={author}]"


def _deleted_cmd(author: str) -> str:
    return "\\deleted" + _deleted_opts(author) + "{"


def _ws_tokens(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split text into (leading whitespace, [(core, trailing ws), ...]).

    Math spans and \\cmd{...} stay one token (via _WORD_RE). Matching runs
    on the cores only: a word move flips which neighbour carries the
    separating space ("reces (x)" -> "(x) reces" moves the space from after
    "reces" to after ")"), and matching raw tokens would treat "App.~A)"
    and "App.~A) " as different words — the moved word then drags the
    bracketed span into a replace opcode and the diff strikes and re-emits
    the parenthesis.
    """
    lead = ""
    toks: list[tuple[str, str]] = []
    for tok in _WORD_RE.findall(text):
        if not tok.strip():
            if toks:
                toks[-1] = (toks[-1][0], toks[-1][1] + tok)
            else:
                lead += tok
            continue
        core = tok.rstrip()
        toks.append((core, tok[len(core) :]))
    return lead, toks


def _inline_views(body: str) -> tuple[str, str]:
    r"""(accept view, reject view) of an inline point-edit body, using the
    exact transforms the resolver applies: accept drops \deleted (eating one
    following space) and unwraps \added; reject unwraps \deleted and drops
    \added (eating one following space)."""
    acc = _brace_matching_sub(body, "added", keep=True)
    acc = _brace_matching_sub(acc, "deleted", keep=False, eat_space=True)
    rej = _brace_matching_sub(body, "deleted", keep=True)
    rej = _brace_matching_sub(rej, "added", keep=False, eat_space=True)
    rej = _unprotect_fragile(rej)
    return acc, rej


def _inline_diff_exact(
    before: str, old: str, after: str, new: str, diff: str
) -> bool:
    """Whether the diffed block body resolves view-exactly: accept must give
    the accept view of `before + new + after`, reject the reject view of
    `before + old + after`. The comparison runs through the same resolver on
    both sides, so accepted markup already standing in the context (`\added`
    inside a host block's plain text) strips out equally from both and does
    not mask a bad diff -- but a diff that eats into the neighbouring text
    (e.g. a word move that welds two words) still fails and is discarded in
    favour of the whole-span fallback."""
    body = before + diff + after
    acc, rej = _inline_views(body)
    return (
        acc == _inline_views(before + new + after)[0]
        and rej == _inline_views(before + old + after)[1]
    )


def _word_diff_verified(
    before: str, old: str, after: str, new: str, author: str
) -> str | None:
    """The best word-level inline diff of old -> new that resolves
    byte-exactly in the context `before`/`after`: accept must give
    `before + new + after`, reject must give `before + old + after`. Tries
    both equal-span whitespace variants (and the separator form); returns
    None when no variant resolves exactly — the caller then uses the
    whole-span form, which always does. Every draft path that splices a word
    diff into an existing block body MUST go through this check: an
    unverified word move used to resolve accept correctly but corrupt the
    line on reject (the moved separator space was lost)."""
    for variant in (("new", False), ("new", True), ("old", False), ("old", True)):
        cand = _word_diff_inline(
            old, new, author, equal_variant=variant[0], separator=variant[1]
        )
        if cand is not None and _inline_diff_exact(before, old, after, new, cand):
            return cand
    return None


def _word_diff_inline(
    old: str, new: str, author: str, equal_variant: str = "new",
    separator: bool = False,
) -> str | None:
    """Word-level inline diff of `old` -> `new` as changes markup.

    Returns None when a word-level diff is not meaningful (multi-paragraph
    text, or an empty side). The result wraps only the changed words in
    \\deleted{...}/\\added{...}, leaving the unchanged words plain, so a
    point edit inside a long source line strikes out only the changed words
    in the review PDF. Whitespace at the outer edges of a changed span is
    moved outside the markup argument so the accepted text keeps its spacing.

    Tokens match on whitespace-normalized cores, so a moved word diffs as
    delete-at-the-old-spot + insert-at-the-new-spot with the bracketed span
    it crossed left plain. An unchanged span whose whitespace differs between
    old and new (the moved separator space) is emitted from `equal_variant`
    ("old" or "new"); the caller verifies both views byte-exactly and falls
    back to the whole-span form when neither variant resolves exactly.
    """
    if old == new:
        return None
    if "\n\n" in old or "\n\n" in new or "\r\n\r\n" in old or "\r\n\r\n" in new:
        return None  # multi-paragraph: keep whole-line blocks
    lead_a, ta = _ws_tokens(old)
    lead_b, tb = _ws_tokens(new)
    if not ta or not tb:
        return None
    if lead_a != lead_b:
        return None  # differing leading whitespace: keep the whole-span form
    sm = difflib.SequenceMatcher(
        a=[c for c, _ in ta], b=[c for c, _ in tb], autojunk=False
    )
    parts = []
    if lead_a:
        parts.append(lead_a)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            old_slice = "".join(c + w for c, w in ta[i1:i2])
            new_slice = "".join(c + w for c, w in tb[j1:j2])
            if old_slice != new_slice and equal_variant == "old":
                parts.append(old_slice)
            else:
                # default (and any drifted span under the "new" variant):
                # the new side's spacing — accept needs it, and reject gets
                # the old spacing back from the whitespace kept inside the
                # adjacent \\deleted/\\added arguments
                parts.append(new_slice)
        elif tag == "delete":
            parts.append((_deleted_cmd(author), _protect_fragile_in_deleted("".join(c + w for c, w in ta[i1:i2])), "}"))
        elif tag == "insert":
            parts.append(("\\added[id=" + author + "]{", "".join(c + w for c, w in tb[j1:j2]), "}"))
        elif tag == "replace":
            parts.append((_deleted_cmd(author), _protect_fragile_in_deleted("".join(c + w for c, w in ta[i1:i2])), "}"))
            parts.append(("\\added[id=" + author + "]{", "".join(c + w for c, w in tb[j1:j2]), "}"))
    # Flatten: strings pass through; tuples become markup commands with
    # edge whitespace moved outside the braces. An adjacent
    # \deleted{...}\added{...} pair whose deleted side carries no trailing
    # space gets one separator space between them on request: reject drops
    # the \added and eats the following space, so without the separator the
    # space before the next word is lost.
    out = []
    prev_del_trail: str | None = None
    for p in parts:
        if isinstance(p, str):
            out.append(p)
            prev_del_trail = None
            continue
        cmd, content, close = p
        is_del = cmd.startswith("\\deleted")
        stripped = content.strip()
        lead = content[: len(content) - len(content.lstrip())]
        trail = content[len(content.rstrip()) :]
        if (
            separator
            and not is_del
            and prev_del_trail is not None
            and not prev_del_trail
            and not lead
        ):
            out.append(" ")
        out.append(lead + cmd + _escape_unbalanced_braces(stripped) + close + trail)
        prev_del_trail = trail if is_del else None
    return "".join(out)



def build_inline_block(
    did: str,
    before: str,
    old: str,
    after: str,
    new: str,
    author: str = DEFAULT_AUTHOR,
    block_number: int | None = None,
) -> str:
    """A draft block for a mid-line point edit: markers on their own lines,
    the line's text around the edit kept plain, only the changed words
    wrapped in \\deleted/\\added inside the block.

    `before`/`after` are the unchanged text on the same source line before
    and after `old`; `new` replaces `old`. The block body is a single line:
    `before` + inline diff + `after`.
    """
    diff = _word_diff_verified(before, old, after, new, author)
    if diff is None:
        # fall back: wrap old->new directly; a pure deletion (empty new)
        # emits no \added — an empty \\added{} is a no-op command and
        # a stray author mark in the review PDF
        diff = _deleted_cmd(author) + _escape_unbalanced_braces(_protect_fragile_in_deleted(old)) + "}"
        if new:
            diff += "\\added[id=" + author + "]{" + _escape_unbalanced_braces(new) + "}"
    if block_number:
        diff = _block_sup(block_number) + " " + diff
    # before/after are the raw fragments of the same source line around the
    # replaced span, so before + diff + after reproduces the original spacing
    # exactly; do NOT insert separator spaces (that puts a space before
    # punctuation when --old abuts it, e.g. "carefully" + ".").
    body = before + diff + after
    return _marker_block(did, body)



def build_block(
    did: str,
    old: str | None = None,
    new: str | None = None,
    author: str = DEFAULT_AUTHOR,
    block_number: int | None = None,
) -> str:
    parts = [f"% latex-draft:{did}"]
    if old is not None:
        for para in split_paragraphs(old):
            parts.append(
                _deleted_cmd(author) + _escape_unbalanced_braces(_protect_fragile_in_deleted(para)) + "}"
            )
            parts.append("")
    if new is not None:
        for i, para in enumerate(split_paragraphs(new)):
            prefix = _block_sup(block_number) + " " if (block_number and i == 0) else ""
            parts.append(
                rf"\added[id={author}]{{{prefix}{_escape_unbalanced_braces(para)}}}"
            )
            parts.append("")
    parts.append(f"% latex-draft-end:{did}")
    return "\n".join(parts)


# Comment text is typeset verbatim inside \footnote/\todo: a math-mode-only
# command outside math (\mathrm{d}x) is a fatal LaTeX error. Commands in this
# set are safe in text mode and stay live in the comment; any other \command
# outside math mode is escaped so it renders literally instead of killing the
# build. Inside $...$ / $$...$$ / \[...\] everything stays as written.
TEXT_SAFE_CMDS = {
    "emph", "textbf", "textit", "textsl", "textsc", "texttt", "textsf",
    "textrm", "textup", "textnormal", "underline", "footnote", "cite",
    "citet", "citep", "citealp", "citealt", "ref", "eqref", "pageref",
    "autoref", "cref", "Cref", "label", "url", "href", "oe", "OE", "ae",
    "AE", "aa", "AA", "o", "O", "l", "L", "i", "j", "ss", "textbackslash",
    "textbar", "textless", "textgreater", "textasciitilde",
    "textasciicircum",
}
_BACKSLASH_CMD_RE = re.compile(r"\\([a-zA-Z]+)")


def _sanitize_comment_text(text: str) -> str:
    r"""Make comment text safe to typeset verbatim (see TEXT_SAFE_CMDS).

    Outside math mode, \command sequences that are not known text-safe are
    rendered literally as \textbackslash{}cmd — e.g. a dictated "the term is
    \mathrm{d}x" becomes visible text "\mathrm{d}x" instead of the fatal
    "! LaTeX Error: \mathrm allowed only in math mode."
    """
    out: list[str] = []
    i = 0
    n = len(text)
    math = False
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "[":
                math = True
                out.append(text[i : i + 2])
                i += 2
                continue
            if nxt == "]":
                math = False
                out.append(text[i : i + 2])
                i += 2
                continue
            m = _BACKSLASH_CMD_RE.match(text, i)
            if m:
                cmd = m.group(1)
                if not math and cmd not in TEXT_SAFE_CMDS:
                    out.append("\\textbackslash{}" + cmd)
                else:
                    out.append(m.group(0))
                i = m.end()
                continue
            out.append(text[i : i + 2])  # \\, \%, \&, ... stay as they are
            i += 2
            continue
        if c == "$":
            if text.startswith("$$", i):
                math = not math
                out.append("$$")
                i += 2
                continue
            math = not math
            out.append("$")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def build_comment_block(
    did: str, text: str, margin: bool = False, block_number: int | None = None
) -> str:
    """A commenter-mode draft that never alters the body text.

    Default is a footnote; with margin=True a todonotes margin note, which is
    safe inside captions, section titles, tables and math (a footnote is not)
    and does not shift pagination. The text is sanitized first: commands that
    are only valid in math mode are escaped outside math mode so the comment
    cannot break the build (use $...$ for math in comments).
    """
    safe = _escape_unbalanced_braces(_sanitize_comment_text(text))
    if block_number:
        safe = _block_sup(block_number) + " " + safe
    cmd = "\\todo" if margin else "\\footnote"
    return _marker_block(did, f"{cmd}{{{safe}}}")


def build_preamble_block(did: str, text: str, block_number: int | None = None) -> str:
    """A preamble draft block: raw LaTeX between marker comments, NOT
    wrapped in \\added/\\deleted.

    Preamble commands (\\renewcommand, \\setlength, \\captionsetup, ...)
    cannot go through the changes package: \\added typesets its argument
    immediately ("Missing \\begin{document}" in the preamble), expands it
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


def _insert_position(
    lines: list[str],
    block: str,
    after: str | None = None,
    before: str | None = None,
    at_line: int | None = None,
    marked: bool = True,
) -> int:
    """Insert `block` (whole lines) at an anchor.

    `marked` says whether the block will be wrapped in the changes markup; an
    unmarked insertion carries none, so it may go where markup cannot (inside
    math, inside a table) — that is how a `\\label` is added.
    """
    if at_line is not None:
        if at_line < 1:
            raise DraftError(f"--at-line must be >= 1, got {at_line}")
        if at_line > len(lines):
            raise DraftError(
                f"--at-line {at_line} is beyond the end of the file "
                f"({len(lines)} lines)"
            )
        # Raw file line numbers, 1-based, exactly as an editor shows them
        # (draft marker lines and the auto-injected preamble included).
        # Never insert before \documentclass or into the injected preamble.
        pos = at_line - 1
        pos = max(pos, _documentclass_end(lines))
        while pos < len(lines) and injected_preamble_line(lines[pos]):
            pos += 1
    else:
        # Anchors match the text a reader sees only: not inside % comments
        # and not inside pending draft blocks (whose bodies repeat text).
        needle = after if after is not None else before
        search = _searchable_lines(lines)
        hits = [i for i, line in enumerate(search) if needle in line]
        if len(hits) != 1:
            # Also count occurrences the reader cannot see: inside % comments
            # and inside pending draft blocks (inline drafts split the phrase
            # across the markup commands), so the error can say where they are.
            extra = ""
            if len(hits) != 1:
                raw_text = "\n".join(lines)
                raw_relaxed = _relax_blocks(raw_text)
                masked_count = _mask_comments_and_blocks(raw_text).count(needle)
                raw_count = raw_text.count(needle)
                # an inline draft block splits the phrase across markup
                # commands; count a split occurrence too
                if needle not in raw_text:
                    compact = re.sub(r"\\(?:deleted|added|highlight)(?:\[[^\]]*\])?\{[^{}]*\}", "", raw_text)
                    if needle in compact.replace("\n", " "):
                        raw_count = masked_count + 1
                if raw_count > masked_count:
                    # Name the pending drafts holding the uncountable
                    # occurrences (raw or with their markup relaxed) and say
                    # how to proceed: resolve them, pick another anchor, or
                    # use --at-line.
                    block_ids = sorted(
                        {
                            m.group(1)
                            for m in BLOCK_RE.finditer(raw_text)
                            if needle in m.group(2)
                            or needle in raw_relaxed[m.start(2) : m.end(2)]
                        }
                    )
                    where = (
                        "pending draft(s) " + ", ".join(block_ids)
                        if block_ids
                        else "draft blocks"
                    )
                    extra = (
                        f" ({raw_count} occurrence(s) in the raw file, but only "
                        f"inside % comments or {where}, which do not count; "
                        "resolve those draft(s) first (accept/reject), pick an "
                        "anchor outside them, or use --at-line)"
                    )
            raise DraftError(
                f"anchor {needle!r} matched {len(hits)} lines "
                f"(need exactly 1){extra}"
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
    # document. An unmarked insertion carries no markup, so it is allowed.
    if marked and _alignment_depth(lines, pos):
        raise DraftError(
            "insertion point is inside a table or array; a drafted line would "
            "be a table row and cannot be wrapped in \\added/\\deleted. Edit "
            "the cell by hand (the tool records and adopts hand edits)"
        )
    if marked and _math_depth(lines, pos):
        raise DraftError(
            "insertion point is inside display math, where \\added/\\deleted "
            "cannot be typeset. Add a label with `draft-insert --unmarked` "
            "(no markup, undoable); change a formula by hand"
        )
    if marked and "\\label" in block and pos and MATH_END_RE.fullmatch(lines[pos - 1].strip()):
        # The counter lives inside the environment's group: a label after
        # \end{equation} compiles but writes an empty number.
        raise DraftError(
            "a \\label after \\end{equation} would capture an empty number; it "
            "has to sit inside the equation, where \\added cannot be "
            "typeset. Use `draft-insert --unmarked` (no markup, undoable)"
        )
    return pos


def insert_block(
    text: str,
    block: str,
    after: str | None = None,
    before: str | None = None,
    at_line: int | None = None,
    marked: bool = True,
) -> str:
    """Insert `block` (whole lines) at an anchor.

    A block always occupies whole lines; insert_lines_at terminates every
    inserted line with the document's own separator, so an end marker lands on
    its own line even at EOF.
    """
    lines = split_lines(text)
    pos = _insert_position(lines, block, after, before, at_line, marked)
    return insert_lines_at(text, pos, split_lines(block))


def _enclosing_end(lines: list[str], pos: int) -> int | None:
    """Closing line of the innermost math or float environment at `pos`.

    A margin note cannot live inside a float or in math, so a note about such
    a change goes after the environment's end.
    """
    kinds = (
        (MATH_BEGIN_RE, MATH_END_RE),
        (MATH_OPEN_RE, MATH_CLOSE_RE),
        (FLOAT_BEGIN_RE, FLOAT_END_RE),
    )
    ends: list[int] = []
    for begin_re, end_re in kinds:
        depth = _env_depth(lines, pos, begin_re, end_re)
        if depth <= 0:
            continue
        for i in range(pos, len(lines)):
            depth += len(begin_re.findall(lines[i])) - len(end_re.findall(lines[i]))
            if depth <= 0:
                ends.append(i)
                break
    return min(ends) if ends else None


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
    package in draft mode, an author definition, or the agent-profile setup
    lines that install the symbol mark)."""
    stripped = line.strip()
    if stripped in (CHANGES_PACKAGE, CHANGES_PACKAGE_LINE):
        return True
    if stripped.startswith(r"\setauthormarkup{"):
        return True
    if stripped in (r"\makeatletter", r"\makeatother"):
        return True
    if stripped.startswith(r"\let\latexdraft@"):
        return True
    if stripped.startswith(r"\def\Changes@output@author"):
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


def _document_has_todo(tex: Path, main_text: str | None = None) -> bool:
    r"""Whether any \\todo remains anywhere in the document (main file — given
    as `main_text` when it is mid-mutation — or any \\input/\\include file).
    Accepted margin notes live in the input files, so a main-file-only check
    used to strip \\usepackage{todonotes} while an accepted \\todo still needed
    it, breaking the build with "! Undefined control sequence \\todo"."""
    if main_text is not None and re.search(r"\\todo\b", main_text):
        return True
    for f in tex_inputs(tex):
        try:
            if re.search(r"\\todo\b", f.read_text(encoding="utf-8")):
                return True
        except OSError:
            continue
    return False


def strip_changes_package_if_unused(
    text: str, injected: list[str] | None = None, keep_todo: bool | None = None
) -> str:
    """Remove the auto-injected preamble lines once no draft markers remain,
    so the document is clean for submission (some classes reject the changes
    package in final mode).

    With `injected` (the lines latex-draft recorded adding to this state),
    exactly those lines are removed — a user-authored todonotes load, changes
    package or author definition is never touched. Without provenance the
    old pattern-based behaviour applies. `keep_todo` overrides the \\todo
    scan of `text` (callers pass the document-wide check)."""
    if "% latex-draft:" in text:
        return text
    if keep_todo is None:
        keep_todo = bool(re.search(r"\\todo\b", text))
    if injected is None:

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
    own_map: dict[str, str] | None = None,
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
                pkey = "pdel:" + _plain_deleted(m.group(2))
                for k in (key, pkey):
                    if k not in splice_winner:
                        if accept and accepted_id is not None:
                            splice_winner[k] = accepted_id
                        else:
                            splice_winner[k] = did

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
        body = m.group(2)
        has_added = re.search(r"\\added\b", body)
        has_deleted = re.search(r"\\deleted\b", body)
        if (has_added or has_deleted) and not re.match(r"\s*\\(added|deleted|footnote|todo)", body):
            # An INLINE point-edit block: the body mixes plain text with
            # \\deleted/\\added spans on one line. Accept unwraps \\added and
            # drops \\deleted; reject unwraps \\deleted and drops \\added.
            # keep_markup keeps the \\added wrapper on accept.
            if accept:
                if keep_markup:
                    # keep_markup: drop \\deleted, keep \\added as-is (it already
                    # carries the author id); just strip the block-number marker.
                    kept = _brace_matching_sub(body, "deleted", keep=False, eat_space=True)
                else:
                    # brace matching (not a flat regex) so nested braces
                    # ($x^{2}$, \\emph{...}) unwrap instead of leaking markup
                    kept = _brace_matching_sub(body, "added", keep=True)
                    kept = _brace_matching_sub(kept, "deleted", keep=False, eat_space=True)
            else:
                own = (own_map or {}).get(did)
                if own:
                    # Fragments this draft spliced are recorded, so revert
                    # only those: pre-existing markup in the body (accepted
                    # text kept marked) must survive the reject.
                    kept = _revert_own(body, own)
                else:
                    kept = _brace_matching_sub(body, "deleted", keep=True)
                    kept = _brace_matching_sub(kept, "added", keep=False, eat_space=True)
                kept = _unprotect_fragile(kept)  # drop the \\mbox we added around \\cite etc.
            kept = re.sub(r"\\textsuperscript\{[0-9]+\} ?", "", kept.strip(chr(13)+chr(10)), count=1)
            return to_newline(kept, nl) + (nl if had_nl else "")
        content = extract_command_content(body, "added" if accept else "deleted")
        if content is not None and not accept:
            content = _unprotect_fragile(content)
        if content is not None:
            deleted = extract_command_content(m.group(2), "deleted")
            if deleted is not None:
                # A replace/delete draft: siblings share the deleted span.
                # Splice only for the first sibling of the group; siblings
                # collapse to nothing. When a sibling collapses, also drop
                # a single blank line that separated it from the previous
                # block, so the original paragraph structure is restored.
                key = "del:" + deleted
                pkey = "pdel:" + _plain_deleted(m.group(2))
                if key in done_groups or pkey in done_groups:
                    return ""
                # Only the winner block splices; the rest collapse.
                winner = splice_winner.get(key) or splice_winner.get(pkey)
                if accept and winner is not None and did != winner:
                    return ""
                done_groups.add(key)
                done_groups.add(pkey)
                spliced.add(did)
            if content:
                spliced_text = to_newline(_strip_block_sup(content), nl)
                if accept and keep_markup:
                    attrs_match = re.search(r"\\added(\[[^\]]*\])?", m.group(2))
                    attrs = (attrs_match.group(1) or "") if attrs_match else ""
                    spliced_text = _provenance(spliced_text, style, attrs, nl)
                return spliced_text + (nl if had_nl else "")
            return ""
        # Only a comment block (body is a bare \footnote{...}/\todo{...})
        # may re-emit the command on accept. A delete block nests the same
        # command inside \deleted{...} (boxed by _protect_fragile_in_deleted);
        # re-emitting it here would restore the text the delete removes.
        if re.match(r"\s*\\(footnote|todo)\b", body):
            for cmd in ("footnote", "todo"):
                content = extract_command_content(body, cmd)
                if content is not None:
                    if not accept:
                        return ""
                    out = "\\" + cmd + "{" + to_newline(_strip_block_sup(content), nl) + "}"
                    return out + (nl if had_nl else "")
        return ""

    return BLOCK_RE.subn(repl, text)[0], found, spliced


def _file_of(tex: Path, fname: str) -> Path:
    r"""Path of a file of the document from the path recorded in a draft.

    Records store the path relative to the main file's directory (e.g.
    ``parts/intro.tex``), so a subdirectory input resolves to its real path.
    Legacy states recorded only the basename: those are migrated to the real
    dependency path by matching against the main file's \\input/\\include
    graph (a basename that matches no dependency falls back to the old
    main-dir/basename guess).
    """
    if fname == tex.name:
        return tex
    if "/" in fname or fname.endswith(".tex"):
        candidate = tex.parent / fname
        if candidate.is_file() or "/" in fname:
            return candidate
    for dep in tex_inputs(tex):
        if dep.name == fname:
            return dep
    return tex.parent / fname


def _same_file(a: Path, b: Path) -> bool:
    """Whether two paths name the same file (the main file is resolved once)."""
    return a.resolve() == b.resolve()


def _blocks_by_id(text: str) -> dict[str, re.Match]:
    """The draft blocks in `text`, first block per draft id."""
    blocks: dict[str, re.Match] = {}
    for m in BLOCK_RE.finditer(text):
        blocks.setdefault(m.group(1), m)
    return blocks


def _next_block_number(state: dict) -> int:
    """1-based ordinal of the next draft block, in creation order."""
    nums = [r.get("block", 0) for r in state["drafts"].values()]
    return (max(nums) if nums else 0) + 1


def draft_record(
    did: str, op: str, preview_text: str, block_number: int | None = None
) -> dict:
    preview = " ".join(preview_text.split())[:120]
    rec = {
        "id": did,
        "op": op,
        "status": "pending",
        "created": now(),
        "preview": preview,
    }
    if block_number is not None:
        rec["block"] = block_number
    return rec


def mutate(
    tex: Path, state: dict, note: str, fn, target: Path | None = None, setup: bool = False
) -> Path:
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
        "kind": "setup" if setup else "change",
        "note": note,
        "turn": state.get("turn", 0),
        "backup": snap.name,
        "file": _rel_key(tex, target),
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
    record = draft_record(did, op, preview, block_number=_next_block_number(state))
    record.update({"file": _rel_key(tex, target), "author": author, **(fields or {})})
    state["drafts"][did] = record
    save_state(tex, state)
    print(f"latex-draft: drafted {op} {did} in {_rel_key(tex, target)}")
    return 0


def _unmarked_insert(
    tex: Path, state: dict, target: Path, args: argparse.Namespace
) -> int:
    r"""Insert raw LaTeX with no draft markup.

    For changes the PDF cannot show (a \label on an equation) or that the
    markup cannot carry (a float environment). An inserted \label also gets a
    draft-only `\comment` after its environment ("labelled by LLM"), so the
    review PDF says that something happened; the note is dropped in final
    mode and removed with the change by `undo`.
    """
    author = _safe_author_id(getattr(args, "author", None) or DEFAULT_AUTHOR)
    is_label = args.text.strip().startswith("\\label")
    note = f"\\comment[id={author}]{{labelled by {author}}}" if is_label else None
    preview = " ".join(args.text.split())[:60]

    # Validate against the target before the main file's preamble is touched.
    try:
        _insert_position(
            split_lines(read_tex(target)), args.text,
            args.after, args.before, args.at_line, marked=False,
        )
    except DraftError as e:
        return _fail(e)
    if note and not _same_file(target, tex):
        ensure_document_preamble(tex, state, author)

    def fn(text: str) -> str:
        if note and _same_file(target, tex):
            text = _prepare_body_text(text, state, author, tex)
        lines = split_lines(text)
        pos = _insert_position(
            lines, args.text, args.after, args.before, args.at_line, marked=False
        )
        label_lines = split_lines(args.text)
        if note is None:
            return insert_lines_at(text, pos, label_lines)
        end = _enclosing_end(lines, pos)
        note_pos = (end + 1 if end is not None else pos) + len(label_lines)
        return insert_lines_at(
            insert_lines_at(text, pos, label_lines), note_pos, [note]
        )

    try:
        mutate(tex, state, f"insert without markup: {preview}", fn, target=target)
    except DraftError as e:
        return _fail(e)
    if note:
        # Remember the note so the preamble is not stripped while it exists.
        state.setdefault("label_notes", []).append(note)
        save_state(tex, state)
        print(
            "latex-draft: inserted without markup, with a note "
            f"\"labelled by {author}\" next to it — the note shows in draft "
            "mode and disappears in final mode; `undo` reverts both"
        )
    else:
        print(
            "latex-draft: inserted without markup — it has no visual effect, "
            "so the PDF will not show it; `undo` reverts it"
        )
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
    author = _safe_author_id(getattr(args, "author", None) or DEFAULT_AUTHOR)
    block_number = _next_block_number(state)
    target = resolve_target(tex, getattr(args, "file", None))
    if target is None:
        return 2
    if getattr(args, "unmarked", False):
        return _unmarked_insert(tex, state, target, args)

    def fn(text: str) -> str:
        _assert_markable(args.text)
        if _same_file(target, tex):
            text = _prepare_body_text(text, state, author, tex, margin)
        if margin:
            lines = split_lines(text)
            # Anchor-only validation (marked=False): the marked-only math/table
            # guards must not pre-empt the margin-specific refusal below.
            pos = _insert_position(
                lines, "", args.after, args.before, args.at_line, marked=False
            )
            # \todo becomes \marginpar, illegal inside a float or in math --
            # and inside a float it fails SILENTLY (later floats of the same
            # class get dropped with no '!' error line), so refuse here.
            if _float_depth(lines, pos) > 0 or _math_depth(lines, pos) > 0:
                raise DraftError(
                    "a --margin note cannot be placed inside a float environment "
                    "(figure/table/...) or in math: todonotes \\todo becomes "
                    "\\marginpar, which is illegal there and can silently drop "
                    "later floats of the same class (no '!' error line). Anchor "
                    "before/after the environment instead, or use a plain % "
                    "comment in the source"
                )
        return insert_block(
            text,
            block_for(text, make_block(did, author, block_number)),
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
        lambda did, author, bn: build_block(did, new=args.text, author=author, block_number=bn),
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
            f"\\input/\\include dependencies ({', '.join(sorted(_rel_key(tex, d) for d in tex_inputs(tex))) or 'none'})",
            file=sys.stderr,
        )
        return None
    return candidate


def _mask_comment_tail(line: str) -> str:
    r"""The line up to the first unescaped % (comment start); \% stays."""
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            return line[:i]
        i += 1
    return line


def _mask_comments_and_blocks(text: str) -> str:
    r"""Same-length copy of `text` with draft-block interiors and % comment
    tails blanked (newlines kept), so anchor and uniqueness searches only see
    the text a reader actually sees: not inside `% ...` comments and not
    inside pending draft blocks (whose \\deleted/\\added bodies repeat text).
    """
    out = list(text)
    for m in BLOCK_RE.finditer(text):
        for k in range(m.start(), m.end()):
            if out[k] not in "\r\n":
                out[k] = " "
    masked = "".join(out)
    res = list(masked)
    i = 0
    n = len(masked)
    while i < n:
        c = masked[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            j = i
            while j < n and masked[j] not in "\r\n":
                res[j] = " "
                j += 1
            i = j
            continue
        i += 1
    return "".join(res)


def _mask_comments_only(text: str) -> str:
    """Same-length copy of `text` with % comment tails blanked but pending
    draft blocks KEPT: the counterpart of _mask_comments_and_blocks, used to
    check that a span found in the relaxed view is not inside a comment."""
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            j = i
            while j < n and text[j] not in "\r\n":
                out[j] = " "
                j += 1
            i = j
            continue
        i += 1
    return "".join(out)


def _plain_block_text(body: str) -> str:
    r"""A block body with every wrapper command removed whole (contents
    included): only the plain, unchanged words of the line remain. An --old
    matching this text is editable (the composition path splices it into
    the block); a match only inside a wrapper's content is not."""
    out = body
    for cmd in ("added", "deleted", "highlight", "footnote", "todo"):
        out = _brace_matching_sub(out, cmd, keep=False)
    return BLOCK_SUP_UNANCHORED_RE.sub("", out)


def _wrapper_commands(body: str) -> str:
    r"""Every \\added/\\deleted/\\highlight/\\footnote/\\todo command in a
    block body, whole (with its balanced braces), concatenated in order:
    the markup fragments a draft contributed to the body, used as a fallback
    for drafts recorded before drafts carried their own fragments."""
    out: list[str] = []
    for m in re.finditer(
        r"\\(?:added|deleted|highlight|footnote|todo)(?:\[[^\]]*\])?\{", body
    ):
        depth, i = 1, m.end()
        while i < len(body) and depth:
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth:
            continue
        out.append(body[m.start():i])
    return "".join(out)


def _revert_own(body: str, own: str) -> str:
    r"""Undo exactly the fragments a pending draft spliced into a block
    body, leaving pre-existing markup (accepted text kept marked) alone:
    each \\deleted{X} in `own` is restored to X, each \\added/\\footnote/
    \\todo{Y} is dropped. `own` is the concatenation of the command
    substrings the draft spliced, so replacement is by exact substring."""
    out = body
    for m in re.finditer(
        r"\\(added|deleted|highlight|footnote|todo)(?:\[[^\]]*\])?\{", own
    ):
        cmd = m.group(1)
        depth, i = 1, m.end()
        while i < len(own) and depth:
            ch = own[i]
            if ch == "\\" and i + 1 < len(own):
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth:
            continue
        frag = own[m.start():i]
        content = own[m.end():i - 1]
        if cmd == "deleted":
            out = out.replace(frag, content, 1)
        else:
            # Dropping the bare command can leave a doubled space: the word
            # diff writes a separator space between \\deleted and \\added,
            # and the document may have its own space after the fragment.
            # Eat one space only when the removal actually created a run of
            # two; a single following space is the document's own and stays.
            pos = out.find(frag)
            if pos == -1:
                continue
            end = pos + len(frag)
            if out[pos - 1: pos] == " " and out[end: end + 1] == " ":
                out = out[:pos] + out[end + 1:]
            else:
                out = out[:pos] + out[end:]
    return out


def _searchable_lines(lines: list[str]) -> list[str]:
    """Lines as a reader sees them: pending draft-block lines blanked and
    % comment tails cut, so anchors match visible text only."""
    out: list[str] = []
    in_block = False
    for line in lines:
        if in_block:
            out.append("")
            in_block = not BLOCK_END_LINE_RE.fullmatch(line)
        elif BLOCK_BEGIN_LINE_RE.fullmatch(line):
            in_block = True
            out.append("")
        else:
            out.append(_mask_comment_tail(line))
    return out


def _relax_blocks(text: str) -> str:
    r"""Same-length copy of `text` with every pending draft block's body
    reduced to its plain text: each wrapper command (
    \\deleted{...}, \\added{...}, ...) is replaced by its inner text,
    and the wrapper's own characters are blanked. Marker lines and the
    \\textsuperscript{N} marker are blanked too. Every plain (non-wrapper)
    character of the body keeps its exact position, so the result has the
    same length as `text` and offsets index the original file directly:
    a --old found in the relaxed text addresses the same characters.
    """
    out = list(text)
    wrapper_re = re.compile(r"\\(?:deleted|added|highlight|todo|footnote)(?:\[[^\]]*\])?\{([^{}]*)\}")
    for m in BLOCK_RE.finditer(text):
        body_start, body_end = m.span(2)
        body = m.group(2)
        # blank the marker lines and the block-number marker
        for k in range(m.start(), m.end()):
            if out[k] not in "\r\n" and not (body_start <= k < body_end):
                out[k] = " "
        for sm in BLOCK_SUP_RE.finditer(body):
            for k in range(sm.start(), sm.end()):
                p = body_start + k
                if text[p] not in "\r\n":
                    out[p] = " "
        # replace each wrapper by its inner text (same length, padded right)
        for wm in wrapper_re.finditer(body):
            inner = wm.group(1)
            wlen = wm.end() - wm.start()
            wbase = body_start + wm.start()
            for k in range(wlen):
                p = wbase + k
                if p < body_end and text[p] not in "\r\n":
                    out[p] = inner[k] if k < len(inner) else " "
    return "".join(out)


def _pending_wrapper_span(text: str, start: int, end: int) -> str | None:
    r"""Id of the pending draft block whose \added/\deleted/... command the
    character span [start, end) overlaps, or None when the span is plain
    (unchanged) text. A span inside a pending wrapper's argument is the text
    that draft adds or deletes — not yet part of the document — so neither a
    marked draft nor an --unmarked patch may splice it."""
    for m in BLOCK_RE.finditer(text):
        body = m.group(2)
        bstart = m.start(2)
        for wm in SIBLING_WRAPPER_RE.finditer(body):
            depth, i = 1, wm.end()
            while i < len(body) and depth:
                ch = body[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            if depth:
                continue
            ws = bstart + wm.start()
            we = bstart + i
            if start < we and end > ws:
                return m.group(1)
    return None


def _boundary_compose_target(text: str, old: str):
    """A pending inline draft block whose line, joined with the line that
    follows the block, contains `old` exactly once across the block boundary
    (the --old starts on the block's line and ends on the next one).

    Such a match is invisible to _visible_span (the block interior is masked)
    and to the relaxed compose (the marker lines between the two halves stay
    blanked), so without this the edit is refused as "not found" even though
    both halves sit in plain sight. Returns (match, nl, tail_len): the
    BLOCK_RE match of the host, the document's newline, and how many
    characters after the block must be absorbed into its body. None when no
    block spans the text.
    """
    if old in _mask_comments_and_blocks(text):
        return None
    nl = newline_of(text)
    relaxed_all = _relax_blocks(text)
    for m in BLOCK_RE.finditer(text):
        body = relaxed_all[m.start(2):m.end(2)]
        # BLOCK_RE's body group keeps the newline that ends its last line and
        # the end marker consumes its own, so joining body directly to
        # text[m.end():] reproduces the document's line structure; add the
        # document newline only when the body does not already end with one.
        sep = "" if body.endswith(("\n", "\r")) else nl
        spliced = text[:m.start()] + body + sep + text[m.end():]
        if spliced.count(old) != 1:
            continue
        j = spliced.index(old)
        body_start = m.start()
        body_end = body_start + len(body)
        if j + len(old) <= body_end:
            continue  # fully inside the body: the plain compose handles it
        if j >= body_end + len(sep):
            continue  # entirely after the block: a visible occurrence
        k = body_end + len(sep) - j
        if k <= 0 or k >= len(old):
            continue
        tail = old[k:]
        if not text[m.end():].startswith(tail):
            continue
        return m, sep, len(tail)
    return None


def _deleted_view(body: str) -> str:
    r"""The text a pending block would restore on reject: the block body with
    \added contents dropped and \deleted/\highlight contents unwrapped, so a
    --old that spans a word-diff's deleted words plus the surrounding plain
    text is still found (as one contiguous string) when checking whether the
    old text overlaps a pending point edit."""
    out = re.sub(r"\\added(?:\[[^\]]*\])?\{[^{}]*\}", "", body)
    out = re.sub(r"\\(?:deleted|highlight)(?:\[[^\]]*\])?\{([^{}]*)\}", r"\1", out)
    out = re.sub(r"\\textsuperscript\{[0-9]+\}\s*", "", out)
    # dropping the \added wrappers leaves doubled spaces behind; this view is
    # only used for substring detection, so collapse whitespace runs
    return " ".join(out.split())


WS_FLEX = r"(?:[ \t]+|(?:\r\n|\r|\n)[ \t]*)"


def _whitespace_flexible_match(text: str, old: str) -> bool:
    """Whether `old` occurs when single whitespace runs may differ: a hard
    wrap quoted as a space, CRLF read as LF, a doubled space. Only the
    wording of the not-found refusal depends on this — the edit itself is
    never applied on such a near-miss. Runs never bridge a blank line (a
    paragraph break is structure, not wrapping)."""
    parts = old.strip().split()
    if not parts:
        return False
    pat = re.compile(WS_FLEX.join(re.escape(p) for p in parts))
    return pat.search(text) is not None


def _accepted_markup_straddle(text: str, old: str) -> str | None:
    r"""Refusal message for an --old that matches only across accepted
    provenance markup: an anchor straddling `\added{...}`,
    `\highlight{...}` or `\replaced{...}{...}` (e.g. `--old "is debating
    the law"` against `The senate \added[id=AI]{is} debating the law.`).
    The raw text has no literal occurrence (the wrapper's closing brace is in
    the way), so this used to surface as a bare "not found" even though the
    words are plainly there. Returns None when the straddle is not the
    explanation."""
    if _mask_comments_and_blocks(text).count(old):
        return None  # a visible match exists: not this problem
    for m in PROVENANCE_CMD_RE.finditer(text):
        span = _provenance_span(text, m.start())
        if span is None:
            continue
        unwrapped = text[: m.start()] + text[m.end() : span[1] - 1] + text[span[1] :]
        # unwrapping one command keeps (new + old) for \replaced; matching
        # across either argument is enough to name the markup
        if unwrapped.count(old) and text.count(old) == 0:
            cmd = text[m.start() : span[1]]
            shown = cmd if len(cmd) <= 60 else cmd[:57] + "..."
            return (
                "--old text crosses accepted \\added/\\highlight/\\replaced "
                f"markup ({shown}); the changes package cannot nest there. "
                "Re-draft over the whole command with --unmarked (--old "
                "quoted as the entire command, --new as the replacement "
                "text), or remove that markup first"
            )
    return None


def _not_found_message(text: str, old: str) -> str:
    """The refusal for an --old with no literal occurrence, naming whitespace
    when only whitespace differs — a bare "not found" sends an agent that
    quoted hard-wrapped source (or read the phrase from the PDF) into a
    blind retry loop."""
    straddle = _accepted_markup_straddle(text, old)
    if straddle is not None:
        return straddle
    if _whitespace_flexible_match(_mask_comments_and_blocks(text), old):
        return (
            "--old text not found in the document; the closest match differs "
            "only in whitespace or line breaks (the source is hard-wrapped "
            "there, or uses different spacing). Quote --old with the file's "
            "exact line breaks and spacing"
        )
    return "--old text not found in the document"


def _visible_span(text: str, old: str) -> tuple[int, int] | None:
    """Character span of `old` in the text a reader sees: outside pending
    draft blocks and outside % comments.

    A match that merely overlaps a block (starts outside, ends inside, or
    the other way round) is not a visible occurrence: drafting over it would
    put another draft's marker lines inside \\deleted{...}.
    """
    masked = _mask_comments_and_blocks(text)
    i = masked.find(old)
    if i < 0:
        return None
    return i, i + len(old)


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


def _env_depth(
    lines: list[str], line_index: int, begin_re: re.Pattern, end_re: re.Pattern
) -> int:
    """Nesting depth of the environments matched by `begin_re`/`end_re`."""
    depth = 0
    for i, line in enumerate(lines):
        if i == line_index:
            return depth
        depth += len(begin_re.findall(line)) - len(end_re.findall(line))
    return depth


def _alignment_depth(lines: list[str], line_index: int) -> int:
    """Table/array nesting depth at a line.

    A line inside one is a table row: wrapping it in \\deleted/\\added would
    put the row break inside the argument, which TeX rejects.
    """
    return _env_depth(lines, line_index, ALIGN_BEGIN_RE, ALIGN_END_RE)


def _math_depth(lines: list[str], line_index: int) -> int:
    r"""Display-math nesting at a line (equation/align/... and \[ ... \]).

    A line inside math mode cannot carry \added/\deleted: the markup is text
    markup and TeX answers "Missing $ inserted".
    """
    depth = _env_depth(lines, line_index, MATH_BEGIN_RE, MATH_END_RE)
    for line in lines[:line_index]:
        depth += len(MATH_OPEN_RE.findall(line)) - len(MATH_CLOSE_RE.findall(line))
    return depth


def _float_depth(lines: list[str], line_index: int) -> int:
    r"""Float-environment nesting depth at a line (figure/table/...)."""
    return _env_depth(lines, line_index, FLOAT_BEGIN_RE, FLOAT_END_RE)


def _line_has_math_delimiter(line: str) -> bool:
    """Whether the line itself opens or closes display math."""
    return bool(MATH_OPEN_RE.search(line) or MATH_CLOSE_RE.search(line))


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
    if span is not None:
        lines = split_lines(text)
        line_index = _line_of(text, span[0])
        if _in_verbatim(text, span[0]):
            raise DraftError(
                "the text is inside verbatim (verbatim/lstlisting/Verbatim/alltt); "
                "\\added/\\deleted would be printed literally there in the review "
                "PDF. Use draft-replace --unmarked to edit it without review markup"
            )
        if _math_depth(lines, line_index) or _line_has_math_delimiter(lines[line_index]):
            raise DraftError(
                "the text is inside display math, where \\added/\\deleted "
                "cannot be typeset (\"Missing $ inserted\"). To add a label, "
                "draft it after the closing \\end{equation}; to change a "
                "formula, make the edit by hand (the tool records and adopts "
                "hand edits)"
            )
        # Caption lines and float interiors: ulem's \sout (used by \deleted)
        # fails inside \caption ("You can't use \hrule here") and the xcolor
        # wrapper trips on the brace scan. Refuse and suggest --unmarked.
        if re.search(r"\\caption\s*\{", lines[line_index]):
            raise DraftError(
                "the text is a \\caption line; \\deleted/\\added break the build "
                "inside captions (! You can't use \\hrule here). Use "
                "draft-comment --margin for a note, or draft-replace "
                "--unmarked to edit it without review markup"
            )
        # Sectioning titles: moving arguments typeset in restricted horizontal
        # mode (LR), where \\added/\\deleted fail ("Not allowed in LR mode").
        # Same family as \\caption: refuse and suggest --unmarked.
        col_start = span[0] - (
            max(text.rfind("\n", 0, span[0]), text.rfind("\r", 0, span[0])) + 1
        )
        col_end = col_start + (span[1] - span[0])
        line = lines[line_index]
        for sm in SECTIONING_RE.finditer(line):
            depth, i = 1, sm.end()
            while i < len(line) and depth:
                ch = line[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            if depth:
                continue
            if col_start < i and col_end > sm.end() - 1:
                raise DraftError(
                    "the text is inside a sectioning title (\\section, "
                    "\\subsection, ...); \\deleted/\\added break the build "
                    "there (\"Not allowed in LR mode\"). Use draft-replace "
                    "--unmarked to edit the title without review markup"
                )
        if _float_depth(lines, line_index):
            raise DraftError(
                "the text is inside a float (figure/table); \\deleted/\\added "
                "cannot wrap float interiors. Use draft-comment --margin for a "
                "note on the float, or draft-replace --unmarked"
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


def _unmarked_replace(
    tex: Path, state: dict, target: Path, old: str, new: str | None,
    op: str, author: str,
) -> int:
    r"""Replace/delete text with NO changes markup (for display math, floats,
    captions — where \added/\deleted break the build). The edit goes straight
    into the source, snapshotted and changelogged, so `undo` reverts it. The
    review PDF will not show it; compile to see the effect.
    """
    text = read_tex(target)
    visible = _mask_comments_and_blocks(text)
    idx: int | None = None
    if visible.count(old) != 1:
        # A pending inline point-edit block keeps the unchanged words of its
        # line plain, so an --unmarked fix on that plain text is legitimate
        # even while the block is pending (it used to be refused with
        # "matched 0 times", blocking the direct repair of e.g. a corrupted
        # line). Match against the relaxed view, whose block interiors are
        # reduced to their plain text at unchanged offsets, and splice at the
        # exact position.
        relaxed = _relax_blocks(text)
        j = relaxed.find(old)
        if relaxed.count(old) == 1 and visible.count(old) == 0:
            bad = _pending_wrapper_span(text, j, j + len(old))
            if bad is not None:
                print(
                    f"latex-draft: --old text is inside the words the pending "
                    f"point-edit draft ({bad}) adds or deletes; accept or "
                    "reject that draft first",
                    file=sys.stderr,
                )
                return 2
            nocomment = _mask_comments_only(text)
            if nocomment[j : j + len(old)] != old:
                print(
                    "latex-draft: --old text matched 0 times in the document "
                    "text (outside % comments and draft blocks; need exactly 1)",
                    file=sys.stderr,
                )
                return 2
            idx = j
    if idx is None and visible.count(old) != 1:
        print(
            f"latex-draft: --old text matched {visible.count(old)} times in "
            "the document text (outside % comments and draft blocks; "
            "need exactly 1)",
            file=sys.stderr,
        )
        return 2
    replacement = "" if new is None else new
    preview = " ".join((new or old).split())[:60]

    if idx is not None:

        def fn(t: str) -> str:
            return t[:idx] + replacement + t[idx + len(old) :]

    else:

        def fn(t: str) -> str:
            return t.replace(old, replacement, 1)

    try:
        mutate(tex, state, f"unmarked {op}: {preview}", fn, target=target)
    except DraftError as e:
        print(f"latex-draft: {e}", file=sys.stderr)
        return 2
    rec = draft_record(new_id(state), op + " (unmarked)", preview)
    rec["file"] = _rel_key(tex, target)
    rec["unmarked"] = True
    # The exact span before/after the direct patch, so `reject` can invert it
    # (accept keeps the patch; both are also covered by `undo`).
    rec["span_before"] = old
    rec["span_after"] = replacement
    state["drafts"][rec["id"]] = rec
    save_state(tex, state)
    print(
        f"latex-draft: {op}d without markup ({rec['id']}) in {_rel_key(tex, target)} — "
        "no draft block, the PDF will not show it; `undo` reverts it"
    )
    return 0


def _replace_or_delete(tex: Path, state: dict, args: argparse.Namespace, op: str) -> int:
    did = new_id(state)
    old = args.old
    new = getattr(args, "new", None)
    author = _safe_author_id(getattr(args, "author", None) or DEFAULT_AUTHOR)
    target = resolve_target(tex, getattr(args, "file", None))
    if target is None:
        return 2
    text = read_tex(target)
    # An agent quoting hard-wrapped source sends \n; a Windows-saved file
    # holds \r\n. Match the edit against the file's own line separators so
    # the phrase is found (and the accepted file keeps its CRLF endings).
    # read_tex (not read_text): the untranslated read is what fn() searches,
    # so newline_of must see the file's real separator.
    if "\n" in old or "\r" in old:
        nl = newline_of(text)
        old = re.sub(r"\r\n|\r|\n", nl, old)
        if new is not None:
            new = re.sub(r"\r\n|\r|\n", nl, new)
    if getattr(args, "unmarked", False):
        return _unmarked_replace(tex, state, target, old, new, op, author)

    def replacement(removed: str) -> str:
        """The line(s) in their accepted form."""
        return removed.replace(old, "" if new is None else new, 1)

    composed: dict[str, str] = {}

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
        if host is not None and _is_inline_body(host.group(2)):
            # The host is an inline point-edit block (plain text mixed with
            # deleted/added on one line). An --old overlapping the words it
            # deletes is an alternative to that point edit; the changes
            # package cannot nest alternatives inside the block, so refuse
            # instead of emitting a fragment block with the wrong diff.
            raise DraftError(
                "--old text overlaps the words a pending point-edit draft "
                f"({host.group(1)}) changes; accept or reject that draft first"
            )
        if host is None and any(old in m.group(2) for m in matches):
            # A pending block covers the text. An inline point-edit block
            # keeps the unchanged words of its line plain, so an --old that
            # matches those plain words is still editable: the composition
            # path below splices it into the block. Refuse only when the
            # text exists solely inside a pending draft's \\added content
            # (a pure insert has no \\deleted, so it is never a host):
            # accepting that draft would splice the original insert in,
            # duplicating the edit.
            in_plain = any(old in _plain_block_text(m.group(2)) for m in matches)
            if not in_plain:
                added_host = next(
                    (
                        m
                        for m in matches
                        if any(
                            old in c
                            for c in extract_command_contents(m.group(2), "added")
                        )
                    ),
                    None,
                )
                if added_host is not None:
                    raise DraftError(
                        "--old text is inside the text pending draft "
                        f"{added_host.group(1)} adds; accept or reject that "
                        "draft before editing it"
                    )
                raise DraftError(
                    "--old text only matches inside pending draft block "
                    f"{next(m.group(1) for m in matches if old in m.group(2))}; "
                    "accept or reject that draft first"
                )
        if host is not None:
            removed = extract_command_content(host.group(2), "deleted")
            host_body = host.group(2)
            whole_line_host = (
                re.match(r"\s*\\(added|deleted|footnote|todo)", host_body) is not None
            )
            if (
                whole_line_host
                and removed is not None
                and "\n" not in removed.strip()
                and old != removed
                and old in removed
            ):
                # A disjoint point edit on a line already covered by a
                # whole-line host block: compose it into the host (one block,
                # sibling \\deleted/\\added fragments) instead of chaining a
                # second full-line block that re-renders the whole line in
                # the review PDF and multiplies fragile markup.
                added = extract_command_content(host_body, "added")
                rev1 = _strip_block_sup(added) if added is not None else removed
                if old not in rev1:
                    raise DraftError(
                        "--old text overlaps the words a pending draft "
                        f"({host.group(1)}) changes; accept or reject that draft first"
                    )
                rev2 = rev1.replace(old, "" if new is None else new, 1)
                diff = _word_diff_verified("", removed, "", rev2, author)
                if diff is None:
                    diff = (
                        _deleted_cmd(author)
                        + _escape_unbalanced_braces(_protect_fragile_in_deleted(removed)) + "}"
                    )
                    if rev2:
                        diff += "\\added[id=" + author + "]{" + _escape_unbalanced_braces(rev2) + "}"
                bn = _next_block_number(state)
                body = _block_sup(bn) + " " + diff
                block = block_for(text, _marker_block(did, body))
                nl = newline_of(text)
                had_nl = host.group(0).endswith(("\n", "\r"))
                composed["host"] = host.group(1)
                composed["own"] = diff
                return text[:host.start()] + block + (nl if had_nl else "") + text[host.end():]
            kept = replacement(removed)
            _assert_markable(removed, what="the replaced text")
            _assert_markable(kept, what="the new text")
            block = block_for(
                text, build_block(did, old=removed, new=kept or None, author=author, block_number=_next_block_number(state))
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
            # An inline point-edit block keeps the unchanged words of its line
            # plain, so a --old that only touches those words is still a visible
            # occurrence: re-run the search with the draft-block interiors
            # reduced to their plain (non-markup) text. The returned span is
            # mapped back onto the real text (same length), so the caller wraps
            # only the plain words and leaves the existing markup alone.
            relaxed = _relax_blocks(text)
            if relaxed.count(old) == 1 and _mask_comments_and_blocks(text).count(old) == 0:
                # An inline point-edit block keeps the unchanged words of its
                # line plain, so this --old matched the plain region inside a
                # pending host block. Compose the two edits into ONE block:
                # wrap old->new in deleted/added inside the host's own line
                # (sibling markup, not nested) and re-attribute the block to
                # this draft. Accepting the composed block merges both fixes;
                # rejecting restores the original line. The host record is
                # marked rejected (superseded_by) by the caller.
                # _relax_blocks expects the FULL text (it blanks the marker
                # lines around each block), so slice the relaxed whole-text
                # copy per block — relaxing the bare body alone is a no-op
                # and used to make this lookup raise a bare StopIteration
                # when the anchor ended inside a pending \deleted{...} span.
                idx = relaxed.index(old)
                host_id = _pending_wrapper_span(text, idx, idx + len(old))
                if host_id is not None:
                    raise DraftError(
                        "--old text overlaps the words a pending point-edit "
                        f"draft ({host_id}) changes; accept or reject that "
                        "draft first"
                    )
                host = next(
                    (m for m in matches if old in relaxed[m.start(2) : m.end(2)]),
                    None,
                )
                if host is None:
                    # The relaxed match spans a block's marker lines (only
                    # possible across blanked marker text); never compose on
                    # such a phantom occurrence.
                    raise DraftError(
                        "--old text matches only across a pending draft "
                        "block; accept or reject that draft first"
                    )
                host_body = host.group(2)
                host_rec = state["drafts"].get(host.group(1))
                host_own = (host_rec or {}).get("own") or _wrapper_commands(host_body)
                if not _is_inline_body(host_body):
                    raise DraftError(
                        "--old text only matches inside pending draft block "
                        f"{host.group(1)}; accept or reject that draft first"
                    )
                if any(old in c for c in extract_command_contents(host_body, "added")):
                    raise DraftError(
                        "--old text is inside the text a pending draft adds; "
                        "accept or reject that draft before editing it"
                    )
                if new is None or not new.strip():
                    diff2 = (
                        _deleted_cmd(author)
                        + _escape_unbalanced_braces(_protect_fragile_in_deleted(old)) + "}"
                    )
                if old not in host_body:
                    # The relaxed match spans a wrapper boundary inside the
                    # raw body (e.g. `law\deleted{x} today` matched relaxed as
                    # `lawx today`); there is no literal splice point, so
                    # never compose on it.
                    raise DraftError(
                        "--old text overlaps the words a pending point-edit "
                        f"draft ({host.group(1)}) changes; accept or reject "
                        "that draft first"
                    )
                if new is None or not new.strip():
                    diff2 = (
                        _deleted_cmd(author)
                        + _escape_unbalanced_braces(_protect_fragile_in_deleted(old)) + "}"
                    )
                else:
                    # Verify byte-exactly in the spliced context (the host
                    # body around old): an unverified word move used to
                    # resolve accept correctly but corrupt the line on
                    # reject (bug-report-6, bug 1).
                    p0 = host_body.index(old)
                    diff2 = _word_diff_verified(
                        host_body[:p0], old, host_body[p0 + len(old):], new, author
                    )
                    if diff2 is None:
                        diff2 = (
                            _deleted_cmd(author)
                            + _escape_unbalanced_braces(_protect_fragile_in_deleted(old)) + "}"
                            "\\added[id=" + author + "]{"
                            + _escape_unbalanced_braces(new) + "}"
                        )
                new_body = host_body.replace(old, diff2, 1).rstrip("\r\n")
                bn = _next_block_number(state)
                # lambda repl: a string repl would process \t in \textsuperscript as TAB
                new_body = re.sub(
                    BLOCK_SUP_UNANCHORED_RE,
                    lambda m: _block_sup(bn) + " ",
                    new_body,
                    count=1,
                )
                block = block_for(text, _marker_block(did, new_body))
                nl = newline_of(text)
                had_nl = host.group(0).endswith(("\n", "\r"))
                composed["host"] = host.group(1)
                composed["own"] = host_own + diff2
                return text[:host.start()] + block + (nl if had_nl else "") + text[host.end():]
            bnd = _boundary_compose_target(text, old)
            if bnd is not None:
                bm, bsep, btail_len = bnd
                host_body = bm.group(2)
                host_rec = state["drafts"].get(bm.group(1))
                host_own = (host_rec or {}).get("own") or _wrapper_commands(host_body)
                if not _is_inline_body(host_body):
                    raise DraftError(
                        "--old text spans pending draft block "
                        f"{bm.group(1)}; accept or reject that draft first"
                    )
                if any(old in c for c in extract_command_contents(host_body, "added")):
                    raise DraftError(
                        "--old text is inside the text a pending draft adds; "
                        "accept or reject that draft before editing it"
                    )
                if _mask_comments_and_blocks(text)[bm.end():bm.end() + btail_len] != text[
                    bm.end(): bm.end() + btail_len
                ]:
                    raise DraftError(
                        "--old text overlaps a % comment or a pending draft block "
                        "(matches there do not count); draft over the visible text "
                        "only, or resolve that draft first"
                    )
                # Absorb the whole rest of the tail's line, not just the
                # tail: text left after the end marker would sit on a new
                # line the original did not have, and neither accept nor
                # reject could restore the source byte for byte.
                _nl_pos = text.find("\n", bm.end())
                _line_end = len(text) if _nl_pos == -1 else _nl_pos + 1
                ext = host_body + bsep + text[bm.end(): _line_end]
                if new is None or not new.strip():
                    diff2 = (
                        _deleted_cmd(author)
                        + _escape_unbalanced_braces(_protect_fragile_in_deleted(old)) + "}"
                    )
                else:
                    # Verify byte-exactly in the spliced context: the anchor
                    # spans the block boundary, so the context is the host
                    # body + separator + tail (ext), not the host body alone.
                    p0 = ext.index(old)
                    diff2 = _word_diff_verified(
                        ext[:p0], old, ext[p0 + len(old):], new, author
                    )
                    if diff2 is None:
                        diff2 = (
                            _deleted_cmd(author)
                            + _escape_unbalanced_braces(_protect_fragile_in_deleted(old)) + "}"
                            "\\added[id=" + author + "]{"
                            + _escape_unbalanced_braces(new) + "}"
                        )
                new_body = ext.replace(old, diff2, 1).rstrip("\r\n")
                bn = _next_block_number(state)
                new_body = re.sub(
                    BLOCK_SUP_UNANCHORED_RE,
                    lambda mm: _block_sup(bn) + " ",
                    new_body,
                    count=1,
                )
                block = block_for(text, _marker_block(did, new_body))
                composed["host"] = bm.group(1)
                composed["own"] = host_own + diff2
                rest = text[_line_end:]
                # The end marker must never glue to a following line: text
                # after it on the same line would sit inside the marker
                # comment and vanish from the review PDF.
                end_sep = "" if (rest.startswith(("\n", "\r")) or not rest) else newline_of(text)
                return text[:bm.start()] + block + end_sep + rest
            elif any(
                _is_inline_body(m.group(2))
                and " ".join(old.split()) in _deleted_view(m.group(2))
                for m in matches
            ):
                hid = next(
                    m.group(1)
                    for m in matches
                    if _is_inline_body(m.group(2))
                    and " ".join(old.split()) in _deleted_view(m.group(2))
                )
                raise DraftError(
                    "--old text overlaps the words a pending point-edit draft "
                    f"({hid}) changes; accept or reject that draft first"
                )
            elif text.count(old):
                raise DraftError(
                    "--old text overlaps a % comment or a pending draft block "
                    "(matches there do not count); draft over the visible text "
                    "only, or resolve that draft first"
                )
            else:
                raise DraftError(_not_found_message(text, old))
        if _provenance_span(text, span[0]) is not None:
            raise DraftError(
                "the text is inside accepted changes markup (\\added/"
                "\\highlight/\\replaced), which the changes package cannot "
                "nest; re-draft over the whole command with --unmarked (the "
                "replacement is plain text, so the provenance mark is dropped "
                "unless --new re-emits it), or remove that markup first"
            )
        start, stop = _line_span(text, span)
        _assert_body_text(text, start)
        removed = text[start:stop]
        kept = replacement(removed)
        _assert_markable(text, (start, stop), "the replaced text")
        _assert_markable(kept, what="the new text")
        bn = _next_block_number(state)
        # Point edit: when --old sits mid-line (there is unchanged text on
        # the same line before or after it), emit an INLINE block that keeps
        # the surrounding text plain and wraps only the changed words —
 #       instead of striking out the whole line in the review PDF.
        line_start = max(text.rfind("\n", 0, span[0]), text.rfind("\r", 0, span[0])) + 1
        line_end_m = EOL_RE.search(text, span[1])
        line_end = line_end_m.start() if line_end_m else len(text)
        before = text[line_start:span[0]]
        after = text[span[1]:line_end]
        mid_line = bool(before.strip()) or bool(after.strip())
        # Inline diff is only safe for plain prose: not inside [...] or {...} of a
        # command on the same line (e.g. \includegraphics[width=...]{...} options,
        # \label{...}), not containing & (alignment), and not spanning lines.
        line_text = text[line_start:line_end]
        before_old = text[line_start:span[0]]
        in_brackets = before_old.count("[") > before_old.count("]") or before_old.rstrip().endswith("[")
        in_braces = before_old.count("{") > before_old.count("}")
        # Text-mode commands allowed in an inline point edit (math spans are
        # masked out first — $..$ content is fine inside \\deleted/\\added).
        # Structural commands (\\section, \\begin, \\includegraphics, ...) are
        # not allowed: those keep the whole-line block form.
        prose_safe = (
            not in_brackets
            and not in_braces
            and "&" not in old
            and _inline_text_safe(old)
            and _inline_text_safe(new or "")
        )
        if (
            mid_line
            and prose_safe
            and old.strip()
            and "\n" not in old.strip()
        ):
            raw = build_inline_block(
                did, before, old, after, new or "", author=author, block_number=bn
            )
            block = block_for(text, raw)
            m_own = BLOCK_RE.search(raw)
            if m_own:
                own = m_own.group(2).rstrip("\r\n")
                if own.startswith(before) and own.endswith(after):
                    own = own[len(before):len(own) - len(after)]
                composed["own"] = BLOCK_SUP_UNANCHORED_RE.sub("", own, count=1).strip()
            return text[:line_start] + block + text[line_end:]
        raw = build_block(
            did, old=removed, new=kept or None, author=author, block_number=bn
        )
        block = block_for(text, raw)
        m_own = BLOCK_RE.search(raw)
        if m_own:
            composed["own"] = m_own.group(2).strip()
        return text[:start] + block + text[stop:]

    visible = _mask_comments_and_blocks(text)
    # An occurrence inside a pending draft block still counts for sibling
    # alternatives (drafted over the host block's \deleted span); matches in
    # % comments never do.
    in_block = any(old in m.group(2) for m in BLOCK_RE.finditer(text)) or any(
        # an --old spanning the words an inline point edit deletes plus the
        # plain text around them matches the block's reject-view, not its raw
        # body; count it so fn() can raise the precise refusal
        _is_inline_body(m.group(2))
        and " ".join(old.split()) in _deleted_view(m.group(2))
        for m in BLOCK_RE.finditer(text)
    )
    if visible.count(old) > 1:
        print(
            f"latex-draft: --old text matched {visible.count(old)} times in "
            "the document text (outside % comments and draft blocks; "
            "need exactly 1)",
            file=sys.stderr,
        )
        return 2
    if visible.count(old) == 0 and not in_block and _boundary_compose_target(text, old) is None:
        if text.count(old) == 0:
            print(
                "latex-draft: " + _not_found_message(text, old),
                file=sys.stderr,
            )
        else:
            print(
                "latex-draft: --old text only partially matches: it overlaps "
                "a % comment or a pending draft block (matches there do not "
                "count); draft over the visible text only",
                file=sys.stderr,
            )
        return 2
    preview = new if op == "replace" else old
    rc = _apply_draft(tex, state, target, fn, did, op, preview, author)
    if rc == 0 and composed.get("own"):
        rec = state["drafts"].get(did)
        if rec is not None:
            rec["own"] = composed["own"]
            save_state(tex, state)
    if rc == 0 and composed.get("host"):
        # The new draft's block now contains both edits; close the host
        # record as superseded (NOT hand_deleted: its text lives on inside
        # the composed block).
        host_rec = state["drafts"].get(composed["host"])
        if host_rec is not None and host_rec["status"] == "pending":
            host_rec["status"] = "rejected"
            host_rec["resolved"] = now()
            host_rec["superseded_by"] = did
            save_state(tex, state)
            print(
                f"latex-draft: composed with pending draft {composed['host']} "
                f"into block {did}; the earlier draft is marked rejected "
                f"(superseded) \u2014 accept/reject {did} to settle both edits"
            )
    return rc


def cmd_draft_replace(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _replace_or_delete(tex, state, args, "replace")


def cmd_draft_delete(tex: Path, state: dict, args: argparse.Namespace) -> int:
    return _replace_or_delete(tex, state, args, "delete")


def cmd_draft_comment(tex: Path, state: dict, args: argparse.Namespace) -> int:
    margin = getattr(args, "margin", False)
    return _insert_draft(
        tex, state, args, "comment",
        lambda did, author, bn: build_comment_block(did, args.text, margin=margin, block_number=bn),
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
            # Search preamble lines only (before \begin{document}), in the
            # text a reader sees (no % comments, no pending draft blocks):
            # an anchor must refer to a line of the user's own preamble.
            search = _searchable_lines(lines)
            hits = [
                i
                for i, line in enumerate(search)
                if i < bd and position in line
            ]
            if len(hits) != 1:
                raise DraftError(
                    f"anchor {position!r} matched {len(hits)} preamble lines "
                    "(need exactly 1)"
                )
            pos = hits[0] + 1
        block = block_for(text, build_preamble_block(did, text_arg, block_number=_next_block_number(state)))
        return insert_lines_at(text, pos, split_lines(block))

    return _apply_draft(
        tex, state, tex, fn, did, "preamble", text_arg, DEFAULT_AUTHOR,
        fields={"preamble": True},
    )


def _block_preview(tex: Path, rec: dict) -> str:
    """Preview of a draft from its actual block content in the file.

    The recorded preview comes from the CLI arguments; the block is what the
    review PDF shows, so `list` must show the block's content (they can
    diverge when a draft was composed into another block). Only the
    fragments the pending draft spliced are previewed: the rest of the
    body is plain unchanged text or markup from earlier accepted drafts.
    """
    try:
        text = read_tex(_file_of(tex, rec.get("file", tex.name)))
    except (OSError, DraftError):
        return rec.get("preview", "")
    m = _blocks_by_id(text).get(rec["id"])
    if m is None:
        return rec.get("preview", "")
    body = m.group(2)
    # Fragments this draft spliced (recorded at draft time) preview what
    # the pending edit changes; the whole body would also show markup from
    # earlier accepted drafts kept marked on the same line, whose tokens
    # do not belong to this block's decision.
    src = rec.get("own") or body
    prev = (
        extract_command_content(src, "added")
        or extract_command_content(src, "deleted")
        or extract_command_content(src, "footnote")
        or extract_command_content(src, "todo")
        or src
    )
    return " ".join(prev.split())[:120]


def cmd_list(tex: Path, state: dict, args: argparse.Namespace) -> int:
    drafts = state["drafts"]
    if not drafts:
        print("latex-draft: no drafts")
        return 0
    def _sort_key(kv):
        rec = kv[1]
        return (rec.get("block", 0), rec["created"])

    for did, rec in sorted(drafts.items(), key=_sort_key):
        block = f"#{rec['block']}" if "block" in rec else "#-"
        preview = (
            _block_preview(tex, rec)
            if rec["status"] == "pending"
            else rec.get("preview", "")
        )
        # The file column maps a block number seen in the PDF back to its
        # file in a multi-file document (without it every file had to be
        # grepped for its % latex-draft:<id> markers).
        fname = rec.get("file") or "-"
        print(
            f"{block:<4} {did}  {rec['status']:<8}  {rec['op']:<7}  "
            f"{rec['created']}  {fname:<20}  {preview}"
        )
    return 0


def _is_inline_body(body: str) -> bool:
    r"""Whether a draft block body is an inline point edit: markup mixed with
    plain text on one line (the body does not start with a wrapper command)."""
    has_markup = re.search(r"\\(added|deleted|footnote|todo)", body) is not None
    starts_with_wrapper = re.match(r"\s*\\(added|deleted|footnote|todo)", body) is not None
    return has_markup and not starts_with_wrapper


def _plain_deleted(body: str) -> str:
    r"""The plain form of a block body's \deleted span: wrapper syntax and
    the block-number marker removed, whitespace collapsed. Used to group
    sibling drafts whose \deleted spans differ only in markup form (an
    inline point edit vs a whole-line block over the same original line)."""
    deleted = extract_command_content(body, "deleted")
    if deleted is None:
        return ""
    plain = " ".join(re.sub(r"\s+", " ", deleted).split())
    return BLOCK_SUP_RE.sub("", plain).strip()


def _sibling_ids(
    tex: Path, state: dict, by_file: dict[str, list[str]], ids: list[str]
) -> set[str]:
    """Pending drafts that are alternatives to the drafts being accepted.

    Siblings share one \\deleted span: they describe changes to the same
    original text, so resolving the accepted one orphans the rest.
    """
    siblings: set[str] = set()
    for fname, file_ids in by_file.items():
        try:
            file_text = read_tex(_file_of(tex, fname))
        except (OSError, DraftError):
            continue  # unreadable file: _close_missing_blocks reports it
        blocks = _blocks_by_id(file_text)
        accepted_old: set[str] = set()
        for did in file_ids:
            block = blocks.get(did)
            if block is None:
                continue
            deleted = extract_command_content(block.group(2), "deleted")
            if deleted is not None:
                accepted_old.add(deleted)
                # also key the plain (relaxed) form of the deleted span, so an
                # inline point-edit host and a whole-line sibling over the
                # same original line group together
                accepted_old.add(_plain_deleted(block.group(2)))
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
            if deleted is not None and (
                deleted in accepted_old or _plain_deleted(block.group(2)) in accepted_old
            ):
                siblings.add(did)
    return siblings


def _inverse_unmarked(text: str, rec: dict) -> str:
    r"""Revert an unmarked draft's direct patch: replace the recorded
    span_after with the recorded span_before."""
    after = rec.get("span_after", "")
    before = rec.get("span_before", "")
    n = text.count(after)
    if n != 1:
        raise DraftError(
            f"the patched text is present {n} times (need exactly 1); the "
            "file was hand-edited since the draft"
        )
    return text.replace(after, before, 1)


def _close_missing_blocks(
    tex: Path, state: dict, ids: list[str], by_file: dict[str, list[str]], all_drafts: bool
) -> tuple[list[str], dict[str, list[str]], int | None]:
    """Handle drafts whose block was hand-deleted from the document.

    A vanished block cannot be resolved in the file, so its record is closed
    (rejected, `hand_deleted`) and the surviving drafts are resolved. This is
    not an error: "reject" means "make sure it is gone", and it is. The
    message says so instead of a confusing "not found" while `list` already
    shows the draft as rejected.
    """
    missing: dict[str, list[str]] = {}
    for fname, file_ids in by_file.items():
        target = _file_of(tex, fname)
        try:
            file_text = read_tex(target)
        except (OSError, DraftError) as e:
            # A file that cannot be read is NOT "all blocks hand-deleted":
            # resolving against it would misclassify every draft in it and
            # could later fabricate a duplicate file at a wrong path. Hard
            # error naming the path; only a readable file whose markers are
            # genuinely absent counts as hand-deletion.
            for did in file_ids:
                state["drafts"][did]["suspect"] = True
            save_state(tex, state)
            print(
                f"latex-draft: cannot read {target} ({e}); draft(s) "
                f"{', '.join(file_ids)} in {fname} are left pending and "
                "marked suspect \u2014 restore the file (or fix its recorded "
                "path) before accepting/rejecting them",
                file=sys.stderr,
            )
            return ids, by_file, 2
        gone = [did for did in file_ids if did not in _blocks_by_id(file_text)]
        if gone:
            missing[fname] = gone
    if not missing:
        return ids, by_file, None
    for fname, gone in missing.items():
        where = ", ".join(sorted(gone))
        print(
            f"latex-draft: draft(s) no longer in {fname} (block removed by "
            f"hand); marked as rejected: {where}",
            file=sys.stderr,
        )
        for did in gone:
            rec = state["drafts"][did]
            rec["status"] = "rejected"
            rec["resolved"] = now()
            rec["hand_deleted"] = True
    save_state(tex, state)
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
        lambda text: strip_changes_package_if_unused(
            text, injected, keep_todo=_document_has_todo(tex, text)
        ),
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
        if getattr(args, "block", None) is not None:
            matches = [
                d for d, r in state["drafts"].items()
                if r.get("block") == args.block and r["status"] == "pending"
            ]
            if not matches:
                print(
                    f"latex-draft: no pending draft with block #{args.block}",
                    file=sys.stderr,
                )
                return 2
            if len(matches) > 1:
                print(
                    f"latex-draft: block #{args.block} is ambiguous "
                    f"({', '.join(sorted(matches))}); use --id",
                    file=sys.stderr,
                )
                return 2
            ids = matches
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
            if rec.get("suspect"):
                print(
                    f"latex-draft: draft {did} is suspect: its recorded file "
                    f"({rec.get('file', tex.name)}) could not be read; restore "
                    "the file (or fix the path) first \u2014 refusing to resolve",
                    file=sys.stderr,
                )
                return 2

    # Unmarked drafts carry no block in the file: their direct patch is kept
    # on accept and reverted on reject (the inverse span is on the record).
    unmarked_ids = [did for did in ids if state["drafts"][did].get("unmarked")]
    if unmarked_ids:
        ids = [did for did in ids if did not in set(unmarked_ids)]

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
        did not in ids
        and did not in unmarked_ids
        and rec["status"] == "pending"
        for did, rec in state["drafts"].items()
    )
    sibling_ids = _sibling_ids(tex, state, by_file, ids) if accept else set()

    ids, by_file, error = _close_missing_blocks(tex, state, ids, by_file, args.all)
    if error is not None:
        return error
    # Resolve the unmarked drafts (no block to splice): accept keeps the
    # direct patch, reject reverts it via the recorded span.
    for did in unmarked_ids:
        rec = state["drafts"][did]
        fname = rec.get("file", tex.name)
        target = _file_of(tex, fname)
        if accept:
            rec["status"] = "accepted"
            rec["resolved"] = now()
            print(
                f"latex-draft: {verb}ed unmarked draft {did} "
                f"(change kept in {fname})"
            )
            continue
        try:
            mutate(
                tex,
                state,
                f"reject unmarked {did}",
                lambda t, r=rec: _inverse_unmarked(t, r),
                target=target,
            )
        except DraftError as e:
            rec["status"] = "rejected"
            rec["resolved"] = now()
            print(
                f"latex-draft: unmarked draft {did} marked rejected, but "
                f"reverting it failed ({e}); the change may still be in "
                f"{fname} — use `undo` to revert it",
                file=sys.stderr,
            )
            continue
        rec["status"] = "rejected"
        rec["resolved"] = now()
        print(f"latex-draft: rejected unmarked draft {did} (reverted in {fname})")
    # The provenance list is emptied once the preamble is stripped; an undo
    # of that strip brings the lines back, so recover them from the changelog
    # rather than leaving them in the document forever.
    injected = _injected_lines(tex, state)
    config = load_config(tex)
    spliced_ids: set[str] = set()
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

        # The accepted draft id (single-id accept) tells resolve_drafts
        # which sibling of a group performs the splice.
        accepted_id = ids[0] if accept and not args.all and len(ids) == 1 else None

        def fn(text: str, file_ids=all_ids) -> str:
            nonlocal spliced_ids
            own_map = {
                d: state["drafts"][d]["own"]
                for d in file_ids
                if (state["drafts"].get(d) or {}).get("own")
            }
            new_text, found, spliced = resolve_drafts(
                text,
                file_ids,
                accept,
                accepted_id=accepted_id,
                keep_markup=config["keep_markup"],
                style=config["style"],
                own_map=own_map,
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
                if _has_label_notes(tex, state):
                    # A note next to an unmarked label needs it too.
                    return new_text
                return strip_changes_package_if_unused(
                    new_text, injected, keep_todo=_document_has_todo(tex, new_text)
                )
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
    # changes package in final mode) — unless markup the package provides is
    # still there: accepted text kept marked, or a note for an unmarked label.
    if not any(rec["status"] == "pending" for rec in state["drafts"].values()):
        if not (config["keep_markup"] and _has_provenance(tex)) and not (
            _has_label_notes(tex, state)
        ):
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


def _log_error_lines(tex: Path) -> list[str]:
    """The actual TeX errors: lines starting with '!' in the LaTeX log."""
    log = tex.parent / (tex.stem + ".log")
    if not log.exists():
        return []
    return [
        line
        for line in log.read_text(errors="replace").splitlines()
        if line.startswith("!")
    ]


BLG_PROBLEM_RE = re.compile(
    r"(?i)(warning--|error message|\berror\b|couldn't|a bad|i'm skipping|"
    r"i was skipping|missing citation)"
)


def _blg_error_lines(tex: Path) -> list[str]:
    """BibTeX problems from the .blg (duplicate entry keys, malformed author
    lines, unfound entries). These never appear as '!' lines in the .log, so
    a build can fail while the log looks clean -- surface them on failure."""
    blg = tex.parent / (tex.stem + ".blg")
    if not blg.exists():
        return []
    return [
        line
        for line in blg.read_text(errors="replace").splitlines()
        if BLG_PROBLEM_RE.search(line)
    ]


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
    pdf = tex.parent / (tex.stem + ".pdf")
    # Hard gate on the final log: any '!' line is a failed build. A non-zero
    # exit is a second gate, unless the final log is clean and the PDF was
    # written (a BibTeX/bib issue, not a LaTeX failure) — then warn and pass.
    errors = _log_error_lines(tex)
    blg_problems = _blg_error_lines(tex)

    def report_blg() -> None:
        if blg_problems:
            print(
                f"latex-draft: {tex.stem}.blg reports problems "
                "(BibTeX warnings/errors -- duplicate keys, malformed entries):",
                file=sys.stderr,
            )
            for line in blg_problems[-15:]:
                print("  " + line, file=sys.stderr)

    if errors:
        print(
            f"latex-draft: compile failed ({len(errors)} LaTeX error(s) in "
            f"{tex.stem}.log; exit {code}); last log lines:",
            file=sys.stderr,
        )
        print(tail, file=sys.stderr)
        report_blg()
        print(
            "latex-draft: run `repair` to analyze the errors and fix them",
            file=sys.stderr,
        )
        return 1
    if code != 0:
        if pdf.exists():
            # A non-zero exit with a '!' -free final log and a written PDF
            # is not a broken LaTeX build: latexmk aborts on a BibTeX error
            # in a .bib file (which this tool does not manage) while the
            # document itself compiles. Failing the turn here makes the
            # skill's "a turn must not end with a broken build" rule
            # unsatisfiable; report the .blg problems as a warning instead.
            print(
                f"latex-draft: compiled {pdf.name} (exit {code}, but "
                f"{tex.stem}.log has no '!' error lines and the PDF was "
                "written; likely a BibTeX/bib issue, not a LaTeX failure)"
            )
            report_blg()
            return 0
        # Hard gate: a non-zero exit means the build is NOT verified, even when
        # the final log has no '!' lines (a review tool must not sign off a
        # broken build — error cascades can appear in later passes while the
        # last log scan raced them). Inspect the .blg/.log before trusting.
        print(
            f"latex-draft: compile failed (exit {code}; {tex.stem}.log has no "
            f"'!' error lines, and no {pdf.name} was produced); "
            f"last log lines:",
            file=sys.stderr,
        )
        print(tail, file=sys.stderr)
        report_blg()
        print(
            "latex-draft: run `repair` to analyze the errors and fix them",
            file=sys.stderr,
        )
        return 1
    print(f"latex-draft: compiled {pdf.name}")
    return 0


# ---------------------------------------------------------------- repair

UNDEFINED_CONTROL_RE = re.compile(
    # TeX reports "! Undefined control sequence." then either
    # "<recently read> \cmd" or "l.NN <source line with \cmd>" — the log line
    # is truncated at the error point and "the control sequence at the end of
    # the top line" is the undefined one, so match the LAST \cmd on the line
    # (greedy), not the first.
    r"! Undefined control sequence\.\s*\n"
    r"(?:<recently read>\s*)?\n?"
    r"(?:l\.(\d+)\s*)?"
    r"[^\n]*(\\[a-zA-Z@]+)"
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


def _unmarked_still_applied(text: str, rec: dict) -> bool:
    """Whether an unmarked draft's direct patch is still present in `text`."""
    after = rec.get("span_after")
    if after is None:
        return True
    if after == "":
        return True  # pure insert reverted already handled via span_before
    return text.count(after) >= 1


def _reconcile_draft_status(tex: Path, state: dict) -> int:
    """Make draft records match the document again.

    A block present in the file re-opens its record as pending (an undo or
    hand restore brought it back); a pending record whose block is gone (the
    change that created it was undone, or the file was restored by hand) is
    closed as rejected (`hand_deleted`). Unmarked drafts have no block, so
    their patch is checked directly in the file. Returns how many pending
    records were closed.
    """
    closed = 0
    for did, rec in state["drafts"].items():
        try:
            text = read_tex(_file_of(tex, rec.get("file", tex.name)))
        except (OSError, DraftError):
            # Unreadable file: not "hand-deleted". Keep the draft pending but
            # flag it so status/check surface the inconsistency.
            if rec["status"] == "pending":
                rec["suspect"] = True
            continue
        rec.pop("suspect", None)  # file readable again: the flag is stale
        if rec.get("unmarked"):
            if rec["status"] == "pending" and not _unmarked_still_applied(text, rec):
                rec["status"] = "rejected"
                rec["resolved"] = now()
                rec["hand_deleted"] = True
                closed += 1
            continue
        has_block = did in _blocks_by_id(text)
        if has_block and rec["status"] != "pending":
            rec["status"] = "pending"
            rec.pop("resolved", None)
            rec.pop("orphaned_by_overlap", None)
        elif not has_block and rec["status"] == "pending":
            rec["status"] = "rejected"
            rec["resolved"] = now()
            rec["hand_deleted"] = True
            closed += 1
    return closed


def _undo_targets(
    tex: Path, state: dict, args: argparse.Namespace
) -> tuple[list[dict], int | None]:
    """The changelog entries an undo reverts, plus an exit code when there is
    nothing to revert.

    `--turn` reverts the current turn, or the most recent turn that has
    changes (the common case: `undo` after `compile`). Entries a trailing run
    of undos already reverted are skipped, so `undo --turn` cannot loop.
    """
    all_entries = read_changelog(tex)
    entries = [e for e in all_entries if e.get("kind") in ("change", "setup")]
    if not entries:
        print("latex-draft: changelog is empty, nothing to undo", file=sys.stderr)
        return [], 1
    if not args.turn:
        if args.n < 1:
            print("latex-draft: --n must be >= 1", file=sys.stderr)
            return [], 2
        # Skip entries a trailing run of undos already reverted (so
        # `undo --n 5` after a plain `undo` does not conflict with itself).
        undone: set[str] = set()
        for e in reversed(all_entries):
            if e.get("kind") != "undo":
                break
            undone.add(e.get("undid_backup"))
        fresh = [e for e in entries if e.get("backup") not in undone]
        if not fresh:
            print("latex-draft: nothing to undo (last action was `undo`)")
            return [], 0
        return fresh[-args.n :], None
    turn = state.get("turn", 0)
    targets = [e for e in entries if e.get("turn", 0) == turn]
    if not targets:
        # fall back to the most recent turn that has any entry (a turn whose
        # only change was the one-time profile setup still counts)
        turn = max(e.get("turn", 0) for e in all_entries)
        targets = [e for e in entries if e.get("turn", 0) == turn]
        if not targets:
            targets = [e for e in all_entries if e.get("turn", 0) == turn
                       and e.get("kind") == "setup"]
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
        print(f"inputs:  {', '.join(_rel_key(tex, d) for d in inputs)}")
    print(f"turn:    {state.get('turn', 0)}")
    suspect = sorted(did for did, r in drafts.items() if r.get("suspect"))
    print(f"drafts:  {len(drafts)} total, {pending} pending")
    if suspect:
        print(
            f"SUSPECT: {len(suspect)} draft(s) in unreadable file(s): "
            + ", ".join(suspect)
            + " \u2014 their file could not be read; resolve the file problem first"
        )
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


ABBREV_REF_RE = re.compile(
    r"(?<![\\A-Za-z])([A-Za-z]+)\.(\\(?:ref|Cref|autoref|pageref|eqref|cref|cite[pt]?)\b)"
)


def _abbrev_ref_notes(tex: Path) -> list[str]:
    r"""`App.\ref{app:C}` typesets as "App.C": TeX gobbles nothing here — the
    source simply has no space between the abbreviation's period and the
    reference command. Report likely missing `~` / spaces (non-failing)."""
    notes: list[str] = []
    for f in watched_files(tex):
        text = _read_or_none(f)
        if text is None:
            continue
        visible = _mask_comments_and_blocks(text)
        for m in ABBREV_REF_RE.finditer(visible):
            notes.append(
                f"{f.name}: '{m.group(1)}.{m.group(2)}' — no space between the "
                "abbreviation and the reference; it will typeset as one word "
                "(use 'App.~\\ref{...}')"
            )
    return notes


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
    suspect = sorted(did for did, rec in state["drafts"].items() if rec.get("suspect"))
    if suspect:
        problems.append(
            f"{len(suspect)} suspect draft(s) in unreadable file(s): "
            + ", ".join(suspect)
            + " (their recorded file could not be read; check the path / restore the file)"
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
        known = pending_by_file.get(f.name, set()) | pending_by_file.get(
            _rel_key(tex, f), set()
        )
        scrubbed = text
        for m in _blocks_by_id(text).values():
            if m.group(1) in known:
                scrubbed = scrubbed.replace(m.group(0), "")
        stray = len(re.findall(r"% latex-draft(-end)?:[0-9a-f]{8}", scrubbed))
        if stray:
            problems.append(f"{f.name}: {stray} stray latex-draft marker line(s)")
    marked = (
        load_config(tex)["keep_markup"] and _has_provenance(tex)
    ) or _has_label_notes(tex, state)
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
    notes = _abbrev_ref_notes(tex)
    if problems:
        print(f"latex-draft: check FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        for n in notes:
            print(f"  note: {n}")
        return 1
    print("latex-draft: check OK — no pending drafts, no stray markers, "
          "no leftover preamble")
    for n in notes:
        print(f"latex-draft: note: {n}")
    if marked and _changes_mode(read_tex(tex)) != "final":
        print(
            "latex-draft: changes-package markup is still present (accepted "
            "text kept marked, or a note for a label); the review PDF shows "
            "it. For production, build with \\usepackage[final]{changes} so "
            "the marks and notes are dropped."
        )
    return 0


# Markup this tool leaves in a document, and what a final copy does with it.
TOOL_MARKUP_RE = re.compile(
    r"\\(added|highlight|deleted|comment|todo|replaced)(?![A-Za-z])(\[[^\]]*\])?"
)
GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
BIBLIOGRAPHY_RE = re.compile(r"\\(?:bibliography|addbibresource)\{([^}]+)\}")
GRAPHICS_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps")


def _brace_args(text: str, start: int, count: int) -> tuple[list[str], int] | None:
    """Read `count` brace arguments starting at `start`, or None if malformed."""
    args: list[str] = []
    i = start
    for _ in range(count):
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if i >= len(text) or text[i] != "{":
            return None
        depth, out = 1, []
        i += 1
        while i < len(text) and depth:
            ch = text[i]
            if ch == "\\" and i + 1 < len(text):
                out.append(ch)
                out.append(text[i + 1])
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            out.append(ch)
            i += 1
        if depth:
            return None
        args.append("".join(out))
    return args, i


def _strip_markup(text: str, counts: dict[str, int]) -> str:
    r"""Remove the changes markup, keeping the text that was accepted.

    `\added`/`\highlight` keep their argument, `\replaced` keeps its new text,
    `\deleted`/`\comment`/`\todo` are dropped entirely. Nested markup is
    handled recursively; a command whose argument does not parse is left as
    it is rather than corrupting the file.
    """
    out: list[str] = []
    pos = 0
    for m in TOOL_MARKUP_RE.finditer(text):
        if m.start() < pos:
            continue
        name = m.group(1)
        parsed = _brace_args(text, m.end(), 2 if name == "replaced" else 1)
        if parsed is None:
            continue
        args, end = parsed
        out.append(text[pos : m.start()])
        if name in ("added", "highlight", "replaced"):
            out.append(_strip_markup(args[0], counts))
        counts[name] = counts.get(name, 0) + 1
        pos = end
        if name not in ("added", "highlight", "replaced") and text[pos : pos + 1] == " ":
            # A dropped command leaves its separating space behind: the word
            # diff wrote "\\deleted{x} \\added{y}" with one, and keeping it
            # would double the space (or strand a leading one at a line
            # start). Exactly one: the space that preceded the command is
            # the word separator the kept text needs.
            pos += 1
    out.append(text[pos:])
    return "".join(out)


def _clean_source(text: str, injected: list[str] | None) -> tuple[str, dict[str, int]]:
    """A document with every trace of this tool removed."""
    counts: dict[str, int] = {}
    text = _strip_markup(text, counts)
    markers = sum(
        1
        for line in split_lines(text)
        if BLOCK_BEGIN_LINE_RE.fullmatch(line) or BLOCK_END_LINE_RE.fullmatch(line)
    )
    if markers:
        counts["marker-line"] = markers
    text = _drop_lines(
        text,
        lambda line: bool(
            BLOCK_BEGIN_LINE_RE.fullmatch(line) or BLOCK_END_LINE_RE.fullmatch(line)
        ),
    )
    return strip_changes_package_if_unused(text, injected), counts


def _referenced_assets(texts: list[str], base: Path) -> list[Path]:
    """Graphics and bibliographies the cleaned sources refer to."""
    found: list[Path] = []
    for text in texts:
        for m in GRAPHICS_RE.finditer(text):
            candidate = base / m.group(1).strip()
            paths = (
                [candidate]
                if candidate.suffix
                else [candidate.with_suffix(ext) for ext in GRAPHICS_EXTENSIONS]
            )
            found.extend(p for p in paths if p.is_file())
        for m in BIBLIOGRAPHY_RE.finditer(text):
            for name in m.group(1).split(","):
                candidate = base / name.strip()
                paths = [candidate] if candidate.suffix else [candidate.with_suffix(".bib")]
                found.extend(p for p in paths if p.is_file())
    return found


def cmd_finalize(tex: Path, state: dict, args: argparse.Namespace) -> int:
    """Write a clean copy of the sources: all corrections, no tool traces.

    Accepted text stays, with the markup unwrapped and the notes dropped, so
    the copy builds the same PDF as `changes` in final mode and can be read
    (or submitted) without knowing this tool. The original is untouched.
    Flattening into one file and pruning for arXiv are left to latexpand /
    arxivable, which do not understand the changes markup.
    """
    pending = [
        did for did, rec in state["drafts"].items() if rec["status"] == "pending"
    ]
    if pending:
        print(
            f"latex-draft: {len(pending)} draft(s) still pending "
            f"({', '.join(sorted(pending))}); accept or reject them first — "
            "their markup is what a clean copy would have to guess at",
            file=sys.stderr,
        )
        return 1
    out_dir = Path(args.out).expanduser() if args.out else tex.parent / f"{tex.stem}-final"
    out_dir = out_dir.resolve()
    injected = _injected_lines(tex, state) or None
    counts: dict[str, int] = {}
    cleaned: list[str] = []
    written = 0
    for f in watched_files(tex):
        try:
            rel = f.resolve().relative_to(tex.parent.resolve())
        except ValueError:
            rel = Path(f.name)
        source, file_counts = _clean_source(read_tex(f), injected)
        for key, value in file_counts.items():
            counts[key] = counts.get(key, 0) + value
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_tex(dest, source)
        cleaned.append(source)
        written += 1
    for asset in _referenced_assets(cleaned, tex.parent):
        try:
            rel = asset.resolve().relative_to(tex.parent.resolve())
        except ValueError:
            rel = Path(asset.name)
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset, dest)
        written += 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing to remove"
    print(f"latex-draft: clean copy in {out_dir} ({summary}; {written} file(s))")
    print(
        f"latex-draft: the original is untouched; build {out_dir / tex.name} "
        "for submission"
    )
    return 0


def cmd_show(tex: Path, state: dict, args: argparse.Namespace) -> int:
    """Print a file the way `accept --all` would leave it.

    The accepted view of the source: markup unwrapped (\\added keeps its
    text, \\deleted is dropped), draft marker lines and the auto-injected
    preamble removed. Pending drafts are included as their accepted form,
    so this is the cheap way to verify what a reviewed change actually
    looks like in the running text — grep-able, unlike the two-column PDF.
    For a submission copy use `finalize --out`.
    """
    target = _file_of(tex, args.file) if args.file else tex
    if target != tex and not target.is_file():
        print(f"latex-draft: no such document file: {args.file}", file=sys.stderr)
        return 2
    injected = _injected_lines(tex, state) or None
    source, _counts = _clean_source(read_tex(target), injected)
    # the ^N block-number superscript is a review artifact; the accepted
    # view is for reading the running text
    source = BLOCK_SUP_UNANCHORED_RE.sub("", source)
    sys.stdout.write(source)
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
    "finalize": cmd_finalize,
    "show": cmd_show,
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
    p.add_argument(
        "--unmarked",
        action="store_true",
        help="insert without changes markup: for content the PDF cannot show "
        "(a \\label on an equation) or that the markup cannot carry (a float "
        "environment). The change is snapshotted, changelogged and undoable.",
    )
    add_file_option(p, p)
    add_author_option(p, p)

    p = sub.add_parser("draft-replace", help="draft replacing existing text")
    p.add_argument("--old", required=True, help="text to replace (must occur exactly once)")
    p.add_argument("--new", required=True, help="replacement LaTeX text")
    p.add_argument(
        "--unmarked", action="store_true",
        help="replace without changes markup (no review PDF change): for edits "
        "inside display math, floats or captions, where \\added/\\deleted "
        "break the build. Snapshotted, changelogged, undoable.",
    )
    add_file_option(p, p)
    add_author_option(p, p)

    p = sub.add_parser("draft-delete", help="draft deleting existing text")
    p.add_argument("--old", required=True, help="text to delete (must occur exactly once)")
    p.add_argument(
        "--unmarked", action="store_true",
        help="delete without changes markup (no review PDF change); see "
        "draft-replace --unmarked",
    )
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
        g.add_argument("--block", type=int, metavar="N",
                       help="block number N (see `list`, e.g. #2)")
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

    p = sub.add_parser(
        "show",
        help="print a file the way accept --all would leave it "
        "(markup unwrapped, \\deleted dropped) — grep-able verification",
    )
    p.add_argument(
        "--accepted",
        action="store_true",
        default=True,
        help="the accepted view (the only mode; explicit for readability)",
    )
    add_file_option(p, p)

    p = sub.add_parser(
        "finalize",
        help="write a clean copy of the sources: corrections kept, markup and "
        "notes removed, no tool dependency; the original is untouched",
    )
    p.add_argument(
        "--out",
        default=None,
        help="output directory (default: <main>-final next to the main file)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tex = args.tex.expanduser().resolve()
    if not tex.is_file():
        print(f"latex-draft: no such file: {tex}", file=sys.stderr)
        return 2
    # One invocation at a time per document: serialize on the lock file so
    # rapid successive calls queue instead of racing on the state and files.
    with _session_lock(tex):
        state = load_state(tex)
        if "_recovered" in state:
            recovered = state.pop("_recovered")
            print(
                "latex-draft: WARNING: the state file was unreadable; "
                f"recovered {recovered} pending draft(s) from the document markers",
                file=sys.stderr,
            )
        # Turn-start safety: make sure a full backup exists, and check whether
        # the user edited the file (or its inputs) by hand since the last tool
        # action.
        try:
            ensure_full_backup(tex, state)
            check_and_adopt_external(tex, state)
            rc = HANDLERS[args.command](tex, state, args)
        except DraftError as e:
            return _fail(e)
        # One-time first-draft setup: define the changes profiles (the agent
        # AI + every author parsed from \author{...}), each with its own
        # color. Runs only after a successful body draft, so refused or failed
        # commands never modify the document, and draft-preamble stays free
        # of the changes package. It is a "setup" changelog entry that replays
        # like any other change in undo, and it always follows the draft's own
        # preamble-injection entry, so a full undo still restores the original
        # document byte for byte.
        if (
            rc == 0
            and args.command
            in ("draft-insert", "draft-replace", "draft-delete", "draft-comment")
            and not state.get("profiles_defined")
        ):
            ensure_author_profiles(tex, state)
            state["profiles_defined"] = True
            save_state(tex, state)
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
