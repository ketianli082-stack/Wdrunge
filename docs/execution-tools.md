# 开发工具与执行权限

开发、前端和测试角色现在可使用通用命令、环境识别、依赖安装及任务进程工具。前端和测试角色另有浏览器验证。所有专业角色可分页检索文件、申请读取外部配置，以及在各自写入范围内移动和删除文件。Leader 继续负责分派和验收。

## 工具

| 工具 | 行为 |
| --- | --- |
| `execute_command` | 分开的 `command`、`arguments`、相对项目的 `cwd`；1–600 秒超时；返回退出码、合并 stdout/stderr、超时与截断信息。没有隐式 shell。`background=true` 立即返回进程 ID，最长 1800 秒，通过 manage_process 查状态、日志或停止；启动服务和安装依赖都使用此入口。 |
| `manage_process` | 当前任务工作区的进程列表、状态、分页日志和停止；不接受任意 PID。 |
| `detect_environment` | 识别 Python/Node 清单、包管理器、锁文件、脚本和环境；也识别 Rust/Go/Ruby/PHP 清单并提示显式配置安装命令。 |
| `browse_directory` | 单层文件和目录分页；`offset`、`limit`，返回 `next_offset`。 |
| `search_code` | 字面量代码搜索，返回路径、行号和分页；跳过依赖、构建输出、符号链接、二进制和秘密配置，扫描预算耗尽会明确报告不完整。 |
| `read_external_file` | 支持绝对路径和 `~/`；审核精确到文件的只读范围，不开放父目录或写权限；最多 2 MiB UTF-8。 |
| `manage_file` | 不覆盖目标的移动、显式递归删除；小模型审核，存在疑问转人工；不能操作根目录或绕过共享文档发布。 |
| `browser_verify` | 一次临时会话内导航、点击、输入、按键、等待可见和文本断言；返回页面文字、控制台/页面/HTTP 错误及 `docs/testing/browser-*.png` 截图。 |

原有 `run_project_check`、`refresh_api_contract`、`refresh_frontend_client` 和 `run_service_check` 同样通过统一执行层。Python 验证优先使用任务自己的环境，必要时只读使用来源项目的 `.venv`，不会悄悄使用 Wdrunge 宿主的依赖冒充项目环境。

任务快照中共享的依赖只能只读使用。需要修改依赖时，通过受审核的命令解除任务内的依赖符号链接，再创建任务自己的依赖目录；不得删除或覆盖链接目标。运行产生的缓存、HOME 和 Wdrunge 测试数据库位于私有临时目录。

## 小模型与人工授权

默认审核模型为 `qwen-flash`，使用现有 DashScope 配置，可通过 `WDRUNGE_REVIEW_MODEL` 替换。审核包括命令、参数、目录、网络、读写范围及已保存的人类需求。Agent 给出的 reason 仅是解释，不是用户授权。

系统还为审核模型计算以下背景，只用于判断相关性，不构成授权：发起角色与当前任务（目标、验收条件）；本项目经执行工具启动且仍在运行的服务及端口（端口取自命令参数和日志中的本机 URL，日志仅提取数字）；操作事实，包括参数中的网络目标（是否本机、由哪个项目服务监听）、可执行文件位置和项目外绝对路径；同一项目内同一可执行文件最近 6 次的决定（人工允许/拒绝、AI 或白名单放行）。因此 `curl http://127.0.0.1:8010/...` 访问本任务刚启动的服务可以自动放行，而探测其他进程仍转人工。访问不属于项目服务的本机端口（可能是用户自己的程序）时，即使小模型给出 allow 也转人工；只有人工曾允许完全相同的命令，之后才可由小模型放行。

审批顺序为具体操作授权/白名单、小模型、人工。模型必须返回 `allow`、`confidence >= 0.75`、`uncertain=false` 才自动放行；缺少这些字段、低置信度、有疑问、无凭据、超时、无效响应或拒绝都转人工。正常的项目内安装依赖、启动服务、联网验证和只读使用来源项目依赖不会仅因联网或额外读取而强制弹窗。审核会读取当前目录的环境及实际 package scripts，不仅根据命令名猜测。外部配置、删除/移动也接受审核，凭据、系统操作、部署发布或目标不清晰时请求人工确认。小模型不是安全边界：执行仍必须通过系统沙箱。

侧栏“执行权限”→“自动审批与白名单”可以关闭小模型自动审批或调整预设白名单。默认免审批的是常用工具的 `--version` 和 `git status` 的限定参数组合，仅限项目内、不联网、没有额外读取范围；不会匹配附加参数、任意路径下同名程序、shell 脚本或其他 Git 操作。设置持久化，修改后只影响后续操作。人工“以后都允许”保存的具体操作也属于可撤销的项目白名单。

AI/白名单自动放行保留最近 200 条审核记录（理由、置信度、操作范围及来源），不生成永久授权，也不会复用一次 AI 判断放行变更后的命令。人类需求来自已保存的用户消息，项目脚本及 Agent reason 都视为不可信的辅助数据。审批设置 API `POST /api/execution/settings` 同样要求独立的本机授权码。

