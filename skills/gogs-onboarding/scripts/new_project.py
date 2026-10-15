#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newproject.py —— 一键把新项目接入项目服务器

做的事（缺一不可，少一步就不算接入）：
  1. **先校验本地目录**（早于建仓——见下方「假成功」说明）
  2. 在 Gogs 建仓
  3. 生成 AGENTS.md（统一规范模板，规矩随代码走）
  4. git init + 首次 commit + 推送到 Gogs
  5. 通过 MCP 在共享记忆登记项目档案（唯一事实源）

「假成功」是这个脚本唯一真正危险的失败模式：
  用户说「把 X 项目接进来」，X 其实在某个名字不同的目录里，
  脚本却按默认路径另建一个空目录，推三个骨架文件上去，
  最后报告「接入完成」——原项目一个字节没动。
两道防线：
  A. 第 1 步就拦：目标目录不存在或为空，脚本直接退出，要建新目录必须显式加 --create。
  B. 推完再查：本次推送若只有 AGENTS.md/README.md/.gitignore 这三个骨架文件，
     立刻大声告警，不让「接错目录」安静地报成功。

只依赖标准库。用法：
  # 接入一个已存在的项目目录（最常见）
  python3 newproject.py 项目中文名 --slug english-slug --dir /真实/项目路径
  # 接入全新项目（显式声明，允许新建目录）
  python3 newproject.py 项目中文名 --slug english-slug --create
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ── 服务器配置（环境变量覆盖；换机器改环境，不用改代码）──────────
GOGS_BASE = os.environ.get("GOGS_BASE", "http://localhost:3000").rstrip("/")
GOGS_USER = os.environ.get("GOGS_USER", "")
GOGS_TOKEN = os.environ.get("GOGS_TOKEN", "")
GOGS_PASSWORD = os.environ.get("GOGS_PASSWORD", "")
MCP_URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8000/mcp")
WORK_ROOT = os.path.expanduser(os.environ.get("PROJECTS_HOME", "~/projects"))
# 框架接入类工作在台账里使用的角色（开箱模式 strict_roster=False 时
# 首次 claim 会自动登记；已收紧名册的部署请在 _roster.json 里准备同名角色）
ONBOARDING_ROLE = os.environ.get("ONBOARDING_ROLE", "role-onboarding")

