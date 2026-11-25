# -*- coding: utf-8 -*-
"""麦麦聊天室 · 分段守卫离线单测（不依赖 MaiBot 运行）。

运行（必须用宿主解释器 3.13）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" test_chatroom_splitter_guard.py

**这个测试在防什么**：
① session_id 的 md5 公式必须与宿主一致（``utils_session.calculate_session_id``）
   ⇒ 算错了 ⇒ 聊天室不被识别 ⇒ **小说体被拆成一堆气泡**（而且看不出来）；
② ``modified_kwargs`` 必须**整包复制** ——
   只回一个键 ⇒ 宿主 ``reply.py:121`` 读 ``skip_post_process`` 拿到 ``None``
   ⇒ ``_require_hook_bool`` **抛 TypeError** ⇒ **整条回复报错**；
③ 真群聊（不在聊天室集合里的 session）**绝不能被误伤**。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.splitter_guard import (  # noqa: E402
    build_hook_result,
    collect_room_session_ids,
    is_chatroom_session,
    session_id_for_group,
    session_id_for_room,
    virtual_group_id,
)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def main() -> int:
    print("== 1. session_id 公式（对着宿主 utils_session.py 核）==")
    # 宿主：md5("_".join([platform, *route_components, group_id]))
    # 虚拟群建群时 account_id 为空 ⇒ 没有 account / scope 段
    expect = hashlib.md5("webui_webui_virtual_group_g1room01".encode()).hexdigest()
    got = session_id_for_room("g1room01")
    check("房间 id → session_id 与宿主公式一致",
          got == expect, f"期望 {expect} 实际 {got}")
    # 已实测过的真值（2026-11-25 实机抓的 G1）
    check("G1 实测值对得上",
          got == "635f9e9c95b42287269441c2318eb23d", f"实际 {got}")
    check("group_id 优先于推导值",
          session_id_for_group("webui_virtual_group_g1room01") == expect)

    print("== 2. 收集房间 session_id ==")
    rooms = [
        {"room_id": "room_ramen", "group_id": ""},
        {"room_id": "room_mission", "group_id": "webui_virtual_group_room_mission"},
        {"room_id": "", "group_id": "x"},          # 空房间 id ⇒ 跳过
        {"not": "a dict"},                          # 非 dict ⇒ 跳过
    ]
    ids = collect_room_session_ids(rooms)
    check("收集到 2 个 session", len(ids) == 2, f"实际 {len(ids)}")
    check("room_ramen 在里面",
          session_id_for_room("room_ramen") in ids, f"实际 {ids}")

    print("== 3. 判别：是不是聊天室的会话 ==")
    chat_ids = collect_room_session_ids([{"room_id": "room_ramen"}])
    check("聊天室会话 → True",
          is_chatroom_session(session_id_for_room("room_ramen"), chat_ids))
    check("别的会话 → False",
          not is_chatroom_session("deadbeef" * 4, chat_ids))
    check("空 session → False", not is_chatroom_session("", chat_ids))
    check("空集合 → False", not is_chatroom_session("x", []))

    print("== 4. ★ modified_kwargs 必须整包复制 ==")
    kwargs = {
        "response": "【心里一紧】……\n（扶着墙）",
        "session_id": session_id_for_room("room_ramen"),
        "reply_message_id": "msg_123",
        "reply_tool_args": {"after_reply": "wait"},
        "skip_post_process": False,
        "enable_splitter": True,
        "enable_chinese_typo": True,
    }
    result = build_hook_result(kwargs, kwargs["session_id"], chat_ids)
    check("返回协议带 action=continue",
          result is not None and result.get("action") == "continue", f"实际 {result}")
    modified = (result or {}).get("modified_kwargs")
    check("带 modified_kwargs", isinstance(modified, dict), f"实际 {modified}")
    check("★ 七个 required 字段一个不少",
          set(modified or {}) == set(kwargs),
          f"少了 {set(kwargs) - set(modified or {})}")
    check("enable_splitter 被关掉", modified.get("enable_splitter") is False)
    check("★ skip_post_process 仍是 False（没被误伤）",
          modified.get("skip_post_process") is False)
    check("★ enable_chinese_typo 保持原值",
          modified.get("enable_chinese_typo") is True)
    check("response 原样保留", modified.get("response") == kwargs["response"])
    check("reply_tool_args 原样保留",
          modified.get("reply_tool_args") == kwargs["reply_tool_args"])
    check("★ 原 kwargs 没被就地改坏（是复制不是同一份）",
          kwargs["enable_splitter"] is True)

    print("== 5. ★ 真群聊绝不能被误伤 ==")
    other = build_hook_result(kwargs, "f" * 32, chat_ids)
    check("不是聊天室的会话 → 不带 modified_kwargs",
          other is not None and "modified_kwargs" not in other,
          f"实际 {other}")
    check("action 仍是 continue", (other or {}).get("action") == "continue")

    print("== 6. decide_splitter 便捷函数 ==")
    from chatroom.splitter_guard import decide_splitter

    check("聊天室 → enable_splitter=False",
          (decide_splitter(kwargs["session_id"], chat_ids) or {}).get("enable_splitter") is False)
    check("非聊天室 → None（不动）",
          decide_splitter("f" * 32, chat_ids) is None)

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
