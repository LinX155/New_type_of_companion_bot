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
- 私聊 `/mem` / `/forget` prompt：`app/llm/prompts.py`
  - `build_mem_command_messages(...)`
  - `build_forget_command_messages(...)`
  - 仅私聊记忆使用，群聊不会复用。

## 记忆入口

- 群聊记忆文件、命令校验、LLM 结果合并：`app/core/group_memory.py`
  - `/mem`、`/forget` 是 LLM 驱动，规则层只做硬边界预检、输出校验和 fallback。
  - 普通文本身份观察只产候选，不直接写长期 CORE。
- 群聊日间记忆 / 凌晨整理调度：`app/scheduler/jobs.py`

## SOUL

- 群聊版目前没有独立 `SOUL.md`。
- 群聊人设直接写在 `app/llm/prompts.py` 的 `GROUP_CHAT_SYSTEM_PROMPT`。
- 根目录 `SOUL.md` 是私聊用的，不进入群聊 prompt。

## 记忆与 TOPIC 文件

- 群聊记忆：`memory/sessions/qq_group_<group_id>/GROUP_MEMORY.md`
- 群聊日记忆：`memory/sessions/qq_group_<group_id>/dm/YYYY-MM-DD.md`
- 日间记忆写 dm；凌晨整理和通过校验后的 `/mem` / `/forget` 才写 `GROUP_MEMORY.md`。
- 群聊记忆链路不写私聊 `MEMORY_CORE.md`。
- 群聊目前没有独立 TOPIC 文件。
- 私聊 TOPIC：`TOMORROW_TOPICS.md` 或 `memory/sessions/<session_id>/TOMORROW_TOPICS.md`，群聊不会读取它主动发群消息。

## 调试配置入口

- 群聊发送 / 观察期 / 各能力开关默认值：`app/core/group_send.py`
  - `DEFAULT_GROUP_SEND_CONFIG`
- WebUI 群聊运营开关：`webui/src/components/GroupChatOpsConfig.tsx`
- 群聊运营开关 API：`app/api/routes.py`
  - `GET /api/group-chat/config`
  - `POST /api/group-chat/config`
- 群聊 roll 参数：`app/core/group_scheduler.py`
  - `DEFAULT_GROUP_ROLL_CONFIG`
- 群聊活跃时间参数：`app/core/group_activity.py`
  - `DEFAULT_GROUP_ACTIVITY_CONFIG`
- 群聊复读参数：`app/core/group_repetition.py`
  - `DEFAULT_GROUP_REPETITION_CONFIG`
- 群聊记忆模板与命令规则：`app/core/group_memory.py`
  - `GROUP_MEMORY_TEMPLATE`
  - `GroupMemoryManager`
