---
name: gogs-onboarding
description: |
  把新项目接入自建项目服务器（Gogs + 共享记忆 MCP），一次完成：建仓、生成
  AGENTS.md、首次推送、共享记忆登记档案。Use when the user says "接入新项目"、
  "建个新项目"、"新项目上服务器"、"onboard project"、"给这个项目建仓库"，或要把一个
  已有 git 仓库接上 Gogs 远端。Do NOT use for: 在已有仓库里改代码、单纯 push、
  建普通非 git 目录，或访问 Gogs 网页界面找信息。
---

# 新项目一键接入

## Inputs to collect

调用前先确认三样，缺哪问哪，**不要替用户编**：

- **项目显示名** —— 会用作共享记忆的 project key、`AGENTS.md` 标题
- **英文 slug** —— Gogs 仓库名，只能 ASCII 字母数字加 `-` `_` `.`
- **一句话描述** —— 会写进仓库描述、README、AGENTS.md 和记忆档案（`--desc`，必填）

slug 没给就根据显示名拟一个，让用户确认后再动手。仓库名会进 URL 和 clone 命令，
定下来就基本不该改了。

另需两个环境变量（没设脚本会直接退出并提示）：

- `GOGS_TOKEN` —— Gogs 个人访问令牌
- `GOGS_USER` —— Gogs 用户名（也可命令行 `--user`）

## Procedure

### 1. 先设令牌与服务器地址

```bash
export GOGS_BASE='http://localhost:3000'   # 你的 Gogs 地址
export GOGS_USER='<你的用户名>'
export GOGS_TOKEN='<令牌>'
export PROJECTS_HOME="$HOME/projects"      # 新项目默认根目录，可选
```

令牌生成：Gogs 网页 → 头像 → 个人设置 → 应用 → 生成新令牌。
**Gogs 令牌没有分级权限**（页面写着"任何令牌都对您的帐户拥有完整的操作权限"），
**没有"全部授权"复选框可勾**，找那个是浪费时间。

### 2. 跑接入脚本

**先定位脚本再跑**——脚本路径是相对于**本 SKILL.md 所在目录**的，
不是相对于你当前的工作目录。照抄相对路径十有八九会 `can't open file`。

```bash
# 稳妥写法：用本文件自己的位置拼出脚本绝对路径
SKILL_DIR="$(cd "$(dirname "<本 SKILL.md 的路径>")" && pwd)"
python3 "$SKILL_DIR/scripts/new_project.py" <显示名> --slug english-slug --desc "描述"
```

也可以用仓库根目录下的同一脚本：`scripts/new_project.py`（两份内容一致）。

**`--desc` 是必填的，而且会被质量闸门检查。**

这句话同时写进四个地方：Gogs 仓库描述、`README.md`、`AGENTS.md`、共享记忆档案。
它是别人（和别的智能体）判断「这项目是干什么的」的第一手依据。

脚本会拦下两种敷衍：

- **太短**（少于 10 字）
- **占位话**（分段后每段都是「测试 / 新建项目 / demo / 临时 / 待补」这类词）

闸门按**词段**判断而不是包含判断，所以「自动化测试框架」这种真项目名不会被误伤，
而「demo项目 测试 临时」会被拦下。

```bash
# ✅ 这样写
--desc "把一批原始文件自动加工成可交付结果，替代人工逐个处理"
# ❌ 这样会被拒
--desc "测试"
--desc "新建项目"
--desc "demo项目 测试 临时"
--desc "这个项目的用途待补充"     # 含「待补」标记
```

**接入已有的项目目录（最常见，别漏 `--dir`）：**

```bash
python3 "$SKILL_DIR/scripts/new_project.py" <显示名> --slug english-slug \
  --dir /真实/项目路径 --desc "描述"
```

**建全新项目**要显式加 `--create`，否则目录不存在脚本直接退出
（防止在别处建空目录却报"接入完成"的假成功）：

```bash
python3 "$SKILL_DIR/scripts/new_project.py" <显示名> --slug english-slug --create --desc "描述"
```

默认建**私有仓**；要建公开仓加 `--public`。

脚本一次做完：校验本地目录 → Gogs 建仓 → 本地 `git init` → 生成 `AGENTS.md`
→ 首次提交推送 → 通过 MCP 登记共享记忆档案。重复执行幂等（已存在的仓库会沿用、
`AGENTS.md` 不覆盖）。**只推了骨架文件时会主动告警**，别把那个告警划过去。

**已有的 git 仓库**（有历史、要保留提交）不要用脚本建新目录，改成只接远端：

