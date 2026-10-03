# EmptyFolder「Vue + Python Hello World」运行复盘报告

> 分析对象：`~/Library/Application Support/Wdrunge/wdrunge.db`（对话 `2cc564fe…`，项目 EmptyFolder）
> 分析方式：只读快照（`sqlite3 .backup`）＋ LangGraph `writes` 表反序列化出每个 worker 的完整工具调用与返回；未修改任何代码。
> 时间：文中时间均为本地时间（UTC+8）；数据库内为 UTC。

---

## 0. 结论摘要

| 问题 | 结论 |
| --- | --- |
| 8.2M token 是否都属于这个 Hello World？ | **不是。** `agent_usage` 表没有项目/对话维度，UI 统计的是全库累计：**EmptyFolder 本次 ≈ 5.25M**，另外 **≈ 2.91M 来自同日上午的 BBBlog 运行**（那批记录没有缓存数据）。 |
| 84.5% 缓存命中率口径 | 只统计“有缓存数据”的调用，恰好等于 EmptyFolder 这批：4,273,408 / 5,055,346 = **84.5%**。和 8.2M 不是同一口径。 |
| 5.25M 合理吗？ | **不合理，约为合理值的 8～10 倍。** 一个 Hello World 合理消耗大约是 40 次模型调用、0.5～0.7M token；实际是 209 次调用。浪费主要来自：任务重跑（后端 3 次、测试 2 次）、过度验证、文档按小节零碎读取、所有调用都要带上的约 9K 固定提示词。 |
| 84.5% 命中率合理吗？ | **对 agent 循环来说属于正常偏低。** worker 线程是 86%，Leader 是 72%（每次唤醒都从新上下文开始，属设计使然）。未命中的约 62 万 token 中，约 **1/3（≈21 万）是“上下文折叠（fold）改写了历史前缀”造成的**，这部分可以优化。 |
| 工具失败（45/402，11.2%）根因 | 平台缺陷 10、沙箱网络语义 7、**预期中的失败被记成失败 13**、模型调用错误 13、Leader 无法溯源 2。真正需要修的是前两类，加上“预期失败”的统计口径。 |
| 测试为什么慢 | ① **33 次人工审批**（全场 52 次），绝大多数是误报：权限审核把 `-H "Origin: http://localhost:5173"` 这样的**请求头值当成了访问 5173 端口**，同时也**识别不出 agent 自己用 `http.server 5173` 启动的服务**；② 后台服务**默认 120 秒超时会被杀**；③ 第 1 轮跑了 35 分钟后撞上模型流超时，**整轮全部作废并从零重来**。 |
| `StreamChunkTimeoutError` | 模型 `qwen3.8-max` 收到 195 个 chunk 后 120 秒没有新数据（langchain-openai 1.6.6 默认 `stream_chunk_timeout=120s`），而模型调用层**没有任何重试**。异常直接让整个请求 `failed`，Leader 重新排队时又用 `context_mode='fresh'`，第 1 轮的 **≈1.76M token 和 33 分钟工作全部丢失**。 |

---

## 1. 数据口径与运行中途的代码变更

### 1.1 线程映射

| 线程 | 角色/轮次 | 模型调用 | 工具调用 | 输入 token | 缓存命中 | 输出 token | 结局 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `7240a5…` | 后端 #1 | 5 | 9 | 56.7K | 69.5% | 9.8K | 平台重启，挂起 |
| `e7dacf…` | 后端 #2 | 34 | 84 | 818.7K | 84.7% | 43.3K | submit 两次失败，平台停机 |
| `2ae4f4…` | 后端 #3 | 29 | 48 | 592.9K | 85.6% | 12.9K | 交付通过 |
| `4761f5…` | 前端 #1 | 28 | 54 | 679.3K | 86.6% | 24.1K | 交付通过 |
| `aa9c9c…` | 测试 #1 | 52 | 111 | 1,704.1K | 87.8% | 55.9K | **StreamChunkTimeout，作废** |
| `fef6c0…` | 测试 #2 | 27 | 55 | 642.1K | 84.8% | 16.7K | 用户停止 |
| —— | Leader | 34 | 42 | 561.6K | 71.8% | 34.1K | —— |
| **合计** | | **209** | | **5.06M** | **84.5%** | **≈197K** | |

