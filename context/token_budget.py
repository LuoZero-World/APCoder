"""
context/token_budget.py

Token budget helpers for prompt components.

BudgetPlan is intentionally small:
- reserve: safety margin kept outside planned prompt content.
- repo_map: upper budget for repository map text.
- history: upper budget for conversation history.

Observations are part of history. When history trimming is enabled, observation
messages may use at most 60% of the budget left after the first task message,
so tool output cannot crowd out normal dialogue.
"""

from __future__ import annotations

from dataclasses import dataclass


_tiktoken_enc = None
_tiktoken_available = False


def _init_tiktoken() -> None:
    global _tiktoken_enc, _tiktoken_available
    if _tiktoken_available or _tiktoken_enc is not None:
        return
    try:
        import tiktoken

        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
    except Exception:
        _tiktoken_available = False


def estimate_tokens(text: str) -> int:
    """Estimate token count, preferring tiktoken with a char-count fallback."""
    if not _tiktoken_available:
        _init_tiktoken()

    if _tiktoken_available and _tiktoken_enc is not None:
        try:
            return max(1, len(_tiktoken_enc.encode(text)))
        except Exception:
            pass

    return max(1, len(text) // 4)


def estimate_chars(tokens: int) -> int:
    """Convert token count to an approximate character budget."""
    return tokens * 4


def is_tiktoken_available() -> bool:
    """Return whether tiktoken is available for diagnostics."""
    _init_tiktoken()
    return _tiktoken_available


@dataclass
class BudgetPlan:
    """Token budget plan for repo-map and history."""

    total: int
    reserve: int
    repo_map: int
    history: int

    @property
    def available(self) -> int:
        return self.total - self.reserve


class TokenBudget:
    """Token budget manager."""

    def __init__(self, total: int = 80_000) -> None:
        self._total = total

    def default_plan(self) -> BudgetPlan:
        total = self._total
        reserve = int(total * 0.15)
        available = total - reserve
        repo_map = int(available * 0.15)
        history = int(available * 0.75)
        return BudgetPlan(
            total=total,
            reserve=reserve,
            repo_map=repo_map,
            history=history,
        )

    def trim_to(self, text: str, token_limit: int) -> str:
        """Trim text to token_limit, keeping the beginning."""
        if token_limit <= 0:
            return ""
        if estimate_tokens(text) <= token_limit:
            return text

        char_limit = token_limit * 4
        candidate = text[:char_limit]
        while estimate_tokens(candidate) > token_limit and len(candidate) > 0:
            candidate = candidate[: int(len(candidate) * 0.9)]
        while True:
            omitted = estimate_tokens(text[len(candidate) :])
            result = candidate + f"\n... [{omitted} tokens truncated]"
            if estimate_tokens(result) <= token_limit:
                return result
            if not candidate:
                return ""
            candidate = candidate[: int(len(candidate) * 0.9)]

    def trim_history(
        self,
        messages: list[dict],
        token_limit: int,
    ) -> list[dict]:
        """Trim history, with observations capped at 60% after the first message."""
        if not messages:
            return messages
        if token_limit <= 0:
            return []

        # 先做总量估算；如果完整 history 已经放得下，就保持原始上下文不变。
        token_counts = [estimate_tokens(m.get("content", "")) for m in messages]
        if sum(token_counts) <= token_limit:
            return messages

        # 第一条通常是任务描述或会话起点，优先保留；极端情况下只裁剪这一条。
        first = messages[0]
        first_tokens = token_counts[0]
        if first_tokens >= token_limit:
            return [
                self._copy_message_with_content(
                    first,
                    self.trim_to(first.get("content", ""), token_limit),
                )
            ]

        remaining_budget = token_limit - first_tokens
        observation_limit = int(remaining_budget * 0.60)
        used_total = 0
        used_observation = 0
        dropped = 0
        selected_units: list[list[dict]] = []

        token_by_index = dict(enumerate(token_counts))
        units = self._build_history_units(messages)

        # 从最新 unit 往旧 unit 一轮处理。unit 可以是普通消息，也可以是 action+observation。
        for unit in reversed(units):
            obs_pos = self._observation_position(unit)
            unit_tokens = sum(token_by_index[idx] for idx, _ in unit)

            if obs_pos is None:
                # 普通消息没有 observation 限额，只需要满足 history 总剩余预算。
                if used_total + unit_tokens <= remaining_budget:
                    selected_units.append([msg for _, msg in unit])
                    used_total += unit_tokens
                else:
                    dropped += len(unit)
                continue

            obs_idx, obs_msg = unit[obs_pos]
            obs_tokens = token_by_index[obs_idx]
            non_obs_tokens = unit_tokens - obs_tokens
            total_left = remaining_budget - used_total
            observation_left = observation_limit - used_observation

            if unit_tokens <= total_left and obs_tokens <= observation_left:
                # action 和 observation 都放得下时，完整保留这个结构单元。
                selected_units.append([msg for _, msg in unit])
                used_total += unit_tokens
                used_observation += obs_tokens
                continue

            # observation 太长时不直接删除，而是替换为占位符，保留工具调用结构。
            placeholder = self._observation_placeholder(obs_msg)
            placeholder_tokens = estimate_tokens(placeholder)
            compacted_tokens = non_obs_tokens + placeholder_tokens
            if (
                compacted_tokens <= total_left
                and placeholder_tokens <= observation_left
            ):
                compacted_unit = [msg for _, msg in unit]
                compacted_unit[obs_pos] = self._copy_message_with_content(
                    obs_msg,
                    placeholder,
                )
                selected_units.append(compacted_unit)
                used_total += compacted_tokens
                used_observation += placeholder_tokens
            else:
                # 占位符也放不下时，丢弃整个 unit，避免留下孤立 action 或孤立 observation。
                dropped += len(unit)

        result = [first]
        if dropped > 0:
            # 插入提示，让模型知道中间历史不再完整连续。
            result.append({
                "role": "user",
                "content": f"[{dropped} earlier messages were truncated to fit context window]",
            })

        # selected_units 是从新到旧挑出来的，这里恢复成正常时间顺序。
        for unit in reversed(selected_units):
            result.extend(unit)
        return result

    @staticmethod
    def _build_history_units(messages: list[dict]) -> list[list[tuple[int, dict]]]:
        """把 history 切成不可拆的裁剪单元，保护 action-observation 配对。"""
        units: list[list[tuple[int, dict]]] = []
        idx = 1
        while idx < len(messages):
            msg = messages[idx]
            next_idx = idx + 1
            if (
                TokenBudget._is_action_message(msg)
                and next_idx < len(messages)
                and TokenBudget._is_observation_message(messages[next_idx])
            ):
                units.append([(idx, msg), (next_idx, messages[next_idx])])
                idx += 2
            else:
                units.append([(idx, msg)])
                idx += 1
        return units

    @staticmethod
    def _observation_position(unit: list[tuple[int, dict]]) -> int | None:
        for pos, (_, msg) in enumerate(unit):
            if TokenBudget._is_observation_message(msg):
                return pos
        return None

    @staticmethod
    def _is_action_message(message: dict) -> bool:
        content = str(message.get("content", ""))
        return message.get("role") == "assistant" and "\nAction:" in f"\n{content}"

    @staticmethod
    def _is_observation_message(message: dict) -> bool:
        return str(message.get("content", "")).lstrip().startswith("[Tool:")

    @staticmethod
    def _observation_placeholder(message: dict) -> str:
        content = str(message.get("content", ""))
        first_line = content.lstrip().splitlines()[0] if content.strip() else "[Tool]"
        original_tokens = estimate_tokens(content)
        return (
            f"{first_line}\n"
            f"[Tool output omitted: original observation was about "
            f"{original_tokens} tokens and exceeded history budget]"
        )

    @staticmethod
    def _copy_message_with_content(message: dict, content: str) -> dict:
        copied = dict(message)
        copied["content"] = content
        return copied

    def fit_all(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
    ) -> tuple[str, str, list[dict], str]:
        plan = self.default_plan()
        trimmed_map = self.trim_to(repo_map_text, plan.repo_map)
        trimmed_history = self.trim_history(history, plan.history)
        return system_text, trimmed_map, trimmed_history, observation_text

    def usage_report(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
    ) -> dict[str, int]:
        history_tokens = sum(
            estimate_tokens(m.get("content", "")) for m in history
        )
        return {
            "system": estimate_tokens(system_text),
            "repo_map": estimate_tokens(repo_map_text),
            "history": history_tokens,
            "observation": estimate_tokens(observation_text),
            "total": (
                estimate_tokens(system_text)
                + estimate_tokens(repo_map_text)
                + history_tokens
                + estimate_tokens(observation_text)
            ),
            "budget": self._total,
            "tiktoken_used": is_tiktoken_available(),
        }
