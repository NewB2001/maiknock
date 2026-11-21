# -*- coding: utf-8 -*-
"""MaiKnock planner 侧处理器 —— 逐条 item 的 relabel + 遮蔽。

**⚠️ 顺序（R6-28 / P18）**：planner 侧是 **先 relabel → 再遮蔽**，
与 replyer（blob）侧**相反**。原因：planner 侧的遮蔽判据用 `meta.timestamp`，
**不依赖文本** ⇒ 顺序无关。

**为什么 planner 侧要先 relabel**：
- `meta.timestamp` 是**原始时刻**，relabel 不会改它（我们只改文本）；
- 先 relabel 之后，遮蔽判定仍用原始 `meta.timestamp` ⇒ 结果一致；
- 而「先遮蔽」会**丢掉待改项**（被遮蔽的项也要改时间标签，
  否则它们虽然消失了，但若判定口径变化会不一致）。

**⚠️ relabel 只改文本，绝不动 `meta.timestamp`** ——
`meta.timestamp` 是判据本身，改了等于改答案。

**设计约束**：无 IO、无宿主依赖 ⇒ 可离线单测。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# ⚠️ **载体白名单统一在 `carriers.py`**（M4 第⑤步 J）——
#    planner 与 replyer 共用同一套判定，避免两边口径漂移。
# ⚠️ 本模块**不 import** carriers（由 plugin.py 预注册后注入）⇒
#    用 `set_carriers()` 在插件加载时注入；离线单测可直接 `sys.modules` 注入。
_CARRIERS = None


def set_carriers(mod) -> None:
    """注入 carriers 模块（由 plugin.py 在加载时调用）。"""
    global _CARRIERS
    _CARRIERS = mod


def _carriers():
    if _CARRIERS is None:
        raise RuntimeError("carriers 模块未注入（plugin.py 应调用 set_carriers）")
    return _CARRIERS


#: planner 侧历史消息的时间属性：`time="HH:MM:SS"`
#: （宿主 `planner_messages.py:64` 硬编码 `%H:%M:%S`，**不受配置影响**）
PLANNER_TIME_ATTR_RE = re.compile(r'(?P<attr>time=")(?P<val>\d{2}:\d{2}:\d{2})(?P<end>")')

#: planner 请求末尾的独立「时间：YYYY-MM-DD HH:MM:SS」item
#: ⚠️ **仅作兼容保留** —— 真正的判定在 `carriers.classify_planner_time_item`
#:    （R6-18 严格全串匹配，此处的 `(?m)^…$` 会被正文里的时间串误伤）。
PLANNER_CURRENT_TIME_RE = re.compile(
    r"(?m)^时间：(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2}:\d{2})$"
)

RELABEL_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class PlannerResult:
    """planner 侧处理结果。"""

    items: List[Dict[str, Any]]
    hidden_items: int = 0
    relabeled_items: int = 0
    current_time_relabeled: bool = False
    #: R5 丢弃的跨日提示数（落在隐藏区间内的那几条）
    r5_dropped: int = 0
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return "遮蔽 %d 项 / relabel %d 项%s" % (
            self.hidden_items,
            self.relabeled_items,
            (" / R5 丢 %d 条跨日提示" % self.r5_dropped) if self.r5_dropped else "",
        )


def _parse_iso_ts(value: Any) -> Optional[float]:
    """把 `meta.timestamp`（ISO 字符串）解析成 epoch。"""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        import datetime as _dt

        # `2026-11-21T16:26:38.658947`
        return _dt.datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _format_story(ts: float, offset_sec: float) -> str:
    return time.strftime(RELABEL_FORMAT, time.localtime(float(ts) + float(offset_sec)))


def _item_text(item: Dict[str, Any]) -> str:
    parts = item.get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _replace_item_text(item: Dict[str, Any], new_text: str) -> Dict[str, Any]:
    """返回**只替换了 text 的新 item**（⚠️ 不动 meta、不动其它字段）。"""
    new_item = dict(item)
    new_parts = []
    for p in item.get("parts") or []:
        if isinstance(p, dict) and p.get("type") == "text":
            q = dict(p)
            q["text"] = new_text
            new_parts.append(q)
        else:
            new_parts.append(p)
    new_item["parts"] = new_parts
    return new_item


def _relabel_tool_prefixes(
    text: str,
    msg_id_ts: Dict[str, float],
    offset_sec: float,
    car,
) -> str:
    """R6-17 第三种载体：`HH:MM:SS[msg_id:…][说话人]正文` → 补上故事日期。

    **时间源 = `msg_id` 反查**（`get_recent` 返回的消息里有 `message_id` + `timestamp`）。
    ⚠️ **查不到 msg_id ⇒ 原样保留**（宁可少改不可错改）——
    只有 `HH:MM:SS` 没有日期时，无法换算成故事时刻。

    ⚠️ **只改时分秒那部分**，`[msg_id:…][说话人]` 一个字都不动。
    """
    hits = car.classify_tool_prefixes(text)
    if not hits:
        return text
    out: List[str] = []
    cursor = 0
    changed = False
    for hit in hits:
        mid = hit.msg_id
        if not mid or mid not in msg_id_ts:
            continue  # 查不到 ⇒ 保留
        out.append(text[cursor:hit.hms_start])
        out.append(_format_story(msg_id_ts[mid], offset_sec))
        cursor = hit.hms_end
        changed = True
    if not changed:
        return text
    out.append(text[cursor:])
    return "".join(out)


def process_planner_items(
    items: Sequence[Dict[str, Any]],
    is_hidden: Callable[[float], bool],
    offset_sec: float,
    *,
    now_ts: Optional[float] = None,
    relabel_items: bool = True,
    relabel_current_time: bool = True,
    msg_id_ts: Optional[Dict[str, float]] = None,
    drop_crossday_in_hidden: bool = True,
) -> PlannerResult:
    """处理 planner 侧的 items：**先 relabel → 再遮蔽**。

    :param items: hook 载荷里的 items（`item_type` / `meta` / `parts`）
    :param is_hidden: 判定某真实时刻是否落在隐藏区间（**真实轴**）
    :param offset_sec: 累计偏移
    :param now_ts: 「现在」的 epoch 秒（用于区分「当前时间提示」与「跨日提示」）
    :param msg_id_ts: ``msg_id → 真实 epoch`` 映射（R6-17 第三种载体用）
    :param drop_crossday_in_hidden: R5 —— 跨日提示若落在隐藏区间内则丢弃
    """
    warnings: List[str] = []
    out: List[Dict[str, Any]] = []
    hidden = 0
    relabeled = 0
    current_time_done = False
    r5_dropped = 0

    if now_ts is None:
        now_ts = time.time()
    car = _carriers()

    # ---------- 第 0 步：R5（风险 B · 解法③）—— 跨日提示若落在隐藏区间 ⇒ 丢弃 ----------
    # ⚠️ **必须在 relabel 之前**判断（用 relabel 之前的**原始 content 值**、**真实时间轴**）——
    #    否则文本里的日期已在虚构轴、隐藏区间在真实轴 ⇒ 量纲不一致，必然判错（风险 H）。
    # ⚠️ **只针对跨日提示**（content 里的时间 ≠ now）；当前时间提示永远保留。
    # ⚠️ **只在 planner 侧存在**（宿主 `chat_loop_service.py:1204`
    #    `include_day_boundary_time_messages=request_kind == "planner"`）。
    r5_drop_idx: set = set()
    if drop_crossday_in_hidden:
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            text = _item_text(item)
            if not text:
                continue
            hit = car.classify_planner_time_item(text, now_ts)
            if hit is None or hit.kind != car.CARRIER_PLANNER_CROSSDAY:
                continue
            if is_hidden(hit.real_ts):
                r5_drop_idx.add(idx)
                r5_dropped += 1
                warnings.append(
                    "R5：跨日提示（%s）落在隐藏区间内，已丢弃" % hit.raw
                )

    # ---------- 第 1 步：relabel ----------
    for idx, item in enumerate(items):
        if idx in r5_drop_idx:
            continue  # R5 判定要丢的跨日提示
        if not isinstance(item, dict):
            out.append(item)
            continue
        text = _item_text(item)
        if not text:
            out.append(item)
            continue

        new_text = text
        did = False

        # ① 当前时间提示 / 跨日提示 —— **整条 content 恰好是它**才改（R6-18）
        if relabel_current_time:
            hit = car.classify_planner_time_item(text, now_ts)
            if hit is not None:
                story = car.format_story(hit.real_ts, offset_sec, with_date=True)
                new_text = text[: hit.start] + "时间：" + story + text[hit.end:]
                current_time_done = True
                did = True

        # ② 历史消息的 `time="HH:MM:SS"` 属性（时间源 = meta.timestamp）
        if relabel_items:
            ts = _parse_iso_ts((item.get("meta") or {}).get("timestamp"))

            def _repl(m: "re.Match[str]") -> str:
                if ts is None:
                    return m.group(0)
                return "%s%s%s" % (
                    m.group("attr"),
                    _format_story(ts, offset_sec),
                    m.group("end"),
                )

            new_text2 = PLANNER_TIME_ATTR_RE.sub(_repl, new_text)
            if new_text2 != new_text:
                did = True
            new_text = new_text2

        # ③ R6-17 第三种载体：`HH:MM:SS[msg_id:…][说话人]正文`（工具结果文本）
        if relabel_items and msg_id_ts:
            new_text3 = _relabel_tool_prefixes(new_text, msg_id_ts, offset_sec, car)
            if new_text3 != new_text:
                did = True
            new_text = new_text3

        if did:
            relabeled += 1
            out.append(_replace_item_text(item, new_text))
        else:
            out.append(item)

    if relabel_current_time and not current_time_done:
        warnings.append(
            "未找到 planner 侧的「时间：」item（relabel 漏掉当前时间）"
        )

    # ---------- 第 2 步：遮蔽（判据用 meta.timestamp，**与文本无关**） ----------
    kept: List[Dict[str, Any]] = []
    for item in out:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        ts = _parse_iso_ts((item.get("meta") or {}).get("timestamp"))
        if ts is not None and is_hidden(ts):
            hidden += 1
            continue  # 丢弃该 item
        kept.append(item)

    return PlannerResult(
        items=kept,
        hidden_items=hidden,
        relabeled_items=relabeled,
        current_time_relabeled=current_time_done,
        r5_dropped=r5_dropped,
        warnings=warnings,
    )