另外 4 个线程（`3dc45c…` `78bca1…` `2e5dea…` `c6fc95…`）属于 BBBlog 运行，不在本报告范围内，但它们的 token 计入了 8.2M。

### 1.2 运行期间发生的代码变更（按文件 mtime 推断）

| 本地时间 | 事件 | 影响 |
| --- | --- | --- |
| 19:25～19:35 | 后端服务重启 | 后端 #1 正在等 curl 的人工审批，活动被标记为“重启后已挂起”，随后整轮重开 |
| 19:55～20:52 | 平台停机约 1 小时；20:29 修改了 `service.py` / `workspaces.py` | 修复了“`submit_task` 在隔离快照里找不到 `collaboration_publish` 发布的文档”这个问题。**这 1 小时不是 agent 耗时** |
| 20:52 | 平台重启（`execution-token` 在此时生成） | 后端 #3 开跑 |
| 21:11～21:16 | 修改 `storage.py`，新增 `0007_tool_calls`，整体迁移 `app/tools → app/agents/tools`，修改 `worker.py` / `runtime.py` | 测试 #1 运行期间改了代码。`tool_calls` 表**当时不存在**，所以 Leader 那 2 次失败的参数无法还原 |

> 因此，判断“慢”的时候要扣掉两段人为停机（约 10 分钟和约 57 分钟）。agent 的实际净耗时：后端约 26 分钟（#2 20 分钟 + #3 6 分钟），前端 9 分钟，测试 35 + 11 分钟。

---

## 2. 时间线（本地时间）

```
19:19  用户：“写一个最简单的 vue+python 的 hello world 网页”
19:20  Leader：建目标（6 条验收标准）→ 3 个任务（第 3 次 create_task 失败一次后重试成功）
19:23  后端 #1：写出 11.5K 字符的 app.py → curl 等待审批时平台重启 → 挂起
19:35  后端 #2：沙箱拒绝绑定 0.0.0.0（未带 network=true）→ 发现 8000 被 Wdrunge 自己占用 → 改用 8765
19:45  后端 #2：collaboration_publish 发布契约 v1
19:48  后端 #2：submit_task ① 参数写错 base_version ② “Expected artifact is missing: docs/api-contract.md”（平台缺陷）
19:49  Leader 误判“后端还没 submit”，催后端补交
19:54  后端 #2：试图用 write_project_file 重写契约 → 被策略拒绝；read/browse docs/ → 快照里没有该文件
19:55  —— 平台停机修代码（约 57 分钟）——
20:53  后端 #3：从零开始，重新读代码、启动 4 次服务、复核契约 → 20:59 submit 成功
21:00  Leader 验收后端；前端开跑
21:09  前端交付（含桩服务反证与失败路径截图）；21:10 Leader 验收
21:09  测试 #1 开跑：33 次人工审批，服务被 120 秒超时杀掉，4 轮浏览器验证，写了大量证据文件
21:44  测试 #1：StreamChunkTimeoutError → 请求 failed
21:45  Leader 重新排队测试（attempts 归零，context fresh）
21:45  测试 #2：在新目录 independent-e2e/ 下重做一遍
21:56  用户停止
```

---

## 3. 工具调用失败的根因

本对话共 402 次工具调用，失败 45 次（11.2%）。逐条对照返回内容后分为 5 类：

| 类别 | 次数 | 说明 |
| --- | --- | --- |
| A. 平台缺陷/设计问题 | 10 | 需要改平台 |
| B. 沙箱网络语义不清 | 7 | 需要改工具描述或默认值 |
| C. 预期中的失败被记成失败 | 13 | 需要改统计口径或工具能力 |
| D. 模型调用错误 | 13 | 一部分可以通过工具设计规避 |
| E. Leader 失败（无法溯源） | 2 | 当时还没有参数日志 |

