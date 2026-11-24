# 运行时间 P0 修复实施记录：命令资源上限、交付后的依赖安装与重启、快照里的依赖目录

> **对应计划**：[2026-11-19-run-time-root-cause-plan.md](2026-11-19-run-time-root-cause-plan.md) 第 5.1、5.2、5.3 节（根因 A2、A1、A3）。你 2026-11-19 说“开始修复 p0”，决定表第 2、3 项按建议值（4,096；写时复制克隆）。
>
> **状态**：已实施，未提交（分支 `context-desk`）。后端全部测试 573 通过、2 跳过（改动前 553 加这次新增的 20 个）。真实环境验证见各节，用的是真实的 `sandbox-exec`、npm 10.9.2、Node 22.16、Vite 6 和 Blog10 的前端工程。
>
> **没做的**：P1、P2 的全部条目（5.4–5.10，包括你已同意的 5.5 验证分工）。没有跑带模型的完整运行，计划第 6 节的验收（下一次同题运行）还没做。
>
> **你的确认**（2026-11-20，回复“Yes, both”）：第 5 节第 1、2 项按现在的做法定下来：`RLIMIT_CPU` 取 14,400 秒；项目根已有、但不是平台安装的依赖目录，第一次重启服务时会重装一次。

---

## 1. 5.2 命令的资源上限（根因 A1）

**根因回顾**：沙箱里每个命令的文件句柄上限是 256。Vite 打包 Element Plus 时句柄用完，Rollup 报的是 `Rollup failed to resolve import "@vue/runtime-dom"`，不提句柄。Blog10 前端为此花了 38 次调用、20.5 分钟，最后在项目配置里加了 `maxParallelFileOps` 绕过。

**改动**（[limit_exec.py](../../backend/app/execution/limit_exec.py)）：

| 上限 | 原来 | 现在 | 说明 |
| --- | --- | --- | --- |
| `RLIMIT_NOFILE` | 256 | 4,096 | 你定的值 |
| `RLIMIT_CPU` | 600 秒 | 14,400 秒 | 我定的值，等于保留服务的默认寿命（4 小时） |
| `RLIMIT_CORE`、`RLIMIT_FSIZE` | 0、64 MiB | 不变 | |

- 新增 `cap()`：宿主自己的硬上限更低时取宿主的值，不会因为设不上去而启动失败。
- `RLIMIT_CPU` 是单个进程所有线程的 CPU 时间之和。8 核上并行构建 2 分钟就可能超过 600 秒，长时间运行的开发服务也会累计到。平台已经有墙钟上限（命令最长 30 分钟，保留服务按寿命），CPU 上限只留作“进程逃出进程组之后”的兜底，所以提高到正常工作碰不到的值，没有按“是否保留服务”区分。原因是服务可以在启动后才被标记为保留（`settle_delivery`），而 macOS 不能从外部改一个运行中进程的上限。

**验证**（真实沙箱）：把 Blog10 的前端还原成初始化任务当时的样子（去掉 `maxParallelFileOps`、去掉 `--configLoader runner`、缓存目录用默认值），整包引入 Element Plus，在 `frontend` 目录跑 `npm run build`：

| 句柄上限 | 结果 |
| --- | --- |
| 256（命令里用 `ulimit -n 256` 降回去） | 退出码 1，`[vite]: Rollup failed to resolve import "@vue/runtime-dom" from ".../node_modules/vue/dist/vue.runtime.esm-bundler.js"`，和 Blog10 03:01 的报错一致 |
| 4,096 | 退出码 0，`✓ built in 2.35s`，整条命令 4.0 秒 |

单元测试：`tests/test_native_sandbox.py::test_commands_get_room_for_bundlers_and_long_services`（真实沙箱里同时打开 1,500 个文件，并检查两个上限的值）。

---

## 2. 5.3 快照里的依赖目录（根因 A3）

**根因回顾**：快照里的 `node_modules` 是指向项目根的链接，沙箱只给“命令所在目录的那一个”读权限，不给写权限。npm workspaces 把依赖提到根目录之后，从 `frontend` 读根目录的包报 EPERM，从根目录构建时 Vite 往 `frontend/node_modules/.vite-temp` 写文件也报 EPERM。Blog10 花了 19 次调用、21 分钟，最后改了项目的构建脚本。

**改动**：

