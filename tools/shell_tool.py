"""
tools/shell_tool.py

Shell 命令执行工具。

注意：本工具不做权限判断。是否允许执行 shell 命令由 agent.permission
和 agent loop 统一决定；ShellTool 只负责把已经获准的命令交给 runtime 执行。
"""

from __future__ import annotations

from typing import Any

from tools.base import BaseTool, ToolResult
from tools.runtime import LocalRuntime, Runtime


class ShellTool(BaseTool):
    """
    执行 shell 命令并返回 stdout + stderr。

    params:
        cmd (str):     shell 命令字符串
        timeout (int): 超时秒数，默认 30
        cwd (str):     工作目录，可选
    """

    def __init__(
        self,
        runtime: Runtime | None = None,
        max_output_chars: int = 2_000,
    ) -> None:
        self._runtime = runtime or LocalRuntime()
        self._max_output_chars = int(max_output_chars)

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return its output (stdout + stderr combined). "
            "Timeout is 30s by default. Permission is checked by the agent before this "
            "tool runs. Prefer targeted commands like 'rg', 'pytest tests/foo.py', "
            "and 'git diff'."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (optional)",
                },
            },
            "required": ["cmd"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        """执行命令；权限检查必须已经在 agent loop 中完成。"""
        cmd = str(params.get("cmd", "")).strip()
        timeout = int(params.get("timeout", 30))
        cwd = params.get("cwd", None)

        if not cmd:
            return ToolResult(success=False, output="", error="cmd is required")

        result = self._runtime.exec(cmd, cwd=cwd, timeout=timeout)
        output = _truncate(result.output, self._max_output_chars)

        if result.success:
            return ToolResult(success=True, output=output)

        if "timed out" in result.stderr.lower():
            error = result.stderr.strip()
        else:
            error = f"Exit code: {result.returncode}"
        return ToolResult(success=False, output=output, error=error)


def _truncate(text: str, max_chars: int) -> str:
    """输出过长时截断，保留头部 60% 和尾部 40%。"""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        text[:head]
        + f"\n... [{omitted} characters truncated] ...\n"
        + text[-tail:]
    )
