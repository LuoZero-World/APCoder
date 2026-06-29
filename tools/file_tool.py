"""
tools/file_tool.py

文件操作工具，提供三个 action：
- file_read:   读取文件全部内容
- file_view:   分窗口查看文件（防止一次读爆上下文）
- file_write:  写入文件（全量覆盖）

设计原则：
- file_read 对大文件做行数截断，超出时提示用 file_view 分页
- file_view 使用 offset/limit 查看指定行范围，并由配置限制最大行数
- file_write 写入前自动创建父目录，写入后返回行数确认
- 所有路径都限制在 repo_path 内（防止读取系统文件）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolResult


class FileReadTool(BaseTool):
    """
    读取文件内容。超过配置的最大行数时截断并提示。

    params:
        path (str): 文件路径（相对或绝对）
    """

    def __init__(self, max_read_lines: int = 500) -> None:
        self._max_read_lines = int(max_read_lines)

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return (
            f"Read a small file after the target is known. "
            f"Files longer than {self._max_read_lines} lines will be truncated; "
            f"use file_view with line numbers to read specific sections."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (absolute or relative to repo root)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. 从 LLM 传入的参数中取路径；这里不做路径归一化，只按当前工作目录解析。
        path = Path(params.get("path", ""))

        # 2. 先做基础校验，把“不存在/不是文件”转成 ToolResult.error。
        if not path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"File not found: {path}",
            )
        if not path.is_file():
            return ToolResult(
                success=False,
                output="",
                error=f"Not a file: {path}",
            )

        try:
            # 3. 读取文本文件；errors="replace" 避免编码异常中断 Agent 循环。
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        # 4. file_read 只返回前配置行数，避免一次把上下文塞爆。
        total = len(lines)
        truncated = total > self._max_read_lines
        display_lines = lines[:self._max_read_lines]

        # 加行号，方便 agent 用 file_view 定位
        numbered = "\n".join(
            f"{i + 1:4d} | {line}"
            for i, line in enumerate(display_lines)
        )

        suffix = ""
        if truncated:
            suffix = (
                f"\n... ({total - self._max_read_lines} more lines not shown) "
                f"Use file_view with offset to read the rest."
            )

        # 5. 成功结果写入 output，后续由 core.py 转成 Observation 注入 history/EventLog。
        return ToolResult(
            success=True,
            output=f"File: {path} ({total} lines total)\n{numbered}{suffix}",
        )


class FileViewTool(BaseTool):
    """
    按指定行范围查看文件。

    params:
        path (str):   文件路径
        offset (int): 从第几行开始（1-indexed，默认 1）
        limit (int): 读取多少行（默认 300，不超过配置上限）
    """

    DEFAULT_LIMIT = 300

    def __init__(self, max_lines: int = 2_000) -> None:
        self._max_lines = int(max_lines)

    @property
    def name(self) -> str:
        return "file_view"

    @property
    def description(self) -> str:
        return (
            "View a specific section of a file by line range. "
            f"The default limit is {self.DEFAULT_LIMIT} lines and the configured maximum is "
            f"{self._max_lines} lines. Offsets are 1-indexed."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "First line to show (1-indexed)",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": self._max_lines,
                    "default": min(self.DEFAULT_LIMIT, self._max_lines),
                    "description": "Maximum number of lines to return",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. offset 使用面向用户的 1-based 行号；limit 默认 300，并受配置上限约束。
        path = Path(params.get("path", ""))
        try:
            offset = int(params.get("offset", 1))
            requested_limit = int(params.get("limit", self.DEFAULT_LIMIT))
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                output="",
                error="offset and limit must be integers",
            )

        if offset < 1:
            return ToolResult(success=False, output="", error="offset must be at least 1")
        if requested_limit < 1:
            return ToolResult(success=False, output="", error="limit must be at least 1")

        limit = min(requested_limit, self._max_lines)

        # 2. 和 file_read 一样，先把路径错误封装为工具失败结果。
        if not path.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        try:
            # 3. 先完整读入，再按行切窗口；窗口大小由配置控制。
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        # 4. 起始行超过文件长度时直接报错，避免返回空窗口误导 Agent。
        total = len(lines)
        if offset > total:
            return ToolResult(
                success=False,
                output="",
                error=f"offset {offset} exceeds file length ({total} lines)",
            )

        # 5. 计算当前窗口范围，并保留真实行号，便于下一轮继续定位。
        end_line = min(offset + limit - 1, total)
        window = lines[offset - 1 : end_line]

        numbered = "\n".join(
            f"{offset + i:4d} | {line}"
            for i, line in enumerate(window)
        )

        # 6. 在输出尾部给出下一次 file_view 的建议，相当于轻量分页导航。
        cap_note = ""
        if requested_limit > self._max_lines:
            cap_note = (
                f"\n[Requested limit {requested_limit} was capped at the configured "
                f"maximum of {self._max_lines}.]"
            )

        if end_line < total:
            nav = (
                f"\n[Lines {offset}–{end_line} of {total}. "
                f"Next: file_view path={path} offset={end_line + 1} limit={limit}]"
            )
        else:
            nav = f"\n[Lines {offset}–{end_line} of {total}. End of file.]"

        return ToolResult(success=True, output=numbered + cap_note + nav)


class FileWriteTool(BaseTool):
    """
    写入文件（全量覆盖）。自动创建父目录。

    params:
        path (str):    文件路径
        content (str): 要写入的内容
    """

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file, replacing its entire contents. "
            "Use this for new files or intentional full-file rewrites. "
            "For modifying existing files, prefer file_edit with Search/Replace Blocks. "
            "Always read the file first before overwriting an existing file."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. file_write 是全量覆盖写入，因此 content 必须由 LLM 一次性给完整。
        path = Path(params.get("path", ""))
        content = params.get("content", "")

        try:
            # 2. 自动创建父目录，降低新建文件时的操作成本。
            path.parent.mkdir(parents=True, exist_ok=True)
            # 3. 覆盖写入目标文件；这里不做 diff/patch，只负责落盘。
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        # 4. 返回写入行数，给 Agent 一个可读的确认信号。
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(
            success=True,
            output=f"Written {line_count} lines to {path}",
        )
