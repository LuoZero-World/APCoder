# `search_text` 结构化输出设计

## 目标

将 `search_text` 从 grep 风格的文本拼接结果改为原生结构化结果，使 Agent 能直接获得文件位置、命中范围和局部上下文，并把位置交给后续 LSP 或局部读取工具。

保留正则搜索、递归文件搜索及 include/exclude glob 过滤；不实现 files、count、ranking 或语义搜索。

## 结果协议

`ToolResult.output` 和 `Observation.output` 从 `str` 扩展为 `Any`。现有工具继续返回字符串，只有需要结构化数据的工具返回 `dict/list`。

EventLog 直接保存原生 JSON 对象。向 LLM 对话历史注入 Observation 时：

- 字符串保持原样；
- `dict/list` 使用 `json.dumps(..., ensure_ascii=False, indent=2)` 序列化；
- 其他简单值转换为字符串。

这样可以保持现有工具兼容，同时避免 `search_text` 在工具内部拼接伪结构化文本。

## 搜索结果结构

成功结果固定为：

```json
{
  "matches": [
    {
      "path": "src/service.py",
      "line": 42,
      "column": 9,
      "match_span": {"start": 8, "end": 15},
      "context": {
        "start_line": 40,
        "lines": ["line 40", "line 41", "line 42", "line 43"]
      }
    }
  ],
  "truncated": false
}
```

字段约定：

- `line`、`column` 为 1-based，方便传给 LSP 和读取工具。
- `match_span.start/end` 是命中行内 0-based、左闭右开字符区间。
- `context.start_line` 是 `context.lines[0]` 的真实文件行号。
- `context.lines` 包含命中行以及请求的前后上下文，不保留换行符。
- 同一行内多个正则命中分别生成结果。
- 无匹配返回空 `matches` 和 `truncated=false`。
- 达到 `max_results` 后继续探测一个命中；存在额外命中时设置 `truncated=true`。

失败结果通过 `ToolResult.error` 返回原因，`output` 使用空的标准结果结构。

## 参数

- `pattern: string`：必填，非空，支持 Python 正则。
- `path: string`：文件或目录，默认当前目录；目录递归搜索。
- `include_patterns: string[]`：包含 glob，默认 `['*']`。
- `exclude_patterns: string[]`：排除 glob，默认空。
- `file_pattern: string`：旧接口兼容别名，与 `include_patterns` 合并去重。
- `case_sensitive: boolean`：默认 `true`。
- `whole_word: boolean`：默认 `false`；启用时用词边界包裹整个正则表达式。
- `context_before: integer`：默认 2，范围 0～20。
- `context_after: integer`：默认 2，范围 0～20。
- `max_results: integer`：默认 50，范围 1～200。

不增加 files 或 count 模式。

## 文件过滤

继续使用 Python 标准库，不新增依赖。为避免影响 `find_symbol`，新增仅供 `search_text` 使用的候选文件遍历辅助函数：

1. 文件路径直接作为单个候选，但仍应用 include/exclude。
2. 目录使用递归遍历。
3. 跳过 `.git`、虚拟环境、依赖、缓存和构建目录。
4. 路径先匹配任一 include，再排除匹配任一 exclude 的文件。

glob 统一按 `/` 规范化后用 `PurePosixPath.match` 处理。

## 匹配流程

1. 校验参数并编译正则；`whole_word=true` 时编译 `\b(?:pattern)\b`。
2. 遍历过滤后的候选文件，每个文件只读取一次。
3. 按行调用 `regex.finditer`，每个 occurrence 独立生成位置记录。
4. 根据命中行裁剪前后上下文，并保留真实起始行号。
5. 收集到上限后继续寻找一个 occurrence，以确定 `truncated`。
6. 返回原生字典，不构造 grep 风格字符串。

正则维持当前逐行匹配语义，不增加多行正则能力。

## 错误处理

- 非法或空正则：工具失败。
- 搜索路径不存在：工具失败。
- 参数类型或数值范围无效：工具失败并指出字段。
- 单个文件读取失败：跳过该文件，保持仓库搜索可继续。

## 测试

覆盖以下行为：

1. `ToolResult/Observation` 能保存原生字典。
2. Agent 历史将结构化 Observation 序列化为可读 JSON。
3. EventLog 能保存原生结构化输出。
4. 基础 regex 返回 path、line、column、span 和 context。
5. 同一行多个命中分别返回。
6. `case_sensitive` 与 `whole_word`。
7. `context_before/context_after` 及文件边界。
8. `include_patterns/exclude_patterns` 和旧 `file_pattern`。
9. `max_results` 与 `truncated`。
10. 无匹配、非法 regex、非法参数和不存在路径。
11. 现有 `find_files/find_symbol` 及完整工具层回归保持通过。

## 非目标

不实现 files/count 模式、结果 ranking、LSP、调用链、语义检索、搜索索引、多行正则或新的第三方依赖。
