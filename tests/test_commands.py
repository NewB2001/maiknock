# -*- coding: utf-8 -*-
"""MaiKnock 命令参数解析的离线单测（``commands.py``）。

对应测试案例文档 §三 B 组 —— **命令面是 0.2.3 改动最大的地方**，
但解析规则原先内联在 ``plugin.py`` 里，离线测不了（import 整个 plugin.py
就得把 maibot_sdk 一起桩起来）。0.2.3 测试报告建议 ③ 已把解析规则抽成
纯逻辑模块，本文件是配套的单测。

覆盖：
  · UT-B-09 ~ UT-B-12  时长解析（``3d`` / ``12h`` / ``90m`` / ``30s`` / 纯数字 / 小数 / 非法）
  · UT-B-13 ~ UT-B-16  ``set`` 目标时刻解析与参数拆分

运行：
    "C:\\Users\\29411\\AppData\\Roaming\\uv\\tools\\maibot\\Scripts\\python.exe" test_commands.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PLUG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUG))

import commands as CMD  # noqa: E402

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


def ts(s, fmt="%Y-%m-%d %H:%M:%S"):
    return time.mktime(time.strptime(s, fmt))


print("=== 1. parse_duration：四种单位 ===")
check("3d → 259200", CMD.parse_duration("3d") == 259200.0)
check("12h → 43200", CMD.parse_duration("12h") == 43200.0)
check("90m → 5400", CMD.parse_duration("90m") == 5400.0)
check("30s → 30", CMD.parse_duration("30s") == 30.0)
check("纯数字按小时算（3 → 10800）", CMD.parse_duration("3") == 10800.0)
check("小数时长 1.5h → 5400", CMD.parse_duration("1.5h") == 5400.0)
check("大小写不敏感（3D）", CMD.parse_duration("3D") == 259200.0)
check("前后空白被吃掉（' 3d '）", CMD.parse_duration(" 3d ") == 259200.0)
check("内部空白被吃掉（'3 d'）", CMD.parse_duration("3 d") == 259200.0)

print()
print("=== 2. parse_duration：非法输入一律 None ===")
check("空串 → None", CMD.parse_duration("") is None)
check("None → None", CMD.parse_duration(None) is None)
check("负号不被接受（-3d）", CMD.parse_duration("-3d") is None)
check("中文单位（3天）", CMD.parse_duration("3天") is None)
check("纯字母（abc）", CMD.parse_duration("abc") is None)
check("带多余字符（3dd）", CMD.parse_duration("3dd") is None)

print()
print("=== 3. parse_target_time：三种时间写法 ===")
check("带秒", CMD.parse_target_time("2026-11-25 09:01:02") == ts("2026-11-25 09:01:02"))
check("只到分", CMD.parse_target_time("2026-11-25 09:01") == ts("2026-11-25 09:01:00"))
check("只到天（落当天 00:00:00）",
      CMD.parse_target_time("2026-11-25") == ts("2026-11-25 00:00:00"))

print()
print("=== 4. parse_target_time：非法输入 ===")
check("空串 → None", CMD.parse_target_time("") is None)
check("None → None", CMD.parse_target_time(None) is None)
check("只有时刻（09:00）→ None", CMD.parse_target_time("09:00") is None)
check("斜杠格式（2026/11/25）→ None", CMD.parse_target_time("2026/11/25") is None)
check("只有年（2026）→ None", CMD.parse_target_time("2026") is None)
check("中文日期（2026年11月25日）→ None", CMD.parse_target_time("2026年11月25日") is None)

print()
print("=== 5. split_set_arg：目标时刻 + 剧情提示词 ===")
t, n = CMD.split_set_arg("2026-11-25 09:00")
check("日期+时刻 → 时刻正确", t == ts("2026-11-25 09:01:00") or t == ts("2026-11-25 09:00:00"), t)
check("日期+时刻 → 提示词为空", n == "")

t, n = CMD.split_set_arg("2026-11-25 09:00 此时你只觉得")
check("日期+时刻+提示词 → 提示词完整", n == "此时你只觉得", repr(n))

t, n = CMD.split_set_arg("2026-11-25 这三天我们去了海边")
check("日期+提示词 → 提示词完整", n == "这三天我们去了海边", repr(n))
check("日期+提示词 → 时刻落到当天 00:00:00", t == ts("2026-11-25 00:00:00"), t)

t, n = CMD.split_set_arg("2026-11-25")
check("只有日期 → 时刻正确、提示词为空",
      t == ts("2026-11-25 00:00:00") and n == "")

t, n = CMD.split_set_arg("2026-11-25 09:00 提示 里还有 空格")
check("提示词含空格 → 只切前两段", n == "提示 里还有 空格", repr(n))

print()
print("=== 6. split_set_arg：解析失败返回 (None, None) ===")
check("空串", CMD.split_set_arg("") == (None, None))
check("None", CMD.split_set_arg(None) == (None, None))
check("乱写", CMD.split_set_arg("abc") == (None, None))
# 注意：split_set_arg 仍不认识纯时刻（日期格式专用）；时刻写法走 split_time_only（第 8 节）
check("只有时刻（09:00）走日期解析不认识", CMD.split_set_arg("09:00") == (None, None))
check("now（已删除的 set now）", CMD.split_set_arg("now") == (None, None))

print()
print("=== 7. 端到端：命令文本 → 解析结果 ===")
# add 3d 这三天我们去了海边
parts = "3d 这三天我们去了海边".split(maxsplit=1)
check("add：幅度解析", CMD.parse_duration(parts[0]) == 259200.0)
check("add：提示词取出", parts[1].strip() == "这三天我们去了海边", repr(parts[1]))
# back 10h 夜深之后
parts = "10h 夜深之后".split(maxsplit=1)
check("back：幅度解析", CMD.parse_duration(parts[0]) == 36000.0)
check("back：提示词取出", parts[1].strip() == "夜深之后")

print()
print("=== 8. split_time_only：只给时刻 = 故事当前日的该时刻（F-02 残留修复）===")
# 锚点用「故事当前时刻」：state.story_now(real_now)。这里直接给一个明确的
# 故事时刻 2026-11-20 15:00 —— 断言锁定的口径：**按故事的日算，不是现实的日**。
anchor = ts("2026-11-20 15:00:00")

got_ts, got_n = CMD.split_time_only("09:00 此时你只觉得", anchor)
check("09:00 解析成功", got_ts is not None, repr(got_ts))
check("落在故事当天（2026-11-20）",
      time.strftime("%Y-%m-%d", time.localtime(got_ts)) == "2026-11-20",
      time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(got_ts)))
check("时刻正确 09:00:00", time.strftime("%H:%M:%S", time.localtime(got_ts)) == "09:00:00",
      time.strftime("%H:%M:%S", time.localtime(got_ts)))
check("提示词取出", got_n == "此时你只觉得", repr(got_n))

got_ts2, got_n2 = CMD.split_time_only("09:00", anchor)
check("不带提示词", got_ts2 == got_ts and got_n2 == "")

got_ts3, _ = CMD.split_time_only("9:00", anchor)
check("小时不补零也认", got_ts3 == got_ts, repr(got_ts3))

got_ts4, _ = CMD.split_time_only("09:00:01", anchor)
check("带秒（09:00:01）", time.strftime("%H:%M:%S", time.localtime(got_ts4)) == "09:00:01")

# 口径锁定：故事退到过去 ⇒ 「当天」跟着故事走，不是现实那天
anchor_past = ts("2026-11-17 23:00:00")  # 故事退到 3 天前
got_ts5, _ = CMD.split_time_only("09:00", anchor_past)
check("回退态按故事日算（2026-11-17）",
      time.strftime("%Y-%m-%d", time.localtime(got_ts5)) == "2026-11-17",
      time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(got_ts5)))
# 口径锁定：故事推进到未来同理
anchor_future = ts("2026-12-05 02:00:00")
got_ts6, _ = CMD.split_time_only("09:00", anchor_future)
check("推进态按故事日算（2026-12-05）",
      time.strftime("%Y-%m-%d", time.localtime(got_ts6)) == "2026-12-05")

# 非法输入一律 (None, None)，由调用方报用法错误
for bad in ["", None, "abc", "25:00", "09:60", "09:00:61", "3d", "2026-11-25 09:00"]:
    check("非法 %r 被拒" % bad, CMD.split_time_only(bad, anchor) == (None, None))

print()
print("=" * 46)
print("通过 %d / 失败 %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
