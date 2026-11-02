"""speaker_map 纯函数单测（标准库 unittest，无需宿主）。"""

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import speaker_map as sm  # noqa: E402


def build_directory() -> sm.SpeakerDirectory:
    directory = sm.SpeakerDirectory()
    directory.by_message["m-bot"] = sm.Identity(uid="1001", nickname="BotName", timestamp=10.0)
    directory.by_message["m-a"] = sm.Identity(uid="2002", nickname="alice", card="AliceCard", timestamp=11.0)
    directory.by_message["m-b"] = sm.Identity(uid="2002", nickname="alice", card="AliceCard", timestamp=12.0)
    for identity in directory.by_message.values():
        person = directory.people.get(identity.uid) or sm.Person(uid=identity.uid)
        person.touch(identity.nickname, identity.card, identity.timestamp)
        directory.people[identity.uid] = person
    directory.people["1001"].touch("BotName", "", 13.0)
    return directory


class SpeakerMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = build_directory()

    def test_self_marker_and_uid(self) -> None:
        text = '<message msg_id="m-bot" time="1" user="BotName">\nhi'
        new_text, counts = sm.enrich_message_tags(text, self.directory, {"1001"}, {"BotName"})
        self.assertIn('uid="1001"', new_text)
        self.assertIn('is_self_message="true"', new_text)
        self.assertEqual(counts, (1, 0, 1))

    def test_same_person_gets_same_uid(self) -> None:
        text_a, _ = sm.enrich_message_tags(
            '<message msg_id="m-a" user="alice" group_card="AliceCard">', self.directory, set(), set()
        )
        text_b, _ = sm.enrich_message_tags('<message msg_id="m-b" user="alice">', self.directory, set(), set())
        self.assertIn('uid="2002"', text_a)
        self.assertIn('uid="2002"', text_b)
        self.assertIn('group_card="AliceCard"', text_b)

    def test_idempotent(self) -> None:
        text = '<message msg_id="m-a" user="alice" group_card="AliceCard" uid="2002">'
        new_text, counts = sm.enrich_message_tags(text, self.directory, set(), set())
        self.assertEqual(new_text, text)
        self.assertEqual(counts, (0, 0, 0))

    def test_legend_mentions_bot_and_fact(self) -> None:
        legend = sm.build_legend(
            self.directory,
            {"1001", "2002"},
            {"1001"},
            ["BotName", "BotPlatformName", "BotGroupCard"],
            authoritative_facts=["Boss（uid=100000001）是我的管理员"],
        )
        self.assertIn("这是你自己", legend)
        self.assertIn("uid=2002", legend)
        self.assertIn("事实校正", legend)

    def test_extract_bot_names(self) -> None:
        names = sm.extract_bot_names(
            "你的名字是BotName，还有人叫你BotAlias。\n"
            "你在 qq 平台的昵称是“BotPlatformName”。你在本群的昵称是“BotGroupCard”。"
        )
        self.assertEqual(names, ["BotName", "BotPlatformName", "BotGroupCard"])

    def test_invisible_name_fallback(self) -> None:
        invisible = "\U000e0021"
        self.directory.by_message["m-inv"] = sm.Identity(uid="3003", nickname=invisible, timestamp=14.0)
        person = sm.Person(uid="3003")
        person.touch(invisible, "", 14.0)
        self.directory.people["3003"] = person

        text = f'<message msg_id="m-inv" user="{invisible}">\nhi'
        new_text, _ = sm.enrich_message_tags(text, self.directory, set(), set())
        self.assertIn('user="用户3003"', new_text)
        self.assertIn('uid="3003"', new_text)

        legend = sm.build_legend(self.directory, {"3003"}, set(), [], max_people=8)
        self.assertIn("uid=3003：昵称 用户3003", legend)


if __name__ == "__main__":
    unittest.main()