# ── AGENTS.md 模板 ───────────────────────────────────────────────
AGENTS_TEMPLATE = """# AGENTS.md —— {name} · AI 协作铁律

> **这个文件是本项目唯一跨会话、跨客户端的规则载体。**
> 规矩写在对话里换个窗口就失忆；写在这里的规矩，clone 仓库就自动生效。
> 改这份文件 = 改项目规矩，改之前先跟主人说一声。

本项目已接入统一的项目服务器，规矩与其他项目一致。

---

## 0. 三十秒上手

```bash
# 1. 读项目记忆（MCP，不是这个仓库）—— 硬门禁，不读不许开任务
read_memory("{name}")
# 2. 报名字（取语义名，别用平台默认值）
register_name("{name_short}")
# 3. 声明任务，拿到 task_id
start_task("{name}", "任务名", "要干什么")
# 4. 干
# 5. 收工
finish_task(task_id, "结果")
append_memory("{name}", "时间 完成了X 证据Y 下一步Z")
```

**项目记忆（MCP `{mcp_url}`）是唯一事实源。**
本文件和你的自身记忆都只是背景参考；冲突时以项目记忆为准。

---

## 1. 规矩

**角色不预先框定。** 各角色由接手本项目的智能体按实际分工自主创建，
在共享记忆的名册里登记清楚"谁管哪块地"即可。下面只写规矩，不给固定角色表。

- **读记忆是硬门禁。** 开工前 `read_memory("{name}")`，不读不许开任务。
  项目记忆是唯一事实源，本文件和自身记忆都只是背景参考，冲突以记忆为准。
- **身份干活时选。** 按**要做的事**认领角色，不按用的哪个客户端。
  名册里没有合适的就自己建一个，建完在记忆里说明它管哪块地。
- **名册是全局共享的，但里面的角色是各项目自己建的。** 别认领不属于自己的角色——
  别的项目的角色写的是它的目录和职责，认了等于按错误的地图干活。
- **跨块改动先报备。** 要动别人地盘（另一个角色负责的区域、或框架本身），
  必须先报备 + 主人点头，不管你是谁、认了哪个角色。没报备就动 = 违规。
- **不许静默动手。** 台账外的零散动作，收工前补一条 `append_memory`。
- **撞名会被服务端拒绝。** 注册名取语义名，别用平台默认值。

---

## 2. 干活必走台账

任何角色的任何工作都必须 `claim_role` → `start_task` → `finish_task` 三连，
不许只干在 git / 对话里不留台账。

- 台账外的零散动作：收工前补一条 `append_memory`，不许静默消失。
- 换角色要**重读记忆**，门禁会拦你。

---

## 3. 本项目

| 项 | 值 |
|---|---|
| 项目名（中文） | {name} |
| 仓库 slug | `{slug}` |
| 仓库地址 | {repo_url} |
| 描述 | {desc} |

---

## 4. 服务器环境事实（别踩）

1. **命令行未必走系统代理**：不少系统上 `curl`/`git` 不读桌面系统的代理设置，
   访问外网慢时显式 `--proxy` 或设 `http_proxy`/`https_proxy`；访问本机服务不受影响。
2. **Cookie 可能绑域名**：某些 Git 服务（如 Gogs）的 CSRF Cookie 绑固定域名，
   换 `127.0.0.1` 访问 `localhost` 站会导致登录/建仓失败且报错像"密码不对"。
3. **launchd 读不了用户目录**（macOS）：常驻服务要扫用户目录，用双击 `.command` 脚本。
4. **禁止直接改服务端数据库**，一律走 API。
5. **服务端空闲可能被自动关闭**：若部署了 engine-guardian，共享记忆里没有 running
   任务且持续空闲超过阈值就会关掉本机服务，需要时重新启动即可。

---

## 5. 不要做的事

- ❌ 别在仓库外单独存规矩文件——规矩只在 `AGENTS.md` 里
- ❌ 别把令牌写进 `remote URL` 或任何提交进仓库的文件
- ❌ 别静默动手：跨块改动先报备
"""

README_TEMPLATE = """# {name}

> {desc}

## 这是什么

一句话说清它解决什么问题、给谁用。**接手本项目的智能体请在下面把这段补完整**——
留一句空话不如写清楚边界，走错方向比慢一点更贵。

<!-- 补充说明：为什么做、给谁用、不做什么、当前处于什么阶段 -->

## 基本信息

| 项 | 值 |
|---|---|
| 项目名 | {name} |
| 仓库 slug | `{slug}` |
| 仓库地址 | {repo_url} |
| 本地路径 | `{local}` |
| 接入时间 | {now} |

## 怎么开工

**规矩写在仓库根目录的 `AGENTS.md` 里**，clone 下来就自动加载。
项目现状不在这个文件里——那是共享记忆的活。

```bash
read_memory("{name}")     # 硬门禁：不读不许开任务
register_name("{slug}")   # 取 slug，天然唯一不撞名
claim_role("role-xxx")   # 名册里没有合适的就自己建，见 AGENTS.md 第 1 节
start_task("{name}", "任务名", "要干什么")
finish_task(task_id, "结果")
append_memory("{name}", "时间 完成了X 证据Y 下一步Z")
```

> **项目记忆是唯一事实源**（MCP `{mcp_url}`）。
> 本文件和你自身的记忆都只是背景参考，冲突以项目记忆为准。

## 目录

```
{slug}/
├── AGENTS.md     协作铁律（规矩随代码走）
├── README.md     你正在读的这份
└── ...           项目代码
```

---

<sub>由项目服务器的 `newproject.py` 于 {now} 接入。</sub>
"""

