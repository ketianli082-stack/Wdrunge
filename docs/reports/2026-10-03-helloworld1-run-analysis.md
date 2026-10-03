# Hello World1「Vue + Python Hello World」运行复盘报告

> 分析对象：`~/Library/Application Support/Wdrunge/wdrunge.db`（对话 `e8f1b6ba…`，项目 Hello World1，工作区 `~/Codes/Agent Test Workspace/Hello World1`）
> 对照基线：[2026-10-02-emptyfolder-run-analysis.md](2026-10-02-emptyfolder-run-analysis.md)（对话 `2cc564fe…`，项目 EmptyFolder）
> 分析方式：只读快照（`sqlite3 .backup`）＋ LangGraph `writes` 表反序列化出每个 worker 的完整消息（按消息 id 去重，保留折叠前的原文）；Leader 的工具调用来自 `tool_calls` 表；token 来自 `agent_usage`（本次已有 `conversation_id` 维度）。未修改任何代码。
> 时间：文中时间均为 UTC（数据库原值），本地时间 +8 小时。

---

## 0. 结论摘要

| 问题 | 结论 |
| --- | --- |
| 3.7M token 是否都属于这个项目？ | **是。** `agent_usage` 已按对话记账：本对话 138 次模型调用，合计 **3,707,211** token（输入 3,612,781，其中缓存命中 3,233,408；输出 94,430）。 |
| 3.7M 合理吗？ | **比上次明显进步，但仍约为合理值的 3～4 倍。** 本次**没有任何重跑、没有平台停机、没有人工审批**，所以 3.7M 是“稳态成本”，剩余的浪费是**流程结构性的**，不是偶发事故。Hello World 的合理消耗约 45 次调用、0.8～1.2M token。 |
| 和上次相比 | 调用数 209 → 138（−34%）；token 5.25M（你记的 5.9M）→ 3.71M（−29%～−37%）；人工审批 52 → **0**；任务重跑 5 → **0**；耗时约 2.5 小时（含停机）→ **41 分钟**。 |
| 钱花在哪 | **测试一个人花了 59%（2.20M）**。它的实际验证只用了 15 次调用、9 分钟，**之后 43 次调用、18 分钟、约 1.6M token 花在写/改/自查文档和第二次完整重启上。** |
| 89.5% 命中率合理吗？ | **正常偏好。** 未命中 37.9 万中约 18 万是新增内容（不可避免），约 **20 万可避免**（fold 改写前缀、`enable_tools` 改变工具列表、Leader 一次疑似提供方缓存丢失）。全部消除后上限约 95%。**但命中率不改变 token 总量**：要降 3.7M，只能减少调用次数和每次的上下文。 |
| 工具失败（17/200，8.5%） | 无一导致任务失败，均在 1～2 次内恢复。其中 **4 次是平台缺陷**（`ok_exit_codes` 语义反了 ×2、`.env.example` 同步时被丢 ×1、环境探测没给出可用 Python ×1），5 次是规则未前置，5 次是文档编辑 API 的易错设计，2 次是版本冲突被错误归类为“内部错误”，1 次是纯模型错误。 |
| 代码质量 | **正确性 9/10，与需求的匹配度 8/10（上次 4/10）。** 后端 102 行标准库（上次 340 行），前端 Vite + 单个 SFC 44 行。主要问题是**平台信息泄漏进代码和文档**，以及一个**由平台缺陷造成、又被写进 README 的“缺失文件”**。 |
| 文档信息密度 | **代码 5.9KB，文档 22.3KB，比例约 1 : 3.8。** 测试报告 13KB，比全部手写代码的两倍还多。契约密度好，README 可用但偏长，测试报告冗余最多。 |

---

## 1. 数据口径与总体对比

### 1.1 线程与 token 分布

| 角色 | 线程 | 模型调用 | 工具调用 | 总 token | 输入 | 缓存命中 | 输出（其中推理） | 最大上下文 | 时间（UTC） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Leader | `leader:e8f1…` | 17 | 21 | 441,748（11.9%） | 424,712 | **70.6%** | 17,036 | 42.0K | 08:17–08:19，08:26–08:28，08:54–08:57 |
| 后端 | `697d05…` | 33 | 45 | 684,326（18.5%） | 668,451 | 90.1% | 15,875（4,041） | 29.9K | 08:18:45–08:26:31（7.8 分钟） |
| 前端 | `92fd09…` | 23 | 36 | 382,496（10.3%） | 370,605 | 91.2% | 11,891（3,768） | 26.0K | 08:21:40–08:27:34（5.9 分钟） |
| 测试 | `b6ec5e…` | 64 | 98 | **2,198,296（59.3%）** | 2,148,675 | 92.8% | 49,621（**23,267**） | 52.3K | 08:27:41–08:54:41（27 分钟） |
| 标题 | —— | 1 | —— | 345 | 338 | 0 | 7 | —— | —— |
| **合计** | | **138** | **200** | **3,707,211** | **3,612,781** | **89.5%** | **94,430** | | **41 分钟** |

