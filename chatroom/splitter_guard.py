# -*- coding: utf-8 -*-
"""麦麦聊天室 · 分段守卫（按会话关掉宿主的回复分段）。

**要解决的问题**：宿主有个全局开关 ``[response_splitter] enable``，
打开时把长回复拆成多个气泡。聊天室的回复是**小说体**
（内心 + 动作 + 台词挤在一段里），拆开就散了 ⇒ 聊天室需要**整条一个气泡**。

**为什么不用全局开关**：全局开关是**所有会话一刀切** ——
QQ/微信真群聊也一起被连坐（之前为了保小说体把全局关了，真群聊被连坐了半年）。

**为什么能按会话关**：宿主专门给插件留了口子 ——
``maisaka.reply.before_post_process`` Hook，载荷里带 ``session_id``，
还能分别控制「要不要拆分」「要不要注错别字」。
⚠️ 宿主最终生效的闸门是 ``src/chat/utils/utils.py:470``::

    if global_config.response_splitter.enable and enable_splitter:
        split_sentences = _split_into_sentence_segments(cleaned_text)
    else:
        split_sentences = [(cleaned_text, "")]      # ← 整段 = 一个气泡

⇒ **两个条件都要满足**：全局 ``enable`` 为真、**本次** ``enable_splitter`` 为真。
⇒ 所以**全局开着**，我们把本次的 ``enable_splitter`` 关掉 ⇒ 只有聊天室不分段。

⚠️ **只关 ``enable_splitter``，不要用 ``skip_post_process``** ——
后者会把错别字注入等**全部**后处理一起关掉（宿主还有别的后处理步骤）。

**判据只能是 ``session_id``**：载荷里既没有 ``platform`` 也没有 ``group_id``。
而 session_id **恒为 md5** ⇒ 只能自己算 hash，不能靠字符串前缀找群聊。
公式对着宿主 ``src/common/utils/utils_session.py:6-42`` 核过：
群聊 = ``md5("_".join([platform, *route_components, group_id]))``；
虚拟群建群时 account_id 为空 ⇒ **没有** account / scope 段。

**纯逻辑模块**：只依赖标准库 ⇒ 可离线单测。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Optional

#: 虚拟群 id 前缀（与 ``identity.VIRTUAL_GROUP_PREFIX`` 同一个常量）
VIRTUAL_GROUP_PREFIX = "webui_virtual_group_"

#: 聊天室固定用的平台（与 ``identity.DEFAULT_PLATFORM`` 同一个）
DEFAULT_PLATFORM = "webui"


def virtual_group_id(room_id: str, prefix: str = VIRTUAL_GROUP_PREFIX) -> str:
    """房间 id → 虚拟群 group_id。"""
    return f"{prefix}{room_id}"


def session_id_for_group(group_id: str, platform: str = DEFAULT_PLATFORM) -> str:
    """虚拟群 group_id → 会话 id（md5）。

    ⚠️ 公式必须与宿主一致（``utils_session.calculate_session_id``）：
    群聊 = ``md5(f"{platform}_{group_id}")``（虚拟群没有 account / scope 段）。
    """
    raw = f"{platform}_{group_id}"
    return hashlib.md5(raw.encode()).hexdigest()


def session_id_for_room(room_id: str, platform: str = DEFAULT_PLATFORM) -> str:
    """房间 id → 会话 id（md5）。"""
    return session_id_for_group(virtual_group_id(room_id), platform)


def collect_room_session_ids(
    rooms: Iterable[Dict[str, Any]],
    platform: str = DEFAULT_PLATFORM,
) -> List[str]:
    """房间列表 → 所有虚拟群的 session_id 集合。

    ⚠️ **房间的 ``group_id`` 优先**：真群聊建出来后宿主会把它写回房间，
    这时用它算才准（虚拟群没建起来时 ``group_id`` 为空 ⇒ 用推导值）。
    """
    out: List[str] = []
    for room in rooms or []:
        if not isinstance(room, dict):
            continue
        room_id = str(room.get("room_id") or "")
        if not room_id:
            continue
        group_id = str(room.get("group_id") or "") or virtual_group_id(room_id)
        out.append(session_id_for_group(group_id, platform))
    return out


def is_chatroom_session(session_id: str, chatroom_session_ids: Iterable[str]) -> bool:
    """这个会话是不是聊天室的虚拟群。

    ⚠️ **比对必须用「集合」而不是逐次 md5 计算** ——
    每次回复都算一遍 md5 不算错，但房间多了就是无谓开销；
    更重要的是：**集合是启动时算好的快照**，语义上更清楚。
    """
    sid = str(session_id or "")
    if not sid:
        return False
    return sid in set(chatroom_session_ids or ())


def decide_splitter(
    session_id: str,
    chatroom_session_ids: Iterable[str],
) -> Dict[str, Any]:
    """要不要为这次回复关掉分段。

    返回 ``{"action": "continue", "modified_kwargs": {...}}`` 或 ``None``
    （``None`` = 不动，让宿主按全局配置走）。

    ⚠️⚠️ **协议三个坑**（宿主 ``hook_dispatcher.py:548/621`` + ``reply.py:87-92``）：
    1. ``modified_kwargs`` 是**整包替换**不是 merge
       （``hook_dispatcher.py:621``：``dispatch_result.kwargs = dict(modified_kwargs)``）。
       ⇒ 必须 ``dict(kwargs)`` 复制**全份**再改一个键。
       ⚠️ 只回 ``{"enable_splitter": False}`` ⇒ 其余 required 字段全丢
       ⇒ 宿主 ``reply.py:121`` 读 ``skip_post_process`` 时拿到 ``None``
       ⇒ ``_require_hook_bool`` **抛 TypeError** ⇒ **整条回复报错**（不是静默失效）。
    2. 返回协议必须是 ``{"action": "continue", "modified_kwargs": {...}}``
       ⇒ 裸 ``{"enable_splitter": False}`` 被静默丢弃（``_extract_modified_kwargs``
       只认 ``response_payload.get("modified_kwargs")``）。
    3. 同一 hook 上多个 handler 是**链式**的
       （``hook_dispatcher.py:297``：``current_kwargs = dict(dispatch_result.kwargs)``）
       ⇒ 前一个必须复制整份 kwargs 再改，不能只回自己那一个键。
    """
    if not is_chatroom_session(session_id, chatroom_session_ids):
        return None
    return {"enable_splitter": False}


def build_hook_result(
    kwargs: Dict[str, Any],
    session_id: str,
    chatroom_session_ids: Iterable[str],
) -> Optional[Dict[str, Any]]:
    """完整的 Hook 返回值（**含整包复制**）。

    ⚠️ 只有确定要改才返回 ``None`` 之外的东西 ——
    **不是聊天室的会话 ⇒ 什么都不做**（``{"action": "continue"}``，
    不带 ``modified_kwargs``）⇒ 绝不影响真群聊。
    """
    if not is_chatroom_session(session_id, chatroom_session_ids):
        return {"action": "continue"}
    # ⚠️ **复制整份 kwargs** —— 只回一个键 ⇒ 协议校验失败 ⇒ 静默不生效
    new_kwargs = dict(kwargs)
    new_kwargs["enable_splitter"] = False
    return {"action": "continue", "modified_kwargs": new_kwargs}
