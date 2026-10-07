# Blog9 修复实施记录：P0-1、P0-2、P0-3、每个成员一个会话和 execute_command 的 argv

> **对应计划**：[2026-10-06-blog9-run-analysis.md](2026-10-06-blog9-run-analysis.md) 第 10 节的 P0-1、P0-2、P0-3，以及复盘后讨论的 `execute_command` 参数形状。P0-4 按你的要求暂时用“每个成员同时只有一个会话”规避（第 5 节）。
>
> **状态**：已实施，未提交。后端全部测试 553 通过、2 跳过；前端 `tsc --noEmit` 通过。
>
> **没做的**：P0-4 本身（每个任务独立的数据库和服务名，现在用第 5 节的并发限制规避）、P1-1、P1-2、P2-1。

---

## 1. P0-1：任务快照移出项目目录树

**根因回顾**：快照在 `<项目根>/.wdrunge-runs/<实例>/work`。`uv`、`pytest` 会向上找 `pyproject.toml`，找到了项目根的那份，但沙箱只允许读快照，于是报 `Operation not permitted`（报告 3.A2）。

**改动**：

| 位置 | 内容 |
| --- | --- |
| `collaboration/workspaces.py` | 新增 `runs_root()`：默认是应用数据目录**旁边**的 `Wdrunge Runs`（macOS 为 `~/Library/Application Support/Wdrunge Runs`），可用 `WDRUNGE_RUNS_DIR` 改。每个项目一个子目录 `<项目名>-<路径哈希12位>`（`project_runs()`）。 |
| 同上 | `IsolatedWorkspace` 改用新位置。如果新位置没有、项目里还有旧快照（`.wdrunge-runs/<实例>/manifest.json`），就继续用旧的。任务下次开始执行时（`prepare()`），平台把旧快照整体移到新位置。还有进程从旧快照运行时不移（`service.prepare_workspace` 检查）。 |
| 同上 | 新位置落在项目内（例如项目就是 HOME，或 `WDRUNGE_RUNS_DIR` 设到了项目里）时拒绝创建快照，避免快照被复制进快照。 |
| `execution/permissions.py` | `task_created` 不再要求路径里有 `.wdrunge-runs`，改为“在 `runs_root()` 下或旧位置”。 |
| `collaboration/delivery_checks.py` | 交付物里出现 `runs_root()` 路径或 “Wdrunge Runs” 也算平台路径泄漏。 |
| `tests/conftest.py`、`.env.example` | 测试用临时的快照目录；说明 `WDRUNGE_RUNS_DIR`。 |

**为什么放在应用数据目录旁边，而不是里面**：应用数据目录在沙箱规则里整个被拒绝读写（`protected_paths`），文件工具也把它当控制面拒绝（`is_control_path`）。放进去的话，快照本身就不可读了。放在旁边的同级目录，这两条规则都不受影响（Seatbelt 的 `subpath` 按路径分段匹配，`Wdrunge Runs` 不算 `Wdrunge` 的子路径，真实运行已验证）。

**和报告的差别**：报告写的是 “平台数据目录下 `runs/…`”，因为上面这个原因改成了同级目录。`.wdrunge-runs` 相关的排除和保护规则都**保留**，用于还没移走的旧快照。

**验证**（真实沙箱，不调用模型，脚本 `repro_p01.py` 在本次会话的 scratchpad）：项目根有 `pyproject.toml`、`package.json` 和 `frontend/node_modules`，快照由 `IsolatedWorkspace` 创建，在快照里跑同样 4 条命令：

| 快照位置 | `uv lock --offline` | `python -m pytest` | `node main.js`（解析软链接的 node_modules） | `npm ls --offline` |
| --- | --- | --- | --- | --- |
| 旧：项目内 `.wdrunge-runs` | **exit 2**，`…/My Proj/pyproject.toml: Operation not permitted` | **exit 1**，`PermissionError … pyproject.toml` | exit 0 | exit 0 |
| 新：`~/Library/Application Support/Wdrunge Runs/…` | exit 0 | exit 0 | exit 0 | exit 0 |
| 旧快照被移到新位置之后 | exit 0 | exit 0 | exit 0 | exit 0 |

单元测试：`tests/test_root_commands.py` 的 `test_snapshots_live_outside_the_project`（位置、复制、旧快照复用和移动、`task_created`）和 `test_a_runs_directory_inside_the_project_is_refused`。

