# 交付检查

任务提交时平台检查交付文件和已发布文档，把发现（findings）附在任务上供 Leader 或用户验收时判断；只提示，不阻止交付。实现在 `backend/app/collaboration/delivery_checks.py`。

## 检查项

| 类型 | 级别 | 内容 |
|---|---|---|
| platform_leak | high | Wdrunge 工作区/应用数据路径、平台工具名（execute_command、collaboration_* 等）、指向平台保留端口的本机地址 |
| platform_leak | medium | 团队成员名字 |
| platform_leak | low | 提到 Wdrunge/协作平台或沙箱（沙箱、seatbelt、network=true 等） |
| port_mismatch | medium | 本机地址的端口不是项目环境中登记的服务端口 |
| evidence_in_contract | low | 接口类文档中出现“验证证据/实测”等章节，或 3 行以上原始 HTTP 记录 |
| oversized | medium | 交付文本行数超过目标规模的参考值（small 400 行、medium 2500 行） |
| generated_without_command | low | OpenAPI、*.gen.*、generated/ 等生成文件没有登记重新生成命令 |

依赖目录（node_modules、vendor、.venv、dist 等）、隐藏目录、二进制和超过 512 KB 的文件不检查。每类最多保留 8 条，同一行同一类只报一次，按严重程度排序。项目工作区就是 Wdrunge 仓库本身时不检查（它本来就会出现这些名字）。

## 流程

- `submit_task` / 交付到 review：在任务快照上计算发现，保存到任务的 `findings`，在提交结果中返回，并把摘要放进通知 Leader 的 `request_status` 事件。
- 成员未自行提交、由运行时代为交付时同样计算。
- 成员可在提交前调用 `collaboration_check_delivery` 自查（需先用 enable_tools 启用）。
- Leader 提示词要求验收前逐条判断，high 级平台泄漏通常退回返工。
- 协作面板的任务详情显示“交付检查”。

## 文档类型

协作说明为契约、README、测试报告规定了各自职责与篇幅（随目标规模），与 `evidence_in_contract`、`oversized` 检查配合。

## 回归覆盖

`tests/test_delivery_checks.py` 覆盖各类发现、依赖目录跳过、登记命令后生成文件不再提示、Wdrunge 自身项目不检查、提交时保存/返回/通知 Leader 且不阻止交付、提交前自查。用真实 EmptyFolder 交付物验证时，能发现报告中人工指出的问题：平台端口与平台描述、工具名、成员名、契约中的证据章节、超出 small 规模、未登记生成命令的 openapi.json。
