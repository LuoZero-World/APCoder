#!/usr/bin/env python
"""
Run forge-agent on SWE-bench Lite and build predictions.json.

Pipeline:
1. load_dataset reads SWE-bench Lite.
2. Iterate selected instances.
3. Clone/fetch the target repo and checkout base_commit.
4. Send problem_statement to Agent.
5. Agent edits code in the checked-out repo.
6. Export git diff as a patch.
7. Save runs/{instance_id}/prediction.patch.
8. Build predictions.json.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from datasets import load_dataset

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_cmd(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = 600,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the completed process."""
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        output = (proc.stdout + proc.stderr).strip()
        raise RuntimeError(f"Command failed: {' '.join(args)}\n{output}")
    return proc


def safe_instance_id(instance_id: str) -> str:
    """Keep SWE-bench instance IDs as directory names."""
    return instance_id.replace("/", "__")


def repo_key(repo: str) -> str:
    """Convert owner/repo to a filesystem-friendly cache name."""
    return repo.replace("/", "__")


def select_instances(dataset: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Apply instance-id/start/limit filters."""
    # dataset 本身包含完整 SWE-bench Lite；这里先转成 list，便于切片和过滤。
    instances = list(dataset)
    if args.instance_id:
        # 指定 --instance-id 时，只跑这些明确给出的任务。
        wanted = set(args.instance_id)
        instances = [item for item in instances if item["instance_id"] in wanted]
    else:
        # 否则按 start/limit 取一个连续子集，适合先小规模试跑。
        instances = instances[args.start :]
        if args.limit is not None:
            instances = instances[: args.limit]
    return instances


def ensure_repo_cache(repo: str, cache_dir: Path) -> Path:
    """Create or update a bare mirror cache for the GitHub repo."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    mirror_path = cache_dir / f"{repo_key(repo)}.git"
    url = f"https://github.com/{repo}.git"

    # 使用 bare mirror 作为缓存，避免每个 instance 都从 GitHub 全量 clone。
    if not mirror_path.exists():
        run_cmd(["git", "clone", "--mirror", url, str(mirror_path)], timeout=1800)
    else:
        # 已有缓存时更新远端引用，保证 base_commit 能被找到。
        run_cmd(["git", "remote", "update", "--prune"], cwd=mirror_path, timeout=1800)

    return mirror_path


def checkout_instance(
    repo: str,
    base_commit: str,
    work_repo: Path,
    mirror_path: Path,
    force_clean: bool,
) -> None:
    """Prepare a clean working checkout at the SWE-bench base commit."""
    # force 模式会删掉旧 worktree，确保这次从完全干净的目录开始。
    if force_clean and work_repo.exists():
        shutil.rmtree(work_repo)

    # 每个 instance 使用独立 worktree，避免不同任务之间的修改互相污染。
    if not (work_repo / ".git").exists():
        work_repo.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(["git", "clone", str(mirror_path), str(work_repo)], timeout=1800)
        run_cmd(["git", "remote", "set-url", "origin", f"https://github.com/{repo}.git"], cwd=work_repo)
    else:
        run_cmd(["git", "fetch", "origin"], cwd=work_repo, timeout=1800)

    # SWE-bench 的关键要求：必须从 instance 给定的 base_commit 开始修改。
    run_cmd(["git", "checkout", "-f", base_commit], cwd=work_repo)
    run_cmd(["git", "reset", "--hard", base_commit], cwd=work_repo)
    # 清理未跟踪文件，保证 Agent 看到的是干净仓库。
    run_cmd(["git", "clean", "-fdx"], cwd=work_repo)


def create_agent(config_path: str | None):
    """Build Agent using the project's normal entry wiring."""
    from agent.core import Agent, AgentConfig
    from config.schema import load_config
    from entry.cli import _build_registry
    from llm.router import create_backend_from_config

    # 复用项目原有配置加载逻辑：模型、provider、token budget 等都来自 config。
    config = load_config(config_path)
    backend = create_backend_from_config(
        {
            "provider": config.llm.provider,
            "model": config.llm.model,
            "api_key": config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        }
    )

    # 复用 CLI 的工具注册逻辑，保证 benchmark 和正常 agent run 使用同一套工具。
    registry = _build_registry(config)
    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )
    return Agent(backend, registry, agent_config), config


