# 服务连接与项目数据库

用户在 Wdrunge 里配置一次数据库、缓存、消息队列等服务的地址和账号，所有项目共用；平台为每个项目准备好它自己的数据库和账号，通过环境变量交给项目进程。Agent 只知道服务名、变量名和状态，看不到任何密码，也不需要启动、登记或声明这些服务。实现在 `backend/app/resources/`，界面入口是左下角“Agent 团队”菜单中的“服务连接与数据库”。

## 配置（用户）

- 入口：左下角“Agent 团队” → “服务连接与数据库”。接口 `/api/resources/*` 与执行审批一样需要本机控制令牌，Agent 无法调用。
- 新建：类型可以选预置的 MySQL、MariaDB、PostgreSQL、Redis、MongoDB、RabbitMQ、MinIO、Elasticsearch、Neo4j，也可以选“自定义…”填写任意类型名。每个连接有名称（任务和变量里引用）、地址、端口、账号、密码、默认库、适用项目（所有项目或指定项目）、自定义字段（原样作为环境变量，可标记保密）和高级选项（变量前缀、连接协议）。保存前可以“测试连接”：MySQL/MariaDB/PostgreSQL 真正登录，Redis 执行 PING，其他类型检查端口。
- 从 Docker 导入：平台在沙箱外执行 `docker ps` / `docker inspect`，识别镜像类型、映射到本机的端口，以及镜像文档约定的账号变量（如 `MYSQL_ROOT_PASSWORD`、`POSTGRES_PASSWORD`、`--requirepass`）。列表只显示“已找到密码”，点“导入”时才读取并保存，随后自动测试连接。镜像变量只在容器第一次初始化时生效，之后改过的密码会让测试显示“账号或密码不正确”，编辑连接填写当前密码即可。
- 存储：应用数据库的 `resource_connections` 表（alembic `0009_resources`），密码和保密字段单独存放在 `secrets` 列，任何列表接口都只返回 `has_password` / `has_value`；编辑时密码留空表示不修改。应用数据库对所有项目进程不可见（`sandbox.protected_paths`）。“清除所有记录”保留连接配置，清除为项目准备的记录。
- 对话中粘贴的连接地址（如 `mysql://root:密码@127.0.0.1:3306`）在写入对话前被保存为只用于该项目的连接（同一地址和账号则更新密码），消息里的密码替换为 `***` 并附一句说明，所以模型、任务和文件都拿不到原文。

## 每个项目的数据库

- MySQL、MariaDB、PostgreSQL 连接默认勾选“为每个项目自动创建独立的数据库和账号”。项目第一次运行命令（或打开设置页）时，平台用配置的管理员账号创建 `wd_<项目名>_<项目ID前6位>` 数据库和同名账号，随机密码，只授予该库的全部权限（MySQL 账号主机为 `%`，以便 Docker 容器内的服务器接受宿主机连接）。管理员账号从不交给项目。准备结果记录在 `project_resources` 表；服务器连不上时状态为“未就绪”，命令照常运行（退回内置 SQLite），60 秒内不重复尝试，设置页可立即重试。
- 没有可用的关系型数据库连接时，项目使用内置 SQLite：`<项目>/.wdrunge-data/app.db`。
- `.wdrunge-data/`（自带 `.gitignore`）是项目的运行时数据目录：所有任务快照共用、可写（沙箱额外授予写权限），不复制进快照、不参与合并。项目中任何位置的 `*.db`、`*.sqlite*` 及其 `-wal`/`-shm`/`-journal` 文件同样不复制、不合并，因此不会再出现 `Binary conflict: blog.db`；旧快照清单中记录的这类文件也不会在交付时被删除。

## 交给项目进程的内容

`processes.start` 在审核前按项目根找到项目，向进程注入（`sandbox.clean_environment`）：

| 变量 | 内容 |
|---|---|
| `<前缀>_HOST/_PORT/_USER/_PASSWORD/_DATABASE/_URL` | 每个可用连接；前缀默认是名称的大写（`mysql` → `MYSQL`），项目自己的数据库账号替代管理员账号 |
| 自定义字段 | 按填写的变量名 |
| `DATABASE_URL`、`SQLALCHEMY_DATABASE_URL`、`DB_ENGINE`、`DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD` | 项目主数据库（第一个可用的关系型连接）；`SQLALCHEMY_DATABASE_URL` 带驱动（`mysql+pymysql`、`postgresql+psycopg`） |
| `DATABASE_URL`、`SQLALCHEMY_DATABASE_URL`、`DB_ENGINE=sqlite`、`DB_NAME`、`SQLITE_PATH` | 没有关系型连接时指向内置 SQLite |
| `WDRUNGE_DATA_DIR` | 运行时数据目录的绝对路径 |