### A. 平台缺陷（10 次，影响最大）

1. **`submit_task` 的产物校验和共享文档存储冲突**（后端 #2，1 次 + 引发 3 次连锁失败）
   - Leader 把 `docs/api-contract.md` 同时定为“用 collaboration_publish 发布的共享文档”和 `artifacts` 期望产物。
   - 当时的 `publish_artifacts` 只到隔离快照 `work/` 里检查，而 publish 的文档在项目根目录/文档库里，所以报 `Expected artifact is missing`。
   - 后端随即尝试 `write_project_file` 写这个 md → 被策略“共享 Markdown 必须用 collaboration_publish”拒绝；`read_project_file` → 文件不存在；`browse_directory docs` → 返回 **`Not a directory`**（这条错误信息本身就有误导性，应该是 not found）。
   - 后端陷入一个**规则上不可能满足**的循环。Leader 只看到 `workspace_published=false`，看不到 worker 的工具错误，因此误判成“后端没提交”。
   - 20:29 的代码改动（`managed` 路径改为去项目根目录校验，并新增 `sync_published`）已经修复了这一点。
2. **平台重启后，正在等待审批的命令被挂起，整轮重跑**（后端 #1，1 次）。重启后用 `fresh` 上下文重跑，之前写好的代码在快照里还在，但模型没有记忆，只能重新读一遍。
3. **`refresh_api_contract` 被暴露给了非 FastAPI 项目**（1 次）。这个工具只适用于固定布局，在本项目必然失败。更糟的是，它的名字和报错还被写进了交付文档（见第 6 节）。
4. **后台进程默认 `timeout=120` 秒，到点被 SIGKILL**（测试 4 次，前端 1 次间接）。`execute_command(background=true)` 沿用了前台命令的默认超时 120 秒，agent 启动的 dev server 在 2 分钟后被杀（前端日志 `exit_code=-9, timed_out=true`；测试截图备注“服务被超时终止”），随后的 curl 和 browser_verify 连锁失败。对后台服务来说，这个默认值是错的。
5. **测试 #2 打开页面时服务尚未就绪**（1 次 `navigation_failed`）。缺少“等端口就绪”的原语，agent 只能靠轮询日志。

### B. 沙箱网络语义（7 次，三个角色各自踩了一遍）

- 现象：不带 `network: true` 时，`python3 backend/app.py` 报 `[Errno 1] Operation not permitted`，curl 访问 127.0.0.1 报 `exit 7`。
- 根因：
  1. `execute_command` 的说明只写了 “Network access is reviewed too”，**没有说明“监听端口或访问 localhost 也需要 network=true”**。
  2. 后端自己写的 app.py 把所有 `OSError` 都提示成“端口可能已被占用”，进一步误导了排查。
  3. 每次重跑都是 fresh 上下文，**同一个教训在后端 #2、后端 #3、前端、测试 #1 里各学了一遍**。
- 本质上这是平台知识，应该由平台提供，而不是让每个 agent 自己试错。

### C. “预期失败”被记成失败（13 次，占失败数的 29%）

- 端口探测（`curl … 8080/8765` 期望连不上）、停服务后的确认探测、`lsof` 返回 1（表示没有监听者，正是想要的结果）、故意让后端不可用去验证错误页的 `browser_verify`。
- 当前的 `tool_result_failed` 只看退出码和 passed，导致活动日志里的失败率虚高，也让 agent 自己需要额外解释（前端在交付说明里专门写了一段“这次 passed=false 属于预期”）。
- `browser_verify` 缺少 `expect_page_errors` / `expect_network_failure` 这类断言，没法表达“我就是要验证失败路径”。

### D. 模型调用错误（13 次）

