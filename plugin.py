# -*- coding: utf-8 -*-
"""麦麦敲门 MaiKnock —— WebUI 原生私聊伴侣插件。

功能规划：
- F1 主动私聊：静默超时后让麦麦主动开口（官方 maisaka.proactive.trigger 能力）
- F2 剧情时间线：/mk now|add|back|set 四命令，钩子改写注入给模型的「当前时间」与相对时间标注
- F3 手动打招呼：/mk hi 立即让麦麦开口

版本号单一来源：见下方 VERSION 常量（审计 A-3：此前版本号散落 6 处手动编辑——
文档串 / config_version / 加载日志 / help / _manifest.json / config.toml，极易漂移）。
_manifest.json 与 config.toml 无法引用 Python 常量，改由启动自检
_check_version_consistency() 比对并告警。

0.4.0（2026-11-24 已发布）：F5 消息分段管理 —— 回复字数配置 + 段落间空行。
    ⚠️ 版本号只在发布时前进，开发期小修小改不涨号（PM 2026-11-23 拍板）。
    0.4.0 由 PM 拍板定版（2026-11-24）：F5 是面向用户的新功能（新配置项 + 新行为），
    按「体量/性质超补丁号 ⇒ 涨号」的既有判据（0.3.0 同判据）升 minor。
    本版本含 F5 两条新行为：
    - 功能 A 回复字数配置（新纯逻辑模块 reply_length.py + 配置节 [reply_length] 的
      「回复篇幅」下拉）：简短（60-200字）/ 适中（100-400字）/ 详细（150-600字）/ 自由，
      四档；字数下限恒为 60（用户铁律）；「自由」= 只放开字数，小说体格式、内心/动作
      开关、段落空行全都照常遵守；软约束（请求前注入中文契约），无硬截断。
    - 功能 B 段落间空行（style_guard.normalize_paragraph_breaks）：**插件被动技能**，
      没有开关、不用配置、所有字数档位生效（含「自由」）；段起点按内心/动作开关组合
      自动切换（【 开头 / （ 开头 / 句末标点），段间统一空一行，幂等。
    ⚠️ 下面 0.3.1~0.3.4 是**开发期迭代号**，从未作为发布号出现过，
       其内容一并收进 0.4.0 的 CHANGELOG 条目。

0.3.0（2026-11-24 已发布）：0.2.2 之后的**所有**变更统一累积在本版本。
    ⚠️ 版本号只在发布时前进，开发期小修小改不涨号（PM 2026-11-23 拍板）。
    0.3.0 由 PM 测试收尾时拍板改定（2026-11-24：F2 命令面大改 + F4 开关真值
    新机制 + 两个新逻辑模块，体量远超补丁号，测试方建议采纳）。
    本版本 F2 命令面大改（PM 拍板：用户是来娱乐的，只留四条时间线命令）：
    - 四命令：/mk now=查看剧情时间（唯一入口）/ /mk add 3d [剧情提示词] /
      /mk back 10h [剧情提示词] / /mk set 2026-11-25 09:00 [剧情提示词]。
      剧情提示词走 TimelineState.narrative，
      每轮与声明一同注入（带魔数前缀、无绝对日期断言）。
    - 删除命令：手动绑定（改为任意 /mk 自动绑定）、诊断、风格守卫对照表、
      帮助、/mk time 全家（含 off）。timeline.plan_off / plan_reset_to_real_now
      （/mk set now，PM 2026-11-24 拍板删除）同步删除。
    - 配置节 [timeline]「启用剧情时间线」曾短暂加入又删除（PM 2026-11-24 拍板）：
      四条命令默认开启，不设开关——插件只是把麦麦本来就有的剧情时间概念做成可控，不是创造。

0.3.4 变更要点（2026-11-23，PM 五条拍板：纯文案与 UI 措辞，运行逻辑零变化）
    —— **开发期迭代号，内容已并入 0.4.0**：
    1) 主动私聊节描述去掉「防打扰的限制」尾巴；
    2) 冷场两字段改同名标题 + 最早/最晚后缀（前端大屏两列网格，相邻声明天然同行并排）；
    3) 「夜间免打扰时段」改名「免打扰时段（麦麦在这段时间内不会找你）」，描述不再提拖动；
    4) 风格守卫节描述改成一段麦麦发言示例（直观演示三段式）；
    5) 内心/动作开关描述显式标注「只在启用小说体格式时生效」。
       注：宿主前端不支持 depends_on（schema 有此键、dist JS 无处理），无法做
       「启用关闭→两开关自动隐藏/置灰」的联动 UI；运行时 enabled=false 时守卫
       整体跳过（两开关天然失效），语义等价，用文案补足依赖关系。

0.3.3 变更要点（2026-11-23，PM 三条拍板：抽签网格化 + 免打扰多时段 + F4 全量生效）：
- 抽签以 **2 分钟为步长**（偶数分钟）：与扫描节奏同网格（扫描 tick=120s），用户对 1~2 分钟
  误差无感，避免无谓的秒级粒度。⚠️ 语义澄清（PM 误以为每 2 分钟重抽）：goal 仍是**一次
  抽签落盘复用**——抽中 76 就在第 76 分钟找；没回复则触发后清空重抽。「再随机一次」
  0.3.2 就是这么实现的，本版只改抽签粒度。
- 免打扰时段重构：quiet_hours_start/end 两个文本框 → `免打扰时段: List[QuietSpanConfig]`
  多时段列表，每项是「开始/结束」双联整点下拉（00:00~23:00 共 24 档）。宿主前端列表
  编辑器支持添加/删除/拖拽排序（plugin-config JS 的 dnd-kit sortable 已查证）。
  语义：空清单=无免打扰；开始==结束=该段无效；开始>结束=跨天（如 22:00~06:00）。
- **删除 style_guard.scope / managed_group_ids**（PM：受管清单是「麦麦聊天室」插件的事，
  且未开发，不该出现在麦麦敲门；麦麦敲门默认影响所有聊天）。_f4_gate 简化为
  模块可用+enabled ⇒ 全量生效；风格守卫不再有受管对照表命令。
  style_guard.managed_session/compute_session_id 保留未删（宿主升级回归对照仍有价值）。
- 回滚备份：backups/maiknock-v033-quiet-spans-20261123/

0.3.2 变更要点（2026-11-23，主动私聊节奏人性化 + F4 开关中文化，PM 四条拍板）：
- 静默阈值改**随机区间**：silence_threshold_minutes(最早,60) + 新增 silence_threshold_max_minutes(最晚,90)。
  每次冷场在区间内抽一个「本次目标」（落盘 pstate.silence_goal_sec 复用，避免每轮重抽导致
  触发时刻分布偏向早端）；触发成功后清空重抽。不再有「60 分钟准点找」的机械感。
- **删除 min_interval_minutes（两次最小间隔）**：PM 认定与冷场区间冗余（120>90 完全覆盖，
  实际效果 = 第二次至少等 120 分钟，不自然）。防骚扰改由「**触发后冷场计时重置**」承担：
  _scan_once 里 silence = min(距用户最后发言, 距上次自动触发 last_effective)——找过一次
  （无论用户回没回）都要重新等满一个随机冷场区间。⚠️ 这是删 min_interval 后防连环骚扰的
  唯一屏障：没有它，用户持续不回复时 silence 恒超阈值，扫描循环会每 tick 触发。
- **删除 hi_cooldown_minutes（/mk hi 冷却）**：PM 拍板「手动技能不该有冷却，我不会给自己刷屏」。
  防叠触发靠 pending 锁位 + 回读窗口（原有）；_do_proactive_trigger 的 cooldown_sec 参数保留，
  两个调用方现均传 0。
- F4 开关中文化 + 扁平化：[style_guard.blocks] inner_enabled/action_enabled →
  [style_guard] 直属中文字段 `内心想法` / `动作环境`（TOML/Python/pydantic 均支持中文标识符；
  SDK extra="ignore" ⇒ 旧 blocks 节被忽略）。_f4_blocks 读新键、兼容回退旧 blocks.inner_enabled；
  _cmd_styleguard 同步。UI 不再出现英文 key 与嵌套类型行。
- 回滚备份：backups/maiknock-v032-interval-random-20261123/

0.3.1 变更要点（2026-11-23，配置页大瘦身——只动文案与字段增删，运行逻辑零变化）：
- 删 8 个用户永远不需要碰的内部配置项（代码侧 _cfg_int / fmt.get 本就有同值兜底，
  删字段 = 恒走默认值，行为不变）：proactive.scan_interval_seconds(120) /
  readback_poll_seconds(20) / readback_timeout_seconds(240) / silence_lookback_hours(720) /
  silence_probe_hours(87600)；style_guard.format 整节（inner_markers → DEFAULT_INNER_MARKERS、
  max_retries → 1、两条重生文案 → enforce() 签名默认值）
- scope 改 Literal 下拉（防手拼错）；config_version 加 hidden=true（SDK 支持，
  plugin-config 前端确认遵守）；所有 label/description/节 docstring 重写为大白话
- PluginConfigBase extra="ignore"（maibot_sdk/config.py:51）⇒ 旧 config.toml 残留键被忽略不报错

0.3.0 变更要点（2026-11-23，F4 小说体风格守卫 —— 规格经外部技术评审 + 逐条复核）：
- 新增纯逻辑模块 style_guard.py（无 IO、无宿主依赖，可离线单测 tests/test_style_guard.py）：
  三段式契约生成（四种开关组合）、块扫描、显式内心标记转换（（内心：…）→【内心：…】，
  迁移主路径）、缺块判定、按开关删块（空正文保护）、会话识别（md5 公式镜像宿主
  utils_session.py:8-42）
- 挂两个新 hook（F2 的两个不动）：
  * before_model_request 第二个 handler，名 `maiknock_style_guard` —— ⚠️ 同 mode/order
    下按 handler 名字母序执行（hook_dispatcher.py:358-364），r < s ⇒ 排在
    `maiknock_replier_timeline` 之后，F2 的 strip_declarations 不会碰 F4 的 item；
    **改名即翻车**
  * after_response，名 `maiknock_style_guard_after`（⚠️ 不可与 before 同名：宿主组件表
    full_name 全局唯一，同名时 after 覆盖 before 注册条目 → 契约注入静默失效）：
    缺块 → {"retry": true, ...}（插件侧上限压到 1 次，retry_count>=1 放行）；
    格式问题 → {"response": 改写文本}。⚠️ 返回协议必须是
    {"action": "continue", "modified_kwargs": {...}}（hook_dispatcher.py:548/619-621
    只认 modified_kwargs，裸 dict 被静默丢弃——评审 P0-2）
- 配置段 [style_guard]（config.get_plugin 读取，禁用 ctx.config.get——F1 踩过的坑）：
  enabled / scope（bound_only|bound_and_webui_group，不设 all）/ managed_group_ids /
  blocks.inner_enabled / blocks.action_enabled / format.inner_markers / format.max_retries
- 新命令风格守卫对照表（公式算出的 hash 并排打印，供宿主升级回归；已于 0.2.3 随 scope 功能删除）
- 已知边界见《MaiKnock-F4小说体风格守卫-需求理解与技术方案-20261123.md》§4：
  开=每轮必有是尽力保证（重生上限 1）；无标记内心不转换；宿主 reply_private.prompt:11
  有「不要输出括号」与人格指令矛盾（实测 system item 拔河获胜）；整条全括号时空正文
  保护会残留关闭块

0.2.2 变更要点（2026-11-22 15:10，R-22/R-22b）：
- R-22【P0 第四通道】：有隐藏窗口时从 tool_definitions 摘除 query_memory /
  query_person_profile（模型主动检索会把记忆库内容经工具输出带回 prompt，
  整段屏蔽碰不到 FunctionCallOutputItem；工具调不到 ⇒ 没有输出 ⇒ 没有泄漏）
- R-22b【结构缺口】：query_memory 检索结果会被宿主构造成
  【长期记忆检索结果-内部参考】独立 item 透传给 replyer ⇒ replyer hook
  在 blob 查找之前整条剔除（含无 blob 的提前 return 路径）
- 回归：224 项断言全过（+6）
- 记忆存储已按 PM 指令清理（<=2026-11-21 全清，详见《记忆存储清理报告-20261122_1505》）

0.2.1 变更要点（2026-11-22 复测修复，测试岗《修复复测报告-20261122_1400》）：
- R-18（P0 残留）：宿主「记忆类一次性注入」（启发式记忆/人物画像/图片记忆）
  可能含被遮蔽时段提炼的条目 ⇒ 有隐藏窗口时整段屏蔽（planner_processor
  第 1.6 步 MEMORY_INJECTION_MARKERS，startswith 判定防误删）
- R-19（P2）：扩窗档位 24/96/384 收敛为 24/96 —— 永远失配的块不再驱动
  第三档 384h 取数（hook 超时上限 6000ms，库大了会贵）
- R-20（P2）：版本号 bump 0.2.1 + 诊断命令显示版本（修复前后可区分；诊断命令已于 0.2.3 删除）
- R-13b 语义拍板（PM 2026-11-22）：回退之后、回退会话期间新写下的
  待办/记忆 = 新时间线的新记忆，**不隐藏**（created_at 落在隐藏区间外
  本来就不滤，与实现一致；语义边界已写进《时间回滚设计》）
- R-17 判定为误诊：工具续跑轮走回 chat_loop_step（reasoning_engine.py:479
  → chat_loop_step，hook 挂在其中）⇒ hook 每轮触发；复测两份快照里
  草莓蛋糕待办两轮都不在 ⇒ 擦除未失效。第二轮出现的「绿萝」待办
  created_at=13:51:21 > 回退上界 13:49:54，属 R-13b 新记忆语义

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
  - F1-2：触发不保证开口——后台轮询回读 get_recent 确认她是否真开口；
    未响应记入 runtime_state 计数。0.2.3 起回读结果**只写插件日志**，
    不再往聊天流发「【MaiKnock 回读】…」消息（PM 2026-11-24：那种系统日志腔
    会进对话历史被麦麦学舌，暴露插件存在）
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
import random
import re
import struct
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

# F-14（0.2.3 回归轮二·P2）：tomllib 是 Python 3.11+ 标准库；低版本用 tomli 兜底。
# 原先在函数内局部 import，ImportError 会被 except Exception 吞掉 → 静默回退宿主段
# （症状与 F-12 一模一样、零提示），提到模块级一次性定案
try:
    import tomllib  # Python 3.11+
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

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
VERSION = "0.4.0"

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
    # timeline.py 的 `build_back_declaration`
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
# F4 小说体风格守卫：纯逻辑模块 style_guard.py（无 IO、无宿主依赖，可离线单测）
#
# ⚠️ 必须按文件路径加载（裸 `import style_guard` 会 ImportError，踩坑 66：
#    宿主把插件**父目录**加进 sys.path，不是插件目录本身）。
# ⚠️ R6-29 纪律：加载失败必须可见（独立 error 日志），不许静默降级——
#    静默降级是本项目历史上最贵的坑。
# ---------------------------------------------------------------------------
mk_style_guard = None
try:  # pragma: no cover - 加载失败只降级 F4，不阻断插件其余功能
    mk_style_guard = _load_sibling("style_guard", "style_guard.py")
except Exception as _sg_exc:  # noqa: BLE001
    mk_style_guard = None
    logger.error("[MaiKnock] F4 风格守卫模块 style_guard.py 加载失败，F4 功能不可用：%r", _sg_exc)

# F5 功能 A 字数配置：纯逻辑模块 reply_length.py（软约束，无硬截断）
# ⚠️ 按文件路径加载（裸 import 会 ImportError，踩坑 66）；R6-29：加载失败必须可见
mk_reply_length = None
try:  # pragma: no cover - 加载失败只降级 F5，不阻断插件其余功能
    mk_reply_length = _load_sibling("reply_length", "reply_length.py")
except Exception as _rl_exc:  # noqa: BLE001
    mk_reply_length = None
    logger.error("[MaiKnock] F5 字数模块 reply_length.py 加载失败，F5 功能不可用：%r", _rl_exc)


# ---------------------------------------------------------------------------
# 纯逻辑模块：proactive_policy.py（F1 策略）+ commands.py（命令参数解析）
#
# 0.2.3 测试报告建议 ⑤：这两个模块原先**内联在下面的实例方法里**，离线测不了
# （要 import 整个 plugin.py 就得把 maibot_sdk 一起桩起来）。
# 抽出后 UT-B 组（命令解析）与 UT-C 组（抽签/免打扰）可以纯 Python 离线单测。
#
# ⚠️ 只做「搬家」，判定口径逐字不变；调用点见下面各处转调。
# ⚠️ 必须按文件路径加载（裸 import 会 ImportError，踩坑 66）——
# ⚠️ R6-29 纪律：加载失败必须可见（独立 error 日志），不许静默降级。
# ---------------------------------------------------------------------------
mk_proactive_policy = None
mk_commands = None
try:  # pragma: no cover - 加载失败只降级这两块纯逻辑，不阻断插件其余功能
    mk_proactive_policy = _load_sibling("proactive_policy", "proactive_policy.py")
except Exception as _pp_exc:  # noqa: BLE001
    mk_proactive_policy = None
    logger.error("[MaiKnock] 纯逻辑模块 proactive_policy.py 加载失败，F1 策略判定不可用：%r", _pp_exc)
try:  # pragma: no cover
    mk_commands = _load_sibling("commands", "commands.py")
except Exception as _cmd_exc:  # noqa: BLE001
    mk_commands = None
    logger.error("[MaiKnock] 纯逻辑模块 commands.py 加载失败，命令参数解析不可用：%r", _cmd_exc)


# ---------------------------------------------------------------------------
# 麦麦聊天室子包（chatroom/）：中控 + 18 个 WebUI 接口面
#
# ⚠️ 加载方式：**按目录**加载成一个包（`_load_package`），而不是按文件。
#    因为包内要用**相对 import**（`from .mock_data import ...`），
#    而宿主把插件**父目录**加进 sys.path、不是插件目录本身 ⇒
#    裸 `import chatroom` 会 ImportError（踩坑 66）。
# ⚠️ **先注册进 sys.modules 再 exec** —— 否则包内相对 import 找不到自己。
# ⚠️ R6-29 纪律：加载失败必须可见（独立 error 日志），不许静默降级。
# ⚠️ 失败时的降级策略：**换成桩基类**，插件照常加载、敲门功能不受影响；
#    只是聊天室的 18 个接口不存在（页面上会报「插件无组件: shell_xxx」，
#    原因明确）——**绝不让聊天室的加载失败把整个插件拖下水**。
# ---------------------------------------------------------------------------


def _load_package(package_name: str, dirname: str):
    """按目录加载插件内的子包，并**先注册 sys.modules 再 exec**（支持包内相对 import）。"""
    import importlib.util as _iu
    import sys as _sys

    pkg_dir = Path(__file__).parent / dirname
    init_file = pkg_dir / "__init__.py"
    if not init_file.exists():
        raise ImportError(f"子包入口不存在：{init_file}")
    spec = _iu.spec_from_file_location(
        package_name, str(init_file), submodule_search_locations=[str(pkg_dir)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {pkg_dir} 创建包 spec")
    mod = _iu.module_from_spec(spec)
    _sys.modules[package_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _ChatroomStub:
    """聊天室子包加载失败时的占位基类。

    只提供 ``__init__`` 透传，**不提供任何 API** ⇒ 宿主收集组件时
    不会收进聊天室的接口，页面调用会得到明确的「插件无组件」错误，
    而敲门本体照常工作。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


