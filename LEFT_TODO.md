# 人工审查意见：

\[√] 系统状态监控字段还得继续优化：删除last\_parsed\_text，前端上面板的全部字段名都使用中文（便于理解）
\[√] 前端优化，所有的保存按钮都得优化：点击保存之后自动置灰，下一次用户开始修改内容时恢复
\[√] 前后端优化：当用户在前端上保存配置（包括API配置）并关闭或者finish之后，下次重启应该能保留用户上次的配置（当前阶段key明文显示即可）

# 自动审查意见

## P0
- [√] 混合多消息底层协议已落地：当前协议从 `action + text` 兼容式升级为 `action + items`，解决“用户连续发多条，对方只能回一条”和“文字/emoji/表情包不能混合回复”的聊天感失真。
  - 核心原则：一次决策，多条有序发送单元；不是主动消息，也不是多次调用 LLM。
  - 新主协议为 `items: [{type, content}] | null`；旧 `text: string | string[] | null` 仅作为兼容入口，内部统一归一化为 `items`。
  - `WAIT` / `END_CHAT` 无可见 `items`。
  - `REPLY` / `ENTER_CHAT` 可混合 `text`、`emoji`、`meme`、`search_meme`。
  - `LIGHT_ACK` 仍由 prompt 约束为短、轻、少，不做硬数量截断。
  - `REACT` 是纯表情动作，可包含一个或多个 `emoji`、`meme`、`search_meme`，不能混入普通文本。
  - 可见发送单元、表情包数量、`search_meme` 数量均不设产品硬上限；仍受模型输出、JSON 解析和平台发送能力等工程边界约束。
  - `search_meme` 同轮批量解析：系统一次性检索所有 search item，再由第二轮 LLM 输出最终 `items`，用 `meme:<file_stem>` 替换内部 search item。
  - 不由发送层按标点自动拆句；是否拆成多条、哪里插入表情，由 LLM 在同一个决策时刻决定。
  - 发送层按 `items[0] -> items[1] -> ...` 顺序发送，不额外制造固定延迟。
  - 同一组 items 绑定同一个 `job_id` 和 `snapshot_id`；每发送一个 item 前都检查 snapshot 是否仍然有效。
  - 如果发送中途出现新的用户消息、撤回、戳一戳或其他会改变 buffer 的平台事件，剩余 items 立即停止发送，旧结果按 stale/drop 处理。
  - 幂等键继续使用 `job_id + snapshot_id + send_index`，`send_index` 表示同一轮里的第几个发送单元。
  - WebUI 支持同一轮响应中连续渲染文本、emoji 和表情包，并在调试面板展示 `job_id` / `snapshot_id` / `send_index` / `item_type`。
- [√] COLD 冷启动元信息的 `last_user_message_age` 计算错误：`EventGate._handle_chat_message` 先把 `last_user_message_at` 覆盖为当前消息时间，再创建 snapshot，导致系统提示词几乎永远看到 `just now`，无法按设计判断离线生活态下“上次用户消息距今多久”。
- [√] `ENTER_CHAT` / `END_CHAT` 状态迁移没有完整落地：`ENTER_CHAT` 且 `text:null` 不会进入 HOT，`END_CHAT` 不会退回 COLD；当前主要依赖“COLD 状态发出可见内容后进入 HOT”，不符合 action 语义。
- [√] `MEMORY_CORE.md`、`/mem`、`/forget` 仍使用“用户长期事实 / 相处习惯 / 关系边界”三分区，和设计要求的“用户明确相处偏好 / 重要事实 / 用户交际圈 / 相处习惯 / 临时近期状态”五分区及 `[来源]: 内容` 单行格式不一致；这会直接污染核心记忆和系统提示词注入。
- [√] `REACT` 协议校验和可见渲染不完整：Pydantic 未限制 `REACT.text` 只能是 `emoji:*`、`search_meme:*` 或 `meme:*`；WebUI 对 `emoji:*` 会直接显示协议文本而不是渲染 emoji，存在把内部协议当聊天内容暴露的风险。

## 未实现 / 设计缺口

- [√] 主动消息模块未实现：缺少 WebUI 主动消息设定/查看入口、主动消息调度、每日上限、免打扰、未回复退避、候选 `used/expired/blocked` 状态更新，以及发送前经 EventGate 做撞车和幂等检查。
- [√] 主回复上下文没有读取 `TOMORROW_TOPICS.md`。
- [√] 系统提示词缺少“记忆使用方式”边界：自然聊天中不应显式说“根据我的记忆/来源”，记忆应该改变行为而不是审计式展示。

## P2 / 轻量优化

- [ ] 凌晨整理线程 prompt 增加“共同梗 / 暗号 / 昵称 / 专属表情含义”的轻提示：不新增独立模块，不新增关系系统；只把长期稳定、自然反复出现或用户通过 `/mem` 明确要求的共同语境，克制写入 `MEMORY_CORE.md` 的“相处习惯”；长期未出现、用户否定或明显过期的内容应删除或降权；主回复使用时避免机械复读和显式炫耀记忆。
- [ ] 重构 COLD/HOT 进入规则以增加 WAIT 机会：COLD 状态下的普通可见 `REPLY` / `LIGHT_ACK` / `REACT` 先按 one-shot reply 处理，不自动进入 HOT；只有 `ENTER_CHAT`、拍一拍 / 戳一戳、或后续明确形成连续互动时才进入 HOT。同步收窄 prompt 中 `ENTER_CHAT` 的触发条件，补充 COLD 下 `WAIT` 的正向示例，避免模型把“刚看到消息”误判成“必须立刻进入热聊”。该改动涉及状态迁移、prompt 和测试，先作为 P2 重构项保留。
