from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import queue as queue_module
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_PYTEST_TIMEOUT,
    TEMP_DIR,
    REPO_ROOT,
    ensure_tasks_file,
    changed_files,
    quixbugs_env,
    result_path,
    run_pytest,
)


TEMP_QUIXBUGS_ROOT = TEMP_DIR / "QuixBugs"


TASK_PROMPT = """\
You are fixing a single Python QuixBugs task.

Target file:
python_programs/{task_id}.py

Test file:
python_testcases/test_{task_id}.py

Context:
- The target file contains one buggy function.
- The function purpose, expected input, and expected output are described in that file.
- The file is known to contain a bug. Your job is to fix that bug.

Rules:
- Read only the target file and its test file first.
- Modify only python_programs/{task_id}.py.
- Do not modify tests, conftest.py, or any other file.
- Avoid repository listing, correct solutions, json test data, and tester.py.
- If pytest times out, treat it as evidence of an infinite loop in the target function; do not rerun the same test before editing.
- Validate with python_testcases/test_{task_id}.py.
- When that pytest passes, finish immediately with a concise summary.
"""


def parse_agent_steps(output: str, fallback: int) -> int:
    """从 agent CLI 输出里提取实际步数。

    如果 agent 在启动阶段失败，输出中可能没有 Steps 行，此时用 max_steps 作为兜底。
    """
    match = re.search(r"^Steps\s*:\s*(\d+)\s*$", output, flags=re.MULTILINE)
    if not match:
        return fallback
    return int(match.group(1))


def empty_token_usage() -> dict[str, int]:
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


class TokenTrackingBackend:
    """Forward LLM calls while accumulating response token usage."""

    def __init__(self, backend) -> None:
        self._backend = backend
        self.usage = empty_token_usage()

    def __getattr__(self, name: str):
        return getattr(self._backend, name)

    def _record(self, response):
        input_tokens = int(getattr(response, "input_tokens", 0) or 0)
        output_tokens = int(getattr(response, "output_tokens", 0) or 0)
        total_tokens = int(getattr(response, "total_tokens", input_tokens + output_tokens) or 0)
        self.usage["input_tokens"] += input_tokens
        self.usage["output_tokens"] += output_tokens
        self.usage["total_tokens"] += total_tokens
        return response

    def complete(self, messages, tools):
        return self._record(self._backend.complete(messages, tools))

    def stream(self, messages, tools, on_text=None, on_thought=None):
        if not hasattr(self._backend, "stream"):
            return self.complete(messages, tools)
        return self._record(
            self._backend.stream(messages, tools, on_text=on_text, on_thought=on_thought)
        )


def write_temp_agent_config(quixbugs_root: Path) -> Path:
    """为当前临时 QuixBugs 写一份 agent 配置。

    agent 进程的 cwd 必须放在临时 QuixBugs 下，这样 file/shell/test 工具的相对路径
    才会指向被测仓库。与此同时，日志不能写进被测仓库，否则会干扰“只允许改目标文件”
    的变更检查，所以这里把 log_dir 改到临时仓库外侧。
    """
    base_config = (REPO_ROOT / "config" / "default.yaml").read_text(encoding="utf-8")
    log_dir = quixbugs_root.parent / "agent_logs"
    config_text, count = re.subn(
        r"(?m)^(\s*)log_dir\s*:.*$",
        lambda match: f"{match.group(1)}log_dir: {json.dumps(str(log_dir))}",
        base_config,
        count=1,
    )
    if count == 0:
        config_text = base_config.rstrip() + f"\nagent:\n  log_dir: {json.dumps(str(log_dir))}\n"
    config_path = quixbugs_root.parent / "agent_config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    return config_path


def get_manual_quixbugs_root() -> Path:
    """Return the manually prepared temporary QuixBugs repo.

    run_quixbugs does not copy QuixBugs anymore. The caller is expected to prepare:
    benchmarks/quixbugs/tmp/QuixBugs
    """
    if not TEMP_QUIXBUGS_ROOT.exists():
        raise FileNotFoundError(
            "Temporary QuixBugs repo not found. Please copy QuixBugs to "
            f"{TEMP_QUIXBUGS_ROOT} before running this benchmark."
        )
    if not (TEMP_QUIXBUGS_ROOT / "python_programs").exists():
        raise FileNotFoundError(
            f"Invalid temporary QuixBugs repo: missing python_programs under {TEMP_QUIXBUGS_ROOT}"
        )
    if not (TEMP_QUIXBUGS_ROOT / "python_testcases").exists():
        raise FileNotFoundError(
            f"Invalid temporary QuixBugs repo: missing python_testcases under {TEMP_QUIXBUGS_ROOT}"
        )
    return TEMP_QUIXBUGS_ROOT


