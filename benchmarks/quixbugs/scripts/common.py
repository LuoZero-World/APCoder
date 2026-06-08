from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[3]
BENCH_ROOT = REPO_ROOT / "benchmarks" / "quixbugs"
SOURCE_QUIXBUGS = REPO_ROOT / "QuixBugs"
TASKS_FILE = BENCH_ROOT / "tasks.jsonl"
RESULTS_DIR = BENCH_ROOT / "results"
TEMP_DIR = BENCH_ROOT / "tmp"

DEFAULT_PYTEST_TIMEOUT = 20
DEFAULT_AGENT_TIMEOUT = 600
OUTPUT_LIMIT = 80_000


@dataclass(frozen=True)
class QuixBugsTask:
    task_id: str
    buggy_file: str
    test_file: str


@dataclass
class CommandResult:
    status: str
    returncode: int | None
    output: str
    duration_sec: float
    command: list[str]


def ensure_quixbugs_root(path: Path = SOURCE_QUIXBUGS) -> None:
    missing = [
        rel
        for rel in ("python_programs", "python_testcases", "conftest.py")
        if not (path / rel).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"QuixBugs root is incomplete at {path}; missing: {', '.join(missing)}"
        )


def discover_tasks(quixbugs_root: Path = SOURCE_QUIXBUGS) -> list[QuixBugsTask]:
    ensure_quixbugs_root(quixbugs_root)
    tasks: list[QuixBugsTask] = []
    for test_path in sorted((quixbugs_root / "python_testcases").glob("test_*.py")):
        task_id = test_path.stem.removeprefix("test_")
        program_path = quixbugs_root / "python_programs" / f"{task_id}.py"
        if not program_path.exists():
            continue
        tasks.append(
            QuixBugsTask(
                task_id=task_id,
                buggy_file=f"python_programs/{task_id}.py",
                test_file=f"python_testcases/test_{task_id}.py",
            )
        )
    if not tasks:
        raise RuntimeError(f"No Python QuixBugs tasks found under {quixbugs_root}")
    return tasks


def write_tasks(tasks: Iterable[QuixBugsTask], path: Path = TASKS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for task in tasks:
            fh.write(json.dumps(asdict(task), ensure_ascii=False) + "\n")


def load_tasks(path: Path = TASKS_FILE) -> list[QuixBugsTask]:
    tasks: list[QuixBugsTask] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                tasks.append(
                    QuixBugsTask(
                        task_id=raw["task_id"],
                        buggy_file=raw["buggy_file"],
                        test_file=raw["test_file"],
                    )
                )
            except (json.JSONDecodeError, KeyError) as exc:
                raise ValueError(f"Invalid task record in {path}:{lineno}: {exc}") from exc
    return tasks


def ensure_tasks_file(refresh: bool = False) -> list[QuixBugsTask]:
    if refresh or not TASKS_FILE.exists():
        tasks = discover_tasks()
        write_tasks(tasks)
        return tasks
    return load_tasks()


def find_task(task_id: str, tasks: Iterable[QuixBugsTask] | None = None) -> QuixBugsTask:
    for task in tasks or ensure_tasks_file():
        if task.task_id == task_id:
            return task
    raise KeyError(f"Unknown QuixBugs task_id: {task_id}")


def copy_quixbugs_to_temp(prefix: str = "quixbugs-") -> tuple[Path, Path]:
    ensure_quixbugs_root()
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=prefix, dir=TEMP_DIR))
    target = temp_root / "QuixBugs"
    shutil.copytree(
        SOURCE_QUIXBUGS,
        target,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )
    return temp_root, target


def remove_temp_root(temp_root: Path, keep: bool = False) -> None:
    if keep:
        return
    shutil.rmtree(temp_root, ignore_errors=True)


def combined_output(proc: subprocess.CompletedProcess[str]) -> str:
    output = (proc.stdout or "") + (proc.stderr or "")
    if len(output) > OUTPUT_LIMIT:
        return output[-OUTPUT_LIMIT:]
    return output


def quixbugs_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = [str(REPO_ROOT)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


def run_pytest(
    quixbugs_root: Path,
    test_file: str,
    timeout: int = DEFAULT_PYTEST_TIMEOUT,
    runslow: bool = False,
) -> CommandResult:
    cmd = [sys.executable, "-m", "pytest", "-q"]
    if runslow:
        cmd.append("--runslow")
    if find_spec("pytest_timeout") is not None:
        cmd.extend(["--timeout", str(timeout)])
    cmd.append(test_file)

    start = time.time()
    try:
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
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return CommandResult(
            status="timeout",
            returncode=None,
            output=output[-OUTPUT_LIMIT:],
            duration_sec=time.time() - start,
            command=cmd,
        )
    except Exception as exc:
        return CommandResult(
            status="error",
            returncode=None,
            output=f"{type(exc).__name__}: {exc}",
            duration_sec=time.time() - start,
            command=cmd,
        )

    return CommandResult(
        status="pass" if proc.returncode == 0 else "fail",
        returncode=proc.returncode,
        output=combined_output(proc),
        duration_sec=time.time() - start,
        command=cmd,
    )


def git_diff(repo_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "diff", "--no-ext-diff", "HEAD", "--"],
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
    return proc.stdout[-OUTPUT_LIMIT:]


def changed_files(repo_path: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_path,
            env=quixbugs_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        if proc.returncode != 0:
            return []
    except Exception:
        return []

    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().replace("\\", "/")
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if is_ignored_status_path(path):
            continue
        paths.append(path)
    return sorted(set(paths))


def is_ignored_status_path(path: str) -> bool:
    return (
        path == "logs"
        or path.startswith("logs/")
        or path == ".pytest_cache"
        or path.startswith(".pytest_cache/")
        or path == "__pycache__"
        or path.startswith("__pycache__/")
        or "/__pycache__/" in path
        or path.endswith(".pyc")
    )


def result_path() -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return RESULTS_DIR / f"run_{stamp}.json"
