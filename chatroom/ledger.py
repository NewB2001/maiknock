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
from concurrent.futures import ThreadPoolExecutor
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
        seq              INTEGER NOT NULL DEFAULT 0,
        turn_id          TEXT NOT NULL DEFAULT '',      -- 属于哪一轮（调度用）
        wave             INTEGER NOT NULL DEFAULT 0     -- 第几波（0 = 第一波）
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
    # ── 轮次（turn）—— 调度计划与 seed 的存档 ─────────────────────────
    """
    CREATE TABLE IF NOT EXISTS turn (
        turn_id      TEXT PRIMARY KEY,
        room_id      TEXT NOT NULL DEFAULT '',
        segment_id   TEXT NOT NULL DEFAULT '',
        seed         INTEGER NOT NULL DEFAULT 0,
        strategy     TEXT NOT NULL DEFAULT 'random',   -- explicit | random
        formation    TEXT NOT NULL DEFAULT 'serial',   -- serial | parallel | composite
        plan_json    TEXT NOT NULL DEFAULT '{}',       -- 波次表（可复现）
        state        TEXT NOT NULL DEFAULT 'open',     -- open | done | timeout
        created_at   TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_turn_room ON turn(room_id, created_at)",
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
        "turn_id": "TEXT NOT NULL DEFAULT ''",
        "wave": "INTEGER NOT NULL DEFAULT 0",
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


#: 单个房间 config_json 的**合理上限**（字节）。
#: 房间配置只有几 KB；超过这个数就说明存/取不对称又发生了（转义膨胀）。
#: ⛔ **硬闸**：宁可丢配置也不能让一行膨胀到几百 MB —— 一行 512 MiB 就能把
#:    共享账本撑到 1.6 GB，页面读账本直接超时（实测踩过）。
_CONFIG_JSON_MAX_BYTES = 64 * 1024

#: 解包时最多剥几层转义（正常的 config_json 只有 1 层）。
_CONFIG_JSON_MAX_UNWRAP = 64


def _decode_config_json(raw: Any) -> Dict[str, Any]:
    """把房间的 ``config_json`` 还原成 dict（**存/取对称**）。

    **为什么必须对称**：``save_state`` 写的是 ``json.dumps(dict)``，
    若 ``load_state`` 不做 ``json.loads`` 就把 TEXT 原样交出去，
    下次存的时候又 dumps 一次 ⇒ **每轮存/取长度翻倍**
    （实测：``"{}"`` 经过 28 轮往返变成 **512 MiB** 的转义串）。

    **自愈**：已经被写坏的行（存了 N 层转义）会被**逐层剥回**原值。
    剥到不再是 JSON 字符串就停；剥不动（超上限 / 不是 JSON）就退化成 ``{}``。

    **硬闸**：正常读路径上，超过 :data:`_CONFIG_JSON_MAX_BYTES` 的行一律丢弃
    （退化成 ``{}``）—— 宁可丢房间配置，也不能让一行膨胀成几百 MB。

    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    # ⚠️ **先把超长的行挡掉再解析** —— 解析 512 MiB 的字符串本身就费内存。
    if len(raw) > _CONFIG_JSON_MAX_BYTES:
        import logging as _logging

        _logging.getLogger("plugin.maiknock.webui_companion").error(
            "[聊天室] 房间 config_json 异常膨胀（%d 字节 > %d）⇒ 已丢弃并置空",
            len(raw), _CONFIG_JSON_MAX_BYTES,
        )
        return {}
    value: Any = raw
    for _ in range(_CONFIG_JSON_MAX_UNWRAP):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        # 解出来不是字符串 ⇒ 已经是最终值了
        if not isinstance(decoded, str):
            return decoded if isinstance(decoded, dict) else {}
        # 解出来还是字符串 ⇒ 说明外面又裹了一层 ⇒ 继续剥
        value = decoded
        # 剥到正常大小了 ⇒ 直接解出来收工（不用再走一圈）
        if len(value) <= _CONFIG_JSON_MAX_BYTES:
            try:
                final = json.loads(value)
            except (TypeError, ValueError):
                return {}
            return final if isinstance(final, dict) else {}
    # 剥了这么多层还没剥完 ⇒ 不正常，丢掉
    return {}


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
        # ⚠️⚠️ **专属单线程 executor** —— 账本的所有读写都在这**一个**线程里串行做。
        #    为什么不直接用 asyncio.to_thread：那是**默认线程池**
        #    （max_workers = min(32, cpu+4)）⇒ 回读一轮 14 条消息 × 2 次账本操作
        #    = 28 个任务，两个 Bot 的进程各一份 ⇒ **把默认池占满**
        #    ⇒ 宿主的组件调用（也要线程）排在后面 ⇒ **10 秒超时**（实测）。
        #    专属单线程 ⇒ 账本访问串行、不占默认池、跨进程写锁争用也最小。
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="chatroom-ledger"
        )

    # ── 身份 ────────────────────────────────────────────────────────────

    async def run(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """把一个同步的账本操作丢到**专属单线程**上跑（不占默认线程池）。

        ⚠️ 组件调用（宿主→插件）走的是 ``runner_main._invoke_plugin_callable``：
        **同步函数丢默认线程池、异步函数直接 await** ⇒ 我们的账本操作
        如果用 ``asyncio.to_thread``，会和组件调用抢同一个池 ⇒ 超时。
        """
        import asyncio as _asyncio
        import functools as _functools
        import logging as _lg
        import time as _tm
        _t0 = _tm.time()
        result = await _asyncio.get_running_loop().run_in_executor(
            self._executor, _functools.partial(func, *args, **kwargs)
        )
        _elapsed = _tm.time() - _t0
        if _elapsed > 0.5:
            _lg.getLogger("plugin.maiknock.webui_companion").warning(
                "[聊天室] 账本操作慢：%s 用了 %.2fs", getattr(func, "__name__", func), _elapsed
            )
        return result

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
        # ⚠️⚠️ **自愈：修掉历史上被写坏的 config_json**（存/取不对称的遗留）。
        #    坏掉的行可能有几百 MB ⇒ 库文件涨到 GB 级 ⇒ 页面读账本直接超时。
        #    这里是**唯一**该做清理的地方：进程启动时跑一次，不在热路径上。
        self._repair_config_json()
        # ⚠️ **修完行还要把空间还回去**（VACUUM）——
        #    修好的行只占几 KB，但**文件不会自己缩**（释放的页还在 freelist 里）
        #    ⇒ 1.6 GB 的库文件会一直占着磁盘。
        #    ⚠️ VACUUM 需要**独占**这个库 ⇒ 另一个 Bot 进程开着就会失败
        #    ⇒ 失败就失败，不影响功能（每个进程启动时都会再试一次）。
        self._vacuum_if_wasted()

    def _repair_config_json(self) -> None:
        """把异常膨胀（或多层转义）的 ``config_json`` 修回正常值。

        ⚠️ **为什么需要**：早期版本的 ``load_state`` 没把 ``config_json`` 解析回 dict，
        ``save_state`` 又 dumps 一次 ⇒ 每轮存/取**长度翻倍**
        （实测：``"{}"`` 经过 28 轮往返变成 **512 MiB**，3 个房间把库撑到 **1.6 GB**）。

        修法：把每行的值**剥回**原样再写回；剥不动（超上限）就置空
        （房间配置不是核心数据，丢了不影响剧情）。
        """
        import logging

        log = logging.getLogger("plugin.maiknock.webui_companion")
        try:
            # ⚠️⚠️ **绝对不要 SELECT 整列** ——
            #    坏行可能有几百 MB ⇒ 一次性读进 Python 就是几个 GB 的内存 ⇒ OOM。
            #    只取**长度**和**首字符**（让 SQL 侧算，不进 Python）。
            with self.read() as conn:
                rows = conn.execute(
                    "SELECT room_id, LENGTH(config_json) AS n,"
                    " SUBSTR(config_json, 1, 1) AS head FROM room"
                ).fetchall()
        except sqlite3.Error as exc:
            log.warning("[聊天室] 检查房间 config_json 失败：%r", exc)
            return
        broken = []
        for row in rows:
            room_id = str(row["room_id"])
            size = int(row["n"] or 0)
            head = str(row["head"] or "")
            # ⚠️⚠️ **判据是「结构」不是「大小」** ——
            #    好的 config_json 是 **JSON 对象**（以 ``{`` 开头）；
            #    被裹了转义的坏行是 **JSON 字符串**（以 ``"`` 开头）。
            #    只按大小判断会漏掉「只裹了几层、但还没到 64 KiB」的行
            #    （那行照样会让每一轮存/取继续翻倍 ⇒ 迟早又膨胀回去）。
            if head == "{" and size <= _CONFIG_JSON_MAX_BYTES:
                continue        # 形状对 ⇒ 大概率没问题
            broken.append((room_id, size, head))
        if not broken:
            return
        log.error(
            "[聊天室] ⚠️ 发现 %d 个房间的 config_json 异常（%s）⇒ 正在修复",
            len(broken),
            "、".join(f"{rid}:{size}字节/首字符{head!r}" for rid, size, head in broken),
        )
        fixed = 0
        try:
            with self.write() as conn:
                for room_id, size, _head in broken:
                    if size > _CONFIG_JSON_MAX_BYTES:
                        # ⚠️ **太大的行：直接用 SQL 覆写，不进 Python**
                        #    （读进 Python 就是几百 MB 的内存 ⇒ OOM）
                        conn.execute(
                            "UPDATE room SET config_json = '{}' WHERE room_id = ?",
                            (room_id,),
                        )
                    else:
                        # 小行才值得剥：多层转义能被还原成原值
                        row = conn.execute(
                            "SELECT config_json FROM room WHERE room_id = ?",
                            (room_id,),
                        ).fetchone()
                        value = _decode_config_json(row["config_json"] if row else None)
                        conn.execute(
                            "UPDATE room SET config_json = ? WHERE room_id = ?",
                            (json.dumps(value, ensure_ascii=False), room_id),
                        )
                    fixed += 1
        except sqlite3.Error as exc:
            log.error("[聊天室] 修复房间 config_json 失败：%r", exc)
            return
        log.info("[聊天室] 已修复 %d 个房间的 config_json", fixed)

    def _vacuum_if_wasted(self) -> None:
        """库文件「虚胖」时做一次 VACUUM + checkpoint，把它缩回实际大小。

        ⚠️ **两种「虚胖」要分清**（踩过）：

        ① **freelist 里的空页** —— 数据被删/改小了，页还在文件里。
           ⇒ 靠 ``PRAGMA freelist_count`` 判断，``VACUUM`` 能解决。

        ② **主文件没被截断** —— WAL 模式下 ``VACUUM`` 只把新内容写进 WAL，
           **主库文件的大小不变**（实测：日志说「1538 MB → 0 MB」，
           磁盘上还是 1.6 GB！）。
           ⇒ 靠**磁盘实际大小 vs 逻辑大小**判断，``wal_checkpoint(TRUNCATE)`` 才能解决。

        两种都做：先按需 VACUUM，再无条件 checkpoint（把 WAL 写回主文件并截断）。
        """
        import logging
        import os

        log = logging.getLogger("plugin.maiknock.webui_companion")
        try:
            conn = self._require_conn()
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            free_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        except sqlite3.Error as exc:
            log.warning("[聊天室] 读账本页数失败：%r", exc)
            return
        # ⚠️⚠️ **「实际用掉」的字节数 = (总页数 - freelist 页数) × 页大小** ——
        #    `page_count` **包含** freelist 里的空页！直接拿它算「逻辑大小」
        #    会得到和磁盘大小差不多的数 ⇒ 永远判定不出「虚胖」（我自己踩过）。
        used_bytes = max(0, page_count - free_count) * page_size
        try:
            disk_bytes = os.path.getsize(self._path)
        except OSError:
            disk_bytes = used_bytes
        # 磁盘上比实际用掉的大 1.5 倍以上 ⇒ 虚胖（freelist 空页 或 主文件没截断）
        wasted = disk_bytes > used_bytes * 3 // 2
        if not wasted:
            return
        before_mb = disk_bytes / (1024 * 1024)
        # ① freelist 里堆了空页 ⇒ 先 VACUUM（把空页扔掉）
        if free_count >= 1000:
            log.warning(
                "[聊天室] 账本有大量空页（%d/%d 页，实际只用 %.0f MB，磁盘占 %.0f MB）"
                "⇒ 正在 VACUUM 瘦身",
                free_count, page_count, used_bytes / (1024 * 1024), before_mb,
            )
            try:
                conn.execute("VACUUM")
            except sqlite3.Error as exc:
                # ⚠️ 另一个 Bot 进程开着这个库时会失败 ⇒ 不当回事
                log.warning("[聊天室] VACUUM 未能执行（可能有别的进程在用）：%r", exc)
        # ② ⚠️ **checkpoint(TRUNCATE) 才是真正让磁盘文件缩小的那一步** ——
        #    WAL 模式下 VACUUM 只写 WAL，主库文件大小不变。
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            log.warning("[聊天室] WAL checkpoint 未完成：%r", exc)
        try:
            disk_after = os.path.getsize(self._path)
        except OSError:
            disk_after = logical_bytes
        log.info(
            "[聊天室] 账本瘦身：%.0f MB → %.0f MB",
            before_mb, disk_after / (1024 * 1024),
        )

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
            # ⚠️ **别等太久**：撞锁时这是**同步阻塞**，会卡住插件的事件循环
            #    ⇒ 宿主调不动插件的组件接口 ⇒ 页面全部超时（实测踩过）。
            #    等 1.5 秒拿不到锁就快速失败，让上层重试。
            "PRAGMA busy_timeout=1500",
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
        turn_id: str = "",
        wave: int = 0,
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
            "turn_id", "wave",
        )
        values = (
            delivery_id, segment_id, room_id, target_bot_id,
            sender_member_id, content, DELIVERY_PENDING, created,
            "", "",
            float(baseline_ts if baseline_ts is not None else time.time()),
            group_id, group_name, sender_platform, sender_account,
            sender_nickname, 0, int(seq),
            str(turn_id or ""), int(wave),
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
        """取属于某个 Bot 的待办任务（按创建顺序）。

        ⚠️⚠️ **按波次门控**：只返回「所在的波次已经轮到」的单。
        波次门控 = 同一轮里，**当前波之前的所有波都已经递出**才算轮到。
        这样：
        - 串行阵型（每波只有 1 个 Bot）⇒ 天然逐个递；
        - 并行阵型（全在同一波）⇒ 全部一次放行；
        - 复合阵型 ⇒ 波内并行、波间串行。
        光靠 SQL 不好表达，所以**取出来再在 Python 里过滤**
        （一轮的任务量很小：几个 Bot 而已）。
        """
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM delivery WHERE target_bot_id = ? AND state = ?"
                " ORDER BY seq, created_at, delivery_id LIMIT ?",
                (target_bot_id, DELIVERY_PENDING, int(limit)),
            ).fetchall()
            pending = [dict(row) for row in rows]
            if not pending:
                return []
            # 收集这些任务涉及的轮次 ⇒ 查每轮「已进行到第几波」
            turn_ids = {str(r.get("turn_id") or "") for r in pending if r.get("turn_id")}
            wave_frontier: Dict[str, int] = {}
            for turn_id in turn_ids:
                wave_frontier[turn_id] = self._turn_wave_frontier(conn, turn_id)
        out = []
        for row in pending:
            turn_id = str(row.get("turn_id") or "")
            if not turn_id:
                out.append(row)             # 无轮次（历史/单条递话）⇒ 不门控
                continue
            if int(row.get("wave") or 0) <= wave_frontier.get(turn_id, 0):
                out.append(row)
        return out

    @staticmethod
    def _turn_wave_frontier(conn: sqlite3.Connection, turn_id: str) -> int:
        """某一轮**当前放行到第几波**。

        ⚠️ 语义：返回「下一个未完成的波号」。
        若第 0 波还有未递完的（pending/delivered），就只放行第 0 波（返回 0）；
        第 0 波全部进入 collected/timeout/cancelled ⇒ 放行到第 1 波（返回 1）……
        """
        rows = conn.execute(
            "SELECT wave, state FROM delivery WHERE turn_id = ?", (turn_id,)
        ).fetchall()
        if not rows:
            return 0
        # 未完成的 = 还会产生后续动作的波
        inflight = {int(r["wave"]) for r in rows if str(r["state"]) in (DELIVERY_PENDING, DELIVERY_DELIVERED)}
        done_max = -1
        for r in rows:
            if str(r["state"]) in (DELIVERY_COLLECTED, DELIVERY_TIMEOUT, DELIVERY_CANCELLED):
                done_max = max(done_max, int(r["wave"]))
        # 最小未完成的波号；没有未完成的 ⇒ 全部放行
        if not inflight:
            return 10 ** 6
        return min(inflight)

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

    def find_member_by_bot_id(self, bot_id: str) -> str:
        """按 bot_id 找成员 id（回读时认人用）。

        ⚠️ 成员的 member_id **不一定是** ``bot_<bot_id>`` ——
        匹配到已有成员时会**保留原来的 member_id**（如壳阶段的 ``bot_hinata``），
        所以必须反查，不能自己拼。
        """
        if not bot_id:
            return ""
        with self.read() as conn:
            row = conn.execute(
                "SELECT member_id FROM member WHERE bot_id = ? LIMIT 1", (bot_id,)
            ).fetchone()
        return str(row[0]) if row else ""

    # ── 回读协作（S6）─────────────────────────────────────────────────

    def find_delivery_segment(
        self, target_bot_id: str, room_id: str, reply_at: str = ""
    ) -> str:
        """找「这条回复回应的是哪一次投递」。

        ⚠️ **按时间配对**，不能只取"最近一条"——
        否则一轮里所有回复都会堆到最后一段（实测踩过）。
        规则：取**投递时刻不晚于回复时刻**的那次投递里最近的一个
        ⇒ 一句话对上一次递话，天然对齐。

        ⚠️ 这里**不按 collected 过滤**：一个 Bot 可能对同一次投递回好几条
        （文本 + 表情包），第二条也要挂到同一段。
        """
        sql = (
            "SELECT segment_id FROM delivery"
            " WHERE target_bot_id = ? AND room_id = ?"
        )
        params: List[Any] = [target_bot_id, room_id]
        if reply_at:
            sql += " AND created_at <= ?"
            params.append(str(reply_at))
        sql += " ORDER BY created_at DESC, seq DESC LIMIT 1"
        with self.read() as conn:
            row = conn.execute(sql, params).fetchone()
        return str(row[0]) if row else ""

    def find_delivery_for_reply(
        self, target_bot_id: str, room_id: str, reply_at: str = ""
    ) -> Optional[Dict[str, Any]]:
        """找「这条回复回应的是哪一次投递」的**完整任务**（含 turn_id / wave）。

        ⚠️ 规则与 :meth:`find_delivery_segment` **完全一致**（按时间配对），
        只是多返回 turn_id / wave —— S8 的接力与波次门控要用。
        """
        sql = (
            "SELECT * FROM delivery"
            " WHERE target_bot_id = ? AND room_id = ?"
        )
        params: List[Any] = [target_bot_id, room_id]
        if reply_at:
            sql += " AND created_at <= ?"
            params.append(str(reply_at))
        sql += " ORDER BY created_at DESC, seq DESC LIMIT 1"
        with self.read() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def relay_reply_to_later_waves(
        self, turn_id: str, from_wave: int, reply_text: str, reply_by: str
    ) -> int:
        """把这个 Bot 的回复**接力**到同一轮里**后续波次**的待办任务。

        ⚠️ **为什么需要**（08 §3.3 串行/复合阵型）：后答的 Bot **要看得见**
        先答的回复。可各 Bot 的群流是**各自独立的副本** ⇒ 先答 Bot 的回复
        只存在**它自己**的群里，后答 Bot 看不见 ⇒ 必须由中控把这段话
        **塞进后答 Bot 的投递正文**。

        :param from_wave: 说话的这个 Bot 在第几波
        :param reply_text: 它到底回了什么
        :param reply_by: 它的显示名（拼进接力前缀）
        :returns: 改了几条后续任务
        """
        text = str(reply_text or "").strip()
        if not turn_id or not text:
            return 0
        prefix = f"（这期间 {reply_by} 说：{text}）\n"
        changed = 0
        with self.write() as conn:
            rows = conn.execute(
                "SELECT delivery_id, content FROM delivery"
                " WHERE turn_id = ? AND wave > ? AND state = ?",
                (turn_id, int(from_wave), DELIVERY_PENDING),
            ).fetchall()
            for row in rows:
                content = str(row["content"] or "")
                conn.execute(
                    "UPDATE delivery SET content = ? WHERE delivery_id = ?",
                    (prefix + content, str(row["delivery_id"])),
                )
                changed += 1
        return changed

    def timeout_stale_deliveries(
        self, target_bot_id: str, older_than_seconds: float = 150.0
    ) -> int:
        """把本 Bot「递出去了但迟迟没回」的投递标记为超时。

        ⚠️ **为什么必须超时**（FR-09）：串行/复合阵型靠波次门控推进，
        某一波里的 Bot 一直不回 ⇒ 后面的波永远不放行 ⇒ 整轮卡死。
        ⇒ 递出超过 ``older_than_seconds`` 还没变成 collected 的，标 timeout
        （**只记状态，不产消息行**——沉默绝不进时间线）。

        ⚠️ 只动本 Bot 自己的单（别的 Bot 由它们各自的实例管）。
        """
        cutoff = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - float(older_than_seconds))
        )
        with self.write() as conn:
            cur = conn.execute(
                "UPDATE delivery SET state = ?"
                " WHERE target_bot_id = ? AND state = ? AND delivered_at <= ?",
                (DELIVERY_TIMEOUT, target_bot_id, DELIVERY_DELIVERED, cutoff),
            )
            return int(cur.rowcount or 0)

    def ensure_open_segment(self, room_id: str) -> str:
        """给「没有对应投递的回复」开一个兜底段（历史消息、旁白等）。

        ⚠️ 兜底段也走正常的时间线渲染 ⇒ 用户至少能看到"她说过什么"，
        而不是回复凭空消失。
        """
        with self.read() as conn:
            row = conn.execute(
                "SELECT segment_id FROM segment WHERE room_id = ? AND kind = 'loose'"
                " ORDER BY seq DESC LIMIT 1", (room_id,)
            ).fetchone()
        if row:
            return str(row[0])
        seq = self.next_segment_seq(room_id)
        segment_id = f"seg_loose_{room_id}_{seq}"
        with self.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO segment(segment_id, room_id, seq, kind, created_at, voided)"
                " VALUES (?,?,?,'loose',?,0)",
                (segment_id, room_id, seq, _now()),
            )
        return segment_id

    def pending_collect_targets(self, target_bot_id: str) -> List[Dict[str, Any]]:
        """我这边「已递出但还没收完」的投递任务。

        ⚠️ **回读只在有这些任务时才做** ——
        没有投递就不可能有新回复 ⇒ 不必去读群流。
        这把「插件→宿主」的 RPC 量降了约九成 ⇒ 组件调用与回读撞同一条 IPC 通道
        的概率大幅下降（实测：回读每 5 秒一次 ⇒ 宿主调插件时经常超时）。
        """
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM delivery WHERE target_bot_id = ? AND state = ?"
                " ORDER BY seq",
                (target_bot_id, DELIVERY_DELIVERED),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_deliveries_collected(self, target_bot_id: str) -> int:
        """把该 Bot 已投递完的投递标记为 collected。"""
        with self.write() as conn:
            cur = conn.execute(
                "UPDATE delivery SET state = ?, collected_at = ?"
                " WHERE target_bot_id = ? AND state = ?",
                (DELIVERY_COLLECTED, _now(), target_bot_id, DELIVERY_DELIVERED),
            )
            return int(cur.rowcount or 0)

    # ── 轮次（turn）—— 调度计划与 seed 的存档（S8）────────────────────
    # ⚠️ **为什么要存 seed**：随机轮次的阵型是掷骰子定的，
    #    不存的话「这一轮为什么是串行」事后无法复现、无法重演（FR-05/FR-14）。

    def save_turn(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        """登记一轮（含 seed 与阵型计划）。覆盖式写入（同 turn_id 重存）。"""
        turn_id = str(turn.get("turn_id") or "")
        if not turn_id:
            raise LedgerError("save_turn 需要 turn_id")
        plan = turn.get("plan") or {}
        plan_json = plan if isinstance(plan, str) else json.dumps(plan, ensure_ascii=False)
        with self.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO turn(turn_id, room_id, segment_id, seed,"
                " strategy, formation, plan_json, state, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    turn_id,
                    str(turn.get("room_id") or ""),
                    str(turn.get("segment_id") or ""),
                    int(turn.get("seed") or 0),
                    str(turn.get("strategy") or "random"),
                    str(turn.get("formation") or "serial"),
                    plan_json,
                    str(turn.get("state") or "open"),
                    str(turn.get("created_at") or _now()),
                ),
            )
        row = self.get_turn(turn_id)
        assert row is not None
        return row

    def get_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        """读一轮（把 plan_json 解析回 dict —— **存/取必须对称**）。"""
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM turn WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        # ⚠️ 存的时候 dumps、读的时候必须 loads —— 否则每轮往返多裹一层
        #    （账本 config_json 就是栽在这上面，见 _decode_config_json 的注释）
        data["plan"] = _decode_config_json(data.get("plan_json"))
        return data

    def update_turn_state(self, turn_id: str, state: str) -> None:
        """更新一轮的状态（open / done / timeout）。"""
        with self.write() as conn:
            conn.execute(
                "UPDATE turn SET state = ? WHERE turn_id = ?", (str(state), turn_id)
            )

    def list_turns(self, room_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """某个房间最近的轮次（调试面板 / 重演用）。"""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM turn WHERE room_id = ? ORDER BY created_at DESC LIMIT ?",
                (room_id, int(limit)),
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["plan"] = _decode_config_json(data.get("plan_json"))
            out.append(data)
        return out

    def turn_deliveries(self, turn_id: str) -> List[Dict[str, Any]]:
        """某一轮的全部投递任务（按波次、序号排）。"""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM delivery WHERE turn_id = ? ORDER BY wave, seq",
                (turn_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def turn_wave_state(self, turn_id: str) -> Dict[int, List[str]]:
        """某一轮**每一波**各任务的状态 ⇒ ``{波号: [state, ...]}``。

        ⚠️ 中控靠它判断「这一波说完了没」（串行/复合阵型靠它决定何时放下一波）：
        一波里的任务**全部**进入 collected/timeout/cancelled ⇒ 这一波结束。
        """
        out: Dict[int, List[str]] = {}
        for row in self.turn_deliveries(turn_id):
            out.setdefault(int(row.get("wave") or 0), []).append(str(row.get("state") or ""))
        return out

    # ── 消息（S6 回读落库）───────────────────────────────────────────

    def append_message(self, message: Dict[str, Any]) -> bool:
        """追加一条消息行。**按 message_id 去重**（同一群消息只收一次）。

        ⚠️ **已存在的行会更新归属段**（segment_id / room_id）——
        回读的归属规则改过之后（比如从"取最近一条投递"改成"按时间配对"），
        重新收一遍就能**自愈**，不用手工清库。

        返回 True = 新增；False = 之前收过（已更新归属）。
        """
        message_id = str(message.get("message_id") or "")
        if not message_id:
            return False
        segment_id = str(message.get("segment_id") or "")
        room_id = str(message.get("room_id") or "")
        with self.write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO message(message_id, segment_id, room_id,"
                " speaker_member_id, speaker_name, kind, content, created_at, voided)"
                " VALUES (?,?,?,?,?,?,?,?,0)",
                (
                    message_id,
                    segment_id,
                    room_id,
                    str(message.get("speaker_member_id") or ""),
                    str(message.get("speaker_name") or ""),
                    str(message.get("kind") or "line"),
                    str(message.get("content") or ""),
                    str(message.get("created_at") or ""),
                ),
            )
            if cur.rowcount:
                return True
            # 已收过 ⇒ 更新归属（自愈）
            conn.execute(
                "UPDATE message SET segment_id = ?, room_id = ? WHERE message_id = ?",
                (segment_id, room_id, message_id),
            )
            return False

    def list_timeline(self, room_id: str, include_voided: bool = True) -> List[Dict[str, Any]]:
        """一个房间的时间线（页面要的形状）。

        ⚠️ **默认把作废段也带上**（带 ``voided`` 标记）——「回到此处」之后，
        作废的内容要**灰显**在舞台上（D9.3），而不是凭空消失
        （壳阶段一直是这么演的：作废旧段保留、不重排、只是灰掉）。
        内容类逻辑（递话/接力/回读）不看这条时间线，所以带上无副作用。
        """
        segments = self.list_segments(room_id, include_voided=include_voided)
        messages = self.list_messages(room_id, include_voided=include_voided)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for msg in messages:
            grouped.setdefault(str(msg.get("segment_id") or ""), []).append(msg)
        timeline: List[Dict[str, Any]] = []
        for segment in segments:
            segment_id = str(segment.get("segment_id") or "")
            timeline.append(
                {
                    "seq": segment.get("seq"),
                    "kind": segment.get("kind") or "normal",
                    "voided": bool(segment.get("voided")),
                    "created_at": segment.get("created_at") or "",
                    "messages": [
                        {
                            "message_id": m.get("message_id"),
                            "speaker_member_id": m.get("speaker_member_id") or "",
                            "speaker_name": m.get("speaker_name") or "",
                            "kind": m.get("kind") or "line",
                            "content": m.get("content") or "",
                            "time": str(m.get("created_at") or "")[-8:-3],
                        }
                        for m in grouped.get(segment_id, [])
                    ],
                }
            )
        return timeline

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
                        json.dumps(
                            _decode_config_json(room.get("config_json")),
                            ensure_ascii=False,
                        ),
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
            # ⚠️⚠️⚠️ **必须把 config_json 解析回 dict** ——
            #    不解析 ⇒ 拿到的是**未解码的 TEXT 字符串**，
            #    下次 save_state 又对它 json.dumps 一次 ⇒ **每轮存/取长度翻倍**。
            #    实测：原始 "{}" 经过 28 轮往返变成 **512 MiB** 的转义串，
            #    3 个房间把共享账本撑到 **1.6 GB** ⇒ bootstrap 读账本直接超时
            #    （实测：timeline 30ms / errors 4.8s / bootstrap 10s ERR）。
            #    根因是**存/取不对称**：存的时候 dumps，读的时候不 loads。
            room["config_json"] = _decode_config_json(room.get("config_json"))
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
