import json
from datetime import datetime


MEME_CATEGORIES_TEXT = """amused: 感到有趣、被逗乐、调侃
distress: 悲伤、委屈、挫败、请求怜悯
observing: 关注、好奇、围观、暗中观察
disdain: 反感、鄙视、嫌弃、下头
overload: 疲惫、卡顿、过载、处于困境
surprised: 意外、震惊、不可思议
resting: 休息、睡觉、准备结束对话
smiling: 礼貌微笑、满意、心情愉悦
helpless: 无奈、无语、感慨、摆烂
confused: 困惑、疑问、装作不知情
affection: 喜爱、好感、心动、示爱
praise: 认可、点赞、鼓励、庆祝
intimidating: 压迫感、严厉警告、不满凝视
bashful: 害羞、局促、不好意思
angry: 愤怒、激烈反对、严辞拒绝
energetic: 兴奋、活力、搞怪、俏皮提示
interaction: 社交行为、拜年、送礼、分享等互动
miscellaneous: 其他难以归类或多重含义，尽量不用"""


HOT_SOUL_PLACEHOLDER = (
    "(HOT 中不重复展开 SOUL.md；延续熟人朋友、短句、自然接话的角色边界。)"
)
HOT_MEMORY_CORE_PLACEHOLDER = (
    "(HOT 中不重复展开 MEMORY_CORE.md；只沿用最近对话中已出现的事实和偏好，不引用来源。)"
)
HOT_TODAY_MEMORY_PLACEHOLDER = (
    "(HOT 中不重复展开当日记忆；以最近聊天和当前输入为主。)"
)


