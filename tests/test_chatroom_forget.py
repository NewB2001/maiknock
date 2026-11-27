# -*- coding: utf-8 -*-
"""麦麦聊天室 · 彻底遗忘（forget.py）离线单测。

运行：
    python test_chatroom_forget.py

**在防什么**：
① 内容层软删（is_deleted=1）而不是物理删——宿主检索认这个标记；
② 共现段落（同时提到多个别名）也删——用户拍板不怕牵连；
③ 画像层/身份层物理删干净；
④ 幂等：重复执行是空操作，不报错（两个 Bot 实例都触发也无害）；
⑤ dry_run 不落盘；
⑥ 库文件缺失/表结构异常 ⇒ errors 里有明确原因（不静默假装成功）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatroom.forget import forget_account, forget_account_all  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def make_memory_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE paragraphs (
            hash TEXT PRIMARY KEY, content TEXT, source TEXT,
            is_deleted INTEGER DEFAULT 0, deleted_at REAL, deletion_reason TEXT
        );
        CREATE TABLE entities (
            hash TEXT PRIMARY KEY, name TEXT UNIQUE,
            is_deleted INTEGER DEFAULT 0, deleted_at REAL
        );
        CREATE TABLE relations (
            hash TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
            is_deleted INTEGER DEFAULT 0, deleted_at REAL,
            is_inactive BOOLEAN DEFAULT 0, inactive_since REAL
        );
        CREATE TABLE person_identities (person_id TEXT PRIMARY KEY, platform TEXT, user_id TEXT, canonical_name TEXT);
        CREATE TABLE person_identity_aliases (person_id TEXT, alias TEXT, group_id TEXT DEFAULT '');
        CREATE TABLE person_profile_snapshots (snapshot_id INTEGER PRIMARY KEY, person_id TEXT, profile_text TEXT);
        CREATE TABLE person_profile_overrides (person_id TEXT, override_text TEXT);
        CREATE TABLE person_profile_alias_overrides (person_id TEXT, aliases_json TEXT);
        CREATE TABLE person_profile_refresh_queue (person_id TEXT, status TEXT);
        CREATE TABLE person_profile_active_persons (stream_id TEXT, user_id TEXT, person_id TEXT);
        """
    )
    conn.execute(
        "INSERT INTO paragraphs VALUES (?,?,?,?,?,?)",
        ("p1", "鸣人今天吃了拉面", "person_fact:pid1", 0, None, None),
    )
    conn.execute(
        "INSERT INTO paragraphs VALUES (?,?,?,?,?,?)",
        ("p2", "鸣人和佐助去钓鱼", "chat_summary:abc", 0, None, None),
    )
    conn.execute(
        "INSERT INTO paragraphs VALUES (?,?,?,?,?,?)",
        ("p3", "佐助独自修炼", "person_fact:pid2", 0, None, None),
    )
    conn.execute("INSERT INTO entities VALUES (?,?,?,?)", ("e1", "鸣人", 0, None))
    conn.execute("INSERT INTO entities VALUES (?,?,?,?)", ("e2", "佐助", 0, None))
    conn.execute(
        "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?)",
        ("r1", "鸣人", "朋友", "佐助", 0, None, 0, None),
    )
    for t in ("person_identities", "person_identity_aliases", "person_profile_snapshots",
              "person_profile_overrides", "person_profile_alias_overrides",
              "person_profile_refresh_queue", "person_profile_active_persons"):
        if t == "person_profile_snapshots":
            conn.execute("INSERT INTO person_profile_snapshots VALUES (?,?,?)", (1, "pid1", "profile"))
        elif t in ("person_identities",):
            conn.execute("INSERT INTO person_identities VALUES (?,?,?,?)", ("pid1", "webui", "user.naruto", "鸣人"))
        else:
            conn.execute(f"INSERT INTO {t} (person_id) VALUES (?)", ("pid1",))
    conn.commit()
    conn.close()


