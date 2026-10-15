"""验证 memory_server 的同名会话修复 (2026-10-03 改动)。

隔离: 全部落 /tmp 临时目录, 不碰 ~/.shared_memory 生产数据。
覆盖:
  1. register_name 不再静默覆盖别人的名字 (C 止血)
  2. force=True 能显式接管
  3. _meta 带 agent 时, 同平台不同会话生成不同 key (A 根治)
  4. 30 分钟门禁按 key 判, 同名不互相放行
  5. 任务归属按 key 判, 同名不互相改
  6. 旧客户端 (无 _meta) 行为不变 (兼容性)
"""
import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="memtest-")
os.environ["MEMORY_TEST_DIR"] = TMP

spec = importlib.util.spec_from_file_location(
    "msrv", os.path.join(os.path.dirname(__file__), "..", "memory_server.py"))
msrv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(msrv)

# 重定向到临时目录 (必须在模块 import 后改, 因为路径是模块级常量)
msrv.DB = TMP
msrv.AUDIT = os.path.join(TMP, "_audit.jsonl")
msrv.CLIENTS = os.path.join(TMP, "_clients.json")
msrv.TASKS = os.path.join(TMP, "_tasks.json")

# 注入测试名册与旧 key 迁移表（开源版默认两者为空；不注入的话写操作会先撞
# "不在名册"闸，根本走不到本测试要验证的门禁/归属逻辑）
msrv.DEFAULT_ROSTER = {
    "role-dev": {"name": "开发", "platform": "CLI", "note": "测试用通用角色"},
    "role-test": {"name": "测试", "platform": "脚本", "note": "测试用角色"},
}
msrv.LEGACY_KEYS = {"ide-runtime-mcp": "role-dev", "legacy-desktop-mcp": "role-dev"}

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
    """模拟 MCP Context: session 报 clientInfo, request_context.meta 带自定义字段。"""
    def __init__(self, client_name, meta=None):
        self.session = FakeSession(client_name)
        self.request_context = FakeRC(FakeMeta(meta) if meta else None)