mk_chatroom = None
try:  # pragma: no cover - 加载失败只降级聊天室，不阻断插件其余功能
    mk_chatroom = _load_package("maichat_chatroom", "chatroom")
    _CHATROOM_BASE = mk_chatroom.api_mixin()
except Exception as _cr_exc:  # noqa: BLE001
    mk_chatroom = None
    _CHATROOM_BASE = _ChatroomStub
    logger.error(
        "[MaiKnock] 聊天室子包 chatroom/ 加载失败，聊天室功能不可用（敲门本体不受影响）：%r",
        _cr_exc,
    )


# ---------------------------------------------------------------------------
# 配置模型（自动生成 config.toml 与 WebUI 配置页）
# 注意：__ui_label__ 仅在嵌套的 PluginConfigBase 子类上生效；
# config_version 必须位于 [plugin] 节内（宿主强制约定 runner_main.py）。
# ---------------------------------------------------------------------------


class PluginSection(PluginConfigBase):
    """麦麦敲门的总开关。"""

    __ui_label__ = "插件设置"

    enabled: bool = Field(
        default=True, description="关掉后整个插件不会加载（此项由宿主读取，不是插件内部开关）",
        json_schema_extra={"label": "启用麦麦敲门"})
    config_version: str = Field(
        default=VERSION, description="配置结构版本（宿主要求保留此项，无需修改）",
        json_schema_extra={"label": "配置版本", "hidden": True})


class QuietSpanConfig(PluginConfigBase):
    """一个免打扰时段。开始==结束 = 该段无效；开始>结束 = 跨天（如 22:00~06:00）。"""

    __ui_label__ = "免打扰时段"

    开始: Literal[
        "00:00", "01:00", "02:00", "03:00", "04:00", "05:00",
        "06:00", "07:00", "08:00", "09:00", "10:00", "11:00",
        "12:00", "13:00", "14:00", "15:00", "16:00", "17:00",
        "18:00", "19:00", "20:00", "21:00", "22:00", "23:00",
    ] = Field(default="01:00", json_schema_extra={"label": "开始"})
    结束: Literal[
        "00:00", "01:00", "02:00", "03:00", "04:00", "05:00",
        "06:00", "07:00", "08:00", "09:00", "10:00", "11:00",
        "12:00", "13:00", "14:00", "15:00", "16:00", "17:00",
        "18:00", "19:00", "20:00", "21:00", "22:00", "23:00",
    ] = Field(default="08:00", json_schema_extra={"label": "结束"})


