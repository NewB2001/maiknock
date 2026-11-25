# -*- coding: utf-8 -*-
"""麦麦聊天室 · 阵型规划（纯逻辑，可离线单测）。

**要解决的问题**：一轮里「谁来接话、按什么次序接」。

**三种阵型**（2026-11-25 与用户确认后写定，08 §3.3）：

============  ==========================================  ==========================
阵型          行为                                        实现要点
============  ==========================================  ==========================
``serial``    随机挑接话的 Bot 与顺序，**逐个**发言；        递话前把先答 Bot 的回复
              后答的**看得见**先答的回复                     **接力**进后者的上下文
``parallel``  所有 Bot **同时**发言；此轮发言                ✅ 天然成立：各 Bot 的群流是
              **完全不参考**对方说了什么                     **各自独立的副本**
``composite`` 随机分波：**波内并行、波间串行**               波间的串行靠回复接力
============  ==========================================  ==========================

⚠️ **「全串行是硬需求」是误解，已收回**（用户澄清）：
三种阵型**都要有**，随机挑，**全程无 UI 阵型选项**。

⚠️ **并行阵型与限流的关系**（08 §3.5.5）：
并行时两个 Bot 同时递 = 8+ 个并发请求 ⇒ 会撞上游限流。
但那是**配额**问题（账号级），不是阵型问题 ⇒ **不砍并行阵型**，
而是靠「每个 Bot 用不同模型 / 不同 provider」分桶来解。

**为什么「接话者」不随机挑**：
08 §3.3 写的是「随机挑本轮接话的 Bot」，但**三种阵型都涉及多个 Bot**
（并行要多个、复合要多个）⇒ 只挑一个的话另外两种阵型无从谈起。
且壳阶段的 mock 实现（`plugin.py:1072` 附近）也是「全部在场 Bot 都接话，只打乱顺序」。
⇒ **口径**：接话者 = 点名的（有则按勾选顺序）或全部在场的；
**随机的是「怎么接」**（阵型 + 顺序 + 分波）。

**seed 必须存档**（FR-05 / FR-14）：随机轮次中控掷骰子定阵型，
**结果连同 seed 存档** ⇒ 同一 seed 必得同一结果（重演可复现）。

**纯逻辑模块**：只依赖标准库 ⇒ 可离线单测。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

#: 三种阵型
FORMATION_SERIAL = "serial"
FORMATION_PARALLEL = "parallel"
FORMATION_COMPOSITE = "composite"

#: 全部阵型（随机时从中挑）
ALL_FORMATIONS = (FORMATION_SERIAL, FORMATION_PARALLEL, FORMATION_COMPOSITE)

#: 阵型的中文标签（给日志与调试面板看；**界面上不出现阵型选项**）
FORMATION_LABELS = {
    FORMATION_SERIAL: "随机串行",
    FORMATION_PARALLEL: "并行",
    FORMATION_COMPOSITE: "随机复合",
}

#: 调度策略：显式点名 / 默认随机
STRATEGY_EXPLICIT = "explicit"
STRATEGY_RANDOM = "random"


@dataclass
class FormationPlan:
    """一轮的调度计划（**可存档、可复现**）。

    :param seed: 本轮的随机种子（存档下来 ⇒ 同一 seed 必得同一计划）
    :param strategy: ``explicit``（点名）/ ``random``（随机）
    :param formation: 阵型（serial / parallel / composite）
    :param speakers: 接话的 Bot（member_id，有序）
    :param waves: 波次表 —— **每个元素是一波，波内并行、波间串行**
    :param named: 显式点名的 member_id 集合
    """

    seed: int
    strategy: str
    formation: str
    speakers: List[str] = field(default_factory=list)
    waves: List[List[str]] = field(default_factory=list)
    named: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """阵型的中文标签。"""
        return FORMATION_LABELS.get(self.formation, self.formation)

    @property
    def wave_count(self) -> int:
        """波数。"""
        return len(self.waves)

    def to_dict(self) -> Dict[str, Any]:
        """存档用的字典形状（能塞进 JSON 列）。"""
        return {
            "seed": self.seed,
            "strategy": self.strategy,
            "formation": self.formation,
            "speakers": list(self.speakers),
            "waves": [list(w) for w in self.waves],
            "named": list(self.named),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "FormationPlan":
        """从存档的字典还原（重演用）。"""
        return cls(
            seed=int(raw.get("seed") or 0),
            strategy=str(raw.get("strategy") or STRATEGY_RANDOM),
            formation=str(raw.get("formation") or FORMATION_SERIAL),
            speakers=[str(x) for x in (raw.get("speakers") or [])],
            waves=[[str(x) for x in wave] for wave in (raw.get("waves") or [])],
            named=[str(x) for x in (raw.get("named") or [])],
        )


def pick_formation(rng: random.Random) -> str:
    """随机挑一个阵型。

    ⚠️ **三种等概率** —— 没有理由偏袒哪一个（用户明确要求「三种都要有，随机挑」）。
    """
    return rng.choice(list(ALL_FORMATIONS))


def split_into_waves(rng: random.Random, speakers: Sequence[str]) -> List[List[str]]:
    """把接话者随机分波（**复合阵型**用）。

    ⚠️ 分波是**随机切分**：洗牌后随机决定波数（1..n），再切成若干段。
    ⇒ 会出现「Bot1+Bot2 并行，Bot3 在后面串行」这种形态。
    """
    items = list(speakers)
    if not items:
        return []
    if len(items) == 1:
        return [[items[0]]]
    rng.shuffle(items)
    wave_count = rng.randint(1, len(items))
    # 随机切点（保证每波至少一个）
    cuts = sorted(rng.sample(range(1, len(items)), wave_count - 1)) if wave_count > 1 else []
    waves: List[List[str]] = []
    prev = 0
    for cut in list(cuts) + [len(items)]:
        waves.append(items[prev:cut])
        prev = cut
    return [w for w in waves if w]


def plan_formation(
    seed: int,
    candidates: Sequence[str],
    named: Sequence[str] = (),
    *,
    formation: Optional[str] = None,
) -> FormationPlan:
    """规划一轮的阵型。

    :param seed: 随机种子（**同一个 seed 必得同一个计划**）
    :param candidates: 候选接话者（member_id）
    :param named: 显式点名的 member_id（**非空 ⇒ 走导演制**：按勾选顺序全串行）
    :param formation: 强制指定阵型（测试 / 重演用；None = 随机挑）
    """
    rng = random.Random(int(seed))
    candidates = [str(x) for x in (candidates or []) if str(x)]
    named_ids = [str(x) for x in (named or []) if str(x)]

    # ① 显式点名 ⇒ 导演制（ADR-2）：按勾选顺序**全串行**
    if named_ids:
        speakers = [m for m in named_ids if m in candidates] or named_ids
        waves = [[m] for m in speakers]
        return FormationPlan(
            seed=int(seed),
            strategy=STRATEGY_EXPLICIT,
            formation=FORMATION_SERIAL,
            speakers=speakers,
            waves=waves,
            named=list(named_ids),
        )

    # ② 随机轮次
    chosen = formation or pick_formation(rng)
    if chosen == FORMATION_PARALLEL:
        speakers = list(candidates)
        waves = [list(speakers)] if speakers else []
    elif chosen == FORMATION_COMPOSITE:
        speakers = list(candidates)
        waves = split_into_waves(rng, speakers)
    else:   # serial
        speakers = list(candidates)
        rng.shuffle(speakers)          # 随机挑顺序
        waves = [[m] for m in speakers]
    return FormationPlan(
        seed=int(seed),
        strategy=STRATEGY_RANDOM,
        formation=chosen,
        speakers=speakers,
        waves=waves,
        named=list(named_ids),
    )


def next_seed(turn_counter: int, room_count: int = 0) -> int:
    """算下一轮的种子。

    ⚠️ **必须可复现** ⇒ 同一 ``(turn_counter, room_count)`` 必得同一种子。
    沿用壳阶段的算法（``plugin.py:1072`` 附近）：``turn * 7919 + rooms * 104729``。
    """
    return int(turn_counter) * 7919 + int(room_count) * 104729
