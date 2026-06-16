import os
from datetime import datetime


MEMORY_CORE_TEMPLATE = """# 永久核心记忆

## 用户长期事实

## 相处习惯

## 关系边界
"""


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
        content = self._strip_command_prefix(command_text, "/mem").strip()
        content = content.lstrip("：: ").strip()
        if content.startswith("记住"):
            content = content[len("记住"):].lstrip("：: ").strip()
        elif content.startswith("修改"):
            content = content[len("修改"):].lstrip("：: ").strip()

        if not content:
            return False, "没有可写入的记忆内容。"

        existing = self.read_memory_core()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        today = datetime.now().strftime("%Y-%m-%d")
        line = f"[用户明确写入 {today}]: {content}"
        success = self.write_memory_core(existing + line + "\n")
        return success, "已存入记忆。" if success else "记忆文件保存失败。"

    def apply_forget_command(self, command_text: str) -> tuple[bool, str]:
        query = self._strip_command_prefix(command_text, "/forget").strip()
        query = query.lstrip("：: ").strip()
        if query.startswith("忘记"):
            query = query[len("忘记"):].lstrip("：: ").strip()

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
        return success, "已从记忆中移除。" if success else "记忆文件保存失败。"

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
