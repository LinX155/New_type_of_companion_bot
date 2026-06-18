# 人工审核：

\[√] 系统状态监控字段还得继续优化：删除last\_parsed\_text，前端上面板的全部字段名都使用中文（便于理解）
\[√] 前端优化，所有的保存按钮都得优化：点击保存之后自动置灰，下一次用户开始修改内容时恢复
\[√] 前后端优化：当用户在前端上保存配置（包括API配置）并关闭或者finish之后，下次重启应该能保留用户上次的配置（当前阶段key明文显示即可）

# 自动审查意见

## P0
- [√] COLD 冷启动元信息的 `last_user_message_age` 计算错误：`EventGate._handle_chat_message` 先把 `last_user_message_at` 覆盖为当前消息时间，再创建 snapshot，导致系统提示词几乎永远看到 `just now`，无法按设计判断离线生活态下“上次用户消息距今多久”。
- [√] `ENTER_CHAT` / `END_CHAT` 状态迁移没有完整落地：`ENTER_CHAT` 且 `text:null` 不会进入 HOT，`END_CHAT` 不会退回 COLD；当前主要依赖“COLD 状态发出可见内容后进入 HOT”，不符合 action 语义。
- [√] `MEMORY_CORE.md`、`/mem`、`/forget` 仍使用“用户长期事实 / 相处习惯 / 关系边界”三分区，和设计要求的“用户明确相处偏好 / 重要事实 / 用户交际圈 / 相处习惯 / 临时近期状态”五分区及 `[来源]: 内容` 单行格式不一致；这会直接污染核心记忆和系统提示词注入。
- [√] `REACT` 协议校验和可见渲染不完整：Pydantic 未限制 `REACT.text` 只能是 `emoji:*`、`search_meme:*` 或 `meme:*`；WebUI 对 `emoji:*` 会直接显示协议文本而不是渲染 emoji，存在把内部协议当聊天内容暴露的风险。

## 未实现 / 设计缺口

- [ ] 主动消息模块未实现：缺少 WebUI 主动消息设定/查看入口、主动消息调度、每日上限、免打扰、未回复退避、候选 `used/expired/blocked` 状态更新，以及发送前经 EventGate 做撞车和幂等检查。
- [ ] 主回复上下文没有读取 `TOMORROW_TOPICS.md`，系统提示词也没有主动消息生成链路中的软边界约束，例如不得表达强烈想念、责备、不满、等待感，不得伪造生活经历，不得利用用户脆弱点。
- [√] 系统提示词缺少“记忆使用方式”边界：自然聊天中不应显式说“根据我的记忆/来源”，记忆应该改变行为而不是审计式展示。
- [ ] Markdown 文件写入冲突保护未实现：`SOUL.md`、`MEMORY_CORE.md`、`TOMORROW_TOPICS.md`、`dm/*.md` 当前直接覆盖写入，没有写前版本检查、外部编辑冲突处理、备份或恢复机制。
- [ ] `conversation_events` / `raw_chat_log` 记录不完整：`nudge` 事件未入库；LLM 原始输出、解析状态、`search_meme` 候选/二轮选择、stale/drop 细节未完整持久化；当前 LLM 决策 raw log 存的是解析后的 action/text，不是模型原始输出。
- [ ] WebUI 表情包目录骨架未完整初始化：`memes/assets/miscellaneous/` 目录缺失，未满足 18 个固定分类目录全部落盘的要求。
- [ ] 表情包 Tab 缺少“清空某个分类”的轻管理能力；该破坏性操作按设计需要二次确认。
- [ ] Alembic 迁移和 repository/storage 边界未落地：当前通过 `Base.metadata.create_all` 建表，路由层直接操作 DB session，和 MVP 技术栈中的 SQLAlchemy 2.x + Alembic + repository 分层不一致。
