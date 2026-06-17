# 第一阶段 WebUI 审查文档（LEFT_TODO）

> 审查日期：2026-06-17  
> 审查范围：项目根目录下 5 份中文设计文档对第一阶段 WebUI 的全部要求  
> 本文档只记录未完成、待完善或已延后的项，已完成项已删除。

---

## 一、P0：第一阶段 WebUI 验证前必须补齐

| # | 问题 | 来源 | 现状 | 涉及文件 |
|---|------|------|------|----------|
| 1 | **表情包 Tab 顶部状态缺少“固定 18 类数量”** | `开发计划.md` 要求顶部状态展示：表情总数、固定 18 类数量、数据目录。 | 当前只显示总数、数据目录、过滤记录，未展示 18 类数量或各分类数量。 | `webui/src/components/MemeManager.tsx` |
| 2 | **过滤摘要缺少原因统计** | `开发计划.md` 要求展示 filtered 数量和原因统计，不展示被过滤原图。 | 前端只展示 `count`，后端 `/api/memes/filtered` 已返回 `items` 但未使用。 | `webui/src/components/MemeManager.tsx` |
| 3 | **前端 `ChatWindow` 中 `renderMemeUrl` 函数未使用** | 工程整洁性。 | 函数已写但无调用，渲染直接走 `/api/memes/render?stem=`。 | `webui/src/components/ChatWindow.tsx` |

---

## 二、P1：第一阶段应完善但未阻塞

| # | 问题 | 来源 | 现状 | 涉及文件 |
|---|------|------|------|----------|
| 4 | **后端 `RawChatLog` 字段覆盖不完整** | `总框架与技术栈选型.md` 5.3 raw chat log 应记录完整审计字段。 | 命令响应和错误日志分支缺少 `input_text`/`llm_raw_output` 等字段。 | `app/api/routes.py` |
| 5 | **记忆文件写入缺少冲突处理** | `总框架与技术栈选型.md` 第 8 节文件写入和冲突处理要求写入前检查文件是否被修改/被打开。 | `MemoryFileManager` 直接覆盖写入，无版本检查、无备份。 | `app/memory/files.py` |
| 6 | **热聊状态 `hot_until` 重启后丢失** | 工程状态一致性。 | 状态仅存内存，进程重启后 `hot_until` 丢失，状态回退 COLD。 | `app/core/event_gate.py`, `app/core/state.py` |
| 7 | **LangGraph 主图缺少 `apply_interaction_flags` 和 `persist_log` 独立节点** | `总框架与技术栈选型.md` 4.4 推荐最小主图包含这两个节点。 | 当前节点更简化，交互标志在 `SnapshotManager` 中处理，日志在 `routes.py` 中处理。 | `app/core/graph.py` |
| 8 | **Alembic 迁移未初始化** | `总框架与技术栈选型.md` 5.2 要求使用迁移工具管理表结构。 | 无 `alembic/` 目录或迁移脚本。 | 无 |
| 9 | **目录结构未按推荐拆分** | `总框架与技术栈选型.md` 13 推荐目录结构。 | 缺少 `adapters/`, `memory/daily.py`, `storage/repositories.py` 等子模块。 | `app/memory/`, `app/storage/` |
| 10 | **表情包降级方式单一** | `表情包系统设计.md` 3.9 要求候选为空或精确 ID 不存在时降级为短文本/emoji/静默。 | 当前多处分支降级为固定 `LIGHT_ACK "嗯"`，不够自然。 | `app/core/graph.py` |
| 11 | **`conversation_history` 记录 `meme:xxx` 协议文本** | 工程上下文质量。 | 长期对话历史中混有 `meme:` 协议字符串，可能影响后续整理和上下文理解。 | `app/core/graph.py` |

---

## 三、P2：已明确延后或超出第一阶段范围

| # | 问题 | 来源 | 说明 | 计划阶段 |
|---|------|------|------|----------|
| 12 | **主动消息调度系统** | `总框架与技术栈选型.md` 7.3 / `情感陪伴聊天机器人产品设计框架.md` 17 | 仅有 `daily_active_message_count` 字段占位，无上限、免打扰、撞车、候选生命周期等逻辑。 | 第二阶段 |
| 13 | **`steal_meme` 偷表情工具** | `表情包系统设计.md` 3.4~3.5 | 文件结构已预留（`image_dhash_index.json`、`filtered.json`），但无工具实现、路径白名单、dHash 去重、写锁。 | 平台接入阶段 |
| 14 | **平台适配层（QQ/微信）** | `总框架与技术栈选型.md` 9 | 无 `adapters/` 目录。 | 第二阶段 |
| 15 | **WebUI 查看 `MEMORY_CORE.md` / 当天 `dm` / `TOMORROW_TOPICS.md` / raw log / 后台任务状态** | `总框架与技术栈选型.md` 10 | 第一阶段最小功能未覆盖这些观测项。 | 后续扩展 |
| 16 | **测评体系** | `情感陪伴聊天机器人产品设计框架.md` 20 | 设计文档明确暂不展开。 | 后续 |

