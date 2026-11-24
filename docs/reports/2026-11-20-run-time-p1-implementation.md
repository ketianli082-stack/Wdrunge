# 运行时间 P1 修复实施记录：合并规则读 `.gitignore`、验证分工、审查事实、浏览器验证

> **对应计划**：[2026-11-19-run-time-root-cause-plan.md](2026-11-19-run-time-root-cause-plan.md) 第 5.4、5.5、5.6、5.7 节（根因 A4、A5、A6、B）。你 2026-11-20 说“Start P1”。决定表第 9 项（验证分工）你已同意；第 6、7 项你没有单独表态，我按建议做了（Leader 只读工具和平台附带事实两个都做；截图改短名并加交付检查），见第 6 节。
>
> **状态**：已实施，未提交（分支 `context-desk`，P0 的改动也还没提交）。后端全部测试 589 通过、2 跳过（P0 之后的 573 加这次新增的 16 个）。真实环境验证见各节，用的是真实的 `sandbox-exec`、真实 Chromium（Playwright 1.63）、npm、Vite、git 2.54 和 Blog10 的快照与运行记录。
>
> **没做的**：P2 的 5.10。5.8（思考预算）和 5.9（上下文预算）是 2026-11-20 追加的，见第 8 节：思考预算你定为 2,000，已是默认值；上下文预算按你的决定改成 100K。没有跑带模型的完整运行，所以提示词改动的效果、Leader 拿到 facts 和只读工具之后的实际行为都没有验证，计划第 6 节的验收（下一次同题运行）还没做。
>
> **你的确认**：2026-11-20 你对第 6 节的问题回复“ok”，第 6 节第 1–10 项按现在的做法定下来。P0 留下的两项（`RLIMIT_CPU` 14,400 秒；没有标记的根目录依赖目录重装一次）你随后回复“Yes, both”，也已确认。

---

## 1. 5.4 合并规则读项目的 `.gitignore`（根因 A4）

**根因回顾**：合并只按固定名单排除（`node_modules`、`dist` 等）。Blog10 的前端把 Vite 缓存目录改到 `frontend/.vite`，并写进了 `frontend/.gitignore`，但平台不看这个文件，28 个缓存文件被当成交付内容合并进项目。Leader 在 05:26 为了弄清“缓存有没有被合并”派了一个只读任务。

**改动**（[workspaces.py](../../backend/app/collaboration/workspaces.py)）：

| 位置 | 内容 |
| --- | --- |
| 新增 `Ignore` 类 | 读项目各级目录的 `.gitignore`，规则和 git 一致：同一文件里后面的规则覆盖前面的，深层目录的文件覆盖浅层的，`!` 取反，只匹配目录的写法（结尾 `/`），带 `/` 的写法相对所在目录锚定，`**`，方括号字符集和转义，父目录被忽略后子路径不能再被取反放回。只读 `.gitignore`，不读 `.git/info/exclude` 和用户全局的忽略文件；`.gitignore` 是链接或读不出来时当作没有 |
| `files()` | 遍历时跳过被忽略的目录和文件。取快照（复制项目）和算合并清单都走这里，所以被忽略的路径既不进快照，也不进合并 |
| `plan()` | 任务中途把一个项目里已有的文件加进 `.gitignore`：这个文件在项目里原样保留，不当成“任务删除了它” |
| `_refresh()` | 项目里别人的合并开始忽略某个文件：任务自己那份保留，只是不再跟踪它 |
| `unmerged_reason()` | 成员把被忽略的路径声明为交付物时当场拒绝，原因写明是哪个 `.gitignore` 的哪条规则，例如 `it is ignored by frontend/.gitignore (pattern ".vite/"); remove the pattern if this is a deliverable`。平台自己登记的文档路径不受 `.gitignore` 影响 |
| 新增 `left_out()` | 列出任务工作区里没有合并的东西和原因，按目录汇总，例如 `frontend/.vite/ (28 files): ignored by frontend/.gitignore (pattern ".vite/")`。它进入 5.6 的 `not_merged` 事实 |

**验证一：和真实 git 对照**。随机生成目录树和 `.gitignore`（多级、取反、锚定、`**`、字符集、转义），对每个路径比较 `Ignore` 的判断和 `git check-ignore` 的结果：

| 轮次 | 结果 |
| --- | --- |
| 第一版 | 1,500 棵树里 65 棵有分歧，都来自三种写法：字符集里转义的 `]`、`[]a]`、连续两个斜杠 |
| 修正后 | 2,500 棵树、59,939 个路径全部一致（git 2.54.0） |