### 1.2 与上次（EmptyFolder）对比

| 指标 | EmptyFolder | Hello World1 | 变化 | 主要原因 |
| --- | --- | --- | --- | --- |
| 模型调用 | 209 | 138 | −34% | 无重跑 |
| token | 5.25M（UI 口径 5.9M） | 3.71M | −29%（−37%） | 无重跑、无作废轮次 |
| 缓存命中 | 84.5% | 89.5% | +5 pt | worker 线程 90～93%，fold 次数从 28 降到 6 |
| 工具失败率 | 11.2%（45/402） | 8.5%（17/200） | −2.7 pt | 沙箱网络语义、后台超时等问题已修复 |
| 人工审批 | 52 | **0** | 消除 | `-H Origin` 误报、端口识别已修复；60 次自动审核全部放行 |
| 任务重跑 | 后端 3 轮、测试 2 轮 | 0 | 消除 | 没有平台重启和流超时 |
| 端到端耗时 | 约 2.5 小时（净约 1.5 小时） | 41 分钟 | −55%（按净耗时） | 同上 |
| 后端代码 | 340 行 | 102 行 | −70% | goal scale = small 生效 |
| 契约文档 | 12KB | 3.6KB | −70% | 同上 |
| 测试报告 | 未交付 | 13.1KB | —— | 这次反而写多了 |

**结论**：上次报告中的 P0/P1 修复（审批误报、沙箱网络默认值、后台超时、端口预注册、按对话记账、small goal 限额）全部见效。**本次剩下的问题集中在“测试 + 文档”这一段流程上。**

---

## 2. 时间线（UTC）

```
08:16  用户：“写一个最简单的 vue+python 的 hello world 网页”
08:17  Leader：预注册服务 api=8100、web=8101；create_goal（small）
08:18  Leader：create_task ×2 被拒（“small goal 每个任务最多 3 条标准”，写了 4 条）→ 改为 3 条后重发，3 个任务成功
08:18  后端开跑：读契约草稿 → 探测 Python（4 次调用）→ 写 app.py（连写两遍）
08:21  后端发布契约 v2 → 前端立即并行开跑（契约先行，这次真正做到了并行）
08:22  后端自检发现 POST 返回 501 HTML 与契约不符 → 修代码、重启、复验
08:24  前端：.env 被拒 → 改为 .env.example + config.js；npm install；Vite 起在 8101
08:25  后端：尝试删 __pycache__（4 次调用，含 enable_tools，导致一次 26.6K 的整段缓存未命中）
08:25  后端：交付检查发现契约里写了成员名 → 改为 v3
08:26  前端：submit 被拒 “Dependency changed: … is v3”（上一步的连锁反应）→ 重读 → 再 submit 被拒 “frontend/.env.example 不存在”（平台缺陷）→ 第三次成功
08:26  后端交付；Leader 验收后端
08:27  Leader 验收前端，并提醒测试核查 .env.example
08:27  测试开跑：两个服务都已被平台停掉 → 从零拉起（含第二次 npm install）
08:29–08:36  测试的真实验证（15 次调用）：接口层、页面层、桩服务反证、失败路径、重试路径 —— 全部通过
08:37–08:44  写测试报告和 README（17 次调用，5 次文档编辑失败）
08:44–08:47  停掉服务，按 README 原文再完整重启验证一遍（7 次调用）
08:47–08:53  自查：复核自己引用过的数字和时间戳、recall_context、修平台泄漏、为 410 行“超长”删减（17 次调用）
08:54  测试交付；08:57 Leader 验收并完成目标
```

---

## 3. 工具调用失败的根因

共 200 次工具调用，失败 17 次（Leader 2、后端 5、前端 5、测试 5）。逐条对照返回内容后分为 5 类：

| 类别 | 次数 | 说明 |
| --- | --- | --- |
| A. 平台缺陷 | 4 | 需要改代码 |
| B. 规则没有前置告知 | 5 | 校验是对的，但模型事先不知道这条规则 |
| C. 文档编辑 API 易错 | 5 | 三个角色在同样的地方犯错 |
| D. 版本冲突被错误归类 | 2 | 错误信息误导 |
| E. 纯模型错误 | 1 | —— |

此外还有 2 次**“看起来成功、其实无效”**的调用，没有计入失败率（见 3.6）。

### A. 平台缺陷（4 次）