| 错误 | 次数 | 可否通过平台规避 |
| --- | --- | --- |
| `submit_task` 传了 `base_version`（与 publish 参数混淆），字符串化的 JSON | 1 | 校验错误已经给出了 allowed_fields，模型下一次就改对了 ✔ |
| `manage_process` 抄错 32 位进程 ID（`…2131a1e5` vs `…2131d141e5`） | 1 | 可以支持按 `name` 操作进程，避免抄长 ID |
| `execute_command` 漏传 `command`（只给 arguments） | 2 | —— |
| `lsof` 语法错误（`tcp:8765 -s tcp:LISTEN`） | 3 | 可以提供 `port_status` 小工具 |
| macOS 上用 GNU 的 `ls --time-style` | 1 | `detect_environment` 可以返回 OS/工具链差异 |
| browser_verify 选择器、断言文案靠猜，没有先读源码 | 3 | 提示词中加“断言前先读 DOM/源码” |
| 用 `write_project_file` 写共享 md | 2 | 其中后端那次是被平台缺陷逼出来的 |

### E. Leader 的 2 次失败（`create_task` 19:22:23、`collaboration_transition` 20:58:52）

当时 `0007_tool_calls` 还没上线，Leader 的工具参数和返回都没有落库，**无法溯源**。从时间点看，后一次很可能是在后端产物尚未发布时就尝试流转状态，随后 `complete_task` 成功。现在已经加上的 tool_calls 日志可以覆盖这类问题。

---

## 4. Leader 下发任务的质量

**评分：6.5 / 10。拆解和依赖关系正确，但任务说明过重，并且埋下了两个冲突。**

做得好的地方：
- 范围控制意识强：主动没有拉产品和美工，明确“不引入数据库、构建链、视觉设计”。
- 依赖链正确（后端 → 前端 → 测试），每个任务都有可检验的验收标准。
- 处理端口冲突得当：把 8765 记录为持久决策并下发给下游；测试超时后在原任务上重试，没有建重复任务，还补充了“分阶段落盘”的建议。
- 验收时逐条对照证据写入 `evidence`，可审计性好。

问题：
1. **指定了与平台自身冲突的端口 8000**。Wdrunge 后端自己就运行在 8000，Leader 不知道这一点，导致后端花了约 10 次调用排查。平台应该在 handoff 中告诉 agent 哪些端口被保留。
2. **artifacts 与共享文档定义冲突**：`docs/api-contract.md` 既要求 publish，又被列为快照内的期望产物（叠加平台缺陷，造成了后端 #2 的死循环）。
3. **任务过度规格化，放大了成本**。Hello World 的目标定了 6 条标准，三个任务分别有 5、5、7 条验收标准，还要求：OPTIONS 预检、错误约定、契约包含“请求头/错误约定/CORS 说明/启动命令”、前端“反写死证明”、测试“停后端刷新”和“异常路径”。这直接导致：
   - 一个接口写出了 12KB 的契约文档；
   - **前端和测试做了几乎相同的验证**（桩服务反证、失败路径截图各做了一遍）；
   - 测试一轮 111 次工具调用。
4. **串行等待契约其实没有必要**：Leader 在任务描述里已经写死了接口（`GET /api/hello → {"message":"Hello World"}`），前端完全可以和后端并行开发，再由测试做联调。
5. **单次任务描述过长**（后端任务约 1,000 字），而且每次 Collaboration reminders 都会**整段重复注入**（后端 #3 的第一条消息里，任务正文出现了两次）。

---

## 5. 代码质量

**评分：正确性 9 / 10，与需求的匹配度 4 / 10（严重过度工程）。**

| 文件 | 大小 | 评价 |
| --- | --- | --- |
| `backend/app.py` | 12.4KB / 约 340 行 | 正确、健壮：错误 JSON、405、HEAD、双栈 IPv6、OpenAPI 内嵌、优雅退出。**但一个返回固定 JSON 的接口，30 行就够了。** |
| `backend/openapi.json` | 2.1KB | 与 app.py 内嵌的 OPENAPI 内容重复 |
| `frontend/app.js` | 5.9KB | 错误分类细致，用 `res.ok` 判定，没有未捕获异常，支持 `?apiBase=` 覆盖（为测试反证提供了便利） |
| `frontend/index.html` | 4.9KB | 有深色模式、无障碍属性、`v-cloak`。需求说“视觉最简”，这里明显超出 |
| `frontend/vendor/vue.global.prod.js` | 158KB | 本地 vendor，可以离线使用，合理 |

