"""用户画像（人 + 事实 + 关系 + 好感度）的纯逻辑测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import Database  # noqa: E402
from core.defaults import default_world  # noqa: E402
from core.models import parse_world  # noqa: E402
from core.profile import ProfileStore  # noqa: E402

SESSION = "aiocqhttp:GroupMessage:1001"
PRIVATE = "aiocqhttp:PrivateMessage:2692047521"
GROUP_KEY = "aiocqhttp:GroupMessage:9000"


def make_world():
    world, warnings = parse_world(default_world())
    assert warnings == [], warnings
    return world


class ProfileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(os.path.join(self._tmp.name, "state.db"))
        self.addCleanup(self.db.close)
        self.world = make_world()
        self.store = ProfileStore(self.db, self.world, group_of=self._group_key)

    @staticmethod
    def _group_key(session_id: str) -> str:
        """把群和私聊当成同一个她。"""

        return GROUP_KEY if session_id in (SESSION, PRIVATE) else session_id

    # ---------------- 建档与平台信息 ----------------

    def test_first_message_creates_profile_with_default_bond(self):
        self.store.touch(SESSION, "42", "小明", now=1000.0)

        profile = self.store.profile(SESSION, "42")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["first_seen_at"], 1000.0)
        self.assertEqual(profile["message_count"], 1)
        self.assertEqual(profile["payload"]["qq_name"], "小明")
        bonds = self.store.bonds(SESSION, "42", statuses=["current"])
        self.assertEqual([item["type"] for item in bonds], ["陌生人"])

    def test_touch_updates_names_and_counts(self):
        self.store.touch(SESSION, "42", "小明", now=1000.0)
        self.store.touch(SESSION, "42", "小明", now=1060.0)
        self.store.touch(SESSION, "42", "阿明", now=1120.0)

        profile = self.store.profile(SESSION, "42")
        self.assertEqual(profile["message_count"], 3)
        self.assertEqual(profile["last_seen_at"], 1120.0)
        self.assertEqual(profile["payload"]["qq_name"], "阿明")
        self.assertEqual(profile["payload"]["names"][-2:], ["小明", "阿明"])
        self.assertEqual(
            profile["payload"]["name_history"][-1]["name"], "阿明"
        )

    def test_talked_stamp_is_separate_from_showing_up(self):
        """「见过」和「聊过」分开记：只在群里露脸不动"上次说话"那个时间。"""

        self.store.touch(SESSION, "42", "小明", now=1000.0)
        self.assertEqual(self.store.last_talked_at(SESSION, "42"), 0.0)

        self.store.note_talked(SESSION, "42", now=1200.0)
        # 之后他又露了几次面，但没有再跟她说话
        self.store.touch(SESSION, "42", "小明", now=1800.0, count_message=False)

        self.assertEqual(self.store.last_talked_at(SESSION, "42"), 1200.0)
        self.assertEqual(self.store.profile(SESSION, "42")["last_seen_at"], 1800.0)
        # 同一个她：私聊里看的是同一份时间
        self.assertEqual(self.store.last_talked_at(PRIVATE, "42"), 1200.0)

    def test_group_card_is_stored_per_session(self):
        self.store.touch(SESSION, "42", "小明")
        self.assertTrue(self.store.set_card(SESSION, "42", "群里的阿明"))
        self.assertFalse(self.store.set_card(SESSION, "42", "群里的阿明"))
        view = self.store.view(SESSION, "42")
        self.assertEqual(view.cards.get(SESSION), "群里的阿明")

    def test_profile_is_shared_inside_a_session_group(self):
        """同一个她：群里认识的，私聊里也认识。"""

        self.store.touch(SESSION, "42", "小明")
        self.store.note_fact(SESSION, "42", text="喜欢猫", kind="喜好")

        private_view = self.store.view(PRIVATE, "42")
        self.assertIsNotNone(private_view)
        self.assertEqual([item["text"] for item in private_view.facts], ["喜欢猫"])

    # ---------------- 称呼 ----------------

    def test_call_names_need_explicit_words(self):
        self.store.touch(SESSION, "42", "小明")
        self.store.set_call_names(SESSION, "42", call_me="主人")
        # 不是明确纠正：不动已有称呼
        result = self.store.set_call_names(
            SESSION, "42", call_me="爸爸", explicit=False
        )
        self.assertEqual(result["rejected"][0]["reason"], "已有称呼，只有他明确改才换")
        self.assertEqual(self.store.view(SESSION, "42").call_me, "主人")
        # 明确纠正：可以改
        self.store.set_call_names(SESSION, "42", call_me="蓝蓝")
        self.assertEqual(self.store.view(SESSION, "42").call_me, "蓝蓝")

    def test_call_names_are_sanitized_and_blacklisted(self):
        self.store.touch(SESSION, "42", "小明")
        self.world.profile.call_name_blacklist = ["爸爸"]

        self.store.set_call_names(SESSION, "42", call_me="爸爸")
        self.assertEqual(self.store.view(SESSION, "42").call_me, "")
        # 太长 / 带标点 / 带链接的都不算称呼
        for bad in ("我叫小明，请多关照", "https://example.com", "x" * 20):
            self.store.set_call_names(SESSION, "42", call_him=bad)
        self.assertEqual(self.store.view(SESSION, "42").call_him, "")
        self.store.set_call_names(SESSION, "42", call_him="小笨蛋")
        self.assertEqual(self.store.view(SESSION, "42").call_him, "小笨蛋")

    # ---------------- 事实 ----------------

    def test_facts_dedupe_and_replace_opposite(self):
        self.store.touch(SESSION, "42", "小明")
        first = self.store.note_fact(SESSION, "42", text="喜欢辣", kind="喜好")
        self.assertEqual(first["action"], "add")
        again = self.store.note_fact(SESSION, "42", text="喜欢辣", kind="喜好")
        self.assertEqual(again["action"], "merge")
        self.assertEqual(again["id"], first["id"])

        swapped = self.store.note_fact(
            SESSION, "42", text="不喜欢辣", kind="喜好", evidence="我现在不吃辣了"
        )
        self.assertEqual(swapped["action"], "replace")
        facts = self.store.facts(SESSION, "42")
        self.assertEqual([item["text"] for item in facts], ["不喜欢辣"])

    def test_facts_keep_evidence_and_pin(self):
        self.store.touch(SESSION, "42", "小明")
        self.store.note_fact(
            SESSION,
            "42",
            text="生日是 10 月 3 日",
            kind="基本信息",
            evidence="我 10 月 3 日生日",
            pinned=True,
        )
        fact = self.store.facts(SESSION, "42")[0]
        self.assertEqual(fact["evidence"], "我 10 月 3 日生日")
        self.assertTrue(fact["pinned"])

    # ---------------- 关系 ----------------

    def test_one_person_can_hold_several_bonds(self):
        """既是主人也是男友：不同槽位可以并存；默认的"陌生人"要让位。"""

        self.store.touch(SESSION, "42", "小明")
        self.store.note_bond(SESSION, "42", type="主人", asserted_by="她的判断")
        result = self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断")
        self.assertTrue(result["ok"], result)
        types = [item["type"] for item in self.store.bonds(SESSION, "42", statuses=["current"])]
        self.assertEqual(sorted(types), sorted(["主人", "男友"]))
        view = self.store.view(SESSION, "42")
        self.assertNotIn("陌生人", view.affinities)
        # 垫底的那条不算"过去的关系"，别在历史里冒出来
        self.assertNotIn("陌生人", [item["type"] for item in view.past])

    def test_upgrade_replaces_the_lower_close_bond(self):
        """朋友升成男友是**替换**：同类只留一条，旧的留成"曾经"。"""

        self.store.touch(SESSION, "42", "小明", now=1000.0)
        self.store.note_bond(SESSION, "42", type="朋友", asserted_by="她的判断", now=1000.0)
        self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断", now=2000.0)

        current = [
            item["type"]
            for item in self.store.visible_bonds(SESSION, "42", statuses=["current"])
        ]
        self.assertEqual(current, ["男友"])
        past = self.store.visible_bonds(SESSION, "42", statuses=["past"])
        self.assertEqual([item["type"] for item in past], ["朋友"])
        self.assertEqual(past[0]["until"], 2000.0, "旧关系要记下是什么时候结束的")

    def test_she_may_downgrade_the_relationship_herself(self):
        """她也可以主动退回：男友 → 朋友同样按"最新的判断"算。"""

        self.store.touch(SESSION, "42", "小明", now=1000.0)
        self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断", now=1000.0)
        self.store.note_bond(
            SESSION,
            "42",
            type="朋友",
            asserted_by="她的判断",
            evidence="我们还是做朋友吧",
            now=3000.0,
        )

        current = [
            item["type"]
            for item in self.store.visible_bonds(SESSION, "42", statuses=["current"])
        ]
        self.assertEqual(current, ["朋友"])
        past = self.store.visible_bonds(SESSION, "42", statuses=["past"])
        self.assertEqual([item["type"] for item in past], ["男友"])

    def test_close_group_walks_up_one_step_at_a_time(self):
        """群友 → 朋友 → 闺蜜：每一步都把上一步收成"曾经"。"""

        self.store.touch(SESSION, "42", "小明")
        for name in ("群友", "朋友", "闺蜜"):
            self.store.note_bond(SESSION, "42", type=name, asserted_by="她的判断")

        current = [
            item["type"]
            for item in self.store.visible_bonds(SESSION, "42", statuses=["current"])
        ]
        self.assertEqual(current, ["闺蜜"])
        past = [
            item["type"]
            for item in self.store.visible_bonds(SESSION, "42", statuses=["past"])
        ]
        self.assertEqual(sorted(past), sorted(["群友", "朋友"]))

    def test_identity_bonds_survive_a_relationship_change(self):
        """主人 / 家人不属于同类：她换亲密关系时它们不动。"""

        self.store.touch(SESSION, "42", "小明")
        self.store.note_bond(SESSION, "42", type="主人", asserted_by="她的判断")
        self.store.note_bond(SESSION, "42", type="朋友", asserted_by="她的判断")
        self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断")
        self.store.note_bond(SESSION, "42", type="家人", asserted_by="她的判断")

        current = [item["type"] for item in self.store.bonds(SESSION, "42", statuses=["current"])]
        self.assertEqual(sorted(current), sorted(["主人", "男友", "家人"]))

        self.store.note_bond(SESSION, "42", type="群友", asserted_by="她的判断")
        current = [item["type"] for item in self.store.bonds(SESSION, "42", statuses=["current"])]
        self.assertEqual(sorted(current), sorted(["主人", "群友", "家人"]))

    def test_view_hides_duplicate_close_bonds_from_older_data(self):
        """老数据里同类并排留着两条：展示时只留最有分量的那条（不动库）。"""

        self.store.touch(SESSION, "42", "小明")
        key = GROUP_KEY
        self.db.add_user_bond(
            group_id=key,
            user_id="42",
            type="朋友",
            slot="friend",
            status="current",
            since=1000.0,
            evidence="旧数据",
            confidence=1.0,
            asserted_by="她的判断",
        )
        self.db.add_user_bond(
            group_id=key,
            user_id="42",
            type="男友",
            slot="romance",
            status="current",
            since=2000.0,
            evidence="旧数据",
            confidence=1.0,
            asserted_by="她的判断",
        )

        view = self.store.view(SESSION, "42")
        self.assertEqual(view.affinities, ["男友"])
        # 库里那两条都还在（展示层收，不做破坏性写入）
        raw = [item["type"] for item in self.store.bonds(SESSION, "42", statuses=["current"])]
        self.assertEqual(sorted(raw), sorted(["朋友", "男友", "陌生人"]))

    def test_unique_slot_conflict_becomes_a_claim(self):
        """同一个槽位只能有一个：再来一个只记成"他自称"，不改关系。"""

        self.store.touch(SESSION, "42", "小明")
        self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断")
        result = self.store.note_bond(
            SESSION, "7", type="男友", asserted_by="他自称", evidence="我是你男朋友"
        )
        self.store.note_bond(SESSION, "7", type="男友", asserted_by="她的判断")
        self.store.note_bond(SESSION, "7", type="朋友", asserted_by="她的判断")

        claims = self.store.bonds(SESSION, "7", statuses=["claimed"])
        self.assertEqual([item["type"] for item in claims], ["男友"])
        self.assertEqual(claims[0]["asserted_by"], "他自称")
        self.assertEqual(result["action"], "claim")
        # 原来那个人还是男友
        current = [item["type"] for item in self.store.bonds(SESSION, "42", statuses=["current"])]
        self.assertIn("男友", current)

    def test_replace_policy_swaps_the_bond(self):
        self.store.touch(SESSION, "42", "小明")
        self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断")
        self.store.note_bond(SESSION, "7", type="朋友", asserted_by="她的判断")
        result = self.store.note_bond(
            SESSION,
            "7",
            type="男友",
            asserted_by="她的判断",
            policy="replace",
        )
        self.assertEqual(result["action"], "add")
        self.assertNotIn(
            "男友", [item["type"] for item in self.store.bonds(SESSION, "42", statuses=["current"])]
        )
        self.assertIn(
            "男友", [item["type"] for item in self.store.bonds(SESSION, "42", statuses=["past"])]
        )

    def test_accept_claim_and_close_bond(self):
        self.store.touch(SESSION, "42", "小明")
        self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断")
        claim = self.store.note_bond(
            SESSION, "7", type="男友", asserted_by="他自称"
        )
        refused = self.store.accept_claim(SESSION, "7", bond_id=claim["id"])
        self.assertFalse(refused["ok"])
        accepted = self.store.accept_claim(
            SESSION, "7", bond_id=claim["id"], policy="replace"
        )
        self.assertTrue(accepted["ok"], accepted)
        self.assertIn(
            "男友", [item["type"] for item in self.store.bonds(SESSION, "7", statuses=["current"])]
        )
        # 解除：变成"曾经"，保留日期
        bond = [item for item in self.store.bonds(SESSION, "7", statuses=["current"]) if item["type"] == "男友"][0]
        self.assertTrue(self.store.close_bond(SESSION, "7", bond_id=bond["id"]))
        past = [item for item in self.store.bonds(SESSION, "7", statuses=["past"]) if item["type"] == "男友"]
        self.assertTrue(past and past[0]["until"] > 0)

    def test_unknown_bond_is_rejected(self):
        self.store.touch(SESSION, "42", "小明")
        result = self.store.note_bond(SESSION, "42", type="天上下来的神仙")
        self.assertFalse(result["ok"])

    # ---------------- 好感度与亲密度上限 ----------------

    def test_affinity_clamps_and_logs(self):
        self.store.touch(SESSION, "42", "小明")
        up = self.store.adjust_affinity(SESSION, "42", 500, reason="聊得很开心")
        self.assertEqual(up["value"], 100.0)
        down = self.store.adjust_affinity(SESSION, "42", -1000, reason="被气到")
        self.assertEqual(down["value"], -100.0)
        logs = self.store.affinity_logs(SESSION, "42")
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["reason"], "被气到")

    def test_negative_affinity_lands_on_negative_level(self):
        self.store.touch(SESSION, "42", "小明")
        self.store.note_bond(SESSION, "42", type="敌人", asserted_by="她的判断")
        self.store.adjust_affinity(SESSION, "42", -60, reason="他欺负过她")
        view = self.store.view(SESSION, "42")
        self.assertEqual(view.level.name, "敌意")
        self.assertIn("抱抱", [self.store.action_label(item) for item in view.level.deny])

    def test_plain_bond_caps_intimacy_even_with_full_affinity(self):
        """普通关系聊再久也上不去：群友的好感拉满，也只能到"熟人"级。"""

        self.store.touch(SESSION, "42", "小明")
        self.store.note_bond(SESSION, "42", type="群友", asserted_by="她的判断")
        self.store.adjust_affinity(SESSION, "42", 100, reason="聊了很久")

        view = self.store.view(SESSION, "42")
        self.assertEqual(view.affinity, 100.0)
        self.assertEqual(view.level.name, "熟人")
        denied = [self.store.action_label(item) for item in view.level.deny]
        self.assertIn("亲亲", denied)

    def test_romance_bond_unlocks_the_top_level(self):
        self.store.touch(SESSION, "42", "小明")
        self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断")
        self.store.adjust_affinity(SESSION, "42", 100, reason="在一起了")
        view = self.store.view(SESSION, "42")
        self.assertEqual(view.level.name, "特别的人")
        self.assertEqual(view.affinities, ["男友"])

    def test_romance_bond_lifts_intimacy_even_with_low_affinity(self):
        """好感掉下来之后，男友这档该给的动作还在：关系本身也是一种态度（floor）。"""

        self.store.touch(SESSION, "42", "小明")
        self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断")
        # 绑上那一刻好感被抬到这一档的最低值（55），之后掉下来也还按这档对待
        self.store.adjust_affinity(SESSION, "42", -35, reason="冷淡了一阵")

        view = self.store.view(SESSION, "42")
        self.assertEqual(view.affinity, 20.0)
        self.assertEqual(view.level.name, "亲近")
        denied = [self.store.action_label(item) for item in view.level.deny]
        self.assertNotIn("抱抱", denied)
        self.assertNotIn("亲亲", denied)
        # 上限还在：好感没到 80，仍然不是"特别的人"
        self.assertLess(view.level_index, 6)

    def test_binding_a_relationship_lifts_affinity_to_its_floor(self):
        """绑关系时把好感抬到"这一档最低值"：绑了男友就不该还停在陌生人的好感上。"""

        self.store.touch(SESSION, "42", "小明")
        self.assertEqual(self.store.view(SESSION, "42").affinity, 0.0)

        self.store.note_bond(SESSION, "42", type="男友", asserted_by="她的判断")
        view = self.store.view(SESSION, "42")
        self.assertEqual(view.affinity, 55.0)
        self.assertEqual(view.level.name, "亲近")
        # 抬起来这一下要在好感日志里看得见
        rows = self.store.affinity_logs(SESSION, "42", limit=3)
        self.assertTrue(
            any("绑上关系" in str(item.get("reason") or "") for item in rows), rows
        )

    def test_binding_never_lowers_a_higher_affinity(self):
        """好感已经比这一档的最低值高：一个数都不动（那是相处养出来的）。"""

        self.store.touch(SESSION, "42", "小明")
        self.store.adjust_affinity(SESSION, "42", 80, reason="养了很久")
        self.store.note_bond(SESSION, "42", type="朋友", asserted_by="她的判断")
        self.assertEqual(self.store.view(SESSION, "42").affinity, 80.0)

    def test_default_bond_does_not_touch_a_fresh_affinity(self):
        """默认初始关系（陌生人 → -10）不该把新认识的人抬起来。"""

        self.store.touch(SESSION, "42", "小明")
        self.store.ensure_default_bond(SESSION, "42")
        self.assertEqual(self.store.view(SESSION, "42").affinity, 0.0)

    def test_accepting_a_claimed_bond_lifts_affinity_too(self):
        """「他自称是你男友 → 你认了」也是一次绑定，同样要抬。"""

        # 男友这个槽位已经有人了，第二个人只能先记成"他自称"
        self.store.touch(SESSION, "7", "小红")
        self.store.note_bond(SESSION, "7", type="男友", asserted_by="她的判断")
        self.store.touch(SESSION, "42", "小明")
        note = self.store.note_bond(SESSION, "42", type="男友", asserted_by="他自称")
        self.assertEqual(note["action"], "claim")
        self.assertEqual(self.store.view(SESSION, "42").affinity, 0.0)

        self.store.accept_claim(
            SESSION, "42", bond_id=int(note["id"]), policy="replace"
        )
        view = self.store.view(SESSION, "42")
        self.assertEqual(view.affinities, ["男友"])
        self.assertEqual(view.affinity, 55.0)

    def test_relationship_tables_have_no_dead_allow_field(self):
        """`allow` 从来没被运行时读过：模型里不该再有它，免得用户白配一遍。"""

        from core.models import IntimacyLevel

        self.assertFalse(hasattr(IntimacyLevel(), "allow"))

    def test_near_duplicate_facts_are_merged(self):
        """同一件事换个说法：合并，不新增。

        不然"他明天要去体检"能攒六条，把提示词里那一类的六个位置全占满。
        """

        self.store.touch(SESSION, "42", "小明")
        self.store.note_fact(
            SESSION, "42", text="明天要去体检，心里有点紧张", kind="近况", evidence="我明天要去体检"
        )
        first = self.store.note_fact(
            SESSION,
            "42",
            text="明天要去医院体检，心里有点紧张。",
            kind="近况",
            evidence="还得去体检",
        )
        self.assertEqual(first["action"], "merge")
        active = [item for item in self.store.facts(SESSION, "42") if item["status"] == "active"]
        self.assertEqual(len(active), 1)
        # 留信息更全的那一版
        self.assertIn("医院", str(active[0]["text"]))

    def test_facts_with_different_numbers_stay_apart(self):
        """数字必须一致才算同一件事：生日 3 月 5 号和 3 月 6 号是两条。"""

        self.store.touch(SESSION, "42", "小明")
        self.store.note_fact(
            SESSION, "42", text="生日是 3 月 5 号", kind="基本信息", evidence="我 3 月 5 号生日"
        )
        self.store.note_fact(
            SESSION, "42", text="生日是 3 月 6 号", kind="基本信息", evidence="说错了，是 3 月 6 号"
        )
        active = [item for item in self.store.facts(SESSION, "42") if item["status"] == "active"]
        self.assertEqual(len(active), 2)

    def test_ask_about_default_list_is_upgraded_but_custom_lists_are_not(self):
        """「想打听的事」默认清单：内置项跟着新版走，自己加过的项一个字都不动。"""

        from core.models import ASK_ABOUT_FIELDS_DEFAULT, parse_world

        old_default = ["性别", "生日", "年龄", "时区", "所在地"]
        for fields in (old_default, ["性别", "生日", "年龄", "所在地"]):
            raw = default_world()
            raw["profile"] = {"ask_about_fields": list(fields), "grudge_visible": True}
            world, _warnings = parse_world(raw)
            self.assertEqual(world.profile.ask_about_fields, ASK_ABOUT_FIELDS_DEFAULT)
            # 删掉的那个开关不该留在配置里
            self.assertNotIn("grudge_visible", dict(world.profile))

        custom = ["性别", "生日", "工作"]
        raw = default_world()
        raw["profile"] = {"ask_about_fields": list(custom)}
        world, _warnings = parse_world(raw)
        self.assertEqual(world.profile.ask_about_fields, custom)

    def test_dedupe_facts_cleans_up_what_is_already_stored(self):
        """存量清理：早先攒下来的重复说法合并成一条，其余记成"曾经"（不删）。"""

        self.store.touch(SESSION, "42", "小明")
        for text in ("抽卡又歪了", "抽卡又吃保底歪了", "抽卡又歪了，是个常年吃保底的非酋"):
            self.db.add_user_fact(
                group_id=GROUP_KEY,
                user_id="42",
                kind="近况",
                text=text,
                evidence="他的原话",
                status="active",
            )
        merged = self.store.dedupe_facts(SESSION, "42")
        self.assertEqual(merged, 2)
        active = [item for item in self.store.facts(SESSION, "42") if item["status"] == "active"]
        self.assertEqual(len(active), 1)
        # 留最全的那一条
        self.assertIn("非酋", str(active[0]["text"]))
        past = self.store.facts(SESSION, "42", statuses=["past"])
        self.assertEqual(len(past), 2)

    def test_negative_affinity_beats_the_relationship_floor(self):
        """下限只在好感不为负时生效：她可以对男友生气，惹到了就是冷淡。"""

        config = self.store.config
        cold = config.level_index_for(-30.0, cap=6, floor=5)
        self.assertEqual(config.levels[cold].name, "冷淡")
        # 同一份好感在非负时才会被下限抬起来
        lively = config.level_index_for(20.0, cap=6, floor=5)
        self.assertEqual(config.levels[lively].name, "亲近")

    def test_legacy_initial_flag_migrates_to_default_bond(self):
        """老配置用 bonds[].initial 标默认关系：读进来要搬进 default_bond（只留一个来源）。"""

        from core.models import ProfileConfig

        config = ProfileConfig.model_validate(
            {
                "default_bond": "陌生人",
                "bonds": [
                    {"name": "陌生人", "slot": "stranger", "cap": 2},
                    {"name": "朋友", "slot": "friend", "cap": 4, "initial": True},
                ],
            }
        )
        self.assertEqual(config.default_bond, "朋友")

    def test_default_bond_falls_back_to_the_first_row(self):
        """default_bond 写了个不在表里的名字：回落到第一条，别让建档直接失败。"""

        from core.models import ProfileConfig

        config = ProfileConfig.model_validate(
            {
                "default_bond": "查无此关系",
                "bonds": [{"name": "陌生人", "slot": "stranger", "cap": 2}],
            }
        )
        self.assertEqual(config.default_bond, "陌生人")

    def test_missing_floor_is_backfilled_from_builtin_table(self):
        """老配置的 bonds 没有 floor：按关系名从内置默认表补一份，"男友不让抱抱"才会自动修好。"""

        from core.models import ProfileConfig

        config = ProfileConfig.model_validate(
            {
                "bonds": [
                    {"name": "陌生人", "slot": "stranger", "cap": 2, "initial": True},
                    {"name": "男友", "slot": "romance", "cap": 6, "unique": True},
                    {"name": "我自己的关系", "slot": "mine", "cap": 5},
                ]
            }
        )
        floors = {item.name: item.floor for item in config.bonds}
        self.assertEqual(floors["男友"], 5)
        self.assertEqual(floors["陌生人"], 2)
        # 自定义关系名对不上内置表：保持 0，不瞎猜
        self.assertEqual(floors["我自己的关系"], 0)
        # 写过的 floor 不要被覆盖
        explicit = ProfileConfig.model_validate(
            {"bonds": [{"name": "男友", "slot": "romance", "cap": 6, "floor": 0}]}
        )
        self.assertEqual(explicit.bonds[0].floor, 0)

    def test_view_collects_dates_and_call_names(self):
        self.store.touch(SESSION, "42", "小明", now=1000.0)
        self.store.set_call_names(SESSION, "42", call_me="主人", call_him="小笨蛋")
        self.store.note_bond(SESSION, "42", type="主人", asserted_by="她的判断", now=2000.0)
        view = self.store.view(SESSION, "42")
        self.assertEqual(view.call_me, "主人")
        self.assertEqual(view.call_him, "小笨蛋")
        self.assertEqual(view.qq_name, "小明")
        bond = [item for item in self.store.bonds(SESSION, "42") if item["type"] == "主人"][0]
        self.assertEqual(bond["since"], 2000.0)
        self.assertEqual(view.days_known, 1)

    def test_digest_is_trimmed_to_config(self):
        self.store.touch(SESSION, "42", "小明")
        self.store.set_digest(SESSION, "42", "很" * 200)
        digest = self.store.profile(SESSION, "42")["digest"]
        self.assertEqual(len(digest), self.world.profile.digest_chars)

    # ---------------- 好感的"日常"三条 ----------------

    def test_presence_affinity_has_cooldown_and_daily_cap(self):
        """混脸熟：露个面就加一点，但同一人 10 分钟只算一次，每天还有上限。"""

        self.store.touch(SESSION, "42", "小明", now=1000.0)
        first = self.store.note_contact(SESSION, "42", now=1000.0)
        again = self.store.note_contact(SESSION, "42", now=1010.0)  # 冷却里
        self.assertGreater(first, 0)
        self.assertEqual(again, 0.0)
        # 冷却过了再加一次
        self.assertGreater(self.store.note_contact(SESSION, "42", now=1000.0 + 11 * 60), 0)
        # 一路刷到当天上限
        stamp = 1000.0 + 11 * 60
        for _ in range(60):
            stamp += 11 * 60
            self.store.note_contact(SESSION, "42", now=stamp)
        affinity = float(self.store.profile(SESSION, "42")["affinity"])
        cap = float(self.world.profile.presence_daily_max)
        self.assertLessEqual(affinity, cap + 1e-6)

    def test_reply_affinity_is_clamped_per_turn_and_per_day(self):
        """模型给的好感：每轮削到上限，每天每人也有总量上限（私聊刷不满）。"""

        self.store.touch(SESSION, "42", "小明", now=1000.0)
        first = self.store.apply_reply_affinity(SESSION, "42", 9.0, reason="夸我", now=1000.0)
        self.assertEqual(first, float(self.world.profile.affinity_reply_max))
        total = first
        for index in range(20):
            total += self.store.apply_reply_affinity(
                SESSION, "42", 2.0, reason="接着夸", now=1000.0 + index
            )
        self.assertLessEqual(total, float(self.world.profile.affinity_daily_max) + 1e-6)

    def test_affinity_decays_toward_zero_once_a_day(self):
        """每天朝 0 回落一点：同一天只结算一次，负好感也会慢慢被忘掉。"""

        self.store.touch(SESSION, "42", "小明", now=1000.0)
        self.store.adjust_affinity(SESSION, "42", 50, reason="聊得多", now=1000.0)
        first = self.store.decay_affinity(SESSION, now=1000.0)
        self.assertEqual(len(first), 1)
        value = float(self.store.profile(SESSION, "42")["affinity"])
        self.assertAlmostEqual(value, 50 - float(self.world.profile.affinity_decay_per_day), 4)
        # 同一天再来一次：不动
        self.assertEqual(self.store.decay_affinity(SESSION, now=1000.0 + 60), [])
        # 第二天：再淡一点
        self.store.decay_affinity(SESSION, now=1000.0 + 86400)
        self.assertLess(float(self.store.profile(SESSION, "42")["affinity"]), value)

    def test_decay_never_crosses_zero(self):
        self.store.touch(SESSION, "42", "小明", now=1000.0)
        self.store.adjust_affinity(SESSION, "42", -0.05, reason="有点不爽", now=1000.0)
        self.store.decay_affinity(SESSION, now=1000.0)
        self.assertEqual(float(self.store.profile(SESSION, "42")["affinity"]), 0.0)

    def test_disabled_profile_writes_nothing(self):
        self.world.profile.enabled = False
        self.assertIsNone(self.store.touch(SESSION, "42", "小明"))
        result = self.store.note_fact(SESSION, "42", text="喜欢猫")
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.list_people(SESSION), [])


if __name__ == "__main__":
    unittest.main()