1. **`ok_exit_codes` 会替换掉 0，而不是追加**（前端 2 次）
   - `app/agents/tools/development.py:113`：`expected = tuple(ok_exit_codes) if ok_exit_codes else (0,)`。
   - 前端传了 `ok_exit_codes: [7]`（本意是“后端没起来时 curl 返回 7 也算正常”），结果后端已经起来、curl 返回 0，反而报 **`"error": "Command exited 0"`**。传 `[1]` 给 grep 时同样如此。
   - 系统提示词说的是“预期中的**非零**退出码放进 ok_exit_codes”，语义上是追加。**修复：始终包含 0**（`(0, *ok_exit_codes)`）。
2. **`.env.example` 在快照同步时被丢弃**（前端 1 次，影响一直延续到最终交付物）
   - `app/collaboration/workspaces.py:74` 的 `files()` 跳过所有 `name.startswith('.env.')` 的文件；而 `app/execution/sandbox.py:42` 明确放行 `.env.example`/`.env.sample`，`write_project_file` 拒绝 `.env` 时还**主动建议使用 `.env.example`**。
   - 结果：前端按建议写了 `.env.example` → submit 时报 “Artifact does not exist” → Leader 让测试去核查 → 测试在项目根目录确实没找到 → **测试报告把它列为“文档补遗”，README 写上“仓库中没有 `.env.example` 模板文件”**。文件其实还留在 `.wdrunge-runs/238f…/work/frontend/.env.example`。
   - 一个平台缺陷变成了交付物里的错误事实，前后牵动了 4 个角色、约 6 次调用。
3. **`detect_environment` 返回 `runtimes: []`**（后端 1 次，间接造成 4 次探测调用）
   - 空目录下环境探测没有给出可用解释器。后端于是自己试 `python3`、`/usr/bin/python3`（沙箱挡住了 Xcode 的 `libxcrun.dylib` → exit 1）、`which -a`、`/opt/anaconda3/bin/python3`。
   - 建议：空项目时也返回宿主上可用的 `python3`/`node` 路径和版本，并标注 `/usr/bin/python3` 在沙箱中不可用。

### B. 规则没有前置告知（5 次）

| 调用 | 规则 | 问题 |
| --- | --- | --- |
| Leader `create_task` ×2 | small goal 每个任务最多 3 条验收标准 | `leader.md` 只写了 “scale (small/medium/large bounds criteria)”，**没写具体数字**；两个并行任务各写了 4 条，一起被拒，多花一轮 Leader 调用（约 1 万 token） |
| 前端 `write_project_file .env` | `.env` 受保护 | 拒绝信息给出了替代方案，前端下一步就改对了 ✔（但替代方案又触发了 A2） |
| 后端 `manage_file delete __pycache__` ×2 | 受保护路径 | `__pycache__` 是后端自己跑 `py_compile` 产生的。为了删它，后端先 `enable_tools(files_more)`，**改变了工具列表，导致下一次调用缓存命中为 0（26.6K 全部未命中）**，然后两次删除都被拒。共浪费 4 次调用。建议在沙箱里设 `PYTHONDONTWRITEBYTECODE=1`，或者在拒绝信息中说明“缓存目录会被自动排除，无需删除” |

### C. 文档编辑 API 易错（5 次，三个角色各踩一遍）

| 错误 | 次数 | 根因 |
| --- | --- | --- |
| 对 `root` 用 `replace_text` 替换草稿提示语，报 “old_text must match exactly once” | 2（后端、测试） | **平台自动创建的草稿**把 `> 草稿：xx 正在编写…` 放在一级标题小节里，`root` 的正文是空的。每个角色都要再读两次 section 才能找到它。之后测试还专门花了一次调用删除草稿留下的占位小节（概要/内容/待办） |
| `summary`/`base_version` 写在顶层而不是 `changes[]` 里 | 2（后端、测试） | 校验错误给出了 `allowed_fields: ["changes"]`，模型下一次就改对了 ✔。但两个角色都犯了同样的错，说明 schema 形状本身容易误解 |
| `replace_text` 的 old_text 里包含了小节标题 | 1（测试） | section body 不含标题，这一点只能靠试错得知 |

**建议**：草稿只包含标题和 `pending` 占位（或者干脆不预建草稿，第一次 publish 时创建），这样第一次就可以 `replace_section_body`；在 `collaboration_publish` 的描述里给出一个完整的最小示例。

### D. 版本冲突被错误归类（2 次）

