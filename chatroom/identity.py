# -*- coding: utf-8 -*-
"""麦麦聊天室的**身份计算**（纯函数，可离线单测）。

这一层解决三件事：

1. **虚拟群 id**：聊天室的每个房间 = 一个虚拟群（02 文档第 4 章）。
   格式固定为 ``webui_virtual_group_<房间 id>``。
2. **会话 id（session_id）**：宿主给一条聊天流算的 md5 指纹。
   ⚠️ **永远是 md5，不是原始 group_id** ⇒ 想按会话判断"这条回复是不是聊天室的"，
   只能**自己算 hash**，不能靠字符串前缀找群聊（那是不可能的）。
   公式对着宿主源码 ``src/common/utils/utils_session.py:6-42`` 核过：
   群聊 = ``md5("_".join([platform, *route_components, group_id]))``；
   虚拟群建群时 account_id 为空 ⇒ **没有** account / scope 段。
3. **固定身份**：麦麦认人靠「平台 + 账号」组合（02 文档「平台身份固定」铁则）。
   同一个角色换平台发消息会被当成两个人（G1 实测：出现过两张名片）。

**纯逻辑模块**：无宿主导入 ⇒ 可离线单测。
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Optional

#: 虚拟群 id 前缀（宿主侧 `src/chat/message_receive/uni_message_sender.py:24` 同一个常量）
VIRTUAL_GROUP_PREFIX = "webui_virtual_group_"

#: 聊天室固定用这个平台。
#: ⚠️ **不能自定义** —— 自定义平台名会被宿主的「适配器名单」拦掉，
#:    聊天运行实例根本建不起来；`webui` / `internal` 在心流白名单里无条件放行。
DEFAULT_PLATFORM = "webui"


def virtual_group_id(room_id: str, prefix: str = VIRTUAL_GROUP_PREFIX) -> str:
    """房间 id → 虚拟群 group_id。"""
    return f"{prefix}{room_id}"


def session_id_for_group(group_id: str, platform: str = DEFAULT_PLATFORM) -> str:
    """虚拟群 → 会话 id（md5）。

    ⚠️ 公式必须与宿主一致（``utils_session.calculate_session_id``）：
    群聊 = ``md5(f"{platform}_{group_id}")``（虚拟群没有 account / scope 段）。
    """
    raw = f"{platform}_{group_id}"
    return hashlib.md5(raw.encode()).hexdigest()


def session_id_for_private(user_id: str, platform: str = DEFAULT_PLATFORM) -> str:
    """私聊 → 会话 id（md5）。

    私聊的拼法多两段：``md5(f"{platform}_{user_id}_private")``（无 account / scope 时）。
    """
    raw = f"{platform}_{user_id}_private"
    return hashlib.md5(raw.encode()).hexdigest()


def new_message_id(prefix: str = "msg") -> str:
    """生成一条消息的唯一 id。

    ⚠️ **必须唯一** —— 服务端按 msg_id 去重，重复的会被直接忽略
    （``maim_message/server_ws_api.py:270-278``）。
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}_{int(time.time())}"


def identity_of(platform: str, account: str) -> tuple:
    """角色的固定身份（平台 + 账号）⇒ 麦麦据此认人。

    ⚠️ 这两个值**一旦定了就不能改** —— 改了等于换个人，
    麦麦会认成新角色，通讯录里多一张名片（02 文档「平台身份固定」铁则）。
    """
    return (str(platform or DEFAULT_PLATFORM), str(account or ""))


def account_of(member_id: str, namespace: str = "user") -> str:
    """由成员 id 推导一个稳定的账号（自建角色用）。

    ⚠️ 推导规则一旦定下就别改 —— 改了等于换人。
    """
    return f"{namespace}.{member_id}"


def optional_text(value: Optional[str]) -> str:
    """None → 空串（避免把 None 塞进协议字段）。"""
    return "" if value is None else str(value)