---

## 2. P0-2：登记的命令要能在项目根运行

**根因回顾**：“已验证”只表示命令在任务快照里跑过。快照里有任务自己建的 `.venv`，项目根没有，平台也只在**命令的 cwd** 里按清单重建 `.venv` / `node_modules`（报告 3.A1）。

### ① 登记时检查（`collaboration/environment.py` 的 `placement_problem`）

`collaboration_register_command` 和 `collaboration_register_service`（带启动命令时）在任务里调用时，平台在**任务快照里**解析 argv[0]：

| 情况 | 结果 |
| --- | --- |
| 解析到 `.venv/` 或 `node_modules/` 里，这个目录就在 cwd 下，cwd 里有对应清单（`pyproject.toml` / `requirements.txt`，或 `package.json`） | 通过：项目根会按这份清单建出同样的目录 |
| 同上，但 cwd 里没有清单 | 拒绝，并列出项目里有清单的目录（例如 “found in: backend”） |
| 依赖目录不在 cwd 下（如 `backend/.venv/bin/python`，cwd 为 `.`） | 拒绝，直接给出正确写法：`cwd="backend"`、`argv[0]=".venv/bin/python"` |
| 解析到其他不合并的目录（`target/`、`dist/` 等构建产物） | 拒绝：平台不会在根目录构建它 |
| argv[0] 是指向快照内部的绝对路径 | 拒绝：项目根没有这个路径 |
| 系统或工具链里的可执行文件（`npm`、`uv`、`python3`），或快照里还解析不到 | 不在登记时判断，交给 ② |

### ② 交付前在干净副本里安装（`check_root_commands`）

- **对象**：本任务登记的命令和服务启动命令（记录里新增 `task` 字段），以及本任务快照里仍在运行的保留服务。
- **做法**：
  1. 先做 ① 的检查。
  2. 再把快照里会合并的文件（与 `files()` 同一规则，不含 `.venv`、`node_modules`、`.env`）复制到 `Wdrunge Runs/<项目>/<实例>-check`。
  3. 用 `processes.prepare_root` 按项目根的同一套逻辑安装依赖，然后解析可执行文件。
  4. 检查完删掉副本。
- **何时运行**：`publish_artifacts` 开始合并之前，并且放在数据库事务之外，因为安装可能要几分钟。不通过就抛出 `ValueError`，开头是 “Not merged”，逐条列出问题，**什么都不合并**。`collaboration_check_delivery` 也会运行它，问题作为 `root_commands` 发现返回，让模型能在提交前看到。
- **缓存**：用命令、cwd 以及 cwd 里各个安装输入文件（`pyproject.toml`、`requirements.txt`、`uv.lock`、`package.json`、各种 lock 文件、`.python-version`）的哈希做指纹。通过后写进记录（`root_checked`），命令和这些文件都不变就不再装第二次。
- **状态分级**：命令记录的 `status` 改成三档：
  - `declared`：只登记了，没跑过；
  - `ran_in_task`：在快照里跑成功过，原来的 “verified” 就是这一档；
  - `verified`：已在项目根运行过，或者通过了 ②。
  - 合并后平台重放迁移命令时，`ran_in_task` 和 `verified` 都按“已授权的平台步骤”处理，和以前一样。

**提示词**（报告 B1）：`backend.md` 改为“依赖清单（pyproject.toml 或 requirements.txt）和 .venv 放在同一个后端目录，登记命令和服务时 cwd 就用这个目录：.venv 不随交付合并，平台在项目根目录按这份清单重建它”。`register_command` 的工具说明也写明了这条规则和三档状态。

**验证**：

- **Blog9 真实数据**（只读，脚本 `replay_b9_registrations.py`）：把 Blog9 全部 9 次登记，放到磁盘上仍保留的 7 个快照里重放。
  - init#1 自己的快照 `e386e30b` 里，它的 6 次登记全部被拒：`api` 服务 2 次，migrate、export-openapi、seed、backend-test 各 1 次。
  - 回执是：`.venv/bin/python inside .venv/, which is never merged … cwd "." has no pyproject.toml or requirements.txt. Keep the manifest in the directory the command runs in (found in: backend; set cwd to that directory and create its .venv there).`
  - 也就是说，第一次返工（2.10M token）的原因会在登记时就指出来，而不是合并后才失败。
  - init#2 的 `uv run …` 和 `uv sync --frozen`，以及前端的 `npm run dev`，都通过了登记检查。