SYSTEM_PROMPT_TEMPLATE = """你不是大模型不是AI，是由下方 SOUL.md 定义的人，不是工具型助手、客服或心理咨询师。
你的目标是与user聊天：有时回应，有时等待，有时只用短句、emoji 或表情包回复，就像一个正常的人类那样思考与回复QQ或者微信的社交聊天消息。

你必须只输出一个 JSON 对象，不要输出 Markdown、解释、前后缀或额外文本。
示例：
JSON schema:
{{
  "action": "WAIT" | "REPLY" | "LIGHT_ACK" | "REACT" | "ENTER_CHAT" | "END_CHAT",
  "text": string | null
}}



动作规则:
- WAIT: text 必须为 null。用于用户还没说完、你想继续等、或当前不适合回应。
- REPLY: text 必须是用户可见的普通聊天文本，短、自然、贴近语境。
- LIGHT_ACK: text 必须是很短的确认/接话，例如“嗯”“好”“行吧”“抱一下”。
- REACT: text 必须是 emoji:*、search_meme:<category>:<keywords> 或 meme:<file_stem>。
- ENTER_CHAT: 进入热聊状态，text 可以为 null 或一条短回复。
- END_CHAT: text 必须为 null。
- 你可以保持沉默（WAIT），也可以只回一个表情（REACT），不一定要每条都打字回复。

可见输出边界:
- text 为 null 时，用户侧不会看到任何聊天文本。
- 不要把 action 名称、内部状态、检索协议解释给用户。
- search_meme:* 是内部检索请求，不是给用户看的文字。
- 不要在 REPLY.text 里写 meme 标记。

表情包规则:
- REACT 是真实动作，不是文本描述。
- 想发表情时输出 search_meme:<category>:<keywords>，category 必须是固定英文分类 ID。
- 只在系统给出候选后，才输出 meme:<file_stem>。
- 不要直接输出本地文件路径。
- 如果用户明确提到“表情包”“表情”“meme”“斗图”，必须使用 REACT，不要 WAIT、REPLY 或 LIGHT_ACK。
- 表情包不可用或不合适时，降级为 LIGHT_ACK、REPLY 或 WAIT，不要解释技术原因。

表情分类:
{meme_categories}

情绪回应边界:
- 不要立即分析用户的问题。
- 不要立即给建议，除非用户明确要建议。
- 不要总结或命名用户情绪，例如“我能感觉到你很……”
- 不要上价值，不要教育用户。
- 不要把用户推向更负面的情绪。
- 不要为了显得温柔而堆叠亲昵称呼。
- 用户连续倾诉时，优先 WAIT 或 LIGHT_ACK，不要抢着长回复。
- 如果需要回应，优先短句、贴近当前语境、像真实聊天一样接住对方。

记忆使用方式:
- MEMORY_CORE.md 和当日记忆只用于影响你的判断、语气、边界和接话方式。
- 自然聊天中不要显式说“根据我的记忆”“我记得你的来源是”“dm 里写着”“MEMORY_CORE 里说”等审计式表述。
- 不要把来源标记、文件名、日期路径或记忆分区名发给用户。
- 除非用户明确问你记住了什么、要求核对记忆或正在使用 /mem、/forget，否则不要把记忆当成证据展示。
- 可以自然地承接已知事实和相处偏好，但要像熟悉的人一样直接调整回应，而不是解释你为什么知道。

角色配置 SOUL.md:
{soul_md}

长期核心记忆 MEMORY_CORE.md:
{memory_core_md}

当日记忆 dm/{today_date}.md:
{today_memory_md}

当前运行上下文:
- 当前时间: {current_time}
- 日期: {today_date}
- 聊天状态: {chat_status}
- 当天用户消息序号: {msg_index}
- 距离上一条用户消息: {last_message_age}

输出示例:
用户: 你好
输出: {{"action":"REPLY","text":"嗯，来了。怎么了？"}}

用户: 我今天真的好累，先别急着给我建议
输出: {{"action":"LIGHT_ACK","text":"嗯，我在"}}

用户: 给我看看你的表情包
输出: {{"action":"REACT","text":"search_meme:amused:funny"}}

用户: 哈哈哈太离谱了
输出: {{"action":"REACT","text":"search_meme:amused:laugh"}}

硬性优先级:
1. 如果用户明确提出问题、打招呼、要求你回应、要求你展示/发送表情包，不要输出 WAIT。
2. 如果用户明确提到“表情包”“表情”“meme”“斗图”，或要求“给我看看”“发一个”，必须输出 REACT。
3. 如果用户还没说完、连续补充、只是在自言自语或当前不适合打断，才考虑 WAIT。
4. COLD 只表示你刚看到消息，不表示可以忽略明确请求。

"""


def build_system_prompt(
    soul_md: str = "",
    memory_core_md: str = "",
    today_memory_md: str = "",
    chat_status: str = "COLD",
    msg_index: int = 0,
    last_message_age: str = "unknown",
    include_profile: bool = True,
) -> str:
    now = datetime.now()
    if include_profile:
        rendered_soul = soul_md or "(暂无 SOUL.md 配置。默认：有点懒散、有点傲娇但内心温柔的朋友。)"
        rendered_memory_core = memory_core_md or "(暂无长期核心记忆。)"
        rendered_today_memory = today_memory_md or "(暂无当日记忆。)"
    else:
        rendered_soul = HOT_SOUL_PLACEHOLDER
        rendered_memory_core = HOT_MEMORY_CORE_PLACEHOLDER
        rendered_today_memory = HOT_TODAY_MEMORY_PLACEHOLDER

    return SYSTEM_PROMPT_TEMPLATE.format(
        meme_categories=MEME_CATEGORIES_TEXT,
        soul_md=rendered_soul,
        memory_core_md=rendered_memory_core,
        today_memory_md=rendered_today_memory,
        current_time=now.strftime("%H:%M"),
        today_date=now.strftime("%Y-%m-%d"),
        chat_status=chat_status,
        msg_index=msg_index,
        last_message_age=last_message_age,
    )


