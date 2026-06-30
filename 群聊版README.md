# 群聊版 README

本文只记录群聊版运行时的提示词、SOUL、记忆和 TOPIC 文件位置。群聊版和私聊版共享同一个 `start` 入口，但运行时、prompt、记忆和发送策略是隔离的。

## 群聊主提示词

群聊主回复决策的系统提示词在：

- `app/llm/prompts.py`
  - `GROUP_CHAT_SYSTEM_PROMPT`
  - `build_group_chat_messages(...)`

调用入口在：

- `app/api/routes.py`
  - `_run_group_reply_decision(...)`

这一路只用于群聊回复判断。它不读取私聊 `SOUL.md`，不使用私聊 HOT / COLD、用户输入状态拦截和私聊主 graph。模型可见 payload 主要包含：

- `trigger`
- `group_window`
- `confirmed_qid_to_nickname`
- `group_memory`
- `output_contract`

## 群聊图片与表情包提示词

普通图片理解使用共享图片理解提示词：

- `app/llm/prompts.py`
  - `build_image_understanding_messages(...)`

群聊调用路径：

- `app/api/routes.py`
  - `_prepare_group_image_understanding_for_window(...)`
- `app/core/media_jobs.py`
  - `process_inline_image_for_prompt(...)`
  - `_build_image_understanding_payload(...)`

表情包后台偷取和入库使用内部提示词，不属于群聊主模型可见协议：

- `app/llm/prompts.py`
  - `build_image_understanding_messages(...)`
  - `build_meme_steal_analysis_messages(...)`
- `app/core/media_jobs.py`
  - `_build_meme_intake_payload(...)`
- `app/memes/steal.py`
  - `MemeStealAnalyzer.analyze(...)`

表情包结果只以极简 meme 事件进入群聊窗口：已入库是文件 stem，未入库是 `unknown`。不使用 `<meme:...>` 这类尖括号文本协议。

## 群聊 SOUL

当前没有独立的群聊 `SOUL.md` 文件。

群聊人设直接写在 `app/llm/prompts.py` 的 `GROUP_CHAT_SYSTEM_PROMPT` 中。它明确要求“小夏不是任何人的女友”，并且和私聊身份隔离。

私聊 SOUL 文件仍在仓库根目录：

- `SOUL.md`

这个文件是私聊主 graph 使用的角色文件，不进入群聊 prompt。

## 群聊记忆

群聊记忆文件是每个群一个，路径格式：

- `memory/sessions/qq_group_<group_id>/GROUP_MEMORY.md`

代码入口：

- `app/core/group_memory.py`
  - `GroupMemoryManager`
  - `GROUP_MEMORY_TEMPLATE`

固定分区：

- `## 群友身份`
- `## 共同记忆`
- `## 个人相关记忆`

身份规则：

- q号是工程层和记忆层的稳定身份索引。
- 平台群名片 / 平台昵称不写入 prompt、记忆、历史或输出。
- 只有 `GROUP_MEMORY.md` 的“群友身份”里存在 `q号:昵称` 时，群聊 prompt 才会拿到 `nickname`。
- `/mem` 和 `/forget` 只允许操作发起者自己的身份信息，不能替别人写或删。

## 群聊 TOPIC

当前没有独立的群聊 TOPIC 文件。

私聊 TOPIC 文件仍按原逻辑存在：

- 默认/旧全局：`TOMORROW_TOPICS.md`
- 私聊 session：`memory/sessions/<session_id>/TOMORROW_TOPICS.md`

代码入口：

- `app/memory/files.py`
  - `MemoryFileManager`
  - `TOMORROW_TOPICS_TEMPLATE`
- `app/scheduler/jobs.py`
  - 主动消息、记忆整理和 TOPIC 维护逻辑

注意：当前群聊 session `qq_group_<group_id>` 被主动消息调度排除，不会读取私聊 TOPIC 去主动发群消息。群聊自己的“活跃时间”不是 TOPIC 文件，而是运行时统计和 raw log：

- `app/core/group_activity.py`
  - `GroupActivityTracker`
- `app/api/routes.py`
  - `run_group_activity_update(...)`

## 群聊 session 与文件目录

群聊 session id 格式：

- `qq_group_<group_id>`

代码入口：

- `app/core/sessions.py`
  - `qq_group_session_id(...)`
  - `infer_identity(...)`
  - `SessionRegistry.session_dir(...)`

对应目录：

- `memory/sessions/qq_group_<group_id>/`

目前群聊主要落盘文件是：

- `GROUP_MEMORY.md`

私聊的 `MEMORY_CORE.md`、`TOMORROW_TOPICS.md`、`dm/` 文件仍属于私聊主链路，不是群聊主回复的记忆来源。
