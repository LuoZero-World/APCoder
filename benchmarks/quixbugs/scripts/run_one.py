from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    DEFAULT_PYTEST_TIMEOUT,
    copy_quixbugs_to_temp,
    find_task,
    remove_temp_root,
    run_pytest,
)


def parse_args() -> argparse.Namespace:
    """解析单任务评估参数。

    run_one 只负责“干净环境 + 一个 pytest 文件”的最小验证，不会调用 agent。
    """
    parser = argparse.ArgumentParser(description="Run one QuixBugs pytest task in a clean temp copy.")
    parser.add_argument("--task_id", required=True, help="QuixBugs task id, for example bitcount")
    parser.add_argument("--timeout", type=int, default=DEFAULT_PYTEST_TIMEOUT, help="pytest subprocess timeout in seconds")
    parser.add_argument("--runslow", action="store_true", help="include QuixBugs slow tests when supported")
    parser.add_argument("--keep-temp", action="store_true", help="keep the copied QuixBugs directory for debugging")
    parser.add_argument("--json", action="store_true", help="print a JSON result instead of a short status line")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    temp_root = None
    try:
        # 1. 从 tasks.jsonl 中找到任务，拿到源码文件和 pytest 文件的相对路径。
        task = find_task(args.task_id)

        # 2. 每次运行都复制一份新的 QuixBugs，避免污染项目根目录里的原始 benchmark。
        temp_root, quixbugs_root = copy_quixbugs_to_temp(prefix=f"quixbugs-{task.task_id}-")

        # 3. 在临时 QuixBugs 根目录下运行对应 pytest。
        #    QuixBugs 自带 conftest.py，所以 --runslow 和默认 skip 逻辑会原样生效。
        test_result = run_pytest(
            quixbugs_root=quixbugs_root,
            test_file=task.test_file,
            timeout=args.timeout,
            runslow=args.runslow,
        )
        payload = {
            "task_id": task.task_id,
            "status": test_result.status,
            "returncode": test_result.returncode,
            "duration_sec": round(test_result.duration_sec, 3),
            "command": test_result.command,
            "temp_root": str(temp_root) if args.keep_temp else None,
            "test_output": test_result.output,
        }

        # 4. 支持人类可读输出，也支持 JSON 输出，方便被上层脚本或 CI 消费。
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"{task.task_id} {test_result.status.upper()}")
            if test_result.output:
                print(test_result.output.rstrip())
        return 0 if test_result.status == "pass" else 1
    except Exception as exc:
        payload = {"task_id": args.task_id, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"{args.task_id} ERROR: {payload['error']}", file=sys.stderr)
        return 2
    finally:
        # 默认清理临时目录；调试时传 --keep-temp 可以保留现场。
        if temp_root is not None:
            remove_temp_root(temp_root, keep=args.keep_temp)


if __name__ == "__main__":
    raise SystemExit(main())