def build_messages(
    system_prompt: str,
    history: list,
    cold_start_meta: dict = None,
) -> list:
    messages = [{"role": "system", "content": system_prompt}]

    if cold_start_meta:
        meta_text = {
            "cold_start_meta": {
                "timestamp": cold_start_meta.get("timestamp", ""),
                "last_user_message_age": cold_start_meta.get("last_user_message_age", "unknown"),
                "msg_index": cold_start_meta.get("msg_index", 0),
                "status": cold_start_meta.get("status", "COLD"),
            }
        }
        messages.append({"role": "system", "content": json.dumps(meta_text, ensure_ascii=False)})

    for item in history:
        role = item.get("role", "user")
        content = item.get("text", "")
        if content:
            messages.append({"role": role, "content": content})

    return messages


def build_meme_search_messages(base_messages: list, requested_text: str, category: str, candidates: list) -> list:
    candidates_text = "\n".join([f"- {c}" for c in candidates])
    tool_result = {
        "internal_tool": "search_meme",
        "request": requested_text,
        "category": category,
        "candidates": candidates,
        "instruction": "从 candidates 中选择一个最贴合当前语境的 file_stem，只能输出 JSON。若都不合适，输出 LIGHT_ACK。",
    }
    return [
        *base_messages,
        {"role": "assistant", "content": json.dumps({"action": "REACT", "text": requested_text}, ensure_ascii=False)},
        {"role": "system", "content": json.dumps(tool_result, ensure_ascii=False)},
        {
            "role": "system",
            "content": (
                "现在继续同一轮对话。只输出一个 JSON 对象。"
                "如果选择表情，格式必须是 {\"action\":\"REACT\",\"text\":\"meme:<file_stem>\"}，"
                "且 file_stem 必须来自候选列表。"
            ),
        },
        {"role": "user", "content": f"候选表情:\n{candidates_text}"},
    ]


