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

from .identity import DEFAULT_PLATFORM, session_id_for_private, virtual_group_id

_log = logging.getLogger("plugin.maiknock.webui_companion")

#: 我方角色的账号前缀（`user.<member_id>`）⇒ 用它区分「谁说的话」
USER_ACCOUNT_PREFIX = "user."

#: Bot 的账号前缀（`bot.<bot_id>`）
BOT_ACCOUNT_PREFIX = "bot."

#: ⚠️ 「别的 Bot 的台词」账号前缀（`peer.<bot_id>`）——2026-11-28 串台修复：
#:    collector 收到本 Bot 回复后，把回复广播给房间其他 Bot，让它们在自己的
#:    群流里沉淀成历史消息（否则每个 Bot 永远只看得见自己说的话）。
#:    用独立前缀而非 bot./user.，让它**不被当成本 Bot 的回复认领**，
#:    但前端能认出来显示成群友台词。
PEER_ACCOUNT_PREFIX = "peer."

#: ⚠️ **宿主给「Bot 自己发的消息」用的账号是字面量 ``self``**（不是 ``bot.<id>``）
#:    —— 实测（2026-11-26）：一号在群里的回复 ``user_id='self'``、``account_id='self'``、
#:    ``user_nickname='日向雏田'``。只按 ``bot.`` 前缀判会把 Bot 的回复全当"其它"丢掉。
HOST_SELF_ACCOUNT = "self"


