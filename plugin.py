# -*- coding: utf-8 -*-
"""麦麦敲门 MaiKnock —— WebUI 原生私聊伴侣插件。

功能规划：
- F1 主动私聊：静默超时后让麦麦主动开口（官方 maisaka.proactive.trigger 能力）
- F2 虚构时间线：/mk time 推进剧情时间，钩子改写注入给模型的「当前时间」与相对时间标注
- F3 手动打招呼：/mk hi 立即让麦麦开口

版本号单一来源：见下方 VERSION 常量（审计 A-3：此前版本号散落 6 处手动编辑——
文档串 / config_version / 加载日志 / help / _manifest.json / config.toml，极易漂移）。
_manifest.json 与 config.toml 无法引用 Python 常量，改由启动自检
_check_version_consistency() 比对并告警。

本版变更要点（M2 扫描循环 + 审计 A 系列修复）：
- M2 扫描循环落地：_scan_loop / _scan_once / _in_quiet_hours（跨天安全）/
  _daily_quota_exceeded / _bump_daily_count / _silence_duration_seconds；
  幂等启动（on_load / on_config_update / handle_mk 三入口兜底），on_unload 取消任务
- 抽取 _do_proactive_trigger 公共链路，/mk hi 与扫描循环共用锁位/回读/计数
- A-1 修复：锁位登记提到 await trigger 之前（先占位落盘、失败回滚）——
  原实现 read-modify-write 跨了 await，并发窗口内可双触发、冷却值被旧快照覆盖
- A-2 修复：静默判定直连底层 message.get_recent 显式传 hours——SDK 封装只发
  chat_id+limit，宿主恒用 24h 窗口，24h 外取不到（睡了一夜她不会主动找你）
- A-4/A-5/A-6：配置页文案改真、daily_max 默认值与落盘对齐、exclude_texts 标注为预留位

前一版（0.1.4）13:24 审计加固：
- 回读排除完备化：所有 /mk 回复统一加【MaiKnock】前缀（止血①），
  此前 bind/diag/time/help 回复不带前缀，回读窗口内敲它们会被误判成麦麦开口
- 防骚扰锁位落盘：last_trigger（per-stream）+ last_trigger_global（全局层）入
  runtime_state.json——热重载清空内存态后冷却依然有效
- 全局冷却层：防打扰是用户级不是流级，跨流共享同一冷却（多流前置设计）
- on_unload 取消未完成回读任务（热重载防孤儿化）
- 0.1.3 = M3 最小闭环（/mk hi 手动触发主动开口）：
- /mk hi：调用 maisaka.proactive.trigger 让麦麦立刻主动开口（支持 <话头> 参数）
  - F1-2：触发不保证开口——后台轮询回读 get_recent 确认她是否真开口，结果以
    「【MaiKnock 回读】」消息发回聊天流；未响应记入 runtime_state 计数
  - F1-4（最小版）：per-stream 冷却 + pending 锁位，防止连环触发互相覆盖
  （将来 F1 定时器与 /mk hi 共用同一组锁位）
- 0.1.2 = M1.6 评审修复 + 附录 C（N-1/N-2）：
- B-1 config_model 已挂载（WebUI 配置页 + config.toml 生成）
- B-2 移除宿主从不调用的 on_enable/on_disable，初始化挪进 on_load（保留惰性初始化兜底热重载）
- B-3 on_config_update 签名对齐宿主调用（scope, config_data, version）
- B-4 后半 命令回复改走 ctx.send.text 直发（旧路径 cmd_result 只进日志不投递）
- B-6 子命令解析前归一化 @机器人 前缀
- B-7 移除与硬编码 pattern 脱节的 command_prefix 配置项
- B-8 移除冗余的 get_config_reload_subscriptions 覆写
- N-1（评审 C.1）命令消息过滤助手：get_recent 拉回后排除 is_command/is_notify，
  否则 /mk 命令会把 F1 静默计时器归零（diag 已集成验证）
- N-2（评审 C.2）状态存储拆分 bindings.json / runtime_state.json + 原子写（tmp+os.replace），
  兼容迁移旧 state.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
# ⚠️ `HookMode` / `HookOrder` **不在 maibot_sdk 顶层导出**，只在 components 里
from maibot_sdk.components import HookMode, HookOrder
from pydantic import ValidationError

# Runner 的 IPC 日志白名单只放行 "plugin." / "plugin_runtime." / "_maibot_plugin_" 前缀
# （src/plugin_runtime/runner/log_handler.py:66）——自定义名（如 "maiknock"）会绕过
# 宿主日志管线、不走 IPC（P3，15:50 审计）。改为规范名 plugin.<plugin_id>，
# 与 SDK ctx.logger 的命名一致（maibot_sdk/context.py:235）。
logger = logging.getLogger("plugin.maiknock.webui_companion")

#: replyer 侧 blob 的块起点正则（行首锚定）——
#: ⚠️ **只能用正则**，不能用字面量 "[HH:MM:SS]"（实际是 `[16:50:07]` 这种真实时刻）。
_MK_BLOCK_START_RE = re.compile(r"(?m)^\[\d{2}:\d{2}:\d{2}\]")

# 插件版本号唯一来源（审计 A-3）：Python 侧一律引用此常量；
# _manifest.json / config.toml 无法引用，由 _check_version_consistency() 启动自检比对告警。
VERSION = "0.2.0"

# ---------------------------------------------------------------------------
# F2 虚构时间线：纯逻辑模块 timeline.py
#
# 为什么单独成文件：timeline.py 是**无 IO、无宿主依赖**的纯函数集，
# 可以离线单测（temp/mk_timeline_test.py），不必启动麦麦就能验算时间轴。
#
# 为什么不用 `import timeline`：宿主用 spec_from_file_location 以「包」的方式加载插件
# （plugin_runtime/runner/plugin_loader.py:594，submodule_search_locations=插件目录），
# 但临时加进 sys.path 的是插件的**父目录**而非插件目录本身 →
# 裸 `import timeline` 在插件目录有同名文件时不可靠。
# 故按文件路径显式加载：与宿主加载插件主模块的方式同源，且热重载时能拿到最新代码。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 加载失败只降级，不阻断插件其余功能
    import importlib.util as _importlib_util
    import sys as _sys

    # ⚠️ 必须**先**加载 date_forms.py 并注册进 sys.modules：
    # timeline.py 顶部 `from date_forms import ...` 依赖它。
    # 而宿主加载插件时 sys.path **不一定**含插件目录 ⇒ 裸 import 会失败
    # ⇒ timeline.py 会静默走降级分支（形态表不共用，R6-23/R6-27 的修复失效）。
    # ⇒ 这里用与 timeline 同样的方式按文件路径显式加载，先注册再执行。
    _date_forms_path = Path(__file__).parent / "date_forms.py"
    _date_forms_spec = _importlib_util.spec_from_file_location(
        "date_forms", str(_date_forms_path))
    if _date_forms_spec is None or _date_forms_spec.loader is None:
        raise ImportError(f"无法为 {_date_forms_path} 创建模块 spec")
    _date_forms_mod = _importlib_util.module_from_spec(_date_forms_spec)
    _sys.modules["date_forms"] = _date_forms_mod
    _date_forms_spec.loader.exec_module(_date_forms_mod)

    # ⚠️ **declaration.py 必须在 timeline.py 之前加载**（2026-11-21 23:45）：
    # timeline.py 的 `build_back_declaration` / `build_reverse_declaration`
    # 现在**转发**到 declaration.py（文案统一，避免两套漂移）。
    # ⇒ 顺序反了会让 timeline 永远走内联降级分支。
    _declaration_path = Path(__file__).parent / "declaration.py"
    _declaration_spec = _importlib_util.spec_from_file_location(
        "declaration", str(_declaration_path))
    if _declaration_spec is None or _declaration_spec.loader is None:
        raise ImportError(f"无法为 {_declaration_path} 创建模块 spec")
    _declaration_mod = _importlib_util.module_from_spec(_declaration_spec)
    _sys.modules["declaration"] = _declaration_mod
    _declaration_spec.loader.exec_module(_declaration_mod)

    _timeline_path = Path(__file__).parent / "timeline.py"
    _timeline_spec = _importlib_util.spec_from_file_location("maiknock_timeline", str(_timeline_path))
    if _timeline_spec is None or _timeline_spec.loader is None:
        raise ImportError(f"无法为 {_timeline_path} 创建模块 spec")
    tl = _importlib_util.module_from_spec(_timeline_spec)
    # ⚠️ 必须先注册进 sys.modules 再执行：
    # timeline.py 用了 `from __future__ import annotations`（注解全是字符串），
    # dataclasses 处理字符串注解时会回查 `sys.modules.get(cls.__module__).__dict__`
    # —— 没注册的话会抛 AttributeError("'NoneType' object has no attribute '__dict__'")。
    # （19:23 实测踩过：插件能加载，但 F2 整体不可用）
    _sys.modules["maiknock_timeline"] = tl
    _timeline_spec.loader.exec_module(tl)
except Exception as _tl_exc:  # noqa: BLE001
    tl = None
    logger.error("[MaiKnock] 时间线模块 timeline.py 加载失败，F2 功能不可用：%r", _tl_exc)

# ⚠️ **R6-29：形态表降级必须可见，不能静默**
# `timeline.py` 顶部对 `from date_forms import ...` 做了 try/except 降级
# （改用本文件内的等价正则）⇒ 一旦 `date_forms.py` 加载失败，
# R6-27 的防护会**整体失效**（中文绝对日期又不设防），而插件侧**零日志、用户零感知**。
# ⇒ 这里显式检查并告警（timeline.py 是纯逻辑模块、不能打日志 ⇒ 检查必须放在这里）。
# ⚠️ logger 名在《技术事实与踩坑》第 4 条的白名单内，不要改。
if tl is not None and not getattr(tl, "_DATE_FORMS_READY", False):
    logger.warning(
        "[MaiKnock] 日期形态表加载失败，R6-27 防护已降级：绝对日期可能不被拦截。"
        "请检查 plugins/maiknock/date_forms.py 是否存在且可读。"
    )


# ---------------------------------------------------------------------------
# F2 第②步：匹配层与两侧处理器
#
# 三个模块都是**无 IO、无宿主依赖**的纯逻辑模块（可离线单测）：
# - `matcher.py`        ：把 replyer 侧 blob 的对话块对齐到真实消息（R6-30 / R6-31 / P21 / P22）
# - `blob_processor.py` ：replyer 侧（1.4.8 唯一路径）—— **先遮蔽 → 后 relabel**（R6-28）
# - `planner_processor.py`：planner 侧（逐条 item）—— **先 relabel → 再遮蔽**
#
# ⚠️ **加载顺序有依赖**（踩坑 66）：
# `blob_processor.py` 内部 `from matcher import ...` ⇒ **必须先把 matcher 注册进
# sys.modules**。裸 import 会失败（宿主 sys.path 不含插件目录）。
# ⚠️ 这里**不静默降级** —— 加载失败就明确报错，避免"防护失效但无人知晓"。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 加载失败只降级，不阻断插件其余功能
    import importlib.util as _importlib_util2
    import sys as _sys2

    def _load_sibling(module_name: str, filename: str):
        """按文件路径加载插件目录下的兄弟模块，并**先注册 sys.modules 再 exec**。"""
        path = Path(__file__).parent / filename
        spec = _importlib_util2.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {path} 创建模块 spec")
        mod = _importlib_util2.module_from_spec(spec)
        _sys2.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod

    # ⚠️ 顺序：matcher 必须先于 blob_processor
    mk_matcher = _load_sibling("matcher", "matcher.py")
    mk_blob = _load_sibling("blob_processor", "blob_processor.py")
    mk_planner = _load_sibling("planner_processor", "planner_processor.py")
    # ⚠️ declaration.py 已在**上面**（timeline 之前）加载过了 ⇒ 直接复用，不要重复加载
    mk_decl = _sys.modules.get("declaration")
    # ⚠️ carriers（relabel 载体白名单，M4 第⑤步 J）—— planner / blob 共用同一套判定
    mk_carriers = _load_sibling("carriers", "carriers.py")
    # ⚠️ **必须注入** —— planner_processor / blob_processor 都不 import 它
    #    （兄弟模块不能裸 import，踩坑 66）⇒ 由这里显式 set_carriers()
    mk_blob.set_carriers(mk_carriers)
    mk_planner.set_carriers(mk_carriers)
except Exception as _mk_exc:  # noqa: BLE001
    mk_matcher = None
    mk_blob = None
    mk_planner = None
    mk_decl = None
    mk_carriers = None
    logger.error(
        "[MaiKnock] 第②步模块（matcher / blob_processor / planner_processor / declaration / carriers）加载失败，"
        "F2 时间线改写不可用：%r",
        _mk_exc,
    )


# ---------------------------------------------------------------------------
# 配置模型（自动生成 config.toml 与 WebUI 配置页）
# 注意：__ui_label__ 仅在嵌套的 PluginConfigBase 子类上生效；
# config_version 必须位于 [plugin] 节内（宿主强制约定 runner_main.py）。
# ---------------------------------------------------------------------------


class PluginSection(PluginConfigBase):
    """插件总开关。"""

    __ui_label__ = "插件设置"

    enabled: bool = Field(default=True, description="是否启用麦麦敲门", json_schema_extra={"label": "启用插件"})
    config_version: str = Field(default=VERSION, description="配置结构版本", json_schema_extra={"label": "配置版本"})


class ProactiveConfig(PluginConfigBase):
    """F1 主动私聊的防骚扰配置。"""

    __ui_label__ = "主动私聊"

    enabled: bool = Field(default=False, description="是否启用主动私聊扫描（静默超时后麦麦主动开口）", json_schema_extra={"label": "启用主动私聊"})
    hi_cooldown_minutes: int = Field(
        default=3, ge=0, le=1440,
        description="/mk hi 手动触发的冷却（分钟，F1-4 防连环触发）", json_schema_extra={"label": "hi 冷却（分钟）"})
    readback_poll_seconds: int = Field(
        default=20, ge=5, le=120,
        description="触发后回读确认的轮询间隔（秒）", json_schema_extra={"label": "回读轮询间隔（秒）"})
    readback_timeout_seconds: int = Field(
        default=240, ge=30, le=1800,
        description="触发后等待麦麦开口的最长回读时间（秒，超时记未响应）", json_schema_extra={"label": "回读超时（秒）"})
    scan_interval_seconds: int = Field(
        default=120, ge=30, le=3600, description="扫描间隔（秒），最小 30", json_schema_extra={"label": "扫描间隔（秒）"})
    silence_threshold_minutes: int = Field(
        default=60, ge=5, le=1440, description="静默多久后麦麦可以主动开口（分钟）", json_schema_extra={"label": "静默阈值（分钟）"})
    silence_lookback_hours: int = Field(
        default=720, ge=1, le=8760,
        description=(
            "静默判定回溯窗口（小时）：在最近这么多小时的消息里找「最后一次用户发言」。"
            "默认 720 小时（30 天）——必须显著大于静默阈值，否则用户沉默一旦超过窗口，"
            "反而取不到那条发言导致永远判不出静默（睡过夜就不触发）"
        ), json_schema_extra={"label": "静默回溯窗口（小时）"})
    silence_probe_hours: int = Field(
        default=87600, ge=1, le=87600,
        description=(
            "静默判定二次探测窗口（小时）：主回溯窗口内找不到用户发言时，用这个更大的窗口"
            "再查一次，用来区分「沉默太久」（该找她了）与「从未说过话」（不该打扰）。"
            "默认 87600 小时（10 年）——上限即默认值，意味着除非用户明确调小，"
            "几乎不存在「沉默超过探测窗口」的隐含行为"
        ), json_schema_extra={"label": "静默探测窗口（小时）"})
    quiet_hours_start: str = Field(default="01:00", description="禁止时段开始（HH:MM，24 小时制）", json_schema_extra={"label": "禁言时段开始"})
    quiet_hours_end: str = Field(default="08:00", description="禁止时段结束（HH:MM，24 小时制）", json_schema_extra={"label": "禁言时段结束"})
    daily_max: int = Field(default=5, ge=1, le=50, description="每天最多主动开口次数", json_schema_extra={"label": "每日主动上限"})
    min_interval_minutes: int = Field(
        default=120, ge=10, le=1440, description="两次主动开口的最小间隔（分钟）", json_schema_extra={"label": "最小主动间隔（分钟）"})


class MaiknockConfig(PluginConfigBase):
    """麦麦敲门插件总配置。"""

    __ui_label__ = "麦麦敲门"

    plugin: PluginSection = Field(default_factory=PluginSection, description="插件开关")
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig, description="主动私聊设置")


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


class MaiKnockPlugin(MaiBotPlugin):
    # B-1：挂载配置模型，宿主据此生成 config.toml 与 WebUI 配置页
    config_model = MaiknockConfig

    def __init__(self) -> None:
        super().__init__()
        self._data_file: Optional[Path] = None
        # F1-4 最小版锁位：per-stream 上次触发时刻 + 等待回读中的流
        # 注意：内存态在热重载时会清零——冷却的权威数据在 runtime_state.json
        # （proactive_state 段），每次触发前从盘上读，写穿回盘（审计 13:24 第 16 条）
        self._proactive_last_trigger: Dict[str, float] = {}
        # pending 锁位改存 target -> stamp（B-4：15:50 审计）。
        # 原 set 无从区分「是谁加的锁」→ _rollback_lock 无条件 discard 会把并发方
        # 刚成功的 pending 误清 → 锁位失效 → 下一轮可能在别人回读窗口内再触发。
        self._proactive_pending: Dict[str, float] = {}
        # 未完成的回读任务：热重载时 on_unload 统一取消，防孤儿任务继续发消息
        self._readback_tasks: set = set()
        # M2 扫描循环任务：单例、幂等启动（热重载不走 on_load，需多入口兜底）
        self._scan_task: Optional[asyncio.Task] = None
        # 扫描日志去重：宿主只输出插件 logger 的 info 级（debug 被过滤），
        # 故跳过原因「状态变化才打」，静默时长按 5 分钟档位变化才打，避免刷屏
        self._scan_last_reason: str = ""
        self._scan_last_silence_bucket: int = -1
        # 插件配置缓存（config.get_plugin 整份取回后本地做点分查找）
        self._config_cache: Optional[Dict[str, Any]] = None
        # 连续配置读取失败计数（B-2：失败不写缓存，但连续失败要打 error）
        self._config_fail_count: int = 0
        # 配置自校验是否失败（C-2：插件侧用自己的模型再校验一遍宿主给的原始 TOML）
        self._config_invalid: bool = False

    # ---- 生命周期（B-2：宿主只调用 on_load/on_unload/on_config_update）----

    async def on_load(self) -> None:
        self._data_file = self._ensure_data_dir()
        logger.info("[MaiKnock] 插件加载：v%s，数据目录：%s", VERSION, self._data_file)
        self._check_version_consistency()
        # C-2：加载即体检——用插件自己的模型校验一遍读到的配置，
        # 让「配置非法」这件事在插件侧也留一条带字段名的日志（宿主日志有，插件侧原本零日志）
        try:
            raw_cfg = await self._plugin_config()
            self._validate_config_self(raw_cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] 加载时配置自校验未能完成：%r", exc)
        self._ensure_scan_loop()

    def _check_version_consistency(self) -> None:
        """版本号一致性自检（审计 A-3）。

        VERSION 常量是唯一来源；_manifest.json / config.toml 是静态文件、无法引用
        Python 常量，故启动时比对并告警——避免「日志说一个版本、manifest 说另一个、
        文档说第三个」这种排查时误导人的漂移。
        """
        try:
            manifest = Path(__file__).parent / "_manifest.json"
            if manifest.exists():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                mver = str(data.get("version") or "")
                if mver and mver != VERSION:
                    logger.warning(
                        "[MaiKnock] 版本不一致：plugin.py VERSION=%s，_manifest.json version=%s —— 请同步",
                        VERSION, mver,
                    )
            # B-5（15:50 审计）：config.toml 的 [plugin].config_version 也要比对——
            # 它恰恰是宿主回写配置时最会动的字段，漏检等于白检。
            cfg = Path(__file__).parent / "config.toml"
            if cfg.exists():
                cver = self._read_toml_config_version(cfg)
                if cver and cver != VERSION:
                    logger.warning(
                        "[MaiKnock] 版本不一致：plugin.py VERSION=%s，config.toml config_version=%s —— 请同步",
                        VERSION, cver,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[MaiKnock] 版本自检跳过：%s", exc)

    @staticmethod
    def _read_toml_config_version(path: Path) -> str:
        """读 config.toml 的 [plugin].config_version（无 tomllib 或解析失败时返回空串）。"""
        try:
            import tomllib
        except ImportError:  # Python < 3.11
            return ""
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            return str((data.get("plugin") or {}).get("config_version") or "")
        except Exception:  # noqa: BLE001
            return ""

    async def on_unload(self) -> None:
        """SDK 契约要求：宿主卸载插件时调用。

        热重载 = 卸载 + 重建实例：未完成的回读任务若不取消会孤儿化
        （旧 task 继续跑并可能在错误时机发消息），这里统一取消。
        """
        cancelled = 0
        for task in list(self._readback_tasks):
            if not task.done():
                task.cancel()
                cancelled += 1
        if cancelled:
            logger.info("[MaiKnock] 卸载时取消了 %d 个未完成的回读任务", cancelled)
        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()
            logger.info("[MaiKnock] 卸载时取消了扫描循环任务")
        logger.info("[MaiKnock] 已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """B-3：宿主以 3 个位置参数调用（runner_main.py:2640-2645）。"""
        logger.info("[MaiKnock] 配置已更新：scope=%s version=%s", scope, version)
        # 配置缓存失效：下次读取重新拉宿主里的插件配置（否则改完不生效）
        self._config_cache = None
        # C-2：配置变更后立刻用插件自己的模型校验一遍读到的值——
        # 宿主校验用的是「归一化后」的配置，插件读到的是「原始 TOML」，
        # 两边对同一文件的解读**可能不同**（宿主支持 normalize_plugin_config 钩子）。
        # 这里做双侧留痕：哪一项非法、什么原因，插件侧也记一份。
        try:
            raw_cfg = await self._plugin_config()
            self._validate_config_self(raw_cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] 配置变更后自校验未能完成：%r", exc)
        # M2：配置变更可能启用/调整扫描循环；幂等确保它在跑（热重载后宿主可能重新推配置）
        self._ensure_scan_loop()

    # ---- 数据存取（N-2：拆文件 + 原子写）----
    # bindings.json     —— 低频写（用户 /mk bind），绑定信息
    # runtime_state.json —— 高频写（F1 扫描循环的每日计数/上次触发时间，M2 启用）
    # 两者均为原子写（tmp + os.replace），并发或崩溃不会损坏 JSON。
    # 注意：data_dir 不含 bot 维度（build_plugin_paths 用 project_root），多 bot 同跑会共享。
    # 当前按单 bot 使用；多 bot 命名空间待 M2 确定可靠的 bot 标识后再引入。

    _BINDINGS_FILE = "bindings.json"
    _RUNTIME_FILE = "runtime_state.json"
    _LEGACY_FILE = "state.json"
    # F2 虚构时间线状态（与绑定信息、运行时状态分开存，避免互相污染）
    _TIMELINE_FILE = "timeline.json"

    def _ensure_data_dir(self) -> Path:
        """优先使用宿主统一持久化目录，回退到插件目录下 data/。"""
        try:
            data_dir = Path(self.ctx.paths.data_dir)
        except Exception:  # noqa: BLE001
            data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    def _ensure_data_dir_file(self) -> Optional[Path]:
        """惰性初始化数据目录（热重载路径不走 on_load，不能只在那里初始化）。"""
        if self._data_file is None:
            self._data_file = self._ensure_data_dir()
        return self._data_file

    def _load_json(self, filename: str) -> Dict[str, Any]:
        data_dir = self._ensure_data_dir_file()
        if data_dir is None:
            return {}
        path = data_dir / filename
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            # 迁移：旧版单文件 state.json → bindings.json
            if filename == self._BINDINGS_FILE:
                legacy = data_dir / self._LEGACY_FILE
                if legacy.exists():
                    data = json.loads(legacy.read_text(encoding="utf-8"))
                    self._write_json(self._BINDINGS_FILE, data)
                    return data
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # 审计第 18 条：只有「内容损坏」（JSON 解析失败 / 编码错误）才留证改名。
            # 瞬时 IO 故障（PermissionError / 杀软扫描 / 磁盘抖动）绝不能碰文件——
            # 否则会把一个完好的文件误改成 .corrupt，把瞬时故障升级成数据破坏。
            logger.warning("[MaiKnock] 读取 %s 失败（内容损坏）：%s", filename, exc)
            self._quarantine_corrupt(path)
        except Exception as exc:  # noqa: BLE001
            # 瞬时 IO 错误：只记 warning，绝不碰文件（第 18 条）。
            logger.warning("[MaiKnock] 读取 %s 失败（IO 错误，保留原文件）：%s", filename, exc)
        return {}

    def _quarantine_corrupt(self, path: Path) -> None:
        """把内容损坏的状态文件另存 .corrupt 留证（文件名带时间戳，避免覆盖上次证据）。"""
        try:
            if path.exists():
                corrupt = path.with_name(f"{path.name}.{int(time.time())}.corrupt")
                path.replace(corrupt)
                logger.warning("[MaiKnock] 已把损坏文件留证：%s", corrupt)
        except Exception:  # noqa: BLE001
            pass

    def _write_json(self, filename: str, data: Dict[str, Any]) -> bool:
        """原子写：先写 tmp 再 os.replace，避免并发/崩溃损坏。"""
        try:
            data_dir = self._ensure_data_dir_file()
            if data_dir is None:
                return False
            path = data_dir / filename
            # 审计二次核实（第 17 条）：tmp 名必须加 PID，否则多 bot 多 Runner 进程
            # 写同一个固定 tmp 名会互相截断，导致 os.replace 搬上半截内容 → 文件损坏。
            tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("[MaiKnock] 写入 %s 失败：%s", filename, exc)
            return False

    # 兼容旧调用名：绑定信息现在走 bindings.json
    def _load_state(self) -> Dict[str, Any]:
        return self._load_json(self._BINDINGS_FILE)

    def _save_state(self, state: Dict[str, Any]) -> bool:
        return self._write_json(self._BINDINGS_FILE, state)

    # ---- F2 虚构时间线：状态读写 ----

    def _load_timeline(self):
        """读时间线状态。

        模块不可用（timeline.py 加载失败）或数据损坏时返回 None ——
        F2 降级，不影响插件的其余功能。
        """
        if tl is None:
            return None
        try:
            return tl.TimelineState.from_dict(self._load_json(self._TIMELINE_FILE))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] 时间线状态读取失败，按未启用处理：%r", exc)
            return None

    def _save_timeline(self, state: Any) -> bool:
        if tl is None or state is None:
            return False
        try:
            return self._write_json(self._TIMELINE_FILE, state.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.error("[MaiKnock] 时间线状态写入失败：%r", exc)
            return False

    async def _send_text(self, text: str, stream_id: str) -> bool:
        """插件出站消息的**统一出口**（P9）。

        PM 拍板：回退生效期间，插件发到聊天流的**任何**消息里的绝对日期
        一律不出现在正文里 —— 否则麦麦能从插件自己的诊断消息里读出
        「现在是几月几号」，故事时间回退了三天她却知道绝对日期 → 当场穿帮。

        ⚠️ 必须做成统一出口，不能逐个命令各写一遍：
        `/mk diag` 的「最近真实消息：11-21 03:49:26」与 `/mk time` 的
        「现实时间：2026-11-21 18:57:00」**两处都会漏**，
        后者尤其隐蔽 —— 它是用户主动查询的命令，输出照样进对话历史被麦麦看到。
        """
        state = self._load_timeline()
        payload = text
        if tl is not None and state is not None:
            payload = tl.sanitize_output(state, text)
        try:
            return bool(await self.ctx.send.text(payload, stream_id))
        except Exception as exc:  # noqa: BLE001
            logger.error("[MaiKnock] 消息发送失败：%s", exc)
            return False

    # ---- 命令入口（仿绘卷 /dr：单一入口 + 子命令分发） ----
    # B-7：命令前缀固定为 /mk（pattern 由 @Command 静态声明，无法跟随配置动态生成，
    # 故不提供可改前缀的配置项，避免「改了不生效」的误导）。

    @Command(
        "mk",
        description="麦麦敲门：bind 绑定私聊流 / diag 诊断 / hi 打招呼 / time 时间线 / help 帮助",
        pattern=r"^/mk\b",
    )
    async def handle_mk(self, **kwargs: Any):
        # M2：命令入口兜底启动扫描循环（热重载不走 on_load，用户可能直接敲命令不配配置）
        self._ensure_scan_loop()
        text = str(kwargs.get("text") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")
        platform = str(kwargs.get("platform") or "")
        user_id = str(kwargs.get("user_id") or "")

        # B-6：归一化 @机器人 前缀（如「@麦麦 /mk bind」），取 /mk 起始部分
        idx = text.find("/mk")
        if idx > 0:
            text = text[idx:]

        parts = text.split(maxsplit=2)  # ["/mk", 子命令, 其余]
        sub = parts[1].lower() if len(parts) >= 2 else "help"
        rest = parts[2].strip() if len(parts) >= 3 else ""

        if sub == "bind":
            reply = self._cmd_bind(stream_id, platform, user_id)
        elif sub == "diag":
            reply = self._cmd_diag(kwargs)
            if stream_id:
                reply += "\n" + await self._cmd_diag_async_detail(stream_id)
        elif sub == "hi":
            reply = await self._cmd_hi(rest, stream_id)
        elif sub in ("time", "nowtime"):
            reply = self._cmd_time(rest)
        elif sub == "now":
            # /mk now = /mk time now（恢复到现实时间，追加反向声明）
            reply = self._cmd_time("now")
        elif sub == "help":
            reply = self._cmd_help()
        else:
            reply = f"未知的子命令：{sub}。试试 /mk help"

        # 回复统一带【MaiKnock】前缀（审计 13:24 第 15 条加固）：回读排除规则
        # 「startswith('【MaiKnock')」因此对所有插件回复完备——否则回读窗口内
        # 敲 bind/diag/time/help 会被误判成麦麦开口
        if not reply.startswith("【MaiKnock"):
            reply = f"【MaiKnock】{reply}"

        # B-4：回复经 ctx.send.text 系统直发（不过 LLM），命令本身拦截不进聊天管线
        # F2/P9：改走 _send_text 统一出口，回退生效时对正文做绝对日期脱敏
        delivered = False
        if stream_id:
            delivered = await self._send_text(reply, stream_id)
        if not delivered:
            logger.warning("[MaiKnock] 回复未能投递（stream_id=%s），内容仅记日志：%s", stream_id, reply)

        return True, reply, True

    # ---- 子命令实现 ----

    def _cmd_bind(self, stream_id: str, platform: str, user_id: str) -> str:
        if not stream_id:
            return "绑定失败：当前上下文没有聊天流 ID。请在 WebUI 私聊窗口里执行 /mk bind"
        state = self._load_state()
        old = state.get("bound_stream_id")
        state["bound_stream_id"] = stream_id
        state["bound_platform"] = platform
        state["bound_user_id"] = user_id
        state["bound_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        saved = self._save_state(state)
        logger.info("[MaiKnock] 绑定聊天流：%s (platform=%s user=%s saved=%s)", stream_id, platform, user_id, saved)
        if not saved:
            return "绑定写入失败：状态文件无法保存，请查看宿主日志。"
        if old and old != stream_id:
            return f"已换绑：\n旧流 {old}\n新流 {stream_id}"
        return f"绑定成功！\n聊天流：{stream_id}\n平台：{platform}（M2 将在此流上实现主动私聊）"

    # ---- N-1：消息过滤助手（F1 静默判定的前置件）----
    # 命令消息（is_command=True，含 /mk 自身）与通知（is_notify=True）都会入库，
    # get_recent 无法在服务端过滤——必须拉回后自行排除，
    # 否则每次敲 /mk 都会把静默计时器归零，F1 永远不触发（评审 C.1）。
    # 注意 SDK 封装 get_recent 默认 limit=10，必须显式给足。

    async def _get_real_user_messages(
        self, stream_id: str, limit: int = 50, hours: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """拉取最近消息，排除命令/通知/麦麦自己的消息，只留用户真实发言。

        评审 C.1（N-1）：命令消息（含 /mk 自身）会入库且 get_recent 无服务端过滤参数，
        不排除的话每次敲 /mk 都会把 F1 静默计时器归零。
        注意：SDK 封装 get_recent 默认 limit=10（宿主默认 100），必须显式给足；
        返回值为裸 list，is_command/is_notify 在消息顶层。

        审计 A-2：SDK 封装（maibot_sdk/capabilities/message.py:20）只发 chat_id+limit，
        宿主侧 hours 恒取默认 24.0（data.py:509 → start_time = now - hours*3600）；
        而 find_messages 的时间过滤是 WHERE 条件、与 limit 相互独立
        （message_repository.py:140 start_time / :145 limit）⇒ limit 管条数不管时间跨度，
        用 limit 替代 hours 是无效的，24 小时外取不到。
        故需要跨天跨度时直连底层能力显式传 hours（manifest 已授权 message.get_recent）。
        """
        try:
            if hours is None:
                result = await self.ctx.message.get_recent(chat_id=stream_id, limit=limit)
            else:
                result = await self.ctx.call_capability(
                    "message.get_recent",
                    chat_id=stream_id,
                    limit=limit,
                    hours=float(hours),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] get_recent 失败（stream=%s）：%s", stream_id, exc)
            return []
        msgs = result if isinstance(result, list) else (
            result.get("messages", []) if isinstance(result, dict) else []
        )
        real = []
        for m in msgs:
            if m.get("is_command") or m.get("is_notify"):
                continue
            info = m.get("message_info") or {}
            if (info.get("user_info") or {}).get("user_id") == "self":
                continue  # 麦麦自己的发言不算"用户说话"
            real.append(m)
        return real

    def _cmd_diag(self, kwargs: Dict[str, Any]) -> str:
        stream_id = str(kwargs.get("stream_id") or "")
        platform = str(kwargs.get("platform") or "")
        user_id = str(kwargs.get("user_id") or "")
        is_op = kwargs.get("is_local_operator")
        keys = sorted(kwargs.keys())
        state = self._load_state()
        bound = state.get("bound_stream_id", "（未绑定）")
        lines = [
            "MaiKnock 诊断",
            f"当前流：{stream_id or '（空）'}",
            f"平台：{platform or '（空）'}",
            f"用户：{user_id or '（空）'}",
            f"本地操作者：{is_op}",
            f"已绑定流：{bound}",
            f"上下文字段：{', '.join(keys)}",
        ]
        # ⚠️ **R6-29（建议②）**：形态表降级时让用户**看见** ——
        # 比日志更有效（用户敲 /mk diag 就能确认防护是否生效）。
        if tl is None:
            lines.append("日期形态表：模块未加载 ⚠️（F2 不可用）")
        elif not getattr(tl, "_DATE_FORMS_READY", False):
            lines.append("日期形态表：已降级 ⚠️（绝对日期可能不被拦截）")
        else:
            lines.append("日期形态表：正常")
        return "\n".join(lines)

    async def _cmd_diag_async_detail(self, stream_id: str) -> str:
        """异步补充段：最近真实用户消息（N-1 过滤验证）。"""
        try:
            result = await self.ctx.message.get_recent(chat_id=stream_id, limit=50)
            msgs = result if isinstance(result, list) else (
                result.get("messages", []) if isinstance(result, dict) else []
            )
            if not msgs:
                return "最近真实消息：（get_recent 返回 0 条）"
            real = await self._get_real_user_messages(stream_id)
            if real:
                last = real[-1]
                try:
                    ts = time.strftime("%m-%d %H:%M:%S", time.localtime(float(last.get("timestamp"))))
                except (TypeError, ValueError):
                    ts = str(last.get("timestamp"))
                text = str(last.get("processed_plain_text") or "")[:30]
                return (
                    f"最近真实消息：{ts}「{text}」\n"
                    f"过滤统计：原始 {len(msgs)} 条 → 用户真实发言 {len(real)} 条"
                    f"（已排除命令/通知/麦麦自己）"
                )
            return f"最近真实消息：（无）——原始 {len(msgs)} 条全部为命令/通知/麦麦自己的消息"
        except Exception as exc:  # noqa: BLE001
            return f"最近真实消息：查询失败 {exc!r}"

    # ---- F3：/mk hi 手动触发主动开口（M3 最小闭环）----
    # F1-2：proactive.trigger 只是"提交任务"（enqueue_proactive_task），
    #       模型自决是否开口——必须后台轮询 get_recent 回读确认。
    # F1-4：主动任务会塌缩成一轮、后到覆盖先到（reasoning_engine.py:1148），
    #       用 per-stream 冷却 + pending 锁位防连环触发互相覆盖。

    _CONFIG_FAIL_ERROR_THRESHOLD = 3

    async def _plugin_config(self) -> Dict[str, Any]:
        """取插件自身配置（整份 dict，带缓存，配置变更时失效）。

        ⚠️ 关键事实（15:30 实测发现，此前踩坑）：宿主 `config.get` 读的是
        **宿主全局主配置**（core.py:891-909 → _get_nested_config_value(global_config, key)），
        **不是插件配置**！查插件配置必须用 `config.get_plugin`（core.py:914-941）。
        此前所有 `ctx.config.get("proactive.xxx")` 都在读主配置 → 主配置里没有
        proactive 节 → 永远返回默认值 → 「用户在 WebUI 配置页改的值不生效」+
        「扫描循环 enabled 恒为 false 永不扫描」。

        注意 SDK 封装 `get_plugin()` 会显式传 plugin_name=None → 宿主
        `str(args.get("plugin_name", plugin_id))` = "None" → 找不到插件。故直连底层
        能力且**不传** plugin_name，让宿主回退到调用方 plugin_id。

        ⚠️ B-2（15:50 审计）：失败**绝不能写缓存**。宿主失败时返回
        `{"success": False, "value": None, "error": ...}`（core.py:939），
        而 SDK 因 `_CAPABILITY_RESULT_KEYS["config.get_plugin"] = "value"`
        （maibot_sdk/context.py:75）会把它规范化成 **None 且不抛异常**
        （context.py:320-323）——于是 except 分支根本不触发、插件拿到 None →
        若缓存成 {}，则「enabled 读回默认 false → 扫描永远不扫描」且**一条日志都没有**，
        症状与上面那个 bug 一模一样但更隐蔽；且空缓存要等下一次 on_config_update
        才被清 → **一次瞬时故障 = 配置读取永久失效**。
        故：失败一律**不写缓存**（保持 None，下次重试）+ 连续失败打 error。
        """
        if self._config_cache is not None:
            return self._config_cache
        try:
            data = await self.ctx.call_capability("config.get_plugin")
        except Exception as exc:  # noqa: BLE001
            self._note_config_failure(f"能力调用异常：{exc!r}")
            return {}
        if data is None:
            self._note_config_failure("宿主返回空配置（能力调用失败，宿主未抛异常）")
            return {}
        value = data
        if isinstance(data, dict) and isinstance(data.get("value"), dict):
            value = data["value"]
        if not isinstance(value, dict):
            self._note_config_failure(f"配置内容类型异常：{type(value).__name__}")
            return {}
        self._config_cache = value
        self._config_fail_count = 0
        self._validate_config_self(value)
        return self._config_cache

    def _validate_config_self(self, raw: Dict[str, Any]) -> None:
        """C-2（16:50 审计）：插件侧自校验配置。

        宿主读插件配置走 `_load_plugin_config_for_supervisor` → `tomlkit.load().unwrap()`
        （integration.py:2238-2239）→ **不跑 pydantic 校验**；宿主那份校验在
        `_apply_plugin_config` 里（runner_main.py:1063-1090 → `_normalize_plugin_config`），
        失败即 `meta.config_error` → **插件只加载配置管理、主逻辑不激活、插件侧零日志**
        （C-1 的完整证据链：runner_main.py:1759-1762 置 config_error 并打 warning →
        :1799 `if not meta.config_error:` 才调 on_load → supervisor.py:387-399 标 failed）。

        **本方法的价值定位（16:50 实测后的修正）**：宿主校验与插件校验用的是**同一个
        pydantic 模型**，且宿主先校验 → 宿主判非法时插件根本不会被激活，
        `on_load` 不执行、命令被 Runner 硬拦截（runner_main.py:2357-2359
        「插件配置错误，业务暂停」）⇒ **「宿主判非法而插件仍在跑」这个状态在设计上
        不存在**。所以本自校验**不是**为了补宿主的漏，而是：
        ① 把「哪个字段、什么原因」在**插件侧**也留一份带字段名的日志（宿主日志有，
           但插件侧零日志——排查时容易只盯一边）；
        ② 兜住「宿主归一化后合法、但原始 TOML 按插件模型解读非法」这类**解读差异**
           （宿主支持 `normalize_plugin_config` 钩子做归一化，两边对同一文件的
           解读可以不同）；
        ③ 给 `_ensure_scan_loop` 一道防线：配置自校验没过就不启动扫描循环。

        校验失败**不抛异常、不清缓存**：宿主此时已停用业务，插件侧报错即可；
        配置读取本身是成功的，缓存仍应保留（否则每次读都重试、刷日志）。
        """
        try:
            MaiknockConfig(**raw)
        except ValidationError as exc:
            details = []
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ())) or "(根)"
                details.append(f"{loc}：{err.get('msg', '')}")
            logger.error(
                "[MaiKnock] 插件配置自校验失败（%d 处）：%s —— "
                "请按上面的字段名修正 config.toml 或 WebUI 配置页"
                "（宿主侧同样会判定该插件「配置错误、仅加载配置管理」，主逻辑不激活）",
                len(details), "；".join(details),
            )
            self._config_invalid = True
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] 配置自校验异常（不影响运行）：%r", exc)
            return
        self._config_invalid = False

    def _note_config_failure(self, detail: str) -> None:
        """记录一次配置读取失败（B-2）。

        失败**不写缓存**（_config_cache 保持 None）→ 下次调用会重试；
        连续失败达阈值打 error（避免「配置永久失效」无声无息）；首次失败打 warning 留痕。
        """
        self._config_fail_count += 1
        if self._config_fail_count == 1:
            logger.warning("[MaiKnock] 读取插件配置失败（将重试）：%s", detail)
        elif self._config_fail_count == self._CONFIG_FAIL_ERROR_THRESHOLD:
            logger.error(
                "[MaiKnock] 插件配置连续读取失败 %d 次，配置可能已永久失效：%s",
                self._config_fail_count, detail,
            )
        else:
            logger.warning(
                "[MaiKnock] 插件配置读取失败（第 %d 次）：%s", self._config_fail_count, detail
            )

    async def _cfg_value(self, key: str, default: Any = None) -> Any:
        """按点分键取插件配置值（如 proactive.enabled）。"""
        node: Any = await self._plugin_config()
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    async def _cfg_int(self, key: str, default: int) -> int:
        """读插件配置值（点分键；读不到回默认值）。"""
        try:
            return int(await self._cfg_value(key, default))
        except Exception:  # noqa: BLE001
            return default

    async def _cfg_bool(self, key: str, default: bool) -> bool:
        """读布尔配置值（兼容字符串 "true"/"1"/"yes"/"on"）。"""
        try:
            value = await self._cfg_value(key, default)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        except Exception:  # noqa: BLE001
            return default

    async def _cfg_str(self, key: str, default: str) -> str:
        """读字符串配置值。"""
        try:
            value = await self._cfg_value(key, default)
            return str(value) if value is not None else default
        except Exception:  # noqa: BLE001
            return default

    async def _do_proactive_trigger(
        self, target: str, intent: str, reason: str, cooldown_sec: int,
        exclude_texts: set, manual: bool = False
    ) -> tuple:
        """触发主动开口的公共链路：冷却检查 → trigger → 写锁位 → 回读。

        /mk hi 与 M2 扫描循环共用（审计 13:24 第 16 条：锁位权威数据落盘，防热重载清零；
        F1-4：与 /mk hi 共用同一组锁位防任务塌缩）。返回 (ok, msg)：
        ok=False 时 msg 是拒绝/失败原因，ok=True 时 msg 是「触发成功」反馈文本。

        manual=True（/mk hi 手动）与 manual=False（扫描自动）**分开计数**
        （PM 15:40 拍板）：手动触发只占用自己的锁位 `last_trigger_manual`，
        不推进自动扫描的 `last_trigger` / `last_trigger_global`——
        手动是明确意图，自动才有「打扰预算」。否则用户手动敲一次 /mk hi，
        接下来 2 小时（min_interval_minutes）自动扫描就不该再主动找他。
        """
        now = time.time()
        runtime = self._load_json(self._RUNTIME_FILE)
        pstate = runtime.setdefault("proactive_state", {})
        if manual:
            manual_map = dict(pstate.get("last_trigger_manual") or {})
            last_effective = float(manual_map.get(target, 0.0))
        else:
            per_stream = dict(pstate.get("last_trigger") or {})
            # 全局冷却层（审计 13:24「跨流互斥复核」）：防打扰是用户级不是流级——
            # 麦麦在流 A、流 B 同时开口在体验上是同一件事，跨流共享同一冷却
            last_effective = max(
                float(per_stream.get(target, 0.0)),
                float(pstate.get("last_trigger_global", 0.0)),
            )
        if now - last_effective < cooldown_sec:
            remain = int(cooldown_sec - (now - last_effective))
            return False, f"触发被冷却拦截：距上次主动触发还需约 {remain} 秒（防连环轰炸，可用 /mk diag 查看状态）"
        if target in self._proactive_pending:
            return False, "已有一笔主动触发在等待麦麦响应——先等她开口，或等回读超时后再试"

        # ---- 锁位先落盘，再发起 trigger（审计 A-1 修复）----
        # 原实现是「读盘 → await trigger → 用旧快照写盘」：read-modify-write 跨了 await，
        # 那个窗口里磁盘冷却未更新、内存 pending 未加入 → 并发落在窗口内的第二个触发
        # （扫描 + /mk hi）能同时通过冷却与 pending 两项检查 → 双触发；
        # 且后写的旧快照会把 last_trigger_global 覆盖回更旧的值 → 冷却窗口缩短、
        # daily_count 可能被吞。现在改成「先占位落盘 → 再触发 → 失败回滚」，
        # 读盘到落盘之间无 await，是原子段。
        if manual:
            manual_map[target] = now
            pstate["last_trigger_manual"] = manual_map
        else:
            per_stream[target] = now
            pstate["last_trigger"] = per_stream
            pstate["last_trigger_global"] = now
        self._proactive_last_trigger[target] = now
        self._proactive_pending[target] = now
        self._write_json(self._RUNTIME_FILE, runtime)

        try:
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id=target,
                intent=intent,
                reason=reason,
                priority="normal",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[MaiKnock] %s 触发异常：%s", reason, exc)
            self._rollback_lock(target, now, manual)
            return False, f"触发失败（能力调用异常）：{exc!r}"

        if not (isinstance(result, dict) and result.get("success")):
            err = result.get("error") if isinstance(result, dict) else repr(result)
            logger.warning("[MaiKnock] %s 触发被拒：%s", reason, err)
            self._rollback_lock(target, now, manual)
            return False, f"触发失败：{err}"

        logger.info("[MaiKnock] %s 已触发 stream=%s", reason, target)

        # 回读任务登记到 self._readback_tasks：热重载时 on_unload 统一取消，防孤儿任务。
        # 插件回复统一带【MaiKnock】前缀，回读的 startswith('【MaiKnock') 排除规则已完备；
        # exclude_texts 目前是「方案②（after_build_message 打 additional_config 标记）
        # 落地前的预留位」——当前调用方均传空集，不参与判定（审计 A-6）。
        task = asyncio.create_task(self._readback_proactive_result(target, now, exclude_texts))
        self._readback_tasks.add(task)
        task.add_done_callback(self._readback_tasks.discard)
        return True, "触发成功！已请求麦麦在当前私聊流主动开口"

    def _rollback_lock(self, target: str, stamp: float, manual: bool = False) -> None:
        """触发失败时回滚锁位（审计 A-1：先占位落盘、失败回滚）。

        只回滚「自己写的那一份」——用时间戳匹配判断，避免误回滚并发方的更新。
        读→改→写之间无 await，单事件循环内是原子段。
        """
        # pending 只在「自己仍是当前持有者」时才清（B-4，15:50 审计）：并发方刚成功
        # 的 pending 不能被误清——否则锁位失效，下一轮可能在别人回读窗口内再触发。
        if self._proactive_pending.get(target) == stamp:
            self._proactive_pending.pop(target, None)
        if abs(float(self._proactive_last_trigger.get(target, 0.0)) - stamp) < 1e-6:
            self._proactive_last_trigger.pop(target, None)
        runtime = self._load_json(self._RUNTIME_FILE)
        pstate = runtime.get("proactive_state")
        if not isinstance(pstate, dict):
            return
        if manual:
            manual_map = dict(pstate.get("last_trigger_manual") or {})
            if abs(float(manual_map.get(target, 0.0)) - stamp) < 1e-6:
                manual_map.pop(target, None)
                pstate["last_trigger_manual"] = manual_map
        else:
            per_stream = dict(pstate.get("last_trigger") or {})
            if abs(float(per_stream.get(target, 0.0)) - stamp) < 1e-6:
                per_stream.pop(target, None)
                pstate["last_trigger"] = per_stream
            if abs(float(pstate.get("last_trigger_global", 0.0)) - stamp) < 1e-6:
                pstate.pop("last_trigger_global", None)
        self._write_json(self._RUNTIME_FILE, runtime)
        logger.info("[MaiKnock] 触发失败，已回滚锁位（stream=%s manual=%s）", target, manual)

    async def _cmd_hi(self, rest: str, stream_id: str) -> str:
        state = self._load_state()
        target = stream_id or str(state.get("bound_stream_id") or "")
        if not target:
            return "触发失败：找不到目标聊天流。请在 WebUI 私聊窗口里执行 /mk hi"

        cooldown_sec = await self._cfg_int("proactive.hi_cooldown_minutes", 3) * 60
        topic = rest.strip()
        # F1-2：intent 写成明确指令而非情境描述
        intent = "请现在主动向用户发起一条消息：自然地打个招呼并开启一个新话题。"
        if topic:
            intent += f"话头：{topic}"

        ok, msg = await self._do_proactive_trigger(
            target, intent, "MaiKnock /mk hi 手动触发", cooldown_sec, set(), manual=True
        )
        if not ok:
            return msg
        head = msg
        if topic:
            head += f"，话头：{topic}"
        return head + "\n她是否真的开口由她自己决定——稍后我会用「【MaiKnock 回读】」汇报结果。"

    async def _readback_proactive_result(self, target: str, trigger_ts: float, exclude_texts: set) -> None:
        """F1-2 触发后回读：轮询 get_recent 看麦麦是否真产出了新消息。"""
        poll = await self._cfg_int("proactive.readback_poll_seconds", 20)
        timeout = await self._cfg_int("proactive.readback_timeout_seconds", 240)
        deadline = time.time() + timeout
        found_text: Optional[str] = None
        try:
            while time.time() < deadline:
                await asyncio.sleep(poll)
                try:
                    result = await self.ctx.message.get_recent(chat_id=target, limit=30)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[MaiKnock] 回读取数失败（继续重试）：%s", exc)
                    continue
                msgs = result if isinstance(result, list) else (
                    result.get("messages", []) if isinstance(result, dict) else []
                )
                for m in msgs:
                    if m.get("is_command") or m.get("is_notify"):
                        continue
                    info = m.get("message_info") or {}
                    if (info.get("user_info") or {}).get("user_id") != "self":
                        continue
                    try:
                        ts = float(m.get("timestamp"))
                    except (TypeError, ValueError):
                        continue
                    if ts < trigger_ts - 1:  # 只看触发之后的新发言（-1s 容时钟误差）
                        continue
                    text = str(m.get("processed_plain_text") or "").strip()
                    # 排除插件自己的回复与回读汇报（前 30 字指纹匹配）
                    if text.startswith("【MaiKnock"):
                        continue
                    if any(text.startswith(str(e)[:30]) for e in exclude_texts):
                        continue
                    found_text = text[:80]
                    break
                if found_text:
                    break
        finally:
            # 同 B-4：只清自己那一份（本回读持有 trigger_ts 这把锁）。
            # 若期间有另一笔触发重新拿到了同一流的锁，也不能被本回读清掉。
            if self._proactive_pending.get(target) == trigger_ts:
                self._proactive_pending.pop(target, None)

        # 未响应计数落盘（F1-2：未响应记一次，供将来 F1 自我降频使用）
        # 并发说明（审计 13:24 第 17 条）：_load_json/_write_json 是同步函数且两者之间
        # 无 await——单事件循环内不会被其他协程插队，read-modify-write 当前实际安全；
        # 若将来改成异步 IO 或多进程，必须先改 append-only 或文件锁
        runtime = self._load_json(self._RUNTIME_FILE)
        stats = runtime.setdefault("proactive_stats", {})
        stats["triggered_total"] = int(stats.get("triggered_total", 0)) + 1
        if found_text:
            report = f"【MaiKnock 回读】麦麦开口了：「{found_text}」"
            logger.info("[MaiKnock] 回读确认：麦麦已开口（stream=%s）", target)
        else:
            stats["no_response_total"] = int(stats.get("no_response_total", 0)) + 1
            report = (
                f"【MaiKnock 回读】{timeout} 秒内未见麦麦的新消息——"
                "这一轮她决定不开口（触发不保证开口，属已知行为），已记一次未响应。"
            )
            logger.info("[MaiKnock] 回读超时：麦麦未响应（stream=%s）", target)
        self._write_json(self._RUNTIME_FILE, runtime)

        try:
            # F2/P9：回读报告同样走统一出口，回退生效时脱敏
            await self._send_text(report, target)
        except Exception as exc:  # noqa: BLE001
            logger.error("[MaiKnock] 回读结果发送失败：%s", exc)

    # ---- F1：M2 扫描循环（静默超时自动触发主动开口）----
    # 自检清单（审计 13:47，技术事实与踩坑.md:100-116）关键点落地：
    # ① SDK get_recent 只暴露 limit 不暴露 hours → 静默判定用大 limit 找最后一条真实用户消息
    # ② 禁止时段跨天处理（start > end 时 start<=now<=end 恒假，深夜防骚扰会失效）
    # ③ 扫描循环里 _load_json/_write_json 是同步 I/O，但文件仅 KB 级、每轮只读一两次，
    #    微秒级不阻塞事件循环（不会拖慢同进程 20s 一轮的回读）
    # ④ 扫描任务登记 self._scan_task + on_unload 取消，防热重载孤儿化

    def _ensure_scan_loop(self) -> None:
        """幂等启动扫描循环任务（热重载不走 on_load，需 on_load/on_config_update/handle_mk 多入口兜底）。

        ⚠️ C-3（16:50 审计建议，实测已证伪其前提）：审计担心「宿主 config_error
        状态下敲 /mk 会把扫描循环拉起来，且用的是未校验的配置值」。**实测结论：
        Runner 侧硬拦截**——`runner_main.py:2357-2359` 在 `meta.config_error` 非空时
        直接 `make_error_response("插件配置错误，业务暂停")`，**根本不会调用插件的
        handle_mk** ⇒ 命令进不了插件 ⇒ 兜底启动不可能发生（实测：config_error 下
        敲 /mk diag 无任何插件侧日志、无回复）。

        但仍保留一道防御：配置自校验失败（C-2）时不启动扫描循环——
        避免任何路径下用宿主已判非法、插件也未校验过的值驱动主动开口。
        """
        if self._scan_task is not None and not self._scan_task.done():
            return
        if self._config_invalid:
            logger.error(
                "[MaiKnock] 配置自校验未通过，扫描循环不启动（避免用非法配置值驱动主动开口）"
            )
            return
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("[MaiKnock] 扫描循环已启动")

    async def _scan_loop(self) -> None:
        """M2 扫描循环：周期检查静默超时，触发麦麦主动开口。"""
        while True:
            interval = await self._cfg_int("proactive.scan_interval_seconds", 120)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("[MaiKnock] 扫描循环已停止")
                raise
            try:
                if not await self._cfg_bool("proactive.enabled", False):
                    continue
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[MaiKnock] 扫描循环异常：%s", exc)

    def _scan_log_skip(self, reason: str) -> None:
        """记录扫描跳过原因（状态变化才打 info）。

        宿主只输出插件 logger 的 info 级（实测 debug 被过滤），而扫描每轮都会跑，
        直接 info 会刷屏——故只在「跳过原因发生变化」时打一条，
        这样「为什么她不主动找我」随时可查，又不至于淹没日志。
        """
        if reason == self._scan_last_reason:
            return
        self._scan_last_reason = reason
        logger.info("[MaiKnock] 扫描跳过：%s", reason)

    def _scan_log_silence(self, minutes: float) -> None:
        """记录静默时长（按 5 分钟档位变化才打）。"""
        bucket = int(minutes // 5)
        if bucket == self._scan_last_silence_bucket:
            return
        self._scan_last_silence_bucket = bucket
        logger.info("[MaiKnock] 当前静默 %.0f 分钟", minutes)

    async def _scan_once(self) -> None:
        """单轮扫描：静默判定 + 防骚扰四件套，满足则触发主动开口。"""
        state = self._load_state()
        target = str(state.get("bound_stream_id") or "")
        if not target:
            self._scan_log_skip("未绑定聊天流（请在 WebUI 私聊执行 /mk bind）")
            return  # 未绑定，静默等待用户 /mk bind

        now = time.time()
        # 1. 禁止时段（跨天处理）
        if await self._in_quiet_hours(now):
            self._scan_log_skip("处于禁止时段")
            return
        # 2. pending 锁位（有回读未结束就跳过，防任务塌缩）
        if target in self._proactive_pending:
            self._scan_log_skip("上一笔主动触发仍在等待麦麦响应")
            return
        # 3. 冷却（全局 + per-stream，复用落盘锁位）
        min_interval = await self._cfg_int("proactive.min_interval_minutes", 120) * 60
        runtime = self._load_json(self._RUNTIME_FILE)
        pstate = runtime.get("proactive_state", {})
        last_effective = max(
            float(pstate.get("last_trigger", {}).get(target, 0.0)),
            float(pstate.get("last_trigger_global", 0.0)),
        )
        cooldown_left = min_interval - (now - last_effective)
        if cooldown_left > 0:
            self._scan_log_skip("冷却中（距上次主动开口未到最小间隔）")
            return
        # 4. 每日上限（落盘按天重置）
        if await self._daily_quota_exceeded():
            self._scan_log_skip("今日主动开口次数已达上限")
            return
        # 5. 静默时长判定
        threshold = await self._cfg_int("proactive.silence_threshold_minutes", 60) * 60
        silence = await self._silence_duration_seconds(target)
        if silence is None:
            # ⚠️ 这里的提前 return 在下面的 manual 重置**之前**（16:50 审计指出）：
            # → 「从来没说过话、只靠 /mk hi 手动触发过」的用户**永远不会**被自动扫描触发。
            # 这与 B-3 的语义一致（从未说过话 = 不主动打扰），**不是 bug**，
            # 但别让后来人看到「手动触发了却不触发自动」以为前后不一致。
            self._scan_log_skip("该流从未有过用户真实发言（不主动打扰）")
            return
        # 手动触发计入「用户互动」→ 重置静默计时（PM 16:05 拍板，决策 4）
        # 语义依据：N-1 把 /mk hi 当命令排除 → 不算「用户说话」→ 计时不重置；
        # 但手动打招呼在语义上就是一次用户互动。现状自相矛盾：命令不算发言、
        # 手动触发又不影响计时 → 自动扫描仍认为「他三小时没理我了」→ 立刻又来找，
        # 用户 2 分钟内收到两条主动消息。修正后「所有形式的用户互动都重置静默
        # 计时」这个模型才自洽。
        # 取 min = 取最近的那次互动；max(0.0, ...) 防时钟回拨/未来时间戳出负数。
        # ⚠️ 只影响「静默计时」，**不**动冷却（min_interval）与 daily_count——
        # 决策 4 的四条全部保留（手动不消耗自动额度、不被 120 分钟挡住、不写自动锁位）。
        manual_ts = float((pstate.get("last_trigger_manual") or {}).get(target, 0.0))
        if manual_ts > 0:
            silence = min(silence, max(0.0, now - manual_ts))
        self._scan_log_silence(silence / 60)
        if silence < threshold:
            self._scan_log_skip("静默时长未达阈值")
            return

        # 满足 → 触发（复用公共链路，锁位/回读/计数统一）
        intent = "用户已经有一段时间没有发消息了，请主动向用户发起一条消息，自然地打个招呼并开启一个新话题。"
        ok, msg = await self._do_proactive_trigger(
            target, intent, "MaiKnock M2 静默扫描", min_interval, set()
        )
        if ok:
            self._bump_daily_count()
            self._scan_last_reason = ""  # 重置跳过原因状态，便于下一轮状态变化被记录
            logger.info("[MaiKnock] M2 静默扫描已触发（stream=%s 静默 %.0f 分钟）", target, silence / 60)
        else:
            logger.warning("[MaiKnock] M2 静默扫描触发被拦：%s", msg)

    async def _in_quiet_hours(self, now: float) -> bool:
        """禁止时段判定（跨天安全）：start > end 表示跨天（如 22:00-08:00）。"""
        start = await self._cfg_str("proactive.quiet_hours_start", "01:00")
        end = await self._cfg_str("proactive.quiet_hours_end", "08:00")
        lt = time.localtime(now)
        minutes = lt.tm_hour * 60 + lt.tm_min
        s = self._parse_hhmm(start)
        e = self._parse_hhmm(end)
        if s is None or e is None or s == e:
            return False  # 配置非法或区间为空，视为不禁止
        if s < e:
            # 同一天内：s <= now < e
            return s <= minutes < e
        # 跨天：now >= s（当晚）或 now < e（次日凌晨）
        return minutes >= s or minutes < e

    @staticmethod
    def _parse_hhmm(value: str) -> Optional[int]:
        """解析 HH:MM 为分钟数，非法返回 None。"""
        try:
            parts = value.strip().split(":")
            if len(parts) != 2:
                return None
            h = int(parts[0])
            m = int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                return None
            return h * 60 + m
        except (TypeError, ValueError):
            return None

    async def _daily_quota_exceeded(self) -> bool:
        """每日上限判定：落盘按天重置（日期 key），热重载不丢。"""
        daily_max = await self._cfg_int("proactive.daily_max", 5)
        today = time.strftime("%Y-%m-%d")
        runtime = self._load_json(self._RUNTIME_FILE)
        daily = runtime.get("daily_count", {})
        used = int(daily.get(today, 0))
        return used >= daily_max

    def _bump_daily_count(self) -> None:
        """每日计数 +1（触发成功后调用；单扫描任务串行，无并发写者）。"""
        today = time.strftime("%Y-%m-%d")
        runtime = self._load_json(self._RUNTIME_FILE)
        daily = runtime.setdefault("daily_count", {})
        daily[today] = int(daily.get(today, 0)) + 1
        self._write_json(self._RUNTIME_FILE, runtime)

    async def _silence_duration_seconds(self, stream_id: str) -> Optional[float]:
        """静默时长：最后一条真实用户消息距现在的秒数；无用户消息返回 None。

        审计 A-2：必须显式给 hours 突破宿主的 24h 默认窗口——否则用户沉默超 24h 时
        会误判成「没有用户消息」→ 扫描永不触发（典型：睡了一夜，早上她不会主动找你）。

        ⚠️ 窗口不能只按阈值算（审计建议的 ceil(阈值/60)+1 存在逻辑循环）：
        判定「用户沉默多久」需要拿到「最后一次发言」，而那条发言可能就在窗口之外。
        阈值 60 分钟 → 窗口 2 小时 → 用户沉默 3 小时（已超阈值）反而取不到那条消息 →
        判定失败退回。故窗口取「阈值窗口」与「回溯上限」的较大者，默认回溯 720 小时
        （30 天），二者解耦。
        """
        threshold = await self._cfg_int("proactive.silence_threshold_minutes", 60)
        lookback = await self._cfg_int("proactive.silence_lookback_hours", 720)
        hours = max((threshold + 59) // 60 + 1, lookback)
        real = await self._get_real_user_messages(stream_id, limit=200, hours=hours)
        if not real:
            # 窗口内没有用户消息 —— 两种可能（B-3，15:50 审计）：
            # ① 用户最后一次发言超出回溯窗口（沉默极久）→ 语义上**该**找她了
            # ② 这个流里用户从未说过话 → 不该打扰
            # 二者原本都返回 None，代码无从区分 → 「回溯窗口」事实上从「取数范围」
            # 变成了「功能有效期」（用户沉默超 30 天后 M2 永久停摆）。
            # 用一个大得多的窗口探测一次把两者分开：探测到 → 沉默极久（照常触发），
            # 探测不到 → 从未说过话（不触发）。
            probe_hours = await self._cfg_int("proactive.silence_probe_hours", 87600)
            probe = await self._get_real_user_messages(stream_id, limit=1, hours=probe_hours)
            if not probe:
                return None
            last = probe[-1]
            try:
                ts = float(last.get("timestamp"))
            except (TypeError, ValueError):
                return None
            return max(0.0, time.time() - ts)
        last = real[-1]
        try:
            ts = float(last.get("timestamp"))
        except (TypeError, ValueError):
            return None
        return max(0.0, time.time() - ts)

    # ---- F2 虚构时间线（命令侧） ----

    @staticmethod
    def _parse_duration(text: str) -> Optional[float]:
        """解析时长：``3d`` / ``12h`` / ``90m`` / ``30s``；纯数字按小时计。"""
        import re

        raw = (text or "").strip().lower().replace(" ", "")
        if not raw:
            return None
        matched = re.fullmatch(r"(\d+(?:\.\d+)?)([dhms])?", raw)
        if not matched:
            return None
        value = float(matched.group(1))
        unit = matched.group(2) or "h"
        factor = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}[unit]
        return value * factor

    @staticmethod
    def _parse_target_time(text: str) -> Optional[float]:
        """解析绝对目标时刻：``2026-11-25 09:00`` / ``2026-11-25``。"""
        raw = (text or "").strip()
        if not raw:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return float(time.mktime(time.strptime(raw, fmt)))
            except ValueError:
                continue
        return None

    def _cmd_time(self, rest: str) -> str:
        """F2 虚构时间线：查看 / 推进 / 回退 / 设定 / 关闭 / 恢复。

        **只进不退**（决策 1）：推进只能往前；回退是独立子命令（开分支线，与决策 1 不冲突）。

        子命令：
        - ``/mk time``                     查看当前时间线状态
        - ``/mk time add 3d`` / ``add 12h`` 推进故事时间
        - ``/mk time back 3d``             回退故事时间（那段时间会被藏起来）
        - ``/mk time set 2026-11-25 09:00`` 把故事时间设为某个绝对时刻
        - ``/mk time off``                 关掉时间线（清隐藏区间 + 追加反向声明）
        - ``/mk time now`` / ``/mk now``   恢复到现实时间（追加反向声明）
        """
        if tl is None:
            return "时间线模块未就绪（timeline.py 加载失败），F2 不可用。详见插件日志。"

        now = time.time()
        state = self._load_timeline()
        if state is None:
            state = tl.TimelineState()

        text = (rest or "").strip()
        if not text:
            return tl.describe_state(state, now)

        parts = text.split(maxsplit=1)
        head = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        def _apply(new_state, action: str) -> str:
            # R6-18 自检：声明里不得出现绝对日期（否则回退后麦麦能读出今天几号）
            try:
                tl.assert_declaration_has_no_absolute_date(new_state.declaration)
            except ValueError as exc:
                return f"操作已放弃：{exc}"
            if not self._save_timeline(new_state):
                return f"{action}失败：时间线状态写入失败（详见插件日志）"
            logger.info(
                "[MaiKnock] 时间线变更：%s mode=%s offset=%.0fs hidden=[%s, %s]",
                action, new_state.mode, new_state.offset_sec,
                new_state.boundary_ts, new_state.hidden_until_ts,
            )
            return f"{action}完成。\n" + tl.describe_state(new_state, now)

        # 恢复现实时间（PM 拍板：追加反向声明，不撤旧声明）
        if head in ("now", "reset", "real"):
            return _apply(tl.plan_reset_to_real_now(state, now), "恢复到现实时间")

        # 关掉时间线（R6-21：清隐藏区间 + 追加反向声明 —— 取消失忆必须同时撤销叙事）
        if head == "off":
            return _apply(tl.plan_off(state, now), "关闭时间线")

        # 回退
        if head in ("back", "backward"):
            delta = self._parse_duration(arg)
            if delta is None:
                return "无法解析回退幅度。用法：/mk time back 3d（或 12h / 90m）"
            if delta >= 0:
                delta = -delta
            try:
                new_state = tl.plan_backward(state, now, delta)
            except ValueError as exc:
                # R6-20：故事时间仍在未来时回退 → 明确报错、不写盘，
                # 避免生成倒挂的空区间（负数显示 + 无谓触发脱敏）。
                return f"回退失败：{exc}"
            return _apply(new_state, f"回退 {abs(delta) / 86400.0:.2f} 天")

        # 设定绝对时刻
        if head == "set":
            target = self._parse_target_time(arg)
            if target is None:
                return "无法解析目标时刻。用法：/mk time set 2026-11-25 09:00"
            try:
                new_state = tl.plan_set_story_time(state, now, target)
            except ValueError as exc:
                return f"设定失败：{exc}"
            return _apply(new_state, "设定故事时间")

        # 推进（add 3d / add 12h）—— 与 back/set 统一的单词子命令
        if head == "add":
            delta = self._parse_duration(arg)
            if delta is None:
                return "无法解析推进幅度。用法：/mk time add 3d（或 add 12h / add 90m）"
            try:
                new_state = tl.plan_forward(state, now, delta)
            except ValueError as exc:
                return f"推进失败：{exc}"
            return _apply(new_state, f"推进 {delta / 3600.0:.2f} 小时")

        return (
            "无法识别的参数。用法：\n"
            "  /mk time                    查看状态\n"
            "  /mk time add 3d             推进故事时间\n"
            "  /mk time back 3d            回退故事时间\n"
            "  /mk time set 2026-11-25 09:00  设定绝对时刻\n"
            "  /mk time off                关闭时间线（恢复对话可见）\n"
            "  /mk now                     恢复到现实时间"
        )

    def _cmd_help(self) -> str:
        return (
            f"麦麦敲门 MaiKnock v{VERSION}\n"
            "/mk bind          —— 在 WebUI 私聊中绑定本聊天流\n"
            "/mk diag          —— 查看当前上下文诊断信息\n"
            "/mk hi [话头]     —— 让麦麦立刻主动开口（是否开口由她决定，稍后回读汇报）\n"
            "/mk time          —— 查看虚构时间线状态\n"
            "/mk time add 3d   —— 推进故事时间（只进不退）\n"
            "/mk time back 3d  —— 回退故事时间（那段时间会被藏起来）\n"
            "/mk time set 2026-11-25 09:00 —— 把故事时间设为某个绝对时刻\n"
            "/mk time off      —— 关闭时间线\n"
            "/mk now           —— 恢复到现实时间\n"
            "/mk help          —— 显示本帮助"
        )

    # ------------------------------------------------------------------
    # F2 第②步：A + G 合流 —— 挂两个 hook
    #
    # ⚠️ **只订这两个**：`maisaka.replyer.before_request` 的 HookSpec 里
    # **没有 `items` 字段**（R6-2）⇒ 工具结果改不了 ⇒ 本期不订。
    #
    # ⚠️ **两侧顺序相反**（R6-28 / P18）：
    #   planner 侧：先 relabel → 再遮蔽（判据用 meta.timestamp，与文本无关）
    #   replyer 侧：先遮蔽 → 再 relabel（遮蔽靠块边界，块边界靠未改写的 [HH:MM:SS]）
    # ------------------------------------------------------------------

    def _f2_state_for(self):
        """取当前时间线状态；不可用时返回 None（⇒ 本轮不改写）。"""
        if tl is None or mk_planner is None:
            return None
        try:
            state = self._load_timeline()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] 时间线状态读取失败，本轮不改写：%r", exc)
            return None
        if state is None:
            return None
        # 只有「有偏移」或「有隐藏区间」时才需要改写
        if not state.offset_sec and not state.has_hidden_window():
            return None
        return state

    def _f2_declaration_text(self, state) -> str:
        """取本轮的叙事声明正文。

        ⚠️ **用状态里存的 `state.declaration`（不是现场生成）** ——
        机制 3 的 **D1** 要求：声明必须持久化，插件在 `on_load` / 首次 hook 调用时重新注入一次；
        而且 `declaration` 是 **append-only**（PM 拍板 C），
        `/mk now` / `/mk time off` 会**追加一条反向声明**（R6-21 修的也是这个）
        ⇒ 只有用存下来的那份，反向声明才能一起送到模型。

        ⚠️ **只在「有隐藏区间」时注入** —— 单纯推进时间线（`add 3d`）不是失忆，
        没必要让麦麦以为那段记忆是空白的。
        ⚠️ **声明不含绝对日期**（R6-18）—— 由 `timeline.py` 写盘时的断言保证。
        """
        if mk_decl is None or state is None:
            return ""
        if not state.has_hidden_window():
            return ""
        text = (state.declaration or "").strip()
        if not text:
            return ""
        # 兜底：万一存下来的声明带了绝对日期 ⇒ 不注入（宁可少一句也不能穿帮）
        try:
            if mk_decl.has_absolute_date(text):
                logger.warning("[MaiKnock] 存储的叙事声明含绝对日期，本轮不注入（防穿帮）")
                return ""
        except Exception:  # noqa: BLE001
            pass
        return text

    @staticmethod
    def _f2_decl_item_id() -> str:
        """声明 item 的 id（用固定串，便于自识别与幂等替换）。"""
        return "maiknock-timeline-declaration"

    @staticmethod
    def _f2_now_iso() -> str:
        """当前时间的 ISO 字符串（与宿主 `meta.timestamp` 同格式）。

        ⚠️ **必须是 now** ⇒ 天然落在隐藏区间外 ⇒ 声明**不会被遮蔽**（R2 自动满足）。
        """
        import datetime as _dt

        return _dt.datetime.now().isoformat()

    async def _f2_fetch_candidates(self, stream_id: str):
        """取 `get_recent` 候选集（**迭代扩窗**，P22 / R6-30）。

        ⚠️ `hours` **每次都要显式传**（能力默认 24.0，`capabilities/data.py:508`）。
        ⚠️ `filter_mai` **必须 False**（默认）—— blob 里麦麦自己的发言也在
        （实测 22 块里 10 块是 `{bot}(你)说：`）⇒ 过滤掉就**无法识别她的块**
        ⇒ 隐藏期里她自己的发言会留在上下文里（R6-11 换形式复现）。

        ⚠️ **调用方式**：`MaisakaCapability` **没有 `message` 命名空间**
        （只有 `reply` / `context` / `proactive`）⇒ 通用能力要走
        `ctx.call_capability("message.get_recent", ...)`。
        且该能力在 `_CAPABILITY_RESULT_KEYS` 里登记为返回 **`messages` 列表**
        （不是整个 dict）⇒ **不要**去取 `res["messages"]`。
        """
        if mk_matcher is None:
            return []
        last_err = None
        for hours, limit in ((24, 300), (96, 600), (384, 1200)):
            try:
                res = await self.ctx.call_capability(
                    "message.get_recent",
                    chat_id=stream_id,
                    hours=hours,
                    limit=limit,
                    filter_mai=False,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
            messages = res if isinstance(res, list) else None
            if messages is None and isinstance(res, dict):
                messages = res.get("messages")
            if not messages:
                last_err = RuntimeError("get_recent 返回空候选")
                continue
            cands = mk_matcher.build_candidates(messages)
            if cands:
                return cands
        if last_err is not None:
            logger.warning("[MaiKnock] get_recent 取候选失败，本轮 replyer 侧不遮蔽：%r", last_err)
        return []

    @HookHandler(
        "maisaka.planner.before_request",
        name="maiknock_planner_timeline",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def _hook_planner_timeline(self, **kwargs):
        """planner 侧：**先 relabel → 再遮蔽**（逐条 item）。

        ⚠️ `modified_kwargs` 是**完整替换**语义 ⇒ 必须**原样回传完整 kwargs**
        （尤其 `item_schema_version`），只替换 `items`。
        漏字段 ⇒ **静默回退真实时间且插件收不到失败信号**。
        """
        if mk_planner is None or tl is None:
            return {"action": "continue"}
        state = self._f2_state_for()
        if state is None:
            return {"action": "continue"}

        items = kwargs.get("items")
        if not isinstance(items, list) or not items:
            return {"action": "continue"}

        try:
            res = mk_planner.process_planner_items(
                items,
                is_hidden=state.is_hidden,
                offset_sec=state.offset_sec,
                now_ts=time.time(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] planner 侧改写失败，本轮放行原样：%r", exc)
            return {"action": "continue"}

        if res.hidden_items or res.relabeled_items:
            logger.info(
                "[MaiKnock] planner 侧改写：%s，时间提示%s",
                res.summary(),
                "已改" if res.current_time_relabeled else "未找到",
            )
        for w in res.warnings:
            logger.warning("[MaiKnock] planner 侧：%s", w)

        # ---------- 叙事声明注入（机制 3 · R6-12：每轮注入到 items 末尾）----------
        # ⚠️ 必须在**遮蔽之后**注入 ⇒ 声明要活到最后（不被遮蔽、不被倒扫挤出）
        final_items = res.items
        decl_text = self._f2_declaration_text(state)
        if decl_text:
            final_items = mk_decl.inject_declaration(
                final_items,
                decl_text,
                item_id=self._f2_decl_item_id(),
                timestamp=self._f2_now_iso(),
            )

        new_kwargs = dict(kwargs)
        new_kwargs["items"] = final_items
        return {"action": "continue", "modified_kwargs": new_kwargs}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="maiknock_replier_timeline",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def _hook_replier_timeline(self, **kwargs):
        """replyer 侧（1.4.8 唯一路径）：**先遮蔽 → 后 relabel**（blob）。

        ⚠️ **顺序不能换**（R6-28）：块边界靠**未改写的** `[HH:MM:SS]` 识别；
        先 relabel 就切不出块 ⇒ **遮蔽彻底失效**。
        """
        if mk_blob is None or mk_matcher is None or tl is None:
            return {"action": "continue"}
        state = self._f2_state_for()
        if state is None:
            return {"action": "continue"}

        items = kwargs.get("items")
        if not isinstance(items, list) or not items:
            return {"action": "continue"}

        stream_id = str(kwargs.get("session_id") or "")
        reply_message_id = str(kwargs.get("reply_message_id") or "")

        # 找出承载 blob 的 UserMessageItem（replyer 侧历史恒为一块 blob）
        # ⚠️ 必须用**正则**判断，不能用字面量 "[HH:MM:SS]" —— 实际内容是
        # `[16:50:07]` 这种真实时刻，字面量匹配**永远不中**（23:10 实测踩过：
        # replyer 侧 hook 静默跳过，planner 侧却正常）。
        target_idx = -1
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if item.get("item_type") != "UserMessageItem":
                continue
            text = _mk_item_text(item)
            if _MK_BLOCK_START_RE.search(text):
                target_idx = i
                break
        if target_idx < 0:
            return {"action": "continue"}

        cands = await self._f2_fetch_candidates(stream_id)
        if not cands:
            return {"action": "continue"}

        item = items[target_idx]
        text = _mk_item_text(item)
        try:
            res = mk_blob.process_blob(
                text,
                cands,
                is_hidden=state.is_hidden,
                offset_sec=state.offset_sec,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] replyer 侧改写失败，本轮放行原样：%r", exc)
            return {"action": "continue"}

        if res.hidden_blocks or res.relabeled_blocks or res.current_time_relabeled:
            logger.info(
                "[MaiKnock] replyer 侧改写：%s，时间提示%s",
                res.summary(),
                "已改" if res.current_time_relabeled else "未找到",
            )
        for w in res.warnings:
            logger.warning("[MaiKnock] replyer 侧：%s", w)

        new_items = list(items)
        new_items[target_idx] = _mk_replace_item_text(item, res.text)

        # ---------- R6-17：工具结果文本里的第三种载体 ----------
        # `HH:MM:SS[msg_id:…][说话人]正文`（宿主 `history.py:41` 产出）
        # ⚠️ **只在 replyer 侧做** —— 那边本来就要调 get_recent 拿候选集，
        #    planner 侧为省一次查询（hook 超时只有 6000ms）不做。
        try:
            msg_id_ts = mk_matcher.build_msg_id_ts(cands)
            new_items, tool_fixed = _f2_relabel_tool_outputs(
                new_items, msg_id_ts, state.offset_sec
            )
            if tool_fixed:
                logger.info(
                    "[MaiKnock] replyer 侧：工具结果第三种载体已 relabel %d 处", tool_fixed
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] 工具结果 relabel 失败（本项跳过）：%r", exc)

        # ---------- 叙事声明注入（机制 3 · R6-12）----------
        # ⚠️ replyer 侧**必须**注入 —— 上下文里 `InternalContextMessage` 会被
        # `_is_replyer_filtered_history_message`（`maisaka_generator_base.py:663-669`）滤掉
        # ⇒ 不注入的话，**声明只到 planner、到不了 replyer**（R6-12 的原始问题）
        decl_text = self._f2_declaration_text(state)
        if decl_text:
            new_items = mk_decl.inject_declaration(
                new_items,
                decl_text,
                item_id=self._f2_decl_item_id(),
                timestamp=self._f2_now_iso(),
            )

        new_kwargs = dict(kwargs)
        new_kwargs["items"] = new_items
        return {"action": "continue", "modified_kwargs": new_kwargs}


def _mk_item_text(item: Dict[str, Any]) -> str:
    """取 item 的文本（所有 text 部件拼起来）。"""
    parts = item.get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _f2_relabel_tool_outputs(
    items: List[Dict[str, Any]],
    msg_id_ts: Dict[str, float],
    offset_sec: float,
):
    """R6-17 第三种载体：工具结果文本里的 `HH:MM:SS[msg_id:…][说话人]正文`。

    **为什么单独处理**：
    - 工具结果的文本在**顶层 `output` 字段**（不在 `parts` 里）
      ⇒ `_mk_item_text` 对它返回空串；
    - 该载体只在**工具结果文本**里出现（对话历史用的是 `[HH:MM:SS] 说：` 形态）。

    **时间源 = msg_id 反查**（`get_recent` 的消息里有展示用短 id + 真实时刻）。
    ⚠️ **查不到 msg_id ⇒ 原样保留**（宁可少改不可错改）——
    只有 `HH:MM:SS` 没有日期时，无法换算成故事时刻。

    :returns: ``(新 items, 改了几处)``
    """
    if mk_carriers is None or not msg_id_ts:
        return items, 0

    changed = 0
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "FunctionCallOutputItem":
            out.append(item)
            continue
        output = item.get("output")
        if not isinstance(output, str) or not output:
            out.append(item)
            continue

        hits = mk_carriers.classify_tool_prefixes(output)
        if not hits:
            out.append(item)
            continue

        buf: List[str] = []
        cursor = 0
        did = False
        for hit in hits:
            mid = hit.msg_id
            if not mid or mid not in msg_id_ts:
                continue  # 查不到 ⇒ 保留
            buf.append(output[cursor:hit.hms_start])
            buf.append(mk_carriers.format_story(msg_id_ts[mid], offset_sec, with_date=True))
            cursor = hit.hms_end
            did = True
        if not did:
            out.append(item)
            continue
        buf.append(output[cursor:])
        new_item = dict(item)
        new_item["output"] = "".join(buf)
        out.append(new_item)
        changed += 1

    return out, changed


def _mk_replace_item_text(item: Dict[str, Any], new_text: str) -> Dict[str, Any]:
    """返回**只替换了 text** 的新 item（⚠️ 不动 meta、不动 item_type）。"""
    new_item = dict(item)
    new_parts = []
    for p in item.get("parts") or []:
        if isinstance(p, dict) and p.get("type") == "text":
            q = dict(p)
            q["text"] = new_text
            new_parts.append(q)
        else:
            new_parts.append(p)
    new_item["parts"] = new_parts
    return new_item


def create_plugin() -> MaiKnockPlugin:
    """宿主要求的插件工厂函数。"""
    return MaiKnockPlugin()
