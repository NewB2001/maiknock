# -*- coding: utf-8 -*-
"""MaiKnock 叙事声明 —— 机制 3（管"她怎么解释矛盾"）。

**注入方式（R6-12 改造后）**：**每轮 hook 注入到 `items` 末尾**，两个 hook 各一处
（`maisaka.planner.before_request` + `maisaka.replyer.before_model_request`）。
- ❌ 不再用 `maisaka.context.append` —— 它注入的 `InternalContextMessage`
  在 replyer 侧被 `_is_replyer_filtered_history_message`（`maisaka_generator_base.py:663-669`）
  滤掉（只收 `SessionBackedMessage`）⇒ **声明只到 planner**；
  且一次性 append 会被 `select_llm_context_messages` 的倒扫窗口越过 ⇒ 几十条消息后无声消失。
- ✅ 每轮注入 ⇒ **永不被挤出**、**两侧都拿得到**、
  **R2 的放行问题自动消失**（注入的 item `meta.timestamp` = now，天然在隐藏区间外）。

**声明内容不得含绝对日期（R6-18）**：
机制 3 的原始模板写着「现在是 2026-11-18 14:32」—— **这会让麦麦读出绝对日期**，
回退三天后她却知道「现在是几月几号」⇒ 语义当场穿帮。
⇒ 声明**只用相对表述**（「最近 3 天」「那段时间」），**不写具体日期**。

**魔数前缀**：正文必须带 `【Maiknock·时间线】`（满足 R2 的强制放行纪律）。

**设计约束**：无 IO、无宿主依赖 ⇒ 可离线单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

#: 声明的魔数前缀（R2：带此前缀的 item 一律强制放行，不参与遮蔽）
DECLARATION_MARKER = "【Maiknock·时间线】"

#: 注入项的 item_type（**用 UserMessageItem** —— 与宿主历史同构，最容易被模型当成"上下文"读）
DECLARATION_ITEM_TYPE = "UserMessageItem"

#: 判断一个 item 是不是声明（靠魔数前缀自识别）
DECLARATION_RE = re.compile(r"^\s*" + re.escape(DECLARATION_MARKER))


def is_declaration_item(item: Dict[str, Any]) -> bool:
    """这个 item 是不是叙事声明（靠魔数前缀自识别）。"""
    if not isinstance(item, dict):
        return False
    for part in item.get("parts") or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text") or ""
        if isinstance(text, str) and DECLARATION_RE.match(text):
            return True
    return False


def build_declaration(
    *,
    days_hidden: float,
    hours_hidden: float = 0.0,
    is_back_mode: bool = True,
    story_time_text: str = "",
) -> str:
    """构造叙事声明正文（**失忆档**）。

    **措辞四条要求**（机制 3，唯一实现档）：
    1. 必须让她**知道自己不记得** —— 否则她会试图解释「那三天怎么想不起来」→ 变成谜语
    2. 必须允许她**自然地说「不记得」** —— 而不是回避话题
    3. 必须禁止她**编造**那三天的事 —— 否则空白会被脑补填满，方向不可控
    4. 必须禁止她**提及本声明本身** —— 否则会暴露插件存在

    🔴 **PM 2026-11-22 00:55 口径修正 —— 第 3 条已不作硬性验收**：
    实测隐藏生效后她答「拉面」（编造）而非「我不记得」。PM 拍板：
    「**只要不刻意回答之前的旧答案就好了**，编造有几率蒙中但**不是刻意**即可」。
    ⇒ **验收标准 =「不刻意复述旧答案」**，不是「必须说不记得」。
    ⇒ **声明文本保持原样、不加强措辞**（避免日常对话变拘谨）。
    ⇒ 下方第 3 条保留作 04:03 原始规格记录，**不要再据此"修 bug"**。

    ⚠️ **不含任何绝对日期**（R6-18）—— 只说相对时长。

    :param days_hidden: 隐藏了多少天（可为小数）
    :param hours_hidden: 隐藏了多少小时（days 不足 1 时用）
    :param is_back_mode: 是否是回退（失忆）档
    :param story_time_text: 故事时间的**文本**（若为空则完全不提时间）
    """
    lines: List[str] = [DECLARATION_MARKER]

    if days_hidden >= 1:
        span = "最近 %d 天" % int(round(days_hidden))
    elif hours_hidden >= 1:
        span = "最近 %d 小时" % int(round(hours_hidden))
    else:
        span = "刚才一小段时间"

    if is_back_mode:
        # —— 失忆档（PM 04:03 拍板，本期唯一实现）——
        lines.append(
            "从现在起，%s发生的事就当没有发生过。" % span
        )
        lines.append(
            "那段时间对你而言是空白的：如果对方提起，就回答「我不记得」"
            "或「那段时间我这边是空白的」。"
        )
        lines.append(
            "不要主动提起这段时间，也不要追问这段时间里发生了什么。"
        )
        lines.append("不要编造这段时间里发生过的事 —— 你不知道，就说不记得。")
    else:
        # —— 非回退档（前进 / 关闭）：只需轻量提示，避免模型误以为失忆 ——
        lines.append("当前的故事时间是%s。" % (story_time_text or "（见时间提示）"))

    lines.append("不要向用户解释时间线，也不要提及本条声明。")
    return "\n".join(lines)


def build_declaration_item(
    declaration_text: str,
    *,
    item_id: str = "",
    timestamp: str = "",
) -> Dict[str, Any]:
    """把声明包装成一个可注入的 item。

    ⚠️ **`meta.timestamp` = now** ⇒ 天然落在隐藏区间外 ⇒ **不会被遮蔽**（R2 自动满足）。
    """
    return {
        "item_type": DECLARATION_ITEM_TYPE,
        "meta": {
            "item_id": item_id,
            "logical_turn_id": None,
            "timestamp": timestamp,
        },
        "parts": [
            {"type": "text", "text": declaration_text},
        ],
    }


def strip_declarations(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去掉已有的声明 item（避免重复注入）。"""
    return [it for it in items if not is_declaration_item(it)]


def has_absolute_date(text: str) -> bool:
    """文本里有没有绝对日期（R6-18 兜底检查）。

    ⚠️ 复用 `date_forms` 的**同一张形态表** ⇒ 与断言/脱敏口径一致。
    """
    try:
        from date_forms import has_absolute_date as _has
    except Exception:  # noqa: BLE001
        return False
    try:
        return bool(_has(text))
    except Exception:  # noqa: BLE001
        return False


def inject_declaration(
    items: List[Dict[str, Any]],
    declaration_text: str,
    *,
    item_id: str = "",
    timestamp: str = "",
) -> List[Dict[str, Any]]:
    """把声明注入到 items **末尾**（R6-12）。

    - 先去掉已有的声明（幂等）
    - 再追加到末尾 ⇒ **永不被倒扫窗口挤出**
    """
    base = strip_declarations(items)
    base.append(
        build_declaration_item(declaration_text, item_id=item_id, timestamp=timestamp)
    )
    return base
