# -*- coding: utf-8 -*-
"""MaiKnock carriers.py 离线单测（M4 第⑤步 J · 载体白名单）。

**核心断言方向**：**不该改的绝对不能改**（R6-18）——
用户自己在正文里写的「时间：xxx」「当前时间：xxx」必须被放过。

运行：
    "C:\\Users\\29411\\AppData\\Roaming\\uv\\tools\\maibot\\Scripts\\python.exe" test_carriers.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import carriers as C  # noqa: E402
import matcher as M  # noqa: E402

FAILED = []


def check(name, cond):
    if cond:
        print("PASS %s" % name)
    else:
        print("FAIL %s" % name)
        FAILED.append(name)


def ts_of(date_str, hms):
    return datetime.strptime("%s %s" % (date_str, hms), "%Y-%m-%d %H:%M:%S").timestamp()


NOW = time.time()

print("=" * 68)
print("第 1 节：planner 时间提示 item（R6-18 严格判定）")
print("=" * 68)

# ⚠️ 用 **NOW 派生**的时间串（固定日期会离 now 很远 ⇒ 被判成跨日提示）
_now_str = datetime.fromtimestamp(NOW).strftime("%Y-%m-%d %H:%M:%S")

# ① 整条 content 恰好是时间串 ⇒ 命中
hit = C.classify_planner_time_item("时间：" + _now_str, NOW)
check("整条 content 是时间串 ⇒ 命中", hit is not None)
check("  载体类别 = 当前时间提示", hit.kind == C.CARRIER_PLANNER_CURRENT_TIME)
check("  真实时刻正确", abs(hit.real_ts - NOW) < 1.5)

# ② 正文中间含「时间：xxx」⇒ **不命中**（这是 R6-18 的核心）
hit2 = C.classify_planner_time_item(
    "我们约在时间：" + _now_str + " 见吧，我记下了", NOW
)
check("正文中间的时间串 ⇒ 不命中", hit2 is None)

hit3 = C.classify_planner_time_item(
    "时间：" + _now_str + " 是我们的约定", NOW
)
check("时间串开头但后面有别的 ⇒ 不命中", hit3 is None)

# ③ 当前时间提示 vs 跨日提示的分类
now_hit = C.classify_planner_time_item("时间：" + _now_str, NOW)
check("≈now ⇒ 当前时间提示", now_hit.kind == C.CARRIER_PLANNER_CURRENT_TIME)

old_hit = C.classify_planner_time_item("时间：2026-11-19 03:00:00", NOW)
check("远离 now ⇒ 跨日提示", old_hit.kind == C.CARRIER_PLANNER_CROSSDAY)

# ④ 容差边界：设计稿说 2 秒，实测偏差 2.795 秒 ⇒ 2 秒会把当前时间提示误判
hit_2_8 = C.classify_planner_time_item(
    "时间：" + datetime.fromtimestamp(NOW - 2.795).strftime("%Y-%m-%d %H:%M:%S"), NOW
)
check("偏差 2.795 秒仍算当前时间提示（2 秒容差会失效）",
      hit_2_8.kind == C.CARRIER_PLANNER_CURRENT_TIME)
check("容差常量 = 3600 秒（1 小时）", C.CURRENT_TIME_TOLERANCE_SECONDS == 3600.0)
check("偏差 65 秒仍算当前时间提示（快照写入滞后实测）",
      C.classify_planner_time_item(
          "时间：" + datetime.fromtimestamp(NOW - 65).strftime("%Y-%m-%d %H:%M:%S"), NOW
      ).kind == C.CARRIER_PLANNER_CURRENT_TIME)

# ⑤ 非法日期不崩
check("非法日期 ⇒ None", C.classify_planner_time_item("时间：2026-13-45 99:99:99", NOW) is None)
check("空串 ⇒ None", C.classify_planner_time_item("", NOW) is None)


print()
print("=" * 68)
print("第 2 节：replyer 当前时间提示（R6-18 · 必须在第一个块之前）")
print("=" * 68)

blob_real = (
    "\n\n你正在和邦本国夫聊天，这是你们之前聊的内容:\n"
    "当前时间：2026-11-21 23:56:47\n"
    "[18:08:14] 日向雏田(你)说：鸣人君……\n"
    "[18:08:25] 日向雏田(你)说：记得哦"
)

hit_r = C.classify_replier_current_time(blob_real, NOW)
check("引导语后的当前时间 ⇒ 命中", hit_r is not None)
check("  载体类别正确", hit_r.kind == C.CARRIER_REPLYER_CURRENT_TIME)
check("  真实时刻正确", abs(hit_r.real_ts - ts_of("2026-11-21", "23:56:47")) < 0.001)

# ② 用户在**块正文里**写「当前时间：xxx」⇒ 不该被改
blob_in_body = (
    "\n\n你正在和邦本国夫聊天，这是你们之前聊的内容:\n"
    "[18:08:14] 日向雏田(你)说：我记得当前时间：2020-01-01 00:00:00 那个约\n"
    "[18:08:25] 日向雏田(你)说：记得哦"
)
hit_rb = C.classify_replier_current_time(blob_in_body, NOW)
check("块正文里的当前时间 ⇒ 不命中", hit_rb is None)

# ③ 完全没有当前时间提示
hit_none = C.classify_replier_current_time("只有对话没有时间提示", NOW)
check("没有当前时间 ⇒ None", hit_none is None)


print()
print("=" * 68)
print("第 3 节：R6-17 第三种载体 `HH:MM:SS[msg_id:…][说话人]正文`")
print("=" * 68)

tool_text = (
    "18:07:05[msg_id:mepsvkf][日向雏田]你好呀\n"
    "18:08:14[msg_id:abc123][邦本国夫]在吗"
)
hits = C.classify_tool_prefixes(tool_text)
check("识别出 2 处载体", len(hits) == 2)
if len(hits) == 2:
    check("  第 1 处 msg_id = mepsvkf", hits[0].msg_id == "mepsvkf")
    check("  第 2 处 msg_id = abc123", hits[1].msg_id == "abc123")
    check("  hms span 精确（只指时分秒）",
          tool_text[hits[0].hms_start:hits[0].hms_end] == "18:07:05")

# ② 缺 msg_id 的形态（`HH:MM:SS[说话人]正文`）也能识别，但 msg_id 为空
hits2 = C.classify_tool_prefixes("18:07:05[日向雏田]你好呀")
check("无 msg_id 形态也识别", len(hits2) == 1)
if hits2:
    check("  msg_id 为空串", hits2[0].msg_id == "")

# ③ **行首锚定**：正文中间夹带的同形态串不算载体
hits3 = C.classify_tool_prefixes("他说：18:07:05[msg_id:xxx][A]这句话")
check("非行首 ⇒ 不算载体", len(hits3) == 0)


print()
print("=" * 68)
print("第 4 节：展示用 message id 口径（同构复刻宿主）")
print("=" * 68)

check("短 id（≤12）原样", M.to_display_message_id("abc123") == "abc123")
check("长 id ⇒ m + base32(6)",
      M.to_display_message_id("ROBOT1.0_abcdefghijklmnop") == "makza7b")
check("空 ⇒ 空串", M.to_display_message_id("") == "")


print()
print("=" * 68)
print("第 5 节：历史消息时间属性")
print("=" * 68)

attrs = C.classify_planner_history_attrs(
    '<message msg_id="mepsvkf" time="18:07:05" user="日向雏田">\n你好'
)
check("找到 1 处 time 属性", len(attrs) == 1)
if attrs:
    check("  值 = 18:07:05", attrs[0][2] == "18:07:05")


print()
if FAILED:
    print("❌ %d 项失败：%s" % (len(FAILED), FAILED))
    sys.exit(1)
print("✅ 全部通过")
