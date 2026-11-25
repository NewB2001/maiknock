# -*- coding: utf-8 -*-
"""麦麦聊天室 · 阵型调度离线单测（不依赖 MaiBot 运行）。

运行（必须用宿主解释器 3.13）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" test_chatroom_formation.py

**这个测试在防什么**：
① 三种阵型都要对：串行（每波一个）/ 并行（全在一波）/ 复合（波内并行、波间串行）；
② **同一 seed 必得同一计划**（FR-05/FR-14：重演可复现）；
③ `plan_json` **存/取对称**（别重蹈 config_json 每轮翻倍的覆辙）；
④ **波次门控**：上一波没说完，下一波的单不能被领走（并行不能退化成串行，
   串行不能退化成并行）；
⑤ **接力**：先答 Bot 的回复要进后答 Bot 的投递正文（各群流是独立副本，
   不接力后答的就看不见）；
⑥ **超时兜底**：递出去一直不回要能标超时，否则门控卡死整轮。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom import formation  # noqa: E402
from chatroom.ledger import ChatroomLedger  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def test_planning() -> None:
    print("== 1. 显式点名 ⇒ 导演制（按勾选顺序全串行）==")
    p = formation.plan_formation(
        12345, ["bot_a", "bot_b", "bot_c"], named=["bot_c", "bot_a"]
    )
    check("strategy=explicit", p.strategy == "explicit")
    check("阵型=serial", p.formation == "serial")
    check("顺序按点名不变", p.speakers == ["bot_c", "bot_a"], f"实际 {p.speakers}")
    check("每波一个", p.waves == [["bot_c"], ["bot_a"]], f"实际 {p.waves}")

    print("== 2. 并行 ⇒ 全在一波 ==")
    p = formation.plan_formation(1, ["a", "b", "c"], formation="parallel")
    check("阵型=parallel", p.formation == "parallel")
    check("一波装下所有", len(p.waves) == 1 and sorted(p.waves[0]) == ["a", "b", "c"],
          f"实际 {p.waves}")

    print("== 3. 串行 ⇒ 每个一波 ==")
    p = formation.plan_formation(1, ["a", "b", "c"], formation="serial")
    check("阵型=serial", p.formation == "serial")
    check("波数=人数", len(p.waves) == 3, f"实际 {p.waves}")
    check("每个波只有一个", all(len(w) == 1 for w in p.waves), f"实际 {p.waves}")
    check("全员都在", sorted(p.speakers) == ["a", "b", "c"])

    print("== 4. 复合 ⇒ 波内并行、波间串行（随机分波）==")
    # 多试几个 seed，确保分波落在合法范围
    ok_partition = True
    saw_multi = False
    for seed in range(30):
        p = formation.plan_formation(seed, ["a", "b", "c", "d"], formation="composite")
        flat = [m for w in p.waves for m in w]
        if sorted(flat) != ["a", "b", "c", "d"]:
            ok_partition = False
            break
        if any(len(w) > 1 for w in p.waves):
            saw_multi = True
    check("分波是合法划分（每人恰好一次）", ok_partition)
    check("确实出现过『波内多人』（并行成分）", saw_multi)

    print("== 5. ★ 同一 seed 必得同一计划（可复现）==")
    a = formation.plan_formation(999, ["x", "y", "z"], formation="composite").to_dict()
    b = formation.plan_formation(999, ["x", "y", "z"], formation="composite").to_dict()
    check("两次调用完全一致", a == b, f"{a} vs {b}")


def test_ledger() -> None:
    tmpdir = tempfile.mkdtemp(prefix="mk_form_")
    db = os.path.join(tmpdir, "t.db")
    led = ChatroomLedger(db, bot_id="bot_test")
    try:
        led.open()

        print("== 6. turn 存/取对称（plan_json 不许每轮翻倍）==")
        plan = formation.plan_formation(42, ["a", "b"], formation="composite")
        led.save_turn({
            "turn_id": "turn_1", "room_id": "room_1", "segment_id": "seg_1",
            "seed": plan.seed, "strategy": plan.strategy,
            "formation": plan.formation, "plan": plan.to_dict(),
            "state": "open", "created_at": "2026-01-01 00:00:00",
        })
        lengths = []
        for _ in range(20):
            t = led.get_turn("turn_1")
            lengths.append(len(json.dumps(t["plan"], ensure_ascii=False)))
            led.save_turn(t)        # 存回去，模拟往返
        check("20 轮往返长度不增长", lengths[-1] == lengths[0], f"{lengths[0]}→{lengths[-1]}")
        t = led.get_turn("turn_1")
        check("plan 解回是 dict", isinstance(t["plan"], dict))
        check("seed 保住了", t["seed"] == 42)
        check("阵型保住了", t["formation"] == "composite")

        print("== 7. ★ 波次门控：上一波没说完，下一波不能领 ==")
        # 一轮：wave0 = [A, B]（并行），wave1 = [C]
        led.enqueue_delivery("d_a", "bot_a", "台词", room_id="r", turn_id="t2", wave=0, seq=1)
        led.enqueue_delivery("d_b", "bot_b", "台词", room_id="r", turn_id="t2", wave=0, seq=2)
        led.enqueue_delivery("d_c", "bot_c", "台词", room_id="r", turn_id="t2", wave=1, seq=3)
        # wave 0 的两个待办都能领；wave 1 的不能
        pa = led.pending_deliveries("bot_a")
        pb = led.pending_deliveries("bot_b")
        pc = led.pending_deliveries("bot_c")
        check("A（wave0）可领", len(pa) == 1 and pa[0]["delivery_id"] == "d_a", f"{pa}")
        check("B（wave0）可领", len(pb) == 1 and pb[0]["delivery_id"] == "d_b", f"{pb}")
        check("★ C（wave1）暂不可领", pc == [], f"实际 {pc}")

        # A 递出并标记收回 ⇒ wave0 还没完（B 还在 delivered）⇒ C 仍不可领
        led.claim_delivery("d_a", "bot_a")
        led.mark_deliveries_collected("bot_a")
        check("A 收回后 C 仍不可领（B 未完成）", led.pending_deliveries("bot_c") == [])

        # B 也完成 ⇒ wave0 全清 ⇒ C 放行
        led.claim_delivery("d_b", "bot_b")
        led.mark_deliveries_collected("bot_b")
        pc = led.pending_deliveries("bot_c")
        check("★ wave0 全清后 C 放行", len(pc) == 1 and pc[0]["delivery_id"] == "d_c", f"{pc}")

        print("== 8. ★ 接力：先答的回复进后答的正文 ==")
        # C 的单在 wave1，先被 A 接力（A 在 wave0）
        w = led.enqueue_delivery("d_x", "bot_x", "原始台词", room_id="r", turn_id="t3", wave=0, seq=1)
        _ = led.enqueue_delivery("d_y", "bot_y", "原始台词2", room_id="r", turn_id="t3", wave=1, seq=2)
        changed = led.relay_reply_to_later_waves("t3", 0, "我接话了！", "雏田")
        check("接力改了 1 条", changed == 1, f"实际 {changed}")
        dy = led.get_delivery("d_y")
        check("后答的正文带上了先答的话", "雏田" in dy["content"] and "我接话了" in dy["content"],
              f"实际 {dy['content']!r}")
        check("原台词还在", "原始台词2" in dy["content"], f"实际 {dy['content']!r}")

        print("== 9. 超时兜底：递出去一直不回 ⇒ 标超时 ==")
        led.enqueue_delivery("d_t", "bot_z", "台词", room_id="r", turn_id="t4", wave=0, seq=1)
        led.claim_delivery("d_t", "bot_z")        # 变成 delivered
        # 强制把 delivered_at 改到很久以前
        with led.write() as conn:
            conn.execute(
                "UPDATE delivery SET delivered_at = '2000-01-01 00:00:00' WHERE delivery_id = 'd_t'"
            )
        n = led.timeout_stale_deliveries("bot_z", older_than_seconds=60)
        check("超时标记 1 条", n == 1, f"实际 {n}")
        check("状态变成 timeout", led.get_delivery("d_t")["state"] == "timeout")

        print("== 10. turn_wave_state ==")
        ws = led.turn_wave_state("t2")
        check("波次状态能读出来", isinstance(ws, dict) and 0 in ws, f"{ws}")
    finally:
        try:
            led.close()
        except Exception:
            pass


def main() -> int:
    test_planning()
    test_ledger()
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
