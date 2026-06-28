# `find_files` Ripgrep 增强设计

## 目标

将 `FindFilesTool` 从 Python `Path.rglob` 文件遍历升级为 `rg --files` 独占实现，使 Agent 能在大型仓库中更快地缩小候选文件范围，并支持多组包含、排除和 `.gitignore` 控制。

本次只增强 `find_files`。`search_text` 和 `find_symbol` 继续使用现有 `_iter_files`，避免无意改变其他工具的搜索语义。

## 外部依赖

运行环境必须安装 `rg`（Ripgrep）并可通过 `PATH` 执行。本工具不提供 Python 回退。找不到 `rg` 时返回失败结果，并明确提示安装 Ripgrep。

## 工具接口

保留现有参数：

- `pattern: string`：单个包含模式的兼容写法。
- `path: string`：搜索文件或目录，默认当前目录。

新增参数：

- `include_patterns: string[]`：多个包含 glob。
- `exclude_patterns: string[]`：多个排除 glob。
- `include_ignored: boolean`：是否搜索被 ignore 规则排除的文件，默认 `false`。

`pattern` 与 `include_patterns` 可以单独使用，也可以同时使用；同时使用时合并、去重并保持首次出现的顺序。调用方必须至少提供一个非空包含模式，否则返回参数错误。空字符串和非字符串数组元素视为参数错误。

为保持当前工具的上下文预算，本次继续使用固定的 `MAX_RESULTS = 50`，不新增分页参数。

## Ripgrep 命令构造

工具通过 `subprocess.Popen` 的参数数组启动 Ripgrep，不拼接 Shell 命令。基础参数为：

```text
rg --files --hidden
```

参数映射规则：

- Ripgrep 的正向 `--glob` 会覆盖 `.gitignore`，因此包含模式在 `rg --files` 的流式结果上匹配；文件发现仍完全由 Ripgrep 完成。
- 每个排除模式转换为 `-g !<pattern>`，并放在包含模式之后。
- `include_ignored=false` 时使用 Ripgrep 默认 ignore 行为，遵守 `.gitignore`、`.ignore` 和全局 ignore 配置。
- `include_ignored=true` 时加入 `--no-ignore`。
- 内置跳过目录始终转换为负向 glob，即使启用 `include_ignored` 也不搜索 `.git`、`node_modules`、虚拟环境、缓存和构建产物。

`--hidden` 用于发现 `.github` 等隐藏源码或配置；`.git` 由内置负向 glob 单独排除。

## 执行流程

1. 读取并校验 `path`、包含模式、排除模式和 `include_ignored`。
2. 合并 `pattern` 与 `include_patterns` 并去重，作为流式结果的包含过滤器。
3. 检查 `rg` 是否可执行；缺失时返回明确错误。
4. 启动 `rg --files`，逐行消费标准输出，避免把整个仓库的文件清单一次性载入内存。
5. 收集前 50 个结果；检测到第 51 个结果后终止进程，并在输出中标记可能还有更多结果。
6. 将路径按现有工具风格输出为一行一个路径；无结果属于成功观察。

代码中的关键阶段采用编号中文注释，与仓库现有工具的注释风格保持一致。

## 路径行为

- 路径不存在时沿用现有 `Path not found` 错误。
- 目录路径作为 Ripgrep 搜索根目录。
- 文件路径只在其自身匹配任一 include 且不匹配 exclude 时返回，保持旧实现支持文件路径的行为。
- 输出路径保持可直接传给后续 `file_read` 或 `file_view` 使用。

## 错误处理

- `rg` 未安装：工具失败，提示 Ripgrep 是必需依赖。
- 参数类型或模式为空：工具失败，说明具体无效字段。
- Ripgrep 正常返回但没有文件：工具成功，返回 `No files found`。
- Ripgrep 启动失败或异常退出：工具失败，保留精简后的 stderr 作为诊断信息。
- 达到结果上限：工具成功，返回前 50 条并提示可能存在更多结果。

## 测试设计

测试覆盖：

1. 原有单 `pattern` 调用保持兼容。
2. 多个 `include_patterns` 按并集返回。
3. `pattern` 与 `include_patterns` 合并并去重。
4. `exclude_patterns` 从包含结果中排除文件。
5. 默认遵守 `.gitignore`。
6. `include_ignored=true` 可以找到 ignored 文件。
7. 启用 `include_ignored` 时仍跳过内置无关目录。
8. 缺少所有 include 参数时返回失败。
9. 模拟未安装 `rg` 时返回明确失败信息。
10. 超过 50 个结果时截断并报告存在更多结果。
11. 搜索根路径是单个文件时保持兼容行为。

现有 `SearchTextTool` 和 `FindSymbolTool` 测试必须继续通过，以证明共享 `_iter_files` 未被破坏。

## 非目标

本次不实现结果分页、相关性排序、搜索文本内容、符号索引、LSP、读取缓存或 Python 搜索回退。这些能力留在后续“定位—精读”迭代中处理。
