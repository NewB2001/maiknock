# -*- coding: utf-8 -*-
"""MaiKnock 命令参数解析 —— **纯逻辑模块**（无 IO、无宿主依赖，可离线单测）。

0.2.3 把命令面砍到四条（``/mk now|add|back|set``）+ ``/mk hi``，
命令参数解析是 0.2.3 改动最大的地方之一，却原先**内联在 ``plugin.py`` 里**、
离线测不了（要 import 整个 plugin.py 就得把 maibot_sdk 一起桩起来）。

本模块把这些解析规则抽成纯函数，覆盖测试案例文档 §三 B 组的：
  · UT-B-09 ~ UT-B-12  时长解析（``3d`` / ``12h`` / ``90m`` / ``30s`` / 纯数字 / 小数 / 非法）
  · UT-B-13 ~ UT-B-16  ``set`` 目标时刻解析与参数拆分

⚠️ **行为必须与抽取前逐字一致** —— 只做「搬家」，不改判定口径。
``plugin.py`` 侧的 ``MaiKnockPlugin._parse_duration`` / ``_parse_target_time`` /
``_split_set_arg`` 保留为薄封装，转调本模块（对外接口不变）。

⚠️ 兄弟模块**不能裸 import**（宿主 sys.path 不含插件目录，踩坑 66）——
宿主侧用 ``_load_sibling("commands", "commands.py")`` 加载。
"""

from __future__ import annotations

import re
import time
from typing import Optional, Tuple

__all__ = ["parse_duration", "parse_target_time", "split_set_arg", "split_time_only"]

#: 时长单位 → 秒
_DURATION_FACTORS = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}

#: ``set`` 目标时刻接受的格式（依次尝试）
_TARGET_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")

#: 只给时刻的写法（F-02 残留修复）：``09:00`` / ``9:00`` / ``09:00:01``
_TIME_ONLY_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_duration(text: str) -> Optional[float]:
    """解析时长：``3d`` / ``12h`` / ``90m`` / ``30s``；**纯数字按小时计**。

    非法（空串、非数字、带负号、带中文单位）一律返回 None。
    与抽取前 ``MaiKnockPlugin._parse_duration`` 逐字等价。
    """
    raw = (text or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    matched = re.fullmatch(r"(\d+(?:\.\d+)?)([dhms])?", raw)
    if not matched:
        return None
    value = float(matched.group(1))
    unit = matched.group(2) or "h"
    return value * _DURATION_FACTORS[unit]


def parse_target_time(text: str) -> Optional[float]:
    """解析绝对目标时刻：``2026-11-25 09:01:02`` / ``2026-11-25 09:01`` / ``2026-11-25``。

    用 ``time.mktime``（本地时区）——与抽取前逐字等价。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in _TARGET_FORMATS:
        try:
            return float(time.mktime(time.strptime(raw, fmt)))
        except ValueError:
            continue
    return None


def split_set_arg(text: str) -> Tuple[Optional[float], Optional[str]]:
    """``/mk set`` 的参数拆分：目标时刻 + 剧情提示词。

    支持 ``2026-11-25 09:00 剧情…`` / ``2026-11-25 剧情…`` / ``2026-11-25``。
    解析失败返回 ``(None, None)``。与抽取前 ``MaiKnockPlugin._split_set_arg`` 逐字等价。
    """
    raw = (text or "").strip()
    if not raw:
        return None, None
    ts = parse_target_time(raw)
    if ts is not None:
        return ts, ""
    parts = raw.split(maxsplit=2)
    if len(parts) >= 2:
        ts = parse_target_time(parts[0] + " " + parts[1])
        if ts is not None:
            return ts, (parts[2].strip() if len(parts) > 2 else "")
    ts = parse_target_time(parts[0])
    if ts is not None:
        return ts, (parts[1].strip() if len(parts) > 1 else "")
    return None, None


def split_time_only(text: str, anchor_story_ts: float) -> Tuple[Optional[float], Optional[str]]:
    """``/mk set`` **只给时刻**的解析（F-02 残留修复，0.2.3 回归轮新增）。

    ``09:00`` / ``9:00`` / ``09:00:01`` ⇒ **故事当前日**的该时刻——锚点
    ``anchor_story_ts`` 传故事当前 epoch（``state.story_now(real_now)``），
    **不是现实当前时刻**：回退态下故事已退到 3 天前，「当天 09:00」必须
    是故事里的那一天，否则语义会跳（口径由 PM/审计方确认，离线断言锁定）。

    - ``09:00 此时你只觉得…`` ⇒（故事当天 09:00 的 epoch, "此时你只觉得…"）
    - 非法时刻（hh>23 / mm>59 / ss>59 / 首词不是时刻）⇒ (None, None)，
      由调用方继续报用法错误——不影响日期格式的解析路径。
    """
    raw = (text or "").strip()
    if not raw:
        return None, None
    parts = raw.split(maxsplit=1)
    m = _TIME_ONLY_RE.match(parts[0])
    if not m:
        return None, None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        return None, None
    # 锚点所在「本地日」的零点 ⇒ 加上时刻（mktime 本地时区，与 parse_target_time 同钟）
    lt = time.localtime(anchor_story_ts)
    day_start = float(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    ts = day_start + h * 3600.0 + mi * 60.0 + s
    narrative = parts[1].strip() if len(parts) > 1 else ""
    return ts, narrative