**验证二：重放 Blog10 的 13 个任务快照**，按“第一次合并之前”的状态分别用旧规则和新规则算合并清单：

| | 旧规则 | 新规则 |
| --- | --- | --- |
| 13 个快照的变更文件合计 | 62 | 34 |
| 快照 `3ce11a61…`（前端初始化） | 含 `frontend/.vite/deps` 下 28 个文件 | 这 28 个不再出现 |
| 其余 12 个快照 | | 清单和旧规则完全相同 |

单元测试：`tests/test_ignored_paths.py`，7 个（和 git 对照、没有 git 时的固定用例、快照和合并都不带被忽略的路径、任务中途忽略项目已有文件、删掉规则后文件能交付、链接或损坏的 `.gitignore`、刷新时保留任务自己的副本）。

**和计划的差别**：计划只写了“合并和快照复制都排除”。实际多做了三件相邻的事：任务中途新增规则时不删项目里的文件；被忽略的交付物当场拒绝并指出规则；没合并的东西汇总成事实交给审查者。没有它们，“被忽略”对成员和 Leader 仍然是看不见的。

**没验证的**：Blog10 项目根里已经合并进去的 `frontend/.vite` 文件没有清理，新规则只管以后的合并。

---

## 2. 5.5 验证分工：开发写用例，测试看页面（根因 A5、B）

**根因回顾**：同一件事做两遍。前端提示词要求每写完一页就用浏览器验证，测试提示词又要求所有模块在浏览器里逐条走验收标准。Blog10 前端有 96 轮、73 分钟花在浏览器验证上，测试角色一次都没轮到；访客页面的尝试 4 纯粹是在补截图证据。

**改动一：提示词**（计划里的五条）

| 文件 | 内容 |
| --- | --- |
| [prompts/frontend.md](../../backend/app/agents/prompts/frontend.md) | 脚手架加 Vitest + @vue/test-utils；每页写完就写用例，用例来自需求文档的验收标准（规定的文案、为空状态、出错状态各有用例，断言写需求里的要求）；登记 `frontend-build` 和 `frontend-test`；模块做完做一次冒烟检查（打开每个页面，不带 actions，不留截图，不算交付证据）；交付内容改成“每条验收标准对应的用例” |
| [prompts/backend.md](../../backend/app/agents/prompts/backend.md) | 原来“普通增删改查不写测试文件”改成“每条验收标准至少一个用例”；登记 `backend-test`；冒烟检查是对运行中的服务把主要接口各调用一次 |
| [prompts/leader.md](../../backend/app/agents/prompts/leader.md) | 给前端、后端的任务，验收标准不要求截图和在线验证记录；开发任务按 facts 里的 checks 验收；页面效果归测试任务 |
| [prompts/testing.md](../../backend/app/agents/prompts/testing.md) | 选择器可以取自失败结果里的 dom；证据路径照结果里的 `screenshot` 原样抄写；说明用例由开发写、平台已重跑 |
| [prompts/modules/worker.md](../../backend/app/agents/prompts/modules/worker.md)、[modules/leader.md](../../backend/app/agents/prompts/modules/leader.md)、[base.py](../../backend/app/agents/base.py) | 交付流程的说明跟着改；删掉“页面证据以浏览器验证结果和截图为准” |
| [tool_profiles.py](../../backend/app/agents/tool_profiles.py) | 前端、后端可以用 `collaboration_register_command` |

**改动二：平台在交付时重跑登记的测试和构建命令**（[environment.py](../../backend/app/collaboration/environment.py) `run_verification`，[review.py](../../backend/app/collaboration/review.py) `delivery_verification`）

| 规则 | 做法 |
| --- | --- |
| 哪些命令算验证 | 登记名里含独立的 `test`、`tests` 或 `build` 一段（用 `-`、`_`、`.` 分隔），例如 `test`、`backend-test`、`frontend-build`；`latest`、`rebuild` 不算 |
| 什么时候跑 | 命令的 `cwd` 覆盖了这次任务改动的文件（`docs/` 下的不算）才跑。没改到的代码不跑 |
| 在哪里跑 | 任务的工作区，合并之前。和成员自己跑命令走同一条路径（同一个沙箱、同一套注入的数据库连接），超时 600 秒 |
| 平台不做第一次运行 | 只重跑“已经成功跑过”的命令（登记状态是已验证，或这次任务里跑通过）。登记了但从没跑通的，记一条“平台没有运行它” |
| 失败怎么办 | 成员主动交付时：拒绝，什么都不合并，命令的退出码和输出末尾 1,500 字符直接回到成员这一轮 |
| 拒绝有上限 | 同一个任务最多拒绝 2 次（`CHECK_REFUSALS`）。之后放行，失败写进 facts 交给 Leader，因为原因可能在任务之外（别人的文件、环境）。成功合并后计数清零 |
| 回合结束时的自动交付 | 不拒绝（成员已经没有机会处理），失败照样写进 facts |
| 没有命令覆盖的代码目录 | 写一条 `no registered test or build command covers the files this task changed in scripts/…`，避免 Leader 把“没有检查”读成“检查全部通过”。项目根的单个文件（清单、配置）不算 |

