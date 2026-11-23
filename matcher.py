# -*- coding: utf-8 -*-
"""MaiKnock 匹配层 —— 把 replyer 侧的对话块对齐到真实消息。

**为什么需要它（R6-30 / R6-31 / P21 / P22）**：
1.4.8 上 replyer 侧历史恒为**一整块 blob**，块首只有 `[HH:MM:SS]`（**没有日期**）。
遮蔽判定要的是**真实 epoch**（`is_hidden` 在真实轴上做），补日期也要完整时间戳
⇒ 必须先把「块」对齐到「真实消息」。

**匹配键（三元组，缺一不可）**：
1. **时分秒相等** —— blob 块首 `[HH:MM:SS]` vs 候选消息的 `HH:MM:SS`
2. **说话人相等** —— blob 里渲染成 `{bot}(你)` / 用户名；`get_recent` 里是真实昵称
3. **归一化后整段正文完全相等** —— ⚠️ **不用固定 N**（实测 N=8 零区分力：
   `【MaiKnoc`×11、`/mk diag`×3、麦麦的 `鸣人君……` 前 5 字全同）

**消歧顺序**：① 顺序约束（块序 = 消息序，单调不回退）② 整段正文相等 ③ LCP ≥ 12（最后手段）

**⚠️ 两条硬前提**：
- **两边比的不是同一段文本** ⇒ 必须先剥 `SPEAKER_PREFIX_PATTERN` 前缀
  （宿主 `src/maisaka/context/message_adapter.py:20-23`）。
  不剥 ⇒ 「正文相等」永远不成立 ⇒ 表现为**全部匹配不上**（少遮、不炸、很难查）。
- **匹配不上 → 保留不删 + 计数告警**（宁可少遮不可错删）。

**设计约束**：无 IO、无宿主依赖 ⇒ 可离线单测。
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 展示用消息 ID 的口径（**同构复刻**宿主
#: `src/maisaka/context/message_id_alias.py:13-26`）。
#: ⚠️ 必须是**同一口径**，否则 R6-17 第三种载体的 msg_id 反查全部落空。
#: 规则：长度 ≤ 12 原样；否则 `m` + sha1 的 base32 前 6 位（小写）。
MAX_PLAIN_MESSAGE_ID_LENGTH = 12
ALIAS_PREFIX = "m"
ALIAS_HASH_LENGTH = 6


def to_display_message_id(message_id: Any) -> str:
    """展示给模型的消息 ID（**同构复刻**宿主 `to_display_message_id`）。

    ⚠️ 这是**纯算法**（sha1 + base32），不 import 宿主模块 —— 避免上游改名的耦合，
    同时保证与 `format_speaker_content` 产出的 `[msg_id:xxx]` 完全同一口径。
    """
    normalized = str(message_id or "").strip()
    if len(normalized) <= MAX_PLAIN_MESSAGE_ID_LENGTH:
        return normalized
    digest = hashlib.sha1(normalized.encode("utf-8")).digest()
    return ALIAS_PREFIX + base64.b32encode(digest).decode("ascii").lower()[:ALIAS_HASH_LENGTH]


#: blob 块起点（行首锚定）。⚠️「22/22 零误伤」是**经验事实不是结构保证** ——
#: 正文或 reply_style 里若出现**行首** `[HH:MM:SS]` 会多切一块 ⇒ 兜底必须留着。
BLOCK_START_RE = re.compile(r"(?m)^\[(\d{2}:\d{2}:\d{2})\]")

#: 宿主接在**最后一块**正文后面的尾部提示的公共前缀
#: （`creative_language.py:90-92` 三个变体共用）。
#: ⚠️ T-B08：末块正文被它拖长 ⇒ 必然失配 ⇒ 末块比对前按它截断。
TRAILING_HINT_MARKER = "你正在和对方私聊"

#: 宿主说话人前缀（**复刻** `message_adapter.py:20-23`，不 import 宿主模块，避免上游改名的耦合）。
#: 格式：`HH:MM:SS[msg_id:xxx][说话人]正文`
SPEAKER_PREFIX_RE = re.compile(
    r"^(?:(?P<timestamp>\d{2}:\d{2}:\d{2}))?"
    r"(?:\[msg_id:(?P<message_id>[^\]]+)\])?"
    r"\[(?P<speaker>[^\]]+)\](?P<content>.*)$",
    re.DOTALL,
)

#: blob 块首格式：`[HH:MM:SS] 说话人说：正文`
BLOCK_LINE_RE = re.compile(
    # ⚠️ `speaker` **不能设长度上限**（原 `{0,40}?`）——
    #    回复消息的说话人字段会带 `[回复了…]` 引用块（200+ 字符），
    #    超长 ⇒ 整条正则失配 ⇒ speaker_raw="" ⇒ 匹配不上 ⇒ **漏遮蔽**。
    #    （2026-11-22 01:05 离线报告发现：未匹配块里 8/12 是这个原因）
    # ⇒ 用 `[^\n]*?` 非贪婪到**最后一个** `说：` 由外层 rpartition 保证；
    #    这里只要能过最长行即可，`normalize_speaker()` 会剥掉引用块。
    # ⚠️ 引用块跨行 ⇒ speaker 里含 `\n`，不能用 `[^\n]`。
    #    用 `[\s\S]*?` 非贪婪 + **1000 字符上限**（防御畸形文本）；
    #    chunk 本身已被下一个块起点截断 ⇒ 不会越块抓到别人的「说：」。
    r"^\[(?P<time>\d{2}:\d{2}:\d{2})\]\s*(?P<speaker>[\s\S]{0,1000}?)说[：:]\s*",
)

#: LCP 兜底阈值（要越过「鸣人君……」这个 5 字通用开头）
LCP_MIN = 12


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlobBlock:
    """blob 里的一个对话块。"""

    index: int
    #: 块起点在 blob 全文中的字符偏移
    start: int
    #: 块起点在 blob 全文中的字符偏移（结束 = 下一个块起点；末块为 None）
    end: Optional[int]
    #: 块首时间戳 `HH:MM:SS`
    hhmmss: str
    #: 块首的说话人（原始文本，未归一化）
    speaker_raw: str
    #: 块正文（不含块首）
    content: str
    #: 块首那一行的完整文本
    head_line: str

    @property
    def is_last(self) -> bool:
        return self.end is None


@dataclass(frozen=True)
class Candidate:
    """一条真实消息（来自 `get_recent`）。"""

    #: 真实 epoch（秒）
    ts: float
    #: `HH:MM:SS`
    hhmmss: str
    #: 说话人（原始）
    speaker_raw: str
    #: 归一化后的正文
    content: str
    #: 原始记录（调试用）
    raw: Any = None
    #: 消息 ID（`get_recent` 返回的 `message_id`）——
    #: R6-17 第三种载体 `HH:MM:SS[msg_id:…][说话人]正文` 靠它反查真实时刻。
    #: ⚠️ 宿主用 `to_display_message_id` 把内部 id 转成展示用的短 id
    #: （`src/maisaka/context/message_id_alias.py`）⇒ 两边必须同一口径。
    message_id: str = ""
    #: **展示用**的短 id（`to_display_message_id(message_id)`）——
    #: 这才是 `[msg_id:xxx]` 里出现的那个。
    display_id: str = ""


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def normalize_content(text: str, strip_prefix: bool = True) -> str:
    """正文归一化 —— **两侧必须用同一个函数**，否则永远匹配不上。

    - **剥说话人前缀**（R6-30）—— ⚠️ **只对 `get_recent` 侧做**！
      blob 侧的正文**本来就已经是剥掉前缀的**（宿主 `_render_reply_history_line`
      用 `parse_speaker_content` 处理过）⇒ 再剥一次会出事。
    - 去首尾空白
    - 连续空白折叠成一个空格
    - ⚠️ **表情包占位原样保留** —— 实测它正是区分那 2 对同秒块的关键信息
      （`[表情包: 委屈,...]` vs 一条正文）

    ⚠️ **表情包为什么必须保留**：审计第 10 轮实测那 2 对同秒块
    （`16:30:07`×2、`18:08:14`×2）分别是「一条表情包 + 一条正文」。
    若把 `[表情包: ...]` 当成 `[说话人]` 前缀剥掉 ⇒ **两条都变成空正文** ⇒ 彻底分不开。

    ⇒ 剥前缀时**必须确认剥出来的 content 非空**，否则视为「这不是前缀，原样保留」。
    """
    if not text:
        return ""
    s = text
    if strip_prefix:
        m = SPEAKER_PREFIX_RE.match(s)
        if m:
            inner = m.group("content") or ""
            # ⚠️ 剥出来是空的 ⇒ 多半是把 `[表情包: ...]` 误认成前缀了 ⇒ 不剥
            if inner.strip():
                s = inner
    s = s.strip()
    s = re.sub(r"[ \t　]+", " ", s)
    return s


def normalize_speaker(speaker: str) -> str:
    """说话人归一化。

    blob 里麦麦自己的发言渲染成 `{bot}(你)`；`get_recent` 里是真实昵称。
    ⇒ 统一去掉「(你)」后缀，并把空白折叠。

    ⚠️ **R6-20b（2026-11-22 00:45）**：回复消息的说话人字段会**混入引用块**：
    `日向雏田(你)[回复了日向雏田的消息: 引用正文…]`
    ⇒ 若不去掉，`speaker` 三键匹配**永远不中** ⇒ **该块漏遮蔽**（不是错删）。
    ⇒ 先剥掉所有 `[回复了...]`（可能嵌套、可能多方括号），再去掉 `(你)`。
    """
    if not speaker:
        return ""
    s = speaker.strip()
    # 剥「[回复了xxx的消息: …]」—— 引用块是宿主渲染，不属于说话人。
    # ⚠️ 可能连续多个、也可能内容里含方括号 ⇒ 非贪婪 + 反复剥到稳定。
    for _ in range(8):  # 上限防御，避免畸形文本死循环
        m = re.search(r"\[回复了[^\]]*\]", s)
        if not m:
            break
        s = (s[: m.start()] + s[m.end():]).strip()
    if s.endswith("(你)"):
        s = s[: -len("(你)")].strip()
    return re.sub(r"[ \t　]+", " ", s)


# ---------------------------------------------------------------------------
# 切块
# ---------------------------------------------------------------------------


def split_blob(blob: str) -> List[BlobBlock]:
    """把 blob 切成对话块。

    **块边界只由块起点唯一定义**（R6-19）⇒ 不依赖任何尾部锚点、
    对 prompt 模板改版免疫。

    末块的 `end = None`（边界未知）⇒ 落在隐藏区间内时**不删 + 告警**。
    """
    if not blob:
        return []
    starts = [(m.start(), m.group(1)) for m in BLOCK_START_RE.finditer(blob)]
    if not starts:
        return []

    blocks: List[BlobBlock] = []
    for i, (pos, hhmmss) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else None
        chunk = blob[pos:end] if end is not None else blob[pos:]
        head_line = chunk.split("\n", 1)[0]
        # ⚠️ **不能只用 head_line 找「说：」**（2026-11-22 01:05 离线报告发现）：
        #    带 `[回复了…]` 引用的消息，说话人与「说：」之间**跨了多行**
        #    （引用内容里有换行）⇒ 第一行根本没有「说：」⇒ 正则失配 ⇒ speaker=""
        #    ⇒ 8/12 个未匹配块由此而来（漏遮蔽）。
        # ⇒ 用**整个 chunk** 做匹配（chunk 已被下一个块起点截断，不会越块）。
        m = BLOCK_LINE_RE.match(chunk)
        speaker_raw = m.group("speaker") if m else ""
        if m:
            content = chunk[m.end():]
        else:
            content = chunk
        blocks.append(
            BlobBlock(
                index=i,
                start=pos,
                end=end,
                hhmmss=hhmmss,
                speaker_raw=speaker_raw,
                content=content,
                head_line=head_line,
            )
        )
    return blocks


# ---------------------------------------------------------------------------
# 候选集构造
# ---------------------------------------------------------------------------


def build_candidates(messages: Sequence[Dict[str, Any]]) -> List[Candidate]:
    """从 `get_recent` 的返回构造候选集。

    ⚠️ **`filter_mai` 必须为 `False`**（P22）：blob 里麦麦自己的发言也在
    （实测 22 块里 10 块是 `日向雏田(你)说：`）⇒ 过滤掉就**无法识别她的块**
    ⇒ 隐藏期里她自己的发言会留在上下文里（R6-11 换形式复现）。

    每条消息取三个字段：
    - `timestamp`（epoch 字符串）
    - `processed_plain_text`（**带说话人前缀** ⇒ 要剥）
    - `message_info.user_info.user_nickname`（说话人，T-C01 修正后为真实键）
    """
    # 第一遍：先收集每条消息的归一化正文（供回复消息拆「引用原文 + 新正文」用，T-C02）
    normalized: Dict[str, str] = {}
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        mid = str(msg.get("message_id") or "")
        p = msg.get("processed_plain_text")
        if not p:
            p = _extract_raw_text(msg)
        normalized[mid] = normalize_content(p or "")

    out: List[Candidate] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        ts_raw = msg.get("timestamp")
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            continue
        hhmmss = _epoch_to_hhmmss(ts)

        speaker = _extract_speaker(msg)
        plain = msg.get("processed_plain_text")
        if not plain:
            # 兜底：退到 raw_message 的文本
            plain = _extract_raw_text(msg)
        content = normalize_content(plain or "")
        raw_mid = str(msg.get("message_id") or "")
        out.append(
            Candidate(
                ts=ts,
                hhmmss=hhmmss,
                speaker_raw=speaker,
                content=content,
                raw=msg,
                message_id=raw_mid,
                display_id=to_display_message_id(raw_mid),
            )
        )
        # ⚠️ 回复消息（T-C02，测试岗 2026-11-22 坐实）：
        #    blob 里回复块的正文只有**新正文**（引用壳在说话人字段，`reply_prompt.py:452-481`），
        #    而库里 `processed_plain_text` = **被引原文 + 空格 + 新正文**（`message_receive/message.py:495`）。
        #    ⇒ 「整段正文相等」与 LCP 前缀兜底双双失配 ⇒ 落在隐藏区间也删不掉（漏遮蔽）。
        #    ⇒ 对回复消息追加一个「剥掉引用原文」的候选变体（同一真实消息，删哪条都等价）。
        reply_to = str(msg.get("reply_to") or "")
        if reply_to:
            quoted = normalized.get(reply_to)
            if quoted and quoted in content:
                rest = content.split(quoted, 1)[1].lstrip(" \n")
                if rest:
                    out.append(
                        Candidate(
                            ts=ts,
                            hhmmss=hhmmss,
                            speaker_raw=speaker,
                            content=rest,
                            raw=msg,
                            message_id=raw_mid,
                            display_id=to_display_message_id(raw_mid),
                        )
                    )
    out.sort(key=lambda c: c.ts)
    return out


def build_msg_id_ts(candidates: Sequence[Candidate]) -> Dict[str, float]:
    """建 ``display_msg_id → 真实 epoch`` 映射（R6-17 第三种载体用）。

    ⚠️ 键用**展示用短 id**（`[msg_id:xxx]` 里出现的那个），
    不是 `get_recent` 返回的原始 id。
    """
    out: Dict[str, float] = {}
    for c in candidates or []:
        if c.display_id:
            out.setdefault(c.display_id, c.ts)
    return out


def _epoch_to_hhmmss(ts: float) -> str:
    import time as _time

    return _time.strftime("%H:%M:%S", _time.localtime(ts))


def _extract_speaker(msg: Dict[str, Any]) -> str:
    info = msg.get("message_info") or {}
    user_info = info.get("user_info") or {}
    # ⚠️ 宿主 `get_recent` 真实序列化的字段名是 `user_nickname` / `user_cardname`
    #    （`plugin_runtime/host/message_utils.py:29-32` 的 UserInfoDict）。
    #    旧键（nickname/user_name/card_name/username）是离线脚本里的假形状，
    #    生产里一个都不存在 ⇒ 说话人键从未生效（测试岗 T-C01，2026-11-22 坐实）。
    #    真实键放最前，旧键保留兼容离线测试台。
    for key in ("user_nickname", "user_cardname", "nickname", "user_name", "card_name", "username"):
        v = user_info.get(key)
        if v:
            return str(v)
    return ""


def _extract_raw_text(msg: Dict[str, Any]) -> str:
    raw = msg.get("raw_message") or {}
    seg = raw.get("segments") or raw.get("message_segment") or []
    if isinstance(seg, list):
        parts = []
        for s in seg:
            if isinstance(s, dict):
                t = s.get("type")
                data = s.get("data") or {}
                if t == "text" or "text" in data:
                    parts.append(str(data.get("text", "")))
        return "".join(parts)
    return ""


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------


def lcp_len(a: str, b: str) -> int:
    """最长公共前缀长度。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


