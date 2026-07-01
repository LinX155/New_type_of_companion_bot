import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional
import asyncio

from .sessions import SESSION_ROOT_DIR, normalize_session_id


GROUP_MEMORY_TEMPLATE = """# 群聊记忆

## 群友身份

## 共同记忆

## 个人相关记忆
"""

GROUP_MEMORY_SECTIONS = ("群友身份", "共同记忆", "个人相关记忆")


GROUP_DAILY_MEMORY_TEMPLATE = """# 群聊日记忆

## 今日群聊大事

## 群友身份候选

## 共同话题与梗

## q号相关近期状态
"""

GROUP_DAILY_MEMORY_SECTIONS = ("今日群聊大事", "群友身份候选", "共同话题与梗", "q号相关近期状态")

GROUP_TOMORROW_TOPICS_TEMPLATE = """# 明日话题

## 未闭合话题

## 昨日记忆

## 生活感消息备选
"""

GROUP_TOMORROW_TOPIC_SECTIONS = ("未闭合话题", "昨日记忆", "生活感消息备选")


class GroupMemoryManager:
    """Group-specific memory store isolated from private MEMORY_CORE."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self._command_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._analysis_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def memory_path(self, session_id: str) -> str:
        sid = normalize_session_id(session_id)
        return os.path.join(self.base_dir, SESSION_ROOT_DIR, sid, "GROUP_MEMORY.md")

    def dm_dir(self, session_id: str) -> str:
        sid = normalize_session_id(session_id)
        return os.path.join(self.base_dir, SESSION_ROOT_DIR, sid, "dm")

    def dm_path(self, session_id: str, date_str: str) -> str:
        safe_date = _normalize_date_str(date_str)
        return os.path.join(self.dm_dir(session_id), f"{safe_date}.md")

    def topic_path(self, session_id: str) -> str:
        sid = normalize_session_id(session_id)
        return os.path.join(self.base_dir, SESSION_ROOT_DIR, sid, "TOMORROW_TOPICS.md")

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

    def read_dm_file(self, session_id: str, date_str: str) -> str:
        path = self.dm_path(session_id, date_str)
        if not os.path.exists(path):
            self.write_dm_file(session_id, date_str, GROUP_DAILY_MEMORY_TEMPLATE)
            return GROUP_DAILY_MEMORY_TEMPLATE
        try:
            with open(path, "r", encoding="utf-8") as file:
                content = file.read()
        except Exception:
            return ""
        if not _is_valid_group_daily_memory(content):
            content = _migrate_group_daily_memory(content)
            self.write_dm_file(session_id, date_str, content)
        return content

    def read_today_memory(self, session_id: str, now: Optional[datetime] = None) -> str:
        return self.read_dm_file(session_id, (now or datetime.now()).strftime("%Y-%m-%d"))

    def write_dm_file(self, session_id: str, date_str: str, content: str) -> bool:
        path = self.dm_path(session_id, date_str)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            normalized = content if _is_valid_group_daily_memory(content) else _migrate_group_daily_memory(content)
            with open(path, "w", encoding="utf-8") as file:
                file.write(normalized.rstrip() + "\n")
            return True
        except Exception as exc:
            print(f"[group_memory] dm write failed: {exc}")
            return False

    def read_tomorrow_topics(self, session_id: str) -> str:
        path = self.topic_path(session_id)
        if not os.path.exists(path):
            self.write_tomorrow_topics(session_id, GROUP_TOMORROW_TOPICS_TEMPLATE)
            return GROUP_TOMORROW_TOPICS_TEMPLATE
        try:
            with open(path, "r", encoding="utf-8") as file:
                content = file.read()
        except Exception:
            return ""
        if not _is_valid_group_tomorrow_topics(content):
            content = _migrate_group_tomorrow_topics(content)
            self.write_tomorrow_topics(session_id, content)
        return content

    def write_tomorrow_topics(self, session_id: str, content: str) -> bool:
        path = self.topic_path(session_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            normalized = content if _is_valid_group_tomorrow_topics(content) else _migrate_group_tomorrow_topics(content)
            with open(path, "w", encoding="utf-8") as file:
                file.write(normalized.rstrip() + "\n")
            return True
        except Exception as exc:
            print(f"[group_memory] tomorrow topics write failed: {exc}")
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
        today = datetime.now().strftime("%Y-%m-%d")
        dm_path = self.dm_path(session_id, today)
        latest_dm = self._latest_dm_path(session_id)
        return {
            "enabled": True,
            "phase": 8,
            "session_id": normalize_session_id(session_id),
            "path": self.memory_path(session_id),
            "dm_path": dm_path,
            "latest_dm_path": latest_dm,
            "latest_daytime_memory": self._latest_raw_status(session_id, "group_memory_analysis"),
            "latest_midnight_cleanup": self._latest_raw_status(session_id, "group_midnight_cleanup"),
            "latest_command": self._latest_raw_status(session_id, "group_memory_command"),
            "identity_count": len(self.qid_to_nickname(session_id)),
            "common_memory_count": len(_section_lines(content, "共同记忆")),
            "personal_memory_count": len(_section_lines(content, "个人相关记忆")),
        }

    def clear(self, session_id: Optional[str] = None) -> None:
        root = os.path.join(self.base_dir, SESSION_ROOT_DIR)
        if session_id:
            sid = normalize_session_id(session_id)
            if not sid.startswith("qq_group_"):
                return
            self._remove_group_memory_files(sid)
            self._command_locks.pop(sid, None)
            self._analysis_locks.pop(sid, None)
            return
        if not os.path.isdir(root):
            return
        for name in os.listdir(root):
            if name.startswith("qq_group_"):
                self._remove_group_memory_files(name)
        self._command_locks.clear()
        self._analysis_locks.clear()

    def _remove_group_memory_files(self, sid: str) -> None:
        path = self.memory_path(sid)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        dm_dir = self.dm_dir(sid)
        if os.path.isdir(dm_dir):
            for root, dirs, files in os.walk(dm_dir, topdown=False):
                for filename in files:
                    try:
                        os.remove(os.path.join(root, filename))
                    except Exception:
                        pass
                for dirname in dirs:
                    try:
                        os.rmdir(os.path.join(root, dirname))
                    except Exception:
                        pass
            try:
                os.rmdir(dm_dir)
            except Exception:
                pass
        topic_path = self.topic_path(sid)
        try:
            if os.path.exists(topic_path):
                os.remove(topic_path)
        except Exception:
            pass

    def _latest_dm_path(self, session_id: str) -> str:
        dm_dir = self.dm_dir(session_id)
        if not os.path.isdir(dm_dir):
            return ""
        candidates = [
            os.path.join(dm_dir, filename)
            for filename in os.listdir(dm_dir)
            if filename.endswith(".md")
        ]
        return max(candidates, key=os.path.getmtime) if candidates else ""

    def _latest_raw_status(self, session_id: str, event_type: str) -> dict:
        try:
            from ..storage.db import SessionLocal
            from ..storage.models import RawChatLog

            db = SessionLocal()
            try:
                row = (
                    db.query(RawChatLog)
                    .filter(RawChatLog.session_id == normalize_session_id(session_id))
                    .filter(RawChatLog.event_type == event_type)
                    .order_by(RawChatLog.id.desc())
                    .first()
                )
                if not row:
                    return {}
                return {
                    "status": row.status,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "error_message": row.error_message,
                    "action": row.action,
                }
            finally:
                db.close()
        except Exception:
            return {}

    async def analyze_window_via_llm(
        self,
        group_window: list[dict],
        *,
        session_id: str,
        llm_client,
        current_time: Optional[datetime] = None,
    ) -> dict:
        """Compatibility wrapper: group visible events update dm, never CORE."""
        events = _memory_events_from_window(group_window)
        date_str = (current_time or datetime.now()).strftime("%Y-%m-%d")
        return await self.analyze_daily_memory_via_llm(
            events,
            session_id=session_id,
            llm_client=llm_client,
            date_str=date_str,
            current_time=current_time,
        )

    async def analyze_daily_memory_via_llm(
        self,
        transcript: list[dict],
        *,
        session_id: str,
        llm_client,
        date_str: Optional[str] = None,
        current_time: Optional[datetime] = None,
    ) -> dict:
        """Update group dm through the group-specific daytime memory thread."""
        events = _sanitize_group_transcript(transcript)
        if not events:
            target_date = _normalize_date_str(date_str or (current_time or datetime.now()).strftime("%Y-%m-%d"))
            self.read_dm_file(session_id, target_date)
            return {"status": "skipped", "reason": "no_group_visible_events", "date": target_date}
        if llm_client is None or not getattr(llm_client, "api_key", ""):
            return {"status": "skipped", "reason": "llm_not_configured"}

        from ..llm.prompts import build_group_memory_analysis_messages

        lock = self._analysis_locks[normalize_session_id(session_id)]
        async with lock:
            return await self._analyze_daily_memory_locked(
                events,
                session_id=session_id,
                llm_client=llm_client,
                date_str=date_str,
                current_time=current_time,
                build_messages=build_group_memory_analysis_messages,
            )

    async def _analyze_daily_memory_locked(
        self,
        events: list[dict],
        *,
        session_id: str,
        llm_client,
        date_str: Optional[str],
        current_time: Optional[datetime],
        build_messages,
    ) -> dict:
        target_date = _normalize_date_str(date_str or (current_time or datetime.now()).strftime("%Y-%m-%d"))
        current_dm = self.read_dm_file(session_id, target_date)
        group_core = self.read(session_id)
        current_topics = self.read_tomorrow_topics(session_id)
        messages = build_messages(
            date_str=target_date,
            transcript=events,
            today_group_memory_md=current_dm,
            current_group_memory_md=group_core,
            current_tomorrow_topics_md=current_topics,
            current_time=(current_time or datetime.now()).isoformat(timespec="seconds"),
        )
        raw_output = await llm_client.chat_completion(messages=messages, temperature=0.2)
        data = _parse_json_object(raw_output)
        proposed = str(data.get("today_group_memory_md") or data.get("group_daily_memory_md") or "").strip()
        valid, reason = _validate_group_daily_memory_output(proposed)
        if not valid:
            return {
                "status": "skipped",
                "reason": reason,
                "date": target_date,
                "raw_output": raw_output,
            }

        proposed_topics = str(data.get("group_tomorrow_topics_md") or data.get("tomorrow_topics_md") or "").strip()
        topics_updated = False
        topics_reason = None
        if proposed_topics:
            if not _is_valid_group_tomorrow_topics(proposed_topics):
                topics_reason = "invalid_group_tomorrow_topics"
            elif proposed_topics.strip() != current_topics.strip():
                topics_updated = self.write_tomorrow_topics(session_id, proposed_topics)
                if not topics_updated:
                    topics_reason = "failed_to_write_group_tomorrow_topics"

        merged, changes = _merge_group_daily_memory(current_dm, proposed, allowed_qids=_known_qids_from_events(events) | _known_qids_from_memory(group_core))
        if merged.strip() == current_dm.strip():
            return {
                "status": "unchanged",
                "note": str(data.get("note") or ""),
                "date": target_date,
                "path": self.dm_path(session_id, target_date),
                "tomorrow_topics_updated": topics_updated,
                "tomorrow_topics_reason": topics_reason,
                "tomorrow_topics_path": self.topic_path(session_id),
                "changes": changes,
                "raw_output": raw_output,
            }
        if not self.write_dm_file(session_id, target_date, merged):
            return {
                "status": "write_failed",
                "date": target_date,
                "tomorrow_topics_updated": topics_updated,
                "tomorrow_topics_reason": topics_reason,
                "tomorrow_topics_path": self.topic_path(session_id),
                "changes": changes,
                "raw_output": raw_output,
            }
        return {
            "status": "updated",
            "note": str(data.get("note") or ""),
            "date": target_date,
            "path": self.dm_path(session_id, target_date),
            "tomorrow_topics_updated": topics_updated,
            "tomorrow_topics_reason": topics_reason,
            "tomorrow_topics_path": self.topic_path(session_id),
            "changes": changes,
            "raw_output": raw_output,
        }

    async def cleanup_via_llm(
        self,
        *,
        session_id: str,
        llm_client,
        date_str: str,
    ) -> dict:
        if llm_client is None or not getattr(llm_client, "api_key", ""):
            return {"status": "skipped", "reason": "llm_not_configured", "date": _normalize_date_str(date_str)}
        target_date = _normalize_date_str(date_str)
        day_memory = self.read_dm_file(session_id, target_date)
        if not _has_group_daily_memory_content(day_memory):
            return {"status": "skipped", "reason": "empty_day_memory", "date": target_date}

        from ..llm.prompts import build_group_midnight_cleanup_messages

        existing = self.read(session_id)
        messages = build_group_midnight_cleanup_messages(
            date_str=target_date,
            current_group_memory_md=existing,
            day_memory_md=day_memory,
        )
        raw_output = await llm_client.chat_completion(messages=messages, temperature=0.2)
        data = _parse_json_object(raw_output)
        proposed = str(data.get("group_memory_md") or "").strip()
        valid, reason = _validate_group_memory_output(proposed)
        if not valid:
            return {"status": "skipped", "reason": reason, "date": target_date, "raw_output": raw_output}

        allowed_qids = _known_qids_from_memory(existing) | _known_qids_from_memory(proposed) | _known_qids_from_daily_memory(day_memory)
        merged, changes = _merge_group_memory(existing, proposed, allowed_qids=allowed_qids)
        if merged.strip() == existing.strip():
            return {"status": "unchanged", "date": target_date, "note": str(data.get("note") or ""), "changes": changes, "raw_output": raw_output}
        if not self.write(session_id, merged):
            return {"status": "write_failed", "date": target_date, "changes": changes, "raw_output": raw_output}
        return {"status": "updated", "date": target_date, "note": str(data.get("note") or ""), "changes": changes, "raw_output": raw_output}

    async def apply_mem_command_via_llm(
        self,
        command_text: str,
        *,
        session_id: str,
        sender_qid: str,
        llm_client,
        timestamp: Optional[datetime] = None,
    ) -> tuple[bool, str, dict]:
        content = _extract_command_content(command_text, "/mem")
        precheck = self._precheck_mem_content(content, sender_qid)
        if precheck:
            return precheck
        return await self._run_command_locked(
            command="mem",
            content=content,
            session_id=session_id,
            sender_qid=sender_qid,
            llm_client=llm_client,
            timestamp=timestamp,
        )

    async def apply_forget_command_via_llm(
        self,
        command_text: str,
        *,
        session_id: str,
        sender_qid: str,
        llm_client,
    ) -> tuple[bool, str, dict]:
        query = _extract_command_content(command_text, "/forget")
        precheck = self._precheck_forget_query(query, sender_qid)
        if precheck:
            return precheck
        return await self._run_command_locked(
            command="forget",
            content=query,
            session_id=session_id,
            sender_qid=sender_qid,
            llm_client=llm_client,
        )

    async def _run_command_locked(
        self,
        *,
        command: str,
        content: str,
        session_id: str,
        sender_qid: str,
        llm_client,
        timestamp: Optional[datetime] = None,
    ) -> tuple[bool, str, dict]:
        sid = normalize_session_id(session_id)
        lock = self._command_locks[sid]
        queue_start = time.monotonic()
        async with lock:
            queue_wait_ms = int((time.monotonic() - queue_start) * 1000)
            if llm_client is None or not getattr(llm_client, "api_key", ""):
                success, response, payload = self._fallback_command(command, content, sid, sender_qid, timestamp)
                payload = {**payload, "llm_fallback": True, "queue_wait_ms": queue_wait_ms}
                return success, response, payload

            try:
                if command == "mem":
                    return await self._apply_mem_llm_locked(content, sid, sender_qid, llm_client, timestamp, queue_wait_ms)
                return await self._apply_forget_llm_locked(content, sid, sender_qid, llm_client, queue_wait_ms)
            except Exception as exc:
                success, response, payload = self._fallback_command(command, content, sid, sender_qid, timestamp)
                payload = {
                    **payload,
                    "llm_fallback": True,
                    "llm_error": str(exc),
                    "queue_wait_ms": queue_wait_ms,
                }
                return success, response, payload

    async def _apply_mem_llm_locked(
        self,
        content: str,
        session_id: str,
        sender_qid: str,
        llm_client,
        timestamp: Optional[datetime],
        queue_wait_ms: int,
    ) -> tuple[bool, str, dict]:
        from ..llm.prompts import build_group_mem_command_messages

        existing = self.read(session_id)
        today = (timestamp or datetime.now()).strftime("%Y-%m-%d")
        messages = build_group_mem_command_messages(
            content=content,
            current_group_memory_md=existing,
            sender_qid=str(sender_qid),
            today_date=today,
        )
        raw_output = await llm_client.chat_completion(messages=messages, temperature=0.2)
        data = _parse_json_object(raw_output)
        proposed = str(data.get("group_memory_md") or "").strip()
        valid, reason = _validate_group_memory_output(proposed)
        if not valid:
            return False, "群聊记忆线程输出不安全，这条先不写入。", {
                "status": "rejected",
                "reason": reason,
                "raw_output": raw_output,
                "queue_wait_ms": queue_wait_ms,
            }
        merged, changes = _merge_group_memory_for_mem(existing, proposed, sender_qid=str(sender_qid))
        if not self.write(session_id, merged):
            return False, "群聊记忆文件保存失败。", {"status": "write_failed", "queue_wait_ms": queue_wait_ms}
        return True, _group_mem_success_response(existing, merged, sender_qid), {
            "status": "llm_updated",
            "sender_qid": str(sender_qid),
            "queue_wait_ms": queue_wait_ms,
            "changes": changes,
            "note": str(data.get("note") or ""),
            "raw_output": raw_output,
        }

    async def _apply_forget_llm_locked(
        self,
        query: str,
        session_id: str,
        sender_qid: str,
        llm_client,
        queue_wait_ms: int,
    ) -> tuple[bool, str, dict]:
        from ..llm.prompts import build_group_forget_command_messages

        existing = self.read(session_id)
        messages = build_group_forget_command_messages(
            query=query,
            current_group_memory_md=existing,
            sender_qid=str(sender_qid),
        )
        raw_output = await llm_client.chat_completion(messages=messages, temperature=0.2)
        data = _parse_json_object(raw_output)
        proposed = str(data.get("group_memory_md") or "").strip()
        valid, reason = _validate_group_memory_output(proposed)
        if not valid:
            return False, "群聊记忆线程输出不安全，这次先不删除。", {
                "status": "rejected",
                "reason": reason,
                "raw_output": raw_output,
                "queue_wait_ms": queue_wait_ms,
            }
        merged, changes = _merge_group_memory_for_forget(existing, proposed, sender_qid=str(sender_qid))
        if not self.write(session_id, merged):
            return False, "群聊记忆文件保存失败。", {"status": "write_failed", "queue_wait_ms": queue_wait_ms}
        removed = str(data.get("removed") or "").strip()
        response = "已按你的要求更新群聊记忆。" if merged.strip() != existing.strip() else "没有找到匹配的群聊记忆。"
        return True, response, {
            "status": "llm_removed" if merged.strip() != existing.strip() else "not_found",
            "sender_qid": str(sender_qid),
            "queue_wait_ms": queue_wait_ms,
            "changes": changes,
            "removed": removed,
            "raw_output": raw_output,
        }

    def _fallback_command(
        self,
        command: str,
        content: str,
        session_id: str,
        sender_qid: str,
        timestamp: Optional[datetime],
    ) -> tuple[bool, str, dict]:
        if command == "mem":
            return self.apply_mem_command(f"/mem {content}", session_id=session_id, sender_qid=sender_qid, timestamp=timestamp)
        return self.apply_forget_command(f"/forget {content}", session_id=session_id, sender_qid=sender_qid)

    def _precheck_mem_content(self, content: str, sender_qid: str) -> Optional[tuple[bool, str, dict]]:
        if not content:
            return False, "没有可写入的群聊记忆内容。", {"status": "empty"}
        if _mentions_platform_nickname(content):
            return False, "平台群名片不稳定，这条我先不写入群聊记忆。", {"status": "rejected", "reason": "platform_nickname"}
        if _mentions_other_person(content, sender_qid):
            return False, "群聊 /mem 只能改你自己的身份信息，不能替别人写记忆。", {"status": "rejected", "reason": "other_person"}
        return None

    def _precheck_forget_query(self, query: str, sender_qid: str) -> Optional[tuple[bool, str, dict]]:
        if not query:
            return False, "没有指定要忘记的群聊记忆。", {"status": "empty"}
        if _mentions_other_person(query, sender_qid):
            return False, "群聊 /forget 只能删除你自己的身份信息，不能替别人删记忆。", {"status": "rejected", "reason": "other_person"}
        return None

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
        """Auxiliary observation only. It returns candidates and does not write CORE."""
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

        return {
            "status": "identity_candidate",
            "sender_qid": str(sender_qid),
            "nickname": nickname,
            "timestamp": (timestamp or datetime.now()).isoformat(),
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


def _is_valid_group_daily_memory(content: str) -> bool:
    return bool(content and "# 群聊日记忆" in content and all(f"## {section}" in content for section in GROUP_DAILY_MEMORY_SECTIONS))


def _is_valid_group_tomorrow_topics(content: str) -> bool:
    return bool(content and "# 明日话题" in content and all(f"## {section}" in content for section in GROUP_TOMORROW_TOPIC_SECTIONS))


def _normalize_date_str(value: str) -> str:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _migrate_group_memory(content: str) -> str:
    result = GROUP_MEMORY_TEMPLATE
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result = _append_section_line(result, "共同记忆", stripped, dedupe=True)
    return result


def _migrate_group_daily_memory(content: str) -> str:
    result = GROUP_DAILY_MEMORY_TEMPLATE
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result = _append_daily_section_line(result, "今日群聊大事", stripped, dedupe=True)
    return result


def _migrate_group_tomorrow_topics(content: str) -> str:
    result = GROUP_TOMORROW_TOPICS_TEMPLATE
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result = _append_topic_section_line(result, "未闭合话题", stripped, dedupe=True)
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


def _replace_daily_section(content: str, section: str, lines: list[str]) -> str:
    if not _is_valid_group_daily_memory(content):
        content = _migrate_group_daily_memory(content)
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


def _replace_topic_section(content: str, section: str, lines: list[str]) -> str:
    if not _is_valid_group_tomorrow_topics(content):
        content = _migrate_group_tomorrow_topics(content)
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


def _append_daily_section_line(content: str, section: str, line: str, dedupe: bool = False) -> str:
    lines = _section_lines(content, section)
    if not dedupe or line not in lines:
        lines.append(line)
    return _replace_daily_section(content, section, lines)


def _append_topic_section_line(content: str, section: str, line: str, dedupe: bool = False) -> str:
    lines = _section_lines(content, section)
    if not dedupe or line not in lines:
        lines.append(line if line.startswith(("-", "*", "•")) else f"- {line}")
    return _replace_topic_section(content, section, lines)


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


def _parse_json_object(raw_output: str) -> dict:
    text = str(raw_output or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _blocked_memory_output_reason(content: str) -> str:
    text = str(content or "")
    blocked_tokens = (
        "sender_card",
        "sender_nickname",
        "message_id",
        "onebot_message_id",
        "群名片",
        "平台昵称",
        "QQ昵称",
        "qq昵称",
        "[[quote]]",
        "<tool",
        "</tool",
        "<system",
        "</system",
        "assistant>",
        "user>",
        "<meme:",
    )
    for token in blocked_tokens:
        if token in text:
            return f"blocked_token:{token}"
    return ""


def _validate_group_memory_output(content: str) -> tuple[bool, str]:
    if not content or not _is_valid_group_memory(content):
        return False, "invalid_group_memory_output"
    blocked = _blocked_memory_output_reason(content)
    if blocked:
        return False, blocked
    return True, ""


def _validate_group_daily_memory_output(content: str) -> tuple[bool, str]:
    if not content or not _is_valid_group_daily_memory(content):
        return False, "invalid_group_daily_memory_output"
    blocked = _blocked_memory_output_reason(content)
    if blocked:
        return False, blocked
    return True, ""


def _sanitize_group_transcript(transcript: list[dict]) -> list[dict]:
    result = []
    for row in transcript or []:
        if not isinstance(row, dict):
            continue
        qid = str(row.get("qid") or row.get("sender_qid") or "").strip()
        role = str(row.get("role") or row.get("event_role") or "").strip()
        if not role and qid:
            role = "user"
        item = {
            "role": role,
            "event_type": str(row.get("event_type") or "").strip(),
            "created_at": str(row.get("created_at") or row.get("timestamp") or "").strip(),
        }
        if qid and re.fullmatch(r"\d{4,}", qid):
            item["qid"] = qid
        text = str(row.get("text") or "").strip()
        if text and not _blocked_memory_output_reason(text):
            item["text"] = text[:800]
        meme = str(row.get("meme") or "").strip()
        if meme and not _blocked_memory_output_reason(meme):
            item["meme"] = meme[:120]
        media = row.get("media")
        if isinstance(media, list):
            safe_media = []
            for media_item in media:
                if not isinstance(media_item, dict):
                    continue
                kind = str(media_item.get("kind") or media_item.get("type") or "").strip()
                summary = str(media_item.get("summary") or media_item.get("result") or "").strip()
                if kind in {"image", "meme", "sticker"}:
                    safe = {"kind": kind}
                    if summary and not _blocked_memory_output_reason(summary):
                        safe["summary"] = summary[:300]
                    safe_media.append(safe)
            if safe_media:
                item["media"] = safe_media
        if item.get("role") and (item.get("text") or item.get("meme") or item.get("media")):
            result.append(item)
    return result


def _memory_events_from_window(group_window: list[dict], limit: int = 60) -> list[dict]:
    events = []
    for entry in (group_window or [])[-max(1, int(limit or 60)):]:
        if not isinstance(entry, dict):
            continue
        qid = str(entry.get("sender_qid") or entry.get("qid") or "").strip()
        if not qid:
            continue
        item = {
            "timestamp": str(entry.get("timestamp") or ""),
            "qid": qid,
            "event_type": str(entry.get("event_type") or ""),
        }
        text = str(entry.get("text") or "").strip()
        if text:
            item["text"] = text
        meme = str(entry.get("meme") or "").strip()
        if meme:
            item["meme"] = meme
        media = _memory_media_from_entry(entry)
        if media:
            item["media"] = media
        if "text" in item or "meme" in item or "media" in item:
            events.append(item)
    return events


def _memory_media_from_entry(entry: dict) -> list[dict]:
    result = []
    for media in entry.get("media") or []:
        if not isinstance(media, dict):
            continue
        kind = str(media.get("kind") or "").strip()
        if kind not in {"image", "meme"}:
            continue
        item = {"kind": kind}
        meme = str(media.get("meme") or "").strip()
        if meme:
            item["meme"] = meme
        result.append(item)
    return result


def _known_qids_from_events(events: list[dict]) -> set[str]:
    return {
        str(event.get("qid") or "").strip()
        for event in events or []
        if re.fullmatch(r"\d{4,}", str(event.get("qid") or "").strip())
    }


def _known_qids_from_memory(content: str) -> set[str]:
    qids = set()
    for section in GROUP_MEMORY_SECTIONS:
        for line in _section_lines(content, section):
            qids.update(re.findall(r"\d{4,}", line))
    return qids


def _known_qids_from_daily_memory(content: str) -> set[str]:
    qids = set()
    for section in GROUP_DAILY_MEMORY_SECTIONS:
        for line in _section_lines(content, section):
            qids.update(re.findall(r"\d{4,}", line))
    return qids


def _has_group_daily_memory_content(content: str) -> bool:
    if not _is_valid_group_daily_memory(content):
        return False
    return any(_section_lines(content, section) for section in GROUP_DAILY_MEMORY_SECTIONS)


def _merge_group_daily_memory(existing: str, proposed: str, *, allowed_qids: set[str]) -> tuple[str, dict]:
    current = existing if _is_valid_group_daily_memory(existing) else GROUP_DAILY_MEMORY_TEMPLATE
    candidate = proposed if _is_valid_group_daily_memory(proposed) else GROUP_DAILY_MEMORY_TEMPLATE
    merged = GROUP_DAILY_MEMORY_TEMPLATE
    changes = {}
    for section in GROUP_DAILY_MEMORY_SECTIONS:
        lines, added = _merge_daily_lines(
            _section_lines(current, section),
            _section_lines(candidate, section),
            allowed_qids=allowed_qids,
        )
        merged = _replace_daily_section(merged, section, lines)
        changes[f"{section}_count"] = len(lines)
        changes[f"{section}_added"] = added
    return merged, changes


def _merge_daily_lines(existing_lines: list[str], proposed_lines: list[str], *, allowed_qids: set[str]) -> tuple[list[str], int]:
    result = []
    seen = set()
    for line in existing_lines:
        normalized = _normalize_daily_line(line, allowed_qids=allowed_qids)
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    added = 0
    for line in proposed_lines:
        normalized = _normalize_daily_line(line, allowed_qids=allowed_qids)
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
            added += 1
    return result[-80:], added


def _normalize_daily_line(line: str, *, allowed_qids: set[str]) -> str:
    text = str(line or "").strip()
    if not text or _blocked_memory_output_reason(text):
        return ""
    qids = set(re.findall(r"\d{4,}", text))
    if qids and allowed_qids and not qids.issubset(allowed_qids):
        return ""
    if len(text) > 260:
        text = text[:260].rstrip()
    if not text.startswith(("-", "*")):
        text = f"- {text}"
    return text


def _merge_group_memory(existing: str, proposed: str, *, allowed_qids: set[str]) -> tuple[str, dict]:
    current = existing if _is_valid_group_memory(existing) else GROUP_MEMORY_TEMPLATE
    candidate = proposed if _is_valid_group_memory(proposed) else GROUP_MEMORY_TEMPLATE
    identity_lines = _merge_identity_lines(
        _section_lines(current, "群友身份"),
        _section_lines(candidate, "群友身份"),
        allowed_qids=allowed_qids,
    )
    common_lines, common_added = _merge_memory_lines(
        _section_lines(current, "共同记忆"),
        _section_lines(candidate, "共同记忆"),
        allowed_qids=allowed_qids,
        personal=False,
    )
    personal_lines, personal_added = _merge_memory_lines(
        _section_lines(current, "个人相关记忆"),
        _section_lines(candidate, "个人相关记忆"),
        allowed_qids=allowed_qids,
        personal=True,
    )
    merged = GROUP_MEMORY_TEMPLATE
    merged = _replace_section(merged, "群友身份", identity_lines)
    merged = _replace_section(merged, "共同记忆", common_lines)
    merged = _replace_section(merged, "个人相关记忆", personal_lines)
    return merged, {
        "identity_count": len(identity_lines),
        "common_added": common_added,
        "personal_added": personal_added,
    }


def _merge_group_memory_for_mem(existing: str, proposed: str, *, sender_qid: str) -> tuple[str, dict]:
    current = existing if _is_valid_group_memory(existing) else GROUP_MEMORY_TEMPLATE
    candidate = proposed if _is_valid_group_memory(proposed) else GROUP_MEMORY_TEMPLATE
    allowed_qids = _known_qids_from_memory(current) | {str(sender_qid)}
    merged, changes = _merge_group_memory(current, candidate, allowed_qids=allowed_qids)
    merged = _preserve_other_qid_lines(current, merged, sender_qid=str(sender_qid))
    changes["mode"] = "mem"
    return merged, changes


def _merge_group_memory_for_forget(existing: str, proposed: str, *, sender_qid: str) -> tuple[str, dict]:
    current = existing if _is_valid_group_memory(existing) else GROUP_MEMORY_TEMPLATE
    candidate = proposed if _is_valid_group_memory(proposed) else GROUP_MEMORY_TEMPLATE
    allowed_qids = _known_qids_from_memory(current) | {str(sender_qid)}
    identity_lines = _merge_identity_lines(
        [],
        _section_lines(candidate, "群友身份"),
        allowed_qids=allowed_qids,
    )
    common_lines, common_added = _merge_memory_lines(
        [],
        _section_lines(candidate, "共同记忆"),
        allowed_qids=allowed_qids,
        personal=False,
    )
    personal_lines, personal_added = _merge_memory_lines(
        [],
        _section_lines(candidate, "个人相关记忆"),
        allowed_qids=allowed_qids,
        personal=True,
    )
    merged = GROUP_MEMORY_TEMPLATE
    merged = _replace_section(merged, "群友身份", identity_lines)
    merged = _replace_section(merged, "共同记忆", common_lines)
    merged = _replace_section(merged, "个人相关记忆", personal_lines)
    merged = _preserve_other_qid_lines(current, merged, sender_qid=str(sender_qid))
    changes = {
        "identity_count": len(identity_lines),
        "common_added": common_added,
        "personal_added": personal_added,
        "mode": "forget",
    }
    return merged, changes


def _preserve_other_qid_lines(existing: str, proposed: str, *, sender_qid: str) -> str:
    qid = str(sender_qid)
    identity_by_qid = {}
    for line in _section_lines(proposed, "群友身份"):
        parsed = _parse_identity_line(line)
        if parsed:
            identity_by_qid[parsed[0]] = line
    for line in _section_lines(existing, "群友身份"):
        parsed = _parse_identity_line(line)
        if parsed and parsed[0] != qid and parsed[0] not in identity_by_qid:
            identity_by_qid[parsed[0]] = line
    identity_lines = list(identity_by_qid.values())
    proposed = _replace_section(proposed, "群友身份", identity_lines)

    personal_lines = list(_section_lines(proposed, "个人相关记忆"))
    personal_seen = set(personal_lines)
    for line in _section_lines(existing, "个人相关记忆"):
        line_qid = _personal_line_qid(line)
        if line_qid and line_qid != qid and line not in personal_seen:
            personal_lines.append(line)
            personal_seen.add(line)
    return _replace_section(proposed, "个人相关记忆", personal_lines)


def _group_mem_success_response(existing: str, merged: str, sender_qid: str) -> str:
    before_name = _identity_name(existing, sender_qid)
    after_name = _identity_name(merged, sender_qid)
    if after_name and after_name != before_name:
        return "已把你的群聊昵称记住了。"
    return "已通过群聊记忆线程写入。"


def _identity_name(content: str, sender_qid: str) -> str:
    for line in _section_lines(content, "群友身份"):
        parsed = _parse_identity_line(line)
        if parsed and parsed[0] == str(sender_qid):
            return parsed[1]
    return ""


def _merge_identity_lines(existing_lines: list[str], proposed_lines: list[str], *, allowed_qids: set[str]) -> list[str]:
    identities: dict[str, str] = {}
    order: list[str] = []
    for line in existing_lines + proposed_lines:
        parsed = _parse_identity_line(line)
        if not parsed:
            continue
        qid, nickname = parsed
        if allowed_qids and qid not in allowed_qids:
            continue
        if qid not in order:
            order.append(qid)
        identities[qid] = nickname
    return [f"- {qid}: {identities[qid]}" for qid in order if identities.get(qid)]


def _parse_identity_line(line: str) -> Optional[tuple[str, str]]:
    match = re.match(r"^\s*[-*]?\s*(?P<qid>\d{4,})\s*[:：]\s*(?P<name>[^<\n]+)", line or "")
    if not match:
        return None
    nickname = _sanitize_nickname(match.group("name"))
    if not nickname:
        return None
    return match.group("qid"), nickname


def _merge_memory_lines(
    existing_lines: list[str],
    proposed_lines: list[str],
    *,
    allowed_qids: set[str],
    personal: bool,
) -> tuple[list[str], int]:
    result = []
    seen = set()
    for line in existing_lines:
        normalized = _normalize_memory_line(line, allowed_qids=allowed_qids, personal=personal, existing=True)
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    added = 0
    for line in proposed_lines:
        normalized = _normalize_memory_line(line, allowed_qids=allowed_qids, personal=personal, existing=False)
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
            added += 1
    return result, added


def _normalize_memory_line(line: str, *, allowed_qids: set[str], personal: bool, existing: bool) -> str:
    text = str(line or "").strip()
    if not text or _mentions_platform_nickname(text) or _blocked_memory_output_reason(text):
        return ""
    if any(token in text for token in ("sender_card", "sender_nickname", "平台群名片", "平台昵称")):
        return ""
    if len(text) > 260:
        text = text[:260].rstrip()
    if personal:
        qid = _personal_line_qid(text)
        if not qid or (allowed_qids and qid not in allowed_qids):
            return ""
        if len(re.findall(r"<[^>\n]+>", text)) < 4:
            return ""
    if not text.startswith(("-", "*")):
        text = f"- {text}"
    return text
