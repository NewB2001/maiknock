# -*- coding: utf-8 -*-
"""麦麦聊天室 · 中控服务（聊天室的业务逻辑）。

⚠️ **本阶段（S1 合并骨架）只做演示**：所有数据来自 mock_data，
   不连接任何 Bot、不递话、不收话。真实数据源在 S2 之后接上，
   届时本模块按 ``data_source`` 分流（mock / real）。

无宿主导入（除类型标注）=> 可离线单测。
"""
from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional, Tuple

from .mock_data import (
    DELAY_MAX_SECONDS,
    DELAY_MIN_SECONDS,
    DELAY_NAMED_SECONDS,
    DELAY_SPONTANEOUS_SECONDS,
    MANAGED_BOTS,
    MEMBER_POOL,
    NARRATOR,
    REPLY_TEMPLATES,
    ROOMS,
    STATUS_GATEWAY_OFF,
    STATUS_LABELS,
    STATUS_READY,
    STATUS_STOPPED,
    STICKER_POOL,
    _build_segment,
)
from .errorscan import describe_report
from .hostinfo import read_bot_config, read_bots
from .identity import DEFAULT_PLATFORM, virtual_group_id
from .roster import build_roster

# 聊天室子模块自己的版本号（插件总版本见 plugin.py 的 VERSION 常量）
CHATROOM_VERSION = "0.1.0"


