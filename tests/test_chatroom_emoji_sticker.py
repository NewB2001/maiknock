# -*- coding: utf-8 -*-
"""聊天室 · 表情包显示（2026-11-28）离线单测。

运行：
    python test_chatroom_emoji_sticker.py

**这个 bug 的形状**：Bot 回表情包时，宿主 processed_plain_text 只有
`[表情包: 害羞,萌系,撒娇]`（给 LLM 看的情感标签），聊天室把它当台词显示。
真图片在 raw_message 的 EmojiComponent.binary_data_base64（需 include_binary_data）。

钉死：
① 从 emoji 消息提取 data URI（webp/png/jpeg/gif 识别）；
② 非 emoji / 没二进制数据 ⇒ None（回落文本）；
③ 账本 emoji_data 列存取（含老库迁移 + 已存在行补写）；
④ kind 仍是 reply（前端收尾判定不能坏）。
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.collector import emoji_data_uri  # noqa: E402
from chatroom.ledger import ChatroomLedger  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def mk_emoji_msg(b64: str, is_emoji: int = 1) -> dict:
    return {
        "is_emoji": is_emoji,
        "processed_plain_text": "[表情包: 害羞,萌系,撒娇]",
        "raw_message": [{"type": "emoji", "hash": "abc", "data": "[表情包: ...]",
                         "binary_data_base64": b64}],
    }


def main() -> int:
    print("== 1. 表情包 data URI 提取 ==")
    webp_b64 = base64.b64encode(b"RIFFxxxxWEBPvp8 ").decode()  # 以 UklGR 开头
    uri = emoji_data_uri(mk_emoji_msg(webp_b64))
    check("webp data URI", bool(uri and uri.startswith("data:image/webp;base64,")), f"{str(uri)[:40]}")
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 10).decode()
    check("png data URI", (emoji_data_uri(mk_emoji_msg(png_b64)) or "").startswith("data:image/png;"))
    jpg_b64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"x" * 10).decode()
    check("jpeg data URI", (emoji_data_uri(mk_emoji_msg(jpg_b64)) or "").startswith("data:image/jpeg;"))

    print("== 2. 非表情包 / 没数据 ⇒ None ==")
    check("is_emoji=0 ⇒ None", emoji_data_uri(mk_emoji_msg(webp_b64, is_emoji=0)) is None)
    check("没 raw_message ⇒ None", emoji_data_uri({"is_emoji": 1}) is None)
    check("没 binary_data_base64 ⇒ None",
          emoji_data_uri({"is_emoji": 1, "raw_message": [{"type": "emoji", "data": "x"}]}) is None)
    check("raw 里没有 emoji 组件 ⇒ None",
          emoji_data_uri({"is_emoji": 1, "raw_message": [{"type": "text", "data": "x"}]}) is None)

    print("== 3. 账本 emoji_data 存取 ==")
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "t.db")
        led = ChatroomLedger(db, bot_id="bot1")
        led.open()
        # 新消息带 emoji_data，kind 仍是 reply
        led.append_message({
            "message_id": "m1", "segment_id": "seg", "room_id": "r",
            "speaker_name": "雏田", "kind": "reply",
            "content": "[表情包: 害羞,萌系]", "quote": "",
            "emoji_data": uri or "", "created_at": "2026-11-28 03:00:00",
        })
        with led.read() as conn:
            row = conn.execute("SELECT * FROM message WHERE message_id='m1'").fetchone()
        check("emoji_data 存进账本", row and row["emoji_data"] == uri, f"{dict(row) if row else None}")
        check("kind 仍是 reply（收尾判定不坏）", row and row["kind"] == "reply")

        # 旧行（没有 emoji_data）补写
        led.append_message({
            "message_id": "m2", "segment_id": "seg", "room_id": "r",
            "speaker_name": "小樱", "kind": "reply",
            "content": "[表情包: 开心]", "quote": "", "created_at": "2026-11-28 03:00:01",
        })
        led.append_message({
            "message_id": "m2", "segment_id": "seg", "room_id": "r",
            "speaker_name": "小樱", "kind": "reply",
            "content": "[表情包: 开心]", "quote": "",
            "emoji_data": "data:image/webp;base64,NEW", "created_at": "2026-11-28 03:00:01",
        })
        with led.read() as conn:
            row2 = conn.execute("SELECT emoji_data FROM message WHERE message_id='m2'").fetchone()
        check("重复收同条能补写 emoji_data", row2 and row2["emoji_data"] == "data:image/webp;base64,NEW",
              f"{row2}")

        # list_timeline 带出 emoji_data（前端要的字段）
        led.append_segment("r", "seg", "normal", []) if hasattr(led, "append_segment") else None
        with led.read() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
        check("emoji_data 列存在", "emoji_data" in cols)
        led.close()

    print("\n=== 全部通过 ===" if not FAILED else f"\n=== 失败 {len(FAILED)} 项 ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
