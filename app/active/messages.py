import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from ..core.decisions import Action, ActionDecision


ACTIVE_SECTIONS = ("未闭合话题", "昨日记忆", "生活感消息备选")
ACTIVE_STATUSES = ("pending", "used", "expired", "blocked")
SECTION_PRIORITY = {section: idx for idx, section in enumerate(ACTIVE_SECTIONS)}


@dataclass
class ActiveTopicCandidate:
    section: str
    line_index: int
    raw_line: str
    status: str
    text: str


def parse_active_candidates(markdown: str) -> list[ActiveTopicCandidate]:
    candidates: list[ActiveTopicCandidate] = []
    current_section = ""

    for idx, raw in enumerate((markdown or "").splitlines()):
        stripped = raw.strip()
        if not stripped:
            continue

        header = _parse_section_header(stripped)
        if header:
            current_section = header
            continue

        if current_section not in ACTIVE_SECTIONS:
            continue
        if not _is_bullet_line(stripped):
            continue

        status, text = _parse_candidate_line(stripped)
        if text:
            candidates.append(
                ActiveTopicCandidate(
                    section=current_section,
                    line_index=idx,
                    raw_line=raw,
                    status=status,
                    text=text,
                )
            )

    return candidates


def select_pending_candidate(markdown: str) -> Optional[ActiveTopicCandidate]:
    pending = [c for c in parse_active_candidates(markdown) if c.status == "pending"]
    pending.sort(key=lambda c: (SECTION_PRIORITY.get(c.section, 99), c.line_index))
    return pending[0] if pending else None


def count_candidates_by_status(markdown: str) -> dict:
    counts = {status: 0 for status in ACTIVE_STATUSES}
    by_section = {section: 0 for section in ACTIVE_SECTIONS}
    for candidate in parse_active_candidates(markdown):
        counts[candidate.status] = counts.get(candidate.status, 0) + 1
        by_section[candidate.section] = by_section.get(candidate.section, 0) + 1
    return {"status": counts, "section": by_section}


def mark_candidate_status(markdown: str, candidate: ActiveTopicCandidate, status: str) -> str:
    if status not in ACTIVE_STATUSES:
        raise ValueError(f"invalid active candidate status: {status}")

    lines = (markdown or "").splitlines()
    idx = _resolve_candidate_index(lines, candidate)
    if idx is None:
        return _ensure_trailing_newline(markdown or "")

    old_line = lines[idx]
    lines[idx] = _replace_or_insert_status(old_line, status)
    return _ensure_trailing_newline("\n".join(lines))


def expire_stale_candidates(markdown: str, max_age_days: int = 3) -> tuple[str, int]:
    lines = (markdown or "").splitlines()
    expired = 0
    today = date.today()

    for candidate in parse_active_candidates(markdown):
        if candidate.status != "pending":
            continue
        source_date = _extract_date(candidate.raw_line)
        if not source_date:
            continue
        if (today - source_date).days > max_age_days:
            idx = _resolve_candidate_index(lines, candidate)
            if idx is not None:
                lines[idx] = _replace_or_insert_status(lines[idx], "expired")
                expired += 1

    return _ensure_trailing_newline("\n".join(lines)), expired


def parse_active_decision(raw_output: str) -> ActionDecision:
    data = _parse_json_object(raw_output)
    action = str(data.get("action", "WAIT")).strip().upper()
    text = data.get("text")
    text = text.strip() if isinstance(text, str) else None

    if action == Action.REPLY.value and text:
        return ActionDecision(action=Action.REPLY, text=_one_line(text, 80))

    if action == Action.REACT.value and text:
        normalized = _normalize_active_react(text)
        if normalized:
            return ActionDecision(action=Action.REACT, text=normalized)

    return ActionDecision(action=Action.WAIT, text=None)


def _parse_section_header(line: str) -> str:
    if line.startswith("## "):
        return line[3:].strip()
    if line.startswith("**") and line.endswith("**") and len(line) > 4:
        return line.strip("*").strip()
    return ""


def _is_bullet_line(line: str) -> bool:
    return line.startswith(("- ", "* ", "• "))


def _parse_candidate_line(line: str) -> tuple[str, str]:
    body = line[2:].strip() if line[:2] in ("- ", "* ") else line[1:].strip()
    status = "pending"

    status_match = re.match(r"^\[(pending|used|expired|blocked)\]\s*", body, re.IGNORECASE)
    if status_match:
        status = status_match.group(1).lower()
        body = body[status_match.end():].strip()

    source_match = re.match(r"^\[[^\]]+\]\s*:\s*", body)
    if source_match:
        body = body[source_match.end():].strip()

    body = _one_line(body, 160)
    return status, body


def _resolve_candidate_index(lines: list[str], candidate: ActiveTopicCandidate) -> Optional[int]:
    if 0 <= candidate.line_index < len(lines) and lines[candidate.line_index] == candidate.raw_line:
        return candidate.line_index
    for idx, line in enumerate(lines):
        if line == candidate.raw_line:
            return idx
    return None


def _replace_or_insert_status(line: str, status: str) -> str:
    return re.sub(
        r"^(\s*[-*•]\s+)(\[(?:pending|used|expired|blocked)\]\s*)?",
        rf"\1[{status}] ",
        line,
        count=1,
        flags=re.IGNORECASE,
    )


def _extract_date(text: str) -> Optional[date]:
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_active_react(text: str) -> Optional[str]:
    stripped = text.strip()
    if stripped.startswith("emoji:"):
        return stripped if _looks_like_emoji(stripped[6:]) else None
    if _looks_like_emoji(stripped):
        return f"emoji:{stripped}"
    return None


def _looks_like_emoji(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 8:
        return False
    return any(ord(ch) >= 0x2600 for ch in stripped)


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


def _one_line(text: str, max_chars: int) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    cleaned = cleaned.replace("```", "").strip()
    return cleaned[:max_chars].rstrip()


def _ensure_trailing_newline(text: str) -> str:
    return text.rstrip() + "\n" if text else ""
