# Agent Project Server · 多智能体协作的项目服务器

一台机器、多个 AI 客户端同时干活时，最大的问题不是代码，是**互相不知道对方在干什么**：
同一个文件被两个智能体同时改、项目现状只存在于某个对话窗口里、任务干到一半换客户端就断片、
服务开着一周没人关。

这套框架用四个组件把这件事管起来。除 `memory-server` 需要官方
[MCP Python SDK](https://pypi.org/project/mcp/)（`pip install mcp`）外，其余全部只用
Python 标准库（3.10+）：

| 组件 | 作用 |
|---|---|
| [memory-server](memory-server/) | 共享记忆 MCP 服务：项目现状、任务台账、身份名册、审计流水，**唯一事实源** |
| [engine-guardian](engine-guardian/) | 台账里没有 running 任务且持续空闲时，自动关掉登记在册的本地服务 |
| [scripts/new_project.py](scripts/new_project.py) | 新项目一键接入：建仓、写规矩模板、首推、记忆登记 |
| Git 服务（示例 [Gogs](https://gogs.io)） | 代码托管；`AGENTS.md` 随代码走，clone 下来规矩就生效 |

建议所有服务只绑 `127.0.0.1`，不对局域网暴露。

## 核心思路：先读记忆，再干活

规矩是**硬门禁，不是倡议**：智能体写任何东西之前，必须在 30 分钟内读过对应项目的记忆——
项目现状只能来自 `read_memory`，不允许凭自身记忆执行。身份也是**干活时现认领**
（`claim_role("role-<语义名>")`），不是登录时定死，同一个客户端今天干框架、明天干评估，
都是它自己 claim 出来的。每个任务都必须 `start_task → finish_task` 留台账，
履职记录靠 **台账 + 审计 + git** 三源对账。

完整规范见 [AGENTS.md](AGENTS.md)——它既是框架的通用规矩，也是每个被接入项目的模板。

## 多个智能体，接力一个项目

**框架不绑模型，也不绑客户端。** 模型是智能体的引擎，本框架通过 MCP 对接智能体客户端；
上下文和进度存在框架侧的共享记忆里，不依赖任何一家的模型上下文——
所以换模型、换客户端、多个智能体接力，都不丢。

一次典型接力：

```
智能体 A（Trae）     认领角色 → 读记忆 → 写引擎代码 → 留进展收工
智能体 B（MiniMax）  认领角色 → 读记忆 → 看到 A 的进展 → 接着写测试 → 留进展收工
主人                打开看板看谁在干活 → 在记忆里追一条决策 → 下一位开工前先读
```

**接入方式是 MCP**（Model Context Protocol，模型上下文协议，业界开放标准）：
不改任何代码，支持 MCP 的智能体客户端配上这一个端点就能接入——

```
http://127.0.0.1:8000/mcp        （streamable-http，本机）
```

国内主流智能体 / 客户端、大模型（2026-10 盘点，均支持 MCP，配上地址即接入）：

| 名字 | 厂商 | 类型 |
|---|---|---|
| Trae | 字节跳动 | AI IDE |
| MiniMax Code | MiniMax | 编程智能体 |
| Qoder CN（原通义灵码） | 阿里云 | IDE 插件 / 独立 IDE / CLI |
| CodeBuddy | 腾讯云 | IDE / 插件 / CLI |
| 文心快码 Comate | 百度 | IDE 插件 / Agent 平台 |
| CodeGeeX | 智谱 AI | IDE 插件 |
| iFlyCode | 科大讯飞 | IDE 插件 |
| Kimi Code | 月之暗面 | IDE 插件 |
| 豆包 / Seed | 字节跳动 | 大模型 |
| 通义千问 Qwen | 阿里巴巴 | 大模型 |
| DeepSeek | 深度求索 | 大模型 |
| GLM | 智谱 AI | 大模型 |
| Kimi | 月之暗面 | 大模型 |
| MiniMax | MiniMax | 大模型 |
| 文心 | 百度 | 大模型 |
| 混元 | 腾讯 | 大模型 |
| 讯飞星火 | 科大讯飞 | 大模型 |
| MiMo | 小米 | 大模型 |

> 模型版本迭代快，表里只写家族名不写版本号。名单之外的（Cursor、Claude Code 等国外产品，
> 或将来的新产品）也一样——判断标准只有一个：**支持 MCP**。
> 配上上面的端点，它就是这套框架的一名成员。

## 快速开始

```bash
# 0. 安装依赖（只有 memory-server 需要，其余组件纯标准库）
pip install -r requirements.txt

# 1. 共享记忆 MCP 服务（默认 127.0.0.1:8000，数据默认在 ~/.shared_memory）
python3 memory-server/memory_server.py

# 2. 实时看板（谁在做什么任务 / 最近操作流水，默认 127.0.0.1:8001）
MEMORY_DB=~/.shared_memory python3 memory-server/monitor.py

# 3. 空闲守护器（可选）
cp engine-guardian/config.example.json engine-guardian/config.json
python3 engine-guardian/guardian.py

# 4. 新项目一键接入（凭据走环境变量，令牌绝不进仓库）
export GOGS_BASE="http://localhost:3000" GOGS_USER="你" GOGS_TOKEN="令牌"
python3 scripts/new_project.py 项目显示名 --slug my-project --dir /path/to/project
```

环境变量总表：`MEMORY_HOST` / `MEMORY_PORT` / `MEMORY_DB`（记忆服务）、
`MEMORY_MONITOR_PORT`（看板）、`GOGS_BASE` / `GOGS_USER` / `GOGS_TOKEN` /
`PROJECTS_HOME`（接入脚本）。

## 给 AI 客户端用的入口

- [skills/gogs-onboarding/SKILL.md](skills/gogs-onboarding/SKILL.md) —— 可加载 Skill 的客户端读这个
- [docs/onboarding-playbook.md](docs/onboarding-playbook.md) —— 加载不了 Skill 时，把这份话术贴给智能体

## 目录

```
.
├── AGENTS.md                  # 通用规范 + 项目规矩模板
├── memory-server/             # MCP 服务 + 实时看板 + 测试
├── engine-guardian/           # 空闲自动关闭守护器
├── scripts/new_project.py     # 新项目一键接入
├── skills/gogs-onboarding/    # 给 AI 智能体加载的接入技能
└── docs/                      # 接入操作话术
```

`memory-server/` 与 `engine-guardian/` 可分别独立部署。

## 安全边界

- MCP 服务与看板都**只绑 `127.0.0.1`**，不对外暴露；需要公网访问请自行加鉴权反代。
- 写记忆前必须 30 分钟内读过该项目（`read_memory` 门禁），防止凭过期记忆覆盖项目现状。
- `write_memory` 拒绝把长内容缩写成很短的内容（`overwrite_shrink_ratio`），防误覆盖。
- 接入脚本的 Git 令牌走 `http.extraHeader`，不落 `.git/config`、不进 remote URL。
- engine-guardian 唯一的破坏性操作是按 `pgrep -f` 匹配串 kill 进程，`match` 必须能唯一认出服务。

## License

[MIT](LICENSE)
