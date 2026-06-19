"""
agent/prompt.py

System prompt 模板管理。

职责：
- 维护 agent 的 system prompt 模板
- 根据运行时信息（工具列表、repo 概况）渲染最终 prompt
- 提供 Reflection prompt 模板

设计原则：
- prompt 集中在这里，修改 prompt 不需要改 core.py
- 模板用 str.format() 而不是 jinja2，减少依赖
- 每个 prompt 都有对应的函数，便于测试和调整
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from llm.base import LLMToolSchema


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You are an autonomous coding agent. Your goal is to understand a coding task, \
explore the repository, make the necessary code changes, and verify they work correctly.

## Workflow
1. **Explore**: Understand the repository structure and the problem
2. **Plan**: Identify what needs to change and why
3. **Edit**: Make precise, minimal changes using the available tools
4. **Verify**: Run tests to confirm the fix works
5. **Finish**: When everything is done, return a final natural-language summary without any tool call

## Rules

- Think step by step, but keep responses brief: prefer tool calls over long explanations.
- Use exactly one tool call per step, then wait for the observation before deciding next.
- Use OpenAI function-calling for tools; do not write textual `Action:` / `Params:` blocks unless function calling is unavailable.
- Make the smallest useful change, then run focused tests after editing.
- Do not repeat unhelpful tool calls; if tests fail, read the error and fix the root cause.
- If repeated attempts fail, change strategy; if the task cannot be solved, finish with a concise explanation and no tool call.


{workspace_context}

## Available tools
{tool_descriptions}
"""

_NO_REPO_SUMMARY = "(Repository summary not yet available — use find_files and file_read to explore)"
_DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
_PROJECT_DOC_SNIPPET_CHARS = 1200


def build_system_prompt(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
) -> str:
    """
    渲染完整的 system prompt。

    Args:
        repo_path:    repo 根目录路径
        tools:        已注册工具的 schema 列表
        repo_summary: repo-map 生成的摘要

    Returns:
        渲染好的 system prompt 字符串
    """
    tool_descriptions = _format_tool_descriptions(tools)
    workspace_context = _build_workspace_context(repo_path, repo_summary)

    return _SYSTEM_TEMPLATE.format(
        workspace_context=workspace_context,
        tool_descriptions=tool_descriptions,
    )


def _format_tool_descriptions(tools: list[LLMToolSchema]) -> str:
    """把工具列表格式化为易读的描述块。"""
    if not tools:
        return "(no tools available)"
    lines = []
    for tool in tools:
        lines.append(f"- **{tool.name}**: {tool.description}")
        lines.append(f"  Parameters: {_format_parameter_summary(tool.parameters)}")
    return "\n".join(lines)


def _format_parameter_summary(parameters: dict) -> str:
    """Format a compact summary from a JSON Schema object."""
    properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    if not properties:
        return "none"

    required = set(parameters.get("required", []))
    parts = []
    for name, schema in properties.items():
        if not isinstance(schema, dict):
            kind = "unknown"
        else:
            kind = schema.get("type") or "unknown"
            if isinstance(kind, list):
                kind = "|".join(str(item) for item in kind)
        marker = "required" if name in required else "optional"
        parts.append(f"{name}: {kind} ({marker})")
    return ", ".join(parts)


def _build_workspace_context(repo_path: str, repo_summary: str | None) -> str:
    """Build the workspace block injected into the system prompt."""
    current_path = str(repo_path)
    repo_root = _git_output(repo_path, ["rev-parse", "--show-toplevel"])
    status = _git_output(repo_path, ["status", "--short"])
    docs_root = Path(repo_root) if repo_root and Path(repo_root).exists() else Path(repo_path)
    docs = _format_project_docs(docs_root)
    summary = repo_summary or _NO_REPO_SUMMARY

    return (
        "## Workspace\n"
        f"current_path: {current_path}\n"
        f"repo_root: {repo_root or '(unknown)'}\n\n"
        "git status:\n"
        f"{status or 'clean'}\n\n"
        "Project docs:\n"
        f"{docs}\n\n"
        "Repo map:\n"
        f"{summary}"
    )


def _git_output(repo_path: str, args: list[str]) -> str:
    """Run a git command in repo_path and return stdout, or an empty fallback."""
    cwd = Path(repo_path)
    if cwd.is_file():
        cwd = cwd.parent
    if not cwd.exists():
        return ""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def _format_project_docs(root: Path) -> str:
    """Read a short snippet from well-known project docs."""
    if not root.exists():
        return "(none)"

    lines: list[str] = []
    for name in _DOC_NAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines.append(f"- {name}:\n{_clip_text(text, _PROJECT_DOC_SNIPPET_CHARS)}")
    return "\n".join(lines) if lines else "(none)"


def _clip_text(text: str, limit: int) -> str:
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


# ---------------------------------------------------------------------------
# Reflection prompts
# ---------------------------------------------------------------------------

REFLECTION_TEST_FAILED = """\
[REFLECTION] The tests just failed. Before your next action, consider:
1. Read the full error message above carefully — what is the root cause?
2. Is your last edit correct? Did it introduce a new bug?
3. Do you need to look at more context before editing again?

Be specific about what you will do differently. What is your next action?\
"""

REFLECTION_NO_EDIT = """\
[REFLECTION] You have taken {n} steps without editing any file.
You may be stuck in an exploration loop. Consider:
1. Do you have enough context to make a change now?
2. If yes — make the edit
3. If no — identify exactly what you still need, get it in one targeted step, then edit

What specific action will move the task forward?\
"""

REFLECTION_LOOP_DETECTED = """\
[REFLECTION] You have repeated the same action {n} times in a row.
This suggests you are stuck. Stop and reconsider:
1. What are you trying to achieve with this action?
2. Why isn't it working?
3. What completely different approach could you try?

Do not repeat the same action again.\
"""


def reflection_test_failed() -> str:
    return REFLECTION_TEST_FAILED


def reflection_no_edit(n: int) -> str:
    return REFLECTION_NO_EDIT.format(n=n)


def reflection_loop_detected(n: int) -> str:
    return REFLECTION_LOOP_DETECTED.format(n=n)


# ---------------------------------------------------------------------------
# Task prompt（用户消息，描述任务）
# ---------------------------------------------------------------------------

_TASK_TEMPLATE = """\
Please fix the following issue in the repository at {repo_path}.

## Task
{description}
{issue_section}
## Instructions
- Start by exploring the repository to understand the codebase
- Make the minimal changes necessary to fix the issue
- Run the tests to verify your fix works
- When complete, return a final answer with a summary of your changes and no tool call\
"""

_ISSUE_SECTION_TEMPLATE = """
## GitHub Issue
URL: {issue_url}
"""


def build_task_prompt(
    description: str,
    repo_path: str,
    issue_url: str | None = None,
) -> str:
    """
    构建任务描述的用户消息（对话的第一条 user 消息）。

    Args:
        description: 任务描述（自然语言）
        repo_path:   repo 根目录
        issue_url:   GitHub issue URL（可选）
    """
    issue_section = ""
    if issue_url:
        issue_section = _ISSUE_SECTION_TEMPLATE.format(issue_url=issue_url)

    return _TASK_TEMPLATE.format(
        repo_path=repo_path,
        description=description.strip(),
        issue_section=issue_section,
    )
