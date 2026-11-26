# -*- coding: utf-8 -*-
"""麦麦聊天室 · 「活人感阀门」（查看频率运行时旋钮）。

**这是什么**（2026-11-26，取代 ㉘/㉙ 的 value=0 规则方案，见 09 文档 ㉚）：
宿主给插件留了官方能力 ``frequency.set_adjust`` —— 按**聊天流**随时改查看频率的
运行时乘数（``runtime._talk_frequency_adjust``，下限 0、无上限，热生效）：

- **拧到 0**：这条流上「静默自说自话」不触发（静默唤醒在触发时会重查频率，
  关着就不发作 —— ``group/scheduling.py`` 的 ``_check_silence``）；
- **拧到 1**：恢复正常（台词批概率放行 + @提及强制触发都能进 Planner）。

**阀门的开合节奏**：
- **开**：worker 递**台词**前（``ensure_group(group_id, open=True)``）——
  必须在 ``send_text`` **之前** await 成功，否则消息进流时阀门还关着，
  会被宿主「静默接收」吞掉（台词不丢：投递单还在，等下一轮重试）；
- **关**：后台回读循环每 2 秒扫一次账本，**没有在途台词投递**的群就关上
  （``sweep``）。旁白投递**不算在途**（拍板：旁白只推进剧情、不引发接话），
  私聊流不在阀门管辖内（㉘ 口径：私聊的静默续聊待用户反馈再定）。

**自愈**：每轮 sweep（2 秒）都 ``get_adjust`` 回读对账——宿主侧乘数被重置
（运行实例重建、心流实例刚建还没跟上）都能在 2 秒内纠回来；
插件重载后 ``_states`` 清空 ⇒ 首次 sweep 会对全集重新对账。
已删房间的幽灵会话直接遗忘（不再空转下发）。

**纯逻辑**：只吃一个 ``call_capability`` 异步可调用对象（注入），不直接依赖宿主 ⇒ 可离线单测。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from .identity import session_id_for_group

_log = logging.getLogger("plugin.maiknock.webui_companion")

#: 开阀的乘数值（× talk_value ⇒ 恢复正常频率）
VALVE_OPEN_VALUE = 1.0

#: 关阀的乘数值（⇒ 查看频率 0，自说自话与消息批都不再进 Planner）
VALVE_CLOSED_VALUE = 0.0

#: 开阀租约（秒）：开阀后至少保持这么久才允许 sweep 关阀 ——
#: 覆盖宿主的消息防抖（2.5~6s）+ 查看判定窗口。无 @ 的寻常消息全靠
#: 阀门开着才会被宿主收下，租约防止 sweep 在判定前把频率拧回 0。
OPEN_LEASE_SECONDS = 10.0

#: 阀门能力名（宿主 ``src/plugin_runtime/capabilities/data.py``）
CAP_FREQUENCY_SET = "frequency.set_adjust"
CAP_FREQUENCY_GET = "frequency.get_adjust"


class TalkValve:
    """按群管理查看频率乘数。``call_capability(name, **args) -> Any`` 由外部注入。

    ⚠️ **为什么每次都要回读核实**（㉛，2026-11-27 实测）：宿主的
    ``set_adjust`` 在**心流实例还不存在**时是**空转但报成功**（只打一条
    warning 日志）——阀门若信了它，会以为自己关上了，实际频率还是默认
    1.0 ⇒ 旁白/台词批照常触发，一整轮 16 回合的思考就烧起来了（实测
    一轮 100~292 秒）。⇒ 下发后必须 ``get_adjust`` 回读对账：
    - 对上了 ⇒ 记账（60 秒内不再重复下发）；
    - 对不上 ⇒ **不记账**，下一轮 sweep（2 秒后）重试，直到真正生效。
    顺带把「宿主运行实例重建把乘数重置回 1.0」的漂移也兜住了。
    """

    def __init__(
        self,
        call_capability: Callable[..., Awaitable[Any]],
        *,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._call = call_capability
        self._log = log or _log
        # session_id -> 最后一次核实在位的开合状态（True=开）
        self._states: Dict[str, bool] = {}
        # session_id -> 最后一次成功下发的时间戳（用于周期重发）
        self._last_set: Dict[str, float] = {}
        # session_id -> 租约到期（monotonic）：开阀后至少保持这么久，
        # 覆盖宿主的消息防抖（2.5~6s）+ 查看判定窗口（㉝：无 @ 的寻常消息
        # 全靠阀门开着才不会被静默吞掉，不能让 sweep 提前关阀）
        self._lease_until: Dict[str, float] = {}

    # ---- 状态查询（给日志/调试用） ----

    def known_sessions(self) -> Dict[str, bool]:
        """{session_id: 是否开着}（插件内存态，重载即清零）。"""
        return dict(self._states)

    # ---- 宿主侧实际值 ----

    async def _get_adjust(self, session_id: str) -> Optional[float]:
        """读宿主侧当前乘数。读不到（RPC 失败/被拒/形状不认识）⇒ None。

        ⚠️ SDK 对 ``frequency.get_adjust`` 有专属解包
        （``_CAPABILITY_RESULT_KEYS`` 里映射到 ``value``）⇒
        正常返回是 **float 本身**；老版本/异常路径可能是
        ``{"success": True, "value": …}`` 或 ``{"success": True, "result": {…}}``
        的包裹形状 —— 三种都认，别只会读一种（实测：只按 dict 解析
        会恒拿到 None ⇒ 阀门每 2 秒盲发一次 set，日志刷屏）。
        """
        try:
            result = await self._call(CAP_FREQUENCY_GET, chat_id=session_id)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("[聊天室] 阀门回读失败（session=%s）：%r", session_id[:12], exc)
            return None
        # 形状 1：SDK 已解包 ⇒ float 本身
        if isinstance(result, (int, float)) and not isinstance(result, bool):
            return float(result)
        if not isinstance(result, dict):
            return None
        # 形状 2：原始 dict {"success":…, "value":…}
        inner = result
        # 形状 3：RPC 包裹 {"success":…, "result": {"success":…, "value":…}}
        if "value" not in inner and isinstance(inner.get("result"), dict):
            inner = inner["result"]
        if inner.get("success") is False:
            return None
        try:
            return float(inner.get("value"))
        except (TypeError, ValueError):
            return None

    # ---- 单群操作 ----

    async def set_session(self, session_id: str, open_: bool) -> bool:
        """直接对会话下发乘数。宿主接受返回 True（**不代表真正生效**，要靠回读核实）。"""
        value = VALVE_OPEN_VALUE if open_ else VALVE_CLOSED_VALUE
        try:
            result = await self._call(CAP_FREQUENCY_SET, chat_id=session_id, value=value)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("[聊天室] 阀门下发失败（session=%s open=%s）：%r", session_id[:12], open_, exc)
            return False
        # SDK 把该能力归一化成 bool（``_BOOLEAN_SUCCESS_CAPABILITIES``）；
        # 异常路径可能给 {"success": False, ...}（陷阱 6：失败不抛异常，必须自己判）
        if result is False or (isinstance(result, dict) and result.get("success") is False):
            self._log.warning(
                "[聊天室] 阀门下发被拒（session=%s open=%s）：%s",
                session_id[:12], open_, result,
            )
            return False
        return True

    async def ensure_session(self, session_id: str, open_: bool) -> bool:
        """把会话的实际乘数拧到期望位置。返回 True = 已核实就位。

        ⚠️ 期望「关」而对不上 ⇒ **不记账**（下轮 sweep 重试）；
        期望「开」而心流实例还没建（回读恒 1.0 = 开）⇒ 视为就位
        （新实例的默认乘数就是 1.0，语义一致）。
        """
        desired = VALVE_OPEN_VALUE if open_ else VALVE_CLOSED_VALUE
        actual = await self._get_adjust(session_id)
        if actual is not None and abs(actual - desired) < 1e-9:
            self._states[session_id] = bool(open_)
            self._last_set[session_id] = self._last_set.get(session_id, 0.0)
            return True
        if not await self.set_session(session_id, open_):
            return False
        self._last_set[session_id] = time.monotonic()
        actual = await self._get_adjust(session_id)
        if actual is not None and abs(actual - desired) > 1e-9:
            # 下发被宿主空转了（心流实例还没建）⇒ 不记账，等下一轮重试
            self._log.warning(
                "[聊天室] 阀门下发未生效（session=%s open=%s，实际 %s）——下轮重试",
                session_id[:12], open_, actual,
            )
            if open_:
                # 期望开而实例未建 ⇒ 默认就是开 ⇒ 语义上等价就位
                self._states[session_id] = True
                return True
            self._states.pop(session_id, None)
            return False
        self._states[session_id] = bool(open_)
        return True

    async def ensure_group(self, group_id: str, open_: bool) -> bool:
        """把某个虚拟群的阀门拧到期望位置（worker 递台词前用）。"""
        session = session_id_for_group(group_id)
        ok = await self.ensure_session(session, open_)
        if ok and open_:
            # ⚠️ 开阀成功 ⇒ 立租约：这段时间内 sweep 不许关（覆盖宿主防抖判定）。
            self._lease_until[session] = time.monotonic() + OPEN_LEASE_SECONDS
        return ok

    # ---- 后台收口 ----

    async def sweep(self, managed_session_ids: Iterable[str], open_session_ids: Iterable[str]) -> None:
        """按「聊天室管的会话全集」收口：在途 ⇒ 开，不在途 ⇒ 关。

        ⚠️ 必须传**全集**（聊天室房间对应的全部 session），不能只传在途的——
        否则插件重载后 ``_states`` 为空 ⇒ 没有已知会话 ⇒ 一轮也不下发，
        宿主侧乘数停在默认 1.0 ⇒ 重载后到第一句台词之间自言自语还活着。

        ⚠️ **已知但不在全集里的会话直接遗忘**（房间已删 ⇒ 群流已成幽灵，
        再对它下发只会每 60 秒刷一条「未找到聊天流」的 warning）。
        仅当全集非空时才遗忘（账本读挂了不该把记账全清了）。
        """
        managed = {str(s) for s in managed_session_ids if str(s)}
        open_sessions = {str(s) for s in open_session_ids if str(s)}
        if managed:
            for ghost in list(self._states):
                if ghost not in managed:
                    self._states.pop(ghost, None)
                    self._last_set.pop(ghost, None)
                    self._lease_until.pop(ghost, None)
        for session in sorted(managed | set(self._states)):
            desired = session in open_sessions
            # ⚠️ **开阀租约**：刚开过阀的会话在租约期内不许关 ——
            #    无 @ 的寻常消息靠阀门开着的窗口被宿主收下，
            #    投递一被 collected 就关阀会把还没做完的防抖判定掐死。
            if not desired and time.monotonic() < self._lease_until.get(session, 0.0):
                continue
            if not desired:
                self._lease_until.pop(session, None)
            try:
                await self.ensure_session(session, desired)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("[聊天室] 阀门收口异常（session=%s）：%r", session[:12], exc)
