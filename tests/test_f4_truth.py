# -*- coding: utf-8 -*-
"""MaiKnock F-12 开关真值解析的离线单测（``plugin.py::_effective_style_guard``）。

背景（0.2.3 回归轮 2026-11-24 实测·P1）：宿主 ``config.get_plugin`` 返回的是
它自己读文件后的**缓存段**（保留 blocks 未知键、缺失新键不填默认值），且
watchfiles 丢变更通知时缓存**陈旧是永久的** ⇒ F4 开关真值一律以原始
config.toml 为准。本文件锁定合成优先级：

  1. 原文写了新键（内心想法/动作环境）→ 以原文为准（缺侧缺省 True）；
  2. 原文只有旧版 [style_guard.blocks] → blocks 旧键接管（F-10）；
  3. 原文读不到 / 无 [style_guard] 段 → 回退宿主段。

手法：monkeypatch 模块级 ``Path`` 把 ``Path(__file__).parent / "config.toml"``
指到临时目录，真实插件 config.toml 全程不被触碰。

运行（宿主解释器，禁写 .pyc）：
    "C:\\Users\\29411\\AppData\\Roaming\\uv\\tools\\maibot\\Scripts\\python.exe" test_f4_truth.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import unittest.mock as mock
from pathlib import Path

PLUG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUG))

spec = importlib.util.spec_from_file_location("mk_plugin_f4truth", PLUG / "plugin.py")
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except Exception as exc:  # noqa: BLE001
    # F-13：SKIP 必须**非零退出**——退出码 0 会让 CI/统计脚本误判「通过」，
    # 数 PASS 行只会得到「少跑了 13 项」的假象（回归轮二实测踩坑）
    print("SKIP  plugin.py 离线导入失败（缺宿主依赖）：%r" % (exc,))
    sys.exit(2)

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, extra))


def make_plugin():
    """不跑 __init__（会碰宿主 API），只造一个带 F-12 状态量的裸实例。"""
    p = mod.MaiKnockPlugin.__new__(mod.MaiKnockPlugin)
    p._f4_raw_cache = None
    p._f4_legacy_warn_key = None
    return p


class _PathShim:
    """把 ``Path(__file__).parent / "config.toml"`` 重定向到临时目录。"""

    def __init__(self, target):
        self._target = Path(target)

    @property
    def parent(self):
        return _PathShim(TMP)

    def __truediv__(self, name):
        return TMP / name


TMP = Path(tempfile.mkdtemp(prefix="mk_f4truth_"))
CFG = TMP / "config.toml"


def write_cfg(text):
    CFG.write_text(text, encoding="utf-8")
    time.sleep(0.02)  # 确保 mtime 指纹变化


def run_case(p, host_sg):
    with mock.patch.object(mod, "Path", _PathShim):
        p._f4_raw_cache = None
        return p._effective_style_guard(host_sg)


print("=== 1. 原文新键优先（吃掉陈旧宿主段的 blocks）===")
p = make_plugin()
write_cfg('[style_guard]\nenabled = true\n"内心想法" = false\n"动作环境" = true\n')
host_stale = {"enabled": True, "blocks": {"inner_enabled": True, "action_enabled": True}}
sg = run_case(p, host_stale)
inner, action = mod.MaiKnockPlugin._f4_blocks(sg)
check("陈旧宿主段 blocks(inner=True) 被原文新键(false) 覆盖", (inner, action) == (False, True),
      (inner, action))
check("其余宿主字段保留（enabled 透传）", sg.get("enabled") is True)

print()
print("=== 2. 原文只写了单侧新键 → 缺侧按规格缺省 True（不吃陈旧 legacy 值）===")
p = make_plugin()
write_cfg('[style_guard]\nenabled = true\n"动作环境" = false\n')
sg = run_case(p, host_stale)
inner, action = mod.MaiKnockPlugin._f4_blocks(sg)
check("内心缺省 True（不被陈旧 blocks 污染）", inner is True, inner)
check("动作环境 = 原文 false", action is False, action)

print()
print("=== 3. 原文只有旧版 [style_guard.blocks] → 接管（F-10）===")
p = make_plugin()
write_cfg('[style_guard]\nenabled = true\n\n[style_guard.blocks]\ninner_enabled = false\n')
sg = run_case(p, host_stale)
inner, action = mod.MaiKnockPlugin._f4_blocks(sg)
check("blocks.inner_enabled=false 接管生效", (inner, action) == (False, True), (inner, action))

print()
print("=== 4. F-12 主场景：宿主缓存陈旧 + 原文已迁新格式 → 真值跟原文走 ===")
p = make_plugin()
# 宿主段 = 陈旧缓存（还是旧格式 era 的 blocks）；原文 = 用户手写的新键
write_cfg('[style_guard]\nenabled = true\n"内心想法" = true\n"动作环境" = true\n')
sg = run_case(p, host_stale)
inner, action = mod.MaiKnockPlugin._f4_blocks(sg)
check("接管正确退出（宿主段陈旧也不影响）", (inner, action) == (True, True), (inner, action))

print()
print("=== 5. 原文读不到 / 无 [style_guard] 段 → 回退宿主段 ===")
p = make_plugin()
CFG.unlink(missing_ok=True)
sg = run_case(p, {"enabled": True, "内心想法": False})
inner, action = mod.MaiKnockPlugin._f4_blocks(sg)
check("文件缺失 → 宿主段兜底", (inner, action) == (False, True), (inner, action))
p = make_plugin()
write_cfg('[plugin]\nenabled = true\n')  # 无 [style_guard] 段
sg = run_case(p, {"enabled": True, "动作环境": False})
inner, action = mod.MaiKnockPlugin._f4_blocks(sg)
check("无 [style_guard] 段 → 宿主段兜底", (inner, action) == (True, False), (inner, action))

print()
print("=== 6. mtime 指纹缓存：文件没变不重读，变了真值跟着变 ===")
p = make_plugin()
write_cfg('[style_guard]\n"内心想法" = false\n"动作环境" = false\n')
sg1 = run_case(p, host_stale)
check("初读：inner=False action=False",
      mod.MaiKnockPlugin._f4_blocks(sg1) == (False, False))
cached = p._f4_raw_cache
check("指纹已缓存", cached is not None and isinstance(cached[1], dict))
with mock.patch.object(mod, "Path", _PathShim):
    sg2 = p._effective_style_guard(host_stale)
check("mtime 未变 → 复用缓存（真值一致）",
      mod.MaiKnockPlugin._f4_blocks(sg2) == (False, False) and p._f4_raw_cache == cached)
write_cfg('[style_guard]\n"内心想法" = true\n"动作环境" = true\n')
with mock.patch.object(mod, "Path", _PathShim):
    sg3 = p._effective_style_guard(host_stale)
check("mtime 变了 → 真值跟着原文更新", mod.MaiKnockPlugin._f4_blocks(sg3) == (True, True))

print()
print("=== 7. 真实插件 config.toml 冒烟（当前是新格式 → 不触发接管路径）===")
p = make_plugin()
real = mod.MaiKnockPlugin._effective_style_guard(p, {"enabled": True, "blocks": {"inner_enabled": True}})
inner, action = mod.MaiKnockPlugin._f4_blocks(real)
check("真实配置读取不抛异常且开关为布尔", isinstance(inner, bool) and isinstance(action, bool),
      (inner, action))

print()
print("=" * 46)
print("通过 %d / 失败 %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
