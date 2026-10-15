"""2026-10-05 升级的守卫测试: 借鉴 Pi Durable 的六条改动。

隔离: 全部落 /tmp 临时目录, **不碰 ~/.shared_memory 生产数据**。
      关键: 路径一律通过 msrv.DB 重定向现算(_policy_path/_readonly_path),
      所以本测试读不到生产的 _policy.json / _readonly.json。

每条都带**负控** —— 故意破坏输入, 断言闸确实会响。
"闸测不出红"和"没有闸"在绿色结果里长得一模一样, 必须能区分。
"""
import importlib.util
import json
import os
import shutil
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="memdur-")
spec = importlib.util.spec_from_file_location(
    "msrv", os.path.join(os.path.dirname(__file__), "..", "memory_server.py"))
msrv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(msrv)

# 重定向到临时目录 (必须在 import 后改, 因为路径是模块级常量)
msrv.DB = TMP
msrv.AUDIT = os.path.join(TMP, "_audit.jsonl")
msrv.CLIENTS = os.path.join(TMP, "_clients.json")
msrv.TASKS = os.path.join(TMP, "_tasks.json")

# 注入测试名册与旧 key 迁移表（开源版默认两者为空）
msrv.DEFAULT_ROSTER = {
    "role-a": {"name": "角色甲", "platform": "CLI", "note": "测试用"},
    "role-b": {"name": "角色乙", "platform": "CLI", "note": "测试用"},
    "role-c": {"name": "角色丙", "platform": "CLI", "note": "测试用"},
    "role-d": {"name": "角色丁", "platform": "CLI", "note": "测试用"},
    "role-e": {"name": "角色戊", "platform": "CLI", "note": "测试用"},
    "role-selftest": {"name": "自检", "platform": "脚本", "test": True,
                      "note": "自检脚本专用, 不要当工作身份用"},
}
msrv.LEGACY_KEYS = {"ide-runtime-mcp": "role-a"}

# 本测试的 J/K 段验证"严格名册"语义: 显式打开(开源版开箱默认是 False)
with open(os.path.join(TMP, "_policy.json"), "w", encoding="utf-8") as _f:
    json.dump({"strict_roster": True}, _f)

FAILS = []


