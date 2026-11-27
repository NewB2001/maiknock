# -*- coding: utf-8 -*-
"""聊天室 · Bot 头像读取与共享（2026-11-28）离线单测。

运行：
    python test_chatroom_botavatar.py

**这个 bug 的形状**：宿主 WebUI 给 Bot 设的头像，聊天室里显示默认 emoji。
根因：头像 user_id 是 `webui_bot_<crypto.randomUUID()>`，uuid 只在设置它的
浏览器 localStorage 里 ⇒ 插件无法从 bot_id 推导 URL。
修法：扫 `<实例根>/data/avatar/internal/webui_bot_*.png` → data URI →
写进共享账本 bot_status.avatar ⇒ 中控读全部 ⇒ 前端 fillAvatar 显示。

钉死：
① 只认 webui_bot_* 前缀（不认 owner.png / 普通用户头像）；
② 多个候选取最近修改；
③ data URI 格式正确；过大/空文件跳过；
④ 账本迁移（老库 bot_status 无 avatar 列也能用）；
⑤ 传空 avatar 不覆盖已有值。
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom import botavatar  # noqa: E402
from chatroom.ledger import ChatroomLedger  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def main() -> int:
    print("== 1. 扫目录：只认 webui_bot_* ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        adir = root / "data" / "avatar" / "internal"
        adir.mkdir(parents=True)
        # 干扰文件：owner.png / 普通用户头像
        (adir / "owner.png").write_bytes(b"not a bot avatar")
        (adir / "webui_alice.png").write_bytes(b"user avatar")
        check("只有干扰文件时找不到", botavatar.find_bot_avatar_path(root) is None)

        # 用 Pillow 造一张**真** PNG（read_avatar_data_uri 要缩图，假字节会被 Pillow 拒绝）
        from PIL import Image
        import io as _io
        _buf = _io.BytesIO()
        Image.new("RGB", (200, 200), (200, 100, 150)).save(_buf, format="PNG")
        bot = adir / "webui_bot_abc-123.png"
        bot.write_bytes(_buf.getvalue())
        found = botavatar.find_bot_avatar_path(root)
        check("找到 Bot 头像", found is not None and found.name == bot.name, f"{found}")

        # 多个候选取最近修改
        old = adir / "webui_bot_old.png"
        old.write_bytes(b"old")
        os.utime(str(old), (time.time() - 100, time.time() - 100))
        found2 = botavatar.find_bot_avatar_path(root)
        check("多个候选取最近修改", found2 is not None and found2.name == bot.name, f"{found2}")

        print("== 2. data URI（缩图后 JPEG）==")
        uri = botavatar.read_avatar_data_uri(bot)
        check("data:image/jpeg;base64 前缀", bool(uri and uri.startswith("data:image/jpeg;base64,")), f"{str(uri)[:40]}")
        if uri:
            raw = base64.b64decode(uri.split(",", 1)[1])
            check("解回的是合法 JPEG", raw[:3] == b"\xff\xd8\xff", f"{raw[:4]!r}")
            from PIL import Image
            import io
            with Image.open(io.BytesIO(raw)) as im:
                check("缩到 <=96x96", max(im.size) <= 96, f"{im.size}")

        # 空文件 / 不存在
        empty = adir / "webui_bot_empty.png"
        empty.write_bytes(b"")
        check("空文件跳过", botavatar.read_avatar_data_uri(empty) is None)
        check("不存在文件跳过", botavatar.read_avatar_data_uri(adir / "no.png") is None)

        print("== 3. 账本：迁移 + 不覆盖 ==")
        # 老库：先建一个没有 avatar 列的 bot_status 表
        db = os.path.join(tmp, "old.db")
        import sqlite3
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE bot_status (bot_id TEXT PRIMARY KEY, name TEXT DEFAULT '',"
                  " status TEXT DEFAULT '', detail TEXT DEFAULT '', handshake_ok INTEGER,"
                  " style_fixed INTEGER, updated_at TEXT DEFAULT '')")
        c.commit(); c.close()
        led = ChatroomLedger(db, bot_id="bot1")
        led.open()
        with led.read() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_status)")}
        check("老库自动补 avatar 列", "avatar" in cols, f"{cols}")

        led.set_bot_status("bot1", "ready", name="雏田", avatar="data:image/png;base64,AAA")
        s = led.get_bot_status("bot1")
        check("头像写进去", bool(s and s.get("avatar") == "data:image/png;base64,AAA"), f"{s}")

        led.set_bot_status("bot1", "ready", name="雏田", avatar="")  # 本次没读到
        s2 = led.get_bot_status("bot1")
        check("★ 传空不覆盖已有头像", bool(s2 and s2.get("avatar") == "data:image/png;base64,AAA"), f"{s2}")

        led.set_bot_status("bot1", "ready", name="雏田", avatar="data:image/png;base64,BBB")
        s3 = led.get_bot_status("bot1")
        check("传新值会覆盖", bool(s3 and s3.get("avatar") == "data:image/png;base64,BBB"), f"{s3}")
        led.close()

    print("\n=== 全部通过 ===" if not FAILED else f"\n=== 失败 {len(FAILED)} 项 ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
