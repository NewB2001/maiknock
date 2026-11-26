# -*- coding: utf-8 -*-
"""麦麦聊天室 · talkrules（提及触发配置维护）离线单测。

运行：
    python test_chatroom_talkrules.py

**口径变更**（2026-11-26 ㉚）：不再写「查看频率=0」规则——那会把整条流的
Planner 入口焊死（实测两个 Bot 全哑巴）。「平时闭嘴、递话才开口」由
talkvalve（frequency.set_adjust 阀门）在运行时拧。

**现在在防什么**：
① 历史写过的 webui_virtual_group_* 静音规则被**清掉**（幂等）；
② 遗留的 webui/* 通配静音规则被清掉（不连坐别的插件）；
③ mentioned_bot_reply 不是 true 时置 true；
④ 别人写的规则（如 qq 平台）原样保留；
⑤ 注释保留（tomlkit 往返不丢 # 注释）；
⑥ 文件不存在 / 空文件 / 缺表 ⇒ 不抛异常、不建多余的表。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tomlkit  # noqa: E402

from chatroom.talkrules import ensure_talk_config  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


def _rule(item_id: str, platform: str = "webui", value: float = 0.0) -> str:
    return (
        '{enabled = true, platform = "%s", item_id = "%s", '
        'rule_type = "group", time = "", value = %s}' % (platform, item_id, value)
    )


def write_cfg(path: Path, rules: list, mentioned: str = "false") -> None:
    rows = ", ".join(rules) if rules else ""
    text = f"""# 我的手写注释要活着
[bot]
nickname = "日向雏田"

[chat]
[chat.reply_timing]
talk_value = 1
mentioned_bot_reply = {mentioned} # 开启后，只要消息提到麦麦名字就更容易回复。

[chat.reply_timing.talk_rule_groups]
active_group = "默认"

[[chat.reply_timing.talk_rule_groups.groups]]
name = "默认"
rules = [{rows}]
"""
    path.write_text(text, encoding="utf-8")


def read_rules(path: Path):
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    groups = doc["chat"]["reply_timing"]["talk_rule_groups"]["groups"]
    for g in groups:
        if str(g["name"]) == "默认":
            return [dict(r) for r in g["rules"]]
    return []


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mk_talkrules_")
    cfg = Path(tmp) / "bot_config.toml"

    print("== 1. ★ 历史的 value=0 静音规则被清掉 + 提及开关置 true ==")
    write_cfg(cfg, [
        _rule("webui_virtual_group_room_a"),
        _rule("webui_virtual_group_room_b"),
    ], mentioned="false")
    changed = ensure_talk_config(cfg)
    check("首次清理返回 True（有改动）", changed is True)
    rules = read_rules(cfg)
    check("★ 我们的静音规则清空了", rules == [], f"实际 {rules}")
    doc = tomlkit.parse(cfg.read_text(encoding="utf-8"))
    check("mentioned_bot_reply 已置 true",
          doc["chat"]["reply_timing"]["mentioned_bot_reply"] is True)
    check("★ 注释保留", "# 我的手写注释要活着" in cfg.read_text(encoding="utf-8"))

    print("== 2. 幂等：再跑 ⇒ 不改文件 ==")
    mtime1 = cfg.stat().st_mtime_ns
    check("返回 False", ensure_talk_config(cfg) is False)
    check("文件没被重写", cfg.stat().st_mtime_ns == mtime1)

    print("== 3. ★ 遗留 webui/* 通配规则被清掉（不连坐别的插件）==")
    write_cfg(cfg, [
        _rule("*"),
        _rule("someone_elses_stream", platform="webui", value=0.5),
    ], mentioned="true")
    check("返回 True", ensure_talk_config(cfg) is True)
    rules = read_rules(cfg)
    check("通配没了、别人的 webui 0.5 规则还在",
          len(rules) == 1 and rules[0]["item_id"] == "someone_elses_stream",
          f"实际 {rules}")

    print("== 4. ★ 别人写的规则（qq 平台）原样保留 ==")
    doc = tomlkit.parse(cfg.read_text(encoding="utf-8"))
    groups = doc["chat"]["reply_timing"]["talk_rule_groups"]["groups"]
    for g in groups:
        if str(g["name"]) == "默认":
            it = tomlkit.inline_table()
            it.update({"enabled": True, "platform": "qq", "item_id": "123456",
                       "rule_type": "group", "time": "", "value": 0.8})
            g["rules"].append(it)
    cfg.write_text(tomlkit.dumps(doc), encoding="utf-8")
    ensure_talk_config(cfg)
    rules = read_rules(cfg)
    check("qq 规则没被动",
          any(r.get("platform") == "qq" and float(r.get("value") or 0) == 0.8 for r in rules),
          f"实际 {rules}")

    print("== 5. 文件不存在 / 空文件 / 缺表 ⇒ 不抛异常 ==")
    check("文件不存在返回 False", ensure_talk_config(Path(tmp) / "nope.toml") is False)
    empty = Path(tmp) / "empty.toml"
    empty.write_text("", encoding="utf-8")
    check("空文件返回 True（补了提及开关）", ensure_talk_config(empty) is True)
    doc = tomlkit.parse(empty.read_text(encoding="utf-8"))
    check("空文件里 mentioned_bot_reply 就位",
          doc["chat"]["reply_timing"]["mentioned_bot_reply"] is True)
    # 缺规则组表：有 [chat.reply_timing] 但没有 talk_rule_groups ⇒ 只补开关、不建多余结构
    partial = Path(tmp) / "partial.toml"
    partial.write_text('[chat.reply_timing]\nmentioned_bot_reply = true\n', encoding="utf-8")
    check("缺规则组表返回 False（没东西可清）", ensure_talk_config(partial) is False)

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
