# -*- coding: utf-8 -*-
"""麦麦聊天室 · 宿主信息（工作区定位 + 受管 Bot 名单 + 我是哪个 Bot）。

**为什么需要这个**：插件上下文里**没有 bot_id**。
`ctx.paths` 只给每 Bot 自己的 ``data_dir`` / ``runtime_dir``（`maibot_sdk/context.py:147-151`），
`chat.*` / `message.*` 能力也都是 current-bot scoped。
但聊天室必须知道「**我是哪个 Bot**」——投递任务按 bot_id 分发、各 Bot 只领自己的单。

**怎么找到**：宿主是**一个进程管所有 Bot**（`maibot-control.bat` 跑
``run --directory <workspace>``），工作区根下有 ``host/registry.sqlite3``，
其 ``bots`` 表记着每个 Bot 的 ``bot_id`` 与它的工作目录（``directory``）：

| Bot | directory | 实际工作目录 |
|---|---|---|
| 麦麦一号 | ``.`` | ``<工作区>`` |
| 麦麦二号 | ``bots/ddf73be0…`` | ``<工作区>/bots/ddf73be0…`` |

⇒ 从插件自己的 ``data_dir`` **往上找**第一个含 ``host/registry.sqlite3`` 的目录 = 工作区根；
⇒ 再拿每个 Bot 的工作目录去比，**取能匹配上的最深那个**（工作区根本身也是所有人的祖先，
只比"是不是祖先"会一号二号都命中）。

**纯逻辑模块**：只用标准库 sqlite3 / pathlib ⇒ 可离线单测。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 工作区根下的宿主数据目录名
HOST_DIR_NAME = "host"

#: 工作区根下宿主注册表的文件名
REGISTRY_FILE_NAME = "registry.sqlite3"


def find_workspace_root(start: Path, max_up: int = 12) -> Optional[Path]:
    """从 ``start`` 往上找第一个含 ``host/registry.sqlite3`` 的目录。

    找不到返回 ``None``（**不抛异常** —— 定位失败不该把插件带崩，
    上层会给出「没配账本路径 / 找不到工作区」的明确提示）。
    """
    try:
        current = Path(start).resolve()
    except OSError:
        return None
    for _ in range(max_up):
        if (current / HOST_DIR_NAME / REGISTRY_FILE_NAME).is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def read_bots(workspace_root: Path) -> List[Dict[str, Any]]:
    """读宿主的受管 Bot 名单（只读）。

    - 以 **只读 URI** 打开（``mode=ro``）⇒ 绝不会误建/误改宿主文件
    - 读不到返回 ``[]``（上层据此提示"名单读不到"），**不抛异常**
    """
    db = Path(workspace_root) / HOST_DIR_NAME / REGISTRY_FILE_NAME
    if not db.is_file():
        return []
    # ⚠️⚠️ **必须用 `Path.as_uri()`**，不能手拼 `f"file:{path}"` ——
    #    Windows 上 `file:E:/Softw/...` **少一个斜杠**，`file:` URI 会被当成
    #    **相对路径** ⇒ 找不到文件 ⇒ 异常 ⇒ 名单读不出来。
    #    （Linux 上 `file:/abs/path` 合法 ⇒ 这个 bug **只在真机暴露**，离线测不出来！）
    try:
        uri = db.resolve().as_uri() + "?mode=ro"
    except (OSError, ValueError):
        return []
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT bot_id, name, directory, desired_state, initialization, error"
            " FROM bots"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(row) for row in rows]


def bot_working_dir(workspace_root: Path, directory: str) -> Path:
    """Bot 的实际工作目录。

    ``directory == "."``（麦麦一号）⇒ 就是工作区根；
    否则是 ``<工作区>/<directory>``。
    """
    rel = str(directory or ".").strip()
    if rel in ("", "."):
        return Path(workspace_root)
    return Path(workspace_root) / rel


def resolve_self_bot(workspace_root: Path, data_dir: Path) -> Optional[Dict[str, Any]]:
    """认出「我是哪个 Bot」。

    判据：``data_dir`` 落在该 Bot 的工作目录**之下**，且取**最深**的那个匹配
    （工作区根是一号的工作目录，也是二号的祖先 —— 只比祖先会一号二号都命中）。

    认不出返回 ``None``。
    """
    try:
        target = Path(data_dir).resolve()
    except OSError:
        return None
    best: Optional[Dict[str, Any]] = None
    best_depth = -1
    for bot in read_bots(workspace_root):
        work = bot_working_dir(workspace_root, str(bot.get("directory") or "."))
        try:
            work_resolved = work.resolve()
        except OSError:
            continue
        try:
            target.relative_to(work_resolved)
        except ValueError:
            continue
        depth = len(work_resolved.parts)
        if depth > best_depth:
            best_depth = depth
            best = bot
    return best


def describe_bots(bots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """整理成页面/日志好用的形状（附带运行状态的中文标签）。"""
    out: List[Dict[str, Any]] = []
    for bot in bots:
        state = str(bot.get("desired_state") or "").strip().lower()
        label = {"running": "运行中", "stopped": "未运行"}.get(state, state or "未知")
        out.append(
            {
                "bot_id": str(bot.get("bot_id") or ""),
                "bot_name": str(bot.get("name") or ""),
                "directory": str(bot.get("directory") or "."),
                "desired_state": state,
                "state_label": label,
                "error": str(bot.get("error") or ""),
            }
        )
    return out


def default_ledger_path(workspace_root: Path) -> str:
    """推荐的账本路径：工作区根下的 ``maichat/chatroom.db``。

    ⚠️ **必须在所有 Bot 的 ``data/`` 之外** —— 工作区根下新建目录，
    一号是 ``<ws>/maichat/``、二号是 ``<ws>/maichat/``（同一个，因为按工作区根算），
    两边拿到的是**同一个路径** ✓
    """
    return str(Path(workspace_root) / "maichat" / "chatroom.db")


def read_webui_port(workspace_root: Path, default: int = 8001) -> int:
    """读宿主 WebUI 的端口（maim 网关挂在同一个端口上）。

    端口在 ``<工作区>/host/config.toml`` 的 ``[webui].port``
    （默认 8001）—— **不要写死**，用户改过端口就会连错。
    读不到就退回默认值。
    """
    path = Path(workspace_root) / HOST_DIR_NAME / "config.toml"
    if not path.is_file():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default
    in_webui = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_webui = line.split("#", 1)[0].strip() == "[webui]"
            continue
        if not in_webui or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "port":
            continue
        try:
            return int(value.split("#", 1)[0].strip())
        except ValueError:
            continue
    return default


def ensure_parent_dir(db_path: str) -> None:
    """确保账本文件的父目录存在（不存在就建）。"""
    parent = os.path.dirname(os.path.abspath(str(db_path)))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def bot_config_path(workspace_root: Path, directory: str) -> Path:
    """某个 Bot 的 ``config/bot_config.toml`` 路径。"""
    return bot_working_dir(workspace_root, directory) / "config" / "bot_config.toml"


def read_bot_config(workspace_root: Path, directory: str) -> Dict[str, Any]:
    """读某个 Bot 的 ``bot_config.toml``（**只读**，读不到返回空 dict）。

    ⚠️ 02 文档 8.1 的边界表把「各 Bot 的人格 / 显示名 / 头像」列在**只读**里 ⇒
    读它没问题，**写**才是越界。
    """
    path = bot_config_path(workspace_root, directory)
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    data: Dict[str, Any] = {}
    try:
        import tomllib

        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001
        try:
            import tomli

            data = tomli.loads(text)
        except Exception:  # noqa: BLE001
            return {}
    bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
    personality = data.get("personality") if isinstance(data.get("personality"), dict) else {}
    return {
        "nickname": str(bot.get("nickname") or ""),
        "alias_names": list(bot.get("alias_names") or []),
        "personality": str(personality.get("personality") or ""),
        "reply_style": str(personality.get("reply_style") or ""),
    }
