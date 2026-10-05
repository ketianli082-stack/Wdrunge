# Blog2 问题修复方案与实施记录（2026-10-05）

依据：`2026-10-04-blog2-run-analysis.md` 第 10 节，以及你的批注（P0-1 摘要改用 qwen3.7-plus，失败后新开干净上下文；P0-2 分级保留；P3-2 暂不做；其余批准）。

原则：从根因修，不针对 Blog2 的具体文件、角色或文档写特例。每一项都说明“根因 → 通用修法 → 验证方式”。

---

## P0-1 摘要失败不再让任务失败

**根因（已用真实接口验证）**
- 推理（thinking）token 计入 `max_tokens`：qwen3.7-plus 回答“17 是质数吗”，输出 251 token，其中 246 个是推理 token（`output_token_details.reasoning`）。摘要的上限 12,000 被推理 token 用完，`finish_reason=length`，没有可用文本，于是抛出 `ContextBudgetError`，任务失败。
- 摘要没有降级路径：只要模型摘要不可用，就直接抛错。

**修法**
1. 摘要模型：`.env` 设置 `WDRUNGE_MODEL_SUMMARY=qwen3.7-plus`。`purpose='summary'` 的调用默认关闭思考（`enable_thinking=false`，可用 `WDRUNGE_SUMMARY_THINKING=1` 打开）。上限改为 `target×2 + 4096`，留出余量。
2. 摘要返回 `length` 或空文本时，先用一半的字数目标重试一次。
3. 仍然失败就改用**平台交接**，不调用模型，也不抛错：直接新开干净上下文继续当前线程。

**平台交接（不调用模型）携带的内容**，按顺序：

| 部分 | 内容 | 理由 |
| --- | --- | --- |
| 系统提示词 | 原样 | 规则不变 |
| 任务账本 | 写过/读过的文件（含 sha）、文档版本、进程、检查结果、未解决的失败 | 平台已有的确定事实 |
| 任务输入 | 最近一条任务消息（Assigned task + handoff）原样；更早的项目交接、历史摘要原样（超长时截断） | 目标和约束不能靠模型复述 |
| 交接说明 | ① 为什么换了上下文；② 本任务 journal 笔记（done/next/blocked/decision/checkpoint，最近 12 条）；③ 自己说过的话：被丢弃历史中最近 8 段非空的模型文字（各 ≤400 字符），这是它的计划和决定；④ 最近 15 次工具调用（名称、精简参数、成功/失败）；⑤ 指针说明：旧结果可用 `recall_context` 按 ID 找回，文件都在磁盘上，按需重读 | 让模型知道做到哪一步、下一步做什么 |
| 最近原文 | 最近的若干轮原样保留（不超过 P0-2 的原文预算和剩余预算），从完整的模型轮次开始，不留孤立的工具结果 | 正在用的材料不丢 |

**效果**：`ContextBudgetError` 只在“系统提示词本身超预算”这种配置错误时出现。

## P0-2 折叠按内容量分级，而不是按轮数

**根因**：只保留最近 3 轮原文，3 轮装不下实现任务的工作集，所以出现“读 → 被折叠 → 再读”的循环。

**修法**：以“轮”（一条模型消息加上它的工具结果）为单位，从新到旧累计**原始**估算 token（字节/2）：

| 层级 | 范围（默认，可用环境变量调整） | 工具结果 | 调用参数 |
| --- | --- | --- | --- |
| 原文层 | 最新的轮次累计 ≤ 30,000 估算 token（`WDRUNGE_FOLD_VERBATIM_TOKENS`）；当前轮始终原样 | 原样 | 原样 |
| 摘录层 | 再往前累计 ≤ 15,000（`WDRUNGE_FOLD_EXCERPT_TOKENS`） | 关键事实（路径、偏移、sha、结构行、退出码、错误）+ 开头 600 字符 + 提示“已截断，共 N 字符，用 recall_context 或按偏移重读” | 长字符串只留 120 字符开头；单个调用参数仍 >1,500 字符时全部换成占位 |
| 指针层 | 更早的 | 一行指针：工具名、资源（路径/章节/偏移）、大小、状态、message ID | 长字符串全部换成占位，只留字段结构 |