def audit_rows():
    if not os.path.exists(msrv.AUDIT):
        return []
    with open(msrv.AUDIT, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


print("\n【1】register_name 撞 key 时的行为 (2026-10-03 改: 拒绝 -> 自动分配)")
# ⚠️ 每次注册用**独立的 ctx 对象** —— 因为 key 一旦分配就绑在这个会话对象上,
#    同一对象连调两次, 第二次会直接复用已绑的 key, 测不到"撞 key"分支。
ctx_design = FakeCtx("ide-runtime-mcp")
r1 = msrv.register_name("IDE-alpha", ctx_design)
ctx_eng = FakeCtx("ide-runtime-mcp")
r2 = msrv.register_name("IDE-beta", ctx_eng)       # 撞 key
check("首次注册成功", "已注册" in r1, r1)
check("撞 key 不再死锁(自动分配独立 key)", "已注册" in r2, r2)
check("自动分配的 key 带 #序号(与先注册者区分)", "#" in r2, r2)
check("先注册者的名字没被覆盖(仍防误伤)",
      msrv._load_clients().get("ide-runtime-mcp") == "IDE-alpha",
      str(msrv._load_clients().get("ide-runtime-mcp")))
check("自动分配的会话名字确实是新名字",
      msrv._load_clients().get(r2.split("key=")[1].split(")")[0].strip().rstrip("'"))
      == "IDE-beta", r2)
# force 是唯一能改原 key 的口子 (放最后, 免得污染上面的断言)
ctx_force = FakeCtx("ide-runtime-mcp")
r3 = msrv.register_name("IDE-temp", ctx_force, force=True)
check("force=True 能显式接管原 key",
      msrv._load_clients().get("ide-runtime-mcp") == "IDE-temp",
      str(msrv._load_clients().get("ide-runtime-mcp")))
# 后续用例统一用这个"老式客户端"(不绑新 key), 保持撞 key 分支之外的路径可测
ctx_a = FakeCtx("ide-runtime-mcp")

print("\n【2】_meta 区分同平台多会话 (A 根治)")
m1 = {"agent": "IDE-beta", "session": "15a3"}
# 身份用册内角色: 写操作统一过"总闸"(身份必须在册), 否则会先撞上
# "还没认领角色"而根本走不到 30 分钟门禁, 那这条就测不到它了
m2 = {"agent": "role-test", "session": "15b7"}
k1 = msrv._client_key(FakeCtx("ide-runtime-mcp", m1))
k2 = msrv._client_key(FakeCtx("ide-runtime-mcp", m2))
k3 = msrv._client_key(FakeCtx("ide-runtime-mcp", m1))   # 同一会话 -> 稳定
check("不同会话 -> 不同 key", k1 != k2, f"{k1} vs {k2}")
check("key 含会话标识", "15a3" in k1, k1)
check("同一会话 -> key 稳定", k1 == k3, f"{k1} vs {k3}")
check("无 _meta 时回退 clientInfo",
      msrv._client_key(FakeCtx("LegacyIDE")) == "LegacyIDE")

print("\n【3】30 分钟门禁按 key 判 (不再同名互相放行)")
proj = "协作测试项目"
msrv.read_memory(proj, context=ctx_a)                      # 旧式客户端读了
msrv._save(proj, {"content": "x", "log": []}) if hasattr(msrv, "_save") else None
# 让 read 真的落审计即可 (它先 _audit 再读)
b1 = msrv.append_memory(proj, "读后写", ctx_a)      # 同 key -> 应放行
b2 = msrv.append_memory(proj, "冒名写", FakeCtx("ide-runtime-mcp", m2))
check("同一 key 读后能写(门禁仍有效)", not b1.startswith("❌"), b1)
# 关键不是措辞(从没读过和读太久措辞不同), 而是**拒的是哪道闸**:
# 必须是 30 分钟读记忆门禁, 不能是"还没认领角色"那道新闸 —— 否则这条就没测到它。
check("另一会话没读过 -> 被拒(且拒的是 30 分钟门禁, 不是身份那道闸)",
      b2.startswith("❌ 被拒") and "还没认领角色" not in b2, b2)
rows = audit_rows()
check("审计写入了 client_key 字段", any("client_key" in r for r in rows))

print("\n【4】任务归属按 key 判")
ta = msrv.start_task(ctx_a, proj, "任务A", "A开的")
tid = ta.split(":")[1].split()[0] if ":" in ta else ta.split()[-1]
ta2 = msrv.start_task(FakeCtx("ide-runtime-mcp", m2), proj, "任务B", "B开的")
tid_b = ta2.split(":")[1].split()[0] if ":" in ta2 else ta2.split()[-1]
u1 = msrv.update_task(FakeCtx("ide-runtime-mcp", m2), tid, "50%")
u2 = msrv.update_task(ctx_a, tid, "50%")
check("B 改 A 的任务被拒", u1.startswith("不是你的任务"), u1)
check("A 改自己的任务成功", "已更新" in u2, u2)
tasks = msrv._load_tasks()["tasks"]
check("新任务存了 client_key", any("client_key" in t for t in tasks))

print("\n【5】旧客户端兼容性 (不破坏各类旧版桌面 MCP 客户端)")
ctx_old = FakeCtx("legacy-desktop-mcp")
msrv.register_name("LegacyIDE-测试", ctx_old)
check("旧客户端注册正常", msrv._load_clients().get("legacy-desktop-mcp") == "LegacyIDE-测试")
check("旧客户端展示名正确", msrv._client_name(ctx_old) == "LegacyIDE-测试")

print("\n【6】list_projects 返回真实项目名 (2026-10-03 修的 bug)")
# 造一个真实的项目档案 (和磁盘上一样)
with open(os.path.join(TMP, "测试项目.json"), "w", encoding="utf-8") as f:
    json.dump({"project": "测试项目", "log": [{"time": "x", "note": "y"}]}, f, ensure_ascii=False)
out = msrv.list_projects(ctx_a)
check("返回可被 json.loads 解析", (lambda r: (json.loads(r), True)[1] if r.strip().startswith("[") else False)(out), out)
try:
    parsed = json.loads(out)
except Exception:
    parsed = None
check("列表里含真实项目名", parsed is not None and "测试项目" in parsed, str(parsed))
check("内部台账文件被过滤(不混进项目列表)",
      parsed is not None and "_clients" not in parsed and "_tasks" not in parsed, str(parsed))

print("\n【7】read_memory 对不存在的项目明说没有 (防误判成'项目是空的')")
r_missing = msrv.read_memory("根本不存在这个项目", ctx_a)
check("不存在的项目返回明确错误", r_missing.startswith("❌ 没有"), r_missing[:60])
check("错误信息附带现有项目清单", "测试项目" in r_missing, r_missing[:120])
r_real = msrv.read_memory("测试项目", context=ctx_a)
check("真实项目仍能读出完整档案", '"log"' in r_real and "测试项目" in r_real,
      r_real[:80])

print("\n【8】start_task 硬门禁 (2026-10-03 收紧: 没读过就拒绝开任务)")
proj2 = "门禁测试项目"
with open(os.path.join(TMP, proj2 + ".json"), "w", encoding="utf-8") as f:
    json.dump({"project": proj2, "log": []}, f, ensure_ascii=False)
r = msrv.start_task(ctx_a, proj2, "没读就开工", "应被拒")
check("没 read_memory 就 start_task 被拒", r.startswith("❌"), r[:60])
check("拒绝时没建出任务(看板干净)",
      not any(t["name"] == "没读就开工" for t in msrv._load_tasks()["tasks"]))
msrv._audit(ctx_a, "read_memory", proj2)          # 读了
r = msrv.start_task(ctx_a, proj2, "读过再开工", "应放行")
check("read_memory 后 start_task 放行", r.startswith("任务已开始"), r[:60])
check("放行时任务真的建出来了",
      any(t["name"] == "读过再开工" for t in msrv._load_tasks()["tasks"]))
# 另一会话没读 -> 也该被拒
r2 = msrv.start_task(FakeCtx("ide-runtime-mcp", m2), proj2, "B冒名开工", "")
check("另一会话没读 -> 被拒", r2.startswith("❌"), r2[:50])

print("\n【8b】新项目不该被门禁锁死 (读说不存在 -> 仍能开第一个任务)")
proj3 = "全新项目X"
check("新项目确实还不存在", not os.path.exists(os.path.join(TMP, proj3 + ".json")))
r = msrv.start_task(ctx_a, proj3, "首个任务", "新项目没有历史记忆可对齐")
check("新项目不需先读就能开任务", r.startswith("任务已开始"), r[:60])
r = msrv.write_memory(proj3, "# 全新项目", ctx_a)
check("新项目不需先读就能写记忆", not r.startswith("❌"), r[:50])
r = msrv.read_memory(proj3, context=ctx_a)
check("新项目建好后能读出内容", "# 全新项目" in r, r[:60])

print("\n【9】全程隔离在临时目录（不碰默认数据目录）")
check("客户端名册写在 TMP 下", str(msrv.CLIENTS).startswith(TMP), str(msrv.CLIENTS))
check("任务台账写在 TMP 下", str(msrv.TASKS).startswith(TMP), str(msrv.TASKS))
check("临时名册里只有本测试的注册",
      set(json.load(open(msrv.CLIENTS, encoding="utf-8")).values()) <= {"IDE-alpha", "IDE-beta", "IDE-temp", "LegacyIDE-测试"})

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("=" * 46))
if FAILS:
    print(f"❌ {len(FAILS)} 项未通过: {FAILS}")
    sys.exit(1)
print("✅ 全部通过")
