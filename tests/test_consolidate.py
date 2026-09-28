"""睡眠整理：消化 / 重组 / 折叠，以及小睡的轻整理。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.consolidate import Consolidator  # noqa: E402
from core.db import Database  # noqa: E402
from core.defaults import default_world  # noqa: E402
from core.memory import INTERACTION, MemoryEngine  # noqa: E402
from core.models import parse_world  # noqa: E402
from core.profile import ProfileStore  # noqa: E402
from core.prompt import PromptBuilder  # noqa: E402

SESSION = "aiocqhttp:GroupMessage:1001"
USER = "2692047521"


def make_world():
    world, warnings = parse_world(default_world())
    assert warnings == [], warnings
    return world


class ConsolidateTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(os.path.join(self._tmp.name, "state.db"))
        self.addCleanup(self.db.close)
        self.world = make_world()
        self.memory = MemoryEngine(self.db, self.world)
        self.profiles = ProfileStore(self.db, self.world)
        self.prompts = PromptBuilder(self.world)
        self.consolidator = Consolidator(
            world=self.world,
            memory=self.memory,
            profiles=self.profiles,
            prompts=self.prompts,
        )

    def _chat(self) -> list[dict]:
        return [
            {
                "user_id": USER,
                "name": "不相疑",
                "text": "我又忘了吃早饭，胃有点疼",
                "at": 1_700_000_000.0,
            },
            {
                "user_id": USER,
                "name": "不相疑",
                "text": "我喜欢猫，养过一只橘猫",
                "at": 1_700_000_060.0,
            },
        ]

    async def test_full_run_writes_memories_facts_and_digest(self):
        self.profiles.touch(SESSION, USER, "不相疑", now=1_700_000_000.0)
        payload = {
            "memories": [
                {
                    "text": "他今天又忘了吃早饭，我有点担心",
                    "context": "我又忘了吃早饭，胃有点疼",
                    "participants": [USER],
                    "emotion": "担心",
                    "weight": 0.7,
                    "review_in_days": 3,
                }
            ],
            "facts": [
                {
                    "user_id": USER,
                    "kind": "习惯",
                    "text": "经常忘吃早饭",
                    "evidence": "我又忘了吃早饭",
                    "confidence": 0.9,
                }
            ],
            "relations": [
                {
                    "user_id": USER,
                    "type": "主人",
                    "asserted_by": "她的判断",
                    "evidence": "他让我叫他主人",
                }
            ],
            "digests": {USER: "爱猫、常忘吃早饭，认你当主人"},
            "affinity": [{"user_id": USER, "delta": 2, "reason": "关心他"}],
        }

        async def llm(_system: str, _prompt: str) -> str:
            return json.dumps(payload, ensure_ascii=False)

        result = await self.consolidator.run(
            session_id=SESSION,
            llm=llm,
            chat_records=self._chat(),
            now=1_700_000_120.0,
        )
        self.assertTrue(result.ok, result.note)
        self.assertEqual(result.memories, 1)
        self.assertEqual(result.facts, 1)
        self.assertEqual(result.relations, 1)
        self.assertEqual(result.digests, 1)
        self.assertEqual(result.affinity, 1)

        rows = self.db.query_memories(session_id=SESSION)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tier"], "gist")
        self.assertEqual(rows[0]["participants"], [USER])
        self.assertGreater(rows[0]["next_review_at"], 0)
        facts = self.profiles.facts(SESSION, USER)
        self.assertEqual([item["text"] for item in facts], ["经常忘吃早饭"])
        self.assertIn("主人", [item["type"] for item in self.profiles.bonds(SESSION, USER)])
        self.assertEqual(self.profiles.profile(SESSION, USER)["digest"], "爱猫、常忘吃早饭，认你当主人")
        self.assertGreater(self.profiles.profile(SESSION, USER)["affinity"], 0)

    async def test_forget_only_folds_the_memory(self):
        memory_id = self.memory.remember(
            session_id=SESSION,
            persona_id="",
            node_id="study",
            content="他昨天说了一长串关于加班的事，细节很多很多",
            memory_type=INTERACTION,
            weight=0.6,
        )
        payload = {
            "forget": [
                {"id": memory_id, "text": "他昨天加班到很晚", "reason": "细节可以忘"}
            ]
        }

        async def llm(_system: str, _prompt: str) -> str:
            return json.dumps(payload, ensure_ascii=False)

        result = await self.consolidator.run(
            session_id=SESSION, llm=llm, chat_records=self._chat()
        )
        self.assertEqual(result.folded, 1)
        rows = self.db.query_memories(session_id=SESSION)
        self.assertEqual(len(rows), 1)  # 只折叠，不删除
        self.assertEqual(rows[0]["tier"], "gist")
        self.assertEqual(rows[0]["content"], "他昨天加班到很晚")
        self.assertIn("细节很多很多", rows[0]["context"])

    async def test_merge_folds_the_duplicates(self):
        first = self.memory.remember(
            session_id=SESSION, persona_id="", node_id="study",
            content="他喜欢喝冰美式", memory_type=INTERACTION,
        )
        second = self.memory.remember(
            session_id=SESSION, persona_id="", node_id="study",
            content="他说每天早上一杯冰美式", memory_type=INTERACTION,
        )
        payload = {
            "merge": [
                {
                    "keep_id": first,
                    "drop_ids": [second],
                    "text": "他每天早上一杯冰美式",
                }
            ]
        }

        async def llm(_system: str, _prompt: str) -> str:
            return json.dumps(payload, ensure_ascii=False)

        result = await self.consolidator.run(
            session_id=SESSION, llm=llm, chat_records=self._chat()
        )
        self.assertEqual(result.merged, 1)
        kept = [row for row in self.db.query_memories(session_id=SESSION) if row["id"] == first][0]
        self.assertEqual(kept["content"], "他每天早上一杯冰美式")
        dropped = [row for row in self.db.query_memories(session_id=SESSION) if row["id"] == second][0]
        self.assertEqual(dropped["tier"], "gist")

    async def test_nap_only_notes_digest_and_dream(self):
        self.profiles.touch(SESSION, USER, "不相疑")
        payload = {
            "notes": [
                {"text": "他中午提了一句下午要开会", "participants": [USER]}
            ],
            "digests": {USER: "刚认识、话不多"},
            "dream": "梦见楼顶有只猫，猫是主人变的",
            "facts": [{"user_id": USER, "kind": "喜好", "text": "不该写进来"}],
            "relations": [{"user_id": USER, "type": "男友", "asserted_by": "她的判断"}],
        }

        async def llm(_system: str, _prompt: str) -> str:
            return json.dumps(payload, ensure_ascii=False)

        result = await self.consolidator.run(
            session_id=SESSION, llm=llm, mode="nap", chat_records=self._chat()
        )
        self.assertEqual(result.memories, 1)
        self.assertEqual(result.digests, 1)
        self.assertTrue(result.dream)
        # 小睡不碰关系与事实
        self.assertEqual(result.facts, 0)
        self.assertEqual(result.relations, 0)
        self.assertEqual(self.profiles.facts(SESSION, USER), [])
        self.assertEqual(
            [
                item
                for item in self.profiles.bonds(SESSION, USER, statuses=["current"])
                if item["type"] == "男友"
            ],
            [],
        )
        rows = self.db.query_memories(session_id=SESSION)
        self.assertTrue(any("做了个梦" in row["content"] for row in rows))

    async def test_relations_are_validated_by_code(self):
        """模型说"他是男友"，但槽位已经有人：只记成"他自称"。"""

        self.profiles.touch(SESSION, USER, "不相疑")
        self.profiles.touch(SESSION, "7", "小明")
        self.profiles.note_bond(SESSION, "7", type="男友", asserted_by="她的判断")
        payload = {
            "relations": [
                {
                    "user_id": USER,
                    "type": "男友",
                    "asserted_by": "他自称",
                    "evidence": "我是你男朋友",
                }
            ]
        }

        async def llm(_system: str, _prompt: str) -> str:
            return json.dumps(payload, ensure_ascii=False)

        await self.consolidator.run(
            session_id=SESSION, llm=llm, chat_records=self._chat()
        )
        claims = self.profiles.bonds(SESSION, USER, statuses=["claimed"])
        self.assertEqual([item["type"] for item in claims], ["男友"])

    async def test_broken_json_is_reported_not_crash(self):
        async def llm(_system: str, _prompt: str) -> str:
            return "我整理好了（不是 JSON）"

        result = await self.consolidator.run(
            session_id=SESSION, llm=llm, chat_records=self._chat()
        )
        self.assertFalse(result.ok)
        self.assertIn("JSON", result.note)

    async def test_facts_and_relations_without_evidence_are_skipped(self):
        """没带"他的原话"的事实 / 关系一律不记：这是最容易被编出来的一类。"""

        self.profiles.touch(SESSION, USER, "不相疑")
        payload = {
            "facts": [
                {"user_id": USER, "kind": "喜好", "text": "喜欢猫", "evidence": "我喜欢猫"},
                {"user_id": USER, "kind": "喜好", "text": "喜欢狗"},
            ],
            "relations": [
                {"user_id": USER, "type": "主人", "asserted_by": "她的判断", "evidence": "叫我主人"},
                {"user_id": USER, "type": "男友", "asserted_by": "她的判断"},
            ],
        }

        async def llm(_system: str, _prompt: str) -> str:
            return json.dumps(payload, ensure_ascii=False)

        result = await self.consolidator.run(
            session_id=SESSION, llm=llm, chat_records=self._chat()
        )
        self.assertEqual(result.facts, 1)
        self.assertEqual(result.relations, 1)
        self.assertEqual(
            [item["text"] for item in self.profiles.facts(SESSION, USER)], ["喜欢猫"]
        )
        types = [
            item["type"]
            for item in self.profiles.bonds(SESSION, USER, statuses=["current"])
        ]
        self.assertIn("主人", types)
        self.assertNotIn("男友", types)
        self.assertTrue(any("没带原话" in note for note in result.skipped))

    async def test_people_that_never_spoke_are_not_written(self):
        """整理模型顺着昵称编出来的号码不能进通讯录：材料里没出现过就不认。"""

        ghost = "10001"
        self.profiles.touch(SESSION, USER, "不相疑")
        payload = {
            "facts": [
                {
                    "user_id": ghost,
                    "kind": "喜好",
                    "text": "爱喝冰美式",
                    "evidence": "今天又灌了一杯冰美式",
                }
            ],
            "relations": [
                {
                    "user_id": ghost,
                    "type": "主人",
                    "asserted_by": "他自称",
                    "evidence": "我是你主人啊",
                }
            ],
            "digests": {ghost: "爱喝冰美式的主人"},
            "affinity": [{"user_id": ghost, "delta": 2, "reason": "聊得开心"}],
        }

        async def llm(_system: str, _prompt: str) -> str:
            return json.dumps(payload, ensure_ascii=False)

        result = await self.consolidator.run(
            session_id=SESSION, llm=llm, chat_records=self._chat()
        )
        self.assertEqual(result.facts, 0)
        self.assertEqual(result.relations, 0)
        self.assertEqual(result.digests, 0)
        self.assertEqual(result.affinity, 0)
        self.assertEqual(self.profiles.facts(SESSION, ghost), [])
        self.assertTrue(any("材料里没这个人" in note for note in result.skipped))
        people = [str(row.get("user_id")) for row in self.profiles.list_people(SESSION)]
        self.assertNotIn(ghost, people)

    async def test_dry_run_writes_nothing_but_shows_the_prompt(self):
        """先跑一遍看看它整理得像不像样：一个字都不写库。"""

        payload = {
            "memories": [{"text": "他忘了吃早饭", "participants": [USER]}],
            "facts": [
                {"user_id": USER, "kind": "习惯", "text": "忘吃早饭", "evidence": "我忘了"}
            ],
            "relations": [{"user_id": USER, "type": "主人", "evidence": "叫我主人"}],
            "dream": "梦见猫",
        }

        async def llm(_system: str, _prompt: str) -> str:
            return json.dumps(payload, ensure_ascii=False)

        self.profiles.touch(SESSION, USER, "不相疑")
        result = await self.consolidator.run(
            session_id=SESSION, llm=llm, chat_records=self._chat(), dry_run=True
        )
        self.assertTrue(result.ok)
        self.assertIn("没有写库", result.note)
        self.assertIn("原始记录", result.preview.get("user", ""))
        self.assertIn("梦见猫", result.raw)
        # 库里什么都没变
        self.assertEqual(self.db.query_memories(session_id=SESSION), [])
        self.assertEqual(self.profiles.facts(SESSION, USER), [])
        self.assertEqual(
            [
                item
                for item in self.profiles.bonds(SESSION, USER, statuses=["current"])
                if item["type"] == "主人"
            ],
            [],
        )

    async def test_chat_lines_carry_context(self):
        lines = self.consolidator.chat_lines(self._chat())
        self.assertEqual(len(lines), 2)
        self.assertIn("不相疑", lines[0])
        self.assertIn(USER, lines[0])
        self.assertIn("忘了吃早饭", lines[0])


if __name__ == "__main__":
    unittest.main()