**改动三：截图证据**

| 位置 | 内容 |
| --- | --- |
| [browser_verify.py](../../backend/app/agents/tools/browser_verify.py) | 新参数 `screenshot`。只有测试角色默认保存并返回截图，其他角色默认不截，需要看效果时传 `screenshot=true`。不截图时浏览器里也不做截取 |
| 截图文件名 | 从 `browser-<16 位哈希>.png` 改成 `shot-<页面>-<4 位哈希>.png`，例如 `shot-admin-posts-3fa2.png`。同样内容的截图仍然是同一个文件；哈希前缀撞上不同内容时自动加长到 8、16 位 |
| 交付时核对引用 | 平台从交付说明、证据、任务改过的和登记的 Markdown 文档里找出引用的截图路径，不存在的当场拒绝，并列出工作区里实际有的截图文件名。和测试命令共用上面的 2 次上限 |

**验证：重放 Blog10 登记的 `test` 命令**（`uv run pytest`，cwd 是 `backend`，32 个用例）。在真实沙箱里，对后端快照 `f749c57e…` 的副本调用 `run_verification`：

| 场景 | 结果 |
| --- | --- |
| 任务没有改 `backend/` 下的文件 | 命令不运行 |
| 做一处无害的改动 | 通过，1.7 秒 |
| 把统一错误格式里 404 的错误码从 `not_found` 改成 `missing` | 拒绝交付，输出是 `FAILED tests/test_health_and_conventions.py::test_unknown_path_uses_unified_error_format`、`1 failed, 31 passed` |

重放的条件：快照副本里的 `.venv` 用运行当时的解释器重建（虚拟环境不能换位置）；平台连接的数据库用一个临时 SQLite 文件代替，没有碰真实的项目数据库。

单元测试：`tests/test_delivery_verification.py`，5 个，都在真实沙箱里执行命令（命令名的识别；失败的命令在同一轮拒绝交付；拒绝 2 次后带着失败事实放行；只改文档或别的目录时不跑、回合结束的交付不拒绝、没有命令覆盖的目录会说明；引用了不存在的截图）。`tests/test_collaboration.py::test_identical_browser_screenshots_share_one_file` 改成新文件名并加了哈希前缀相撞的情形。

**和计划的差别**：

1. 计划写“平台在验收时重跑这些命令并把退出码交给 Leader”。实际在**交付时**跑，失败先回到成员自己这一轮，过了 2 次上限才交给 Leader。原因是弱模型的 Leader 看到失败只会退回，一次退回在 Blog10 里是 20–40 分钟；在成员手里当场修是几分钟。Leader 看到的仍然是平台自己跑的结果，不是成员的描述。
2. 计划写截图“改成短编号”。实际是 `shot-<页面>-<短哈希>`，不是递增编号。编号需要一个跨任务的计数器，两个任务同时截图会得到相同的名字，合并时冲突；内容哈希没有这个问题，页面名让它可读。
3. 计划没写“没有命令覆盖”怎么处理，实际加了一条说明（见上表）。
4. 计划没写开发成员怎么告诉平台哪条命令是测试。实际靠登记名的约定，所以给前端、后端开放了登记命令的工具。

**没验证的**：前端 `vitest` 这条路没有真实数据可重放（Blog10 没有前端测试命令），只有单元测试里的模拟命令。提示词改动要等下一次带模型的运行。

---

## 3. 5.6 把审查需要的事实交给 Leader（根因 A6）

**根因回顾**：Leader 审查时只能看成员在合并之前写下的描述，也看不到服务状态。Blog10 04:47 后端管理模块被退回，理由是“正式服务上管理登录还是 404”，而平台当时正在迁移和重启这个服务；05:26 Leader 为了三个事实问题（缓存有没有被合并等）派了一个只读任务。

**改动一：审查事实**（新文件 [review.py](../../backend/app/collaboration/review.py)，接入 [service.py](../../backend/app/collaboration/service.py) 的 `inbox()`、`publish_artifacts()` 和 [tools.py](../../backend/app/collaboration/tools.py) 的 `inspect_request`）

审查通知和 `inspect_request` 的结果里多一个 `facts`，内容是平台自己掌握的，查看时现算：

