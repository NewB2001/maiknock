# -*- coding: utf-8 -*-
"""麦麦聊天室 · 投递 worker（领单 → 递话 → 回填状态）。

**这是什么**：每个 Bot 的插件实例里跑的一个后台循环，
做的是整个聊天室里**唯一**真正"把话递出去"的动作。

**为什么是「领单」而不是「中控直发」**（方案 B，08 ADR-13）：
- 每个 Bot 的实例只知道**自己**的 api_key ⇒ 只能递给自己；
- 中控不碰别的 Bot 的通道 ⇒ 跨 Bot 的协调靠**共享账本**里的投递任务；
- 各实例各领各的单 ⇒ 谁都在跑，谁都能接。

**串行**：一个实例**一次只处理一条** ⇒ 同一 Bot 的多条台词天然按顺序递
（FR-14：点名 = 按勾选顺序全串行接话）。
**跨 Bot 的严格串行**（等 A 说完再给 B）属于调度层（S8）。

**重试**：网关抖一下不该把这句话丢了 ⇒ 递话失败先**放回待办**，
超过次数上限才标超时（看得见"这句没递出去"）。

**纯逻辑**：只吃 ledger / gateway 两个对象，不直接依赖宿主 ⇒ 可离线单测。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

_log = logging.getLogger("plugin.maiknock.webui_companion")

#: 领单间隔（秒）。太小会空转刷日志，太大会让"点了发送"迟迟没反应。
DEFAULT_POLL_INTERVAL = 2.0

#: 递话失败最多重试几次（超过就标超时）
DEFAULT_MAX_ATTEMPTS = 3


class DeliveryWorker:
    """投递 worker：把账本里派给我的台词递进我的虚拟群。"""

    def __init__(
        self,
        ledger: Any,
        gateway: Any,
        bot_id: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._ledger = ledger
        self._gateway = gateway
        self._bot_id = str(bot_id)
        self._poll_interval = float(poll_interval)
        self._max_attempts = int(max_attempts)
        self._log = log or _log
        self._stopping = False
        self._task: Optional[asyncio.Task] = None
        self._delivered = 0

    # ---- 统计 ----

    @property
    def delivered_count(self) -> int:
        """已成功递出的条数（本实例）。"""
        return self._delivered

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- 主循环 ----

    async def deliver_once(self) -> int:
        """处理**一条**待办。返回 1 = 递出去了；0 = 没有或失败。

        ⚠️ **一次只领一条** —— 这是同一 Bot 内串行的保证。
        """
        try:
            pendings = self._ledger.pending_deliveries(self._bot_id, limit=1)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("[聊天室] 领单失败（Bot %s）：%r", self._bot_id[:12], exc)
            return 0
        if not pendings:
            return 0
        task: Dict[str, Any] = pendings[0]
        delivery_id = str(task.get("delivery_id") or "")
        # ⚠️ 抢占是**原子**的（条件 UPDATE）⇒ 两个实例不会递同一条
        if not self._ledger.claim_delivery(delivery_id, self._bot_id):
            return 0
        content = str(task.get("content") or "")
        try:
            ok = await self._gateway.send_text(
                self._bot_id,
                content,
                user_id=str(task.get("sender_account") or ""),
                user_nickname=str(task.get("sender_nickname") or ""),
                group_id=str(task.get("group_id") or ""),
                group_name=str(task.get("group_name") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            ok = False
            self._log.warning(
                "[聊天室] 递话给 Bot %s 时出错：%r", self._bot_id[:12], exc
            )
        if ok:
            self._delivered += 1
            self._log.info(
                "[聊天室] 已递话（Bot %s）：%s", self._bot_id[:12], content[:40]
            )
            return 1
        # 失败 ⇒ 放回待办（次数用尽才标超时）
        try:
            requeued = self._ledger.requeue_delivery(delivery_id, self._max_attempts)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("[聊天室] 回填投递状态失败：%r", exc)
            return 0
        if requeued:
            self._log.warning(
                "[聊天室] 递话失败，已放回待办（Bot %s）：%s", self._bot_id[:12], content[:40]
            )
        else:
            self._log.warning(
                "[聊天室] 递话连续失败 %d 次，标记超时（Bot %s）：%s",
                self._max_attempts, self._bot_id[:12], content[:40],
            )
        return 0

    async def run_forever(self) -> None:
        """后台循环：每隔 ``poll_interval`` 领一条单。"""
        self._stopping = False
        while not self._stopping:
            try:
                await self.deliver_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self._log.warning("[聊天室] 投递循环异常（Bot %s）：%r", self._bot_id[:12], exc)
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                return

    def start(self) -> None:
        """起后台循环（幂等）。"""
        if self.running:
            return
        self._stopping = False
        self._task = asyncio.create_task(self.run_forever())
        self._log.info("[聊天室] 投递循环已启动（Bot %s）", self._bot_id[:12])

    async def stop(self) -> None:
        """停后台循环（热重载 / 卸载时必须停，否则成孤儿任务）。"""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        self._log.info("[聊天室] 投递循环已停止（Bot %s）", self._bot_id[:12])
