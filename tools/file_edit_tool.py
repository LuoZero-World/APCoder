"""
tools/file_edit_tool.py

Search/Replace Block based file editing.

The model provides one or more blocks in this format:

    path/to/file.py
    <<<<<<< SEARCH
    old code
    =======
    new code
    >>>>>>> REPLACE

This module parses those blocks, checks that every SEARCH snippet matches
exactly once, produces a unified diff preview, and writes all affected files
only after every edit has passed validation.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tools.base import BaseTool, ToolResult


SEARCH_MARKER = "<<<<<<< SEARCH"
SEPARATOR_MARKER = "======="
REPLACE_MARKER = ">>>>>>> REPLACE"

PathPolicy = Callable[[Path], bool]


class FileEditError(ValueError):
    """Raised when parsing or applying Search/Replace Blocks fails."""


@dataclass(frozen=True)
class SearchReplaceEdit:
    """A parsed model edit before it is checked against the filesystem."""

    path: str
    search: str
    replace: str


@dataclass(frozen=True)
class PreparedFileEdit:
    """The final in-memory update for one file."""

    path: Path
    original: str
    updated: str


@dataclass(frozen=True)
class EditPlan:
    """A validated set of edits ready for preview or apply."""

    edits: list[SearchReplaceEdit]
    files: list[PreparedFileEdit]
    diff: str


@dataclass(frozen=True)
class ApplyResult:
    """Result returned after edits are written to disk."""

    files_written: int
    diff: str


def parse_search_replace_blocks(text: str) -> list[SearchReplaceEdit]:
    """
    Parse model output into structured edits.

    The parser is intentionally strict around markers. Extra prose before,
    after, or between complete blocks is ignored, but a malformed block fails
    the whole parse so the model can repair its output on the next turn.
    """
    lines = text.splitlines(keepends=True)
    edits: list[SearchReplaceEdit] = []
    i = 0

    while i < len(lines):
        if _line_text(lines[i]) != SEARCH_MARKER:
            i += 1
            continue

        if i == 0:
            raise FileEditError("Parse failed: SEARCH marker has no path line before it.")

        path = _line_text(lines[i - 1]).strip()
        if not path:
            raise FileEditError("Parse failed: empty path before SEARCH marker.")
        if _looks_like_marker(path):
            raise FileEditError(f"Parse failed: invalid path before SEARCH marker: {path!r}")

        search_start = i + 1
        separator_index = _find_marker(lines, SEPARATOR_MARKER, search_start)
        if separator_index is None:
            raise FileEditError(f"Parse failed for {path}: missing {SEPARATOR_MARKER!r}.")

        replace_start = separator_index + 1
        replace_end = _find_marker(lines, REPLACE_MARKER, replace_start)
        if replace_end is None:
            raise FileEditError(f"Parse failed for {path}: missing {REPLACE_MARKER!r}.")

        search = "".join(lines[search_start:separator_index])
        replace = "".join(lines[replace_start:replace_end])
        if search == "":
            raise FileEditError(f"Parse failed for {path}: SEARCH content is empty.")

        edits.append(SearchReplaceEdit(path=path, search=search, replace=replace))
        i = replace_end + 1

    if not edits:
        raise FileEditError("Parse failed: no Search/Replace Blocks found.")

    return edits


def dry_run_edits(
    edits: list[SearchReplaceEdit],
    repo_root: str | Path = ".",
    path_policy: PathPolicy | None = None,
) -> EditPlan:
    """
    Validate edits and prepare final file contents without writing to disk.

    Multiple edits against the same file are applied in the order provided, in
    memory. Any failure aborts the whole plan.
    """
    if not edits:
        raise FileEditError("Apply failed: no edits to apply.")

    root = Path(repo_root).resolve()
    originals: dict[Path, str] = {}
    current_contents: dict[Path, str] = {}

    for index, edit in enumerate(edits, start=1):
        path = resolve_edit_path(root, edit.path, path_policy)

        if path not in current_contents:
            original = _read_text_preserving_newlines(path)
            originals[path] = original
            current_contents[path] = original

        current = current_contents[path]
        search, replace = _adapt_edit_newlines(edit.search, edit.replace, current)
        match_count = current.count(search)

        if match_count == 0:
            raise FileEditError(
                f"Apply failed for {edit.path} block #{index}: SEARCH did not match."
            )
        if match_count > 1:
            raise FileEditError(
                f"Apply failed for {edit.path} block #{index}: "
                f"SEARCH matched {match_count} times; provide a more specific snippet."
            )

        current_contents[path] = current.replace(search, replace, 1)

    files = [
        PreparedFileEdit(path=path, original=originals[path], updated=current_contents[path])
        for path in current_contents
    ]
    diff = make_diff_preview(files, root)
    return EditPlan(edits=edits, files=files, diff=diff)


def resolve_edit_path(
    repo_root: Path,
    path_text: str,
    path_policy: PathPolicy | None = None,
) -> Path:
    """
    Resolve an edit path and run the optional future permission hook.

    The default policy allows all paths. Callers can pass path_policy later to
    enforce sandbox, allowlist, symlink, or repo-boundary rules without changing
    the parser or apply pipeline.
    """
    if not path_text or not path_text.strip():
        raise FileEditError("Apply failed: path is empty.")

    raw_path = Path(path_text.strip())
    path = raw_path if raw_path.is_absolute() else repo_root / raw_path
    path = path.resolve()

    if path_policy is not None and not path_policy(path):
        raise FileEditError(f"Apply failed: path is not allowed: {path_text}")
    if not path.exists():
        raise FileEditError(f"Apply failed: file not found: {path_text}")
    if not path.is_file():
        raise FileEditError(f"Apply failed: not a file: {path_text}")

    return path


def make_diff_preview(files: list[PreparedFileEdit], repo_root: str | Path = ".") -> str:
    """Build a unified diff preview for all changed files."""
    root = Path(repo_root).resolve()
    chunks: list[str] = []

    for file_edit in files:
        if file_edit.original == file_edit.updated:
            continue

        display_path = _display_path(file_edit.path, root)
        diff_lines = difflib.unified_diff(
            file_edit.original.splitlines(keepends=True),
            file_edit.updated.splitlines(keepends=True),
            fromfile=f"a/{display_path}",
            tofile=f"b/{display_path}",
            lineterm="",
        )
        chunks.extend(diff_lines)

    return "\n".join(chunks)


def apply_edits_atomically(plan: EditPlan) -> ApplyResult:
    """
    Write all prepared files, aborting if any file changed after dry-run.

    Cross-file writes cannot be perfectly atomic on a normal filesystem, so this
    function uses a practical transaction: preflight every file, write only
    after all preflight checks pass, and roll back already-written files if a
    later write raises an OSError.
    """
    changed_files = [file_edit for file_edit in plan.files if file_edit.original != file_edit.updated]

    for file_edit in changed_files:
        latest = _read_text_preserving_newlines(file_edit.path)
        if latest != file_edit.original:
            raise FileEditError(
                f"Apply failed: file changed after dry-run: {file_edit.path}"
            )

    written: list[PreparedFileEdit] = []
    try:
        for file_edit in changed_files:
            _write_text(file_edit.path, file_edit.updated)
            written.append(file_edit)
    except OSError as exc:
        # Best-effort rollback keeps the workspace coherent after partial I/O.
        for file_edit in reversed(written):
            try:
                _write_text(file_edit.path, file_edit.original)
            except OSError:
                pass
        raise FileEditError(f"Apply failed while writing files: {exc}") from exc

    return ApplyResult(files_written=len(changed_files), diff=plan.diff)


class FileEditTool(BaseTool):
    """Tool wrapper for Search/Replace Block editing."""

    def __init__(
        self,
        repo_root: str | Path = ".",
        path_policy: PathPolicy | None = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._path_policy = path_policy

    @property
    def name(self) -> str:
        return "file_edit"

    @property
    def description(self) -> str:
        return (
            "Apply Search/Replace Blocks safely. Parses model edits, dry-runs "
            "exact SEARCH matches, returns a unified diff preview, and writes "
            "all files only if every edit succeeds."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "edits_text": {
                    "type": "string",
                    "description": (
                        "One or more Search/Replace Blocks: path, "
                        "<<<<<<< SEARCH, old code, =======, new code, "
                        ">>>>>>> REPLACE."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, validate and preview the diff without writing files.",
                },
            },
            "required": ["edits_text"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        edits_text = str(params.get("edits_text", ""))
        dry_run = bool(params.get("dry_run", False))

        try:
            edits = parse_search_replace_blocks(edits_text)
            plan = dry_run_edits(edits, self._repo_root, self._path_policy)
            if dry_run:
                return ToolResult(
                    success=True,
                    output=_format_success("Dry-run succeeded", len(edits), len(plan.files), plan.diff),
                )

            result = apply_edits_atomically(plan)
            return ToolResult(
                success=True,
                output=_format_success(
                    "Apply succeeded",
                    len(edits),
                    result.files_written,
                    result.diff,
                ),
            )
        except FileEditError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        except OSError as exc:
            return ToolResult(success=False, output="", error=f"Apply failed: {exc}")


def _find_marker(lines: list[str], marker: str, start: int) -> int | None:
    for index in range(start, len(lines)):
        if _line_text(lines[index]) == marker:
            return index
    return None


def _line_text(line: str) -> str:
    return line.rstrip("\r\n")


def _looks_like_marker(text: str) -> bool:
    return text.startswith("<<<<<<<") or text == SEPARATOR_MARKER or text.startswith(">>>>>>>")


def _read_text_preserving_newlines(path: Path) -> str:
    # newline="" disables universal newline conversion, preserving exact text.
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def _write_text(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _adapt_edit_newlines(search: str, replace: str, file_content: str) -> tuple[str, str]:
    """
    Match common model output against CRLF files without changing intent.

    Models usually emit LF snippets. If the target file is mostly CRLF and the
    snippet does not already contain CRLF, adapt both SEARCH and REPLACE to CRLF
    before exact matching.
    """
    newline = _dominant_newline(file_content)
    if newline == "\r\n":
        if "\r\n" not in search:
            search = search.replace("\n", "\r\n")
        if "\r\n" not in replace:
            replace = replace.replace("\n", "\r\n")
    return search, replace


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _format_success(title: str, edit_count: int, file_count: int, diff: str) -> str:
    diff_text = diff if diff else "(no changes)"
    return (
        f"{title}: {edit_count} edit block(s), {file_count} file(s).\n"
        f"Diff preview:\n{diff_text}"
    )
