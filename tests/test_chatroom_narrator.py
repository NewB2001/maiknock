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

    print("== 6. ★ 时光机 = mk 命令翻译器 ==")
    # 锚点 2026-11-27 00:57:53 本地时间
    import time as _t
    anchor = _t.mktime(_t.strptime("2026-11-27 00:57:53", "%Y-%m-%d %H:%M:%S"))
    cmds = narrator.build_mk_lines(["夜深之后，大家都回家了"], anchor, 2, "时")
    check("选 2h ⇒ /mk set 目标时刻", cmds[0].startswith("/mk set 2026-11-27 02:57:53 "), cmds[0])
    check("剧情跟在命令后", "夜深之后，大家都回家了" in cmds[0], cmds[0])
    cmds_plain = narrator.build_mk_lines(["夜深之后"], anchor, 0, "")
    check("不选时间 ⇒ /mk hi", cmds_plain[0].startswith("/mk hi "), cmds_plain[0])
    cmds_empty = narrator.build_mk_lines([], anchor, 2, "时")
    check("剧情空 ⇒ 仍发命令", cmds_empty == ["/mk set 2026-11-27 02:57:53"], cmds_empty)
    cmds_day = narrator.build_mk_lines(["三天后"], anchor, 3, "日")
    check("3 日 ⇒ 加 3 天", cmds_day[0].startswith("/mk set 2026-11-30 00:57:53 "), cmds_day[0])
    check("年/月单位不被接受", narrator.duration_seconds(2, "年") == 0.0)
    back = narrator.build_mk_lines(["回到过去"], anchor, 3, "时", "back")
    check("★ 后退 ⇒ /mk back 3h", back == ["/mk back 3h 回到过去"], back)
    back_d = narrator.build_mk_lines(["回到过去"], anchor, 2, "日", "back")
    check("后退 2 日 ⇒ /mk back 2d", back_d[0].startswith("/mk back 2d "), back_d[0])
    fwd = narrator.build_mk_lines(["去未来"], anchor, 3, "时", "forward")
    check("前进 ⇒ /mk set 绝对时刻", fwd[0].startswith("/mk set 2026-11-27 03:57:53 "), fwd[0])

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