```bash
git remote add gogs "$GOGS_BASE/$GOGS_USER/<slug>.git"
AUTH=$(printf '%s:%s' "$GOGS_USER" "$GOGS_TOKEN" | base64)
git -c http.extraHeader="Authorization: Basic $AUTH" push -u gogs main
```

用 `extraHeader` 而不是把令牌拼进 remote URL，令牌才不会残留在 `.git/config` 里。

### 3. 报告接入结果

给出：仓库地址、本地路径、`read_memory("<项目显示名>")` 这句开工指令。

## Protocol traps

这几条是实际踩出来的，不是防御性编程，踩中的表现都极具误导性：

**共享记忆写之前必须认领角色。** 开箱模式（`strict_roster=False`）下
`claim_role("role-onboarding")` 会自动登记角色；收紧过名册的部署要先在
`_roster.json` 里准备好角色。未认领时的拒绝长这样：`❌ 还没认领角色, 写不了。`

**服务器的拒绝放在 content 文本里，JSON-RPC 层照样返回 success，`isError` 也是 false。**
只看协议层错误会把拒绝当成成功。判断成功要看返回文本是否以 `❌` 开头。

**Gogs 对已存在的仓库名可能返回 500，不是 409。** 所以脚本先
`GET /api/v1/repos/<user>/<slug>` 查存在再建，手工操作时也要先探活。

**访问地址必须用 `localhost:3000`，不能用 `127.0.0.1:3000`。**
Gogs 的 `_csrf` Cookie 绑死 `Domain=localhost`，用 IP 访问时 Cookie 不带过去，
登录和建仓都失败，而且报错长得像"密码不对"。

**命令行未必走系统代理。** `curl` 和 `git` 可能不读桌面系统的代理设置。
**只有访问外网时**才需要设 `http_proxy`/`https_proxy`；访问 localhost 不用。

**禁止直接改 Gogs 的数据库。** Gogs 进程持有 SQLite 时外部写入会让库里等值比较失效，
表现是用户名页面全部 404，可能修不回来。一律走 API。

## 角色

**不要预先框定角色。** 各项目的角色由接手该项目的智能体按实际分工自主创建。
接入时生成的 `AGENTS.md` 只写规矩（读记忆、留台账、跨块改动先报备），
不给固定角色表。

## Output contract

接入完成后输出：

- 仓库地址 `$GOGS_BASE/<user>/<slug>`
- 本地路径
- 开工指令 `read_memory("<项目显示名>")`
- 若共享记忆登记失败：**明确说失败**并给手动补的步骤，不要谎报成功

## Failure handling

- **没设 GOGS_TOKEN / GOGS_USER** → 脚本第 1 步就退出并说明怎么设，不要尝试无认证建仓
- **记忆登记失败** → 建仓和推送已经成功，不算整体失败。报告失败原因，
  给出手动补的 `claim_role` + `write_memory` 指令
- **推送失败** → 打印 stderr 原文让用户判断，常见是磁盘满或仓库名冲突
- **脚本不存在** → 多半是路径搞错了。脚本相对的是本 SKILL.md 所在目录，
  不是当前工作目录；先用 `SKILL_DIR` 拼绝对路径。确认真的没有，才改用手工 git 命令
- **远端有历史、本地是空目录** → 脚本会提前拦下并让你 clone 或换 slug；
  别硬推，非快进拒绝之后容易把两边搞得更乱

## Windows (PowerShell) notes

`new_project.py` 只用标准库，Python 3.8+ 在 Windows 也能跑，差异：

- 环境变量写法：`$env:GOGS_TOKEN='<令牌>'` 而非 `export GOGS_TOKEN='...'`
- `PROJECTS_HOME` 要设成实际的项目根目录
- `base64` 计算：
  `[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$env:GOGS_USER`:$env:GOGS_TOKEN"))`
- 访问地址同理必须写 `localhost:3000`，用 `127.0.0.1` 会出现 Cookie 域名不匹配的假失败

## Examples

**Input**: "把 my-robot 这个项目接到服务器上"

**Output**: 确认 slug 用 `my-robot`（非 ASCII 名字会被 Gogs 拒绝），
设好令牌/用户名后跑脚本（已存在目录记得 `--dir`），最后报：
仓库 `http://localhost:3000/<user>/my-robot`，本地路径，
开工前 `read_memory("my-robot")`。

**Input**: "这个仓已经一堆提交了，接上去，别丢历史"

**Output**: 不用脚本建新目录（会丢历史）。只 `git remote add gogs` +
`extraHeader` 推送，再手工登记共享记忆档案。
