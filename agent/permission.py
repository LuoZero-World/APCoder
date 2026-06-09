"""
agent/permission.py

Agent 层统一权限系统。

权限判断放在 agent loop 中，而不是散落在具体工具里。这样 shell、
file_write、file_edit、git 等工具都只负责“执行”，是否允许执行由这里统一决策。
"""

from __future__ import annotations

import fnmatch
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


PermissionBehavior = Literal["allow", "deny", "ask"]
PermissionMode = Literal["confirm", "human", "yolo"]
PermissionCallback = Callable[[str, dict[str, Any], str], bool]


READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "file_read",
        "file_view",
        "search_text",
        "find_files",
        "find_symbol",
        "git_status",
        "git_diff",
        "test",
        "pytest",
    }
)

WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "file_write",
        "file_edit",
        "edit",
        "git_add",
        "git_commit",
    }
)


DEFAULT_RULES: tuple[dict[str, Any], ...] = (
    {"tool": "file_read", "path": "*", "behavior": "allow"},
    {"tool": "file_view", "path": "*", "behavior": "allow"},
)


# 这些 shell 前缀通常只读取信息，在 confirm/human 模式下可以自动允许。
READONLY_SHELL_PREFIXES: tuple[str, ...] = (
    "ls",
    "ll",
    "la",
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "echo",
    "printf",
    "pwd",
    "whoami",
    "which",
    "type",
    "find",
    "locate",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "wc",
    "sort",
    "uniq",
    "cut",
    "awk",
    "sed -n",
    "diff",
    "diff3",
    "file",
    "stat",
    "python -m pytest",
    "python3 -m pytest",
    "pytest",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git tag",
    "git remote",
    "git stash list",
    "tree",
    "env",
    "printenv",
    "ps",
    "top",
    "htop",
    "df",
    "du",
    "uname",
    "hostname",
    "date",
    "cal",
    "man",
    "help",
)


# severe 会直接 deny；ask 会升级给用户确认。
SHELL_RISK_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("rm -rf /", "severe", "Refuses to remove the filesystem root"),
    ("rm -rf ~", "severe", "Refuses to remove the home directory"),
    ("sudo *", "severe", "Refuses sudo commands"),
    ("mkfs*", "severe", "Refuses filesystem formatting commands"),
    ("dd if=*", "severe", "Refuses raw disk write commands"),
    (":(){:|:&};:", "severe", "Refuses fork bombs"),
    ("chmod -R 777 /", "severe", "Refuses unsafe root permission changes"),
    ("* > /dev/sda*", "severe", "Refuses writes to block devices"),
    ("rm *", "ask", "Removes files"),
    ("rmdir*", "ask", "Removes directories"),
    ("mv *", "ask", "Moves or renames files"),
    ("cp -r *", "ask", "Copies directories"),
    ("cp -f *", "ask", "Overwrites files"),
    ("chmod *", "ask", "Changes file permissions"),
    ("chown *", "ask", "Changes file ownership"),
    ("pip install *", "ask", "Installs Python packages"),
    ("pip uninstall *", "ask", "Uninstalls Python packages"),
    ("npm install *", "ask", "Installs npm packages"),
    ("npm uninstall *", "ask", "Uninstalls npm packages"),
    ("git commit *", "ask", "Creates a git commit"),
    ("git push *", "ask", "Pushes commits to a remote"),
    ("git reset *", "ask", "Resets git state"),
    ("git checkout *", "ask", "Changes git working tree state"),
    ("git merge *", "ask", "Merges branches"),
    ("git rebase *", "ask", "Rewrites git history"),
    ("git clean *", "ask", "Deletes untracked files"),
    ("curl *", "ask", "Makes a network request"),
    ("wget *", "ask", "Makes a network request"),
    ("kill *", "ask", "Terminates processes"),
    ("pkill *", "ask", "Terminates processes"),
    ("killall *", "ask", "Terminates processes"),
    ("docker *", "ask", "Runs Docker"),
    ("kubectl *", "ask", "Controls Kubernetes"),
    ("make install*", "ask", "Installs built artifacts"),
    ("* > *", "ask", "Writes command output to files"),
    ("*| tee *", "ask", "Writes command output to files"),
)


