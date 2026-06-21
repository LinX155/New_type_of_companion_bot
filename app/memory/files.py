import json
import os
from datetime import datetime


MEMORY_CORE_TEMPLATE = """# 永久核心记忆

## 用户明确相处偏好

## 重要事实

## 用户交际圈

## 相处习惯

## 临时近期状态
"""

_REQUIRED_CORE_SECTIONS = ("用户明确相处偏好", "重要事实", "用户交际圈", "相处习惯", "临时近期状态")


TOMORROW_TOPICS_TEMPLATE = """# 明日话题

## 未闭合话题

## 昨日记忆

## 生活感消息备选
"""


TODAY_MEMORY_TEMPLATE = """# 每日记忆

## 今日大事

## 重要事实

## 相处习惯

## 临时近期状态
"""


class MemoryFileManager:
    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir or os.path.join(os.path.dirname(__file__), "..", "..")
        self.soul_path = os.path.join(self.base_dir, "SOUL.md")
        self.memory_core_path = os.path.join(self.base_dir, "MEMORY_CORE.md")
        self.tomorrow_topics_path = os.path.join(self.base_dir, "TOMORROW_TOPICS.md")
        self.dm_dir = os.path.join(self.base_dir, "dm")
        os.makedirs(self.dm_dir, exist_ok=True)
        self.ensure_base_files()

    def ensure_base_files(self):
        if not os.path.exists(self.memory_core_path):
            self._write_file(self.memory_core_path, MEMORY_CORE_TEMPLATE)
        else:
            existing = self._read_file(self.memory_core_path)
            if existing and not _is_valid_core(existing):
                self._write_file(self.memory_core_path, _migrate_core_schema(existing))
        if not os.path.exists(self.tomorrow_topics_path):
            self._write_file(self.tomorrow_topics_path, TOMORROW_TOPICS_TEMPLATE)

    def _read_file(self, path: str) -> str:
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"Error reading {path}: {e}")
            return ""

    def _write_file(self, path: str, content: str) -> bool:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except Exception as e:
            print(f"Error writing {path}: {e}")
            return False

    def read_soul(self) -> str:
        return self._read_file(self.soul_path)

    def write_soul(self, content: str) -> bool:
        return self._write_file(self.soul_path, content)

    def read_memory_core(self) -> str:
        return self._read_file(self.memory_core_path)

    def write_memory_core(self, content: str) -> bool:
        return self._write_file(self.memory_core_path, content)

    def read_tomorrow_topics(self) -> str:
        return self._read_file(self.tomorrow_topics_path)

    def write_tomorrow_topics(self, content: str) -> bool:
        return self._write_file(self.tomorrow_topics_path, content)

    def read_today_memory(self) -> str:
        today_str = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.dm_dir, f"{today_str}.md")
        if not os.path.exists(path):
            self._write_file(path, TODAY_MEMORY_TEMPLATE)
        return self._read_file(path)

    def write_today_memory(self, content: str) -> bool:
        today_str = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.dm_dir, f"{today_str}.md")
        return self._write_file(path, content)

    def write_dm_file(self, date_str: str, content: str) -> bool:
        path = os.path.join(self.dm_dir, f"{date_str}.md")
        return self._write_file(path, content)

    def append_to_today_memory(self, text: str) -> bool:
        today_str = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.dm_dir, f"{today_str}.md")
        existing = self._read_file(path)
        if existing and not existing.endswith("\n"):
            existing += "\n"
        new_content = existing + text + "\n"
        return self._write_file(path, new_content)

    def read_dm_file(self, date_str: str) -> str:
        path = os.path.join(self.dm_dir, f"{date_str}.md")
        return self._read_file(path)

    def list_dm_files(self) -> list:
        if not os.path.exists(self.dm_dir):
            return []
        files = []
        for f in sorted(os.listdir(self.dm_dir)):
            if f.endswith(".md"):
                files.append(f[:-3])  # remove .md
        return files

    def apply_mem_command(self, command_text: str) -> tuple[bool, str]:
        content = self._extract_mem_content(command_text)
        return self._sync_append_mem(content)

    def _extract_mem_content(self, command_text: str) -> str:
        content = self._strip_command_prefix(command_text, "/mem").strip()
        content = content.lstrip("：: ").strip()
        if content.startswith("记住"):
            content = content[len("记住"):].lstrip("：: ").strip()
        elif content.startswith("修改"):
            content = content[len("修改"):].lstrip("：: ").strip()
        return content.strip()

    def _sync_append_mem(self, content: str) -> tuple[bool, str]:
        if not content:
            return False, "没有可写入的记忆内容。"
        content = _normalize_mem_content_for_storage(content)
        existing = self.read_memory_core()
        today = datetime.now().strftime("%Y-%m-%d")
        line = f"- [/mem指令 {today}]: {content}"
        new_content = _append_entry_to_core(existing, _guess_core_section(content), line)
        success = self.write_memory_core(new_content)
        return success, "已存入记忆（同步）。" if success else "记忆文件保存失败。"

    async def apply_mem_via_llm(self, command_text: str, llm_client) -> tuple[bool, str]:
        """通过 LLM 记忆线程把 /mem 内容结构化写入 MEMORY_CORE.md。

        无 API key 或 LLM 失败时自动降级为同步追加，保证一定落盘。
        """
        content = self._extract_mem_content(command_text)
        if not content:
            return False, "没有可写入的记忆内容。"

        if llm_client is None or not getattr(llm_client, "api_key", ""):
            return self._sync_append_mem(content)

        try:
            from ..llm.prompts import build_mem_command_messages
            existing = self.read_memory_core()
            today_date = datetime.now().strftime("%Y-%m-%d")
            messages = build_mem_command_messages(content, existing, today_date)
            raw = await llm_client.chat_completion(
                messages=messages, temperature=0.2
            )
            data = _parse_json_object(raw)
            new_core = (data.get("memory_core_md") or "").strip()
            if not new_core or not _is_valid_core(new_core):
                return self._sync_append_mem(content)
            # 兜底：给本次新增的条目补上来源标记，不依赖 LLM 是否遵守指令
            new_core = _stamp_new_entries(existing, new_core, today_date)
            if not self.write_memory_core(new_core):
                return False, "记忆文件保存失败。"
            return True, "已通过记忆线程写入 CORE。"
        except Exception as e:
            print(f"[mem] LLM thread failed, fallback to sync: {e}")
            return self._sync_append_mem(content)

    def apply_forget_command(self, command_text: str) -> tuple[bool, str]:
        query = self._extract_forget_query(command_text)
        return self._sync_remove_forget(query)

    def _extract_forget_query(self, command_text: str) -> str:
        query = self._strip_command_prefix(command_text, "/forget").strip()
        query = query.lstrip("：: ").strip()
        if query.startswith("忘记"):
            query = query[len("忘记"):].lstrip("：: ").strip()
        return query.strip()

    def _sync_remove_forget(self, query: str) -> tuple[bool, str]:
        if not query:
            return False, "没有指定要忘记的内容。"
        existing = self.read_memory_core()
        if not existing:
            return True, "记忆里暂时没有可移除的内容。"
        query_tokens = self._forget_tokens(query)
        kept_lines = []
        removed = 0
        for line in existing.splitlines():
            normalized = line.lower()
            if query in line or any(token in normalized for token in query_tokens):
                removed += 1
                continue
            kept_lines.append(line)
        if removed == 0:
            return True, "没有找到匹配的记忆，未改动。"
        new_content = "\n".join(kept_lines).rstrip() + "\n"
        success = self.write_memory_core(new_content)
        return success, "已从记忆中移除（同步）。" if success else "记忆文件保存失败。"

    async def apply_forget_via_llm(self, command_text: str, llm_client) -> tuple[bool, str]:
        """通过 LLM 记忆线程按语义从 MEMORY_CORE.md 中移除相关内容。

        无 API key 或 LLM 失败时自动降级为同步字面匹配删除。
        """
        query = self._extract_forget_query(command_text)
        if not query:
            return False, "没有指定要忘记的内容。"

        if llm_client is None or not getattr(llm_client, "api_key", ""):
            return self._sync_remove_forget(query)

        existing = self.read_memory_core()
        if not existing:
            return True, "记忆里暂时没有可移除的内容。"

        try:
            from ..llm.prompts import build_forget_command_messages
            messages = build_forget_command_messages(query, existing)
            raw = await llm_client.chat_completion(
                messages=messages, temperature=0.2
            )
            data = _parse_json_object(raw)
            new_core = (data.get("memory_core_md") or "").strip()
            removed_note = (data.get("removed") or "").strip()
            if not new_core or not _is_valid_core(new_core):
                return self._sync_remove_forget(query)
            if not self.write_memory_core(new_core):
                return False, "记忆文件保存失败。"
            return True, removed_note or "已通过记忆线程从 CORE 移除。"
        except Exception as e:
            print(f"[forget] LLM thread failed, fallback to sync: {e}")
            return self._sync_remove_forget(query)

    def _strip_command_prefix(self, text: str, prefix: str) -> str:
        stripped = (text or "").strip()
        if stripped.lower().startswith(prefix):
            return stripped[len(prefix):]
        return stripped

    def _forget_tokens(self, query: str) -> list[str]:
        lowered = query.lower()
        separators = ["，", "。", "、", "：", ":", "；", ";", " ", "\t", "关于", "那条", "记忆", "的"]
        for sep in separators:
            lowered = lowered.replace(sep, " ")
        return [token for token in lowered.split() if len(token) >= 2]


