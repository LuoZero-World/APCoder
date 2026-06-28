"""
tools/search_tool.py

代码搜索工具，三个 action：
- search_text:   在文件内容中搜索字符串（grep 风格）
- find_files:    按文件名 pattern 查找文件
- find_symbol:   在 Python 文件中查找函数/类定义（不依赖 tree-sitter，用正则）

设计说明：
- search_text/find_symbol 使用 Python 原生实现；find_files 依赖 Ripgrep
- find_symbol 用正则匹配 def/class，Day 5 接入 tree-sitter 后可替换
- 结果数量上限防止返回太多内容爆上下文
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from tools.base import BaseTool, ToolResult


MAX_RESULTS = 50        # 单次搜索最多返回的结果数

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
    """递归搜索文件内容，并返回适合 Agent 继续定位的结构化结果。"""

    @property
    def name(self) -> str:
        return "search_text"

    @property
    def description(self) -> str:
        return (
            "Search file contents with a regular expression. Returns structured "
            "matches containing path, line, column, match span, and nearby context."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Non-empty Python regular expression",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search recursively (default: current directory)",
                },
                "include_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File globs to include (default: ['*'])",
                },
                "exclude_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File globs to exclude",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Legacy single include glob; merged with include_patterns",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case-sensitive search (default true)",
                },
                "whole_word": {
                    "type": "boolean",
                    "description": "Require word boundaries around the entire pattern (default false)",
                },
                "context_before": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "description": "Context lines before each match (default 2)",
                },
                "context_after": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "description": "Context lines after each match (default 2)",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum matches to return (default 50)",
                },
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # 1. 校验正则、路径和布尔开关，避免隐式类型转换造成搜索偏差。
        raw_pattern = params.get("pattern")
        if not isinstance(raw_pattern, str) or not raw_pattern:
            return _search_error("pattern must be a non-empty string")

        try:
            search_path = Path(params.get("path", "."))
        except TypeError:
            return _search_error("path must be a string")
        if not search_path.exists():
            return _search_error(f"Path not found: {search_path}")

        case_sensitive, error = _validate_bool_param(
            params, "case_sensitive", True
        )
        if error:
            return _search_error(error)
        whole_word, error = _validate_bool_param(params, "whole_word", False)
        if error:
            return _search_error(error)

        # 2. 校验上下文窗口和结果上限，给 Agent 输出设置明确边界。
        context_before, error = _validate_int_param(
            params, "context_before", default=2, minimum=0, maximum=20
        )
        if error:
            return _search_error(error)
        context_after, error = _validate_int_param(
            params, "context_after", default=2, minimum=0, maximum=20
        )
        if error:
            return _search_error(error)
        max_results, error = _validate_int_param(
            params, "max_results", default=MAX_RESULTS, minimum=1, maximum=200
        )
        if error:
            return _search_error(error)

        # 3. 合并新版 include_patterns 与旧 file_pattern，并校验 exclude。
        includes, error = _collect_search_include_patterns(params)
        if error:
            return _search_error(error)
        excludes, error = _validate_pattern_list(
            params.get("exclude_patterns"), "exclude_patterns"
        )
        if error:
            return _search_error(error)

        # 4. whole_word 包裹整个表达式；非法正则在进入文件遍历前失败。
        expression = rf"\b(?:{raw_pattern})\b" if whole_word else raw_pattern
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(expression, flags)
        except re.error as exc:
            return _search_error(f"Invalid regex: {exc}")

        # 5. 每个文件只读取一次；同一行的多个 occurrence 分别生成结果。
        matches: list[dict[str, Any]] = []
        for filepath in _iter_search_files(search_path, includes, excludes):
            try:
                lines = filepath.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue

            for line_index, line in enumerate(lines):
                for match in regex.finditer(line):
                    # 6. 多探测一个命中，用 truncated 明确告知 Agent 结果未完整返回。
                    if len(matches) >= max_results:
                        return ToolResult(
                            success=True,
                            output={"matches": matches, "truncated": True},
                        )

                    context_start = max(0, line_index - context_before)
                    context_end = min(
                        len(lines), line_index + context_after + 1
                    )
                    matches.append({
                        "path": str(filepath),
                        "line": line_index + 1,
                        "column": match.start() + 1,
                        "match_span": {
                            "start": match.start(),
                            "end": match.end(),
                        },
                        "context": {
                            "start_line": context_start + 1,
                            "lines": lines[context_start:context_end],
                        },
                    })

        # 7. 无匹配也返回稳定结构，不再生成自然语言提示。
        return ToolResult(
            success=True,
            output={"matches": matches, "truncated": False},
        )

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

def _empty_search_output() -> dict[str, Any]:
    """为成功空结果和失败结果创建互不共享的标准结构。"""
    return {"matches": [], "truncated": False}


def _search_error(message: str) -> ToolResult:
    """统一构造 search_text 的结构化失败结果。"""
    return ToolResult(
        success=False,
        output=_empty_search_output(),
        error=message,
    )


def _validate_bool_param(
    params: dict[str, Any],
    name: str,
    default: bool,
) -> tuple[bool, str | None]:
    """读取布尔参数，拒绝字符串等隐式真值。"""
    value = params.get(name, default)
    if not isinstance(value, bool):
        return default, f"{name} must be a boolean"
    return value, None


def _validate_int_param(
    params: dict[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> tuple[int, str | None]:
    """读取有上下界的整数参数；bool 不作为整数接受。"""
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default, f"{name} must be an integer"
    if value < minimum or value > maximum:
        return default, f"{name} must be between {minimum} and {maximum}"
    return value, None


def _collect_search_include_patterns(
    params: dict[str, Any],
) -> tuple[list[str], str | None]:
    """合并 search_text 新版 include 数组与旧 file_pattern。"""
    includes, error = _validate_pattern_list(
        params.get("include_patterns"), "include_patterns"
    )
    if error:
        return [], error

    legacy_pattern = params.get("file_pattern")
    if legacy_pattern is not None:
        if not isinstance(legacy_pattern, str) or not legacy_pattern.strip():
            return [], "file_pattern must be a non-empty string"
        includes.insert(0, legacy_pattern.strip())

    deduplicated = list(dict.fromkeys(includes))
    return deduplicated or ["*"], None


def _is_search_skip_dir(name: str) -> bool:
    """判断搜索时应剪枝的依赖、缓存或构建目录。"""
    return name in _SKIP_DIRS or name.endswith(".egg-info")


def _iter_search_files(
    root: Path,
    include_patterns: list[str],
    exclude_patterns: list[str],
):
    """流式遍历 search_text 候选文件，并应用 include/exclude glob。"""
    def should_include(filepath: Path) -> bool:
        if not _matches_include_glob(str(filepath), include_patterns):
            return False
        if exclude_patterns and _matches_include_glob(
            str(filepath), exclude_patterns
        ):
            return False
        return True

    if root.is_file():
        if should_include(root):
            yield root
        return

    for dirpath, dirnames, filenames in os.walk(root):
        # 原地剪枝，避免继续进入依赖与缓存目录。
        dirnames[:] = sorted(
            dirname for dirname in dirnames
            if not _is_search_skip_dir(dirname)
        )
        for filename in sorted(filenames):
            filepath = Path(dirpath) / filename
            if should_include(filepath):
                yield filepath


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