def run_agent_on_instance(
    instance: dict[str, Any],
    work_repo: Path,
    run_dir: Path,
    config_path: str | None,
) -> dict[str, Any]:
    """Run forge-agent once and return run metadata."""
    from agent.event_log import EventLog
    from agent.task import Task

    agent, config = create_agent(config_path)
    # problem_statement 是 SWE-bench 给出的 issue 描述，直接作为 Agent 任务输入。
    task = Task(
        description=instance["problem_statement"],
        repo_path=str(work_repo),
        issue_url=f"swe-bench://{instance['instance_id']}",
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    # 每个 instance 单独写 EventLog，方便之后复盘 Agent 的行为轨迹。
    started = time.time()
    log_dir = run_dir / "logs"
    with EventLog.create(task, log_dir=str(log_dir)) as log:
        result = agent.run(task, log)

    # run.json 保存轻量元信息；真正提交给 SWE-bench 的是 prediction.patch。
    return {
        "instance_id": instance["instance_id"],
        "status": result.status.value,
        "steps_taken": result.steps_taken,
        "total_tokens": result.total_tokens,
        "elapsed_seconds": round(time.time() - started, 3),
        "summary": result.summary,
    }


def export_patch(work_repo: Path, patch_path: Path) -> str:
    """Export a git patch, including untracked files via intent-to-add."""
    # git diff 默认看不到未跟踪新文件；git add -N 让新文件进入 diff 视野但不真正暂存内容。
    run_cmd(["git", "add", "-N", "."], cwd=work_repo, check=False)
    # SWE-bench prediction 的核心内容就是从 base_commit 到 Agent 修改后的 git diff。
    proc = run_cmd(["git", "diff", "HEAD", "--"], cwd=work_repo, check=False)
    patch = proc.stdout
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch, encoding="utf-8")
    return patch


def build_prediction_files(
    dataset: Any,
    runs_dir: Path,
    output_json: Path,
    model_name: str,
) -> int:
    """Collect runs/{instance_id}/prediction.patch into predictions.json."""
    predictions: list[dict[str, str]] = []

    # 按 dataset 原始顺序组装 predictions，跳过还没有生成 patch 的 instance。
    for instance in dataset:
        instance_id = instance["instance_id"]
        patch_path = runs_dir / safe_instance_id(instance_id) / "prediction.patch"
        if not patch_path.exists():
            continue

        predictions.append(
            {
                # SWE-bench harness 用 instance_id 对齐任务。
                "instance_id": instance_id,
                # model_name_or_path 只是标识本次预测来源。
                "model_name_or_path": model_name,
                # model_patch 是 Agent 生成的最终 git diff。
                "model_patch": patch_path.read_text(encoding="utf-8"),
            }
        )

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(predictions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(predictions)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-name", default="princeton-nlp/SWE-bench_Lite")
    parser.add_argument("--split", default="test")
    parser.add_argument("--work-dir", type=Path, default=Path("benchmark_work"))
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--output", type=Path, default=Path("predictions.json"))
    parser.add_argument("--model-name", default="forge-agent")
    parser.add_argument("--config", default=None)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--instance-id", action="append", help="Run only this instance; repeatable")
    parser.add_argument("--force", action="store_true", help="Recreate worktree and rerun existing patches")
    args = parser.parse_args()

    # 1. 读取 SWE-bench Lite 数据集。
    dataset = load_dataset(args.dataset_name, split=args.split)
    # 2. 根据参数决定本次实际要跑哪些 instance。
    selected = select_instances(dataset, args)

    cache_dir = args.work_dir / "cache"
    repos_dir = args.work_dir / "repos"

    # 3. 逐个 instance：准备仓库、运行 Agent、导出 patch。
    for index, instance in enumerate(selected, start=1):
        instance_id = instance["instance_id"]
        safe_id = safe_instance_id(instance_id)
        run_dir = args.runs_dir / safe_id
        patch_path = run_dir / "prediction.patch"

        if patch_path.exists() and not args.force:
            # 已经有 patch 时默认跳过，方便中断后续跑。
            print(f"[{index}/{len(selected)}] skip existing {instance_id}")
            continue

        print(f"[{index}/{len(selected)}] run {instance_id}")
        # 3.1 准备仓库缓存和当前 instance 的干净 worktree。
        mirror = ensure_repo_cache(instance["repo"], cache_dir)
        work_repo = repos_dir / safe_id
        checkout_instance(
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            work_repo=work_repo,
            mirror_path=mirror,
            force_clean=args.force,
        )

        # 3.2 把 problem_statement 交给 Agent，让它在 work_repo 中修改代码。
        metadata = run_agent_on_instance(
            instance=instance,
            work_repo=work_repo,
            run_dir=run_dir,
            config_path=args.config,
        )
        # 3.3 导出 Agent 修改后的 git diff，保存为 prediction.patch。
        patch = export_patch(work_repo, patch_path)
        metadata["patch_chars"] = len(patch)
        (run_dir / "run.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 4. 收集所有 prediction.patch，生成最终提交给 SWE-bench 的 predictions.json。
    count = build_prediction_files(
        dataset=dataset,
        runs_dir=args.runs_dir,
        output_json=args.output,
        model_name=args.model_name,
    )
    print(f"Wrote {count} predictions to {args.output}")


if __name__ == "__main__":
    main()
