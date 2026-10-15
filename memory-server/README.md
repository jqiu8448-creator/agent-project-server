# memory-server · 共享记忆 MCP 服务

项目服务器的**唯一事实源**：项目现状、任务台账、身份名册、审计流水都存这里。
多个 AI 客户端通过 [MCP](https://modelcontextprotocol.io)（streamable-http）协议访问，
默认监听 `127.0.0.1:8000/mcp`，数据落在 `~/.shared_memory/`。

依赖：官方 MCP Python SDK（`pip install mcp`，开发测试于 1.30.0）；其余全是标准库。

## 启动

```bash
python3 memory_server.py        # 默认 127.0.0.1:8000
# 覆盖监听与数据目录：
MEMORY_HOST=127.0.0.1 MEMORY_PORT=8000 MEMORY_DB=~/.shared_memory python3 memory_server.py
```

## 实时看板（monitor.py）

```bash
MEMORY_DB=~/.shared_memory MEMORY_MONITOR_PORT=8001 python3 monitor.py
# 打开 http://127.0.0.1:8001 —— 进行中任务 / 最近完成 / 最近操作流水，每 5 秒自动刷新
```

看板是只读投影，不提供任何写路径。

## 数据目录里有什么

| 文件 | 内容 |
|---|---|
| `<项目名>.json` | 每个项目的记忆正文（项目现状） |
| `_tasks.json` | 任务台账（start_task / finish_task 的落点） |
| `_roster.json` | 身份名册（规范角色 key + 中文显示名，可热改） |
| `_policy.json` | 策略开关（`strict_roster` / `overwrite_shrink_ratio` 等） |
| `_audit.jsonl` | 审计流水：谁在何时对哪个项目做了什么 |

## 工具集

| 类别 | 工具 |
|---|---|
| 记忆 | `read_memory` / `write_memory` / `append_memory` / `list_projects` |
| 身份 | `list_roles` / `claim_role` / `release_role` / `register_name` / `whoami` |
| 台账 | `start_task` / `update_task` / `finish_task` |

两条关键纪律由服务端强制，不是文档倡议：

1. **读门禁**：`write_memory` / `append_memory` / `start_task` 要求该客户端 30 分钟内
   `read_memory` 过目标项目——项目现状只能来自记忆服务，不允许凭自身记忆执行。
2. **防缩水覆盖**：新内容明显短于旧内容时拒绝 `write_memory`，防误覆盖。

## 身份模型

- 智能体**干活时现认领角色**：`claim_role("role-<语义名>")`，同一个客户端可以今天干
  框架、明天干评估。读取不需要身份（你得先读到名册才能决定认领谁），写需要。
- 开箱默认 `strict_roster=false`：名册为空时首次 `claim_role` 会自动登记语义角色并
  热写 `_roster.json`；角色稳定后在 `_policy.json` 里收紧。
- `register_name` 只登记中文显示名，只认名册里登记过的 key。
- 也可以走 HTTP 头（`x-agent-key`）绑定固定身份，适合脚本化客户端。

## 测试

```bash
cd tests && python3 -m unittest discover -v
```

测试全部在临时数据目录里跑，不碰真实记忆库，也不连生产服务。

## 排障

`check_identity.py` 打印当前客户端在该服务上的身份视图（whoami 的命令行版）：

```bash
python3 check_identity.py            # 默认连 http://127.0.0.1:8000/mcp
MEMORY_URL=http://127.0.0.1:9000/mcp python3 check_identity.py
```
