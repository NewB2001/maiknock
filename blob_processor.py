# -*- coding: utf-8 -*-
"""MaiKnock blob 处理器 —— replyer 侧（1.4.8 唯一路径）的遮蔽 + relabel。

**⚠️ 顺序（R6-28 / P18）**：replyer 侧必须 **先遮蔽 → 后 relabel**，
与 planner 侧**相反**。原因：
- 遮蔽要靠**块边界**切块，块边界靠**未改写的** `[HH:MM:SS]` 识别；
- 而 relabel 改的就是块首那个 `[HH:MM:SS]`；
⇒ **先 relabel 会让块起点正则失效 ⇒ 切不出块 ⇒ 遮蔽彻底失效。**

**⚠️ relabel 只改块首那个方括号**（P23），**不能全文替换** ——
块正文里出现的 `11-21 16:50:07`（MaiKnock 诊断里的）不能动（R6-18）。
⇒ 必须用块起点正则的**同一个 match 的 span** 去改写。

**设计约束**：无 IO、无宿主依赖 ⇒ 可离线单测。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Dict, List, Optional, Sequence, Tuple

try:
    # ⚠️ 兄弟模块不能裸 import —— 宿主 `sys.path` 不含插件目录（踩坑 66）
    # ⇒ 由 `plugin.py` 预注册进 `sys.modules["matcher"]` 后再 exec 本模块
    from matcher import (  # type: ignore[import-not-found]
        BLOCK_START_RE,
        BlobBlock,
        Candidate,
        MatchReport,
        match_blocks,
        split_blob,
    )
except Exception:  # noqa: BLE001
    # 降级：模块不可用时抛 ImportError，让 plugin.py 明确报错，
    # **不要**在这里静默吞掉 —— 静默降级是这个项目最贵的坑（踩坑 66 / R6-29）
    raise

#: 载体白名单模块（M4 第⑤步 J）—— 由 `plugin.py` 预注册后注入。
_CARRIERS = None


def set_carriers(mod) -> None:
    """注入 carriers 模块（由 plugin.py 在加载时调用）。"""
    global _CARRIERS
    _CARRIERS = mod


def _carriers():
    if _CARRIERS is None:
        raise RuntimeError("carriers 模块未注入（plugin.py 应调用 set_carriers）")
    return _CARRIERS

#: 当前时间提示（**代码级常量**，来自宿主 `reply_prompt.py:339`，
#: **不依赖 prompt 模板占位符** —— 模板改版就会变）
#: ⚠️ **仅作兼容保留** —— 真正的判定在 `carriers.classify_replier_current_time`
#:    （R6-18 严格判定：必须在第一个块起点之前）。
CURRENT_TIME_RE = re.compile(
    r"(?m)^当前时间：(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2}:\d{2})$"
)

#: relabel 后的块首格式（**统一带年份**，与 planner 侧一致、跨年安全）
RELABEL_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class BlobResult:
    """一次 blob 处理的结果（供告警/计数用）。"""

    #: 处理后的 blob 文本
    text: str
    #: 匹配报告
    report: MatchReport
    #: 被删除的块数
    hidden_blocks: int = 0
    #: 被 relabel 的块数
    relabeled_blocks: int = 0
    #: `当前时间：` 是否被 relabel
    current_time_relabeled: bool = False
    #: 告警信息
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            "遮蔽 %d 块 / relabel %d 块 / %s"
            % (self.hidden_blocks, self.relabeled_blocks, self.report.summary())
        )


def _to_fiction(ts: float, offset_sec: float) -> float:
    """真实 epoch → 故事 epoch（**唯一换算入口**，与 timeline.to_fiction 一致）。"""
    return float(ts) + float(offset_sec)


def _format_story(ts: float, offset_sec: float) -> str:
    return time.strftime(RELABEL_FORMAT, time.localtime(_to_fiction(ts, offset_sec)))


def process_blob(
    blob: str,
    candidates: Sequence[Candidate],
    is_hidden: Callable[[float], bool],
    offset_sec: float,
    *,
    relabel_blocks: bool = True,
    relabel_current_time: bool = True,
) -> BlobResult:
    """处理 replyer 侧的 blob：**先遮蔽 → 后 relabel**。

    :param blob: replyer 侧那一整块文本
    :param candidates: `get_recent` 构造的候选集（`matcher.build_candidates`）
    :param is_hidden: 判定某真实时刻是否落在隐藏区间（**真实轴**）——
                     通常传 `timeline.TimelineState.is_hidden`
    :param offset_sec: 累计偏移（relabel 用；**遮蔽判定不用它**）
    :param relabel_blocks: 是否给幸存块补日期
    :param relabel_current_time: 是否改写 `当前时间：`
    """
    warnings: List[str] = []

    # ---------- 第 1 步：切块（**此时文本未改**） ----------
    blocks = split_blob(blob)
    matched, report = match_blocks(blocks, candidates)

    if report.unmatched_blocks:
        warnings.append(
            "有 %d 个块未能对齐到真实消息（已保留不删）：%s"
            % (len(report.unmatched_blocks), report.unmatched_blocks)
        )
    if report.total and report.ratio < 1.0:
        warnings.append("匹配率 %.0f%%（%s）" % (report.ratio * 100, report.summary()))

    # ---------- 第 2 步：判定要删哪些块 ----------
    to_delete: List[BlobBlock] = []
    for blk in blocks:
        cand = matched.get(blk.index)
        if cand is None:
            continue  # 未匹配 ⇒ 保留不删（宁可少遮不可错删）
        if not is_hidden(cand.ts):
            continue
        if blk.is_last:
            # 末块边界未知 ⇒ 保守不删 + 告警（P19）
            warnings.append(
                "末块（#%d）落在隐藏区间内但边界未知，已保留不删" % blk.index
            )
            continue
        to_delete.append(blk)

    # ---------- 第 3 步：按块删除（从后往前，避免偏移错乱） ----------
    text = blob
    for blk in sorted(to_delete, key=lambda b: b.start, reverse=True):
        end = blk.end if blk.end is not None else len(text)
        text = text[: blk.start] + text[end:]

    hidden_blocks = len(to_delete)

    # ---------- 第 3.5 步：移除宿主的「对话局面与关键事实」摘要段 ----------
    # 🔴 **失忆穿帮的直接原因**：摘要里摘录了麦麦之前的回复原话，
    #    而它**不在对话块结构里** ⇒ 遮蔽层碰不到它。
    # ⚠️ 只在**确实遮蔽了块**时才动（没有隐藏 ⇒ 摘要无害）。
    facts_masked = False
    if hidden_blocks > 0:
        text, facts_masked = _mask_facts_section(text)
        if facts_masked:
            warnings.append(
                "已移除宿主的「对话局面与关键事实」摘要段（它摘录了被遮蔽期的回复）"
            )

    # ---------- 第 4 步：relabel（**在删除之后**，改的是幸存块） ----------
    relabeled = 0
    if relabel_blocks:
        # ⚠️ **必须先改 `当前时间：`** —— 它那一行含 `[...]` 形态的日期，
        # 若先做块首 relabel，它可能被误当成块首处理。
        if relabel_current_time:
            text, current_time_done = _relabel_current_time(text, offset_sec)
            if not current_time_done:
                warnings.append(
                    "未找到「当前时间：」提示行（relabel 漏掉当前时间）—— "
                    "兜底判据是代码级常量，不依赖模板占位符"
                )
        deleted_heads = [b.head_line for b in to_delete]  # noqa: F841（日志排障用）
        text, relabeled = _relabel_block_heads(
            text, blocks, matched, {b.index for b in to_delete}, offset_sec)
        if relabeled == 0 and blocks:
            warnings.append("没有任何块被 relabel（可能块首格式与预期不符）")

    # ---------- 第 5 步：relabel 当前时间提示（若上面没做） ----------
    if relabel_current_time and not current_time_done:
        text, current_time_done = _relabel_current_time(text, offset_sec)
        if not current_time_done:
            warnings.append(
                "未找到「当前时间：」提示行（relabel 漏掉当前时间）—— "
                "兜底判据是代码级常量，不依赖模板占位符"
            )

    return BlobResult(
        text=text,
        report=report,
        hidden_blocks=hidden_blocks,
        relabeled_blocks=relabeled,
        current_time_relabeled=current_time_done,
        warnings=warnings,
    )


def _relabel_block_heads(
    text: str,
    blocks: Sequence[BlobBlock],
    matched: Dict[int, Candidate],
    deleted_indices: Collection[int],
    offset_sec: float,
) -> Tuple[str, int]:
    """给**幸存块**的块首补日期。

    ⚠️ **只改块首那个方括号**（用 match 的 span），不动正文（P23 / R6-18）。

    ⚠️⚠️ **不能用字符偏移反查块**（踩坑 R6-30 延伸）：
    删除块之后文本偏移全变了 ⇒ `m.start()` 与原块 `start` 对不上 ⇒ 会匹配到**错误的块**
    （实测把 `当前时间：` 行当成块首改了）。
    ⇒ **正确做法**：按**块首方括号文本**建立索引，在**新文本**上重新扫描。

    ⚠️⚠️⚠️ **同键多块必须用 FIFO 队列**（测试岗 T-C03，2026-11-22 坐实）：
    同一时分秒、跨天的两个块，方括号键相同；若按**单值**键建索引，后者覆盖前者
    ⇒ 两块都被标成同一天 ⇒ 日期改错（生产数据里同一秒多条消息很常见）。
    ⇒ 队列按**幸存块在文本中的出现顺序**入队（未匹配块也入队，占位 None）；
      重扫描 `finditer` 的顺序 == 块在文本中的顺序（块边界只由行首块首定义，R6-19）；
      每命中一次弹出一个，天然对齐同键的多个块。
    ⇒ 被删除的块不在新文本里 ⇒ 不入队（重扫描也扫不到它们）。
    """
    head_to_cands: Dict[str, deque] = {}
    for blk in blocks:
        if blk.index in deleted_indices:
            continue  # 被删除的块不在新文本里，不入队
        m = BLOCK_START_RE.match(blk.head_line)
        if m:
            head_to_cands.setdefault(m.group(0), deque()).append(matched.get(blk.index))

    if not head_to_cands:
        return text, 0

    out: List[str] = []
    pos = 0
    count = 0
    for m in BLOCK_START_RE.finditer(text):
        queue = head_to_cands.get(m.group(0))
        if not queue:
            continue  # 不是已知块首（或该键的队列已弹空）⇒ 不动
        cand = queue.popleft()
        if cand is None:
            continue  # 该位置是未匹配块（保留不删）⇒ 不动日期
        out.append(text[pos:m.start()])
        out.append("[%s]" % _format_story(cand.ts, offset_sec))
        pos = m.end()
        count += 1
    out.append(text[pos:])
    return "".join(out), count


#: 宿主注入的「对话局面与关键事实」段落的**起点标记**。
#: ⚠️ 代码级常量（`maisaka_expression_selector.py:221` 的 f-string 字面量）
#: ⇒ **不依赖 prompt 模板占位符**。
FACTS_SECTION_MARKER = "对话局面与关键事实："

#: 该段落的**终点标记** —— 宿主 replyer 的尾部提示。
#: ⚠️ 代码级常量（`creative_language.py:90-92` 三个变体的**公共前缀**）
#:    「你正在和对方私聊,现在请你读读之前的聊天记录…」
FACTS_SECTION_END_MARKER = "你正在和对方私聊"


def _mask_facts_section(text: str) -> Tuple[str, bool]:
    """移除宿主的「对话局面与关键事实」摘要段落。

    🔴 **为什么必须移除（2026-11-22 00:35 实测发现）**：
    宿主会在 blob 后面追加一段**对话摘要**（`maisaka_expression_selector.py:221`
    `reference_parts.append(f"对话局面与关键事实：\\n{chat_anlyze}")`），
    里面**摘录了麦麦之前的回复原话**。

    ⚠️ **它不在对话块结构里**（没有 `[HH:MM:SS]` 块首）⇒
    **匹配层与遮蔽层根本碰不到它** ⇒ **失忆穿帮的直接原因**。

    实测：回退 2 小时后，对话块被正确遮蔽（blob 3720 → 747 字节），
    但摘要里仍写着「邦哥哥最喜欢吃的……是草莓蛋糕哦」⇒ **答案被直接喂给模型**。

    ⇒ **有块被遮蔽时，整段移除**（宁可少给上下文，也不能泄漏）。
    ⚠️ 找不到终点标记 ⇒ **不动**（宁可少遮不可错删）。
    """
    start = text.find(FACTS_SECTION_MARKER)
    if start < 0:
        return text, False
    end = text.find(FACTS_SECTION_END_MARKER, start)
    if end < 0:
        return text, False
    # 保留终点标记本身（它是宿主尾部提示，不是摘要的一部分）
    return text[:start] + FACTS_SECTION_MARKER + "\n（本段已因时间线遮蔽而移除）\n" + text[end:], True


def _relabel_current_time(text: str, offset_sec: float) -> Tuple[str, bool]:
    """改写 `当前时间：YYYY-MM-DD HH:MM:SS` 那一行。

    ⚠️ **用代码级常量正则**（字面量来自宿主 `reply_prompt.py:339`），
    **不依赖 prompt 模板占位符**（模板改版就会变）。
    ⇒ 找不到就返回 False，由上层告警。

    ⚠️ **R6-18 严格判定**：必须在**第一个对话块起点之前**
    （宿主把「当前时间：」拼在引导语之后、第一个块之前）——
    用户在**块正文里**写的「当前时间：xxx」不该被改。
    """
    car = _carriers()
    hit = car.classify_replier_current_time(text, time.time())
    if hit is None:
        return text, False
    story = car.format_story(hit.real_ts, offset_sec, with_date=True)
    return text[: hit.start] + "当前时间：" + story + text[hit.end:], True
