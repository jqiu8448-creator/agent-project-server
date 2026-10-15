"""检查"某个客户端配的身份对不对" —— 从任意 HTTP 客户端发起真实 MCP 调用。

用法:
    python check_identity.py role-dev        # 模拟某个客户端带头
    python check_identity.py                 # 不带头(看回落成什么样)

地址取环境变量 MEMORY_URL，默认 http://127.0.0.1:8000/mcp。

为什么需要这个脚本:
    身份配错的时候, **症状全发生在别处** —— 任务被拒、门禁失效、记到别人账上,
    没有一条会提示"你的身份不对"。而 whoami 是新工具, 老会话的工具列表里还没有。
    这个脚本不依赖会话, 拿一个 key 直接问服务端"你认出的我是谁"。

它同时是**验收工具**: 你给某个客户端配完 headers, 跑一遍这个脚本(填同样的 key),
看到"身份来源 = HTTP 头 X-Agent-Key"就说明配对了。
"""
import asyncio
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8000/mcp")


async def probe(agent_key: str | None):
    headers = {"X-Agent-Key": agent_key} if agent_key else {}
    hc = httpx.AsyncClient(headers=headers, timeout=30)
    async with streamable_http_client(URL, http_client=hc) as tup:
        r, w = tup[0], tup[1]
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print(f"服务: {init.serverInfo.name} {init.serverInfo.version}  协议 {init.protocolVersion}")
            print(f"带头: {headers or '(无)'}\n")
            res = await s.call_tool("whoami", {})
            print(res.content[0].text)


async def main():
    key = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else None
    await probe(key)


if __name__ == "__main__":
    asyncio.run(main())
