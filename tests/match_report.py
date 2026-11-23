# -*- coding: utf-8 -*-
"""MaiKnock · 匹配层离线歧义率报告（M4 第⑧步）—— **纯离线，不碰麦麦**。

**目的**（设计稿第 1490 行）：
    「量化歧义率，决定丙′（retro 兜底）要不要接线」

**做法**：把 `logs/maisaka_prompt/replyer/internal_private_owner/*.json` 的**每一份**真实快照
当测试样本，用 `matcher.match_blocks` 把 blob 的块对齐到真实消息，统计：

- **匹配率**（匹配上的块 / 总块数）—— 低于 100% 的块会被**保留不删**（漏遮蔽风险）
- **歧义率**（候选数 > 1 的块占比）—— 靠「整段相等 + 顺序约束」消歧，歧义高说明危险
- **LCP 兜底使用率** —— 用了兜底说明主判据不够
- **各类 `how` 的分布** —— 看哪种匹配依据最常见、哪种失败最多
- **未匹配块的形态分类** —— 找出系统性的匹配漏洞（如「[回复了…]」前缀）

**⚠️ 重要**：这个脚本**只读**快照与数据库，不写任何东西、不连 WS、不影响麦麦。

用法：
    python mk_match_report.py
    python mk_match_report.py --json report.json   # 另存 JSON
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

PLUG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUG))

import matcher as M  # noqa: E402

SNAP_DIR = Path(r"E:\Softw\MaiBot\logs\maisaka_prompt\replyer\internal_private_owner")
DB = Path(r"E:\Softw\MaiBot\data\MaiBot.db")


def load_messages() -> list:
    """从数据库读消息，**模拟 `get_recent` 的口径**（timestamp = epoch 字符串）。

    ⚠️ 数据库 timestamp 列是**日期字符串**，而 `get_recent` 返回 **epoch 字符串**
    ⇒ 必须转换，否则 `build_candidates` 全部跳过（已踩过两次）。
    """
    con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    cur = con.cursor()
    cur.execute(
        "SELECT message_id, timestamp, processed_plain_text, user_nickname "
        "FROM mai_messages ORDER BY id")
    rows = cur.fetchall()
    con.close()
    out = []
    for mid, ts, plain, nick in rows:
        try:
            ep = datetime.datetime.fromisoformat(ts).timestamp()
        except (TypeError, ValueError):
            continue
        out.append({
            "message_id": mid,
            "timestamp": str(ep),
            "processed_plain_text": plain,
            "message_info": {"user_info": {"nickname": nick}},
        })
    return out


def load_snapshots() -> list:
    """读所有 replyer 快照，返回 ``(文件名, blob)`` 列表（按时间排序）。"""
    out = []
    for f in sorted(glob.glob(str(SNAP_DIR / "*.json")), key=os.path.getmtime):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        blob = ""
        for it in d.get("request_items") or []:
            if it.get("item_type") != "UserMessageItem":
                continue
            t = "".join(
                p.get("text", "") for p in (it.get("parts") or []) if isinstance(p, dict))
            if len(t) > len(blob):
                blob = t
        if blob and M.BLOCK_START_RE.search(blob):
            out.append((os.path.basename(f), blob))
    return out


def classify_unmatched(block, result) -> str:
    """给未匹配的块归类（用于找系统性漏洞）。"""
    if result.how == "无候选":
        return "无候选（时分秒/说话人都不匹配）"
    if result.how.startswith("正文不等且LCP不足"):
        return "有候选但正文不等"
    return result.how or "未知"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=str, default="", help="另存 JSON 报告")
    a = ap.parse_args()

    messages = load_messages()
    cands = M.build_candidates(messages)
    snaps = load_snapshots()

    print("=" * 78)
    print("MaiKnock · 匹配层离线歧义率报告")
    print("=" * 78)
    print("  样本快照: %d 份" % len(snaps))
    print("  候选消息: %d 条" % len(cands))
    print()

    total_blocks = 0
    total_matched = 0
    total_ambiguous = 0
    total_lcp = 0
    how_counter = collections.Counter()
    unmatched_kinds = collections.Counter()
    per_snap = []
    worst = []

    for fn, blob in snaps:
        blocks = M.split_blob(blob)
        if not blocks:
            continue
        mapping, report = M.match_blocks(blocks, cands)
        matched = report.matched
        # 歧义：同一「时分秒 + 归一化说话人」的候选 > 1
        # （matcher 内部就是按这个键建池的，这里复刻同样的口径）
        pool_sizes = collections.Counter(
            (c.hhmmss, M.normalize_speaker(c.speaker_raw)) for c in cands)
        ambiguous = sum(
            1 for b in blocks
            if pool_sizes.get((b.hhmmss, M.normalize_speaker(b.speaker_raw)), 0) > 1)
        total_blocks += len(blocks)
        total_matched += matched
        total_ambiguous += ambiguous
        # 未匹配块的形态分类（找系统性漏洞）
        for idx in report.unmatched_blocks:
            b = blocks[idx]
            sp = M.normalize_speaker(b.speaker_raw)
            if "[回复了" in b.speaker_raw:
                unmatched_kinds["说话人带 [回复了…] 引用块"] += 1
            elif not b.speaker_raw:
                unmatched_kinds["说话人为空（续行/多行消息体）"] += 1
            elif pool_sizes.get((b.hhmmss, sp), 0) == 0:
                unmatched_kinds["无同秒同人候选（消息未落库/时间差）"] += 1
            else:
                unmatched_kinds["有候选但正文不等"] += 1
        rate = report.ratio
        per_snap.append((fn, len(blocks), matched, rate))
        if rate < 1.0:
            worst.append((rate, fn, len(blocks), matched))

    if total_blocks == 0:
        print("❌ 没有可分析的块")
        return 1

    print("=" * 78)
    print("总览")
    print("=" * 78)
    print("  总块数         : %d" % total_blocks)
    print("  匹配上         : %d  (%.2f%%)" % (
        total_matched, 100.0 * total_matched / total_blocks))
    print("  未匹配         : %d  (%.2f%%)  ← 这些块**不会被遮蔽**（漏遮蔽风险）" % (
        total_blocks - total_matched, 100.0 * (total_blocks - total_matched) / total_blocks))
    print("  多候选（歧义） : %d  (%.2f%%)" % (
        total_ambiguous, 100.0 * total_ambiguous / total_blocks))
    print()
    print()
    if unmatched_kinds:
        print("  未匹配原因分布:")
        for kind, n in unmatched_kinds.most_common():
            print("    %-46s %5d" % (kind, n))
    print()

    print("=" * 78)
    print("匹配率低于 100% 的快照（Top 10）")
    print("=" * 78)
    for rate, fn, n, m in sorted(worst)[:10]:
        print("  %6.1f%%  %2d/%2d  %s" % (rate * 100, m, n, fn))
    if not worst:
        print("  （无 —— 全部快照 100% 匹配）")
    print()

    # 结论：决定丙′要不要接线
    rate_all = total_matched / total_blocks
    print("=" * 78)
    print("结论（供 PM / 审计参考）")
    print("=" * 78)
    if rate_all >= 0.95:
        print("  ✅ 匹配率 %.1f%% ≥ 95%% —— 主判据（整段正文相等 + 顺序约束）够用，" % (rate_all * 100))
        print("     **丙′（retro 兜底）不需要接线**（当前 creative_mode=false 也跑不到）。")
    elif rate_all >= 0.80:
        print("  ⚠️ 匹配率 %.1f%% —— 存在系统性漏遮蔽，建议先修匹配层再决定丙′。" % (rate_all * 100))
    else:
        print("  🔴 匹配率 %.1f%% —— 匹配层不可靠，丙′ 接线也救不了。" % (rate_all * 100))

    if a.json:
        payload = {
            "snapshots": len(snaps),
            "candidates": len(cands),
            "total_blocks": total_blocks,
            "matched": total_matched,
            "ambiguous": total_ambiguous,
            "match_rate": rate_all,
            "unmatched_reasons": dict(unmatched_kinds),
            "per_snapshot": [
                {"file": f, "blocks": n, "matched": m, "rate": r}
                for f, n, m, r in per_snap],
        }
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("\n  JSON 报告已写入: %s" % a.json)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
