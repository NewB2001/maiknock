# -*- coding: utf-8 -*-
"""麦麦聊天室子包（并入麦麦敲门插件）。

**这是什么**：聊天室的中控 —— 挑人、写台词、递话、收话、管房间。
**和敲门的关系**：敲门是「能力内核」（小说体怎么写、剧情时间怎么算），
聊天室是「操作界面」（让人挑人写词看记录）。合并成一个插件后，
同一个插件同时提供这两样能力，**不需要插件间通信**（宿主没有那个通道）。

**目录**：
- ``mock_data.py`` —— 壳阶段的演示数据（纯逻辑，无宿主依赖，可离线单测）
- ``service.py``   —— 中控服务：业务逻辑（当前阶段全是演示数据）
- ``api.py``       —— 18 个 WebUI 接口面（混入类；唯一依赖宿主的地方是 ``maibot_sdk.API``）
- ``ledger.py``    —— 共享账本（跨 Bot 交换消息的载体，SQLite）
- ``hostinfo.py``  —— 工作区定位 + 受管 Bot 名单 + 「我是哪个 Bot」

⚠️ **加载约定**（踩坑 22 的延续）：宿主按文件路径加载插件主文件，
``sys.path`` **不含**插件目录 ⇒ 兄弟模块不能裸 ``import``。
本包由 plugin.py 用 ``_load_package()`` 按路径加载并**先注册进 sys.modules**，
包内一律用**相对 import**（``from .mock_data import ...``）。

⚠️ **本包被 import 时不碰 maibot_sdk** —— 只有 ``api.py`` 需要它，
而它是**惰性**导入的（见本文件的 ``__getattr__``）⇒
``service`` / ``mock_data`` / ``ledger`` / ``hostinfo`` 都能离线单测。

版本：见 plugin.py 的 VERSION 常量。
"""
from __future__ import annotations

from typing import Any

# 这三个子模块**只依赖标准库** ⇒ 直接导入，不走惰性
from . import hostinfo, ledger, mock_data, service
from .ledger import ChatroomLedger
from .service import ChatroomService

__all__ = [
    "ChatroomApiMixin",
    "ChatroomLedger",
    "ChatroomService",
    "api_mixin",
    "hostinfo",
    "ledger",
    "mock_data",
    "service",
]


def __getattr__(name: str) -> Any:
    """惰性导出（PEP 562）。

    ``ChatroomApiMixin`` 住在 ``api.py``，而 ``api.py`` 依赖 ``maibot_sdk``。
    做成惰性 ⇒ ``import chatroom``（或离线单测 service / mock_data / ledger / hostinfo）
    不会把宿主 SDK 拖进来。
    """
    if name == "ChatroomApiMixin":
        from .api import ChatroomApiMixin

        return ChatroomApiMixin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def api_mixin() -> Any:
    """返回 API 混入类（显式取用，避免属性式惰性导入在类定义处不好读）。"""
    from .api import ChatroomApiMixin

    return ChatroomApiMixin
