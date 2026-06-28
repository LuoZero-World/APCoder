"""
tools/search_tool.py

代码搜索工具，三个 action：
- search_text:   在文件内容中搜索字符串（grep 风格）
- find_files:    按文件名 pattern 查找文件
- find_symbol:   在 Python 文件中查找函数/类定义（不依赖 tree-sitter，用正则）

设计说明：
- 不依赖外部工具（grep 不一定存在），用 Python 原生实现
- find_symbol 用正则匹配 def/class，Day 5 接入 tree-sitter 后可替换
- 结果数量上限防止返回太多内容爆上下文
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from tools.base import BaseTool, ToolResult


MAX_RESULTS = 50        # 单次搜索最多返回的结果数
MAX_LINE_LENGTH = 200   # 单行超长时截断显示

# 搜索时跳过的目录
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", "*.egg-info",
})

# find_files 即使开启 include_ignored，也始终跳过这些高噪声目录。
_RG_HARD_EXCLUDE_GLOBS: tuple[str, ...] = (
    "!**/.git/**",
    "!**/__pycache__/**",
    "!**/.venv/**",
    "!**/venv/**",
    "!**/node_modules/**",
    "!**/.mypy_cache/**",
    "!**/.pytest_cache/**",
    "!**/dist/**",
    "!**/build/**",
    "!**/*.egg-info/**",
)


class SearchTextTool(BaseTool):
    """
    在 repo 文件中搜索文本，返回匹配行及其上下文。

    params:
        pattern (str):    搜索字符串（支持正则）
        path (str):       搜索范围（文件或目录，默认当前目录）
        file_pattern (str): 只搜索匹配的文件名（如 "*.py"，默认所有文件）
        case_sensitive (bool): 是否区分大小写（默认 True）
    """

    @property
    def name(self) -> str:
        return "search_text"

    @property
    def description(self) -> str:
        return (
            "Search for a text pattern (regex supported) in files. "
            "Returns matching lines with file path and line number. "
            f"Returns at most {MAX_RESULTS} matches."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: current directory)",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py'). Default: all files",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case-sensitive search (default true)",
                },
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. 读取搜索条件：pattern 支持正则，path/file_pattern 控制搜索范围。
        raw_pattern = params.get("pattern", "")
        search_path = Path(params.get("path", "."))
        file_pattern = params.get("file_pattern", "*")
        case_sensitive = params.get("case_sensitive", True)

        # 2. 先编译正则；正则非法时直接返回工具错误，不进入文件遍历。
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(raw_pattern, flags)
        except re.error as e:
            return ToolResult(success=False, output="", error=f"Invalid regex: {e}")

        # 3. 搜索根路径不存在时返回失败，避免静默给出空结果。
        if not search_path.exists():
            return ToolResult(
                success=False, output="", error=f"Path not found: {search_path}"
            )

        # 4. _iter_files 负责递归遍历和跳过无关目录，这里只消费候选文件。
        matches: list[str] = []
        files = _iter_files(search_path, file_pattern)

        for filepath in files:
            # 5. 达到结果上限立即停止，防止观察结果过长挤占上下文。
            if len(matches) >= MAX_RESULTS:
                break
            try:
                for lineno, line in enumerate(
                    filepath.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if regex.search(line):
                        # 6. 单行也做长度截断；返回格式保持 grep 风格：path:line: text。
                        display_line = line[:MAX_LINE_LENGTH]
                        if len(line) > MAX_LINE_LENGTH:
                            display_line += " ..."
                        matches.append(f"{filepath}:{lineno}: {display_line}")
                        if len(matches) >= MAX_RESULTS:
                            break
            except OSError:
                # 7. 单个文件读失败不影响整体搜索，跳过即可。
                continue

        if not matches:
            return ToolResult(
                success=True,
                output=f"No matches found for '{raw_pattern}'",
            )

        suffix = f"\n[Showing {len(matches)} matches]"
        if len(matches) == MAX_RESULTS:
            suffix = f"\n[Showing first {MAX_RESULTS} matches, there may be more]"

        return ToolResult(success=True, output="\n".join(matches) + suffix)


class FindFilesTool(BaseTool):
    """
    使用 Ripgrep 按文件名 glob 查找文件。

    params:
        pattern (str):            单个 glob 的兼容写法
        include_patterns (list):  多个包含 glob
        exclude_patterns (list):  多个排除 glob
        include_ignored (bool):   是否包含被 ignore 规则忽略的文件
        path (str):               搜索根目录（默认当前目录）
    """

    @property
    def name(self) -> str:
        return "find_files"

    @property
    def description(self) -> str:
        return (
            "Find files with required Ripgrep (rg) using glob patterns. "
            "Use pattern for one include or include_patterns for multiple includes; "
            "exclude_patterns removes matches. Respects ignore files by default. "
            f"Returns at most {MAX_RESULTS} results."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Single include glob; compatible with the original API",
                },
                "include_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Multiple include globs; merged with pattern as a union",
                },
                "exclude_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Globs to exclude from the included files",
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "Include ignored files (default false)",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: current directory)",
                },
            },
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. 读取并校验搜索路径；不存在时沿用原工具的错误语义。
        search_path = Path(params.get("path", "."))
        if not search_path.exists():
            return ToolResult(
                success=False, output="", error=f"Path not found: {search_path}"
            )

        # 2. 合并单 pattern 与 include_patterns，形成包含模式的并集。
        includes, error = _collect_include_patterns(params)
        if error:
            return ToolResult(success=False, output="", error=error)

        # 3. 校验排除模式与 include_ignored，避免把错误类型传给子进程。
        excludes, error = _validate_pattern_list(
            params.get("exclude_patterns"), "exclude_patterns"
        )
        if error:
            return ToolResult(success=False, output="", error=error)

        include_ignored = params.get("include_ignored", False)
        if not isinstance(include_ignored, bool):
            return ToolResult(
                success=False,
                output="",
                error="include_ignored must be a boolean",
            )

        # 4. rg 是 find_files 的必需依赖；缺失时明确失败，不做 Python 回退。
        rg_path = shutil.which("rg")
        if rg_path is None:
            return ToolResult(
                success=False,
                output="",
                error="Ripgrep (rg) is required for find_files but was not found in PATH",
            )

        # 5. 构造 rg 文件发现命令；正向 include 留到流式输出阶段过滤。
        command = [rg_path, "--files", "--hidden", "--no-require-git"]
        if include_ignored:
            command.append("--no-ignore")
        for exclude_pattern in excludes:
            command.extend(["--glob", _as_exclude_glob(exclude_pattern)])
        for hard_exclude in _RG_HARD_EXCLUDE_GLOBS:
            command.extend(["--glob", hard_exclude])
        command.extend(["--", str(search_path)])

        # 6. 逐行读取 rg 输出，仅多读一条用于判断是否还有更多结果。
        results: list[str] = []
        has_more = False
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Failed to start Ripgrep: {exc}",
            )

        assert process.stdout is not None
        for raw_line in process.stdout:
            filepath = raw_line.rstrip("\r\n")
            if not filepath:
                continue
            # rg 的正向 glob 会覆盖 .gitignore，因此在流式结果上匹配 include。
            if not _matches_include_glob(filepath, includes):
                continue
            if len(results) >= MAX_RESULTS:
                has_more = True
                process.terminate()
                break
            results.append(filepath)

        # 7. 正常完成时检查退出码；主动截断时不把 terminate 视为搜索失败。
        try:
            _, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate()

        if not has_more and process.returncode not in (0, 1):
            detail = stderr.strip() or f"exit code {process.returncode}"
            return ToolResult(
                success=False,
                output="",
                error=f"Ripgrep failed: {detail[:1000]}",
            )

        # 8. 组装紧凑结果；无匹配仍属于成功观察。
        if not results:
            return ToolResult(
                success=True,
                output=f"No files found matching {includes!r} in {search_path}",
            )

        suffix = ""
        if has_more:
            suffix = f"\n[Showing first {MAX_RESULTS} results, there may be more]"

        return ToolResult(
            success=True,
            output="\n".join(results) + suffix,
        )

class FindSymbolTool(BaseTool):
    """
    在 Python 文件中查找函数/类定义。
    用正则匹配 def / class 语句，Day 5 可替换为 tree-sitter 精确实现。

    params:
        symbol (str): 函数名或类名（支持部分匹配）
        path (str):   搜索根目录（默认当前目录）
    """

    @property
    def name(self) -> str:
        return "find_symbol"

    @property
    def description(self) -> str:
        return (
            "Find function or class definitions in Python files. "
            "Searches for 'def symbol' or 'class symbol' patterns. "
            "Supports partial name matching."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Function or class name to find (partial match supported)",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: current directory)",
                },
            },
            "required": ["symbol"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. 读取符号名和搜索根目录；symbol 支持前缀/部分匹配。
        symbol = params.get("symbol", "")
        search_path = Path(params.get("path", "."))

        # 2. symbol 是必填核心参数，缺失时直接返回失败。
        if not symbol:
            return ToolResult(success=False, output="", error="symbol is required")

        # 匹配 def foo / class Foo（含缩进，用于方法）
        pattern = re.compile(
            rf"^(\s*)(def|class)\s+({re.escape(symbol)}\w*)\s*[:(]",
            re.MULTILINE,
        )

        # 3. 只扫描 Python 文件；这是一个轻量正则版 symbol finder。
        matches: list[str] = []
        for filepath in _iter_files(search_path, "*.py"):
            if len(matches) >= MAX_RESULTS:
                break
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
                for m in pattern.finditer(content):
                    # 4. 通过匹配位置前的换行数计算行号。
                    lineno = content[: m.start()].count("\n") + 1
                    kind = m.group(2)   # def / class
                    name = m.group(3)
                    # 5. 有缩进就粗略认为是 method，否则认为是顶层函数/类。
                    indent = len(m.group(1))
                    scope = "method" if indent > 0 else "top-level"
                    matches.append(
                        f"{filepath}:{lineno}: {kind} {name} ({scope})"
                    )
                    if len(matches) >= MAX_RESULTS:
                        break
            except OSError:
                # 6. 个别文件读失败时跳过，保持搜索工具整体可用。
                continue

        if not matches:
            return ToolResult(
                success=True,
                output=f"No definition found for '{symbol}'",
            )

        return ToolResult(success=True, output="\n".join(matches))


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _validate_pattern_list(
    value: Any,
    field_name: str,
) -> tuple[list[str], str | None]:
    """校验 glob 数组，并保持原有顺序去重。"""
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], f"{field_name} must be an array of non-empty strings"

    patterns: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            return [], f"{field_name}[{index}] must be a non-empty string"
        pattern = item.strip()
        if pattern not in seen:
            seen.add(pattern)
            patterns.append(pattern)
    return patterns, None


def _collect_include_patterns(
    params: dict[str, Any],
) -> tuple[list[str], str | None]:
    """合并兼容参数 pattern 和多值 include_patterns。"""
    patterns: list[str] = []
    raw_pattern = params.get("pattern")
    if raw_pattern is not None:
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            return [], "pattern must be a non-empty string"
        patterns.append(raw_pattern.strip())

    extra_patterns, error = _validate_pattern_list(
        params.get("include_patterns"), "include_patterns"
    )
    if error:
        return [], error

    # 兼容参数排在前面；去重时保留首次出现位置，便于测试和诊断。
    deduplicated = list(dict.fromkeys([*patterns, *extra_patterns]))
    if not deduplicated:
        return [], "pattern or include_patterns must contain at least one glob"
    return deduplicated, None


def _matches_include_glob(filepath: str, patterns: list[str]) -> bool:
    """在 rg 已完成 ignore 过滤的路径上匹配任一正向 glob。"""
    normalized_path = filepath.replace("\\", "/")
    candidate = PurePosixPath(normalized_path)
    return any(candidate.match(pattern.replace("\\", "/")) for pattern in patterns)

def _as_exclude_glob(pattern: str) -> str:
    """把用户排除模式规范成 rg 需要的负向 glob。"""
    return pattern if pattern.startswith("!") else f"!{pattern}"

def _iter_files(root: Path, glob_pattern: str):
    """
    递归遍历目录，跳过 _SKIP_DIRS，按 glob_pattern 过滤文件名。
    """
    if root.is_file():
        yield root
        return

    for filepath in sorted(root.rglob(glob_pattern)):
        # 跳过黑名单目录
        if any(part in _SKIP_DIRS for part in filepath.parts):
            continue
        if filepath.is_file():
            yield filepath
