from __future__ import annotations

from typing import Any

import pytest

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.permission import (
    PermissionManager,
    always_allow,
    always_deny,
    check_shell_blocked,
    is_shell_readonly,
    shell_needs_confirmation,
)
from agent.task import Action, ActionType, EventType, Task, ToolCall
from llm.base import MockBackend
from tools.base import BaseTool, ToolRegistry, ToolResult


class RecordingTool(BaseTool):
    """测试用工具：记录是否真的被 agent 执行。"""

    def __init__(self, name: str, output: str = "ok") -> None:
        self._name = name
        self._output = output
        self.call_count = 0
        self.last_params: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Recording tool {self._name}"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        self.call_count += 1
        self.last_params = params
        return ToolResult(success=True, output=self._output)


def _run_agent_with_tool(tool: RecordingTool, action: Action, config: AgentConfig, tmp_path):
    backend = MockBackend([action, Action(ActionType.FINISH, "done", message="done")])
    registry = ToolRegistry().register(tool)
    agent = Agent(backend, registry, config)
    task = Task(task_id="perm", description="permission test", repo_path=str(tmp_path), max_steps=5)
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    result = agent.run(task, log)
    return result, log


# ===========================================================================
# shell/bash 风险识别
# ===========================================================================


class TestShellValidation:
    def test_readonly_shell_command(self):
        assert is_shell_readonly("ls -la")
        assert is_shell_readonly("git diff HEAD")
        assert is_shell_readonly("pytest tests/test_permission.py")

    def test_write_redirect_is_not_readonly(self):
        assert not is_shell_readonly("echo hello > output.txt")

    def test_severe_shell_command_blocked(self):
        assert check_shell_blocked("rm -rf /") is not None
        assert check_shell_blocked("sudo apt-get install vim") is not None

    def test_dangerous_shell_command_needs_confirmation(self):
        assert shell_needs_confirmation("pip install requests")
        assert shell_needs_confirmation("git reset --hard HEAD")
        assert shell_needs_confirmation("curl https://example.com")

    def test_unknown_non_risky_shell_command_does_not_need_confirmation(self):
        assert not shell_needs_confirmation("python parse_data.py")


# ===========================================================================
# PermissionManager 四步决策
# ===========================================================================


class TestPermissionManager:
    def test_deny_rule_wins_even_in_yolo(self):
        manager = PermissionManager(
            mode="yolo",
            rules=[{"tool": "file_write", "path": "*", "behavior": "deny"}],
        )

        decision = manager.check("file_write", {"path": "app.py", "content": "x"})

        assert decision.behavior == "deny"
        assert "deny rule" in decision.reason

    def test_shell_severe_pattern_denied_before_mode(self):
        manager = PermissionManager(mode="yolo")

        decision = manager.check("shell", {"cmd": "rm -rf /"})

        assert decision.behavior == "deny"
        assert "shell safety validator" in decision.reason

    def test_shell_risky_pattern_asks(self):
        manager = PermissionManager(mode="yolo")

        decision = manager.check("shell", {"cmd": "pip install requests"})

        assert decision.behavior == "ask"
        assert "needs confirmation" in decision.reason

    def test_confirm_mode_allows_read_only_tools(self):
        manager = PermissionManager(mode="confirm")

        decision = manager.check("file_read", {"path": "app.py"})

        assert decision.behavior == "allow"

    def test_confirm_mode_asks_for_writes(self):
        manager = PermissionManager(mode="confirm")

        decision = manager.check("file_edit", {"edits_text": "..."})

        assert decision.behavior == "ask"

    def test_human_mode_allows_explicit_allow_rule_only(self):
        manager = PermissionManager(
            mode="human",
            rules=[{"tool": "file_read", "path": "*", "behavior": "allow"}],
        )

        assert manager.check("file_read", {"path": "app.py"}).behavior == "allow"
        assert manager.check("file_edit", {"edits_text": "..."}).behavior == "ask"

    def test_yolo_allows_after_deny_checks(self):
        manager = PermissionManager(mode="yolo")

        decision = manager.check("file_edit", {"edits_text": "..."})

        assert decision.behavior == "allow"

    def test_invalid_mode_fails_fast(self):
        with pytest.raises(ValueError, match="Unknown permission mode"):
            PermissionManager(mode="plan")  # type: ignore[arg-type]