| 键 | 内容 | 例子 |
| --- | --- | --- |
| `merged` | 合并进项目的文件，最多列 15 个 | `3 file(s) merged into the project: frontend/package.json, …` |
| `not_merged` | 工作区里没合并的东西和原因（5.4 的 `left_out`，加上没有被引用的截图） | `frontend/dist/ (1 file): build output is never merged` |
| `checks` | 5.5 里平台重跑的每条命令的结果 | |
| `migration` | 合并后平台在项目根跑的迁移命令和结果 | |
| `services` | 每个登记的服务现在从哪里运行、用的是哪份代码 | `web (port 5173): running from the project root, restarted after this merge: it serves the merged files` |
| `in_progress` | 平台还在为这次合并迁移或重启服务时出现 | 提示此刻服务的表现不算数，稍后再看 |

`services` 区分这些状态：平台正在重启；从项目根运行且在这次合并之后重启过；从项目根运行但启动早于这次合并改动的文件（列出文件）；这次合并没改到它的目录；还在本任务或别的任务的快照里运行；平台没能把它启动到项目根；已停止。

**改动二：审查通知等平台忙完再发**。[processes.py](../../backend/app/execution/processes.py) 新增 `track_merge()` 和 `merge_settling()`，记录每次合并之后平台的后台工作（迁移、安装、重启、把保留的服务迁到项目根）。`inbox()` 在这些工作结束之前不把这个任务的审查通知交给 Leader，最长等 900 秒（`REVIEW_HOLD`），超过就照发并带上 `in_progress`。

**改动三：Leader 的只读项目工具**（[leader.py](../../backend/app/agents/leader.py)、[team.py](../../backend/app/agents/team.py)、[tool_profiles.py](../../backend/app/agents/tool_profiles.py)）：`read_project_file`、`search_code`、`browse_directory`，范围是项目根。单次读取上限 6,000 字符（`LEADER_READ_LIMIT`），返回内容最多 12,000 字符。和工作成员用的是同一套工具实现，`.env` 这类文件照样拒绝。提示词写明：只核实具体事实，不逐行审代码，不为核实事实派任务。

**验证一：端到端**（真实 npm 和 Vite，服务从项目根运行，一次交付改了清单）

| 时间 | 观察 |
| --- | --- |
| 交付后 0–15 秒 | 平台停服务、装依赖、重启，页面拒绝连接。Leader 的收件箱里没有这条审查通知 |
| 15.3 秒 | 通知出现，带 `merged`（3 个文件）和 `web …: running from the project root, restarted after this merge: it serves the merged files` |

这对应 Blog10 04:47 的场景：Leader 不会再在服务重启到一半时审查。

**验证二：Blog10 05:26 的三个核查问题**，用新的只读工具对 Blog10 的项目根直接回答，每个 1–3 毫秒；读 `.env` 被拒绝。

单元测试：`tests/test_review_facts.py`，3 个（通知等平台忙完并带上事实；每种服务状态的描述；Leader 自己核实一个项目事实）。

**和计划的差别**：

1. 计划写“重放 Blog10 的两个场景”。实际是按 04:47 的条件搭了真实场景（验证一），05:26 的问题是对真实的 Blog10 项目根回答的。两者都没有调用模型。
2. “等它结束再发”实现在读收件箱的时候，不是在发事件的时候。事件照常写入，所以平台重启不会丢通知；代价是合并的后台工作只记在内存里，后端进程重启后不再等待，通知直接发出。
3. facts 用英文写，和平台其他通知、工具结果一致。
4. 计划说只读工具的代价是“每次 Leader 调用多带几个工具的定义”。实测三个工具的定义占 1,031 token；Leader 的固定开销合计从 13,096 涨到 14,828，其余是提示词的增加，见第 5 节。

**没验证的**：Leader（qwen3.8-max）看到 facts 之后是否真的按它判断、是否会滥用只读工具去逐行审代码，要等带模型的运行。

---

## 4. 5.7 浏览器验证（根因 B）

**根因回顾**：运行历史里 308 次 `browser_verify` 调用有 187 次失败。最大的几类不是页面的问题：选择器匹配到多个元素（Playwright 的严格模式）、断言只差空白、等待超时但结果里没有任何页面内容可以用来改选择器。

**改动**（[browser_runner.py](../../backend/app/execution/browser_runner.py)、[browser_verify.py](../../backend/app/agents/tools/browser_verify.py)、[validation.py](../../backend/app/agents/tools/validation.py)）：