- **单元测试和真实沙箱**（`tests/test_root_commands.py`，用真实的 Seatbelt，真实创建虚拟环境）：
  - 登记规则的 9 种情况。
  - 任务里通过工具登记：拒绝时的回执，以及记录里的 `task` 字段。
  - 干净副本检查通过 → `verified` 加 `root_checked`，副本被删掉；输入不变时第二次不再安装。
  - `uvicorn` 只装在任务自己的 `.venv` 里、清单没声明它：交付被拒，原因是 “找不到启动命令 uvicorn”，项目里一个文件都没合并。改成清单能提供的命令后，交付成功合并，`.venv` 没有被合并。
  - 跳过登记检查写进去的旧式记录（Blog9 init#1 那种）：在交付时被拒，回执带 “found in: backend”。

**已知代价**：干净副本检查做的是真实安装，每条命令的沙箱缓存目录都是新建的，所以每次都要重新下载依赖（FastAPI 一套大约几十秒，`npm ci` 可能一两分钟）。指纹缓存只在清单不变时省掉重复检查。这段时间只花在交付时，不消耗 token。如果实际运行觉得慢，可以考虑给平台安装步骤一个按项目共享的包缓存目录。

---

## 3. execute_command 改成一个 argv 数组

**改动**：

- `execute_command(command, arguments, …)` 改为 `execute_command(argv, …)`。`argv` 是至少一个元素的字符串数组，说明里写明“需要管道或 && 时用 `["sh", "-c", "<整行>"]`”。
- `collaboration_register_service` 的 `command` 改名为 `argv`，`collaboration_register_command` 本来就是 `argv`。三个工具现在用同一个名字、同一种形状。
- 前端环境面板登记服务时也改成发 `argv`。
- **已知的错误形状由平台纠正**：在 `app/agents/tools/arguments.py` 新增按工具的 `SHAPERS`，在通用的参数处理之前运行。纠正的内容会通过 notice 告诉模型，免得它下次再犯，同时记在调用日志的 `corrected` 里。
  - 旧的 `command` + `arguments` → 合并成 `argv`。如果 `arguments[0]` 重复了可执行文件，去掉重复的那个。
  - 只发了 `arguments`（Blog9 15:12:22 那次）→ 当作 `argv`。
  - `argv` 是一行字符串、或者第一个元素是一整行命令 → 按 shell 的引号规则（`shlex`）拆开。第一个元素本身是带空格的真实路径（例如 “Agent Test Workspace”）时不拆。
  - 字符串里在引号外出现 `| & ; < > ( ) $ \` * ? [ ] { } ~` 或换行时**不猜**，直接拒绝，并给出 `["sh", "-c", "<整行>"]` 的写法：这类语法只有 shell 能解释。
  - `arguments` 不是数组、也解析不成合法的 JSON 数组时拒绝，不会丢掉参数后执行。
- 读历史的地方（上下文折叠、进度判断）通过 `call_argv()` 同时认新旧两种形状，所以已有会话的历史和 checkpoint 不受影响。

**验证**（脚本 `replay_argv.py`）：把 Blog3～Blog9 记录下来的全部 1,141 次 `execute_command` 调用，用新的参数处理和新 schema 重放：

| 结果 | 次数 | 说明 |
| --- | --- | --- |
| argv 与当时实际执行的完全相同 | 1,127 | 包括 2 次当时因为别的原因失败的调用 |
| 当时因为参数形状失败、现在按原意执行 | 10 | 重复可执行文件 5 次（当时都以 1、2 或 126 退出），整行写在 `command` 里 4 次，只发了 `arguments` 1 次 |
| 拒绝，并给出具体写法 | 4 | 3 次带管道的整行命令，1 次 `arguments` 是不合法的数组字符串 |
| 当时能运行、现在含义被改变 | **0** | |

单元测试：`tests/test_tool_arguments.py` 新增 20 个用例（各种形状、拒绝的情况、带空格的真实路径、登记工具、读历史）。

---

## 4. 对正在暂停的 Blog9 的影响

