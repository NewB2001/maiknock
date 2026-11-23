# -*- coding: utf-8 -*-
"""批量给麦麦发消息并逐条收集回复（实机测试用，MaiKnock 测试台架配套）。

⚠️ 三条硬约束（0.2.3 回归轮实测坐实，违反即丢回复/丢消息，成本最高的一课）：
  1. **每条消息必须用一条全新的 WS 连接** —— 同一连接上的第 2 条 message.send
     不入库（DB 里查得到用户消息、查不到对应回复就是这个坑）。
  2. **拿到回复才发下一条** —— 否则后一条把前一条的 MaiSaka loop 顶掉
     （「后到覆盖先到」），前一条的回复被丢弃。
  3. **连接空闲会断**（BrokenPipeError）—— 每条消息独立连接天然规避；
     同一条内遇到连接级失败自动重试（最多 3 次）。

另两条纪律：
  · 不要用固定 sleep 连发（sleep 2s × 20 条会堆到 4 条被覆盖）——本脚本按约束 2 串行；
  · 批次消息**不要重复**——新模型会引用前文（「这已经是第三次了」），污染上下文。

用法（宿主解释器，websockets/httpx 宿主自带）：
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" batch_send.py --file messages.txt
    "C:/Users/29411/AppData/Roaming/uv/tools/maibot/Scripts/python.exe" batch_send.py "消息一" "消息二"

messages.txt 每行一条消息（# 开头 = 注释，空行跳过）。
消息会真实进入麦麦的聊天记忆——测试内容请认真写，别发垃圾刷屏。

输出：[sent] / [reply N] 实时进度，收尾 [summary] sent=X replied=Y no_reply=[…]；
退出码 0 = 全部有回复 / 2 = 部分无回复 / 1 = 出错。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

sys.stdout.reconfigure(encoding="utf-8")

HOST = "http://127.0.0.1:8001"
MAIBOT_ROOT = Path("E:/Softw/MaiBot")
CRED_PATH = MAIBOT_ROOT / "host" / "webui.json"
# anti_crawler 会拦非浏览器 UA（403 空响应体），必须伪装浏览器
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MAX_RETRY = 3  # 单条消息连接级失败的最大重试次数


class AckError(Exception):
    """message.send 的 ack 报 ok=false——消息被服务端拒绝，重试无意义。"""


def login() -> str:
    """读凭据文件登录宿主，返回完整 Cookie 头值。"""
    token = json.loads(CRED_PATH.read_text(encoding="utf-8"))["access_token"]
    with httpx.Client(base_url=HOST, timeout=15, headers={"User-Agent": UA}) as c:
        resp = c.post("/api/webui/auth/verify", json={"token": token})
        if not resp.json().get("valid"):
            raise SystemExit("登录失败：token 无效（检查 %s）" % CRED_PATH)
        return "; ".join(f"{k}={v}" for k, v in resp.cookies.items())


def pick_running_bot(cookie: str, want: str | None) -> str:
    """查 bot 列表，选 running 状态的 bot_id；want 按名称/id 模糊匹配。"""
    with httpx.Client(base_url=HOST, timeout=15,
                      headers={"User-Agent": UA, "Cookie": cookie}) as c:
        bots = c.get("/api/host/bots").json()["bots"]
    running = [b for b in bots if b["status"] == "running"]
    if want:
        hit = [b for b in running
               if want.lower() in b["bot_id"].lower() or want.lower() in b["name"].lower()]
        if not hit:
            raise SystemExit("找不到 running 且匹配 %r 的 bot。running: %s"
                             % (want, [b["name"] for b in running]))
        running = hit
    if not running:
        raise SystemExit("没有 running 状态的 bot（MaiBot 没启动？）。全部: "
                         + str([(b["name"], b["status"]) for b in bots]))
    return running[0]["bot_id"]


async def _send_one_attempt(cookie: str, bot_id: str, text: str,
                            wait_s: float, user_name: str) -> list[str]:
    """单条消息单次尝试：全新连接 → session.open → 排空 → send → 等回复。"""
    ws_url = f"ws://127.0.0.1:8001/api/host/bots/{bot_id}/proxy/api/webui/ws"
    headers = {"Cookie": cookie, "User-Agent": UA, "Origin": "http://127.0.0.1:7999"}
    client_session = "wb_batch_%d" % int(time.time() * 1000)

    async with websockets.connect(ws_url, additional_headers=headers, max_size=None) as ws:
        await ws.send(json.dumps({
            "op": "call", "domain": "chat", "method": "session.open",
            "id": "1", "session": client_session,
            "data": {"user_name": user_name, "restore": True},
        }, ensure_ascii=False))
        opened = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if not opened.get("ok", True):
            raise AckError("session.open 失败: " + json.dumps(opened, ensure_ascii=False)[:200])

        await asyncio.sleep(1.0)
        try:  # 排空 open 触发的欢迎/历史事件
            while True:
                await asyncio.wait_for(ws.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            pass

        await ws.send(json.dumps({
            "op": "call", "domain": "chat", "method": "message.send",
            "id": "2", "session": client_session,
            "data": {"content": text},
        }, ensure_ascii=False))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if not ack.get("ok"):
            raise AckError("message.send 失败: " + json.dumps(ack, ensure_ascii=False)[:200])
        print(f"[sent] {text}", flush=True)

        deadline = time.time() + wait_s
        replies: list[str] = []
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(),
                                             timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            evt = json.loads(raw)
            if evt.get("domain") != "chat":
                continue
            data = evt.get("data") or {}
            # bot 回复 type="bot_message"（用户回显是 type="message"，跳过——
            # 只匹配 "message" 会永远等不到回复，2026-11-23 实机踩坑）
            if str(data.get("type") or "") == "bot_message":
                content = str(data.get("content") or "")
                if content.startswith(text):  # 去掉拼在开头的用户原话回显
                    content = content[len(text):].lstrip()
                if content:
                    replies.append(content)
                    print(f"[reply {len(replies)}]\n{content}", flush=True)
        return replies


async def send_one(cookie: str, bot_id: str, text: str, wait_s: float,
                   user_name: str) -> list[str]:
    """硬约束 1+3：一条消息 = 全新连接；连接级失败自动重试（ack 拒绝不重试）。"""
    last_err: Exception | None = None
    for i in range(1, MAX_RETRY + 1):
        try:
            return await _send_one_attempt(cookie, bot_id, text, wait_s, user_name)
        except (websockets.ConnectionClosed, BrokenPipeError, ConnectionError,
                OSError, asyncio.TimeoutError) as exc:
            last_err = exc
            print(f"[retry {i}/{MAX_RETRY}] 连接级失败（{text[:16]}…）：{exc!r}", flush=True)
            await asyncio.sleep(2)
    raise RuntimeError(f"重试 {MAX_RETRY} 次仍失败：{last_err!r}")


def load_messages(args) -> list[str]:
    msgs: list[str] = []
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                msgs.append(line)
    msgs.extend(args.messages)
    if not msgs:
        raise SystemExit("没有消息可发：给位置参数或 --file")
    seen: set[str] = set()
    uniq: list[str] = []
    for m in msgs:
        if m in seen:
            print(f"[warn] 批次内重复消息已去重（新模型会引用前文污染上下文）：{m[:24]}…",
                  flush=True)
            continue
        seen.add(m)
        uniq.append(m)
    return uniq


async def run(cookie: str, bot_id: str, msgs: list[str],
              wait_s: float, cooldown_s: float, user_name: str):
    no_reply: list[str] = []
    for idx, text in enumerate(msgs, 1):
        print(f"\n===== 消息 {idx}/{len(msgs)} =====", flush=True)
        try:
            replies = await send_one(cookie, bot_id, text, wait_s, user_name)
        except AckError as exc:
            print(f"[error] 服务端拒绝，跳过该条：{exc}", flush=True)
            no_reply.append(text)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[error] 发送失败，跳过该条：{exc!r}", flush=True)
            no_reply.append(text)
            continue
        if not replies:
            no_reply.append(text)
            print("[result] 等待期内无回复（硬约束 2：等到了超时才继续，不会顶掉下一条）",
                  flush=True)
        if idx < len(msgs) and cooldown_s > 0:
            await asyncio.sleep(cooldown_s)  # 冷却 ≠ 固定 sleep 连发：此时上一条已收尾
    return no_reply


def main() -> None:
    ap = argparse.ArgumentParser(description="批量实机发消息测试（三条硬约束内置）")
    ap.add_argument("messages", nargs="*", help="要发的消息（与 --file 二选一或混用）")
    ap.add_argument("--file", help="消息清单文件：每行一条，# 注释，空行跳过")
    ap.add_argument("--wait", type=float, default=60.0, help="单条等待回复秒数（默认 60）")
    ap.add_argument("--cooldown", type=float, default=5.0,
                    help="两条之间的冷却秒数（默认 5；此时上一条已收尾，不算连发）")
    ap.add_argument("--bot", default=None, help="bot 名称/id 模糊匹配（默认唯一 running bot）")
    ap.add_argument("--user", default="国强", help="发送者显示名（默认 国强=主人身份）")
    args = ap.parse_args()

    msgs = load_messages(args)
    cookie = login()
    bot_id = pick_running_bot(cookie, args.bot)
    print(f"[batch] 共 {len(msgs)} 条消息，bot={bot_id}，单条等待 {args.wait}s", flush=True)

    no_reply = asyncio.run(run(cookie, bot_id, msgs, args.wait, args.cooldown, args.user))
    print("\n[summary] sent=%d replied=%d no_reply=%d" % (len(msgs), len(msgs) - len(no_reply),
                                                          len(no_reply)), flush=True)
    if no_reply:
        for t in no_reply:
            print("  无回复：%s" % t, flush=True)
    sys.exit(0 if not no_reply else 2)


if __name__ == "__main__":
    main()
