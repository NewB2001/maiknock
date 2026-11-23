# -*- coding: utf-8 -*-
"""MaiKnock F1 主动私聊策略 —— **纯逻辑模块**（无 IO、无宿主依赖，可离线单测）。

本模块是 0.2.3 测试报告「建议施工方补的第 ⑤ 条」的落地：把原先**内联在
``plugin.py`` 实例方法里**、离线测不了的策略判定抽成纯函数。

覆盖的用例（测试案例文档 §四 C 组）：
  · UT-C-01 / UT-C-02  冷场随机区间抽签（区间内、2 分钟步长）
  · UT-C-05 / UT-C-06  抽签落盘复用、越界重抽
  · UT-C-07 / UT-C-08  区间倒挂兜底、最早=最晚退回固定时刻
  · UT-C-17 ~ UT-C-24 免打扰多时段判定（空清单 / 常规 / 左闭右开 / 跨天 /
                        开始==结束 / 多段并存 / 非法段跳过）

⚠️ 除 F-05（PM 2026-11-24 拍板：抽签改「偶数分钟候选优先，区间内无偶数候选
才取奇数」，见 ``draw_silence_goal``）外，行为与抽取前逐字一致。
``plugin.py`` 侧仍保留 warning 去重、落盘、读配置等副作用逻辑。

⚠️ 兄弟模块**不能裸 import**（宿主 sys.path 不含插件目录，踩坑 66）——
宿主侧用 ``_load_sibling("proactive_policy", "proactive_policy.py")`` 加载。
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "parse_hhmm",
    "normalize_silence_range",
    "draw_silence_goal",
    "goal_in_range",
    "in_quiet_hours",
    "invalid_spans",
]


# ---------------------------------------------------------------------------
# 免打扰时段
# ---------------------------------------------------------------------------

def parse_hhmm(value: str) -> Optional[int]:
    """解析 ``HH:MM`` 为分钟数；非法返回 None。

    与抽取前 ``MaiKnockPlugin._parse_hhmm`` 逐字等价。
    """
    try:
        parts = value.strip().split(":")
        if len(parts) != 2:
            return None
        h = int(parts[0])
        m = int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h * 60 + m
    except (TypeError, ValueError):
        return None


def invalid_spans(spans: Any) -> List[Dict[str, Any]]:
    """列出「开始/结束」解析失败的时段（供插件侧打去重 warning）。

    抽取前这段逻辑内联在 ``_in_quiet_hours`` 里；现在拆出来，
    **warning 行为（只告警一次）由插件侧保持不变**。
    """
    bad: List[Dict[str, Any]] = []
    if not isinstance(spans, list):
        return bad
    for span in spans:
        if not isinstance(span, dict):
            continue
        s = parse_hhmm(str(span.get("开始") or ""))
        e = parse_hhmm(str(span.get("结束") or ""))
        if s is None or e is None:
            bad.append(span)
    return bad


def in_quiet_hours(spans: Any, minutes: int) -> bool:
    """判定「当天第 ``minutes`` 分钟」是否落在任一免打扰时段内。

    口径（与抽取前一致，**逐条不差**）：
      · 空清单 = 没有免打扰时段；
      · 开始 == 结束 = 空区间，该段跳过；
      · 开始 < 结束 = 常规区间，**左闭右开** ``s <= m < e``；
      · 开始 > 结束 = 跨天，``m >= s``（当晚）或 ``m < e``（次日凌晨）；
      · 段配置非法（解析失败）= 该段静默跳过（warning 由调用方打）。
    """
    if not isinstance(spans, list) or not spans:
        return False
    for span in spans:
        if not isinstance(span, dict):
            continue
        s = parse_hhmm(str(span.get("开始") or ""))
        e = parse_hhmm(str(span.get("结束") or ""))
        if s is None or e is None:
            continue
        if s == e:
            continue  # 空区间（下拉选了同一个整点）
        if s < e:
            if s <= minutes < e:
                return True
        else:  # 跨天：now >= s（当晚）或 now < e（次日凌晨）
            if minutes >= s or minutes < e:
                return True
    return False


def local_minutes(now: float) -> int:
    """把 epoch 秒换成本地时区的「当天第几分钟」（宿主与沙箱同钟同时区）。"""
    lt = time.localtime(now)
    return lt.tm_hour * 60 + lt.tm_min


# ---------------------------------------------------------------------------
# 冷场随机区间抽签
# ---------------------------------------------------------------------------

def normalize_silence_range(min_sec: int, max_sec: int) -> Tuple[int, int]:
    """区间倒挂兜底：最晚 < 最早时收敛为相等（不让区间倒挂）。

    与抽取前 ``_scan_once`` 里的 ``if max_sec < min_sec: max_sec = min_sec`` 等价。
    """
    if max_sec < min_sec:
        max_sec = min_sec
    return int(min_sec), int(max_sec)


def goal_in_range(goal: Any, min_sec: int, max_sec: int) -> bool:
    """已抽的签是否仍落在当前区间内（配置改了就要重抽）。"""
    if not isinstance(goal, (int, float)):
        return False
    return int(min_sec) <= float(goal) <= int(max_sec)


def draw_silence_goal(min_sec: int, max_sec: int, *, rng: Any = None) -> int:
    """在 ``[min_sec, max_sec]`` 内抽一个「本次冷场目标」。

    F-05（PM 2026-11-24 拍板）：**偶数分钟候选优先**——扫描每 2 分钟跑一次，
    目标落在双数分钟上节奏才对齐；区间内连一个偶数分钟候选都没有时
    （如 61~61），才退化取奇数分钟（取区间下界）。
    「奇数宽度导致上界不可达」（如 60~61 恒 60）属极少数场景，按 PM 意见
    接受、不为此改方案——候选集合仍以偶数分钟为准。
    """
    min_sec, max_sec = normalize_silence_range(min_sec, max_sec)
    r = rng if rng is not None else random
    lo = -(-int(min_sec) // 60)  # 向上取整到分钟
    hi = int(max_sec) // 60  # 向下取整到分钟
    even_lo = lo + (lo % 2)  # 第一个 ≥ lo 的偶数分钟
    if even_lo <= hi:
        # 偶数分钟网格：even_lo, even_lo+2, …, ≤ hi
        index = r.randrange(0, (hi - even_lo) // 2 + 1)
        return (even_lo + 2 * index) * 60
    # 极少数场景：区间内无偶数分钟候选（宽度 < 1 分钟且落在奇数分钟）→ 取奇数
    return min_sec