具体问题：
1. **把开发机/平台的环境事实写进了产品代码**：
   - `app.py` 的注释和 `--help` 里写着“8000 被本机开发平台占用”；
   - `app.js` 专门识别 `{"detail": …}`，并提示“说明打到了 8000 的平台服务”。
   - 这些是 Wdrunge 运行环境的偶然事实，换一台机器就是错误信息。
2. **一份契约有三个事实源**：app.py 内的 OPENAPI、`backend/openapi.json`、`docs/api-contract.md`。日后改动容易出现不一致。
3. **误导性的错误提示**：`build_server` 把所有 `OSError` 都提示为“端口可能已被占用”，在沙箱 EPERM 场景下直接误导了三个 agent。
4. `CORS_ALLOW_HEADERS` 包含 `Authorization`，但接口没有鉴权；属于无用的宽松配置（影响很小）。
5. 后端三轮写出的代码基本一致（第 2、3 轮只改了端口和文案），说明**重跑的成本主要花在重新验证上，而不是重新实现**。

---

## 6. 设计文档质量

**评分：内容准确性 8 / 10，信息密度 4 / 10。**

| 文档 | 大小 | 评价 |
| --- | --- | --- |
| `docs/api-contract.md` | 12KB，8 章 20+ 小节 | 准确、可照做，但对单个接口来说过于庞大；第 8 节“验证证据”的原始报文应该放在测试报告里，而不是契约里 |
| `README.md`（根目录） | 7KB | 结构清晰，有 FAQ |
| `backend/README.md` | 2.8KB | 与根 README、契约第 7 节内容大量重复 |
| `docs/verification-report.md` | **不存在** | 测试两轮都没有交付 |

具体问题：
1. **文档之间不一致**：契约 v2 写的是启动输出 `listening on :::8765`（后端 #3 专门修正过），根 README 仍写 `listening on 0.0.0.0:8765`。如果测试严格按 README 核对，会记成缺陷。
2. **平台内部信息泄漏到交付文档**：
   - 契约第 6 节写着“`refresh_api_contract` 工具不适用（实测报 `environment_unavailable / unsupported_project_layout`）”；
   - 文档中写入团队成员名“@前端（林青禾）和 @测试（许明镜）”；
   - README 写着“沙箱/系统不允许绑定 0.0.0.0 时加 `--host 127.0.0.1`”。
   - 这些对最终用户毫无意义。
3. **“8000 被平台占用”在 4 份文档、2 份代码里反复出现**，属于环境噪声污染了产物。
4. 正面：契约的错误码表、`res.ok` 判定、`credentials` 与 `*` 互斥等说明，对前端接入确实有帮助，前端也严格照做了。

---

## 7. 各 worker 工作历史质量

### 后端（陆野）：5 / 10
- ✅ 技术选型最简单（标准库零依赖）；用实测取证解决端口问题并主动广播给前端、测试、Leader；交付证据完整。
- ❌ 第一次写代码就输出了 7.8K token（推理 4.1K），写了一个 340 行的服务。
- ❌ **读契约文档用了 26 次分节 `collaboration_read`**，分 4 批读完（outline → section 的工具设计迫使它碎片化读取）。
- ❌ 第 3 轮在代码已经完成的情况下，又启动了 4 次服务、反复读契约和 app.py。缺少“上一轮做到哪一步”的交接，导致整轮重新验证。
- ⚠️ 3 轮里有 2 轮是被平台问题打断的，不能全部算在模型头上。

### 前端（林青禾）：8 / 10（表现最好）
- ✅ 28 次调用、9 分钟完成；先读契约和后端代码，再动手；Vue 运行时本地化并校验文件头；用桩服务证明“不是写死的”，失败路径截图，最后清理进程。
- ✅ 交付说明清楚区分了“预期失败”和缺陷，并主动声明“README 从零启动由测试独立复核”。
- ❌ 同样踩了 network 坑两次；做了本该由测试做的验证（与测试重复）；视觉超出需求。

