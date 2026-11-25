# -*- coding: utf-8 -*-
"""麦麦聊天室 · 共享账本（SQLite 读写层）。

**这是什么**：跨 Bot 交换消息的**唯一**载体。
宿主插件的所有 `chat.*` / `message.*` 能力都是 **current-bot scoped**，
插件之间也没有直接调用通道 ⇒ 想让「一号的中控」和「二号的代理实例」说话，
只能靠一份**大家都读写得到的文件**。

**放哪**：所有 Bot 的 `data/` 之外（铁律：不动 Bot 的数据目录），
由插件配置 `[chatroom].ledger_path` 指定，**所有参与的 Bot 必须配成同一个**。

**并发**（多个 Bot 的 Runner 进程同时写一个 SQLite 文件 ⇒ 必须）：
- ``journal_mode = WAL`` —— 写不阻塞读
- ``busy_timeout = 5000`` —— 撞锁等 5 秒，而不是立刻报错
- 事务尽量短 —— 一条消息一个事务，**不要把网络 IO 包在事务里**
- ``database is locked`` ⇒ 重试 3 次，退避 50 / 100 / 200 ms
- **DDL 只在启动时做一次**（``CREATE TABLE IF NOT EXISTS``），别在热路径上跑

**账本不是唯一真相**：Bot 库里的原始消息是另一份。
两者不一致时（账本写失败、进程被 kill）**以 Bot 库为准**，账本可以重放重建
（`delivery.baseline_ts` 给了重放的起点）。

**纯逻辑模块**：无宿主导入 ⇒ 可离线单测。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: 投递任务的状态机
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_COLLECTED = "collected"
DELIVERY_TIMEOUT = "timeout"
DELIVERY_CANCELLED = "cancelled"

#: 名单用途（上游的两张表）
LIST_MY_ROLES = "my_roles"
LIST_PEERS = "peers"

#: 撞锁重试：次数 + 退避（毫秒）
_RETRY_TIMES = 3
_RETRY_BACKOFF_MS = (50, 100, 200)

#: 业务错误（与「数据库错误」区分开，调用方据此决定要不要回滚业务动作）
class LedgerError(RuntimeError):
    """账本层的业务异常（表结构不匹配、数据不存在、状态冲突……）。"""


# ---------------------------------------------------------------------------
# 表结构
#
# ⚠️ 与 08 文档 §2.2 的一处**有意偏差**：那里把 `room_id` 放在 `member` 表上，
#    但「我的角色」是全局一份、且**同一个角色可以同时在多个房间里**
#    （壳阶段就是 `room.member_ids` 的结构）⇒ 一列 `room_id` 表达不了。
#    改成：`member` 是**全局登记表**，房间归属另用 `room_member` 关联表。
# ---------------------------------------------------------------------------

SCHEMA: Tuple[str, ...] = (
    # ── 房间 ────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS room (
        room_id     TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        mode        TEXT NOT NULL DEFAULT 'stage',   -- stage | private
        group_id    TEXT NOT NULL DEFAULT '',        -- 虚拟群 id（正式版用）
        created_at  TEXT NOT NULL DEFAULT '',
        config_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # ── 群员（全局登记表）────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS member (
        member_id      TEXT PRIMARY KEY,
        kind           TEXT NOT NULL,                -- user_role | bot | narrator
        display_name   TEXT NOT NULL,
        avatar         TEXT NOT NULL DEFAULT '',
        persona_label  TEXT NOT NULL DEFAULT '',
        sort_order     INTEGER NOT NULL DEFAULT 0,
        bot_id         TEXT NOT NULL DEFAULT '',     -- 引用，不复制人格配置
        platform       TEXT NOT NULL DEFAULT '',     -- 固定的「平台+账号」身份
        account        TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT '',     -- 接入状态（四态）
        last_active_at REAL NOT NULL DEFAULT 0,      -- 私聊排序用
        created_at     TEXT NOT NULL DEFAULT ''
    )
    """,
    # ── 上游名单（我的角色 / 私聊对象）──────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS member_list (
        list_id    TEXT NOT NULL,                    -- my_roles | peers
        member_id  TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (list_id, member_id)
    )
    """,
    # ── 房间群员（关联表）────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS room_member (
        room_id    TEXT NOT NULL,
        member_id  TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (room_id, member_id)
    )
    """,
    # ── 段（时间线的分段单位）────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS segment (
        segment_id TEXT PRIMARY KEY,
        room_id    TEXT NOT NULL,
        seq        INTEGER NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'normal',   -- normal | plot
        created_at TEXT NOT NULL DEFAULT '',
        voided     INTEGER NOT NULL DEFAULT 0        -- 软作废，不物理删
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_segment_room_seq ON segment(room_id, seq)",
    # ── 消息行 ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS message (
        message_id        TEXT PRIMARY KEY,
        segment_id        TEXT NOT NULL,
        room_id           TEXT NOT NULL,
        speaker_member_id TEXT NOT NULL DEFAULT '',
        speaker_name      TEXT NOT NULL DEFAULT '',
        kind              TEXT NOT NULL DEFAULT 'line',  -- line|reply|narrator|sticker
        content           TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT '',
        voided            INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_message_segment ON message(segment_id)",
    "CREATE INDEX IF NOT EXISTS idx_message_room ON message(room_id)",
    # ── 投递任务（跨 Bot 的工作单）───────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS delivery (
        delivery_id      TEXT PRIMARY KEY,
        segment_id       TEXT NOT NULL DEFAULT '',
        room_id          TEXT NOT NULL DEFAULT '',
        target_bot_id    TEXT NOT NULL,
        sender_member_id TEXT NOT NULL DEFAULT '',
        content          TEXT NOT NULL DEFAULT '',
        state            TEXT NOT NULL DEFAULT 'pending',
        created_at       TEXT NOT NULL DEFAULT '',
        delivered_at     TEXT NOT NULL DEFAULT '',
        collected_at     TEXT NOT NULL DEFAULT '',
        baseline_ts      REAL NOT NULL DEFAULT 0,
        group_id         TEXT NOT NULL DEFAULT '',
        group_name       TEXT NOT NULL DEFAULT '',
        sender_platform  TEXT NOT NULL DEFAULT '',
        sender_account   TEXT NOT NULL DEFAULT '',
        sender_nickname  TEXT NOT NULL DEFAULT '',
        attempts         INTEGER NOT NULL DEFAULT 0,
        seq              INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_delivery_target ON delivery(target_bot_id, state)",
    # ── 各 Bot 自报的接入状态（接入校验的握手层 / 风格层）──────────────
    # ⚠️ **每个实例只写自己那一行** —— 别的 Bot 的 api_key 不在本进程里，
    #    也就没法替别人测握手；中控把各行合起来算四态。
    # ── 错误报告（429 限流等硬错误，**不许默默出错**）──────────────────
    # ⚠️ 每个 Bot 的插件实例只扫**自己实例**的 llm_error 快照目录，
    #    扫到就往这张表写一行；中控从账本汇总推给前端 ⇒
    #    二号出的问题，一号的中控也看得见。
    """
    CREATE TABLE IF NOT EXISTS error_report (
        report_id    TEXT PRIMARY KEY,      -- 快照文件名（天然唯一，可去重）
        bot_id       TEXT NOT NULL DEFAULT '',
        bot_name     TEXT NOT NULL DEFAULT '',
        kind         TEXT NOT NULL DEFAULT '',   -- rate_limit | upstream_error | empty_reply
        status_code  INTEGER NOT NULL DEFAULT 0,
        provider     TEXT NOT NULL DEFAULT '',
        model        TEXT NOT NULL DEFAULT '',
        task         TEXT NOT NULL DEFAULT '',
        room_id      TEXT NOT NULL DEFAULT '',
        session_id   TEXT NOT NULL DEFAULT '',
        message      TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT '',
        acked        INTEGER NOT NULL DEFAULT 0   -- 前端读过一次就置 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_error_report_ack ON error_report(acked, created_at)",
    """
    CREATE TABLE IF NOT EXISTS bot_status (
        bot_id       TEXT PRIMARY KEY,
        name         TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT '',
        detail       TEXT NOT NULL DEFAULT '',
        handshake_ok INTEGER,                 -- 1/0/NULL（NULL = 还没上报）
        style_fixed  INTEGER,                 -- 1/0/NULL
        updated_at   TEXT NOT NULL DEFAULT ''
    )
    """,
)

#: 需要在**已存在的表**上补的列（`CREATE TABLE IF NOT EXISTS` 不会加列）。
#: ⚠️ 表结构会随开发演进 ⇒ 必须有迁移，否则老账本会一直缺列。
#:    格式：{表名: {列名: "列定义"}}
MIGRATIONS: Dict[str, Dict[str, str]] = {
    "delivery": {
        "group_id": "TEXT NOT NULL DEFAULT ''",
        "group_name": "TEXT NOT NULL DEFAULT ''",
        "sender_platform": "TEXT NOT NULL DEFAULT ''",
        "sender_account": "TEXT NOT NULL DEFAULT ''",
        "sender_nickname": "TEXT NOT NULL DEFAULT ''",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "seq": "INTEGER NOT NULL DEFAULT 0",
    },
}


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> List[str]:
    """给已存在的表补列（返回实际补了哪些列）。

    ⚠️ SQLite 的 ``ALTER TABLE ... ADD COLUMN`` 不支持带默认值的 NOT NULL 之外的复杂约束，
    所以这里的列定义一律用 ``TEXT NOT NULL DEFAULT ''`` 这种最稳的形状。
    """
    try:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return []
    if not existing:
        return []          # 表还没建（首次打开）⇒ 不用迁移
    added: List[str] = []
    for column, definition in columns.items():
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            added.append(column)
        except sqlite3.Error:
            continue
    return added


def _now() -> str:
    """本地时刻字符串（排序与显示用；不是 epoch）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