**数值依据**：轮次不可拆，所以实际原文量落在 24K–30K。估算 token 按 UTF-8 字节/2，中文约 1.5 估算 token/字，代码约 0.5 估算 token/字符。30K 估算 token ≈ 2 万中文字或 6 万字符代码，能装下 Blog2 前端写样式所需的设计 §2+§3（约 1.2 万字，≈18K）加契约字段（≈4.5K），也能装下后端写路由层所需的各文件签名部分。摘录层 15K 原始量折叠后约剩 1/4 到 1/3。指针每条约 150 字符。

**其余不变**：同一资源的新结果替换旧结果、最新大纲保留章节 ID、批量折叠（可回收 ≥12K 才改写历史，保护缓存）、任务账本。

## P0-3 读循环检测

- **只读**的定义按工具语义，不按角色：读文件、读文档、搜索、目录、找回、查看类协作工具；`execute_command` 只有在命令本身是只读工具（cat/sed -n/head/tail/grep/rg/ls/find/wc，含 `sh -c` 包装）时才算只读。其他调用（写文件、发布、提交、运行测试/服务、浏览器验证……）都算推进。
- 连续 N 轮只读（`WDRUNGE_READ_STREAK`，默认 12）时注入一次提醒；到 2N 轮时自动换成 P0-1 的平台交接（干净上下文 + 最近原文），并再提醒一次。之后每满 N 轮重复同样的处理。
- worker 通用提示词加一段“边读边写”：读到下一个文件需要的内容就写它，不为一次写完先读全部；旧结果会被折叠，需要时读单个章节或偏移。

## P1-1 `full_read()` 误判

**根因**：在已经折叠的文本上判断是否整读。
**修法**：折叠时把原文的事实记录到消息的 `additional_kwargs['fold']`（层级、原始字符数、原始估算 token、是否整读）。`supersession()` 读这个记录；对没有记录的已折叠消息（旧 checkpoint），一律不当作整读。`additional_kwargs` 不会发给模型（已核对 langchain_openai 的序列化逻辑）。

## P1-2 沙箱：`.git`/`.agents` 等保护名在依赖目录里放行

**根因**：`(deny file-write* (regex "/(\.git|…)(/|$)"))` 匹配所有可写目录的任意深度，包括 scratch（HOME、各种包管理器缓存）和 `site-packages`、`node_modules`。
**修法**：拦截规则只作用于工作区和数据目录（scratch 是沙箱自己的 HOME，不受影响），并在依赖树里放行（`node_modules`、`site-packages`、`dist-packages`、`__pypackages__`、`.pnpm-store`、`.yarn`、`.cache`）。Seatbelt 后出现的规则优先，所以顺序是：放行写 → 拦截保护名 → 放行依赖树 → 拦截受保护路径（.env、应用数据库等，始终最后）。用真实的 sandbox-exec 验证。

## P1-3 文档 ready 的触发点

- `task_brief`：凡是本任务要发布的文档，都提示“全部章节写完后发布时加 ready=true，之后最多一轮通读修正”，不再只在“有人等待”时提示，也不再只以 API 契约举例。
- 发布回执：某个声明文档这一版第一次没有剩余的待填写占位（上一版还有，或者上一版就是草稿），并且尚未 ready 时，回执里提示一次“已全部填写，可以 ready=true”。只在跨过这个点的那一次提示，避免反复。

## P2-1 失败重试续接上下文

- worker 执行中抛出的运行时异常（不是任务本身失败）一律标记为可续接；Leader 重试时恢复原线程，原线程的上下文由 P0-1/P0-2 正常压缩。
- 防死循环：一次“失败后续接”的尝试如果又因非瞬时错误失败，下一次重试改为全新上下文（携带 journal）。
- Leader 重试的结果里写明下一次尝试是 `continue`（续接）还是 `fresh`（全新上下文）。

