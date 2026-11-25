# -*- coding: utf-8 -*-
"""麦麦聊天室 · 「回到此处」离线单测（不依赖 MaiBot 运行）。

运行（必须用宿主解释器 3.13）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" test_chatroom_truncate.py

**这个测试在防什么**：
① `truncate_from` 必须**软作废**（`voided=1`），**不物理删**（FR-12）；
② 作废范围 = 段序号 ≥ 截断点（之前的段全保留）；
③ 段和消息**一起**作废（不能只灰段不灰消息）；
④ 页面时间线**带上作废段**（带 `voided` 标记）⇒ 前端能灰显（D9.3），
   而不是凭空消失；
⑤ 截断后新提交**续编**段号（旧段不重排，新段在作废段之后）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.ledger import ChatroomLedger  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="mk_trunc_")
    db = os.path.join(tmpdir, "t.db")
    led = ChatroomLedger(db, bot_id="bot_test")
    try:
        led.open()
        # 建 3 段，每段 1 条消息
        for i in range(1, 4):
            led.append_segment(
                "room_1", f"seg_{i}", "normal",
                [{
                    "message_id": f"m_{i}",
                    "speaker_member_id": "role_a",
                    "speaker_name": "鸣人",
                    "kind": "line",
                    "content": f"第{i}句",
                    "created_at": f"2026-01-01 00:0{i}:00",
                }],
            )

        print("== 1. 截断前：3 段都在、都没作废 ==")
        tl = led.list_timeline("room_1")
        check("3 段", len(tl) == 3, f"实际 {len(tl)}")
        check("都没作废", all(not s["voided"] for s in tl))

        print("== 2. ★ 从第 2 段截断 ⇒ 只软作废 seq>=2，不物理删 ==")
        n = led.truncate_from("room_1", 2)
        check("作废了 2 段", n == 2, f"实际 {n}")

        # 物理行还在（软作废，不删）
        with led.read() as conn:
            seg_total = conn.execute("SELECT COUNT(*) FROM segment WHERE room_id='room_1'").fetchone()[0]
            seg_voided = conn.execute("SELECT COUNT(*) FROM segment WHERE room_id='room_1' AND voided=1").fetchone()[0]
            msg_total = conn.execute("SELECT COUNT(*) FROM message WHERE room_id='room_1'").fetchone()[0]
        check("★ 物理上仍是 3 段（没删）", seg_total == 3, f"实际 {seg_total}")
        check("其中 2 段被标作废", seg_voided == 2, f"实际 {seg_voided}")
        check("★ 物理上仍是 3 条消息（没删）", msg_total == 3, f"实际 {msg_total}")

        print("== 3. 页面时间线带上作废段 + voided 标记（供前端灰显）==")
        tl = led.list_timeline("room_1")
        check("仍是 3 段（作废的也在，供灰显）", len(tl) == 3, f"实际 {len(tl)}")
        flags = [s["voided"] for s in sorted(tl, key=lambda s: s["seq"])]
        check("★ 作废标记正确（第1段保留、第2/3段作废）", flags == [False, True, True], f"实际 {flags}")
        # seq=2 那段的消息也要在（灰显要用）
        seg2 = [s for s in tl if s["seq"] == 2][0]
        check("作废段的消息也在（不丢内容）", len(seg2["messages"]) == 1, f"实际 {seg2['messages']}")

        print("== 4. 消息也一起作废（段级联）==")
        with led.read() as conn:
            m2 = conn.execute("SELECT voided FROM message WHERE message_id='m_2'").fetchone()[0]
        check("第2段的消息被标作废", int(m2) == 1, f"实际 {m2}")

        print("== 5. ★ 截断后新提交续编段号（不重排、新段不作废）==")
        new_seg = led.append_segment(
            "room_1", "seg_4", "normal",
            [{"message_id": "m_4", "speaker_member_id": "role_a",
              "speaker_name": "鸣人", "kind": "line", "content": "重演后的新内容",
              "created_at": "2026-01-01 00:09:00"}],
        )
        check("新段号 = 4（续编，不重排）", new_seg["seq"] == 4, f"实际 {new_seg['seq']}")
        check("新段没被作废", int(new_seg["voided"]) == 0)
        tl = led.list_timeline("room_1")
        check("时间线现在 4 段", len(tl) == 4, f"实际 {len(tl)}")
    finally:
        try:
            led.close()
        except Exception:
            pass

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
