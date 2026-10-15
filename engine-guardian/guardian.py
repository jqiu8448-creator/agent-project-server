#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine-guardian —— 项目服务器 · 空闲自动关闭守护器

判定「有没有活跃智能体」的唯一可靠信号是共享记忆的任务台账：
    ~/.shared_memory/_tasks.json  里 status == "running" 的任务数

    running > 0  → 有人在干活，重置空闲计时
    running == 0 → 开始累计空闲时间
    空闲累计 ≥ idle_minutes → 关闭被守护的服务

只依赖标准库。不读受保护目录，所以可以交给 launchd 托管。

auto_start 刻意保持关闭：本守护器只管「关」不管「开」。
各项目的智能体开工时自己会打开服务，不需要代劳；而 launchd 进程没有
~/Desktop 权限，执行桌面上的 .command 会报 Operation not permitted，
开了也成功不了。详见 AGENTS.md「铁律」第 4 条。
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
_MEMORY_DB = Path(os.path.expanduser(os.environ.get("MEMORY_DB", "~/.shared_memory")))
TASKS_JSON = _MEMORY_DB / "_tasks.json"
LOG_PATH = Path(os.path.expanduser("~/.engine-guardian/guardian.log"))

_stop = False


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def load_config():
    default = {
        "check_interval_sec": 30,
        "idle_minutes": 30,
        "auto_start": False,
        "services": [
            {
                "name": "示例服务",
                "port": 8000,
                "match": "python3 example_service.py",
                "start_cmd": None,
            }
        ],
    }
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # services 整体以用户配置为准(不能按下标 merge: 用户配 N 个、
            # 默认只有 1 个时, 第 2..N 个会被静默丢掉)。
            user_services = user.pop("services", None)
            default.update(user)
            if isinstance(user_services, list) and user_services:
                default["services"] = user_services
        except Exception as e:
            log("配置文件读取失败，用默认值：%s" % e)
    return default


def count_running_tasks():
    """台账里 status=running 的任务数。读不到时保守返回 0（视为空闲）。"""
    try:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0, 0
    except Exception as e:
        log("台账解析失败（本次按空闲处理）：%s" % e)
        return 0, 0
    tasks = data.get("tasks", data) if isinstance(data, dict) else data
    if isinstance(tasks, dict):
        tasks = list(tasks.values())
    if not isinstance(tasks, list):
        return 0, 0
    running = [t for t in tasks if isinstance(t, dict) and t.get("status") == "running"]
    return len(running), len(tasks)


def pids_of(pattern):
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10)
        return [p for p in out.stdout.split() if p.strip()]
    except Exception:
        return []


def port_alive(port):
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP", "LISTEN"],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def start_service(svc):
    cmd = svc.get("start_cmd")
    if not cmd:
        log("    %s 没有配置 start_cmd，跳过自动启动" % svc["name"])
        return False
    try:
        subprocess.Popen(cmd, shell=isinstance(cmd, str),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        log("    已执行 start_cmd：%s" % cmd)
        return True
    except Exception as e:
        log("    start_cmd 执行失败：%s" % e)
        return False


def stop_service(svc):
    pids = pids_of(svc["match"])
    if not pids:
        log("    %s 本来就没在跑" % svc["name"])
        return False
    log("    正在停止 %s：%s" % (svc["name"], " ".join(pids)))
    try:
        subprocess.run(["kill"] + pids, timeout=10)
    except Exception:
        pass
    time.sleep(2)
    left = pids_of(svc["match"])
    if left:
        try:
            subprocess.run(["kill", "-9"] + left, timeout=10)
            log("    强制结束：%s" % " ".join(left))
        except Exception:
            pass
    return True


def handle_signal(signum, frame):
    global _stop
    _stop = True
    log("收到信号 %d，准备退出" % signum)


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    cfg = load_config()
    interval = int(cfg["check_interval_sec"])
    idle_limit = int(cfg["idle_minutes"]) * 60
    auto_start = bool(cfg.get("auto_start"))
    services = cfg["services"]

    log("=" * 56)
    log("engine-guardian 启动")
    log("  检查间隔：%d 秒" % interval)
    log("  空闲关闭：%d 分钟" % cfg["idle_minutes"])
    log("  自动启动：%s" % ("开" if auto_start else "关"))
    log("  守护服务：%s" % "、".join(s["name"] for s in services))
    log("  台账路径：%s" % TASKS_JSON)

    idle = 0
    while not _stop:
        try:
            running, total = count_running_tasks()

            if running > 0:
                if idle:
                    log("检测到 %d 个运行中任务，重置空闲计时" % running)
                idle = 0

                # 空闲期间有人在干活 → 服务该在
                for svc in services:
                    if auto_start and not port_alive(svc["port"]):
                        log("  %s 未运行，但有活跃任务，尝试拉起" % svc["name"])
                        start_service(svc)
            else:
                idle += interval
                remain = idle_limit - idle
                if remain <= 0:
                    log("空闲累计 %d 分钟（台账 %d 个任务全部已完成），执行关闭"
                        % (cfg["idle_minutes"], total))
                    for svc in services:
                        stop_service(svc)
                    idle = 0
                elif idle % (interval * 4) == 0:
                    log("无运行中任务，已空闲 %d 分钟，还剩 %d 分钟关闭"
                        % (idle // 60, max(remain // 60, 0)))

        except Exception as e:
            log("循环异常（继续运行）：%s" % e)

        for _ in range(int(interval)):
            if _stop:
                break
            time.sleep(1)

    log("engine-guardian 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
