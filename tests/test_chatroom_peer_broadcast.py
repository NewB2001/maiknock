# -*- coding: utf-8 -*-
"""聊天室 · 对方 Bot 台词广播（2026-11-28 串台修复）离线单测。

运行：
    python test_chatroom_peer_broadcast.py

**这个 bug 的形状**：每个 Bot 实例只有自己群流的副本 —— 小樱回的话只进
小樱实例的库，雏田的 Planner **永远**看不见（不是「同轮并行看不见」，
是历史也完全没有）。修法：collector 收到本 Bot 回复后往账本排广播
delivery（peer.<bot_id>），由目标 Bot 自己的 worker 投递沉淀成历史。

本测试钉死：
① classify_message 认 peer. 前缀为 peer（不当 bot 认领）；
② 广播给房间其他 Bot、不含自己；
③ delivery 幂等（同 message_id 不重复排）；
④ 账本只写一条 peer 消息（前端不重复显示）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.collector import (  # noqa: E402
    ReplyCollector, classify_message, PEER_ACCOUNT_PREFIX,
)
from chatroom.ledger import ChatroomLedger  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def mk_msg(account: str, text: str) -> dict:
    return {
        "message_id": "m_" + account + "_1",
        "message_info": {"user_info": {
            "user_id": account, "user_nickname": "春野樱",
        }},
        "processed_plain_text": text,
        "is_command": 0, "is_notify": 0,
    }


async def main() -> int:
    print("== 1. classify_message 认 peer. ==")
    kind, acct, nick = classify_message(mk_msg("peer.ddf73be", "小樱说的话"))
    check("peer. → peer 类", kind == "peer", f"{kind}")
    check("昵称带出来", nick == "春野樱", f"{nick}")
    kind2, _, _ = classify_message(mk_msg("self", "雏田自己"))
    check("self 仍是 bot", kind2 == "bot", f"{kind2}")
    check("前缀常量", PEER_ACCOUNT_PREFIX == "peer.")

    print("== 2. 广播给房间其他 Bot ==")
    tmp = tempfile.mkdtemp(prefix="peer-bc-")
    led = ChatroomLedger(os.path.join(tmp, "t.db"), bot_id="7465370a")
    led.open()
    led.create_room("room1", "测试房间", group_id="webui_virtual_group_room1")
    # 三个 Bot 成员：自己 + 两个对方
    led.upsert_member({"member_id": "bot_hinata", "kind": "bot",
                       "display_name": "雏田", "bot_id": "7465370a", "account": "bot.7465370a"})
    led.upsert_member({"member_id": "bot_sakura", "kind": "bot",
                       "display_name": "小樱", "bot_id": "ddf73be", "account": "bot.ddf73be"})
    led.upsert_member({"member_id": "bot_ino", "kind": "bot",
                       "display_name": "井野", "bot_id": "111111", "account": "bot.111111"})
    for mid in ("bot_hinata", "bot_sakura", "bot_ino"):
        led.add_to_room("room1", mid)

    col = ReplyCollector(ctx=None, ledger=led, bot_id="7465370a")
    n = await col._broadcast_peer_reply(
        room_id="room1", message_id="m_reply_1",
        text="八成他去风俗店了", nickname="雏田",
    )
    check("广播给 2 个对方 Bot", n == 2, f"{n}")
    # 用 ledger 的只读连接
    with led.read() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM delivery WHERE delivery_id LIKE 'peer_m_reply_1%'"
        ).fetchall()]
    targets = {r["target_bot_id"] for r in rows}
    check("目标不含自己", "7465370a" not in targets, f"{targets}")
    check("目标含两个对方", targets == {"ddf73be", "111111"}, f"{targets}")
    check("身份是 peer. 前缀", all(r["sender_account"] == "peer.7465370a" for r in rows))
    check("turn_id 为空（不参与接话）", all(r["turn_id"] == "" for r in rows))
    check("昵称带过去", all(r["sender_nickname"] == "雏田" for r in rows))

    print("== 3. 幂等：重复广播不重复排 ==")
    n2 = await col._broadcast_peer_reply(
        room_id="room1", message_id="m_reply_1",
        text="八成他去风俗店了", nickname="雏田",
    )
    with led.read() as conn:
        cnt = conn.execute(
            "SELECT count(*) FROM delivery WHERE delivery_id LIKE 'peer_m_reply_1%'"
        ).fetchone()[0]
    check("重复广播排 0 条", n2 == 0, f"{n2}")
    check("账本仍只有 2 条", cnt == 2, f"{cnt}")

    print("== 4. 账本只写一条 peer 消息 ==")
    with led.read() as conn:
        msgs = conn.execute(
            "SELECT * FROM message WHERE message_id='peer_msg_m_reply_1'"
        ).fetchall()
    check("前端消息只有 1 条", len(msgs) == 1, f"{len(msgs)}")
    if msgs:
        check("kind=peer", msgs[0]["kind"] == "peer", f"{msgs[0]['kind']}")

    led.close()
    print("\n=== 全部通过 ===" if not FAILED else f"\n=== 失败 {len(FAILED)} 项 ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
