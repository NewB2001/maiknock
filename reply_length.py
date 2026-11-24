# -*- coding: utf-8 -*-
"""MaiKnock F5 消息分段管理 —— 功能 A「回复字数配置」纯逻辑模块（无 IO、无宿主依赖，可离线单测）。

**本期只做软约束**：把字数范围写成一条中文契约，请求前注入 items 末尾，
让模型第一遍就按范围写。没有硬截断（用户拍板：字数上限是「尽量约等于」不是「死规矩」）。

字数口径（固定，不暴露给用户）：
  - 只算正文裸文本，`【】`内心 / `（）`动作不计入——否则三段式光括号就超了；
  - 配置窗口是**一个中文下拉档位**（简短/适中/详细/自由），前三个档位自带一对字数范围，
    「自由」= 不注入任何字数契约（麦麦完全自由发挥）；
    档位从源头防呆——用户选不到「最大 < 最小」，代码层不做数值输入校验；
  - **字数下限恒为 60**（用户铁律：不少于 60），2026-11-24 曾误设为 40，已纠正；
  - 手改 config.toml 写错档位名时回落默认档（简短），不崩。

幂等：注入前先删同 item_id 的旧契约 item，再追加；同一段文本处理两次结果一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

#: 字数契约 item 的类型（与 style_guard / declaration 同构，UserMessageItem 最容易被模型当上下文读）
CONTRACT_ITEM_TYPE = "UserMessageItem"

#: 契约 item 的固定 id（幂等替换的判定键）——**必须与 F4 的 maiknock-style-guard-contract 不同**
CONTRACT_ITEM_ID = "maiknock-reply-length-contract"

#: 自由档：不注入任何字数契约，麦麦完全自由发挥（配置窗口的第四个选项）
FREE_PRESET = "自由"

#: 档位 → 字数范围。配置窗口只有**一个**中文下拉，选项即 key（自带范围说明，一眼看懂）。
#: 档位从源头防呆：用户只能选预设组合，选不到「最大 < 最小」。
#: 下限全部 >= 60（用户铁律），上限时高时低成阶梯。
PRESETS: Dict[str, Tuple[int, int]] = {
    "简短（60-200字）": (60, 200),
    "适中（100-400字）": (100, 400),
    "详细（150-600字）": (150, 600),
    FREE_PRESET: None,  # type: ignore[dict-item] —— 自由档无范围，单独处理
}

#: 默认档位（用户拍板：简短）
DEFAULT_PRESET = "简短（60-200字）"

#: 兜底规整：手改 config.toml 时防御——min 恒 >= 60，max 压到 2000 内，min>max 互换
MIN_FLOOR = 60
MAX_CEILING = 2000


def is_free(preset: str) -> bool:
    """档位是否为「自由」（不注入字数契约）。"""
    return str(preset) == FREE_PRESET


def limits_from_preset(preset: str) -> Tuple[int, int]:
    """档位名 → 规整后的 (min, max)。未知档位回落默认档（简短）；自由档回落默认（调用方应先查 is_free）。"""
    got = PRESETS.get(str(preset))
    if got is None:  # 自由档或未知档 —— 都回落默认范围
        got = PRESETS[DEFAULT_PRESET]
    return normalize_limits(got[0], got[1])


def normalize_limits(min_chars: int, max_chars: int) -> Tuple[int, int]:
    """把一对字数规整成可用范围（仅作手改 config.toml 时的兜底，UI 走档位不会触发）。

    规则（均幂等）：
      1. min < 60 → 抬到 60（用户铁律：字数至少 60）；
      2. max > 2000 → 压到 2000（防天文数字）；
      3. min > max → 互换（保证文案自洽）。
    """
    lo = int(min_chars)
    hi = int(max_chars)
    lo = max(lo, MIN_FLOOR)
    hi = min(hi, MAX_CEILING)
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def build_contract(min_chars: int, max_chars: int) -> str:
    """按规整后的范围生成中文白话契约文案。

    措辞要点（用户要求「约等于」而非「=60」）：
      - 用「尽量」「大致」表达软性，不是硬指标；
      - 明确「括号内容不算字数」，避免模型把【】（）也算进去压缩正文；
      - 首尾加「自然表达优先」，防止为凑字数而啰嗦。
    """
    lo, hi = normalize_limits(min_chars, max_chars)
    return (
        f"这条回复的篇幅尽量控制在 {lo} 到 {hi} 字之间（大致范围，不是硬性数字）。"
        "字数只算说出口的话，【】里的内心想法和（）里的动作环境描写不算在内。"
        "自然表达优先：话没说完不要为了省字硬停，也别为凑字数啰嗦。"
    )


def build_contract_item(
    contract_text: str,
    *,
    item_id: str = CONTRACT_ITEM_ID,
    timestamp: str = "",
) -> Dict[str, Any]:
    """把契约文本包装成可注入的 item（与宿主 item 协议同构，过 context_protocol 校验）。"""
    return {
        "item_type": CONTRACT_ITEM_TYPE,
        "meta": {
            "item_id": item_id,
            "logical_turn_id": None,
            "timestamp": timestamp,
        },
        "parts": [
            {"type": "text", "text": contract_text},
        ],
    }


def _is_contract_item(item: Any) -> bool:
    """按 meta.item_id 自识别字数契约 item（幂等替换的判定键）。"""
    if not isinstance(item, dict):
        return False
    meta = item.get("meta")
    return isinstance(meta, dict) and meta.get("item_id") == CONTRACT_ITEM_ID


def strip_contract(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """移除 items 里所有 F5 字数契约 item（自由档用：清残留，避免切档后旧约束还挂着）。"""
    return [it for it in items if not _is_contract_item(it)]


def inject_contract(
    items: List[Dict[str, Any]],
    contract_text: str,
    *,
    timestamp: str = "",
) -> List[Dict[str, Any]]:
    """把字数契约注入到 items 末尾；先移除已有契约 item（幂等），再追加。

    ⚠️ 与 F4 的契约各自独立：两者判定键不同（item_id 不同），互不覆盖。
    """
    base = strip_contract(items)
    base.append(build_contract_item(contract_text, timestamp=timestamp))
    return base
