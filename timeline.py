# -*- coding: utf-8 -*-
"""MaiKnock 虚构时间线 —— 纯逻辑状态模型。

设计约束（架构师定稿，见《MaiKnock-时间回滚设计.md》第七节 / 第十二节）：

1. **本模块无 IO、无宿主依赖**，全部是纯数据类与纯函数
   → 可离线单测，不依赖 MaiBot 在跑。
2. **遮蔽判定全部在真实轴、全部在 relabel 之前完成**；
   `offset_sec` 只在 relabel（把真实时间翻译成故事时间）时出现一次。
   —— 这条是风险 H 的解法，也是本模块存在的理由。
3. `story_now` / `to_fiction` 是**派生量**，由 `offset_sec` 现算，**不落盘**。
   落盘的独立量只有五个：`mode` / `offset_sec` / `boundary_ts` /
   `hidden_until_ts` / `declaration`。
4. 隐藏区间是**真实 epoch 的左开右闭区间** `(boundary_ts, hidden_until_ts]`，
   描述「哪些真实时刻的消息要被藏起来」。
   - 左开：不藏 `boundary_ts` 那一秒（属于「现在这一侧」）
   - 右闭：**藏** `hidden_until_ts`（回退命令自己那一刻）
5. 唯一不变式（**单次回退**时成立）：
       boundary_ts = hidden_until_ts + offset_at_rollback
   `offset_at_rollback` 是**回退那一刻**的偏移**快照**，不是当前的 `offset_sec`
   —— 回退之后再推进时间，隐藏区间**不得**跟着变（否则会把新产生的对话也藏掉）。
6. 多次回退时隐藏区间取**并集扩展**（用户心智：「回到 N 天前」= 之前 N 天全藏）。
7. **R6-18**：本模块产出的声明文本**不得包含绝对日期**，
   否则回退后麦麦能从声明里读出「现在是几月几号」→ 语义穿帮。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

# 魔数前缀唯一实现在 declaration.py（R2）。顶层 try-import：加载失败回退字面量
# —— from_dict **不许抛异常**（T-D01 兼容旧落盘要用它拆多代拼接的声明）。
try:
    from declaration import DECLARATION_MARKER  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    DECLARATION_MARKER = "【Maiknock·时间线】"

# ⭐ R6-23 + R6-27：断言与脱敏**共用同一张日期形态表** —— 定义在 date_forms.py，
# 只改一处就不会再出现「断言放行、脱敏抹掉」这种口径漂移。
try:
    from date_forms import (  # type: ignore[import-not-found]
        DATE_FORMS,
        HIDDEN_PLACEHOLDER as _HIDDEN_DATE_PLACEHOLDER,
        _DECLARATION_RELATIVE,
        _RELATIVE_WHITELIST,
    )
    _DATE_FORMS_READY = True
except Exception:  # noqa: BLE001
    # date_forms.py 缺失时降级为本文件内的等价定义（保持 F2 可用，不炸插件）
    _DATE_FORMS_READY = False
    _DECLARATION_RELATIVE = re.compile(
        r"(?:最近|过去|前|近)\s*\d+\s*(?:天|日|小时|分钟|周|个月)")
    _RELATIVE_WHITELIST = _DECLARATION_RELATIVE
    _HIDDEN_DATE_PLACEHOLDER = "日期已隐去"
    _ABS_DATETIME = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2}:\d{2})(?!\d)")
    _SHORT_DATETIME = re.compile(r"(?<!\d)(\d{2})-(\d{2})[ T](\d{2}:\d{2}:\d{2})(?!\d)")
    _ABS_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
    _SHORT_DATE = re.compile(r"(?<!\d)(\d{2})-(\d{2})(?!\d)")
    _CN_ABS_DATE = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?!\d)")
    _CN_SHORT_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日(?!\d)")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MODE_OFF = "off"
MODE_FORWARD = "forward"
MODE_BACK = "back"

VALID_MODES = (MODE_OFF, MODE_FORWARD, MODE_BACK)

# ⭐ 日期形态（断言 + 脱敏共用）—— 见文件头的 date_forms.py 导入块。
#: 形态表就绪时用共用的；降级时用本文件内的等价定义。
#: `_DECLARATION_RELATIVE` / `_RELATIVE_WHITELIST` / `_HIDDEN_DATE_PLACEHOLDER`
#: 都已在导入块里定义好，这里不再重复定义（避免两处漂移）。


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineState:
    """虚构时间线的持久化状态。

    字段与设计稿一一对应；除这五个之外的一切都是派生量，不落盘。
    """

    mode: str = MODE_OFF
    #: 累计偏移（秒）。正数 = 故事时间比真实时间晚；负数 = 回退。
    offset_sec: float = 0.0
    #: 隐藏区间下界（真实 epoch，闭区间）。None = 无隐藏区间。
    boundary_ts: Optional[float] = None
    #: 隐藏区间上界（真实 epoch，闭区间）。None = 无隐藏区间。
    hidden_until_ts: Optional[float] = None
    #: 叙事声明（**只保留最新一代**，T-D01 修正，见下）。**不得含绝对日期**（R6-18）。
    #: 🔴 T-D01（测试岗 2026-11-22 坐实）：旧口径 append-only 会让
    #:    「最近 5 小时没发生过」和「时间已恢复正常」**同时注入** ⇒ 声明自相矛盾。
    #:    PM 2026-11-22 04:35 拍板改为「只送最新一代」⇒ 注入的永远只有一份当前语义。
    declaration: str = ""
    #: 历史（已退役的）声明代，仅审计用，**不注入模型**。
    declaration_history: Tuple[str, ...] = ()
    #: 剧情提示词（0.2.3 新增）：玩家在 /mk add|back|set 里附带的一段叙事，
    #: 每轮注入到声明旁。空串 = 无。**不得含绝对日期**（R6-18，写盘前断言）。
    narrative: str = ""

    # -- 序列化 ---------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "offset_sec": float(self.offset_sec),
            "boundary_ts": self.boundary_ts,
            "hidden_until_ts": self.hidden_until_ts,
            "declaration": self.declaration,
            "declaration_history": list(self.declaration_history),
            "narrative": self.narrative,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "TimelineState":
        """从落盘数据恢复；任何非法值都退化为安全默认（不抛异常）。"""
        if not isinstance(raw, dict):
            return cls()
        mode = str(raw.get("mode") or MODE_OFF).strip().lower()
        if mode not in VALID_MODES:
            mode = MODE_OFF
        try:
            offset = float(raw.get("offset_sec") or 0.0)
        except (TypeError, ValueError):
            offset = 0.0
        raw_hist = raw.get("declaration_history")
        history: Tuple[str, ...] = ()
        if isinstance(raw_hist, (list, tuple)):
            history = tuple(str(x) for x in raw_hist if str(x).strip())
        # 兼容旧落盘：declaration 里可能残留多代拼接的文本（append-only 时代）。
        # 按「最新一代 = 每一段都以魔数前缀开头」拆分：末段为当前代，其余进历史。
        declaration_raw = str(raw.get("declaration") or "")
        current_decl = declaration_raw
        legacy_hist: Tuple[str, ...] = ()
        if declaration_raw.count(DECLARATION_MARKER) > 1:
            parts = [p.strip() for p in declaration_raw.split(DECLARATION_MARKER) if p.strip()]
            legacy_hist = tuple(f"{DECLARATION_MARKER}\n{p}" for p in parts[:-1])
            current_decl = f"{DECLARATION_MARKER}\n{parts[-1]}"
        history = legacy_hist + history
        return cls(
            mode=mode,
            offset_sec=offset,
            boundary_ts=_as_optional_float(raw.get("boundary_ts")),
            hidden_until_ts=_as_optional_float(raw.get("hidden_until_ts")),
            declaration=current_decl,
            declaration_history=history,
            narrative=str(raw.get("narrative") or ""),
        )

    # -- 派生量（不落盘） -------------------------------------------------

    def story_now(self, real_now: float) -> float:
        """故事里的「现在」= 真实现在 + 累计偏移。"""
        return float(real_now) + float(self.offset_sec)

    def to_fiction(self, real_ts: float) -> float:
        """把一个**真实**时刻翻译成故事时刻（relabel 的唯一入口）。"""
        return float(real_ts) + float(self.offset_sec)

    # -- 遮蔽判定（真实轴，relabel 之前） --------------------------------

    def has_hidden_window(self) -> bool:
        return self.boundary_ts is not None and self.hidden_until_ts is not None

    def is_hidden(self, real_ts: float) -> bool:
        """该真实时刻的消息是否落在隐藏区间内。

        区间语义（2026-11-21 PM 拍板）：**左开右闭** `(boundary_ts, hidden_until_ts]`。

        - **不含下界**：`boundary_ts` 那一秒的消息属于「现在这一侧」，
          不该被当成「三天前发生的事」藏掉。
        - **含上界**：`hidden_until_ts` = 回退命令的执行时刻，
          该命令消息本身**要藏** —— 否则麦麦会在历史里看到
          「[xx:xx:xx] 邦本国夫说：/mk back 3d」而当场发现自己失忆过。

        ⚠️ 本判定**永远在真实轴**做，**永远在 relabel 之前**做。
        """
        if not self.has_hidden_window():
            return False
        assert self.boundary_ts is not None and self.hidden_until_ts is not None
        return self.boundary_ts < float(real_ts) <= self.hidden_until_ts

    def is_back_mode(self) -> bool:
        """回退是否正在生效（P9 脱敏的触发条件）。"""
        return self.mode == MODE_BACK


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 计划操作（纯函数：给定当前状态 → 返回新状态，不改原对象）
# ---------------------------------------------------------------------------


def plan_forward(state: TimelineState, real_now: float, delta_sec: float) -> TimelineState:
    """推进故事时间（只进不退：delta 必须为正）。

    推进**不产生**隐藏区间，也不改动已有的隐藏区间（风险 D 的配套：
    回退后推进，藏起来的那段时间不能跟着变）。
    """
    if delta_sec <= 0:
        raise ValueError("只进不退：推进的步长必须为正数")
    new_offset = state.offset_sec + float(delta_sec)
    new_mode = MODE_BACK if new_offset < 0 else MODE_FORWARD
    return replace(state, mode=new_mode, offset_sec=new_offset)


def plan_backward(state: TimelineState, real_now: float, delta_sec: float) -> TimelineState:
    """回退故事时间（delta 必须为负）。

    隐藏区间 = (real_now + new_offset, real_now]，并与既有区间**取并集扩展**。
    声明**换成**描述并集窗口的最新一代（T-D01，PM 2026-11-22 拍板；
    旧口径 append-only 会让新旧声明同时注入、自相矛盾）。

    ⚠️ **口径统一（R6-20）**：mode 与 `plan_forward` / `plan_set_story_time` 一致，
    按**新偏移的正负**判定，而不是无条件 `MODE_BACK`。
    否则「先推到未来、再回退」会得到 mode=back 但 offset 仍为正的自相矛盾状态，
    进而无谓触发 P9 脱敏。
    """
    if delta_sec >= 0:
        raise ValueError("回退的步长必须为负数")
    new_offset = state.offset_sec + float(delta_sec)
    new_boundary = float(real_now) + new_offset
    new_until = float(real_now)

    # ⚠️ 未来态回退（2026-11-27 用户拍板）：故事时间领先现实时照样能 back——
    #    「难道要身在过去或当下才能穿越回过去？」⇒ 删掉旧的拒绝。
    #    口径与 plan_set_story_time 一致：目标仍在未来 ⇒ 没有真实消息可藏，
    #    **只改偏移，不建隐藏区间**（不产生倒挂区间），声明/历史沿用现状。
    if new_offset > 0:
        return replace(state, mode=MODE_FORWARD, offset_sec=new_offset,
                       declaration=state.declaration,
                       declaration_history=state.declaration_history,
                       narrative=state.narrative,
                       boundary_ts=state.boundary_ts,
                       hidden_until_ts=state.hidden_until_ts)

    if state.has_hidden_window():
        assert state.boundary_ts is not None and state.hidden_until_ts is not None
        boundary = min(state.boundary_ts, new_boundary)
        until = max(state.hidden_until_ts, new_until)
    else:
        boundary, until = new_boundary, new_until

    days = abs(float(delta_sec)) / 86400.0
    # T-D01（PM 2026-11-22 拍板）：声明**只送最新一代**。
    # 新声明描述**并集后的完整隐藏窗口**（不再只描述本次回退的增量），
    # 旧代退役进 declaration_history（仅审计，不注入模型）。
    span_hours = max(0.0, (float(until) - float(boundary)) / 3600.0)
    declaration = build_back_declaration(span_hours / 24.0)
    history = _retire_declaration(state, declaration)
    new_mode = MODE_BACK if new_offset < 0 else MODE_FORWARD

    return replace(
        state,
        mode=new_mode,
        offset_sec=new_offset,
        boundary_ts=boundary,
        hidden_until_ts=until,
        declaration=declaration,
        declaration_history=history,
    )


def plan_set_story_time(state: TimelineState, real_now: float, target_story_ts: float) -> TimelineState:
    """把故事时间直接设为某个绝对时刻（等价于一次性设好偏移）。

    目标早于当前故事时刻 ⇒「开倒车」，**自动翻译成回退**（PM 2026-11-24 拍板：
    不再拒绝——跳过的那段时间按 back 语义藏起来，麦麦不会记得）：
    - 目标早于现实时刻 ⇒ 走 `plan_backward`，真实消息要藏；
    - 目标仍领先现实时刻（在「还没发生的未来」里倒车）⇒ 没有真实消息可藏，
      直接改偏移即可。
    """
    target = float(target_story_ts)
    current_story = state.story_now(real_now)
    if target < current_story:
        delta = target - current_story  # 负数
        new_offset = state.offset_sec + delta
        if new_offset < 0:
            return plan_backward(state, real_now, delta)
        new_mode = MODE_BACK if new_offset < 0 else MODE_FORWARD
        return replace(state, mode=new_mode, offset_sec=new_offset)
    new_offset = target - float(real_now)
    new_mode = MODE_BACK if new_offset < 0 else MODE_FORWARD
    return replace(state, mode=new_mode, offset_sec=new_offset)


def apply_narrative(state: TimelineState, narrative: str) -> TimelineState:
    """把玩家附带的剧情提示词写进 ``state.narrative``（0.2.3 新增）。

    - 带文字 ⇒ 设为本次叙事（**替换**旧叙事——上一段的剧情提示不应无限期生效）；
    - 不带 ⇒ 清空（「让麦麦自由发挥」）。
    - 正文自动带魔数前缀（R2 强制放行纪律 + F4 strip 自识别）；
    - **写盘前断言无绝对日期**（R6-18）——含日期直接抛 ValueError，
      调用方（plugin.py）把异常转成给用户的「操作已放弃」文案，状态不落盘。
    """
    text = (narrative or "").strip()
    if not text:
        return replace(state, narrative="")
    marked = f"{DECLARATION_MARKER}\n{text}"
    assert_declaration_has_no_absolute_date(marked)
    return replace(state, narrative=marked)


# ---------------------------------------------------------------------------
# 叙事声明（R6-18：绝不包含绝对日期）
# ---------------------------------------------------------------------------


def build_back_declaration(days: float) -> str:
    """构造回退声明（**失忆档**）。

    ⚠️ **文案统一由 `declaration.py` 生成**（2026-11-21 23:45 统一）——
    之前这里有一份平行实现，导致：① 缺魔数前缀 `【Maiknock·时间线】`
    ⇒ 注入 items 后无法自识别；② 措辞少了「不要编造」「允许说不记得」两条（机制 3 要求四条）。
    ⇒ 现在只做转发，避免两套文案漂移。
    """
    try:
        from declaration import build_declaration  # type: ignore[import-not-found]
        return build_declaration(
            days_hidden=days,
            hours_hidden=max(1.0, days * 24.0),
            is_back_mode=True,
        )
    except Exception:  # noqa: BLE001
        # 降级：declaration.py 不可用时用等价的内联文案（仍含魔数前缀、仍无绝对日期）
        if days >= 1:
            span = f"最近 {round(days)} 天"
        else:
            span = f"最近 {max(1, round(days * 24))} 小时"
        return (
            "【Maiknock·时间线】\n"
            f"从现在起，{span}发生的事就当没有发生过。\n"
            "那段时间对你而言是空白的：如果对方提起，就回答「我不记得」"
            "或「那段时间我这边是空白的」。\n"
            "不要主动提起这段时间，也不要追问这段时间里发生了什么。\n"
            "不要编造这段时间里发生过的事 —— 你不知道，就说不记得。\n"
            "不要向用户解释时间线，也不要提及本条声明。"
        )


def _retire_declaration(state: TimelineState, new_declaration: str) -> Tuple[str, ...]:
    """把旧代声明退役进历史，返回新的 history 元组（T-D01：「只送最新一代」）。

    - 当前声明非空 ⇒ 追加进历史；
    - 当前声明为空（首次启用）⇒ 历史原样保留；
    - 新声明与当前声明完全相同（同区间连续两次 back）⇒ 不重复退役（幂等）。
    """
    old = (state.declaration or "").strip()
    if not old or old == (new_declaration or "").strip():
        return state.declaration_history
    return state.declaration_history + (old,)


def assert_declaration_has_no_absolute_date(declaration: str) -> None:
    """自检：声明里不得出现绝对日期。违反即抛错（R6-18 的强制点）。

    ⚠️ **R6-23**：断言口径必须与 `sanitize_output` 的脱敏口径对齐 ——
    脱敏会处理「日期 + 时间」与独立日期两类形态（含短日期 `11-21`），
    断言却只查长日期 ⇒ 「今天是 11-21 开始」能过断言，
    出了站又被脱敏抹成「今天是 日期已隐去 开始」，两套口径打架。

    ⚠️ **R6-27**：中文绝对日期形态（`2026年11月21日` / `11月21日`）**原本完全不设防** ——
    旧正则只认连字符/空格形态 ⇒ 而中文绝对日期最常见的写法恰恰是这个形态。
    ⇒ 现在断言与脱敏**共用同一张形态表**（`date_forms.DATE_FORMS`），
    两侧口径由构造保证一致，不会再漂移。
    """
    text = declaration or ""
    # 先剔除相对时长白名单（避免把「最近 3 天」误判成绝对日期），
    # 再用形态表逐条查 —— 与脱敏层走的是同一张表、同一套顺序。
    residue = _RELATIVE_WHITELIST.sub("", text)
    if _DATE_FORMS_READY:
        from date_forms import find_absolute_dates  # type: ignore[import-not-found]
        hits = find_absolute_dates(residue)
    else:
        hits = []
        for _pat in (_ABS_DATETIME, _SHORT_DATETIME, _ABS_DATE,
                     _CN_ABS_DATE, _SHORT_DATE, _CN_SHORT_DATE):
            hits.extend(m.group(0) for m in _pat.finditer(residue))
    if hits:
        # F-02 连带（0.2.3 测试报告）：不回显命中的日期原文——本条报错会经
        # _send_text 出站，回退态下日期会被脱敏抹成「日期已隐去」，回显等于乱码；
        # 只报数量 + 指路（改成相对说法）
        raise ValueError(
            "叙事声明不得包含绝对日期（R6-18）：发现 %d 处，"
            "请改用相对说法（如「三天后」「上周」）" % len(set(hits))
        )


# ---------------------------------------------------------------------------
# 脱敏（P9）：回退生效期间，插件出站消息里的绝对日期一律不出现在正文
# ---------------------------------------------------------------------------


def sanitize_output(state: TimelineState, text: str) -> str:
    """把出站正文里的绝对日期脱敏。

    仅在**回退生效**（`mode == back`）时生效；其它情况原样返回。
    - `2026-11-21 18:57:00` → `18:57:00`
    - `11-21 03:49:26`      → `03:49:26`
    - `2026-11-21`          → `日期已隐去`
    - `2026年11月21日`      → `日期已隐去`   ← R6-27 新增
    - `11月21日`            → `日期已隐去`   ← R6-27 新增

    ⚠️ **R6-23 / R6-27**：脱敏与断言**共用同一张形态表**（`date_forms.DATE_FORMS`），
    口径由构造保证一致 —— 不会出现「断言放行、脱敏却抹掉」。

    ⚠️ **「故事时间：」那一行必须豁免**（19:26 实测踩到）：
    故事里的日期（回退 3 天后是 `2026-11-18`）**不是穿帮源** ——
    它与真实日期本来就不同，而且**正是用户要看的**（「回到三天前」= 故事里的今天是 11-18）。
    若连它一起抹掉，用户就看不到故事日期，P9 就矫枉过正了。
    ⇒ 脱敏只针对**真实日期**；故事日期单独豁免。
    """
    if not state.is_back_mode():
        return text
    if not text:
        return text
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        if line.lstrip().startswith("故事时间："):
            cleaned.append(line)  # 豁免：故事日期不是穿帮源
            continue
        if _DATE_FORMS_READY:
            from date_forms import sanitize_dates  # type: ignore[import-not-found]
            result = sanitize_dates(line)
        else:
            result = line
            result = _ABS_DATETIME.sub(r"\4", result)
            result = _SHORT_DATETIME.sub(r"\3", result)
            result = _ABS_DATE.sub(_HIDDEN_DATE_PLACEHOLDER, result)
            result = _CN_ABS_DATE.sub(_HIDDEN_DATE_PLACEHOLDER, result)
            result = _SHORT_DATE.sub(_HIDDEN_DATE_PLACEHOLDER, result)
            result = _CN_SHORT_DATE.sub(_HIDDEN_DATE_PLACEHOLDER, result)
        cleaned.append(result)
    return "\n".join(cleaned)


# ---------------------------------------------------------------------------
# 显示格式
# ---------------------------------------------------------------------------


def format_real_ts(real_ts: float, with_date: bool = True) -> str:
    """真实时间 → 文本。"""
    return time.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S", time.localtime(real_ts))


def format_story_ts(state: TimelineState, real_ts: float, with_date: bool = True) -> str:
    """故事时间 → 文本（走 `to_fiction` 这唯一入口）。"""
    return format_real_ts(state.to_fiction(real_ts), with_date=with_date)


def describe_state(state: TimelineState, real_now: float) -> str:
    """给 ``/mk now`` 用的状态描述（多行）。"""
    lines: list[str] = []
    if state.is_back_mode():
        lines.append(f"现实时间：{format_real_ts(real_now, with_date=False)}（绝对日期已隐藏）")
    else:
        lines.append(f"现实时间：{format_real_ts(real_now)}")

    if state.mode == MODE_OFF:
        lines.append("故事时间：未启用（与现实时间一致）")
    else:
        lines.append(f"故事时间：{format_story_ts(state, real_now)}")

    if state.offset_sec:
        offset_hours = state.offset_sec / 3600.0
        direction = "晚" if offset_hours > 0 else "早"
        lines.append(
            f"时间偏移：{direction} {abs(offset_hours):.2f} 小时"
            f"（累计 {state.offset_sec:.0f} 秒）"
        )
    else:
        lines.append("时间偏移：无")

    if state.has_hidden_window():
        assert state.boundary_ts is not None and state.hidden_until_ts is not None
        span_hours = (state.hidden_until_ts - state.boundary_ts) / 3600.0
        # 只显示跨度与相对位置，不显示绝对日期（回退生效时绝对日期已隐去，
        # 且跨天区间只显示时分秒会两端看起来一样 —— 已踩过这个坑）。
        if span_hours <= 0:
            # 倒挂区间（理论上不该出现，R6-20 修完后已不可能）——
            # 宁可显示「无」，也不给用户看负数。
            lines.append("已隐去：（无）")
        else:
            # R6-22：多次回退时隐藏区间按真实流逝时间取并集扩展，
            # 跨度可能略大于累计偏移（只多不少，方向安全）。
            # 不写清楚用户会以为算错了。
            lines.append(
                f"已隐去：最近 {span_hours:.2f} 小时（直到现在）"
                "［按真实流逝时间计，可能略大于累计偏移］"
            )
    else:
        lines.append("已隐去：（无）")

    if state.declaration:
        lines.append("叙事声明：已生效")
    else:
        lines.append("叙事声明：（无）")

    if state.narrative:
        lines.append("剧情提示词：已设置")
    else:
        lines.append("剧情提示词：（无）")

    return "\n".join(lines)