def git_diff_for_file(repo_path: Path, path: str) -> str:
    """Collect only the current task file diff.

    The shared tmp/QuixBugs repo accumulates fixes across tasks, so a full git diff
    would include previous tasks and make each result record noisy.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--no-ext-diff", "HEAD", "--", path],
            cwd=repo_path,
            env=quixbugs_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except Exception as exc:
        return f"<failed to collect git diff: {type(exc).__name__}: {exc}>"
    return proc.stdout[-80_000:]


def parse_args() -> argparse.Namespace:
    """解析批量 benchmark 参数。"""
    parser = argparse.ArgumentParser(description="Run the local QuixBugs benchmark against this Code Agent.")
    parser.add_argument("--max-steps", "--max_steps", dest="max_steps", type=int, default=10, help="maximum agent steps per task")
    parser.add_argument("--task-id", "--task_id", dest="task_id", action="append", help="run only the specified task id; may be repeated")
    parser.add_argument("--limit", type=int, default=None, help="run only the first N selected tasks")
    parser.add_argument("--timeout", type=int, default=DEFAULT_PYTEST_TIMEOUT, help="pytest subprocess timeout in seconds")
    parser.add_argument("--agent-timeout", type=int, default=DEFAULT_AGENT_TIMEOUT, help="agent subprocess timeout in seconds")
    parser.add_argument("--runslow", action="store_true", help="include knapsack slow test; levenshtein slow case remains skipped by QuixBugs")
    parser.add_argument("--refresh-tasks", action="store_true", help="regenerate benchmarks/quixbugs/tasks.jsonl before running")
    parser.add_argument("--output", type=Path, default=None, help="result JSON path; defaults to benchmarks/quixbugs/results/run_*.json")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    return args


def _run_agent_worker(
    queue: multiprocessing.Queue,
    quixbugs_root: Path,
    task_id: str,
    max_steps: int,
) -> None:
    """Run one agent task in a child process and send back a compact result."""
    prompt = TASK_PROMPT.format(task_id=task_id)
    config_path = write_temp_agent_config(quixbugs_root)
    old_cwd = Path.cwd()
    try:
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog
        from agent.task import Task
        from config.schema import load_config, merge_cli_overrides
        from entry.cli import _build_registry
        from llm.router import create_backend_from_config

        config = load_config(str(config_path))
        config = merge_cli_overrides(config, max_steps=max_steps)
        backend = create_backend_from_config(
            {
                "provider": config.llm.provider,
                "model": config.llm.model,
                "api_key": config.llm.api_key or None,
                "base_url": config.llm.base_url or None,
                "max_tokens": config.llm.max_tokens,
            }
        )
        tracking_backend = TokenTrackingBackend(backend)
        registry = _build_registry(config, confirm_callback=None, runtime=None)
        agent_config = AgentConfig(
            max_steps=config.agent.max_steps,
            budget_tokens=config.agent.budget_tokens,
            history_max_messages=config.context.history_window * 2,
            stream=True,
            stream_callback=lambda _text: None,
            thought_callback=lambda _text: None,
            confirm_dangerous=False,
            confirm_callback=None,
        )
        agent = Agent(tracking_backend, registry, agent_config)
        task = Task(
            description=prompt,
            repo_path=str(quixbugs_root),
            max_steps=config.agent.max_steps,
            budget_tokens=config.agent.budget_tokens,
        )

        # Existing tools resolve relative paths against cwd, mirroring the old
        # CLI subprocess cwd=quixbugs_root behavior.
        os.chdir(quixbugs_root)
        with EventLog.create(task, log_dir=config.agent.log_dir) as log:
            result = agent.run(task, log)

        queue.put(
            {
                "status": "ok" if result.is_success() else "fail",
                "returncode": 0 if result.is_success() else 1,
                "steps": result.steps_taken,
                "summary": result.summary,
                "error": result.error,
                "tokens": tracking_backend.usage,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "status": "error",
                "returncode": None,
                "steps": max_steps,
                "summary": "",
                "error": f"{type(exc).__name__}: {exc}",
                "tokens": empty_token_usage(),
            }
        )
    finally:
        try:
            os.chdir(old_cwd)
        except OSError:
            pass


def run_agent(quixbugs_root: Path, task_id: str, max_steps: int, timeout: int) -> dict[str, object]:
    """Run one Code Agent task and return compact execution metadata."""
    start = time.time()
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(
        target=_run_agent_worker,
        args=(queue, quixbugs_root, task_id, max_steps),
    )
    process.start()
    process.join(timeout)
    duration_sec = round(time.time() - start, 3)

    if process.is_alive():
        process.terminate()
        process.join(5)
        return {
            "status": "timeout",
            "returncode": None,
            "duration_sec": duration_sec,
            "steps": max_steps,
            "summary": "",
            "error": f"agent timed out after {timeout}s",
            "tokens": empty_token_usage(),
        }

    try:
        result = queue.get(timeout=5)
    except queue_module.Empty:
        return {
            "status": "error",
            "returncode": process.exitcode,
            "duration_sec": duration_sec,
            "steps": max_steps,
            "summary": "",
            "error": f"agent worker exited without result (exitcode={process.exitcode})",
            "tokens": empty_token_usage(),
        }

    result["duration_sec"] = duration_sec
    return result


def evaluate_task(
    task,
    index: int,
    total: int,
    args: argparse.Namespace,
    quixbugs_root: Path,
) -> dict[str, object]:
    """评估单个 QuixBugs 任务，并返回写入结果 JSON 的记录。"""
    failed_reason = None
    agent_result: dict[str, object] = {}
    try:
        # Reuse the manually prepared tmp/QuixBugs repo for every task. Snapshot
        # changed files before this run so old task fixes do not count as current
        # task changes.
        changed_before = set(changed_files(quixbugs_root))
        agent_result = run_agent(
            quixbugs_root=quixbugs_root,
            task_id=task.task_id,
            max_steps=args.max_steps,
            timeout=args.agent_timeout,
        )

        # Only newly changed files are checked against the current task target.
        # This prevents previous task edits from poisoning later task judgments.
        changed_after = set(changed_files(quixbugs_root))
        files_changed = sorted(changed_after - changed_before)
        allowed_file = task.buggy_file
        disallowed = [path for path in files_changed if path != allowed_file]

        if agent_result["status"] == "timeout":
            failed_reason = "agent_timeout"
        elif agent_result["status"] == "error":
            failed_reason = "agent_error"
        elif disallowed:
            failed_reason = f"modified_disallowed_files: {', '.join(disallowed)}"

        if failed_reason is None:
            # 只有 agent 没有超时/报错/越界修改时，才运行官方 pytest 文件做最终判定。
            test_result = run_pytest(
                quixbugs_root=quixbugs_root,
                test_file=task.test_file,
                timeout=args.timeout,
                runslow=args.runslow,
            )
            passed = test_result.status == "pass"
            if not passed:
                failed_reason = test_result.status
        else:
            passed = False

        # 结果记录既保留最终测试输出，也保留 agent 子进程输出，便于事后分析失败原因。
        steps = int(agent_result.get("steps", args.max_steps) or args.max_steps)
        status_text = "PASS" if passed else "FAIL"
        print(f"[{index}/{total}] {task.task_id} {status_text}", flush=True)
        return {
            "task_id": task.task_id,
            "passed": passed,
            "steps": steps,
            "failed_reason": failed_reason,
            "duration_sec": agent_result.get("duration_sec", 0),
            "tokens": agent_result.get("tokens", empty_token_usage()),
            "changed_files": files_changed,
        }
    except Exception as exc:
        print(f"[{index}/{total}] {task.task_id} ERROR", flush=True)
        return {
            "task_id": task.task_id,
            "passed": False,
            "steps": args.max_steps,
            "failed_reason": f"error: {type(exc).__name__}: {exc}",
            "duration_sec": agent_result.get("duration_sec", 0),
            "tokens": agent_result.get("tokens", empty_token_usage()),
            "changed_files": [],
        }


def main() -> int:
    args = parse_args()
    try:
        quixbugs_root = get_manual_quixbugs_root()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # 默认复用已生成的 tasks.jsonl；--refresh-tasks 会从 QuixBugs 目录重新发现任务。
    tasks = ensure_tasks_file(refresh=args.refresh_tasks)
    if args.task_id:
        # 支持只跑一个或几个任务，便于早期调试 agent 能力。
        selected = set(args.task_id)
        tasks = [task for task in tasks if task.task_id in selected]
        missing = selected - {task.task_id for task in tasks}
        if missing:
            print(f"Unknown task_id(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    if args.limit is not None:
        tasks = tasks[: args.limit]

    results = []
    total = len(tasks)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    wall_start = time.time()
    for index, task in enumerate(tasks, start=1):
        results.append(evaluate_task(task, index, total, args, quixbugs_root))

    # 汇总全局指标，并把每个任务的 patch/test_output/failed_reason 写进同一个 JSON。
    passed = sum(1 for item in results if item["passed"])
    pass_rate = (passed / total) if total else 0.0
    token_totals = empty_token_usage()
    for item in results:
        tokens = item.get("tokens", {})
        for key in token_totals:
            token_totals[key] += int(tokens.get(key, 0) or 0)

    payload = {
        "total": total,
        "passed": passed,
        "pass_rate": pass_rate,
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_sec": round(time.time() - wall_start, 3),
        "tokens": token_totals,
        "max_steps": args.max_steps,
        "runslow": args.runslow,
        "limit": args.limit,
        "task_ids": args.task_id,
        "tasks": results,
    }

    out_path = args.output or result_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台输出保持轻量，详细信息看 results/run_*.json。
    print(f"Solved: {passed}/{total}")
    print(f"Pass rate: {pass_rate * 100:.1f}%")
    print(
        "Tokens: "
        f"input={token_totals['input_tokens']:,}, "
        f"output={token_totals['output_tokens']:,}, "
        f"total={token_totals['total_tokens']:,}"
    )
    print(f"Results: {out_path}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
