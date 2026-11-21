# -*- coding: utf-8 -*-
"""MaiKnock · relabel **载体白名单**（M4 第⑤步 J）—— 纯逻辑，无宿主依赖。

**为什么要有这个模块**：relabel 只能改「宿主生成的固定载体」，
**绝不能对所有 User item 正文做正则替换** —— 否则用户自己在正文里写的
「时间：2026-01-01」也会被改（穿帮）。这是设计稿的**风险 J / R6-18**。

**载体清单（R6-5 三类时间源 + R6-17 第三种形态）**：

| 载体 | 形态 | 出现在 | 时间源 | 判据 |
|---|---|---|---|---|
| `planner_current_time` | `时间：2026-11-22 00:04:38`（**整条 item 就是它**） | planner | **content 里的 ts** | 全串匹配 |
| `planner_crossday` | 同上，但 ts 是历史时刻 | planner | **content 里的 ts** | 全串匹配 + 与 now 差 > 阈值 |
| `planner_history_attr` | `<message ... time="18:07:05" ...>` | planner | **`meta.timestamp`** | 只改这个属性 |
| `replier_current_time` | `当前时间：2026-11-21 23:56:47` | replyer blob | **content 里的 ts** | **必须在第一个块起点之前** |
| `replier_block_head` | `[18:08:14] 说话人说：…` | replyer blob | 匹配到的真实消息 ts | 只改这个方括号 |
| `tool_prefix` | `HH:MM:SS[msg_id:…][说话人]正文` | 工具结果文本 | **msg_id**（时间只是展示） | R6-17 第三种 |

**⚠️ 关于 R6-6 容差（实测推翻了设计稿的 2 秒）**：
设计稿说「content_ts 与 now 差 > 2 秒 ⇒ 跨日提示」。**实测不成立**：
2026-11-22 00:04 的真实快照里，「时间：」item 的 content ts = `00:04:38`，
而 `meta.timestamp` = `00:04:40.795` ⇒ **差 2.795 秒 > 2 秒** ⇒ 按 2 秒判据会把
**当前时间提示误判成跨日提示**。

**为什么放宽到 60 秒是安全的**：
- 当前时间提示的 content ts = `datetime.now()`（`chat_loop_service.py:877`）⇒ 差几秒；
- 跨日提示的 content ts = `msg.timestamp`（**那条历史消息自己的时刻**，
  `chat_loop_service.py:1090`）⇒ 差几小时/几天。
⇒ **60 秒能干净分开**，且分类结果**只影响日志措辞、不影响 relabel 值**
（两类的 relabel 都是 `content_ts + offset`）。

---

"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# 载体类别常量
# ---------------------------------------------------------------------------

CARRIER_PLANNER_CURRENT_TIME = "planner_current_time"
CARRIER_PLANNER_CROSSDAY = "planner_crossday"
CARRIER_PLANNER_HISTORY_ATTR = "planner_history_attr"
CARRIER_REPLYER_CURRENT_TIME = "replier_current_time"
CARRIER_REPLYER_BLOCK_HEAD = "replier_block_head"
CARRIER_TOOL_PREFIX = "tool_prefix"

#: 当前时间提示与「现在」的容差（秒）—— **仅用于诊断分类，不影响 relabel 值**。
#:
#: ⚠️ **设计稿的 2 秒已被实测推翻两次**：
#: 1. 真实快照里 content ts 与 `meta.timestamp` 差 **2.795 秒** > 2 秒；
#: 2. 连「快照时刻 vs content 时刻」都差 **65 秒**（快照写入滞后于请求构建）。
#:
#: ⇒ 判据改为 **1 小时**：跨日锚点是**历史消息自己的时刻**（差几小时~几天），
#: 当前时间提示是 `datetime.now()`（同一个请求内，差不到 1 秒）⇒ 1 小时能干净分开。
#:
#: **⚠️ 关键：分类结果只影响日志措辞，不影响 relabel 值** ——
#: 两类的 relabel 都是 `content_ts + offset`，所以**即使分类错了也不会改错值**。
CURRENT_TIME_TOLERANCE_SECONDS = 3600.0

# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------

#: planner 侧的时间提示 item —— **整条 content 恰好就是它**（R6-18 严格判定）。
#: ⚠️ 必须用 `fullmatch` 语义，**不能用 `search`**：
#:    用户在正文中间写「时间：xxx」不该被改。
PLANNER_TIME_ITEM_RE = re.compile(
    r"时间：(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<hms>\d{2}:\d{2}:\d{2})"
)

#: planner 侧历史消息的时间属性 —— 只改 `time="..."` 这个属性。
PLANNER_TIME_ATTR_RE = re.compile(
    r'(?P<attr>\btime=")(?P<val>\d{2}:\d{2}:\d{2})(?P<end>")'
)

#: replyer 侧当前时间提示。
REPLIER_CURRENT_TIME_RE = re.compile(
    r"当前时间：(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<hms>\d{2}:\d{2}:\d{2})"
)

#: replyer 侧对话块起点 —— **只认行首**（`(?m)^`），
#: 多行消息体的续行不会误切。
BLOCK_START_RE = re.compile(r"(?m)^\[(?P<hms>\d{2}:\d{2}:\d{2})\]")

#: R6-17 第三种载体：`HH:MM:SS[msg_id:…][说话人]正文`
#: （图片消息 + 非视觉模型时，工具结果文本里会出现）。
#: ⚠️ **只认行首** ⇒ 正文里夹带的「12:00:00[msg_id:xxx][A]」不算载体。
TOOL_PREFIX_RE = re.compile(
    r"(?m)^(?P<hms>\d{2}:\d{2}:\d{2})"
    r"(?:\[msg_id:(?P<msg_id>[^\]]+)\])?"
    r"\[(?P<speaker>[^\]]+)\]"
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CarrierHit:
    """一次载体命中。"""

    #: 载体类别（见 CARRIER_* 常量）
    kind: str
    #: 载体的真实时刻（epoch 秒）
    real_ts: float
    #: 该载体在 content 里的 span（用于精确替换）
    start: int
    end: int
    #: 载体里携带的时间串原文（不含标签）
    raw: str
    #: **只指时分秒那部分**的 span（R6-17 第三种载体要精确改它，
    #: 不能动后面的 `[msg_id:…][说话人]`）
    hms_start: int = -1
    hms_end: int = -1
    #: R6-17 第三种载体里的 msg_id（没有则空串）
    msg_id: str = ""


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------


def parse_ts(date_str: str, hms: str) -> Optional[float]:
    """把 `YYYY-MM-DD` + `HH:MM:SS` 解析成 epoch 秒。失败返回 None。"""
    try:
        dt = datetime.strptime("%s %s" % (date_str, hms), "%Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def format_story(real_ts: float, offset_sec: float, with_date: bool = True) -> str:
    """真实时刻 → 故事时刻串。

    :param with_date: ``True`` ⇒ `YYYY-MM-DD HH:MM:SS`；``False`` ⇒ `HH:MM:SS`
    """
    dt = datetime.fromtimestamp(real_ts + offset_sec)
    if with_date:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# 分类判据
# ---------------------------------------------------------------------------


def classify_planner_time_item(
    text: str, now_ts: float
) -> Optional[CarrierHit]:
    """判定 planner 侧的时间提示 item。

    **R6-18 严格判定**：整条 content **恰好**是 `时间：{ts}` ⇒ 才是载体。
    用户在正文中间写的「时间：xxx」**不算**载体、不被改。

    :returns: ``None`` 表示不是载体；否则返回 ``CarrierHit``
        （``kind`` 区分「当前时间提示」与「跨日提示」）
    """
    if not text:
        return None
    stripped = text.strip()
    m = PLANNER_TIME_ITEM_RE.fullmatch(stripped)
    if not m:
        return None
    real_ts = parse_ts(m.group("date"), m.group("hms"))
    if real_ts is None:
        return None
    kind = (
        CARRIER_PLANNER_CURRENT_TIME
        if abs(real_ts - now_ts) <= CURRENT_TIME_TOLERANCE_SECONDS
        else CARRIER_PLANNER_CROSSDAY
    )
    return CarrierHit(
        kind=kind,
        real_ts=real_ts,
        start=m.start(),
        end=m.end(),
        raw="%s %s" % (m.group("date"), m.group("hms")),
    )


def classify_replier_current_time(
    text: str, now_ts: float
) -> Optional[CarrierHit]:
    """判定 replyer 侧 blob 里的「当前时间：{ts}」。

    **R6-18 严格判定**：它必须位于**第一个对话块起点之前**
    （即宿主引导语之后的紧邻位置）——
    实测结构是::

        \\n\\n你正在和邦本国夫聊天，这是你们之前聊的内容:
        当前时间：2026-11-21 23:56:47
        [18:08:14] 日向雏田(你)说：…

    ⇒ 用户在**块正文里**写的「当前时间：xxx」不会被误改。
    """
    if not text:
        return None
    first_block = BLOCK_START_RE.search(text)
    limit = first_block.start() if first_block else len(text)
    m = REPLIER_CURRENT_TIME_RE.search(text, 0, limit)
    if not m:
        return None
    real_ts = parse_ts(m.group("date"), m.group("hms"))
    if real_ts is None:
        return None
    return CarrierHit(
        kind=CARRIER_REPLYER_CURRENT_TIME,
        real_ts=real_ts,
        start=m.start(),
        end=m.end(),
        raw="%s %s" % (m.group("date"), m.group("hms")),
    )


def classify_tool_prefixes(text: str) -> list:
    """找出 R6-17 第三种载体：`HH:MM:SS[msg_id:…][说话人]正文`。

    ⚠️ **只认行首**（`(?m)^`）—— 正文里夹带的同形态串不算载体。
    """
    hits = []
    for m in TOOL_PREFIX_RE.finditer(text or ""):
        hits.append(
            CarrierHit(
                kind=CARRIER_TOOL_PREFIX,
                real_ts=0.0,  # 时间只是展示；真实时刻靠 msg_id 反查
                start=m.start(),
                end=m.end(),
                raw=m.group(0),
                hms_start=m.start("hms"),
                hms_end=m.end("hms"),
                msg_id=m.group("msg_id") or "",
            )
        )
    return hits


def classify_planner_history_attrs(text: str) -> list:
    """找出 planner 侧历史消息的 `time="HH:MM:SS"` 属性。

    :returns: ``[(span_start, span_end, hms), ...]``
    """
    out = []
    for m in PLANNER_TIME_ATTR_RE.finditer(text or ""):
        out.append((m.start("val"), m.end("val"), m.group("val")))
    return out
