from dataclasses import dataclass
import json
from typing import Iterable

from .decisions import Action, ActionDecision, SendItem, SendItemType


RECENT_REPETITION_WINDOW = 5


@dataclass(frozen=True)
class RepetitionRemoval:
    kind: str
    value: str
    item_index: int


@dataclass(frozen=True)
class RepetitionFilterResult:
    decision: ActionDecision
    removed: list[RepetitionRemoval]


def filter_recent_repeated_reactions(
    decision: ActionDecision,
    recent_visible_texts: Iterable[str],
) -> RepetitionFilterResult:
    recent_emojis, recent_memes = _recent_reaction_sets(recent_visible_texts)
    if not recent_emojis and not recent_memes:
        return RepetitionFilterResult(decision=decision, removed=[])

    kept: list[SendItem] = []
    removed: list[RepetitionRemoval] = []
    changed = False

    for index, item in enumerate(decision.all_items()):
        if item.type == SendItemType.MEME:
            stem = item.harness_value()
            if stem in recent_memes:
                removed.append(RepetitionRemoval(kind="meme", value=stem, item_index=index))
                changed = True
                continue

        if item.type == SendItemType.EMOJI:
            emoji = _normalize_emoji_token(item.harness_value())
            if emoji in recent_emojis:
                removed.append(RepetitionRemoval(kind="emoji", value=emoji, item_index=index))
                changed = True
                continue

        if item.type == SendItemType.TEXT:
            stripped_text, stripped_emojis = _strip_blocked_emojis(item.content, recent_emojis)
            if stripped_emojis:
                changed = True
                removed.extend(
                    RepetitionRemoval(kind="emoji", value=emoji, item_index=index)
                    for emoji in sorted(stripped_emojis)
                )
                if stripped_text:
                    kept.append(SendItem(type=SendItemType.TEXT, content=stripped_text))
                continue

        kept.append(item)

    if not changed:
        return RepetitionFilterResult(decision=decision, removed=[])

    if not kept:
        return RepetitionFilterResult(decision=ActionDecision(action=Action.WAIT, items=None), removed=removed)

    return RepetitionFilterResult(decision=decision.with_items(kept), removed=removed)


def build_repetition_guard_system_reminder(removed: list[RepetitionRemoval]) -> dict:
    filtered_items = [
        {
            "kind": item.kind,
            "value": item.value,
            "item_index": item.item_index,
        }
        for item in removed
    ]
    payload = {
        "message_type": "SYSTEM_REMINDER",
        "status": "REPETITION_GUARD_FILTERED",
        "visibility": "internal_only_not_visible_to_user",
        "scope": "next_turns",
        "reason": "recent_visible_reaction_repetition",
        "filtered_items": filtered_items,
        "rules": [
            "刚才你的输出中重复使用了最近 5 条助手可见消息里已经出现过的 emoji 或 meme，系统已在发送前删除这些重复项。",
            "接下来不要继续使用 filtered_items 中相同的 emoji 或 meme file_stem；如需表达同类情绪，换成自然文字或不同表情。",
            "不要向用户解释系统删除、过滤、规则、提醒或内部判断。",
        ],
    }
    return {
        "role": "system",
        "content": json.dumps(payload, ensure_ascii=False),
    }


def reaction_signals_from_visible_text(text: str) -> tuple[set[str], set[str]]:
    text = str(text or "")
    emojis = set(_iter_normalized_emojis(text))
    memes = set()

    stripped = text.strip()
    if stripped.startswith("meme:"):
        stem = stripped[len("meme:"):].strip()
        if stem:
            memes.add(stem)
    if stripped.startswith("[表情: meme:") and stripped.endswith("]"):
        stem = stripped[len("[表情: meme:"):-1].strip()
        if stem:
            memes.add(stem)

    return emojis, memes


def _recent_reaction_sets(recent_visible_texts: Iterable[str]) -> tuple[set[str], set[str]]:
    emojis: set[str] = set()
    memes: set[str] = set()
    for text in recent_visible_texts:
        text_emojis, text_memes = reaction_signals_from_visible_text(text)
        emojis.update(text_emojis)
        memes.update(text_memes)
    return emojis, memes


def _strip_blocked_emojis(text: str, blocked_emojis: set[str]) -> tuple[str, set[str]]:
    if not text or not blocked_emojis:
        return text, set()

    pieces: list[str] = []
    removed: set[str] = set()
    index = 0
    for start, end, normalized in _iter_emoji_spans(text):
        if start > index:
            pieces.append(text[index:start])
        raw = text[start:end]
        if normalized in blocked_emojis:
            removed.add(normalized)
        else:
            pieces.append(raw)
        index = end
    if index < len(text):
        pieces.append(text[index:])

    return "".join(pieces).strip(), removed


def _iter_normalized_emojis(text: str):
    for _, _, normalized in _iter_emoji_spans(text):
        yield normalized


def _iter_emoji_spans(text: str):
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if not _is_emoji_base(char):
            index += 1
            continue

        start = index
        index += 1
        index = _consume_emoji_modifiers(text, index)
        while index + 1 < length and text[index] == "\u200d" and _is_emoji_base(text[index + 1]):
            index += 2
            index = _consume_emoji_modifiers(text, index)

        raw = text[start:index]
        yield start, index, _normalize_emoji_token(raw)


def _consume_emoji_modifiers(text: str, index: int) -> int:
    length = len(text)
    while index < length:
        codepoint = ord(text[index])
        if text[index] == "\ufe0f" or 0x1F3FB <= codepoint <= 0x1F3FF:
            index += 1
            continue
        break
    return index


def _normalize_emoji_token(value: str) -> str:
    return str(value or "").strip().replace("\ufe0f", "")


def _is_emoji_base(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
    )