class ChatroomLedger:
    """共享账本。

    **用法**::

        with ChatroomLedger(path, bot_id="...") as ledger:
            ledger.create_room("room_1", "拉面店门口")
            for d in ledger.pending_deliveries(my_bot_id):
                ledger.claim_delivery(d["delivery_id"], my_bot_id)

    **线程安全**：SQLite 连接默认不能跨线程用，这里开 ``check_same_thread=False``
    并用一把 ``RLock`` 串住所有写操作（插件跑在 asyncio 单线程里，
    但 ``asyncio.to_thread`` 会另起线程）。
    """

    def __init__(self, path: str, bot_id: str = "") -> None:
        self._path = str(path or "")
        self._bot_id = str(bot_id or "")
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    # ── 身份 ────────────────────────────────────────────────────────────

    @property
    def path(self) -> str:
        return self._path

    @property
    def bot_id(self) -> str:
        """本实例代表的 Bot id（由调用方传入；账本自己不猜）。"""
        return self._bot_id

    # ── 连接 ────────────────────────────────────────────────────────────

    def open(self) -> None:
        """打开连接 + 建表。

        ⚠️ **DDL 只在这里跑一次** —— 热路径上建表会拖慢每一次读写，
        也会在多进程同时建表时制造不必要的锁竞争。
        """
        if not self._path:
            raise LedgerError("账本路径为空：请在插件配置里填 [chatroom].ledger_path")
        parent = os.path.dirname(os.path.abspath(self._path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(
            self._path,
            timeout=5.0,
            isolation_level=None,      # 自己管事务（autocommit 模式）
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        self._apply_pragmas(conn)
        for ddl in SCHEMA:
            conn.execute(ddl)
        # ⚠️ 迁移：**已存在**的表要补列（CREATE TABLE IF NOT EXISTS 不会加列）
        for table, columns in MIGRATIONS.items():
            added = _ensure_columns(conn, table, columns)
            if added:
                self._log_migration(table, added)
        self._conn = conn

    @staticmethod
    def _log_migration(table: str, added: List[str]) -> None:
        """迁移日志（补了列要说一声，别静默）。"""
        import logging

        logging.getLogger("plugin.maiknock.webui_companion").info(
            "[聊天室] 账本迁移：表 %s 补了列 %s", table, "、".join(added)
        )

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        """并发相关的 PRAGMA。**多进程同时设 WAL 可能报 locked ⇒ 忽略它**。"""
        for stmt in (
            "PRAGMA journal_mode=WAL",     # 写不阻塞读
            "PRAGMA busy_timeout=5000",    # 撞锁等 5 秒而不是立刻报错
            "PRAGMA synchronous=NORMAL",   # WAL 下的常规取舍：崩溃不丢、掉电可能丢最后几笔
            "PRAGMA foreign_keys=ON",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                # WAL 模式切换是全局的，多进程同时切会有一方拿到 locked —— 无所谓，
                # 已经有进程切好了。不许因此把整个账本判成不可用。
                pass

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def __enter__(self) -> "ChatroomLedger":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise LedgerError("账本未打开：先调用 open()")
        return self._conn

    # ── 事务 ────────────────────────────────────────────────────────────

    @contextmanager
    def write(self):
        """写事务（BEGIN IMMEDIATE）。撞锁自动重试。

        ⚠️ **事务要短**：调用方不要在 `with ledger.write():` 里做网络 IO。
        """
        with self._lock:
            conn = self._require_conn()
            last_exc: Optional[Exception] = None
            for attempt in range(_RETRY_TIMES):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    last_exc = exc
                    if not _is_locked(exc) or attempt == _RETRY_TIMES - 1:
                        raise
                    time.sleep(_RETRY_BACKOFF_MS[attempt] / 1000.0)
            else:  # pragma: no cover - 上面 break/raise 覆盖
                raise LedgerError(f"写事务开启失败：{last_exc}")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                conn.execute("COMMIT")

    @contextmanager
    def read(self):
        """只读访问。"""
        with self._lock:
            yield self._require_conn()

    # ── 房间 ────────────────────────────────────────────────────────────

    def list_rooms(self) -> List[Dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM room ORDER BY created_at, room_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_room(self, room_id: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM room WHERE room_id = ?", (room_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_room(
        self,
        room_id: str,
        name: str,
        mode: str = "stage",
        group_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO room(room_id, name, mode, group_id, created_at, config_json)"
                " VALUES (?,?,?,?,?,?)",
                (room_id, name, mode, group_id, _now(), json.dumps(config or {}, ensure_ascii=False)),
            )
        room = self.get_room(room_id)
        assert room is not None
        return room

    def rename_room(self, room_id: str, name: str) -> Dict[str, Any]:
        with self.write() as conn:
            cur = conn.execute("UPDATE room SET name = ? WHERE room_id = ?", (name, room_id))
            if cur.rowcount == 0:
                raise LedgerError(f"房间不存在：{room_id}")
        room = self.get_room(room_id)
        assert room is not None
        return room

    def delete_room(self, room_id: str) -> None:
        """删房间：连同群员关联、段、消息、投递任务一起清。

        ⚠️ 与「回到此处」的**软作废**不同：这里是真的删房间（06 文档 3.1 的「✕」）。
        """
        with self.write() as conn:
            conn.execute("DELETE FROM room_member WHERE room_id = ?", (room_id,))
            conn.execute("DELETE FROM message WHERE room_id = ?", (room_id,))
            conn.execute("DELETE FROM segment WHERE room_id = ?", (room_id,))
            conn.execute("DELETE FROM delivery WHERE room_id = ?", (room_id,))
            conn.execute("DELETE FROM room WHERE room_id = ?", (room_id,))

    # ── 群员（全局登记表）────────────────────────────────────────────────

    def upsert_member(self, member: Dict[str, Any]) -> Dict[str, Any]:
        """写入/更新一个群员（按 member_id）。"""
        fields = (
            "member_id", "kind", "display_name", "avatar", "persona_label",
            "sort_order", "bot_id", "platform", "account", "status",
            "last_active_at", "created_at",
        )
        values = [
            str(member.get("member_id") or ""),
            str(member.get("kind") or "user_role"),
            str(member.get("display_name") or ""),
            str(member.get("avatar") or ""),
            str(member.get("persona_label") or ""),
            int(member.get("sort_order") or 0),
            str(member.get("bot_id") or ""),
            str(member.get("platform") or ""),
            str(member.get("account") or ""),
            str(member.get("status") or ""),
            float(member.get("last_active_at") or 0),
            str(member.get("created_at") or _now()),
        ]
        placeholders = ",".join("?" for _ in fields)
        updates = ",".join(f"{f}=excluded.{f}" for f in fields if f != "member_id")
        with self.write() as conn:
            conn.execute(
                f"INSERT INTO member({','.join(fields)}) VALUES ({placeholders})"
                f" ON CONFLICT(member_id) DO UPDATE SET {updates}",
                values,
            )
        return self.get_member(str(member.get("member_id") or ""))

    def get_member(self, member_id: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM member WHERE member_id = ?", (member_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_member(self, member_id: str) -> None:
        """删群员：从登记表、上游名单、房间关联里一并摘掉。"""
        with self.write() as conn:
            conn.execute("DELETE FROM member_list WHERE member_id = ?", (member_id,))
            conn.execute("DELETE FROM room_member WHERE member_id = ?", (member_id,))
            conn.execute("DELETE FROM member WHERE member_id = ?", (member_id,))

    def list_members(self, kind: str = "") -> List[Dict[str, Any]]:
        with self.read() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM member WHERE kind = ? ORDER BY sort_order, member_id",
                    (kind,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM member ORDER BY sort_order, member_id"
                ).fetchall()
        return [dict(row) for row in rows]

    def touch_member_activity(self, member_id: str, when: Optional[float] = None) -> None:
        """记「最近一次来往」的时刻（私聊排序用）。"""
        ts = float(when if when is not None else time.time())
        with self.write() as conn:
            conn.execute(
                "UPDATE member SET last_active_at = ? WHERE member_id = ?", (ts, member_id)
            )

    # ── 上游名单（我的角色 / 私聊对象）──────────────────────────────────

    def list_members_of(self, list_id: str) -> List[Dict[str, Any]]:
        """按名单顺序返回成员（join 登记表）。"""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT m.* FROM member_list l JOIN member m ON m.member_id = l.member_id"
                " WHERE l.list_id = ? ORDER BY l.sort_order, l.member_id",
                (list_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_to_list(self, list_id: str, member_id: str, sort_order: int = 0) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO member_list(list_id, member_id, sort_order) VALUES (?,?,?)"
                " ON CONFLICT(list_id, member_id) DO UPDATE SET sort_order = excluded.sort_order",
                (list_id, member_id, sort_order),
            )

    def remove_from_list(self, list_id: str, member_id: str) -> None:
        with self.write() as conn:
            conn.execute(
                "DELETE FROM member_list WHERE list_id = ? AND member_id = ?",
                (list_id, member_id),
            )

    def reorder_list(self, list_id: str, member_ids: Sequence[str]) -> None:
        """整张名单重排。没提到的排到后面，保持相对顺序。"""
        with self.write() as conn:
            current = [
                str(row[0])
                for row in conn.execute(
                    "SELECT member_id FROM member_list WHERE list_id = ? ORDER BY sort_order, member_id",
                    (list_id,),
                ).fetchall()
            ]
            given = [mid for mid in member_ids if mid in current]
            rest = [mid for mid in current if mid not in set(given)]
            for index, member_id in enumerate(given + rest):
                conn.execute(
                    "UPDATE member_list SET sort_order = ? WHERE list_id = ? AND member_id = ?",
                    (index, list_id, member_id),
                )

    # ── 房间群员 ────────────────────────────────────────────────────────

    def list_room_members(self, room_id: str) -> List[Dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT m.* FROM room_member r JOIN member m ON m.member_id = r.member_id"
                " WHERE r.room_id = ? ORDER BY r.sort_order, m.member_id",
                (room_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_to_room(self, room_id: str, member_id: str, sort_order: int = 0) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO room_member(room_id, member_id, sort_order) VALUES (?,?,?)"
                " ON CONFLICT(room_id, member_id) DO UPDATE SET sort_order = excluded.sort_order",
                (room_id, member_id, sort_order),
            )

    def remove_from_room(self, room_id: str, member_id: str) -> None:
        with self.write() as conn:
            conn.execute(
                "DELETE FROM room_member WHERE room_id = ? AND member_id = ?",
                (room_id, member_id),
            )

    def replace_in_room(self, room_id: str, old_member_id: str, new_member_id: str) -> None:
        """用新群员顶替旧的（**位置保持在原处**，不顶到末尾）。"""
        with self.write() as conn:
            row = conn.execute(
                "SELECT sort_order FROM room_member WHERE room_id = ? AND member_id = ?",
                (room_id, old_member_id),
            ).fetchone()
            if row is None:
                raise LedgerError(f"要替换的群员不在这个房间里：{old_member_id}")
            position = int(row[0])
            conn.execute(
                "DELETE FROM room_member WHERE room_id = ? AND member_id = ?",
                (room_id, old_member_id),
            )
            conn.execute(
                "DELETE FROM room_member WHERE room_id = ? AND member_id = ?",
                (room_id, new_member_id),
            )
            conn.execute(
                "INSERT INTO room_member(room_id, member_id, sort_order) VALUES (?,?,?)",
                (room_id, new_member_id, position),
            )

    # ── 段与消息 ────────────────────────────────────────────────────────

    def next_segment_seq(self, room_id: str) -> int:
        """下一个段号（**续编**，旧段不重排）。"""
        with self.read() as conn:
            row = conn.execute(
                "SELECT MAX(seq) FROM segment WHERE room_id = ?", (room_id,)
            ).fetchone()
        return int(row[0] or 0) + 1

    def append_segment(
        self,
        room_id: str,
        segment_id: str,
        kind: str = "normal",
        messages: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """追加一段（连同它的消息行）。段号自动续编。"""
        seq = self.next_segment_seq(room_id)
        created = _now()
        rows = list(messages or [])
        with self.write() as conn:
            conn.execute(
                "INSERT INTO segment(segment_id, room_id, seq, kind, created_at, voided)"
                " VALUES (?,?,?,?,?,0)",
                (segment_id, room_id, seq, kind, created),
            )
            for msg in rows:
                conn.execute(
                    "INSERT INTO message(message_id, segment_id, room_id, speaker_member_id,"
                    " speaker_name, kind, content, created_at, voided)"
                    " VALUES (?,?,?,?,?,?,?,?,0)",
                    (
                        str(msg.get("message_id") or ""),
                        segment_id,
                        room_id,
                        str(msg.get("speaker_member_id") or ""),
                        str(msg.get("speaker_name") or ""),
                        str(msg.get("kind") or "line"),
                        str(msg.get("content") or ""),
                        str(msg.get("created_at") or created),
                    ),
                )
        segment = self.get_segment(segment_id)
        assert segment is not None
        return segment

    def get_segment(self, segment_id: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM segment WHERE segment_id = ?", (segment_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_segments(self, room_id: str, include_voided: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM segment WHERE room_id = ?"
        if not include_voided:
            sql += " AND voided = 0"
        sql += " ORDER BY seq"
        with self.read() as conn:
            rows = conn.execute(sql, (room_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_messages(
        self, room_id: str, segment_id: str = "", include_voided: bool = False
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM message WHERE room_id = ?"
        params: List[Any] = [room_id]
        if segment_id:
            sql += " AND segment_id = ?"
            params.append(segment_id)
        if not include_voided:
            sql += " AND voided = 0"
        sql += " ORDER BY rowid"
        with self.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def truncate_from(self, room_id: str, cutoff_seq: int) -> int:
        """「回到此处」L1：**软作废**段序号 ≥ cutoff_seq 的所有段（连同消息行）。

        ⚠️ **不物理删**（02 文档 FR-12）：作废条件 = 段序号 ≥ 截断点，一条查询完成。
        返回被作废的段数。
        """
        with self.write() as conn:
            cur = conn.execute(
                "UPDATE segment SET voided = 1 WHERE room_id = ? AND seq >= ?",
                (room_id, cutoff_seq),
            )
            count = int(cur.rowcount or 0)
            conn.execute(
                "UPDATE message SET voided = 1 WHERE room_id = ?"
                " AND segment_id IN (SELECT segment_id FROM segment WHERE room_id = ? AND seq >= ?)",
                (room_id, room_id, cutoff_seq),
            )
        return count

    # ── 投递任务 ────────────────────────────────────────────────────────

    def enqueue_delivery(
        self,
        delivery_id: str,
        target_bot_id: str,
        content: str,
        room_id: str = "",
        segment_id: str = "",
        sender_member_id: str = "",
        baseline_ts: Optional[float] = None,
        group_id: str = "",
        group_name: str = "",
        sender_platform: str = "",
        sender_account: str = "",
        sender_nickname: str = "",
        seq: int = 0,
    ) -> Dict[str, Any]:
        """登记一条投递任务（跨 Bot 的工作单）。

        ⚠️ **投递身份写进任务里**（平台 / 账号 / 昵称 / 群）——
        领单的那个 Bot 进程**没有**中控的角色登记表，
        身份随任务走才不会认错人（02 文档「平台身份固定」铁则）。
        ⚠️ ``seq`` 是**中控分配的单调序号**——``created_at`` 只有秒级精度，
        同一秒建的多条任务排序不确定 ⇒ 台词会乱序递。
        """
        created = _now()
        columns = (
            "delivery_id", "segment_id", "room_id", "target_bot_id",
            "sender_member_id", "content", "state", "created_at",
            "delivered_at", "collected_at", "baseline_ts",
            "group_id", "group_name", "sender_platform", "sender_account",
            "sender_nickname", "attempts", "seq",
        )
        values = (
            delivery_id, segment_id, room_id, target_bot_id,
            sender_member_id, content, DELIVERY_PENDING, created,
            "", "",
            float(baseline_ts if baseline_ts is not None else time.time()),
            group_id, group_name, sender_platform, sender_account,
            sender_nickname, 0, int(seq),
        )
        placeholders = ",".join("?" for _ in columns)
        with self.write() as conn:
            conn.execute(
                f"INSERT INTO delivery({','.join(columns)}) VALUES ({placeholders})",
                values,
            )
        return self.get_delivery(delivery_id)

    def next_delivery_seq(self) -> int:
        """下一个投递序号（**全局单调递增**）。

        ⚠️ 为什么需要它：``created_at`` 只有**秒级**精度 ⇒ 同一秒建的多条任务
        排序是不确定的 ⇒ 台词会乱序递。序号由中控分配，保证顺序。
        """
        with self.read() as conn:
            row = conn.execute("SELECT MAX(seq) FROM delivery").fetchone()
        return int(row[0] or 0) + 1

    def get_delivery(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM delivery WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return dict(row) if row else None

    def pending_deliveries(self, target_bot_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """取属于某个 Bot 的待办任务（按创建顺序）。"""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM delivery WHERE target_bot_id = ? AND state = ?"
                " ORDER BY seq, created_at, delivery_id LIMIT ?",
                (target_bot_id, DELIVERY_PENDING, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_delivery(self, delivery_id: str, target_bot_id: str) -> bool:
        """**原子**地把任务从 pending 领走（防两个实例抢同一条）。

        返回 True = 领到了；False = 已被别人领走或状态已变。
        """
        with self.write() as conn:
            cur = conn.execute(
                "UPDATE delivery SET state = ?, delivered_at = ?, attempts = attempts + 1"
                " WHERE delivery_id = ? AND target_bot_id = ? AND state = ?",
                (DELIVERY_DELIVERED, _now(), delivery_id, target_bot_id, DELIVERY_PENDING),
            )
            return bool(cur.rowcount)

    def mark_collected(self, delivery_id: str) -> None:
        with self.write() as conn:
            conn.execute(
                "UPDATE delivery SET state = ?, collected_at = ? WHERE delivery_id = ?",
                (DELIVERY_COLLECTED, _now(), delivery_id),
            )

    def mark_timeout(self, delivery_id: str) -> None:
        """超时：点名了却没开口。**只记状态，不产消息行**（FR-09：沉默不进时间线）。"""
        with self.write() as conn:
            conn.execute(
                "UPDATE delivery SET state = ? WHERE delivery_id = ? AND state IN (?, ?)",
                (DELIVERY_TIMEOUT, delivery_id, DELIVERY_PENDING, DELIVERY_DELIVERED),
            )

    def requeue_delivery(self, delivery_id: str, max_attempts: int = 3) -> bool:
        """递话失败 ⇒ **放回待办**（网关抖一下不该把这句话丢了）。

        超过 ``max_attempts`` 就不再重试 ⇒ 标超时（看得见"这句没递出去"）。
        返回 True = 已放回待办；False = 次数用尽，已标超时。
        """
        with self.write() as conn:
            row = conn.execute(
                "SELECT attempts FROM delivery WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if row is None:
                return False
            if int(row[0] or 0) >= max_attempts:
                conn.execute(
                    "UPDATE delivery SET state = ? WHERE delivery_id = ?",
                    (DELIVERY_TIMEOUT, delivery_id),
                )
                return False
            conn.execute(
                "UPDATE delivery SET state = ? WHERE delivery_id = ? AND state = ?",
                (DELIVERY_PENDING, delivery_id, DELIVERY_DELIVERED),
            )
            return True

    def cancel_deliveries(self, room_id: str) -> int:
        """「停止本轮」：把该房间还没递出去的任务标成取消。

        ⚠️ **已经递出去的那条收不回** —— 只能"不再发后面的"。
        """
        with self.write() as conn:
            cur = conn.execute(
                "UPDATE delivery SET state = ? WHERE room_id = ? AND state = ?",
                (DELIVERY_CANCELLED, room_id, DELIVERY_PENDING),
            )
            return int(cur.rowcount or 0)


    # ── 各 Bot 的接入状态（自报）─────────────────────────────────────

    def set_bot_status(
        self,
        bot_id: str,
        status: str,
        *,
        name: str = "",
        detail: str = "",
        handshake_ok: Optional[bool] = None,
        style_fixed: Optional[bool] = None,
    ) -> None:
        """登记**本实例**的接入状态。

        ⚠️ 只写自己那一行 —— 别的 Bot 的握手要在它们自己的进程里测
        （api_key 不该跨 Bot 流通）。
        """
        with self.write() as conn:
            conn.execute(
                "INSERT INTO bot_status(bot_id, name, status, detail, handshake_ok,"
                " style_fixed, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(bot_id) DO UPDATE SET name=excluded.name, status=excluded.status,"
                " detail=excluded.detail, handshake_ok=excluded.handshake_ok,"
                " style_fixed=excluded.style_fixed, updated_at=excluded.updated_at",
                (
                    str(bot_id), str(name), str(status), str(detail),
                    None if handshake_ok is None else int(bool(handshake_ok)),
                    None if style_fixed is None else int(bool(style_fixed)),
                    _now(),
                ),
            )

    # ── 错误报告（429 等硬错误）────────────────────────────────────

    def report_error(self, report: Dict[str, Any]) -> bool:
        """登记一条错误报告。**按 report_id 去重**（同一个快照不重复报）。

        返回 True = 新增；False = 之前报过（已存在）。
        """
        report_id = str(report.get("report_id") or "")
        if not report_id:
            return False
        with self.write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO error_report(report_id, bot_id, bot_name, kind,"
                " status_code, provider, model, task, room_id, session_id, message,"
                " created_at, acked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    report_id,
                    str(report.get("bot_id") or ""),
                    str(report.get("bot_name") or ""),
                    str(report.get("kind") or ""),
                    int(report.get("status_code") or 0),
                    str(report.get("provider") or ""),
                    str(report.get("model") or ""),
                    str(report.get("task") or ""),
                    str(report.get("room_id") or ""),
                    str(report.get("session_id") or ""),
                    str(report.get("message") or ""),
                    str(report.get("created_at") or ""),
                ),
            )
            return bool(cur.rowcount)

    def list_errors(self, limit: int = 20, only_unacked: bool = False) -> List[Dict[str, Any]]:
        """最近的错误报告（新的在前）。"""
        sql = "SELECT * FROM error_report"
        if only_unacked:
            sql += " WHERE acked = 0"
        sql += " ORDER BY created_at DESC, report_id DESC LIMIT ?"
        with self.read() as conn:
            rows = conn.execute(sql, (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def ack_errors(self, report_ids: Optional[List[str]] = None) -> int:
        """把错误标记为「用户已看到」。不给 id 就全标记。"""
        with self.write() as conn:
            if report_ids:
                marks = ",".join("?" for _ in report_ids)
                cur = conn.execute(
                    f"UPDATE error_report SET acked = 1 WHERE report_id IN ({marks})",
                    [str(item) for item in report_ids],
                )
            else:
                cur = conn.execute("UPDATE error_report SET acked = 1 WHERE acked = 0")
            return int(cur.rowcount or 0)

    # ── 聊天室状态快照（房间 / 群员 / 两个上游名单）────────────────────
    # ⚠️ 为什么用「整体快照」而不是逐表增量：这套状态很小（几个房间 + 十几个群员），
    #    整体写一次最简单，也**天然一致**（不会出现房间写了成员没写）。
    # ⚠️ 为什么要存：插件**每次重载都会丢内存态**（宿主改 .py 就重载）⇒
    #    用户加的 Bot、建的房间、重排的顺序全没了。

    def save_state(self, state: Dict[str, Any]) -> None:
        """把聊天室的房间/群员/名单整体写进账本（覆盖式）。"""
        rooms = state.get("rooms") or []
        members = state.get("members") or []
        my_roles = state.get("my_role_order") or []
        peers = state.get("peer_order") or []
        with self.write() as conn:
            conn.execute("DELETE FROM room_member")
            conn.execute("DELETE FROM member_list")
            conn.execute("DELETE FROM room")
            conn.execute("DELETE FROM member")
            for room in rooms:
                conn.execute(
                    "INSERT OR REPLACE INTO room(room_id, name, mode, group_id, created_at, config_json)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        str(room.get("room_id") or ""),
                        str(room.get("name") or ""),
                        str(room.get("mode") or "stage"),
                        str(room.get("group_id") or ""),
                        str(room.get("created_at") or ""),
                        json.dumps(room.get("config_json") or {}, ensure_ascii=False),
                    ),
                )
            for member in members:
                conn.execute(
                    "INSERT OR REPLACE INTO member(member_id, kind, display_name, avatar,"
                    " persona_label, sort_order, bot_id, platform, account, status,"
                    " last_active_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(member.get("member_id") or ""),
                        str(member.get("kind") or "user_role"),
                        str(member.get("display_name") or ""),
                        str(member.get("avatar") or ""),
                        str(member.get("persona_label") or ""),
                        int(member.get("sort_order") or 0),
                        str(member.get("bot_id") or ""),
                        str(member.get("platform") or ""),
                        str(member.get("account") or ""),
                        str(member.get("status") or ""),
                        float(member.get("last_active_at") or 0),
                        str(member.get("created_at") or ""),
                    ),
                )
            for list_id, order in (("my_roles", my_roles), ("peers", peers)):
                for index, member_id in enumerate(order):
                    conn.execute(
                        "INSERT OR REPLACE INTO member_list(list_id, member_id, sort_order)"
                        " VALUES (?,?,?)",
                        (list_id, str(member_id), index),
                    )
            for room in rooms:
                for index, member_id in enumerate(room.get("member_ids") or []):
                    conn.execute(
                        "INSERT OR REPLACE INTO room_member(room_id, member_id, sort_order)"
                        " VALUES (?,?,?)",
                        (str(room.get("room_id") or ""), str(member_id), index),
                    )

    def load_state(self) -> Optional[Dict[str, Any]]:
        """读回聊天室状态；账本里没有 ⇒ 返回 None（调用方保留出厂假数据）。"""
        with self.read() as conn:
            rooms = [dict(r) for r in conn.execute("SELECT * FROM room").fetchall()]
            if not rooms:
                return None
            members = [dict(r) for r in conn.execute("SELECT * FROM member").fetchall()]
            room_members = [
                dict(r) for r in conn.execute(
                    "SELECT room_id, member_id FROM room_member ORDER BY room_id, sort_order"
                ).fetchall()
            ]
            lists: Dict[str, List[str]] = {}
            for row in conn.execute("SELECT DISTINCT list_id FROM member_list").fetchall():
                lists[str(row["list_id"])] = [
                    str(r["member_id"]) for r in conn.execute(
                        "SELECT member_id FROM member_list WHERE list_id = ? ORDER BY sort_order",
                        (row["list_id"],),
                    ).fetchall()
                ]
        grouped: Dict[str, List[str]] = {}
        for row in room_members:
            grouped.setdefault(str(row["room_id"]), []).append(str(row["member_id"]))
        for room in rooms:
            room["member_ids"] = grouped.get(str(room["room_id"]), [])
        return {
            "rooms": rooms,
            "members": members,
            "my_role_order": lists.get("my_roles", []),
            "peer_order": lists.get("peers", []),
        }

    def get_bot_status(self, bot_id: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM bot_status WHERE bot_id = ?", (bot_id,)
            ).fetchone()
        return _status_row_to_dict(row) if row else None

    def list_bot_status(self) -> List[Dict[str, Any]]:
        """所有 Bot 自报的状态（没上报过的不会出现在这里）。"""
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM bot_status ORDER BY bot_id").fetchall()
        return [_status_row_to_dict(row) for row in rows]


def _status_row_to_dict(row: Any) -> Dict[str, Any]:
    """``bot_status`` 行 → dict（把 1/0 还原成 True/False/None）。"""
    data = dict(row)
    for field in ("handshake_ok", "style_fixed"):
        value = data.get(field)
        data[field] = None if value is None else bool(value)
    return data


def _is_locked(exc: Exception) -> bool:
    """判断是不是「数据库被锁」这类**可以重试**的错误。"""
    text = str(exc).lower()
    return "locked" in text or "busy" in text