### 测试（许明镜）：4 / 10
- ✅ 严谨：按 README 原文启动、分层验证、区分“前端已有截图”和“本次产出”、事后清理端口。
- ❌ **效率极低**：第 1 轮 52 次模型调用、111 次工具调用、35 分钟，产出了 6 张截图和 5 份证据 txt，还写了一个桩服务脚本，**但始终没有发布 `docs/verification-report.md`**。“先做完全部，最后统一写报告”的模式，一旦失败就全部归零。
- ❌ 做了大量超出需求的检查：POST/HEAD 边界、OpenAPI 端点、lsof 端口复核（还写错了语法）、8000 平台端口复核。
- ❌ 第 2 轮没有复用第 1 轮已经落在同一工作区的 `docs/testing/e2e-verification/`，而是新建了 `independent-e2e/` 重做一遍。Leader 的重试说明里没有提到已有证据，而 fresh 上下文也看不到。
- ❌ 断言文案和选择器靠猜（3 次失败）。
- ⚠️ 33 次人工审批是平台问题（见 8.4），极大拖慢了测试。

### Leader（沈砚）：7 / 10
- ✅ 34 次调用、约 0.6M token，相对克制；决策记录、验收证据、重试处理都比较规范。
- ❌ 见第 4 节：过度规格化、端口冲突、artifacts 冲突；后端 submit 失败时误判为“没提交”，因为看不到 worker 的工具错误。

---

## 8. Token 与缓存分析

### 8.1 总量拆分

```
UI 显示 8.2M  = BBBlog（02:37–03:12 UTC）2.91M + EmptyFolder 5.25M
EmptyFolder 5.25M = worker 4.66M（输入 4.49M + 输出 0.16M）+ Leader 0.60M
```

> ⚠️ `agent_usage` 表只有 `agent_id`，**没有 project_id 和 conversation_id**；`load_agent_usage()` 返回的是全库累计。不同项目、不同对话的消耗混在一起，这本身就是一个统计缺陷。

### 8.2 钱花在哪里

| 去向 | 估算 token | 占比 | 说明 |
| --- | --- | --- | --- |
| 因故障作废的轮次 | ≈2.70M | 51% | 后端 #1/#2 0.93M（产物保留了，但 #3 又重新验证一遍）+ 测试 #1 1.76M（全部作废） |
| 后端 #3 的重复验证 | ≈0.61M | 12% | 代码已经存在，仍花了 29 次调用 |
| 测试 #2（未完成） | ≈0.66M | 13% | 与测试 #1 重复 |
| 有效产出（前端 + 后端 #2 的有效部分 + Leader） | ≈1.3M | 25% | 仍然偏重 |

### 8.3 单次调用为什么这么贵

- **固定开销约 9K token**：系统提示词（角色 + 团队 + 记忆 + 工具错误说明 + 开发工具说明 + collaboration.md）加上 30 多个工具的 schema。后端 #1 第一次调用时，消息几乎为空，输入已经是 9,183。209 次调用 × 9K ≈ **1.9M**（大部分能命中缓存，但仍按缓存价计费）。
- **大段工具结果反复进入上下文**：app.py（11.5K 字符）被读了 3～4 次，契约被整篇读过 4 次，测试读了 README 3 次。
- **平均输入 24K、最大 54.6K**：上下文只增不减，直到触发 fold。

### 8.4 缓存命中率：84.5% 正常，但有一块可以优化

- 输入总量 5.06M，未命中 **0.79M**；worker 线程未命中 0.62M。
- **上下文 fold/压缩共发生 28 次，造成约 21 万未命中（占 worker 未命中的 33%）**：fold 会把早期的 ToolMessage 改写成摘要，从改写点之后的缓存前缀全部失效。例如后端 #2 的一次调用，输入从 15,491 降到 11,539，命中只剩 8,192（≈ 系统提示词）。
- `goal_messages()` 每次都追加在消息末尾，但不写入 state，所以末尾这段永远无法命中（影响较小）。
- DashScope 的缓存是按块计费的（命中值都是 512/1024 的整数倍），每次调用天然会损失最多约 1K。
- Leader 只有 72%，因为每次唤醒都从新上下文开始（这是 26513bf 的设计选择），首轮调用只能命中系统提示词部分。可以接受。

