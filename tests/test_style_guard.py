# -*- coding: utf-8 -*-
"""F4 小说体风格守卫的离线单测（规格 §5 验收第 1 条）。

跑法（必须用宿主解释器 3.13）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" test_style_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import style_guard as SG  # noqa: E402

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


print("=== 1. 契约生成：四种开关组合（§3.1 逐字）===")
check("开/开", SG.build_contract(True, True) == SG.CONTRACT_BOTH)
check("开/关", SG.build_contract(True, False) == SG.CONTRACT_INNER_ONLY)
check("关/开", SG.build_contract(False, True) == SG.CONTRACT_ACTION_ONLY)
check("关/关", SG.build_contract(False, False) == SG.CONTRACT_NONE)
check("开/开 提到【】与（）", "【】" in SG.CONTRACT_BOTH and "（）" in SG.CONTRACT_BOTH)
check("开/开 提到不堆开头结尾", "不要把同类内容全堆在开头或结尾" in SG.CONTRACT_BOTH)

print()
print("=== 2. 显式标记转换（步骤①·迁移主路径）===")
t = "（身体垂下眼）（内心：不要这样问……你明明知道我会怕）……嗯。"
out, n = SG.convert_explicit_inner(t, SG.DEFAULT_INNER_MARKERS)
check("转换 1 处", n == 1, n)
check("括号已换成【】", "【内心：不要这样问……你明明知道我会怕】" in out, out)
check("内容一字不改", "不要这样问……你明明知道我会怕" in out)
check("无标记的（）不动", "（身体垂下眼）" in out)
out2, n2 = SG.convert_explicit_inner(out, SG.DEFAULT_INNER_MARKERS)
check("幂等：再转一次 0 处", n2 == 0 and out2 == out, n2)
for marker in ["内心：", "内心:", "心声：", "心想：", "暗想：", "OS："]:
    _, nm = SG.convert_explicit_inner("（%s测试）" % marker, SG.DEFAULT_INNER_MARKERS)
    check("标记 %s 可识别" % marker, nm == 1, nm)
_, nws = SG.convert_explicit_inner("（ 内心：允许前导空白）", SG.DEFAULT_INNER_MARKERS)
check("标记前允许空白", nws == 1, nws)
_, nh = SG.convert_explicit_inner("(内心：半角括号不处理)", SG.DEFAULT_INNER_MARKERS)
check("半角 () 不处理（边界 5）", nh == 0, nh)

print()
print("=== 3. enforce：四种开关组合（§3.3 表）===")
# 3.1 开/开：合规放行
ok = "【他来了】（指尖发颤）……鸣人君，你来了。"
r = SG.enforce(ok, inner_enabled=True, action_enabled=True)
check("开/开 合规放行", r.retry_reason is None and r.final_text is None, r)
# 3.2 开/开：现状风格（内心在圆括号）→ 转换后合规放行（不误判缺块 → 不白烧重生）
cur = "（身体垂下眼，像是在等你的话落下来）（内心：不要这样问……）嗯……是我误会了。"
r = SG.enforce(cur, inner_enabled=True, action_enabled=True)
check("现状风格转换后放行（P1-1 顺序正确性）", r.retry_reason is None and r.final_text and "【内心：" in r.final_text, r)
# 3.3 开/开：真缺内心 → 重生
r = SG.enforce("（她轻轻点头）……好。", inner_enabled=True, action_enabled=True)
check("缺内心 → 重生", r.retry_reason is not None and r.matched_rule == SG.RULE_INNER_MISSING, r)
check("重生时不产出改写文本（二选一）", r.final_text is None, r)
check("重生原因提到【】", "【】" in (r.retry_reason or ""))
# 3.4 开/开：真缺动作 → 重生
r = SG.enforce("【心里很慌】……好。", inner_enabled=True, action_enabled=True)
check("缺动作 → 重生", r.retry_reason is not None and r.matched_rule == SG.RULE_ACTION_MISSING, r)
check("动作重生原因提到（）", "（）" in (r.retry_reason or ""))
# 3.5 开/关：删动作，留内心
r = SG.enforce("【心里很慌】（她轻轻点头）……好。", inner_enabled=True, action_enabled=False)
check("开/关 删动作", r.final_text == "【心里很慌】……好。", r.final_text)
check("开/关 删了 1 处", r.deleted_action == 1 and r.deleted_inner == 0, r)
# 3.6 关/开：删内心，留动作
r = SG.enforce("【心里很慌】（她轻轻点头）……好。", inner_enabled=False, action_enabled=True)
check("关/开 删内心", r.final_text == "（她轻轻点头）……好。", r.final_text)
# 3.7 关/关：全删
r = SG.enforce("【心里很慌】（她轻轻点头）……好。", inner_enabled=False, action_enabled=False)
check("关/关 全删", r.final_text == "……好。", r.final_text)
# 3.8 关/开：无标记内心（（心里其实很慌））按动作处理、合规放行（产品决策 §3.4）
r = SG.enforce("（心里其实很慌）……好。", inner_enabled=False, action_enabled=True)
check("无标记内心按动作放行（关/开）", r.retry_reason is None and r.final_text is None, r)

print()
print("=== 4. 空正文保护（边界 6）===")
r = SG.enforce("【心里很慌】", inner_enabled=False, action_enabled=False)
check("整条只有内心块 → 放弃删除", r.empty_body_protected is True and r.final_text == "【心里很慌】", r)
r = SG.enforce("（她轻轻点头）", inner_enabled=False, action_enabled=False)
check("整条只有动作块 → 放弃删除", r.empty_body_protected is True and r.final_text == "（她轻轻点头）", r)
r = SG.enforce("【心里很慌】", inner_enabled=False, action_enabled=True)
check("关/开 且缺动作块 → 仍是重生（保护不越权）", r.retry_reason is not None and r.matched_rule == SG.RULE_ACTION_MISSING, r)

print()
print("=== 5. 幂等（§3.3）===")
src = "（身体垂下眼）（内心：不要这样问……）嗯。"
once = SG.enforce(src, inner_enabled=True, action_enabled=True)
twice = SG.enforce(once.final_text, inner_enabled=True, action_enabled=True)
check("第一次转换后合规", once.retry_reason is None and twice.retry_reason is None, once)
check("第二次无改写", twice.final_text is None and twice.converted == 0, twice)
src2 = "【内心：不要】（动作）话。"
once2 = SG.enforce(src2, inner_enabled=False, action_enabled=False)
twice2 = SG.enforce(once2.final_text, inner_enabled=False, action_enabled=False)
check("删块幂等：第二次无改写", twice2.final_text is None and twice2.deleted_inner == 0 and twice2.deleted_action == 0, twice2)

print()
print("=== 6. 契约 item 注入（与 declaration.py 同构）===")
items = [{"item_type": "UserMessageItem", "meta": {"item_id": "x"}, "parts": [{"type": "text", "text": "历史"}]}]
out = SG.inject_contract(items, SG.CONTRACT_BOTH, timestamp="2026-11-23T20:00:00")
check("注入到末尾", len(out) == 2 and out[-1]["meta"]["item_id"] == SG.CONTRACT_ITEM_ID, len(out))
check("item 结构过协议（item_type/meta/parts）", out[-1]["item_type"] == "UserMessageItem" and out[-1]["parts"][0]["type"] == "text")
check("meta.logical_turn_id 为 None", out[-1]["meta"]["logical_turn_id"] is None)
out2 = SG.inject_contract(out, SG.CONTRACT_BOTH, timestamp="2026-11-23T20:00:01")
check("幂等：不重复注入", len(out2) == 2, len(out2))
check("不影响非契约 item", out2[0]["meta"]["item_id"] == "x")
check("F2 声明不被误删", SG.inject_contract(
    [{"item_type": "UserMessageItem", "meta": {"item_id": "maiknock-timeline-declaration"},
      "parts": [{"type": "text", "text": "【Maiknock·时间线】声明"}]}],
    SG.CONTRACT_ACTION_ONLY)[0]["parts"][0]["text"].startswith("【Maiknock·时间线】"))

print()
print("=== 7. 空输入与边界 ===")
r = SG.enforce("", inner_enabled=True, action_enabled=True)
check("空文本放行", r.retry_reason is None and r.final_text is None)
r = SG.enforce("   ", inner_enabled=True, action_enabled=True)
check("纯空白放行", r.retry_reason is None and r.final_text is None)
r = SG.enforce("【a】【b】话（动作1）话（动作2）", inner_enabled=False, action_enabled=False)
check("多块全删", r.deleted_inner == 2 and r.deleted_action == 2 and r.final_text == "话话", r)
r = SG.enforce("【内心：慌】（动作）话", inner_enabled=False, action_enabled=True)
check("显式内心无需转换即被删（关）", r.converted == 0 and r.deleted_inner == 1 and r.final_text == "（动作）话", r)
r = SG.enforce("（内心：慌）（动作）话", inner_enabled=False, action_enabled=True)
check("显式内心先转后删（步骤顺序正确性）", r.converted == 1 and r.deleted_inner == 1 and r.final_text == "（动作）话", r)

print()
print("=" * 46)
print("通过 %d / 失败 %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