@dataclass(frozen=True)
class PermissionDecision:
    """一次权限检查的结果。"""

    behavior: PermissionBehavior
    reason: str


@dataclass(frozen=True)
class BashValidationHit:
    """shell/bash 命令命中的风险模式。"""

    pattern: str
    severity: Literal["severe", "ask"]
    reason: str


@dataclass
class BashValidator:
    """负责识别 shell 命令中的危险或需确认模式。"""

    patterns: tuple[tuple[str, str, str], ...] = SHELL_RISK_PATTERNS

    def validate(self, command: str) -> list[BashValidationHit]:
        """返回命中的风险列表；不命中则返回空列表。"""
        normalized = _normalize_command(command)
        hits: list[BashValidationHit] = []
        for pattern, severity, reason in self.patterns:
            if fnmatch.fnmatch(normalized, pattern.lower()):
                hits.append(
                    BashValidationHit(
                        pattern=pattern,
                        severity=severity,  # type: ignore[arg-type]
                        reason=reason,
                    )
                )
        return hits


@dataclass
class PermissionManager:
    """执行四步权限检测的核心对象。"""

    mode: PermissionMode = "confirm"
    rules: list[dict[str, Any]] | None = None
    bash_validator: BashValidator = field(default_factory=BashValidator)
    consecutive_denials: int = 0

    def __post_init__(self) -> None:
        if self.mode not in ("confirm", "human", "yolo"):
            raise ValueError(f"Unknown permission mode: {self.mode}")
        if self.rules is None:
            self.rules = [dict(rule) for rule in DEFAULT_RULES]

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> PermissionDecision:
        """
        按固定顺序检查权限：
        0. shell 安全校验
        1. deny rules
        2. mode check
        3. allow rules
        4. 默认 ask
        """
        # Step 0: shell/bash 额外安全检查，严重风险直接拒绝，其余风险升级为 ask。
        if _is_shell_tool(tool_name):
            command = _get_shell_command(tool_input)
            hits = self.bash_validator.validate(command)
            severe_hits = [hit for hit in hits if hit.severity == "severe"]
            if severe_hits:
                self.consecutive_denials += 1
                hit = severe_hits[0]
                return PermissionDecision(
                    "deny",
                    f"Blocked by shell safety validator: {hit.reason} ({hit.pattern})",
                )
            ask_hits = [hit for hit in hits if hit.severity == "ask"]
            if ask_hits:
                hit = ask_hits[0]
                return PermissionDecision(
                    "ask",
                    f"Shell command needs confirmation: {hit.reason} ({hit.pattern})",
                )

        # Step 1: deny 规则永远优先，不能被 yolo 或 allow 规则绕过。
        for rule in self.rules or []:
            if rule.get("behavior") != "deny":
                continue
            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials += 1
                return PermissionDecision("deny", f"Blocked by deny rule: {rule}")

        # Step 2: 模式决策。yolo 在 deny 之后直接允许；confirm/human 自动允许只读工具。
        if self.mode == "yolo":
            return PermissionDecision("allow", "Yolo mode: allowed after deny checks")

        elif self.mode == "confirm" or self.mode == "human":
            if _is_read_only_tool(tool_name, tool_input):
                return PermissionDecision(
                    "allow",
                    f"{self.mode.title()} mode: read-only tool auto-approved",
                )

        # Step 3: allow 规则。
        for rule in self.rules or []:
            if rule.get("behavior") != "allow":
                continue
            if self._matches(rule, tool_name, tool_input):
                return PermissionDecision("allow", f"Matched allow rule: {rule}")

        # Step 4: 默认询问用户。
        return PermissionDecision(
            "ask",
            f"No permission rule matched for {tool_name}; asking user",
        )

    def _matches(self, rule: dict[str, Any], tool_name: str, tool_input: dict[str, Any]) -> bool:
        """判断一条规则是否匹配当前工具调用。"""
        rule_tool = str(rule.get("tool", "*"))
        if not fnmatch.fnmatch(tool_name, rule_tool):
            return False

        if "content" in rule:
            content = _tool_content(tool_name, tool_input)
            if not fnmatch.fnmatch(_normalize_command(content), str(rule["content"]).lower()):
                return False

        if "path" in rule:
            path = str(tool_input.get("path") or tool_input.get("cwd") or "")
            if not fnmatch.fnmatch(path, str(rule["path"])):
                return False

        return True


