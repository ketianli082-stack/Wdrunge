# 项目环境登记

每个项目由平台记录“这个项目怎么运行”：服务（任意数量与类型）、端口、地址、启动命令、可复用命令，以及 Wdrunge 平台自身占用的保留端口。成员按服务名称引用，不再凭习惯写 8000、3000、5173 等端口。实现在 `backend/app/collaboration/environment.py`，数据存放在 `collaboration_records`（`kind='service'` / `kind='command'`），无需新表。

## 内容

| 项 | 字段 | 来源 |
|---|---|---|
| 主机 | 操作系统、BSD/GNU 工具约定 | 运行时检测 |
| 平台保留端口 | Wdrunge API 实际服务端口、界面开发服务器端口、`WDRUNGE_RESERVED_PORTS` | `PlatformPortsMiddleware` 从本机请求中学习，不写死端口 |
| 服务 | name、kind（http/tcp/other）、port、host、base_url、health_path、启动命令与目录、owner、status（declared/verified）、来源、claimed | 成员登记、用户登记、项目配置发现、运行观察 |
| 命令 | name、argv、cwd、用途、status | `collaboration_register_command`；本任务已成功执行过同一命令时标为 verified |

## 端口归属（claimed）

端口只有在以下情况才算本项目所有：登记时端口空闲（包括平台分配）、观察到本项目进程正在提供该端口、或由用户登记。登记一个已被其他程序占用的端口时可以记录，但不算本项目所有，并返回警告。平台保留端口和其他服务已登记的端口不能登记；不填端口时平台分配一个空闲端口（http 从 8100、tcp 从 9100 起）。

## 写入途径

- 成员：`collaboration_register_service`（不填端口即分配，`remove=true` 删除）、`collaboration_register_command`；`collaboration_environment` 读取完整登记（默认隐藏，交接中已有摘要）。
- 运行观察：`execute_command(background=true)` 时，`ready_port` 确认的端口、启动前空闲且现在可连接的 argv 端口、进程日志中自报且可连接的地址（启动前已被其他进程或已登记服务占用的端口除外：那是它连接的上游，如代理打印的转发目标），都会把对应服务标为 verified，并记录启动命令与相对目录；尚未登记的端口记为 `observed` 服务。
- 项目配置：读取时扫描 package.json 脚本的 `--port`、vite 配置的 `port`、docker-compose 端口映射、`.env.example`/Procfile 中的 `*PORT=`，以 declared 状态加入，不覆盖已有登记。
- 用户：协作面板“项目环境”可查看、登记、修改和移除；用户登记的服务只有用户能移除。

## 运行状态与保留服务

登记中的 `verified` 只表示“曾观察到本项目进程提供该端口”，不代表现在仍在运行。每次读取登记时，平台按运行中的进程为每个服务计算实时 `runtime`：`running`（进程 ID、启动成员、是否保留、是否 stale）、`listening`（端口有响应但不是团队进程）或 `stopped`。`detect_environment`、`collaboration_environment`、handoff 的 `environment.services[].running` 和协作面板都显示它。

- 默认：任务结束时停止它启动的所有进程。
- 保留：服务是否在任务后继续运行，以任务声明为准。任务交付（review/completed）时，自己 `services` 中声明的服务自动保留；用 `keep_running=true` 启动的进程只有在它提供的服务由某个任务声明时才保留（例如测试恢复了后端交付的服务），其余（预览服务、mock 等自测辅助进程）随任务停止，`keep_running` 的返回结果会提前说明。未交付（失败、取消、冲突）的任务一律停止。
- 交付合并后，保留的服务由平台用同一命令改从项目根目录运行（命令照常审核，审核通过前旧副本继续服务）。从旧副本停止到新副本就绪，服务状态显示 restarting（“平台正在重启，无需处理”），此时启动同一服务返回 `service_restarting`；依赖该任务的任务、以及 `needs` 该服务的任务，等搬迁完成后才派发（最多等 120 秒）。寿命为 `WDRUNGE_SERVICE_LIFETIME_SECONDS`（默认 4 小时）；在停止会话、应用关闭、被同名服务替换或用户/成员手动停止时结束。项目根的保留服务属于项目，不属于在项目根运行的某个回合：Leader 回合或任务结束时的进程清理不会停止它。
- 复用与替换：其他任务看到 running 时直接使用；不带 keep_running 再次启动同一服务会返回 `service_already_running`；带 keep_running 启动会替换正在运行的副本（改了代码后重启）；但不能从任务快照替换项目根运行的保留服务（快照里是未合并的代码），会返回 `service_already_running`，要测试未交付的改动请登记新服务、在新端口上启动自己的副本。按服务查找进程时先按名称、再按端口。
- stale：之后有其他任务发布了该服务目录下的文件时，记录变更路径。只在重启确实能加载新文件时提示重启：项目根的保留服务由平台自动重启（显示“平台正在重启，无需处理”）；仍在任务快照中运行的服务看不到合并的文件，重启也一样，显示为“任务交付后改从项目根运行”而不提示重启；只有改从项目根运行失败、或未保留的服务，才标 STALE 并提示用 keep_running=true 重启。
- 管理：`manage_process` 可按服务名查看/读取日志/停止项目中运行的服务（含其他任务保留的）；协作面板“项目环境”提供“停止”，对应 `POST /api/projects/{project_id}/collaboration/services/{name}/stop`。
- 交付检查：任务声明的服务在提交时必须正在运行，仅“曾验证”不够。

数据库、缓存等由用户在设置中连接的服务不进入这份登记：它们由平台提供，见 [服务连接与项目数据库](connected-services.md)。handoff 的 `environment.connected` 列出它们；成员不能把它们登记为项目服务或在任务中声明。

## 使用

- 每个成员和 Leader 的 handoff 都带 `environment` 摘要（主机、保留端口、服务及启动命令、项目命令）。保存的阶段交接记录不包含这部分实时数据。
- 命令审核：本机地址的端口若是本项目已归属的登记服务，即使当前没有进程运行，也视为本项目服务，审核模型可直接放行；访问平台保留端口一律需要人工确认，且不因相同命令的历史人工放行而免审。审核模型的输入包含 `project_environment`。
- 提示词：Leader 与协作说明要求按服务名引用、通过登记取得端口、只在用户需要的交付物（README、配置）中写入解析后的地址。

## 回归覆盖

`tests/test_environment_registry.py` 覆盖端口分配与冲突、占用端口不归属、用户条目、项目配置发现、handoff 摘要、成员/用户工具、运行观察登记、审核信任登记端口与平台端口始终人工确认、平台端口学习。`tests/test_kept_services.py` 覆盖跨任务保留、复用与替换、stale 标记及只在有用时提示重启、项目根保留服务不随回合清理停止、代理打印的上游端口不归属代理、搬迁期间显示 restarting 并拒绝重复启动、快照副本不能替换项目根服务、随会话停止、寿命延长、未交付任务停止保留服务、交付时只保留有任务声明的服务和面板停止接口。