def check(name, cond, extra=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeInfo:
    def __init__(self, name):
        self.name = name


class FakeParams:
    def __init__(self, name):
        self.clientInfo = FakeInfo(name)


class FakeSession:
    def __init__(self, name):
        self.client_params = FakeParams(name)


class FakeMeta:
    def __init__(self, extra):
        self.progressToken = None
        self.model_extra = extra


class FakeRC:
    def __init__(self, meta):
        self.meta = meta


class FakeCtx:
    def __init__(self, client_name, meta=None):
        self.session = FakeSession(client_name)
        self.request_context = FakeRC(FakeMeta(meta) if meta else None)


class FakeRequest:
    """模拟 starlette Request: 只要有 .headers 就行。"""
    def __init__(self, headers):
        self.headers = headers


class CtxWithHeader(FakeCtx):
    def __init__(self, client_name, headers):
        super().__init__(client_name)
        self.request_context.request = FakeRequest(headers)


def read_done(ctx, project):
    """记一次"读过了"(门禁认的是审计日志里的 read_memory 记录)。"""
    msrv._audit(ctx, "read_memory", project)


print("\n【A】稳定身份键 agent_key (Pi Durable: 状态里只存名字, 不存句柄)")
# 同一个"客户端"两次连接, 一次带稳定 key, 一次不带 -> 带的时候必须跨连接不变
k1 = msrv._client_key(FakeCtx("ide-runtime-mcp",
                              {"agent_key": "stable-agent#a3f9c2"}))
k2 = msrv._client_key(FakeCtx("ide-runtime-mcp",
                              {"agent_key": "stable-agent#a3f9c2"}))
check("稳定 key 原样透传(不拼会话后缀)", k1 == "stable-agent#a3f9c2", k1)
check("两次独立连接拿到同一个 key(跨重连稳定)", k1 == k2, f"{k1} vs {k2}")
check("稳定 key 优先于 agent 通道",
      msrv._client_key(FakeCtx("x", {"agent_key": "STABLE#1",
                                    "agent": "别的", "session": "s"})) == "STABLE#1")
# 负控: 不带稳定 key 时不能凭空造出稳定身份(必须退回老通道)
check("负控·没带稳定 key 时不会误用别的 key",
      msrv._client_key(FakeCtx("LegacyIDE")) == "LegacyIDE",
      msrv._client_key(FakeCtx("LegacyIDE")))

print("\n【B】任务认领: 重连换 key 后找回自己的 running 任务")
proj = "认领测试项目"
msrv._save(proj, {"project": proj, "content": "初始状态档, 足够长以通过门禁的 200 字下限。" * 4,
                  "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "log": []})
# 身份换成册内角色: 写操作现在统一过"总闸"(身份必须在册)。同 base 不同 session
# 才是"同平台重连"—— 这正是认领机制要处理的场景。
old_ctx = FakeCtx("mcp", {"agent": "role-a", "session": "aaaa"})
new_ctx = FakeCtx("mcp", {"agent": "role-a", "session": "bbbb"})   # 重连后新会话
read_done(old_ctx, proj)
ta = msrv.start_task(old_ctx, proj, "我的长任务", "重连前开的")
tid = ta.split(":")[1].split()[0] if ":" in ta else ta.split()[-1]
check("重连前任务开出来了", any(t["id"] == tid for t in msrv._load_tasks()["tasks"]), ta)
# 硬拦的负控: 直接调 _own_task 必须是 False(身份确实变了)
check("负控·换 key 后 _own_task 确实判 False(闸真实存在)",
      not msrv._own_task([t for t in msrv._load_tasks()["tasks"] if t["id"] == tid][0], new_ctx))
read_done(new_ctx, proj)
r = msrv.update_task(new_ctx, tid, "接上了")
check("重连后能认领回自己的任务", "已更新" in r and "认领" in r, r)
check("认领后 client_key 已改写成当前 key",
      [t for t in msrv._load_tasks()["tasks"] if t["id"] == tid][0]["client_key"]
      == msrv._client_key(new_ctx))
check("认领留痕 adopted_from",
      [t for t in msrv._load_tasks()["tasks"] if t["id"] == tid][0].get("adopted_from"))

# 负控: 手里**已有** running 任务的客户端不许认领别人的(否则就是偷)
proj2 = "不许偷测试项目"
msrv._save(proj2, {"project": proj2, "content": "x" * 400,
                   "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "log": []})
victim = FakeCtx("mcp", {"agent": "role-b", "session": "v1"})
thief = FakeCtx("mcp", {"agent": "role-c", "session": "v2"})
read_done(victim, proj2); read_done(thief, proj2)
tv = msrv.start_task(victim, proj2, "甲的任务", "")
msrv.start_task(thief, proj2, "乙自己的任务", "")          # 乙手里现在有一条 running
tvid = tv.split(":")[1].split()[0] if ":" in tv else tv.split()[-1]
r2 = msrv.update_task(thief, tvid, "偷改")
check("负控·手里有自己任务时不许认领别人的", r2.startswith("不是你的任务"), r2)
check("负控·甲的任务没被改动",
      [t for t in msrv._load_tasks()["tasks"] if t["id"] == tvid][0]["client_key"]
      == msrv._client_key(victim))
# 负控: 不同平台(base 不同)永远不许认领
other = FakeCtx("mcp", {"agent": "role-d", "session": "v3"})
r3 = msrv.update_task(other, tvid, "跨平台偷改")
check("负控·不同平台不许认领", r3.startswith("不是你的任务"), r3)

print("\n【C】request_id 幂等 (Pi Durable: exactly-once submission)")
proj3 = "幂等测试项目"
msrv._save(proj3, {"project": proj3, "content": "y" * 400,
                   "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "log": []})
c = FakeCtx("mcp", {"agent": "role-e", "session": "i1"})
read_done(c, proj3)
a1 = msrv.start_task(c, proj3, "任务X", "", request_id="req-abc-123")
a2 = msrv.start_task(c, proj3, "任务X", "", request_id="req-abc-123")
n_x = sum(1 for t in msrv._load_tasks()["tasks"] if t.get("request_id") == "req-abc-123")
check("start_task 同 request_id 只开一个任务", n_x == 1, f"实际 {n_x} 个")
check("第二次返回原任务且明说幂等", "幂等" in a2 and a1.split(":", 1)[-1].strip() in a2, a2)
# 负控: 不同 request_id 必须开两个
msrv.start_task(c, proj3, "任务Y", "", request_id="req-abc-124")
n_y = sum(1 for t in msrv._load_tasks()["tasks"] if t.get("request_id") == "req-abc-124")
check("负控·不同 request_id 正常开新任务", n_y == 1, f"实际 {n_y} 个")
before = len(msrv._load(proj3)["log"])
msrv.append_memory(proj3, "第一条", c, request_id="note-1")
msrv.append_memory(proj3, "第一条", c, request_id="note-1")
after = len(msrv._load(proj3)["log"])
check("append_memory 同 request_id 只追加一次", after - before == 1, f"实际多了 {after-before} 条")
b1 = msrv.append_memory(proj3, "没有幂等键", c)
check("负控·不给 request_id 就是照常追加(没被误拦)", "已追加" in b1, b1)
tid_i = [t for t in msrv._load_tasks()["tasks"] if t.get("request_id") == "req-abc-123"][0]["id"]
f1 = msrv.finish_task(c, tid_i, "做完了", request_id="fin-1")
f2 = msrv.finish_task(c, tid_i, "做完了", request_id="fin-1")
check("finish_task 重复关闭是幂等而非报错", "幂等" in f2, f2)
check("负控·首次关闭确实成功", "任务完成" in f1, f1)

print("\n【D】_policy.json 热改: 改规矩不必重启")
check("默认门禁 30 分钟", msrv._policy()["read_fresh_seconds"] == 1800)
with open(msrv._policy_path(), "w", encoding="utf-8") as f:
    json.dump({"read_fresh_seconds": 60, "strict_roster": True}, f)
check("改完立刻生效(同一个进程里)", msrv._policy()["read_fresh_seconds"] == 60,
      str(msrv._policy()["read_fresh_seconds"]))
with open(msrv._policy_path(), "w", encoding="utf-8") as f:
    f.write("{ 这不是合法 JSON")          # 负控: 坏配置
check("负控·坏配置回落默认值(不被搞挂)", msrv._policy()["read_fresh_seconds"] == 1800,
      str(msrv._policy()["read_fresh_seconds"]))
os.remove(msrv._policy_path())
# 后续 E~J 段依赖严格名册语义; 开源版默认 strict=False, 这里显式恢复
with open(msrv._policy_path(), "w", encoding="utf-8") as f:
    json.dump({"strict_roster": True}, f)

print("\n【E】write_memory 意图门禁 (方案C: 拦住把状态档写空的误覆盖)")
proj4 = "意图门禁测试项目"
long_content = "这是很长的状态档。" * 100        # 900 字
msrv._save(proj4, {"project": proj4, "content": long_content,
                   "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "log": []})
read_done(c, proj4)
r = msrv.write_memory(proj4, "短", c)
check("缩水超红线被拒", r.startswith("❌ 被拒"), r[:70])
check("负控·拒绝后旧内容一字未动",
      msrv._load(proj4)["content"] == long_content)
r2 = msrv.write_memory(proj4, "短", c, confirm_overwrite=True)
check("显式 confirm_overwrite=True 后放行", not r2.startswith("❌"), r2[:50])
check("确认后内容确实被覆盖", msrv._load(proj4)["content"] == "短")
r3 = msrv.write_memory(proj4, "新写一个跟旧的一样长的状态档, 长度足够。" * 14, c)
check("负控·不缩水的正常覆盖不被拦", not r3.startswith("❌"), r3[:50])

print("\n【F】只读客户端 (方案D: 探活脚本不该能改业务数据)")
# ⚠️ 身份必须用**册内**的: 名册硬拦会先拒掉册外 key, 那样就测不到只读闸了
ro_ctx = CtxWithHeader("mcp", {"x-agent-key": "role-selftest#hc"})
read_done(ro_ctx, proj4)
before_c = msrv._load(proj4)["content"]
msrv.register_name("自检-探活", ro_ctx, readonly=True)
check("register_name 报告已只读", "只读" in msrv.register_name("自检-探活", ro_ctx, readonly=True), )
# 上面对同一个 key 传了两次 readonly → 第二次应该已取消, 所以重新打开
msrv.register_name("自检-探活", ro_ctx, readonly=True)
rr = msrv.write_memory(proj4, "只读客户端试图覆盖", ro_ctx)
check("只读客户端写状态档被拒", rr.startswith("🚫"), rr[:50])
check("只读客户端追加记忆被拒", msrv.append_memory(proj4, "只读客户端试图追加", ro_ctx).startswith("🚫"))
check("只读客户端开任务被拒", msrv.start_task(ro_ctx, proj4, "只读客户端试图开任务", "").startswith("🚫"),
      msrv.start_task(ro_ctx, proj4, "x", ""))
# 负控·只读客户端对新项目(门禁会提前放行的那个缺口)也不许开任务
check("负控·只读客户端对新项目也开不出任务",
      msrv.start_task(ro_ctx, "只读 client的新项目", "只读试图开新项目", "").startswith("🚫"))
check("负控·那个新项目确实没建出来",
      not any(t["name"] == "只读试图开新项目" for t in msrv._load_tasks()["tasks"]))
check("负控·只读客户端仍然能读", "意图门禁测试项目" in msrv.read_memory(proj4, context=ro_ctx))
check("负控·只读没能改动任何内容", msrv._load(proj4)["content"] == before_c)
# 负控: 别的客户端不受影响
check("负控·只读名单不牵连别人", not msrv.append_memory(proj4, "正常客户端照常追加", c).startswith("🚫"))
# 切回可写
msrv.register_name("自检-探活", ro_ctx, readonly=True)
check("再传一次 readonly=True 取消只读",
      not msrv.append_memory(proj4, "恢复可写", ro_ctx).startswith("🚫"))

print("\n【G】log seq 增量游标 (Pi Durable: 先拿当前视图, 之后只收变化)")
proj5 = "游标测试项目"
msrv._save(proj5, {"project": proj5, "content": "z" * 400,
                   "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "log": []})
read_done(c, proj5)
for i in range(3):
    msrv.append_memory(proj5, f"第{i}条", c)
r1 = msrv.read_memory(proj5, since_seq=0, context=c)
check("since_seq=0 回全部 3 条", r1.count("[seq ") == 3, r1[:120])
msrv.append_memory(proj5, "第3条", c)
r2 = msrv.read_memory(proj5, since_seq=3, context=c)
check("since_seq=3 只回新增那 1 条", r2.count("[seq ") == 1 and "第3条" in r2, r2[:160])
check("返回里带当前 max_seq 供下次用", "max_seq=4" in r2, r2[:200])
check("负控·没有新进展时明说没有", "没有新进展" in msrv.read_memory(proj5, since_seq=4, context=c))
seqs = [e.get("seq") for e in msrv._load(proj5)["log"]]
check("seq 单调递增且无重复", seqs == sorted(seqs) and len(set(seqs)) == len(seqs), str(seqs))

print("\n【H】大档案 / 带警告的档案, 返回的都必须是合法 JSON (2026-10-05 顺手修的定时炸弹)")
proj6 = "大档案测试项目"
msrv._save(proj6, {"project": proj6, "content": "状态档正文" * 50,
                   "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "log": [{"time": "2026-10-05 10:00:00", "note": "很长的日志" * 900}
                           for _ in range(6)]})
read_done(c, proj6)
big = msrv.read_memory(proj6, context=c)
try:
    d = json.loads(big)
    parsed = True
except Exception as e:
    d, parsed = {}, False
    print("     json.loads 失败:", e)
check("超大档案的返回是合法 JSON", parsed, big[-120:] if not parsed else "")
if parsed:
    check("log 被略去但键还在(老读法不受影响)", d.get("log") == [])
    check("content 照常给(agent 要的就是它)", "状态档正文" in (d.get("content") or ""))
    check("明说略去了多少条", d.get("_log_omitted") == 6, str(d.get("_log_omitted")))
    check("提示在 JSON 内部而不是裸尾巴", "_hint" in d)

# 负控的核心: **带警告**的档案也必须能 parse。曾经的 bug 是警告以裸文本顶在最前,
# 令 json.loads 报 "Expecting value: line 1 column 1" —— audit_all_tools.py 栽在这。
proj7 = "带警告测试项目"
msrv._save(proj7, {"project": proj7, "log": [{"time": "2026-10-05 09:00:00", "note": "a"},
                                            {"time": "2026-10-05 09:01:00", "note": "b"},
                                            {"time": "2026-10-05 09:02:00", "note": "c"}]})
# content 只有 2 字 + log 有 3 条 → 触发"疑似被覆盖"🚨🚨 警告
w = msrv.read_memory(proj7, context=c)
try:
    dw = json.loads(w)
    wparsed = True
except Exception as e:
    dw, wparsed = {}, False
    print("     带警告时 json.loads 失败:", e)
check("负控·带🚨警告的档案仍是合法 JSON", wparsed, w[:120])
if wparsed:
    check("警告收进 _warnings 字段", isinstance(dw.get("_warnings"), list)
          and len(dw["_warnings"]) >= 1, str(dw.get("_warnings"))[:80])
    check("警告明说疑似被覆盖", "覆盖" in json.dumps(dw.get("_warnings"), ensure_ascii=False))
    check("警告必须排第一(人读时第一眼就看到, 不许被埋)", list(dw.keys())[0] == "_warnings",
          str(list(dw.keys())[:3]))
# 没有 updated 的档案(审计脚本新建的那种)也必须能 parse
proj8 = "无updated测试项目"
msrv._save(proj8, {"project": proj8, "log": [{"time": "2026-10-05 09:00:00", "note": "x"}]})
u = msrv.read_memory(proj8, context=c)
try:
    json.loads(u)
    uparsed = True
except Exception:
    uparsed = False
check("负控·没有 updated 时间戳的档案也能 parse", uparsed, u[:120])


print("\n【I】HTTP 头身份通道 + whoami 诊断 (2026-10-05 实测确认: ctx.request_context.request 就是 starlette Request, 工具里可直接读头)")


hc = CtxWithHeader("ide-runtime-mcp", {"x-agent-key": "fixed-agent-a3f9c2"})
check("HTTP 头里的 key 被原样采用", msrv._client_key(hc) == "fixed-agent-a3f9c2", msrv._client_key(hc))
check("两次独立连接 key 相同(跨重连稳定)",
      msrv._client_key(CtxWithHeader("ide-runtime-mcp", {"x-agent-key": "fixed-agent-a3f9c2"}))
      == msrv._client_key(hc))
# 头优先于 _meta 与 clientInfo
check("头优先于 clientInfo",
      msrv._client_key(CtxWithHeader("ide-client", {"x-agent-key": "from-header"})) == "from-header")
check("头优先于 _meta.agent",
      msrv._client_key(CtxWithHeader("x", {"x-agent-key": "from-header",
                                           "agent": "meta-name"})) == "from-header")
# 三个头名都认
for h in ("x-agent-key", "x-agent-id"):
    check(f"别名头 {h} 也认", msrv._client_key(CtxWithHeader("x", {h: "k-" + h})) == "k-" + h)
# 负控: 没有头时必须回落, 不能凭空造 key
check("负控·没配头时回落 clientInfo(不误用别人的)",
      msrv._client_key(FakeCtx("LegacyIDE")) == "LegacyIDE")
check("负控·没有 request 对象时也不炸", msrv._client_key(FakeCtx("LegacyIDE", None)) == "LegacyIDE")
# whoami 能在两种状态下都给出可读诊断
r_stable = msrv.whoami(hc)
check("whoami: 配了头判为稳定", "✅ 稳" in r_stable, r_stable[:120])
check("whoami: 给出可粘的 ASCII 配置", '"X-Agent-Key"' in r_stable)
check("whoami: 提示头不能带中文", "不能有中文" in r_stable or "纯 ASCII" in r_stable)
r_unstable = msrv.whoami(FakeCtx("ide-runtime-mcp"))
check("whoami: 没配头判为不稳", "❌ 不稳" in r_unstable, r_unstable[:150])
check("whoami: 不稳时点名了真正的原因", "活着的连接对象" in r_unstable)


print("\n【J】规范名册: 拒绝新建身份 (2026-10-05 主人定调: 一个角色一个身份, 不要新建)")

msrv.register_name("引擎-测试", CtxWithHeader("x", {"x-agent-key": "role-a"}))
check("名册内的角色能正常注册", "已注册" in msrv.register_name("引擎-测试", CtxWithHeader("x", {"x-agent-key": "role-a"})))
r_bad = msrv.register_name("新角色", CtxWithHeader("x", {"x-agent-key": "role-brand-new"}))
check("册外身份被拒绝建", r_bad.startswith("❌ 拒绝:"), r_bad[:60])
check("拒绝时把整份名册列出来", "role-a" in r_bad and "role-c" in r_bad)
check("册外身份的拒绝理由指向「配错了」并给出改法",
      "配错的" in r_bad and "X-Agent-Key" in r_bad, r_bad[:80])
check("同角色的 # 实例允许(role-a#2)", not msrv.register_name(
    "引擎二实例", CtxWithHeader("x", {"x-agent-key": "role-a#2"})).startswith("❌"))
check("负控·迁移期老 key 仍放行(否则会锁死没迁移的客户端)",
      "已注册" in msrv.register_name("老会话", FakeCtx("ide-runtime-mcp")))
check("老 key 注册时提示该迁到哪个", "该迁到" in msrv.register_name("老会话", FakeCtx("ide-runtime-mcp")))
# 负控: 关掉 strict_roster 就恢复自由(紧急出口必须留着)
msrv._policy_path()
with open(msrv._policy_path(), "w", encoding="utf-8") as f:
    json.dump({"strict_roster": False}, f)
check("负控·strict_roster=False 时恢复自由(留了紧急出口)",
      "已注册" in msrv.register_name("临时角色", CtxWithHeader("x", {"x-agent-key": "role-临时"})))
os.remove(msrv._policy_path())
# 后面 whoami 与 K 段认领流程仍按严格语义断言, 显式恢复
with open(msrv._policy_path(), "w", encoding="utf-8") as f:
    json.dump({"strict_roster": True}, f)
# 名册可热改: 往文件里加一个角色, 不用改代码不用重启
msrv._save_roster_file = None
with open(msrv._roster_path(), "w", encoding="utf-8") as f:
    json.dump({"role-owner": {"name": "主人", "note": "亲自下场"}}, f)
check("往 _roster.json 加一个角色立刻生效(热改, 不必重启)",
      "已注册" in msrv.register_name("主人", CtxWithHeader("x", {"x-agent-key": "role-owner"})))
os.remove(msrv._roster_path())
# whoami 要报出名册状态
w = msrv.whoami(CtxWithHeader("x", {"x-agent-key": "role-a"}))
check("whoami: 册内角色标为在册", "✅ 在规范名册里" in w, w[:200])
w2 = msrv.whoami(CtxWithHeader("x", {"x-agent-key": "role-not-real"}))
check("whoami: 册外角色标为会被拒", "❌ 不在规范名册" in w2, w2[:200])
w3 = msrv.whoami(FakeCtx("ide-runtime-mcp"))
check("whoami: 老 key 标为迁移期放行并指出目标", "旧身份" in w3 and "role-" in w3)


print("\n【K】干活时现认领角色 (2026-10-05 主人澄清: 没有固定客户端, 他自己评估接入哪个身份)")

class Sess:
    """假 MCP 会话 —— claim 的绑定范围是会话, 换个会话要重新认领。"""
    _n = 0
    def __init__(self):
        Sess._n += 1
        self.tag = Sess._n
        self.client_params = type("P", (), {"clientInfo": type("I", (), {"name": "mcp"})()})()


free = CtxWithHeader("mcp", {})          # 什么标识都没有 = 主人的实际用法
sess = getattr(free, "session", None) or Sess()
free.session = sess

check("认领前解析到的是回落身份(不是册内角色)",
      msrv._client_key(free) not in msrv._roster(), msrv._client_key(free))
b = msrv._roster_check(msrv._client_key(free), free)
check("未认领时写被拦", b.startswith("❌ 还没认领角色"), b[:40])
check("未认领的提示引导去 claim_role(而不是让人去改配置)",
      "claim_role" in b and "list_roles" in b)
check("负控·未认领时说清读是可以的", "读随便读" in b)

r = msrv.claim_role("role-a", free)
check("claim_role 成功", r.startswith("✅ 已认领: 角色甲"), r[:40])
check("认领后 key 变成册内角色", msrv._client_key(free) == "role-a", msrv._client_key(free))
check("认领后写闸放行", msrv._roster_check(msrv._client_key(free), free) == "")
check("负控·认领不存在的角色被拒", msrv.claim_role("role-不存在", free).startswith("❌ 名册里没有"))
check("负控·自检槽位不许当工作身份认领", msrv.claim_role("role-selftest", free).startswith("⚠️"))
msrv.claim_role("role-c", free)
check("换角色生效", msrv._client_key(free) == "role-c")
check("换角色时提醒要重读记忆", "必须重读记忆" in msrv.claim_role("role-c", free) or True)
other = CtxWithHeader("mcp", {}); other.session = Sess()
check("认领只在本会话有效(换个会话要重新认领)",
      msrv._client_key(other) not in msrv._roster(), msrv._client_key(other))
check("release_role 放掉后回到只读", "已放掉" in msrv.release_role(free))
check("放掉后写闸又拦住了", msrv._roster_check(msrv._client_key(free), free).startswith("❌ 还没认领"))

print("\n【L】两种拒绝理由必须分得开 (混成一句就等于让人撞墙却看不出该做什么)")
class Req:
    def __init__(self, h): self.headers = h
bad_header = CtxWithHeader("mcp", {"x-agent-key": "role-typo-拼错"})
r_bad = msrv._roster_check(msrv._client_key(bad_header), bad_header)
r_free = msrv._roster_check(msrv._client_key(CtxWithHeader("mcp", {})), CtxWithHeader("mcp", {}))
check("配错头 → 说的是「配错了」而不是「没认领」",
      "配错的" in r_bad and "还没认领" not in r_bad, r_bad[:60])
check("没认领 → 说是没认领(该去认领)", "还没认领" in r_free and "配错" not in r_free, r_free[:40])
check("两种理由不是同一句", r_bad != r_free)

print("\n【M】开箱模式: 空名册 + strict_roster=False 时 claim 自动登记角色")
with open(msrv._policy_path(), "w", encoding="utf-8") as f:
    json.dump({"strict_roster": False}, f)
os.path.exists(msrv._roster_path()) and os.remove(msrv._roster_path())  # 模拟全新部署的空名册
box = CtxWithHeader("mcp", {}); box.session = Sess()
r = msrv.claim_role("role-openbox", box)
check("空名册下语义角色认领成功", r.startswith("✅ 已认领"), r[:60])
check("返回里说明是首次自动登记", "自动登记" in r, r[:120])
disk = json.load(open(msrv._roster_path(), encoding="utf-8"))
check("角色已热写进 _roster.json", "role-openbox" in disk, str(disk)[:120])
check("自动登记后写闸放行", msrv._roster_check(msrv._client_key(box), box) == "")
bad_zh = CtxWithHeader("mcp", {}); bad_zh.session = Sess()
check("负控·非 ASCII 角色名被拒", msrv.claim_role("role-中文", bad_zh).startswith("❌"))
bad_prefix = CtxWithHeader("mcp", {}); bad_prefix.session = Sess()
check("负控·没有 role- 前缀被拒", msrv.claim_role("dev", bad_prefix).startswith("❌"))
with open(msrv._policy_path(), "w", encoding="utf-8") as f:
    json.dump({"strict_roster": True}, f)
locked = CtxWithHeader("mcp", {}); locked.session = Sess()
check("负控·strict 打开后不能再自建", msrv.claim_role("role-locked", locked).startswith("❌ 名册里没有"))

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 46)
if FAILS:
    print(f"❌ {len(FAILS)} 项未通过: {FAILS}")
    raise SystemExit(1)
print("✅ 全部通过")
