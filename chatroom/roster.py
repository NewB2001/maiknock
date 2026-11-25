# -*- coding: utf-8 -*-
"""麦麦聊天室 · 受管 Bot 名单与**接入校验三层**（FR-10）。

**三层校验**（02 文档 8.4）：

| 层 | 查什么 | 不通过时 | 硬拦？ |
|---|---|---|---|
| **名单层** | 宿主受管 Bot 名单 + 运行状态 | 标 ``stopped`` | **拦** |
| **握手层** | 该 Bot 的 maim 网关 api_key 连通 | 标 ``gateway_off`` | **拦** |
| **风格层** | 说话风格是否已按敲门侧固化 | 标 ``style_unfixed`` | **不拦**（只提示） |

**为什么风格层不拦**：技术上能收到消息、只是说话风格飘，是**可以后修的**；
而名单层/握手层不通过就**根本收不到消息**，是死路。
02 文档 v0.5 用户定稿：**风格层只提示**。

**凭据隔离下的分工**（方案 B）：
- **名单层**由**每个实例都能读**的宿主 registry 提供（``hostinfo.read_bots``）；
- **握手层**由**每个 Bot 自己的插件实例**连自己的网关后**写进共享账本**
  （``bot_status`` 表）—— 别的 Bot 的 api_key 根本不该出现在本进程里；
- **风格层**同理：各实例自报「我的风格固化了没」。

⇒ 中控把 registry 与账本里的状态**合起来**算四态。

**纯逻辑模块**：无宿主导入 ⇒ 可离线单测。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# ── 四态词表（与页面 webui/index.html 的 STATUS_TEXT 对齐）────────────
STATUS_READY = "ready"              # 可接入
STATUS_GATEWAY_OFF = "gateway_off"  # 未启用网关
STATUS_STOPPED = "stopped"          # 未运行
STATUS_STYLE_UNFIXED = "style_unfixed"  # 风格未固化（**只提示不拦**）
STATUS_UNKNOWN = "unknown"          # 还没上报（实例还没起来）

STATUS_LABELS = {
    STATUS_READY: "可接入",
    STATUS_GATEWAY_OFF: "未启用网关",
    STATUS_STOPPED: "未运行",
    STATUS_STYLE_UNFIXED: "风格未固化",
    STATUS_UNKNOWN: "状态未知",
}

#: 严重度：数字越大越"不可用"。
#: ⚠️ **风格未固化排在 ready 前面、排在硬故障后面** —— 它不拦人，
#:    但要被标出来（02 文档 8.4 第 4 条的四态展示）。
_SEVERITY = {
    STATUS_READY: 0,
    STATUS_STYLE_UNFIXED: 1,
    STATUS_GATEWAY_OFF: 2,
    STATUS_STOPPED: 3,
    STATUS_UNKNOWN: 3,
}


def status_label(status: str) -> str:
    """状态 → 中文标签。"""
    return STATUS_LABELS.get(str(status or ""), STATUS_LABELS[STATUS_UNKNOWN])


def is_selectable(status: str) -> bool:
    """这个 Bot 能不能被拉进房间？

    ⚠️ **风格未固化也算可以** —— 风格层只提示不拦（02 文档 8.4）。
    """
    return str(status or "") in (STATUS_READY, STATUS_STYLE_UNFIXED)


def compute_status(
    *,
    in_registry: bool,
    running: bool,
    handshake_ok: Optional[bool],
    style_fixed: Optional[bool],
) -> str:
    """算出某个 Bot 的接入状态（四态）。

    - 不在 registry ⇒ ``stopped``（它压根没被宿主管着）
    - 在 registry 但没在跑 ⇒ ``stopped``
    - 在跑但握手没过（实例上报了、结果是失败）⇒ ``gateway_off``
    - 握手过了但风格没固化 ⇒ ``style_unfixed``（**不拦**）
    - 其余 ⇒ ``ready``

    ⚠️ ``handshake_ok`` / ``style_fixed`` 传 ``None`` 表示**"还没上报"**
    （那个 Bot 的实例还没起来）⇒ 按未通过处理，但状态标签是 ``unknown``
    而不是 ``gateway_off`` —— 别把"人家还没启动"说成"网关没开"。
    """
    if not in_registry or not running:
        return STATUS_STOPPED
    if handshake_ok is False:
        return STATUS_GATEWAY_OFF
    if handshake_ok is None:
        return STATUS_UNKNOWN
    if style_fixed is False:
        return STATUS_STYLE_UNFIXED
    return STATUS_READY


def build_roster(
    registry_bots: Iterable[Dict[str, Any]],
    statuses: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """把「宿主 registry」与「账本里各实例自报的状态」合成一份名单。

    ``registry_bots``：``hostinfo.read_bots()`` 的形状
    （bot_id / name / directory / desired_state）
    ``statuses``：``ledger.list_bot_status()`` 的形状
    （bot_id / status / detail / style_fixed）
    """
    reported = {str(row.get("bot_id") or ""): row for row in statuses}
    roster: List[Dict[str, Any]] = []
    for bot in registry_bots:
        bot_id = str(bot.get("bot_id") or "")
        row = reported.get(bot_id) or {}
        running = str(bot.get("desired_state") or "").strip().lower() == "running"
        handshake_ok = row.get("handshake_ok")
        style_fixed = row.get("style_fixed")
        status = compute_status(
            in_registry=True,
            running=running,
            handshake_ok=handshake_ok,
            style_fixed=style_fixed,
        )
        roster.append(
            {
                "bot_id": bot_id,
                "bot_name": str(bot.get("name") or ""),
                "directory": str(bot.get("directory") or "."),
                "running": running,
                "status": status,
                "status_label": status_label(status),
                "selectable": is_selectable(status),
                "style_fixed": style_fixed,
                "detail": str(row.get("detail") or ""),
                "reported": bool(row),
            }
        )
    roster.sort(key=lambda item: (not item["selectable"], item["bot_name"] or item["bot_id"]))
    return roster
