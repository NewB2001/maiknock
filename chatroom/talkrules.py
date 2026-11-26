# -*- coding: utf-8 -*-
"""麦麦聊天室 · bot_config 里「提及强制触发」的自动维护。

**背景**（2026-11-26 复盘，见 09 文档 ㉚）：
最初的做法是给每个房间写一条「查看频率=0」规则来关掉宿主「静默自说自话」——
**错了**：查看频率是这条流上进 Planner 的一切入口的总闸，
value=0 连「被 @点名的台词」也一起静默（``maisaka_reasoning_engine``：
「查看频率为 0，静默接收并完成历史维护，不进入 Planner」），两个 Bot 全变哑巴。

**现在的做法（阀门方案）**：
- **不再写任何 value=0 规则**；历史上写过的由本模块自动清掉（幂等）；
- 「平时不自言自语、递话时才开口」改由 ``talkvalve.TalkValve`` 用宿主官方能力
  ``frequency.set_adjust`` 在运行时拧（平时 0、递话前 1、收尾回 0）；
- ``mentioned_bot_reply = true`` 保留：台词统一带 @名字，命中提及强制触发，
  双保险（阀门开着时消息批概率本来就放行，提及触发是兜底）。

**为什么插件直接改 ``bot_config.toml``**：宿主给插件的能力只有 ``config.get/*``（只读），
没有写宿主配置的通道；而 ``bot_config.toml`` 是 600ms 热重载，改完即生效、不用重启。
写法：tomlkit 解析（**保注释**）→ 只动自己管的键 → tmp + ``os.replace`` 原子替换。

**会动哪些键（就这些，别的不碰）**：
- ``chat.reply_timing.talk_rule_groups`` 里 item_id 带 ``webui_virtual_group_`` 前缀的规则（**清掉**）；
- 顺手清掉历史遗留的 ``webui/*`` 通配规则（会连坐别的插件的 webui 群流）；
- ``chat.reply_timing.mentioned_bot_reply``（只在不是 true 时置 true）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 聊天室房间规则在 item_id 上的前缀（virtual_group_id 的形状）——历史上写过，现在要清
ROOM_RULE_PREFIX = "webui_virtual_group_"

#: 平台名（聊天室的所有群流都在 webui 平台上）
RULE_PLATFORM = "webui"

#: 需要置 true 的强制触发开关
MENTION_FLAG = "mentioned_bot_reply"


def _as_dict(rule: Any) -> Optional[Dict[str, Any]]:
    try:
        d = rule.unwrap() if hasattr(rule, "unwrap") else rule
        return dict(d) if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _is_ours(rule_dict: Dict[str, Any]) -> bool:
    """这条规则是不是聊天室写的（按 item_id 前缀认）⇒ 现在一律清掉。"""
    return str(rule_dict.get("item_id") or "").startswith(ROOM_RULE_PREFIX)


def _is_stale_wildcard(rule_dict: Dict[str, Any]) -> bool:
    """2026-11-26 手工配置留下的 ``webui/*`` 通配静音规则（会连坐别的插件，要清掉）。"""
    return (
        str(rule_dict.get("platform") or "") == RULE_PLATFORM
        and str(rule_dict.get("item_id") or "") == "*"
        and str(rule_dict.get("rule_type") or "group") == "group"
        and float(rule_dict.get("value") or 0.0) == 0.0
    )


def ensure_talk_config(config_path: Path) -> bool:
    """确保 bot_config 里没有聊天室写的静音规则、且提及强制触发开着。

    **改了文件返回 True**，没动返回 False。幂等：每次调用都把历史上写过的
    ``webui_virtual_group_*`` 规则与 ``webui/*`` 通配规则清掉。

    ⚠️ 任何失败都返回 False 并**不抛异常**（配置写不进去不该把插件带崩，
    调用方会记 warning；宿主 600ms 热重载，下次同步会再试）。
    """
    path = Path(config_path)
    if not path.is_file():
        return False
    try:
        import tomlkit
    except ImportError:
        return False
    try:
        with path.open("r", encoding="utf-8") as f:
            doc = tomlkit.load(f)

        chat = doc.get("chat")
        if chat is None:
            chat = tomlkit.table()
            doc["chat"] = chat
        reply_timing = chat.get("reply_timing")
        if reply_timing is None:
            reply_timing = tomlkit.table()
            chat["reply_timing"] = reply_timing

        changed = False

        # ── ① mentioned_bot_reply ──
        flag = reply_timing.get(MENTION_FLAG)
        flag_val = flag.unwrap() if hasattr(flag, "unwrap") else flag
        if flag_val is not True:
            reply_timing[MENTION_FLAG] = True
            changed = True

        # ── ② 规则组：清掉我们写的规则 + 遗留通配 ──
        # ⚠️ 缺表就跳过这段（没有我们管的规则可清），但 **必须落到写盘** ——
        #    changed 为 True 时任何提前 return 都会把改动丢掉（空文件分支踩过）。
        groups_container = reply_timing.get("talk_rule_groups")
        if groups_container is not None:
            active_group = str(groups_container.get("active_group") or "默认")
            groups = groups_container.get("groups")
            target = None
            if groups is not None and len(groups) > 0:
                for group in groups:
                    gd = _as_dict(group)
                    if gd is not None and str(gd.get("name") or "") == active_group:
                        target = group
                        break
            if target is not None:
                rules = target.get("rules")
                if rules is not None:
                    kept: List[Any] = []
                    for rule in list(rules):
                        rd = _as_dict(rule)
                        if rd is None:
                            kept.append(rule)
                            continue
                        if _is_ours(rd) or _is_stale_wildcard(rd):
                            changed = True          # 聊天室的静音规则 / 遗留通配 ⇒ 清掉
                        else:
                            kept.append(rule)
                    if changed:
                        new_rules = tomlkit.array()
                        for rule in kept:
                            new_rules.append(rule)
                        target["rules"] = new_rules

        if not changed:
            return False

        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(tomlkit.dumps(doc))
        os.replace(tmp, path)       # ⚠️ 原子替换（惯例）
        return True
    except Exception:  # noqa: BLE001
        return False
