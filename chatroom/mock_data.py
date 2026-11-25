# -*- coding: utf-8 -*-
"""麦麦聊天室 · 演示数据（壳阶段原样搬过来，**内容一字未改**）。

**纯逻辑模块**：无 IO、无宿主依赖 => 可离线单测。
⚠️ 模块级常量里的可变对象是**同一个对象**（踩坑 33）——
   登记表必须是**自己的副本**，改一个角色的名字不许改坏常量本身。
   （壳阶段 plugin.py:352 已有这个纪律，本模块沿用）

本模块只提供**出厂演示数据**；正式版接真数据源后，
这里的角色/房间会被账本里的真实记录取代（见 08 文档 §2.1 的逐项映射表）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

VERSION = "0.1.0"

STATUS_READY = "ready"

STATUS_GATEWAY_OFF = "gateway_off"

STATUS_STOPPED = "stopped"

STATUS_LABELS = {
    STATUS_READY: "可接入",
    STATUS_GATEWAY_OFF: "未启用网关",
    STATUS_STOPPED: "未运行",
}

DELAY_NAMED_SECONDS = 23

DELAY_SPONTANEOUS_SECONDS = 50

DELAY_MIN_SECONDS = 3

DELAY_MAX_SECONDS = 50

MEMBER_POOL: List[Dict[str, Any]] = [
    {
        "member_id": "role_naruto",
        "kind": "user_role",
        "display_name": "鸣人",
        "persona_label": "漩涡鸣人",
        "sort_order": 1,
        "status": None,
        "status_label": "我饰演",
        "bot_id": None,
        "bot_name": None,
        "avatar": "🧑",
        "editable": True,
    },
    {
        "member_id": "role_sasuke",
        "kind": "user_role",
        "display_name": "佐助",
        "persona_label": "宇智波佐助",
        "sort_order": 2,
        "status": None,
        "status_label": "我饰演",
        "bot_id": None,
        "bot_name": None,
        "avatar": "😤",
        "editable": True,
    },
    {
        "member_id": "bot_hinata",
        "kind": "bot",
        "display_name": "雏田",
        "persona_label": "日向雏田",
        "sort_order": 1,
        "status": STATUS_READY,
        "status_label": STATUS_LABELS[STATUS_READY],
        "bot_id": "7465370ac85c480e84eca97e1a0fc606",
        "bot_name": "麦麦一号",
        "avatar": "🌸",
        "editable": False,
    },
    {
        "member_id": "bot_sakura",
        "kind": "bot",
        "display_name": "小樱",
        "persona_label": "千手小樱",
        "sort_order": 3,
        "status": STATUS_STOPPED,
        "status_label": STATUS_LABELS[STATUS_STOPPED],
        "bot_id": "478de351a42d4ae59a4f1ed9a6a0c2b3",
        "bot_name": "麦麦二号",
        "avatar": "🍑",
        "editable": False,
    },
    # ── 私聊专用 Bot：为了让「最近聊过排序」有东西可看而加 ──
    # 三个接入状态各一个，界面上能对照展示（不塞进群聊房间，两种模式数据分开）。
    {
        "member_id": "bot_kakashi",
        "kind": "bot",
        "display_name": "卡卡西",
        "persona_label": "旗木卡卡西",
        "sort_order": 4,
        "status": STATUS_READY,
        "status_label": STATUS_LABELS[STATUS_READY],
        "bot_id": "11111111111111111111111111111111",
        "bot_name": "麦麦三号",
        "avatar": "😷",
        "editable": False,
    },
    {
        "member_id": "bot_ino",
        "kind": "bot",
        "display_name": "井野",
        "persona_label": "山中井野",
        "sort_order": 5,
        "status": STATUS_GATEWAY_OFF,
        "status_label": STATUS_LABELS[STATUS_GATEWAY_OFF],
        "bot_id": "22222222222222222222222222222222",
        "bot_name": "麦麦四号",
        "avatar": "🌼",
        "editable": False,
    },
    {
        "member_id": "bot_shikamaru",
        "kind": "bot",
        "display_name": "鹿丸",
        "persona_label": "奈良鹿丸",
        "sort_order": 6,
        "status": STATUS_STOPPED,
        "status_label": STATUS_LABELS[STATUS_STOPPED],
        "bot_id": "33333333333333333333333333333333",
        "bot_name": "麦麦五号",
        "avatar": "☁️",
        "editable": False,
    },
]

_IDENTITY: Dict[str, Tuple[str, str]] = {
    "role_naruto": ("webui", "user.naruto"),
    "role_sasuke": ("webui", "user.sasuke"),
    "bot_hinata": ("webui", "bot.7465370ac85c480e84eca97e1a0fc606"),
    "bot_sakura": ("webui", "bot.478de351a42d4ae59a4f1ed9a6a0c2b3"),
    "bot_kakashi": ("webui", "bot.11111111111111111111111111111111"),
    "bot_ino": ("webui", "bot.22222222222222222222222222222222"),
    "bot_shikamaru": ("webui", "bot.33333333333333333333333333333333"),
}

for _member in MEMBER_POOL:
    _member["platform"], _member["account"] = _IDENTITY[_member["member_id"]]

MANAGED_BOTS: List[Dict[str, Any]] = [
    {
        "bot_id": "7465370ac85c480e84eca97e1a0fc606",
        "bot_name": "麦麦一号",
        "peer_member_id": "bot_hinata",
    },
    {
        "bot_id": "478de351a42d4ae59a4f1ed9a6a0c2b3",
        "bot_name": "麦麦二号",
        "peer_member_id": "bot_sakura",
    },
    {
        "bot_id": "11111111111111111111111111111111",
        "bot_name": "麦麦三号",
        "peer_member_id": "bot_kakashi",
    },
    {
        "bot_id": "22222222222222222222222222222222",
        "bot_name": "麦麦四号",
        "peer_member_id": "bot_ino",
    },
    {
        "bot_id": "33333333333333333333333333333333",
        "bot_name": "麦麦五号",
        "peer_member_id": "bot_shikamaru",
    },
]

NARRATOR: Dict[str, Any] = {
    "member_id": "narrator",
    "kind": "narrator",
    "display_name": "剧情旁白",
    "persona_label": "旁白",
    "sort_order": 0,
    "status": None,
    "status_label": "系统",
    "bot_id": None,
    "bot_name": None,
    "avatar": "📖",
    "editable": False,
    # 平台同样是 `webui`（自定义平台会被适配器名单拦掉）；账号用 G2 实测脚本里的
    # 那个 `g1_narrator` —— **平台 + 账号必须永久固定**，否则旁白会裂成两张名片。
    "platform": "webui",
    "account": "g1_narrator",
    "fixed": True,
}

_SEED_MEMBER_MAP: Dict[str, Dict[str, Any]] = {
    member["display_name"]: member for member in MEMBER_POOL
}

_SEED_MEMBER_MAP[NARRATOR["display_name"]] = NARRATOR

_MESSAGE_COUNTER = {"n": 0}

def _build_segment(
    seq: int,
    kind: str,
    rows: List[Tuple[str, str, str, float]],
) -> Dict[str, Any]:
    """把一段的原始行组装成段结构。

    ``rows`` 每项是 ``(发言人显示名, 消息类型, 正文, 相对秒数)``，
    顺序即时间线上的顺序：**我方的台词在前，这一轮的 Bot 回复在后**。

    ``kind``：
      * ``normal`` —— 普通对话段（我方台词 + Bot 回复）
      * ``plot``   —— 剧情段（时光机生成），发言人是固定身份「剧情旁白」
    """
    messages: List[Dict[str, Any]] = []
    for speaker, msg_kind, content, offset in rows:
        member = _SEED_MEMBER_MAP.get(speaker)
        if member is None:
            continue
        _MESSAGE_COUNTER["n"] += 1
        messages.append(
            {
                "message_id": f"seed_{_MESSAGE_COUNTER['n']:03d}",
                "speaker_member_id": member["member_id"],
                "speaker_name": member["display_name"],
                "speaker_kind": member["kind"],
                "kind": msg_kind,
                "content": content,
                "time": _clock(offset),
            }
        )
    return {"seq": seq, "kind": kind, "voided": False, "messages": messages}

ROOMS: List[Dict[str, Any]] = [
    {
        "room_id": "room_ramen",
        "name": "拉面店门口",
        "mode": "stage",
        "created_at": "2026-11-22 19:12",
        "member_ids": ["role_naruto", "role_sasuke", "bot_hinata", "bot_sakura"],
    },
    {
        "room_id": "room_mission",
        "name": "任务分配室",
        "mode": "stage",
        "created_at": "2026-11-22 19:20",
        "member_ids": ["role_naruto", "role_sasuke", "bot_sakura"],
    },
    {
        "room_id": "room_school",
        "name": "忍者学校",
        "mode": "stage",
        "created_at": "2026-11-22 19:31",
        "member_ids": ["role_naruto", "role_sasuke", "bot_hinata"],
    },
]

REPLY_TEMPLATES: Dict[str, List[str]] = {
    "bot_hinata": [
        "「{echo}」……鸣人君，别、别介意，我不在意这些的。",
        "那个……{echo}——听起来是件开心的事呢。",
        "呜……{echo}。不过，请不要再打架了，好吗？",
        "「{echo}」……我、我会记住的。",
    ],
    "bot_sakura": [
        "……你们俩能不能别在拉面店门口吵。",
        "喂！{echo}——这种事要一起扛啦，笨蛋。",
        "{echo}？哼，我可不会输给这种小场面。",
    ],
    "bot_kakashi": [
        "「{echo}」……嗯，有点意思。",
        "抱歉我迟到了，路上遇到点小麻烦。{echo}？",
        "{echo}——这种事，交给我吧。",
    ],
    "bot_ino": [
        "喂喂，{echo}——这可是都听说了哦。",
        "别小看山中一族的心转身之术，{echo}瞒不过我。",
        "哼，井野我可不会输给你。",
    ],
    "bot_shikamaru": [
        "「{echo}」……真麻烦。",
        "啊——{echo}这种事，想想都累。",
        "算了，反正也没什么大不了的。",
    ],
}

STICKER_POOL: Dict[str, List[str]] = {
    "bot_hinata": ["害羞", "捂脸", "点头"],
    "bot_sakura": ["生气", "叉腰", "爆炸"],
    "bot_kakashi": ["无奈", "看书", "眯眼"],
    "bot_ino": ["得意", "挑眉", "加油"],
    "bot_shikamaru": ["叹气", "躺平", "无聊"],
}

def _clock(offset_seconds: float = 0.0) -> str:
    """返回 HH:MM 形式的假时刻。"""
    return time.strftime("%H:%M", time.localtime(time.time() + offset_seconds))