| 位置 | 内容 |
| --- | --- |
| [workspaces.py](../../backend/app/collaboration/workspaces.py) | 新增 `clone_tree()`。`IsolatedWorkspace.prepare()` 不再链接，改为把项目里每个 `node_modules` 克隆进快照。macOS 上用 `copyfile(3)` 的递归克隆（APFS 写时复制，文件系统不支持时由系统退回普通复制）；其他系统用普通复制，目录内的链接保持为链接。 |
| [validation.py](../../backend/app/agents/tools/validation.py) | 新增 `denied_path()`、`permission_advice()`。`os_permission_denied` 不再给同一句“检查沙箱和网络边界”，而是写出被拒绝的路径属于哪一类和对应的做法（见下表）。 |
| [service.py](../../backend/app/collaboration/service.py) `prepare_workspace` | 新快照在平台完成根目录的依赖安装之后才取（等 `processes.settled()`，并持有该项目根的锁）。原因见本节“和计划的差别”。 |
| [processes.py](../../backend/app/execution/processes.py) `start` | 只读授权的逻辑没有删：项目的 `.venv` 仍然只读共享，改动之前创建的旧快照里还有链接。只改了注释。 |

拒绝提示的分类：

| 被拒绝的路径 | 提示的要点 |
| --- | --- |
| 在项目根（任务在快照里运行） | 任务在自己的副本里工作，命令读写不了项目根；有东西用了根目录的绝对路径，改用相对路径，删掉并重新生成记录了旧路径的缓存 |
| 快照内，但经过一个指向工作区外的链接 | 指出是哪个链接、指向哪里，让它删掉链接、在工作区里建自己的目录 |
| 含 `..`，爬出了工作区 | 命令只能读写工作区和临时目录 |
| 工作区和项目根之外 | 把工具的缓存、配置或输出指到工作区或 `$TMPDIR` |
| `.git`、`.agents` 等受保护的名字 | 项目里不允许创建 |
| `.env` 类文件 | 不能读写，用 `.env.example`，连接信息由环境变量提供 |
| 快照内的普通路径 | 沙箱允许，问题在文件自己的权限位 |
| 输出里没有路径 | 是网络边界，带 `network=true` 重跑 |

分类规则用运行历史里全部 27 条 `os_permission_denied` 的真实输出核对过（Blog4、Blog9、Blog10），每一类都进了单元测试。

**测量**（Blog10 的根目录 `node_modules`，160 MB，14,159 个文件）：

| 方式 | 耗时 | 说明 |
| --- | --- | --- |
| `copyfile(3)` 递归克隆 | 1.66 秒 | 采用。苹果推荐的目录克隆方式，不占额外磁盘，直到某一边改动 |
| 对目录直接 `clonefile(2)` | 0.14 秒 | 没采用。手册明确不建议对目录用 |
| 普通复制 | 3.17 秒 | 非 APFS 时的退路，每个快照多占一份磁盘 |

`prepare()` 整体实测 1.79 秒（15,478 个目录项）。

**验证**（真实沙箱，Blog10 的 workspaces 结构，原始构建脚本 `vue-tsc --noEmit && vite build`）：

| 快照里的依赖目录 | `frontend` 目录 `npm run build` | 根目录 `npm run build` |
| --- | --- | --- |
| 链接（按改动前 `prepare()` 的做法手工搭的快照） | 退出码 1，`EPERM … open '<项目根>/node_modules/vue-tsc/bin/vue-tsc.js'`（Blog10 06:21 的报错） | 退出码 1，`EPERM … frontend/node_modules/.vite-temp/…`（Blog10 06:23 的报错） |
| 克隆 | 退出码 0，8.4 秒 | 退出码 0，4.3 秒 |

克隆的快照里还验证了：`npm install is-odd@3.0.1` 退出码 0（3.9 秒），包装进了快照，项目根的 `node_modules` 没有变化，`plan()` 只合并 `package.json` 和 `package-lock.json`。两条“改动前”的失败现在各自得到上表第 1、2 类提示。

单元测试：`tests/test_root_dependencies.py` 的 `test_a_snapshot_has_its_own_writable_copy_of_node_modules` 和 `test_a_refused_path_is_explained_by_the_boundary_it_crossed`（11 种输出）。

**和计划的差别**：

