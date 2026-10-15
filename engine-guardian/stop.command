#!/bin/bash
# engine-guardian · 停止
# 若用 launchd 常驻，把下面的 label 换成你 plist 里写的那个
LABEL="com.example.engine-guardian"
pkill -f "guardian.py" 2>/dev/null && echo "守护器已停止" || echo "守护器本来就没在跑"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