| 情况 | 原来 | 现在 |
| --- | --- | --- |
| `wait_visible` 匹配到多个元素 | 严格模式报错 | 任意一个匹配可见就通过 |
| `assert_text` 匹配到多个元素 | 严格模式报错 | 对所有匹配的文字判断，任意一个包含预期文字就通过 |
| `assert_text` 的比较 | 立即比较，逐字符 | 先等最多 5 秒（`TEXT_WAIT`）让文字出现；空白归一化；只差空格和换行时通过并注明 `matched ignoring spaces and line breaks` |
| `click`、`fill` 等匹配到多个元素 | 严格模式的原始报错 | 新错误码 `ambiguous_selector`：写明匹配了几个，逐个列出编号、标签、类名、文字，并给出写法（`>> nth=0`，或更具体的选择器） |
| 选择器语法错误 | 归为浏览器故障 | 新错误码 `invalid_selector`，归为参数错误，给出语法提示 |
| 选择器没有匹配 | 超时 | `selector_not_found`：写明匹配数为 0 |
| 任何失败 | 只有错误信息 | 返回当时的 `url`、`title`、页面文字 `text`（3,000 字符以内）和 `dom`：一份压缩的元素清单（标签、id、类名、`data-testid`、`role`、`name`、`placeholder`、`aria-label` 等属性和文字，6,000 字符以内；重复的容器只展开一个，其余写“还有 N 个同样结构的”；隐藏的子树只留名字；密码框的值不显示） |
| 动作数量上限 | 20，工具说明里没写 | 30（`MAX_ACTIONS`），写进工具说明 |
| 工具说明 | | 写明选择器的规则：多个匹配时怎么选、文字选择器的写法 |

**验证一：真实 Chromium**（真实沙箱里，一个固定页面）：`tests/test_native_sandbox.py::test_native_browser_selectors_text_and_failure_report` 覆盖上表每一类；原有的浏览器测试改为显式传 `screenshot: True`，并加了“不截图时不产生文件”的断言。

**验证二：对运行历史里 187 次失败逐条归类**（页面本身已经不在，只能按记录下来的参数和报错判断新规则下的结果）：

| 次数 | 原来的失败 | 新规则下 |
| --- | --- | --- |
| 38 | `wait_visible` 匹配多个元素 | 通过 |
| 12 | 断言只差空格或换行 | 通过 |
| 2 | 动作超过 20 个 | 接受（都不超过 30） |
| 10 | `assert_text` 匹配多个元素 | 按所有匹配的文字判断，结果取决于页面 |
| 10 | `click` 等匹配多个元素 | 仍然失败，但带编号清单和写法 |
| 46 | 等待超时 | 仍然失败，但带匹配数、页面文字和元素清单 |
| 2 | 选择器语法错误 | `invalid_selector` |
| 48 | 文字确实不同 | 仍然失败（现在先等 5 秒） |
| 19 | 页面错误 14、导航失败 4、参数错误 1 | 不变 |

计划预期“匹配多个元素（58 次）和空白差异这两类应当消失”。实际是：58 次里 38 次直接通过，10 次改为按全部匹配判断，10 次点击类仍然需要模型选一个，但现在有清单可选；空白差异 12 次全部通过。直接消失的失败是 52 次（187 次的 28%）。

**和计划的差别**：

1. `assert_text` 多了最多 5 秒的等待。记录里有一批断言在页面数据还没加载完时就比较了，这和空白差异是同一类“不是页面的问题”。
2. 动作上限选了“提高并写明”，值是 30。
3. 点击类动作没有做“默认点第一个”。点错元素是无声的错误，比报错更难发现，所以仍然要求模型明确选择。

**没验证的**：48 次“文字确实不同”里有多少会因为 5 秒等待而通过，记录里看不出来。

---

## 5. 这些改动带来的行为变化

| 变化 | 影响 |
| --- | --- |
| 被 `.gitignore` 忽略的本地文件不再复制进任务快照 | 项目里被忽略但运行需要的本地文件（例如手工放的本地配置、数据文件）任务里看不到。`.env` 类本来就不进快照，连接信息由平台注入，不受影响 |
| `.gitignore` 写得太宽（例如 `lib/`）会把源码挡在合并之外 | 不会无声发生：声明为交付物时当场拒绝并指出规则，否则出现在 `not_merged` 事实里 |
| 登记的测试或构建命令失败会挡住交付，最多 2 次 | 用例本身写错、或被别人的改动弄坏时，成员多花一到两轮；第 3 次带着失败事实放行 |
| 审查通知可能晚到 | 一般是几秒到十几秒（P0 实测一次安装加重启 8–16 秒），上限 900 秒 |
| 开发成员默认拿不到截图 | 前端要看页面效果必须传 `screenshot=true` |
| 截图文件名变了 | 旧的 `browser-<哈希>.png` 仍然被识别为截图，已有报告里的引用不受影响 |
| 各角色的固定开销增加 | 见下表 |

