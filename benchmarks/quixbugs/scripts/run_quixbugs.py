from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
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

Rules:
- Only modify python_programs/{task_id}.py.
- Do not modify tests, conftest.py, correct_python_programs, json_testcases, Java files, or any other file.
- Do not use QuixBugs tester.py.
- Keep exploration short. Start with python_programs/{task_id}.py and python_testcases/test_{task_id}.py.
- Do not list the repository unless a required file is missing.
- Do not read correct_python_programs, bin/default, or json_testcases unless you cannot infer the fix from the target and test files.
- Make the minimal edit to python_programs/{task_id}.py.
- Use pytest against python_testcases/test_{task_id}.py to validate your fix.
- After the target pytest passes, finish immediately with a concise summary.
"""


def parse_agent_steps(output: str, fallback: int) -> int:
    """从 agent CLI 输出里提取实际步数。

    如果 agent 在启动阶段失败，输出中可能没有 Steps 行，此时用 max_steps 作为兜底。
    """
    match = re.search(r"^Steps\s*:\s*(\d+)\s*$", output, flags=re.MULTILINE)
    if not match:
        return fallback
    return int(match.group(1))


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
    parser.add_argument("--max_steps", type=int, default=10, help="maximum agent steps per task")
    parser.add_argument("--task_id", action="append", help="run only the specified task id; may be repeated")
    parser.add_argument("--timeout", type=int, default=DEFAULT_PYTEST_TIMEOUT, help="pytest subprocess timeout in seconds")
    parser.add_argument("--agent-timeout", type=int, default=DEFAULT_AGENT_TIMEOUT, help="agent subprocess timeout in seconds")
    parser.add_argument("--runslow", action="store_true", help="include knapsack slow test; levenshtein slow case remains skipped by QuixBugs")
    parser.add_argument("--refresh-tasks", action="store_true", help="regenerate benchmarks/quixbugs/tasks.jsonl before running")
    parser.add_argument("--keep-temp", action="store_true", help="compatibility no-op; tmp/QuixBugs is never deleted")
    parser.add_argument("--output", type=Path, default=None, help="result JSON path; defaults to benchmarks/quixbugs/results/run_*.json")
    return parser.parse_args()


def run_agent(quixbugs_root: Path, task_id: str, max_steps: int, timeout: int) -> dict[str, object]:
    """在临时 QuixBugs 仓库中调用一次 Code Agent。

    这里不直接 import Agent 类，而是走真实 CLI 入口，尽量模拟用户实际使用方式。
    """
    prompt = TASK_PROMPT.format(task_id=task_id)
    config_path = write_temp_agent_config(quixbugs_root)
    # 提示词写入临时文件，避免在命令行里处理长文本和引号转义。
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as fh:
        fh.write(prompt)
        task_file = Path(fh.name)

    cmd = [
        sys.executable,
        "-m",
        "entry.cli",
        "--config",
        str(config_path),
        "run",
        "--repo",
        str(quixbugs_root),
        "--task-file",
        str(task_file),
        "--max-steps",
        str(max_steps),
    ]
    start = time.time()
    try:
        # cwd=quixbugs_root 很重要：现有工具层按当前工作目录解析相对路径。
        # PYTHONPATH 由 quixbugs_env 指回 forge-agent 根目录，保证 entry.cli 可被导入。
        proc = subprocess.run(
            cmd,
            cwd=quixbugs_root,
            env=quixbugs_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output = ((proc.stdout or "") + (proc.stderr or ""))[-80_000:]
        return {
            "status": "ok" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
            "duration_sec": round(time.time() - start, 3),
            "command": cmd,
            "output": output,
        }
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or ""))[-80_000:]
        return {
            "status": "timeout",
            "returncode": None,
            "duration_sec": round(time.time() - start, 3),
            "command": cmd,
            "output": output,
        }
    except Exception as exc:
        return {
            "status": "error",
            "returncode": None,
            "duration_sec": round(time.time() - start, 3),
            "command": cmd,
            "output": f"{type(exc).__name__}: {exc}",
        }
    finally:
        # task_file 在 QuixBugs 仓库外侧，只是 agent CLI 的输入介质，用完即删。
        try:
            task_file.unlink()
        except OSError:
            pass


def evaluate_task(
    task,
    index: int,
    total: int,
    args: argparse.Namespace,
    quixbugs_root: Path,
) -> dict[str, object]:
    """评估单个 QuixBugs 任务，并返回写入结果 JSON 的记录。"""
    failed_reason = None
    test_output = ""
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
        patch = git_diff_for_file(quixbugs_root, task.buggy_file)
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
            test_output = test_result.output
            passed = test_result.status == "pass"
            if not passed:
                failed_reason = test_result.status
        else:
            passed = False
            test_output = str(agent_result.get("output", ""))

        # 结果记录既保留最终测试输出，也保留 agent 子进程输出，便于事后分析失败原因。
        steps = parse_agent_steps(str(agent_result.get("output", "")), args.max_steps)
        status_text = "PASS" if passed else "FAIL"
        print(f"[{index}/{total}] {task.task_id} {status_text}", flush=True)
        return {
            "task_id": task.task_id,
            "passed": passed,
            "steps": steps,
            "patch": patch,
            "test_output": test_output,
            "failed_reason": failed_reason,
            "agent": agent_result,
            "changed_files": files_changed,
            "quixbugs_root": str(quixbugs_root),
        }
    except Exception as exc:
        print(f"[{index}/{total}] {task.task_id} ERROR", flush=True)
        return {
            "task_id": task.task_id,
            "passed": False,
            "steps": args.max_steps,
            "patch": "",
            "test_output": "",
            "failed_reason": f"error: {type(exc).__name__}: {exc}",
            "agent": agent_result,
            "changed_files": [],
            "quixbugs_root": str(quixbugs_root),
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

    results = []
    total = len(tasks)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    for index, task in enumerate(tasks, start=1):
        results.append(evaluate_task(task, index, total, args, quixbugs_root))

    # 汇总全局指标，并把每个任务的 patch/test_output/failed_reason 写进同一个 JSON。
    passed = sum(1 for item in results if item["passed"])
    pass_rate = (passed / total) if total else 0.0
    payload = {
        "total": total,
        "passed": passed,
        "pass_rate": pass_rate,
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "repo_root": str(REPO_ROOT),
        "quixbugs_root": str(quixbugs_root),
        "max_steps": args.max_steps,
        "runslow": args.runslow,
        "tasks": results,
    }

    out_path = args.output or result_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台输出保持轻量，详细信息看 results/run_*.json。
    print(f"Solved: {passed}/{total}")
    print(f"Pass rate: {pass_rate * 100:.1f}%")
    print(f"Results: {out_path}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