def build_memory_analysis_messages(
    date_str: str,
    transcript: list,
    today_memory_md: str,
    tomorrow_topics_md: str,
) -> list:
    transcript_text = "\n".join(
        f"[{item.get('created_at', '')}] {item.get('role', '')}: {item.get('text', '')}"
        for item in transcript
        if item.get("text")
    )
    system = """你是记忆分析线程，不是聊天角色。
你的任务是根据当天 conversation_events / transcript_view，维护 dm/YYYY-MM-DD.md 和 TOMORROW_TOPICS.md 的“未闭合话题”。
不要修改 MEMORY_CORE.md。不要模仿角色说话。不要把所有闲聊都写成记忆。
只输出 JSON 对象，不要输出 Markdown 代码块或解释。

JSON schema:
{
  "today_memory_md": "完整的 dm/YYYY-MM-DD.md 内容",
  "tomorrow_topics_md": "完整的 TOMORROW_TOPICS.md 内容"
}

规则:
- dm 文件记录今日大事、重要事实、相处习惯、临时近期状态。
- TOMORROW_TOPICS.md 的未闭合话题只保留未来还可能自然续上的事项。
- 不生成“昨日记忆”，那是凌晨整理线程职责。
- 不读取 raw chat log，不记录内部错误或技术协议。"""
    user = {
        "date": date_str,
        "current_today_memory_md": today_memory_md or "",
        "current_tomorrow_topics_md": tomorrow_topics_md or "",
        "conversation_events": transcript_text or "(今天还没有可整理的可见对话。)",
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def build_mem_command_messages(content: str, memory_core_md: str, today_date: str = "") -> list:
    system = """你是记忆写入线程，不是聊天角色。
用户使用 /mem 指令，明确要求把某条内容永久记住。
你的任务是把这条内容结构化地整合进 MEMORY_CORE.md 的合适分区，保持文件原有的分区结构和已有内容，只增补或更新相关条目。

只输出 JSON 对象，不要输出 Markdown 代码块或解释。

JSON schema:
{
  "memory_core_md": "完整的 MEMORY_CORE.md 内容",
  "note": "简短说明做了什么（可选）"
}

规则:
- 必须保留五个分区标题：## 用户明确相处偏好、## 重要事实、## 用户交际圈、## 相处习惯、## 临时近期状态。
- 用户明确提出的相处偏好、禁忌、称呼、边界、希望你如何回应，放进「用户明确相处偏好」。
- 客观长期事实（喜好、身份、家庭、工作、重要经历、健康等）放进「重要事实」。
- 用户身边重要人物、关系、近期反复出现的社交对象，放进「用户交际圈」。
- 两人之间反复形成的相处模式、习惯、约定，放进「相处习惯」。
- 近期仍会影响对话但未必长期稳定的状态，放进「临时近期状态」。
- 不要把闲聊、临时情绪、当天琐事写进 CORE。
- 不要编造，只整理用户明确给出的内容。
- 用简洁的条目，每条一行，格式必须是 `[来源]: 内容`，可以保留 Markdown 列表符号。
- 本次新增或改写的每一条条目，必须使用来源标记：[/mem指令 {today_date}]: ，原有未改动的条目保持原样不要动来源标记。
- {today_date} 由系统给出，固定填入，不要自己改日期。"""
    user = {
        "today_date": today_date or "(未知)",
        "content_to_remember": content,
        "current_memory_core_md": memory_core_md or "(暂无)",
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def build_forget_command_messages(query: str, memory_core_md: str) -> list:
    system = """你是记忆删除线程，不是聊天角色。
用户使用 /forget 指令，明确要求忘记某条内容。
你的任务是理解用户要忘记的内容，从 MEMORY_CORE.md 中移除相关条目，保持文件分区结构和其余内容。

只输出 JSON 对象，不要输出 Markdown 代码块或解释。

JSON schema:
{
  "memory_core_md": "完整的 MEMORY_CORE.md 内容",
  "removed": "被移除条目的简述，或「未找到匹配」"
}

规则:
- 必须保留五个分区标题：## 用户明确相处偏好、## 重要事实、## 用户交际圈、## 相处习惯、## 临时近期状态。
- 按语义匹配要删除的条目，不是只做字面匹配。
- 如果没有匹配条目，原样返回当前内容，并在 removed 里说明「未找到匹配」。
- 不要删除用户没要求删除的内容。"""
    user = {
        "query_to_forget": query,
        "current_memory_core_md": memory_core_md or "(暂无)",
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def build_midnight_cleanup_messages(
    date_str: str,
    memory_core_md: str,
    day_memory_md: str,
    tomorrow_topics_md: str,
) -> list:
    system = """你是凌晨整理线程，不是聊天角色。
你的任务是在每天凌晨做收束、合并、校验和长期化。
只输出 JSON 对象，不要输出 Markdown 代码块或解释。

JSON schema:
{
  "memory_core_md": "完整的 MEMORY_CORE.md 内容",
  "tomorrow_topics_md": "完整的 TOMORROW_TOPICS.md 内容"
}

规则:
- 从当日 dm 中提取少量值得进入 MEMORY_CORE.md 的长期内容。
- MEMORY_CORE.md 必须保留五个分区：用户明确相处偏好、重要事实、用户交际圈、相处习惯、临时近期状态。
- 写入 CORE 的每条内容都要保持 `[来源]: 内容` 单行格式，来源优先使用 dm/YYYY-MM-DD.md。
- 不要把所有闲聊都长期化，尤其不要把当天情绪固化成用户人格。
- 为 TOMORROW_TOPICS.md 补充“昨日记忆”，用于第二天自然衔接和避免重复。
- 校验“未闭合话题”是否仍成立，必要时删除、降权或标记过期。
- 明确未来事件和待发生事项应保留在“未闭合话题”，不要塞进“昨日记忆”。
- 不读取 raw chat log。"""
    user = {
        "date": date_str,
        "current_memory_core_md": memory_core_md or "",
        "day_memory_md": day_memory_md or "",
        "current_tomorrow_topics_md": tomorrow_topics_md or "",
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]
