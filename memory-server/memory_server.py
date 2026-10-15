#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 共享记忆服务器 —— 一台机器上所有智能体共用的项目记忆与任务台账。

记忆落盘: $MEMORY_DB/<project>.json，默认 ~/.shared_memory/<project>.json
          (单事实源, 任何人可直接读文件)
暴露工具: read_memory / write_memory / append_memory / list_projects
          + 身份与任务台账: list_roles / claim_role / register_name
            / start_task / update_task / finish_task ...
协议:     streamable-http (HTTP 模式, 跨进程/跨客户端)

启动:  python memory_server.py   (默认 127.0.0.1:8000)
环境:  MEMORY_HOST / MEMORY_PORT / MEMORY_DB 可覆盖监听地址与数据目录
"""

from datetime import datetime

import fcntl
import json
import os
import threading
import time
import weakref

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context

#: 会话对象 → 本会话分配到的 key (2026-10-03 加)。
#: 用 WeakKeyDictionary: 会话断开后条目自动消失, 不会泄漏。
#: 解决"同平台多会话共用 clientInfo 时, 第二个会话注册不上名字"的死锁。
_SESSION_KEYS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

DB = os.path.expanduser(os.environ.get("MEMORY_DB", "~/.shared_memory"))
AUDIT = os.path.join(DB, "_audit.jsonl")   # 审计日志: 谁/何时/做了什么
CLIENTS = os.path.join(DB, "_clients.json")  # 客户端名册: client_id → 名字
TASKS = os.path.join(DB, "_tasks.json")      # 任务看板: 谁在做什么任务
HOST = os.environ.get("MEMORY_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEMORY_PORT", "8000"))


# ⚠️ _readonly.json / _policy.json 的路径**故意不在这里烤成常量**, 而是由
# _readonly_path() / _policy_path() 在调用时从 DB 现算。理由: 测试(以及任何将来
# 想换数据目录的代码)只重定向 msrv.DB 一个变量就能整体隔离; 如果路径在 import 时
# 就烤死, 改 DB 不会影响它们 —— 测试会静默读到**生产**的只读名单和策略文件。
# 这个泄漏是真实发生过的: 2026-10-05 第一次加这两个文件时就踩了, 由测试暴露。

def _readonly_path() -> str:
    return os.path.join(DB, "_readonly.json")


def _policy_path() -> str:
    return os.path.join(DB, "_policy.json")


def _roster_path() -> str:
    return os.path.join(DB, "_roster.json")


#: **规范身份名册: 一组固定的角色, 每个角色一个 key, 只有一个身份。**
#:
#: 为什么要它 (2026-10-05 主人定调): *"标记单独身份列表, 引擎/设计/评估这些,
#: 要用上谁就接上谁的身份去工作, **不要新建身份了不然太乱了**"*。
#: 之前的现实: 名册撑到 27 条, 其中一大半是 `#N` 碎片和审查脚本的垃圾 ——
#: 同一个角色反复注册出 `#2 #3 #7 #8 #9 #10 #11 #12`, 每换一个就多一条。
#:
#: 光写进文档守不住(已经证明守不住)。所以做成**服务端硬拦**:
#: `register_name` 只认名册里的 key, 不在册的直接拒, 并把册子列给你看。
#: 迁移期对老 key 放行(见 LEGACY_KEYS), 但每条都回一句"你该换成哪个"。
#: 开箱默认为**空名册**——角色由接手项目的智能体按"要干什么活"自建,
#: 框架不预设任何具体项目的分工(预设的角色表一固定就过期)。
#:
#: 两种建法, 都不用改本文件、不用重启:
#:   1. 在数据目录放 _roster.json, 形如
#:        {"role-dev": {"name": "开发", "platform": "CLI",
#:                      "note": "本项目的通用开发, 管 src/ 与测试"}}
#:   2. 服务端默认关闭 strict_roster 时, 智能体可直接 claim_role 任意语义 key
#:      (见 _policy.json 的 strict_roster 开关)。
#:
#: 规矩: key 用 role-<英文语义词>, note 写清"管哪块地、属于哪个项目",
#: 跨块改动先报备。详见仓库根 AGENTS.md 第 2 节。
DEFAULT_ROSTER: dict = {}

#: 历史 key 迁移表: 老客户端带着旧身份重连时, 在这里登记"旧 key → 新角色",
#: 服务端放行并提示改名。新部署不需要, 留空即可。
LEGACY_KEYS: dict = {}


def _roster() -> dict:
    """读规范名册。文件可热改(改完下一次 register_name 就生效, 不必重启)。"""
    r = {k: dict(v) for k, v in DEFAULT_ROSTER.items()}
    try:
        with open(_roster_path(), encoding="utf-8") as f:
            over = json.load(f)
        if isinstance(over, dict):
            r.update(over)
    except Exception:
        pass
    return r


def _save_roster_entry(key: str, entry: dict) -> bool:
    """把一个新角色热写进 _roster.json(人工也在编辑这个文件, 故带文件锁)。

    开箱模式(strict_roster=False)下由 claim_role 首次认领语义角色时调用。
    成功返回 True。
    """
    path = _roster_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            if key in data:                # 已被别的会话抢先登记
                fcntl.flock(f, fcntl.LOCK_UN)
                return False
            data[key] = entry
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except Exception:
        return False


def _roster_check(key: str, ctx: Context | None = None) -> str:
    """写操作前的身份检查。返回 "" = 通过; 否则返回**该怎么办**的说明。

    刻意分成两种情况, 因为它们要做的动作完全不同:
      · **还没认领角色** → 去 list_roles 看一眼, 再 claim_role。这是正常流程。
      · **认领了但不在册** → 才是真的越界(配错了头 / 自造了 key), 该改配置。
    早先把两者混成一句"拒绝建新身份", 结果是: 新客户端一律撞墙, 却看不出
    "你只是还没认领" —— 那是它该走的正路。
    """
    if not key:
        return ("❌ 拿不到任何身份 —— 连 clientInfo 都没有, 认不了领。\n"
                "   重连一次通常就好了(客户端没自动重连是已知的毛病)。")
    if not _policy().get("strict_roster", True):
        return ""
    ros = _roster()
    if key in ros or key.split("#", 1)[0] in ros:
        return ""
    if key in LEGACY_KEYS:
        return ""                    # 迁移期放行(register_name 会附迁移提示)
    # 没配头、也没认领 → 走正路
    if not _header_key(ctx or None)[0] and not _claimed_role(ctx):
        lines = "\n".join(f"     {k:<16} {v.get('name',''):<8} {v.get('note','')}"
                          for k, v in ros.items() if not v.get("test"))
        return (f"❌ 还没认领角色, 写不了。\n"
                f"   读随便读(你得先读到名册才能决定认领谁), 但 start_task / write_memory /\n"
                f"   append_memory 都要先有身份 —— **身份是干活时选的, 不是登录时定的**。\n"
                f"\n"
                f"   两步:\n"
                f"     1. list_roles()  看有哪些角色、各管什么\n"
                f"     2. claim_role(\"role-xxx\")  认领你这次要干的活对应的那个\n"
                f"\n"
                f"   可选角色:\n{lines}\n"
                f"   换角色就再 claim 一次(但换角色后必须重读记忆)。")
    return (f"❌ 拒绝: `{key}` 不在规范名册里。\n"
            f"   主人的规矩是**不要新建身份** —— 一个角色一个身份, 用上谁就接谁的身份。\n"
            f"   你这个 key 是**配错的**(头里写错了, 或客户端自己瞎报的)。\n"
            f"   修法: 把 MCP 配置里的 X-Agent-Key 改成册内的 key, 或者干脆不配头、\n"
            f"         改成开工时 claim_role —— **那是主人的实际用法**(没有固定的客户端,\n"
            f"         谁来干什么就自己认领哪个角色)。\n"
            f"   册内可选: {', '.join(ros.keys())}\n"
            f"   自检: python check_identity.py <你的角色 key>")



#: 全部"规矩"的默认值。**每一项都能被 _policy.json 覆盖, 改完下一次请求就生效。**
#:
#: 为什么要有这个文件 (2026-10-05 加): 8000 被禁止重启 —— 重启会掐断所有客户端的
#: MCP 会话且**客户端不会自动重连**(实测连报三次 Session not found, 必须显式
#: 重连)。于是从上一次启动起, 这份代码就被冻结了: 改任何一条规矩的唯一办法是
#: 重启, 而重启等于全员失联。规矩和它的修改方式互相锁死, 这才是真正的死结。
#:
#: 解法借鉴 Pi Durable 的 "Malleable"(registry 可以在跑着的时候换掉, 正在跑的
#: 会话不受影响): 把可变的规矩抽成数据文件, **每次调用都重读**。改门禁阈值不用
#: 重启服务了。文件坏了就回落到默认值, 绝不因为一个坏配置把服务搞挂。
DEFAULT_POLICY = {
    "read_fresh_seconds": 1800,       # 读过的记忆多久算新鲜(先读后写门禁)
    "stale_minutes": 30,              # 状态档落后 log 超过多久, 读取时报警
    "overwrite_shrink_ratio": 0.20,   # write_memory 时新内容短于旧内容的这个比例 → 拒绝
    "allow_task_adoption": True,      # 重连后允许认领自己丢失的 running 任务
    "strict_roster": False,
    # 开箱默认 False: 空名册下允许 claim_role 自动登记第一个 role-<语义名> 角色,
    # 不会把新部署锁死。角色表稳定后, 在数据目录 _policy.json 里设 true 即可收紧
    # (register_name/claim 只认 _roster.json 里登记过的身份)。
    "readonly": [],                   # 只读客户端的 key 名单
    "audit": True,                    # 审计开关(排障时可临时关, 平时别关)
}


def _policy() -> dict:
    """读 _policy.json。**每次调用都重读** —— 这就是"改规矩不用重启"的全部机制。"""
    p = dict(DEFAULT_POLICY)
    try:
        with open(_policy_path(), encoding="utf-8") as f:
            over = json.load(f)
        if isinstance(over, dict):
            for k, v in over.items():
                if k in p:
                    p[k] = v
    except Exception:
        pass
    return p


mcp = FastMCP("project-memory", host=HOST, port=PORT)


#: 客户端可以在 MCP 配置里自定义的 HTTP 头 → 稳定身份。按顺序取第一个有值的。
#:
#: ⚠️ **必须纯 ASCII**。HTTP 头不允许非 ASCII 字符: 实测传
#: "Agent-角色#probe42" 会直接炸 `UnicodeEncodeError: 'ascii' codec can't
#: encode characters`(httpx 客户端和 starlette 服务端都这样), 而报错信息完全
#: 指向不到真正的原因。
#: 所以约定: **头里只放稳定的 ASCII key**(如 `agent-a3f9c2`);
#: 中文显示名照旧走 `register_name` —— 那是 JSON 请求体, 中文完全没问题。
#: 这个"ASCII key 管身份 / 中文名管显示"的分工是刻意的, 不是妥协。
AGENT_KEY_HEADERS = ("x-agent-key", "x-agent-id")


#: 会话 → 本会话**已认领**的角色 key。WeakKeyDictionary: 会话断了自己消失。
#:
#: 2026-10-05 主人澄清用法后加的: *"没有固定的客户端, 我只会让他来做什么事情,
#: 然后他自己去评估接入哪个身份去完成"*。所以身份不是**登录时**定的, 是**干活时**
#: 选的 —— 同一个客户端今天干引擎、明天干评估, 都是它自己 claim 出来的。
#: 认领是本会话范围: 换个会话要重新认领, 这正好等于"开工先认领"这条纪律。
_SESSION_ROLE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _claimed_role(ctx: Context | None) -> str:
    session = getattr(ctx, "session", None) if ctx else None
    if session is None:
        return ""
    try:
        return _SESSION_ROLE.get(session) or ""
    except Exception:
        return ""


def _header_key(ctx: Context | None) -> tuple[str, str]:
    """从 HTTP 请求头里取**固定身份**。返回 (key, 头名); 取不到返回 ("", "")。

    这条通道只给"固定机"用 —— 这台机器永远演同一个角色, 写死在配置里最省事。
    主人的实际用法是**干活时现认领**(见 claim_role), 那条路不需要头。

    为什么头值得留着 (2026-10-05 实测确认):
      · `_meta` 是**运行时注入**的, 用户改不了 —— 实测部分多会话客户端不发,
        于是身份只能从 `clientInfo` 推, 服务一重启所有会话塌回同一个 key。
      · HTTP 头是**用户配置里就能写**的, 多数 HTTP 型 MCP 客户端都支持
        `headers` 字段, 写一次就永久生效, 跨重连跨重启都不变。
      · 实测 `ctx.request_context.request` 就是 starlette 的 Request,
        **在工具函数里可以直接读头, 不需要加中间件**。
    """
    try:
        req = ctx.request_context.request if ctx else None
        hdrs = getattr(req, "headers", None)
        if not hdrs:
            return "", ""
        for h in AGENT_KEY_HEADERS:
            v = hdrs.get(h)
            if v:
                return str(v)[:60], h
    except Exception:
        pass
    return "", ""


def _client_key(ctx: Context | None) -> str:
    """客户端身份 key。

    优先级 (2026-10-03 改版, 根因是同名会话互相顶账; 2026-10-05 补齐为四级):
      0. **客户端配置里的 HTTP 头 `X-Agent-Key`** (纯 ASCII 稳定串)。**首选** ——
         用户在 MCP 配置里写一次就永久生效, 跨重连跨服务重启都不变, 且不依赖
         客户端运行时是否支持注入 `_meta`。
      0'. **请求 _meta 里的 `agent_key`** —— 运行时注入, 客户端支持就用。
      1. **请求 _meta 里的 agent 名** —— MCP 协议允许每个请求带 _meta, 客户端
         可主动报 "我是谁 + 哪个会话"(实测 server 能完整读到自定义字段)。
      2. **本会话已分配的 key** —— 同平台多会话都报同一个 clientInfo 时, 第一次
         冲突后自动分配 `key#2` / `key#3`, 并**绑到这个会话对象上**。用
         WeakKeyDictionary 存, 会话断了自动回收。⚠️ 这一档是**进程内**的, 重连即失效。
      3. **initialize 自报的 clientInfo.name** —— 兼容老客户端。
      4. 空串。

    ⚠️ 第 2/3 档从**活着的连接对象**上 derive 身份, 这是一次真实事故的根因:
    客户端重连后 `_SESSION_KEYS` 里那条没了, 于是 key 退化成
    `某客户端-runtime#N`, 而 N 还可能和上次不同 → 自己的任务再也
    `_own_task` 不了 → 任务变孤儿(压测时多次复现)。
    Pi Durable 的原则是"状态里只存名字, 不存句柄" —— 身份必须是一个**持久化的
    名字**。第 0/0' 档是正解, 第 2/3 档只是没法改客户端时的兜底。
    配套兜底是 `_adoptable()`: 重连后把丢掉的 running 任务认领回来。
    """
    session = getattr(ctx, "session", None) if ctx else None
    # ⓪ 客户端配置里的 HTTP 头 (给"固定机"用: 这台机器永远演同一个角色)
    hk, _src = _header_key(ctx)
    if hk:
        return hk
    # ⓪' 本会话**认领**的角色 (主人的实际用法: 谁来干什么, 自己评估认领哪个)
    cr = _claimed_role(ctx)
    if cr:
        return cr
    # ⓪'' 请求级 _meta 里的稳定 key
    try:
        meta = ctx.request_context.meta if ctx else None
        if meta is not None:
            extra = getattr(meta, "model_extra", None) or {}
            stable = extra.get("agent_key")
            if stable:
                return str(stable)[:60]
            who = extra.get("agent")
            if who:
                sess = extra.get("session") or ""
                sess = str(sess)[:12]
                return f"{str(who)[:28]}" + (f"#{sess}" if sess else "")
    except Exception:
        pass
    # ② 本会话之前已分配过 key (同平台多会话的第 2/3/... 个)
    if session is not None:
        try:
            got = _SESSION_KEYS.get(session)
            if got:
                return got
        except Exception:
            pass
    # ③ 退回 initialize 的 clientInfo (兼容旧客户端)
    try:
        if session is not None:
            info = session.client_params.clientInfo
            if info and info.name:
                return str(info.name)[:40]
    except Exception:
        pass
    return ""


def _assign_key(ctx: Context | None, base: str) -> str:
    """给一个撞了 base key 的新会话分配独立 key (`base#2` / `base#3` ...)。

    2026-10-03 加。同平台多会话(共用 clientInfo)在客户端不支持 _meta 时,
    只能由服务端按"第几个来"分配 —— 这样三个会话各记各的账, 且**不覆盖**
    先来者的名字 (原同名保护会让人永远注册不上, 那是从"防误伤"变成了"全锁死")。

    ⚠️ 分配时必须**剥掉已有的 #序号**再当基础 (2026-10-03 修): 否则第 3 个会话
    拿到的 key 会是 `xxx#2#2` 这样的套娃, 越滚越长且难认。这里取第一段 `#` 之前
    的部分作 base, 重新从 #2 找空位, 于是永远是 `base#2 / base#3 / base#4`。
    """
    root = base.split("#", 1)[0]          # 剥掉可能的 #N / #N#M 尾巴
    reg = _load_clients()
    n = 2
    while f"{root}#{n}" in reg:
        n += 1
    key = f"{root}#{n}"
    if hasattr(ctx, "session") and ctx.session is not None:
        try:
            _SESSION_KEYS[ctx.session] = key
        except Exception:
            pass
    return key


def _load_clients() -> dict:
    try:
        if os.path.exists(CLIENTS):
            with open(CLIENTS, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_clients(reg: dict):
    try:
        os.makedirs(DB, exist_ok=True)
        with open(CLIENTS + ".tmp", "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
        os.replace(CLIENTS + ".tmp", CLIENTS)
    except Exception:
        pass


def _clients_lock():
    return _guard(CLIENTS)


def _client_name(ctx: Context | None) -> str:
    """客户端展示名: 名册里注册过的用名字, 否则用客户端自带名, 再没有标 unknown。"""
    key = _client_key(ctx)
    if key:
        return _load_clients().get(key, key)
    return "unknown"


def _load_readonly() -> list:
    """只读客户端名单。名单同时认 _policy.json 的 readonly 和本文件(后者可热改)。"""
    names = list(_policy().get("readonly") or [])
    try:
        p = _readonly_path()
        with open(p, encoding="utf-8") as f:
            extra = json.load(f)
        if isinstance(extra, list):
            names += [str(x) for x in extra]
    except Exception:
        pass
    return names


def _save_readonly(names: list):
    try:
        os.makedirs(DB, exist_ok=True)
        p = _readonly_path()
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(sorted(set(names)), f, ensure_ascii=False, indent=2)
        os.replace(p + ".tmp", p)
    except Exception:
        pass


def _is_readonly(ctx: Context | None) -> bool:
    """当前客户端是不是只读的(探活 / 压测 / 健康检查这类非生产客户端)。

    2026-10-05 加。起因: 2026-10-03 19:42 一次真实事故 —— 一个自称
    「健康检查-临时」的客户端持有**完整写权限**, 先 read_memory 合法过了 30 分钟
    门禁, 然后 write_memory 用 17 字覆盖掉 6314 字的项目现状。探活脚本不该能改
    业务数据 —— 它只是确认服务活着, 不该有能力重写"唯一事实源"。
    单独存 _readonly.json 而不塞进 _clients.json: 名册的格式是 key→名字 的字符串
    映射, 改成嵌套结构会让所有历史读取方一起崩, 不值得为这个冒兼容风险。
    """
    key = _client_key(ctx)
    return bool(key) and key in _load_readonly()


def _write_guard(ctx: Context | None, what: str) -> str | None:
    """写操作的总闸: 先要身份在册(该认领的去认领), 再要不是只读。

    ⚠️ 顺序有讲究: **先查身份, 后查只读**。因为"还没认领角色"是最常见的状态,
    让它先撞上这一条, 拿到的信息才是真正该做的下一步(去 claim_role), 而不是
    "你是只读" —— 后者在还没认领的人看来是莫名其妙的。
    """
    bad = _roster_check(_client_key(ctx), ctx)
    if bad:
        return bad
    return _readonly_guard(ctx, what)


def _readonly_guard(ctx: Context | None, what: str) -> str | None:
    """只读客户端试图写 → 返回拒绝理由, 不是只读则返回 None。"""
    if not _is_readonly(ctx):
        return None
    return (f"🚫 拒绝：{_client_name(ctx)} 被标为只读客户端(探活/压测类), 不能{what}。\n"
            f"   它的价值是确认服务活着, 不是改数据。要写请让主人把它从只读名单里去掉。")


def _audit(ctx: Context | None, tool: str, project: str, detail: str = ""):
    """写一条审计: 谁在什么时间做了什么 (多进程安全, 追加写, 超量自动裁剪)。

    额外存 client_key (2026-10-03): 展示名会随 register_name 变, key 不会 ——
    排查"谁顶了谁"时只有 key 能定位到具体那次注册。
    """
    try:
        if not _policy().get("audit", True):
            return
        os.makedirs(DB, exist_ok=True)
        rec = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "client": _client_name(ctx),
            "client_key": _client_key(ctx),
            "tool": tool,
            "project": project,
            "detail": detail[:200],
        }
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # 防无限增长: 超 3000 行裁剪到最近 2000 (低频, 带锁)
        try:
            if os.path.getsize(AUDIT) > 2 * 1024 * 1024:
                tl, lf = _audit_lock()
                try:
                    with open(AUDIT, encoding="utf-8") as f:
                        lines = f.readlines()
                    if len(lines) > 3000:
                        with open(AUDIT + ".tmp", "w", encoding="utf-8") as f:
                            f.writelines(lines[-2000:])
                        os.replace(AUDIT + ".tmp", AUDIT)
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                    lf.close()
                    tl.release()
        except Exception:
            pass
    except Exception:
        pass


def _audit_lock():
    return _guard(AUDIT)


def _path(project: str) -> str:
    name = project.strip().replace("/", "_").replace("..", "_")
    if not name:
        raise ValueError("项目名不能为空")
    return os.path.join(DB, name + ".json")


_tlock = {}  # 进程内线程锁 (双保险: fcntl 锁跨进程, 线程锁管同进程多线程)


def _guard(path: str):
    """获取 线程锁 + 文件锁 双重互斥, 返回 (thread_lock, file_obj)。

    用法: tl, lf = _guard(P); try: ... finally: flock UN; lf.close(); tl.release()
    """
    tl = _tlock.setdefault(path, threading.Lock())
    tl.acquire()
    lf = open(path + ".lock", "w")
    fcntl.flock(lf, fcntl.LOCK_EX)
    return tl, lf


def _with_lock(project, fn):
    """多进程安全的读-改-写: 同一项目同一时刻只允许一个写入者。

    并发 append 时, 后写者会等锁, 不会覆盖先写者的记录。
    线程锁 + 文件锁双保险 (同进程多线程 / 多进程都互斥)。
    """
    os.makedirs(DB, exist_ok=True)
    lk = _path(project) + ".lock"
    tl = _tlock.setdefault(lk, threading.Lock())
    tl.acquire()
    try:
        with open(lk, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        tl.release()


def _load(project: str) -> dict:
    p = _path(project)
    if not os.path.exists(p):
        return {"project": project, "created": None, "sections": {}}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _save(project: str, doc: dict) -> str:
    os.makedirs(DB, exist_ok=True)
    p = _path(project)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)  # 原子写, 防半截文件
    return p


def _backup_content(project: str, doc: dict) -> str | None:
    """覆盖之前, 把**整份档案**另存一份 —— 写前备份(可一键回滚)。

    2026-10-03 加的版本只存**裸 content**(裸 markdown)。2026-10-04 交接时发现这是个
    半成品: 要回滚还得先读懂备份里是什么, 再手动塞回 content 字段。2026-10-05 改成存
    **整份 JSON**, 于是回滚就是一行 `cp`, 不需要理解里面有什么。

    起因仍是一次真实事故: 某个健康检查客户端调 write_memory 写进字符串
    "health-check 不应写入"(17 字), 把 6314 字的项目现状整个覆盖, 而接口照样返回
    "已写入"。那次 log 全部存活(append 是追加式), 只有 content 字段被毁。

    ⚠️ 备份失败**不阻断**正常写入 —— 备份是保险, 不是业务依赖。
    """
    if not doc.get("content"):
        return None  # 原来就没有 content(新建项目), 无可备份
    ts = time.strftime("%Y%m%d-%H%M%S")
    bp = _path(project) + f".bak-{ts}"
    n = 0
    while os.path.exists(bp):  # 同一秒内连写两次, 补后缀防互相覆盖
        n += 1
        bp = _path(project) + f".bak-{ts}-{n}"
    try:
        with open(bp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
    except OSError:
        return None  # 备份失败不该阻断正常写入
    return bp


def _load_tasks() -> dict:
    try:
        if os.path.exists(TASKS):
            with open(TASKS, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"tasks": []}


def _save_tasks(data: dict):
    try:
        os.makedirs(DB, exist_ok=True)
        with open(TASKS + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(TASKS + ".tmp", TASKS)
    except Exception:
        pass


def _task_lock():
    return _guard(TASKS)


READ_FRESH = 30 * 60  # ← 已并入 DEFAULT_POLICY["read_fresh_seconds"], 保留仅为兼容旧引用


def _last_read_time(ctx: Context | None, project: str) -> datetime | None:
    """该客户端最近一次 read_memory 该项目的时间 (从审计日志反查)。

    匹配口径 (2026-10-03 改): **优先按 client_key 匹配**, 回退按展示名。
    为什么: 门禁的作用是"确认这个 agent 自己读过", 而展示名会被
    register_name 改掉 —— 两个同名会话里 A 改名后, B 的旧记录就再也匹配不上,
    门禁对 B 失效 (实测这正是同名顶账的连锁后果之一)。key 是身份, 名是显示。
    旧记录没有 client_key 字段, 回退按名匹配以兼容历史数据。
    """
    try:
        who = _client_name(ctx)
        key = _client_key(ctx)
        with open(AUDIT, encoding="utf-8") as f:
            for ln in reversed(f.readlines()[-500:]):
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("tool") != "read_memory" or r.get("project") != project:
                    continue
                rk = r.get("client_key")
                if rk and key:
                    if rk == key:                      # 新记录: 精确按 key
                        return _parse_time(r.get("time"))
                elif r.get("client") == who:            # 旧记录: 回退按名
                    return _parse_time(r.get("time"))
    except Exception:
        pass
    return None


def _parse_time(t: str | None):
    try:
        return datetime.strptime(t or "", "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _require_read(ctx: Context | None, project: str) -> str | None:
    """硬性检查: 写操作前必须刚读过项目记忆 (30 分钟内), 否则返回拒绝理由。

    特例 (2026-10-03 加): **项目压根不存在时放行**。
    为什么: read_memory 对不存在的项目会返回"❌ 没有…", 而那条"读"不构成
    真正的记忆对齐 —— 但如果不放行, 新项目就永远开不了第一个任务
    (读说不存在 → 不算读过 → 写被拒 → 读还是不存在, 死锁)。
    正确做法: 新项目本来就没有历史可对齐, 不需要先读, 直接开就行。
    """
    if not os.path.exists(_path(project)):
        return None          # 新项目: 没有历史记忆可对齐, 放行
    ro = _write_guard(ctx, "往记忆里写东西")
    if ro:
        return ro
    last = _last_read_time(ctx, project)
    if last is None:
        return (f"❌ 被拒：你还没读过「{project}」的项目记忆。"
                f"硬性要求：先 read_memory(\"{project}\") 再写，禁止凭自身记忆执行。")
    fresh = float(_policy()["read_fresh_seconds"])
    if (datetime.now() - last).total_seconds() > fresh:
        return (f"❌ 被拒：你上次读「{project}」是 {last:%H:%M:%S}，记忆可能已过期。"
                f"硬性要求：重新 read_memory(\"{project}\") 再写。")
    return None


@mcp.tool()
def list_roles(context: Context) -> str:
    """**开工第一步**: 看有哪些角色可选, 然后认领一个。读写都靠它。

    主人的用法是"没有固定的客户端, 我让他做什么, **他自己评估接入哪个身份**完成",
    所以身份是**干活时选的, 不是登录时定的**。这个工具就是让他做这个选择。

    怎么选: 按**要做的事**选, 不按你跑在哪个客户端上。同一个客户端今天干引擎、
    明天干评估, 都是它自己 claim 出来的 —— 名册里每个角色只有一个身份, 用谁接谁。
    """
    ros = _roster()
    mine = _claimed_role(context) or _header_key(context)[0]
    lines = []
    for k, v in ros.items():
        mark = "✅ 本会话已认领" if k == mine else ("（自检专用，别当工作身份）" if v.get("test") else "")
        lines.append(f"  {k:<16} {v.get('name',''):<8} [{v.get('platform','')}] {v.get('note','')} {mark}")
    roster_block = "\n".join(lines) if lines else "  （名册为空）"
    empty_tip = ("\n名册还是空的: 这是开箱状态, 直接 claim_role(\"role-<英文语义词>\") "
                 "就会自动登记第一个角色。" if not ros else "")
    return (
        "规范身份名册 —— 挑一个, 然后 claim_role(那个 key):\n"
        + roster_block + empty_tip
        + "\n\n怎么挑: 按**要做的事**选, 不按你跑在哪个客户端。同一个角色只有一个身份, 用谁接谁。\n"
          "strict_roster=False(开箱默认)时册外语义角色会在首次 claim 时自动登记; "
          "设为 true 后 register_name 只认名册内身份。\n"
          "选错了不要紧: 再 claim 一次就换过去了(但换角色要重读记忆, 门禁会拦你)。\n"
          f"本会话当前: {mine or '❌ 还没认领 —— 读随便读, 写会被拒'}")


@mcp.tool()
def claim_role(role: str, context: Context) -> str:
    """认领一个角色, 从现在起本会话的写操作都记在这个角色名下。

    什么时候调: **开工时**, 读完记忆、评估完"这事该谁干"之后, 动手写之前。
    读取不需要认领(你得先读到名册才能决定认领谁), 但 start_task / write_memory /
    append_memory 都需要 —— 这是"身份是干活时选的"这条规则的强制点。

    换角色: 直接再调一次。**换了角色要重读记忆** —— 不同角色有不同地盘和禁区,
    带着上一个角色的判断去干下一个角色的活是本项目反复踩过的坑。
    """
    key = role.strip()
    ros = _roster()
    base = key.split("#", 1)[0]
    auto_created = False
    if base not in ros:
        strict = _policy().get("strict_roster", False)
        # 开箱模式(strict_roster=False): 允许认领并自动登记第一个 role-<语义名> 角色。
        # 要求纯 ASCII、role- 前缀、名字非空 —— 角色表本就该由干活的人自建。
        if (not strict and base.isascii() and base.startswith("role-")
                and len(base) > len("role-") and base.replace("-", "").isalnum()):
            entry = {"name": base[len("role-"):], "platform": "self-claim",
                     "note": "首次 claim_role 自动登记, 请在 _roster.json 补 note(管哪块地)"}
            if _save_roster_entry(base, entry):
                auto_created = True
                ros = _roster()
        if base not in ros:
            lines = "\n".join(f"  {k:<16} {v.get('name',''):<8} {v.get('note','')}" for k, v in ros.items())
            tip = ("(名册是空的: 直接 claim_role(\"role-<语义名>\") 即可自动登记一个)"
                   if not ros else "")
            return (f"❌ 名册里没有 `{key}`。可选的是这些:\n{lines}{tip}\n"
                    f"要加新角色: 直接 claim 一个 role-<英文语义词>(开箱模式自动登记), "
                    f"或在数据目录的 _roster.json 里手工加一条。")
    if ros[base].get("test") and base != _claimed_role(context):
        return (f"⚠️ `{key}` 是自检专用槽位(审查脚本在用), 别当工作身份认领。\n"
                f"   真要自检就让维护者调整脚本或另开一个非 test 角色。")
    old = _claimed_role(context) or _header_key(context)[0]
    session = getattr(context, "session", None)
    if session is not None:
        try:
            _SESSION_ROLE[session] = key
        except Exception:
            pass
    v = ros[base]
    nm = _load_clients().get(key) or v.get("name", "")
    f = _clients_lock()
    tl = f[0]
    try:
        reg = _load_clients()
        if not reg.get(key):          # 没人注册过就自动写上角色名, 省一次 register_name
            reg[key] = v.get("name", "")
            _save_clients(reg)
    finally:
        fcntl.flock(f[1], fcntl.LOCK_UN)
        f[1].close()
        tl.release()
    _audit(context, "claim_role", "*", f"{key} ({v.get('name','')})")
    switch = ""
    if old and old != key:
        switch = (f"\n⚠️ 你之前是 `{old}`, 现在换成 `{key}` —— "
                  f"**必须重读记忆**: 不同角色地盘和禁区不同, 带着上一个角色的判断干活是本项目最贵的错之一。")
    created_note = ""
    if auto_created:
        created_note = (f"\n🆕 角色 `{key}` 是本次首次认领、已自动登记进 _roster.json"
                        f"(开箱模式 strict_roster=False)。名字和 note 是占位的, "
                        f"请补清它管哪块地; 角色表稳定后可在 _policy.json 里开 strict_roster 收紧。")
    return (f"✅ 已认领: {v.get('name','')} (key={key})\n"
            f"   平台: {v.get('platform','')} | 职责: {v.get('note','')}\n"
            f"   本会话的写操作都记在这个角色名下; 换会话要重新认领。{switch}{created_note}")


@mcp.tool()
def release_role(context: Context) -> str:
    """放掉当前认领的角色(交给别人, 或准备换个身份)。

    放掉之后本会话回到"只读"状态: 读随便读, 写会被拒, 直到再次 claim_role。
    """
    old = _claimed_role(context)
    if not old:
        return "本会话没有认领任何角色(可能配的是固定头, 那不受影响)。"
    session = getattr(context, "session", None)
    if session is not None:
        try:
            _SESSION_ROLE.pop(session, None)
        except Exception:
            pass
    _audit(context, "release_role", "*", old)
    return f"✅ 已放掉 `{old}`。本会话现在只读; 要继续写就再 claim_role。"


@mcp.tool()
def whoami(context: Context) -> str:
    """我服务端眼里的你是谁 + 你的身份稳不稳。**统一身份体系的诊断入口。**

    开工第一件事调它, 拿到的东西直接告诉你三件事:
      1. 服务端现在认出的 key 是什么(以及是从哪条通道认出来的);
      2. 这个 key **稳不稳** —— 稳=跨重连跨重启都不会变; 不稳=下次重启就换人;
      3. 粘哪一行配置就能变稳(以及你名下有几条 running 任务、会不会撞别人)。

    为什么需要它: 身份不稳这件事**从外面完全看不出来**。任务被拒、门禁失效、
    记到别人账上, 症状都发生在别处, 根因却在"服务端没认出你是谁"。
    有了这个工具, 不用猜 —— 调一下就知道。
    """
    key = _client_key(context)
    src = "HTTP 头 X-Agent-Key(稳定)"
    if not _header_key(context)[0]:
        try:
            meta = context.request_context.meta
            extra = getattr(meta, "model_extra", None) or {}
            if extra.get("agent_key"):
                src = "_meta.agent_key(稳定)"
            elif extra.get("agent"):
                src = "_meta.agent(半稳: 依赖客户端每次都发)"
            else:
                src = "⚠️ 回落通道"
        except Exception:
            src = "⚠️ 回落通道"
    if src.startswith("⚠️"):
        src = ("⚠️ 回落通道 —— 身份从**活着的连接对象**推出来的, "
               "服务端一重启所有同平台会话就会塌回同一个 key")

    reg = _load_clients()
    mine = [t for t in _load_tasks().get("tasks", [])
            if t.get("status") == "running" and _own_task(t, context)]
    name = reg.get(key, "")
    stable = _header_key(context)[0] != "" or "agent_key" in src

    ros = _roster()
    in_ros = key.split("#", 1)[0] in ros
    legacy = key in LEGACY_KEYS
    if in_ros:
        ros_line = f"✅ 在规范名册里: {key} =「{ros.get(key.split('#',1)[0], {}).get('name', '?')}」"
    elif legacy:
        ros_line = (f"⚠️ 旧身份(迁移期放行): `{key}` 该迁到 "
                    f"`{LEGACY_KEYS[key]}`=「{ros.get(LEGACY_KEYS[key], {}).get('name', '?')}」")
    else:
        ros_line = f"❌ 不在规范名册: `{key}` —— register_name 会拒绝它"

    # 给你一段能直接粘的 ASCII key(头不允许中文, 这点实测踩过)
    import uuid as _uuid
    if in_ros:
        suggest = key
    elif legacy:
        suggest = LEGACY_KEYS[key]
    else:
        suggest = "role-" + _uuid.uuid4().hex[:6]
    role_names = ", ".join(f"{k}({v.get('name','')})" for k, v in ros.items())

    return (
        f"服务端认出的 key: {key or '(空 —— 拿不到身份, 写操作会被拒)'}\n"
        f"名册里的名字:     {name or '(还没 register_name, 或名册里没有你)'}\n"
        f"身份来源:         {src}\n"
        f"稳不稳:           {'✅ 稳 —— 跨重连跨重启都不变' if stable else '❌ 不稳 —— 重启就可能换人/撞别人'}\n"
        f"规范名册:         {ros_line}\n"
        f"你名下 running:   {len(mine)} 条{'  ← 0 条时若你之前开过任务, 说明掉到别人账上了' if not mine else ''}\n"
        f"只读状态:         {'🔒 是(写工具会被拒)' if _is_readonly(context) else '否'}\n"
        f"\n"
        f"想让身份永久稳定, 在 MCP 配置里加这个头(**必须纯 ASCII, 不能有中文**):\n"
        f'    "headers": {{"X-Agent-Key": "{suggest}"}}\n'
        f"中文显示名照旧用 register_name(\"如 {ros.get(suggest, {}).get('name', '某某')}\") —— "
        f"那是 JSON 请求体, 中文没问题。分工是: ASCII key 管身份, 中文名管显示。\n"
        f"规范名册(主人在用这些角色, **不要新建**): {role_names}\n"
        f"加完调一次 whoami, 上面「身份来源」应该变成「HTTP 头」。")


@mcp.tool()
def start_task(context: Context, project: str, task_name: str, description: str = "",
               request_id: str = "") -> str:
    """开始一个任务: 声明你在为哪个项目做什么事。

    监控页任务看板会显示: 谁 / 在哪个项目 / 做什么任务 / 开始时间 / 进度。
    干完调 finish_task 收尾; 中途可 update_task 报进度。

    ⚠️ 硬门禁 (2026-10-03 收紧): **30 分钟内没 read_memory 过就拒绝开任务**。
    原来只是任务建好之后附一句"⚠️ 你还没读过" —— 那是**提醒不是拦截**, 照样
    放行, 于是"开工先读"这条纪律实际上没人遵守 (实测: 多个 agent 只写不读)。
    收紧的理由: read_memory 是 agent 看到"别人改了什么"的唯一入口, 不强制
    就等于让 agent 在信息过期的情况下开工。拒绝比提醒有效得多。

    ── request_id: 幂等键 (2026-10-05 加) ──
    同一个 request_id 重复调用**不会开出第二个任务**, 直接返回原来那个 id。
    借鉴 Pi Durable: *"A requestId makes a submission exactly-once, so a client
    that retries after a crash gets the original submission back instead of
    asking twice."*
    为什么这里需要: 客户端重连后最自然的反应就是把刚才那次调用**再发一遍**。
    没有幂等键, 重发就多一条任务 —— 实测里的孤儿任务和压测残留, 都是重发出来的。
    建议: 传一个你自己生成的稳定串(同一次任务重试时**保持同一个**)。
    """
    block = _require_read(context, project)
    if block:
        return (block + "\n(改用 start_task 前先 read_memory —— 记忆是这个项目"
                "唯一的共享事实源, 不读就开工等于凭自身记忆干, 这是最贵的错。)")
    # 只读闸在这里**必须再查一遍**: _require_read 对"项目压根不存在"会提前放行
    # (新项目免读, 防死锁), 只读客户端正好会从那个缺口溜进来开任务。
    ro = _write_guard(context, "开任务")
    if ro:
        return ro
    rid = request_id.strip()[:64]
    f = _task_lock()
    tl = f[0]
    tid = dup = None
    try:
        data = _load_tasks()
        if rid:
            for t in data["tasks"]:
                if t.get("request_id") == rid and t.get("client_key") == _client_key(context):
                    dup = t["id"]      # 幂等命中: 返回原任务, 不新建
                    break
        if dup is None:
            tid = "tk" + time.strftime("%y%m%d%H%M%S") + "_" + str(len(data["tasks"]) + 1)
            data["tasks"].append({
                "id": tid,
                "project": project,
                "name": task_name.strip()[:60],
                "description": description.strip()[:200],
                "client": _client_name(context),
                "client_key": _client_key(context),   # 2026-10-03 加: 归属按 key 判, 不按名
                "status": "running",
                "start": time.strftime("%Y-%m-%d %H:%M:%S"),
                "progress": "",
                "finish": None,
                "result": "",
                "request_id": rid,                    # 2026-10-05 加: 幂等键
            })
            _save_tasks(data)
    finally:
        fcntl.flock(f[1], fcntl.LOCK_UN)
        f[1].close()
        tl.release()
    _audit(context, "start_task", project,
           f"{task_name.strip()[:40]}" + (f" 幂等命中 {dup}" if dup else ""))
    if dup:
        return f"任务已存在(幂等, 未新建): {dup}"
    return f"任务已开始: {tid}"


def _own_task(t: dict, ctx: Context | None) -> bool:
    """这条任务是不是当前客户端开的 (2026-10-03: 改按 key 判定)。

    原来按展示名比 → 同平台多会话 (同名) 时**互相能改、能关对方的任务**。
    key 是身份; 老任务没有 client_key 字段则回退按名比, 兼容历史数据。
    """
    tk, ck = t.get("client_key"), _client_key(ctx)
    if tk and ck:
        return tk == ck
    return t.get("client") == _client_name(ctx)


def _adoptable(t: dict, ctx: Context | None, data: dict) -> bool:
    """这条 running 任务能不能被当前客户端**认领**回来(重连后找回自己的任务)。

    起因 (2026-10-03 真实事故): 身份 key 是从**活着的会话对象** derive 的
    (`_SESSION_KEYS` 是 WeakKeyDictionary, 客户端一重连就失效), 重连后 key 退化成
    `某客户端-runtime#N`, 而 N 还可能和上次不同 → 自己的任务再也
    `_own_task` 不了 → 任务变孤儿(压测时多次复现)。

    Pi Durable 的解法是"状态里只存名字"+ requestId 幂等, 前提是客户端持有稳定
    身份。这里做等价物: 三个条件**同时**成立才允许认领, 缺一不可 ——

      ① 同平台: 旧 key 与当前 key 的 `#` 之前部分相同;
      ② 旧 key 现在**没有注册名字**, 或注册的名字就是当前客户端的名字
         (挡掉 A 会话去动 B 会话的任务: 同平台不同角色, 注册名不同);
      ③ 当前客户端名下**一条 running 任务都没有** —— 最强的一条判据:
         手里空着的客户端不可能是在偷别人的任务, 它只可能是丢了自己的。

    走 policy 的 `allow_task_adoption` 开关; 认领会改写任务的 client_key 并在
    审计里留痕(`adopted_from` 字段也留在任务上, 事后查得到)。
    """
    old = t.get("client_key") or ""
    cur = _client_key(ctx) or ""
    if not old or not cur:
        return False
    if old.split("#", 1)[0] != cur.split("#", 1)[0]:
        return False
    if t.get("status") != "running":
        return False
    reg = _load_clients()
    old_name = reg.get(old)
    if old_name and old_name != _client_name(ctx):
        return False
    mine = sum(1 for x in data.get("tasks", [])
               if x.get("status") == "running" and _own_task(x, ctx))
    return mine == 0


def _try_adopt(t: dict, ctx: Context | None, data: dict) -> str:
    """尝试认领。成功返回一句给客户端看的话, 失败返回空串。"""
    if not _policy().get("allow_task_adoption", True):
        return ""
    if not _adoptable(t, ctx, data):
        return ""
    old_key = t.get("client_key") or "?"
    old_name = t.get("client") or "?"
    t["client_key"] = _client_key(ctx)
    t["client"] = _client_name(ctx)
    t["adopted_from"] = old_key
    _audit(ctx, "adopt_task", t.get("project", "*"),
           f"{t.get('id')} 从 {old_key}/{old_name} 认领")
    return (f"(已把 {t.get('id')} 从旧身份 {old_key} 认领回来 —— 你重连后换了身份 key, "
            f"这条是你自己的任务, 不是别人的。)")


@mcp.tool()
def update_task(context: Context, task_id: str, progress: str) -> str:
    """更新任务进度 (如 "调色中 50%")。只允许改自己开的任务。

    ⚠️ 如果这条任务其实是你自己开的、只是重连后身份 key 变了, 服务端会**自动
    认领**它并告诉你(见 _adoptable 的三个条件), 不会硬拦。
    """
    f = _task_lock()
    tl = f[0]
    try:
        data = _load_tasks()
        for t in data["tasks"]:
            if t["id"] == task_id:
                note = ""
                if not _own_task(t, context):
                    note = _try_adopt(t, context, data)
                    if not note:
                        return f"不是你的任务 ({t['client']} 开的), 改不了"
                t["progress"] = progress.strip()[:100]
                _save_tasks(data)
                return f"进度已更新: {progress.strip()[:100]}" + (f"\n{note}" if note else "")
        return f"找不到任务 {task_id}"
    finally:
        fcntl.flock(f[1], fcntl.LOCK_UN)
        f[1].close()
        tl.release()


@mcp.tool()
def finish_task(context: Context, task_id: str, result: str = "", request_id: str = "") -> str:
    """完成任务, 可选填结果摘要 (如 "交付 120 张调色片")。

    幂等: 已经 done 的任务再关一次, 不会报错也不会覆盖已有的 result —— 返回
    "已完成(幂等)"。客户端重连后重发是常态, 不该因此拿到一句报错。
    """
    rid = request_id.strip()[:64]
    f = _task_lock()
    tl = f[0]
    try:
        data = _load_tasks()
        target = None
        note = ""
        for t in data["tasks"]:
            if t["id"] == task_id:
                if not _own_task(t, context):
                    note = _try_adopt(t, context, data)
                    if not note:
                        return f"不是你的任务 ({t['client']} 开的), 关不了"
                if t.get("status") == "done":
                    return (f"任务已完成(幂等, 未重复关闭): {t['name']}"
                            + (f"\n{note}" if note else ""))
                t["status"] = "done"
                t["finish"] = time.strftime("%Y-%m-%d %H:%M:%S")
                t["result"] = result.strip()[:200]
                if rid:
                    t["request_id_finish"] = rid
                target = t
                break
        if target is None:
            return f"找不到任务 {task_id}"
        # 完成的任务只留最近 20 条, 防止看板无限堆积 (循环外统一裁剪)
        done = [t for t in data["tasks"] if t["status"] == "done"]
        if len(done) > 20:
            for t in done[:-20]:
                if t["id"] != task_id:
                    data["tasks"].remove(t)
        _save_tasks(data)
        return f"任务完成: {target['name']}" + (f"\n{note}" if note else "")
    finally:
        fcntl.flock(f[1], fcntl.LOCK_UN)
        f[1].close()
        tl.release()


@mcp.tool()
def register_name(name: str, context: Context, force: bool = False,
                  readonly: bool = False) -> str:
    """给自己命名: 开工第一件事调它, 之后你在监控页上的所有操作都显示这个名字。

    例: register_name("CLI-构建") / register_name("桌面端-测试")。
    建议格式: 平台-角色 (如 IDE-后端维护 / 浏览器-验收)。
    如果客户端本身带名字 (MCP 客户端通常会自动报 clientInfo.name), 不用调也能显示。

    ⚠️ 同名保护 (2026-10-03 加): 身份 key 来自 MCP initialize 自报的客户端名,
    **同一平台的多个会话会撞同一个 key** (实测: 多个同平台会话都报
    同一个 clientInfo 名)。若该 key 已被别人注册成别的名字, 本工具**不再
    静默覆盖**, 而是返回冲突说明; 确认要接管再加 force=True。
    事故记录: 曾出现 "同平台-A" 被静默覆盖成 "同平台-B", 导致
    另一个会话的动作被记到错的账上。

    ── readonly=True (2026-10-05 加) ──
    把自己标成**只读客户端**: 仍可 read_memory / list_projects / 探活, 但所有
    写工具一律拒绝。给健康检查、压测、监控脚本这类**非生产客户端**用。
    起因: 2026-10-03 19:42 一个自称「健康检查-临时」的客户端持有完整写权限,
    先 read 合法过了门禁, 再用 17 字覆盖掉 6314 字的项目现状。探活脚本不该有
    能力重写"唯一事实源"。再传一次 readonly=True 可取消只读(名单存 _readonly.json,
    热改, 不必重启服务)。

    💡 想彻底分开同平台多会话: 让客户端在每个请求的 _meta 里带
    agent="你的名字" + session="会话标识", 服务端会据此
    生成唯一 key (2026-10-03 新增能力, 需客户端配合)。
    💡 更彻底的做法(2026-10-05): 带一个**客户端自己生成并本地持久化**的
    agent_key, 服务端原样当身份用 —— 跨重连、跨服务重启都不变, 于是
    任务不会再因为换 key 而变成孤儿。
    """
    key = _client_key(context)
    bad = _roster_check(key, context)   # ⚠️ 必须传 context: 不传就分不出
                                         # "没配头"(该去认领) 和
                                         # "配错了头"(该改配置) 两种情况
    if bad:
        return bad
    want = name.strip()[:30]
    if not want:
        return "警告: 名字不能为空"
    f = _clients_lock()
    tl = f[0]
    try:
        reg = _load_clients()
        taken_by = reg.get(key)
        switched = ""
        if taken_by and taken_by != want and not force:
            # 撞 key 了。**不静默覆盖**(防误伤先注册者), 也不直接拒绝(那会锁死
            # 同平台后来者 —— 实测三个同平台会话都报同一个 clientInfo,
            # 第一个注册后, 后两个无论如何都注册不上, 包括换个名字也不行)。
            # 折中: 给本会话分配独立 key `key#2`, 各记各的账, 谁也不覆盖谁。
            key = _assign_key(context, key)
            reg = _load_clients()
            switched = (f" (原 key 已被 {taken_by!r} 占用, 已为你自动分配独立 key "
                        f"{key!r}, 不影响先注册者)")
        reg[key] = want
        _save_clients(reg)
    finally:
        fcntl.flock(f[1], fcntl.LOCK_UN)
        f[1].close()
        tl.release()
    ro_note = ""
    if readonly:                      # 切换只读状态(传一次开, 再传一次关)
        names = _load_readonly()
        if key in names:
            names = [n for n in names if n != key]
            _save_readonly(names)
            ro_note = "\n🔓 已取消只读: 写工具恢复可用。"
        else:
            names.append(key)
            _save_readonly(names)
            ro_note = ("\n🔒 已标为只读客户端: 写工具一律拒绝(读/探活照常)。"
                       "再传一次 readonly=True 可取消。")
    # 迁移提示: 老 key 仍放行, 但每次都告诉它该换成哪个角色
    if key in LEGACY_KEYS:
        ros = _roster()
        tgt = LEGACY_KEYS[key]
        ro_note += (f"\n⚠️ 你还在用旧身份 `{key}`。该迁到 `{tgt}`"
                    f"({ros.get(tgt, {}).get('name', '?')}) —— 在 MCP 配置里加 "
                    f"\"headers\": {{\"X-Agent-Key\": \"{tgt}\"}}, 重连后身份就固定了。")
    _audit(context, "register_name", "*", f"我是 {want} (key={key})" + (" 只读切换" if readonly else ""))
    return f"已注册: {want} (key={key}){switched}{ro_note}"


@mcp.tool()
def list_projects(context: Context) -> str:
    """列出所有已有记忆档案的项目名。返回一个 JSON 数组字符串。

    ⚠️ 为什么返回字符串而不是 list (2026-10-03 修的**真 bug**):
    原来返回 `list`, 但 FastMCP 把 list 的每个**字符串**元素当成一个独立的
    text content block 输出 —— MCP 协议的 content 是数组, 客户端拿到的是
    `["_clients"]` 这样的拼接文本, 真实的项目名被丢掉了。实测症状: 调用方
    只看见 "_clients" 一行, 于是判断"没有建立任何项目记忆"(而实际上
    数据目录里某个 <项目>.json 已经有几十条记录好好地在那儿)。
    根因: _clients.json / _tasks.json 是内部台账(以下划线开头), 不该出现在
    项目列表里, 一起被扫出来更让人以为那就是全部。

    修法两处: ① 过滤下划线开头的内部文件 ② 显式 json.dumps 成一个字符串,
    保证客户端拿到完整数组。
    """
    _audit(context, "list_projects", "*")
    if not os.path.isdir(DB):
        return "[]"
    names = sorted(f[:-5] for f in os.listdir(DB)
                   if f.endswith(".json") and not f.startswith("_"))
    return json.dumps(names, ensure_ascii=False)


@mcp.tool()
def read_memory(project: str, search: str = "", since: str = "", since_seq: int = -1,
                context: Context = None) -> str:
    """读某项目的完整记忆档案 (含状态/最近进展/产物/下一步/关键决策)。

    开工前先读: 让智能体对齐项目当前事实, 不靠猜。

    ⚠️ 2026-10-03 加: 项目不存在时**明说"没有"**, 不再返回一个空壳档案。
    原来任何名字都读得出 `{"project": "...", "log": []}` —— 于是 agent
    会把"项目是空的"和"项目不存在"混为一谈, 凭空的空壳往下写。
    现在返回明确的错误说明并附上现有项目清单, 让人和 agent 都能立刻纠正。

    ⚠️ 2026-10-03 加: **过期预警**。读到不新鲜的状态档时, 返回内容顶部会顶一条
    警告(数据原样不改, 只多一段前缀)。起因: 旧档 updated=16:45, 而它之后的
    2.5 小时里发生了 10 条新进展 + 6 个 git 提交, 期间开工的 agent 读到的
    测试数 / HEAD / 人脸检测状态**全是错的**, 而接口一声不响。
    → "知道最新项目状态"不能靠自觉, 必须让 agent 在被骗之前先知道自己可能被骗。

    ── 四个可选参数, 都是为了少烧上下文 ──
    search: 关键词, 空格分隔, **任一命中即返回**(OR 语义, 同官方 search_nodes)。
             只回命中的 log 条目 + 状态档命中行, 不回全量。
             "log 攒到几千条后, 不带 search 读一次就是几万 token" 是真问题。
    since : "YYYY-MM-DD HH:MM" 或 "YYYY-MM-DD"。只要该时间之后的 log, 常用于
             "我上次开工之后又发生了什么"。
    since_seq: 只回 **seq 大于这个数**的 log 条目, 并在开头告诉你当前最大 seq。
             默认 -1 = 不启用。**since_seq=0 是合法值**(等于"从头给我"), 所以这里
             必须显式判 >= 0, 不能用 `if since_seq:` —— 0 是 falsy, 那样写会让
             "从头开始"这个最常用的游标静默退化成全量返回(2026-10-05 被自己的
             测试当场抓出来)。
             这是最省 token 的"增量读": 第一次读记住返回里的 max_seq, 下次直接
             `read_memory(项目, since_seq=<你记住的>)`, 只拿这中间新写的。
             借鉴 Pi Durable 的"客户端先拿当前视图, 之后只收变化"。
             (seq 由 append_memory 递增写入; 2026-10-05 之前的老条目没有 seq 字段,
              按"下标+1"计算 —— log 只追加不重排, 两者始终一致。)
    都不给 = 返回全量(向后兼容), 但超过 LARGE_PAYLOAD 字数会在末尾提示怎么收窄。
    """
    _audit(context, "read_memory", project,
           f"search={search!r} since={since!r} since_seq={since_seq!r}"
           if (search or since or since_seq) else "")
    if not os.path.exists(_path(project)):
        have = sorted(f[:-5] for f in os.listdir(DB)
                      if f.endswith(".json") and not f.startswith("_")) if os.path.isdir(DB) else []
        return ("❌ 没有「%s」这个项目的记忆档案（文件不存在）。"
                "现有项目: %s" % (project, (", ".join(have) if have else "（一个都没有）")))
    doc = _load(project)
    warn = _staleness_warning(doc)

    if since_seq >= 0:   # 增量模式: 只回 seq 更大的, 并报当前游标位置
        log = doc.get("log") or []
        keep = []
        for i, e in enumerate(log):
            if int(e.get("seq") or (i + 1)) > int(since_seq):
                keep.append(e)
        maxseq = max((int(e.get("seq") or (i + 1)) for i, e in enumerate(log)), default=0)
        head = (f"── 增量: seq > {since_seq} ── 命中 {len(keep)} 条 / 共 {len(log)} 条"
                f" ｜ 当前 max_seq={maxseq}(下次读把它当 since_seq 传回来)")
        if not keep:
            return (warn + "\n" if warn else "") + head + "\n（这之后没有新进展）"
        body = "\n\n".join("── %s [seq %s] ──\n%s"
                           % (e.get("time", "?"), e.get("seq", "?"), e.get("note", ""))
                           for e in keep)
        return (warn + "\n" if warn else "") + head + "\n\n" + body

    if search or since:   # 检索模式: 只回命中的部分(文本形态, 本来就不是 JSON)
        return (warn + "\n" if warn else "") + _search_memory(doc, project, search, since)

    return _full_doc_response(doc, project, warn)


def _full_doc_response(doc: dict, project: str, warn: str) -> str:
    """默认"读整份档案"路径。**保证返回值是合法 JSON** —— 脚本要能 json.loads。

    这里修掉的是同一个契约问题的两半(2026-10-05):
      · 尾巴那半: 超阈值时在 JSON **后面**追加提示 → `Extra data`。
      · 开头这半: 过期/疑似覆盖警告以裸文本顶在最前 → `Expecting value: char 0`。
    某个 <项目>.json 涨到上百 KB、审计项目没写 updated 之后,
    自检脚本两次分别栽在这两半上。教训: **返回"看起来像 JSON 的东西"比返回明确的文本更危险**
    —— 调用方一定会去 parse 它, 于是错在远处、报错在近处, 极难定位。

    做法: 警告收进 `_warnings` 字段, 且**放在字典第一位** —— json.dumps 保序,
    所以人读的时候它仍然是第一眼看到的(那个 🚨🚨 覆盖预警不许被埋掉);
    超阈值时略去 log(它占 95%+ 体积), 顶层 content/log 键名保持不变。
    """
    out = dict(doc)
    payload = json.dumps(out, ensure_ascii=False, indent=2)
    if len(payload) > LARGE_PAYLOAD:
        log = out.pop("log", []) or []
        out["log"] = []                       # 键留着, 老的 .get("log", []) 读法不受影响
        out["_log_omitted"] = len(log)
        out["_hint"] = (
            f"这份档案的 log 有 {len(log)} 条, 已全部略去(完整返回会占 %d 字符)。"
            f"要其中的内容请用 search= 关键词 / since= 时间 / since_seq= 游标 取, "
            f"别去掉参数整个读。" % len(payload))
    if warn:
        merged = {"_warnings": [warn.strip()]}
        merged.update(out)
        out = merged
    return json.dumps(out, ensure_ascii=False, indent=2)


LARGE_PAYLOAD = 20000   # 全量响应超过这个字数, 就提示怎么收窄


def _search_memory(doc: dict, project: str, search: str, since: str) -> str:
    """按关键词 / 时间检索, 返回命中的 log 条目与状态档命中行。OR 语义。"""
    content = doc.get("content") or ""
    keys = [k for k in search.split() if k]
    log = doc.get("log") or []

    if since:
        cut = since if len(since) > 10 else since + " 00:00"
        log = [e for e in log if e.get("time", "") >= cut]

    def hit(t: str) -> bool:
        t = t.lower()
        return any(k.lower() in t for k in keys)

    matched = [e for e in log if (not keys or hit(e.get("note", "")))]
    head = "🔎 检索「%s」%s —— 命中 %d / %d 条 log" % (
        search, f"（{cut} 之后）" if since else "", len(matched), len(log))
    out = [head]
    if not matched:
        out.append("（无命中）")
    for e in matched:
        out.append("\n── %s ──\n%s" % (e.get("time", "?"), e.get("note", "")))
    if keys:
        cl = [ln for ln in content.splitlines() if hit(ln)]
        out.append("\n━━ 状态档命中行（%d）━━" % len(cl))
        out.extend("  " + ln for ln in cl) if cl else out.append("  （无）")
    out.append("\n（以上为检索结果。未命中部分不在此返回 —— 需要全量请去掉 search/since 参数。）")
    return "\n".join(out)


STALE_MINUTES = 30  # ← 已并入 DEFAULT_POLICY["stale_minutes"], 保留仅为兼容旧引用


def _staleness_warning(doc: dict) -> str:
    """判断状态档是否已落后于 log。返回警告文本, 没异常返回空串。

    ⚠️ 两个检测是【并列】的, 不能嵌套 —— 这条是负控跑出来的真 bug:
    最初把"content 异常短"写在了 staleness 判断的 if 里面, 结果一个
    updated 只落后 1 分钟、但刚被 17 字垃圾覆盖的档, 因为"还够新鲜"
    而**完全逃过检测**。可被覆盖的档恰恰是最新写进去的, 时间一定很新。
    → 覆盖嫌疑与过期是两件独立的事, 谁也不能当谁的前置条件。

    检测一(过期): 最新一条 log 减去 状态档 updated。间隔 > STALE_MINUTES 就报,
                  并说清落后多久 / 期间攒了多少条, 让 agent 自己判断要不要翻 log。
    检测二(覆盖): content 异常短(< 50 字)而 log 已有 >= 3 条 —— 典型的
                  "被一次误 write_memory 覆盖"特征(见 2026-10-03 19:42 事故)。
    """
    log = doc.get("log") or []
    if not log:
        return ""

    # ── 检测二: 疑似被覆盖(独立判断, 不依赖 updated) ──
    w = ""
    content = doc.get("content") or ""
    if len(content) < 50 and len(log) >= 3:
        w += ("🚨🚨 **状态档疑似被一次误调用覆盖** 🚨🚨\n"
              "  content 只有 %d 字, 而 log 已有 %d 条 —— 这不可能是正常维护出来的。\n"
              "  → **别信下面的现状**, 它不是项目现状, 是一次误写入的残留。\n"
              "  → 同目录下的 %s.json.bak-* 很可能有被覆盖前的完整版本, 建议回滚。\n"
              % (len(content), len(log), doc.get("project", "项目")))

    # ── 检测一: 过期 ──
    upd = doc.get("updated")
    if not upd:
        return w + ("\n⚠️ 状态档没有 updated 时间戳, 无法判断新鲜度 —— "
                    "以下内容请当作**不可信**。")
    try:
        t_upd = datetime.strptime(upd, "%Y-%m-%d %H:%M:%S")
        t_new = datetime.strptime(log[-1].get("time", ""), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return w
    gap = (t_new - t_upd).total_seconds() / 60
    if gap > float(_policy()["stale_minutes"]):
        newer = sum(1 for e in log if e.get("time", "") > upd)
        w += ("\n" if w else "") + (
            "⚠️⚠️ **状态档已过期, 不要直接采信** ⚠️⚠️\n"
            f"  状态档最后更新: {upd}\n"
            f"  最新进展时间:   {log[-1].get('time', '?')}\n"
            f"  已落后: {int(gap)} 分钟, 期间新增 {newer} 条进展。\n"
            "  → 下面的「现状」可能已经不准(测试数 / 提交号 / 修复状态都可能过时)。\n"
            "  → **先翻本档 log 里这 %d 条新进展**, 或直接查代码与 git 实况, 再动手。" % newer)
    return w


@mcp.tool()
def write_memory(project: str, content: str, context: Context,
                 confirm_overwrite: bool = False) -> str:
    """覆盖写入某项目的记忆档案。

    content 建议为 Markdown: 至少包含 当前状态 / 最近进展 / 产物路径 /
    下一步 / 关键决策 五段。干完活记得更新, 共享记忆靠写入纪律。
    多客户端并发写安全 (文件锁)。
    ⚠️ 硬性要求: 必须先 read_memory 过该项目 (30 分钟内), 禁止凭自身记忆覆盖。

    ⚠️ 意图门禁 (2026-10-05 加, 补 30 分钟门禁的一个真实漏洞):
    那道门禁只管"有没有读过", **管不了"读过之后要干什么"**。2026-10-03 19:42
    那个健康检查客户端**确实先 read 了**, 合法过闸, 然后 write_memory 用 17 字
    覆盖掉 6314 字的项目现状 —— 门禁是合法的, 行为是灾难性的。
    所以补第二道闸: 新内容比旧内容短到 policy 的比例以下(默认 20%)直接拒绝,
    确实要覆盖就显式加 confirm_overwrite=True。阈值在 _policy.json 里, 热改。
    """
    block = _require_read(context, project)
    if block:
        return block
    ro = _write_guard(context, "覆盖写入记忆")
    if ro:
        return ro
    # ── 意图门禁: 拦住"误覆盖" ──
    old_len = 0
    try:
        old_len = len(_load(project).get("content") or "")
    except Exception:
        pass
    ratio = float(_policy()["overwrite_shrink_ratio"])
    if old_len >= 200 and not confirm_overwrite and len(content) < old_len * ratio:
        keep_pct = int(len(content) / old_len * 100)
        return (f"❌ 被拒：这次覆盖会把状态档从 {old_len} 字砍到 {len(content)} 字"
                f"(只剩 {keep_pct}%), 低于 {ratio:.0%} 红线。\n"
                f"   真实前例: 2026-10-03 19:42 有客户端先 read_memory 合法过了 30 分钟"
                f"门禁, 然后用 17 字覆盖掉 6314 字的项目现状, 接口照样返回\"已写入\"。\n"
                f"   · 确实要覆盖(状态档本来就写空了、要重来)→ 加 confirm_overwrite=True 重发。\n"
                f"   · 只是要记一条进展 → 用 append_memory, 它不碰状态档。\n"
                f"   · 阈值可调: ~/.shared_memory/_policy.json 的 overwrite_shrink_ratio。")
    bak = {}

    def _w():
        doc = _load(project)
        bak["p"] = _backup_content(project, doc)  # 覆盖前先备份**整份档案**
        doc["project"] = project
        doc["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        doc["content"] = content
        return _save(project, doc)
    p = _with_lock(project, _w)
    _audit(context, "write_memory", project, f"写入 {len(content)} 字 → {p}")
    if bak.get("p"):
        return (f"已写入 {p}\n（覆盖前已备份**整份档案** → {bak['p']}；"
                f"要回滚: cp {bak['p']} {p}）")
    return f"已写入 {p}"


@mcp.tool()
def append_memory(project: str, note: str, context: Context, request_id: str = "") -> str:
    """向某项目档案追加一条进展记录 (保留历史, 不覆盖)。

    用法: 每次任务结束追加 "2026-10-03 22:00 完成了 xxx, 产物在 yy, 下一步 zz"。
    多客户端并发追加安全 (文件锁, 不会互相覆盖)。
    ⚠️ 硬性要求: 必须先 read_memory 过该项目 (30 分钟内), 禁止凭自身记忆追加。

    ── request_id: 幂等键 (2026-10-05 加) ──
    同一个 request_id 重复调用**只会追加一次**, 第二次直接返回"已存在"。
    借鉴 Pi Durable 的 exactly-once 提交: 客户端崩了/重连了以后重发同一条记录,
    拿回的是原来那条, 而不是让 log 里多出一条重复的流水。
    log 条目会带 seq(单调递增), 配合 read_memory(since_seq=) 可以只取增量。

    📏 长度建议(借官方 memory server 的"一条 observation 只放一个事实"那条规矩):
    超过 800 字会返回一句提醒 —— 不阻断, 但强烈建议**拆成多条追加**。
    理由是实测出来的: 一条几千字的 log, 读的时候整段进上下文, 而检索也命中不了
    里面的某一行。长条目挤占预算, 却降低了可检索性。
    """
    block = _require_read(context, project)
    if block:
        return block
    ro = _write_guard(context, "追加记忆")
    if ro:
        return ro
    rid = request_id.strip()[:64]
    dup = []

    def _a():
        doc = _load(project)
        doc.setdefault("log", [])
        if rid:
            for e in doc["log"]:
                if e.get("request_id") == rid:
                    dup.append(True)          # 幂等命中: 不重复追加
                    return _path(project)
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note,
                 "seq": len(doc["log"]) + 1}   # seq 与下标一致, 旧条目无 seq 时按下标+1
        if rid:
            entry["request_id"] = rid
        doc["log"].append(entry)
        return _save(project, doc)
    p = _with_lock(project, _a)
    _audit(context, "append_memory", project, note[:80] + (" 幂等命中" if dup else ""))
    if dup:
        return f"已存在(幂等, 未重复追加) → {p}"
    hint = ""
    if len(note) > 800:
        hint = ("\n📏 这条 %d 字, 偏长。建议**拆成多条追加**: 一条只讲一件事。\n"
                "   长条目会整段进上下文, 而且 search 检索时命中不了中间的行。" % len(note))
    return f"已追加 → {p}" + hint


if __name__ == "__main__":
    os.makedirs(DB, exist_ok=True)
    print(f"[memory] 记忆目录: {DB}")
    print(f"[memory] 监听 {HOST}:{PORT}/mcp (streamable-http)")
    mcp.run(transport="streamable-http")
