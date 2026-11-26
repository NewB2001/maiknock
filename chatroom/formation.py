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
⚠️ **2026-11-27 用户拍板加权**：三种不再等概率 ——
复合 50% / 并行 30% / 串行 20%（串行让后答 Bot 干等，体感慢）；
两 Bot 的复合恒 1 波（真并行），避免退化成串行。

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

#: ★ 随机权重（2026-11-27 用户拍板）：合流为主、并行次之、串行最小。
#:    原因：等概率时「逐个接话」的实际占比≈50%（串行 1/3 + 复合在两 Bot 时
#:    有一半概率随机切成每人一波 = 行为退化成串行）⇒ 第二个 Bot 总要等前面的
#:    思考完（实测宿主单轮 planner+replyer 可达 3 分钟）。
#:    改权重 + 两 Bot 复合恒 1 波后，「实际逐个接话」降到 ~20%。
FORMATION_WEIGHTS: Dict[str, int] = {
    FORMATION_COMPOSITE: 50,    # 串并合流（主流）
    FORMATION_PARALLEL: 30,     # 并行
    FORMATION_SERIAL: 20,       # 随机串行（最小）
}

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
    """按权重随机挑一个阵型（2026-11-27 用户拍板）。

    ★ 不再等概率 —— 用户要求「串并合流为主、并行次之、随机串行最小」
    （串行让后答 Bot 干等，体感很慢）。权重见 ``FORMATION_WEIGHTS``：
    复合 50% / 并行 30% / 串行 20%。

    ⚠️ 用 ``rng.choices`` 而不是手写累计概率：同一 seed 仍必得同一结果
    （``random.Random`` 的采样序列可复现），FR-05 / FR-14 的重演契约不破。
    """
    names = list(FORMATION_WEIGHTS)
    weights = [FORMATION_WEIGHTS[n] for n in names]
    return rng.choices(names, weights=weights, k=1)[0]


#: 无勾选轮次里，单个 Bot「这轮不发言」的概率（2026-11-27 用户拍板 5%）
DEFAULT_SILENCE_RATE = 0.05


def apply_silence_roll(
    rng: random.Random,
    candidates: Sequence[str],
    rate: float = DEFAULT_SILENCE_RATE,
) -> List[str]:
    """无勾选轮次：先挨个掷「不发言」骰子，再返回留下来的接话者。

    ⚠️ 用户口径（2026-11-27）：
    - 每个 Bot 独立按 ``rate`` 概率沉默（不递话 ⇒ 它根本没看见这轮台词）；
    - **全军覆没则重掷**（递归重试，直到至少留一个）——绝不允许整轮没人说话；
    - 留下来的才参与后面的三种阵型随机。

    ⚠️ 重演可复现：rng 是同一 seed 建立的，重掷消耗它的序列，
    同一 seed 必得同一结果（FR-05）。
    """
    items = [str(x) for x in (candidates or []) if str(x)]
    if not items:
        return []
    rate = max(0.0, min(1.0, float(rate)))
    while True:
        kept = [m for m in items if rng.random() >= rate]
        if kept:
            return kept
        # 全军覆没 ⇒ 重掷（rate=1.0 的病态输入会死循环，兜底留第一个）
        if rate >= 1.0:
            return [items[0]]


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
    # ★ 两 Bot 时恒 1 波（2026-11-27 用户拍板）：
    #    原来的 randint(1,2) 有一半概率切成每人一波 ⇒ 行为跟纯串行完全一样，
    #    导致「名义复合 33% + 退化一半 = 实际逐个接话 50%」的体感。
    #    两 Bot 就两个，合流 = 一起说，分波没有中间形态。
    if len(items) == 2:
        return [items]
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
