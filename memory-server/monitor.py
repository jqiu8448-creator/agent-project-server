#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享记忆 · 任务监控页 —— http://127.0.0.1:8001

上半区: 任务看板 (谁在做什么任务 / 进行中 / 最近完成)
下半区: 最近操作流水 (谁在何时读了/写了什么)
标准库 http.server, 零依赖, 每 5 秒自动刷新。

数据目录与端口都走环境变量, 不硬编码:

    MEMORY_DB=~/.shared_memory       记忆库目录(与 memory_server.py 共用同一份)
    MEMORY_MONITOR_PORT=8001         监控页监听端口(只绑 127.0.0.1)

★ 模板为什么用 replace() 不用 .format()
  本页有一大块 CSS, 花括号要写成 {{ }}。漏写一个不会报错, 只渲染出一坨错乱的样式 ——
  正是本项目最忌讳的静默失效。改成占位符 + .replace(), 这类 bug 直接消失。
"""

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.expanduser(os.environ.get("MEMORY_DB", "~/.shared_memory"))
AUDIT = os.path.join(BASE, "_audit.jsonl")
TASKS = os.path.join(BASE, "_tasks.json")
PORT = int(os.environ.get("MEMORY_MONITOR_PORT", "8001"))

TOOL_CN = {
    "read_memory": "读档案",
    "write_memory": "写档案",
    "append_memory": "追加记录",
    "list_projects": "列出项目",
    "register_name": "命名",
    "start_task": "开始任务",
    "update_task": "更新进度",
    "finish_task": "完成任务",
    "claim_role": "认领角色",
    "release_role": "放掉角色",
    "whoami": "查身份",
}

TASK_CARD = """
<div class="card {cls}">
  <div class="row"><span class="who">{client}</span><span class="proj">{project}</span><span class="when">{when}</span></div>
  <div class="name">{name}</div>
  <div class="meta">{meta}</div>
</div>"""

ROW = """
<tr>
  <td class="t">{time}</td>
  <td><span class="c">{client}</span></td>
  <td><span class="tool">{tool}</span></td>
  <td class="p">{project}</td>
  <td class="d">{detail}</td>
</tr>"""

PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>共享记忆 · 任务监控</title>
<style>
  body{font:13px/1.6 -apple-system,"PingFang SC",sans-serif;background:#0f1216;color:#dbe2ea;margin:0;padding:24px}
  h1{font-size:15px;font-weight:700;color:#9ab} h1 small{color:#667;font-weight:400;margin-left:8px}
  h2{font-size:12px;color:#667;letter-spacing:.5px;margin:28px 0 10px;font-weight:600}
  .cards{display:flex;flex-wrap:wrap;gap:10px}
  .card{background:#151b23;border:1px solid #1d242d;border-radius:8px;padding:10px 14px;min-width:260px;max-width:340px}
  .card.run{border-left:3px solid #7ec8ff}
  .card.done{border-left:3px solid #79d8a8;opacity:.75}
  .row{display:flex;gap:8px;align-items:center;font-size:11px;color:#667}
  .who{color:#7ec8ff;font-weight:600;font-size:12px}
  .proj{color:#79d8a8} .when{margin-left:auto}
  .name{font-size:13px;font-weight:600;margin:4px 0 2px}
  .meta{font-size:11px;color:#8a94a0}
  .empty{color:#556;padding:14px 0}
  table{border-collapse:collapse;width:100%;margin-top:8px}
  th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #1d242d;white-space:nowrap}
  th{font-size:11px;color:#667;letter-spacing:.5px}
  td.d{white-space:normal;color:#8a94a0;max-width:460px}
  td.t{color:#5c6a7a;font-size:12px;font-variant-numeric:tabular-nums}
  .c{color:#7ec8ff} .tool{color:#ffd479} td.p{color:#79d8a8;font-size:12px}
</style></head>
<body>
<h1>共享记忆 · 任务监控 <small>谁 / 在做什么任务 · 每 5 秒自动刷新</small></h1>
<h2>🟢 进行中 (__NRUNNING__)</h2>
<div class="cards">__RUNNING__</div>
<h2>✅ 最近完成 (__NDONE__)</h2>
<div class="cards">__DONE__</div>

<h2>最近操作</h2>
<table><thead><tr><th>时间</th><th>客户端</th><th>操作</th><th>项目</th><th>内容</th></tr></thead>
<tbody>__ROWS__</tbody></table>
</body></html>"""


def _tasks_html():
    running, done = [], []
    try:
        if os.path.exists(TASKS):
            with open(TASKS, encoding="utf-8") as f:
                data = json.load(f)
            for t in reversed(data.get("tasks", [])):
                if t.get("status") == "running":
                    running.append(t)
                else:
                    done.append(t)
    except Exception:
        pass

    def card(t, cls):
        when = t.get("start", "")[:16]
        if cls == "done":
            meta = f"完成于 {str(t.get('finish',''))[5:16]} · {t.get('result','')}"
        else:
            meta = f"开始 {when} · {t.get('progress') or '刚开工'}"
        return TASK_CARD.format(
            cls=cls,
            client=html.escape(t.get("client", "unknown")),
            project=html.escape(t.get("project", "")),
            when=when,
            name=html.escape(t.get("name", "")),
            meta=html.escape(meta),
        )

    running_html = "".join(card(t, "run") for t in running[:8]) or '<div class="empty">暂无进行中的任务</div>'
    done_html = "".join(card(t, "done") for t in done[:6]) or '<div class="empty">暂无已完成任务</div>'
    return running_html, done_html, len(running), len(done[:8])


def _rows_html():
    rows = ""
    try:
        if os.path.exists(AUDIT):
            with open(AUDIT, encoding="utf-8") as f:
                lines = f.readlines()
            for ln in lines[-50:]:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                rows += ROW.format(
                    time=html.escape(r.get("time", "")),
                    client=html.escape(r.get("client", "unknown")),
                    tool=TOOL_CN.get(r.get("tool", ""), html.escape(r.get("tool", ""))),
                    project=html.escape(r.get("project", "")),
                    detail=html.escape(r.get("detail", "")),
                )
    except Exception:
        pass
    return rows or '<tr><td colspan="5" class="empty">暂无操作记录</td></tr>'


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        r_html, d_html, nr, nd = _tasks_html()
        body = (PAGE
                .replace("__RUNNING__", r_html).replace("__DONE__", d_html)
                .replace("__NRUNNING__", str(nr)).replace("__NDONE__", str(nd))
                .replace("__ROWS__", _rows_html()))
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"[monitor] http://127.0.0.1:{PORT}  (任务: {TASKS})")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