弹窗提供“拒绝”“单次允许”“以后都允许”。持久授权只匹配同一来源项目、相同命令参数、目录、超时和权限；任务快照路径规范化为工作区占位符，因此同项目后续任务可以复用相同授权。不同项目或参数不会复用，也没有任意命令通配放行。授权可从侧栏“执行权限”撤销。待确认请求 10 分钟过期，取消和重启会使旧请求失效；不会重启后自动执行旧命令。

权限记录保存在应用 SQLite 数据库。授权 API 使用独立的随机本机凭据，不能通过公开 HTTP 获取。桌面版通过 Tauri 从私有应用数据目录读取，网页模式需手工复制该目录的 `execution-token`。不要将它放进项目文件、VITE 环境变量或 Agent 对话。应用数据及权限数据库禁止被执行沙箱或外部文件工具读取。

默认凭据位置：

- macOS：`~/Library/Application Support/Wdrunge/execution-token`
- Linux：`${XDG_DATA_HOME:-~/.local/share}/Wdrunge/execution-token`
- Windows：`%LOCALAPPDATA%/Wdrunge/Wdrunge/execution-token`

这是单用户、单后端进程的本地开发控制面。不要部署多个 uvicorn worker 共享它；多用户远程使用需要独立身份认证和租户隔离。授权只控制后续执行，撤销不会终止已经启动的服务；用户可以从同一面板停止服务。

## 系统沙箱支持

| 平台 | 实现与条件 |
| --- | --- |
| macOS | 使用系统 `/usr/bin/sandbox-exec` 的 Seatbelt 策略。默认拒绝，按需只读运行时和项目、只写任务工作区及临时目录；阻止项目秘密文件和应用控制数据。`sandbox-exec` 已被 Apple 弃用，宿主不支持时明确失败。 |
| Linux | 使用 Bubblewrap 调用内核用户、PID、挂载等命名空间；默认隔离网络，按路径只读/读写挂载。`bwrap` 不是发行版必装组件，用户命名空间也可能被禁用；缺失或启动失败都不会降级。 |
| Windows | 返回明确的不可用状态并阻止命令。Windows Sandbox 是有版本/虚拟化要求的可选虚拟机功能，不能冒充任意子进程的默认沙箱。尚未实现并验证 AppContainer + Job Object 适配器。 |

`network=true` 意味着共享宿主网络，包括本地服务和公网，需要审核；不宣称它仅允许 localhost。浏览器工具还限制 HTTP/WebSocket 请求到明确列出的 origins，阻止服务工作线程，使用全新配置且不载入用户 cookies。

进程输出保留最近 128 KiB，单次返回最多 30,000 字节；`execute_command` 前台输出超过 4,000 字节时只返回开头、整份保留日志中的错误/警告行和结尾，并标记 `output_truncated`，完整日志通过 `manage_process` 的 logs 分页读取；退出记录最多保留 100 条，不跨重启恢复 PID。最多并行 8 个进程，命令/服务受墙钟时间、CPU、文件大小和文件描述符限制，Linux 的普通命令另设 64 GiB 单进程虚拟地址空间上限；浏览器为兼容 V8 大地址预留不使用该上限。取消命令、任务结束、清除记录和正常退出会清理任务进程组。进程组清理不等于完整的容器资源配额；特别是 macOS 上刻意脱离进程组的程序、系统强杀后端，以及磁盘总量/整个进程树的内存配额，仍需要更强的 OS 级监督与配额。不要把这套开发防护当作任意恶意代码的云多租户运行环境。

## 浏览器安装与验证

在 backend 环境执行：

```sh
uv sync --extra browser
uv run playwright install chromium
```

Linux 还需要浏览器系统运行库，按 Playwright 官方安装说明准备；工具不会自动执行系统包管理命令。可用 `PLAYWRIGHT_BROWSERS_PATH` 指定浏览器缓存，验证使用对应安装，不借用日常浏览器。

示例参数：

```json
{
  "url": "http://127.0.0.1:5173",
  "allowed_origins": ["http://127.0.0.1:8001"],
  "actions": [
    {"action": "fill", "selector": "input[name=email]", "value": "test@example.com"},
    {"action": "click", "selector": "button[type=submit]"},
    {"action": "assert_text", "selector": "#result", "value": "成功"}
  ]
}
```

构建通过、进程启动、HTTP 响应和真实浏览器验证分别返回自身证据，不能互相代替。浏览器缺失、沙箱失败、交互失败和页面错误均返回明确失败结果。噪声页面的输出会截断，并保留条数与通过/失败标志。

测试：`uv run pytest -q`。`tests/test_native_sandbox.py` 在实际宿主验证系统边界与浏览器，要求宿主允许嵌套沙箱和本地端口；Chromium 未安装时可选浏览器用例跳过。单元测试里的模拟授权/执行传输仅存在于 tests，没有生产环境的关闭沙箱开关。

## 调用示例

检查版本：`execute_command(command="python3", arguments=["--version"], cwd=".")`。
安装依赖：`execute_command(command="npm", arguments=["ci"], cwd="frontend", network=true)`。
后台服务：`execute_command(command="npm", arguments=["run", "dev"], cwd="frontend", background=true, name="frontend", timeout=1800, network=true)`。
Python 先创建项目 `.venv`，之后使用该目录下的 Python 安装依赖。命令的参数数组名为
`arguments`，避免框架的 `args` 保留名；不再向模型注册独立的 start_service 或 install_dependencies。
