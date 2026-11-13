"""说话人身份索引、消息标签改写与对照表生成。

这里只放纯函数和数据结构，不依赖宿主能力，方便单独测试：

- ``SpeakerDirectory.from_recent`` 把 ``message.get_recent`` 的返回整理成
  ``message_id -> Identity`` 和 ``uid -> Person`` 两张表。
- ``enrich_items`` 给提示词里的 ``<message ...>`` 开标签补 ``uid`` /
  ``group_card`` / ``is_self_message``。
- ``build_legend`` 生成注入用的「发言者身份对照」文本块。
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

LEGEND_SENTINEL = "【发言者身份对照"

OPEN_TAG_RE = re.compile(r"<message\b([^>]*)>")
ATTR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\"([^\"]*)\"")

BOT_NAME_RE = re.compile(r"你的名字是([^，。,\n\r]+)")
PLATFORM_NICK_RE = re.compile(r"平台的昵称是[“\"]([^”\"]+)[”\"]")
GROUP_CARD_RE = re.compile(r"本群的昵称是[“\"]([^”\"]+)[”\"]")

MAX_ALIASES_IN_LINE = 4


@dataclass(slots=True)
class Identity:
    """一条消息的发送者身份。"""

    uid: str
    nickname: str = ""
    card: str = ""
    timestamp: float = 0.0


@dataclass(slots=True)
class Person:
    """一个稳定身份（uid）下见过的所有名字。"""

    uid: str
    nickname: str = ""
    card: str = ""
    last_seen: float = 0.0
    nicknames: dict[str, float] = field(default_factory=dict)
    cards: dict[str, float] = field(default_factory=dict)

    def touch(self, nickname: str, card: str, timestamp: float) -> None:
        """记录一次出现，保留最近使用的昵称和群名片。"""

        if timestamp >= self.last_seen:
            self.last_seen = timestamp
            if nickname:
                self.nickname = nickname
            if card:
                self.card = card
        if nickname:
            self.nicknames[nickname] = max(self.nicknames.get(nickname, 0.0), timestamp)
        if card:
            self.cards[card] = max(self.cards.get(card, 0.0), timestamp)

    def aliases(self, limit: int = MAX_ALIASES_IN_LINE) -> list[str]:
        """返回除当前昵称/名片之外的曾用名，最近的在前。"""

        history: list[tuple[float, str]] = []
        for name, seen in self.nicknames.items():
            if name and name != self.nickname:
                history.append((seen, name))
        for name, seen in self.cards.items():
            if name and name != self.card:
                history.append((seen, name))
        history.sort(key=lambda item: item[0], reverse=True)
        aliases: list[str] = []
        for _, name in history:
            if name not in aliases:
                aliases.append(name)
            if len(aliases) >= limit:
                break
        return aliases


class SpeakerDirectory:
    """按会话维护的 message_id -> 身份、uid -> 人物 索引。"""

    def __init__(self) -> None:
        self.by_message: dict[str, Identity] = {}
        self.people: dict[str, Person] = {}

    @classmethod
    def from_recent(cls, messages: Iterable[Any]) -> "SpeakerDirectory":
        """从 ``message.get_recent`` 的消息列表构造索引。"""

        directory = cls()
        for raw in messages or ():
            if not isinstance(raw, Mapping):
                continue
            message_id = str(raw.get("message_id") or "").strip()
            message_info = raw.get("message_info")
            if not isinstance(message_info, Mapping):
                continue
            user_info = message_info.get("user_info")
            if not isinstance(user_info, Mapping):
                continue
            uid = str(user_info.get("user_id") or "").strip()
            if not uid:
                continue
            nickname = str(user_info.get("user_nickname") or "").strip()
            card = str(user_info.get("user_cardname") or "").strip()
            timestamp = _to_float(raw.get("timestamp"))
            identity = Identity(uid=uid, nickname=nickname, card=card, timestamp=timestamp)
            if message_id:
                directory.by_message[message_id] = identity
            person = directory.people.get(uid)
            if person is None:
                person = Person(uid=uid)
                directory.people[uid] = person
            person.touch(nickname, card, timestamp)
        return directory

    def identity(self, message_id: str) -> Identity | None:
        """按消息编号取发送者身份。"""

        return self.by_message.get(message_id or "")

    def person(self, uid: str) -> Person | None:
        """按稳定身份取人物记录。"""

        return self.people.get(uid or "")

    def __bool__(self) -> bool:
        return bool(self.by_message or self.people)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def attr_value(attrs: str, name: str) -> str:
    """从开标签属性串里取一个属性值（已做 HTML 反转义）。"""

    for match in ATTR_RE.finditer(attrs or ""):
        if match.group(1) == name:
            return html.unescape(match.group(2))
    return ""


def _escape(value: str) -> str:
    return html.escape(str(value or ""), quote=True)


# 不可见字符：零宽、BOM、变体选择符，以及 U+E0000–U+E007F 的 tag 字符。
_INVISIBLE_CHARS = frozenset({"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\ufe0e", "\ufe0f"})


def is_displayable_name(value: object) -> bool:
    """判断昵称/群名片是否能被模型看见（排除空串和纯不可见字符）。"""

    text = html.unescape(str(value or "")).strip()
    if not text:
        return False
    visible = "".join(
        ch
        for ch in text
        if ch not in _INVISIBLE_CHARS and unicodedata.category(ch)[0] not in {"C", "Z"}
    )
    return bool(visible.strip())


def display_speaker_name(value: object, uid: str) -> str:
    """名字不可见时退化成稳定可读的“用户<uid>”，避免模型拿上一个说话人补空。"""

    text = str(value or "").strip()
    if is_displayable_name(text):
        return text
    return f"用户{uid}" if str(uid or "").strip() else "群友"


def _replace_attr(attrs: str, name: str, value: str) -> str:
    """替换开标签里某个属性的值；属性不存在时原样返回。"""

    pattern = re.compile(rf'({re.escape(name)}\s*=\s*")([^"]*)(")')
    return pattern.sub(lambda m: f"{m.group(1)}{_escape(value)}{m.group(3)}", attrs, count=1)


def _enrich_attrs(
    attrs: str,
    directory: SpeakerDirectory | None,
    bot_ids: set[str],
    self_names: set[str],
    *,
    fill_group_card: bool,
) -> tuple[str, int, int, int]:
    """给一个开标签属性串补 uid / group_card / is_self_message，并修正不可见名字。"""

    msg_id = attr_value(attrs, "msg_id")
    identity = directory.identity(msg_id) if (directory is not None and msg_id) else None
    uid = identity.uid if identity is not None else ""

    # 先处理不可见名字：user 为空/纯不可见字符时，模型会把它当成“上一条消息的人”。
    user_name = attr_value(attrs, "user")
    if not is_displayable_name(user_name):
        user_name = display_speaker_name(user_name, uid)
        attrs = _replace_attr(attrs, "user", user_name)

    card_name = attr_value(attrs, "group_card")
    if card_name and not is_displayable_name(card_name):
        attrs = _replace_attr(attrs, "group_card", display_speaker_name(card_name, uid))

    additions: list[str] = []
    added_uid = added_card = added_self = 0

    if uid and not attr_value(attrs, "uid"):
        additions.append(f'uid="{_escape(uid)}"')
        added_uid = 1

    if fill_group_card and identity is not None and identity.card and not attr_value(attrs, "group_card"):
        additions.append(f'group_card="{_escape(identity.card)}"')
        added_card = 1

    already_self = attr_value(attrs, "is_self_message").strip().lower() == "true"
    if not already_self:
        if uid and uid in bot_ids:
            additions.append('is_self_message="true"')
            added_self = 1
        elif not uid and user_name and user_name in self_names:
            additions.append('is_self_message="true"')
            added_self = 1

    if not additions:
        return f"<message{attrs}>", added_uid, added_card, added_self
    return f"<message{attrs} {' '.join(additions)}>", added_uid, added_card, added_self


def enrich_message_tags(
    text: str,
    directory: SpeakerDirectory | None,
    bot_ids: set[str],
    self_names: set[str],
    *,
    fill_group_card: bool = True,
) -> tuple[str, tuple[int, int, int]]:
    """改写一段文本里所有 ``<message ...>`` 开标签，返回新文本与计数。"""

    if not isinstance(text, str) or "<message" not in text:
        return text, (0, 0, 0)

    totals = [0, 0, 0]

    def replace(match: re.Match[str]) -> str:
        new_tag, added_uid, added_card, added_self = _enrich_attrs(
            match.group(1),
            directory,
            bot_ids,
            self_names,
            fill_group_card=fill_group_card,
        )
        totals[0] += added_uid
        totals[1] += added_card
        totals[2] += added_self
        return new_tag

    return OPEN_TAG_RE.sub(replace, text), (totals[0], totals[1], totals[2])


def enrich_items(
    items: Sequence[Any],
    directory: SpeakerDirectory | None,
    bot_ids: set[str],
    self_names: set[str],
    *,
    fill_group_card: bool = True,
) -> dict[str, int]:
    """就地改写提示词 items 里的用户消息，返回各类补丁计数。"""

    stats = {"uid": 0, "group_card": 0, "self": 0, "changed_items": 0}
    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        item_changed = False
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str) or "<message" not in text:
                continue
            new_text, counts = enrich_message_tags(
                text,
                directory,
                bot_ids,
                self_names,
                fill_group_card=fill_group_card,
            )
            if new_text != text:
                part["text"] = new_text
                stats["uid"] += counts[0]
                stats["group_card"] += counts[1]
                stats["self"] += counts[2]
                item_changed = True
        if item_changed:
            stats["changed_items"] += 1
    return stats


def collect_uids(items: Sequence[Any], directory: SpeakerDirectory | None) -> set[str]:
    """收集当前提示词里出现过的 uid。"""

    if directory is None:
        return set()
    used: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            for match in OPEN_TAG_RE.finditer(text):
                message_id = attr_value(match.group(1), "msg_id")
                identity = directory.identity(message_id)
                if identity is not None:
                    used.add(identity.uid)
    return used


def collect_self_names(items: Sequence[Any]) -> set[str]:
    """从已带自我标记的消息里学习“自己的名字”，作为查不到 uid 时的兜底。"""

    names: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            for match in OPEN_TAG_RE.finditer(text):
                attrs = match.group(1)
                if attr_value(attrs, "is_self_message").strip().lower() == "true":
                    name = attr_value(attrs, "user")
                    if name:
                        names.add(name)
    return names


def extract_system_text(items: Sequence[Any]) -> str:
    """取第一条系统消息的文本，用于读取 bot 名字。"""

    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "SystemMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        chunks = [
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        return "".join(chunks)
    return ""


def extract_bot_names(system_text: str) -> list[str]:
    """从系统提示里提取 bot 自己的名字，顺序为：配置名、平台昵称、本群昵称。"""

    names: list[str] = []
    if not system_text:
        return names

    def add(name: str) -> None:
        normalized = name.strip()
        if normalized and normalized not in names:
            names.append(normalized)

    for match in BOT_NAME_RE.finditer(system_text):
        add(match.group(1))
    for pattern in (PLATFORM_NICK_RE, GROUP_CARD_RE):
        for match in pattern.finditer(system_text):
            add(match.group(1))
    return names


def _format_person_line(person: Person, *, is_bot: bool, bot_names: Sequence[str]) -> str:
    nickname = display_speaker_name(person.nickname, person.uid)
    card = display_speaker_name(person.card, person.uid) if person.card else ""
    if is_bot:
        labels: list[str] = []
        if person.nickname:
            labels.append(f"昵称 {nickname}")
        extras = [name for name in bot_names if name and name != person.nickname]
        if extras:
            labels.append("也叫 " + "、".join(extras))
        if card and card != nickname:
            labels.append(f"群名片 {card}")
        detail = "；".join(labels) if labels else "机器人"
        return f"- uid={person.uid}：这是你自己（{detail}）"

    labels = []
    if person.nickname:
        labels.append(f"昵称 {nickname}")
    if card and card != nickname:
        labels.append(f"群名片 {card}")
    elif card and card == nickname:
        labels.append("昵称与群名片相同")
    aliases = [alias for alias in person.aliases() if is_displayable_name(alias)]
    if aliases:
        labels.append("曾用 " + "、".join(aliases))
    detail = "；".join(labels) if labels else "名字未知"
    return f"- uid={person.uid}：{detail}"


def build_legend(
    directory: SpeakerDirectory | None,
    used_uids: Iterable[str],
    bot_ids: set[str],
    bot_names: Sequence[str],
    *,
    max_people: int = 8,
    authoritative_facts: Sequence[str] = (),
    speech_rules: Sequence[str] = (),
) -> str:
    """生成「发言者身份对照」提示块。"""

    ordered_bot_names = [str(name).strip() for name in bot_names if str(name).strip()]
    bot_name = ordered_bot_names[0] if ordered_bot_names else ""
    lines: list[str] = [
        f"{LEGEND_SENTINEL}｜内部参考】",
        "读上下文时按以下规则判断“谁在说话”，不要把属性写进回复：",
        "1. <message> 里的 user 是 QQ 昵称，group_card 是群名片，uid 是稳定身份；同一个 uid 的不同名字是同一个人，不是多个人。",
        '2. is_self_message="true" 的消息是你自己（机器人）说的；没有这个标记的才是群友。',
        "3. “X戳了戳我 / 我戳了戳X”这类没有 uid 的裸文本是系统事件，X 是对方。",
    ]
    if bot_name:
        lines.append(f"4. 摘要里出现“{bot_name} 说/回/用…”时，那是在描述你自己，不是第三个人。")
    else:
        lines.append("4. 摘要里以你的名字为主语的描述，说的都是你自己，不是第三个人。")
    lines.append("5. 摘要、记忆、人物画像里的人名与本表冲突时，以本表 uid 为准。")

    rules = [str(rule).strip() for rule in speech_rules if str(rule).strip()]
    if rules:
        lines.append("称呼规则（写回复时必须遵守）：")
        for rule in rules:
            lines.append(f"- {rule}")

    people: list[tuple[Person, bool]] = []
    fact_uids: set[str] = set()
    for fact in authoritative_facts:
        fact_uids.update(re.findall(r"uid\s*=\s*(\d+)", str(fact)))
    if directory is not None:
        for uid in sorted(set(used_uids) | bot_ids | fact_uids):
            person = directory.person(uid)
            if person is None:
                continue
            people.append((person, uid in bot_ids))
        if len([p for p, _ in people if p.uid not in bot_ids]) > max_people:
            bots = [entry for entry in people if entry[1]]
            others = sorted((entry for entry in people if not entry[1]), key=lambda e: e[0].last_seen, reverse=True)
            people = bots + others[:max_people]

    if people:
        lines.append("本会话身份表（你自己在最前，其余按最近发言排序）：")
        # bot 自己放最前面，其余按最近发言时间倒序
        ordered = sorted(people, key=lambda entry: (not entry[1], -entry[0].last_seen))
        for person, is_bot in ordered:
            lines.append(_format_person_line(person, is_bot=is_bot, bot_names=bot_names))
    else:
        lines.append("本会话暂时拿不到身份表，只能按上面的规则和消息里已给的 user/group_card 判断。")

    facts = [str(item).strip() for item in authoritative_facts if str(item).strip()]
    if facts:
        lines.append("事实校正（优先于记忆与摘要）：")
        for fact in facts:
            lines.append(f"- {fact}")

    return "\n".join(lines)


def items_have_legend(items: Sequence[Any]) -> bool:
    """判断 items 里是否已经有对照表，避免重试时重复追加。"""

    for item in items:
        if not isinstance(item, dict):
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str) and LEGEND_SENTINEL in text:
                return True
    return False


def insert_legend(items: Sequence[Any], legend: str, position: str = "last_user") -> bool:
    """把对照表插入系统消息或最后一条用户消息，返回是否插入成功。"""

    if not legend:
        return False
    target: dict[str, Any] | None = None
    if position == "system":
        for item in items:
            if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem":
                target = item
                break
    if target is None:
        for item in items:
            if isinstance(item, dict) and item.get("item_type") == "UserMessageItem":
                target = item
    if target is None:
        return False

    parts = target.get("parts")
    if not isinstance(parts, list):
        target["parts"] = [{"type": "text", "text": legend}]
        return True

    for part in reversed(parts):
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            part["text"] = part["text"].rstrip() + "\n\n" + legend
            return True

    parts.append({"type": "text", "text": legend})
    return True