MEMORY_TEMPLATE = """## 当前状态

项目「{name}」已接入项目服务器。

- 仓库：`{slug}`（{repo_url}）
- 描述：{desc}
- 接入时间：{now}
- 规矩载体：`AGENTS.md`（仓库根目录，随代码走，随代码版本化）

## 最近进展

- {now} 完成接入：Gogs 建仓 → 生成 AGENTS.md → 首次提交推送 → 共享记忆登记档案。

## 产物路径

- 本地副本：{local}
- 代码托管账号：`{user}`（仓库 slug：`{slug}`）

## 下一步

1. 首次开工时按实际分工认领/创建角色（角色不预设，谁干活谁建，见 AGENTS.md 第 1 节）
2. 首次开工前先 `read_memory("{name}")` 对齐现状

## 关键决策

- **规矩只存 `AGENTS.md`**，不另设仓库外的规则文件。
- **项目记忆是唯一事实源**，AGENTS.md 只是背景参考。
- **完工必须走台账**：`start_task` → `finish_task`，不许静默消失。
"""


def die(msg, code=1):
    print("❌ %s" % msg, file=sys.stderr)
    sys.exit(code)


def ok(msg):
    print("✅ %s" % msg)


def info(msg):
    print("   %s" % msg)


def warn(msg):
    print("⚠️  %s" % msg)


def gogs_api(method, path, payload=None):
    url = GOGS_BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "token " + GOGS_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:300]}
    except Exception as e:
        return 0, {"error": str(e)}


class MCP:
    """极简 MCP 客户端，只够调 tools/call。"""

    def __init__(self, url):
        self.url = url
        self.sid = None
        self._id = 0

    def _post(self, body, notify=False):
        self._id += 1
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if self.sid:
            req.add_header("Mcp-Session-Id", self.sid)
        with urllib.request.urlopen(req, timeout=30) as r:
            if not self.sid:
                self.sid = r.headers.get("Mcp-Session-Id")
            raw = r.read().decode()
        if notify or not raw.strip():
            return None
        # SSE: event: message\ndata: {...}
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(raw)

    def connect(self):
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "newproject", "version": "1.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)

    def call(self, tool, args):
        """调一个工具。注意：服务器的拒绝是放在 content 文本里的（开头一个 ❌），
        JSON-RPC 层照样返回 success，isError 也可能是 false。
        只看 JSON-RPC error 会把拒绝当成成功——2026-10-14 就这么误报过一次。"""
        resp = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})
        if not resp:
            return False, "无响应"
        if "error" in resp:
            return False, str(resp["error"])[:200]
        result = resp.get("result", {})
        content = result.get("content", [])
        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if result.get("isError") or text.lstrip().startswith("❌"):
            return False, text.strip()[:200]
        return True, text

    def register_project(self, name, content):
        """共享记忆要求先认领身份才能写。接入类工作用 ONBOARDING_ROLE。"""
        self.call("list_roles", {})
        good, msg = self.call("claim_role", {"role": ONBOARDING_ROLE})
        if not good:
            return False, "认领角色失败：%s" % msg
        try:
            return self.call("write_memory", {"project": name, "content": content})
        finally:
            self.call("release_role", {})


def git(path, *args):
    return subprocess.run(["git", "-C", path] + list(args),
                          capture_output=True, text=True, timeout=180)


