# -*- coding: utf-8 -*-
"""麦麦聊天室 · 导出（纯逻辑，可离线单测）。

**要解决的问题**：把一段剧情**完整**导出成 Markdown / JSON —— 剧本存档已经落在共享账本
（`segment` + `message`）里，含已作废的段（否则时间线对不上账）。

**两条纪律**：
1. **含已作废的段**：导出的是一份「真实发生过什么」的底账，作废段要标出来（`[已作废]`），
   不能悄悄丢掉 —— 否则用户拿到的记录比实际少，对不上账。
2. **绝不捎带凭据**：`api_key` 等**绝不能**出现在导出里。账本时间线本来就不含它，
   这里也只从「段/消息」取字段，不做任何额外的字典透传。

**纯逻辑模块**：只依赖标准库 ⇒ 可离线单测。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

#: 消息类型 → 中文标签（导出可读性）
_KIND_LABEL = {
    "line": "台词",
    "reply": "回复",
    "narrator": "旁白",
    "sticker": "表情",
}


def _message_line(message: Dict[str, Any]) -> str:
    """一条消息 → Markdown 行（`时间 发言人：正文`）。"""
    speaker = str(message.get("speaker_name") or message.get("speaker_member_id") or "（未知）")
    time = str(message.get("time") or message.get("created_at") or "")
    content = str(message.get("content") or "")
    kind = str(message.get("kind") or "")
    tag = _KIND_LABEL.get(kind, "")
    prefix = f"**{speaker}**" + (f"（{tag}）" if tag else "")
    if time:
        prefix = f"`{time}` {prefix}"
    return f"{prefix}：{content}"


def build_markdown(
    timeline: List[Dict[str, Any]],
    room_name: str = "",
    room_id: str = "",
) -> str:
    """时间线（段列表）→ Markdown 文本。

    ⚠️ **含已作废段**，并在标题里标 `[已作废]`（保留账目完整性）。
    """
    header = f"# {room_name or room_id or '聊天室'} · 剧情导出\n"
    if room_id and room_name:
        header += f"\n> 房间：{room_name}（`{room_id}`）\n"
    parts: List[str] = [header]
    for segment in timeline or []:
        seq = segment.get("seq", "?")
        voided = bool(segment.get("voided"))
        kind = str(segment.get("kind") or "normal")
        created = str(segment.get("created_at") or "")
        title = f"## 第 {seq} 段"
        if kind == "plot":
            title += "（剧情旁白）"
        if voided:
            title += " [已作废]"
        if created:
            title += f" · {created}"
        parts.append(title + "\n")
        messages = segment.get("messages") or []
        if not messages:
            parts.append("_（空段）_\n")
            continue
        for message in messages:
            parts.append("- " + _message_line(message))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def build_json(
    timeline: List[Dict[str, Any]],
    room_name: str = "",
    room_id: str = "",
) -> str:
    """时间线 → JSON 文本（结构化，含 voided 标记）。

    ⚠️ **只挑白名单字段** —— 绝不做整字典透传，避免捎带 api_key 之类的凭据。
    """
    segments: List[Dict[str, Any]] = []
    for segment in timeline or []:
        segments.append(
            {
                "seq": segment.get("seq"),
                "kind": segment.get("kind") or "normal",
                "voided": bool(segment.get("voided")),
                "created_at": segment.get("created_at") or "",
                "messages": [
                    {
                        "speaker_name": m.get("speaker_name") or "",
                        "speaker_member_id": m.get("speaker_member_id") or "",
                        "kind": m.get("kind") or "",
                        "content": m.get("content") or "",
                        "time": m.get("time") or m.get("created_at") or "",
                    }
                    for m in (segment.get("messages") or [])
                ],
            }
        )
    payload = {
        "room_id": room_id,
        "room_name": room_name,
        "segments": segments,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def safe_filename(room_name: str, room_id: str, ext: str) -> str:
    """生成安全的文件名（去掉路径分隔符等）。"""
    base = (room_name or room_id or "chatroom").strip()
    for ch in '/\\:*?"<>|':
        base = base.replace(ch, "_")
    base = base.replace(" ", "_") or "chatroom"
    return f"{base}.{ext}"