def classify_message(message: Dict[str, Any]) -> Tuple[str, str, str]:
    """认一条消息 ⇒ (类别, 账号, 昵称)。

    类别：
    - ``bot``    —— 本 Bot 的回复（``self`` / 账号以 ``bot.`` 开头）
    - ``user``  —— 我方角色（账号以 ``user.`` 开头）
    - ``peer``  —— **别的 Bot 的台词**（账号以 ``peer.`` 开头，广播来的历史消息）
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
    if account.startswith(PEER_ACCOUNT_PREFIX):
        return "peer", account, nickname
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

    async def _broadcast_peer_reply(
        self, *, room_id: str, message_id: str, text: str, nickname: str
    ) -> int:
        """把**本 Bot 的回复**广播给房间里的其他 Bot（2026-11-28 串台修复）。

        ⚠️ **为什么需要**：每个 Bot 实例只有自己群流的副本——小樱回的话只进
        小樱实例的库，雏田实例的库里永远没有 ⇒ 雏田的 Planner **永远**
        看不见小樱说过什么（不是「同轮并行看不见」，是历史也完全没有）。
        ⇒ 收到回复后，往账本给其他 Bot 各排一条**纯历史投递**，
        由目标 Bot 自己的 worker 用自己的 gateway 投进它自己的群流，
        沉淀成 `user_id=peer.<bot_id>` 的历史消息（Planner 下次就能读到）。

        ⚠️ 用 `peer.` 前缀（不是 bot./self）⇒ 对方的 collector 认成 peer 类，
        **不会当自己的回复认领**（不推进波次/不标 collected）。
        ⚠️ delivery_id 由 message_id + target_bot_id 拼成固定值 ⇒ 幂等，
        重复 poll 不会重复广播。
        """
        if not room_id or not message_id or not text:
            return 0
        # ⚠️ 幂等**先查再写**：重复 poll 不该靠 UNIQUE 冲突兜底——
        #    那会在每次重复时打一条 IntegrityError warning（刷屏）。
        try:
            already = await self._ledger.run(
                lambda: self._ledger.has_delivery_prefix(f"peer_{message_id}_")
            )
            if already:
                return 0
        except Exception:  # noqa: BLE001
            pass   # 没这个接口就退回 UNIQUE 兜底（老版本兼容）

        def _do() -> int:
            room = self._ledger.get_room(room_id) or {}
            members = self._ledger.list_room_members(room_id) or []
            peers = [
                m for m in members
                if str(m.get("kind") or "") == "bot"
                and str(m.get("bot_id") or "") != self._bot_id
                and str(m.get("bot_id") or "")
            ]
            if not peers:
                return 0
            group_id = str(room.get("group_id") or "") or virtual_group_id(room_id)
            seq = self._ledger.next_delivery_seq()
            sender_account = f"{PEER_ACCOUNT_PREFIX}{self._bot_id}"
            count = 0
            for peer in peers:
                target = str(peer.get("bot_id") or "")
                delivery_id = f"peer_{message_id}_{target}"
                self._ledger.enqueue_delivery(
                    delivery_id=delivery_id,
                    target_bot_id=target,
                    content=text,
                    room_id=room_id,
                    segment_id="",
                    sender_member_id=str(peer.get("member_id") or ""),
                    sender_platform=DEFAULT_PLATFORM,
                    sender_account=sender_account,
                    sender_nickname=str(nickname or ""),
                    group_id=group_id,
                    group_name=str(room.get("name") or ""),
                    seq=seq,
                    turn_id="",   # 不参与接话门控/回复采集，只是历史消息
                    wave=0,
                )
                seq += 1
                count += 1
            # ⚠️ 账本只写**一条**（共享账本/前端时间线只显示一份）——
            #    按 message_id 去重，重复 poll 不会重复显示。
            #    gateway 投递给每个 Bot 是各自实例群流的事（Planner 历史）。
            if count:
                self._ledger.append_message({
                    "message_id": f"peer_msg_{message_id}",
                    "segment_id": "",
                    "room_id": room_id,
                    "speaker_member_id": sender_account,
                    "speaker_name": str(nickname or ""),
                    "kind": "peer",
                    "content": text,
                    "quote": "",
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            return count

        try:
            count = await self._ledger.run(_do)
            if count:
                self._log.info(
                    "[聊天室] 已广播对方台词给 %d 个 Bot（room=%s，%s：%s）",
                    count, room_id, nickname, text[:30],
                )
            return count
        except Exception as exc:  # noqa: BLE001
            self._log.warning("[聊天室] 广播对方台词失败（room=%s）：%r", room_id, exc)
            return 0

    async def _fetch(self, session_id: str) -> List[Dict[str, Any]]:
        """读一条群流的最近消息（**直连能力**，绕开 SDK 那层不带 hours 的封装）。

        ⚠️ **必须带超时**（实测 2026-11-26：一次 get_recent 卡了 114 秒才返回，
        期间整个回读循环被拖住 ⇒ 回复迟迟进不了账本）。超时就放弃这一轮，
        下一轮（2 秒后）再来 —— 反正消息还在宿主库里，丢不了。
        """
        self.rpc_idle.clear()
        try:
            result = await asyncio.wait_for(
                self._ctx.call_capability(
                    "message.get_recent",
                    chat_id=session_id,
                    limit=self._limit,
                    hours=self._hours,
                    filter_mai=False,   # ⚠️ 必须 False：过滤掉麦麦就认不出她的回复了
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            self._log.warning(
                "[聊天室] 回读 %s：宿主 15 秒没响应，本轮放弃（下一轮再取）", session_id[:12]
            )
            return []
        except Exception as exc:  # noqa: BLE001
            self._log.warning("[聊天室] 回读失败（session=%s）：%r", session_id[:12], exc)
            return []
        finally:
            self.rpc_idle.set()
        if isinstance(result, dict):
            messages = result.get("messages")
            return messages if isinstance(messages, list) else []
        return result if isinstance(result, list) else []

    @staticmethod
    def strip_quoted_prefix(text: str, quotes: List[str]) -> tuple:
        """剥掉回复消息开头的**被引原文**，返回 ``(新正文, 被引原文)``。

        陷阱 27：``processed_plain_text`` = 被引原文 + 空格 + 新正文 ——
        聊天流里引用是壳，账本里是拼在一起的。
        ⇒ 用「她可能引用的原文」做**最长前缀匹配**剥离，只留新正文，
        **同时把剥掉的原文返回**（2026-11-27 用户要引用提示：存进 message.quote，
        前端气泡上显示「回复了哪句」，不再像以前那样直接丢掉）。
        匹配不上就不动（宁可不剥也不能误伤正文），被引原文返回空串。
        """
        text = str(text or "")
        best = ""
        for quote in quotes or []:
            q = str(quote or "")
            if q and text.startswith(q) and len(q) > len(best):
                best = q
        if best:
            return text[len(best):].strip(), best.strip()
        return text, ""

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
        # ⚠️ **宽限期**（㉛，2026-11-27 实测）：Bot 思考可以远超 150 秒（回复器
        #    实测一轮 218 秒）——超时一标、门槛一关，她**迟到的回复就永远
        #    进不了时间线**。⇒ 超时后 30 分钟内的单仍算「要盯着的流」，
        #    迟到的回复照常入账（归属匹配不看状态，自动挂回正确的段）。
        try:
            grace = await self._ledger.run(
                self._ledger.recent_collect_targets, self._bot_id
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("[聊天室] 查宽限期投递失败：%r", exc)
            grace = []
        if not pending and not grace:
            return 0
        # ⚠️ **私聊回读**：待收投递里若有私聊单（group_id 为空、room 是 private_），
        #    它所在的群流其实是「专属私聊流」``md5(webui_<发言人账号>_private)``。
        #    这些 session 不在房间表里 ⇒ 从待收投递里派生出来一起采。
        sessions = dict(self._sessions)
        for row in list(pending) + list(grace):
            is_private = (
                str(row.get("group_id") or "") == ""
                and str(row.get("room_id") or "").startswith("private_")
            )
            if not is_private:
                continue
            account = str(row.get("sender_account") or "")
            if not account:
                continue
            sid = session_id_for_private(account)
            sessions.setdefault(sid, str(row.get("room_id") or ""))
        collected = 0
        for session_id, room_id in list(sessions.items()):
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
                # ⚠️ 跳过 F2 剧情时间的「种子消息」（2026-11-27）：
                #    add/back/set 带剧情提示词时，插件先投一条「（剧情时间已更新）」
                #    给 Planner 当 reply_target，否则它报「没有可回复的真实消息」。
                #    这条是机制用的占位，不是 Bot 台词 —— 不能当 reply 收进账本，
                #    否则前端会把它当「她回了」提前收尾，还显示成鹦鹉学舌的假台词。
                if message_text(message) == "（剧情时间已更新）":
                    continue
                kind, account, nickname = classify_message(message)
                # ⚠️ **别的 Bot 的台词（peer.）**：只写账本给前端显示，**不当回复认领**
                #    （不找归属投递/不标 collected/不接力）——否则会污染波次门控。
                if kind == "peer":
                    peer_id = str(message.get("message_id") or "")
                    peer_text = message_text(message)
                    if peer_id and peer_text:
                        await self._ledger.run(
                            self._ledger.append_message,
                            {
                                "message_id": peer_id,
                                "segment_id": "",
                                "room_id": room_id,
                                "speaker_member_id": account,
                                "speaker_name": nickname or account,
                                "kind": "peer",
                                "content": peer_text,
                                "quote": "",
                                "created_at": message_time(message),
                            },
                        )
                    continue
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
                quoted = ""     # 被引原文（剥壳时填；存进 message.quote 给前端画引用块）
                # 归属段：这条回复回应的是**哪一段**的投递
                # ⚠️ 用 find_delivery_for_reply（多返回 turn_id / wave）⇒
                #    S8 的接力要用「这条回复属于哪一轮、哪个波次」
                reply_delivery = await self._ledger.run(
                    self._ledger.find_delivery_for_reply,
                    target_bot_id=self._bot_id,
                    room_id=room_id,
                    reply_at=message_time(message),
                )
                # ⚠️ **剥引用壳**（陷阱 27）：回复消息的 processed_plain_text =
                #    被引原文 + 空格 + 新正文 —— 不剥的话，接力前缀和被引台词
                #    会整段拼进聊天室的消息里（实测症状：雏田的回复带着
                #    「这期间 春野樱 说：…」和整段被引原文）。
                if kind == "bot":
                    quotes = [str((reply_delivery or {}).get("content") or "")]
                    quotes.extend(
                        await self._ledger.run(
                            self._ledger.reply_quote_candidates, self._bot_id, room_id
                        )
                    )
                    text, quoted = self.strip_quoted_prefix(text, quotes)
                    if not text:
                        continue        # 剥完只剩引用 ⇒ 没有新正文可收
                segment_id = str((reply_delivery or {}).get("segment_id") or "")
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
                        "quote": quoted,
                        "created_at": message_time(message),
                    },
                )
                if written:
                    collected += 1
                    self._collected += 1
                    self._log.info(
                        "[聊天室] 收到回复（Bot %s）：%s", self._bot_id[:12], text[:40]
                    )
                    # ── 接力（S8）：串行/复合阵型下，把这句回复带给后续波次 ──
                    # ⚠️ 只有**新写进去**的才接力（append_message 已按 message_id 去重
                    #    ⇒ 重复 poll 时 written=False ⇒ 不会重复接力）。
                    reply_delivery_id = str((reply_delivery or {}).get("delivery_id") or "")
                    turn_id = str((reply_delivery or {}).get("turn_id") or "")
                    if turn_id:
                        wave = int((reply_delivery or {}).get("wave") or 0)
                        relayed = await self._ledger.run(
                            self._ledger.relay_reply_to_later_waves,
                            turn_id,
                            wave,
                            text,
                            nickname or member_id,
                        )
                        if relayed:
                            self._log.info(
                                "[聊天室] 已接力给后续波次（轮=%s，本波=%d，影响 %d 条）：%s",
                                turn_id[:16], wave, relayed, text[:30],
                            )
                    # ⚠️ **广播给房间其他 Bot**（2026-11-28 串台修复）：
                    #    让对方实例也把这句台词收进自己的群流 ⇒ 它的 Planner
                    #    以后能看见「春野樱说过什么」。用 peer. 账号 ⇒ 对方 collector
                    #    不会当自己的回复认领；turn_id="" ⇒ 不参与接话/波次。
                    #    失败只记日志，不影响本条回复的正常回收。
                    await self._broadcast_peer_reply(
                        room_id=room_id, message_id=message_id,
                        text=text, nickname=nickname or member_id,
                    )
                    # ── 精确标记这一条投递为已收回 ──
                    # ⚠️⚠️ **原来是「本 Bot 收到任何回复就把所有 delivered 都标 collected」
                    #    ⇒ 会在 Bot 还没真回复时**提前推进波次门控**（实测踩过：
                    #    小樱没回，她的投递却被标 collected，雏田的下一波被提前放行）。
                    #    ⇒ 改成**只标这一条**（回复实际归属到哪条投递就标哪条）；
                    #    没回复的投递留在 delivered，由超时兜底。
                    if reply_delivery_id:
                        await self._ledger.run(
                            self._ledger.mark_collected, reply_delivery_id
                        )
                        # ⚠️ **波次收口**（实测：串行阵型每过一个波次白等 150 秒）——
                        #    这个 Bot 开口了 ⇒ 它这一波其余没被单独回复的单一起收口，
                        #    别让它们熬满 150 秒超时才放行下一波。
                        _turn = str((reply_delivery or {}).get("turn_id") or "")
                        if _turn:
                            await self._ledger.run(
                                self._ledger.mark_wave_answered,
                                _turn,
                                int((reply_delivery or {}).get("wave") or 0),
                                self._bot_id,
                            )
        # ⚠️ **超时兜底**：递出去一直不回的投递要标超时，否则波次门控会卡死整轮。
        try:
            await self._ledger.run(
                self._ledger.timeout_stale_deliveries,
                self._bot_id,
                150.0,
            )
        except Exception:  # noqa: BLE001
            pass
        # ⚠️ **孤儿投递收尾**：被删房间的遮蔽旁白递出并终态后，把行清掉
        #    （用户拍板：删除房间的数据永远消失，不残留）。
        try:
            await self._ledger.run(self._ledger.cleanup_orphan_deliveries)
        except Exception:  # noqa: BLE001
            pass
        return collected
