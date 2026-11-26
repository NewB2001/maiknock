# -*- coding: utf-8 -*-
"""麦麦聊天室 · 网关连接生命周期离线单测（不依赖 MaiBot 运行 / 不联网）。

运行（沙箱或宿主解释器都行）：
    python test_chatroom_gateway_leak.py

**这个测试在防什么**（测试报告 V1.0 问题 B）：
断线 / 重连路径只把 ``_connected`` 置 False、**不关闭旧 websocket**
⇒ 网络抖动频繁时旧连接一条条攒下来（fd 泄漏）。

① 发送失败 ⇒ 旧连接被 close、置空；
② 心跳失败 ⇒ 同上；
③ close() 自己抛错也不外抛（不能把投递循环带崩）；
④ connect() 覆盖旧连接**之前**先 close 旧的；
⑤ 正常发送**不**误关连接。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.gateway import MaimConnection  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


class FakeWS:
    """假的 websocket：可配置 send/ping/close 是否抛错，记录 close 次数。"""

    def __init__(
        self,
        *,
        send_fails: bool = False,
        ping_fails: bool = False,
        close_raises: bool = False,
    ) -> None:
        self.send_fails = send_fails
        self.ping_fails = ping_fails
        self.close_raises = close_raises
        self.closed = 0
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        if self.send_fails:
            raise RuntimeError("socket gone")
        self.sent.append(data)

    async def ping(self) -> "asyncio.Future":
        if self.ping_fails:
            raise RuntimeError("ping failed")
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut

    async def close(self) -> None:
        self.closed += 1
        if self.close_raises:
            raise RuntimeError("close failed")


async def main_async() -> None:
    print("== 1. 发送失败 ⇒ 关闭并丢弃旧连接 ==")
    conn = MaimConnection("bot_x", "key")
    fake = FakeWS(send_fails=True)
    conn._ws = fake
    conn._connected = True
    ok = await conn.send_text("你好", user_id="user.naruto", group_id="g")
    check("发送失败返回 False", ok is False)
    check("★ 旧连接被 close（发送失败）", fake.closed == 1, f"实际 {fake.closed}")
    check("旧连接已置空", conn._ws is None)
    check("连接状态置 False", conn.connected is False)

    print("== 2. 心跳失败 ⇒ 关闭并丢弃旧连接 ==")
    conn = MaimConnection("bot_x", "key")
    fake = FakeWS(ping_fails=True)
    conn._ws = fake
    conn._connected = True
    ok = await conn.ping_once()
    check("心跳失败返回 False", ok is False)
    check("★ 旧连接被 close（心跳失败）", fake.closed == 1, f"实际 {fake.closed}")
    check("连接已置空", conn._ws is None)

    print("== 3. close() 自己抛错也不外抛 ==")
    conn = MaimConnection("bot_x", "key")
    fake = FakeWS(close_raises=True)
    conn._ws = fake
    conn._connected = True
    await conn._drop_ws()
    check("close 抛错也能收口", conn._ws is None and conn.connected is False)

    print("== 4. ★ 重连前先关旧连接（connect 覆盖前）==")
    new_ws = FakeWS()
    prev_mod = sys.modules.get("websockets")
    fake_mod = types.ModuleType("websockets")

    async def fake_connect(*args, **kwargs):
        return new_ws

    fake_mod.connect = fake_connect  # type: ignore[attr-defined]
    sys.modules["websockets"] = fake_mod
    try:
        conn = MaimConnection("bot_x", "key")
        old = FakeWS()
        conn._ws = old
        conn._connected = False       # 断线留下的旧连接
        ok = await conn.connect()
    finally:
        if prev_mod is None:
            sys.modules.pop("websockets", None)
        else:
            sys.modules["websockets"] = prev_mod
    check("重连成功", ok is True)
    check("★ 重连前 close 掉旧连接", old.closed == 1, f"实际 {old.closed}")
    check("新连接就位", conn._ws is new_ws and conn.connected is True)

    print("== 5. 正常发送不误关连接 ==")
    conn = MaimConnection("bot_x", "key")
    fake = FakeWS()
    conn._ws = fake
    conn._connected = True
    ok = await conn.send_text("在吗", user_id="user.naruto", group_id="g")
    check("发送成功", ok is True and fake.closed == 0)
    check("信封进了 send", len(fake.sent) == 1, f"实际 {len(fake.sent)}")


def main() -> int:
    asyncio.run(main_async())
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
