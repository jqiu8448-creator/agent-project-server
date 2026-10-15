# engine-guardian · 无活跃智能体时自动关闭服务

项目服务器上跑着的本地服务（代码托管、各类开发服务），用完忘了关就会一直占着端口。
这个守护器接管这件事：**任务台账里没有 `running` 任务、且持续空闲超过设定时间，自动把登记在册的服务关掉。**

管哪些服务由 `config.json` 的 `services[]` 决定，不是写死的——加服务只改配置。只依赖 Python 标准库。

## 判定「有没有活跃智能体」用的是什么

唯一可靠信号是共享记忆服务的**任务台账**：

```
~/.shared_memory/_tasks.json   （可用环境变量 MEMORY_DB 改目录）
```

里面 `status == "running"` 的任务数 > 0，就认为有人在干活。

```
running > 0   → 有人在干活，重置空闲计时
running == 0  → 累计空闲时间
累计 ≥ idle_minutes → 关闭服务
```

### 为什么不看日志 / 不看客户端列表

- **服务日志判不了**：日志级别调高后访问日志不写，mtime 完全不代表活跃度——实测日志几天没更新、服务却一直在被访问。
- **客户端名册判不了**：它只是名字注册表，**没有心跳时间戳**，分不出"三分钟前还在用"和"三个月前用过一次"。
- **台账能判**：框架的规矩是干活必走 `start_task → finish_task`，所以 running 任务数就是真实的工作面。

> 代价：如果有人不按规矩干活（不开任务就动手），守护器看不到他。
> 这是框架本身的纪律要求，不是守护器的缺陷。

## 配置

```bash
cp config.example.json config.json
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `idle_minutes` | 30 | 无 running 任务后累计多少分钟关闭 |
| `check_interval_sec` | 30 | 检查间隔 |
| `auto_start` | false | true = 有 running 任务但服务没跑时自动拉起 |
| `services[].name` | — | 日志里显示的名字 |
| `services[].port` | — | 服务端口（auto_start 时用来探活） |
| `services[].match` | — | `pgrep -f` 匹配串，**必须能唯一认出服务**，改前先 `pgrep -f "<串>"` 验证 |
| `services[].start_cmd` | — | `auto_start` 开启时用来拉起，否则填 `null` |

改完重启守护器生效。别把参数硬编进 `guardian.py`。

## 用法

双击或命令行执行：

- `start.command` — 前台跑，实时看日志
- `stop.command` — 停掉

前台跑也可以直接：

```bash
python3 guardian.py
```

### 开机自启（macOS launchd）

`launchd/` 下有 plist 模板，按文件头注释替换 label、路径和用户名后：

```bash
cp launchd/com.example.engine-guardian.plist.template \
   ~/Library/LaunchAgents/<你的label>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<你的label>.plist
```

日志在 `~/.engine-guardian/guardian.log`。

> LaunchAgent 进程没有 `~/Desktop` 访问权（`Operation not permitted`）。
> 守护器只读任务台账、不读桌面，所以常驻没问题；被守护的服务和 `start_cmd` 别放在桌面目录。

## 安全边界

- 唯一的破坏性操作是按 `match` 匹配进程后 `kill`。**match 写太宽会误杀无关进程**，
  这是配置时唯一需要格外小心的地方。
- 只读 `_tasks.json`，不改台账内容；不碰被守护服务的代码和数据。
