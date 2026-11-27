# -*- coding: utf-8 -*-
"""麦麦聊天室 · Bot 头像读取（2026-11-28）。

**问题**：宿主 WebUI 里给 Bot 设的头像显示在聊天室里却是默认 emoji。

**根因**（查过宿主前端）：Bot 头像的 user_id 是 `webui_bot_<crypto.randomUUID()>`，
**随机 UUID 只存在设置它的那个浏览器的 localStorage 里** ⇒ 插件无法从 bot_id
推导出头像 URL。但头像文件本身就在磁盘上：
``<实例根>/data/avatar/internal/webui_bot_<uuid>.<suffix>``，且**每个 Bot 实例只有一个**。

**解法**：每个 Bot 实例扫自己的 ``data/avatar/internal/webui_bot_*`` 文件，
读成 base64 data URI，写进共享账本的 bot_status.avatar 列（跟状态自报同款：
各 Bot 写自己那一行，中控 bootstrap 时读全部）⇒ 前端 ``fillAvatar`` 直接吃 data URI。

⚠️ 只认 ``webui_bot_*``（Bot 专属头像），不认 ``owner.png``（主人头像）或
   其它 ``webui_<user>``（WebUI 本地用户头像）——避免把错的图当 Bot 头像。

**纯逻辑**：只依赖标准库 ⇒ 可离线单测。
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

_log = logging.getLogger("plugin.maiknock.webui_companion")

#: Bot 专属头像文件前缀（宿主 WebUI 前端定的）
BOT_AVATAR_PREFIX = "webui_bot_"

#: 支持的图片后缀（跟宿主 webui/routers/avatar.py 一致）
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")

#: 上限 5 MiB（跟宿主 MAX_AVATAR_BYTES 一致）；超了不读
MAX_AVATAR_BYTES = 5 * 1024 * 1024

#: ⚠️ 真机实测（2026-11-28）：宿主存的 Bot 头像是**用户上传的原图 2.1~2.5 MB**！
#:    bootstrap 有 512 KiB 硬上限 ⇒ 原图 data URI 塞进去会直接把页面搞崩。
#:    ⇒ 必须**缩到 96x96 JPEG** 再 embed（通常 3~8 KB，base64 后 4~11 KB）。
AVATAR_SIZE = (96, 96)
JPEG_QUALITY = 78
#: 压缩后 data URI 上限（base64 后 ~27 KB）；两个 Bot 加起来 < 60 KB，bootstrap 安全
MAX_EMBED_BYTES = 20 * 1024


def find_bot_avatar_path(instance_root: Path) -> Optional[Path]:
    """找这个 Bot 实例的专属头像文件（``data/avatar/internal/webui_bot_*``）。

    多个候选时取**最近修改**的那个（用户可能传过多次，宿主可能残留旧文件）。
    找不到返回 None，**不抛异常**。
    """
    try:
        avatar_dir = Path(instance_root) / "data" / "avatar" / "internal"
        if not avatar_dir.is_dir():
            return None
        candidates = [
            p for p in avatar_dir.iterdir()
            if p.is_file()
            and p.name.startswith(BOT_AVATAR_PREFIX)
            and p.suffix.lower() in IMAGE_SUFFIXES
            and p.stat().st_size <= MAX_AVATAR_BYTES
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def read_avatar_data_uri(path: Path) -> Optional[str]:
    """把头像文件**缩到 96x96 JPEG**后读成 ``data:image/jpeg;base64,...``。

    ⚠️ 为什么要缩：真机头像是用户上传的原图 2.1~2.5 MB，而 bootstrap 硬上限
       512 KiB ⇒ 不缩就崩页面（2026-11-28 实测踩过）。
    ⚠️ Pillow 惰性导入：没装就返回 None（回落默认 emoji，不影响页面）。
    """
    try:
        size = path.stat().st_size
        if not size or size > MAX_AVATAR_BYTES:
            return None
        from PIL import Image  # noqa: PLC0415 — 惰性，避免无 Pillow 环境直接炸
        import io  # noqa: PLC0415
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail(AVATAR_SIZE, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            raw = buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        _log.debug("[聊天室] 头像缩图失败（回落默认）：%s: %r", path, exc)
        return None
    if len(raw) > MAX_EMBED_BYTES:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def load_bot_avatar(instance_root: Path) -> Optional[str]:
    """一步到位：找头像文件 → 读成 data URI。读不到返回 None。"""
    path = find_bot_avatar_path(instance_root)
    if path is None:
        return None
    uri = read_avatar_data_uri(path)
    if uri is None:
        _log.warning("[聊天室] Bot 头像过大或读不出，回落默认头像：%s", path)
    return uri
