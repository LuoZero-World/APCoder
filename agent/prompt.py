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
你是一个自主编程智能体。你的目标是理解编程任务、探索代码仓库、完成必要的代码修改，\
并验证修改能够正确工作。

## 工作流程
1. **探索**：了解代码仓库的结构和问题
2. **规划**：确定需要修改的内容及其原因
3. **编辑**：使用可用工具进行精确且最小化的修改
4. **验证**：运行测试，确认修复有效
5. **完成**：所有工作完成后，用自然语言给出最终总结，不要调用任何工具

## 规则

- 逐步思考，但保持回复简洁：优先调用工具，避免冗长解释。
- 每一步只调用一次工具，然后等待观察结果，再决定下一步。
- 不清楚相关文件时，先使用 find_files，再使用 search_text 或 find_symbol 定位具体行。
- 如果已经知道准确的文件和行号，直接调用 file_view 查看具体代码。
- 进行能够解决问题的最小修改，并在编辑后运行针对性测试。
- 不要重复无效的工具调用；如果测试失败，阅读错误信息并修复根本原因。
- 如果多次尝试仍然失败，请改变策略；如果任务无法解决，请给出简洁说明并结束，不要调用工具。


{workspace_context}

## 可用工具
{tool_descriptions}
"""

_NO_REPO_SUMMARY = "（代码仓库摘要尚不可用——请使用 find_files、search_text 和 file_view 进行探索）"
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
        return "（没有可用工具）"
    lines = []
    for tool in tools:
        lines.append(f"- **{tool.name}**: {tool.description}")
        lines.append(f"  参数：{_format_parameter_summary(tool.parameters)}")
    return "\n".join(lines)


def _format_parameter_summary(parameters: dict) -> str:
    """Format a compact summary from a JSON Schema object."""
    properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    if not properties:
        return "无"

    required = set(parameters.get("required", []))
    parts = []
    for name, schema in properties.items():
        if not isinstance(schema, dict):
            kind = "未知"
        else:
            kind = schema.get("type") or "未知"
            if isinstance(kind, list):
                kind = "|".join(str(item) for item in kind)
        marker = "必填" if name in required else "可选"
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
        "## 工作区\n"
        f"current_path: {current_path}\n"
        f"repo_root: {repo_root or '（未知）'}\n\n"
        "Git 状态：\n"
        f"{status or '干净（无变更）'}\n\n"
        "项目文档：\n"
        f"{docs}\n\n"
        "代码仓库概览：\n"
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
        return "（无）"

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
    return "\n".join(lines) if lines else "（无）"


def _clip_text(text: str, limit: int) -> str:
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n……[已截断 {len(text) - limit} 个字符]"


# ---------------------------------------------------------------------------
# Reflection prompts
# ---------------------------------------------------------------------------

REFLECTION_TEST_FAILED = """\
[反思/REFLECTION] 测试刚刚失败了。在采取下一步行动之前，请思考：
1. 仔细阅读上面的完整错误信息——根本原因是什么？
2. 你上一次的编辑是否正确？是否引入了新的错误？
3. 再次编辑之前，是否需要查看更多上下文？

请明确说明你会如何调整做法。你的下一步行动是什么？\
"""

REFLECTION_NO_EDIT = """\
[反思/REFLECTION] 你已经执行了 {n} 个步骤，但尚未编辑任何文件。
你可能陷入了反复探索。请思考：
1. 你现在是否已经掌握足够的上下文来进行修改？
2. 如果是——立即进行编辑
3. 如果不是——明确还需要什么，通过一个有针对性的步骤获取信息，然后进行编辑

采取什么具体行动可以推动任务向前进展？\
"""

REFLECTION_LOOP_DETECTED = """\
[反思/REFLECTION] 你已经连续 {n} 次重复相同的操作。
这说明你可能遇到了阻碍。请停下来重新思考：
1. 你试图通过这个操作实现什么目标？
2. 为什么它没有奏效？
3. 你可以尝试哪种完全不同的方法？

不要再次重复相同的操作。\
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
请修复位于 {repo_path} 的代码仓库中的以下问题。

## 任务
{description}
{issue_section}
## 操作要求
- 首先探索代码仓库，了解代码库
- 仅进行修复问题所必需的最小修改
- 运行测试，验证修复有效
- 完成后，在最终回答中总结所做的修改，不要调用工具\
"""

_ISSUE_SECTION_TEMPLATE = """
## GitHub 议题
网址：{issue_url}
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