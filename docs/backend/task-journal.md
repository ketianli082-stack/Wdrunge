# 任务工作记录与中断续跑

## 工作记录

每个协作任务有一份工作记录（`collaboration_records`，`kind='journal'`，ID 为任务 ID），跨重试、上下文重置和应用重启保存。实现在 `backend/app/collaboration/journal.py`。它是平台数据，不写入项目仓库；需要时可在任务详情中导出为 Markdown（`GET /api/projects/{project}/collaboration/requests/{request}/journal`）。

- **平台记录的事实**：Worker 每轮工具调用后（`after_tools`）从工具结果提取写入/读取的文件及哈希、共享文档版本、后台进程与端口、命令最近退出码、浏览器验证结果、尚未解决的工具失败。按消息 ID 幂等合并，与上下文台账使用同一提取逻辑（`context_fold.record_facts`）。
- **成员笔记**：`collaboration_journal(kind, text)`，kind 为 done/next/blocked/decision/evidence；`checkpoint_context` 的交接内容自动记为 checkpoint 笔记。仅在执行中的任务里可用，Leader 不显示该工具。
- **执行尝试**：每次执行记录序号、上下文模式（fresh/continue）、使用的模型线程、结束方式（已交付、失败、中断后自动续跑、取消、挂起等待、交付冲突、结束未交付）和原因；同时记录最近一次工具失败。

## 使用

- 新一次执行（重试、新上下文）的 handoff 附带工作记录摘要：之前尝试如何结束、最近工具失败、已记录的事实与笔记，要求不重做已完成工作并核实依赖的事实。
- Leader 的 `collaboration_inspect_request` 返回任务连同工作记录摘要，可直接看到提交失败等原因，而不是只看到状态。默认视图完整包含交付结果（运行时最多保留 4000 字符），只截断调用者自己写的任务正文，验收无需 `detail='full'`。
- 协作面板的任务详情显示“工作记录”（执行尝试、最近失败、笔记、事实）和导出链接；快照只带摘要，不含内部去重数据。
- 执行中任务收到的提醒不再重复当前任务的完整目标文本。

## 中断续跑而非重来

- **模型调用重试**（`backend/app/agents/resilience.py`）：流式无数据超时、连接中断、429、5xx 等瞬时错误在同一图状态上重试（`WDRUNGE_MODEL_RETRIES`，默认 3 次，指数退避），不重复工具副作用；参数错误与上下文超限不在此重试。Worker、Leader 与摘要模型调用都使用。`QWEN_STREAM_CHUNK_TIMEOUT` 可调整流式无数据超时。
- **自动续跑**：重试仍失败的瞬时错误不再把任务置为失败，而是排队并标记续跑（每个任务最多自动 2 次）；应用重启或执行租约过期打断的任务同样标记续跑。续跑时恢复工作记录中该任务上次的模型线程（collaboration 任务不在会话表中，原先每次重试都只能新开上下文），以 continue 模式追加一条简短的续跑说明和最新工作记录，并为中断时没有结果的工具调用补上“已中断、效果未知”的错误结果。线程状态不存在时退回新上下文加工作记录。
- 超出自动续跑次数后任务置为失败并标记 `resumable`；Leader 或用户把它重新排队时同样续跑原上下文。

## 回归覆盖

`tests/test_task_journal.py` 覆盖事实/笔记/尝试记录与交接摘要、导出、任务范围与 Leader 可见、瞬时错误识别与重试、续跑标记与次数上限、悬空工具调用补全，以及一次真实调度：模型流中断后自动排队、恢复同一线程完成交付且不重复任务文本。`tests/test_environment_ui.py` 覆盖任务详情中的工作记录与导出链接。