1. 计划没写快照和根目录安装的先后。链接是“活”的，克隆是取快照那一刻的副本。每个角色同时只跑一个任务，下一个任务恰好在上一个交付之后立刻开始，也就是平台正在根目录装依赖的时候。所以加了一步：取快照前等平台的服务迁移和安装结束。等待有上限（每个服务的“重启中”状态最长 120 秒，安装期间最长 900 + 120 秒）。
2. 只克隆 `node_modules`，不克隆 `.venv`。虚拟环境不能换位置：里面脚本的首行和可编辑安装的 `.pth` 都是绝对路径，克隆后会跑到项目根的解释器和源码上。`.venv` 维持原来的做法（快照里没有，命令所在目录的那一个只读共享）。后端任务每次在快照里重新建 `.venv` 的开销没有动，它不在这次的关键路径上。
3. `manage_file` 没改。新快照里没有链接了，旧快照的链接由提示告诉成员用命令删掉。

**没验证的**：Linux（只有普通复制这条路，没有在 Linux 上跑）；pnpm、yarn、bun 的 `node_modules` 结构（只用 npm 实测）。

---

## 3. 5.1 交付后的依赖安装和服务重启（根因 A2）

**根因回顾**：

- 04:24:51：平台在 `Blog10/frontend` 跑 `npm ci`，初始化任务留下的 Vite 开发服务还在这个目录里运行并往 `node_modules/.vite` 写缓存，`npm ci` 报 `ENOTEMPTY`。通知让 Leader“请负责的成员修复”，能用的页面被退回，36.2 分钟。
- 05:05:10：根目录的依赖目录已经被上一次失败的安装删掉一部分，但平台只在“这次合并改了清单，或目录不存在”时才安装，于是直接启动，`sh: vite: command not found`，再退回一次，17.8 分钟。

**改动**（主要在 [processes.py](../../backend/app/execution/processes.py)）：

| 规则 | 原来 | 现在 |
| --- | --- | --- |
| 什么时候安装 | 这次合并改了命令所在目录的清单，或依赖目录不存在 | 依赖目录不是“平台按当前清单装出来的那一个”就安装。平台装完后在目录里写标记文件 `.wdrunge-installed`，内容是清单指纹（`dependency_fingerprint`，[execution/environment.py](../../backend/app/execution/environment.py)）。失败的安装不写标记，下次重启自然再装 |
| 在哪里装 | 原地，旧目录还在，服务还在跑 | 先停掉从项目根运行、工作目录在该目录及其下面的保留服务；旧依赖目录改名挪到 `<目录>/.wdrunge-previous/`；从空目录安装，和合并前干净副本检查的条件一致 |
| 安装失败 | 留下半个目录，报给 Leader | 非零退出时自动再装一次（超时和命令起不来不重试）；仍失败就删掉半成品、把旧目录挪回原位，再把停掉的服务用原命令启动 |
| 安装成功 | 启动被重启的那个服务 | 同时把因安装停掉的其他服务也启动回来（例如同一目录下的 `web` 和 `web-admin`） |
| 根目录启动后不提供服务 | 恢复快照里的副本，报给 Leader | 先按清单重建一次依赖再启动一次（这一轮刚装过就不再装）；仍失败才恢复并通知 |
| 同一项目根的多个重启 | 各自在后台并发，可能同时安装 | 每个项目根一把锁，逐个进行。排队期间已经被前一个重启顺带启动过的服务不再重启第二次 |
| 快照里的服务副本 | 安装期间继续服务 | 不变。只有从项目根运行的服务才需要为安装让路 |

其余改动：

- `Job.successor`：平台在原位置或项目根重新启动一个服务时，记下新进程，失败通知和后续重启都以最新的那个为准。
- “重启中”状态改为记截止时间（`hold()`），安装期间按安装的时限算，不会在安装到一半时显示成“已停止”。
- `relaunch()`、`prepare_root()` 去掉了 `changed` 参数，是否安装只看指纹。
- 顺带修了一个旧问题：`installation_commands()` 里参数 `python` 被同名局部变量覆盖，“用任务当时的解释器建根目录的 `.venv`”一直没有生效。现在从空目录重建 `.venv` 的次数变多，这个参数必须生效，否则每次重建可能换一个 Python 版本。

**通知的分类**（[collaboration/environment.py](../../backend/app/collaboration/environment.py) `report_service_failure`）：

| 情况 | 判断依据 | 通知结尾 |
| --- | --- | --- |
| 安装失败，且合并前的干净副本检查用同一条命令、同一份清单通过过 | 服务登记里的 `root_checked` 等于当前的 `install_fingerprint` | “这是项目根目录的环境问题，不是交付内容的问题……不要因此退回任务，也不要让成员改代码或手动复制依赖目录；平台在之后每次交付或服务重启时会再安装。同样的失败反复出现时请告诉用户。” |
| 安装失败，没有这个证据 | 其余 | “先看上面的输出再决定：清单或锁文件本身的错误才需要负责的成员改清单后重新交付；网络、文件占用、权限这类是环境问题……不需要成员改代码。” |
| 依赖装好了但启动失败，或找不到启动命令 | `stage == 'start'` | “根目录的依赖已按清单安装，问题在启动：请按上面的信息让负责的成员修复……” |

