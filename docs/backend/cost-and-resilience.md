# 用量、工具暴露、会话延续与模型容错

## 允许所有操作

执行权限设置新增“允许所有操作”（`ApprovalSettings.allow_all`）。开启后命令、联网、外部文件读取和文件移动都不再询问，也不调用审核小模型，开启时等待中的操作会直接放行；记录为 `source=allow_all` 的自动通过。系统沙箱、凭据屏蔽和平台控制面保护不受影响。界面在执行权限按钮上标明“全部允许”，开启前需确认，可随时关闭。

## 按会话统计用量

`agent_usage` 每次模型调用记录会话（conversation）、项目、任务、执行次数、用途（work / coordination / summary / review / title）、模型、输入/缓存/输出 token、耗时和重试次数（迁移 `0008_usage_sessions`，旧库启动时自动补列）。归属通过上下文变量 `usage_scope` 传递：Leader 每轮、调度任务执行、标题生成时设置，Worker、上下文摘要和命令审核模型自动继承。审核模型与标题模型的用量也计入。

`GET /api/conversations/{id}/usage` 返回本会话的总量与缓存命中率，按成员、用途、任务、模型分组，以及人工确认/自动通过次数和工具调用失败数。工作日志顶部显示“本会话”的 tokens 与缓存命中率，展开后看分组明细；所有会话累计只作为附注。

## 工具按角色分组暴露

`backend/app/agents/tool_profiles.py` 把工具分组（files、commands、browser、documents、team、delivery、environment、memory、goals 等），每个角色只默认看到核心组；所有工具仍可执行。

- 所有 Worker 默认都有沟通工具（`collaboration_send`、`collaboration_request`、`collaboration_inspect_request`、`collaboration_wait`），测试发现缺陷时可直接向前端/后端请求修复；Worker 使用精简版 `collaboration_request`（不含 Leader 的规划字段）。
- 任务决定额外分组：声明服务时开放命令与登记、端到端验证开放浏览器检查、声明文档开放文档编辑；在任务开始时确定，整个任务内保持不变，以免改变工具列表破坏缓存。
- 交付动词每个任务只有一个：目标任务用 `submit_task`，其他用 `collaboration_transition`；请求过同事修复后可用 transition 验收。
- `enable_tools(groups=[...])` 按组加载，加载后本任务内一直可用；没有可加载的组时不再展示。
- 工具描述精简，详细规则只在提示词中出现一次；工具 schema 去掉自动生成的 title。

## 提示词模块化

`prompts/collaboration.md` 拆为 `prompts/modules/` 下的 shared、documents、worker、environment、leader 模块，按角色组合：所有角色相同的平台规则在最前，随后是角色提示、技能和角色模块，便于跨角色复用缓存前缀。开发工具说明只给能执行命令的角色。

目标提醒不再每次调用临时追加，而是在内容变化时写入上下文一次（上下文压缩后自动补回），旧的提醒在折叠时替换为短存根。

固定开销（估算 token，约为英文真实 token 的两倍）由 `scripts/benchmark_overhead.py` 测量，`tests/test_overhead_budget.py` 设定上限：

| 角色 | 之前 | 现在 |
|---|---|---|
| Leader | 24.3K | 12.7K |
| backend | 20.5K | 12.4K |
| frontend / testing | 21.4K / 21.9K | 13.4K / 13.5K |
| product / designer | 18.4K | 9.6K |

## Leader 延续会话

Leader 每轮结束都保存自己的模型视图（含工具调用与结果），下一次唤醒在此基础上追加新的用户消息，不再只凭聊天记录重建；自己的上一条回复不会重复。每轮的协作状态只在新会话时给全量，之后只给“自上一轮以来的变化”（任务状态变化、不再待办的任务、新/更新文档、新决策、环境变化），并注明这些状态为当前权威状态。上下文增长由分层记忆控制，前缀只追加，可整体命中缓存。

## 返工延续上下文

任务在验收中被退回（review → queued）时，负责人延续原来的模型线程，追加验收反馈继续工作；交付已合并进新的工作区快照。工作记录中可看到 fresh / continue 两次执行。

## 模型中断与容错

- **诊断**：Worker 记录每次模型调用流式返回的片段数、最后片段类型（推理、正文、工具参数及工具名）和空闲时长；调用中断时写入工作记录（`stalls`），导出时显示“在生成哪个工具的参数时中断”。
- **回退链**：同一调用先按 `WDRUNGE_MODEL_RETRIES` 重试，仍失败则用非流式调用重试一次，再用 `QWEN_FALLBACK_MODEL`（如已配置）重试一次；参数错误等非瞬时错误不重试。Worker、Leader 与摘要调用都使用。
- **输出**：`QWEN_MAX_OUTPUT_TOKENS` 可限制单次输出；提示词要求长文件、报告和文档分多次写入。
- **熔断**：90 秒内出现 `WDRUNGE_PROVIDER_BREAKER`（默认 4）次瞬时失败时，调度器 60 秒内不再启动新任务，避免所有任务同时耗尽重试。

## 按任务选择模型

`Agent.model_name(purpose, scale)` 依次使用：用途覆盖（如 `WDRUNGE_MODEL_SUMMARY` 用于上下文摘要）、角色覆盖（如 `WDRUNGE_MODEL_PRODUCT`）、小规模目标的任务（`WDRUNGE_MODEL_SMALL_TASKS`，Leader 除外）、默认 `QWEN_MODEL`。所用模型记录在用量中，可按会话比较质量与成本。

## 回归覆盖

`tests/test_session_usage.py`、`tests/test_overhead_budget.py`、`tests/test_leader_session.py`、`tests/test_resilience_routing.py`、`tests/test_approval_policy.py`（允许所有操作）、`tests/test_tool_validation.py`（工具分组）。
