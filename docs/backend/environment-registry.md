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
- 运行观察：`execute_command(background=true)` 时，`ready_port` 确认的端口、启动前空闲且现在可连接的 argv 端口、进程日志中自报且可连接的地址，都会把对应服务标为 verified，并记录启动命令与相对目录；尚未登记的端口记为 `observed` 服务。
- 项目配置：读取时扫描 package.json 脚本的 `--port`、vite 配置的 `port`、docker-compose 端口映射、`.env.example`/Procfile 中的 `*PORT=`，以 declared 状态加入，不覆盖已有登记。
- 用户：协作面板“项目环境”可查看、登记、修改和移除；用户登记的服务只有用户能移除。

## 使用

- 每个成员和 Leader 的 handoff 都带 `environment` 摘要（主机、保留端口、服务及启动命令、项目命令）。保存的阶段交接记录不包含这部分实时数据。
- 命令审核：本机地址的端口若是本项目已归属的登记服务，即使当前没有进程运行，也视为本项目服务，审核模型可直接放行；访问平台保留端口一律需要人工确认，且不因相同命令的历史人工放行而免审。审核模型的输入包含 `project_environment`。
- 提示词：Leader 与协作说明要求按服务名引用、通过登记取得端口、只在用户需要的交付物（README、配置）中写入解析后的地址。

## 回归覆盖

`tests/test_environment_registry.py` 覆盖端口分配与冲突、占用端口不归属、用户条目、项目配置发现、handoff 摘要、成员/用户工具、运行观察登记、审核信任登记端口与平台端口始终人工确认、平台端口学习。