@dataclass
class MatchReport:
    """匹配报告 —— 供每轮输出「已匹配块数 / 总块数」。"""

    total: int = 0
    matched: int = 0
    unmatched_blocks: List[int] = None  # type: ignore[assignment]
    #: 块 index → 候选 index
    mapping: Dict[int, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.unmatched_blocks is None:
            self.unmatched_blocks = []
        if self.mapping is None:
            self.mapping = {}

    @property
    def ratio(self) -> float:
        if not self.total:
            return 1.0
        return self.matched / float(self.total)

    def summary(self) -> str:
        return "已匹配块数 %d / 总块数 %d（%.0f%%）" % (
            self.matched, self.total, self.ratio * 100)


def match_blocks(
    blocks: Sequence[BlobBlock],
    candidates: Sequence[Candidate],
) -> Tuple[Dict[int, Candidate], MatchReport]:
    """把块对齐到真实消息。

    **消歧顺序**：
    1. **顺序约束**（块序 = 消息序，单调不回退）—— 主力
    2. **整段正文相等** —— 同秒内的 tie-break
    3. **LCP ≥ 12** —— 最后手段，且必须唯一

    **匹配不上 → 不返回该块**（上层据此「保留不删 + 计数告警」）。
    """
    report = MatchReport(total=len(blocks))

    # 预索引：按 hhmmss 分组
    by_time: Dict[str, List[int]] = {}
    for i, c in enumerate(candidates):
        by_time.setdefault(c.hhmmss, []).append(i)

    last_cand_idx = -1  # 顺序约束：块 i 的候选不得早于块 i-1 的候选

    is_last = len(blocks) - 1
    for blk in blocks:
        pool = by_time.get(blk.hhmmss, [])
        if not pool:
            report.unmatched_blocks.append(blk.index)
            continue

        blk_speaker = normalize_speaker(blk.speaker_raw)
        # ⚠️ blob 侧正文**已剥过前缀** ⇒ 不再剥（否则表情包占位会被误吞）
        blk_content = normalize_content(blk.content, strip_prefix=False)
        # ⚠️ T-B08（测试岗 2026-11-22）：宿主把尾部提示（「你正在和对方私聊…」）
        #    接在**最后一块**正文后面 ⇒ 末块正文永远多一截 ⇒ 必然失配。
        #    ⇒ 末块按公共前缀截掉尾部提示再参与比对（提示本身不受影响，它不在块边界里）。
        if blk.index == is_last and TRAILING_HINT_MARKER in blk_content:
            blk_content = blk_content.split(TRAILING_HINT_MARKER, 1)[0].rstrip()

        # 候选过滤：说话人相等（若 blob 说话人可解析）+ 顺序约束
        narrowed: List[int] = []
        for ci in pool:
            cand = candidates[ci]
            if blk_speaker and normalize_speaker(cand.speaker_raw):
                if normalize_speaker(cand.speaker_raw) != blk_speaker:
                    continue
            if ci < last_cand_idx:
                continue  # 顺序约束：单调不回退
            narrowed.append(ci)

        if not narrowed:
            report.unmatched_blocks.append(blk.index)
            continue

        picked: Optional[int] = None
        if len(narrowed) == 1:
            # ⚠️ T-C09（测试岗 2026-11-22）：唯一候选**也不免检正文**。
            #    扩窗修好后（T-C06，24→96→384h）候选池变大，「碰巧同秒」的唯一候选
            #    可能根本不是目标块 ⇒ 直接采纳 = 错删入口。
            #    保守口径：正文相等或 LCP≥12 才采纳；对不上 ⇒ 保留不删 + 告警
            #    （宁可少遮不可错删，与既有纪律一致）。
            ci = narrowed[0]
            if (
                candidates[ci].content == blk_content
                or lcp_len(candidates[ci].content, blk_content) >= LCP_MIN
            ):
                picked = ci
        else:
            # tie-break ①：整段正文完全相等
            exact = [ci for ci in narrowed if candidates[ci].content == blk_content]
            if len(exact) == 1:
                picked = exact[0]
            elif len(exact) > 1:
                picked = exact[0]
            else:
                # tie-break ②：LCP ≥ 12，且必须唯一
                lcp_hits = [
                    ci for ci in narrowed
                    if lcp_len(candidates[ci].content, blk_content) >= LCP_MIN
                ]
                if len(lcp_hits) == 1:
                    picked = lcp_hits[0]

        if picked is None:
            report.unmatched_blocks.append(blk.index)
            continue

        report.mapping[blk.index] = picked
        report.matched += 1
        last_cand_idx = picked

    return {k: candidates[v] for k, v in report.mapping.items()}, report