固定开销（提示词加工具定义，token）：

| 角色 | 改动前 | 改动后 | 原预算 | 新预算 |
| --- | --- | --- | --- | --- |
| leader | 13,096 | 14,828 | 13,500 | 15,000 |
| backend | 13,810 | 14,645 | 14,000 | 15,000 |
| frontend | 14,593 | 15,942 | 15,000 | 16,000 |
| testing | 14,070 | 14,536 | 14,500 | 15,000 |
| product | 10,326 | 10,416 | 10,500 | 不变 |
| designer | 10,051 | 10,141 | 10,500 | 不变 |

按你之前定的规则（预算是软的，不为凑预算删说明），我把 [test_overhead_budget.py](../../backend/tests/test_overhead_budget.py) 里四个角色的预算上调了，注释里写了日期和原因。这会让每次调用的输入多 0.5–1.7K token，和缓存方案（`2026-10-07-cache-hit-rate-plan.md`）不冲突：增加的都在固定前缀里。

---

## 6. 需要你决定或确认的

2026-11-20：第 1–10 项你已确认（回复“ok”），按“现在的做法”一列执行。

| # | 事项 | 现在的做法 | 备选 |
| --- | --- | --- | --- |
| 1 | 计划决定表第 6 项：Leader 只读工具 | 做了（和平台附带事实一起） | 只留平台附带的事实，去掉三个工具，Leader 的固定开销少 1,031 token |
| 2 | 计划决定表第 7 项：截图命名 | `shot-<页面>-<短哈希>.png` 加交付时核对 | 保持哈希名只加核对；或做递增编号（需要解决跨任务重名） |
| 3 | 交付检查失败的拒绝次数 | 2 次，之后带着失败放行给 Leader | 1 次；不设上限（风险：任务外的原因会让成员卡死）；不拒绝，只把结果交给 Leader（计划原来的写法） |
| 4 | 审查通知最长等待 | 900 秒，等于根目录安装的时限 | 更短 |
| 5 | `assert_text` 的等待 | 5 秒 | 不等；更长 |
| 6 | 浏览器动作上限 | 30 个（单个动作的等待没有改，仍是 10 秒） | 保持 20 |
| 7 | Leader 单次读文件上限 | 6,000 字符 | 更大会让 Leader 的上下文涨得快 |
| 8 | 固定开销预算上调 | 见第 5 节的表 | 你指定要压缩的角色 |
| 9 | 测试、构建命令靠登记名识别 | 名字里含 `test`、`tests`、`build` 一段 | 登记命令时加一个明确的“用途”字段。更清楚，但多一个参数，弱模型多一个填错的地方 |
| 10 | Blog10 项目根里已合并的 `frontend/.vite` | 没动，那是测试工作区 | 下次同题运行从空项目开始 |

P0 留下的两项你 2026-11-20 已确认（回复“Yes, both”）：`RLIMIT_CPU` 取 14,400 秒；没有标记的根目录依赖目录在第一次重启服务时重装一次。

---

## 7. 改动的文件

| 文件 | 内容 |
| --- | --- |
| `backend/app/collaboration/workspaces.py` | 5.4 `Ignore`、`files()`、`plan()`、`_refresh()`、`unmerged_reason()`、`left_out()`；5.5 截图名和引用的识别、`missing_screenshots()`、`changed_paths()` |
| `backend/app/collaboration/review.py` | 新增。5.5 `delivery_verification`；5.6 `review_facts`、`review_waits`、`service_fact` |
| `backend/app/collaboration/service.py` | `inbox()` 的等待和 facts；`publish_artifacts()` 接入交付检查，记录 `merged_at`、`left_out`、`checks`、`migration` |
| `backend/app/collaboration/environment.py` | 5.5 `run_verification`、`verification_line` |
| `backend/app/collaboration/tools.py` | 成员主动交付时允许拒绝；`inspect_request` 带 facts |
| `backend/app/collaboration/runtime.py`、`backend/app/execution/processes.py` | 5.6 记录每次合并之后的后台工作 |
| `backend/app/execution/browser_runner.py` | 5.7 |
| `backend/app/agents/tools/browser_verify.py` | 5.7 动作上限和说明；5.5 `screenshot` 参数和文件名 |
| `backend/app/agents/tools/validation.py` | 新错误码的归类 |
| `backend/app/agents/leader.py`、`team.py`、`tool_profiles.py` | 5.6 只读工具；5.5 开发成员可登记命令 |
| `backend/app/agents/prompts/` 下 `frontend.md`、`backend.md`、`testing.md`、`leader.md`、`modules/worker.md`、`modules/leader.md`，`backend/app/agents/base.py` | 5.5、5.6 的提示词 |
| `backend/tests/test_ignored_paths.py` | 新增，7 个 |
| `backend/tests/test_delivery_verification.py` | 新增，5 个 |
| `backend/tests/test_review_facts.py` | 新增，3 个 |
| `backend/tests/test_native_sandbox.py` | 新增 1 个，原有浏览器测试跟着改 |
| `backend/tests/test_collaboration.py`、`test_tool_feedback.py`、`test_overhead_budget.py`、`test_team.py` | 截图名、新错误码的归类、预算、Leader 的工具清单 |