- 网络：macOS 沙箱即使 `network=false`，也允许连接这些连接的本机端口（仅这些端口），迁移、测试等普通命令不必申请联网。Linux（bubblewrap）仍需 `network=true`。
- 脱敏：进程输出经 `processes.logs()` 读取时，所有注入的密码和保密字段被替换为等长的 `*`，执行结果、工具调用记录、审核输入都只见到掩码。
- 审核：操作带 `resources` 字段（已连接服务名、端口、数据目录；不含密码，不计入“以后都允许”的匹配键）。审核识别 `mysql://`、`postgresql://`、`redis://`、`mongodb://`、`amqp://` 等地址及其默认端口；指向已连接服务端口的连接标为 `served_by: connected service …`，不再按“陌生本机端口”转人工。

## 对 Agent 的约束

- handoff 的 `environment.connected` 列出连接名、类型、状态、变量名、项目库名和推荐驱动，以及规则：从环境变量读取配置，不把值写进文件，不启动/登记/声明这些服务，运行时数据放在 `WDRUNGE_DATA_DIR`，表结构变更写成迁移并登记为 `migrate` 命令。
- `collaboration_register_service` 拒绝已连接服务的名称或端口；`create_task(services=[...])` 拒绝声明它们；`needs=[{service: 'mysql'}]` 直接视为满足；运行观察不会把它们记成项目服务。
- 交付检查中唯一会阻止提交的一项：合并的文件里出现任何已连接服务的真实密码（包括管理员密码和项目账号密码）时，`publish_artifacts` 拒绝合并并说明哪个文件、哪个连接。

## 交付后的迁移与重启

1. 合并后，如果项目登记了名为 `migrate`（或用途写明迁移）的命令，且合并的文件在该命令目录下，平台先备份项目数据库，再在项目根目录执行该命令（照常审核）。成功后命令标为 verified，保留服务才重启到新代码；失败时 Leader 收到 `migration_failed` 事件（含输出摘要和备份名），服务继续运行原来的代码。
2. 保留服务从快照改到项目根运行（或因合并而重启）之前，`prepare_root` 先检查：依赖目录不随交付合并，若服务在快照里有自己的 `.venv`/`node_modules`、根目录找不到启动命令，或依赖清单有变化，就在根目录按清单安装（`installation_commands`，照常审核）；装不上或仍找不到启动命令时，旧副本保持运行，Leader 收到 `service_attention` 事件。
3. 新副本启动后没有在原端口提供服务时，平台停止它，从任务快照恢复交付前的副本（`relaunch='failed'`），并把最后的日志发给 Leader。

## 备份

- 迁移前自动备份；设置页“当前项目”也可以“立即备份数据库”，并从最近 5 份备份中恢复（恢复会用备份替换项目数据库的全部表）。
- SQLite 用在线备份 API；MySQL/MariaDB 用项目账号做逻辑导出（超过 200 MB 不自动备份）；PostgreSQL 暂不支持备份，迁移照常执行并在失败通知里说明没有备份。备份存放在 `.wdrunge-data/backups/`。

## 回归覆盖

`tests/test_resources.py` 覆盖：密码不出现在任何列表与 handoff、编辑保留密码、名称/前缀/变量名校验、内置 SQLite、项目独立库与账号（管理员账号不下发）、服务器不可用时不阻塞命令且限频重试、按项目限定连接、控制令牌保护的接口、Docker 描述与导入、进程注入与输出脱敏、沙箱参数、已连接服务不能登记/声明且 needs 直接满足、审核识别数据库地址、运行时数据不进快照和交付、旧快照不会删除数据库文件、密码泄漏阻止合并、重启前检查失败保留旧副本、重启后不服务时恢复快照副本、根目录依赖安装、SQLite 备份与恢复、迁移成功/失败、迁移失败时服务不搬迁、对话粘贴的连接地址保存并隐去。设置 `WDRUNGE_TEST_MYSQL=root:密码@127.0.0.1:3306` 或 `WDRUNGE_TEST_POSTGRES=...` 时另外对真实服务器验证建库、授权和隔离。`tests/test_resource_ui.py` 用模拟接口验证左下角菜单、新建、测试、Docker 导入流程。