**判断**：在 agent 多轮工具调用的场景里，85%～92% 都属于正常区间。84.5% 不算差，**真正的问题是调用次数（209 次）和重跑，而不是命中率**。即使命中率提高到 95%，总量也只会减少约 0.5M；而去掉重跑和过度验证，可以减少 3M 以上。

### 8.5 人工审批对耗时的影响（关键）

本对话共 52 次人工审批（后端 14、前端 5、测试 33），其中：

| 审批原因 | 次数 | 是否误报 |
| --- | --- | --- |
| curl 带 `-H "Origin: http://localhost:5173"` → `访问的本机端口 [5173] 不属于本项目启动的服务` | ≈20 | **是。** `operation_facts()` 用 URL 正则扫描了全部 argv，**把请求头的值当成了网络目标** |
| `browser_verify` 打开 `http://localhost:5173`（`[5173, 5173]` 等） | ≈10 | **是。** 5173 正是本任务自己用 `python3 -m http.server 5173` 启动的服务，但没被识别：`argv_ports()` 只认 `--port/-p`，不认位置参数；`http.server` 的启动日志在管道下被 stdout 块缓冲，`manage_process logs` 是空的，`LOG_PORT_PATTERN` 也匹配不到 |
| 访问 8765 / 8000（服务由别的任务启动，或已停止） | ≈10 | 部分是：测试访问前端或后端已交付的服务、做停机后的确认探测，都被判为“外来端口” |
| 小模型审核不可用或返回无效结果 | 4 | 小模型不稳定（`lsof`、`ls` 这类只读命令也被送去人工） |
| 小模型的语义判断，如“/api/nope 不在任务范围”“8765 与任务要求的 8000 不符” | 若干 | 审核模型读的是任务原文，**看不到已经接受的端口决策**，因此给出了过时的判断 |

每次审批都要等人点，测试 35 分钟里有相当一部分在等审批。**这是测试慢的首要原因**，比模型本身慢更重要。

---

## 9. `StreamChunkTimeoutError` 分析

```
StreamChunkTimeoutError: No streaming chunk received for 120.0s
(model=qwen3.8-max, chunks_received=195)
at worker.py call_model → runtime.py execute → request failed
```

**直接原因**：langchain-openai 1.6.6 默认 `stream_chunk_timeout=120s`。`Agent.model()` 创建 `ChatOpenAI` 时没有配置这个参数，也没有配置 `max_retries` 或 `timeout`。DashScope 在吐出 195 个 chunk 之后 120 秒没有再返回内容。

**当时模型在做什么（推断）**：最后一次工具调用是 21:42:23 写入 `startup-commands.txt`；此时所有证据都已齐备，上下文约 52K。下一步大概率是一次性生成完整的 `verification-report.md`（几千 token 的 publish 调用）。在生成长 tool-call 参数时，提供方中途停顿超过 120 秒。这一点**无法从数据库直接证实**，只能从行为模式推断。

**为什么损失这么大（这才是需要修的地方）**：
1. LangGraph 的 `agent` 节点**没有 RetryPolicy**，瞬时网络错误会直接抛到 `runtime.execute()`。
2. `runtime.execute()` 把任何异常都转成 `transition(failed)`，然后由 Leader 决定重试。
3. 重试时用的是 `context_mode='fresh'`，**丢弃了已经 durable 的 checkpoint**（`durability: sync` 已经把 52 步的状态写进了 checkpoints 表，本来可以直接从断点续跑）。
4. 测试“把报告放到最后写”，失败前没有任何已发布的阶段性产物。