## P2-2 提醒去重

**根因（已在 Blog2 的 checkpoint 中确认）**：提醒只有在“模型调用前倒数第二条消息”时才会被确认已读。目标状态变化时，goal 消息追加在提醒后面，提醒就永远不会被确认，下一轮再注入一次。
**修法**：提醒消息带上 `event_ids`，模型返回后确认上一条模型消息之后注入的所有提醒，不依赖位置。

## P2-3 发布参数在历史里折叠

已包含在 P0-2 的调用参数分级里，对所有工具通用，包括 `collaboration_publish` 的 `changes`。

## P2-4 `ready`/`based_on` 可放在顶层；错误信息附示例

- `Publish` 的顶层 `ready`、`based_on` 作为每个 change 的默认值（与已有的 `summary`、`base_version` 同一机制）。
- `changes` 字段带一个最小合法示例，参数错误时 `example` 直接给出。

## P3-1 遥测

1. **审核调用记账（已验证根因）**：审核调用发生在 worker 的流式执行里，`ainvoke` 被自动转成流式，而 DashScope 默认不在流里返回用量，所以 85 次审核都没有记账。修法：审核使用非流式调用并开启 `stream_usage`；所有 Agent 模型都开启 `stream_usage`。
2. **工具失败记账**：工具抛异常时（参数校验失败、not_found 等），事件里没有 ToolMessage，所以没写进 `tool_calls`。修法：`on_tool_error` 也记录，结果写成规范化的失败 JSON。
3. **推理 token**：`agent_usage` 新增 `reasoning_tokens` 列（迁移 0010），来源是 `output_token_details.reasoning`。

## P3-2 交付检查拦截团队成员名

暂不做（按你的批注）。

---

## 实施记录（2026-10-05）

所有改动都在工作区里，**没有提交**。

### 改动文件

| 项 | 文件 |
| --- | --- |
| P0-1 | `app/agents/memory.py`（`summarize_chunk` 重试、`SummaryUnavailable`、`platform_handoff`/`handoff_note`/`handoff`、`fallback`）、`app/agents/base.py`（摘要关闭思考、`stream_usage`）、`backend/.env` 与 `.env.example`（`WDRUNGE_MODEL_SUMMARY=qwen3.7-plus`） |
| P0-2、P1-1、P2-3 | `app/agents/context_fold.py`（`tier_bounds`、`facts_of`、`fold_result` 三层、`pointer_facts`、`shrink_args`、`full_read` 改用原文事实） |
| P0-3 | 新增 `app/agents/progress.py`；`app/agents/worker.py`（提醒与平台交接）；`prompts/modules/worker.md`（边读边写） |
| P1-2 | `app/execution/sandbox.py` |
| P1-3 | `app/collaboration/planning.py`（task_brief、`ready_hint`、`became_complete`） |
| P2-1 | `app/collaboration/runtime.py`、`service.py`（`resume_after_failure`）、`tools.py`（`next_attempt`） |
| P2-2 | `app/agents/worker.py`（`event_ids`、`reminder_events`、按位置无关的确认） |
| P2-4 | `app/collaboration/payloads.py`、`app/agents/tools/validation.py`（`EXAMPLE_CALLS`）、`app/collaboration/tools.py` |
| P3-1 | `app/execution/permissions.py`（审核非流式）、`app/agents/worker.py`（`on_tool_error` 记账）、`app/agents/usage.py`、`app/storage.py`、`app/main.py`、`alembic/versions/0010_reasoning_tokens.py`、`openapi.json`、`frontend/src/generated/*` |
| 文档 | `docs/backend/context-memory.md`、`cost-and-resilience.md`、`progressive-documents.md` |

### 测试

