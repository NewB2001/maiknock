# -*- coding: utf-8 -*-
"""麦麦聊天室 · 时光机旁白离线单测（不依赖 MaiBot 运行）。

运行（必须用宿主解释器 3.13）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" test_chatroom_narrator.py

**这个测试在防什么**：
① 相对时间表述正确（「3 日」→「三天后」），**绝不产出绝对日期**（R6-18）；
② 用户写什么旁白就说什么（**不改写正文**，不生成精妙叙事）；
③ 逐行拼装（一行一条，空行丢弃）；
④ 旁白段形状对（kind=plot、发言人=剧情旁白、kind=narrator，占普通段位可被截断）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom import narrator  # noqa: E402
from chatroom.mock_data import NARRATOR  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def main() -> int:
    print("== 1. 相对时间表述 ==")
    check("3 日 → 三天后", narrator.relative_phrase(3, "日") == "三天后",
          narrator.relative_phrase(3, "日"))
    check("2 个月 → 两个月后", narrator.relative_phrase(2, "月") == "两个月后")
    check("1 年 → 一年后", narrator.relative_phrase(1, "年") == "一年后")
    check("数字 0 → 空", narrator.relative_phrase(0, "日") == "")
    check("数字缺失 → 空", narrator.relative_phrase("", "日") == "")
    check("非法数字 → 空", narrator.relative_phrase("abc", "日") == "")
    # ★ 绝不产出绝对日期：结果里不该出现「年-月-日」这种数字日期形态
    phrase = narrator.relative_phrase(3, "日")
    check("★ 不写绝对日期（无 2026- 之类）", "2026" not in phrase and "-" not in phrase, phrase)

    print("== 2. 逐行拼装 + 相对时间并进第一条 ==")
    texts = narrator.build_narrator_texts(["第七班接到召集", "窗开着，没人捡简报"], 3, "日")
    check("两条事件 → 两条旁白", len(texts) == 2, f"实际 {texts}")
    check("★ 相对时间并进第一条", texts[0].startswith("三天后，"), texts[0])
    check("第一条正文保留用户原文", "第七班接到召集" in texts[0], texts[0])
    check("第二条原样（不改写）", texts[1] == "窗开着，没人捡简报", texts[1])

    print("== 3. 不给时间 ⇒ 不加前缀 ==")
    texts = narrator.build_narrator_texts(["只是普通的一天"], 0, "日")
    check("无前缀", texts == ["只是普通的一天"], f"实际 {texts}")

    print("== 4. 空行丢弃 / 空输入 ==")
    check("空行被丢", narrator.build_narrator_texts(["  ", "有内容", ""], 0, "") == ["有内容"])
    check("全空 → 空列表", narrator.build_narrator_texts(["", "  "], 0, "") == [])
    check("None → 空列表", narrator.build_narrator_texts(None, 0, "") == [])

    print("== 5. 旁白段形状（占普通段位、可被截断）==")
    seg = narrator.build_narrator_segment(["三天后，风起"], NARRATOR)
    check("kind=plot", seg["kind"] == "plot")
    check("未作废", seg["voided"] is False)
    check("每条一条消息", len(seg["messages"]) == 1)
    m = seg["messages"][0]
    check("发言人是剧情旁白", m["speaker_name"] == "剧情旁白", m["speaker_name"])
    check("消息 kind=narrator", m["kind"] == "narrator")
    check("member_id=narrator", m["speaker_member_id"] == "narrator")

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
