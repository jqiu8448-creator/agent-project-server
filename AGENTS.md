# AGENTS.md —— 项目服务器 · AI 协作铁律

> **这个文件是框架的通用规范，也是每个被接入项目的规矩模板。**
> 规矩写在对话里换个窗口就失忆；写在这里的规矩，clone 仓库就自动生效。
> 本文件只放通用规矩；项目专属的规矩写进各自仓库的 `AGENTS.md`。

这套框架不是业务代码，是**所有项目的协作基础设施**。每个项目在这里是一个独立仓库，
每个仓库根目录都带自己的 `AGENTS.md`。

---

## 0. 三十秒上手

```text
# 1. 读项目记忆（MCP，硬门禁，不读不许开任务）
read_memory("<项目名>")
# 2. 报名字（取语义名，别用平台默认值，别跟别人撞）
register_name("<客户端>-<语义名>")
# 3. 认领一个角色（怎么选见第 2 节；开箱模式可直接认领 role-<语义名> 自动登记）
claim_role("role-xxx")
# 4. 声明任务，拿到 task_id
start_task("<项目名>", "任务名", "要干什么")
# 5. 干
# 6. 收工
finish_task(task_id, "结果")
append_memory("<项目名>", "时间 完成了X 证据Y 下一步Z")
```

**项目记忆（MCP，默认 `http://127.0.0.1:8000/mcp`）是唯一事实源。**
本文件和智能体自身的记忆都只是背景参考；冲突时以项目记忆为准。

---

## 1. 这套服务器长什么样