- init#3 和登录任务的快照还在 `Blog9/.wdrunge-runs/` 里。继续运行时，任务一开始就会被移到 `Wdrunge Runs/`，之后快照里的 `uv` 和 `pytest` 不再读到项目根的 `pyproject.toml`。
- 它们已登记的命令仍是旧记录（`status: verified`，没有 `task` 字段），② 不会去检查。只有被任务重新登记，或者以后新任务登记的命令，才会被检查。
- 已经运行中的模型会话从新的工具定义里看到 `argv`。历史里旧的 `command`/`arguments` 调用不影响读取；模型如果照旧形状调用，平台会合并参数并提示一次。
- 两个任务恢复后同一时间只会有一个在运行（第 5 节）。登录任务提交时如果 init 还没验收，会被挂起等待，而不是被拒或变成合并冲突。

---

## 5. P0-3 和每个成员一个会话

### 每个成员同时只有一个会话（暂时规避 P0-4）

`WDRUNGE_ROLE_CONCURRENCY` 的默认值从 2 改成 1（`service.py`，`.env.example` 有说明）。同一成员的两个任务不再同时运行，所以不会再出现“两个会话共用一个数据库和一个服务名、互相替换服务、在共享库上各自迁移”（报告 3.A3 ②③④）。

不同成员仍然并行（`WDRUNGE_MAX_CONCURRENCY` 默认 4 不变），后端和前端可以同时干活。等 P0-4（每个任务独立的数据库，以及只能替换自己启动的服务）做完，可以再把这个值调回 2。

### P0-3：依赖什么时候放行

按报告决定表里推荐的（a）实施，再补上“交付暂缓”：

| 情况 | 以前 | 现在 |
| --- | --- | --- |
| 依赖是**同一成员**的任务，处于待验收 | 下游马上开工（Blog9 登录任务 15:29:33 在 init 提交 0.2 秒后开工） | 等依赖**验收通过**才开工。同一成员的下一个任务改的是同一套代码，提前开工收益很小，而且上游一被退回就白做 |
| 依赖是**其他成员**的任务，处于待验收 | 下游开工 | 不变：前端可以按后端已交付的契约先做 |
| 下游做完，但依赖还没验收 | 提交被拒，回执只有 “Accept dependency deliveries before publishing artifacts”；如果是轮次结束时平台自动交付，任务会被标成 `merge_conflict`，调度器永远不会再拾起它 | **交付暂缓**（`hold_for_acceptance`）：什么都不合并，快照和上下文都保留，任务变成 `blocked` / `awaiting_acceptance`。回执写明在等哪个任务、等的是验收，本轮自动结束。Leader 收到 `delivery_waiting` 事件 |
| 被暂缓的任务什么时候继续 | 无 | 依赖全部**验收通过**后，调度器按原上下文恢复它，并告诉它“这些依赖已验收，验收后的文件已在你的快照里：确认你的工作仍然对得上，再提交一次”。依赖在此期间被退回，它就继续等返工的那一版验收 |

**代码**：
- `service.py`：
  - `claim()` 里的放行条件；
  - 新增 `AwaitingAcceptance` 异常和 `hold_for_acceptance()`；
  - `publish_artifacts` 在做安装检查之前就先判断依赖是否都已验收，免得白装一次依赖。
- `tools.py`：`deliver()` 遇到这种情况时把任务暂缓；`submit_task` 的回执现在会显示 `waiting_for` 和 `note`。
- `runtime.py`：轮次结束时的自动交付遇到同样情况时也暂缓，不再标记 `merge_conflict`。

**验证**（`tests/test_root_commands.py`）：
- 按 Blog9 的时间线构造：
  - init 进入待验收后，前端任务开工，同一成员的登录任务不开工；
  - init 被退回时，登录任务仍在排队；
  - init 验收后，登录任务才开工；
  - 默认并发为 1。
- 交付暂缓：
  - 回执、Leader 事件；
  - 依赖待验收或被退回期间不恢复；
  - 依赖验收后按原上下文、原快照恢复。
- 轮次结束时的自动交付：变成 `awaiting_acceptance`，不是 `merge_conflict`，项目里没有合并任何文件。
- `test_collaboration.py` 里有 3 个测试本来就是测同一成员同时跑两个任务的，改为在测试里显式设置并发为 2。
