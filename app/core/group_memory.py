import os
import re
from datetime import datetime
from typing import Optional

from .sessions import SESSION_ROOT_DIR, normalize_session_id


GROUP_MEMORY_TEMPLATE = """# 群聊记忆

## 群友身份

## 共同记忆

## 个人相关记忆
"""

GROUP_MEMORY_SECTIONS = ("群友身份", "共同记忆", "个人相关记忆")


class GroupMemoryManager:
    """Group-specific memory store isolated from private MEMORY_CORE."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir

    def memory_path(self, session_id: str) -> str:
        sid = normalize_session_id(session_id)
        return os.path.join(self.base_dir, SESSION_ROOT_DIR, sid, "GROUP_MEMORY.md")

    def read(self, session_id: str) -> str:
        path = self.memory_path(session_id)
        if not os.path.exists(path):
            self.write(session_id, GROUP_MEMORY_TEMPLATE)
            return GROUP_MEMORY_TEMPLATE
        try:
            with open(path, "r", encoding="utf-8") as file:
                content = file.read()
        except Exception:
            return ""
        if not _is_valid_group_memory(content):
            content = _migrate_group_memory(content)
            self.write(session_id, content)
        return content

    def write(self, session_id: str, content: str) -> bool:
        path = self.memory_path(session_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as file:
                file.write(content.rstrip() + "\n")
            return True
        except Exception as exc:
            print(f"[group_memory] write failed: {exc}")
            return False

    def qid_to_nickname(self, session_id: str) -> dict[str, str]:
        section = _section_lines(self.read(session_id), "群友身份")
        result: dict[str, str] = {}
        for line in section:
            match = re.match(r"^\s*[-*]?\s*(?P<qid>\d{4,})\s*[:：]\s*(?P<name>[^<\n]+)", line)
            if not match:
                continue
            nickname = _sanitize_nickname(match.group("name"))
            if nickname:
                result[match.group("qid")] = nickname
        return result

    def prompt_context(self, session_id: str, qids: Optional[list[str]] = None) -> dict:
        content = self.read(session_id)
        known_qids = {str(qid) for qid in (qids or []) if str(qid).strip()}
        all_names = self.qid_to_nickname(session_id)
        visible_names = {
            qid: name
            for qid, name in all_names.items()
            if not known_qids or qid in known_qids
        }
        personal_lines = []
        for line in _section_lines(content, "个人相关记忆"):
            qid = _personal_line_qid(line)
            if not known_qids or qid in known_qids:
                personal_lines.append(line)
        return {
            "qid_to_nickname": visible_names,
            "common_memory": _section_lines(content, "共同记忆")[-12:],
            "personal_memory": personal_lines[-20:],
        }

    def status(self, session_id: str) -> dict:
        content = self.read(session_id)
        return {
            "enabled": True,
            "phase": 8,
            "session_id": normalize_session_id(session_id),
            "path": self.memory_path(session_id),
            "identity_count": len(self.qid_to_nickname(session_id)),
            "common_memory_count": len(_section_lines(content, "共同记忆")),
            "personal_memory_count": len(_section_lines(content, "个人相关记忆")),
        }

    def apply_mem_command(
        self,
        command_text: str,
        *,
        session_id: str,
        sender_qid: str,
        timestamp: Optional[datetime] = None,
    ) -> tuple[bool, str, dict]:
        content = _extract_command_content(command_text, "/mem")
        if not content:
            return False, "没有可写入的群聊记忆内容。", {"status": "empty"}
        if _mentions_platform_nickname(content):
            return False, "平台群名片不稳定，这条我先不写入群聊记忆。", {"status": "rejected", "reason": "platform_nickname"}
        if _mentions_other_person(content, sender_qid):
            return False, "群聊 /mem 只能改你自己的身份信息，不能替别人写记忆。", {"status": "rejected", "reason": "other_person"}

        now = timestamp or datetime.now()
        nickname = _extract_self_nickname(content)
        existing = self.read(session_id)
        if nickname:
            updated = _upsert_identity(existing, sender_qid, nickname)
            personal_line = _personal_line(sender_qid, now, "群聊/mem本人", f"昵称={nickname}")
            updated = _append_section_line(updated, "个人相关记忆", personal_line, dedupe=True)
            if not self.write(session_id, updated):
                return False, "群聊记忆文件保存失败。", {"status": "write_failed"}
            return True, "已把你的群聊昵称记住了。", {
                "status": "identity_updated",
                "sender_qid": str(sender_qid),
                "nickname": nickname,
            }

        common_line = _common_line(now, sender_qid, content)
        updated = _append_section_line(existing, "共同记忆", common_line, dedupe=True)
        if not self.write(session_id, updated):
            return False, "群聊记忆文件保存失败。", {"status": "write_failed"}
        return True, "已写入群聊共同记忆。", {
            "status": "common_memory_added",
            "sender_qid": str(sender_qid),
        }

    def observe_group_text(
        self,
        text: str,
        *,
        session_id: str,
        sender_qid: str,
        timestamp: Optional[datetime] = None,
    ) -> dict:
        content = str(text or "").strip()
        if not content:
            return {"status": "skipped", "reason": "empty"}
        if _mentions_platform_nickname(content):
            return {"status": "skipped", "reason": "platform_nickname"}
        if _mentions_other_person(content, sender_qid):
            return {"status": "skipped", "reason": "other_person"}

        nickname = _extract_observed_self_nickname(content)
        if not nickname:
            return {"status": "skipped", "reason": "no_self_identity"}

        current = self.qid_to_nickname(session_id).get(str(sender_qid))
        if current == nickname:
            return {
                "status": "unchanged",
                "sender_qid": str(sender_qid),
                "nickname": nickname,
            }

        now = timestamp or datetime.now()
        existing = self.read(session_id)
        updated = _upsert_identity(existing, sender_qid, nickname)
        personal_line = _personal_line(sender_qid, now, "群聊/明确自称", f"昵称={nickname}")
        updated = _append_section_line(updated, "个人相关记忆", personal_line, dedupe=True)
        if not self.write(session_id, updated):
            return {"status": "write_failed", "sender_qid": str(sender_qid)}
        return {
            "status": "identity_observed",
            "sender_qid": str(sender_qid),
            "nickname": nickname,
        }

    def apply_forget_command(
        self,
        command_text: str,
        *,
        session_id: str,
        sender_qid: str,
    ) -> tuple[bool, str, dict]:
        query = _extract_command_content(command_text, "/forget")
        if not query:
            return False, "没有指定要忘记的群聊记忆。", {"status": "empty"}
        if _mentions_other_person(query, sender_qid):
            return False, "群聊 /forget 只能删除你自己的身份信息，不能替别人删记忆。", {"status": "rejected", "reason": "other_person"}

        existing = self.read(session_id)
        current_name = self.qid_to_nickname(session_id).get(str(sender_qid), "")
        if _looks_like_self_identity_forget(query, current_name):
            updated, removed = _remove_sender_identity(existing, sender_qid, query)
            if not self.write(session_id, updated):
                return False, "群聊记忆文件保存失败。", {"status": "write_failed"}
            return True, ("已从你的群聊身份记忆里移除。" if removed else "没有找到你的匹配身份记忆。"), {
                "status": "identity_removed" if removed else "not_found",
                "removed": removed,
            }

        updated, removed = _remove_common_memory(existing, query)
        if not self.write(session_id, updated):
            return False, "群聊记忆文件保存失败。", {"status": "write_failed"}
        return True, ("已从群聊共同记忆里移除。" if removed else "没有找到匹配的共同记忆。"), {
            "status": "common_memory_removed" if removed else "not_found",
            "removed": removed,
        }


def _is_valid_group_memory(content: str) -> bool:
    return bool(content and "# 群聊记忆" in content and all(f"## {section}" in content for section in GROUP_MEMORY_SECTIONS))


def _migrate_group_memory(content: str) -> str:
    result = GROUP_MEMORY_TEMPLATE
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result = _append_section_line(result, "共同记忆", stripped, dedupe=True)
    return result


def _section_lines(content: str, section: str) -> list[str]:
    lines = []
    in_section = False
    for raw in (content or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip() == section
            continue
        if in_section and stripped:
            lines.append(stripped)
    return lines


def _replace_section(content: str, section: str, lines: list[str]) -> str:
    if not _is_valid_group_memory(content):
        content = _migrate_group_memory(content)
    out = []
    in_section = False
    replaced = False
    for raw in content.rstrip().splitlines():
        stripped = raw.strip()
        if stripped.startswith("## "):
            if in_section and not replaced:
                out.extend(lines)
                replaced = True
            in_section = stripped[3:].strip() == section
            out.append(raw)
            continue
        if in_section:
            continue
        out.append(raw)
    if in_section and not replaced:
        out.extend(lines)
    return "\n".join(out).rstrip() + "\n"


def _append_section_line(content: str, section: str, line: str, dedupe: bool = False) -> str:
    lines = _section_lines(content, section)
    if not dedupe or line not in lines:
        lines.append(line)
    return _replace_section(content, section, lines)


def _upsert_identity(content: str, sender_qid: str, nickname: str) -> str:
    qid = str(sender_qid)
    lines = []
    replaced = False
    for line in _section_lines(content, "群友身份"):
        if re.match(rf"^\s*[-*]?\s*{re.escape(qid)}\s*[:：]", line):
            lines.append(f"- {qid}: {nickname}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"- {qid}: {nickname}")
    return _replace_section(content, "群友身份", lines)


def _remove_sender_identity(content: str, sender_qid: str, query: str) -> tuple[str, int]:
    qid = str(sender_qid)
    nickname_query = _sanitize_nickname(query)
    identity_lines = []
    removed = 0
    for line in _section_lines(content, "群友身份"):
        is_sender_line = bool(re.match(rf"^\s*[-*]?\s*{re.escape(qid)}\s*[:：]", line))
        if is_sender_line and (not nickname_query or nickname_query in line or any(word in query for word in ("昵称", "名字", "叫我", "我"))):
            removed += 1
            continue
        identity_lines.append(line)
    updated = _replace_section(content, "群友身份", identity_lines)

    personal_lines = []
    for line in _section_lines(updated, "个人相关记忆"):
        is_sender_personal = _personal_line_qid(line) == qid
        if is_sender_personal and (not nickname_query or nickname_query in line or "昵称" in line):
            removed += 1
            continue
        personal_lines.append(line)
    updated = _replace_section(updated, "个人相关记忆", personal_lines)
    return updated, removed


def _remove_common_memory(content: str, query: str) -> tuple[str, int]:
    tokens = _query_tokens(query)
    kept = []
    removed = 0
    for line in _section_lines(content, "共同记忆"):
        if query in line or any(token in line for token in tokens):
            removed += 1
            continue
        kept.append(line)
    return _replace_section(content, "共同记忆", kept), removed


def _extract_command_content(command_text: str, command: str) -> str:
    text = str(command_text or "").strip()
    if text.lower().startswith(command):
        text = text[len(command):]
    text = text.lstrip("：: ").strip()
    for prefix in ("记住", "修改", "忘记"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip("：: ").strip()
    return text


def _extract_self_nickname(content: str) -> str:
    text = str(content or "").strip()
    patterns = (
        r"^我(?:的)?(?:群聊)?(?:昵称|名字)(?:是|叫)?(?P<name>.+)$",
        r"^我叫(?P<name>.+)$",
        r"^叫我(?P<name>.+)$",
        r"^群里叫我(?P<name>.+)$",
        r"^我是(?P<name>.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text)
        if not match:
            continue
        raw = match.group("name").strip(" ：:，,。.!！")
        if pattern.endswith("(?P<name>.+)$") and text.startswith("我是") and raw.startswith(("一个", "一名", "正在", "喜欢", "不")):
            return ""
        return _sanitize_nickname(raw)
    return ""


def _extract_observed_self_nickname(content: str) -> str:
    text = str(content or "").strip()
    strong_patterns = (
        r"^我(?:的)?(?:群聊)?(?:昵称|名字)(?:是|叫)?(?P<name>.+)$",
        r"^我叫(?P<name>.+)$",
        r"^叫我(?P<name>.+)$",
        r"^群里叫我(?P<name>.+)$",
    )
    for pattern in strong_patterns:
        match = re.match(pattern, text)
        if match:
            return _sanitize_nickname(match.group("name").strip(" ：:，,。.!！"))

    match = re.match(r"^我是(?P<name>[^，,。.!！?？\s]{1,16})$", text)
    if not match:
        return ""
    raw = match.group("name").strip(" ：:，,。.!！")
    if raw.startswith(("一个", "一名", "正在", "喜欢", "不是", "不会", "不想", "觉得", "来")):
        return ""
    return _sanitize_nickname(raw)


def _sanitize_nickname(value: str) -> str:
    name = str(value or "").strip(" \t\r\n：:，,。.!！")
    name = re.sub(r"\s+", "", name)
    if not name or len(name) > 24:
        return ""
    if re.search(r"\d{4,}", name):
        return ""
    if any(token in name for token in ("群名片", "平台昵称", "message_id", "qid")):
        return ""
    return name


def _mentions_platform_nickname(content: str) -> bool:
    return any(token in str(content or "") for token in ("群名片", "平台昵称", "QQ昵称", "qq昵称"))


def _mentions_other_person(content: str, sender_qid: str) -> bool:
    text = str(content or "")
    for qid in re.findall(r"\d{4,}", text):
        if qid != str(sender_qid):
            return True
    if re.search(r"(他|她|TA|ta|别人|某人).{0,8}(是|叫|昵称|名字)", text):
        return True
    if re.match(r"^(?!我|我的|叫我|群里|这个群|大家|我们|最近|今天|梗|共同).{1,16}(是|叫).+", text):
        return True
    return False


def _looks_like_self_identity_forget(query: str, current_name: str) -> bool:
    text = str(query or "")
    if any(token in text for token in ("我", "我的", "昵称", "名字", "叫我")):
        return True
    return bool(current_name and current_name in text)


def _personal_line(sender_qid: str, timestamp: datetime, source: str, memory: str) -> str:
    return f"- <{sender_qid}><{timestamp.isoformat()}><{source}><{memory}>"


def _common_line(timestamp: datetime, sender_qid: str, content: str) -> str:
    return f"- <{timestamp.isoformat()}><群聊/mem {sender_qid}><{content}>"


def _personal_line_qid(line: str) -> str:
    match = re.search(r"<(?P<qid>\d{4,})>", line or "")
    return match.group("qid") if match else ""


def _query_tokens(query: str) -> list[str]:
    text = str(query or "")
    for sep in ("，", "。", "、", "：", ":", "；", ";", " ", "\t", "关于", "记忆", "那条"):
        text = text.replace(sep, " ")
    return [token for token in text.split() if len(token) >= 2]