- 前端 `submit_task`：`Dependency changed: … is v3`；测试 `collaboration_publish`：`expected version 1`（测试把报告的版本号用到了 README 上，属于模型错误）。
- 两者都是 `StorageConflictError`，在 `app/agents/tools/validation.py:206-208` 落入兜底分支：**`category=internal_error`，`retry_policy=do_not_retry_unchanged`，提示 “This may be a tool/runtime defect… report the blocker”**。
- 这其实是正常的乐观锁冲突，正确的指导应该是“重新读取最新版本后重试”。这次两个 agent 都没有被误导（自己判断出了原因），但提示词里又说“internal_error 表示工具或运行时问题……向 Leader 报告阻塞”，**这个错误归类存在把正常冲突升级成阻塞的风险**。建议加一个 `conflict` 类别，`retry_policy=after_state_change`。
- 前端那次的源头值得注意：后端的交付检查（`platform_leak`）要求删除契约里的成员名 → 契约从 v2 升到 v3 → 前端基于 v2 的提交被拒。**一个纯措辞的修改让下游的依赖校验失败了。**建议：只改措辞（没有改结构或字段）的修订不影响依赖，或者交付检查在发布前就运行。

### E. 纯模型错误（1 次）

- 测试 `recall_context` 传了一个不在折叠记录中的 message_id（它想复核自己写进报告的日志时间戳）。随后改用 `manage_process logs` 拿到了。这个调用本身也属于第 5 节说的“过度自查”。

### 3.6 “成功但无效”的调用

- 前端 `execute_command sh -s http://…/App.vue | head -40`：把管道当成参数传给 `sh -s`，命令什么都没做，但 exit 0 被记为成功。下一步才改成 `sh -c`。
- 前端 `collaboration_check_delivery` 报 “**1516 lines delivered**（small 上限 400）”，前端于是又花 2 次调用 `ls`/`wc -l` 证明主要是 `package-lock.json`（1340 行）。`delivery_checks.py:64` 只排除了 `.lock` 后缀和 `.min.js`，**`package-lock.json` 没有被排除**（`uv.lock`、`poetry.lock` 会被排除，npm 的锁文件不会）。这是误报，属于平台缺陷。

---

## 4. Leader 下发任务的质量

**评分：8 / 10（上次 6.5）。拆解、并行和端口规划都明显改进；扣分点在于下发了平台做不到的要求，以及把 README 和“可复现性验证”都压在测试身上。**

做得好的地方：
- **预注册端口**：开局就为 api/web 注册了 8100/8101，并告知 1420/17420 是保留端口。上次 8000 端口冲突的问题没有再出现。
- **契约先行、真正并行**：前端在契约 v2 发布后（08:21）立即开工，和后端的实现、自检并行，前端比后端还早结束开发。
- **唯一负责人**：README 只由测试负责，“端到端验证只由测试做”，前后端只做自检。上次前端和测试重复验证的问题基本消失。
- **验收克制**：验收测试时读了 4 次文档，然后 complete_task、complete_goal，合计约 2 分钟。
- 主动发现前端 artifacts 和磁盘不一致（`.env.example`），并提醒测试核查，判断是对的（只是根因是平台缺陷）。

问题：
1. **要求“服务需保持运行交付”，平台做不到。** 每个 worker 任务结束时，`app/collaboration/runtime.py:194` 的 `processes.close(workspace=active_workspace.get())` 会关闭它启动的全部进程。后端和前端都按要求“保持运行”并在交付说明里写明，测试接手时两个端口都是空的，只能从零拉起（含第二次 `npm install`）。要么平台在任务之间保留已注册的服务，要么 Leader 的提示词明确“服务由测试统一拉起”。
2. **指定了 CORS 跨域方案**（“需允许该来源跨域读取”）。对一个 Vite 项目来说，`server.proxy` 把 `/api` 转发到 8100 更简单：不需要 CORS、不需要 OPTIONS，也不会出现 README 里“不要用 localhost 访问，否则 CORS 失败”这一整节 FAQ 和测试报告里的 3 项检查。架构选择应该留给执行者，或者在 small goal 下默认选择最简方案。
3. **测试任务的第 3 项“确认按 README 步骤可复现”和“README 由测试编写”放在一起**，导致测试必须先完成一轮验证、再写 README、再按 README 完整重启一遍（7 次调用、26.8 万 token）。更好的顺序是：先写 README，再按 README 做唯一一次从零启动的验证。
4. **3 条标准的上限不知道**，导致首轮 create_task 失败（见 3.B）。
5. 任务描述仍然偏长（1.2K～2K 字），并通过 `collaboration_inspect_request` 被其他角色整段读取（见 6.3）。

---

## 5. 各成员的工作质量与调用链合理性

