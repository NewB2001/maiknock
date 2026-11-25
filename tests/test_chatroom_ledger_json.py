# -*- coding: utf-8 -*-
"""聊天室账本离线单测：config_json 的**存/取对称**与**自愈**。

运行（必须用宿主解释器 3.13）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" test_chatroom_ledger_json.py

**这个 bug 的形状**（2026-11-26 实测踩过）：
``save_state`` 写 ``json.dumps(dict)``，``load_state`` 若把 TEXT 原样交出去
（不 loads），下次存又 dumps 一次 ⇒ **每轮存/取长度翻倍**。
原始 ``"{}"`` 经过 **28 轮**往返变成 **512 MiB** 的转义串，
3 个房间把共享账本撑到 **1.6 GB** ⇒ ``bootstrap`` 读账本直接 10 秒超时。

⇒ 本测试跑 **30 轮**存/取往返，断言长度**不增长**；并验证历史坏行能自愈。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.ledger import (  # noqa: E402
    ChatroomLedger,
    _CONFIG_JSON_MAX_BYTES,
    _decode_config_json,
)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def main() -> int:
    print("== 1. _decode_config_json 基本行为 ==")
    check("空值 → {}", _decode_config_json(None) == {})
    check("空串 → {}", _decode_config_json("") == {})
    check("dict 原样返回", _decode_config_json({"a": 1}) == {"a": 1})
    check("JSON 文本 → dict", _decode_config_json('{"a": 1}') == {"a": 1})
    check("JSON 文本（非 dict）→ {}", _decode_config_json("[1,2]") == {})
    check("坏 JSON → {}", _decode_config_json("{not json") == {})

    print("== 2. 自愈：剥掉多层转义 ==")
    raw = json.dumps({})
    for _ in range(5):
        raw = json.dumps(raw)
    check("5 层转义能剥回 {}", _decode_config_json(raw) == {},
          f"得到 {_decode_config_json(raw)!r}")

    print("== 3. 硬闸：超长行直接丢弃（不能 OOM）==")
    huge = "x" * (_CONFIG_JSON_MAX_BYTES + 1)
    check("超长行 → {}", _decode_config_json(huge) == {})

    print("== 4. ★ 存/取往返 30 轮，长度不许增长 ==")
    tmpdir = tempfile.mkdtemp(prefix="mk_chatroom_json_")
    db = os.path.join(tmpdir, "t.db")
    led = ChatroomLedger(db, bot_id="bot_test")
    try:
        led.open()
        led.create_room("room_1", "测试房间", config={"note": "你好", "n": 42})
        lengths = []
        for _ in range(30):
            state = led.load_state()
            assert state is not None
            cfg = state["rooms"][0]["config_json"]
            lengths.append(len(json.dumps(cfg, ensure_ascii=False)))
            led.save_state(state)      # 每轮都存回去（模拟用户改了点东西）
        check("30 轮后长度与第 1 轮相同", lengths[-1] == lengths[0],
              f"第1轮={lengths[0]} 第30轮={lengths[-1]}")
        check("30 轮后长度仍很小", lengths[-1] < 1024, f"实际 {lengths[-1]}")
        with led.read() as conn:
            row = conn.execute(
                "SELECT LENGTH(config_json) n FROM room WHERE room_id = 'room_1'"
            ).fetchone()
        check("库里的 config_json 行很小", row["n"] < 1024, f"实际 {row['n']} 字节")
        state = led.load_state()
        check("值往返不变",
              state["rooms"][0]["config_json"] == {"note": "你好", "n": 42},
              f"实际 {state['rooms'][0]['config_json']!r}")
    finally:
        try:
            led.close()
        except Exception:
            pass

    print("== 5. 自愈：open() 自动修好历史写坏的行 ==")
    db2 = os.path.join(tmpdir, "broken.db")
    import sqlite3

    conn = sqlite3.connect(db2)
    conn.execute(
        "CREATE TABLE room (room_id TEXT PRIMARY KEY, name TEXT, mode TEXT,"
        " group_id TEXT, created_at TEXT,"
        " config_json TEXT NOT NULL DEFAULT '{}')"
    )
    raw = json.dumps({})
    for _ in range(12):          # 12 层转义 ⇒ 8 KiB（还没到 64 KiB 阈值）
        raw = json.dumps(raw)
    conn.execute(
        "INSERT INTO room(room_id,name,mode,group_id,created_at,config_json)"
        " VALUES (?,?,?,?,?,?)",
        ("room_1", "坏房间", "stage", "", "2026-01-01 00:00:00", raw),
    )
    conn.commit()
    conn.close()

    led2 = ChatroomLedger(db2, bot_id="bot_test")
    try:
        led2.open()
        with led2.read() as conn:
            row = conn.execute(
                "SELECT config_json FROM room WHERE room_id = 'room_1'"
            ).fetchone()
        got = row["config_json"]
        check("坏行已被修好（不再是多层转义）", len(got) <= 64, f"修复后长度={len(got)}")
        check("修好后能解回 dict", isinstance(json.loads(got), dict), f"实际 {got[:80]!r}")
        check("值还原成 {}（原始值）", json.loads(got) == {}, f"实际 {got[:80]!r}")
        state = led2.load_state()
        led2.save_state(state)
        with led2.read() as conn:
            row2 = conn.execute(
                "SELECT LENGTH(config_json) n FROM room WHERE room_id = 'room_1'"
            ).fetchone()
        check("再存一轮长度不增长", row2["n"] <= 64, f"实际 {row2['n']}")
    finally:
        try:
            led2.close()
        except Exception:
            pass

    print("== 6. 正常库不该被误伤 ==")
    db3 = os.path.join(tmpdir, "ok.db")
    led3 = ChatroomLedger(db3, bot_id="bot_test")
    try:
        led3.open()
        led3.create_room("room_x", "正常房间", config={"note": "你好"})
        with led3.read() as conn:
            row = conn.execute(
                "SELECT config_json FROM room WHERE room_id = 'room_x'"
            ).fetchone()
        check("正常房间的配置没被动",
              json.loads(row["config_json"]) == {"note": "你好"},
              f"实际 {row['config_json']!r}")
    finally:
        try:
            led3.close()
        except Exception:
            pass

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
