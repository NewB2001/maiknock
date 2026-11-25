# -*- coding: utf-8 -*-
"""麦麦聊天室 · 错误扫描（把宿主的硬错误变成用户看得见的提示）。

**为什么需要这个**：宿主在 LLM 调用失败时（429 限流、超时、上游 5xx……）
会把完整快照写进**它自己实例**的
``<实例根>/logs/maisaka_prompt/llm_error/<会话目录>/<时间戳>_<请求id>.json``，
但**界面上什么都不显示** —— 聊天室的 Bot 就是"沉默"。

实测（2026-11-25）：两个 Bot 差 1 秒同时递话 ⇒ 上游返回
``429 - Request was rejected due to rate limiting. Details: RPM limit reached.``
⇒ 一个 Bot 重试后成功、另一个重试次数用尽 ⇒ **它一句都没说**，
而聊天室里只有一片安静。

⇒ **本模块**把这件事捡起来：扫快照 → 认出错 → 写进共享账本 → 中控推给前端。

**分工（跟「各 Bot 自报接入状态」同构）**：
- **每个 Bot 的插件实例只扫自己实例的日志目录**（一号在 ``<ws>/logs/``，
  二号在 ``<ws>/bots/<id>/logs/``）—— 各扫各的，**不越界读别人的**；
- 扫到的错误写进**共享账本** ⇒ 中控（一号）汇总时**二号的错也看得见**；
- 中控把未确认的错误推给前端 ⇒ 用户在标题行的提示栏看到红字。

**判定依据**（从快照里取，不猜）：
- ``generation_attempts[-1].error.type`` = ``RateLimitError`` ⇒ 限流
- ``...error.status_code`` = 429 ⇒ 限流
- ``...error.message`` 含 ``RPM limit`` / ``rate limiting`` ⇒ 限流
- 其余非空错误 ⇒ 上游错误

**纯逻辑模块**：不依赖宿主，只吃文件系统 ⇒ 可离线单测。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_log = logging.getLogger("plugin.maiknock.webui_companion")

#: 宿主把失败快照写在哪儿（相对实例根）
LLM_ERROR_SUBDIR = ("logs", "maisaka_prompt", "llm_error")

#: 快照文件的扩展名（``.events.jsonl`` 是事件流，不是摘要）
SNAPSHOT_SUFFIX = ".json"

#: 限流的判定关键词
_RATE_MARKERS = ("rpm limit", "rate limit", "too many requests", "请求过于频繁")


def instance_root_from_data_dir(data_dir: Path) -> Path:
    """从插件的 ``data_dir`` 反推**实例根**。

    插件的 data_dir = ``<实例根>/data/plugins/<plugin_id>`` ⇒ 上溯三级。
    ⚠️ 这依赖宿主的目录布局（``ctx.paths.data_dir`` 的约定），
    推导不出就返回 None（调用方据此跳过扫描，**不猜路径**）。
    """
    try:
        resolved = Path(data_dir).resolve()
    except OSError:
        return None
    # <root>/data/plugins/<id>  ⇒  <root> = parents[2]
    parents = resolved.parents
    if len(parents) < 3:
        return None
    root = parents[2]
    # 自检：root 下应该有 data/ 和 logs/（logs 可以还没有）
    if not (root / "data").is_dir():
        return None
    return root


def llm_error_dir(root: Path) -> Path:
    """实例根 → 宿主 LLM 错误快照目录。"""
    return Path(root).joinpath(*LLM_ERROR_SUBDIR)


def _classify(error: Dict[str, Any]) -> str:
    """错误 → 类别。"""
    etype = str(error.get("type") or "").lower()
    code = int(error.get("status_code") or 0)
    message = str(error.get("message") or "").lower()
    if etype == "ratelimiterror" or code == 429:
        return "rate_limit"
    if any(marker in message for marker in _RATE_MARKERS):
        return "rate_limit"
    if code >= 500:
        return "upstream_error"
    if code:
        return "upstream_error"
    return "error"


def parse_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    """读一份失败快照，抽出我们要的字段。读不出来返回 None（跳过，不报错）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    attempts = data.get("generation_attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    last = attempts[-1] if isinstance(attempts[-1], dict) else {}
    error = last.get("error") if isinstance(last.get("error"), dict) else {}
    if not error:
        return None
    message = str(error.get("message") or "").strip()
    if not message:
        return None
    return {
        "report_id": path.name,
        "kind": _classify(error),
        "status_code": int(error.get("status_code") or 0),
        "error_type": str(error.get("type") or ""),
        "provider": str(metadata.get("provider_name") or last.get("provider") or ""),
        "model": str(metadata.get("model_name") or last.get("model") or ""),
        "task": str(metadata.get("task_name") or ""),
        "session_id": str(metadata.get("session_id") or ""),
        "created_at": str(metadata.get("created_at") or ""),
        "message": message,
    }


def scan_error_dir(
    root: Path,
    *,
    session_ids: Optional[Iterable[str]] = None,
    since_ts: float = 0.0,
) -> List[Dict[str, Any]]:
    """扫一个实例的 llm_error 目录，返回**新出现**的错误摘要。

    - ``session_ids``：只收这些会话的错（**虚拟群**的 session id）；
      传 None = 全收。
    - ``since_ts``：只收这个时间点之后**新增**的文件（增量扫描）。
    """
    base = llm_error_dir(root)
    if not base.is_dir():
        return []
    wanted = {str(item) for item in session_ids} if session_ids is not None else None
    found: List[Dict[str, Any]] = []
    try:
        candidates = sorted(base.rglob(f"*{SNAPSHOT_SUFFIX}"))
    except OSError:
        return []
    for path in candidates:
        try:
            if path.name.endswith(".events.jsonl"):
                continue
            if since_ts and path.stat().st_mtime <= since_ts:
                continue
        except OSError:
            continue
        item = parse_snapshot(path)
        if item is None:
            continue
        if wanted is not None and item["session_id"] not in wanted:
            continue
        found.append(item)
    return found


def describe_report(report: Dict[str, Any], bot_name: str = "") -> str:
    """把一条错误摘要写成**给用户看的白话**。

    ⚠️ 用户要的是「别让用户觉得插件在默默出错」⇒ 措辞要说清
    **谁**出的问题、**为什么**、**怎么办**。
    """
    who = bot_name or str(report.get("bot_name") or "") or "某个 Bot"
    kind = str(report.get("kind") or "")
    provider = str(report.get("provider") or "")
    model = str(report.get("model") or "")
    detail = str(report.get("message") or "")
    if kind == "rate_limit":
        tip = (
            "上游触发了速率限流（同一账号下的多个 Bot 共用一份配额，"
            "换 API Key 不管用 —— 要让它们用不同的模型或不同的服务商）"
        )
        head = f"{who} 回复失败：被上游限流了（429）"
    else:
        tip = "上游调用失败"
        head = f"{who} 回复失败：上游调用出错"
    parts = [head]
    if provider or model:
        parts.append(f"（走的是 {provider or '未知服务商'} 的 {model or '未知模型'}）")
    if tip:
        parts.append("→ " + tip)
    if detail:
        parts.append(f"原始报错：{detail[:120]}")
    return " ".join(parts)


class ErrorWatcher:
    """错误扫描器（每个 Bot 的插件实例各持有一个，只扫自己那份日志）。"""

    def __init__(
        self,
        data_dir: Optional[Path],
        bot_id: str = "",
        bot_name: str = "",
        *,
        interval: float = 5.0,
    ) -> None:
        self._data_dir = Path(data_dir) if data_dir else None
        self._bot_id = str(bot_id or "")
        self._bot_name = str(bot_name or "")
        self._interval = float(interval)
        self._root = instance_root_from_data_dir(self._data_dir) if self._data_dir else None
        self._last_ts = time.time()
        self._reported = 0

    @property
    def root(self) -> Optional[Path]:
        """推导出的实例根（推不出就是 None ⇒ 不扫）。"""
        return self._root

    def poll(
        self, ledger: Any, session_ids: Optional[Iterable[str]] = None
    ) -> List[Dict[str, Any]]:
        """扫一次，把新错误写进账本，返回新写入的报告。"""
        if self._root is None or ledger is None:
            return []
        try:
            items = scan_error_dir(self._root, session_ids=session_ids, since_ts=self._last_ts)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[聊天室] 扫描错误快照失败：%r", exc)
            return []
        if not items:
            # 没有新文件也要推进时间戳，否则会反复扫同一批旧文件
            self._last_ts = time.time()
            return []
        written: List[Dict[str, Any]] = []
        for item in items:
            payload = dict(item)
            payload["bot_id"] = self._bot_id
            payload["bot_name"] = self._bot_name
            try:
                if ledger.report_error(payload):
                    written.append(payload)
                    self._reported += 1
            except Exception as exc:  # noqa: BLE001
                _log.warning("[聊天室] 写错误报告失败：%r", exc)
        self._last_ts = time.time()
        if written:
            _log.warning(
                "[聊天室] 发现 %d 条上游错误（Bot %s），已登记待提示",
                len(written), self._bot_id[:12],
            )
        return written
