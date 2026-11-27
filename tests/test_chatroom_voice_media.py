# -*- coding: utf-8 -*-
"""聊天室 · 图片 + 语音（2026-11-28）离线单测。

运行：
    python test_chatroom_voice_media.py

钉死：
① 图片（is_picture=1）走 emoji_data_uri 提取，跟表情包同款；
② 语音 base64 / hash 兜底提取；
③ voice_blob 存取 + attach_voice + timeline 带 voice_id；
④ 音频不进 timeline（只带 id，按需 API 取）。
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.collector import emoji_data_uri, voice_data_uri  # noqa: E402
from chatroom.ledger import ChatroomLedger  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def main() -> int:
    print("== 1. 图片提取（跟表情包同款）==")
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 20).decode()
    pic_msg = {"is_picture": 1, "is_emoji": 0,
               "raw_message": [{"type": "image", "binary_data_base64": png_b64}]}
    check("图片提取成 PNG data URI",
          (emoji_data_uri(pic_msg) or "").startswith("data:image/png;"),
          f"{str(emoji_data_uri(pic_msg))[:40]}")
    check("普通文本消息不提取", emoji_data_uri({"raw_message": [{"type": "text", "data": "x"}]}) is None)

    print("== 2. 语音提取 ==")
    mp3_b64 = base64.b64encode(b"ID3" + b"x" * 100).decode()
    voice_msg = {"raw_message": [{"type": "voice", "hash": "h1",
                                  "binary_data_base64": mp3_b64}]}
    check("语音 base64 → audio/mpeg",
          (voice_data_uri(voice_msg) or "").startswith("data:audio/mpeg;"),
          f"{str(voice_data_uri(voice_msg))[:40]}")
    check("没有语音组件 ⇒ None", voice_data_uri({"raw_message": [{"type": "text", "data": "x"}]}) is None)

    print("== 3. 语音 hash 兜底（二进制被清空）==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        vdir = root / "data" / "voices"
        vdir.mkdir(parents=True)
        (vdir / "abc123.mp3").write_bytes(b"ID3" + b"y" * 50)
        msg = {"raw_message": [{"type": "voice", "hash": "abc123", "data": "[语音: 你好]"}]}
        uri = voice_data_uri(msg, root)
        check("按 hash 找到语音文件", bool(uri and uri.startswith("data:audio/mpeg;")), f"{str(uri)[:40]}")
        check("错 hash ⇒ None", voice_data_uri({"raw_message": [{"type": "voice", "hash": "nope"}]}, root) is None)

        print("== 4. 账本 voice_blob + 按需取 ==")
        db = os.path.join(tmp, "t.db")
        led = ChatroomLedger(db, bot_id="bot1")
        led.open()
        check("save_voice 新增", led.save_voice("voice_m1", uri or ""))
        check("重复 save 不覆盖", not led.save_voice("voice_m1", "data:audio/mpeg;base64,OTHER"))
        check("get_voice 取回原文", led.get_voice("voice_m1") == uri)
        check("不存在的 id ⇒ None", led.get_voice("nope") is None)

        led.append_message({
            "message_id": "m1", "segment_id": "seg", "room_id": "r",
            "speaker_name": "雏田", "kind": "reply",
            "content": "[语音: 你好]", "quote": "", "created_at": "2026-11-28 04:00:00",
        })
        led.attach_voice("m1", "voice_m1")
        with led.read() as conn:
            row = conn.execute("SELECT voice_id FROM message WHERE message_id='m1'").fetchone()
        check("attach_voice 挂上 id", row and row["voice_id"] == "voice_m1")

        # timeline 只带 voice_id，不带音频本体（防撑爆 bootstrap）
        with led.read() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
        check("voice_id 列存在", "voice_id" in cols)
        led.close()

    print("\n=== 全部通过 ===" if not FAILED else f"\n=== 失败 {len(FAILED)} 项 ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