| 组件 | 作用 | 本仓位置 |
|---|---|---|
| Git 服务（示例用 [Gogs](https://gogs.io)） | 代码托管；`AGENTS.md` 随代码走 | 你自己部署，脚本默认 `http://localhost:3000` |
| 共享记忆 MCP 服务 | 唯一事实源：项目现状、任务台账、角色名册、审计流水 | `memory-server/` |
| engine-guardian | 台账里没有 running 任务且持续空闲时，自动关闭本机服务 | `engine-guardian/` |
| new_project.py | 新项目一键接入（建仓 / 规矩模板 / 首推 / 记忆登记） | `scripts/new_project.py` |

建议监听范围**只绑 `127.0.0.1`**，不对局域网暴露；需要公网访问请自行加鉴权反代。

### ⚠️ localhost 与 127.0.0.1 可能不等价

Gogs 的 `_csrf` Cookie 绑死在 `Domain=localhost`。用 `127.0.0.1:3000` 访问，
Cookie 不会带过去，**登录和建仓都会失败**，报错还长得像"密码不对"。
统一用 `localhost` 访问 Git 服务（MCP 端口用 IP 没问题，两者别混）。

---

## 2. 角色：不预先框定，谁干活谁建

**框架不提供固定角色表。** 角色表一固定就过期，过期的角色表比没有更糟——
新智能体照着领了个早就该拆的活。

`memory-server` 开箱默认 `strict_roster=False`：名册为空时，智能体第一次
`claim_role("role-<英文语义词>")` 会自动把角色登记进 `_roster.json`（热生效，不用重启）。
角色稳定后，在数据目录的 `_policy.json` 里设 `"strict_roster": true` 即可收紧。

规矩四条：

1. **按要做的事选角色，不按用的哪个客户端。**
2. **名册里没有合适的角色，就自己建一个。** 这是常态，不是例外。
3. **自建角色的规矩**：key 用 `role-<英文语义词>`（纯 ASCII）；`note` 写清
   **管哪块地**和**属于哪个项目**——别的智能体一眼能判断能不能用。
4. **跨块改动先报备。** 要动别人地盘（另一个角色负责的区域、或框架本身），
   必须先报备并得到同意，不管你认了哪个角色。**没报备就动 = 违规。**

**开工前先 `read_memory` 确认没人在做同一块地。** 台账能查到谁开着任务。

---

## 3. 干活必走台账

**任何角色的任何工作**都必须 `claim_role` → `start_task` → `finish_task` 三连，
不许只干在 git / 对话里不留台账。履职记录靠 **台账 + 审计 + git** 三源对账，
只走 git 会导致"谁干了什么"查不到。

- 台账外的零散动作：收工前补一条 `append_memory`，不许静默消失。
- 换角色要**重读记忆**，门禁会拦你。
- 代码类工作收工前要 `commit` 并推到远端，**未推送不算收工**。
- 角色名用语义名；撞名会被服务端拒绝。

---

## 4. 新项目接入

**首选：跑脚本。** 一把做完建仓、写 `AGENTS.md`、首次推送、共享记忆登记：

```bash
# 凭据与服务器地址走环境变量（令牌绝不能写进仓库）
export GOGS_BASE="http://localhost:3000"
export GOGS_USER="你的用户名"
export GOGS_TOKEN="你的令牌"
export PROJECTS_HOME="$HOME/projects"     # 新项目默认根目录

# 接入已有的项目目录
python3 scripts/new_project.py 项目显示名 --slug english-slug \
  --dir /真实/项目路径 --desc "一句话说清它解决什么问题、给谁用"
# 建全新项目（显式声明才允许新建目录）
python3 scripts/new_project.py 项目显示名 --slug english-slug --create --desc "..."
```

不带 `--dir` 也不带 `--create` 时脚本会直接退出——它无法判断你是要新建项目、
还是路径拼错了。在别处建个空壳目录再报「接入完成」，原项目纹丝不动，那是假成功，
也是这个脚本唯一真正危险的失败模式。

**手工路径**（脚本不可用时）：

```bash
# 1. 建仓库（走 API，不要去点网页）
curl -s -X POST "$GOGS_BASE/api/v1/user/repos" \
  -H "Authorization: token $GOGS_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"仓库名","description":"描述","private":true,"auto_init":false,"default_branch":"main"}'
#   201 = 成功；Gogs 对重名可能返回 500 而不是 409，建之前先 GET 探活

# 2. 接远端并首次推送（令牌走 extraHeader，不残留在 .git/config）
git init -b main && git add -A && git commit -m "init"
git remote add origin "$GOGS_BASE/$GOGS_USER/仓库名.git"
AUTH=$(printf '%s:%s' "$GOGS_USER" "$GOGS_TOKEN" | base64)
git -c http.extraHeader="Authorization: Basic $AUTH" push -u origin main

# 3. 在仓库根目录放 AGENTS.md（规矩随代码走）
```

**新仓库必须自带 `AGENTS.md`**——没有它，clone 下来的人不知道规矩、不知道台账要求。
模板里**不预填角色表**，角色留给接手项目的智能体自己建。

令牌生成：Gogs 个人设置 → 应用 → 生成新令牌。Gogs 令牌没有分级权限，
别把令牌写进 remote URL、提交记录或任何入库文件。

**只给「代码是项目主体」的项目建仓。** 纯文档、纯素材、预研产物不要复制进 Git 服务
——它们不是代码，复制进去等于制造第二份会过期的副本。这类内容留在本地目录，
在项目总清单里用一句话说明本地路径即可。脚本对"只有文档没有代码"的目录会直接拒绝。

---

## 5. 常见环境坑（实测）

| 坑 | 事实 | 怎么办 |
|---|---|---|
| **命令行不走系统代理** | 桌面系统设了代理，`curl`/`git` 未必读 | 访问外网显式设 `http_proxy`/`https_proxy`；访问本机服务不用代理 |
| **launchd 读不了用户目录（macOS）** | LaunchAgent 拉起的进程可能没有桌面/文稿访问权，报 `Operation not permitted` | 要扫用户目录的常驻服务，用双击 `.command` 脚本，别用 launchd |
| **禁止直接改服务端数据库** | Gogs 持有 SQLite 时外部写入可能让等值比较失效，且可能修不回来 | 改数据走 API；实在要动先停服务并备份 |
| **仓库名只用 ASCII** | 中文 slug 进 URL 会出问题 | slug 用英文；显示名写进 `AGENTS.md` 标题和共享记忆 project key |
| **Shell 不共享环境变量** | 一次调用一个新 shell，上一条 `export` 下条就没了 | 令牌和命令写在**同一条**里：`GOGS_TOKEN='...' python3 new_project.py ...` |
| **Cookie 绑域名** | CSRF Cookie 可能绑 `localhost` | Git 服务一律用 `localhost` 访问 |

---

## 6. 目录说明

```
.
├── AGENTS.md                # 你在读的通用规范
├── README.md                # 面向公众的项目说明
├── LICENSE
├── requirements.txt         # memory-server 的依赖
├── scripts/
│   └── new_project.py       # 新项目一键接入
├── skills/
│   └── gogs-onboarding/     # 给 AI 智能体加载的接入技能
├── memory-server/           # 共享记忆 MCP 服务 + 实时看板 + 测试（详见其 README.md）
├── engine-guardian/         # 空闲自动关闭守护器（独立可用）
└── docs/
    └── onboarding-playbook.md  # 装不了 Skill 时给智能体复制用的操作话术
```

- `memory-server/` 与 `engine-guardian/` 可以分别独立部署使用。
- 各项目的业务仓库与本框架完全分开，只在根目录放自己的 `AGENTS.md`。

---

## 7. 不要做的事

- ❌ 别把令牌写进 remote URL、提交记录或任何入库文件
- ❌ 别直接改 Git 服务的数据库（见第 5 节）
- ❌ 别在仓库外单独存规矩文件——规矩只在 `AGENTS.md` 里
- ❌ 别认领别的项目建的角色（第 2 节）
- ❌ 别把通用规矩改成某个项目的专属规矩——项目专属写进各自仓库
- ❌ 别静默动手：跨块改动先报备