---

## 四、一句话结论

第一阶段 WebUI 核心对话验证、表情包链路、状态机、记忆文件模块已跑通；当前阻塞项主要是 **2 项前端表情包 Tab 展示项** 和 **1 项代码整洁性小项**。P1 工程完善项建议第一阶段收尾时顺手补齐，P2 延后项按设计文档进入后续阶段实现。

---

## 五、调试使用的 MVP 状态监控栏

> 目标：在对话窗口右侧提供只展示“当前 / 最近一次”状态的调试面板，不展示历史 log，便于逐条观察主回复对象（LLM）和发送权对象（EventGate）的行为。
> 依据：`情感陪伴聊天机器人产品设计框架.md` 3.4 / 3.5，`总框架与技术栈选型.md` 4.3 / 4.4 / 4.5。

### 5.1 面板 1：当前 Snapshot / 主回复对象

公共头部（`COLD` / `HOT` 都显示）：

| 字段 | 说明 |
|------|------|
| `snapshot_id` | 当前快照 ID |
| `buffer_version` | 当前 buffer 版本 |
| `status` | `COLD` / `HOT` |
| `buffered_events` | 缓冲区事件数 |
| `memory_sources` | 本次注入：SOUL / MEMORY_CORE / dm |

#### 状态 A：`COLD` — 冷启动上下文

标注“以下 cold_start_meta 会传入 LLM”：

| 字段 | 说明 |
|------|------|
| `cold_start_meta.timestamp` | 当前时间 |
| `cold_start_meta.last_user_message_age` | 距离上条用户消息多久 |
| `cold_start_meta.msg_index` | 当天用户消息序号 |
| `cold_start_meta.status` | `COLD` |

#### 状态 B：`HOT` — 热聊上下文

标注“热聊状态，只传递普通聊天上下文，不传递冷启动元信息”：

| 字段 | 说明 |
|------|------|
| `hot_until` / `hot_remaining` | 热聊到期时间和剩余分钟 |
| `context_note` | 当前不注入 cold_start_meta |

#### LLM 决策结果（`COLD` / `HOT` 都显示）

| 字段 | 说明 |
|------|------|
| `last_llm_raw` | 最近一次 LLM 原始 JSON 输出 |
| `last_parsed_action` | 解析后的 action |
| `last_parsed_text` | 解析后的 text |
| `parse_status` | `ok` / `fallback` / `error` |
| `decision_result` | `sent` / `dropped` / `stale_dropped` / `error` |

---

### 5.2 面板 2：实时事件门 / 发送权

| 字段 | 说明 |
|------|------|
| `pending_job_id` | 当前 pending 的 LLM 任务 |
| `buffered_events` | 缓冲区事件数 |
| `stale_jobs_count` | 已作废任务数 |
| `sent_jobs_count` | 已发送任务数 |

---

### 5.3 面板 3：Meme / 命令链路（可选，仅在相关时展开）

| 字段 | 说明 |
|------|------|
| `search_meme` | 第一轮请求，如 `search_meme:amused:funny` |
| `candidates` | 返回候选 file_stem 列表 |
| `selected_meme` | 选中的 file_stem |
| `render_status` | `hit` / `miss` / `fallback` |
| `last_command` | 最近 `/mem` / `/forget` |
| `command_result` | `success` / `error` |

---

### 5.4 设计要点

1. **只展示当前 / 最近一次**：不展示历史 log，适合逐条观察状态。
2. **按 `COLD` / `HOT` 切换显示**：`msg_index` 和 `last_message_age` 只在 `COLD` 时展示，避免误判 LLM 实际输入。
3. **保持扁平化**：三个面板均为单层卡片，不嵌套，符合项目 UI 设计原则。
4. **后端配合**：需扩展 `/api/status` 暴露 `cold_start_meta`、`last_llm_raw`、`parse_status`、`decision_result`、`meme_candidates`、`selected_meme`、`render_status` 等字段。