### 后端（陆野）：7.5 / 10（上次 5）
- ✅ 标准库零依赖，102 行；先发布契约再实现；自检**发现了自己的实现和契约不一致**（POST 返回 501 HTML，契约写的是 405 JSON）并修复；主动通知前端和测试，附带实际输出。
- ❌ **连写两遍 `app.py`**（#3 写了 3,896 字符，#4 立刻改写成 3,235 字符），多输出约 1.2K token。
- ❌ 探测 Python 用了 4 次调用（平台缺陷 A3 引起）。
- ❌ 自检超出 3 条标准：curl 共 8 次（含 `evil.example` 来源、POST、404、OPTIONS），改代码后又全部复验一遍。
- ❌ 删除 `__pycache__` 用了 4 次调用，其中 `enable_tools` 造成一次 26.6K 的整段缓存未命中。
- 调用链：`读契约 → 探测环境(4) → 写代码(2) → 发布契约(2，含 1 次失败) → 启动 → 自检(4) → 修复(2) → 重启 → 复验(2) → 通知 → 清理(4，全部无效) → 交付检查 → 改契约(2) → 终检 → 提交`。**有效路径约 15 步，实际 33 步。**

### 前端（林青禾）：8.5 / 10（上次 8）
- ✅ 23 次调用、6 分钟；Vite + 单个 SFC，`config.js` 是后端地址的唯一来源；`res.ok` 判断、加载/失败/重试三种状态；用 `browser_verify` 拿到了真实的渲染证据。
- ✅ 契约从 v2 升到 v3 后，先 diff 内容、确认只是措辞变化，再重新提交，判断准确。
- ❌ 只改了一行 `catch (err)` → `catch`，就又跑了一次 `browser_verify`（#16）。
- ❌ `sh -s` 的误用（3.6）；为 `oversized` 误报花了 2 次调用。
- ❌ 3 次 submit 中有 2 次是平台原因（A2、D）。

### 测试（许明镜）：5.5 / 10（上次 4）
- ✅ **验证本身很出色**：15 次调用、9 分钟内完成接口层、页面层，用**桩服务返回不同文案**证明不是硬编码，再停掉后端验证失败提示，恢复后点“重试”并用后端访问日志证明是真实的重新请求。证据链完整，结论可信。
- ❌ **之后的 43 次调用（67%）、约 1.6M token（73%）、18 分钟都在处理文档：**

| 阶段 | 调用 | 输入 token | 说明 |
| --- | --- | --- | --- |
| #1–6 侦察 | 6 | 114K | 读两个依赖任务的完整记录（18K 字符）、读代码 |
| #7–21 **真实验证** | 15 | 325K | ✅ |
| #22–38 写报告和 README | 17 | 558K | 5 次编辑失败（3.C）、3 次读 outline 定位 |
| #39–45 按 README 再完整重启一遍 | 7 | 268K | 和 #7 的从零启动重复（见 4.3） |
| #46–62 自查 | 17 | **780K** | 复核自己引用的 npm 包数、日志时间戳（含失败的 recall_context）、补充 localhost 的 CORS 证据、修 4 处平台泄漏、为“410 行超长”做删减 |
| #63–64 提交 | 2 | 104K | |

- ❌ 推理 token 23K，占输出的 47%（后端 25%、前端 32%），几次“规划下一步”的推理超过 2～3K。
- ❌ 测试报告的检查项 9～12（OPTIONS、404、405、非白名单来源）与后端自检完全重复，也超出了 Leader 给的 3 项验证内容。
- 调用链评价：**前三分之一是教科书式的验证，后三分之二是“对自己写的文档做审计”。**根因有三：Leader 把 README 和可复现性验证都交给了测试（4.3）；草稿文档 + 交付检查的循环（编辑 → 检查 → 修泄漏 → 检查 → 删减）；角色提示词鼓励“声明必有证据”，在文档阶段变成了逐条复核。

### Leader（沈砚）：8 / 10（上次 7）
- 见第 4 节。17 次调用、约 44 万 token，相对克制。命中率只有 70.6%，原因见 7.3。

---

## 6. 文档与上下文的有效信息密度

### 6.1 交付物：代码 vs 文档

| 类别 | 文件 | 大小 | 评价 |
| --- | --- | --- | --- |
| 代码 | `backend/app.py` | 3.6KB / 102 行 | 简洁、正确 |
| 代码 | `frontend/src/App.vue` + `config.js` + `main.js` + `index.html` + `vite.config.js` + `package.json` | 2.3KB | 简洁、正确 |
| 文档 | `docs/api-contract.md` | 3.6KB / 83 行 | **密度好**：概览表、字段表、错误结构、前端调用示例都是前端真正要用的 |
| 文档 | `README.md` | 5.6KB / 159 行 | 可用，但约 40% 是 FAQ（其中 CORS/localhost 一节是架构选择带来的，见 4.2）；还包含一句因平台缺陷产生的错误陈述 |
| 文档 | `docs/test-report.md` | **13.1KB / 194 行** | 核心价值是 12 行的检查结果表（约 3KB）。“复现与重启”（60 行）与 README 重复；“证据”是原始 shell 记录；检查项 9～12 与后端自检重复 |
| 证据 | `docs/testing/*.png` ×7 | 72KB | **其中 4 张字节完全相同**（md5 `ff25a86f…`），即同一个“Hello World”页面截了 4 次 |
| 证据 | `docs/testing/stub_api.py` | 2KB | 合理，可复现反证 |

