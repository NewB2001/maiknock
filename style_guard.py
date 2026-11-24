# -*- coding: utf-8 -*-
"""MaiKnock F4 小说体风格守卫 —— 纯逻辑模块（无 IO、无宿主依赖，可离线单测）。

三段式契约：`【】`内心／`（）`环境动作／裸文本口头语言（规格 §1.1）。

**执行顺序写死（规格 §3.3，顺序错会每轮误判缺块 → 每条消息重生一次）**：
  ① 显式标记转换：圆括号内含 `inner_markers` 前缀 → 括号转 `【】`，内容一字不改
  ② 缺块判定：开关打开且转换后仍缺块 → 返回重生原因（插件侧再按 retry_count 限次）
  ③ 按开关删块：开关关闭 → 代码删除；删完为空 → 空正文保护、放弃删除

**只认全角 `（）` 和 `【】`，半角 `()` 一律不处理**（规格 §2.3 / 边界 5）。

**幂等**：同一文本处理两次结果一致；「显式标记转换」是独立幂等步骤、单独单测
——它是本次上线的迁移主路径：模型现状风格就是 `（内心：…）`。

会话识别（规格 §2.4 / §3.7）：session_id 永远是 md5（宿主 `utils_session.py:8-42`）：
  - 私聊 `md5(f"{platform}_account:{acct}_{user_id}_private")`
  - 群聊 `md5(f"{platform}_{group_id}")`
⇒ **禁止**拿 `webui_virtual_group_` 前缀直接匹配 session_id（物理不可能）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 契约注入 item 的类型（与 declaration.py 同构：UserMessageItem 最容易被模型当上下文读）
CONTRACT_ITEM_TYPE = "UserMessageItem"

#: 契约 item 的固定 id（幂等替换的判定键）
CONTRACT_ITEM_ID = "maiknock-style-guard-contract"

#: 重生原因的规则名（进宿主日志，方便查「为什么这条被重生」）
RULE_INNER_MISSING = "style_guard.inner_missing"
RULE_ACTION_MISSING = "style_guard.action_missing"

# 四种开关组合的契约文案。CONTRACT_BOTH 保持规格 §3.1 逐字；「关一侧」的两种
# 由 PM 拍板重写（0.2.3 测试报告 F-09：旧文案只说「不要写 X」，模型会连必写的
# 块一起省掉——关内心批次重生率 62.5%）。新写法：①先立「必须写」的块；
# ②再禁另一侧；③点明两者泾渭分明，禁写不影响必写。
CONTRACT_BOTH = (
    "每说一小段话，先用【】写她此刻没说出口的内心想法，再用（）写对应的环境/肢体动作，"
    "然后说出口。三者都要有，随对话自然重复出现，不要把同类内容全堆在开头或结尾。"
    "每一小段（从【开始到下一段【之前）之间空一行，即连续两个换行。"
)
CONTRACT_INNER_ONLY = (
    "每说一小段话，都必须先用【】写她此刻没说出口的内心想法，然后说出口——"
    "【】块不可缺少，一段都不能少。同时严格禁止动作/环境描写：全文不要出现任何（），"
    "不要写肢体动作、表情、环境。这两条泾渭分明：禁写动作不影响【】必须写。"
    "每一小段（从【开始到下一段【之前）之间空一行，即连续两个换行。"
)
CONTRACT_ACTION_ONLY = (
    "每说一小段话，都必须先用（）写对应的环境/肢体动作，然后说出口——"
    "（）块不可缺少，一段都不能少。同时严格禁止内心想法：全文不要出现任何【】，"
    "不要写「心想」「内心」这类描写。这两条泾渭分明：禁写内心不代表可以省略（）。"
    "每一小段（从（开始到下一段（之前）之间空一行，即连续两个换行。"
)
CONTRACT_NONE = "只输出角色说出口的话，不要任何括号描写。每句话之间空一行，即连续两个换行。"

#: 全角括号块（判定「该块是否存在」用；【[^】]*】不跨块，与宿主清括号正则无冲突：
#: 宿主只认 `(`/`[`/`（`（utils.py:451-456），中文【】不会被宿主清理）
INNER_BLOCK_RE = re.compile(r"【[^】]*】")
ACTION_BLOCK_RE = re.compile(r"（[^）]*）")

#: 默认显式内心标记（规格 §3.4）
DEFAULT_INNER_MARKERS: List[str] = ["内心：", "内心:", "心声：", "心想：", "暗想：", "OS："]


# ---------------------------------------------------------------------------
# 契约生成（请求前）
# ---------------------------------------------------------------------------

def build_contract(inner_enabled: bool, action_enabled: bool) -> str:
    """按开关动态生成契约文本，只提打开的块（四种组合各一段，规格 §3.1）。"""
    if inner_enabled and action_enabled:
        return CONTRACT_BOTH
    if inner_enabled:
        return CONTRACT_INNER_ONLY
    if action_enabled:
        return CONTRACT_ACTION_ONLY
    return CONTRACT_NONE


# ---------------------------------------------------------------------------
# 块扫描与显式标记转换
# ---------------------------------------------------------------------------

def _marker_patterns(markers: List[str]) -> List[re.Pattern]:
    """每个显式标记编译一条正则：全角圆括号、括号内以标记开头（允许前导空白）。"""
    patterns = []
    for marker in markers or []:
        if not marker:
            continue
        patterns.append(re.compile(r"（\s*" + re.escape(marker) + r"[^）]*）"))
    return patterns


def convert_explicit_inner(text: str, markers: List[str]) -> Tuple[str, int]:
    """步骤①：`（内心：…）` → `【内心：…】`，内容一字不改，只换括号字形。

    幂等：已是 `【】` 的文本不会再被匹配（本函数只认全角 `（）`）。
    返回 (新文本, 转换处数)。
    """
    if not text:
        return text, 0
    converted = text
    count = 0
    for pattern in _marker_patterns(markers):
        def _swap(match: re.Match) -> str:
            return "【" + match.group(0)[1:-1] + "】"

        converted, n = pattern.subn(_swap, converted)
        count += n
    return converted, count


def _cleanup(text: str) -> str:
    """删块后的轻量清理：行尾空白、三连以上空行、首尾空白。幂等。"""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# F5 功能 B 段落间空行（插件被动技能，所有字数档位生效）
# ---------------------------------------------------------------------------

#: 句末标点（全关档的段起点锚点）；连续标点（……）算一个句尾
SENTENCE_END_CHARS = "。！？…"


def normalize_paragraph_breaks(
    text: str,
    *,
    inner_enabled: bool = True,
    action_enabled: bool = True,
) -> str:
    """把正文规整成「段间空一行」，幂等。纯字符串规则，不依赖语义理解。

    段起点锚点按开关组合（规格 §2.3）：
      - inner_enabled  → 每个 `【` 的开头
      - action_enabled → 每个 `（` 的开头（仅当 inner 关闭）
      - 两者都关       → 句末标点 `。！？…` 之后（连续标点算一个句尾）

    处理：① 行尾空白去掉、连续换行压成单换行；② 按锚点切段；③ 段间用 `\\n\\n` 拼接。
    已是整齐空行的文本处理结果不变（幂等）；空文本/纯括号块放行。
    """
    if not text or not text.strip():
        return text
    norm = re.sub(r"[ \t]+\n", "\n", text)
    norm = re.sub(r"\n{2,}", "\n", norm)
    norm = norm.strip()
    if not norm:
        return text

    if inner_enabled:
        parts = re.split(r"(?=【)", norm)
    elif action_enabled:
        parts = re.split(r"(?=（)", norm)
    else:
        raw: List[str] = []
        for ch in norm:
            raw.append(ch)
            if ch in SENTENCE_END_CHARS:
                raw.append("\x00")
        chunks = "".join(raw).split("\x00")
        parts = []
        pending = ""
        for chunk in chunks:
            if not chunk.strip():
                continue
            # 连续标点（……）或纯空白 → 合到上一段，不单开
            if pending and not chunk.strip(SENTENCE_END_CHARS + " \t\n"):
                pending += chunk
            else:
                if pending:
                    parts.append(pending)
                pending = chunk
        if pending:
            parts.append(pending)

    cleaned = [p.strip() for p in parts if p.strip()]
    if not cleaned:
        return text
    return "\n\n".join(cleaned)


# ---------------------------------------------------------------------------
# 开关执行（响应后）—— 顺序固定：①转换 → ②缺块判定 → ③删块
# ---------------------------------------------------------------------------

@dataclass
class EnforceResult:
    """enforce() 的结果。

    - `retry_reason` 非 None ⇒ 打回重生（插件侧按 retry_count 限次）；
      **此时不产出改写文本**（改写与重生二选一，规格 §2.2）。
    - `final_text` 非 None 且 ≠ 原文 ⇒ 有改写，插件返回 `{"response": …}`。
    - 两者都空 ⇒ 放行原样。
    """

    final_text: Optional[str] = None
    retry_reason: Optional[str] = None
    matched_rule: Optional[str] = None
    converted: int = 0
    deleted_inner: int = 0
    deleted_action: int = 0
    empty_body_protected: bool = False


def enforce(
    text: str,
    *,
    inner_enabled: bool,
    action_enabled: bool,
    markers: Optional[List[str]] = None,
    retry_reason_inner: str = "本次回复缺少内心块，请用【】写一句她此刻真实但没说出口的想法。",
    retry_reason_action: str = "本次回复缺少动作块，请用（）写一处贴合当下情绪的肢体动作或环境描写。",
) -> EnforceResult:
    """对一条回复执行三步守卫（§3.3）。纯函数、幂等。

    注意：本函数**不管 retry_count**——是否真打回由插件层按 `retry_count >= max_retries`
    决定（`retry_reason` 只是「按规则该不该重生」的判定）。
    """
    if not text or not text.strip():
        return EnforceResult()
    markers = markers if markers is not None else DEFAULT_INNER_MARKERS

    # ---- 步骤①：显式标记转换（迁移主路径：现状风格全是（内心：…））----
    converted, n_conv = convert_explicit_inner(text, markers)

    # ---- 步骤②：缺块判定（只看打开的开关；判定按整条回复）----
    has_inner = bool(INNER_BLOCK_RE.search(converted))
    has_action = bool(ACTION_BLOCK_RE.search(converted))
    if inner_enabled and not has_inner:
        return EnforceResult(retry_reason=retry_reason_inner, matched_rule=RULE_INNER_MISSING, converted=n_conv)
    if action_enabled and not has_action:
        return EnforceResult(retry_reason=retry_reason_action, matched_rule=RULE_ACTION_MISSING, converted=n_conv)

    # ---- 步骤③：按开关删块（关 → 全删；开 → 保留）----
    final = converted
    n_del_inner = 0
    n_del_action = 0
    if not inner_enabled:
        final, n_del_inner = INNER_BLOCK_RE.subn("", final)
    if not action_enabled:
        final, n_del_action = ACTION_BLOCK_RE.subn("", final)

    if n_del_inner or n_del_action:
        final = _cleanup(final)
        # 空正文保护（边界 6）：删完为空 → 放弃删除，保留删前文本，不发空回复
        if not final:
            return EnforceResult(
                final_text=converted,
                converted=n_conv,
                deleted_inner=n_del_inner,
                deleted_action=n_del_action,
                empty_body_protected=True,
            )

    if n_conv == 0 and n_del_inner == 0 and n_del_action == 0:
        return EnforceResult()  # 原样放行
    return EnforceResult(
        final_text=final,
        converted=n_conv,
        deleted_inner=n_del_inner,
        deleted_action=n_del_action,
    )


# ---------------------------------------------------------------------------
# 契约 item 注入（请求前；结构照 declaration.py 的 build_declaration_item）
# ---------------------------------------------------------------------------

def build_contract_item(text: str, *, item_id: str = CONTRACT_ITEM_ID, timestamp: str = "") -> Dict[str, Any]:
    """把契约文本包装成可注入的 item（与宿主 item 协议同构，过 context_protocol 校验）。"""
    return {
        "item_type": CONTRACT_ITEM_TYPE,
        "meta": {
            "item_id": item_id,
            "logical_turn_id": None,
            "timestamp": timestamp,
        },
        "parts": [
            {"type": "text", "text": text},
        ],
    }


def _is_contract_item(item: Any) -> bool:
    """按 meta.item_id 自识别契约 item（幂等替换的判定键）。"""
    if not isinstance(item, dict):
        return False
    meta = item.get("meta")
    return isinstance(meta, dict) and meta.get("item_id") == CONTRACT_ITEM_ID


def inject_contract(
    items: List[Dict[str, Any]],
    contract_text: str,
    *,
    item_id: str = CONTRACT_ITEM_ID,
    timestamp: str = "",
) -> List[Dict[str, Any]]:
    """把契约注入到 items 末尾；先移除已有契约 item（幂等），再追加。

    ⚠️ 本 handler 排在 F2（`maiknock_replier_timeline`）**之后**执行
    （handler 名字母序，hook_dispatcher.py:358-364），F2 的 strip_declarations
    不会碰本 item（两者判定键不同：魔数前缀 vs meta.item_id）。
    """
    base = [it for it in items if not _is_contract_item(it)]
    base.append(build_contract_item(contract_text, item_id=item_id, timestamp=timestamp))
    return base
