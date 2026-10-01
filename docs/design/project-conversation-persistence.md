# 项目、对话与团队记录持久化

Wdrunge 的导航和存储层级为 `Project → Conversation → Message/Event`。项目保存名称与唯一工作区路径；一个项目可以包含多段互相隔离的对话。对话恢复模型历史时，由后端读取数据库，不再接受浏览器作为完整历史的来源。

SQLite 表之间采用逻辑外键。`project_id`、`conversation_id` 和 `message_id` 均建立索引或唯一索引，但数据库 schema 不声明 `FOREIGN KEY`。父记录存在性、幂等校验和相关写入由 `ChatStore` 在事务内完成。

消息流的持久化顺序是先写库、再发送 SSE。用户消息在模型调用前写入；派活消息、已读、完成反应和工具活动在发送给浏览器前写入；Leader 回复在流式过程中保存草稿并在结束时标记为 `completed`。浏览器断线或生成失败时，已有回复会标记为 `cancelled` 或 `failed`。

成员的 LangGraph checkpoint 使用同一个 SQLite 文件中的 checkpoint 表。`agent_sessions` 保存对话与成员线程 ID 的映射，`leader_checkpoints` 保存 Leader 的结构化交接，因此后端重启后可以继续同一项目对话。
