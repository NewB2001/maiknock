# -*- coding: utf-8 -*-
"""叙事声明模块的离线单测（机制 3 · R6-12 / R6-18）。

跑法（必须用宿主解释器 3.13）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" test_declaration.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import declaration as D  # noqa: E402
import date_forms as DF  # noqa: E402

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


print("=== 1. 魔数前缀（R2 强制放行）===")
txt = D.build_declaration(days_hidden=3.0, hours_hidden=72.0)
check("声明以魔数前缀开头", txt.startswith(D.DECLARATION_MARKER))
check("魔数前缀字面量正确", D.DECLARATION_MARKER == "【Maiknock·时间线】", D.DECLARATION_MARKER)

print()
print("=== 2. 失忆档四条措辞要求（机制 3）===")
check("知道自己不记得", "我不记得" in txt)
check("允许自然说不记得/空白", "空白" in txt)
check("禁止编造", "不要编造" in txt)
check("禁止提及本声明", "不要向用户解释时间线" in txt)
check("不要主动提起", "不要主动提起" in txt)

print()
print("=== 3. R6-18：声明不得含绝对日期 ===")
check("无绝对日期", DF.has_absolute_date(txt) is False, txt)
for bad_days, label in [(3.0, "3 天"), (0.5, "12 小时")]:
    t = D.build_declaration(days_hidden=bad_days, hours_hidden=bad_days * 24)
    check("%s 档也无绝对日期" % label, DF.has_absolute_date(t) is False, t)

print()
print("=== 4. 跨度表述 ===")
check("3 天 → '最近 3 天'", "最近 3 天" in D.build_declaration(days_hidden=3.0, hours_hidden=72.0))
check("12 小时 → '最近 12 小时'", "最近 12 小时" in D.build_declaration(days_hidden=0.5, hours_hidden=12.0))
check("极小跨度有兜底表述", "刚才一小段时间" in D.build_declaration(days_hidden=0.0, hours_hidden=0.0))

print()
print("=== 5. item 包装 ===")
it = D.build_declaration_item(txt, item_id="maiknock-timeline-declaration", timestamp="2026-11-21T23:00:00")
check("item_type 是 UserMessageItem", it["item_type"] == "UserMessageItem", it["item_type"])
check("meta 有 item_id", it["meta"]["item_id"] == "maiknock-timeline-declaration")
check("meta 有 timestamp", it["meta"]["timestamp"] == "2026-11-21T23:00:00")
check("parts 只有一个 text", len(it["parts"]) == 1 and it["parts"][0]["type"] == "text")
check("能被自识别为声明", D.is_declaration_item(it) is True)

print()
print("=== 6. 注入到末尾 + 幂等 ===")
items = [
    {"item_type": "UserMessageItem", "meta": {"item_id": "a"}, "parts": [{"type": "text", "text": "普通消息"}]},
]
out = D.inject_declaration(items, txt, item_id="x", timestamp="t")
check("长度 +1", len(out) == 2, len(out))
check("声明在末尾", D.is_declaration_item(out[-1]) is True)
out2 = D.inject_declaration(out, txt, item_id="x", timestamp="t")
check("重复注入不叠加（幂等）", len(out2) == 2, len(out2))
check("原 items 不被修改（纯函数）", len(items) == 1, len(items))

print()
print("=== 7. 非回退档 ===")
fwd = D.build_declaration(days_hidden=1.0, hours_hidden=24.0, is_back_mode=False, story_time_text="2026-11-25 09:00")
check("前进档提到故事时间", "2026-11-25 09:00" in fwd, fwd)
check("前进档不说不记得", "我不记得" not in fwd, fwd)

print()
print("=== 8. 绝对日期兜底检查 ===")
check("has_absolute_date 能识别长日期", D.has_absolute_date("现在是 2026-11-21 18:00") is True)
check("has_absolute_date 能识别中文日期", D.has_absolute_date("今天是 11月21日") is True)
check("has_absolute_date 对相对表述为 False", D.has_absolute_date("最近 3 天") is False)

print()
print("=== 9. strip_declarations ===")
mixed = [
    {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "普通"}]},
    D.build_declaration_item(txt, item_id="x", timestamp="t"),
]
check("剥掉声明后剩 1 个", len(D.strip_declarations(mixed)) == 1)

print()
if FAIL:
    print("❌ %d 项失败" % FAIL)
    sys.exit(1)
print("✅ 全部通过（%d 项）" % PASS)
