# -*- coding: utf-8 -*-
"""麦麦聊天室 · WebUI API 层（18 个接口，转发给 ChatroomService）。

⚠️ 这层**只做转发**：签名与 @API 装饰器与壳阶段**逐字一致**
   （含默认值与类型标注）⇒ 页面（webui/index.html）一个字节都不用改。
⚠️ 混入类（mixin）而不是直接写在插件类里：
   * 聊天室的接口面全部关在子包里，plugin.py 不被 18 个方法撑爆；
   * 宿主用 `dir(instance)` 收集组件（maibot_sdk/components.py:788），
     **MRO 上的方法都会被收进去** ⇒ 混入类一样能被 WebUI 调用；
   * 子包加载失败时，plugin.py 会换成桩类 ⇒ **敲门本体照常工作**。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from maibot_sdk import API


class ChatroomApiMixin:
    """麦麦聊天室的 WebUI 接口面（混入插件类）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 延迟到真正用到时再建 Service（避免半构造的插件实例被提前持有）
        self._chatroom = None

    #: 这些接口会**改动**聊天室的房间/群员/名单 ⇒ 返回成功后要写回账本
    _MUTATING = {
        "shell_create_room", "shell_rename_room", "shell_delete_room",
        "shell_add_member", "shell_remove_member", "shell_replace_member",
        "shell_create_member", "shell_update_member",
        "shell_add_my_role", "shell_delete_my_role", "shell_reorder_my_roles",
        "shell_add_peer", "shell_delete_peer",
    }

    async def _svc_call(self, name: str, *args, **kwargs):
        """调服务的方法；**改动类**的成功结果顺手写回账本。

        ⚠️ **为什么写回要挂在这一层**：改动点散落在十几个接口里，
        逐个加 save 迟早漏一个 ⇒ 漏一个就是「这个操作重启后不见了」。
        这里统一兜住：**只要结果里 ok=True 就存**。
        """
        # ⚠️⚠️ **组件调用期间置 busy 标志** ——
        #    插件→宿主的 RPC（回读用的 message.get_recent）与宿主→插件的 RPC
        #    **抢同一条 IPC 通道** ⇒ 两者重叠时互相等 ⇒ **死锁**，
        #    表现为：页面所有查询超时 + 宿主日志「Runner 健康检查失败」。
        #    ⇒ 回读循环看到 busy 就跳过这一轮，**绝不与组件调用重叠**。
        # ⚠️⚠️ **等在飞的「插件→宿主 RPC」结束**（回读用的 message.get_recent）。
        #    两条方向抢同一条 IPC 通道 ⇒ 重叠就互相等 ⇒ 死锁
        #    （实测：页面所有查询超时 + 宿主报「Runner 健康检查失败」）。
        #    busy 标志只能挡「新发起」，挡不住「已在飞」⇒ 这里补上互斥。
        #
        #    ⚠️ **这个 Event 与组件调用在同一个 Runner loop 上**（都是
        #    ``asyncio.run()`` 里的协程）⇒ 等得到，不是跨 loop 的坑。
        #    ⚠️ 正常情况下**立即返回**（回读绝大多数时候没在飞）；
        #    2 秒只是**上限**，防止回读卡死时把组件调用一起拖死。
        #    ⇒ 「页面查询慢」不要往这里找：真正的原因是账本里的数据本身坏了
        #    （见 ledger._decode_config_json 的注释：存/取不对称 ⇒ 单行涨到 512 MiB）。
        collector = getattr(self, "_chatroom_collector", None)
        idle = getattr(collector, "rpc_idle", None)
        if idle is not None:
            try:
                await asyncio.wait_for(idle.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        self._chatroom_api_busy = getattr(self, "_chatroom_api_busy", 0) + 1
        try:
            result = await getattr(self._svc(), name)(*args, **kwargs)
            if name in self._MUTATING and isinstance(result, dict) and result.get("ok"):
                await self._svc().persist_state()
            return result
        finally:
            self._chatroom_api_busy = max(0, getattr(self, "_chatroom_api_busy", 1) - 1)

    def _svc(self):
        """拿中控服务实例。"""
        if self._chatroom is None:
            from .service import ChatroomService

            self._chatroom = ChatroomService(self)
        return self._chatroom

    @API("shell_bootstrap", description="读取剧本聊天室的初始假数据", version="1")
    async def shell_bootstrap(self) -> Dict[str, Any]:
        """一次性拉取页面需要的全部初始假数据。"""
        return await self._svc_call("shell_bootstrap", )

    @API("shell_ack_errors", description="确认已看到的上游错误（不再重复提示）", version="1")
    async def shell_ack_errors(self, report_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """把错误标记为「用户已看到」。

        ⚠️ **只在用户真的看到提示时才调** —— 标了就再也不会提示；
        静默失败会退化成「提示也没了 ⇒ 没人知道出过错」。
        """
        return await self._svc_call("shell_ack_errors", report_ids)

    @API("shell_errors", description="读取最近的上游错误（429 限流等）", version="1")
    async def shell_errors(self, limit: int = 5) -> Dict[str, Any]:
        """取最近的**未确认**上游错误。

        ⚠️ 页面靠它轮询 ⇒ 宿主报错时**用户马上看得到**，
        不用等下一次刷新或下一次提交才发现「Bot 刚才没接上话」。
        """
        return {"errors": await self._svc_call("_chatroom_errors", limit=limit)}

    @API("shell_private_threads", description="读取私聊线程列表（懒加载）", version="1")
    async def shell_private_threads(self) -> Dict[str, Any]:
        """私聊窗口的线程列表。

        ⚠️ **懒加载**：bootstrap 一次带全部线程会超过宿主 API 上限（512 KiB）。
        """
        return await self._svc().shell_private_threads()

    @API("shell_timeline", description="读取某个房间的时间线（按房间懒加载）", version="1")
    async def shell_timeline(self, room_id: str) -> Dict[str, Any]:
        """一个房间的时间线。

        ⚠️ **按房间懒加载**：一次把全部房间的完整正文塞进 bootstrap
        会超过宿主的 API 上限（512 KiB，实测报「JSON 超过 512 KiB」）。
        """
        return await self._svc().shell_timeline(room_id)

    @API("shell_rooms", description="读取房间列表（假数据）", version="1")
    async def shell_rooms(self) -> Dict[str, Any]:
        """单独刷新房间列表用。"""
        return await self._svc_call("shell_rooms", )

    @API("shell_my_roles", description="读取我的角色列表（有序，假数据）", version="1")
    async def shell_my_roles(self) -> Dict[str, Any]:
        """「我的角色」= 我自己演的角色，全局一份、有顺序。"""
        return await self._svc_call("shell_my_roles", )

    @API("shell_add_my_role", description="新建一个我的角色并登记（假数据）", version="1")
    async def shell_add_my_role(
        self,
        display_name: str,
        avatar: str = "",
        persona_label: str = "",
    ) -> Dict[str, Any]:
        """在**上游**「我的角色」里新建一个角色。

        上游登记过的角色，下游（房间群员、剧本框的角色下拉）才选得到。
        """
        return await self._svc_call("shell_add_my_role", display_name, avatar=avatar, persona_label=persona_label)

    @API("shell_add_peer", description="新建一个私聊对象（Bot）并登记（假数据）", version="1")
    async def shell_add_peer(self, bot_id: str) -> Dict[str, Any]:
        """把宿主**受管 Bot**加进上游「私聊对象」。

        可选范围只有受管 Bot（麦麦一号 / 麦麦二号）—— 不能凭空造一个没有的 Bot。
        已经加过的会明确告诉你。
        """
        return await self._svc_call("shell_add_peer", bot_id)

    @API("shell_delete_peer", description="删掉一个私聊对象（假数据）", version="1")
    async def shell_delete_peer(self, member_id: str) -> Dict[str, Any]:
        """从「私聊对象」里删掉一个 Bot（也会从各房间的成员名单里移除）。"""
        return await self._svc_call("shell_delete_peer", member_id)

    @API("shell_reorder_my_roles", description="调整我的角色的顺序（假数据）", version="1")
    async def shell_reorder_my_roles(self, order: Optional[List[str]] = None) -> Dict[str, Any]:
        """把「我的角色」按给定顺序重排；没提到的排到后面，保持相对顺序。"""
        return await self._svc_call("shell_reorder_my_roles", order=order)

    @API("shell_delete_my_role", description="删除一个我的角色（假数据）", version="1")
    async def shell_delete_my_role(self, member_id: str) -> Dict[str, Any]:
        """删除一个「我的角色」。

        会同时从群员登记表、我的角色顺序表、以及**所有房间**的成员名单里移除它
        —— 不然房间里会留一个查不到人的悬空成员。
        """
        return await self._svc_call("shell_delete_my_role", member_id)

    @API("shell_create_room", description="新建剧本房间（假数据）", version="1")
    async def shell_create_room(self, name: str) -> Dict[str, Any]:
        """新建房间；只存在于内存，插件重载即消失。

        用户输入错误用 ``ok=False`` 返回业务结果，不抛异常——
        宿主会把异常翻译成「插件 API 执行失败」，反而看不出错在哪。
        """
        return await self._svc_call("shell_create_room", name)

    @API("shell_rename_room", description="重命名剧本房间（假数据）", version="1")
    async def shell_rename_room(self, room_id: str, name: str) -> Dict[str, Any]:
        """重命名房间；只存在于内存，插件重载即恢复。"""
        return await self._svc_call("shell_rename_room", room_id, name)

    @API("shell_add_member", description="往房间里添加一个群员（假数据）", version="1")
    async def shell_add_member(self, room_id: str, member_id: str) -> Dict[str, Any]:
        """把**上游已有**的一个角色加进房间。

        上游 = 「我的角色」+「私聊对象」。下游（房间群员）不能出现上游没有的角色 ——
        必须先在上游添加好，这里才选得到。
        """
        return await self._svc_call("shell_add_member", room_id, member_id)

    @API("shell_remove_member", description="从房间里移除一个群员（假数据）", version="1")
    async def shell_remove_member(self, room_id: str, member_id: str) -> Dict[str, Any]:
        """把一个成员移出房间（时间线上他已有的发言保留）。"""
        return await self._svc_call("shell_remove_member", room_id, member_id)

    @API(
        "shell_replace_member",
        description="替换房间里的一个群员（先删后增的封装，假数据）",
        version="1",
    )
    async def shell_replace_member(
        self, room_id: str, old_member_id: str, new_member_id: str
    ) -> Dict[str, Any]:
        """把房间里的一个成员换成另一个 —— 实现上就是「先删后增」。

        放在一个接口里做，是为了保证这步是原子的：
        中间任何一步失败都不会留下「旧人没了新人也没进来」的空档。
        新成员如果本来就在房间里（比如刚自建的），会先把它挪走再插到旧成员的位置上，
        这样「用谁顶替谁」不会因为新人已在场而做不了。
        """
        return await self._svc_call("shell_replace_member", room_id, old_member_id, new_member_id)

    @API("shell_delete_room", description="删除一个剧本房间（假数据）", version="1")
    async def shell_delete_room(self, room_id: str) -> Dict[str, Any]:
        """删掉一个房间。

        房间删了之后，时间线里那些发言也跟着没（房间的存档本来就是房间自己的）。
        至少要留一个房间 —— 全删了页面就没舞台了。
        """
        return await self._svc_call("shell_delete_room", room_id)

    @API("shell_truncate_from", description="「回到此处」：软作废某段及之后的全部内容", version="1")
    async def shell_truncate_from(self, room_id: str, cutoff_seq: int) -> Dict[str, Any]:
        """把段序号 ≥ ``cutoff_seq`` 的段（连同消息行）**软作废**。

        ⚠️ **不物理删**（FR-12）：只是 ``voided=1``，查询时过滤掉。
        ⚠️ 粒度 = **段**，明确拒绝按单条消息回滚（段内半截状态会让账目混乱）。
        ⚠️ mock 模式下账本里的房间可能不存在 ⇒ 服务端返回 ok 但不做事，
        由前端做本地作废（壳阶段一直是这么演的）。
        """
        return await self._svc_call("shell_truncate_from", room_id, cutoff_seq)

    @API("shell_submit_narrator", description="时光机「生成剧情」：把剧情事件做成旁白投进虚拟群", version="1")
    async def shell_submit_narrator(
        self, room_id: str, lines: Optional[List[str]] = None, number: int = 0, unit: str = ""
    ) -> Dict[str, Any]:
        """生成一段旁白（固定身份「剧情旁白」）并投进虚拟群。

        ⚠️ 时间单位只变成「三天后」这类**相对表述**，**绝不写绝对日期**（R6-18）。
        ⚠️ 旁白**占一个普通段位**，可被「回到此处」截断（FR-17）。
        """
        return await self._svc_call("shell_submit_narrator", room_id, lines or [], number, unit)

    @API(
        "shell_update_member",
        description="改群员的名字 / 头像（只限我方角色，假数据）",
        version="1",
    )
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
        return await self._svc_call("shell_update_member", member_id, display_name=display_name, avatar=avatar, room_id=room_id)

    @API(
        "shell_create_member",
        description="新建一个群员并加进房间（假数据）",
        version="1",
    )
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
        return await self._svc_call("shell_create_member", room_id=room_id, display_name=display_name, kind=kind, avatar=avatar, persona_label=persona_label)

    @API(
        "shell_submit_beat",
        description="提交一幕剧本，返回本轮调度计划（假数据）",
        version="1",
    )
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
        **真实的递话与收话不在本阶段范围**，这里只是把「未来会发生什么」
        提前算好交给页面按节奏演出来。
        """
        return await self._svc_call("shell_submit_beat", room_id, lines=lines, script=script, speakers=speakers, auto_reply=auto_reply)

    @API(
        "shell_submit_private",
        description="提交一句私聊剧本，返回对方的假回复计划（假数据）",
        version="1",
    )
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
        return await self._svc_call("shell_submit_private", peer_member_id, lines=lines, script=script)

