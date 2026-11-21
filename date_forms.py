# -*- coding: utf-8 -*-
"""日期形态表 —— 断言（R6-18）与脱敏（P9）**共用同一张表**。

**为什么要单独一个模块（R6-23 + R6-27）**：
- R6-23 发现：断言只查长日期、脱敏查长短两种 ⇒ **两套口径漂移**，
  「今天是 11-21 开始」能过断言、出了站又被脱敏抹掉。
- R6-27 进一步发现：`_ABS_DATE` / `_SHORT_DATE` 只认**连字符/空格**形态 ⇒
  `2026年11月21日`、`11月21日` 这类**中文绝对日期完全漏网** ——
  而中文绝对日期最常见的写法恰恰是这个形态。
- ⇒ 两边必须**共用同一张形态表**，定义一次、两处引用，以后新增形态只改一处。

**设计约束（无 IO、无宿主依赖，纯逻辑）**：
1. 每种形态登记两件事：**匹配正则** + **归一化输出**（脱敏后替换成什么）。
2. 顺序有意义：**长形态必须排在短形态之前** ——
   `2026年11月21日` 要先被「年月日」形态吃掉，不能先被「月日」形态切成半截。
3. 白名单（`_RELATIVE_WHITELIST`）只登记**真正的相对表述**。
   ⚠️ R6-27 的教训：白名单里**绝不能放绝对日期形态**
   （旧版把「2026年」「11月21日」当白名单 = 主动允许把绝对日期写进声明）。
"""

from __future__ import annotations

import re
from typing import List, Pattern, Tuple

#: 脱敏后的占位符
HIDDEN_PLACEHOLDER = "日期已隐去"


class DateForm:
    """一种日期形态：匹配正则 + 归一化输出。"""

    __slots__ = ("name", "pattern", "repl")

    def __init__(self, name: str, pattern: Pattern[str], repl: str) -> None:
        self.name = name
        self.pattern = pattern
        self.repl = repl

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<DateForm %s>" % self.name


# ---------------------------------------------------------------------------
# 形态表（**长形态在前、短形态在后**）
# ---------------------------------------------------------------------------

#: 长日期 + 时间：`2026-11-21 18:57:00` / `2026-11-21T18:57:00`
_ABS_DATETIME = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2}:\d{2})(?!\d)")
#: 短日期 + 时间：`11-21 03:49:26`
_SHORT_DATETIME = re.compile(r"(?<!\d)(\d{2})-(\d{2})[ T](\d{2}:\d{2}:\d{2})(?!\d)")
#: 长日期：`2026-11-21`
_ABS_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
#: 短日期：`11-21`
_SHORT_DATE = re.compile(r"(?<!\d)(\d{2})-(\d{2})(?!\d)")
#: 中文长日期：`2026年11月21日` / `2026 年 11 月 21 日`
_CN_ABS_DATE = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?!\d)")
#: 中文短日期：`11月21日`
_CN_SHORT_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日(?!\d)")

#: ⭐ 唯一的形态表 —— 断言与脱敏都从这里取，**不各自定义正则**
#: （顺序：带时间的先于不带时间的；长的先于短的）
DATE_FORMS: Tuple[DateForm, ...] = (
    DateForm("abs_datetime", _ABS_DATETIME, r"\4"),
    DateForm("short_datetime", _SHORT_DATETIME, r"\3"),
    DateForm("abs_date", _ABS_DATE, HIDDEN_PLACEHOLDER),
    DateForm("cn_abs_date", _CN_ABS_DATE, HIDDEN_PLACEHOLDER),
    DateForm("short_date", _SHORT_DATE, HIDDEN_PLACEHOLDER),
    DateForm("cn_short_date", _CN_SHORT_DATE, HIDDEN_PLACEHOLDER),
)

#: 只登记**真正的相对表述**：量词前缀 + 数字 + 时间单位。
#: ⚠️ R6-27：绝不能把 `\d{4}\s*年`（绝对年份）或 `\d{1,2}\s*月\s*\d{1,2}\s*日`（绝对日期）
#:    放进这里 —— 那是「主动允许绝对日期进声明」。
_RELATIVE_WHITELIST: Pattern[str] = re.compile(
    r"(?:最近|过去|前|近)\s*\d+\s*(?:天|日|小时|分钟|周|个月)"
)

#: 声明里允许出现的相对时间描述（不含绝对日期）—— 兼容旧名字
_DECLARATION_RELATIVE = _RELATIVE_WHITELIST


def strip_relative(text: str) -> str:
    """把相对时长表述剔除，剩下的才是「可能有绝对日期」的残余。

    白名单的作用是**避免误杀**（「最近 3 天」里的数字不是日期），
    不是放行 —— 白名单太宽不会导致漏放行，只会导致误杀
    （把真日期当成白名单剔掉）。
    """
    return _RELATIVE_WHITELIST.sub("", text or "")


def find_absolute_dates(text: str) -> List[str]:
    """找出文本里所有绝对日期形态的片段（供断言与诊断使用）。"""
    found: List[str] = []
    residue = strip_relative(text)
    for form in DATE_FORMS:
        for m in form.pattern.finditer(residue):
            found.append(m.group(0))
    return found


def has_absolute_date(text: str) -> bool:
    """文本里是否存在任何绝对日期形态。"""
    return bool(find_absolute_dates(text))


def sanitize_dates(text: str) -> str:
    """把文本里的绝对日期形态按表归一化（脱敏）。

    ⚠️ 归一化输出与形态表绑定：带时间的形态只保留时分秒（`\4` / `\3`），
    不带时间的形态整段替换为「日期已隐去」。
    """
    if not text:
        return text
    result = text
    for form in DATE_FORMS:
        result = form.pattern.sub(form.repl, result)
    return result