每条通知还写明服务现在的状态：仍由快照里的副本提供、已在根目录用原来的依赖重新启动，或已停止。

**验证一：复现 04:24 的失败机制**（真实沙箱，真实 npm，独立的 `frontend` 目录带自己的 `package-lock.json`）

| 场景 | 结果 |
| --- | --- |
| 改动前的做法：根目录有一个保留服务不停往 `node_modules/.vite` 写文件，同时在该目录 `npm ci` | 退出码 190，`npm error code ENOTEMPTY`、`syscall rmdir`、`path …/frontend/node_modules/.vite`，和 Blog10 的通知逐项一致 |
| 改动后：同一个服务，清单已变，走平台的重启 | 两次实测 7.6 秒和 13.2 秒；服务被停、装完、重新启动；标记已写；没有残留的 `.wdrunge-previous`；没有通知 |

说明：用真实的 Vite 开发服务加一个不停请求页面的循环，`npm ci` 6 次都成功了（9.4–14.9 秒），没有复现出 ENOTEMPTY。这个竞争取决于 Vite 恰好在删除目录时写缓存。所以上表用一个持续写缓存目录的进程复现的是机制，不是 Blog10 当时那一瞬间。

**验证二：真实 Vite 开发服务从项目根运行**（同一环境，`npm run dev`，每一步都请求页面确认返回 200）

| 场景 | 结果 |
| --- | --- |
| 交付改了清单和锁文件 | 重启 10.5 秒和 16.0 秒（含 `npm ci`），新依赖已装，页面 200，没有通知 |
| 清单没变 | 重启 0.5 秒，没有安装 |
| 依赖目录被破坏（删掉 `vite` 包和 `.bin/vite`），标记仍是最新。这是 05:05 的场景 | 第一次启动失败后平台重建依赖再启动，12.3 秒和 15.6 秒，页面 200，没有通知 |
| 清单和锁文件不一致（`package.json` 多写一个依赖），安装必然失败 | 两次安装共 1.3–1.6 秒都失败；依赖目录和安装前完全一样（标记、已装的包都在）；服务已重新启动，页面 200；通知是“安装”类，带 npm 的输出、“已自动重试 1 次”和“服务已在项目根目录用原来的依赖重新启动” |

**验证三：workspaces 结构端到端**（Blog10 最后的结构，服务命令 `npm run dev --workspace frontend`，从克隆的快照启动）

| 步骤 | 结果 |
| --- | --- |
| 合并前的干净副本检查 | 通过，9.0 秒和 12.7 秒，副本里真的装出了 `vite` |
| 服务从快照迁到项目根 | 10.3 秒和 14.6 秒，页面 200，快照里的旧副本已停，根目录有标记 |
| 紧接着取下一个任务的快照 | 1.8 秒和 2.5 秒，里面的 `node_modules` 有 `vite` |

单元测试（`tests/test_root_dependencies.py`，用假的包管理器记录每次调用时的现场）：

- `test_a_root_service_stops_for_the_installation_and_starts_again`：安装时根目录没有服务在跑、目录是空的；同目录的另一个服务被带回来；标记正确；清单不变不再装，清单一变就装。
- `test_a_failed_installation_puts_the_folder_back_and_the_service_serves_again`
- `test_a_snapshot_copy_keeps_serving_while_the_root_cannot_install`
- `test_a_root_copy_that_does_not_serve_gets_its_dependencies_rebuilt_once`（05:05 的场景）
- `test_a_start_that_fails_on_fresh_dependencies_is_the_projects_problem`（刚装过就不再装第二次）
- `test_two_restarts_at_one_root_install_once`
- `test_the_leader_is_told_whose_problem_a_failed_restart_is`（三种通知）

**和计划的差别**：

