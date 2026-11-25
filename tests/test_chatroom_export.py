# -*- coding: utf-8 -*-
"""麦麦聊天室 · 导出离线单测（不依赖 MaiBot 运行）。

运行（必须用宿主解释器 3.13）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" test_chatroom_export.py

**这个测试在防什么**：
① **含已作废的段**（否则导出的记录比实际少，对不上账）；
② **绝不捎带 api_key** 之类的凭据（只挑白名单字段）；
③ Markdown / JSON 结构对（按段分组、带发言人+时间）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom import export  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


TIMELINE = [
    {
        "seq": 1, "kind": "normal", "voided": False, "created_at": "2026-01-01 10:00:00",
        "messages": [
            {"speaker_name": "鸣人", "speaker_member_id": "role_naruto", "kind": "line",
             "content": "拉面店集合！", "time": "10:00"},
            {"speaker_name": "雏田", "speaker_member_id": "bot_hinata", "kind": "reply",
             "content": "好的……马上来。", "time": "10:01"},
        ],
    },
    {
        "seq": 2, "kind": "plot", "voided": False, "created_at": "2026-01-01 10:05:00",
        "messages": [
            {"speaker_name": "剧情旁白", "speaker_member_id": "narrator", "kind": "narrator",
             "content": "三天后，第七班接到召集。", "time": "10:05"},
        ],
    },
    {
        "seq": 3, "kind": "normal", "voided": True, "created_at": "2026-01-01 10:10:00",
        "messages": [
            {"speaker_name": "鸣人", "speaker_member_id": "role_naruto", "kind": "line",
             "content": "重演前被作废的台词", "time": "10:10"},
        ],
    },
]


def main() -> int:
    print("== 1. Markdown 导出 ==")
    md = export.build_markdown(TIMELINE, "拉面店门口", "room_ramen")
    check("标题带房间名", "拉面店门口" in md)
    check("按段分组（第 1/2/3 段）", "第 1 段" in md and "第 3 段" in md)
    check("旁白段标注", "剧情旁白" in md)
    check("★ 含已作废段（标 [已作废]）", "[已作废]" in md and "被作废的台词" in md)
    check("发言人+时间都在", "鸣人" in md and "10:00" in md)
    check("回复也在", "马上来" in md)

    print("== 2. JSON 导出 ==")
    js = export.build_json(TIMELINE, "拉面店门口", "room_ramen")
    data = json.loads(js)
    check("能解析成 JSON", isinstance(data, dict))
    check("段数对（含作废段）", len(data["segments"]) == 3, f"实际 {len(data['segments'])}")
    check("★ 作废标记在", data["segments"][2]["voided"] is True)
    check("房间信息在", data["room_name"] == "拉面店门口")

    print("== 3. ★ 绝不捎带凭据 ==")
    # 往输入里塞一个 api_key 字段，确认导出里不出现
    poisoned = json.loads(json.dumps(TIMELINE))
    poisoned[0]["api_key"] = "SECRET_KEY_123"
    poisoned[0]["messages"][0]["api_key"] = "SECRET_KEY_456"
    md2 = export.build_markdown(poisoned, "房", "r")
    js2 = export.build_json(poisoned, "房", "r")
    check("★ Markdown 不含 api_key", "SECRET_KEY" not in md2)
    check("★ JSON 不含 api_key", "SECRET_KEY" not in js2)
    check("★ JSON 不含 api_key 键名", "api_key" not in js2)

    print("== 4. 文件名安全 ==")
    fn = export.safe_filename('a/b:c*d', "rid", "md")
    check("路径分隔符被替换", "/" not in fn and ":" not in fn and "*" not in fn, fn)
    check("扩展名对", fn.endswith(".md"), fn)
    check("空名兜底", export.safe_filename("", "", "json").endswith(".json"))

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