def make_main_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE person_info (id INTEGER PRIMARY KEY, person_id TEXT, platform TEXT, user_id TEXT, user_nickname TEXT);
        CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY, session_id TEXT, platform TEXT, user_id TEXT);
        CREATE TABLE mai_messages (id INTEGER PRIMARY KEY, message_id TEXT, session_id TEXT, user_id TEXT, processed_plain_text TEXT);
        """
    )
    conn.execute("INSERT INTO person_info VALUES (?,?,?,?,?)", (1, "pid1", "webui", "user.naruto", "鸣人"))
    conn.execute("INSERT INTO chat_sessions VALUES (?,?,?,?)", (1, "sid1", "webui", "user.naruto"))
    conn.execute("INSERT INTO mai_messages VALUES (?,?,?,?,?)", (1, "m1", "sid1", "user.naruto", "你好"))
    conn.commit()
    conn.close()


def main() -> int:
    print("== 1. 内容层软删 + 共现段落一并删 ==")
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "metadata.db"
        bd = Path(tmp) / "MaiBot.db"
        make_memory_db(md)
        make_main_db(bd)
        res = forget_account(
            memory_db=md, main_db=bd, person_id="pid1", user_id="user.naruto",
            session_id="sid1", aliases=["鸣人", "路飞"],
        )
        print("  ", res["counts"])
        check("执行成功无错误", res["ok"], f"{res['errors']}")
        c = sqlite3.connect(str(md))
        deleted = [r[0] for r in c.execute("SELECT hash FROM paragraphs WHERE is_deleted=1")]
        active = [r[0] for r in c.execute("SELECT hash FROM paragraphs WHERE is_deleted=0")]
        check("鸣人段落软删", "p1" in deleted, f"{deleted}")
        check("共现段落一并删除（不怕牵连）", "p2" in deleted, f"{deleted}")
        check("无关段落保留", active == ["p3"], f"{active}")
        ents = [r[0] for r in c.execute("SELECT name FROM entities WHERE is_deleted=0")]
        check("实体鸣人软删、佐助保留", ents == ["佐助"], f"{ents}")
        rel = c.execute("SELECT is_deleted FROM relations WHERE hash='r1'").fetchone()
        check("关系软删", rel and rel[0] == 1, f"{rel}")
        check("画像表物理删干净",
              c.execute("SELECT count(*) FROM person_identities").fetchone()[0] == 0)
        c.close()
        c = sqlite3.connect(str(bd))
        check("person_info 物理删", c.execute("SELECT count(*) FROM person_info").fetchone()[0] == 0)
        check("chat_sessions 物理删", c.execute("SELECT count(*) FROM chat_sessions").fetchone()[0] == 0)
        check("mai_messages 物理删", c.execute("SELECT count(*) FROM mai_messages").fetchone()[0] == 0)
        c.close()

        print("== 2. 幂等：重复执行不报错 ==")
        res2 = forget_account(
            memory_db=md, main_db=bd, person_id="pid1", user_id="user.naruto",
            session_id="sid1", aliases=["鸣人", "路飞"],
        )
        check("第二次仍成功", res2["ok"], f"{res2['errors']}")
        check("第二次无新增软删", res2["counts"]["memory"].get("paragraphs", 0) == 0,
              f"{res2['counts']}")

    print("== 3. dry_run 不落盘 ==")
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "metadata.db"
        bd = Path(tmp) / "MaiBot.db"
        make_memory_db(md)
        make_main_db(bd)
        res = forget_account(
            memory_db=md, main_db=bd, person_id="pid1", user_id="user.naruto",
            session_id="sid1", aliases=["鸣人"], dry_run=True,
        )
        check("dry_run 算到命中", res["counts"]["memory"]["paragraphs"] == 2, f"{res['counts']}")
        c = sqlite3.connect(str(md))
        check("dry_run 后段落仍活着",
              c.execute("SELECT count(*) FROM paragraphs WHERE is_deleted=0").fetchone()[0] == 3)
        c.close()

    print("== 4. 失败留痕：缺库/缺字段 ==")
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "none.db"
        bd = Path(tmp) / "MaiBot.db"
        make_main_db(bd)
        res = forget_account(
            memory_db=md, main_db=bd, person_id="pid1", user_id="user.naruto",
            session_id="sid1", aliases=["鸣人"],
        )
        check("缺库时 ok=False", not res["ok"])
        check("缺库时有明确错误", any("库文件不存在" in e for e in res["errors"]), f"{res['errors']}")

    print("== 5. 多实例遍历 ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ("bot1", "bot2"):
            work = root / name
            (work / "data" / "a-memorix" / "metadata").mkdir(parents=True)
            make_memory_db(work / "data" / "a-memorix" / "metadata" / "metadata.db")
            make_main_db(work / "data" / "MaiBot.db")
        res = forget_account_all(
            workspace=root, bot_dirs=[root / "bot1", root / "bot2"],
            person_id="pid1", user_id="user.naruto", session_id="sid1", aliases=["鸣人"],
        )
        check("两实例都成功", res["ok"] and len(res["instances"]) == 2, f"{res['errors']}")

    print("\n=== 全部通过 ===" if not FAILED else f"\n=== 失败 {len(FAILED)} 项 ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
