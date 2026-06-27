# APCoder

本仓库基于原项目扩展而来：
https://github.com/muk610648-design/forge-agent/tree/main

在原有能力之上，本版本补充并规划了以下方向：

- 本地 coding agent harness：统一 ReAct 主循环、模型接入、工具调用、任务状态与运行结果。
- 上下文与仓库理解：引入 repo-map、token budget 和历史裁剪，提升长任务中的仓库感知能力。
- 安全工具体系：补充文件读写、精确编辑、搜索、测试、Git、Shell 等工具，并加入权限控制与高风险命令拦截。
- 运行审计与评测闭环：通过 JSONL EventLog 记录完整执行轨迹，并接入 QuixBugs / SWE-bench Lite 评测流程。
- 交互与运行隔离：支持 CLI、Chat、GitHub Issue 入口，以及 Local / Docker Runtime 执行环境切换。

未来迭代方向：
- 更完善的上下文压缩机制，降低长任务中的信息丢失风险。
- 更稳定的记忆机制，让智能体能够持续沉淀任务经验与偏好。
- 完整的 checkpoint / resume 能力
- 多智能体协作能力，支持不同角色分工、并行处理与结果汇总。