**代码 5.9KB，文档 22.3KB，比例约 1 : 3.8。** 对 Hello World 来说，合理的比例是 1 : 1 左右（契约 1～2KB，README 1～2KB，测试报告 2～3KB）。

### 6.2 平台信息泄漏（交付检查漏掉了）

| 位置 | 内容 |
| --- | --- |
| `frontend/vite.config.js:4` | `// 服务 web：固定监听 127.0.0.1:8101（不使用保留端口 1420 / 17420）` |
| `docs/api-contract.md:8,10` | “服务注册表中的名称，前端请按服务名引用”、“不使用保留端口 1420 / 17420” |
| `docs/test-report.md:8,107,195` | “按注册表登记的命令”、“注册表均 verified 状态” |
| `README.md:112` | “仓库中**没有** `.env.example` 模板文件”（由平台缺陷 A2 产生的错误事实） |

`delivery_checks.py` 的 `platform_terms()` 只匹配工具名和成员名，**不包含“保留端口 1420/17420”和“服务注册表/注册表”**。这些词来自 Leader 的任务描述，被各角色原样带进了交付物。建议：把保留端口号和“注册表”加入检测词表；或者在 handoff 中改写为不需要转述的形式（例如只给“可用端口 8100、8101”，不提保留端口）。

### 6.3 上下文中的低密度内容

| 来源 | 大小 | 问题 |
| --- | --- | --- |
| 系统提示词 + 工具 schema | 每次约 7.5～9.5K token | 138 次 × 约 7.5K ≈ **1.0M（占总量 27%）**，大部分命中缓存，但仍然计入 token |
| `submit_task` 的返回值 | 6.3K～7.9K 字符 / 次 | **把整个任务记录（任务原文 + 刚提交的结果）原样回显**。agent 只需要 `{id, state}` |
| 测试 #1 `collaboration_inspect_request` ×2 | 17.9K 字符 | 读取前后端任务的完整记录，包括两份 1.2～2K 字的任务原文和 2.7～3K 字的交付说明；在第一次 fold 之前被重复发送 5 次 |
| `manage_process list`（测试 #46） | 12.4K 字符 | 列出了本任务的**所有**历史进程，包括几十个已退出的 `ls`/`curl`；默认应只列运行中的进程 |
| Goal reminder | 约 900 字符 / 次 | 任何任务的状态变化都会追加一份新的（`task_counts` 变了），后端线程里有 4 份 |
| 角色间消息 | 1.0～1.4K 字 / 条 | 后端发给前端和测试的通知各 1.3K，大部分内容与契约重复 |

---

## 7. Token 与缓存分析

### 7.1 3.7M 花在哪里

| 去向 | 估算 token | 占比 |
| --- | --- | --- |
| 测试：写文档 + 按 README 再重启 + 自查（#22–62） | ≈1.61M | **43%** |
| 测试：侦察 + 真实验证 + 提交 | ≈0.54M | 15% |
| 后端（含约 15 次非必要调用） | ≈0.68M | 18% |
| 前端 | ≈0.38M | 10% |
| Leader | ≈0.44M | 12% |
| 其他（标题） | <0.01M | —— |

### 7.2 合理基线估算

| 角色 | 合理调用数 | 平均上下文 | token |
| --- | --- | --- | --- |
| Leader | 6～8 | 15K | ≈0.12M |
| 后端 | 10～12 | 14K | ≈0.16M |
| 前端 | 10～12 | 14K | ≈0.16M |
| 测试 | 15～20 | 20K | ≈0.35M |
| **合计** | **≈45** | | **≈0.8～1.0M**（按现有固定开销，留余量可到 1.2M） |

所以 3.7M 约为合理值的 **3～4 倍**（上次是 8～10 倍）。

### 7.3 缓存命中率：89.5%

