# Gogs 智能体建仓话术

给任何 AI 智能体复制粘贴用（当它加载不了 `skills/gogs-onboarding` 时）。
把 `<...>` 占位全部换成你自己的值。

---

## 一、你需要先做一次：生成访问令牌

只做一次，之后所有智能体共用这一个令牌。

**方式 A（推荐，走网页）**

打开 `http://localhost:3000` → 右上角头像 → **个人设置** → **应用** → **生成新令牌**
→ 名称随便填（如 `agents`）→ 直接点**生成令牌** → 立刻复制（只显示一次）。

**注意：Gogs 的令牌没有分级权限。** 页面上会写「任何令牌都对您的帐户拥有完整的操作权限」——
这是事实，不是省略。所有令牌天生全权限，**没有"全部授权"复选框可勾**，找那个是浪费时间。

**方式 B（走命令行）**

```bash
curl -s -X POST "http://localhost:3000/api/v1/users/<你的用户名>/tokens" \
  -u "<你的用户名>:<你的登录密码>" \
  -H "Content-Type: application/json" \
  -d '{"name":"agents"}'
```

返回 JSON 里的 `sha1` 字段就是令牌。

---

## 二、通用话术（贴给智能体）

```
我有一个自托管的 Gogs 实例，需要你在上面建仓库并推送代码。

【环境信息】
- Gogs 地址：http://localhost:3000  （只监听本机，局域网无法访问）
- 用户名：<你的用户名>
- 访问令牌：<在这里粘贴你的令牌>

【重要环境事实，别踩坑】
1. 必须用 localhost，不要用 127.0.0.1。
   Gogs 的 _csrf Cookie 绑死在 Domain=localhost，用 IP 访问 Cookie 带不过去，
   登录和建仓都会失败，而且报错长得像「密码不对」，极具误导性。
2. curl/git 未必读桌面系统的代理设置；访问 localhost 不需要代理，
   访问外网慢时再显式设 http_proxy/https_proxy。
3. 令牌属于凭据，不要打印到日志、不要写进代码文件、不要提交进 git。
4. 推送认证用 git -c http.extraHeader，别把令牌拼进 remote URL（会残留在 .git/config）。

【建仓库】执行下面这条（把字段换掉）：
curl -s -X POST "http://localhost:3000/api/v1/user/repos" \
  -H "Authorization: token <你的令牌>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "仓库名-只能用ASCII字母数字连字符",
    "description": "一句话描述",
    "private": true,
    "auto_init": false,
    "default_branch": "main"
  }'

# auto_init 用 false：部分 Gogs 版本 auto_init:true 会 500；
# 建完在本地 git init + 首次提交推上去即可。
# HTTP 状态码：201 成功，422 参数不合法；重名在某些版本返回 500 而非 409，
# 所以建之前先 GET 探活。

【推送已有代码】不要把令牌写进 remote URL，改用 extraHeader：
AUTH=$(printf '<你的用户名>:<你的令牌>' | base64)
cd <你的项目目录>
git init -b main   # 如果还不是 git 仓库
git remote add gogs http://localhost:3000/<你的用户名>/<仓库名>.git
git -c http.extraHeader="Authorization: Basic $AUTH" push -u gogs main

【验证】建完仓库后确认一下：
curl -s -H "Authorization: token <你的令牌>" \
  http://localhost:3000/api/v1/repos/<你的用户名>/<仓库名>

【如果 401】令牌错了或没生效。重新生成，注意不要有多余空格或换行。

【如果连不上】先自查 Gogs 是否还活着：
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/v1/repos/search
返回 000 说明服务没起来。若部署了 engine-guardian，服务可能因空闲被自动关闭，
重新启动即可。

【约定】建仓库前先跟我确认仓库名和是否设为私有，别自作主张建一堆。
```

---

## 三、极简话术（只建一个空仓）

```
在我的 Gogs（http://localhost:3000，用户名 <你的用户名>，令牌 <粘贴>）上建一个私有仓库，
名字叫 <仓库名>，描述写「<描述>」，默认分支 main，用 API 建：

curl -s -X POST "http://localhost:3000/api/v1/user/repos" \
  -H "Authorization: token <粘贴>" -H "Content-Type: application/json" \
  -d '{"name":"<仓库名>","description":"<描述>","private":true,"auto_init":false,"default_branch":"main"}'

建完把返回的 clone_url 和网页地址给我。
```

---

## 四、常用 API 一览

| 目的 | 方法与路径 |
|---|---|
| 建仓库 | `POST /api/v1/user/repos` |
| 查仓库 | `GET /api/v1/repos/<用户>/<仓库名>` |
| 列我的仓库 | `GET /api/v1/user/repos` |
| 删仓库 | `DELETE /api/v1/repos/<用户>/<仓库名>` |
| 建分支 | `POST /api/v1/repos/<用户>/<仓库名>/branches` |
| 建 Issue | `POST /api/v1/repos/<用户>/<仓库名>/issues` |
| 读文件 | `GET /api/v1/repos/<用户>/<仓库名>/raw/<分支>/<路径>` |
| 搜公开仓库 | `GET /api/v1/repos/search?q=<关键词>` |

除 `repos/search` 外都需要 `Authorization: token <令牌>` 头。

---

## 五、给你的备忘

- 令牌**只放环境变量**，别写进任何文件：

  ```bash
  export GOGS_BASE='http://localhost:3000'
  export GOGS_USER='<你的用户名>'
  export GOGS_TOKEN='你的令牌'
  ```

- 更省事的做法是直接跑 `scripts/new_project.py`（建仓 + AGENTS.md + 首推 + 记忆登记一把完成），
  见仓库根 `AGENTS.md` 第 4 节。
- 空闲自动关闭由 `engine-guardian/` 负责；需要开机自启，参考
  `engine-guardian/launchd/` 下的 plist 模板。