def _parse_json_object(raw_output: str) -> dict:
    text = (raw_output or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return {}
    return {}


def _is_valid_core(content: str) -> bool:
    """校验 LLM 输出的 MEMORY_CORE.md 保留了必要的分区结构。"""
    if not content or "# 永久核心记忆" not in content:
        return False
    return all(section in content for section in _REQUIRED_CORE_SECTIONS)


def _migrate_core_schema(content: str) -> str:
    """把旧版 CORE 分区迁移到当前五分区结构，尽量保留已有条目。"""
    buckets = {section: [] for section in _REQUIRED_CORE_SECTIONS}
    current_section = "重要事实"
    old_to_new = {
        "用户长期事实": "重要事实",
        "关系边界": "用户明确相处偏好",
        "相处习惯": "相处习惯",
        "用户明确相处偏好": "用户明确相处偏好",
        "重要事实": "重要事实",
        "用户交际圈": "用户交际圈",
        "临时近期状态": "临时近期状态",
    }

    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("# 永久核心记忆"):
            continue
        if line.startswith("## "):
            current_section = old_to_new.get(line[3:].strip(), "重要事实")
            continue
        if not _has_source_marker(line):
            line = f"- [迁移]: {line.lstrip('-*• ').strip()}"
        buckets.setdefault(current_section, []).append(line)

    parts = ["# 永久核心记忆", ""]
    for section in _REQUIRED_CORE_SECTIONS:
        parts.append(f"## {section}")
        parts.extend(buckets.get(section, []))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _guess_core_section(content: str) -> str:
    text = (content or "").lower()
    if any(word in text for word in ["最近", "这几天", "今天", "明天", "临时", "近期", "正在", "准备"]):
        return "临时近期状态"
    if any(word in text for word in ["老板", "同事", "朋友", "家人", "妈妈", "爸爸", "前任", "老师", "室友"]):
        return "用户交际圈"
    if any(word in text for word in ["不要", "别", "不喜欢", "喜欢", "希望", "称呼", "边界", "先陪", "建议"]):
        return "用户明确相处偏好"
    return "重要事实"


def _normalize_mem_content_for_storage(content: str) -> str:
    """Convert explicit /mem text into a user-centric memory sentence.

    The LLM memory thread does richer semantic rewriting. This lightweight
    fallback only prevents the most common identity ambiguity when the sync
    append path is used.
    """
    text = (content or "").strip()
    if not text:
        return ""

    self_prefix_replacements = (
        ("你应该", "我应该"),
        ("你要", "我要"),
        ("你以后", "我以后"),
        ("你不要", "我不要"),
        ("你别", "我别"),
        ("你的", "我的"),
        ("你", "我"),
    )
    for prefix, replacement in self_prefix_replacements:
        if text.startswith(prefix):
            text = replacement + text[len(prefix):]
            return _normalize_user_pronouns_inside_instruction(text)

    prefix_replacements = (
        ("我的", "用户的"),
        ("我", "用户"),
        ("俺的", "用户的"),
        ("俺", "用户"),
        ("本人", "用户"),
    )
    for prefix, replacement in prefix_replacements:
        if text.startswith(prefix):
            text = replacement + text[len(prefix):]
            break

    user_to_self_replacements = (
        ("用户不希望你的", "用户不希望我的"),
        ("用户不希望你", "用户不希望我"),
        ("用户希望你的", "用户希望我的"),
        ("用户希望你", "用户希望我"),
        ("用户想让你的", "用户想让我的"),
        ("用户想让你", "用户想让我"),
        ("用户要求你的", "用户要求我的"),
        ("用户要求你", "用户要求我"),
        ("用户需要你的", "用户需要我的"),
        ("用户需要你", "用户需要我"),
    )
    for target, replacement in user_to_self_replacements:
        if target in text:
            text = text.replace(target, replacement)

    return text


def _normalize_user_pronouns_inside_instruction(text: str) -> str:
    self_prefix = ""
    body = text
    for prefix in ("我的", "我"):
        if text.startswith(prefix):
            self_prefix = prefix
            body = text[len(prefix):]
            break

    replacements = (
        ("我的", "用户的"),
        ("我", "用户"),
        ("俺的", "用户的"),
        ("俺", "用户"),
        ("本人", "用户"),
    )
    for target, replacement in replacements:
        body = body.replace(target, replacement)
    return self_prefix + body


def _append_entry_to_core(core: str, section: str, line: str) -> str:
    if not _is_valid_core(core or ""):
        core = _migrate_core_schema(core or MEMORY_CORE_TEMPLATE)

    lines = core.rstrip().splitlines()
    target_header = f"## {section}"
    out = []
    inserted = False
    in_target = False

    for raw in lines:
        if raw.strip().startswith("## "):
            if in_target and not inserted:
                out.append(line)
                inserted = True
            in_target = raw.strip() == target_header
        out.append(raw)

    if in_target and not inserted:
        out.append(line)
        inserted = True

    if not inserted:
        if out and out[-1].strip():
            out.append("")
        out.append(target_header)
        out.append(line)

    return "\n".join(out).rstrip() + "\n"


_SOURCE_RE = None


def _source_marker(date_str: str) -> str:
    return f"[/mem指令 {date_str}]: "


def _has_source_marker(line: str) -> bool:
    """该行是否已带 [来源]: 内容 标记（兼容列表符号前缀）。"""
    body = line.lstrip()
    # 去掉可能的列表符号前缀 (- * • 或 1.)
    for b in ("- ", "-　", "* ", "*　", "• ", "•　"):
        if body.startswith(b):
            body = body[len(b):].lstrip()
            break
    return body.startswith("[") and "]:" in body


def _stamp_new_entries(old_core: str, new_core: str, date_str: str) -> str:
    """对比新旧 CORE，给本次新增的条目行补上来源标记。

    - 只处理分区标题下的实质条目（跳过标题、空行、模板占位）。
    - 已带来源标记或原文里已存在的行不动。
    """
    marker = _source_marker(date_str)
    old_lines = {ln.strip() for ln in (old_core or "").splitlines()}
    out = []
    for raw in new_core.splitlines():
        stripped = raw.strip()
        is_entry = bool(stripped) and not stripped.startswith("#") and not _has_source_marker(stripped)
        if is_entry and stripped not in old_lines:
            indent = raw[: len(raw) - len(raw.lstrip())]
            # 保留行首列表符号（- * • 1.），标记放在符号之后、内容之前
            bullet = ""
            body = stripped
            for b in ("- ", "-　", "* ", "*　", "• ", "•　"):
                if stripped.startswith(b):
                    bullet = b
                    body = stripped[len(b):].lstrip()
                    break
            else:
                # 数字编号 1. / 2.
                m = stripped.split(".", 1)
                if len(m) == 2 and m[0].isdigit() and body[:2] not in ("",):
                    pass  # 数字列表较少见，保持原样不剥离
            out.append(f"{indent}{bullet}{marker}{body}")
        else:
            out.append(raw)
    return "\n".join(out).rstrip() + "\n"