| 角色 | 未命中 | 其中新增内容（不可避免） | 其中可避免 | 主要原因 |
| --- | --- | --- | --- | --- |
| Leader | 125K | 53K | **72K** | 3 次 fold（08:28:33 命中只剩 8K，08:55:23 剩 13K，08:56:45 剩 17K）；**08:27:06 一次调用 25.4K 中只命中 1,024**：当时没有 fold、系统提示词是静态的、工具列表也没变，最可能是 DashScope 隐式缓存被驱逐或请求被路由到了别的节点 |
| 测试 | 155K | 73K | 83K | 3 次 fold（#7、#33 只命中 8.7K/31K，#50 只命中 10K/37K） |
| 后端 | 66K | 30K | 36K | `enable_tools` 改变工具列表，导致 #25 整段未命中（26.6K） |
| 前端 | 33K | 25K | 8K | —— |
| **合计** | **379K** | **≈181K** | **≈199K** | |

判断：
- 消除全部可避免的未命中后，命中率上限约 **95%**。但**命中率只影响费用，不影响 token 总量**。要把 3.7M 降下来，只能减少调用次数（第 5 节）和每次的上下文大小（6.3）。
- Leader 的 70.6% 偏低：调用次数少（17 次），每次 fold 都会让它损失一大段前缀，而每次唤醒又会追加一段 coordination update。可以考虑对 Leader 提高 fold 阈值（`WDRUNGE_FOLD_TRIGGER_TOKENS`，当前 12000）——它的上下文最大只有 42K，本来就不太需要折叠。
- `enable_tools` 的代价在 `tool_profiles.py` 的注释里已经写明（“a changed tool list invalidates the provider's prompt cache from the start”）。这次触发它的是一个本就不该发生的操作（删 `__pycache__`）。

### 7.4 遥测缺口

- worker 的 `agent_usage.latency_ms` 和 `retries` 全部为空，只有 Leader 有值，所以无法拆分 worker 的“模型耗时”和“工具耗时”。
- `tool_calls` 表只记录 Leader 的调用。worker 的调用只能从 LangGraph `writes` 中反序列化得到，而且 fold 之后需要按消息 id 去重才能拿到原文。建议 worker 也写入 `tool_calls`。

---

## 8. 代码质量

**评分：正确性 9 / 10，与需求的匹配度 8 / 10（上次 9 / 4）。**

做得好的地方：
- 后端：`ThreadingHTTPServer`，CORS 白名单可以通过 `API_CORS_ORIGINS` 配置，统一的 JSON 错误结构，`Vary: Origin`，`Content-Length` 正确，`log_message` 输出到 stderr。没有多余的抽象。
- 前端：`<script setup>` + `ref`，`res.ok` 判断，按钮在加载时禁用，`data-testid` 便于测试；`vite.config.js` 使用 `strictPort: true`，避免端口漂移后和 CORS 白名单对不上。
- 后端地址只在 `config.js` 一处出现，满足 Leader 的要求。

问题：
1. **契约与实现的小出入**：契约写“所有非 2xx 响应（除 204 预检外）都是 JSON”，但后端在 #15 把 `do_HEAD` 从别名列表中删掉了，`HEAD /api/hello` 会返回 `BaseHTTPRequestHandler` 默认的 **501 HTML**。另外 `OPTIONS` 对任意路径都返回 204，契约写的是只针对 `/api/hello`。影响很小，但说明自检和测试都没有覆盖 HEAD。
2. **平台信息进入代码注释**：`vite.config.js` 里的“保留端口 1420 / 17420”（6.2）。
3. **架构可以更简单**：Vite 的 `server.proxy` 可以去掉整套 CORS 逻辑（后端约 20 行、契约的 CORS 小节、README 的 localhost FAQ、测试检查项 2/9/12）。
4. `.env.example` 被平台丢弃（A2），交付物里少了一个前端本来写好的文件，README 还为此专门写了一句说明。
5. `App.vue` 没有校验 `data.message` 的类型；如果响应里没有 `message` 字段，页面会显示空白而不是错误提示。对 Hello World 来说可以接受。

---

## 9. 其他影响建站效率的因素

1. **worker 启动的服务在任务结束时会被关闭**，而任务描述和服务注册表都假设服务“保持运行”。测试因此重新 `npm install`、重新拉起服务，后端和前端为“保持运行”做的确认也都白做了。这是平台语义和 Leader 提示词之间的冲突。
2. **每个 worker 都在独立快照中运行**（`.wdrunge-runs/<id>/work`，本次三个快照共 75MB，主要是 `node_modules`）。快照会保留在项目目录里，交付之后应该清理。
3. **草稿文档机制**本意是让依赖方尽早开工，但对 small goal 来说，它带来了 5 次编辑失败、多次读 outline 和一次专门的占位清理，收益很小。
4. **交付检查是“编辑 → 检查 → 修改”的循环**：后端（成员名）→ 契约 v3 → 前端提交冲突；测试（工具名、410 行）→ 两轮修改。建议在 publish 时就做泄漏检测并直接拒绝，而不是在交付前作为 finding 返回。
5. **`oversized` 统计包含 `package-lock.json`**（3.6），small goal 下几乎所有 npm 项目都会误报。
6. **推理 token**：测试的推理占输出的 47%，有几次单次推理达到 2～3K。可以考虑对 small goal 的 worker 降低推理预算，或者使用更便宜的模型（`WDRUNGE_MODEL_SMALL_TASKS` 已经支持，本次没有配置）。

