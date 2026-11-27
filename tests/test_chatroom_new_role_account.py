# -*- coding: utf-8 -*-
"""聊天室 · 新建「我的角色」必须有平台身份（2026-11-27 bug 回归）。

运行：
    python test_chatroom_new_role_account.py

**这个 bug 的形状**：两条新建路径——
①「我的角色」面板添加 → `shell_add_my_role`（2026-11-27 实测「井野」撞到）
②「群员」面板新建   → `shell_create_member`（上一轮已修）
其中一条漏写 platform/account ⇒ 私聊发言报「发言角色没有账号（平台身份缺失）」。
本测试把**两条路都钉死**：任何一条回退到没有账号都会 FAIL。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.service import ChatroomService  # noqa: E402
from chatroom.identity import DEFAULT_PLATFORM, account_of  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


async def main() -> int:
    svc = ChatroomService(plugin=None)

    print("== 1. 「我的角色」面板新建（shell_add_my_role）==")
    res = await svc.shell_add_my_role("测试角色甲")
    check("新建成功", res.get("ok"), str(res))
    rid = res.get("created", {}).get("member_id")
    member = svc._member_map().get(rid)
    check("member 存在", member is not None)
    if member:
        check("platform 有值", member.get("platform") == DEFAULT_PLATFORM, f"{member.get('platform')}")
        check("account = user.<member_id>", member.get("account") == account_of(rid), f"{member.get('account')}")
        check("account 非空", bool(str(member.get("account") or "").strip()))

    print("== 2. 「群员」面板新建（shell_create_member）==")
    res2 = await svc.shell_create_member(display_name="测试角色乙", kind="user_role")
    check("新建成功", res2.get("ok"), str(res2))
    rid2 = res2.get("created", {}).get("member_id")
    member2 = svc._member_map().get(rid2)
    check("member 存在", member2 is not None)
    if member2:
        check("platform 有值", member2.get("platform") == DEFAULT_PLATFORM, f"{member2.get('platform')}")
        check("account = user.<member_id>", member2.get("account") == account_of(rid2), f"{member2.get('account')}")

    print("== 3. 两个角色账号不同（不会撞名片）==")
    check("账号唯一", member and member2 and member.get("account") != member2.get("account"))

    print("== 4. 旧账本迁移：恢复时给缺账号的自建角色补齐 ==")
    import asyncio as _a
    from chatroom.ledger import ChatroomLedger
    import tempfile, os
    # ⚠️ 不用 TemporaryDirectory：ledger 的专属线程在 close 后可能还占着 db 文件
    #    ⇒ Windows 退出时删目录会 PermissionError（假失败）。测试库留在 temp 无妨。
    import shutil as _sh
    tmp = tempfile.mkdtemp(prefix="role-acct-")
    try:
        led = ChatroomLedger(os.path.join(tmp, "t.db"), bot_id="b")
        led.open()
        led.save_state({
            "rooms": [],
            "members": [
                {"member_id": "role_naruto", "kind": "user_role", "display_name": "鸣人",
                 "platform": "webui", "account": "user.naruto"},
                {"member_id": "custom_001", "kind": "user_role", "display_name": "井野",
                 "platform": "", "account": ""},   # 旧代码建的坏数据
            ],
            "my_role_order": ["role_naruto", "custom_001"],
            "peer_order": [],
        })
        led.set_meta("has_state", "1")

        class Stub:
            _chatroom_ledger = led
            _chatroom_workspace = None
        svc2 = ChatroomService(plugin=Stub())
        ok = await svc2.restore_state()
        check("从账本恢复成功", ok)
        old = svc2._member_map().get("custom_001")
        check("旧自建角色补上账号", old and old.get("account") == account_of("custom_001"),
              f"{old}")
        check("预置角色账号不变",
              svc2._member_map().get("role_naruto", {}).get("account") == "user.naruto")
        # 回写后账本里也应是好的
        saved = await led.run(led.load_state)
        saved_old = next((m for m in saved.get("members", []) if m.get("member_id") == "custom_001"), {})
        check("修复已回写账本", saved_old.get("account") == account_of("custom_001"), f"{saved_old}")
        led.close()
    finally:
        pass   # temp 目录故意留着（Windows 上 ledger 线程可能还占着 db）

    print("\n=== 全部通过 ===" if not FAILED else f"\n=== 失败 {len(FAILED)} 项 ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
