"""
tools/git_tool.py

Git 操作工具，四个 action：
- git_status:  查看工作区状态（等同 git status --short）
- git_diff:    查看变更内容（等同 git diff 或 git diff HEAD）
- git_add:     暂存文件（等同 git add）
- git_commit:  提交（等同 git commit -m）

设计决策：
- 不封装 git push / PR 创建，这些由 entry/github_issue.py 负责
- git_diff 做输出截断，大型重构的 diff 可能很长
- 所有操作都通过 subprocess 调 git CLI，不用 gitpython
  （减少依赖，git CLI 输出 agent 更容易理解）
"""

from __future__ import annotations

import subprocess
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.runtime import LocalRuntime, Runtime


MAX_DIFF_CHARS = 8_000


def _run_git(
    args: list[str],
    cwd: str | None = None,
    runtime: "Runtime | None" = None,
) -> tuple[bool, str]:
    """
    运行 git 命令，返回 (success, output)。
    runtime 为 None 时直接用 subprocess（向后兼容）。
    """
    from tools.runtime import LocalRuntime
    rt = runtime or LocalRuntime()
    cmd = "git " + " ".join(
        f'"{a}"' if " " in a else a for a in args
    )
    result = rt.exec(cmd, cwd=cwd, timeout=30)
    output = result.output.strip()
    return result.success, output


class GitStatusTool(BaseTool):
    """
    (see class docstring below)
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        from tools.runtime import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    """
    查看工作区状态。

    params:
        cwd (str): repo 根目录（默认当前目录）
    """

    @property
    def name(self) -> str:
        return "git_status"

    @property
    def description(self) -> str:
        return (
            "Show the working tree status (modified, untracked, staged files). "
            "Run this before committing to see what has changed."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. cwd 可指定 repo 根目录；不传则使用当前进程工作目录。
        cwd = params.get("cwd")

        # 2. 使用 --short --branch，输出紧凑，适合塞回 Agent 上下文。
        success, output = _run_git(["status", "--short", "--branch"], cwd=cwd, runtime=self._runtime)
        if not output:
            # 3. git status 可能没有输出，转换成明确的人类可读状态。
            output = "Nothing to commit, working tree clean"
        return ToolResult(success=success, output=output, error=None if success else output)


class GitDiffTool(BaseTool):
    """
    (see class docstring below)
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        from tools.runtime import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    """
    查看变更 diff。

    params:
        staged (bool): True 则查看已暂存的 diff（git diff --cached），默认 False
        path (str):    只查看特定文件的 diff
        cwd (str):     repo 根目录
    """

    @property
    def name(self) -> str:
        return "git_diff"

    @property
    def description(self) -> str:
        return (
            "Show changes in the working tree or staging area. "
            "Use staged=true to see what will be committed. "
            "Use path to diff a specific file."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "staged": {
                    "type": "boolean",
                    "description": "Show staged changes (git diff --cached). Default false.",
                },
                "path": {
                    "type": "string",
                    "description": "Specific file to diff (optional)",
                },
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. staged/path/cwd 分别控制 diff 类型、目标文件和工作目录。
        cwd = params.get("cwd")
        staged = params.get("staged", False)
        path = params.get("path")

        # 2. 逐步组装 git diff 参数，避免在字符串里手写复杂命令。
        args = ["diff"]
        if staged:
            args.append("--cached")
        if path:
            args += ["--", path]

        success, output = _run_git(args, cwd=cwd, runtime=self._runtime)

        if not output:
            # 3. 没有 diff 不是错误，而是一个有效观察结果。
            label = "staged" if staged else "unstaged"
            return ToolResult(success=True, output=f"No {label} changes.")

        # 截断超长 diff
        if len(output) > MAX_DIFF_CHARS:
            kept = MAX_DIFF_CHARS
            omitted = len(output) - kept
            output = output[:kept] + f"\n... [{omitted} chars truncated]"

        # 4. git 命令失败时，把 git 输出同时放进 output/error，方便 LLM 看到原因。
        return ToolResult(success=success, output=output, error=None if success else output)


class GitAddTool(BaseTool):
    """
    (see class docstring below)
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        from tools.runtime import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    """
    暂存文件。

    params:
        paths (list[str]): 要暂存的文件路径列表，默认 ["."]（暂存所有）
        cwd (str):         repo 根目录
    """

    @property
    def name(self) -> str:
        return "git_add"

    @property
    def description(self) -> str:
        return (
            "Stage files for commit. "
            "Pass a list of paths, or omit to stage all changes (git add .)."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to stage. Default: ['.'] (all changes)",
                },
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. 默认暂存所有变更；也允许 LLM 传入具体文件列表。
        cwd = params.get("cwd")
        paths: list[str] = params.get("paths", ["."])
        if not paths:
            # 2. 空列表按默认值处理，避免生成 git add 后没有路径的命令。
            paths = ["."]

        # 3. 委托 git CLI 执行暂存动作。
        success, output = _run_git(["add"] + paths, cwd=cwd, runtime=self._runtime)
        if success:
            # 4. git add 成功通常没有输出，这里补一个确认消息。
            return ToolResult(success=True, output=f"Staged: {', '.join(paths)}")
        return ToolResult(success=False, output=output, error=output)


class GitCommitTool(BaseTool):
    """
    (see class docstring below)
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        from tools.runtime import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    """
    提交暂存的变更。

    params:
        message (str): commit message（必填）
        cwd (str):     repo 根目录
    """

    @property
    def name(self) -> str:
        return "git_commit"

    @property
    def description(self) -> str:
        return (
            "Commit staged changes with a message. "
            "Always run git_add before git_commit. "
            "Write a clear, descriptive commit message."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Commit message (be descriptive)",
                },
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": ["message"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. commit 必须有 message；cwd 用于指定仓库目录。
        cwd = params.get("cwd")
        message = params.get("message", "").strip()

        # 2. 没有提交信息时不调用 git，直接返回参数错误。
        if not message:
            return ToolResult(
                success=False, output="", error="commit message is required"
            )

        # 3. 只封装 git commit -m，不负责 git add / git push。
        success, output = _run_git(["commit", "-m", message], cwd=cwd, runtime=self._runtime)
        return ToolResult(
            success=success,
            output=output,
            error=None if success else output,
        )
