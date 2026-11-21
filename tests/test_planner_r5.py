# -*- coding: utf-8 -*-
"""MaiKnock planner 侧 R5（风险 B · 解法③）离线单测。

**R5 的两问都在 relabel 之前、用真实时间轴回答**：
1. 「这是不是跨日提示？」→ content 里的时间戳 **≠ 真实 now**
2. 「它落在隐藏区间吗？」→ **真实 epoch** 区间

⚠️ **风险 H**：若在 relabel **之后**判断，文本日期已在**虚构轴**、隐藏区间在**真实轴**
⇒ 量纲不一致、必然判错。

运行：
    "C:\\Users\\29411\\AppData\\Roaming\\uv\\tools\\maibot\\Scripts\\python.exe" test_planner_r5.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(r"E:\Softw\MaiBot\plugins\maiknock")))

import carriers as C  # noqa: E402
import planner_processor as PP  # noqa: E402

PP.set_carriers(C)

FAILED = []


def check(name, cond):
    if cond:
        print("PASS %s" % name)
    else:
        print("FAIL %s" % name)
        FAILED.append(name)


NOW = time.time()
OFFSET = -2 * 3600

# 隐藏区间：(NOW - 5 小时, NOW - 10 分钟]  ← 左开右闭
# ⚠️ **右边界 = 回退命令的执行时刻**（不是"现在"）——
#    这符合真实语义（回退之后我们又聊了几分钟），
#    也让测试 item 的 meta.timestamp(=NOW，构建时刻) 稳稳落在区间外。
#    ⚠️ 之前把 UNTIL 取成 NOW，导致 meta.timestamp 正好压在边界上，
#    而 `isoformat()` 的**微秒舍入**让它随机翻转 ⇒ 测试随机失败（已踩）。
BOUNDARY = NOW - 5 * 3600
UNTIL = NOW - 10 * 60


def is_hidden(ts: float) -> bool:
    """真实轴、左开右闭（与 timeline.TimelineState.is_hidden 口径一致）。"""
    return BOUNDARY < ts <= UNTIL


def ts_str(offset_sec: float) -> str:
    return datetime.fromtimestamp(NOW + offset_sec).strftime("%Y-%m-%d %H:%M:%S")


def mk_item(text: str, ts: float, item_id: str = "x"):
    return {
        "item_type": "UserMessageItem",
        "meta": {"item_id": item_id, "timestamp": datetime.fromtimestamp(ts).isoformat()},
        "parts": [{"type": "text", "text": text}],
    }


print("=" * 68)
print("第 1 节：跨日提示落在隐藏区间 ⇒ 丢弃（R5）")
print("=" * 68)

# 跨日提示：content ts = 2 小时前（落在隐藏区间内）
crossday_text = "时间：" + ts_str(-2 * 3600)
items = [mk_item(crossday_text, NOW, "crossday")]
res = PP.process_planner_items(items, is_hidden, OFFSET, now_ts=NOW)
check("R5 丢弃了 1 条跨日提示", res.r5_dropped == 1)
check("  items 里已没有它", len(res.items) == 0)
check("  有告警说明", any("R5" in w for w in res.warnings))

print()
print("=" * 68)
print("第 2 节：跨日提示不在隐藏区间 ⇒ 保留 + relabel")
print("=" * 68)

# 跨日提示：content ts = 10 小时前（不在隐藏区间）
keep_text = "时间：" + ts_str(-10 * 3600)
items2 = [mk_item(keep_text, NOW, "crossday2")]
res2 = PP.process_planner_items(items2, is_hidden, OFFSET, now_ts=NOW)
check("R5 没有丢弃", res2.r5_dropped == 0)
check("  item 保留", len(res2.items) == 1)
if res2.items:
    out_text = "".join(p.get("text", "") for p in res2.items[0].get("parts", []))
    print("  改后:", repr(out_text))
    # 故事时间 = 真实 - 2h
    expect = datetime.fromtimestamp(
        NOW - 10 * 3600 + OFFSET).strftime("%Y-%m-%d %H:%M:%S")
    check("  relabel 到故事时间", out_text == "时间：" + expect)

print()
print("=" * 68)
print("第 3 节：当前时间提示永远不被 R5 丢")
print("=" * 68)

now_text = "时间：" + datetime.fromtimestamp(NOW).strftime("%Y-%m-%d %H:%M:%S")
items3 = [mk_item(now_text, NOW, "cur")]
res3 = PP.process_planner_items(items3, is_hidden, OFFSET, now_ts=NOW)
check("当前时间提示未被 R5 丢", res3.r5_dropped == 0)
check("  item 保留", len(res3.items) == 1)
check("  且被 relabel", res3.current_time_relabeled is True)
if res3.items:
    out3 = "".join(p.get("text", "") for p in res3.items[0].get("parts", []))
    print("  改后:", repr(out3))

print()
print("=" * 68)
print("第 4 节：R5 用的是 relabel 之前的值（风险 H 的核心）")
print("=" * 68)

# 造一个「relabel 前后判定结果相反」的场景：
# content ts = 2 小时前（真实轴，落在隐藏区间内）
# 若错误地用 relabel 后的值（再减一次 offset = 4 小时前，仍在区间内）——
# 这里换个更强的证据：relabel 后的值 = -4h，而 is_hidden 的边界是 -5h
# ⇒ 两者都在区间内，结论相同 ⇒ 换用「边界附近」的用例。
BOUNDARY2 = NOW - 3 * 3600  # 边界在 -3h
UNTIL2 = UNTIL


def is_hidden2(ts: float) -> bool:
    return BOUNDARY2 < ts <= UNTIL2


# content ts = -2h（真实轴，在区间内 ⇒ 该丢）
# relabel 后 = -4h（不在区间内 ⇒ 若用错的值就不该丢）⇒ 两者结论相反 ⇒ 能区分！
items4 = [mk_item("时间：" + ts_str(-2 * 3600), NOW, "cd4")]
res4 = PP.process_planner_items(items4, is_hidden2, OFFSET, now_ts=NOW)
check("用 relabel 前的真实值判定 ⇒ 丢弃", res4.r5_dropped == 1)
check("  （若误用 relabel 后的虚构值就不会丢 ⇒ 本用例能区分）", len(res4.items) == 0)

print()
print("=" * 68)
print("第 5 节：R5 关闭开关时不动")
print("=" * 68)

items5 = [mk_item("时间：" + ts_str(-2 * 3600), NOW, "cd5")]
res5 = PP.process_planner_items(
    items5, is_hidden, OFFSET, now_ts=NOW, drop_crossday_in_hidden=False)
check("开关关闭 ⇒ 不丢", res5.r5_dropped == 0)
check("  item 保留", len(res5.items) == 1)

print()
print("=" * 68)
print("第 6 节：R5 不影响普通历史消息")
print("=" * 68)

hist = [{
    "item_type": "UserMessageItem",
    "meta": {"item_id": "h", "timestamp": datetime.fromtimestamp(NOW - 2 * 3600).isoformat()},
    "parts": [{"type": "text",
               "text": '<message msg_id="m1" time="10:00:00" user="邦本国夫">\n你好'}],
}]
res6 = PP.process_planner_items(hist, is_hidden, OFFSET, now_ts=NOW)
check("普通历史消息不被 R5 丢（靠 meta.timestamp 遮蔽那条路走）",
      res6.r5_dropped == 0)

print()
if FAILED:
    print("❌ %d 项失败：%s" % (len(FAILED), FAILED))
    sys.exit(1)
print("✅ 全部通过")