class ProactiveConfig(PluginConfigBase):
    """太久没聊天时，麦麦会主动发消息来找你。"""

    __ui_label__ = "麦麦主动找你聊天"

    enabled: bool = Field(
        default=False, description="打开后，冷场时间一到，她就可能主动发消息来找你",
        json_schema_extra={"label": "开启主动找你"})
    silence_threshold_minutes: int = Field(
        default=60, ge=5, le=1440,
        description="冷场区间的最早值：比如 60 = 至少一小时没说话她才可能来找你。"
        "实际来找你的时刻在「最早」和「最晚」之间随机挑一个，不会准点出现",
        json_schema_extra={"label": "冷场多久之后来找你·最早（分钟）"})
    silence_threshold_max_minutes: int = Field(
        default=90, ge=5, le=1440,
        description="冷场区间的最晚值：比如 90 = 最晚一个半小时内一定来找你。"
        "设成和「最早」一样大就退回固定时刻",
        json_schema_extra={"label": "冷场多久之后来找你·最晚（分钟）"})
    daily_max: int = Field(
        default=5, ge=1, le=50, description="一天之内，她最多主动来找你几次",
        json_schema_extra={"label": "每天最多主动几次"})
    免打扰时段: List[QuietSpanConfig] = Field(
        default_factory=lambda: [QuietSpanConfig()],
        description="可添加多条。开始==结束 = 该段无效；开始>结束 = 跨天（如 22:00~06:00）",
        json_schema_extra={"label": "免打扰时段（麦麦在这段时间内不会找你）", "max_items": 12})


class StyleGuardConfig(PluginConfigBase):
    """让麦麦的回复固定成三段式，就像这样：【他今天好像不太开心……】（把刚倒的水轻轻推到鸣人手边）先喝点水吧。"""

    __ui_label__ = "小说体说话格式"

    enabled: bool = Field(
        default=True,
        description="打开后，她的每条回复都会固定成三段：【】内心 →（动作）→ 直接说话。"
        "下面的「内心想法」「动作/环境」两个开关只有在这一项打开时才起作用",
        json_schema_extra={"label": "启用小说体格式"})
    内心想法: bool = Field(
        default=True,
        description="只在「启用小说体格式」打开时生效。开：每条回复都带【内心想法】（没有就让她重说一遍）；"
        "关：回复里永远不会出现【】内容",
        json_schema_extra={"label": "内心想法【】"})
    动作环境: bool = Field(
        default=True,
        description="只在「启用小说体格式」打开时生效。开：每条回复都带（动作或环境描写）（没有就让她重说一遍）；"
        "关：回复里永远不会出现（）内容",
        json_schema_extra={"label": "动作/环境（）"})


class ReplyLengthConfig(PluginConfigBase):
    """控制麦麦一条回复的篇幅"""

    __ui_label__ = "回复字数"

    # ⚠️ 配置窗口只有一个中文下拉档位——档位自带字数范围，从源头防呆：
    #    用户选不到「最大 < 最小」，不需要任何数值输入校验（0.3.3 用户拍板）
    #    「自由」= 不注入任何字数契约，麦麦完全自己决定篇幅
    回复篇幅: Literal[
        "简短（60-200字）",
        "适中（100-400字）",
        "详细（150-600字）",
        "自由",
    ] = Field(
        default="简短（60-200字）",
        description="选一个档位决定麦麦一条回复的大致篇幅。简短=60到200字，适中=100到400字，"
        "详细=150到600字，自由=不限制、麦麦自己决定。这是大致范围、不是硬性截断，自然表达优先。"
        "字数只算说出口的话，【】内心和（）动作不算",
        json_schema_extra={"label": "回复篇幅（一句话选档位）"})


class ChatroomSplitterConfig(PluginConfigBase):
    """聊天室里的回复不要被拆成好几个气泡。"""

    __ui_label__ = "回复气泡"

    enabled: bool = Field(
        default=False,
        description="打开后，聊天室里的麦麦回复会**整条显示为一个气泡**（内部用段间空行分段，"
        "而不是拆成多个气泡）。只影响聊天室，不影响 QQ/微信等真群聊的分段行为。"
        "关掉就退回宿主默认分段。",
        json_schema_extra={"label": "一条回复只显示为一个气泡"})


class ChatroomDeliveryConfig(PluginConfigBase):
    """把你写的台词递进各 Bot 的虚拟群。"""

    __ui_label__ = "台词递送"

    enabled: bool = Field(
        default=False,
        description="打开后，聊天室里的台词会通过宿主的消息网关递进虚拟群，麦麦才会接话。"
        "关掉 = 界面照常，但不递话（麦麦不会开口）。",
        json_schema_extra={"label": "把台词递进虚拟群"})


class ChatroomCollectConfig(PluginConfigBase):
    """把 Bot 在虚拟群里的回复收回来，显示到时间线上。"""

    __ui_label__ = "回复回收"

    enabled: bool = Field(
        default=False,
        description="打开后，聊天室会读取各 Bot 虚拟群里的新消息并显示到时间线上。"
        "关掉 = 只递话不收话（时间线上只有你方台词）。",
        json_schema_extra={"label": "把 Bot 的回复收回来"})


class ChatroomConfig(PluginConfigBase):
    """麦麦聊天室：多个 Bot 与你同台一个虚拟群，你写台词、她们接话。"""

    __ui_label__ = "麦麦聊天室"

    enabled: bool = Field(
        default=False,
        description="打开后聊天室功能才启用。关掉 = 插件只剩「麦麦敲门」那部分，行为跟没装聊天室一样。",
        json_schema_extra={"label": "启用麦麦聊天室"})
    data_source: Literal["mock", "real"] = Field(
        default="mock",
        description="mock = 演示模式，全部是假数据、不连接任何 Bot（出厂状态）；"
        "real = 正式模式，真的递话、真的收回复。出问题时把它切回 mock 就是一个总后门。",
        json_schema_extra={"label": "数据来源"})
    ledger_path: str = Field(
        default="",
        description="共享账本（SQLite）的绝对路径。**所有参与的 Bot 必须配成同一个路径**——"
        "它们靠这一个文件互相传递消息。留空 = 不启用共享账本，聊天室只能单机单 Bot 用。"
        "建议放在所有 Bot 的 data 目录之外，例如 E:\\Softw\\MaiBot\\maichat\\chatroom.db",
        json_schema_extra={"label": "共享账本路径"})
    api_key: str = Field(
        default="",
        description="**本 Bot 自己**的消息服务 Key（就是该 Bot 的 bot_config.toml 里 "
        "[maim_message].api_server_allowed_api_keys 中的一条）。插件用它把台词递进本 Bot 的虚拟群。"
        "每个 Bot 填自己的，不要互相复制。",
        json_schema_extra={"label": "本 Bot 的消息服务 Key"})

    splitter: ChatroomSplitterConfig = Field(
        default_factory=ChatroomSplitterConfig, description="回复气泡")
    delivery: ChatroomDeliveryConfig = Field(
        default_factory=ChatroomDeliveryConfig, description="台词递送")
    collect: ChatroomCollectConfig = Field(
        default_factory=ChatroomCollectConfig, description="回复回收")


class MaiknockConfig(PluginConfigBase):
    """麦麦敲门插件总配置。"""

    __ui_label__ = "麦麦敲门"

    plugin: PluginSection = Field(default_factory=PluginSection, description="插件开关")
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig, description="麦麦主动找你聊天")

    style_guard: StyleGuardConfig = Field(default_factory=StyleGuardConfig, description="小说体说话格式")
    reply_length: ReplyLengthConfig = Field(default_factory=ReplyLengthConfig, description="回复字数")

    chatroom: ChatroomConfig = Field(default_factory=ChatroomConfig, description="麦麦聊天室")


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


