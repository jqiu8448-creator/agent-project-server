#!/bin/bash
# engine-guardian · 前台启动（能看到实时日志）
cd "$(dirname "$0")" || exit 1
[ -f config.json ] || { echo "还没有 config.json：先 cp config.example.json config.json 再改"; exit 1; }
mkdir -p ~/.engine-guardian
echo "守护器启动中，Ctrl+C 停止。"
echo "日志同时写入：~/.engine-guardian/guardian.log"
echo "---------------------------------------------"
exec /usr/bin/python3 guardian.py
