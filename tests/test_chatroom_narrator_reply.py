# -*- coding: utf-8 -*-
"""麦麦聊天室 · 时光机旁白走「默认接话」机制（2026-11-27 用户拍板）离线单测。

运行：
    python test_chatroom_narrator_reply.py

**在防什么**（这次修的 bug：时光机生成旁白后 Bot 全哑巴）：
① 旁白**建 turn + 带 wave** ⇒ worker 认 is_line=True ⇒ 递话前开阀
   （旧实现 turn_id="" ⇒ 不开阀 ⇒ 宿主频率 0 时旁白被静默吞掉）；
② 旁白走**默认语义**：无勾选 ⇒ 5% 沉默掷骰 + 阵型随机（跟剧本窗口啥也不勾同款）；
③ 旁白**不带 @**（它是「我的角色」发的寻常消息，不是点名）；
④ 旁白段类型仍是 plot、发言人仍是固定身份 g1_narrator（不裂名片）；
⑤ 超窗按 timeout 收口 ⇒ 进 collector 的 30 分钟宽限期（迟到回复不丢）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom import formation  # noqa: E402
from chatroom.ledger import ChatroomLedger  # noqa: E402
from chatroom.mock_data import NARRATOR  # noqa: E402
from chatroom.narrator import build_narrator_texts  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


async def main() -> int:
    print("== 1. 默认语义：5% 沉默掷骰 + 阵型随机 ==")
    seed = 1795702595387
    bot_ids = ["bot_hinata", "bot_sakura", "bot_shikamaru"]
    rng = __import__("random").Random(seed)
    kept = formation.apply_silence_roll(rng, bot_ids)
    plan = formation.plan_formation(seed, kept, [])
    check("无勾选 ⇒ strategy=random", plan.strategy == formation.STRATEGY_RANDOM,
          plan.strategy)
    check("无勾选 ⇒ named 为空（不带 @）", plan.named == [], f"{plan.named}")
    check("留在阵型里的才接话", set(plan.speakers) == set(kept), f"{plan.speakers}")
    check("seed 可复现", formation.plan_formation(seed, kept, []).to_dict() == plan.to_dict())

    print("== 2. 旁白文本拼装不变（固定身份 / 相对时间 / 不改写正文）==")
    texts = build_narrator_texts(["木叶村的集会广场上……", "谁都没说话"], 3, "日")
    check("相对时间并进第一条", texts[0].startswith("三天后，"), texts[0])
    check("第二条原样", texts[1] == "谁都没说话", texts[1])
    check("固定身份账号 g1_narrator", NARRATOR["account"] == "g1_narrator")

    print("== 3. 建轮 + 带 wave 的投递 ⇒ worker 认在途、开阀 ==")
    with tempfile.TemporaryDirectory() as tmp:
        led = ChatroomLedger(os.path.join(tmp, "t.db"), bot_id="bot_v")
        led.open()
        room_id = "room_tangwu"
        group_id = "webui_virtual_group_" + room_id
        segment_id = "seg_narr_test"
        turn_id = "turn_" + segment_id
        led.append_segment(room_id, segment_id, "plot", [
            {
                "message_id": segment_id + "_n0",
                "speaker_member_id": NARRATOR["member_id"],
                "speaker_name": NARRATOR["display_name"],
                "kind": "narrator",
                "content": texts[0],
                "created_at": "2026-11-26 22:16:35",
            }
        ])
        led.save_turn({
            "turn_id": turn_id, "room_id": room_id, "segment_id": segment_id,
            "seed": seed, "strategy": plan.strategy, "formation": plan.formation,
            "plan": plan.to_dict(), "state": "open",
            "created_at": "2026-11-26 22:16:35",
        })
        for i, bot in enumerate(["bot_hinata", "bot_sakura"]):
            led.enqueue_delivery(
                f"{segment_id}_d{i}", "bot_v", texts[0], room_id=room_id,
                segment_id=segment_id, sender_member_id=NARRATOR["member_id"],
                sender_platform=NARRATOR["platform"], sender_account=NARRATOR["account"],
                sender_nickname=NARRATOR["display_name"],
                group_id=group_id, seq=i, turn_id=turn_id, wave=0,
            )
        rows = led.turn_deliveries(turn_id)
        check("旁白投递带 turn_id", all(r["turn_id"] == turn_id for r in rows), f"{rows}")
        check("旁白投递带 wave", all(r["wave"] == 0 for r in rows))
        check("★ 旁白算在途 ⇒ sweep 保持开阀",
              group_id in led.inflight_valve_groups("bot_v"),
              f"{led.inflight_valve_groups('bot_v')}")
        check("段类型仍是 plot", led.get_segment(segment_id)["kind"] == "plot")

        print("== 4. 超窗按 timeout ⇒ 进 30 分钟宽限期 ==")
        for r in rows:
            led.claim_delivery(r["delivery_id"], "bot_v")
        # delivered_at 保持「刚刚」（claim 时写入），older_than_seconds=0 ⇒ 立刻超窗；
        # 但仍在 30 分钟宽限期内 ⇒ 迟到回复能被 collector 看到。
        n = led.timeout_stale_deliveries("bot_v", older_than_seconds=0.0)
        check("旁白超窗记 timeout", n == 2, f"实际 {n}")
        grace = led.recent_collect_targets("bot_v", within_seconds=1800.0)
        check("★ 迟到的旁白回复仍在宽限期里", len(grace) == 2, f"实际 {len(grace)}")

    print("\n=== 全部通过 ===" if not FAILED else f"\n=== 失败 {len(FAILED)} 项 ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
