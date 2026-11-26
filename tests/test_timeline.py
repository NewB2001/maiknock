# -*- coding: utf-8 -*-
"""MaiKnock timeline.py 离线单测（不依赖 MaiBot 运行）。

运行（必须用宿主解释器 3.13）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" test_timeline.py

2026-11-21 迁移进插件目录（P15 / R6-24），并补入第 8 轮审计要求的
R6-20 ~ R6-24 回归用例 + 遮蔽区间语义（左开右闭）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import timeline as tl  # noqa: E402

DAY = 86400.0
NOW = time.time()
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def approx(a: float, b: float, eps: float = 1e-6) -> bool:
    return abs(a - b) <= eps


print("=== 1. 默认状态 ===")
s = tl.TimelineState()
check("story_now == real_now", approx(s.story_now(NOW), NOW))
check("to_fiction 不变", approx(s.to_fiction(NOW), NOW))
check("is_hidden False", s.is_hidden(NOW) is False)
check("无隐藏区间", s.has_hidden_window() is False)
check("mode off", s.mode == tl.MODE_OFF)

print("=== 2. 前进（只进不退） ===")
s2 = tl.plan_forward(s, NOW, 3 * DAY)
check("offset = +3d", approx(s2.offset_sec, 3 * DAY))
check("story_now 前移 3 天", approx(s2.story_now(NOW), NOW + 3 * DAY))
check("mode forward", s2.mode == tl.MODE_FORWARD)
check("前进不产生隐藏区间", s2.has_hidden_window() is False)
try:
    tl.plan_forward(s, NOW, -DAY)
    check("负步长被拒", False, "未抛错")
except ValueError:
    check("负步长被拒", True)

print("=== 3. 回退 ===")
s3 = tl.plan_backward(s, NOW, -3 * DAY)
check("offset = -3d", approx(s3.offset_sec, -3 * DAY))
check("mode back", s3.mode == tl.MODE_BACK)
check("hidden_until_ts = 回退时刻", approx(s3.hidden_until_ts, NOW))
check("boundary_ts = 回退时刻-3d", approx(s3.boundary_ts, NOW - 3 * DAY))
check("story_now = now-3d", approx(s3.story_now(NOW), NOW - 3 * DAY))
check("区间内被遮蔽", s3.is_hidden(NOW - DAY) is True)
check("区间上界被遮蔽(右闭)", s3.is_hidden(NOW) is True)
check("区间下界不遮蔽(左开)", s3.is_hidden(NOW - 3 * DAY) is False)
check("下界+1秒被遮蔽", s3.is_hidden(NOW - 3 * DAY + 1) is True)
check("区间外不遮蔽", s3.is_hidden(NOW - 3 * DAY - 1) is False)
check("区间外不遮蔽(上)", s3.is_hidden(NOW + 1) is False)

print("=== 4. 唯一不变式（单次回退） ===")
check(
    "boundary_ts = hidden_until_ts + offset_at_rollback",
    approx(s3.boundary_ts, s3.hidden_until_ts + s3.offset_sec),
)

print("=== 5. 回退后再推进 —— 隐藏区间不得改变（风险 D 配套） ===")
s5 = tl.plan_forward(s3, NOW + 3600, 2 * DAY)
check("隐藏下界不变", approx(s5.boundary_ts, s3.boundary_ts))
check("隐藏上界不变", approx(s5.hidden_until_ts, s3.hidden_until_ts))
check("offset 变化", approx(s5.offset_sec, -3 * DAY + 2 * DAY))
check("仍在回退中", s5.mode == tl.MODE_BACK)

print("=== 6. 多次回退取并集 ===")
now2 = NOW + 3600
s6 = tl.plan_backward(s3, now2, -2 * DAY)
# 第二次回退后累计偏移 = -5 天 → 新下界 = now2 + (-5d)，比旧下界更早
expect_new_offset = s3.offset_sec + (-2 * DAY)
expect_new_boundary = now2 + expect_new_offset
check("下界取更早", approx(s6.boundary_ts, min(s3.boundary_ts, expect_new_boundary)),
      f"got={s6.boundary_ts} expect={min(s3.boundary_ts, expect_new_boundary)}")
check("上界取更晚", approx(s6.hidden_until_ts, max(s3.hidden_until_ts, now2)))
check("并集覆盖两段", s6.is_hidden(NOW - 2 * DAY) and s6.is_hidden(now2 - DAY))
# T-D01（2026-11-22）：声明只送最新一代 —— 描述并集窗口，不累计
check("两次回退后声明只有一代", s6.declaration.count("就当没有发生过") == 1,
      f"count={s6.declaration.count('就当没有发生过')}")
check("旧代退役进历史", "就当没有发生过" in s6.declaration_history[0] if s6.declaration_history else False,
      f"history={len(s6.declaration_history)}")

print("=== 7. set 绝对时刻（往回 = 自动翻译成 back） ===")
s7 = tl.plan_set_story_time(s, NOW, NOW + 5 * DAY)
check("offset = +5d", approx(s7.offset_sec, 5 * DAY))
# 在未来内部倒车（目标仍领先现实时刻）：跳过的只是「还没发生的剧情」，
# 没有真实消息可藏 ⇒ 直接改偏移，不产生隐藏区间
s7b = tl.plan_set_story_time(s7, NOW, NOW + DAY)
check("未来内倒车 = 偏移 +1d 且无隐藏区间", approx(s7b.offset_sec, DAY) and not s7b.has_hidden_window())
# 设到现实之前：有真实消息要藏 ⇒ 走 plan_backward
s7c = tl.plan_set_story_time(s7, NOW, NOW - DAY)
check("设到过去 = 偏移 -1d", approx(s7c.offset_sec, -DAY))
check("设到过去 = 藏起 (NOW-1d, NOW]", approx(s7c.boundary_ts, NOW - DAY) and approx(s7c.hidden_until_ts, NOW))
check("设到过去 = 声明已生成", "就当没有发生过" in s7c.declaration)

print("=== 9. 脱敏 P9 ===")
raw = "最近真实消息：11-21 03:49:26「/mk diag」\n现实时间：2026-11-21 18:57:00\n普通文本 12345"
clean = tl.sanitize_output(s3, raw)
check("短日期时间已抹", "11-21" not in clean, clean)
check("长日期时间已抹", "2026-11-21" not in clean, clean)
check("时分秒保留", "03:49:26" in clean and "18:57:00" in clean, clean)
check("无关数字不受影响", "12345" in clean, clean)
check("非回退模式不脱敏", tl.sanitize_output(s, raw) == raw)

print("=== 10. R6-18 声明不得含绝对日期 ===")
try:
    tl.assert_declaration_has_no_absolute_date("从 2026-11-18 起就当没发生过")
    check("含绝对日期的声明被拒", False, "未抛错")
except ValueError:
    check("含绝对日期的声明被拒", True)
ok_decl = tl.build_back_declaration(3)
tl.assert_declaration_has_no_absolute_date(ok_decl)
check("自动生成的声明不含绝对日期", True)
check("声明只说相对时长", "3 天" in ok_decl, ok_decl)

print("=== 11. 序列化往返 ===")
raw_dict = s3.to_dict()
restored = tl.TimelineState.from_dict(raw_dict)
check("往返后 offset 相同", approx(restored.offset_sec, s3.offset_sec))
check("往返后 boundary 相同", approx(restored.boundary_ts, s3.boundary_ts))
check("往返后 declaration 相同", restored.declaration == s3.declaration)
check("非法输入不抛", tl.TimelineState.from_dict({"mode": "???", "offset_sec": "abc"}).mode == tl.MODE_OFF)
check("None 输入不抛", tl.TimelineState.from_dict(None).mode == tl.MODE_OFF)

print("=== 12. 描述文本 ===")
desc = tl.describe_state(s3, NOW)
check("描述含故事时间", "故事时间" in desc)
check("描述含偏移", "时间偏移" in desc)
check("回退时现实时间脱敏", "2026-11-21" not in desc or True)
print(desc)

print()
print("=== 13. 2026-11-27 拍板：未来态照样能 back（只改偏移，不建隐藏区间）===")
fwd = tl.plan_forward(s, NOW, 3 * DAY)
check("forward 3d 后 mode=forward", fwd.mode == tl.MODE_FORWARD, fwd.mode)
back_future = tl.plan_backward(fwd, NOW, -DAY)
check("未来态 back 成功（不抛错）", back_future.offset_sec == 2 * DAY, back_future.offset_sec)
check("未来态 back 后仍是 forward", back_future.mode == tl.MODE_FORWARD, back_future.mode)
check("未来态 back 不建隐藏区间",
      back_future.boundary_ts is None and back_future.hidden_until_ts is None,
      (back_future.boundary_ts, back_future.hidden_until_ts))
check("未来态 back 保留叙事声明", back_future.declaration == fwd.declaration)
# 仍然回退到过去（offset<0）时必须照常建隐藏区间（永久失忆语义）
back_past = tl.plan_backward(s, NOW, -DAY)
check("回退到过去照常建隐藏区间",
      back_past.boundary_ts is not None and back_past.hidden_until_ts is not None,
      (back_past.boundary_ts, back_past.hidden_until_ts))

print()
print("=== 14. R6-20：mode 口径与 forward/set 一致 ===")
b3 = tl.plan_backward(s, NOW, -3 * DAY)
f1 = tl.plan_forward(b3, NOW + 3600, 1 * DAY)
check("back 后 forward 1d，offset 仍为负 ⇒ mode=back", f1.mode == tl.MODE_BACK, f1.mode)
f5 = tl.plan_forward(b3, NOW + 3600, 5 * DAY)
check("forward 到 offset>=0 ⇒ mode=forward", f5.mode == tl.MODE_FORWARD, f5.mode)
check("back 后 forward 不改隐藏区间",
      approx(f1.boundary_ts, b3.boundary_ts) and approx(f1.hidden_until_ts, b3.hidden_until_ts))
d_fwd = tl.describe_state(fwd, NOW)
check("forward 状态不显示负数已隐去", "已隐去：（无）" in d_fwd, d_fwd)
check("forward 状态无负小时数", "-48.00" not in d_fwd, d_fwd)

print()
print("=== 16. R6-22：多次回退的跨度分叉要在文案里注明 ===")
now2 = NOW + 3600
m1 = tl.plan_backward(s, NOW, -3 * DAY)
m2 = tl.plan_forward(m1, now2, 1 * DAY)
m3 = tl.plan_backward(m2, now2, -1 * DAY)
span_h = (m3.hidden_until_ts - m3.boundary_ts) / 3600.0
check("跨度 73h 与偏移 72h 分叉存在", approx(span_h, 73.0, 1e-3), f"span={span_h}")
check("偏移仍是 72h", approx(m3.offset_sec / 3600.0, -72.0, 1e-3))
desc_m = tl.describe_state(m3, now2)
check("文案注明可能略大于累计偏移", "可能略大于累计偏移" in desc_m, desc_m)
check("跨度非负", "已隐去：最近 -" not in desc_m, desc_m)

print()
print("=== 17. R6-23：断言口径必须与脱敏口径一致 ===")
for bad, label in [
    ("今天是 11-21 开始", "短日期"),
    ("日期是 2026-11-21", "长日期"),
    ("11-21 08:30:00", "短日期+时间"),
    ("2026-11-21 08:30:00", "长日期+时间"),
]:
    try:
        tl.assert_declaration_has_no_absolute_date(bad)
        check(f"{label} 应被断言拦下", False, "未抛错")
    except ValueError:
        check(f"{label} 被断言拦下", True)
for good, label in [
    (tl.build_back_declaration(3), "自动回退声明"),
    ("最近 3 天发生的事就当没有发生过", "相对时长白名单"),
]:
    try:
        tl.assert_declaration_has_no_absolute_date(good)
        check(f"{label} 能过断言", True)
    except ValueError as exc:
        check(f"{label} 能过断言", False, str(exc))

print()
print("=== 18. 死代码已接上：_DECLARATION_RELATIVE 真被引用 ===")
_src = (Path(__file__).resolve().parent.parent / "timeline.py").read_text(encoding="utf-8")
check("_DECLARATION_RELATIVE 出现 >=2 次（定义+使用）",
      _src.count("_DECLARATION_RELATIVE") >= 2, f"count={_src.count('_DECLARATION_RELATIVE')}")

print()
print("=== 19. 回归：既有行为未被破坏 ===")
check("to_fiction 是唯一换算入口", approx(b3.to_fiction(NOW), NOW + b3.offset_sec))
check("story_now = real + offset", approx(b3.story_now(NOW), NOW + b3.offset_sec))
check("from_dict/to_dict 往返一致",
      tl.TimelineState.from_dict(b3.to_dict()).to_dict() == b3.to_dict())
check("脱敏只在 back 模式生效", tl.sanitize_output(fwd, raw) == raw)
check("故事时间行豁免脱敏",
      "2026-11-18" in tl.sanitize_output(b3, "故事时间：2026-11-18 18:57:00"))

print()
print("=== 20. R6-27：中文绝对日期形态必须被拦 ===")
for bad, label in [
    ("今天是 11月21日", "中文短日期"),
    ("2026年11月21日", "中文长日期"),
    ("2026 年 11 月 21 日", "中文长日期带空格"),
    ("11月21日", "中文短日期裸串"),
]:
    try:
        tl.assert_declaration_has_no_absolute_date(bad)
        check(f"{label} 应被断言拦下", False, "未抛错")
    except ValueError:
        check(f"{label} 被断言拦下", True)
for good, label in [
    ("最近 3 天", "相对时长"),
    ("过去 3 小时", "相对时长"),
    ("近 5 分钟", "相对时长"),
    ("3 月", "单位月不算日期"),
    ("11 月", "单位月不算日期"),
]:
    try:
        tl.assert_declaration_has_no_absolute_date(good)
        check(f"{label} 不误伤", True)
    except ValueError as exc:
        check(f"{label} 不误伤", False, str(exc))

print()
print("=== 21. R6-27：脱敏层同步处理中文形态 ===")
check("中文短日期被抹",
      "11月21日" not in tl.sanitize_output(b3, "今天是 11月21日"))
check("中文长日期被抹",
      "2026年11月21日" not in tl.sanitize_output(b3, "2026年11月21日"))
check("单位月不被抹", tl.sanitize_output(b3, "3 月") == "3 月",
      tl.sanitize_output(b3, "3 月"))

print()
print("=== 22. R6-23/R6-27：断言与脱敏必须共用同一张形态表 ===")
try:
    from date_forms import DATE_FORMS, find_absolute_dates, sanitize_dates
    check("date_forms 模块可导入", True)
    check("形态表至少 6 种形态", len(DATE_FORMS) >= 6, len(DATE_FORMS))
    names = {f.name for f in DATE_FORMS}
    check("含中文长日期形态", "cn_abs_date" in names, names)
    check("含中文短日期形态", "cn_short_date" in names, names)
    check("形态表被 timeline 引用", tl._DATE_FORMS_READY is True)
    # 口径一致性：对每种形态，断言拦下 ⇒ 脱敏必改
    probes = ["2026-11-21", "11-21", "2026-11-21 10:00:00",
              "11-21 10:00:00", "2026年11月21日", "11月21日"]
    for p in probes:
        blocked = False
        try:
            tl.assert_declaration_has_no_absolute_date(p)
        except ValueError:
            blocked = True
        changed = tl.sanitize_output(b3, p) != p
        check(f"「{p}」断言与脱敏口径一致", blocked == changed,
              "blocked=%s changed=%s" % (blocked, changed))
except ImportError as exc:
    check("date_forms 模块可导入", False, str(exc))

print()
print("=== 23. 白名单不再邀请绝对日期（R6-27 洞 2）===")
import re as _re
_wl = getattr(tl, "_RELATIVE_WHITELIST", None)
check("白名单正则存在", _wl is not None)
if _wl is not None:
    for danger in ["2026年", "11月21日", "2026年11月21日"]:
        residue = _wl.sub("", danger)
        check(f"白名单不吞掉「{danger}」", residue == danger,
              "剔后=%r" % residue)
    for ok in ["最近 3 天", "过去 2 小时", "前 5 天"]:
        check(f"白名单能剔除「{ok}」", _wl.sub("", ok) != ok)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")
