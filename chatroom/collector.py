# -*- coding: utf-8 -*-
"""麦麦聊天室 · 回读（把 Bot 的回复收进共享账本）。

**问题**：S5 只做了「递话」，麦麦回的话留在它**自己实例的库**里
⇒ 聊天室的页面上看不到回复（只有 WebUI 聊天页能看到）。

**解法**：每个 Bot 的插件实例**读自己的群流**（`message.get_recent`），
认出「哪些是 Bot 的回复」，写进**共享账本** ⇒ 中控汇总后渲染到时间线。

**分工**（跟「各 Bot 自报接入状态 / 错误」同构）：
- **每个 Bot 只读自己群流的副本**（各 Bot 的群流本来就是独立的副本，见 08 §3.3）
  ⇒ 一号读到的只有一号自己的回复，二号读到的只有二号的 ⇒ **天然不重复**
- 写进共享账本 ⇒ 中控（一号）把两边的都显示出来

**宿主能力的几个坑（都踩过）**：
1. SDK 的 ``ctx.message.get_recent(chat_id, limit)`` **不暴露 ``hours``**
   ⇒ 必须直连 ``ctx.call_capability("message.get_recent", chat_id=…, limit=…, hours=…)``；
2. ``hours`` 每次都要显式给（默认 24.0），跨天-span 的剧情读不到；
3. ``limit`` 要给足（SDK 默认 10）；
4. ``filter_mai`` **必须 False** —— 过滤掉麦麦自己的发言就认不出她的回复了；
5. 发言人字段是 ``message_info.user_info.user_id`` / ``user_nickname``
   （**不是** ``nickname`` / ``user_name`` ⇒ 按错键名取会**恒取不到**）；
6. 正文用 ``processed_plain_text``（别去解析 ``raw_message``）。

**纯逻辑部分**（认人、归属、去重）与宿主调用分离 ⇒ 可离线单测。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("plugin.maiknock.webui_companion")

#: 我方角色的账号前缀（`user.<member_id>`）⇒ 用它区分「谁说的话」
USER_ACCOUNT_PREFIX = "user."

#: Bot 的账号前缀（`bot.<bot_id>`）
BOT_ACCOUNT_PREFIX = "bot."

#: ⚠️ **宿主给「Bot 自己发的消息」用的账号是字面量 ``self``**（不是 ``bot.<id>``）
#:    —— 实测（2026-11-26）：一号在群里的回复 ``user_id='self'``、``account_id='self'``、
#:    ``user_nickname='日向雏田'``。只按 ``bot.`` 前缀判会把 Bot 的回复全当"其它"丢掉。
HOST_SELF_ACCOUNT = "self"


def classify_message(message: Dict[str, Any]) -> Tuple[str, str, str]:
    """认一条消息 ⇒ (类别, 账号, 昵称)。

    类别：
    - ``bot``    —— Bot 的回复（账号以 ``bot.`` 开头）
    - ``user``  —— 我方角色（账号以 ``user.`` 开头）
    - ``other`` —— 其它（旁白、未知来源）
    """
    info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
    user = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
    account = str(user.get("user_id") or "")
    nickname = str(user.get("user_nickname") or user.get("user_cardname") or "")
    if account == HOST_SELF_ACCOUNT or account.startswith(BOT_ACCOUNT_PREFIX):
        return "bot", account, nickname
    if account.startswith(USER_ACCOUNT_PREFIX):
        return "user", account, nickname
    return "other", account, nickname


def message_text(message: Dict[str, Any]) -> str:
    """取消息正文。

    ⚠️ 用 ``processed_plain_text``（宿主算好的纯文本）；
    实在没有才退回 ``raw_message`` 里的文本段。
    """
    text = message.get("processed_plain_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    raw = message.get("raw_message")
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, str):
            return data.strip()
    return ""


def message_time(message: Dict[str, Any]) -> str:
    """取消息时刻（``YYYY-MM-DD HH:MM:SS``）。

    ⚠️ 宿主给的 ``timestamp`` 是**秒级浮点的字符串**（``message_utils.py:431``），
    这里转成本地时间的可读格式。
    """
    raw = message.get("timestamp")
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(raw)))
    except (TypeError, ValueError):
        return str(raw or "")


class ReplyCollector:
    """回读器：把自己群流里的 Bot 回复收进共享账本。"""

    def __init__(
        self,
        ctx: Any,
        ledger: Any,
        bot_id: str,
        *,
        sessions: Optional[Dict[str, str]] = None,
        hours: float = 6.0,
        limit: int = 100,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._ctx = ctx
        self._ledger = ledger
        self._bot_id = str(bot_id)
        #: {session_id: room_id} —— 哪些群流要采
        self._sessions = dict(sessions or {})
        self._hours = float(hours)
        self._limit = int(limit)
        self._log = log or _log
        self._seen: set = set()
        self._collected = 0
        #: 「有没有插件→宿主的 RPC 正在飞」——
        #: 组件调用（宿主→插件）要等它清零，避免**两个方向抢同一条 IPC 通道**。
        #:
        #: ⚠️ **组件调用与本回读器跑在同一个 Runner loop 上**（都是
        #:    ``asyncio.run()`` 里的协程），所以这个 Event 是**可以**跨协程等待的
        #:    —— 不是「跨 loop 等不到」那种坑。
        self.rpc_idle = asyncio.Event()
        self.rpc_idle.set()

    @property
    def collected_count(self) -> int:
        """已收进账本的回复条数。"""
        return self._collected

    def set_sessions(self, sessions: Dict[str, str]) -> None:
        """更新要采的群流（房间会增删 ⇒ 要能变）。"""
        self._sessions = dict(sessions or {})

    async def _fetch(self, session_id: str) -> List[Dict[str, Any]]:
        """读一条群流的最近消息（**直连能力**，绕开 SDK 那层不带 hours 的封装）。"""
        self.rpc_idle.clear()
        try:
            result = await self._ctx.call_capability(
                "message.get_recent",
                chat_id=session_id,
                limit=self._limit,
                hours=self._hours,
                filter_mai=False,   # ⚠️ 必须 False：过滤掉麦麦就认不出她的回复了
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("[聊天室] 回读失败（session=%s）：%r", session_id[:12], exc)
            return []
        finally:
            self.rpc_idle.set()
        if isinstance(result, dict):
            messages = result.get("messages")
            return messages if isinstance(messages, list) else []
        return result if isinstance(result, list) else []

    def _member_id_for(self, account: str) -> str:
        """发言人的成员 id（要能与成员登记表对上，页面才能认出人）。

        - ``self`` ⇒ 是**本 Bot** ⇒ 用它在登记表里的 member_id
        - ``bot.<bot_id>`` ⇒ 按 bot_id 反查
        - 查不到 ⇒ 退回 ``bot_<bot_id>`` 形式（不至于认不出人）
        """
        if account == HOST_SELF_ACCOUNT:
            found = self._ledger.find_member_by_bot_id(self._bot_id)
            return found or f"bot_{self._bot_id}"
        if "." in account:
            bot_id = account.split(".", 1)[-1]
            found = self._ledger.find_member_by_bot_id(bot_id)
            return found or f"bot_{bot_id}"
        return account

    async def poll(self) -> int:
        """采一轮。返回新收进账本的条数。"""
        if self._ledger is None or not self._sessions:
            return 0
        # ⚠️⚠️ **没有待收的投递 ⇒ 不去读群流**。
        #    没有投递就不可能有新回复 ⇒ 这一轮根本不用发「插件→宿主」的 RPC。
        #    这把 RPC 量降了约九成 ⇒ 组件调用与回读撞同一条 IPC 通道的概率大幅下降
        #    （实测：回读每 5 秒一次时，宿主调插件经常 10 秒超时）。
        try:
            pending = await self._ledger.run(
                self._ledger.pending_collect_targets, self._bot_id
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("[聊天室] 查待收投递失败：%r", exc)
            return 0
        if not pending:
            return 0
        collected = 0
        for session_id, room_id in list(self._sessions.items()):
            fetched = await self._fetch(session_id)
            self._log.info(
                "[聊天室] 回读 %s：取到 %d 条（room=%s）",
                session_id[:12], len(fetched), room_id,
            )
            for message in fetched:
                if not isinstance(message, dict):
                    continue
                if message.get("is_command") or message.get("is_notify"):
                    continue        # 命令/通知不是剧情
                kind, account, nickname = classify_message(message)
                if kind != "bot":
                    continue        # 只收 Bot 的回复（我方角色的话是"递出去"的，不回读）
                message_id = str(message.get("message_id") or "")
                if not message_id:
                    continue
                # ⚠️ **不在这里跳过已见过的**：归属规则改过之后要能**自愈**
                # （ledger.append_message 对已存在的行会更新归属段）。
                # 靠 ledger 的 message_id 去重，不会重复写内容。
                self._seen.add(message_id)
                text = message_text(message)
                if not text:
                    continue
                # 归属段：这条回复回应的是**哪一段**的投递
                segment_id = await self._ledger.run(
                    self._ledger.find_delivery_segment,
                    target_bot_id=self._bot_id,
                    room_id=room_id,
                    reply_at=message_time(message),
                )
                if not segment_id:
                    # 没有对应的投递（可能是历史消息）⇒ 挂到房间的"未归属"段
                    segment_id = await self._ledger.run(
                        self._ledger.ensure_open_segment, room_id
                    )
                member_id = self._member_id_for(account)
                # ⚠️ **写账本必须挪到线程里**：SQLite 写是**同步**的，
                #    跨进程抢锁时会阻塞插件的**事件循环** ⇒ 宿主调不动组件接口
                #    ⇒ 页面的查询全部超时（实测踩过：bootstrap / errors / timeline 全挂）。
                written = await self._ledger.run(
                    self._ledger.append_message,
                    {
                        "message_id": message_id,
                        "segment_id": segment_id,
                        "room_id": room_id,
                        "speaker_member_id": member_id,
                        "speaker_name": nickname or member_id,
                        "kind": "reply",
                        "content": text,
                        "created_at": message_time(message),
                    },
                )
                if written:
                    collected += 1
                    self._collected += 1
                    self._log.info(
                        "[聊天室] 收到回复（Bot %s）：%s", self._bot_id[:12], text[:40]
                    )
        if collected:
            await self._ledger.run(
                self._ledger.mark_deliveries_collected, self._bot_id
            )
        return collected
