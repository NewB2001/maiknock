# -*- coding: utf-8 -*-
"""麦麦聊天室 · 频率阀门（talkvalve + ledger 在途/宽限查询）离线单测。

运行：
    python test_chatroom_talkvalve.py

**在防什么**：
① 开阀参数形状（chat_id = md5("webui_<group_id>")，value=1/0）；
② ★ 回读核实：set 后必须 get_adjust 对账——宿主在心流实例未建时 set 是
   「空转但报成功」，不核实就会假关（㉛ 实测：旁白触发一整轮 16 回合思考）；
③ 期望「关」而对不上 ⇒ 不记账、下轮重试；期望「开」而实例未建 ⇒ 视为就位；
④ ★ 漂移自愈：宿主侧乘数被重置（运行实例重建）⇒ sweep 2 秒内纠回；
⑤ 幽灵会话（已删房间）被遗忘，不再空转下发；
⑥ ledger.inflight_valve_groups：算台词/旁白（都带 turn_id）在途单，排除私聊；
⑦ ★ ledger.recent_collect_targets：宽限期内的超时单（迟到回复不许丢）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.identity import session_id_for_group, virtual_group_id  # noqa: E402
from chatroom.ledger import ChatroomLedger  # noqa: E402
from chatroom.talkvalve import TalkValve  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


class FakeHost:
    """假的宿主能力通道：模拟「心流实例存在才真正生效」的 set_adjust。"""

    def __init__(self) -> None:
        self.calls: list = []
        self.values: dict = {}          # session -> 实际乘数（缺失 ⇒ 心流实例未建）
        self.fail_next = 0

    def create_chat(self, session: str) -> None:
        """心流实例建成（默认乘数 1.0）。"""
        self.values.setdefault(session, 1.0)

    def drop_chat(self, session: str) -> None:
        """心流实例销毁（运行实例重建 ⇒ 乘数丢失）。"""
        self.values.pop(session, None)

    async def call_capability(self, name: str, **args):
        self.calls.append((name, dict(args)))
        session = str(args.get("chat_id") or "")
        if name == "frequency.get_adjust":
            # ⚠️ SDK 的 _CAPABILITY_RESULT_KEYS 把它解包成 float 本身
            return float(self.values.get(session, 1.0))
        if name == "frequency.set_adjust":
            if self.fail_next > 0:
                self.fail_next -= 1
                return {"success": False, "error": "denied"}
            if session in self.values:      # 实例存在 ⇒ 真正生效
                self.values[session] = float(args.get("value"))
            # 实例不存在 ⇒ 宿主照样报成功，但什么都没改（空转，㉛ 的坑）
            return True    # ⚠️ SDK 归一化成 bool（_BOOLEAN_SUCCESS_CAPABILITIES）
        return {"success": False, "error": f"unknown {name}"}


def _session_of(group_id: str) -> str:
    return session_id_for_group(group_id)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mk_valve_")
    gid = virtual_group_id("room_ramen")
    sid = _session_of(gid)

    print("== 1. ★ 开阀：实例未建时回读=1.0 ⇒ 视为就位，不多发 set ==")
    host = FakeHost()
    valve = TalkValve(host.call_capability)
    ok = asyncio.run(valve.ensure_group(gid, True))
    check("ensure_group 返回 True", ok is True)
    check("只发了 get（没发 set）",
          [n for n, _ in host.calls] == ["frequency.get_adjust"], f"{host.calls}")
    check("记账为开", valve.known_sessions().get(sid) is True)

    print("== 2. ★ 关阀：实例未建 ⇒ set 空转 ⇒ 回读对不上 ⇒ 不记账、重试 ==")
    ok = asyncio.run(valve.ensure_group(gid, False))
    check("返回 False（没关上）", ok is False)
    check("★ 不记账（下轮要重试）", sid not in valve.known_sessions(),
          f"{valve.known_sessions()}")
    check("实际值没变（宿主空转）", host.values.get(sid, 1.0) == 1.0)
    ok = asyncio.run(valve.ensure_group(gid, False))     # 下轮重试，还是没建
    check("重试仍 False", ok is False and sid not in valve.known_sessions())
    host.create_chat(sid)                                 # 心流实例建好了
    ok = asyncio.run(valve.ensure_group(gid, False))
    check("★ 实例建好后关阀成功", ok is True and host.values.get(sid) == 0.0,
          f"value={host.values.get(sid)}")
    check("记账为关", valve.known_sessions().get(sid) is False)

    print("== 3. ★ 漂移自愈：实例重建把乘数重置 ⇒ sweep 2 秒内纠回 ==")
    host.drop_chat(sid)                                   # 运行实例重建 ⇒ 乘数丢失
    host.create_chat(sid)                                 # 新实例默认 1.0
    check("宿主侧已经漂回 1.0", host.values.get(sid) == 1.0)
    valve._lease_until.pop(sid, None)                     # 清掉前面开阀留下的租约
    asyncio.run(valve.sweep([sid], []))                   # 空闲 ⇒ 期望关
    check("★ sweep 把漂移纠回来了", host.values.get(sid) == 0.0,
          f"value={host.values.get(sid)}")
    # 在途 ⇒ sweep 重新开阀
    asyncio.run(valve.sweep([sid], [sid]))
    check("★ 在途时 sweep 开阀", host.values.get(sid) == 1.0)

    print("== 4. ★ 幽灵会话（已删房间）被遗忘 ==")
    ghost = _session_of(virtual_group_id("room_deleted"))
    host.create_chat(ghost)
    asyncio.run(valve.ensure_session(ghost, False))
    check("幽灵会话先记账", valve.known_sessions().get(ghost) is False)
    asyncio.run(valve.sweep([sid], []))                   # 全集里没有 ghost
    check("★ 幽灵被遗忘", ghost not in valve.known_sessions(),
          f"{valve.known_sessions()}")
    # 全集为空（账本读挂）⇒ 不许清记账
    asyncio.run(valve.ensure_session(sid, False))
    asyncio.run(valve.sweep([], []))
    check("全集为空时不清记账", sid in valve.known_sessions())

    print("== 4b. ★ 开阀租约：sweep 不许在宿主防抖判定前关阀 ==")
    host5 = FakeHost(); valve5 = TalkValve(host5.call_capability)
    host5.create_chat(sid)
    asyncio.run(valve5.ensure_group(gid, True))     # 开阀 + 立租约
    asyncio.run(valve5.sweep([sid], []))            # 无在途 ⇒ 期望关
    check("★ 租约期内阀门保持开", host5.values.get(sid) == 1.0,
          f"实际 {host5.values.get(sid)}")
    valve5._lease_until[sid] = 0                    # 模拟租约到期
    asyncio.run(valve5.sweep([sid], []))
    check("租约到期后正常关阀", host5.values.get(sid) == 0.0,
          f"实际 {host5.values.get(sid)}")

    print("== 5. ledger.inflight_valve_groups：台词/旁白算在途，私聊不算 ==")
    db = os.path.join(tmp, "t.db")
    led = ChatroomLedger(db, bot_id="bot_v")
    led.open()
    led.enqueue_delivery("v1", "bot_v", "台词", room_id="r", turn_id="t1", wave=0, seq=1,
                         group_id="webui_virtual_group_room_a")
    # ★ 旁白现在 = 「我的角色」说的一句话（2026-11-27 用户拍板）：
    #   它带 turn_id、期待接话 ⇒ 必须算在途，否则 sweep 会在 Bot 思考期间把阀关掉，
    #   重演「时光机旁白后 Bot 全哑巴」。
    led.enqueue_delivery("v2", "bot_v", "旁白", room_id="r", sender_member_id="narrator",
                         turn_id="t_narr", wave=0, seq=2,
                         group_id="webui_virtual_group_room_a")
    led.enqueue_delivery("v3", "bot_v", "私聊", room_id="private_x", turn_id="", wave=0, seq=3,
                         group_id="")
    led.enqueue_delivery("v4", "bot_v", "别的群台词", room_id="r2", turn_id="t2", wave=0, seq=4,
                         group_id="webui_virtual_group_room_b")
    groups = led.inflight_valve_groups("bot_v")
    check("★ 在途群 = 台词/旁白所在的两个群（私聊不算）",
          sorted(groups) == ["webui_virtual_group_room_a", "webui_virtual_group_room_b"],
          f"实际 {groups}")
    led.claim_delivery("v1", "bot_v")
    check("delivered 也算在途", "webui_virtual_group_room_a" in led.inflight_valve_groups("bot_v"))
    led.mark_collected("v1")
    led.mark_collected("v2")
    led.mark_collected("v4")
    check("全部终态后在途为空", led.inflight_valve_groups("bot_v") == [])

    print("== 6. ★ 宽限期：超时单 30 分钟内仍算「要盯着的流」 ==")
    led.enqueue_delivery("g1", "bot_v", "慢台词", room_id="r3", turn_id="t3", wave=0, seq=10,
                         group_id="webui_virtual_group_room_c")
    led.claim_delivery("g1", "bot_v")          # 变 delivered
    led.timeout_stale_deliveries("bot_v", older_than_seconds=0.0)   # 立刻标超时
    grace = led.recent_collect_targets("bot_v", within_seconds=1800.0)
    check("★ 刚超时的单在宽限期里", len(grace) == 1 and grace[0]["delivery_id"] == "g1",
          f"实际 {grace}")
    # 把 delivered_at 拨回 2 小时前 ⇒ 出了宽限期
    with led.write() as conn:
        conn.execute(
            "UPDATE delivery SET delivered_at = datetime('now','localtime','-2 hours')"
            " WHERE delivery_id = 'g1'"
        )
    check("超时太久出宽限期", led.recent_collect_targets("bot_v", within_seconds=1800.0) == [])
    # pending/delivered 不进宽限名单（它们本来就在待收名单里）
    led.enqueue_delivery("g2", "bot_v", "在途台词", room_id="r4", turn_id="t4", wave=0, seq=11,
                         group_id="webui_virtual_group_room_d")
    check("pending 不进宽限名单", led.recent_collect_targets("bot_v") == [])

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
