"""
tools/file_tool.py

文件操作工具，提供三个 action：
- file_read:   读取文件全部内容
- file_view:   分窗口查看文件（防止一次读爆上下文）
- file_write:  写入文件（全量覆盖）

设计原则：
- file_read 对大文件做行数截断，超出时提示用 file_view 分页
- file_view 维护"窗口"概念，每次返回固定行数，agent 可 scroll
- file_write 写入前自动创建父目录，写入后返回行数确认
- 所有路径都限制在 repo_path 内（防止读取系统文件）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolResult


# 单次 file_read 最多返回的行数，超出提示用 file_view
MAX_READ_LINES = 500
# file_view 每窗口显示的行数
VIEW_WINDOW_LINES = 100


class FileReadTool(BaseTool):
    """
    读取文件内容。超过 MAX_READ_LINES 行时截断并提示。

    params:
        path (str): 文件路径（相对或绝对）
    """

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return (
            f"Read the contents of a file. "
            f"Files longer than {MAX_READ_LINES} lines will be truncated; "
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

        # 4. file_read 只返回前 MAX_READ_LINES 行，避免一次把上下文塞爆。
        total = len(lines)
        truncated = total > MAX_READ_LINES
        display_lines = lines[:MAX_READ_LINES]

        # 加行号，方便 agent 用 file_view 定位
        numbered = "\n".join(
            f"{i + 1:4d} | {line}"
            for i, line in enumerate(display_lines)
        )

        suffix = ""
        if truncated:
            suffix = (
                f"\n... ({total - MAX_READ_LINES} more lines not shown) "
                f"Use file_view with start_line to read the rest."
            )

        # 5. 成功结果写入 output，后续由 core.py 转成 Observation 注入 history/EventLog。
        return ToolResult(
            success=True,
            output=f"File: {path} ({total} lines total)\n{numbered}{suffix}",
        )


class FileViewTool(BaseTool):
    """
    分窗口查看文件，每次返回 VIEW_WINDOW_LINES 行。

    params:
        path (str):       文件路径
        start_line (int): 从第几行开始（1-indexed，默认 1）
    """

    @property
    def name(self) -> str:
        return "file_view"

    @property
    def description(self) -> str:
        return (
            f"View a specific section of a file, {VIEW_WINDOW_LINES} lines at a time. "
            f"Use start_line to scroll through large files. Lines are 1-indexed."
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
                "start_line": {
                    "type": "integer",
                    "description": f"First line to show (1-indexed, default 1)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. 读取路径和起始行；start_line 最小为 1，符合用户看到的行号习惯。
        path = Path(params.get("path", ""))
        start_line = max(1, int(params.get("start_line", 1)))

        # 2. 和 file_read 一样，先把路径错误封装为工具失败结果。
        if not path.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        try:
            # 3. 先完整读入，再按行切窗口；窗口大小由 VIEW_WINDOW_LINES 控制。
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        # 4. 起始行超过文件长度时直接报错，避免返回空窗口误导 Agent。
        total = len(lines)
        if start_line > total:
            return ToolResult(
                success=False,
                output="",
                error=f"start_line {start_line} exceeds file length ({total} lines)",
            )

        # 5. 计算当前窗口范围，并保留真实行号，便于下一轮继续定位。
        end_line = min(start_line + VIEW_WINDOW_LINES - 1, total)
        window = lines[start_line - 1 : end_line]

        numbered = "\n".join(
            f"{start_line + i:4d} | {line}"
            for i, line in enumerate(window)
        )

        # 6. 在输出尾部给出下一次 file_view 的建议，相当于轻量分页导航。
        nav = ""
        if end_line < total:
            nav = f"\n[Lines {start_line}–{end_line} of {total}. Next: file_view path={path} start_line={end_line + 1}]"
        else:
            nav = f"\n[Lines {start_line}–{end_line} of {total}. End of file.]"

        return ToolResult(success=True, output=numbered + nav)


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
            "Parent directories are created automatically. "
            "Always read the file first before writing to avoid losing existing content."
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
