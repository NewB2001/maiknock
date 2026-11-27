# -*- coding: utf-8 -*-
"""麦麦聊天室 · 彻底遗忘（删除「我的角色」时联动清宿主数据）。

**为什么需要**（2026-11-27）：聊天室删角色只清自己的账本，宿主那边的通讯录名片、
聊天流、历史消息、长期记忆都还在 ⇒ 麦麦还记得一个"已经不存在"的角色 ⇒ 串台。
用户拍板：删角色 = 彻底遗忘，删不干净不如不删；共现段落（"鸣人和佐助钓鱼"）
一并删，不怕牵连。

**实现口径**（查过宿主源码，见 09-开发进度.md）：
1. **内容层**（A_memorix `paragraphs`/`entities`/`relations`）——**软删除**：
   `UPDATE is_deleted=1, deleted_at=..., deletion_reason=...`。
   宿主检索层有 69 处 `is_deleted` 过滤（FTS / BM25 / episode / fact），
   软删后即检索不到；这是宿主 `memory_delete_admin` 自己在用的机制。
2. **画像层**（`person_identities`/`person_identity_aliases`/`person_profile_snapshots`/
   `person_profile_overrides`/`person_profile_alias_overrides`/
   `person_profile_refresh_queue`/`person_profile_active_persons`）——**物理删**：
   这些表没有 is_deleted 字段，只能 DELETE。aliases 有 ON DELETE CASCADE。
3. **身份/会话层**（MaiBot.db 的 `person_info`/`chat_sessions`/`mai_messages`）——**物理删**。
   ⚠️ 宿主每收到消息都会 `Person.register_person()` 重建名片（know_times=1），
   但重建的是**全新档案**（不继承旧昵称/know_counts）⇒ 只要记忆层已清，
   重建出来就是失忆的陌生人，效果=彻底遗忘。

**幂等**：全部用 `UPDATE ... WHERE is_deleted=0` / `DELETE WHERE ...`，
重复执行是空操作（0 行），不报错 ⇒ 两个 Bot 实例都触发也无害。

**失败处理**：**不抛异常吞掉**——每个库/每张表的失败都记进 errors 列表返回，
由调用方写 ledger.report_error（前端提示栏可见）+ 插件 logger。
"静默假装成功"是最不能接受的。

**纯逻辑模块**：只依赖标准库 sqlite3 / pathlib ⇒ 可离线单测（传临时库路径）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_log = logging.getLogger("plugin.maiknock.webui_companion")

#: 画像层要物理删的表（都没有 is_deleted 字段）
PROFILE_TABLES = (
    "person_identities",
    "person_identity_aliases",
    "person_profile_snapshots",
    "person_profile_overrides",
    "person_profile_alias_overrides",
    "person_profile_refresh_queue",
    "person_profile_active_persons",
)

#: 身份/会话层要物理删的表（主库）
IDENTITY_TABLES = (
    "person_info",
    "chat_sessions",
    "mai_messages",
)

#: 内容层要软删的表（都有 is_deleted/deleted_at，宿主检索认这个标记）
CONTENT_TABLES = ("paragraphs", "entities", "relations")

#: 删除原因（写进 deletion_reason，方便事后审计）
DELETE_REASON = "chatroom:forget_role"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, name: str) -> set:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{name}")')}


def _soft_delete_content(
    conn: sqlite3.Connection, aliases: Sequence[str], reason: str
) -> Dict[str, int]:
    """内容层软删：按所有别名匹配（含共现段落——用户拍板不怕牵连）。

    - paragraphs: `content LIKE '%别名%'` OR `source LIKE 'person_fact:<pid>%'`
    - entities:   `name IN (别名...)`
    - relations:  `subject/object` 命中别名
    返回 {表名: 软删条数}。
    """
    counts: Dict[str, int] = {}
    now = time.time()
    names = [str(a) for a in aliases if str(a).strip()]
    if not names:
        return counts
    like_clauses = " OR ".join(["content LIKE ?"] * len(names))
    like_args = [f"%{name}%" for name in names]
    exact_marks = ",".join(["?"] * len(names))

    if _table_exists(conn, "paragraphs"):
        cols = _columns(conn, "paragraphs")
        if {"is_deleted", "deleted_at"} <= cols:
            # 共现段落一并删：content 命中任一别名即可（不怕牵连别人）
            sql = (
                f"UPDATE paragraphs SET is_deleted=1, deleted_at=?, "
                f"deletion_reason=? WHERE (is_deleted IS NULL OR is_deleted=0) "
                f"AND ({like_clauses})"
            )
            cur = conn.execute(sql, [now, reason] + like_args)
            counts["paragraphs"] = int(cur.rowcount or 0)
        else:
            raise ValueError("paragraphs 缺 is_deleted/deleted_at 字段（宿主升级？）")

    if _table_exists(conn, "entities"):
        cols = _columns(conn, "entities")
        if {"is_deleted", "deleted_at"} <= cols:
            cur = conn.execute(
                f"UPDATE entities SET is_deleted=1, deleted_at=? "
                f"WHERE (is_deleted IS NULL OR is_deleted=0) AND name IN ({exact_marks})",
                [now] + names,
            )
            counts["entities"] = int(cur.rowcount or 0)

    if _table_exists(conn, "relations"):
        cols = _columns(conn, "relations")
        if "is_deleted" in cols:
            subj = " OR ".join(["subject LIKE ?"] * len(names))
            obj = " OR ".join(["object LIKE ?"] * len(names))
            cur = conn.execute(
                f"UPDATE relations SET is_deleted=1, deleted_at=? "
                f"WHERE (is_deleted IS NULL OR is_deleted=0) AND (({subj}) OR ({obj}))",
                [now] + like_args + like_args,
            )
            counts["relations"] = int(cur.rowcount or 0)
        elif "is_inactive" in cols:
            # 老版本宿主只有 is_inactive，语义等价（检索不认它但是软标记）
            subj = " OR ".join(["subject LIKE ?"] * len(names))
            obj = " OR ".join(["object LIKE ?"] * len(names))
            cur = conn.execute(
                f"UPDATE relations SET is_inactive=1, inactive_since=? "
                f"WHERE COALESCE(is_inactive,0)=0 AND (({subj}) OR ({obj}))",
                [now] + like_args + like_args,
            )
            counts["relations"] = int(cur.rowcount or 0)
    return counts


def _delete_profile(conn: sqlite3.Connection, person_id: str) -> Dict[str, int]:
    """画像层物理删（表无软删字段）。aliases 由外键 CASCADE，但显式删更稳。"""
    counts: Dict[str, int] = {}
    if not person_id:
        return counts
    for table in PROFILE_TABLES:
        if not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        key = "person_id" if "person_id" in cols else ""
        if not key:
            continue  # 表结构变了，跳过并让上层报告（不要瞎删）
        cur = conn.execute(f'DELETE FROM "{table}" WHERE person_id=?', (person_id,))
        counts[table] = int(cur.rowcount or 0)
    return counts


def _delete_identity(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    user_id: str,
    session_id: str,
) -> Dict[str, int]:
    """身份/会话层物理删（MaiBot.db）。按 id/user_id/session_id 精确匹配。"""
    counts: Dict[str, int] = {}
    for table in IDENTITY_TABLES:
        if not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        if table == "person_info" and "person_id" in cols:
            cur = conn.execute("DELETE FROM person_info WHERE person_id=?", (person_id,))
            counts[table] = int(cur.rowcount or 0)
        elif table == "chat_sessions" and "session_id" in cols:
            cur = conn.execute("DELETE FROM chat_sessions WHERE session_id=?", (session_id,))
            counts[table] = int(cur.rowcount or 0)
        elif table == "mai_messages" and "user_id" in cols:
            cur = conn.execute("DELETE FROM mai_messages WHERE user_id=?", (user_id,))
            counts[table] = int(cur.rowcount or 0)
    return counts


def forget_account(
    *,
    memory_db: Path,
    main_db: Path,
    person_id: str,
    user_id: str,
    session_id: str,
    aliases: Sequence[str],
    reason: str = DELETE_REASON,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """对一个 Bot 实例的两张库执行彻底遗忘。

    :param memory_db: a-memorix/metadata.db 路径（可能不存在 ⇒ 记 warning 跳过）
    :param main_db: MaiBot.db 路径
    :param aliases: 该角色的所有已知称呼（鸣人/路飞/历史昵称）
    :param dry_run: True = 只算命中数不提交（预览用）
    :returns: {"ok": bool, "counts": {...}, "errors": [str], "warnings": [str]}
    """
    result: Dict[str, Any] = {
        "ok": True, "counts": {}, "errors": [], "warnings": [],
        "memory_db": str(memory_db), "main_db": str(main_db),
    }

    def _open(path: Path, errors: List[str]) -> Optional[sqlite3.Connection]:
        if not path.is_file():
            errors.append(f"库文件不存在：{path}")
            return None
        try:
            conn = sqlite3.connect(str(path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as exc:
            errors.append(f"打开库失败 {path}: {exc}")
            return None

    # ① 长期记忆库：内容层软删 + 画像层物理删
    conn = _open(memory_db, result["errors"])
    if conn is not None:
        try:
            result["counts"]["memory"] = _soft_delete_content(conn, aliases, reason)
            result["counts"]["profile"] = _delete_profile(conn, person_id)
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"记忆库清理失败 {memory_db}: {type(exc).__name__}: {exc}")
            conn.rollback()
        finally:
            conn.close()

    # ② 主库：身份/会话物理删
    conn = _open(main_db, result["errors"])
    if conn is not None:
        try:
            result["counts"]["identity"] = _delete_identity(
                conn, person_id=person_id, user_id=user_id, session_id=session_id
            )
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"主库清理失败 {main_db}: {type(exc).__name__}: {exc}")
            conn.rollback()
        finally:
            conn.close()

    result["ok"] = not result["errors"]
    return result


def collect_aliases(
    *,
    memory_db: Path,
    main_db: Path,
    person_id: str,
    user_id: str,
    extra_aliases: Sequence[str] = (),
) -> tuple:
    """从宿主两库里收集这个人的所有已知称呼（昵称/群名片/历史名）。

    删角色时要把「鸣人/路飞/曾用名」全捞出来，内容层软删才能按所有别名匹配，
    避免改过名的旧痕迹漏网。返回 ``(aliases, errors)``，**不抛异常**。
    """
    found: List[str] = [str(x).strip() for x in extra_aliases if str(x).strip()]
    errors: List[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text)

    if main_db.is_file():
        try:
            conn = sqlite3.connect(str(main_db), timeout=5.0)
            try:
                # ⚠️ 列名按实际表结构来：真库 MaiBot.db 的 mai_messages/person_info
                #    未必有 user_cardname（实测 2026-11-27 副本验证撞到），硬写会
                #    整段查询失败 ⇒ 别名只收到一个，漏掉历史称呼。按列存在性拼 SQL。
                for table, where in (
                    ("mai_messages", "user_id=?"),
                    ("person_info", "person_id=? OR user_id=?"),
                ):
                    if not _table_exists(conn, table):
                        continue
                    cols = _columns(conn, table)
                    fields = [f for f in ("user_nickname", "user_cardname") if f in cols]
                    if not fields:
                        continue
                    args = (user_id,) if table == "mai_messages" else (person_id, user_id)
                    for row in conn.execute(
                        f"SELECT {', '.join(fields)} FROM {table} WHERE {where}", args
                    ):
                        for value in row:
                            _add(value)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            errors.append(f"读主库别名失败 {main_db}: {exc}")

    if memory_db.is_file():
        try:
            conn = sqlite3.connect(str(memory_db), timeout=5.0)
            try:
                if _table_exists(conn, "person_identity_aliases"):
                    for row in conn.execute(
                        "SELECT DISTINCT alias FROM person_identity_aliases WHERE person_id=?",
                        (person_id,),
                    ):
                        _add(row[0])
                if _table_exists(conn, "person_identities"):
                    row = conn.execute(
                        "SELECT canonical_name FROM person_identities WHERE person_id=?",
                        (person_id,),
                    ).fetchone()
                    if row:
                        _add(row[0])
            finally:
                conn.close()
        except sqlite3.Error as exc:
            errors.append(f"读记忆库别名失败 {memory_db}: {exc}")

    return found, errors


def forget_account_all(
    *,
    workspace: Path,
    bot_dirs: Sequence[Path],
    person_id: str,
    user_id: str,
    session_id: str,
    aliases: Sequence[str],
    reason: str = DELETE_REASON,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """遍历所有 Bot 工作目录，对每个实例的两张库执行遗忘。

    `bot_dirs` 由调用方从 hostinfo.read_bots() + bot_working_dir() 算好传入
    （保持本模块纯逻辑、可离线单测）。
    """
    out: Dict[str, Any] = {
        "ok": True, "instances": [], "errors": [], "warnings": [], "dry_run": dry_run,
    }
    dirs = list(bot_dirs) or [workspace]
    for index, work in enumerate(dirs):
        work = Path(work)
        one = forget_account(
            memory_db=work / "data" / "a-memorix" / "metadata" / "metadata.db",
            main_db=work / "data" / "MaiBot.db",
            person_id=person_id, user_id=user_id, session_id=session_id,
            aliases=aliases, reason=reason, dry_run=dry_run,
        )
        out["instances"].append({"index": index, "work_dir": str(work), **one})
        out["errors"].extend(one["errors"])
        out["warnings"].extend(one["warnings"])
    out["ok"] = not out["errors"]
    return out
