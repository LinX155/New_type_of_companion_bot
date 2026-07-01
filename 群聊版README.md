# 群聊版 README

只保留调试时最常用的位置索引。

## 系统提示词

- 群聊主回复 prompt：`app/llm/prompts.py`
  - `GROUP_CHAT_SYSTEM_PROMPT`
  - `build_group_chat_messages(...)`
- 普通图片理解 prompt：`app/llm/prompts.py`
  - `build_image_understanding_messages(...)`
- 表情包偷取 / 入库分析 prompt：`app/llm/prompts.py`
  - `build_meme_steal_analysis_messages(...)`
- 群聊记忆线程 prompt：`app/llm/prompts.py`
  - `build_group_memory_analysis_messages(...)`
  - `build_group_midnight_cleanup_messages(...)`
  - `build_group_mem_command_messages(...)`
  - `build_group_forget_command_messages(...)`
- 群聊主动消息 prompt：`app/llm/prompts.py`
  - `build_group_active_message_messages(...)`
- 群聊长上下文 checkpoint prompt：`app/llm/prompts.py`
  - `build_group_context_checkpoint_messages(...)`
- 私聊 `/mem` / `/forget` prompt：`app/llm/prompts.py`
  - `build_mem_command_messages(...)`
  - `build_forget_command_messages(...)`
  - 仅私聊记忆使用，群聊不会复用。

## 记忆入口

- 群聊记忆文件、命令校验、LLM 结果合并：`app/core/group_memory.py`
  - `/mem`、`/forget` 是 LLM 驱动，规则层只做硬边界预检、输出校验和 fallback。
  - 普通文本身份观察只产候选，不直接写长期 CORE。
- 群聊日间记忆 / 凌晨整理 / checkpoint 调度：`app/scheduler/jobs.py`

## SOUL

- 群聊 SOUL：`GROUP_SOUL.md`
- 群聊主 prompt 每轮通过 `build_group_chat_messages(...)` 注入 `group_soul`。
- 根目录 `SOUL.md` 是私聊用的，不进入群聊 prompt。

## 记忆与 TOPIC 文件

- 群聊记忆：`memory/sessions/qq_group_<group_id>/GROUP_MEMORY.md`
- 群聊日记忆：`memory/sessions/qq_group_<group_id>/dm/YYYY-MM-DD.md`
- 群聊 TOPIC：`memory/sessions/qq_group_<group_id>/TOMORROW_TOPICS.md`
- 群聊活跃时间：`memory/sessions/qq_group_<group_id>/GROUP_ACTIVITY.json`
- 群聊 context checkpoint：SQLite `context_checkpoints` 表，`session_id=qq_group_<group_id>`。
- 日间记忆写 dm，并维护当前群自己的 `TOMORROW_TOPICS.md`；凌晨整理和通过校验后的 `/mem` / `/forget` 才写 `GROUP_MEMORY.md`。
- 群聊记忆链路不写私聊 `MEMORY_CORE.md`，群聊主动消息不读取私聊 TOPIC。
- 私聊 TOPIC：`TOMORROW_TOPICS.md` 或 `memory/sessions/<session_id>/TOMORROW_TOPICS.md`，只服务私聊。

## 调试配置入口

- 群聊发送 / 观察期 / 各能力开关默认值：`app/core/group_send.py`
  - `DEFAULT_GROUP_SEND_CONFIG`
- WebUI 群聊运营开关：`webui/src/components/GroupChatOpsConfig.tsx`
  - `允许主动消息` 默认关闭，需要显式开启。
- 群聊运营开关 API：`app/api/routes.py`
  - `GET /api/group-chat/config`
  - `POST /api/group-chat/config`
- 群聊发送与审计链路：`app/api/routes.py`
  - `_send_group_reply_candidate(...)`
  - `_record_onebot_group_send(...)`
  - `group_send_audit`
- 群聊 roll 参数：`app/core/group_scheduler.py`
  - `DEFAULT_GROUP_ROLL_CONFIG`
- 群聊活跃时间参数：`app/core/group_activity.py`
  - `DEFAULT_GROUP_ACTIVITY_CONFIG`
  - `GROUP_ACTIVITY.json` 记录 EMA、active/quiet/normal 小时和参数快照。
  - WebUI 群聊状态里的 `group_activity.profile_path` / `profile_persisted` 可直接看落盘状态。
- 群聊复读参数：`app/core/group_repetition.py`
  - `DEFAULT_GROUP_REPETITION_CONFIG`
- 群聊记忆模板与命令规则：`app/core/group_memory.py`
  - `GROUP_MEMORY_TEMPLATE`
  - `GroupMemoryManager`
- Context checkpoint 阈值：WebUI 的 context checkpoint 配置卡片，底层配置键是 `context_checkpoint.threshold_k`。
