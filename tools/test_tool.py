"""
tools/test_tool.py

pytest 执行工具，返回结构化的测试结果。

关键设计：
- 成功时压缩为统计行，失败时保留 pytest 的完整 short traceback
- 超长失败输出保留头尾并限制长度，兼顾诊断信息和上下文预算
- 通过 exit code 判断成功/失败，不依赖字符串匹配
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.runtime import LocalRuntime, Runtime


PYTEST_TIMEOUT = 20        # pytest 默认超时，比 shell 工具更长
MAX_OUTPUT_CHARS = 6_000    # 测试输出比普通 shell 输出更容易很长
TRUNCATION_MARKER = "\n...[pytest output truncated]...\n"


class PytestTool(BaseTool):
    """
    运行 pytest 并返回结构化结果。

    params:
        path (str):  测试文件或目录（默认 "tests/"，不存在则用 "."）
        args (str):  额外的 pytest 参数（如 "-x -v --tb=short"）
        cwd (str):   工作目录（默认当前目录）
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime or LocalRuntime()

    @property
    def name(self) -> str:
        return "test"

    @property
    def description(self) -> str:
        return (
            "Run pytest and return compact pass output or complete short failure tracebacks. "
            "Shows which tests failed and their error messages. "
            "Use path to run specific test files or directories."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Test file or directory to run (default: 'tests/' or '.')",
                },
                "args": {
                    "type": "string",
                    "description": "Extra pytest arguments (e.g. '-x -v --tb=short')",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory",
                },
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. cwd 影响默认测试路径判断，也会传给 runtime 作为命令工作目录。
        cwd = params.get("cwd", None)
        cwd_path = Path(cwd) if cwd else Path.cwd()

        # 决定测试路径
        test_path = params.get("path", "")
        if not test_path:
            if (cwd_path / "tests").exists():
                test_path = "tests/"
            else:
                test_path = "."

        # 2. 按 shell 参数规则解析额外选项，保留 -k 等表达式中的空格。
        extra_args = params.get("args", "")
        try:
            extra_arg_parts = shlex.split(extra_args) if extra_args else []
        except ValueError as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"invalid pytest args: {exc}",
            )

        # 组装命令：--tb=short 足够 agent 理解，--no-header 减少噪音
        cmd_parts = [
            "python", "-m", "pytest",
            test_path,
            "--tb=short",
            "--no-header",
            "-q",               # 安静模式：只输出失败详情和最终统计
        ]
        cmd_parts.extend(extra_arg_parts)

        # 3. 通过 runtime 执行 pytest；测试工具默认超时比通用 shell 更长。
        cmd_str = (
            subprocess.list2cmdline(cmd_parts)
            if os.name == "nt"
            else shlex.join(cmd_parts)
        )
        run_result = self._runtime.exec(cmd_str, cwd=cwd, timeout=PYTEST_TIMEOUT)

        # 4. 超时单独作为失败类型返回，方便 Agent 识别不是普通断言失败。
        if "timed out" in run_result.stderr.lower():
            return ToolResult(
                success=False,
                output="",
                error=f"pytest timed out after {PYTEST_TIMEOUT}s",
            )

        # 5. 成败只看 exit code；stdout/stderr 文本只用于生成摘要。
        raw = run_result.output
        success = run_result.returncode == 0

        # 解析并格式化输出
        output = _format_pytest_output(raw, success)

        # 6. 失败时 error 放退出码，详细失败原因放 output，供 history/reflection 使用。
        return ToolResult(
            success=success,
            output=output,
            error=None if success else f"pytest exited with code {run_result.returncode}",
        )


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------

def _format_pytest_output(raw: str, success: bool) -> str:
    """Format pytest output for an agent without hiding failure diagnostics."""
    if success:
        lines = raw.strip().splitlines()
        summary_lines = [line for line in lines if re.search(r"passed|no tests", line)]
        if summary_lines:
            return summary_lines[-1]
        return raw.strip()

    # --tb=short already controls traceback detail. Preserve assertion expressions,
    # exception types, and custom messages instead of parsing them away.
    return _truncate_failed_output(raw.strip())


def _truncate_failed_output(raw: str) -> str:
    """Cap failed pytest output while preserving its beginning and summary tail."""
    if len(raw) <= MAX_OUTPUT_CHARS:
        return raw

    available = MAX_OUTPUT_CHARS - len(TRUNCATION_MARKER)
    if available <= 0:
        return TRUNCATION_MARKER[:MAX_OUTPUT_CHARS]

    head_chars = available // 2
    tail_chars = available - head_chars
    return raw[:head_chars] + TRUNCATION_MARKER + raw[-tail_chars:]