验证脚本在本次会话的临时目录里（可能被清理），都不调用模型：

- `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/ignore_fuzz.py`：和 `git check-ignore` 对照
- `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/ignore_replay.py`：Blog10 快照的合并清单，旧规则对新规则（`--pre` 是第一次合并之前的状态）
- `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/replay_checks.py`：Blog10 的 `test` 命令在快照副本上重跑
- `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/verify_review.py`：审查通知的等待和 facts，真实 npm 和 Vite
- `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/leader_reads.py`：Leader 的三个核查问题
- `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/bv_replay.py`、`/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/bv_stats.py`：浏览器验证失败记录的归类
- `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/overhead.py`：各角色固定开销

---

## 8. 追加（2026-11-20）：5.8 思考预算，5.9 上下文预算

你问“can we add thinking budget”，并决定把上下文预算改成 100K。两件事都是模型调用的参数，先用真实调用量了再改。

### 8.1 思考预算（5.8，根因 C1）

**改动**（[base.py](../../backend/app/agents/base.py) `thinking_budget()`、`model()`）：

| 设置 | 作用 |
| --- | --- |
| `WDRUNGE_THINKING_BUDGET` | 每次调用最多用多少 token 思考。不设时用默认值 2,000；设成 0 表示不限（不传参数） |
| `WDRUNGE_THINKING_BUDGET_<角色>`（如 `WDRUNGE_THINKING_BUDGET_LEADER`） | 某个角色自己的值，优先于上面那个；设成 0 表示这个角色不限 |

工作调用带上 DashScope 的 `thinking_budget` 参数。摘要调用不变，仍然完全关闭思考。**默认值是 2,000**：你 2026-11-20 看了下面的数据后定的，写在代码里（`THINKING_BUDGET`），`backend/.env` 不用改。

**验证一：参数是否生效**（真实调用，兼容接口，流式，带一个工具；题目是一个需要推理的排班题，要求用工具提交答案）

| 模型 | 预算 | 思考 token | 输出 token 合计 | 耗时 | 工具调用 |
| --- | --- | --- | --- | --- | --- |
| qwen3.8-max | 不设 | 18,234 | 18,954 | 404 秒 | 完整 |
| qwen3.8-max | 2,000 | 2,000 | 2,594 | 58 秒 | 完整 |
| qwen3.8-max | 500 | 500 | 1,108 | 26 秒 | 完整 |
| qwen3.8-max | 100 | 100 | 506 | 14 秒 | 完整 |
| qwen3.8-max | 关闭思考 | 0 | 1,503 | 17 秒 | 完整 |
| qwen3.7-plus | 2,000 | 2,000 | 2,420 | 41 秒 | 完整 |
| qwen3.7-plus | 500 | 500 | 1,314 | 24 秒 | 完整 |
| qwen3.7-plus | 100 | 100 | 988 | 18 秒 | 完整 |

- 思考 token 每次都正好等于预算，没有超出。
- 思考被截断后，工具调用的 JSON 每次都完整（5 条排班）。
- 经过后端自己的路径（`Agent.model()`，LangChain 的 `ChatOpenAI` 加 `extra_body`）再测了三次：预算 300、1,000（qwen3.8-max）和 300（qwen3.7-plus），思考 token 正好是 300、1,000、300，平台记录用量的字段（`reasoning_tokens`）读到的也是这个数。

**验证二：答案对不对**。排班题有 3 个合法解（穷举得到）。qwen3.8-max 在预算 100、500、1,000、2,000、4,000 下各问 3 次：完成的 13 次全部答对，另外 2 次是网络连接错误，没有结果。

**截断之后正文有没有变长**：没有明显变长。带预算时“输出合计减思考”是 400–1,000 token，不限思考时是 720。完全关闭思考则不同：推理全部写进正文（qwen3.8-max 1,503 token；qwen3.7-plus 5,321 token、87 秒），而正文会留在对话历史里。所以要缩短思考，用预算比关闭思考合适。

