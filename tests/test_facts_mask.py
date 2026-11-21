# -*- coding: utf-8 -*-
"""MaiKnock 「对话局面与关键事实」摘要段移除的离线单测。

🔴 **这个缺口是失忆穿帮的直接原因**（2026-11-22 00:35 实测发现）：
宿主的 blob 后面会追加一段**对话摘要**，里面摘录了麦麦之前的回复原话；
而它**不在对话块结构里**（没有 `[HH:MM:SS]` 块首）⇒ 遮蔽层碰不到它。

实测证据：回退 2 小时后，对话块被正确遮蔽（blob 3720 → 747 字节），
但摘要里仍写着「邦哥哥最喜欢吃的……是草莓蛋糕哦」⇒ **答案被直接喂给模型**。

**样本**：`samples/replyer_blob_with_facts.txt` —— 真实 1.4.8 快照里的 blob（原样）。

运行：
    "C:\\Users\\29411\\AppData\\Roaming\\uv\\tools\\maibot\\Scripts\\python.exe" test_facts_mask.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUG = Path(r"E:\Softw\MaiBot\plugins\maiknock")
sys.path.insert(0, str(PLUG))

import blob_processor as BP  # noqa: E402
import carriers as C  # noqa: E402
from matcher import Candidate  # noqa: E402

BP.set_carriers(C)

FAILED = []


def check(name, cond):
    if cond:
        print("PASS %s" % name)
    else:
        print("FAIL %s" % name)
        FAILED.append(name)


SAMPLE = (PLUG / "tests" / "samples" / "replyer_blob_with_facts.txt").read_text(
    encoding="utf-8")

print("=" * 68)
print("第 1 节：样本自检")
print("=" * 68)

check("样本含「对话局面与关键事实」", "对话局面与关键事实" in SAMPLE)
check("样本含「你正在和对方私聊」", "你正在和对方私聊" in SAMPLE)
check("样本含泄漏的答案「草莓蛋糕」", "草莓蛋糕" in SAMPLE)
print("  样本长度 =", len(SAMPLE))

print()
print("=" * 68)
print("第 2 节：_mask_facts_section 直接调用")
print("=" * 68)

out, done = BP._mask_facts_section(SAMPLE)
check("已移除", done is True)
check("  摘要标记还在（占位说明）", "对话局面与关键事实" in out)
check("  ★ 泄漏的答案已消失", "草莓蛋糕" not in out)
check("  ★ 宿主尾部提示保留", "你正在和对方私聊" in out)
check("  长度变短", len(out) < len(SAMPLE))

print()
print("=" * 68)
print("第 3 节：边界情况")
print("=" * 68)

# ① 没有摘要段 ⇒ 不动
plain = "当前时间：2026-11-21 23:00:00\n[10:00:00] A说：hi"
out2, done2 = BP._mask_facts_section(plain)
check("没有摘要段 ⇒ 不动", done2 is False and out2 == plain)

# ② 有起点但没有终点 ⇒ **不动**（宁可少遮不可错删）
no_end = "对话局面与关键事实：\n一些内容"
out3, done3 = BP._mask_facts_section(no_end)
check("没有终点标记 ⇒ 不动", done3 is False and out3 == no_end)

# ③ 空串
out4, done4 = BP._mask_facts_section("")
check("空串 ⇒ 不动", done4 is False and out4 == "")

print()
print("=" * 68)
print("第 4 节：process_blob 集成（有遮蔽 ⇒ 才移除）")
print("=" * 68)

# ⚠️ **不能拿上面的真实样本做集成** —— 它的块首已被 relabel 成
#    `[2026-11-21 21:46:06]` ⇒ **不再匹配块起点正则** `^\[HH:MM:SS\]`
#    ⇒ 切不出块 ⇒ 天然没有遮蔽 ⇒ 也不会触发摘要移除。
#    ⇒ 这里用一个**未 relabel** 的自造 blob。
FAKE_BLOB = (
    "\n\n你正在和邦本国夫聊天，这是你们之前聊的内容:\n"
    "当前时间：2026-11-21 23:00:00\n"
    "[10:00:00] 日向雏田(你)说：我最喜欢吃草莓蛋糕哦\n"
    "[10:05:00] 邦本国夫说：记住了\n"
    "\n对话局面与关键事实：\n"
    "\"邦哥哥最喜欢吃的……是草莓蛋糕哦。\"\n"
    "\n你正在和对方私聊,现在请你读读之前的聊天记录，把握当前的话题，然后给出日常且口语化的回复。\n"
)


def _mk_cand(ts, speaker, content, hhmmss):
    return Candidate(ts=ts, hhmmss=hhmmss, speaker_raw=speaker, content=content,
                     raw=None, message_id="", display_id="")


import datetime as _dt  # noqa: E402

t1 = _dt.datetime.strptime("2026-11-21 10:00:00", "%Y-%m-%d %H:%M:%S").timestamp()
t2 = _dt.datetime.strptime("2026-11-21 10:05:00", "%Y-%m-%d %H:%M:%S").timestamp()
cands = [
    _mk_cand(t1, "日向雏田(你)", "我最喜欢吃草莓蛋糕哦", "10:00:00"),
    _mk_cand(t2, "邦本国夫", "记住了", "10:05:00"),
]
res = BP.process_blob(FAKE_BLOB, cands, is_hidden=lambda ts: True, offset_sec=-2 * 3600)
check("有块被遮蔽", res.hidden_blocks > 0)
check("有遮蔽 ⇒ 摘要段被移除",
      "对话局面与关键事实" in res.text and "草莓蛋糕" not in res.text)
check("  有告警说明", any("对话局面与关键事实" in w for w in res.warnings))
check("  宿主尾部提示仍在", "你正在和对方私聊" in res.text)

print()
print("=" * 68)
print("第 5 节：没有遮蔽 ⇒ 不动摘要（不能误伤正常对话）")
print("=" * 68)

cands2 = [_mk_cand(1.0, "x", "y", "10:00:00")]
res2 = BP.process_blob(FAKE_BLOB, cands2, is_hidden=lambda ts: False, offset_sec=-2 * 3600)
check("没有遮蔽 ⇒ 摘要保留", "草莓蛋糕" in res2.text)

print()
if FAILED:
    print("❌ %d 项失败：%s" % (len(FAILED), FAILED))
    sys.exit(1)
print("✅ 全部通过")