def is_shell_readonly(command: str) -> bool:
    """判断 shell 命令是否属于只读命令。"""
    if _has_write_redirect(command):
        return False
    stripped = _normalize_command(command)
    for prefix in READONLY_SHELL_PREFIXES:
        prefix = prefix.lower()
        if stripped == prefix or stripped.startswith(prefix + " "):
            return True
    return False


def check_shell_blocked(command: str) -> str | None:
    """兼容旧测试的辅助函数：返回命中的 severe shell 模式。"""
    for hit in BashValidator().validate(command):
        if hit.severity == "severe":
            return hit.pattern
    return None


def shell_needs_confirmation(command: str) -> bool:
    """判断 shell 命令是否需要用户确认。"""
    if is_shell_readonly(command):
        return False
    return any(hit.severity == "ask" for hit in BashValidator().validate(command))


def terminal_confirm(tool_name: str, tool_input: dict[str, Any] | None = None, reason: str = "") -> bool:
    """
    在终端询问用户是否允许一次工具调用。

    返回 True 表示允许执行，False 表示拒绝执行。
    """
    tool_input = tool_input or {}
    if not sys.stdin.isatty():
        print(f"\n[confirm] Non-interactive terminal, rejecting {tool_name}: {reason}", flush=True)
        return False

    print(f"\n\033[33m  Agent wants to run tool:\033[0m")
    print(f"     \033[1m{tool_name}\033[0m")
    key = _tool_content(tool_name, tool_input) or str(tool_input)
    if key:
        print(f"     {key}")
    if reason:
        print(f"     reason: {reason}")

    while True:
        try:
            ans = input("  Allow? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", ""):
            print("  \033[31mRejected\033[0m")
            return False
        print("  Please enter y or n.")


def always_allow(tool_name: str, tool_input: dict[str, Any] | None = None, reason: str = "") -> bool:
    """测试用回调：始终允许。"""
    return True


def always_deny(tool_name: str, tool_input: dict[str, Any] | None = None, reason: str = "") -> bool:
    """测试用回调：始终拒绝。"""
    return False


def _is_shell_tool(tool_name: str) -> bool:
    return tool_name in ("shell", "bash")


def _get_shell_command(tool_input: dict[str, Any]) -> str:
    return str(tool_input.get("cmd") or tool_input.get("command") or "")


def _tool_content(tool_name: str, tool_input: dict[str, Any]) -> str:
    if _is_shell_tool(tool_name):
        return _get_shell_command(tool_input)
    return str(
        tool_input.get("content")
        or tool_input.get("edits_text")
        or tool_input.get("message")
        or tool_input.get("path")
        or ""
    )


def _is_read_only_tool(tool_name: str, tool_input: dict[str, Any]) -> bool:
    if tool_name in READ_ONLY_TOOLS:
        return True
    if _is_shell_tool(tool_name):
        return is_shell_readonly(_get_shell_command(tool_input))
    return False


def _normalize_command(command: str) -> str:
    return " ".join(command.strip().lower().split())


def _has_write_redirect(command: str) -> bool:
    # 匹配单个 >，排除追加重定向 >>。
    return re.search(r"(?<![>])>(?![>])", command) is not None
