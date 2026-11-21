# 麦麦敲门 MaiKnock

> 给 [MaiBot](https://github.com/Mai-with-u/MaiBot) 做的 WebUI 私聊伴侣插件。
> 让麦麦**主动来找你聊天**，让你**一键打招呼**，还能**由你掌管她的"现在是哪一天"**——把某段时间从她的记忆里藏起来，或直接推进剧情。

- **版本**：v0.2.0
- **插件 ID**：`maiknock.webui_companion`
- **兼容**：MaiBot 1.3.11 ~ 1.4.x，plugin-sdk 2.15+
- **协议**：MIT

---

## 一、它能做什么

| 功能 | 说明 |
|---|---|
| **F1 主动私聊** | 你沉默够久后，麦麦会主动开口找你聊；带防打扰（静默阈值、禁止时段、每日上限、最小间隔、冷却） |
| **F2 虚构时间线** | 用 `/mk time` 把故事时间往前推、往后拉、或设成某一天。回退时那段时间会从她的对话记忆里"消失" |
| **F3 手动打招呼** | `/mk hi [话头]` 立刻让麦麦开口，不必等静默计时 |

三个功能完全独立，可以只用其中一个。

---

## 二、安装

1. 把整个 `maiknock` 文件夹放到 MaiBot 的 `plugins/` 目录下：

   ```
   MaiBot/
   └── plugins/
       └── maiknock/
           ├── _manifest.json
           ├── plugin.py
           ├── config.toml
           └── ...（其余 .py 文件）
   ```

2. 启动（或重启）MaiBot。

3. 打开 **WebUI → 插件**，找到 **「麦麦敲门 MaiKnock」**，确认开关是 **启用**。

4. 在 WebUI 的 **私聊** 里给麦麦发一条消息（先建立对话）。

5. 发送 `/mk bind` 绑定当前私聊流，看到"绑定成功"即完成。

> 主动私聊默认**已开启**：静默 60 分钟后麦麦会主动开口，每天最多 5 次，禁止时段 01:00–08:00。这些都能在 WebUI 插件配置页改。

---

## 三、配置

在 **WebUI → 插件 → 麦麦敲门 → 配置** 里可视化修改（字段名是中文标签）。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `启用插件` | true | 总开关 |
| `启用主动私聊` | true | 关掉后麦麦不再主动开口，`/mk hi` 仍可用 |
| `hi_cooldown_minutes` | 3 | `/mk hi` 手动触发的冷却（分钟），防连环触发 |
| `readback_poll_seconds` | 20 | 触发后回读确认的轮询间隔（秒） |
| `readback_timeout_seconds` | 240 | 等待麦麦开口的最长回读时间（秒，超时记未响应） |
| `scan_interval_seconds` | 120 | 主动扫描间隔（秒），最小 30 |
| `silence_threshold_minutes` | 60 | 静默多久后麦麦可以主动开口（分钟） |
| `silence_lookback_hours` | 720 | 静默判定回溯窗口（小时），默认 30 天 |
| `silence_probe_hours` | 87600 | 二次探测窗口（小时），区分"沉默太久"与"从未说过话" |
| `quiet_hours_start` | 01:00 | 禁止时段开始（24 小时制） |
| `quiet_hours_end` | 08:00 | 禁止时段结束 |
| `daily_max` | 5 | 每天最多主动开口次数 |
| `min_interval_minutes` | 120 | 两次主动开口的最小间隔（分钟） |

配置保存在插件目录的 `config.toml`，也可以直接改文件（改完热更新）。

---

## 四、命令表（`/mk`）

在私聊里发送，支持 `@麦麦 /mk ...` 前缀形式。

| 命令 | 作用 |
|---|---|
| `/mk bind` | 把当前 WebUI 私聊流绑定为本插件的私聊对象（**用前先绑一次**） |
| `/mk diag` | 诊断：当前上下文、绑定状态、最近消息等 |
| `/mk hi [话头]` | 让麦麦立刻主动开口（开不开口由她决定，稍后回读汇报） |
| `/mk help` | 显示帮助 |
| `/mk now` | 时间线：恢复到现实时间 |
| `/mk time` | 时间线：查看当前状态 |

### F2 虚构时间线子命令

| 命令 | 作用 |
|---|---|
| `/mk time` | 查看当前时间线状态 |
| `/mk time add 3d` | 推进故事时间 3 天（也支持 `add 12h`、`add 90m`） |
| `/mk time back 3d` | 回退故事时间 3 天——**那段时间会从麦麦记忆里藏起来** |
| `/mk time set 2026-11-25 09:00` | 把故事时间设为某个绝对时刻 |
| `/mk time off` | 关闭时间线（恢复对话可见） |
| `/mk time now` / `/mk now` | 恢复到现实时间 |

> **只进不退的例外**：`add 3d` 是推进（决策上"只进不退"），`back` 是独立的回退子命令，用于开分支剧情，两者不冲突。六个子命令统一用单词：`add` / `back` / `set` / `off` / `now`（外加 `time` 查状态）。

**时长写法**：`3d`（天）、`12h`（小时）、`90m`（分钟）、`30s`（秒）；只写数字按小时算。

---

## 五、原理简述

### 两个 hook

插件向 MaiBot 的 Maisaka 生成器注册两个 **BLOCKING** hook，在同一个回调里改写送进模型的对话：

| Hook | 作用位置 | 改写顺序 |
|---|---|---|
| `maisaka.planner.before_request` | 规划阶段（逐条 item） | relabel（改时间） → 遮蔽（藏对话） → 注入叙事声明 |
| `maisaka.replyer.before_model_request` | 回复阶段（整块 blob） | 遮蔽（藏对话） → relabel（改时间） → 注入叙事声明 |

两侧顺序相反是刻意设计的：planner 用 `meta.timestamp` 判断（与文本无关），replyer 靠块边界的 `[HH:MM:SS]` 判断，必须先遮蔽再改块首时间，否则块边界就找不到了。

### 虚构时间线（F2）

- 落盘状态只有五个量：`mode` / `offset_sec` / `boundary_ts` / `hidden_until_ts` / `declaration`。
- **隐藏区间**是真实 epoch 上的左开右闭区间 `(boundary_ts, hidden_until_ts]`，描述"哪些真实时刻的消息要藏起来"。
- 回退后，处于隐藏区间的对话会从 planner 的 item 和 replyer 的 blob 里被移除，并被替换成一段**不含绝对日期**的叙事声明（否则麦麦会从声明里读出真实日期，穿帮）。
- 恢复 `/mk now` / `/mk time off` 时**追加一条反向声明**（不删旧声明），让麦麦知道时间线已复位。

用户验收口径：**隐藏生效时，麦麦编造一个答案是可以接受的；关键是她不会刻意说出旧答案。**

---

## 六、目录结构

```
maiknock/
├── _manifest.json        # 插件清单（名称、版本、能力声明、图标）
├── icon.png              # 插件图标（512×512）
├── config.toml           # 当前配置
├── plugin.py             # 插件本体：hook、命令、配置模型
├── timeline.py           # F2 纯逻辑状态模型（无 IO、可离线单测）
├── date_forms.py         # 日期形态表（声明断言与脱敏共用）
├── matcher.py            # 真实 blob 切块与消息匹配
├── blob_processor.py     # replyer 侧遮蔽 + relabel
├── planner_processor.py  # planner 侧遮蔽 + relabel
├── declaration.py        # 叙事声明生成与注入
├── carriers.py           # relabel 载体白名单
└── tests/                # 单元测试
```

---

## 七、常见问题

**Q：发了 `/mk hi` 麦麦没反应？**
A：命令本身不进聊天管线（由系统直发），是否开口由麦麦当时的生成决定；插件会在 `readback_timeout_seconds` 内回读汇报结果。若处于禁止时段或已到每日上限，不会触发。

**Q：改了配置没生效？**
A：WebUI 配置保存后会热更新；直接改 `config.toml` 也是。若改了字段结构，重启一次插件。

**Q：时间线回退后想恢复？**
A：`/mk now` 或 `/mk time off`，会追加反向声明并清空隐藏区间。

**Q：升级 MaiBot 后插件报错？**
A：检查兼容范围（1.3.11 ~ 1.4.x）。1.4.8 上 replyer 侧只有一个 blob 路径（`reply_prompt.py`），本插件已适配。

---

*Made with ❤️ for 麦麦。*