class ChatroomService:
    """聊天室的业务逻辑（S1：全部演示数据）。"""

    def __init__(self, plugin: Any = None) -> None:
        super().__init__()
        # 插件实例（拿 ctx / 配置用）。⚠️ 构造时插件可能还没构造完 ⇒
        # 只存引用、不在这里读配置；一切读取都在调用时惰性做。
        self._plugin = plugin
        self._rooms: List[Dict[str, Any]] = [dict(room) for room in ROOMS]
        self._room_counter: int = len(self._rooms)
        # 预置时间线已经占掉第 1、2 幕，第一次提交要从第 3 幕开始，否则幕号会撞车。
        self._turn_counter: int = 2
        # 群员登记表：**必须是本实例自己的副本**。
        # 直接改模块级的 MEMBER_POOL 会把常量改坏 —— 改个名字就会让
        # 按名字查人的预置时间线查不到人，bootstrap 直接 502。
        self._members: Dict[str, Dict[str, Any]] = {
            member["member_id"]: dict(member) for member in MEMBER_POOL
        }
        # 用户在房间里自建的群员（假数据，只存在内存里）
        self._custom_counter: int = 0
        # 「我的角色」= 我自己演的那些角色，**全局一份、有顺序**（不挂在某个房间下）。
        # 顺序会影响剧本框里角色下拉的排列，也影响私聊窗口的默认发言人。
        self._my_role_order: List[str] = [
            member["member_id"] for member in MEMBER_POOL if member.get("kind") == "user_role"
        ]
        # 「私聊对象」是另一个上游名单（Bot 群员）。
        # 层级：**我的角色 / 私聊对象（上游） → 房间群员（下游）**，
        # 下游只能用上游已经有的角色。
        self._peer_order: List[str] = [
            member["member_id"] for member in MEMBER_POOL if member.get("kind") == "bot"
        ]

    def _member_pool(self) -> List[Dict[str, Any]]:
        """全部可选群员 = 固定候选池 + 用户自建的（本实例自己的副本）。"""
        return list(self._members.values())

    def _member_map(self) -> Dict[str, Dict[str, Any]]:
        return self._members

    def _peer_list(self) -> List[Dict[str, Any]]:
        """「私聊对象」名单（上游）：可用的 Bot 群员。"""
        pool = self._member_map()
        peers = []
        for member_id in self._peer_order:
            member = pool.get(member_id)
            if member and member.get("kind") == "bot":
                peers.append(member)
        return peers

    async def _managed_bot_list(self) -> List[Dict[str, Any]]:
        """宿主受管 Bot 列表（私聊对象的可选范围）。

        - **real 模式**：真名单（registry 的名单层 + 账本里各实例自报的握手/风格层），
          附**四态**；
        - **mock 模式**：壳阶段的假名单（写死 5 个）。
        """
        if await self._data_source() == "real":
            return await self._real_managed_bot_list()
        pool = self._member_map()
        items = []
        for bot in MANAGED_BOTS:
            peer = pool.get(bot["peer_member_id"])
            items.append(
                {
                    "bot_id": bot["bot_id"],
                    "bot_name": bot["bot_name"],
                    "peer_member_id": bot["peer_member_id"],
                    "peer_name": peer["display_name"] if peer else bot["bot_name"],
                    "added": bot["peer_member_id"] in self._peer_order,
                }
            )
        return items

    # ---- 真实名单（real 模式）-------------------------------------------

    def _workspace(self):
        """工作区根（从插件实例拿；拿不到就返回 None）。"""
        return getattr(self._plugin, "_chatroom_workspace", None)

    def _ledger(self):
        """共享账本（从插件实例拿）。"""
        return getattr(self._plugin, "_chatroom_ledger", None)

    def _real_roster(self) -> List[Dict[str, Any]]:
        """真实受管 Bot 名单 = registry（名单层）+ 账本里各实例自报的状态。

        ⚠️ 读不到工作区或账本就返回空列表 —— 调用方据此回落到 mock，
        **不抛异常**（聊天室的毛病不该把页面搞崩）。
        """
        workspace = self._workspace()
        ledger = self._ledger()
        if workspace is None or ledger is None:
            return []
        try:
            bots = read_bots(workspace)
            statuses = ledger.list_bot_status()
        except Exception:  # noqa: BLE001
            return []
        return build_roster(bots, statuses)

    async def _real_managed_bot_list(self) -> List[Dict[str, Any]]:
        """真实模式下的「受管 Bot 列表」。

        每一项都带**四态**（ready / gateway_off / stopped / style_unfixed）——
        页面把状态放在**头像悬停提示**里（不占列）。
        """
        items: List[Dict[str, Any]] = []
        pool = self._member_map()
        for entry in self._real_roster():
            # ⚠️ **按 bot_id 找成员，不能按派生的 id 找** ——
            #    `_sync_real_members` 匹配到已有成员时**保留原来的 member_id**
            #    （比如壳阶段的 bot_hinata 就还是 bot_hinata），
            #    按 `bot_<bot_id>` 查会查不到 ⇒ peer_name 退化成实例名。
            member = next(
                (
                    item
                    for item in pool.values()
                    if item.get("kind") == "bot"
                    and str(item.get("bot_id") or "") == str(entry["bot_id"])
                ),
                None,
            )
            member_id = member["member_id"] if member else f"bot_{entry['bot_id']}"
            items.append(
                {
                    "bot_id": entry["bot_id"],
                    "bot_name": entry["bot_name"],
                    "peer_member_id": member_id,
                    "peer_name": member["display_name"] if member else entry["bot_name"],
                    "status": entry["status"],
                    "status_label": entry["status_label"],
                    "selectable": entry["selectable"],
                    "detail": entry["detail"],
                    "added": member_id in self._peer_order,
                }
            )
        return items

    def _upstream_member_ids(self) -> List[str]:
        """上游已经有的全部角色（我的角色 + 私聊对象）。"""
        return [*self._my_role_order, *self._peer_order]

    def _room_by_id(self, room_id: str) -> Optional[Dict[str, Any]]:
        return next((room for room in self._rooms if room["room_id"] == room_id), None)

    def _my_role_list(self) -> List[Dict[str, Any]]:
        """「我的角色」列表（有序）。"""
        pool = self._member_map()
        ordered: List[Dict[str, Any]] = []
        seen = set()
        for member_id in self._my_role_order:
            member = pool.get(member_id)
            if member and member.get("kind") == "user_role":
                ordered.append(member)
                seen.add(member_id)
        # 没进顺序表的自建角色，追加到末尾（不至于丢）
        for member in pool.values():
            if member.get("kind") == "user_role" and member["member_id"] not in seen:
                ordered.append(member)
        return ordered

    async def _load_bot_profile(self, bot_id: str) -> Optional[Dict[str, Any]]:
        """从 Bot 自己的配置里读显示名 / 人格标签。

        - **real 模式**：读该 Bot 自己的 ``config/bot_config.toml`` ——
          ``[bot].nickname`` 是它自称的名字（麦麦一号那边是「日向雏田」，
          这才是聊天室该显示的**角色名**，不是实例名「麦麦一号」），
          ``[personality].personality`` 是人格描述。
        - **mock 模式**：**不读**（壳阶段铁律：演示阶段不碰 Bot 的配置），返回 None，
          调用方回落到候选池里的假数据。

        ⚠️ 02 文档 8.1 的边界表把「各 Bot 的人格 / 显示名 / 头像」列在**只读**里 ⇒
        读它没问题，**写**才是越界。

        返回值形状（约定）：``{"display_name": str, "avatar": str, "persona_label": str}``，
        字段可以缺；返回 None 表示「读不到，用兜底值」。
        """
        if await self._data_source() != "real":
            return None
        workspace = self._workspace()
        if workspace is None:
            return None
        entry = next((e for e in self._real_roster() if e["bot_id"] == bot_id), None)
        if entry is None:
            return None
        try:
            cfg = read_bot_config(workspace, entry["directory"])
        except Exception:  # noqa: BLE001
            return None
        if not cfg:
            return None
        profile: Dict[str, Any] = {}
        if cfg.get("nickname"):
            profile["display_name"] = str(cfg["nickname"])
        persona = str(cfg.get("personality") or "")
        if persona:
            profile["persona_label"] = persona[:24]
        return profile or None

    async def _resolve_member(self, member: Dict[str, Any]) -> Dict[str, Any]:
        """Bot 群员的显示名 / 头像 / 人格标签优先从它自己的配置里读。

        我方角色（user_role）没有「自己的配置」，名字和头像都由用户临场发挥。
        """
        resolved = dict(member)
        if member.get("kind") != "bot" or not member.get("bot_id"):
            return resolved
        profile = await self._load_bot_profile(member["bot_id"])
        if not profile:
            return resolved
        for key in ("display_name", "avatar", "persona_label"):
            if profile.get(key):
                resolved[key] = profile[key]
        return resolved

    async def _room_members(self, room: Dict[str, Any]) -> List[Dict[str, Any]]:
        """某个房间的群员（按候选池顺序）。"""
        pool = self._member_map()
        members = []
        for member_id in room.get("member_ids", []):
            member = pool.get(member_id)
            if member:
                members.append(await self._resolve_member(member))
        return sorted(members, key=lambda item: (item.get("kind") != "user_role", item.get("sort_order", 99)))

    async def _room_bot_members(self, room: Dict[str, Any]) -> List[Dict[str, Any]]:
        """某个房间里的麦麦 Bot（本轮接话的候选范围）。"""
        return [member for member in await self._room_members(room) if member["kind"] == "bot"]

    @staticmethod
    def _parse_script(script: str) -> List[Dict[str, str]]:
        """解析多行剧本：每行 ``角色：台词``。

        返回 [{speaker, line}]；无法解析的行会被标成 ``_invalid``，
        由页面负责提示用户，插件不静默丢弃。
        """
        parsed: List[Dict[str, str]] = []
        for raw_line in (script or "").splitlines():
            text = raw_line.strip()
            if not text:
                continue
            if "：" in text:
                speaker, _, content = text.partition("：")
            elif ":" in text:
                speaker, _, content = text.partition(":")
            else:
                parsed.append({"speaker": "", "line": text, "invalid": "1"})
                continue
            speaker = speaker.strip()
            content = content.strip()
            if not speaker:
                parsed.append({"speaker": "", "line": text, "invalid": "1"})
                continue
            parsed.append({"speaker": speaker, "line": content, "invalid": ""})
        return parsed

    def _compose_reply(
        self, member: Dict[str, Any], echo: str, rng: random.Random
    ) -> List[Dict[str, str]]:
        """造出一条 Bot 的假回复（可能含表情包行）。"""
        member_id = member["member_id"]
        templates = REPLY_TEMPLATES.get(member_id, ["……{echo}"])
        text = rng.choice(templates).replace("{echo}", echo or "……")
        lines = [{"kind": "reply", "content": text}]
        stickers = STICKER_POOL.get(member_id, [])
        if stickers and rng.random() < 0.7:
            lines.append({"kind": "sticker", "content": f"【表情包·{rng.choice(stickers)}】"})
        return lines

    async def shell_bootstrap(self) -> Dict[str, Any]:
        """一次性拉取页面需要的全部初始数据。"""
        source = await self._data_source()
        # ⚠️ **顺序要紧**：先按数据源同步群员，再组装 member_pool ——
        #    否则返回的还是同步**之前**的旧名单（假 Bot 还在、真 Bot 没建）。
        await self._sync_real_members()
        await self._refresh_peer_status()
        pool = []
        for member in self._member_pool():
            pool.append(await self._resolve_member(member))
        return {
            "version": CHATROOM_VERSION,
            "stage": "shell" if source == "mock" else "real",
            "notice": self._notice(source, await self._chatroom_cfg()),
            "member_pool": pool,
            "my_roles": self._my_role_list(),
            "peers": self._peer_list(),
            "managed_bots": await self._managed_bot_list(),
            "narrator": NARRATOR,
            "errors": await self._chatroom_errors(),
            "rooms": self._rooms,
            "timeline": self._seeded_timeline(),
            "private_threads": self._seeded_private_threads(),
            "timing": {
                "named_seconds": DELAY_NAMED_SECONDS,
                "spontaneous_seconds": DELAY_SPONTANEOUS_SECONDS,
                "min_seconds": DELAY_MIN_SECONDS,
                "max_seconds": DELAY_MAX_SECONDS,
                "note": "实测参考：@点名 23s、无点名自发接话 51s、沉默观察 150s 超时。",
            },
        }

    # ---- 配置读取（真实数据源接上后，这里是唯一的入口）--------------

    async def _chatroom_cfg(self) -> Dict[str, Any]:
        """读插件配置的 [chatroom] 段。

        ⚠️ 读插件自己的配置**必须**用 `config.get_plugin` 能力；
        `ctx.config.get()` 读的是宿主全局主配置（插件陷阱 1）。
        ⚠️ 读不到就返回空 dict ⇒ 全部走默认值（mock / 关），**不抛异常**。
        """
        plugin = self._plugin
        getter = getattr(plugin, "_plugin_config", None)
        if getter is None:
            return {}
        try:
            raw = await getter()
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(raw, dict):
            return {}
        cfg = raw.get("chatroom")
        return cfg if isinstance(cfg, dict) else {}

    async def _data_source(self) -> str:
        """当前数据来源：mock（演示）或 real（真递话真收话）。"""
        cfg = await self._chatroom_cfg()
        source = str(cfg.get("data_source") or "mock").strip().lower()
        return source if source in ("mock", "real") else "mock"

    @staticmethod
    def _notice(source: str, cfg: Optional[Dict[str, Any]] = None) -> str:
        """页面顶部那句提示 —— **必须跟当前实际状态一致，不能说假话**。

        ⚠️ 正式模式也不是"全都真了"：递送（delivery）与回收（collect）各有各的开关，
        提示语要照着**实际开着哪些**来说，别一律喊"正式模式"。
        """
        if source != "real":
            return (
                "演示模式：本页所有内容都是假数据（Mock）。"
                "不连接 maim 网关、不读写任何 Bot 数据、不做任何消息处理。"
            )
        cfg = cfg or {}
        delivery = bool((cfg.get("delivery") or {}).get("enabled"))
        collect = bool((cfg.get("collect") or {}).get("enabled"))
        parts = ["正式模式：受管 Bot 名单与接入状态是真的"]
        if delivery and collect:
            parts.append("台词会真的递进各 Bot 的虚拟群，回复是麦麦真说的")
        elif delivery:
            parts.append("台词会真的递进各 Bot 的虚拟群（回复回收还没接上）")
        elif collect:
            parts.append("回复回收已接上（台词递送还没接上）")
        else:
            parts.append("台词递送与回复回收都还没接上")
        return "；".join(parts) + "。"

    async def shell_rooms(self) -> Dict[str, Any]:
        """单独刷新房间列表用。"""
        return {"rooms": self._rooms}

    async def shell_my_roles(self) -> Dict[str, Any]:
        """「我的角色」= 我自己演的角色，全局一份、有顺序。"""
        return {"my_roles": self._my_role_list()}

    async def shell_add_my_role(
        self,
        display_name: str,
        avatar: str = "",
        persona_label: str = "",
    ) -> Dict[str, Any]:
        """在**上游**「我的角色」里新建一个角色。

        上游登记过的角色，下游（房间群员、剧本框的角色下拉）才选得到。
        """
        clean = (display_name or "").strip()[:24]
        if not clean:
            return {"ok": False, "error": "角色名字不能为空"}
        if any(member["display_name"] == clean for member in self._member_pool()):
            return {"ok": False, "error": f"已经有叫「{clean}」的角色了"}

        self._custom_counter += 1
        member_id = f"custom_{self._custom_counter:03d}"
        member = {
            "member_id": member_id,
            "kind": "user_role",
            "display_name": clean,
            "persona_label": (persona_label or "").strip()[:24] or "自建角色",
            "sort_order": 100 + self._custom_counter,
            "status": None,
            "status_label": "我饰演",
            "bot_id": None,
            "bot_name": None,
            "avatar": (avatar or "").strip() or "🧑",
            "editable": True,
            "custom": True,
        }
        self._members[member_id] = member
        self._my_role_order.append(member_id)
        return {
            "ok": True,
            "my_roles": self._my_role_list(),
            "member_pool": [await self._resolve_member(item) for item in self._member_pool()],
            "created": member,
        }

    async def shell_add_peer(self, bot_id: str) -> Dict[str, Any]:
        """把宿主**受管 Bot**加进上游「私聊对象」。

        可选范围只有受管 Bot —— 不能凭空造一个没有的 Bot。
        已经加过的会明确告诉你。

        - **real 模式**：从真实名单里挑，并**按需建群员**（显示名取它自己的
          ``[bot].nickname``，即角色名而不是实例名）；
        - **mock 模式**：壳阶段的假名单。
        """
        if await self._data_source() == "real":
            return await self._real_add_peer(bot_id)
        target = next((item for item in MANAGED_BOTS if item["bot_id"] == bot_id), None)
        if target is None:
            return {"ok": False, "error": f"这不是受管 Bot：{bot_id}"}
        member = self._member_map().get(target["peer_member_id"])
        if member is None:
            return {"ok": False, "error": f"受管 Bot「{target['bot_name']}」还没有对应的人格"}
        if target["peer_member_id"] in self._peer_order:
            return {
                "ok": False,
                "error": f"{member['display_name']}（{target['bot_name']}）已经在私聊对象里了",
            }
        self._peer_order.append(target["peer_member_id"])
        return {
            "ok": True,
            "peers": self._peer_list(),
            "managed_bots": await self._managed_bot_list(),
            "member_pool": [await self._resolve_member(item) for item in self._member_pool()],
            "created": member,
        }

    async def _real_add_peer(self, bot_id: str) -> Dict[str, Any]:
        """real 模式：把真实名单里的某个 Bot 加进「私聊对象」（**按需建群员**）。"""
        entry = next((e for e in self._real_roster() if e["bot_id"] == bot_id), None)
        if entry is None:
            return {"ok": False, "error": f"这不是受管 Bot：{bot_id}"}
        if not entry["selectable"]:
            return {
                "ok": False,
                "error": f"{entry['bot_name']} 现在不可接入（{entry['status_label']}）",
            }
        member_id = f"bot_{bot_id}"
        if member_id in self._peer_order:
            return {"ok": False, "error": f"{entry['bot_name']} 已经在私聊对象里了"}
        profile = await self._load_bot_profile(bot_id) or {}
        member = {
            "member_id": member_id,
            "kind": "bot",
            "display_name": profile.get("display_name") or entry["bot_name"],
            "persona_label": profile.get("persona_label") or entry["bot_name"],
            "sort_order": 100 + len(self._peer_order),
            "status": entry["status"],
            "status_label": entry["status_label"],
            "bot_id": bot_id,
            "bot_name": entry["bot_name"],
            "platform": DEFAULT_PLATFORM,
            "account": f"bot.{bot_id}",
            "avatar": "🤖",
            "editable": False,
            "custom": True,
        }
        self._members[member_id] = member
        self._peer_order.append(member_id)
        return {
            "ok": True,
            "peers": self._peer_list(),
            "managed_bots": await self._managed_bot_list(),
            "member_pool": [await self._resolve_member(item) for item in self._member_pool()],
            "created": member,
        }

    async def _refresh_peer_status(self) -> None:
        """按真实名单刷新各 Bot 群员的状态（页面拿的是群员上的 status）。"""
        if await self._data_source() != "real":
            return
        pool = self._member_map()
        for entry in self._real_roster():
            member = pool.get(f"bot_{entry['bot_id']}")
            if member is None:
                continue
            member["status"] = entry["status"]
            member["status_label"] = entry["status_label"]

    async def _sync_real_members(self) -> None:
        """real 模式：**Bot 群员以真实名单为准**重建。

        三件事：
        1. **同一个 bot_id 的**就地更新（保持 member_id 不变 ⇒ 房间里已有的引用不失效）；
        2. **名单里没有的**（壳阶段那些指向假 bot_id / 已不存在的 bot_id 的 Bot）
           从登记表、上游名单、房间成员里**摘掉** —— 留着就是拿一个不存在的 Bot 充数，
           界面上会出现同名角色出现两次；
        3. **名单里新增的**建一个新成员（显示名取它自己的 ``[bot].nickname``，
           也就是角色名而不是实例名）。

        ⚠️ 「我的角色」（我自建的）不受影响 —— 那是用户自己的角色，不归宿主管。
        """
        if await self._data_source() != "real":
            return
        roster = self._real_roster()
        if not roster:
            return
        pool = self._member_map()
        real_by_bot_id = {str(entry["bot_id"]): entry for entry in roster}

        # ① 摘掉名单里没有的 Bot 成员
        stale = [
            member_id
            for member_id, member in pool.items()
            if member.get("kind") == "bot"
            and str(member.get("bot_id") or "") not in real_by_bot_id
        ]
        for member_id in stale:
            pool.pop(member_id, None)
            self._peer_order = [item for item in self._peer_order if item != member_id]
            for room in self._rooms:
                if member_id in room.get("member_ids", []):
                    room["member_ids"].remove(member_id)

        # ② 名单里的 Bot：能对上就更新，对不上就新建
        for entry in roster:
            bot_id = str(entry["bot_id"])
            profile = await self._load_bot_profile(bot_id) or {}
            existing = next(
                (
                    member
                    for member in pool.values()
                    if member.get("kind") == "bot" and str(member.get("bot_id") or "") == bot_id
                ),
                None,
            )
            member_id = existing["member_id"] if existing else f"bot_{bot_id}"
            member = existing or {
                "member_id": member_id,
                "kind": "bot",
                "sort_order": 100 + len(self._peer_order),
                "avatar": "🤖",
                "editable": False,
                "custom": True,
            }
            member.update(
                {
                    "display_name": profile.get("display_name") or entry["bot_name"],
                    "persona_label": profile.get("persona_label") or entry["bot_name"],
                    "bot_id": bot_id,
                    "bot_name": entry["bot_name"],
                    "platform": DEFAULT_PLATFORM,
                    "account": f"bot.{bot_id}",
                    "status": entry["status"],
                    "status_label": entry["status_label"],
                }
            )
            pool[member_id] = member

    async def shell_delete_peer(self, member_id: str) -> Dict[str, Any]:
        """从「私聊对象」里删掉一个 Bot（也会从各房间的成员名单里移除）。"""
        member = self._member_map().get(member_id)
        if member is None:
            return {"ok": False, "error": f"角色不存在：{member_id}"}
        if member.get("kind") != "bot":
            return {"ok": False, "error": "只能删「私聊对象」里的 Bot"}
        # 只把「用户自建的角色」从登记表里抹掉；
        # 雏田 / 小樱这种常驻角色只是「暂时不在私聊对象名单里」，
        # 还得留着，否则下拉框里再选一次就找不到了。
        if member.get("custom"):
            self._members.pop(member_id, None)
        self._peer_order = [item for item in self._peer_order if item != member_id]
        for room in self._rooms:
            if member_id in room.get("member_ids", []):
                room["member_ids"].remove(member_id)
        return {
            "ok": True,
            "peers": self._peer_list(),
            "managed_bots": await self._managed_bot_list(),
            "rooms": self._rooms,
            "member_pool": [await self._resolve_member(item) for item in self._member_pool()],
        }

    async def shell_reorder_my_roles(self, order: Optional[List[str]] = None) -> Dict[str, Any]:
        """把「我的角色」按给定顺序重排；没提到的排到后面，保持相对顺序。"""
        pool = self._member_map()
        given = [
            member_id
            for member_id in (order or [])
            if member_id in pool and pool[member_id].get("kind") == "user_role"
        ]
        rest = [member_id for member_id in self._my_role_order if member_id not in given]
        self._my_role_order = given + rest
        return {
            "ok": True,
            "my_roles": self._my_role_list(),
            "member_pool": [await self._resolve_member(item) for item in self._member_pool()],
        }

    async def shell_delete_my_role(self, member_id: str) -> Dict[str, Any]:
        """删除一个「我的角色」。

        会同时从群员登记表、我的角色顺序表、以及**所有房间**的成员名单里移除它
        —— 不然房间里会留一个查不到人的悬空成员。
        """
        member = self._member_map().get(member_id)
        if member is None:
            return {"ok": False, "error": f"角色不存在：{member_id}"}
        if member.get("kind") != "user_role":
            return {"ok": False, "error": "只能删除「我的角色」，Bot 群员不在这里管"}
        self._members.pop(member_id, None)
        self._my_role_order = [item for item in self._my_role_order if item != member_id]
        for room in self._rooms:
            if member_id in room.get("member_ids", []):
                room["member_ids"].remove(member_id)
        return {
            "ok": True,
            "my_roles": self._my_role_list(),
            "rooms": self._rooms,
            "member_pool": [await self._resolve_member(item) for item in self._member_pool()],
        }

    async def shell_create_room(self, name: str) -> Dict[str, Any]:
        """新建房间；只存在于内存，插件重载即消失。

        用户输入错误用 ``ok=False`` 返回业务结果，不抛异常——
        宿主会把异常翻译成「插件 API 执行失败」，反而看不出错在哪。
        """
        clean = (name or "").strip()[:40]
        if not clean:
            return {"ok": False, "error": "房间名不能为空"}
        self._room_counter += 1
        room = {
            "room_id": f"room_mock_{self._room_counter:03d}",
            "name": clean,
            "mode": "stage",
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
            # 新房间默认带上全部候选群员，用户可以再增删替换。
            "member_ids": [member["member_id"] for member in MEMBER_POOL],
        }
        self._rooms.append(room)
        return {"ok": True, "rooms": self._rooms, "member_pool": self._member_pool(), "created": room}

    async def shell_rename_room(self, room_id: str, name: str) -> Dict[str, Any]:
        """重命名房间；只存在于内存，插件重载即恢复。"""
        clean = (name or "").strip()[:40]
        if not clean:
            return {"ok": False, "error": "房间名不能为空"}
        room = self._room_by_id(room_id)
        if room is None:
            return {"ok": False, "error": f"房间不存在：{room_id}"}
        room["name"] = clean
        return {"ok": True, "rooms": self._rooms, "renamed": room}

    async def shell_add_member(self, room_id: str, member_id: str) -> Dict[str, Any]:
        """把**上游已有**的一个角色加进房间。

        上游 = 「我的角色」+「私聊对象」。下游（房间群员）不能出现上游没有的角色 ——
        必须先在上游添加好，这里才选得到。
        """
        room = self._room_by_id(room_id)
        if room is None:
            return {"ok": False, "error": f"房间不存在：{room_id}"}
        member = self._member_map().get(member_id)
        if member is None:
            return {"ok": False, "error": f"角色不存在：{member_id}"}
        if member_id not in self._upstream_member_ids():
            return {
                "ok": False,
                "error": f"{member['display_name']} 还没添加，先在「我的角色」或「私聊对象」里添加",
            }
        if member_id in room.setdefault("member_ids", []):
            return {"ok": False, "error": f"{member['display_name']} 已经在房间里了"}
        room["member_ids"].append(member_id)
        return {"ok": True, "rooms": self._rooms, "member_pool": self._member_pool(), "changed": member}

    async def shell_remove_member(self, room_id: str, member_id: str) -> Dict[str, Any]:
        """把一个成员移出房间（时间线上他已有的发言保留）。"""
        room = self._room_by_id(room_id)
        if room is None:
            return {"ok": False, "error": f"房间不存在：{room_id}"}
        member_ids = room.setdefault("member_ids", [])
        if member_id not in member_ids:
            return {"ok": False, "error": "该群员不在这个房间里"}
        member_ids.remove(member_id)
        return {"ok": True, "rooms": self._rooms, "member_pool": self._member_pool(), "changed_id": member_id}

    async def shell_replace_member(
        self, room_id: str, old_member_id: str, new_member_id: str
    ) -> Dict[str, Any]:
        """把房间里的一个成员换成另一个 —— 实现上就是「先删后增」。

        放在一个接口里做，是为了保证这步是原子的：
        中间任何一步失败都不会留下「旧人没了新人也没进来」的空档。
        新成员如果本来就在房间里（比如刚自建的），会先把它挪走再插到旧成员的位置上，
        这样「用谁顶替谁」不会因为新人已在场而做不了。
        """
        room = self._room_by_id(room_id)
        if room is None:
            return {"ok": False, "error": f"房间不存在：{room_id}"}
        member_ids = room.setdefault("member_ids", [])
        if old_member_id not in member_ids:
            return {"ok": False, "error": "要替换的群员不在这个房间里"}
        if old_member_id == new_member_id:
            return {"ok": False, "error": "新群员和旧群员是同一个"}
        pool = self._member_map()
        new_member = pool.get(new_member_id)
        if new_member is None:
            return {"ok": False, "error": f"候选群员不存在：{new_member_id}"}
        # 先删后增：位置保持在原处，成员排序不会被顶到末尾
        position = member_ids.index(old_member_id)
        member_ids.pop(position)
        if new_member_id in member_ids:
            member_ids.remove(new_member_id)
        member_ids.insert(position, new_member_id)
        return {
            "ok": True,
            "rooms": self._rooms,
            "member_pool": self._member_pool(),
            "changed_id": new_member_id,
        }

    async def shell_delete_room(self, room_id: str) -> Dict[str, Any]:
        """删掉一个房间。

        房间删了之后，时间线里那些发言也跟着没（房间的存档本来就是房间自己的）。
        至少要留一个房间 —— 全删了页面就没舞台了。
        """
        room = self._room_by_id(room_id)
        if room is None:
            return {"ok": False, "error": f"房间不存在：{room_id}"}
        if len(self._rooms) <= 1:
            return {"ok": False, "error": "至少要留一个房间"}
        self._rooms = [item for item in self._rooms if item["room_id"] != room_id]
        return {
            "ok": True,
            "rooms": self._rooms,
            "deleted": room,
            "member_pool": [await self._resolve_member(item) for item in self._member_pool()],
        }

    async def shell_update_member(
        self,
        member_id: str,
        display_name: str = "",
        avatar: str = "",
        room_id: str = "",
    ) -> Dict[str, Any]:
        """改群员的名字和头像。

        **只允许改我方角色（user_role）** —— Bot 群员的显示名和头像来自它自己的配置
        （见 ``_load_bot_profile`` 的接口约定），在这里改会和配置打架。

        ``room_id`` 可以不给：给了就额外要求这个角色在那个房间里；
        不给（「我的角色」面板走这条）就只改角色本身。
        """
        member = self._member_map().get(member_id)
        if member is None:
            return {"ok": False, "error": f"群员不存在：{member_id}"}
        if room_id:
            room = self._room_by_id(room_id)
            if room is None:
                return {"ok": False, "error": f"房间不存在：{room_id}"}
            if member_id not in room.setdefault("member_ids", []):
                return {"ok": False, "error": "该群员不在这个房间里"}
        if member.get("kind") != "user_role" and not member.get("custom"):
            return {"ok": False, "error": "Bot 群员的名字和头像来自它自己的配置，不能在这里改"}

        clean_name = (display_name or "").strip()[:24]
        if display_name and not clean_name:
            return {"ok": False, "error": "群员名字不能为空"}
        if clean_name:
            if room_id:
                # 同一个房间里不能重名
                clash = [
                    other
                    for other in room.get("member_ids", [])
                    if other != member_id
                    and (self._member_map().get(other) or {}).get("display_name") == clean_name
                ]
            else:
                # 「我的角色」是全局的，重名会让人分不清是谁
                clash = [
                    other
                    for other in self._member_map()
                    if other != member_id
                    and self._member_map()[other].get("display_name") == clean_name
                ]
            if clash:
                return {"ok": False, "error": f"已经有叫「{clean_name}」的了"}
            member["display_name"] = clean_name
        if avatar:
            # 头像**完整保存**，不做长度截断：
            # 头像可能是上传来的图片 data URI（几百 KB），
            # 截成 8 个字符就只剩 "data:ima"，页面只能退回首字圆牌。
            # 存储是插件内存态，没有长度上限；webui.json 那边也放开了 max_length。
            member["avatar"] = avatar
        return {
            "ok": True,
            "rooms": self._rooms,
            "member_pool": [await self._resolve_member(item) for item in self._member_pool()],
            "my_roles": self._my_role_list(),
            "changed_id": member_id,
        }

    async def shell_create_member(
        self,
        room_id: str = "",
        display_name: str = "",
        kind: str = "user_role",
        avatar: str = "",
        persona_label: str = "",
    ) -> Dict[str, Any]:
        """自建一个群员。

        ``room_id`` 可以不给：
        * 给了 → 同时加进那个房间（群员面板的「新建并加入」走这条）；
        * 不给 → 只建出来，**我方角色**会进「我的角色」名单（私聊面板的「新增」走这条）。
        """
        room = self._room_by_id(room_id) if room_id else None
        if room_id and room is None:
            return {"ok": False, "error": f"房间不存在：{room_id}"}
        clean = (display_name or "").strip()[:24]
        if not clean:
            return {"ok": False, "error": "群员名字不能为空"}
        if kind not in {"user_role", "bot"}:
            return {"ok": False, "error": "群员类型只能是 user_role 或 bot"}
        if any(member["display_name"] == clean for member in self._member_pool()):
            return {"ok": False, "error": f"已经有叫「{clean}」的群员了"}

        self._custom_counter += 1
        member_id = f"custom_{self._custom_counter:03d}"
        member = {
            "member_id": member_id,
            "kind": kind,
            "display_name": clean,
            "persona_label": (persona_label or "").strip()[:24] or ("自建角色" if kind == "user_role" else "自建 Bot"),
            "sort_order": 100 + self._custom_counter,
            "status": None if kind == "user_role" else STATUS_STOPPED,
            "status_label": "我饰演" if kind == "user_role" else STATUS_LABELS[STATUS_STOPPED],
            "bot_id": None if kind == "user_role" else f"maichat-mock-custom-{self._custom_counter:03d}",
            "bot_name": None if kind == "user_role" else f"自建 Bot {self._custom_counter}",
            "avatar": (avatar or "").strip() or ("🧑" if kind == "user_role" else "🤖"),
            "editable": kind == "user_role",
            "custom": True,
        }
        self._members[member_id] = member
        if kind == "user_role":
            self._my_role_order.append(member_id)
        if room is not None:
            room.setdefault("member_ids", []).append(member_id)
        return {
            "ok": True,
            "rooms": self._rooms,
            "member_pool": [await self._resolve_member(item) for item in self._member_pool()],
            "my_roles": self._my_role_list(),
            "created": member,
        }

    async def _resolve_lines(
        self,
        room: Dict[str, Any],
        lines: Optional[List[Dict[str, Any]]],
        script: str,
    ) -> Any:
        """把提交上来的台词行解析成「确定的发言人 + 台词」。

        优先用结构化的 ``lines``（页面选好了角色）；没有就用 ``script`` 纯文本
        （``角色：台词``），按**房间成员的名字**唯一匹配。
        匹配不上就明确报错，不猜。

        返回 ``[{member_id, display_name, kind, text}]``；
        出错时返回一个错误字符串（调用方据此返回 ok=False）。
        """
        raw: List[Dict[str, Any]] = []
        if lines:
            for item in lines:
                if not isinstance(item, dict):
                    continue
                raw.append(
                    {
                        "member_id": str(item.get("speaker_member_id") or "").strip(),
                        "speaker": str(item.get("speaker") or "").strip(),
                        "text": str(item.get("text") or "").strip(),
                    }
                )
        elif script:
            for item in self._parse_script(script):
                if item.get("invalid"):
                    return f"台词格式不对，每行都要写成「角色：台词」：{item['line']}"
                raw.append({"member_id": "", "speaker": item["speaker"], "text": item["line"]})

        room_members = await self._room_members(room)
        by_id = {member["member_id"]: member for member in room_members}
        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for member in room_members:
            by_name.setdefault(member["display_name"], []).append(member)

        resolved: List[Dict[str, Any]] = []
        for index, item in enumerate(raw, start=1):
            text = item["text"]
            if not text:
                continue
            member = by_id.get(item["member_id"]) if item["member_id"] else None
            if member is None and item["speaker"]:
                matches = by_name.get(item["speaker"], [])
                if len(matches) == 1:
                    member = matches[0]
                elif len(matches) > 1:
                    return f"第 {index} 行的角色「{item['speaker']}」在这个房间里有好几个，请用下拉框选一个"
            if member is None:
                who = item["speaker"] or item["member_id"] or "（空）"
                return f"第 {index} 行的角色「{who}」不在这个房间里，请先在左边「群员」里加人或改这一行的角色"
            resolved.append(
                {
                    "member_id": member["member_id"],
                    "display_name": member["display_name"],
                    "kind": member["kind"],
                    "text": text,
                }
            )
        return resolved

    # ---- 错误反馈（429 等硬错误，不许默默出错）------------------------

    async def _chatroom_errors(self, limit: int = 5) -> List[Dict[str, Any]]:
        """取最近的**未确认**错误，写成给用户看的白话。

        ⚠️ **中控（一号）能读到别的 Bot 报的错误** —— 各 Bot 的插件实例
        扫自己实例的日志、写进共享账本，这里只是汇总。
        """
        ledger = self._ledger()
        if ledger is None:
            return []
        try:
            rows = ledger.list_errors(limit=limit, only_unacked=True)
        except Exception:  # noqa: BLE001
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "report_id": str(row.get("report_id") or ""),
                    "kind": str(row.get("kind") or ""),
                    "bot_name": str(row.get("bot_name") or ""),
                    "created_at": str(row.get("created_at") or ""),
                    "text": describe_report(row, str(row.get("bot_name") or "")),
                }
            )
        return out

    async def shell_ack_errors(self, report_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """把错误标记为「用户已看到」。

        ⚠️ **只在用户真的看到了才调** —— 标记了就再也不会提示，
        静默失败会变成"提示也没了 ⇒ 没人知道出过错"。
        """
        ledger = self._ledger()
        if ledger is None:
            return {"ok": False, "error": "共享账本没开"}
        try:
            count = ledger.ack_errors(report_ids)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "acked": count}

    async def _real_submit_beat(
        self,
        room_id: str,
        lines: Optional[List[Dict[str, Any]]] = None,
        script: str = "",
        speakers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """real 模式：把这一幕**真的**排成投递任务（跨 Bot 的工作单）。

        流程：
        1. 解析台词（沿用壳阶段那套：角色要么是页面选好的 member_id，
           要么能被房间成员名唯一匹配上；**绝不猜**）；
        2. 写一段（segment）+ 若干消息行进共享账本；
        3. **每行 × 每个在场 Bot** 排一条投递任务 ——
           FR-07：同一句台词要递进**每个**在场 Bot 各自的群聊流；
        4. 点名的 Bot（``speakers``）的那份**在正文前加 ``@显示名``** ——
           02 文档 8.6 实测：@点名是麦麦**开口**的保证（23s vs 不点名 51s）。

        ⚠️ 投递**身份随任务走**（平台 / 账号 / 昵称）——领单的那个 Bot 进程
        没有中控的角色登记表，身份不写进任务就会认错人。
        ⚠️ 任务带**单调序号**（``seq``）——``created_at`` 只有秒级精度，
        同一秒建的多条任务排序不确定 ⇒ 台词会乱序递。
        """
        ledger = self._ledger()
        if ledger is None:
            return {"ok": False, "error": "共享账本没开，聊天室暂时不能递话"}
        # ⚠️ 先同步成员（真名单的显示名 / 状态）—— @点名 用的是**当时的显示名**，
        #    不同步就会拿旧名字去点名（壳阶段的「雏田」而不是真名单的「日向雏田」）。
        await self._sync_real_members()
        await self._refresh_peer_status()
        room = self._room_by_id(room_id)
        if room is None:
            return {"ok": False, "error": f"房间不存在：{room_id}"}
        resolved = await self._resolve_lines(room, lines, script)
        if isinstance(resolved, str):
            return {"ok": False, "error": resolved}
        if not resolved:
            return {"ok": False, "error": "这一幕还没有台词"}
        bots = await self._room_bot_members(room)
        if not bots:
            return {"ok": False, "error": "这个房间里还没有麦麦 Bot，先在左边「群员」里加一个"}
        named = {str(item) for item in (speakers or [])}
        group_id = virtual_group_id(room_id)
        pool = self._member_map()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        segment_id = f"seg_{int(time.time() * 1000)}"
        messages = [
            {
                "message_id": f"{segment_id}_m{index}",
                "speaker_member_id": line["member_id"],
                "speaker_name": line["display_name"],
                "kind": "line",
                "content": line["text"],
                "created_at": stamp,
            }
            for index, line in enumerate(resolved)
        ]
        try:
            ledger.append_segment(room_id, segment_id, "normal", messages)
            seq = ledger.next_delivery_seq()
            queued = 0
            for line in resolved:
                for bot in bots:
                    content = line["text"]
                    if bot["member_id"] in named:
                        # 02 文档 8.6：@点名 是开口保证（正文里带 @显示名）
                        content = f"@{bot['display_name']} {content}"
                    sender = pool.get(line["member_id"]) or {}
                    ledger.enqueue_delivery(
                        delivery_id=f"{segment_id}_d{seq}",
                        target_bot_id=str(bot.get("bot_id") or ""),
                        content=content,
                        room_id=room_id,
                        segment_id=segment_id,
                        sender_member_id=line["member_id"],
                        sender_platform=str(sender.get("platform") or "webui"),
                        sender_account=str(sender.get("account") or ""),
                        sender_nickname=line["display_name"],
                        group_id=group_id,
                        group_name=str(room.get("name") or ""),
                        seq=seq,
                    )
                    seq += 1
                    queued += 1
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"写投递任务失败：{exc}"}
        return {
            "ok": True,
            "room_id": room_id,
            "segment_id": segment_id,
            "queued": queued,
            "bots": [str(bot.get("display_name") or "") for bot in bots],
            "named": sorted(named),
            "lines": resolved,
        }

    async def shell_submit_beat(
        self,
        room_id: str,
        lines: Optional[List[Dict[str, Any]]] = None,
        script: str = "",
        speakers: Optional[List[str]] = None,
        auto_reply: bool = True,
    ) -> Dict[str, Any]:
        """一次提交 = 一幕。

        台词用**结构化的行**传进来（``[{"speaker_member_id": ..., "speaker": ..., "text": ...}]``），
        插件不需要、也不应该去猜「这段话是谁说的」——
        角色要么是页面选好的 member_id，要么是能被房间成员名唯一匹配上的名字。
        ``script``（纯文本 ``角色：台词``）保留为兼容入口，解析后走同一条路。

        返回的内容包括：解析后的台词、本轮的调度策略（显式点名 / 默认随机）、
        seed、每个 Bot 的等待秒数与它要产出的消息行。
        **mock 模式**下这里只是把「未来会发生什么」提前算好交给页面按节奏演出来；
        **real 模式**下会**真的**把台词写进共享账本、排成投递任务，
        由各 Bot 自己的插件实例领单递进虚拟群（S5）。
        """
        if await self._data_source() == "real":
            return await self._real_submit_beat(room_id, lines, script, speakers)
        room = self._room_by_id(room_id)
        if room is None:
            return {"ok": False, "error": f"房间不存在：{room_id}"}

        resolved = await self._resolve_lines(room, lines, script)
        if isinstance(resolved, str):
            return {"ok": False, "error": resolved}
        if not resolved:
            return {"ok": False, "error": "这一幕还没有台词"}

        self._turn_counter += 1
        seed = self._turn_counter * 7919 + len(self._rooms) * 104729
        rng = random.Random(seed)

        # 接话候选 = **这个房间里的**麦麦 Bot（不是全局所有 Bot）
        bots = await self._room_bot_members(room)
        bot_ids = {member["member_id"] for member in bots}
        member_map = self._member_map()
        picked = [
            member_id
            for member_id in (speakers or [])
            if member_id in member_map and member_id in bot_ids
        ]

        if picked:
            strategy = "explicit"
            ordered = [member_map[member_id] for member_id in picked]
        else:
            strategy = "random"
            ordered = list(bots)
            rng.shuffle(ordered)

        echo = resolved[-1]["text"]
        plan: List[Dict[str, Any]] = []
        silenced: List[str] = []
        for member in ordered:
            named = strategy == "explicit"
            base = DELAY_NAMED_SECONDS if named else DELAY_SPONTANEOUS_SECONDS
            spread = 4 if named else 6
            delay = int(round(base + rng.uniform(-spread, 0)))
            delay = max(DELAY_MIN_SECONDS, min(DELAY_MAX_SECONDS, delay))
            # 显式点名的必须开口（ADR-2 导演制）；随机轮次允许拟真沉默，
            # 沉默只记后台状态，不在时间线上留下任何发言（FR-09）。
            is_silent = (not named) and rng.random() < 0.12
            reply_lines: List[Dict[str, str]] = []
            if not is_silent:
                reply_lines = self._compose_reply(member, echo, rng)
            else:
                silenced.append(member["display_name"])
            plan.append(
                {
                    "member_id": member["member_id"],
                    "display_name": member["display_name"],
                    "kind": member["kind"],
                    "delay_seconds": delay,
                    "lines": reply_lines,
                    "silenced": is_silent,
                    "named": named,
                }
            )

        return {
            "ok": True,
            "room_id": room_id,
            "beat_seq": self._turn_counter,
            "seed": seed,
            "strategy": strategy,
            "strategy_label": "显式点名（按勾选顺序固定）" if strategy == "explicit" else "默认随机",
            "speakers": picked,
            "auto_reply": bool(auto_reply),
            "lines": resolved,
            "plan": plan,
            "silenced": silenced,
            "echo": echo,
        }

    @staticmethod
    def _seeded_timeline() -> Dict[str, List[Dict[str, Any]]]:
        """预置时间线假数据，**按段组织**。

        一段 = 我方一次提交的多条台词 + 紧随其后的一轮 Bot 回复
        （一轮可以含多个 Bot，每人又可以有多条消息行）。

        段有两类：
          * ``normal`` —— 普通对话段（我方台词 + Bot 回复）
          * ``plot``   —— 剧情段，由「时光机」生成，发言人是固定身份「剧情旁白」

        场景取自设计文档第 5 章：第 1 幕鸣人自言自语、雏田接话；
        第 2 幕佐助挑衅、小樱回击——**本轮没轮到雏田，
        所以时间线上不出现任何关于雏田的记录**（沉默不进时间线）。
        """
        # 用**模块级的原始候选池**，不用本实例的登记表：
        # 预置时间线是「出厂剧情」，用户后来改了名字也不该影响它；
        # 而且这里查不到人时直接跳过，绝不能抛异常把整个 bootstrap 带崩。
        build_segment = _build_segment

        # ── 拉面店门口：10 段（有来有回），其中 2 段是剧情旁白段 ──
        ramen = [
            build_segment(1, "normal", [
                ("鸣人", "line", "任务报告交了没？今天练得累死了。", -5400),
                ("鸣人", "line", "那就好！我要去拉面店了，雏田要不要一起？", -5390),
                ("雏田", "reply", "交了……队长已经看过了，写得很详细呢。", -5370),
                ("雏田", "sticker", "【表情包·点头】", -5369),
                ("雏田", "reply", "嗯……我在图书馆这边。你别又跑那么快啦。", -5320),
            ]),
            build_segment(2, "normal", [
                ("佐助", "line", "……训练场怎么一个人都没有。", -5100),
                ("佐助", "line", "算了，我去任务大厅。", -5095),
                ("小樱", "reply", "喂！难得来一次训练场，别又板着脸啊。", -5070),
                ("小樱", "sticker", "【表情包·叉腰】", -5069),
                ("小樱", "reply", "……喂，别总绷着脸啊。同伴是要互相照应的。", -5040),
            ]),
            build_segment(3, "plot", [
                ("剧情旁白", "narrator", "三天后，第七班收到了来自上忍的紧急召集。", -4980),
                ("剧情旁白", "narrator", "办公室的窗户开着，风把桌上的任务简报吹到了地上，没有人去捡。", -4975),
            ]),
            build_segment(4, "normal", [
                ("鸣人", "line", "紧急召集？出什么事了。", -4800),
                ("鸣人", "line", "不管了，先把拉面吃完再说！", -4795),
                ("小樱", "reply", "简报上写着「立即到指挥室集合」。你打算端着半碗面去？", -4770),
                ("雏田", "reply", "鸣人君……我陪你一起去吧，路上可以边走边说。", -4740),
            ]),
            build_segment(5, "normal", [
                ("佐助", "line", "……", -4500),
                ("小樱", "reply", "喂，你到底去不去啊。", -4470),
                ("小樱", "sticker", "【表情包·生气】", -4469),
                ("雏田", "reply", "别催他。佐助在想事情，催了反而更慢。", -4440),
            ]),
            build_segment(6, "normal", [
                ("鸣人", "line", "好了好了！吃完出发！", -4200),
                ("鸣人", "line", "小樱你也快点，别迟到啊。", -4195),
                ("小樱", "reply", "我什么时候迟到过！", -4170),
                ("雏田", "reply", "那……我们在指挥室门口等大家吧。", -4140),
            ]),
            build_segment(7, "plot", [
                ("剧情旁白", "narrator", "指挥室的门开着。里面的灯没有全亮，只有长桌尽头那一盏还亮着。", -4080),
                ("剧情旁白", "narrator", "有人比他们先到了。", -4075),
            ]),
            build_segment(8, "normal", [
                ("佐助", "line", "……是你。", -3900),
                ("雏田", "reply", "卡卡西老师……很少见你比学生还早到。", -3870),
                ("小樱", "reply", "等等，那个人是不是——", -3840),
            ]),
            build_segment(9, "normal", [
                ("鸣人", "line", "卡卡西老师！你也在这儿啊！", -3600),
                ("佐助", "line", "……说说这次的任务吧。", -3595),
                ("雏田", "reply", "大家先坐好，听老师说完。", -3570),
                ("小樱", "reply", "别急。听完再讨论，比乱猜省时间。", -3540),
            ]),
            build_segment(10, "normal", [
                ("鸣人", "line", "明白了！交给我吧！", -3300),
                ("佐助", "line", "……我和你一组。别拖后腿。", -3295),
                ("小樱", "reply", "又来了又来了，每次都这样。", -3270),
                ("雏田", "reply", "那……大家要平安回来哦。", -3240),
            ]),
        ]

        mission = [
            build_segment(1, "normal", [
                ("鸣人", "line", "今天的任务是什么来着？", -2400),
                ("小樱", "reply", "翻墙。十分钟。就你话多。", -2380),
                ("小樱", "sticker", "【表情包·叹气】", -2379),
            ]),
            build_segment(2, "plot", [
                ("剧情旁白", "narrator", "十分钟后，他们站在墙根下，看着足有二十米的高度。", -2300),
            ]),
            build_segment(3, "normal", [
                ("佐助", "line", "……我说什么来着。", -2200),
                ("小樱", "reply", "叫你别接这个任务啊笨蛋！", -2180),
            ]),
        ]

        school = [
            build_segment(1, "normal", [
                ("鸣人", "line", "伊鲁卡老师！今天的考试是分身术吗！", -1800),
                ("雏田", "reply", "……鸣人君，那是上忍的测试。", -1780),
            ]),
            build_segment(2, "normal", [
                ("佐助", "line", "分身术不难。", -1500),
                ("雏田", "reply", "你当然觉得不难……你的查克拉是别人的三倍。", -1480),
            ]),
        ]

        return {
            "room_ramen": ramen,
            "room_mission": mission,
            "room_school": school,
        }

    @staticmethod
    def _seeded_private_threads() -> List[Dict[str, Any]]:
        """私聊入口的假数据（FR-02）。

        私聊是**与群聊并列的独立入口**：没有「房间」概念，只有「和谁私聊」。

        每个线程 = 一个 Bot 对方 × 一个我方角色。
        页面切「消息发送」里的角色下拉时，聊天窗口就切到对应组合的那份记录
        （所以同一个 Bot 配不同角色，看到的私聊内容是不一样的）。
        """
        return [
            {
                "thread_id": "private_hinata_naruto",
                "peer_member_id": "bot_hinata",
                "peer_name": "雏田",
                "peer_kind": "bot",
                "peer_status": STATUS_READY,
                "peer_status_label": STATUS_LABELS[STATUS_READY],
                "my_member_id": "role_naruto",
                "my_name": "鸣人",
                "segments": [
                    _build_segment(1, "normal", [
                        ("鸣人", "line", "今天任务结束得早，一起走？", -240),
                        ("雏田", "reply", "嗯……我、我在图书馆这边。你别又跑那么快啦。", -212),
                    ]),
                    _build_segment(2, "normal", [
                        ("鸣人", "line", "那你快点出来，我去占位子。", -200),
                        ("雏田", "reply", "好、好的……我马上就来。", -180),
                    ]),
                ],
            },
            {
                "thread_id": "private_hinata_sasuke",
                "peer_member_id": "bot_hinata",
                "peer_name": "雏田",
                "peer_kind": "bot",
                "peer_status": STATUS_READY,
                "peer_status_label": STATUS_LABELS[STATUS_READY],
                "my_member_id": "role_sasuke",
                "my_name": "佐助",
                "segments": [
                    _build_segment(1, "normal", [
                        ("佐助", "line", "……图书馆那边，有多余的椅子吗。", -190),
                        ("雏田", "reply", "有、有的……但是鸣人君刚才好像占掉了两个。", -160),
                    ]),
                ],
            },
            {
                "thread_id": "private_sakura_sasuke",
                "peer_member_id": "bot_sakura",
                "peer_name": "小樱",
                "peer_kind": "bot",
                "peer_status": STATUS_STOPPED,
                "peer_status_label": STATUS_LABELS[STATUS_STOPPED],
                "my_member_id": "role_sasuke",
                "my_name": "佐助",
                "segments": [
                    _build_segment(1, "normal", [
                        ("佐助", "line", "喂。刚才在群里那事，别放在心上。", -120),
                        ("小樱", "reply", "哼，谁放心不下了。我只是不想在拉面店门口被队长看到而已。", -95),
                    ]),
                    _build_segment(2, "normal", [
                        ("佐助", "line", "……那就好。", -80),
                        ("小樱", "reply", "什么那就好！你今天到底怎么了啦。", -60),
                    ]),
                ],
            },
            {
                "thread_id": "private_sakura_naruto",
                "peer_member_id": "bot_sakura",
                "peer_name": "小樱",
                "peer_kind": "bot",
                "peer_status": STATUS_STOPPED,
                "peer_status_label": STATUS_LABELS[STATUS_STOPPED],
                "my_member_id": "role_naruto",
                "my_name": "鸣人",
                "segments": [
                    _build_segment(1, "normal", [
                        ("鸣人", "line", "小樱！今天的拉面我请客！", -110),
                        ("小樱", "reply", "……你上次也是这么说的，然后钱包空了三天。", -88),
                    ]),
                ],
            },
            # ── 私聊演示数据：让「最近聊过排序」有明显效果 ──
            # offset 是相对现在的秒数，决定消息上的 HH:MM；越接近 0 越靠前。
            # 鸣人视角：卡卡西最近 → 雏田 → 小樱；井野/鹿丸没聊过（兜底按首字母）。
            {
                "thread_id": "private_kakashi_naruto",
                "peer_member_id": "bot_kakashi",
                "peer_name": "卡卡西",
                "peer_kind": "bot",
                "peer_status": STATUS_READY,
                "peer_status_label": STATUS_LABELS[STATUS_READY],
                "my_member_id": "role_naruto",
                "my_name": "鸣人",
                "segments": [
                    _build_segment(1, "normal", [
                        ("鸣人", "line", "卡卡西老师，今天的训练能早点结束吗？", -540),
                        ("卡卡西", "reply", "「早点结束训练」……嗯，今天破例一次吧。", -530),
                    ]),
                    _build_segment(2, "normal", [
                        ("鸣人", "line", "太好了！我请你吃拉面！", -120),
                        ("卡卡西", "reply", "哈哈，那我就不客气了。", -110),
                    ]),
                ],
            },
            # 佐助视角：井野最近聊过（23:05 感觉），小樱次之；其余没聊过。
            {
                "thread_id": "private_ino_sasuke",
                "peer_member_id": "bot_ino",
                "peer_name": "井野",
                "peer_kind": "bot",
                "peer_status": STATUS_GATEWAY_OFF,
                "peer_status_label": STATUS_LABELS[STATUS_GATEWAY_OFF],
                "my_member_id": "role_sasuke",
                "my_name": "佐助",
                "segments": [
                    _build_segment(1, "normal", [
                        ("井野", "line", "佐助，你今天看起来心情不错嘛。", -300),
                        ("佐助", "reply", "……没有的事。", -290),
                    ]),
                ],
            },
            # 鹿丸：只跟鸣人有一句很早的对话（演示「聊过但很久以前」）。
            {
                "thread_id": "private_shikamaru_naruto",
                "peer_member_id": "bot_shikamaru",
                "peer_name": "鹿丸",
                "peer_kind": "bot",
                "peer_status": STATUS_STOPPED,
                "peer_status_label": STATUS_LABELS[STATUS_STOPPED],
                "my_member_id": "role_naruto",
                "my_name": "鸣人",
                "segments": [
                    _build_segment(1, "normal", [
                        ("鸣人", "line", "鹿丸，陪我下棋吧！", -3600),
                        ("鹿丸", "reply", "「陪我下棋」……真麻烦。", -3580),
                    ]),
                ],
            },
        ]

    async def shell_submit_private(
        self,
        peer_member_id: str,
        lines: Optional[List[Dict[str, Any]]] = None,
        script: str = "",
    ) -> Dict[str, Any]:
        """私聊窗口的「发送」。

        和群聊一样收**结构化的行**；区别是：私聊没有「房间」，
        只有「和谁私聊」——对方固定是 ``peer_member_id`` 那个 Bot，
        没有多点名、没有随机顺序这回事。

        线程按「对方 × 我方角色」找：先认出发言人（我方角色），
        再找 (对方, 发言人) 那一对；找不到就用预置里同对方的线程当模板（内容留空）。

        返回的形状和群聊的调度计划一致（顺序 / 等待秒数 / 消息行），
        页面那边用同一套演法。
        """
        pool = self._member_map()
        peer = pool.get(peer_member_id)
        if peer is None or peer.get("kind") != "bot":
            return {"ok": False, "error": f"私聊对象不存在：{peer_member_id}"}

        # 私聊里说话的是「我」——发言人从所有我方角色里认
        user_roles = [m for m in pool.values() if m["kind"] == "user_role"]
        by_id = {m["member_id"]: m for m in user_roles}
        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for member in user_roles:
            by_name.setdefault(member["display_name"], []).append(member)

        raw: List[Dict[str, str]] = []
        if lines:
            for item in lines:
                if isinstance(item, dict):
                    raw.append(
                        {
                            "member_id": str(item.get("speaker_member_id") or "").strip(),
                            "speaker": str(item.get("speaker") or "").strip(),
                            "text": str(item.get("text") or "").strip(),
                        }
                    )
        elif script:
            for item in self._parse_script(script):
                if not item.get("invalid"):
                    raw.append({"member_id": "", "speaker": item["speaker"], "text": item["line"]})

        resolved: List[Dict[str, Any]] = []
        for index, item in enumerate(raw, start=1):
            if not item["text"]:
                continue
            member = by_id.get(item["member_id"]) if item["member_id"] else None
            if member is None and item["speaker"]:
                matches = by_name.get(item["speaker"], [])
                if len(matches) == 1:
                    member = matches[0]
            if member is None:
                who = item["speaker"] or item["member_id"] or "（空）"
                return {"ok": False, "error": f"第 {index} 行的角色「{who}」不是你的角色"}
            resolved.append(
                {
                    "member_id": member["member_id"],
                    "display_name": member["display_name"],
                    "kind": member["kind"],
                    "text": item["text"],
                }
            )
        if not resolved:
            return {"ok": False, "error": "还没有台词"}

        # 线程 = (对方, 发言人)；找不到就用同对方的预置线程当模板
        speaker_id = resolved[0]["member_id"]
        threads = self._seeded_private_threads()
        thread = next(
            (
                item
                for item in threads
                if item["peer_member_id"] == peer_member_id
                and item["my_member_id"] == speaker_id
            ),
            None,
        )
        if thread is None:
            template = next(
                (item for item in threads if item["peer_member_id"] == peer_member_id),
                None,
            )
            thread = {
                "thread_id": f"private_{peer_member_id}_{speaker_id}",
                "peer_member_id": peer_member_id,
                "peer_name": peer["display_name"],
                "my_member_id": speaker_id,
                "my_name": resolved[0]["display_name"],
                "segments": [],
            }
            if template is not None:
                thread["peer_status"] = template["peer_status"]
                thread["peer_status_label"] = template["peer_status_label"]

        self._turn_counter += 1
        seed = self._turn_counter * 7919 + 17
        rng = random.Random(seed)
        delay = max(
            DELAY_MIN_SECONDS,
            min(DELAY_MAX_SECONDS, int(round(DELAY_NAMED_SECONDS + rng.uniform(-4, 0)))),
        )
        return {
            "ok": True,
            "thread_id": thread["thread_id"],
            "peer_member_id": peer["member_id"],
            "peer_name": peer["display_name"],
            "seed": seed,
            "lines": resolved,
            "plan": [
                {
                    "member_id": peer["member_id"],
                    "display_name": peer["display_name"],
                    "kind": peer["kind"],
                    "delay_seconds": delay,
                    "lines": self._compose_reply(peer, resolved[-1]["text"], rng),
                    "silenced": False,
                    "named": True,
                }
            ],
        }
