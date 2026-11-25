# -*- coding: utf-8 -*-
"""麦麦聊天室 · maim 网关客户端（跨 Bot 递话的**唯一**通道）。

**为什么手写而不复用 maim_message 的客户端**：那个库是给「一个客户端连多个平台」
设计的（自带路由、多连接管理、消息缓存、客户端工厂），而我们这边是
**一个 Bot 一条连接**、连接目标固定是宿主的 ``/maim/<bot_id>/api``。
手写两百行更直白、更好测，协议细节全部对着 maim_message 0.6.8 的源码逐条核过
（出处见各处注释）。

**协议速查**（出处：``maim_message/client_ws_api.py:210-220`` 与
``maim_message/server_ws_api.py:262-330``）::

    {
      "ver": 1,
      "msg_id": "msg_<12位hex>_<epoch>",     ← 必须唯一（服务端按它去重）
      "type": "sys_std",                      ← 必须是这个值，别的被当「未知消息类型」丢弃
      "meta": {"sender_user": api_key, "platform": platform, "timestamp": ...},
      "payload": {                            ← 必须是 APIMessageBase 的形状
         "message_info": {
            "platform": "webui", "message_id": ..., "time": ...,
            "sender_info": {                  ← ⚠️ 少了它麦麦就不认人
               "group_info": {"platform": "webui", "group_id": "webui_virtual_group_xxx"},
               "user_info":  {"platform": "webui", "user_id": "user.naruto"}
            }
         },
         "message_segment": {"type": "text", "data": "今天天气真好啊"},
         "message_dim": {"api_key": ..., "platform": ...}
      }
    }

⚠️ **认证走 header，不走 URL** —— 服务端是「查询参数优先、header 兜底」
（``maim_message/server_ws_connection.py:288-289``），而宿主代理会转发
``x-apikey`` / ``x-platform`` / ``x-uuid`` 三个头
（``src/host/maim.py:27``）⇒ 用 header 就够，**api_key 不会进 URL、不进日志**。

**纯逻辑部分**（``build_envelope``）无宿主导入 ⇒ 可离线单测。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .identity import DEFAULT_PLATFORM, new_message_id

logger = logging.getLogger("plugin.maiknock.webui_companion")

#: 新协议信封的 type —— 服务端只认这个值（server_ws_api.py:281-290）
ENVELOPE_TYPE = "sys_std"

#: 新协议信封的版本号（client_ws_api.py:211）
ENVELOPE_VERSION = 1


def build_envelope(
    text: str,
    *,
    platform: str = DEFAULT_PLATFORM,
    user_id: str,
    user_nickname: str = "",
    group_id: str = "",
    group_name: str = "",
    api_key: str = "",
    message_id: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> Dict[str, Any]:
    """构造一条新协议消息的信封（**纯函数**）。

    ⚠️ 三个必守的约定（错了服务端会静默丢弃或当成纯文本）：
    1. ``type`` 必须是 ``sys_std`` —— 别的值走「未知消息类型」分支被丢
       （server_ws_api.py:281-290）；
    2. ``msg_id`` 必须唯一 —— 服务端按它去重，重复的直接忽略（:270-278）；
    3. ``payload`` 必须带 ``message_info`` + ``message_segment`` —— 否则服务端
       把它**包成纯文本**（丢掉 sender_info ⇒ 麦麦不认这条消息是谁发的，
       :303-319）。
    """
    now = float(timestamp if timestamp is not None else time.time())
    msg_id = str(message_id or new_message_id())
    user_info: Dict[str, Any] = {"platform": platform, "user_id": str(user_id)}
    if user_nickname:
        user_info["user_nickname"] = str(user_nickname)
    group_info: Dict[str, Any] = {"platform": platform, "group_id": str(group_id)}
    if group_name:
        group_info["group_name"] = str(group_name)

    return {
        "ver": ENVELOPE_VERSION,
        "msg_id": msg_id,
        "type": ENVELOPE_TYPE,
        "meta": {
            "sender_user": str(api_key),
            "platform": platform,
            "timestamp": now,
        },
        "payload": {
            "message_info": {
                "platform": platform,
                "message_id": msg_id,
                "time": now,
                "format_info": {"content_format": ["text"], "accept_format": ["text"]},
                "sender_info": {
                    "group_info": group_info,
                    "user_info": user_info,
                },
            },
            "message_segment": {"type": "text", "data": str(text)},
            "message_dim": {"api_key": str(api_key), "platform": platform},
        },
    }


class MaimConnection:
    """到**某个 Bot** 的一条网关连接。

    **一个 Bot 一条连接** —— 02 文档 8.5：一条连接只服务一个 Bot，
    断开也不改连别的 Bot（宿主代理那边也是这个语义，``src/host/maim.py:64-73``）。
    """

    def __init__(
        self,
        bot_id: str,
        api_key: str,
        *,
        host: str = "127.0.0.1",
        port: int = 8001,
        platform: str = DEFAULT_PLATFORM,
        log: Optional[logging.Logger] = None,
        keepalive_interval: float = 60.0,
    ) -> None:
        self.bot_id = str(bot_id)
        self.api_key = str(api_key)
        self.host = str(host)
        self.port = int(port)
        self.platform = str(platform)
        self._log = log or logger
        self._ws: Any = None
        self._connected = False
        self._closing = False
        self._task: Optional[asyncio.Task] = None
        self._last_error: str = ""
        self._keepalive_interval = float(keepalive_interval)
        self._keepalive_task: Optional[asyncio.Task] = None

    # ---- 状态 ----

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str:
        return self._last_error

    def endpoint(self) -> str:
        """网关地址（**不带 api_key** —— 认证走 header，见模块 docstring）。"""
        return f"ws://{self.host}:{self.port}/maim/{self.bot_id}/api"

    def _headers(self) -> Dict[str, str]:
        """连接头。

        ⚠️ 宿主代理只转发这几个头（``src/host/maim.py:27``）：
        ``authorization`` / ``platform`` / ``x-apikey`` / ``x-platform`` / ``x-uuid``。
        """
        return {
            "x-apikey": self.api_key,
            "x-platform": self.platform,
            "x-uuid": f"maichat-{self.bot_id}",
        }

    # ---- 连接生命周期 ----

    async def connect(self) -> bool:
        """建立连接。成功返回 True。

        ⚠️ 失败**不抛异常** —— 返回 False 并记 ``last_error``，
        让调用方把它变成"这个 Bot 暂时不可用"的状态，而不是把插件带崩。
        """
        if self._connected:
            return True
        if not self.api_key:
            self._last_error = "没有配置 api_key"
            self._log.warning("[聊天室] Bot %s 无法连接：没有配置 api_key", self.bot_id[:12])
            return False
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            self._last_error = f"websockets 不可用：{exc}"
            self._log.error("[聊天室] Bot %s 无法连接：%s", self.bot_id[:12], self._last_error)
            return False
        try:
            self._ws = await websockets.connect(
                self.endpoint(),
                additional_headers=self._headers(),
                ping_interval=20,
                ping_timeout=20,
                open_timeout=10,
                max_size=2 ** 20,
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._log.warning("[聊天室] Bot %s 网关握手失败：%s", self.bot_id[:12], self._last_error)
            self._ws = None
            return False
        self._connected = True
        self._last_error = ""
        self._log.info("[聊天室] 已连上 Bot %s 的消息网关（%s）", self.bot_id[:12], self.endpoint())
        return True

    async def close(self) -> None:
        """主动断开。"""
        self._closing = True
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None and not task.done():
            task.cancel()
        ws, self._ws = self._ws, None
        self._connected = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- 保活（心跳）---------------------------------------------------

    async def start_keepalive(self) -> None:
        """起后台保活循环：连着就 ping，断了就重连。

        ⚠️ **连着就是心跳**（02 文档 8.4 第 5 条）—— 不需要额外的探活协议。
        ⚠️ 保活循环挂在插件的 on_unload 上关闭（避免孤儿任务）。
        """
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        self._closing = False
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        # ⚠️⚠️ **没连上时要「快一点重试」** ——
        #    插件的 on_load 比宿主的消息服务启动早 ⇒ 第一次握手通常失败。
        #    若一律等 60 秒才重试，Bot 会有整整一分钟「在但不能用」
        #    （实测：二号重启后一直 403，而网关其实几秒后就绪了）。
        #    ⇒ 连不上时用 3 秒起步的退避，连上后回到正常心跳间隔。
        retry_delay = 3.0
        while not self._closing:
            try:
                await asyncio.sleep(self._keepalive_interval if self._connected else retry_delay)
            except asyncio.CancelledError:
                return
            if self._closing:
                return
            try:
                if self._connected:
                    await self.ping_once()
                else:
                    if await self.connect():
                        retry_delay = 3.0
                    else:
                        # 指数退避（封顶 30 秒）⇒ 别把日志刷爆
                        retry_delay = min(retry_delay * 2, 30.0)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._log.warning("[聊天室] 保活异常（Bot %s）：%s", self.bot_id[:12], self._last_error)

    # ---- 发消息 ----

    async def send_text(
        self,
        text: str,
        *,
        user_id: str,
        user_nickname: str = "",
        group_id: str = "",
        group_name: str = "",
    ) -> bool:
        """递一条文本进对方的群流。成功返回 True。

        ⚠️ 群聊里同一句台词要**递进每个在场 Bot 各自的群流**
        （02 文档 FR-07）—— 由调用方循环，本类只管"递一条给这个 Bot"。
        """
        if not self._connected or self._ws is None:
            return False
        envelope = build_envelope(
            text,
            platform=self.platform,
            user_id=user_id,
            user_nickname=user_nickname,
            group_id=group_id,
            group_name=group_name,
            api_key=self.api_key,
        )
        try:
            await self._ws.send(json.dumps(envelope, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._log.warning("[聊天室] 递给 Bot %s 失败：%s", self.bot_id[:12], self._last_error)
            self._connected = False
            return False
        self._log.info(
            "[聊天室] 已递给 Bot %s：%s", self.bot_id[:12], (text or "")[:40]
        )
        return True

    # ---- 心跳 ----

    async def ping_once(self) -> bool:
        """探一次连通性。**连着就是心跳**（02 文档 8.4 第 5 条）。"""
        if not self._connected or self._ws is None:
            return False
        try:
            pong = await self._ws.ping()
            await asyncio.wait_for(pong, timeout=10)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._connected = False
            return False
        return True


class GatewayPool:
    """管理到多个 Bot 的连接（一个 Bot 一条）。

    ⚠️ **凭据隔离**：每个 Bot 的 ``api_key`` 只在**它自己那个 Bot 的插件配置**里，
    所以本池在方案 B（各 Bot 代理实例各递各的）下**只会有一条连接**——自己的。
    保留多 Bot 的能力是为了将来切到方案 C（中控集中投递）。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8001,
        platform: str = DEFAULT_PLATFORM,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.platform = str(platform)
        self._log = log or logger
        self._conns: Dict[str, MaimConnection] = {}

    def set_api_key(self, bot_id: str, api_key: str) -> None:
        """登记某个 Bot 的 key（后续 ``get`` 时用它建连）。"""
        conn = self._conns.get(bot_id)
        if conn is None:
            conn = MaimConnection(
                bot_id, api_key, host=self.host, port=self.port,
                platform=self.platform, log=self._log,
            )
            self._conns[bot_id] = conn
        else:
            conn.api_key = str(api_key)

    def connection(self, bot_id: str) -> Optional[MaimConnection]:
        """拿连接对象（**不尝试连接**）。

        ⚠️ **为什么要有这个**：`get()` 建连失败时返回 ``None``，
        调用方就拿不到连接对象 ⇒ **保活循环起不来** ⇒ 永远连不上。
        插件的 on_load 比宿主的消息服务启动早，第一次握手**大概率失败**，
        必须能「先拿到对象、让保活循环去重连」。
        """
        return self._conns.get(bot_id)

    async def get(self, bot_id: str) -> Optional[MaimConnection]:
        """拿到（或建）到某个 Bot 的连接。建不成功返回 None。"""
        conn = self._conns.get(bot_id)
        if conn is None:
            return None
        if not conn.connected:
            if not await conn.connect():
                return None
        return conn

    async def send_text(self, bot_id: str, text: str, **kwargs: Any) -> bool:
        """递一条文本给某个 Bot。"""
        conn = await self.get(bot_id)
        if conn is None:
            return False
        return await conn.send_text(text, **kwargs)

    def status(self) -> List[Dict[str, Any]]:
        """各 Bot 的连接状态（接入校验的握手层用这个）。"""
        out: List[Dict[str, Any]] = []
        for bot_id, conn in self._conns.items():
            out.append(
                {
                    "bot_id": bot_id,
                    "connected": conn.connected,
                    "error": conn.last_error,
                }
            )
        return out

    async def close(self) -> None:
        """全部断开。"""
        for conn in list(self._conns.values()):
            await conn.close()
        self._conns.clear()


def probe_endpoint(bot_id: str, host: str = "127.0.0.1", port: int = 8001) -> str:
    """某个 Bot 的网关地址（给日志/报错用）。"""
    return f"ws://{host}:{port}/maim/{bot_id}/api"
