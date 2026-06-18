"""
tools/file_edit_tool.py

基于 Search/Replace Block 的安全文件编辑工具。

模型需要输出一个或多个如下格式的编辑块：

    path/to/file.py
    <<<<<<< SEARCH
    old code
    =======
    new code
    >>>>>>> REPLACE

本模块负责解析这些编辑块，检查每个 SEARCH 片段是否精确命中一次，
生成 unified diff 预览，并且只在所有编辑都通过校验后才写入文件。
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
    """解析或应用 Search/Replace Block 失败时抛出的受控异常。"""


@dataclass(frozen=True)
class SearchReplaceEdit:
    """模型编辑块解析后的结构化表示，尚未访问文件系统校验。"""

    path: str
    search: str
    replace: str


@dataclass(frozen=True)
class PreparedFileEdit:
    """某个文件在内存中计算出的最终修改结果。"""

    path: Path
    original: str
    updated: str


@dataclass(frozen=True)
class EditPlan:
    """已经通过 dry-run 校验、可用于预览或写入的一组编辑计划。"""

    edits: list[SearchReplaceEdit]
    files: list[PreparedFileEdit]
    diff: str


@dataclass(frozen=True)
class ApplyResult:
    """编辑成功写入磁盘后的结果。"""

    files_written: int
    diff: str


def parse_search_replace_blocks(text: str) -> list[SearchReplaceEdit]:
    """
    将模型输出解析为结构化编辑对象。

    解析器会严格检查标记行。完整 block 前后或 block 之间的额外说明文字会被忽略；
    但只要出现格式不完整的 block，就会让整个解析失败，方便下一轮让模型修正输出。
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

        path = _normalize_edit_path_line(_line_text(lines[i - 1]).strip())
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
    在不写入磁盘的情况下校验编辑，并计算每个文件的最终内容。

    同一个文件上的多个编辑会按模型给出的顺序在内存中依次应用。
    任何一个编辑失败，整个计划都会中止。
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
    解析编辑路径，并调用预留的权限检查钩子。

    默认不做额外权限限制。以后调用方可以通过 path_policy 接入 sandbox、
    allowlist、软链接逃逸检查或 repo 边界检查，而不用改解析和应用流程。
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
    """为所有发生变化的文件生成 unified diff 预览。"""
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
    将所有准备好的文件写入磁盘；如果 dry-run 后文件发生变化，则中止写入。

    普通文件系统很难提供真正的跨文件事务，因此这里采用实用型事务策略：
    先复查所有文件，全部通过后才开始写入；如果后续某个文件写入失败，
    则尽力把已经写入的文件恢复为原内容。
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
        # 尽力回滚已经写入的文件，降低部分写入导致工作区不一致的风险。
        for file_edit in reversed(written):
            try:
                _write_text(file_edit.path, file_edit.original)
            except OSError:
                pass
        raise FileEditError(f"Apply failed while writing files: {exc}") from exc

    return ApplyResult(files_written=len(changed_files), diff=plan.diff)


class FileEditTool(BaseTool):
    """Search/Replace Block 编辑能力的工具封装。"""

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
            "Preferred tool for modifying existing files. Apply precise "
            "Search/Replace Blocks safely: parse model edits, dry-run exact "
            "SEARCH matches, return a unified diff preview, and write all "
            "files only if every edit succeeds. Use file_write only for new "
            "files or intentional full-file rewrites."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "edits_text": {
                    "type": "string",
                    "description": (
                        "One or more Search/Replace Blocks. The line immediately "
                        "before <<<<<<< SEARCH must be the raw file path only, "
                        "for example python_programs/bucketsort.py; do not prefix "
                        "it with 'path:'. Format: file.py, <<<<<<< SEARCH, old "
                        "code, =======, new code, >>>>>>> REPLACE."
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
            # 开启 dry_run 后不写入磁盘，直接返回校验结果和 diff 预览。
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


def _normalize_edit_path_line(path: str) -> str:
    """Accept the common model slip of writing 'path: file.py'."""
    stripped = path.strip()
    if stripped.lower().startswith("path:"):
        return stripped[len("path:"):].strip()
    return stripped


def _looks_like_marker(text: str) -> bool:
    return text.startswith("<<<<<<<") or text == SEPARATOR_MARKER or text.startswith(">>>>>>>")


def _read_text_preserving_newlines(path: Path) -> str:
    # newline="" 会关闭通用换行转换，尽量保留文件原始文本。
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def _write_text(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _adapt_edit_newlines(search: str, replace: str, file_content: str) -> tuple[str, str]:
    """
    让常见的 LF 模型输出可以匹配 CRLF 文件，同时不改变编辑意图。

    模型通常输出 LF 片段。如果目标文件主要使用 CRLF，且片段本身没有 CRLF，
    则在精确匹配前把 SEARCH 和 REPLACE 一起转换为 CRLF。
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