# ===========================================================================
# agent loop 集成
# ===========================================================================


class TestPermissionInAgentLoop:
    def test_deny_prevents_tool_execution(self, tmp_path):
        tool = RecordingTool("shell")
        action = Action(
            ActionType.TOOL_CALL,
            "try severe command",
            ToolCall("shell", {"cmd": "rm -rf /"}),
        )
        result, log = _run_agent_with_tool(
            tool,
            action,
            AgentConfig(max_steps=5, permission_mode="yolo"),
            tmp_path,
        )

        assert result.is_success()
        assert tool.call_count == 0
        observations = [e for e in log.replay() if e.event_type == EventType.OBSERVATION]
        first_obs = observations[0].payload["observation"]
        assert first_obs["status"] == "error"
        assert "Permission denied" in first_obs["error"]
        log.close()

    def test_ask_rejected_prevents_tool_execution(self, tmp_path):
        tool = RecordingTool("file_write")
        action = Action(
            ActionType.TOOL_CALL,
            "write file",
            ToolCall("file_write", {"path": "app.py", "content": "x"}),
        )
        result, log = _run_agent_with_tool(
            tool,
            action,
            AgentConfig(
                max_steps=5,
                permission_mode="confirm",
                permission_callback=always_deny,
            ),
            tmp_path,
        )

        assert result.is_success()
        assert tool.call_count == 0
        observations = [e for e in log.replay() if e.event_type == EventType.OBSERVATION]
        assert "rejected by user" in observations[0].payload["observation"]["error"]
        log.close()

    def test_ask_allowed_executes_tool(self, tmp_path):
        tool = RecordingTool("file_write")
        action = Action(
            ActionType.TOOL_CALL,
            "write file",
            ToolCall("file_write", {"path": "app.py", "content": "x"}),
        )
        result, log = _run_agent_with_tool(
            tool,
            action,
            AgentConfig(
                max_steps=5,
                permission_mode="confirm",
                permission_callback=always_allow,
            ),
            tmp_path,
        )

        assert result.is_success()
        assert tool.call_count == 1
        log.close()


# ===========================================================================
# CLI 参数
# ===========================================================================


class TestPermissionModeCli:
    def test_run_help_uses_permission_mode(self):
        from click.testing import CliRunner
        from entry.cli import cli

        result = CliRunner().invoke(cli, ["run", "--help"])

        assert result.exit_code == 0
        assert "--permission-mode" in result.output
        assert "--confirm" not in result.output

    def test_chat_help_uses_permission_mode(self):
        from click.testing import CliRunner
        from entry.cli import cli

        result = CliRunner().invoke(cli, ["chat", "--help"])

        assert result.exit_code == 0
        assert "--permission-mode" in result.output
        assert "--confirm" not in result.output

    def test_confirm_mode_without_callback_does_not_execute_write(self, tmp_path):
        tool = RecordingTool("file_edit")
        action = Action(
            ActionType.TOOL_CALL,
            "edit file",
            ToolCall("file_edit", {"edits_text": "..."}),
        )
        result, log = _run_agent_with_tool(
            tool,
            action,
            AgentConfig(max_steps=5, permission_mode="confirm"),
            tmp_path,
        )

        assert result.is_success()
        assert tool.call_count == 0
        observations = [e for e in log.replay() if e.event_type == EventType.OBSERVATION]
        assert "no callback" in observations[0].payload["observation"]["error"]
        log.close()

    def test_read_only_tool_executes_without_callback_in_confirm_mode(self, tmp_path):
        tool = RecordingTool("file_read")
        action = Action(
            ActionType.TOOL_CALL,
            "read file",
            ToolCall("file_read", {"path": "app.py"}),
        )
        result, log = _run_agent_with_tool(
            tool,
            action,
            AgentConfig(max_steps=5, permission_mode="confirm"),
            tmp_path,
        )

        assert result.is_success()
        assert tool.call_count == 1
        log.close()