---

## 10. 改进建议（按收益排序；均未实施，需要你批准）

| 优先级 | 改动 | 预期收益 |
| --- | --- | --- |
| P0 | `development.py:113`：`ok_exit_codes` 始终包含 0 | 消除“exit 0 却失败”的误报 |
| P0 | `workspaces.py:74`：同步时放行 `.env.example`/`.env.sample`，与 `sandbox.py:42` 保持一致 | 修复缺失文件以及它在 README/报告中造成的错误陈述 |
| P0 | 测试流程：**先写 README，再按 README 做唯一一次从零启动验证**；报告只写检查结果表 + 证据路径，不写复现步骤（链接到 README）；`testing.md` 明确“报告写完不做逐条自查” | 测试 64 → 约 25 次调用，**约节省 1.2～1.5M token、15 分钟** |
| P1 | 服务语义统一：要么平台在任务之间保留已注册的服务，要么 Leader 提示词里改为“服务由测试统一拉起，开发只需自检后停止” | 去掉一次 npm install 和重复启动；避免不可能满足的要求 |
| P1 | `submit_task` 只返回 `{id, state, findings}`；`collaboration_inspect_request` 默认返回摘要（任务原文截断、只附带交付说明）；`manage_process list` 默认只列运行中的进程 | 每个线程减少 5～20K 字符的低密度上下文 |
| P1 | `StorageConflictError` 归类为 `conflict` / `after_state_change`，不要再落入 `internal_error` | 避免把正常的版本冲突当成阻塞上报 |
| P1 | 把 “small 每任务 ≤3 条标准” 等具体数字写进 `create_goal`/`create_task` 的描述，或者在 `create_goal` 的返回值里说明 | 消除 Leader 首轮被拒 |
| P2 | 草稿文档只放标题和 `pending` 占位（不要放在一级小节正文里），或者 small goal 不预建草稿；`collaboration_publish` 的描述里给出完整示例 | 消除 5 次编辑失败和若干次 outline 读取 |
| P2 | 交付检查：`platform_terms` 增加保留端口号和“注册表”；只改措辞的修订不触发依赖冲突；`oversized` 排除 `package-lock.json`/`pnpm-lock.yaml`/`yarn.lock` | 消除漏检、连锁冲突和误报 |
| P2 | `detect_environment` 在空项目下也返回宿主可用的 python/node 路径；沙箱设置 `PYTHONDONTWRITEBYTECODE=1` | 后端少 6～8 次调用 |
| P2 | Leader 提示词：small goal 下不指定实现架构（例如 CORS 还是代理），由执行者选择最简方案 | 文档和测试项可再减少约 30% |
| P3 | Leader 的 fold 阈值单独提高（它的上下文很少超过 40K）；worker 也写入 `tool_calls`、记录 `latency_ms` | 命中率 +2～3 pt；可观测性 |
| P3 | 对 small goal 配置 `WDRUNGE_MODEL_SMALL_TASKS` 或降低推理预算 | 降低单价 |

**预期**：落实 P0 + P1 后，同样的 Hello World 大约是 **75～85 次调用、1.8～2.2M token、25 分钟左右**；再落实 P2，可以接近 **1.0～1.2M**。

---

## 附录：复现分析的方法

```bash
# 1. 只读快照
sqlite3 "$HOME/Library/Application Support/Wdrunge/wdrunge.db" ".backup /path/w.db"
# 2. 按对话统计 token（本次已有 conversation_id）
sqlite3 /path/w.db "select agent_id, count(*), sum(total_tokens), sum(cached_tokens), sum(context_tokens)
                    from agent_usage where conversation_id='e8f1b6ba-…' group by agent_id"
# 3. Leader 工具调用：tool_calls 表（参数与返回原文）
# 4. worker 消息：用 backend 的 venv，以 JsonPlusSerializer.loads_typed 反序列化 writes 表中
#    channel='messages' 的值；按 (message.id, 类型) 去重并保留第一次出现的版本（即 fold 之前的原文），
#    得到每个线程完整的 AIMessage（含 usage_metadata）与 ToolMessage
#    线程：后端 697d05…，前端 92fd09…，测试 b6ec5e…
# 5. 交叉对照：context_compactions（fold 时间点）、execution_permissions（审核/审批）、
#    工作区实际文件与 .wdrunge-runs 快照
```