1. 计划写“停旧进程 → 安装 → 启动”。实际只对从项目根运行的服务这样做。服务还在快照里时（刚交付、第一次迁到根目录），快照副本用的是自己的依赖目录，安装期间继续服务，装好并启动成功后才让位。
2. 计划写“记录每个目录上一次成功安装的指纹”，没说记在哪。实际记在依赖目录里的标记文件，不记数据库：目录被删、被替换、被 `npm ci` 清空时标记跟着消失，不会出现“记录说装过、目录其实不是那一个”。
3. 计划写“平台先自己重试一次”。实际是两层：安装命令非零退出时立刻再装一次；之后每次交付或服务重启时指纹仍然对不上，会再装。没有做定时重试。
4. 计划写“干净副本检查已通过、根目录仍失败，按构造就是环境问题”。实际按证据判断（登记里的 `root_checked` 和当前指纹相等才这样说），因为被别人的合并触发重启的旧服务，它的命令未必被那次检查覆盖。
5. 计划写“重放 Blog10 的两次 `service_attention` 场景”。实际是按那两次的条件搭了真实场景（验证一、二），不是重放数据库里的事件，也没有调用模型。

**没验证的**：真实的 `uv sync` 在项目根从空目录重建 `.venv`（Python 这条路只有单元测试和 `tests/test_root_commands.py` 里真实沙箱的 venv 测试）；pnpm、yarn、bun。

---

## 4. 这些改动带来的行为变化

| 变化 | 影响 | 实测 |
| --- | --- | --- |
| 项目根已有、但不是平台安装的依赖目录（没有标记），在第一次重启服务时重装一次 | 现有项目（包括 Blog10 的工作区）升级后第一次重启会多一次安装；手工装的依赖目录会被按清单重建 | npm 约 10–16 秒 |
| 合并前的干净副本检查现在真的会安装 | 原来快照里的依赖目录是链接、启动命令又是 `npm` 这类全局命令时，检查其实什么都没装就通过了。现在每次命令或清单变化多一次安装 | 9–13 秒 |
| 根目录的服务在安装期间停止 | 显示为“重启中”，依赖它的任务按原有机制等待 | 整个重启 8–16 秒 |
| 新任务取快照前等平台的迁移和安装结束 | 交付后紧接着开始的任务晚几秒到十几秒开始 | 取快照本身 1.8–2.5 秒 |
| Python：清单变化时 `.venv` 从空目录重建，不再原地增量同步 | 命令的包缓存在每条命令自己的临时目录里，所以会重新下载全部包 | 未实测 |

---

## 5. 需要你决定或确认的

2026-11-20：第 1、2 项你已确认，按“现在的做法”一列执行。

| # | 事项 | 现在的做法 | 备选 |
| --- | --- | --- | --- |
| 1 | `RLIMIT_CPU` 的值 | 14,400 秒 | 完全取消；或保留服务和普通命令用不同的值（需要在启动时就知道是不是保留服务） |
| 2 | 没有标记的根目录依赖目录 | 第一次重启服务时重装一次，之后只看指纹 | 没有标记时沿用旧规则（只在这次合并改了清单时装）。代价是“目录存在但内容不对”仍然发现不了，要等启动失败后的重建来补救 |
| 3 | Blog10 工作区里为适应旧沙箱加的三处改动（`maxParallelFileOps`、`--configLoader runner`、`cacheDir` 指到运行时数据目录） | 没动，那是测试工作区 | 下次同题运行从空项目开始，自然不会再出现 |

---

## 6. 改动的文件

| 文件 | 内容 |
| --- | --- |
| `backend/app/execution/limit_exec.py` | 5.2 |
| `backend/app/collaboration/workspaces.py` | 5.3 克隆 |
| `backend/app/agents/tools/validation.py` | 5.3 拒绝提示 |
| `backend/app/collaboration/service.py` | 取快照的时机 |
| `backend/app/execution/processes.py` | 5.1 安装、重启、锁、状态 |
| `backend/app/execution/environment.py` | 指纹和标记；`python` 参数的修复 |
| `backend/app/collaboration/environment.py` | 通知分类；清单名单改为引用执行层的定义（顺序不变，已有的 `root_checked` 指纹不受影响） |
| `backend/tests/test_root_dependencies.py` | 新增，19 个测试 |
| `backend/tests/test_native_sandbox.py` | 新增 1 个测试 |
| `backend/tests/test_resources.py` | 失败回调多一个参数；一处断言的文字跟着通知改了 |

验证脚本在本次会话的临时目录里（可能被清理）：`/private/tmp/claude-501/-Users-yefenghua--claude-spares-spare-syjfU4/f3bc1209-a083-4188-aea8-16e5bcaedeae/scratchpad/` 下的 `verify_limits.py`、`verify_snapshot.py`、`verify_race.py`、`verify_restart.py`、`verify_move.py`，公共部分在 `verify_common.py`。它们通过 `app.execution.processes` 在真实沙箱里执行命令，不调用模型。