- 后端全量：**420 passed，1 failed，2 skipped**。失败的是 `test_overhead_budget`，修改前就失败：后端固定开销 13,663 > 13,500，来自工作区里已有的环境/提示词改动。修改后降到 13,594（重写了更短的 `MEMORY_PROMPT`，抵消了新加的“边读边写”），其他角色都在预算内。需要你决定：放宽后端预算，还是精简 `environment.md`。
- 新增测试：`tests/test_context_tiers.py`（分层、小窗口、`full_read` 回归、发布参数折叠、摘要截断后重试再转平台交接、摘要关闭思考、只读判定、worker 提醒与交接、提醒确认）；另在 `test_task_journal.py`（失败后续接一次）、`test_native_sandbox.py`（真实 Seatbelt）、`test_document_release.py`（ready 提示）、`test_tool_validation.py`（顶层 ready、示例、失败工具记账、审核非流式）、`test_session_usage.py`（推理 token）中各加了用例。原有测试中按“3 轮”编写的用例改为固定小的分层预算，测试的仍是同样的机制。
- 前端 `tsc --noEmit` 通过。

### 真实环境验证

| 验证 | 结果 |
| --- | --- |
| 流式图中的审核调用（真实接口，qwen-flash） | 默认 `ainvoke` 的 usage 为 `None`；`disable_streaming` 或 `stream_usage` 后有 usage，确认了 85 次审核漏记的根因 |
| 推理 token（qwen3.7-plus，开启思考） | 输出 251 token，其中推理 246，计入输出和 `max_tokens`，确认摘要被截断的根因 |
| 新的摘要调用（qwen3.7-plus，关闭思考，约 1.5 万输入） | 11.5 秒，输出 516 token，推理 0，`finish_reason=stop`。Blog2 失败的摘要约 250 秒、12,000 token |
| 沙箱（真实 sandbox-exec） | `site-packages/fastapi/.agents`、`node_modules/pkg/.git`、`.cache/.../.git`、scratch 下的 `uv-cache/.../.git` 可写；项目里的 `.git`、`server/.git`、`.agents`、`docs/.codex`、`.ssh` 仍被拦截 |
| 用新代码折叠 Blog2 的真实 checkpoint | 旧格式的折叠消息兼容，重复折叠结果不变 |
| 用原始工具结果重放 Blog2 线程 | 再次读取同一文件/章节时，之前的副本仍是原文的次数：后端第二轮 84 次中旧规则 36 次、新分层 62 次；后端第一轮 25 次中 13 → 21；前端第二轮 34 次中 4 → 12。前端第一轮按很小的切片换偏移读同一文件，即使旧规则下原文还在也照样重读（53/73），这一类要靠只读循环提醒解决 |

**需要如实说明的一点**：按同样的调用序列重放，单次调用的上下文并没有变小（估算约 6.7 万～7.6 万，约合 3.4 万～5 万真实 token；Blog2 实际约 4.6 万～4.8 万）。区别在于其中约 2.5 万是可用的原文，而不是 600 字符的摘要。节省来自**调用次数减少**：不再重读、不再因摘要失败冷启动、读循环会被打断。这一点只能用真实运行来验证。

### 与方案的偏差

- P1-3 的 ready 提示没有和 `check_delivery` 联动，只在“草稿占位第一次全部填满”时提示一次。`check_delivery` 要扫描整个工作区，放进每次发布回执里太重，交付时也已经会运行它。
- P2-2 修的是确认逻辑（根因），没有另做内容去重。同一上下文里不会再重复注入；新线程第一次看到未确认的提醒是正常行为。
- P3-1：抛异常的工具调用拿不到 `tool_call_id`，日志里用 `run:<run_id>` 作为 ID。

### 建议的下一步

用同样的需求跑一次 Blog3，重点看：调用次数和总 token；`agent_usage` 里 `purpose='summary'` 的耗时与是否出现 `handoff` 阶段（`context_compactions.stage`）；只读提醒触发了几次；`purpose='review'` 和 `reasoning_tokens` 是否已有数据；FastAPI 是否装上了最新版。