class MaiKnockPlugin(_CHATROOM_BASE, MaiBotPlugin):
    """麦麦敲门 + 麦麦聊天室。

    ⚠️ **基类顺序有意义**：`_CHATROOM_BASE`（聊天室的 API 混入类）在前、
       `MaiBotPlugin` 在后 ⇒
       * 宿主用 `dir(instance)` 收集组件（maibot_sdk/components.py:788），
         **MRO 上的方法都会被收进去** ⇒ 聊天室的 18 个接口照样能被 WebUI 调用；
       * 子包加载失败时 `_CHATROOM_BASE` 退化成 `_ChatroomStub` ⇒ 插件照常加载。
    """
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
        # F-12（0.2.3 回归轮 2026-11-24 实测·P1）：宿主 config.get_plugin 返回的是
        # 它自己读文件后的**缓存段**（保留未知键、缺失新键不填默认值），且宿主
        # watchfiles 丢变更通知时缓存陈旧是永久的 ⇒ F4 开关真值一律以原始
        # config.toml 为准（_effective_style_guard）。这里缓存原始文件的
        # (mtime, [style_guard] 段)，mtime 没变就复用，变了才重读
        self._f4_raw_cache: Optional[Tuple[float, Dict[str, Any]]] = None
        # 旧格式 [style_guard.blocks] 接管的 warning 去重指纹（值组合变了才再打）
        self._f4_legacy_warn_key: Optional[Tuple[Optional[bool], Optional[bool]]] = None
        # ── 麦麦聊天室（S1 起的合并部分）────────────────────────────────
        # 共享账本（跨 Bot 交换消息的唯一载体，chatroom/ledger.py）
        self._chatroom_ledger = None
        # 我是哪个 Bot（从工作区 registry 反查，chatroom/hostinfo.py）
        self._chatroom_bot_id: str = ""
        self._chatroom_bot_name: str = ""
        # 工作区根（Path 或 None）
        self._chatroom_workspace = None
        # maim 网关连接池（跨 Bot 递话的通道，chatroom/gateway.py）
        self._chatroom_gateway = None
        # 投递 worker（领单 → 递话 → 回填状态，chatroom/worker.py）
        self._chatroom_worker = None

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
        # F-10/F-12：预热原始 config.toml 缓存并提前告警旧格式——
        # F4 开关真值每次 gate 都以原始文件为准（_effective_style_guard），
        # 这里只是让迁移 warning 在加载时就打出来，不等第一条消息
        self._detect_legacy_style_guard()
        self._ensure_scan_loop()
        # 麦麦聊天室：定位工作区 → 认出自己的 bot_id → 打开共享账本
        await self._chatroom_bootstrap()

    # ------------------------------------------------------------------
    # 麦麦聊天室 · 启动准备
    #
    # ⚠️ **任何一步失败都只记日志、不抛异常** —— 聊天室不可用不该把敲门带下水。
    # ⚠️ 账本在**加载时就打开并建表**：路径不对、权限不够、目录不存在这类问题
    #    应当在日志里当场暴露，而不是等到 S5 递话时才发现。
    # ------------------------------------------------------------------

    async def _chatroom_config(self) -> Dict[str, Any]:
        """读插件配置的 [chatroom] 段（读不到就返回空 dict ⇒ 全走默认）。"""
        try:
            raw = await self._plugin_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock][聊天室] 读取配置失败，走默认值：%r", exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        cfg = raw.get("chatroom")
        return cfg if isinstance(cfg, dict) else {}

    async def _chatroom_bootstrap(self) -> None:
        """定位工作区 → 认出我是哪个 Bot → 打开共享账本。"""
        if mk_chatroom is None:
            logger.warning("[MaiKnock][聊天室] 子包未加载，聊天室不可用")
            return
        data_dir = getattr(getattr(self.ctx, "paths", None), "data_dir", None)
        if data_dir is None:
            logger.warning("[MaiKnock][聊天室] 拿不到插件数据目录，聊天室不可用")
            return
        workspace = mk_chatroom.hostinfo.find_workspace_root(Path(data_dir))
        if workspace is None:
            logger.warning(
                "[MaiKnock][聊天室] 找不到工作区根（从 %s 向上找 %s/%s 失败），聊天室不可用",
                data_dir, mk_chatroom.hostinfo.HOST_DIR_NAME, mk_chatroom.hostinfo.REGISTRY_FILE_NAME,
            )
            return
        self._chatroom_workspace = workspace
        me = mk_chatroom.hostinfo.resolve_self_bot(workspace, Path(data_dir))
        if me is None:
            logger.warning("[MaiKnock][聊天室] 认不出自己是哪个 Bot（registry 里没有匹配的工作目录），聊天室不可用")
            return
        self._chatroom_bot_id = str(me.get("bot_id") or "")
        self._chatroom_bot_name = str(me.get("name") or "")

        cfg = await self._chatroom_config()
        logger.info(
            "[MaiKnock][聊天室] 配置：enabled=%s data_source=%s 账本=%s key=%s",
            cfg.get("enabled"), cfg.get("data_source"),
            "已配" if str(cfg.get("ledger_path") or "").strip() else "未配（用推导路径）",
            "已配" if str(cfg.get("api_key") or "").strip() else "未配",
        )
        ledger_path = str(cfg.get("ledger_path") or "").strip()
        if not ledger_path:
            # ⚠️ 默认路径由**工作区根**推导 ⇒ 一号二号拿到的是**同一个路径**
            #    （`<工作区>/maichat/chatroom.db`），不用人工对齐。
            ledger_path = mk_chatroom.hostinfo.default_ledger_path(workspace)
        try:
            ledger = mk_chatroom.ChatroomLedger(ledger_path, bot_id=self._chatroom_bot_id)
            ledger.open()
        except Exception as exc:  # noqa: BLE001
            logger.error("[MaiKnock][聊天室] 打开共享账本失败（%s）：%r", ledger_path, exc)
            return
        self._chatroom_ledger = ledger
        logger.info(
            "[MaiKnock][聊天室] 已就绪：我是 %s（bot_id=%s），账本=%s，工作区=%s",
            self._chatroom_bot_name or "（未命名）", self._chatroom_bot_id[:12] or "（空）",
            ledger_path, workspace,
        )
        await self._chatroom_gateway_bootstrap(cfg)

    async def _chatroom_gateway_bootstrap(self, cfg: Dict[str, Any]) -> None:
        """连上**本 Bot 自己**的 maim 网关（接入校验的**握手层**）。

        ⚠️ **只连自己**：方案 B（各 Bot 的代理实例各递各的）下，每个实例只需要
        知道自己那个 Bot 的 key —— 别的 Bot 的 key 根本不该出现在本进程里
        （凭据隔离）。别的 Bot 的状态由它们各自的实例写进共享账本。

        ⚠️ 聊天室总开关关着时**不连**（保持出厂状态什么都不碰）。
        """
        if not bool(cfg.get("enabled")):
            logger.info("[MaiKnock][聊天室] 总开关未开，不连消息网关（出厂状态）")
            return
        api_key = str(cfg.get("api_key") or "").strip()
        if not api_key:
            logger.warning("[MaiKnock][聊天室] 没配 api_key，跳过握手（接入校验的握手层会判为未就绪）")
            return
        port = mk_chatroom.hostinfo.read_webui_port(self._chatroom_workspace)
        pool = mk_chatroom.GatewayPool(host="127.0.0.1", port=port)
        pool.set_api_key(self._chatroom_bot_id, api_key)
        self._chatroom_gateway = pool
        conn = await pool.get(self._chatroom_bot_id)
        if conn is None or not conn.connected:
            reason = (conn.last_error if conn else "未知原因") or "未知原因"
            logger.warning("[MaiKnock][聊天室] 握手层未通过（Bot %s）：%s", self._chatroom_bot_id[:12], reason)
            return
        await conn.start_keepalive()
        logger.info(
            "[MaiKnock][聊天室] 握手层通过：已连上 %s 的消息网关（%s）",
            self._chatroom_bot_id[:12], conn.endpoint(),
        )
        await self._chatroom_start_worker()
        await self._chatroom_report_status()

    async def _chatroom_start_worker(self) -> None:
        """起投递循环：把账本里派给**本 Bot** 的台词递进本 Bot 的虚拟群。

        ⚠️ **各 Bot 各领各的单**（方案 B）：每个实例只用自己的 key 递给自己，
        跨 Bot 的协调靠共享账本里的投递任务。
        """
        if self._chatroom_worker is not None:
            return
        ledger = self._chatroom_ledger
        pool = self._chatroom_gateway
        if ledger is None or pool is None:
            return
        delivery_enabled = bool((await self._chatroom_config()).get("delivery", {}).get("enabled"))
        if not delivery_enabled:
            logger.info("[MaiKnock][聊天室] 递送开关未开，不起投递循环")
            return
        worker = mk_chatroom.DeliveryWorker(ledger, pool, self._chatroom_bot_id)
        worker.start()
        self._chatroom_worker = worker

    async def _chatroom_style_fixed(self) -> Optional[bool]:
        """本 Bot 的说话风格是否已按敲门侧固化。

        **判定口径**：F4 小说体格式守卫在生效（``style_guard.enabled = true``）
        ⇒ 风格已固化；关掉 ⇒ 未固化；读不到 ⇒ 不知道（返回 None）。

        02 文档 8.4 第 3 条：「具体怎么判定由**敲门侧**提供结果，聊天室只认这个结果、
        不关心实现」—— 这里就是敲门侧，判定口径由本插件自己给。
        """
        try:
            raw = await self._plugin_config()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(raw, dict):
            return None
        cfg = raw.get("style_guard")
        if not isinstance(cfg, dict):
            return None
        try:
            effective = self._effective_style_guard(cfg)
        except Exception:  # noqa: BLE001
            return None
        return bool(effective.get("enabled", True))

    async def _chatroom_report_status(self) -> None:
        """把**本实例**的接入状态写进共享账本（各 Bot 自报，中控汇总）。"""
        ledger = self._chatroom_ledger
        if ledger is None or not self._chatroom_bot_id:
            return
        pool = self._chatroom_gateway
        handshake_ok: Optional[bool] = None
        detail = ""
        if pool is not None:
            rows = [row for row in pool.status() if row.get("bot_id") == self._chatroom_bot_id]
            if rows:
                handshake_ok = bool(rows[0].get("connected"))
                detail = str(rows[0].get("error") or "")
        style_fixed = await self._chatroom_style_fixed()
        status = "ready"
        if handshake_ok is False:
            status = "gateway_off"
        elif style_fixed is False:
            status = "style_unfixed"
        try:
            ledger.set_bot_status(
                self._chatroom_bot_id,
                status,
                name=self._chatroom_bot_name,
                detail=detail,
                handshake_ok=handshake_ok,
                style_fixed=style_fixed,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock][聊天室] 写接入状态失败：%r", exc)

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
        if tomllib is None:  # F-14：Python < 3.11 且无 tomli
            return ""
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            return str((data.get("plugin") or {}).get("config_version") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _detect_legacy_style_guard(self) -> None:
        """启动/配置变更时预热原始 config.toml 缓存，并提前告警旧格式。

        ⚠️ 根因订正（0.2.3 回归轮 2026-11-24 实测，F-12·P1）：宿主
        ``config.get_plugin`` 返回的是它自己读文件后的**缓存段**——保留
        ``blocks`` 未知键、缺失新键**不填**默认值；宿主 watchfiles 丢变更
        通知时这份缓存**陈旧是永久的**（on_config_update 不触发）。早前
        「pydantic 归一化剥掉 blocks」的判断是错的，特此更正。

        ⇒ F4 开关真值不能信宿主段：``_effective_style_guard`` 每次 gate 都
        以原始 config.toml 为准（mtime 缓存）。本方法只负责 ①预热缓存；
        ②旧格式迁移 warning 在加载时就打，不等第一条消息。
        """
        self._f4_legacy_warn_key = None
        raw = self._raw_style_guard()
        if not raw:
            return
        blocks = raw.get("blocks")
        if (
            isinstance(blocks, dict)
            and ("inner_enabled" in blocks or "action_enabled" in blocks)
            and "内心想法" not in raw
            and "动作环境" not in raw
        ):
            key = (
                bool(blocks.get("inner_enabled", True)),
                bool(blocks.get("action_enabled", True)),
            )
            if self._f4_legacy_warn_key != key:
                self._f4_legacy_warn_key = key
                logger.warning(
                    "[MaiKnock] 检测到旧版配置 [style_guard.blocks]——开关真值已按它生效："
                    "内心想法=%s 动作/环境=%s。在 WebUI 配置页保存一次设置即可彻底迁移成新格式",
                    key[0], key[1],
                )

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
        if self._chatroom_ledger is not None:
            try:
                self._chatroom_ledger.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[MaiKnock][聊天室] 关闭共享账本时出错：%r", exc)
            self._chatroom_ledger = None
        if self._chatroom_worker is not None:
            try:
                await self._chatroom_worker.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[MaiKnock][聊天室] 停投递循环时出错：%r", exc)
            self._chatroom_worker = None
        if self._chatroom_gateway is not None:
            try:
                await self._chatroom_gateway.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[MaiKnock][聊天室] 关闭消息网关连接时出错：%r", exc)
            self._chatroom_gateway = None
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
        # F-10：config.toml 可能被改过（WebUI 保存 / 手编），重新检测旧格式接管
        self._detect_legacy_style_guard()
        # M2：配置变更可能启用/调整扫描循环；幂等确保它在跑（热重载后宿主可能重新推配置）
        self._ensure_scan_loop()

    # ---- 数据存取（N-2：拆文件 + 原子写）----
    # bindings.json     —— 低频写（敲任意 /mk 命令时自动绑定），绑定信息
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
        `/mk now` 的「现实时间：2026-11-21 18:57:00」等命令输出**都会**
        进对话历史被麦麦看到 —— 逐个命令各写一遍必然有漏网的。
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
        description="麦麦敲门：now 查看剧情时间线 / add 推进 / back 回退 / set 设定 / hi 让麦麦开口",
        pattern=r"^/mk\b",
    )
    async def handle_mk(self, **kwargs: Any):
        # M2：命令入口兜底启动扫描循环（热重载不走 on_load，用户可能直接敲命令不配配置）
        self._ensure_scan_loop()
        text = str(kwargs.get("text") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")
        platform = str(kwargs.get("platform") or "")
        user_id = str(kwargs.get("user_id") or "")

        # B-6：归一化 @机器人 前缀（如「@麦麦 /mk now」），取 /mk 起始部分
        idx = text.find("/mk")
        if idx > 0:
            text = text[idx:]

        # 自动绑定（0.2.3）：私聊里敲任何 /mk 命令 ⇒ 该流自动成为「麦麦主动找你」的目标，
        # 不再有手动绑定命令（PM 2026-11-23 拍板：能删的命令就删）。
        # 校验失败（拿不到私聊清单/不是私聊流）一律静默跳过，绝不挡命令。
        bind_note = await self._auto_bind(stream_id, platform, user_id)

        parts = text.split(maxsplit=2)  # ["/mk", 子命令, 其余]
        sub = parts[1].lower() if len(parts) >= 2 else ""
        rest = parts[2].strip() if len(parts) >= 3 else ""

        if sub == "hi":
            reply = await self._cmd_hi(rest, stream_id)
        elif sub in ("now", "add", "back", "set"):
            reply = await self._cmd_timeline(sub, rest)
        else:
            reply = (
                "麦麦敲门命令：\n"
                "/mk now —— 看麦麦理解中的剧情时间\n"
                "/mk add 3d 这三天我们去了海边 —— 剧情时间推进 3 天（建议附带剧情提示）\n"
                "/mk back 10h 夜深之后 —— 剧情时间回到 10 小时前（建议附带剧情提示）\n"
                "/mk set 09:00 此时你只觉得 —— 直接指定剧情时间（建议附带剧情提示）\n"
                "/mk hi 马上过来给我讲讲今天的事 —— 让麦麦立刻主动开口（hi 后面直接写想让她做的事）"
            )

        if bind_note:
            reply = bind_note + "\n" + reply

        # 回复统一带【MaiKnock】前缀（审计 13:24 第 15 条加固）：回读排除规则
        # 「startswith('【MaiKnock')」因此对所有插件回复完备——否则回读窗口内
        # 敲 now/add/back/set/hi 会被误判成麦麦开口
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

    async def _auto_bind(self, stream_id: str, platform: str, user_id: str) -> str:
        """私聊里敲 /mk ⇒ 自动绑定该流为「麦麦主动找你」的目标（替代原手动绑定命令）。

        返回给用户看的提示（未新绑定返回空串）。任何失败都静默跳过：
        绑定只是 F1 的目标记录，绝不能挡住娱乐命令。
        """
        if not stream_id or not platform:
            return ""
        state = self._load_state()
        if str(state.get("bound_stream_id") or "") == stream_id:
            return ""
        try:
            res = await self.ctx.call_capability("chat.get_private_streams", platform=platform)
            streams = (res or {}).get("streams") if isinstance(res, dict) else None
            private_ids = {
                str(s.get("stream_id") or s.get("session_id") or "")
                for s in (streams or []) if isinstance(s, dict)
            }
            if stream_id not in private_ids:
                return ""  # 群聊等非私聊流：不绑定也不提示（T-F03 口径）
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] 自动绑定校验失败，跳过（platform=%s）：%r", platform, exc)
            return ""
        state["bound_stream_id"] = stream_id
        state["bound_platform"] = platform
        state["bound_user_id"] = user_id
        state["bound_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_state(state)
        logger.info("[MaiKnock] 自动绑定聊天流：%s (platform=%s user=%s)", stream_id, platform, user_id)
        return "（已把本窗口设为「麦麦主动找你」的目标）"


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

        ⚠️ F-08（0.2.3 测试报告 2026-11-24）：**最常见的失败路径上本方法不会被调用**——
        宿主判配置非法时插件只加载配置管理，on_load / on_config_update 都不执行，
        此时「插件侧零日志」是设计使然而非缺陷。本方法真正兜住的只有上面②的
        解读差异与正常运行期的配置变更；不要指望它在宿主判非法时留下插件侧日志。
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
        接下来一个随机冷场区间内自动扫描也不该再主动找他
        （0.3.2 起静默计时被 last_trigger_manual 重置，天然满足）。
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
            return False, f"触发被冷却拦截：距上次主动触发还需约 {remain} 秒（防连环轰炸）"
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

        # 0.3.2 起 /mk hi 不再设冷却（PM 拍板：手动技能不该有冷却）；
        # 防叠触发靠 pending 锁位 + 回读窗口（_do_proactive_trigger 原有）。
        # cooldown_sec 传 0：手动锁位 last_trigger_manual 仍落盘（用于重置静默计时）。
        topic = rest.strip()
        # F1-2：intent 写成明确指令而非情境描述
        intent = "请现在主动向用户发起一条消息：自然地打个招呼并开启一个新话题。"
        if topic:
            intent += f"话头：{topic}"

        ok, msg = await self._do_proactive_trigger(
            target, intent, "MaiKnock /mk hi 手动触发", 0, set(), manual=True
        )
        if not ok:
            return msg
        head = msg
        if topic:
            head += f"，话头：{topic}"
        # F-03（0.2.3 测试报告坐实）：0.2.3 起回读结果只写插件日志、不再进聊天流，
        # 旧文案「稍后我会用『【MaiKnock 回读】』汇报结果」承诺了一条永远不会来的回执
        return head + "\n她是否真的开口由她自己决定——如果等了一会儿还没动静，再敲一次 /mk hi 就行。"

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
            logger.info("[MaiKnock] 回读确认：麦麦已开口（stream=%s）：“%s”", target, found_text)
        else:
            stats["no_response_total"] = int(stats.get("no_response_total", 0)) + 1
            logger.info("[MaiKnock] 回读超时：麦麦未响应（stream=%s）", target)
        self._write_json(self._RUNTIME_FILE, runtime)

        # ⚠️ PM 2026-11-24：回读结果**不再发进聊天流**。此前「【MaiKnock 回读】…」
        # 是麦麦账号发出的消息，会进对话历史——她翻自己的历史时把这种系统日志腔
        # 学舌出来，暴露插件存在。回读结果只写插件日志（上面 logger.info）。

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
        敲 /mk 命令无任何插件侧日志、无回复）。

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
            self._scan_log_skip("未绑定聊天流（用户在私聊里敲任意 /mk 命令即可自动绑定）")
            return  # 未绑定，静默等待用户敲任意 /mk 命令触发自动绑定

        now = time.time()
        # 1. 禁止时段（跨天处理）
        if await self._in_quiet_hours(now):
            self._scan_log_skip("处于禁止时段")
            return
        # 2. pending 锁位（有回读未结束就跳过，防任务塌缩）
        if target in self._proactive_pending:
            self._scan_log_skip("上一笔主动触发仍在等待麦麦响应")
            return
        # 3. 上次自动触发时刻（0.3.2 起删除固定 min_interval 冷却——PM 认定与冷场区间
        #    冗余且生硬。防骚扰改由第 5 步的「触发后冷场计时重置」承担；
        #    锁位本身保留，用于压缩静默时长）
        runtime = self._load_json(self._RUNTIME_FILE)
        pstate = runtime.get("proactive_state", {})
        last_effective = max(
            float(pstate.get("last_trigger", {}).get(target, 0.0)),
            float(pstate.get("last_trigger_global", 0.0)),
        )
        # 4. 每日上限（落盘按天重置）
        if await self._daily_quota_exceeded():
            self._scan_log_skip("今日主动开口次数已达上限")
            return
        # 5. 静默时长判定（0.3.2：随机区间——每次冷场在 [最早, 最晚] 内抽一个「本次目标」）
        min_sec = await self._cfg_int("proactive.silence_threshold_minutes", 60) * 60
        max_sec = await self._cfg_int("proactive.silence_threshold_max_minutes", 90) * 60
        if max_sec < min_sec:  # 用户把最晚设得比最早还小：兜底收敛，别让区间倒挂
            max_sec = min_sec
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
        # ⚠️ 只影响「静默计时」，**不**动触发锁位与 daily_count——
        # 决策 4 的四条全部保留（手动不消耗自动额度、不写自动锁位）。
        manual_ts = float((pstate.get("last_trigger_manual") or {}).get(target, 0.0))
        if manual_ts > 0:
            silence = min(silence, max(0.0, now - manual_ts))
        # 触发后冷场重置（0.3.2，替代原 min_interval 冷却——删固定冷却后防连环骚扰
        # 的**唯一屏障**，不可删）：距上次自动触发越近，有效静默越短。
        # 她刚找过你（无论你回没回），都要重新等满一个随机冷场区间。
        if last_effective > 0:
            silence = min(silence, max(0.0, now - last_effective))
        self._scan_log_silence(silence / 60)
        # 随机目标抽签（0.3.2 / 0.3.3 修正）：「这次要等到多久」在 [min, max] 内随机。
        # 语义（PM 0.3.3 确认）：**一次抽签、落盘复用**——抽中 76 就在第 76 分钟找；
        # 没回复则触发后清空重抽（见下方触发成功分支）。扫描 tick 只是每 2 分钟
        # 看一眼表，不是重抽。0.3.3 起抽签以 **2 分钟为步长**（偶数分钟）：
        # 与扫描节奏同网格，秒级粒度无意义。
        # 若每轮重抽，silence 刚过 min 就每 tick 都有概率命中小区间值，触发时刻
        # 会系统性偏向早端。读-改-写之间无 await（原子段，A-1 同款纪律）。
        fresh = self._load_json(self._RUNTIME_FILE)
        fpstate = fresh.setdefault("proactive_state", {})
        goal_raw = fpstate.get("silence_goal_sec")
        goal = float(goal_raw) if isinstance(goal_raw, (int, float)) else None
        # 抽签/越界重抽的判定在纯逻辑模块里（proactive_policy.py，可离线单测）
        if mk_proactive_policy is not None:
            need_draw = not mk_proactive_policy.goal_in_range(goal, min_sec, max_sec)
            draw = lambda: mk_proactive_policy.draw_silence_goal(min_sec, max_sec)  # noqa: E731
        else:  # 纯逻辑模块加载失败 ⇒ 退回内联同逻辑（行为一致，不静默降级）
            need_draw = goal is None or not (min_sec <= goal <= max_sec)
            draw = lambda: (min_sec // 60 + random.randrange(0, (max_sec - min_sec) // 60 + 1, 2)) * 60  # noqa: E731
        if need_draw:
            goal = draw()
            fpstate["silence_goal_sec"] = goal
            self._write_json(self._RUNTIME_FILE, fresh)
        if silence < goal:
            self._scan_log_skip(
                f"静默未达本次随机目标（还需约 {max(0, int((goal - silence) / 60))} 分钟）"
            )
            return

        # 满足 → 触发（复用公共链路，锁位/回读/计数统一）；cooldown 传 0（0.3.2 删固定冷却）
        intent = "用户已经有一段时间没有发消息了，请主动向用户发起一条消息，自然地打个招呼并开启一个新话题。"
        ok, msg = await self._do_proactive_trigger(
            target, intent, "MaiKnock M2 静默扫描", 0, set()
        )
        if ok:
            self._bump_daily_count()
            # 清空随机目标，下次触发重新抽签——不清的话每次都等到同一时长，随机性失效。
            # ⚠️ 必须重新读盘再写：_do_proactive_trigger 刚写过锁位，
            # 用旧快照写盘会把锁位覆盖回旧值（A-1 同款坑）
            fresh = self._load_json(self._RUNTIME_FILE)
            fresh.setdefault("proactive_state", {})["silence_goal_sec"] = None
            self._write_json(self._RUNTIME_FILE, fresh)
            self._scan_last_reason = ""  # 重置跳过原因状态，便于下一轮状态变化被记录
            logger.info(
                "[MaiKnock] M2 静默扫描已触发（stream=%s 静默 %.0f 分钟，本次随机目标 %.0f 分钟）",
                target, silence / 60, goal / 60,
            )
        else:
            logger.warning("[MaiKnock] M2 静默扫描触发被拦：%s", msg)

    async def _in_quiet_hours(self, now: float) -> bool:
        """免打扰时段判定（0.3.3：多区间列表，每段「开始/结束」双联整点下拉）。

        - 空清单 = 没有免打扰时段；
        - 开始 == 结束 = 空区间，该段跳过；
        - 开始 > 结束 = 跨天（如 22:00~06:00）；
        - 段配置非法（解析失败）→ 打一次去重告警并跳过该段（T-E01 同款：非法静默禁用）。
        """
        try:
            cfg = await self._plugin_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock] 配置读取失败，本轮免打扰判定按「无时段」处理：%r", exc)
            return False
        pro = cfg.get("proactive") if isinstance(cfg, dict) else None
        spans = pro.get("免打扰时段") if isinstance(pro, dict) else None
        if not isinstance(spans, list) or not spans:
            return False
        # 非法段告警（去重一次）——判定本身在纯逻辑模块里（可离线单测）
        if mk_proactive_policy is not None:
            bad = mk_proactive_policy.invalid_spans(spans)
            if bad and not getattr(self, "_quiet_hours_warned", False):
                self._quiet_hours_warned = True
                span = bad[0]
                logger.warning(
                    "[MaiKnock] 免打扰时段配置非法（%r~%r），该段已被静默跳过，请重新选择整点",
                    span.get("开始"), span.get("结束"),
                )
            return mk_proactive_policy.in_quiet_hours(spans, mk_proactive_policy.local_minutes(now))
        # 纯逻辑模块加载失败 ⇒ 退回内联同逻辑（行为一致，不静默降级）
        lt = time.localtime(now)
        minutes = lt.tm_hour * 60 + lt.tm_min
        for span in spans:
            if not isinstance(span, dict):
                continue
            s = self._parse_hhmm(str(span.get("开始") or ""))
            e = self._parse_hhmm(str(span.get("结束") or ""))
            if s is None or e is None:
                if not getattr(self, "_quiet_hours_warned", False):
                    self._quiet_hours_warned = True
                    logger.warning(
                        "[MaiKnock] 免打扰时段配置非法（%r~%r），该段已被静默跳过，请重新选择整点",
                        span.get("开始"), span.get("结束"),
                    )
                continue
            if s == e:
                continue  # 空区间（下拉选了同一个整点）
            if s < e:
                if s <= minutes < e:
                    return True
            else:  # 跨天：now >= s（当晚）或 now < e（次日凌晨）
                if minutes >= s or minutes < e:
                    return True
        return False

    @staticmethod
    def _parse_hhmm(value: str) -> Optional[int]:
        """解析 HH:MM 为分钟数，非法返回 None。

        实现在纯逻辑模块 proactive_policy.py（可离线单测）；此处保留薄封装，
        对外接口不变（外部调用方仍可用 ``MaiKnockPlugin._parse_hhmm``）。
        """
        if mk_proactive_policy is not None:
            return mk_proactive_policy.parse_hhmm(value)
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
        """解析时长：``3d`` / ``12h`` / ``90m`` / ``30s``；纯数字按小时计。

        实现在纯逻辑模块 commands.py（可离线单测）；此处保留薄封装，
        对外接口不变。
        """
        if mk_commands is not None:
            return mk_commands.parse_duration(text)
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
        """解析绝对目标时刻：``2026-11-25 09:00`` / ``2026-11-25``。

        实现在纯逻辑模块 commands.py（可离线单测）；此处保留薄封装，
        对外接口不变。
        """
        if mk_commands is not None:
            return mk_commands.parse_target_time(text)
        raw = (text or "").strip()
        if not raw:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return float(time.mktime(time.strptime(raw, fmt)))
            except ValueError:
                continue
        return None



    async def _cmd_timeline(self, sub: str, rest: str) -> str:
        """F2 剧情时间线四命令：now 查看 / add 推进 / back 回退 / set 设定。

        - ``/mk now``                          查看麦麦理解中的剧情时间（唯一入口）
        - ``/mk add 3d 这三天我们…``            剧情时间推进 3 天
        - ``/mk back 10h 夜深之后…``            剧情时间回到 10 小时前（那段时间会被藏起来）
        - ``/mk set 2026-11-25 09:00 此时…``    直接指定剧情时间（设到过去 = 自动按回退处理，跳过的时间藏起来）；
          时间三种写法都行：``2026-11-25 09:01:02`` / ``2026-11-25 09:01`` / ``2026-11-25``，
          没写的部分落到整分 / 当天 00:00:00，「现在几点」的具体感受交给剧情提示词自由发挥

        剧情提示词可带可不带：带了会作为叙事注入给麦麦；不带 = 让她自由发挥
        （并清掉上一次的提示词，不无限期生效）。
        """
        if tl is None:
            return "时间线模块未就绪（timeline.py 加载失败），F2 不可用。详见插件日志。"

        now = time.time()
        state = self._load_timeline()
        if state is None:
            state = tl.TimelineState()

        # 测试岗 T-A08（2026-11-22 坐实）：极端幅度会在渲染时 OverflowError ⇒
        # 状态已落盘、命令无回复。⇒ ①幅度上限前置校验；②**先渲染成功再写盘**。
        _MAX_OFFSET_SEC = 100.0 * 365.25 * 86400.0

        def _apply(new_state, action: str, narrative: str = "") -> str:
            # R6-18：剧情提示词不得含绝对日期（否则回退后麦麦能读出今天几号）
            try:
                new_state = tl.apply_narrative(new_state, narrative)
            except ValueError as exc:
                return f"操作已放弃：{exc}"
            if abs(float(new_state.offset_sec or 0.0)) > _MAX_OFFSET_SEC:
                return (
                    "操作已放弃：时间偏移超出上限（±100 年）。"
                    "状态未写入，时间线保持原样。"
                )
            try:
                desc = tl.describe_state(new_state, now)
            except (OverflowError, OSError, ValueError, struct.error) as exc:
                return f"操作已放弃：时间幅度超出可表示范围，状态未写入（{exc}）"
            if not self._save_timeline(new_state):
                return f"{action}失败：时间线状态写入失败（详见插件日志）"
            logger.info(
                "[MaiKnock] 时间线变更：%s mode=%s offset=%.0fs hidden=[%s, %s] 剧情提示=%s",
                action, new_state.mode, new_state.offset_sec,
                new_state.boundary_ts, new_state.hidden_until_ts,
                "有" if new_state.narrative else "无",
            )
            return f"{action}完成。\n" + desc

        text = (rest or "").strip()

        # ---- /mk now：查看（唯一入口）----
        if sub == "now":
            if text:
                return "查看不需要参数，直接发 /mk now 就好。"
            return tl.describe_state(state, now)

        # ---- /mk add 3d [剧情提示词] ----
        if sub == "add":
            parts = text.split(maxsplit=1)
            delta = self._parse_duration(parts[0]) if parts else None
            if delta is None:
                return "无法解析推进幅度。用法：/mk add 3d 这三天我们去了海边（幅度支持 3d / 12h / 90m；剧情提示可省略）"
            if delta <= 0:
                return "推进的幅度必须是正数（往未来走）。要往回走请用 /mk back"
            narrative = parts[1].strip() if len(parts) > 1 else ""
            try:
                new_state = tl.plan_forward(state, now, delta)
            except ValueError as exc:
                return f"推进失败：{exc}"
            return _apply(new_state, f"推进 {delta / 3600.0:.2f} 小时", narrative)

        # ---- /mk back 10h [剧情提示词] ----
        if sub == "back":
            parts = text.split(maxsplit=1)
            delta = self._parse_duration(parts[0]) if parts else None
            if delta is None:
                return "无法解析回退幅度。用法：/mk back 10h 夜深之后（幅度支持 10h / 3d / 90m；剧情提示可省略）"
            if delta <= 0:
                # F-04（0.2.3 测试报告）：与 add 对称的非正数前置校验
                return "回退的幅度必须是正数（往过去走）。要往未来走请用 /mk add"
            narrative = parts[1].strip() if len(parts) > 1 else ""
            try:
                new_state = tl.plan_backward(state, now, -delta)
            except ValueError as exc:
                return f"回退失败：{exc}"
            return _apply(new_state, f"回退 {delta / 3600.0:.2f} 小时", narrative)

        # ---- /mk set 09:00 [剧情提示词] ----
        if sub == "set":
            if not text:
                return "用法：/mk set 09:00 此时你只觉得…（时间支持四种写法：日期+时刻（带秒或只到分）/ 只写日期 / 只给时刻 = 故事当天的该时刻；没写的部分落到整分或当天 00:00:00；剧情提示可省略）"
            target, narrative = self._split_set_arg(text)
            if target is None:
                # F-02 残留（0.2.3 回归轮坐实）：只给时刻（09:00）= 故事当天的该时刻。
                # 按**故事当前日**算（state.story_now），不是现实日——回退态下故事
                # 已退到 3 天前，「当天 09:00」必须是故事里那天，否则语义跳
                target, narrative = self._split_time_only(text, state.story_now(now))
            if target is None:
                # T-A14 / F-02：示例不得含绝对日期——回退态下出站脱敏会把示例日期
                # 抹成「日期已隐去」，用户照抄必失败
                return "无法解析目标时刻。用法：/mk set 09:00 此时你只觉得…"
            try:
                new_state = tl.plan_set_story_time(state, now, target)
            except ValueError as exc:
                return f"设定失败：{exc}"
            return _apply(new_state, "设定故事时间", narrative)

        # 无兜底分支：handle_mk 的分发只把 now/add/back/set 传进来，
        # 「未知的子命令」永远不可达（0.2.3 测试报告 F-07：死代码已删）

    @staticmethod
    def _split_set_arg(text: str):
        """/mk set 的参数拆分：目标时刻 + 剧情提示词。

        支持 ``2026-11-25 09:00 剧情…`` / ``2026-11-25 剧情…`` / ``2026-11-25``。
        解析失败返回 (None, None)。

        实现在纯逻辑模块 commands.py（可离线单测）；此处保留薄封装，
        对外接口不变。
        """
        if mk_commands is not None:
            return mk_commands.split_set_arg(text)
        raw = (text or "").strip()
        if not raw:
            return None, None
        ts = MaiKnockPlugin._parse_target_time(raw)
        if ts is not None:
            return ts, ""
        parts = raw.split(maxsplit=2)
        if len(parts) >= 2:
            ts = MaiKnockPlugin._parse_target_time(parts[0] + " " + parts[1])
            if ts is not None:
                return ts, (parts[2].strip() if len(parts) > 2 else "")
        ts = MaiKnockPlugin._parse_target_time(parts[0])
        if ts is not None:
            return ts, (parts[1].strip() if len(parts) > 1 else "")
        return None, None

    @staticmethod
    def _split_time_only(text: str, anchor_story_ts: float):
        """``/mk set`` 只给时刻的解析（F-02 残留修复）：``09:00`` = 故事当天的该时刻。

        实现在纯逻辑模块 commands.py（可离线单测）；此处保留薄封装，
        纯逻辑模块加载失败时退回内联同逻辑（不静默降级）。
        """
        if mk_commands is not None:
            return mk_commands.split_time_only(text, anchor_story_ts)
        import re as _re

        raw = (text or "").strip()
        if not raw:
            return None, None
        parts = raw.split(maxsplit=1)
        m = _re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", parts[0])
        if not m:
            return None, None
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
            return None, None
        lt = time.localtime(anchor_story_ts)
        day_start = float(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        ts = day_start + h * 3600.0 + mi * 60.0 + s
        return ts, (parts[1].strip() if len(parts) > 1 else "")

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
        # 有偏移 / 有隐藏区间 / 有剧情提示词，任一成立都需要改写
        # （偏移可能归零但仍带着剧情提示词 ⇒ 也要注入）
        if (
            not state.offset_sec
            and not state.has_hidden_window()
            and not (getattr(state, "narrative", "") or "").strip()
        ):
            return None
        return state

    def _f2_declaration_text(self, state) -> str:
        """取本轮的叙事声明正文。

        ⚠️ **用状态里存的 `state.declaration`（不是现场生成）** ——
        机制 3 的 **D1** 要求：声明必须持久化，插件在 `on_load` / 首次 hook 调用时重新注入一次；
        而且 `declaration` 是 **append-only**（PM 拍板 C），
        新声明会**替换**旧声明（旧代退役进历史）
        ⇒ 只有用存下来的那份，最新一代声明才能送到模型。

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

    async def _f2_fetch_candidates(self, stream_id: str, blocks=None):
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

        ⚠️ T-C06（测试岗 2026-11-22 坐实）：旧实现「拿到非空候选就 return」
        ⇒ 24h 窗口几乎永远非空 ⇒ 96/384 两档从未被用到 ⇒
        回退跨度 >24h 时，更早的对话永远匹配不上 ⇒ **漏遮蔽**。
        ⇒ 规格口径：传入 blocks 时按「未匹配块数」判据迭代扩窗 ——
          24h 有候选但仍有块对不上 ⇒ 扩到 96h，取**匹配块数最多**的一档；
          全部对齐则提前返回。
        ⚠️ R-19（复测 2026-11-22，P2）：**不再设 384h 第三档** ——
          只要存在永远失配的块（噪声/宿主不保留的形状），旧三档会让
          **每一轮** replyer 都跑满 24/96/384 三次取数（实测 11ms，库大了
          会逼近 hook 6000ms 超时上限）。96h 已覆盖常规回退跨度（±100 年
          上限约束的是 offset，不是取数窗；回退跨度 >96h 属罕见场景，
          接受少遮并靠「保留不删 + 告警」兜底）。
        未传 blocks（兼容旧调用形态）保持旧口径：拿非空候选即返回。
        """
        if mk_matcher is None:
            return []
        best_cands = None
        best_matched = -1
        last_err = None
        # R-19：档位 (hours, limit) —— 384h 档已移除（见上）
        for hours, limit in ((24, 300), (96, 600)):
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
            if not cands:
                continue
            if blocks is None:
                return cands  # 兼容旧口径：拿非空候选即返回
            matched, report = mk_matcher.match_blocks(blocks, cands)
            if report.matched > best_matched:
                best_matched = report.matched
                best_cands = cands
            if report.matched == report.total:
                return cands  # 全部对齐，无需扩窗
        if best_cands is not None:
            return best_cands
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
                scrub_schedule=state.has_hidden_window(),
                block_memory_injections=state.has_hidden_window(),
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
        if res.schedule_scrubbed:
            logger.info(
                "[MaiKnock] 已从宿主待办上下文隐藏 %d 条创建于遮蔽期内的日程（T-F04）",
                res.schedule_scrubbed,
            )
        if res.memory_blocked:
            logger.info(
                "[MaiKnock] 已整段屏蔽 %d 条宿主记忆类注入（启发式记忆/人物画像/图片记忆，R-18）",
                res.memory_blocked,
            )
        for w in res.warnings:
            logger.warning("[MaiKnock] planner 侧：%s", w)

        # ---------- R-22（PM 2026-11-22 拍板）：摘除记忆检索类工具 ----------
        # 有隐藏窗口时，从工具目录剔除 query_memory / query_person_profile ——
        # 模型主动检索会把记忆库里的被藏时段内容经工具输出带回 prompt（第四条通道）。
        # 工具调不到 ⇒ 没有输出 ⇒ 没有泄漏。hook 每轮都触发 ⇒ 每轮目录都被过滤。
        filtered_tools = None
        if state.has_hidden_window() and isinstance(kwargs.get("tool_definitions"), list):
            filtered_tools, tools_removed = mk_planner.filter_memory_tools(kwargs["tool_definitions"])
            if tools_removed:
                logger.info(
                    "[MaiKnock] 已从工具目录摘除 %d 个记忆检索工具（query_memory/query_person_profile，R-22）",
                    tools_removed,
                )

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
        # 剧情提示词（0.2.3）：/mk add|back|set 附带的叙事，每轮注入（与声明同理）
        narr = (getattr(state, "narrative", "") or "").strip()
        if narr:
            final_items = mk_decl.inject_declaration(
                final_items,
                narr,
                item_id="maiknock-timeline-narrative",
                timestamp=self._f2_now_iso(),
            )

        new_kwargs = dict(kwargs)
        new_kwargs["items"] = final_items
        if filtered_tools is not None:
            new_kwargs["tool_definitions"] = filtered_tools
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

        # ---------- R-22b（复测更正报告 2026-11-22 15:00）----------
        # `query_memory` 的检索结果会被宿主构造成以
        # 【长期记忆检索结果-内部参考】开头的独立 item 透传给 replyer
        # （query_memory.py:146/335）⇒ 有隐藏窗口时整条剔除。
        # ⚠️ startswith 判定（与 planner 侧同一纪律），防误删正文提及标记的消息。
        # ⚠️ 必须放在 blob 查找之前 —— 没有 blob 时也会提前 return，不能漏拦。
        new_items = None
        if state.has_hidden_window():
            kept_items = []
            ref_dropped = 0
            for it in items:
                if isinstance(it, dict):
                    t = _mk_item_text(it)
                    if t and t.lstrip().startswith(
                        mk_planner.REPLYER_MEMORY_REFERENCE_MARKER
                    ):
                        ref_dropped += 1
                        continue
                kept_items.append(it)
            if ref_dropped:
                items = kept_items
                new_items = kept_items
                logger.info(
                    "[MaiKnock] 已屏蔽 %d 条长期记忆检索结果透传（R-22b）", ref_dropped
                )

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
        def _continue_with_filtered() -> Dict[str, Any]:
            """R-22b 已剔除记忆透传时的提前返回：必须把过滤后的 items 回传。"""
            if new_items is not None:
                return {"action": "continue", "modified_kwargs": {**dict(kwargs), "items": new_items}}
            return {"action": "continue"}

        if target_idx < 0:
            return _continue_with_filtered()

        # T-C06：先切块，再按「未匹配块数」判据迭代扩窗取候选集
        blocks = mk_matcher.split_blob(text) if mk_matcher else []
        cands = await self._f2_fetch_candidates(stream_id, blocks or None)
        if not cands:
            return _continue_with_filtered()

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
            return _continue_with_filtered()

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
        # 剧情提示词（0.2.3）：同声明，replyer 侧也必须注入
        narr = (getattr(state, "narrative", "") or "").strip()
        if narr:
            new_items = mk_decl.inject_declaration(
                new_items,
                narr,
                item_id="maiknock-timeline-narrative",
                timestamp=self._f2_now_iso(),
            )

        new_kwargs = dict(kwargs)
        new_kwargs["items"] = new_items
        return {"action": "continue", "modified_kwargs": new_kwargs}

    # ------------------------------------------------------------------
    # F4 小说体风格守卫（规格 §3.1 / §3.3 / §3.7）
    #
    # 三段式契约：【】内心 / （）环境动作 / 裸文本口头语言。
    # 两个 hook（F2 的两个不动）：
    #   * before_model_request —— 注入契约（本 handler 名必须按字母序排在
    #     `maiknock_replier_timeline` 之后：r < s；改名即翻车，见模块头 0.3.0 说明）
    #   * after_response —— 缺块打回重生（插件侧上限 1 次）/ 格式问题代码改写
    # ⚠️ 返回协议一律 {"action": "continue", "modified_kwargs": {...}}——
    #    裸 {"response": ...} / {"retry": ...} 会被宿主静默丢弃（hook_dispatcher.py:548/619-621）。
    # ------------------------------------------------------------------

    async def _f4_gate(self, session_id: str) -> Optional[Dict[str, Any]]:
        """F4 闸门：模块可用 + 配置启用 ⇒ 返回 [style_guard] 配置段；否则 None。

        0.3.3 起**对所有聊天全量生效**（PM 拍板：受管清单是「麦麦聊天室」插件的事，
        麦麦敲门不该管生效范围）——不再按 session 过滤。
        ⚠️ 配置只走 _plugin_config()（内部 config.get_plugin）——禁用 ctx.config.get，
        那读的是宿主全局主配置（F1 踩过的坑，plugin.py:778-797）。
        """
        if mk_style_guard is None:
            return None
        try:
            cfg = await self._plugin_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock][F4] 配置读取失败，本轮守卫不生效：%r", exc)
            return None
        sg = cfg.get("style_guard") if isinstance(cfg, dict) else None
        if not isinstance(sg, dict) or not bool(sg.get("enabled", True)):
            return None
        # F-12（0.2.3 回归轮坐实·P1）：宿主段可能陈旧且永不刷新（watchfiles 丢
        # 变更通知时 on_config_update 不触发）⇒ 开关真值一律以原始 config.toml
        # 为准合成：原文新键 > 旧版 blocks 接管 > 宿主段兜底。mtime 没变只付
        # 一次 stat 的开销，宿主缓存陈旧与否不再影响开关
        return self._effective_style_guard(sg)

    def _raw_style_guard(self) -> Optional[Dict[str, Any]]:
        """读原始 config.toml 的 [style_guard] 段（mtime 指纹缓存，F-12 真值来源）。

        返回值语义：dict = 文件里的 [style_guard]（可能为空 dict = 段不存在/解析失败）；
        None = config.toml 缺失或不可 stat。读失败会缓存 ``(mtime, {})`` 避免刷屏，
        mtime 一变自然重试。

        ⚠️ 本函数是 F4 开关真值的唯一来源（F-12）——任何读失败都意味着真值路径
        走不通、症状与「宿主缓存陈旧」一模一样，**一律 error 级喊出来**（F-14），
        不允许静默回退宿主段。
        """
        try:
            path = Path(__file__).parent / "config.toml"
            mtime = path.stat().st_mtime
        except OSError:
            self._f4_raw_cache = None
            return None
        cached = self._f4_raw_cache
        if cached is not None and cached[0] == mtime:
            return cached[1]
        if tomllib is None:
            # F-14：解析器不可用（Python < 3.11 且没装 tomli）——真值路径整体
            # 走不通，必须 error 级提示，不允许被 except 链静默吞掉
            logger.error(
                "[MaiKnock] 无可用 TOML 解析器（需 Python 3.11+ 的 tomllib，或 pip 安装 tomli），"
                "F4 开关真值回退宿主段——请尽快补齐解析器"
            )
            self._f4_raw_cache = (mtime, {})
            return {}
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._f4_raw_cache = None
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[MaiKnock] 读取原始 config.toml 失败，F4 开关真值回退宿主段（症状"
                "与宿主缓存陈旧相同，务必排查）：%r",
                exc,
            )
            self._f4_raw_cache = (mtime, {})
            return {}
        sg = raw.get("style_guard")
        if not isinstance(sg, dict):
            sg = {}
        self._f4_raw_cache = (mtime, sg)
        return sg

    def _effective_style_guard(self, host_sg: Dict[str, Any]) -> Dict[str, Any]:
        """合成 F4 开关真值——**原始 config.toml 优先，宿主段兜底**（F-12）。

        优先级（0.2.3 回归轮拍板，同时闭合 F-10 / F-11 / F-12）：
          1. 原文写了新键（内心想法/动作环境）→ 以原文为准（用户亲手写的才是真相；
             缺的那侧按规格缺省 True，不吃陈旧宿主段的 legacy 值）；
          2. 原文只有旧版 [style_guard.blocks] → 用 blocks 旧键接管（F-10 场景）；
          3. 原文读不到 / 没有 [style_guard] 段 → 回退宿主段（尽力而为）。
        """
        raw = self._raw_style_guard()
        if not raw:
            return host_sg
        if "内心想法" in raw or "动作环境" in raw:
            merged = dict(host_sg)
            merged["内心想法"] = bool(raw.get("内心想法", True))
            merged["动作环境"] = bool(raw.get("动作环境", True))
            return merged
        blocks = raw.get("blocks")
        if isinstance(blocks, dict) and (
            "inner_enabled" in blocks or "action_enabled" in blocks
        ):
            overrides: Dict[str, bool] = {}
            if "inner_enabled" in blocks:
                overrides["内心想法"] = bool(blocks["inner_enabled"])
            if "action_enabled" in blocks:
                overrides["动作环境"] = bool(blocks["action_enabled"])
            key = (overrides.get("内心想法"), overrides.get("动作环境"))
            if self._f4_legacy_warn_key != key:
                self._f4_legacy_warn_key = key
                logger.warning(
                    "[MaiKnock] 开关真值取自旧版 [style_guard.blocks]（原始 config.toml）："
                    "内心想法=%s 动作/环境=%s。在 WebUI 配置页保存一次即可彻底迁移成新格式",
                    key[0], key[1],
                )
            return {**host_sg, **overrides}
        return host_sg

    @staticmethod
    def _f4_blocks(sg: Dict[str, Any]) -> "tuple[bool, bool]":
        """取两大块开关（缺省 true，规格 §1.2）。"""
        # F-12 订正：宿主 config.get_plugin 返回的是它读文件后的**缓存段**（保留
        # blocks、缺失新键不填默认值，且可能永久陈旧）⇒ 上面的 legacy 回退对宿主段
        # 也可能真的走到。_f4_gate 传入的 sg 已由 _effective_style_guard 以原始
        # config.toml 为准合成；这里的回退只是最后一道保险
        legacy = sg.get("blocks") if isinstance(sg.get("blocks"), dict) else {}
        return (
            bool(sg.get("内心想法", legacy.get("inner_enabled", True))),
            bool(sg.get("动作环境", legacy.get("action_enabled", True))),
        )

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="maiknock_style_guard",  # ⚠️ 字母序排在 maiknock_replier_timeline 之后，改名即翻车
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def _hook_replier_style_contract(self, **kwargs):
        """F4 请求前：向 items 末尾注入三段式契约（按开关动态生成，注入幂等）。"""
        session_id = str(kwargs.get("session_id") or "")
        sg = await self._f4_gate(session_id)
        if sg is None:
            return {"action": "continue"}
        items = kwargs.get("items")
        if not isinstance(items, list) or not items:
            return {"action": "continue"}
        inner_enabled, action_enabled = self._f4_blocks(sg)
        contract = mk_style_guard.build_contract(inner_enabled, action_enabled)
        new_items = mk_style_guard.inject_contract(items, contract, timestamp=self._f2_now_iso())
        logger.info(
            "[MaiKnock][F4] 契约已注入 session=%s（inner=%s action=%s）",
            session_id[:12] or "（空）", inner_enabled, action_enabled,
        )
        # ⚠️ 必须复制整份 kwargs 再改 items——只回 {"items": ...} 会丢
        #    item_schema_version 等字段（协议校验整包丢弃且只打一条 warning）
        new_kwargs = dict(kwargs)
        new_kwargs["items"] = new_items
        return {"action": "continue", "modified_kwargs": new_kwargs}

    @HookHandler(
        "maisaka.replyer.after_response",
        name="maiknock_style_guard_after",  # ⚠️ 不能与 before 同名：宿主组件表按 full_name 全局唯一，
        #     同名会让 after 覆盖 before 的注册条目 → 契约注入静默失效（2026-11-23 实机踩坑）
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def _hook_replier_style_enforce(self, **kwargs):
        """F4 响应后：①显式内心标记转换 → ②缺块判定（打回重生，上限 1 次）→ ③按开关删块。

        ⚠️ 改写与重生一个回合二选一（宿主先改写再判重生，同时返回语义错乱）；
        ⚠️ retry_count >= max_retries 时不再打回，接受当前回复（尽力保证，非数学保证）。
        """
        session_id = str(kwargs.get("session_id") or "")
        sg = await self._f4_gate(session_id)
        if sg is None:
            return {"action": "continue"}
        response = str(kwargs.get("response") or "")
        if not response.strip():
            return {"action": "continue"}

        fmt = sg.get("format") if isinstance(sg.get("format"), dict) else {}
        markers_raw = fmt.get("inner_markers")
        markers = (
            [str(m) for m in markers_raw if str(m).strip()]
            if isinstance(markers_raw, list) else None
        )
        try:
            max_retries = int(fmt.get("max_retries", 1))
        except (TypeError, ValueError):
            max_retries = 1
        retry_inner = str(fmt.get("retry_reason_inner") or "").strip() or None
        retry_action = str(fmt.get("retry_reason_action") or "").strip() or None
        try:
            retry_count = int(kwargs.get("retry_count") or 0)
        except (TypeError, ValueError):
            retry_count = 0

        inner_enabled, action_enabled = self._f4_blocks(sg)
        # F-09（0.2.3 测试报告）：打回重生的指令按开关状态生成——关掉另一侧时
        # 明确告诉模型「禁写不等于可以省略」，降低二次重生率
        if not retry_inner:
            retry_inner = (
                "刚才的回复漏了【】内心想法——这不能省。请重新输出整条回复："
                "每段先用【】写她此刻真实但没说出口的想法，再说话。"
                + ("同时仍然不要出现任何（）动作/环境描写。" if not action_enabled else "")
            )
        if not retry_action:
            retry_action = (
                "刚才的回复漏了（）动作/环境描写——这不能省。请重新输出整条回复："
                "每段先用（）写贴合当下情绪的肢体动作或环境描写，再说话。"
                + ("注意：禁写内心想法不代表可以省略（），不要连动作一起丢掉。" if not inner_enabled else "")
            )
        result = mk_style_guard.enforce(
            response,
            inner_enabled=inner_enabled,
            action_enabled=action_enabled,
            markers=markers,
            retry_reason_inner=retry_inner,
            retry_reason_action=retry_action,
        )

        # ---- 缺块 → 打回重生（二选一：不产出改写文本）----
        if result.retry_reason:
            if retry_count >= max_retries:
                logger.info(
                    "[MaiKnock][F4] 缺块（%s）但已达插件侧重生上限（retry_count=%s max=%s），放行当前回复",
                    result.matched_rule, retry_count, max_retries,
                )
                # 放行也要过第④步换行规整（被动技能：所有路径都生效）
                settled = mk_style_guard.normalize_paragraph_breaks(
                    response, inner_enabled=inner_enabled, action_enabled=action_enabled)
                if settled != response:
                    return {"action": "continue", "modified_kwargs": {"response": settled}}
                return {"action": "continue"}
            logger.info(
                "[MaiKnock][F4] 缺块打回重生：%s（session=%s retry_count=%s）",
                result.matched_rule, session_id[:12] or "（空）", retry_count,
            )
            return {"action": "continue", "modified_kwargs": {
                "retry": True,
                "retry_reason": result.retry_reason,
                "matched_regex": result.matched_rule,
            }}

        # ---- ①②③ 格式问题 → 代码改写（免费、确定）----
        final = result.final_text if result.final_text is not None else response
        # ---- ④ 段落间空行（被动技能，所有字数档位生效）----
        settled = mk_style_guard.normalize_paragraph_breaks(
            final, inner_enabled=inner_enabled, action_enabled=action_enabled)

        if settled != response:
            parts = []
            if result.converted:
                parts.append(f"内心标记转换 {result.converted} 处")
            if result.deleted_inner:
                parts.append(f"删内心块 {result.deleted_inner} 处")
            if result.deleted_action:
                parts.append(f"删动作块 {result.deleted_action} 处")
            if result.empty_body_protected:
                parts.append("空正文保护生效（保留原文）")
            if settled != final:
                parts.append("段落空行规整")
            logger.info("[MaiKnock][F4] 已改写回复：%s", "；".join(parts) or "（微调）")
            return {"action": "continue", "modified_kwargs": {"response": settled}}
        return {"action": "continue"}

    # ------------------------------------------------------------------
    # F5 功能 A 回复字数配置（软约束；无硬截断）
    #
    # 只挂一个 hook：before_model_request 注入字数契约到 items 末尾。
    # 契约文案自带「约等于、自然表达优先」，没有代码层截断——用户拍板
    # 「字数上限是尽量约等于，不是死规矩」，硬切会切碎小说体语义片段。
    #
    # ⚠️ 返回协议一律 {"action":"continue","modified_kwargs":{...}}；
    #    before hook 必须复制**整份** kwargs 再改 items（只回 {"items":...} 会丢
    #    item_schema_version 等字段，宿主整包丢弃）。
    # ⚠️ F4/F5 两个契约的 item_id 不同（style-guard-contract / reply-length-contract），
    #    各自幂等注入、互不覆盖。
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="maiknock_reply_length",  # 字母序：r < s，排在 F4 的 maiknock_style_guard 之前；无依赖
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def _hook_reply_length_contract(self, **kwargs):
        """F5 请求前：注入字数范围契约（软约束），让模型第一遍就写在范围内。"""
        if mk_reply_length is None:
            return {"action": "continue"}
        try:
            cfg = await self._plugin_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MaiKnock][F5] 配置读取失败，本轮字数约束不生效：%r", exc)
            return {"action": "continue"}
        rl = cfg.get("reply_length") if isinstance(cfg, dict) else None
        if not isinstance(rl, dict):
            rl = {}
        # 手改 config.toml 可能留下旧版两个数字键（0.3.1）——兼容读取，但以档位为准
        preset = str(rl.get("回复篇幅") or "")
        items = kwargs.get("items")
        if not isinstance(items, list) or not items:
            return {"action": "continue"}

        # 「自由」档：不注入任何字数契约，并清掉历史里可能残留的旧契约——
        # 否则从别的档切过来时，旧字数要求还挂在上下文里，等于没切（F5 切档一致性）
        if mk_reply_length.is_free(preset):
            cleaned = mk_reply_length.strip_contract(items)
            if len(cleaned) == len(items):
                return {"action": "continue"}
            new_kwargs = dict(kwargs)
            new_kwargs["items"] = cleaned
            logger.info("[MaiKnock][F5] 自由档：已清除 %s 条历史字数契约", len(items) - len(cleaned))
            return {"action": "continue", "modified_kwargs": new_kwargs}

        lo, hi = mk_reply_length.limits_from_preset(preset)
        contract = mk_reply_length.build_contract(lo, hi)
        new_items = mk_reply_length.inject_contract(items, contract, timestamp=self._f2_now_iso())
        new_kwargs = dict(kwargs)
        new_kwargs["items"] = new_items
        logger.info("[MaiKnock][F5] 字数契约已注入：档位=%s，要求 %s-%s 字", preset, lo, hi)
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