**建议（未实施，等你确认）**：
- P0：为 `agent` 节点加 `RetryPolicy`，对 `StreamChunkTimeoutError`、`APIConnectionError`、`APITimeoutError`、`RateLimitError` 重试 2～3 次并指数退避；从当前 checkpoint 重试一次模型调用即可，不会重复执行工具。
- P0：基础设施类异常不要标记任务 failed，而是带 `context_mode='continue'` 在原线程上续跑；只有业务失败才走 fresh。
- P1：`stream_chunk_timeout` 改为可配置（例如 `QWEN_STREAM_CHUNK_TIMEOUT=300`），并在日志中记录当时的输入 token 数和最后一个 chunk 的类型，便于判断是“推理中”还是“tool 参数生成中”卡住。
- P1：提示测试“每完成一层就 publish 一次报告草稿”。Leader 已经在重试说明里这样要求了，但应该写进 testing.md，作为默认行为。

---

## 10. 改进建议（按收益排序；均未实施，需要你批准）

| 优先级 | 改动 | 预期收益 |
| --- | --- | --- |
| P0 | `operation_facts()` 跳过 `-H` 后面的值和 `Origin:`/`Referer:` 这类请求头；`task_services` 识别 `http.server 5173` 这类位置参数端口（或改为直接查询进程实际监听的端口，不依赖可能被缓冲的日志） | 消除约 30 次误报审批，测试耗时明显下降 |
| P0 | 模型调用加 RetryPolicy；基础设施失败时 continue 续跑，而不是 fresh 重跑 | 本次可以避免 1.76M token 和 35 分钟的损失 |
| P0 | `agent_usage` 增加 `project_id` / `conversation_id`（以及 request_id），UI 按对话统计 | 8.2M 这类口径错误不会再出现 |
| P1 | `execute_command(background=true)` 的默认 timeout 改为 1800 秒，或者“随任务结束而结束” | 消除服务被中途杀掉导致的连锁失败 |
| P1 | 在工具说明里写清楚：**监听端口或访问 localhost 都需要 `network: true`**；或者对 loopback 默认放行 | 消除 7 次同类失败和重复试错 |
| P1 | 在 handoff 里告诉 agent 平台保留的端口（8000 等），以及“已接受决策”的摘要；审核小模型也要能看到决策 | 避免端口冲突排查，以及“8765 与任务不符”这类过时审核 |
| P1 | 重试时把上一轮的已有产物和进度（快照 diff、已写文件列表）放进 handoff | 避免后端 #3、测试 #2 的重复劳动 |
| P2 | 在 `collaboration_read` 中提供“整篇读取（带上限）”，避免 outline 加 N 次 section 的碎片化读取 | 后端 #2 的 26 次读取可以压缩到 1～2 次 |
| P2 | `browser_verify` 支持 `expect_errors`；`tool_result_failed` 支持“预期失败”，例如 `expect_exit` | 失败统计不再失真 |
| P2 | Leader 提示词：Hello World 级别的任务验收标准不超过 3 条；有固定契约时前后端并行；验证类工作只放在一个角色 | 把调用量压到约 40 次 |
| P2 | worker 提示词：交付物不写入平台环境信息（工具名、成员名、沙箱限制、平台端口） | 提升产物质量 |
| P3 | fold 尽量“只追加不改写”，或者让 fold 的边界对齐缓存块；`goal_messages` 放进 state 的固定位置 | 缓存命中率可以提升几个百分点 |
| P3 | `manage_process` 支持按 `name` 操作；`detect_environment` 返回 OS 和工具差异（BSD/GNU） | 减少模型调用错误 |

---

## 附录：复现分析的方法

```bash
# 1. 只读快照
sqlite3 "$HOME/Library/Application Support/Wdrunge/wdrunge.db" ".backup /tmp/w.db"
# 2. 用 backend 的 venv 反序列化 LangGraph writes 表（JsonPlusSerializer.loads_typed），
#    按 checkpoint_id 排序还原每个线程的 AI / ToolMessage，包括 usage_metadata
# 3. 交叉对照：activities（失败阶段）、collaboration_records（request/goal/decision）、
#    execution_permissions（审批原因）、agent_usage（token），以及文件 mtime（代码变更时间）
```