def main():
    ap = argparse.ArgumentParser(description="一键接入新项目")
    ap.add_argument("name", help="项目显示名，如 my-cool-app")
    ap.add_argument("--slug", required=True, help="仓库英文名（ASCII），如 my-cool-app")
    ap.add_argument("--user", default=GOGS_USER,
                    help="代码托管用户名（默认取环境变量 GOGS_USER）")
    ap.add_argument("--public", action="store_true",
                    help="建公开仓；默认建私有仓")
    ap.add_argument("--desc", required=True,
                    help="一句话项目介绍（必填）。会写进仓库描述、README、AGENTS.md 和共享记忆档案。"
                         "写清楚这项目解决什么问题、给谁用，别写「测试」「新建项目」这种占位话")
    ap.add_argument("--dir", default="", help="本地目录，默认 $PROJECTS_HOME/<name>；接入已有项目请显式指定")
    ap.add_argument("--create", action="store_true",
                    help="显式声明「这是全新项目」，允许新建目录。不加则目录不存在就退出")
    ap.add_argument("--skip-memory", action="store_true", help="跳过共享记忆登记")
    args = ap.parse_args()

    user = args.user.strip()
    if not GOGS_TOKEN:
        die("没设 GOGS_TOKEN。先 export GOGS_TOKEN='你的令牌'，"
            "或参考仓库 AGENTS.md 的接入说明生成。")
    if not user:
        die("没设代码托管用户名：export GOGS_USER='你的用户名' 或命令行加 --user。")
    if not args.slug.replace("-", "").replace("_", "").replace(".", "").isalnum() \
            or not all(c.isascii() for c in args.slug):
        die("slug 只能用 ASCII 字母数字和 - _ .（仓库名会进 URL，中文会被 Gogs 拒绝）")

    # 项目介绍的质量闸门。
    # 为什么较真：--desc 会同时进 Gogs 仓库描述、README、AGENTS.md 和共享记忆档案，
    # 是别人（和别的智能体）判断「这项目是干嘛的」的第一手依据。
    # 写成「测试」「新建项目」等于把这句话永久钉在四个地方，比空着更糟。
    desc = args.desc.strip()
    if len(desc) < 10:
        die("项目介绍太短（%d 字）：%r\n"
            "    这句话会写进 Gogs 仓库描述、README、AGENTS.md 和共享记忆档案，"
            "是别人判断这项目干什么的第一手依据。\n"
            "    至少写清：解决什么问题、给谁用。" % (len(desc), desc))
    lazy = {"测试", "test", "testing", "新建项目", "新项目", "项目", "todo", "tbd",
            "待补", "待完善", "待定", "暂无", "无", "空", "demo", "demo项目",
            "临时", "临时项目", "样例", "example", "hello", "xxx", "asdf", "aaa"}
    # 不能用「包含」判断——「自动化测试框架」是真项目名，含有「测试」但不是占位话。
    # 所以按分隔符切成词段，只有当**每一段**都是占位词时才判定为敷衍。
    segments = [s for s in re.split(r"[\s,，。.、;；:：/|]+", desc) if s]
    if segments and all(s.lower() in lazy for s in segments):
        die("项目介绍是占位话：%r\n"
            "    这句话会被永久提交进仓库、写进 AGENTS.md 和共享记忆档案。\n"
            "    请写清楚这项目解决什么问题、给谁用。" % desc)
    if re.search(r"(待补|待完善|待定|tbd|todo|xxx)", desc, re.I):
        die("项目介绍里有占位标记：%r\n"
            "    这句话会被永久提交进仓库、写进 AGENTS.md 和共享记忆档案，别留着以后再说。" % desc)

    local = Path(args.dir) if args.dir else Path(WORK_ROOT) / args.name

    # 只有代码是项目主体的项目才建仓。
    # 纯文档 / 纯素材 / 预研产物的项目复制进 Gogs 等于制造第二份会过期的副本：
    # 那份一改就和本地对不上，而它又没有独立的价值。留在本地目录，
    # 在 project-server 的 README 清单里写一句本地路径就行。
    code_ext = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
                ".swift", ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".sh"}
    if not args.create and local.exists():
        try:
            names = [p.name for p in local.iterdir()]
        except Exception:
            names = []
        if names:
            has_code = any(
                (p.is_file() and p.suffix in code_ext)
                for p in list(local.iterdir())[:400])
            doc_like = sum(1 for n in names if n.endswith((".md", ".txt", ".pdf")))
            if not has_code and doc_like:
                die("这个目录里没看到代码文件，只有 %d 个文档。\n"
                    "    **文档类项目不要建仓**——复制进 Gogs 等于多一份会过期的副本。\n"
                    "    需要的话：把这个项目在 project-server 的 README 清单里加一行，\n"
                    "    写清它是干嘛的 + 本地路径，用超链接指向本地位置。" % doc_like)
    repo_url = "%s/%s/%s" % (GOGS_BASE, user, args.slug)
    # 注册名用 slug 而不是显示名：多个前缀相同的显示名会撞 register_name，
    # 而 slug 在同一个托管账号下天然唯一。
    name_short = args.slug

    print("\n项目：%s（%s）\n" % (args.name, args.slug))

    # 1. 本地目录校验 —— 必须早于建仓
    #    这里有个容易造成「假成功」的坑：不给 --dir 时默认指向 WORK_ROOT/<中文名>。
    #    如果用户本意是接入一个**已存在的**、但名字不同的项目目录（比如 my-app），
    #    脚本早先会在别处建一个空壳目录，原项目原封不动，最后报告却说接入完成。
    #    先校验的目的很直接：目录不对就别在 Gogs 里留垃圾仓，清理成本远低于事后收拾。
    if args.dir:
        if not local.exists():
            die("--dir 指向的目录不存在：%s" % local)
        if not local.is_dir():
            die("--dir 指向的不是目录：%s" % local)
        if not any(local.iterdir()):
            warn("--dir 指向的目录是空的：%s（明确指定了路径，按你的判断走）" % local)
        else:
            ok("使用指定目录 %s（%d 项）" % (local, len(list(local.iterdir()))))
    elif args.create:
        # --create 是显式声明「这里要建新项目」，所以空目录不算错
        if local.exists() and not local.is_dir():
            die("--create 指向的位置已被文件占用：%s" % local)
        if local.exists() and any(local.iterdir()):
            die("目录非空，拒绝用 --create 往里塞新项目：%s\n"
                "    里面的 %d 项会被当成本项目代码提交。\n"
                "    要接入这个已有项目，去掉 --create 直接跑。" % (local, len(list(local.iterdir()))))
        local.mkdir(parents=True, exist_ok=True)
        ok("已按 --create 使用/新建目录 %s" % local)
    elif local.exists():
        if not any(local.iterdir()):
            die("目标目录存在但是空的：%s\n"
                "    空目录通常意味着路径拼错了（比如你以为是 my-ap，其实是 my-app）。\n"
                "    确认要在那儿建全新项目，加 --create；否则用 --dir 指向真实路径。" % local)
        ok("本地目录已存在 %s（%d 项）" % (local, len(list(local.iterdir()))))
    else:
        die("目标目录不存在：%s\n"
            "    两种可能，处理方式不同，别猜：\n"
            "    ① 这是全新项目 → 确认后加 --create 重新跑\n"
            "    ② 这是已有项目，只是路径/名字和我猜的不一样 → 用 --dir 指向真实路径\n"
            "    （在没确认之前脚本不会去 Gogs 建仓，避免留垃圾仓）" % local)

    # 2. Gogs 建仓（先查存在再建——已存在时 Gogs 返回 500 而不是 409）
    st, existing = gogs_api("GET", "/api/v1/repos/%s/%s" % (user, args.slug))
    if st == 200:
        info("仓库已存在，沿用")
        # 远端有历史 + 本地还没 git 历史 = 非快进推送，git 会报
        # "! [rejected] ... (fetch first)"。这里提前拦，免得推到一半才发现。
        if not existing.get("empty", True) and not (local / ".git").exists():
            die("远端 %s 已经有提交，但 %s 还没初始化 git。\n"
                "    直接 push 会被拒（非快进 rejected / fetch first），先选一条：\n"
                "    ① 本地就是这仓库的副本 → git clone %s.git 到本地再干活\n"
                "    ② 你要接的是另一个项目 → 换个 slug，别往已有仓库里塞"
                % (existing.get("full_name"), local,
                   existing.get("clone_url", repo_url)))
    else:
        status, body = gogs_api("POST", "/api/v1/user/repos", {
            "name": args.slug,
            "description": desc,
            "private": not args.public, "auto_init": False, "default_branch": "main",
        })
        if status == 201:
            ok("Gogs 建仓 → %s" % body.get("full_name"))
        else:
            die("建仓失败 HTTP %s: %s" % (status, json.dumps(body, ensure_ascii=False)[:200]))

    # 3. AGENTS.md
    if not (local / "AGENTS.md").exists():
        (local / "AGENTS.md").write_text(AGENTS_TEMPLATE.format(
            name=args.name, slug=args.slug, desc=desc,
            repo_url=repo_url, name_short=name_short, mcp_url=MCP_URL), encoding="utf-8")
        ok("生成 AGENTS.md（规矩随代码走）")
    else:
        info("AGENTS.md 已存在，不覆盖")

    if not (local / ".gitignore").exists():
        (local / ".gitignore").write_text(".DS_Store\n*.log\n__pycache__/\n", encoding="utf-8")
    if not (local / "README.md").exists():
        (local / "README.md").write_text(
            README_TEMPLATE.format(name=args.name, slug=args.slug, desc=desc,
                                   repo_url=repo_url, local=local, mcp_url=MCP_URL,
                                   now=__import__("datetime").datetime.now().strftime("%Y-%m-%d")),
            encoding="utf-8")
        ok("生成 README.md（项目介绍 + 开工指引）")

    # 4. git init + push
    if not (local / ".git").exists():
        git(str(local), "init", "-q", "-b", "main")
        git(str(local), "add", "-A")
        # 不显式覆盖提交身份：用使用者自己的 git config；没配时给中性兜底，
        # 避免脚本替别人的提交署名。
        ident = git(str(local), "config", "user.email")
        ident_args = [] if ident.returncode == 0 and ident.stdout.strip() else [
            "-c", "user.name=newproject", "-c", "user.email=newproject@local"]
        subprocess.run(["git", "-C", str(local)] + ident_args + [
                        "commit", "-q",
                        "-m", "接入项目服务器: %s\n\n生成 AGENTS.md，规矩随代码走。" % args.name])
        git(str(local), "remote", "remove", "gogs")
        git(str(local), "remote", "add", "gogs", "%s.git" % repo_url)

    # 认证用 extraHeader，令牌不落 .git/config
    import base64
    auth = base64.b64encode(("%s:%s" % (user, GOGS_TOKEN)).encode()).decode()
    r = git(str(local), "-c", "http.extraHeader=Authorization: Basic " + auth,
            "push", "-u", "gogs", "main")
    if r.returncode == 0:
        ok("推送成功 → %s" % repo_url)
    else:
        die("推送失败：%s" % (r.stderr or r.stdout)[:300])

    # 防线 B：推完再查一次「接错目录」
    #    只推了骨架文件 = 原项目代码一个字节都没进去。
    #    这种情况下脚本依然完成了技术动作，但结果对用户是「假成功」，必须喊出来。
    tracked = [p for p in git(str(local), "ls-tree", "-r", "--name-only", "HEAD").stdout.split("\n") if p.strip()]
    skeleton = {"AGENTS.md", "README.md", ".gitignore"}
    only_skeleton = bool(tracked) and set(tracked) <= skeleton
    if only_skeleton:
        warn("这次推送只有 %s —— 没有接入任何原有项目代码。" % "、".join(sorted(tracked)))
        warn("如果你本意是接入一个**已存在**的项目，那多半是目录指错了。")
        warn("真正的项目在别处的话，删掉这个仓库重来：")
        warn("  python3 newproject.py %s --slug %s --dir /真实/路径" % (args.name, args.slug))
    elif tracked:
        info("本次推送 %d 个文件" % len(tracked))

    # 5. 共享记忆登记
    if args.skip_memory:
        info("已跳过共享记忆登记（--skip-memory）")
    else:
        try:
            mcp = MCP(MCP_URL)
            mcp.connect()
            content = MEMORY_TEMPLATE.format(
                name=args.name, slug=args.slug, repo_url=repo_url,
                desc=desc, user=user, local=local,
                now=__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"))
            good, msg = mcp.register_project(args.name, content)
            if good:
                ok('共享记忆已登记 → read_memory("%s")' % args.name)
            else:
                info("共享记忆登记失败（不影响建仓和推送）：%s" % msg[:160])
                info('   手动补：先 claim_role("%s")，再 write_memory("%s", ...)' % (ONBOARDING_ROLE, args.name))
        except Exception as e:
            info("共享记忆登记失败（不影响建仓）：%s" % str(e)[:120])

    if only_skeleton:
        print("\n⚠️  技术上跑完了，但**不等于接入了你的项目**。")
        print("   智能体开工前仍需先核对：read_memory(\"%s\")" % args.name)
    else:
        print("\n接入完成。智能体开工前：read_memory(\"%s\")" % args.name)
    print("   本地目录里现在有：%s\n"
          % "、".join(sorted(p.name for p in local.iterdir())[:12]))


if __name__ == "__main__":
    main()
