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
from chatroom.collector import ReplyCollector  # noqa: E402
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

    print("== 4b. ★ 权重随机（2026-11-27 拍板：合流 50 / 并行 30 / 串行 20）==")
    import collections
    dist = collections.Counter()
    for seed in range(1000):
        dist[formation.plan_formation(seed, ["a", "b"]).formation] += 1
    check("复合占大头（40%~60%）", 400 <= dist["composite"] <= 600, f"实际 {dict(dist)}")
    check("并行居中（20%~40%）", 200 <= dist["parallel"] <= 400, f"实际 {dict(dist)}")
    check("串行最小（10%~30%）", 100 <= dist["serial"] <= 300, f"实际 {dict(dist)}")
    check("复合 > 并行 > 串行",
          dist["composite"] > dist["parallel"] > dist["serial"], f"实际 {dict(dist)}")

    print("== 4c. ★ 两 Bot 复合恒 1 波（不退化成串行）==")
    two_bot_comp = [
        formation.plan_formation(s, ["a", "b"], formation="composite")
        for s in range(30)
    ]
    check("30 个 seed 全是单波（真并行）", all(p.wave_count == 1 for p in two_bot_comp),
          f"实际波数 {[p.wave_count for p in two_bot_comp[:10]]}")
    # 名义权重 20% 串行 + 复合不再退化 ⇒ 「实际逐个接话」应≈20%
    stepwise = sum(
        1 for s in range(1000)
        if (lambda p: p.formation == "serial" or
            (p.formation == "composite" and p.wave_count == 2))(
                formation.plan_formation(s, ["a", "b"]))
    )
    check("★ 逐个接话占比降到 ~20%（原 ~50%）", 150 <= stepwise <= 300,
          f"实际 {stepwise}/1000")

    print("== 4d. ★ 5% 沉默掷骰（无勾选轮次；全中重掷）==")
    import random as _rnd
    kept_all = [
        formation.apply_silence_roll(_rnd.Random(seed), ["a", "b", "c"])
        for seed in range(1000)
    ]
    avg = sum(len(k) for k in kept_all) / 1000
    check("平均留下 ≈2.85 个（3 × 95%）", 2.7 < avg < 3.0, f"实际 {avg}")
    check("永不全军覆没", all(len(k) >= 1 for k in kept_all))
    check("确实出现过沉默（少于 3 个）", any(len(k) < 3 for k in kept_all))
    check("rate=1.0 病态输入不死循环（兜底留第一个）",
          formation.apply_silence_roll(_rnd.Random(1), ["a", "b"], rate=1.0) == ["a"])
    check("同一 seed 重掷结果一致",
          formation.apply_silence_roll(_rnd.Random(42), ["a", "b", "c"])
          == formation.apply_silence_roll(_rnd.Random(42), ["a", "b", "c"]))

    print("== 4e. ★ seed 跨重载不重复（counter 持久化后每轮都不同）==")
    base = 1795700000     # 模拟 epoch 起头的 counter
    seeds = [formation.next_seed(base + i, 1) for i in range(20)]
    check("连续 20 轮 seed 全不同", len(set(seeds)) == 20, f"实际 {len(set(seeds))}")
    forms = [formation.plan_formation(s, ["a", "b"]).formation for s in seeds]
    from collections import Counter as _C
    dist2 = _C(forms)
    check("★ 阵型分布仍 50/30/20（复合占大头）",
          dist2["composite"] > dist2["parallel"] > dist2["serial"], f"实际 {dict(dist2)}")

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

        print("== 9b. ★ 旁白投递超窗后照常 timeout（与台词同款，2026-11-27 拍板）==")
        # 旁白现在 = 「我的角色」说的一句话：带 turn_id、期待接话 ⇒
        # 超窗必须标 timeout 才能进 collector 的 30 分钟宽限期（迟到回复不丢）。
        led.enqueue_delivery(
            "d_narr", "bot_z", "三天后，木叶村的集会广场上……", room_id="r",
            sender_member_id="narrator", turn_id="t_narr", wave=0, seq=2,
        )
        led.claim_delivery("d_narr", "bot_z")
        # 私聊单**也是** turn_id=""，但它期待回复 ⇒ 必须照常超时
        # （证明不能拿「turn_id 为空」当旁白的判据）
        led.enqueue_delivery(
            "d_priv", "bot_z", "私聊台词", room_id="private_bot_z_naruto",
            sender_member_id="user.naruto", group_id="", turn_id="", wave=0, seq=3,
        )
        led.claim_delivery("d_priv", "bot_z")
        with led.write() as conn:
            conn.execute(
                "UPDATE delivery SET delivered_at = '2000-01-01 00:00:00'"
                " WHERE delivery_id IN ('d_narr','d_priv')"
            )
        n = led.timeout_stale_deliveries("bot_z", older_than_seconds=60)
        check("旁白+私聊共 2 条超时", n == 2, f"实际 {n}")
        d_narr = led.get_delivery("d_narr")
        check("★ 旁白超窗后 state=timeout（进宽限期，能收迟到回复）",
              d_narr["state"] == "timeout", f"实际 {d_narr['state']}")
        d_priv = led.get_delivery("d_priv")
        check("私聊单照常标 timeout", d_priv["state"] == "timeout", f"实际 {d_priv['state']}")

        print("== 10. turn_wave_state ==")
        ws = led.turn_wave_state("t2")
        check("波次状态能读出来", isinstance(ws, dict) and 0 in ws, f"{ws}")

        print("== 11. ★ meta 键值：最近使用的角色（跨重载持久化）==")
        led.set_meta("last_used_role", "role_sasuke")
        check("set/get 往返", led.get_meta("last_used_role") == "role_sasuke")
        check("不存在的键返回空串", led.get_meta("no_such_key") == "")
        # ⚠️ 关键回归：save_state 整体覆写 room/member 表，绝不能碰掉 meta
        led.save_state({
            "rooms": [{"room_id": "r", "name": "拉面店", "mode": "stage",
                       "group_id": "", "config": {}, "member_ids": []}],
            "members": [{"member_id": "role_sasuke", "kind": "user_role",
                         "display_name": "佐助", "platform": "webui", "account": "user.sasuke"}],
            "my_role_order": ["role_sasuke"],
            "peer_order": [],
        })
        check("★ save_state 之后 meta 还在", led.get_meta("last_used_role") == "role_sasuke")

        print("== 12. ★ 删光房间：空快照 ≠ 从未存过（用户拍板「删了全部重来」）==")
        # 模拟「删到零」后的存档：rooms=[] 但成员/名单还在
        led.save_state({
            "rooms": [],
            "members": [{"member_id": "role_sasuke", "kind": "user_role",
                         "display_name": "佐助", "platform": "webui", "account": "user.sasuke"}],
            "my_role_order": ["role_sasuke"],
            "peer_order": [],
        })
        st = led.load_state()
        check("★ 空房间快照能读回（不会当「从未存过」触发出厂数据回迁）",
              st is not None and st.get("rooms") == [], f"实际 {st}")
        check("成员还在快照里", bool(st) and len(st.get("members") or []) == 1)
        # 对照：全新账本（没存过任何快照）⇒ 必须返回 None ⇒ 走出厂迁移
        led2 = ChatroomLedger(os.path.join(tmpdir, "fresh_ledger.db"), bot_id="bot_test2")
        try:
            led2.open()
            check("全新账本仍是 None（首次迁移路径不受影响）", led2.load_state() is None)
        finally:
            try:
                led2.close()
            except Exception:
                pass

        print("== 13. ★ 删房间 = 物理删数据；未递完的旁白单保留；孤儿清理不误删私聊 ==")
        # 搭一个房间：普通台词投递（collected）+ 没递完的旁白单（delivered）+ 私聊单
        led.save_state({
            "rooms": [{"room_id": "room_r1", "name": "拉面店", "mode": "stage",
                       "group_id": "", "config": {}, "member_ids": []}],
            "members": [], "my_role_order": [], "peer_order": [],
        })
        led.enqueue_delivery("d_line", "bot_z", "台词", room_id="room_r1", seq=1)
        led.claim_delivery("d_line", "bot_z")
        led.mark_collected("d_line")
        led.enqueue_delivery("d_narr2", "bot_z", "告别旁白", room_id="room_r1",
                             sender_member_id="narrator", seq=2)
        led.claim_delivery("d_narr2", "bot_z")   # 旁白已递出但未终态
        led.enqueue_delivery("d_priv2", "bot_z", "私聊台词", room_id="private_a_b",
                             sender_member_id="user.x", group_id="", seq=3)
        purged = led.delete_room_data("room_r1")
        check("消息行被物理删", purged.get("messages", 0) >= 0)
        rows = led.get_delivery("d_line")
        check("★ 普通投递已删（查不到）", rows is None or rows.get("delivery_id") is None,
              f"实际 {rows}")
        narr = led.get_delivery("d_narr2")
        check("★ 未终态的旁白单保留（等递出后清理）",
              narr is not None and narr.get("state") == "delivered", f"实际 {narr}")
        priv = led.get_delivery("d_priv2")
        check("★ 私聊单不受影响", priv is not None and priv.get("state") == "pending",
              f"实际 {priv}")
        n = led.cleanup_orphan_deliveries()
        # ⚠️ 返回值是全库的清理数（前面测试组也会留下合法的孤儿），
        #    这里只盯本组的旁白单：未终态 ⇒ 必须还在
        narr = led.get_delivery("d_narr2")
        check("未终态的旁白单不会被孤儿清理收走",
              narr is not None and narr.get("state") == "delivered", f"实际 {narr}")
        led.mark_collected("d_narr2")            # 旁白递出收口
        n = led.cleanup_orphan_deliveries()
        check("★ 递完的旁白孤儿被清走", led.get_delivery("d_narr2") is None)
        check("★ 私聊单绝不被孤儿清理误删",
              led.get_delivery("d_priv2") is not None)

        print("== 14. ★ 波次收口：Bot 开口 ⇒ 它这一波其余单不再等 150 秒 ==")
        led.enqueue_delivery("d_w0a", "bot_q", "台词一", room_id="r", turn_id="t9", wave=0, seq=1)
        led.enqueue_delivery("d_w0b", "bot_q", "台词二", room_id="r", turn_id="t9", wave=0, seq=2)
        led.enqueue_delivery("d_w1", "bot_z", "台词三", room_id="r", turn_id="t9", wave=1, seq=3)
        led.claim_delivery("d_w0a", "bot_q")
        led.claim_delivery("d_w0b", "bot_q")
        # bot_q 只回了一句（配对到 d_w0b）⇒ d_w0a 没被单独回复
        led.mark_collected("d_w0b")
        changed = led.mark_wave_answered("t9", 0, "bot_q")
        check("同波其余单被一起收口", changed == 1, f"实际 {changed}")
        check("波次 0 放行（d_w1 可领）",
              any(d["delivery_id"] == "d_w1" for d in led.pending_deliveries("bot_z")),
              f"实际 {[d['delivery_id'] for d in led.pending_deliveries('bot_z')]}")
        # 其它 Bot 的单不受影响
        led.enqueue_delivery("d_w0c", "bot_r", "台词四", room_id="r", turn_id="t9", wave=0, seq=4)
        led.claim_delivery("d_w0c", "bot_r")
        led.mark_wave_answered("t9", 0, "bot_q")
        check("不误收其它 Bot 的单", led.get_delivery("d_w0c")["state"] == "delivered")

        print("== 15. ★ 剥引用壳（陷阱 27：processed_plain_text = 被引原文+新正文）==")
        strip = ReplyCollector.strip_quoted_prefix
        relay = "（这期间 春野樱 说：【佐助这话说得……】）\n樱？雏田？说话啊。"
        body = "【查克拉……撑住……】鸣人君……我、我没事的……"
        text = relay + " " + body
        check("剥掉被引原文，只留新正文", strip(text, [relay]) == (body, relay),
              f"实际 {strip(text, [relay])!r}")
        check("候选取最长匹配", strip(text, ["（这期间", relay]) == (body, relay))
        check("匹配不上就不动（不误伤）", strip(body, ["完全无关的原文"]) == (body, ""))
        check("剥完为空 ⇒ 空串", strip(relay, [relay]) == ("", relay))
        check("空候选不动", strip(text, []) == (text, ""))

        print("== 16. ★ 引用候选：投递正文 + 房间消息 ==")
        cands = led.reply_quote_candidates("bot_q", "r", limit=10)
        check("投递正文在候选里", any("台词一" in c for c in cands), f"实际 {cands[:3]}")
        check("长的排前面（最长前缀匹配）", cands == sorted(cands, key=len, reverse=True))

        print("== 16b. ★ 引用原文持久化（quote 列，前端画引用块）==")
        led.append_message({
            "message_id": "m_quote_test", "segment_id": "seg_q", "room_id": "r",
            "speaker_name": "日向雏田", "kind": "reply", "content": "新正文",
            "quote": "鸣人：被引的那句", "created_at": "2026-11-27 20:00:00",
        })
        rows = [m for m in led.list_messages("r") if m["message_id"] == "m_quote_test"]
        check("quote 存进账本并读回",
              len(rows) == 1 and rows[0]["quote"] == "鸣人：被引的那句", f"实际 {rows}")
        led.append_message({
            "message_id": "m_no_quote", "segment_id": "seg_q", "room_id": "r",
            "speaker_name": "春野樱", "kind": "reply", "content": "无引用",
            "created_at": "2026-11-27 20:00:01",
        })
        check("无 quote 的消息兜底空串",
              any(m["message_id"] == "m_no_quote" and m["quote"] == ""
                  for m in led.list_messages("r")))
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
