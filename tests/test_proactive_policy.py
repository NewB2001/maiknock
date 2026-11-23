# -*- coding: utf-8 -*-
"""MaiKnock F1 主动私聊策略的离线单测（``proactive_policy.py``）。

对应测试案例文档 §四 C 组 —— **这 28 条此前完全没有自动化覆盖**
（抽签与免打扰判定原先内联在 ``plugin.py`` 的实例方法里，离线测不了；
0.2.3 测试报告建议 ⑤ 已把两者抽成纯逻辑模块，本文件是配套的单测）。

覆盖：
  · UT-C-01 / UT-C-02  冷场随机区间抽签（区间内、2 分钟步长）
  · UT-C-05 / UT-C-06  抽签落盘复用、越界重抽
  · UT-C-07 / UT-C-08  区间倒挂兜底、最早=最晚退回固定时刻
  · UT-C-17 ~ UT-C-24 免打扰多时段（空清单 / 常规 / 左闭右开 / 跨天 /
                        开始==结束 / 多段并存 / 非法段跳过）

运行：
    "C:\\Users\\29411\\AppData\\Roaming\\uv\\tools\\maibot\\Scripts\\python.exe" test_proactive_policy.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUG))

import proactive_policy as PP  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, extra))


print("=== 1. parse_hhmm：HH:MM → 分钟数 ===")
check("01:00 → 60", PP.parse_hhmm("01:00") == 60)
check("08:00 → 480", PP.parse_hhmm("08:00") == 480)
check("23:59 → 1439", PP.parse_hhmm("23:59") == 1439)
check("00:00 → 0", PP.parse_hhmm("00:00") == 0)
check("非法格式 → None", PP.parse_hhmm("abc") is None)
check("缺段 → None", PP.parse_hhmm("25") is None)
check("小时越界 → None", PP.parse_hhmm("24:00") is None)
check("分钟越界 → None", PP.parse_hhmm("01:60") is None)
check("空串 → None", PP.parse_hhmm("") is None)

print()
print("=== 2. normalize_silence_range：区间倒挂兜底 ===")
check("60<90 → (60,90)", PP.normalize_silence_range(60, 90) == (60, 90))
check("90<60 → 收敛为 (90,90)", PP.normalize_silence_range(90, 60) == (90, 90))
check("相等 → 原样", PP.normalize_silence_range(60, 60) == (60, 60))

class _FixedRng:
    """确定性 rng（randrange 恒返回固定值），用于验证抽签公式可注入。"""

    def __init__(self, value):
        self.value = value

    def randrange(self, *args):
        return self.value


print()
print("=== 3. draw_silence_goal：抽签在区间内 + 2 分钟步长 ===")
N = 1000
goals = [PP.draw_silence_goal(60 * 60, 90 * 60) for _ in range(N)]
check("全部落在 [3600,5400]", all(3600 <= g <= 5400 for g in goals),
      (min(goals), max(goals)))
check("全部为偶数分钟", all(g % 120 == 0 for g in goals),
      sorted({g % 120 for g in goals})[:5])
check("出现的值都是偶数分钟且在区间内",
      all(g in (3600, 3720, 3840, 3960, 4080, 4200, 4320, 4440, 4560, 4680, 4800, 4920,
                5040, 5160, 5280, 5400) for g in goals))
check("有随机性（不总是同一个值）", len(set(goals)) > 1, len(set(goals)))
check("最早=最晚 → 固定时刻", PP.draw_silence_goal(3600, 3600) == 3600)
check("倒挂输入 → 收敛后抽签", PP.draw_silence_goal(5400, 3600) == 5400)
check("自定义 rng 可注入（确定性）",
      PP.draw_silence_goal(3600, 5400, rng=_FixedRng(0)) == 3600)
# F-05 改版后公式 = (even_lo + 2 * randrange(...)) * 60 ⇒ randrange 的返回值是**网格索引**（每格 2 分钟）
check("自定义 rng 可注入（返回值即网格索引，索引 4 → +8 分钟）",
      PP.draw_silence_goal(3600, 5400, rng=_FixedRng(4)) == (60 + 8) * 60,
      PP.draw_silence_goal(3600, 5400, rng=_FixedRng(4)))
check("偶数分钟优先（真实 rng 抽出的偏移必为偶数分钟）",
      all((PP.draw_silence_goal(3600, 5400) - 3600) // 60 % 2 == 0 for _ in range(200)))

print()
print("=== 3b. draw_silence_goal：F-05 拍板口径（偶数优先，无偶数才取奇数）===")
check("60~61：偶数候选只有 60 → 恒 60（上界不可达按 PM 意见接受）",
      PP.draw_silence_goal(3600, 3660) == 3600)
check("61~61：区间内无偶数分钟 → 取奇数（下界）",
      PP.draw_silence_goal(3660, 3660) == 3660)
check("61~63：偶数候选 62 → 恒 62（不再抽奇数端点）",
      all(PP.draw_silence_goal(3660, 3780) == 3720 for _ in range(100)))
check("61~62：偶数候选 62 → 恒 62",
      PP.draw_silence_goal(3660, 3720) == 3720)
check("63~65：偶数候选 64 → 恒 64",
      all(PP.draw_silence_goal(3780, 3900) == 3840 for _ in range(100)))
check("默认区间 60~90 仍只出偶数分钟",
      all(PP.draw_silence_goal(3600, 5400) % 120 == 0 for _ in range(300)))

print()
print("=== 4. goal_in_range：越界即重抽 ===")
check("区间内 → True", PP.goal_in_range(5040, 3600, 5400) is True)
check("小于下界 → False", PP.goal_in_range(1800, 3600, 5400) is False)
check("大于上界 → False", PP.goal_in_range(7200, 3600, 5400) is False)
check("None → False", PP.goal_in_range(None, 3600, 5400) is False)
check("非数字 → False", PP.goal_in_range("abc", 3600, 5400) is False)
check("等于下界 → True", PP.goal_in_range(3600, 3600, 5400) is True)
check("等于上界 → True", PP.goal_in_range(5400, 3600, 5400) is True)

print()
print("=== 5. in_quiet_hours：常规区间（左闭右开）===")
spans = [{"开始": "01:00", "结束": "08:00"}]
check("03:00 在区间内", PP.in_quiet_hours(spans, 3 * 60) is True)
check("01:00 起点生效（左闭）", PP.in_quiet_hours(spans, 60) is True)
check("08:00 终点不生效（右开）", PP.in_quiet_hours(spans, 480) is False)
check("00:30 在区间外", PP.in_quiet_hours(spans, 30) is False)
check("12:00 在区间外", PP.in_quiet_hours(spans, 720) is False)

print()
print("=== 6. in_quiet_hours：跨天区间 ===")
cross = [{"开始": "22:00", "结束": "06:00"}]
check("23:00（当晚）", PP.in_quiet_hours(cross, 23 * 60) is True)
check("02:00（次日凌晨）", PP.in_quiet_hours(cross, 2 * 60) is True)
check("05:59（次日内）", PP.in_quiet_hours(cross, 5 * 60 + 59) is True)
check("06:00（终点不生效）", PP.in_quiet_hours(cross, 360) is False)
check("12:00（区间外）", PP.in_quiet_hours(cross, 720) is False)
check("22:00 起点生效", PP.in_quiet_hours(cross, 22 * 60) is True)

print()
print("=== 7. in_quiet_hours：空区间 / 多段 / 非法段 ===")
check("开始==结束 → 该段无效",
      PP.in_quiet_hours([{"开始": "05:00", "结束": "05:00"}], 300) is False)
check("空清单 → 不免打扰", PP.in_quiet_hours([], 300) is False)
check("None → 不免打扰", PP.in_quiet_hours(None, 300) is False)
check("非 list → 不免打扰", PP.in_quiet_hours("x", 300) is False)
multi = [{"开始": "01:00", "结束": "08:00"}, {"开始": "22:00", "结束": "06:00"}]
check("多段：命中第一段", PP.in_quiet_hours(multi, 180) is True)
check("多段：命中第二段", PP.in_quiet_hours(multi, 120) is True)
check("多段：都不命中", PP.in_quiet_hours(multi, 720) is False)
check("非法段被跳过（不影响其他段判定）",
      PP.in_quiet_hours([{"开始": "坏值", "结束": "08:00"},
                         {"开始": "01:00", "结束": "08:00"}], 180) is True)
check("非 dict 元素被跳过", PP.in_quiet_hours(["x", 1, None], 180) is False)

print()
print("=== 8. invalid_spans：非法段清单（供插件侧打去重 warning）===")
check("全部合法 → 空", PP.invalid_spans([{"开始": "01:00", "结束": "08:00"}]) == [])
check("含非法 → 1 条", len(PP.invalid_spans([{"开始": "x", "结束": "08:00"}])) == 1)
check("非 list → 空", PP.invalid_spans(None) == [])
check("非 dict 元素不算非法段", PP.invalid_spans(["x", 1]) == [])

print()
print("=== 9. local_minutes：epoch → 当天分钟 ===")
import time as _time  # noqa: E402
_stamp = _time.mktime(_time.strptime("2026-11-24 03:07:00", "%Y-%m-%d %H:%M:%S"))
check("03:07 → 187", PP.local_minutes(_stamp) == 187, PP.local_minutes(_stamp))

print()
print("=" * 46)
print("通过 %d / 失败 %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
