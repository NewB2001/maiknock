# -*- coding: utf-8 -*-
"""麦麦聊天室 · 时光机旁白（纯逻辑，可离线单测）。

**这是什么**：时光机面板「生成剧情」背后的文本拼装 —— 用户写「一行一条剧情事件」，
这里把时间单位换成**相对表述**、逐行拼成旁白正文。

**两条铁规**（02 文档 R6-18 / 06 文档）：
1. **绝不写绝对日期** —— 只写「三天后」这类相对表述。写「2026 年 3 月 1 日」会跟现实日期打架
   （现实是 11 月，剧情可能是「三个月后已是盛夏」，绝对日期说不通）。
2. **不生成精妙叙事** —— 用户写什么，旁白就说什么（「剧情写留白，Bot 补脑更自然」）。

**时间单位只影响提示语，不改变宿主时间** —— 它唯一的作用就是把「3 日」变成旁白开头的「三天后，」。

**纯逻辑模块**：只依赖标准库 ⇒ 可离线单测。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: 单位 → 中文量词（拼进相对表述时用；`日` 说成 `天` 更自然）
_UNIT_WORD = {
    "年": "年",
    "月": "个月",
    "日": "天",
    "时": "个小时",
    "分": "分钟",
    "秒": "秒",
}

#: 数字 → 中文（只覆盖常用的小数字；超出就退回阿拉伯数字，绝不写绝对日期）
_CN_NUM = {
    1: "一", 2: "两", 3: "三", 4: "四", 5: "五",
    6: "六", 7: "七", 8: "八", 9: "九", 10: "十",
    11: "十一", 12: "十二", 15: "十五", 20: "二十", 30: "三十",
}


def _cn_num(n: int) -> str:
    """整数 → 中文（1~30 常用词，其余退回阿拉伯数字）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n in _CN_NUM:
        return _CN_NUM[n]
    return str(n)


def relative_phrase(number: Any, unit: str) -> str:
    """生成相对时间表述（如「三天后」）。

    - 数字缺失 / ≤0 ⇒ 返回空串（用户没选时间 ⇒ 旁白不带时间前缀）
    - **只产出相对表述，绝不产出绝对日期**
    """
    try:
        amount = int(number)
    except (TypeError, ValueError):
        return ""
    if amount <= 0:
        return ""
    word = _UNIT_WORD.get(str(unit or "").strip(), str(unit or "").strip())
    if not word:
        return ""
    return f"{_cn_num(amount)}{word}后"


def build_narrator_texts(
    lines: Optional[List[str]],
    number: Any = 0,
    unit: str = "",
) -> List[str]:
    """把用户的「一行一条事件」拼成旁白正文（逐条）。

    :param lines: 每行一条剧情事件（空行会被丢弃）
    :param number: 时间数字（只用来拼相对表述）
    :param unit: 时间单位
    :returns: 旁白文本列表（每条对应一段旁白消息）

    ⚠️ 若给了相对时间，就把「三天后，」**并进第一条**事件（旁白自然读作
    「三天后，<第一件事>」）；没给就不加前缀。
    ⚠️ **不改写用户正文其余部分** —— 用户写什么旁白就说什么。
    """
    clean = [str(line).strip() for line in (lines or []) if str(line).strip()]
    if not clean:
        return []
    phrase = relative_phrase(number, unit)
    if phrase:
        clean = [f"{phrase}，{clean[0]}"] + clean[1:]
    return clean


def build_narrator_segment(texts: List[str], narrator: Dict[str, Any]) -> Dict[str, Any]:
    """把旁白文本列表包成一个「剧情段」形状（页面时间线要的形状）。

    ⚠️ 旁白**占一个普通段位**（kind = ``plot``），可被「回到此处」截断（FR-17）。
    """
    return {
        "kind": "plot",
        "voided": False,
        "messages": [
            {
                "speaker_member_id": narrator.get("member_id") or "narrator",
                "speaker_name": narrator.get("display_name") or "剧情旁白",
                "kind": "narrator",
                "content": text,
            }
            for text in texts
        ],
    }