**和计划的差别**：计划写“数值可按角色或环境变量配置”，两种都做了。计划建议从 4,000 开始；看到截断之后工具调用完整、答案仍然正确，我改为建议 2,000，你定了 2,000。

**没验证的**：一道题不能代表真实任务里的决策质量。思考变短之后调用次数和返工次数会不会增加，只有完整运行能看出来。qwen3.7-plus 预算 2,000 的第一次调用 240 秒没有返回，重试一次 41 秒正常完成，原因没有查到。

按 Blog10 的数据推算能省的时间（计划 C1 节的表，上界估算）：4,000 省 3.3%，2,000 省 11.6%，1,000 省 20.5%。Blog10 里思考 token 的 90 分位是 2,195，所以 2,000 大约只碰到最长的一成调用。

### 8.2 上下文预算改为 100K（5.9，根因 C2）

**改动**（[memory.py](../../backend/app/agents/memory.py) `context_tokens()`）：`WDRUNGE_CONTEXT_TOKENS` 的默认值从 56,000 改成 100,000。压缩、分段摘要这些阈值都是按预算的比例算的，跟着一起变，没有别的常数要改。

**一个你需要知道的限制**：代码里还有一条“预算不超过窗口的四分之三”，窗口默认按 131,072 算（`QWEN_CONTEXT_WINDOW`，没有设），所以实际生效的是 98,304，比 100K 少 1.7%。下面的实验里 qwen3.8-max 接受了 225,089 token 的输入，说明它的真实窗口比 131,072 大。要正好 100K，可以把 `QWEN_CONTEXT_WINDOW` 设成模型的真实窗口；我没有改，因为这个值还决定给摘要模型（qwen3.7-plus）一次送多少内容，而它的窗口我没有测。

**实验：首 token 时间和上下文长度的关系**（计划 5.9 写的那个实验。qwen3.8-max，关闭思考，只要 1 个输出 token，每个长度先调 1 次冷的，再调 4 次命中缓存的）

| 输入 token | 冷（没有缓存） | 命中缓存（中位） |
| --- | --- | --- |
| 44,118 | 4.8 秒 | 3.3 秒 |
| 88,345 | 6.2 秒 | 4.3 秒 |
| 133,106 | 8.0 秒 | 4.5 秒 |
| 177,819 | 13.4 秒 | 5.2 秒 |
| 225,089 | 12.7 秒 | 5.6 秒 |

20 次命中缓存的调用里有 1 次是 58.9 秒，另有 1 次 11.9 秒，其余在 3.0–6.2 秒之间。

**这推翻了我在计划里的一个判断**。计划第 2.2 节按 Blog10 的数据拟合出“每 10K 上下文约 4.7 秒”，C2 节据此说 30K 以上首 token 时间翻倍，并建议实验之前不要提高预算。实验量到的是：命中缓存时每 10K 约 0.1 秒，冷的时候约 0.5 秒。从 56K 到 100K，每次调用大约多 0.5–1 秒，冷的时候多 2 秒左右，不是 20 秒。Blog10 里上下文越长调用越慢是事实，但原因不是读上下文本身，具体是什么我没有查清。计划里那两处已经加了更正。

**代价主要是钱**。缓存方案（`2026-10-07-cache-hit-rate-plan.md`）算过 90K：输入 token 比 56K 多 9–14%，费用高约 10%，命中率比 72K 再高 0.4 个百分点。100K 没有单独算，应该比这略高。

**没验证的**：实验用的是合成文本、没有工具定义、思考关闭，只在一个时间段测了一轮。100K 历史下模型的工作质量（读得更多会不会更容易跑偏）没有测，要看完整运行。

### 8.3 测试和脚本

- 新增 `tests/test_context_tiers.py::test_a_thinking_budget_applies_to_work_calls_and_a_role_can_have_its_own`。后端全部测试 590 通过、2 跳过。
- 脚本（临时目录，可能被清理；都会用 `backend/.env` 里的密钥调用 DashScope，这一轮总共约 50 次调用）：
  - `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/thinking_budget.py`：各预算下的思考 token 和工具调用
  - `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/thinking_once.py`：单次调用，记录时间花在哪
  - `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/thinking_backend_path.py`：经过后端自己的模型路径
  - `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/thinking_quality.py`：各预算下答案是否正确
  - `/private/tmp/claude-501/-Users-yefenghua-Codes-Wdrunge/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/context_ttft.py`：首 token 时间和上下文长度
