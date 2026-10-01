"""验证 main.py 能被 AstrBot 正确导入与实例化（用桩模块替代 AstrBot）。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN_DIR)
sys.path.insert(0, HERE)

import fake_astrbot  # noqa: E402

fake_astrbot.install()

# 让实例化测试写到临时目录，不碰插件目录里的真实数据
_TMP_DIR = tempfile.mkdtemp(prefix="virtual-world-test-")
os.environ["VIRTUAL_WORLD_DATA_DIR"] = _TMP_DIR


def load_plugin_module():
    """把插件目录当作包加载，这样 main.py 里的相对导入才能生效。"""

    if "vw_plugin_under_test" in sys.modules:
        return sys.modules["vw_plugin_under_test"]
    spec = importlib.util.spec_from_file_location(
        "vw_plugin_under_test",
        os.path.join(PLUGIN_DIR, "main.py"),
        submodule_search_locations=[PLUGIN_DIR],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["vw_plugin_under_test"] = module
    spec.loader.exec_module(module)
    return module


class _ReplyPlain:
    def __init__(self, text: str) -> None:
        self.text = text


class _ReplyResult:
    """够用的 MessageEventResult 替身：只记 Plain 段。"""

    def __init__(self) -> None:
        self.chain: list[_ReplyPlain] = []

    def message(self, text: str) -> "_ReplyResult":
        self.chain.append(_ReplyPlain(text))
        return self

    def set_result_content_type(self, _typ) -> "_ReplyResult":
        return self


class _ReplyEvent:
    def __init__(self) -> None:
        self._result = None

    def get_result(self):
        return self._result

    def set_result(self, result) -> None:
        self._result = result

    def clear_result(self) -> None:
        self._result = None


def _install_reply_hook_stubs() -> None:
    """装上 AstrBot 回复钩子相关的桩模块（这些模块只在桥接里懒加载）。"""

    core = types.ModuleType("astrbot.core")
    pipeline = types.ModuleType("astrbot.core.pipeline")
    context_utils = types.ModuleType("astrbot.core.pipeline.context_utils")

    async def call_event_hook(event, hook_type, *args, **kwargs):
        return False

    context_utils.call_event_hook = call_event_hook
    provider = types.ModuleType("astrbot.core.provider")
    entities = types.ModuleType("astrbot.core.provider.entities")

    class LLMResponse:
        def __init__(self, role: str = "", completion_text: str = "", **_kwargs) -> None:
            self.role = role
            self.completion_text = completion_text

    entities.LLMResponse = LLMResponse
    star = types.ModuleType("astrbot.core.star")
    star_handler = types.ModuleType("astrbot.core.star.star_handler")

    class EventType:
        OnLLMResponseEvent = "llm_response"
        OnDecoratingResultEvent = "decorating_result"

    star_handler.EventType = EventType
    message = types.ModuleType("astrbot.core.message")
    result_module = types.ModuleType("astrbot.core.message.message_event_result")

    class ResultContentType:
        LLM_RESULT = "llm_result"

    result_module.MessageEventResult = _ReplyResult
    result_module.ResultContentType = ResultContentType

    sys.modules.update(
        {
            "astrbot.core": core,
            "astrbot.core.pipeline": pipeline,
            "astrbot.core.pipeline.context_utils": context_utils,
            "astrbot.core.provider": provider,
            "astrbot.core.provider.entities": entities,
            "astrbot.core.star": star,
            "astrbot.core.star.star_handler": star_handler,
            "astrbot.core.message": message,
            "astrbot.core.message.message_event_result": result_module,
        }
    )


class TestMainImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def _plugin(self):
        return self.module.VirtualWorldPlugin(
            self.module.Context(),
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )

    def test_plugin_class_is_registered(self):
        self.assertTrue(hasattr(self.module, "VirtualWorldPlugin"))
        self.assertEqual(
            self.module.VirtualWorldPlugin.registered_name,
            "astrbot_plugin_virtual_world",
        )

    def test_hooks_and_commands_exist(self):
        cls = self.module.VirtualWorldPlugin
        for name in ("on_llm_request", "on_any_message", "cmd_vw", "initialize", "terminate"):
            self.assertTrue(callable(getattr(cls, name)), name)

    def test_plugin_instantiates_and_registers_web_apis(self):
        context = self.module.Context()
        config = self.module.AstrBotConfig(
            {
                "enabled": True,
                "web_enabled": True,
                "tick_interval": 60,
                "decider_interval": 300,
            }
        )
        plugin = self.module.VirtualWorldPlugin(context, config)
        self.assertTrue(plugin.engine.world.nodes)
        self.assertTrue(plugin.engine.world.actions)
        routes = [item[0] for item in context.registered_web_apis]
        self.assertTrue(any(route.endswith("/config") for route in routes))
        self.assertTrue(any(route.endswith("/state") for route in routes))
        self.assertGreaterEqual(len(routes), 20)
        plugin.db.raw.close()

    def test_bridge_does_not_glue_messages_together(self):
        """回复钩子桥接不能把多条消息粘成一条（say 的话和动作文案要分开）。"""

        plugin = self.module.VirtualWorldPlugin(
            self.module.Context(),
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        _install_reply_hook_stubs()
        original_plain = self.module.Plain
        self.module.Plain = _ReplyPlain
        try:
            event = _ReplyEvent()
            messages = ["总算迭代得差不多了？\n行吧行吧，批准你抱一下😤", "（小鲸鱼 | 在卧室抱了你一下）"]
            out = asyncio.run(plugin._bridge_reply_hooks(event, list(messages)))
        finally:
            self.module.Plain = original_plain
            plugin.db.raw.close()
        self.assertEqual(out, messages)

    def test_reply_is_split_into_separate_messages(self):
        """一段一条消息：说的话和动作文案不该挤进同一条。"""

        plugin = self.module.VirtualWorldPlugin
        split = plugin.split_messages
        self.assertEqual(
            split(["总算迭代得差不多了？\n行吧行吧，批准你抱一下😤", "（小鲸鱼 | 在卧室抱了你一下）"]),
            [
                "总算迭代得差不多了？",
                "行吧行吧，批准你抱一下😤",
                "（小鲸鱼 | 在卧室抱了你一下）",
            ],
        )
        # 没有换行时原样保留，空行丢掉
        self.assertEqual(split(["就一句"]), ["就一句"])
        self.assertEqual(split(["第一句\n\n第二句"]), ["第一句", "第二句"])
        self.assertEqual(split(["", "  "]), [])

    def test_debug_echo_is_sent_in_the_order_it_happened(self):
        """先调工具、再说话：调试回显要插在她开口之前，而不是统一堆到最后。"""

        from core.engine import TickOutcome

        plugin = self.module.VirtualWorldPlugin
        context = self.module.Context()
        instance = plugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        outcome = TickOutcome(session_id="aiocqhttp:GroupMessage:1001")
        outcome.add_debug("🔧 调用「web_search」")
        outcome.add_debug("📥 「web_search」返回：三条新闻")
        outcome.messages.append("查到啦，今天有条开源模型的消息。\n顺带说一句，别熬夜。")
        try:
            merged = instance._merge_with_echo(
                list(outcome.messages), outcome, list(outcome.debug_messages)
            )
        finally:
            instance.db.raw.close()
        self.assertEqual(
            merged,
            [
                "🔧 调用「web_search」",
                "📥 「web_search」返回：三条新闻",
                "查到啦，今天有条开源模型的消息。",
                "顺带说一句，别熬夜。",
            ],
        )

    # ---------------- @ 记录 / 戳一戳 / 管理员名单 ----------------

    def test_mention_note_marks_herself(self):
        """别人 @ 她的时候要写清楚"@ 的是你"：她并不知道自己的 QQ 号。"""

        class At:
            def __init__(self, qq, name=""):
                self.qq = qq
                self.name = name

        class _Msg:
            def __init__(self, items):
                self.message = items

        class _Ev:
            def __init__(self, items):
                self.message_obj = _Msg(items)

            def get_self_id(self):
                return "10001"

        note = self.module._mention_note(_Ev([At("10001", "小鲸鱼"), At("42", "小明")]))
        self.assertIn("你（10001）", note)
        self.assertIn("小明(42)", note)
        # @ 的是别人：得写清楚"不是在 @ 你"，不然她会当成在叫她
        self.assertIn("不是在 @ 你", note)
        # 正文里那一下可能被平台省掉，得提醒她这条确实点到了自己
        self.assertIn("确实 @ 了你", note)
        self.assertEqual(self.module._mention_note(_Ev([])), "")

    def test_mention_note_does_not_repeat_a_numeric_name(self):
        """协议端没解析出名字时 name 就是号码：别写成 2829449702(2829449702)。"""

        class At:
            def __init__(self, qq, name=""):
                self.qq = qq
                self.name = name

        class _Msg:
            def __init__(self, items):
                self.message = items

        class _Ev:
            def __init__(self, items):
                self.message_obj = _Msg(items)

            def get_self_id(self):
                return "10001"

        note = self.module._mention_note(
            _Ev([At("10001", "10001"), At("42", "42")])
        )
        self.assertIn("你（10001）", note)
        self.assertIn("42", note)
        self.assertNotIn("10001(10001)", note)
        self.assertNotIn("42(42)", note)

    def test_soft_wake_does_not_claim_she_was_mentioned(self):
        """意图路由补的那个 @ 是假的：不能记成"这条消息 @ 了你"。"""

        class At:
            def __init__(self, qq, name=""):
                self.qq = qq
                self.name = name

        class _Msg:
            def __init__(self, items):
                self.message = items

        class _Ev:
            def __init__(self, items, soft=False):
                self.message_obj = _Msg(items)
                self._extras = {"intent_router_no_at": True} if soft else {}

            def get_self_id(self):
                return "2829449702"

            def get_extra(self, key, default=None):
                return self._extras.get(key, default)

        # 路由补的 @ 她自己（外加正文里真的 @ 了别人）：只留别人那一段
        event = _Ev([At("2829449702", "2829449702"), At("42", "小明")], soft=True)
        note = self.module._mention_note(event)
        self.assertNotIn("你（", note.replace("没 @ 你", ""))
        self.assertIn("小明(42)", note)
        self.assertNotIn("2829449702(2829449702)", note)

        # 只有那个假 @ 的时候：明说"没 @ 你，是顺着话题说到你的"
        alone = self.module._mention_note(_Ev([At("2829449702", "2829449702")], soft=True))
        self.assertIn("没 @ 你", alone)
        self.assertNotIn("确实 @ 了你", alone)

        # 结构化那份也要剔掉它，免得渲染成「→ 你」
        targets = self.module._at_targets(_Ev([At("2829449702", "2829449702")], soft=True))
        self.assertEqual(targets, [])

    def test_mention_note_about_someone_else_only(self):
        """整条消息只 @ 了别人：同样要标出来，别让她以为是叫自己。"""

        class At:
            def __init__(self, qq, name=""):
                self.qq = qq
                self.name = name

        class _Msg:
            def __init__(self, items):
                self.message = items

        class _Ev:
            def __init__(self, items):
                self.message_obj = _Msg(items)

            def get_self_id(self):
                return "10001"

        note = self.module._mention_note(_Ev([At("42", "小明")]))
        self.assertIn("这条消息 @ 了：小明(42)", note)
        self.assertIn("不是在 @ 你", note)
        self.assertNotIn("你（", note)

    # ---------------- 「指令触发」型动作：参数类型要按 AstrBot 的规矩还原 ----------------

    def test_command_params_resolve_string_annotations(self):
        """注解写成字符串时（`from __future__ import annotations`）要还原成真类型。

        以前直接 `inspect.signature(...)`（没有 eval_str），注解是字符串就被当成
        "默认值"原样透传——需要 MessageChain 这类对象的指令会拿到一个 str，
        接着就报 `'str' object has no attribute 'chain'`。
        """

        record = types.SimpleNamespace(handler=_weather_like_command)
        params = self.module.AstrBotCommands._params(record, _FilterStub(), "/天气 北京")

        self.assertIn("chain", params)
        self.assertIsInstance(params["chain"], _Chain)
        self.assertEqual(params["chain"].chain, ["北京"])

    def test_command_params_prefer_the_filters_resolved_types(self):
        """AstrBot 自己解析好的 `handler_params` 优先（它已经 eval 过注解）。"""

        filter_stub = _FilterStub({"chain": _Chain})
        record = types.SimpleNamespace(handler=_weather_like_command)
        params = self.module.AstrBotCommands._params(record, filter_stub, "/天气 上海")

        self.assertIsInstance(params["chain"], _Chain)
        self.assertEqual(params["chain"].chain, ["上海"])

    def test_command_failure_reports_where_it_broke(self):
        """别的插件的指令跑挂时，错误里要带"哪个文件哪一行"。"""

        def boom() -> None:
            raise ValueError("炸了")

        try:
            boom()
        except ValueError as exc:
            where = self.module._exception_where(exc)
        self.assertIn("test_main_import.py", where)
        self.assertIn("boom", where)

    def test_one_message_is_recorded_once_when_the_second_hook_lost_the_at(self):
        """真实管线的两个钩子：前一个带 @ 注释，后一个只剩正文，不能记成两遍。

        （「亲亲」这种短消息最容易中招：正文比对要求最少 4 个字，靠它认不出来，
        于是留档里出现两行，她会以为对方连着说了两次。）
        """

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        session = "aiocqhttp:GroupMessage:1"
        plugin.store.add_session(session, session_type="group", platform="aiocqhttp")
        plugin.engine.reload_config()

        event = _GateEvent("亲亲", session=session)
        asyncio.run(plugin.on_any_message(event))
        # 管线后面的某一步把 @ 段清掉了：第二个钩子拿到的只剩正文
        event.message_obj.message = []
        asyncio.run(
            plugin.engine.handle_incoming(
                self.module.MessageContext(
                    session_id=session,
                    user_id="42",
                    user_name="tester",
                    text="亲亲",
                    is_wake=True,
                    is_mentioned=True,
                )
            )
        )

        async def lines() -> list[str]:
            async with plugin.engine.session_state(session) as state:
                return [str(item.get("text") or "") for item in state.recent_chat]

        texts = asyncio.run(lines())
        self.assertEqual(len([item for item in texts if "亲亲" in item]), 1, texts)
        plugin.db.raw.close()

    def test_poke_falls_back_to_the_platform_api(self):
        """消息段发不出去时，改用协议端的 send_poke。"""

        plugin = self.module.VirtualWorldPlugin
        instance = plugin(
            self.module.Context(),
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        calls: list[tuple[str, dict]] = []

        class _Bot:
            async def call_action(self, name, **kwargs):
                calls.append((name, kwargs))
                return {"status": "ok"}

        class _Ev:
            bot = _Bot()

        async def scenario():
            async def boom(*_args, **_kwargs):
                raise RuntimeError("消息段这条路不通")

            instance.context.send_message = boom
            instance._last_events["aiocqhttp:GroupMessage:1001"] = _Ev()
            return await instance.messenger.poke("aiocqhttp:GroupMessage:1001", "3397734465")

        try:
            result = asyncio.run(scenario())
        finally:
            instance.db.raw.close()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(calls[0][0], "send_poke")
        self.assertEqual(calls[0][1]["user_id"], 3397734465)
        self.assertEqual(calls[0][1]["group_id"], 1001)

    def test_admin_ids_can_run_management_commands(self):
        """「全局设置 → 管理员 QQ」里的人也能用管理指令。"""

        plugin = self.module.VirtualWorldPlugin
        instance = plugin(
            self.module.Context(),
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )

        class _Ev:
            def __init__(self, sender: str, admin: bool):
                self._sender = sender
                self._admin = admin

            def is_admin(self):
                return self._admin

            def get_sender_id(self):
                return self._sender

        try:
            instance.engine.world.admin_ids = ["3525522255"]
            self.assertTrue(instance._can_admin(_Ev("9999", True)))
            self.assertTrue(instance._can_admin(_Ev("3525522255", False)))
            self.assertFalse(instance._can_admin(_Ev("9999", False)))
        finally:
            instance.db.raw.close()

    def test_nickname_buttons_do_not_wipe_session_state(self):
        """「恢复原名」以前发的动作名是 reset，正好撞上"删掉整个会话状态"的那个动作。

        结果就是：点一下恢复原名，实时状态里的位置、数值、计划、留档全没了。
        """

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        session = "aiocqhttp:GroupMessage:1001"
        plugin.store.add_session(session, session_type="group", platform="aiocqhttp")
        request = self.module.request

        async def scenario():
            async with plugin.engine.session_state(session) as state:
                state.node_id = "kitchen"
                state.energy = 0.2
                state.bot_base_nickname = "小鲸鱼"
            request._json = {"session": session, "action": "reset_nickname"}
            renamed = await plugin.api_state_action()
            after_rename = await plugin.db.call("get_state", session)
            # 老名字（那个会删数据的动作）必须变成"未知动作"
            request._json = {"session": session, "action": "reset"}
            legacy = await plugin.api_state_action()
            after_legacy = await plugin.db.call("get_state", session)
            return renamed, after_rename, legacy, after_legacy

        renamed, after_rename, legacy, after_legacy = asyncio.run(scenario())
        self.assertEqual(renamed["status_code"], 200)
        self.assertIn("原名", str(renamed["data"].get("note", "")))
        self.assertIsNotNone(after_rename, "恢复原名不该删掉会话状态")
        self.assertIn("小鲸鱼", str(after_rename))
        self.assertEqual(legacy["status_code"], 400, "reset 这个动作已经改名，不该再删数据")
        self.assertIsNotNone(after_legacy)
        plugin.db.raw.close()

    def test_send_failure_is_not_retried(self):
        """发送失败就当它失败：同批不再试、冷却期内也不再碰平台。"""

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        calls: list[str] = []

        async def boom(session, chain) -> bool:
            calls.append(session)
            raise RuntimeError("Timeout: NTEvent NodeKernelMsgService/sendMsg")

        plugin.context.send_message = boom
        messenger = plugin.messenger

        self.assertFalse(asyncio.run(messenger.send_text("s1", ["第一句", "第二句"])))
        self.assertEqual(len(calls), 1)  # 同一批里后面的不再尝试
        self.assertTrue(messenger.blocked("s1"))

        self.assertFalse(asyncio.run(messenger.send_text("s1", ["第三句"])))
        self.assertEqual(len(calls), 1)  # 冷却期内直接放弃，不再碰平台

        note = messenger.take_fail_note("s1")
        self.assertIn("Timeout", note)
        self.assertEqual(messenger.take_fail_note("s1"), "")  # 每个窗口只报一次
        plugin.db.raw.close()

    def test_send_success_clears_the_cooldown(self):
        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        messenger = plugin.messenger
        self.assertTrue(asyncio.run(messenger.send_text("s1", ["你好"])))
        self.assertFalse(messenger.blocked("s1"))
        plugin.db.raw.close()

    def test_reply_path_does_not_send_twice(self):
        """接管回复以前会"event.send 失败就换一条通道再发一次"，遇到超时已送达就会重复。"""

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        fallbacks: list[str] = []

        async def fallback(session, chain) -> bool:
            fallbacks.append(session)
            return True

        plugin.context.send_message = fallback
        event = _GateEvent("在吗")

        async def boom(chain):
            raise RuntimeError("Timeout: NTEvent NodeKernelMsgService/sendMsg")

        event.send = boom
        asyncio.run(plugin._send_reply(event, ["我在这儿呢"]))
        self.assertEqual(fallbacks, [])
        self.assertTrue(plugin.messenger.blocked(event.unified_msg_origin))
        plugin.db.raw.close()

    def test_reply_can_quote_the_message_that_triggered_her(self):
        """「回复时引用」开着时只有第一条带引用段；平台不认引用就整批不带。"""

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        event = _QuoteEvent("在吗")

        # 默认「智能」：单独一句对答不顶引用
        asyncio.run(plugin._send_reply(event, ["第一句", "第二句"], quote=True))
        self.assertEqual(event.sent[0].chain, [{"type": "plain", "text": "第一句"}])
        self.assertEqual(event.sent[1].chain, [{"type": "plain", "text": "第二句"}])

        # 总是引用：引用段在第一条，后面几条照旧
        plugin.engine.world.reply_style.quote_mode = "always"
        event.sent.clear()
        asyncio.run(plugin._send_reply(event, ["第一句", "第二句"], quote=True))
        self.assertEqual(
            event.sent[0].chain,
            [{"type": "reply", "id": "7788"}, {"type": "plain", "text": "第一句"}],
        )
        self.assertEqual(event.sent[1].chain, [{"type": "plain", "text": "第二句"}])

        # 智能 + 这一轮回的是一串消息（引擎给 always）：引用
        plugin.engine.world.reply_style.quote_mode = "smart"
        event.sent.clear()
        asyncio.run(
            plugin._send_reply(
                event, ["第一句"], quote=True, quote_mode_hint="always"
            )
        )
        self.assertEqual(
            event.sent[0].chain,
            [{"type": "reply", "id": "7788"}, {"type": "plain", "text": "第一句"}],
        )
        # 只发了图、没有说话：图片那条去引用
        event.sent.clear()
        asyncio.run(
            plugin._send_reply(
                event,
                [],
                quote=True,
                quote_mode_hint="always",
                images=["https://img/1.png"],
            )
        )
        self.assertEqual(
            event.sent[0].chain,
            [{"type": "reply", "id": "7788"}, {"type": "image", "file": "https://img/1.png"}],
        )

        # 不走这条路的（调试回显等）不带引用
        event.sent.clear()
        asyncio.run(plugin._send_reply(event, ["（伸了个懒腰）"]))
        self.assertEqual(event.sent[0].chain, [{"type": "plain", "text": "（伸了个懒腰）"}])

        # 平台没有引用段（例如 Telegram）：跳过引用，消息照发
        other = _QuoteEvent("在吗", platform="telegram")
        asyncio.run(plugin._send_reply(other, ["第一句"], quote=True))
        self.assertEqual(other.sent[0].chain, [{"type": "plain", "text": "第一句"}])
        plugin.db.raw.close()

    def test_quoted_message_says_who_sent_it(self):
        """引用消息要写清是谁发的；引用她自己发的图时写成「你自己」。"""

        event = _QuoteEvent("这是啥")

        event.message_obj.message = [
            Reply(message_str="晚上吃鱼", sender_id="42", sender_nickname="小明")
        ]
        self.assertEqual(self.module._quoted_text(event), "小明：晚上吃鱼")

        # 她自己的消息（self_id 是 1）：昵称她不认识，要写成「你自己」
        event.message_obj.message = [
            Reply(
                message_str="本小姐发的",
                sender_id="1",
                sender_nickname="凶猛蓝色虎鲸",
            )
        ]
        self.assertEqual(self.module._quoted_text(event), "你自己：本小姐发的")

        # 引用的是一条只有图片的消息：不能变成空串
        event.message_obj.message = [
            Reply(chain=[Image()], sender_id="1", sender_nickname="凶猛蓝色虎鲸")
        ]
        self.assertEqual(self.module._quoted_text(event), "你自己：［图片］")

    def test_a_quote_of_a_message_in_the_records_only_shows_its_head(self):
        """引用的那条聊天记录里就有：只点出开头，让她自己往上对照（不再抄一遍原文）。"""

        session = "aiocqhttp:GroupMessage:probe-quote-inside"
        plugin, _context, _session = self._plugin_with_session(session=session)
        long_one = "2.0更新老多东西了 1.随机事件系统 和事件配套的能力值系统 " * 4

        async def drive():
            # 群里先发了这条（会进聊天留档）
            await plugin.on_any_message(_GateEvent(long_one, session=session))
            event = _GateEvent("如何评价", session=session)
            event.message_obj.message = [
                At("1"),
                Reply(message_str=long_one, sender_id="42", sender_nickname="不相疑"),
            ]
            return await plugin._quoted_note(event)

        note = asyncio.run(drive())
        plugin.db.raw.close()
        self.assertIn("不相疑", note)
        self.assertIn(long_one[:40], note)
        self.assertIn("不是他这次打的字", note)
        # 原文不再重复一份：这条注解比原文短得多
        self.assertLess(len(note), len(long_one))

    def test_a_quote_of_a_message_outside_the_records_shows_the_full_text(self):
        """引用的是很久以前、聊天记录里已经没有的消息：把原文写进正文。"""

        session = "aiocqhttp:GroupMessage:probe-quote-outside"
        plugin, context, _session = self._plugin_with_session(session=session)
        prompts: list[str] = []

        async def llm_generate(**kwargs):
            prompts.append(str(kwargs.get("prompt") or ""))

            class _Response:
                completion_text = '{"actions":[{"type":"say","messages":["好"]}]}'

            return _Response()

        context.llm_generate = llm_generate
        old_one = "很久以前说的那句：把锅端上去之前先看一眼火"
        event = _GateEvent("还记得这个吗", session=session)
        event.message_obj.message = [
            At("1"),
            Reply(message_str=old_one, sender_id="42", sender_nickname="不相疑"),
        ]

        try:
            asyncio.run(plugin.on_llm_request(event, _GateRequest("还记得这个吗")))
        finally:
            plugin.db.raw.close()
        prompt = prompts[-1]
        self.assertIn(old_one, prompt)
        self.assertIn("引用", prompt)
        self.assertIn("不是他这次打的字", prompt)

    def test_a_quote_longer_than_the_line_limit_is_written_out(self):
        """记录里那条本身就长到会被截断：还是把原文写出来，不然她不知道引的是哪段。"""

        session = "aiocqhttp:GroupMessage:probe-quote-clipped"
        plugin, _context, _session = self._plugin_with_session(session=session)
        plugin.engine.world.context.chat_line_chars = 60  # 记录里那行会被截断
        long_one = "更新公告的内容" * 30

        async def drive():
            await plugin.on_any_message(_GateEvent(long_one, session=session))
            event = _GateEvent("如何评价", session=session)
            event.message_obj.message = [
                At("1"),
                Reply(message_str=long_one, sender_id="42", sender_nickname="不相疑"),
            ]
            return await plugin._quoted_note(event)

        note = asyncio.run(drive())
        plugin.db.raw.close()
        self.assertIn("原文", note)
        self.assertNotIn("照上面那条看", note)

    def test_a_very_long_quote_is_cut_with_a_note(self):
        """超长引用按上限截断，并标明还剩多少字。"""

        session = "aiocqhttp:GroupMessage:probe-quote-long"
        plugin, _context, _session = self._plugin_with_session(session=session)
        plugin.engine.world.context.quote_chars = 60
        old_one = "以前那句很长的话" * 20
        event = _GateEvent("这个呢", session=session)
        event.message_obj.message = [
            At("1"),
            Reply(message_str=old_one, sender_id="42", sender_nickname="不相疑"),
        ]
        note = asyncio.run(plugin._quoted_note(event))
        plugin.db.raw.close()
        self.assertIn("（这条还有", note)
        self.assertNotIn(old_one, note)

    def test_reply_posts_generated_images_after_her_words(self):
        """生图结果跟着回复一起发：先说内容，再把图贴上去（多张放同一条消息）。"""

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        event = _QuoteEvent("给我画张图")

        asyncio.run(
            plugin._send_reply(
                event,
                ["画好了～", "看看喜不喜欢"],
                images=["https://img/1.png", "https://img/2.png"],
            )
        )
        self.assertEqual(event.sent[0].chain, [{"type": "plain", "text": "画好了～"}])
        self.assertEqual(
            event.sent[1].chain, [{"type": "plain", "text": "看看喜不喜欢"}]
        )
        self.assertEqual(
            event.sent[2].chain,
            [
                {"type": "image", "file": "https://img/1.png"},
                {"type": "image", "file": "https://img/2.png"},
            ],
        )

        # 她这轮什么都没说（只有图）：图照样发
        event.sent.clear()
        asyncio.run(plugin._send_reply(event, [], images=["https://img/3.png"]))
        self.assertEqual(
            event.sent[0].chain, [{"type": "image", "file": "https://img/3.png"}]
        )

        # 什么都没有：什么都不发（别凭空冒一条空气泡）
        event.sent.clear()
        asyncio.run(plugin._send_reply(event, []))
        self.assertEqual(event.sent, [])
        plugin.db.raw.close()

    def _group_with_private(
        self, plugin, *, group: str, private: str, note: str = "柏峰礼主人"
    ) -> None:
        """把群和私聊编成一组（群当主会话），模拟"一个她，两个说话的地方"。"""

        plugin.store.add_session(group, session_type="group", platform="aiocqhttp")
        raw = plugin.store.raw_sessions()
        for item in raw.get("sessions") or []:
            if str(item.get("session_id")) == private:
                item["type"] = "private"
                item["note"] = note
        raw["groups"] = [
            {
                "id": "team",
                "name": "一起",
                "sessions": [group, private],
                "main_session": group,
            }
        ]
        plugin.store.save_sessions(raw)
        plugin.engine.reload_config()

    def test_a_private_round_is_logged_and_answered_where_it_happened(self):
        """私聊里被搭话：话回私聊，日志也要记明是在私聊发生的那一轮。"""

        group = "aiocqhttp:GroupMessage:236074560"
        private = "aiocqhttp:PrivateMessage:2692047521"
        plugin, context, _session = self._plugin_with_session(session=private)
        self._group_with_private(plugin, group=group, private=private)
        self._stub_say_reply(context, "这就去")

        event = _GateEvent("你去群里骂他一下", session=private)
        try:
            asyncio.run(plugin.on_llm_request(event, _GateRequest("你去群里骂他一下")))
            events = asyncio.run(
                plugin.db.call("query_events", session_id=group, limit=20)
            )
            private_events = asyncio.run(
                plugin.db.call("query_events", session_id=private, limit=20)
            )
        finally:
            plugin.db.raw.close()

        # 她的话回在私聊里
        self.assertEqual(
            [seg.get("text") for chain in event.sent for seg in chain.chain], ["这就去"]
        )
        kinds = [item["event_type"] for item in events]
        self.assertIn("user_message", kinds)
        self.assertIn("reply", kinds)
        # 这一轮发生在私聊：日志里要能看出来
        where = [
            str((item.get("detail") or {}).get("session") or "")
            for item in events
            if item["event_type"] in ("user_message", "reply")
        ]
        self.assertTrue(where, kinds)
        self.assertTrue(all("私聊" in text for text in where), where)
        self.assertEqual(private_events, [])

    def test_a_line_for_another_session_of_the_group_reaches_there(self):
        """她在私聊里被要求"去群里说一句"：那句要真的发到群，不会掉在原地。"""

        group = "aiocqhttp:GroupMessage:236074560"
        private = "aiocqhttp:PrivateMessage:2692047521"
        plugin, context, _session = self._plugin_with_session(session=private)
        self._group_with_private(plugin, group=group, private=private)

        async def llm_generate(**_kwargs):
            class _Response:
                completion_text = (
                    '{"actions":[{"type":"say","messages":["大白你等着"],'
                    '"send_to":"群 236074560"}]}'
                )

            return _Response()

        context.llm_generate = llm_generate
        sent: list[tuple[str, list[str]]] = []

        async def fake_send_text(session_id, messages):
            sent.append((str(session_id), list(messages)))
            return True

        plugin.messenger.send_text = fake_send_text
        event = _GateEvent("你去群里骂他一下", session=private)
        try:
            asyncio.run(plugin.on_llm_request(event, _GateRequest("你去群里骂他一下")))
        finally:
            plugin.db.raw.close()

        self.assertEqual(sent, [(group, ["大白你等着"])])
        self.assertEqual(
            [seg.get("text") for chain in event.sent for seg in chain.chain], []
        )

    def test_live_say_is_wired_to_the_real_time_channel(self):
        """慢动作之前先说的话：插件把它接到实时通道，并且真的发出去了。"""

        plugin, _context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-live-wire"
        )
        self.assertEqual(plugin.engine.say_sink, plugin._send_live_say)
        sent: list[tuple[str, list[str]]] = []

        async def fake_send_text(session_id, messages):
            sent.append((str(session_id), list(messages)))
            return True

        plugin.messenger.send_text = fake_send_text
        try:
            self.assertTrue(
                asyncio.run(plugin._send_live_say(session, "坐好等我两分钟～"))
            )
        finally:
            plugin.db.raw.close()
        self.assertEqual(sent, [(session, ["坐好等我两分钟～"])])

    def test_takeover_only_sends_what_is_left_after_a_live_say(self):
        """先说的那句已经即时发出去了：收尾不再发第二遍，也不再引用触发她的消息。"""

        from core.engine import ReplyOutcome

        plugin, _context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-live-once"
        )

        async def fake_handle_reply(ctx, history=None):
            return ReplyOutcome(
                ok=True,
                messages=["查到啦"],
                live_messages=["坐好等我两分钟～"],
                live_sent=True,
            )

        plugin.engine.handle_reply = fake_handle_reply
        event = _QuoteEvent("帮我查下新闻")
        event.unified_msg_origin = session
        try:
            asyncio.run(plugin.on_llm_request(event, _GateRequest("帮我查下新闻")))
        finally:
            plugin.db.raw.close()
        texts = [seg.get("text") for chain in event.sent for seg in chain.chain]
        self.assertEqual(texts, ["查到啦"])

    def test_send_text_sends_one_chain_per_message(self):
        """多条消息要一条一个消息链——塞进同一个 chain，平台会拼成一条发出去。"""

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        messenger = self.module.AstrBotMessenger(plugin)
        ok = asyncio.run(messenger.send_text("aiocqhttp:GroupMessage:1", ["第一句", "第二句"]))
        self.assertTrue(ok)
        self.assertEqual(len(context.sent_chains), 2)
        def part_text(comp):
            if isinstance(comp, dict):
                return comp.get("text", "")
            return getattr(comp, "text", "")

        contents = [
            [part_text(comp) for comp in chain.chain] for chain in context.sent_chains
        ]
        self.assertEqual(contents, [["第一句"], ["第二句"]])
        plugin.db.raw.close()

    def test_adapter_helpers(self):
        self.assertEqual(
            self.module._command_args(_FakeEvent("/vw debug state", None)),
            ["debug", "state"],
        )
        rendered = self.module._render_status(
            {
                "node_name": "书房",
                "state": "idle",
                "mood": "好奇",
                "values": {"energy": 0.5},
                "world_time": 3,
                "tick_seconds": 60,
            }
        )
        self.assertIn("书房", rendered)

    # ---------------- 工具调用：三种写法都要能调 ----------------

    def _plugin_with_tool(self, tool):
        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        manager = context.get_llm_tool_manager()
        manager.func_list = [tool]
        event = _GateEvent("找点东西")
        plugin._remember_event(event)
        return plugin, event

    def test_call_tool_uses_the_modern_call_interface(self):
        """新版工具（只有 call()、没有 handler）以前会直接报"没有可调用的 handler"。"""

        class _ModernTool:
            name = "anysearch_search"
            description = "搜索"
            parameters = {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            }

            def __init__(self):
                self.seen = []

            async def call(self, context, **kwargs):
                self.seen.append((context.context.event.get_message_str(), kwargs))

                class _Result:
                    content = [{"type": "text", "text": "今天有三条科技新闻"}]

                return _Result()

        tool = _ModernTool()
        plugin, _event = self._plugin_with_tool(tool)
        result = asyncio.run(
            self.module.AstrBotTools(plugin).call_tool(
                "anysearch_search", {"query": "今天的新闻", "多余的参数": 1}
            )
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, "今天有三条科技新闻")
        # 参数按 schema 过滤过，上下文里也能拿到事件
        self.assertEqual(tool.seen[0][1], {"query": "今天的新闻"})
        self.assertEqual(tool.seen[0][0], "找点东西")
        plugin.db.raw.close()

    def test_call_tool_keeps_supporting_the_decorator_handler(self):
        """老的 @filter.llm_tool 工具（handler）保持原样可用。"""

        class _LegacyTool:
            name = "legacy_search"
            description = "搜索"
            parameters = {}

            def __init__(self):
                self.calls = []

            async def handler(self, event, **kwargs):
                self.calls.append(kwargs)
                return "旧版结果"

        tool = _LegacyTool()
        plugin, _event = self._plugin_with_tool(tool)
        result = asyncio.run(
            self.module.AstrBotTools(plugin).call_tool("legacy_search", {})
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, "旧版结果")
        plugin.db.raw.close()

    def test_tool_list_includes_official_builtin_tools(self):
        """AstrBot 自带的内置工具（搜索、知识库…）也要列出来，并标成「官方」。"""

        class _PluginTool:
            name = "my_plugin_tool"
            description = "插件工具"
            parameters = {}

        class _BuiltinTool:
            name = "web_search_tavily"
            description = "官方搜索"
            parameters = {"type": "object", "properties": {"query": {"type": "string"}}}

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        manager = context.get_llm_tool_manager()
        manager.func_list = [_PluginTool()]
        manager.iter_builtin_tools = lambda: [_BuiltinTool()]

        tools = {item.name: item for item in self.module.AstrBotTools(plugin).list_tools()}
        self.assertEqual(tools["my_plugin_tool"].source, "plugin")
        self.assertEqual(tools["web_search_tavily"].source, "official")
        self.assertIn("query", tools["web_search_tavily"].parameters.get("properties", {}))
        plugin.db.raw.close()

    def test_call_tool_reports_tools_without_any_entry_point(self):
        class _BrokenTool:
            name = "broken"
            description = ""
            parameters = {}

        plugin, _event = self._plugin_with_tool(_BrokenTool())
        result = asyncio.run(
            self.module.AstrBotTools(plugin).call_tool("broken", {})
        )
        self.assertFalse(result.ok)
        self.assertIn("没有可调用的 handler", result.error)
        plugin.db.raw.close()

    def test_sleeping_bot_is_gated_before_the_persona(self):
        """睡着时被 @：只发固定文案并吃掉这次回复，主人格不会替她熬夜聊天。"""

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        session = "aiocqhttp:GroupMessage:1"
        plugin.store.add_session(session, session_type="group", platform="aiocqhttp")
        plugin.engine.reload_config()

        async def put_her_to_sleep():
            async with plugin.engine.session_state(session) as state:
                state.state = "sleeping"
                state.current_action = {"type": "sleep", "duration_ticks": 480}

        asyncio.run(put_her_to_sleep())

        event = _GateEvent("在吗")
        asyncio.run(plugin.on_llm_request(event, _GateRequest("在吗")))
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(event.sent), 1)
        self.assertIn("睡觉中", event.sent[0].chain[0]["text"])
        plugin.db.raw.close()

    def test_sleep_guard_hook_stops_the_event_before_other_plugins(self):
        """睡着时没 @ 她的消息在钩子里就被截住（其它插件不会执行），指令照常放行。"""

        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        session = "aiocqhttp:GroupMessage:1"
        plugin.store.add_session(session, session_type="group", platform="aiocqhttp")
        plugin.engine.reload_config()

        async def put_her_to_sleep():
            async with plugin.engine.session_state(session) as state:
                state.state = "sleeping"
                state.current_action = {"type": "sleep", "duration_ticks": 480}

        asyncio.run(put_her_to_sleep())
        self.assertGreater(
            self.module.SLEEP_GUARD_PRIORITY, 100
        )  # 必须比意图路由的 priority=100 更早执行

        blocked = _GateEvent("今天好热啊", wake=False, mention=False)
        asyncio.run(plugin.on_sleep_guard(blocked))
        self.assertTrue(blocked.is_stopped())

        command = _GateEvent("/vw status", wake=False, mention=False)
        asyncio.run(plugin.on_sleep_guard(command))
        self.assertFalse(command.is_stopped())

        mentioned = _GateEvent("小鲸鱼在吗", mention=True)
        asyncio.run(plugin.on_sleep_guard(mentioned))
        self.assertFalse(mentioned.is_stopped())
        plugin.db.raw.close()

    def test_sleeping_bot_stays_silent_for_unaddressed_messages(self):
        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        session = "aiocqhttp:GroupMessage:1"
        plugin.store.add_session(session, session_type="group", platform="aiocqhttp")
        plugin.engine.reload_config()

        async def put_her_to_sleep():
            async with plugin.engine.session_state(session) as state:
                state.state = "sleeping"
                state.current_action = {"type": "sleep", "duration_ticks": 480}

        asyncio.run(put_her_to_sleep())

        # 没 @ 她的闲聊：睡着时保持安静（不能借上一条用例留下的固定文案冷却来"通过"）
        event = _GateEvent("今天好热啊", wake=False, mention=False)
        asyncio.run(plugin.on_llm_request(event, _GateRequest("今天好热啊")))
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.sent, [])
        plugin.db.raw.close()

    # ---------------- 「我们自己的模型调用」不该吞掉别人的消息 ----------------

    @staticmethod
    def _stub_say_reply(context, text: str = "来了") -> None:
        """让桩模型回一条 say，好断言"这一次真的被接管了"。"""

        async def llm_generate(**_kwargs):
            class _Response:
                completion_text = (
                    '{"actions":[{"type":"say","messages":["' + text + '"]}]}'
                )

            return _Response()

        context.llm_generate = llm_generate

    def _plugin_with_session(self, **config):
        """建一个只属于这个用例的会话：本文件的用例共用一个数据目录。"""

        session = config.pop("session", "aiocqhttp:GroupMessage:1")
        context = self.module.Context()
        plugin = self.module.VirtualWorldPlugin(
            context,
            self.module.AstrBotConfig(
                {"enabled": True, "tick_interval": 60, **config}
            ),
        )
        plugin.store.add_session(session, session_type="group", platform="aiocqhttp")
        plugin.engine.reload_config()

        async def reset_state():
            async with plugin.engine.session_state(session) as state:
                state.state = "idle"
                state.current_action = None
                state.current_plan = None

        asyncio.run(reset_state())
        return plugin, context, session

    def test_message_arriving_during_our_own_model_call_is_still_taken_over(self):
        """后台正在调模型时进来的 @ 也要接管，不能漏给主人格。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-takeover"
        )
        self._stub_say_reply(context)

        async def drive():
            async def background_call():
                token = plugin.set_self_initiated()
                try:
                    await asyncio.sleep(0.2)  # 正在等模型返回
                finally:
                    plugin.reset_self_initiated(token)

            task = asyncio.create_task(background_call())
            await asyncio.sleep(0)  # 让后台那次调用先拿到标记
            event = _GateEvent("在吗", session=session)
            await plugin.on_llm_request(event, _GateRequest("在吗"))
            await task
            return event

        event = asyncio.run(drive())
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(event.sent), 1)
        plugin.db.raw.close()

    def test_sleep_gate_blocks_even_while_our_own_call_is_in_flight(self):
        """睡觉门禁不能被"我们自己的调用"窗口绕过。"""

        plugin, _context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-sleep"
        )

        async def put_her_to_sleep():
            async with plugin.engine.session_state(session) as state:
                state.state = "sleeping"
                state.current_action = {"type": "sleep", "duration_ticks": 480}

        asyncio.run(put_her_to_sleep())

        async def drive():
            async def background_call():
                token = plugin.set_self_initiated()
                try:
                    await asyncio.sleep(0.2)
                finally:
                    plugin.reset_self_initiated(token)

            task = asyncio.create_task(background_call())
            await asyncio.sleep(0)
            event = _GateEvent(
                "今天好热啊", wake=False, mention=False, session=session
            )
            await plugin.on_sleep_guard(event)
            await task
            return event

        event = asyncio.run(drive())
        self.assertTrue(event.is_stopped())
        plugin.db.raw.close()

    def test_our_own_call_is_still_never_taken_over_by_itself(self):
        """我们自己的调用带着标记：同一条协程里不再注入，也不接管自己。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-inner"
        )
        self._stub_say_reply(context)

        async def drive():
            token = plugin.set_self_initiated()
            try:
                event = _GateEvent("内部调用", session=session)
                req = _GateRequest("内部调用")
                await plugin.on_llm_request(event, req)
                return event, req
            finally:
                plugin.reset_self_initiated(token)

        event, req = asyncio.run(drive())
        self.assertFalse(event.is_stopped())
        self.assertEqual(event.sent, [])
        self.assertEqual(req.system_prompt, "")
        plugin.db.raw.close()

    def test_two_messages_in_one_session_are_merged_into_one_reply(self):
        """同一会话连发两条：不并发、也不各回一条——并成一次请求回一次。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-serial"
        )
        busy = {"now": 0, "peak": 0}
        prompts: list[str] = []

        async def llm_generate(**kwargs):
            busy["now"] += 1
            busy["peak"] = max(busy["peak"], busy["now"])
            prompts.append(str(kwargs.get("prompt") or ""))
            try:
                await asyncio.sleep(0.1)
            finally:
                busy["now"] -= 1

            class _Response:
                completion_text = '{"actions":[{"type":"say","messages":["来了"]}]}'

            return _Response()

        context.llm_generate = llm_generate

        async def drive():
            first = _GateEvent("在吗", session=session)
            second = _GateEvent("还在吗", session=session)
            await asyncio.gather(
                plugin.on_llm_request(first, _GateRequest("在吗")),
                plugin.on_llm_request(second, _GateRequest("还在吗")),
            )
            return first, second

        first, second = asyncio.run(drive())

        self.assertEqual(busy["peak"], 1)
        # 第二条在安静期里到达：会被并进同一次请求，所以只调一次模型、只发一次
        self.assertEqual(len(first.sent) + len(second.sent), 1)
        self.assertEqual(len(prompts), 1, "安静期里到的消息应当并进同一次请求")
        self.assertIn("在吗", prompts[-1])
        self.assertIn("还在吗", prompts[-1])
        plugin.db.raw.close()

    def test_burst_is_merged_when_the_register_comes_before_the_lock(self):
        """真实管线的顺序：第二条的 on_any_message 先跑（不被会话锁挡），
        它的 LLM 钩子才排队等锁——安静期必须能看见它，两条并成一次回复。
        """

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-burst-quiet"
        )
        plugin.engine.world.reply_style.merge_wait_seconds = 1.0
        prompts: list[str] = []

        async def llm_generate(**kwargs):
            prompts.append(str(kwargs.get("prompt") or ""))

            class _Response:
                completion_text = '{"actions":[{"type":"say","messages":["来了"]}]}'

            return _Response()

        context.llm_generate = llm_generate

        async def drive():
            first = _GateEvent("在吗", session=session)
            second = _GateEvent("还在吗", session=session)
            task = asyncio.create_task(
                plugin.on_llm_request(first, _GateRequest("在吗"))
            )
            await asyncio.sleep(0.05)  # 第一条已经进入安静期
            await plugin.on_any_message(second)  # 第二条先登记（锁在 LLM 那一段）
            await task
            await plugin.on_llm_request(second, _GateRequest("还在吗"))
            return first, second

        first, second = asyncio.run(drive())

        self.assertEqual(len(prompts), 1, "安静期里登记的消息应当并进同一次请求")
        self.assertIn("在吗", prompts[0])
        self.assertIn("还在吗", prompts[0])
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(second.sent, [], "第二条不该另外再答一遍")
        self.assertTrue(second.is_stopped())
        plugin.db.raw.close()

    def test_generation_is_dropped_for_two_lines_arriving_back_to_back(self):
        """第二条在**调模型期间**到达：作废这一次生成，让新那条带着两条话重新回一次。

        重点是不许出现"两条各回一次"，也不许第二条落到主人格。
        """

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-burst-late"
        )
        plugin.engine.world.reply_style.merge_wait_seconds = 0.05
        prompts: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def llm_generate(**kwargs):
            prompts.append(str(kwargs.get("prompt") or ""))
            started.set()
            await asyncio.wait_for(release.wait(), timeout=5)

            class _Response:
                completion_text = '{"actions":[{"type":"say","messages":["来了"]}]}'

            return _Response()

        context.llm_generate = llm_generate

        async def drive():
            first = _GateEvent("在吗", session=session)
            second = _GateEvent("还在吗", session=session)
            task = asyncio.create_task(
                plugin.on_llm_request(first, _GateRequest("在吗"))
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            await plugin.on_any_message(second)
            release.set()
            await task
            await plugin.on_llm_request(second, _GateRequest("还在吗"))
            return first, second

        first, second = asyncio.run(drive())

        self.assertEqual(len(prompts), 2, "第一次生成应当被作废、再重新发起一次")
        self.assertIn("在吗", prompts[-1])
        self.assertIn("还在吗", prompts[-1])
        self.assertEqual(first.sent, [], "作废的那次不能发出去")
        self.assertEqual(len(second.sent), 1, "两条话只回一条")
        self.assertTrue(first.is_stopped())
        self.assertTrue(second.is_stopped(), "第二条也要被接管，不能落到主人格")
        plugin.db.raw.close()

    # ---------------- 聊天记录里的图 ----------------

    def test_provider_modality_check_reads_astrbot_config(self):
        """「主模型能吃图吗」读的是 AstrBot 里这个 Provider 勾选的模态。"""

        module = self.module

        class _Provider:
            def __init__(self, modalities) -> None:
                self.provider_config = {"modalities": modalities}

        self.assertTrue(module._provider_supports_images(_Provider(["text", "image"])))
        self.assertFalse(module._provider_supports_images(_Provider(["text"])))
        # 空列表是"没配"（老版本迁移过来的），按支持处理
        self.assertTrue(module._provider_supports_images(_Provider([])))
        # 读不到就交回配置里的开关
        self.assertIsNone(module._provider_supports_images(_Provider(None)))
        self.assertIsNone(module._provider_supports_images(object()))

    def _image_plugin(self, session: str, *, inline: str = "always"):
        """建一个配了转述模型的会话：主模型能不能看图由 ``inline`` 指定。"""

        plugin, context, _session = self._plugin_with_session(
            session=session, vision_provider_id="vision-provider"
        )
        plugin.engine.world.context.chat_image_inline = inline
        plugin.engine.world.reply_style.merge_wait_seconds = 0.05
        calls: list[dict] = []

        async def llm_generate(**kwargs):
            calls.append(kwargs)
            images = list(kwargs.get("image_urls") or [])
            prompt = str(kwargs.get("prompt") or "")

            class _Response:
                completion_text = ""

            if "图片转述" in prompt:
                # 转述的第二步（纯文本的关系判断）
                _Response.completion_text = "跟群里正在说的事对得上"
            elif len(images) > 1:
                # 一次看多张：按「图1：…」逐行返回
                _Response.completion_text = "\n".join(
                    f"图{index}：画面{index}" for index in range(1, len(images) + 1)
                )
            elif images:
                _Response.completion_text = "画面：一张图"
            else:
                _Response.completion_text = '{"actions":[{"type":"say","messages":["看到了"]}]}'

            return _Response()

        context.llm_generate = llm_generate
        return plugin, context, calls

    @staticmethod
    def _photo(text: str, urls: str | list[str], *, session: str = "") -> "_GateEvent":
        """一条带图的消息（图片识别按类名走，所以这里用真的 Image 段）。"""

        class Image:
            """图片段（识别按类名走，名字必须叫 Image）。"""

            def __init__(self, url: str) -> None:
                self.url = url
                self.file = ""
                self.path = ""

        items = [urls] if isinstance(urls, str) else list(urls)
        event = _GateEvent(text, session=session)
        event.message_obj.message = [At("1"), *[Image(url) for url in items]]
        return event

    @staticmethod
    def _caption_calls(calls: list[dict]) -> list[dict]:
        """挑出"看图"那几次调用（转述走配置里那个看图 Provider，而且带图）。"""

        return [
            item
            for item in calls
            if item.get("chat_provider_id") == "vision-provider"
            and item.get("image_urls")
        ]

    def test_the_fourth_image_captions_the_first_right_away(self):
        """收到第 4 张时就把第 1 张转掉：不等被叫到、更不等调主模型。"""

        session = "aiocqhttp:GroupMessage:probe-images-arrival"
        plugin, _context, calls = self._image_plugin(session)
        urls = [f"https://img/arrival/{index}.jpg" for index in range(1, 5)]
        seen: list[int] = []

        async def drive():
            # 没人 @ 她：只是群里在发图
            for index, url in enumerate(urls, start=1):
                await plugin.on_any_message(self._photo(f"第{index}张", url, session=session))
                seen.append(len(self._caption_calls(calls)))

        asyncio.run(drive())
        # 前三张一张都不转述；第四张一到，正好多出一次转述（最早那张）
        self.assertEqual(seen, [0, 0, 0, 1], seen)
        captions = self._caption_calls(calls)
        self.assertEqual(captions[0].get("image_urls"), [urls[0]])
        plugin.db.raw.close()

    def test_images_for_the_main_model_are_not_captioned(self):
        """图本来就要发给能看图的主模型：不再多调一次转述（一张图只看一遍）。"""

        session = "aiocqhttp:GroupMessage:probe-images-inline"
        plugin, _context, calls = self._image_plugin(session)
        urls = [f"https://img/inline/{index}.jpg" for index in (1, 2)]
        ask = self._photo(
            "看这两张", urls, session=session
        )

        asyncio.run(plugin.on_llm_request(ask, _GateRequest("看这两张")))

        self.assertEqual(self._caption_calls(calls), [], "要交给主模型的图不该再转述")
        self.assertEqual(calls[-1].get("image_urls"), urls)
        plugin.db.raw.close()

    def test_images_beyond_the_limit_are_captioned_in_one_batch(self):
        """超出发给主模型的那几张上限：先来的旧图转述一次，最新三张照旧发图。"""

        session = "aiocqhttp:GroupMessage:probe-images-overflow"
        plugin, _context, calls = self._image_plugin(session)
        urls = [f"https://img/overflow/{index}.jpg" for index in range(1, 5)]
        ask = self._photo(
            "这四张", urls, session=session
        )

        asyncio.run(plugin.on_llm_request(ask, _GateRequest("这四张")))

        captions = self._caption_calls(calls)
        self.assertEqual(len(captions), 1, "四张图只该转述一次（被挤掉的那一张）")
        self.assertEqual(captions[0].get("image_urls"), [urls[0]])
        self.assertEqual(calls[-1].get("image_urls"), urls[1:])
        plugin.db.raw.close()

    def test_a_blind_main_model_gets_all_images_captioned_in_one_batch(self):
        """主模型看不见图：这一轮的图攒到一起打包转述一次，不再一条消息调一次。"""

        session = "aiocqhttp:GroupMessage:probe-images-blind"
        plugin, _context, calls = self._image_plugin(session, inline="never")
        urls = [f"https://img/blind/{index}.jpg" for index in (1, 2)]

        async def drive():
            # 群里先发了两张（没 @ 她），第三条 @ 她但不带图
            for index, url in enumerate(urls, start=1):
                await plugin.on_any_message(
                    self._photo(f"第{index}张", url, session=session)
                )
            ask = _GateEvent("这两张是啥", session=session)
            await plugin.on_llm_request(ask, _GateRequest("这两张是啥"))
            return ask

        asyncio.run(drive())
        captions = self._caption_calls(calls)
        self.assertEqual(len(captions), 1, "两条消息的图该打包成一次转述")
        self.assertEqual(captions[0].get("image_urls"), urls)
        self.assertEqual(calls[-1].get("image_urls"), None, "转述过了就不再附图")
        plugin.db.raw.close()

    def test_each_image_is_captioned_at_most_once(self):
        """同一张图转过一次就不再转：连着几轮不会反复调转述。"""

        session = "aiocqhttp:GroupMessage:probe-images-once"
        plugin, _context, calls = self._image_plugin(session)
        urls = [f"https://img/once/{index}.jpg" for index in (1, 2)]

        async def drive():
            # 群里发了两张图，但聊天记录里只带最近一张：最早那张会被转成文字
            for index, url in enumerate(urls, start=1):
                await plugin.on_any_message(
                    self._photo(f"第{index}张", url, session=session)
                )
            for text in ("这张是啥", "还有别的不"):
                event = _GateEvent(text, session=session)
                await plugin.on_llm_request(event, _GateRequest(text))
            return calls

        asyncio.run(drive())
        captions = self._caption_calls(calls)
        self.assertEqual(len(captions), 1, captions)
        self.assertEqual(captions[0].get("image_urls"), [urls[0]])
        plugin.db.raw.close()

    def test_chat_log_images_are_attached_for_a_multimodal_model(self):
        """主模型能吃图时：聊天记录里最近的图跟着请求发过去，并标上编号。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-chat-images"
        )
        plugin.engine.world.context.chat_image_inline = "always"
        plugin.engine.world.context.chat_image_max = 1
        plugin.engine.world.reply_style.merge_wait_seconds = 0.05
        calls: list[dict] = []

        async def llm_generate(**kwargs):
            calls.append(kwargs)

            class _Response:
                completion_text = '{"actions":[{"type":"say","messages":["看到了"]}]}'

            return _Response()

        context.llm_generate = llm_generate

        class Image:
            """假图片段（识别按类名走，名字必须叫 Image）。"""

            def __init__(self, url: str) -> None:
                self.url = url
                self.file = ""
                self.path = ""

        async def drive():
            # 群里先来了张图（不一定是在跟她说话，但会进聊天留档）
            photo = _GateEvent("看这张", session=session)
            photo.message_obj.message = [At("1"), Image("https://img/1.jpg")]
            await plugin.on_any_message(photo)
            # 接着她被人 @ 了：这一轮应当把上面那张图一起带过去
            ask = _GateEvent("这张是啥", session=session)
            await plugin.on_llm_request(ask, _GateRequest("这张是啥"))
            return ask

        ask = asyncio.run(drive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("image_urls"), ["https://img/1.jpg"])
        self.assertIn("（见图1）", calls[0]["system_prompt"])
        self.assertEqual(len(ask.sent), 1)
        plugin.db.raw.close()

    def test_chat_log_images_stay_text_only_when_disabled(self):
        """关掉之后：不附图、也不写编号，免得她去找不存在的附件。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-chat-images-off"
        )
        plugin.engine.world.context.chat_image_inline = "never"
        plugin.engine.world.reply_style.merge_wait_seconds = 0.05
        calls: list[dict] = []

        async def llm_generate(**kwargs):
            calls.append(kwargs)

            class _Response:
                completion_text = '{"actions":[{"type":"say","messages":["嗯"]}]}'

            return _Response()

        context.llm_generate = llm_generate

        class Image:
            def __init__(self, url: str) -> None:
                self.url = url
                self.file = ""
                self.path = ""

        async def drive():
            photo = _GateEvent("看这张", session=session)
            photo.message_obj.message = [At("1"), Image("https://img/1.jpg")]
            await plugin.on_any_message(photo)
            ask = _GateEvent("这张是啥", session=session)
            await plugin.on_llm_request(ask, _GateRequest("这张是啥"))

        asyncio.run(drive())
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].get("image_urls"))
        self.assertNotIn("见图", calls[0]["system_prompt"])
        plugin.db.raw.close()

    # ---------------- 合并转发压成摘要 ----------------

    def test_forward_in_the_message_is_summarized_and_put_in_the_log(self):
        """转发的聊天记录交给多模态模型读一遍：摘要替换正文，图也一起给它看。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-forward",
            vision_provider_id="vision-provider",
        )
        plugin.engine.world.reply_style.merge_wait_seconds = 0.05
        prompts: list[str] = []

        async def llm_generate(**kwargs):
            prompts.append(str(kwargs.get("prompt") or ""))
            text = (
                "这段转发的聊天记录：张三说明天团建改到下午三点，李四说知道了他会转告"
            )

            class _Response:
                completion_text = (
                    '"' + text + '"'
                    if len(prompts) == 1
                    else '{"actions":[{"type":"say","messages":["看到啦"]}]}'
                )

            return _Response()

        context.llm_generate = llm_generate
        forward_image = "https://img/forward.jpg"

        async def drive():
            ask = _GateEvent("看看这个", session=session)
            ask.message_obj.message = [At("1"), Nodes([Node("张三", [
                Plain("明天团建改到下午三点"),
                Image(url=forward_image),
            ])])]
            await plugin.on_llm_request(ask, _GateRequest("看看这个"))

        asyncio.run(drive())
        self.assertEqual(len(prompts), 2, "先摘一次转发，再回一句")
        self.assertIn("这段转发的内容", prompts[0])
        # 摘要替换掉占位符，跟着这条消息一起交给她
        self.assertIn("【转发的聊天记录·摘要】", prompts[1])
        self.assertIn("团建改到下午三点", prompts[1])
        self.assertNotIn("这是一条转发的聊天记录", prompts[1])
        plugin.db.raw.close()

    def test_forward_summary_is_cached(self):
        """同一条转发再来一次不再调模型。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-forward-cache",
            vision_provider_id="vision-provider",
        )
        plugin.engine.world.reply_style.merge_wait_seconds = 0.05
        summary_calls: list[dict] = []

        async def llm_generate(**kwargs):
            prompt = str(kwargs.get("prompt") or "")
            if "这段转发的内容" in prompt:
                summary_calls.append(kwargs)
                text = "张三说团建改到下午三点"
            else:
                text = '{"actions":[{"type":"say","messages":["嗯"]}]}'

            class _Response:
                completion_text = text

            return _Response()

        context.llm_generate = llm_generate

        def forward_message(text: str):
            event = _GateEvent(text, session=session)
            event.message_obj.message = [At("1"), Nodes([Node("张三", [Plain("团建改到下午三点")])])]
            return event

        async def drive():
            first = forward_message("看看这个")
            await plugin.on_llm_request(first, _GateRequest("看看这个"))
            second = forward_message("再看一遍")
            await plugin.on_llm_request(second, _GateRequest("再看一遍"))

        asyncio.run(drive())
        self.assertEqual(len(summary_calls), 1, "第二次应当命中缓存")
        plugin.db.raw.close()

    def test_forward_summary_can_be_turned_off(self):
        """关掉之后还是老样子：只落一句「这是一条转发的聊天记录」。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-forward-off",
            vision_provider_id="vision-provider",
        )
        plugin.engine.world.vision.forward_summary = False
        plugin.engine.world.reply_style.merge_wait_seconds = 0.05
        prompts: list[str] = []

        async def llm_generate(**kwargs):
            prompts.append(str(kwargs.get("prompt") or ""))

            class _Response:
                completion_text = '{"actions":[{"type":"say","messages":["嗯"]}]}'

            return _Response()

        context.llm_generate = llm_generate

        async def drive():
            ask = _GateEvent("看看这个", session=session)
            ask.message_obj.message = [At("1"), Nodes([Node("张三", [Plain("团建改到下午三点")])])]
            await plugin.on_llm_request(ask, _GateRequest("看看这个"))

        asyncio.run(drive())
        self.assertEqual(len(prompts), 1, "关掉之后不该为摘要调模型")
        self.assertIn("这是一条转发的聊天记录", prompts[0])
        self.assertNotIn("转发的聊天记录·摘要", prompts[0])
        plugin.db.raw.close()

    def test_forward_without_a_vision_model_keeps_the_old_note(self):
        """没配多模态模型：读不了，也只落那句提示。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-forward-no-vision"
        )
        plugin.engine.world.reply_style.merge_wait_seconds = 0.05
        prompts: list[str] = []

        async def llm_generate(**kwargs):
            prompts.append(str(kwargs.get("prompt") or ""))

            class _Response:
                completion_text = '{"actions":[{"type":"say","messages":["嗯"]}]}'

            return _Response()

        context.llm_generate = llm_generate

        async def drive():
            ask = _GateEvent("看看这个", session=session)
            ask.message_obj.message = [At("1"), Nodes([Node("张三", [Plain("团建改到下午三点")])])]
            await plugin.on_llm_request(ask, _GateRequest("看看这个"))

        asyncio.run(drive())
        self.assertEqual(len(prompts), 1)
        self.assertIn("这是一条转发的聊天记录", prompts[0])
        plugin.db.raw.close()

    # ---------------- 保存配置 ----------------

    def test_advance_tick_does_not_cancel_a_slow_round(self):
        """推进 tick 等不到就留在后台跑完，不能中途取消（以前会把这一轮砍在半路）。"""

        module = self.module
        plugin, _context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-tick"
        )
        finished = {"value": False}
        was_advancing: list[bool] = []

        async def slow_tick():
            was_advancing.append(bool(plugin._advancing))
            await asyncio.sleep(0.3)
            finished["value"] = True
            return []

        plugin.engine.tick = slow_tick
        module.request._json = {"session": session, "action": "tick"}
        original = module.STATE_TICK_WAIT_SECONDS
        module.STATE_TICK_WAIT_SECONDS = 0.05

        async def drive():
            result = await plugin.api_state_action()
            await asyncio.sleep(0.4)  # 让后台那一轮跑完
            return result

        try:
            result = asyncio.run(drive())
        finally:
            module.request._json = {}
            module.STATE_TICK_WAIT_SECONDS = original
            plugin.db.raw.close()

        data = result.get("data") or {}
        self.assertFalse(data.get("ok"), result)
        self.assertIn("没有打断", str(data.get("note")))
        self.assertTrue(finished["value"], "后台的推进应当继续跑完，而不是被取消")
        # 跑完了就把"正在推进"的口子放开，下一次点击照常响应
        self.assertEqual(plugin._advancing, set())
        self.assertTrue(was_advancing and was_advancing[0], "推进期间要挡住连点")

    def test_contacts_api_reads_and_writes_a_person(self):
        """通讯录接口：列表 → 改称呼/备注 → 加事实 → 关系 → 好感 → 忘掉。"""

        session = "aiocqhttp:GroupMessage:probe-contacts"
        plugin, _context, _session = self._plugin_with_session(session=session)
        module = self.module
        original_query = module.request.query

        def get(path_params=None):
            module.request.query = module.request._Query(
                {"session": session, **(path_params or {})}
            )
            return asyncio.run(plugin.api_profile_people())

        def post(handler, payload):
            module.request.query = module.request._Query(
                {"session": session, "_method": "POST"}
            )
            module.request._json = {"session": session, **payload}
            return asyncio.run(handler())

        try:
            # 她"见过"这个人（一条消息就够）
            asyncio.run(plugin.on_any_message(_GateEvent("你好", session=session)))

            listed = (get().get("data") or {}).get("people") or []
            self.assertEqual([item["user_id"] for item in listed], ["42"])
            self.assertEqual(listed[0]["bonds"], ["陌生人"])

            saved = post(
                plugin.api_profile_person,
                {"user_id": "42", "call_me": "主人", "note": "老熟人"},
            )
            person = (saved.get("data") or {}).get("person") or {}
            self.assertEqual(person["call_me"], "主人")
            self.assertEqual(person["note"], "老熟人")

            facts = post(
                plugin.api_profile_fact,
                {"user_id": "42", "action": "add", "kind": "喜好", "text": "喜欢猫"},
            )
            self.assertEqual(len((facts.get("data") or {}).get("facts") or []), 1)
            fact_id = (facts["data"]["facts"][0])["id"]
            post(
                plugin.api_profile_fact,
                {"user_id": "42", "action": "update", "id": fact_id, "pinned": True},
            )
            self.assertTrue(
                ((post(
                    plugin.api_profile_fact,
                    {"user_id": "42", "action": "update", "id": fact_id, "pinned": True},
                ).get("data") or {}).get("facts") or [])[0]["pinned"]
            )

            bonded = post(
                plugin.api_profile_bond,
                {"user_id": "42", "action": "note", "type": "主人"},
            )
            self.assertIn(
                "主人", ((bonded.get("data") or {}).get("person") or {}).get("affinities")
            )

            affinity = post(
                plugin.api_profile_affinity,
                {"user_id": "42", "value": 80, "reason": "测试"},
            )
            self.assertEqual(((affinity.get("data") or {}).get("person") or {}).get("affinity"), 80.0)
            self.assertEqual(
                ((affinity["data"]["person"])["level"])["name"], "亲近"
            )

            detail = (post(plugin.api_profile_person, {"user_id": "42"}).get("data") or {})
            bonds = [item for item in detail.get("bonds") or [] if item["type"] == "主人"]
            self.assertTrue(bonds and bonds[0]["since_text"])
            post(
                plugin.api_profile_bond,
                {"user_id": "42", "action": "close", "id": bonds[0]["id"]},
            )
            detail = (post(plugin.api_profile_person, {"user_id": "42"}).get("data") or {})
            past = [item for item in detail.get("bonds") or [] if item["status"] == "past"]
            self.assertTrue(past and past[0]["until_text"])

            post(plugin.api_profile_forget, {"user_id": "42"})
            self.assertEqual((get().get("data") or {}).get("people"), [])
        finally:
            module.request.query = original_query
            module.request._json = {}
            plugin.db.raw.close()

    def test_saving_schedules_with_null_sessions_succeeds(self):
        """落点多选框里混进 null（编辑器偶发）时，保存不该整份失败。"""

        plugin, _context, _session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-save"
        )
        self.module.request._json = {
            "schedules": {
                "schedules": [{"id": "s1", "time": "08:00", "sessions": [None]}]
            }
        }
        # 这个文件是几个用例共用的，跑完要放回去
        original = plugin.store.raw_schedules()
        try:
            result = asyncio.run(plugin.api_put_schedules())
        finally:
            self.module.request._json = {}
            plugin.store.save_schedules(original)
            plugin.engine.reload_config()
        self.assertTrue(result.get("data", {}).get("ok"), result)
        plugin.db.raw.close()

    def test_save_error_is_reported_in_chinese(self):
        """校验没过时给一句人话 + 具体哪一项，别把英文校验串直接丢给用户。"""

        from core.models import parse_schedules

        plugin, _context, _session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-save-error"
        )
        try:
            parse_schedules(
                {"schedules": [{"id": "s1", "time": "25:99", "action_chain": []}]}
            )
            raised = None
        except Exception as exc:  # noqa: BLE001 - 这里就是要把 pydantic 的异常接住
            raised = exc
        self.assertIsNotNone(raised, "时间写错时应当抛校验错误")
        response = plugin._save_error("日程", raised)
        message = str(response.get("data") or response)
        self.assertIn("没通过校验", message)
        # 具体到哪一条：pydantic 的字段路径 + 中文原因
        self.assertIn("schedules / 0 / time", message)
        self.assertIn("时间", message)
        plugin.db.raw.close()


class _Chain:
    """测试用：模拟 ``MessageChain``（一个带 ``.chain`` 的对象）。"""

    def __init__(self, text: str = "") -> None:
        self.chain = [text] if text else []


async def _weather_like_command(event, chain: _Chain):
    """模拟"参数需要对象"的指令（本文件有 `from __future__`，注解就是字符串）。"""

    return chain.chain


class _FilterStub:
    """模拟 AstrBot 的 ``CommandFilter``：类型是字符串就原样透传，是类型就构造它。"""

    def __init__(self, handler_params: dict | None = None) -> None:
        self.handler_params = dict(handler_params or {})
        self.seen: dict = {}

    def get_complete_command_names(self) -> list[str]:
        return ["天气"]

    def validate_and_convert_params(self, params: list, param_type: dict) -> dict:
        self.seen = dict(param_type)
        result: dict = {}
        for name, spec in param_type.items():
            value = params[0] if params else ""
            result[name] = value if isinstance(spec, str) else spec(value)
        return result


class _FakeEvent:
    """只实现 _command_args 需要的方法。"""

    def __init__(self, text: str, _unused) -> None:
        self._text = text

    def get_message_str(self) -> str:
        return self._text


class _GateEvent(fake_astrbot._AstrMessageEvent):
    """驱动 on_llm_request 的最小事件桩。"""

    def __init__(
        self,
        text: str,
        *,
        wake: bool = True,
        mention: bool = True,
        session: str = "",
    ) -> None:
        super().__init__()
        self._text = text
        self._wake = wake
        if session:
            self.unified_msg_origin = session
        self.message_obj = types.SimpleNamespace(
            message=[At("1")] if mention else []
        )

    def get_message_str(self) -> str:
        return self._text

    def is_wake_up(self) -> bool:
        return self._wake


class At:
    """假的 @ 消息段（识别按类名走，所以名字必须叫 At）。"""

    def __init__(self, qq: str) -> None:
        self.qq = qq


class Reply:
    """假的引用段（名字必须叫 Reply）。"""

    def __init__(
        self,
        *,
        chain: list[Any] | None = None,
        sender_id: str = "",
        sender_nickname: str = "",
        message_str: str = "",
    ) -> None:
        self.chain = list(chain or [])
        self.sender_id = sender_id
        self.sender_nickname = sender_nickname
        self.message_str = message_str


class Image:
    """假的图片段（名字必须叫 Image）。"""

    def __init__(self, url: str = "", file: str = "", path: str = "") -> None:
        self.url = url
        self.file = file
        self.path = path


class Plain:
    """假的文本段（名字必须叫 Plain）。"""

    def __init__(self, text: str) -> None:
        self.text = text


class Node:
    """假的合并转发节点（名字必须叫 Node）。"""

    def __init__(self, name: str, content: list) -> None:
        self.name = name
        self.uin = "0"
        self.content = list(content)


class Nodes:
    """假的合并转发（名字必须叫 Nodes）。"""

    def __init__(self, nodes: list) -> None:
        self.nodes = list(nodes)


class _QuoteEvent(_GateEvent):
    """能拿到消息 id 与平台名的事件桩（用来验引用回复）。"""

    def __init__(
        self, text: str, *, message_id: str = "7788", platform: str = "aiocqhttp"
    ) -> None:
        super().__init__(text)
        self.message_obj.message_id = message_id
        self._platform = platform

    def get_platform_name(self) -> str:
        return self._platform


class _GateRequest:
    """驱动 on_llm_request 的最小请求桩。"""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self.system_prompt = ""
        self.contexts: list[dict] = []
        self.func_tool = None


class TestAddressingInfo(unittest.TestCase):
    """「这句是冲谁说的」：收到消息时就结构化存下来，渲染聊天记录时画箭头。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    class At:
        def __init__(self, qq: str, name: str = "") -> None:
            self.qq = qq
            self.name = name

    class Reply:
        def __init__(self, sender_id: str, nickname: str = "") -> None:
            self.sender_id = sender_id
            self.sender_nickname = nickname

    class Plain:
        def __init__(self, text: str) -> None:
            self.text = text

    class Msg:
        def __init__(self, items: list) -> None:
            self.message = items

    class Ev:
        def __init__(self, items: list, self_id: str = "10001") -> None:
            self.message_obj = TestAddressingInfo.Msg(items)
            self._self_id = self_id

        def get_self_id(self) -> str:
            return self._self_id

    def test_targets_and_kind(self):
        event = self.Ev(
            [self.Plain("出来干活"), self.At("42", "老普机器人")]
        )
        targets = self.module._at_targets(event)
        self.assertEqual(targets[0]["id"], "42")
        self.assertFalse(targets[0]["self"])
        self.assertEqual(
            self.module._addressing_kind(targets, {}, "10001"), "others"
        )

        mine = self.Ev([self.At("10001", "小鲸鱼")])
        targets = self.module._at_targets(mine)
        self.assertTrue(targets[0]["self"])
        self.assertEqual(self.module._addressing_kind(targets, {}, "10001"), "me")

    def test_reply_target_counts_as_talking_to_her(self):
        event = self.Ev([self.Reply("10001", "小鲸鱼")])
        reply = self.module._reply_target(event)
        self.assertEqual(reply.get("id"), "10001")
        self.assertEqual(
            self.module._addressing_kind([], reply, "10001"), "me"
        )
        other = self.Ev([self.Reply("42", "小明")])
        self.assertEqual(
            self.module._addressing_kind([], self.module._reply_target(other), "10001"),
            "others",
        )

    def test_nothing_said_means_no_guess(self):
        event = self.Ev([self.Plain("早上好")])
        self.assertEqual(self.module._at_targets(event), [])
        self.assertEqual(self.module._addressing_kind([], {}, "10001"), "")


class TestExtensionScopes(unittest.TestCase):
    """扩展页的「会话」选择器：会话组优先，组里的成员不再单独出现。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def test_groups_come_first_and_cover_their_members(self):
        module = self.module
        context = module.Context()
        plugin = module.VirtualWorldPlugin(
            context,
            module.AstrBotConfig(
                {"enabled": True, "web_enabled": True, "tick_interval": 60}
            ),
        )
        try:
            raw = plugin.store.raw_sessions()
            raw["sessions"] = [
                {"session_id": "aiocqhttp:GroupMessage:1", "type": "group", "enabled": True},
                {"session_id": "aiocqhttp:FriendMessage:2", "type": "private", "enabled": True},
                {"session_id": "aiocqhttp:GroupMessage:9", "type": "group", "enabled": True},
            ]
            raw["groups"] = [
                {
                    "id": "team",
                    "name": "同一个她",
                    "sessions": [
                        "aiocqhttp:GroupMessage:1",
                        "aiocqhttp:FriendMessage:2",
                    ],
                    "main_session": "aiocqhttp:GroupMessage:1",
                }
            ]
            plugin.store.save_sessions(raw)
            plugin.engine.reload_config()

            items = plugin.extension_host.sessions()
            labels = [str(item.get("label") or "") for item in items]
            self.assertTrue(any("同一个她" in label for label in labels), labels)
            group = [item for item in items if item.get("type") == "group"][0]
            # 挑组 = 挑到组代表会话名下那一份状态
            self.assertEqual(group["session_id"], "aiocqhttp:GroupMessage:1")
            self.assertEqual(len(group["members"]), 2)
            # 组里的成员不再单独列一遍
            self.assertNotIn(
                "aiocqhttp:FriendMessage:2",
                [item["session_id"] for item in items],
            )
            # 没归组的会话照常列
            self.assertIn(
                "aiocqhttp:GroupMessage:9",
                [item["session_id"] for item in items],
            )
        finally:
            handle = getattr(getattr(plugin, "db", None), "raw", None)
            if handle is not None:
                handle.close()


class TestInjectWorldState(unittest.TestCase):
    """注入模式：世界状态要挂到用户消息那一侧，别把 system prompt 每轮都改一遍。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    class _Req:
        def __init__(self, system_prompt: str = "") -> None:
            self.system_prompt = system_prompt
            self.extra_user_content_parts: list = []

    def _inject(self, req, injection: str) -> str:
        return self.module.VirtualWorldPlugin._inject_world_state(
            self, req, injection
        )

    def test_world_state_goes_to_the_user_side(self):
        req = self._Req("主人格提示词")
        where = self._inject(req, "世界状态：她在书房，心情一般")
        self.assertEqual(where, "user")
        # system prompt 只多了那段固定说明：跨轮一字不差，缓存前缀才不会被切断
        self.assertIn("主人格提示词", req.system_prompt)
        self.assertIn(self.module.INJECT_STUB.strip(), req.system_prompt)
        self.assertNotIn("她在这个书房", req.system_prompt)
        self.assertNotIn("世界状态：她在书房", req.system_prompt)
        # 真正会变的那一大段在用户消息那一侧
        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        text = part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
        self.assertEqual(text, "世界状态：她在书房，心情一般")

    def test_system_prompt_stays_identical_across_turns(self):
        first = self._Req("主人格提示词")
        self._inject(first, "第一轮的状态：t=1")
        second = self._Req("主人格提示词")
        self._inject(second, "第二轮的状态：t=2")
        self.assertEqual(first.system_prompt, second.system_prompt)

    def test_old_astrbot_without_extra_parts_falls_back(self):
        class _Old:
            def __init__(self) -> None:
                self.system_prompt = "主人格提示词"

        req = _Old()
        where = self._inject(req, "世界状态：她在书房")
        self.assertEqual(where, "system")
        self.assertIn("世界状态：她在书房", req.system_prompt)

    def test_the_same_injection_is_not_added_twice(self):
        req = self._Req("主人格提示词")
        self._inject(req, "状态")
        self.assertEqual(self._inject(req, "状态"), "user（已在）")
        self.assertEqual(len(req.extra_user_content_parts), 1)


class TestImageExtraction(unittest.TestCase):
    """图片地址的提取：协议端给的字段千奇百怪，不能默默什么都不做。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    class Image:
        """类名必须叫 Image——识别按类名走，和真实 AstrBot 的组件一致。"""

        def __init__(self, *, url: str = "", file: str = "") -> None:
            self.url = url
            self.file = file
            self.path = ""

    class Reply:
        def __init__(self, chain: list) -> None:
            self.chain = chain
            self.message_str = "看这张"

    def _event(self, components: list) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:1",
            message_obj=types.SimpleNamespace(message=components),
        )

    def test_plain_text_message_has_no_images(self):
        event = self._event([_ReplyPlain("在吗")])
        self.assertEqual(asyncio.run(self.module._image_sources(event)), [])

    def test_http_url_wins_over_file(self):
        event = self._event(
            [self.Image(url="https://example.com/a.png", file="abc.jpg")]
        )
        self.assertEqual(
            asyncio.run(self.module._image_sources(event)),
            ["https://example.com/a.png"],
        )

    def test_napcat_filename_is_kept_for_the_log_but_not_sent_to_the_model(self):
        """NapCat 只给文件名时：不交给模型（它取不到，只会回"未提供图片"），原始值写进日志。"""

        event = self._event([self.Image(file="abc.jpg")])
        self.assertEqual(asyncio.run(self.module._image_sources(event)), [])
        self.assertIn("abc.jpg", self.module._image_components_debug(event))

    def test_qq_market_face_is_read_as_a_sticker(self):
        """QQ 表情包（大表情）不是 Image：取不到图时，至少要把她看懂的那句文案留下。"""

        class MarketFace:
            """类名对得上协议端的商城表情（MarketFace）。"""

            def __init__(self) -> None:
                self.summary = "笑死"
                self.emoji_id = "1234"
                self.key = "abc"

        event = self._event([MarketFace()])
        self.assertEqual(asyncio.run(self.module._image_sources(event)), [])
        note = self.module._sticker_note(event)
        self.assertIn("表情包", note)
        self.assertIn("笑死", note)

    def test_qq_face_emoji_falls_back_to_its_name(self):
        """系统小黄脸表情没有图：用它的名字写成一句话，别让她只看到空气。"""

        class Face:
            def __init__(self) -> None:
                self.id = "14"
                self.name = "微笑"

        event = self._event([Face()])
        note = self.module._sticker_note(event)
        self.assertIn("表情", note)
        self.assertIn("微笑", note)

    def test_quoted_message_images_are_included(self):
        event = self._event(
            [
                _ReplyPlain("这张是谁"),
                self.Reply([self.Image(url="https://example.com/quoted.jpg")]),
            ]
        )
        self.assertEqual(
            asyncio.run(self.module._image_sources(event)),
            ["https://example.com/quoted.jpg"],
        )

    def test_component_to_base64_is_used_when_the_raw_ref_is_unusable(self):
        image = self.Image(file="no-extension-temp")

        async def convert_to_base64() -> str:
            return "QUJD"

        image.convert_to_base64 = convert_to_base64
        event = self._event([image])
        self.assertEqual(asyncio.run(self.module._image_sources(event)), ["base64://QUJD"])

    def test_debug_note_lists_component_fields(self):
        event = self._event([self.Image(file="abc.jpg")])
        note = self.module._image_components_debug(event)
        self.assertIn("file=abc.jpg", note)


class TestForwardDigest(unittest.TestCase):
    """合并转发内容的读取：自带内容的节点、只有 id 的转发、multimsg JSON 都要认。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def _event(self, components, payloads=None, message_str=""):
        calls: list[tuple[str, dict]] = []

        class _Api:
            async def call_action(self, action, **params):
                calls.append((action, params))
                key = str(params.get("message_id") or params.get("id") or "")
                return (payloads or {}).get(key)

        event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:1",
            message_obj=types.SimpleNamespace(
                message=list(components), message_str=message_str
            ),
            bot=types.SimpleNamespace(api=_Api()),
        )
        return event, calls

    def test_nodes_in_the_message_are_read(self):
        event, _calls = self._event(
            [
                Nodes(
                    [
                        Node("张三", [Plain("明天团建改到下午三点")]),
                        Node("李四", [Plain("收到"), Image(url="https://img/a.jpg")]),
                    ]
                )
            ]
        )
        digest, images, ids = asyncio.run(self.module._forward_digest(event))
        self.assertIn("张三：明天团建改到下午三点", digest)
        self.assertIn("李四", digest)
        self.assertIn("［图片1］", digest)
        self.assertEqual(images, ["https://img/a.jpg"])
        self.assertEqual(ids, [])

    def test_forward_id_is_fetched_from_the_platform(self):
        payload = {
            "status": "ok",
            "data": {
                "messages": [
                    {
                        "sender": {"nickname": "阿May", "user_id": "7"},
                        "message": [
                            {"type": "text", "data": {"text": "今晚八点开黑"}},
                            {"type": "image", "data": {"url": "https://img/b.jpg"}},
                        ],
                    }
                ]
            },
        }

        class Forward:
            def __init__(self, fid: str) -> None:
                self.id = fid

        event, calls = self._event([Forward("7788")], payloads={"7788": payload})
        digest, images, ids = asyncio.run(self.module._forward_digest(event))
        self.assertEqual(ids, ["7788"])
        self.assertIn("阿May：今晚八点开黑", digest)
        self.assertIn("［图片1］", digest)
        self.assertEqual(images, ["https://img/b.jpg"])
        self.assertTrue(calls and calls[0][0] == "get_forward_msg")

    def test_multimsg_json_is_read(self):
        class Json:
            def __init__(self, data) -> None:
                self.data = data

        data = {
            "app": "com.tencent.multimsg",
            "config": {"forward": 1},
            "meta": {"detail": {"news": [{"text": "小明：[图片] 这周不回去了"}]}},
        }
        import json as _json

        event, _calls = self._event([Json(_json.dumps(data, ensure_ascii=False))])
        digest, _images, _ids = asyncio.run(self.module._forward_digest(event))
        self.assertIn("这周不回去了", digest)

    def test_forward_placeholders_are_stripped(self):
        self.assertEqual(
            self.module._strip_forward_placeholders("[转发消息] 你看看这个"),
            "你看看这个",
        )
        self.assertEqual(
            self.module._strip_forward_placeholders("看看 [聊天记录]"), "看看"
        )

    def test_summary_is_cleaned_and_cut_to_the_limit(self):
        clean = self.module._clean_forward_summary("摘要： 张三说团建改到下午三点 ", 300)
        self.assertEqual(clean, "张三说团建改到下午三点")
        cut = self.module._clean_forward_summary("很" * 500, 100)
        self.assertEqual(len(cut), 100)
        self.assertTrue(cut.endswith("…"))
        self.assertEqual(self.module._clean_forward_summary("   ", 300), "")


class TestCommandResultParsing(unittest.TestCase):
    """指令 / 工具返回的 MessageEventResult 要拆成人话，而不是 dataclass 的 repr。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    class Plain:
        def __init__(self, text: str) -> None:
            self.text = text

    class At:
        def __init__(self, qq: str, name: str = "") -> None:
            self.qq = qq
            self.name = name

    class Image:
        def __init__(self, *, url: str = "", file: str = "") -> None:
            self.url = url
            self.file = file
            self.path = ""

    class Result:
        """MessageEventResult 的替身：只要有一个 chain 就够。"""

        def __init__(self, chain: list) -> None:
            self.chain = chain

    def parse(self, value):
        return asyncio.run(self.module._result_text(value))

    def test_plain_text_is_clean(self):
        text, images = self.parse(self.Result([self.Plain("北京 晴 26℃")]))
        self.assertEqual(text, "北京 晴 26℃")
        self.assertEqual(images, [])
        # 不能把 dataclass 表示丢给模型
        self.assertNotIn("ComponentType", text)
        self.assertNotIn("result_type", text)

    def test_at_becomes_a_mention(self):
        text, _images = self.parse(
            self.Result([self.Plain("查到了 "), self.At("42", "小明")])
        )
        self.assertEqual(text, "查到了 @小明")

    def test_image_is_split_out_with_a_marker(self):
        text, images = self.parse(
            self.Result(
                [
                    self.Plain("给你看看今天的图"),
                    self.Image(url="https://example.com/today.png"),
                ]
            )
        )
        self.assertEqual(text, "给你看看今天的图［图片1］")
        self.assertEqual(images, ["https://example.com/today.png"])
        # 图片地址绝不进正文：base64 图会把提示词撑爆
        self.assertNotIn("example.com", text)

    def test_bare_image_yield_is_supported(self):
        text, images = self.parse(self.Image(url="https://example.com/a.png"))
        self.assertEqual(text, "")
        self.assertEqual(images, ["https://example.com/a.png"])

    def test_plain_string_still_works(self):
        text, images = self.parse("直接返回的文字")
        self.assertEqual(text, "直接返回的文字")
        self.assertEqual(images, [])

    def test_long_result_is_truncated(self):
        text, _images = self.parse("X" * 5000)
        self.assertLess(len(text), self.module.MAX_RESULT_CHARS + 40)
        self.assertIn("已截断", text)

    def test_unknown_components_are_named_not_dumped(self):
        class Record:
            def __init__(self) -> None:
                self.file = "voice.silk"

        text, _images = self.parse(self.Result([self.Plain("听听"), Record()]))
        self.assertIn("［Record］", text)
        self.assertNotIn("voice.silk", text)


class _BorrowEventStub:
    """够用的消息事件替身：认得出会话，也能换会话。"""

    def __init__(self, origin: str) -> None:
        self.unified_msg_origin = origin
        self.session = origin
        self.new_session = origin


class TestCommandEventBorrowing(unittest.TestCase):
    """指令借事件上下文：只能借同一个会话组里的，而且得改成真正的落点。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    GROUP = "aiocqhttp:GroupMessage:1001"
    PRIVATE = "aiocqhttp:PrivateMessage:2692047521"
    ELSEWHERE = "aiocqhttp:GroupMessage:9999"

    def plugin(self, sessions: list[str]):
        """一个真实的插件实例，外加上面几个会话（编成一组）。"""

        plugin = self.module.VirtualWorldPlugin(
            self.module.Context(),
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        for item in sessions:
            plugin.store.add_session(
                item,
                session_type="private" if "Private" in item else "group",
                platform=item.split(":", 1)[0],
            )
        raw = plugin.store.raw_sessions()
        raw["groups"] = [
            {
                "id": "team",
                "name": "team",
                "sessions": list(sessions),
                "main_session": sessions[0],
            }
        ]
        plugin.store.save_sessions(raw)
        plugin.engine.reload_config()
        return plugin

    def test_event_from_other_chats_is_not_borrowed(self):
        """别的会话组的事件宁可不借：出图插件会照着事件的会话把图发出去。"""

        plugin = self.plugin([self.GROUP, self.PRIVATE])
        plugin._last_events[self.ELSEWHERE] = _BorrowEventStub(self.ELSEWHERE)
        try:
            self.assertIsNone(plugin.nearby_event(self.PRIVATE))
        finally:
            plugin.db.raw.close()

    def test_event_from_the_same_group_is_repointed(self):
        """同组的事件可以借，但跑之前要把它指到真正的落点上，跑完还原。"""

        plugin = self.plugin([self.GROUP, self.PRIVATE])
        event = _BorrowEventStub(self.GROUP)
        plugin._last_events[self.GROUP] = event
        try:
            borrowed = plugin.nearby_event(self.PRIVATE)
            self.assertIs(borrowed, event)

            restore = self.module._swap_event_session(borrowed, self.PRIVATE)
            self.assertEqual(borrowed.unified_msg_origin, self.PRIVATE)
            restore()
            self.assertEqual(borrowed.unified_msg_origin, self.GROUP)
        finally:
            plugin.db.raw.close()

    def test_event_of_this_chat_comes_first(self):
        """这个会话自己有过消息就用它，不必去借同组别人的。"""

        plugin = self.plugin([self.GROUP, self.PRIVATE])
        mine = _BorrowEventStub(self.PRIVATE)
        plugin._last_events[self.GROUP] = _BorrowEventStub(self.GROUP)
        plugin._last_events[self.PRIVATE] = mine
        try:
            self.assertIs(plugin.nearby_event(self.PRIVATE), mine)
        finally:
            plugin.db.raw.close()

    def test_same_session_needs_no_swap(self):
        """事件本来就属于这个会话：什么都不用改。"""

        event = _BorrowEventStub(self.PRIVATE)
        restore = self.module._swap_event_session(event, self.PRIVATE)
        self.assertEqual(event.unified_msg_origin, self.PRIVATE)
        restore()
        self.assertEqual(event.unified_msg_origin, self.PRIVATE)


class TestScheduleRunCommand(unittest.TestCase):
    """`/vw schedule run <id>`：管理员立刻跑一遍日程。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def _plugin(self):
        plugin = TestMainImport._plugin(self)
        plugin.store.add_session(
            "aiocqhttp:GroupMessage:1", session_type="group", platform="aiocqhttp"
        )
        plugin.engine.reload_config()
        return plugin

    def _event(self, text: str, *, admin: bool = True):
        event = fake_astrbot._AstrMessageEvent()
        event.get_message_str = lambda: text
        event.is_admin = lambda: admin
        return event

    async def _run_command(self, plugin, event) -> list[str]:
        return [str(item) for item in [chunk async for chunk in plugin.cmd_vw(event)]]

    def test_admin_can_run_a_schedule_now(self):
        plugin = self._plugin()
        try:
            output = asyncio.run(
                self._run_command(plugin, self._event("/vw schedule run night_sleep"))
            )
        finally:
            plugin.db.raw.close()
        self.assertTrue(any("已执行" in line for line in output), output)
        self.assertTrue(any("night_sleep" in line for line in output), output)

    def test_non_admin_is_refused(self):
        plugin = self._plugin()
        try:
            output = asyncio.run(
                self._run_command(
                    plugin,
                    self._event("/vw schedule run night_sleep", admin=False),
                )
            )
        finally:
            plugin.db.raw.close()
        self.assertTrue(any("管理员" in line for line in output), output)

    def test_unknown_schedule_reports_the_reason(self):
        plugin = self._plugin()
        try:
            output = asyncio.run(
                self._run_command(plugin, self._event("/vw schedule run nope"))
            )
        finally:
            plugin.db.raw.close()
        self.assertTrue(any("没有找到" in line for line in output), output)


class TestTakeoverHookBridge(unittest.TestCase):
    """接管回复时，AstrBot 那几个生命周期钩子要按同样的时机补发一遍。

    靠这些钩子工作的插件（例如「开始处理时贴个表情、处理完摘掉」）在接管模式下才不会漏掉一半。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def _plugin_and_event(self):
        plugin = TestMainImport._plugin(self)
        plugin.store.add_session(
            "aiocqhttp:GroupMessage:1", session_type="group", platform="aiocqhttp"
        )
        plugin.engine.reload_config()
        fired: list[str] = []

        async def recorder(event, name, *args):
            fired.append(name)
            return False

        plugin._fire_hook = recorder  # type: ignore[assignment]

        async def fake_reply(ctx, history=None):
            from core.engine import ReplyOutcome

            return ReplyOutcome(ok=True, messages=["我在呢"])

        plugin.engine.handle_reply = fake_reply  # type: ignore[assignment]

        async def identity(event, messages, tail=""):
            return list(messages)

        plugin._bridge_reply_hooks = identity  # type: ignore[assignment]
        return plugin, _GateEvent("在吗"), _GateRequest("在吗"), fired

    def test_waiting_and_sent_hooks_are_dispatched(self):
        plugin, event, req, fired = self._plugin_and_event()
        try:
            asyncio.run(plugin.on_llm_request(event, req))
        finally:
            plugin.db.raw.close()
        self.assertEqual(fired, ["OnWaitingLLMRequestEvent", "OnAfterMessageSentEvent"])
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(event.sent), 1)

    def test_waiting_hook_can_veto_the_reply(self):
        """钩子把事件拦下时，本插件就不要再接管了。"""

        plugin, event, req, fired = self._plugin_and_event()

        async def veto(event, name, *args):
            fired.append(name)
            event.stop_event()
            return True

        plugin._fire_hook = veto  # type: ignore[assignment]
        try:
            asyncio.run(plugin.on_llm_request(event, req))
        finally:
            plugin.db.raw.close()
        self.assertEqual(fired, ["OnWaitingLLMRequestEvent"])
        self.assertEqual(event.sent, [])


class TestToolCallContext(unittest.TestCase):
    """插件刚启动 / 刚重载（一条消息都没收到）时的工具调用。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    class _Tool:
        def __init__(self, handler) -> None:
            self.name = "get_current_weather"
            self.handler = handler
            self.parameters = {}

    def _plugin_with_tool(self, handler):
        plugin = TestMainImport._plugin(self)
        plugin.context._tools.func_list.append(self._Tool(handler))
        plugin._last_events.clear()
        return plugin

    def test_tool_is_not_called_without_any_event(self):
        """没有事件对象时不要硬调：那样只会得到一句看不懂的 AttributeError。"""

        called: list[Any] = []

        def handler(event, **_kwargs):
            called.append(event)
            return "不该走到这里"

        plugin = self._plugin_with_tool(handler)
        try:
            result = asyncio.run(
                plugin.engine.tools.call_tool("get_current_weather", {"city": "武汉"})
            )
        finally:
            plugin.db.raw.close()
        self.assertFalse(result.ok)
        self.assertEqual(called, [])
        self.assertIn("还没收到过消息", result.error)

    def test_none_event_error_gets_a_readable_hint(self):
        """万一工具自己还是炸了，日志里要能看出是"缺消息上下文"。"""

        def handler(event, **_kwargs):
            raise AttributeError("'NoneType' object has no attribute 'image_result'")

        plugin = self._plugin_with_tool(handler)
        plugin._last_events["aiocqhttp:GroupMessage:1"] = object()
        try:
            result = asyncio.run(
                plugin.engine.tools.call_tool(
                    "get_current_weather", {"city": "武汉"}, "aiocqhttp:GroupMessage:1"
                )
            )
        finally:
            plugin.db.raw.close()
        self.assertFalse(result.ok)
        self.assertIn("image_result", result.error)
        self.assertIn("消息上下文", result.error)


class TestCaptionPrompt(unittest.TestCase):
    """图片转述提示词：默认那段要认出表情包，也可以在全局设置里换成自己的。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def test_default_prompt_asks_for_meme_detection(self):
        from core.defaults import DEFAULT_CAPTION_PROMPT, DEFAULT_CAPTION_RELATION_PROMPT

        self.assertIn("表情包", DEFAULT_CAPTION_PROMPT)
        self.assertIn("梗", DEFAULT_CAPTION_PROMPT)
        # 图里的文字要写细一点，这是这次特意放宽的
        self.assertIn("文字", DEFAULT_CAPTION_PROMPT)
        self.assertIn("60~80", DEFAULT_CAPTION_PROMPT)
        # 看图这段**不写**关系：关系由另一段纯文本提示词负责
        self.assertIn("不要写这张图和话题的关系", DEFAULT_CAPTION_PROMPT)
        # 只写真的看到的：不许按话题脑补画面，也不许把梗图说成截图
        self.assertIn("只写你在图里真的看到的东西", DEFAULT_CAPTION_PROMPT)
        self.assertIn("表情包 / 梗图", DEFAULT_CAPTION_PROMPT)
        self.assertIn("才是「截图」", DEFAULT_CAPTION_PROMPT)
        # 「文字」那栏只照抄图上的字，不许写场景想象
        self.assertIn("只照抄图上看得见的字", DEFAULT_CAPTION_PROMPT)
        self.assertIn("别在「文字」里写画面描述", DEFAULT_CAPTION_PROMPT)
        # 关系那一步：一张图不要「图1：」，多张图不要重复标签
        self.assertIn("与话题的关系", DEFAULT_CAPTION_RELATION_PROMPT)
        self.assertIn("不要写「图1：」", DEFAULT_CAPTION_RELATION_PROMPT)
        self.assertIn("不要再写一次「与话题的关系」", DEFAULT_CAPTION_RELATION_PROMPT)

    def test_custom_prompt_wins(self):
        plugin = TestMainImport._plugin(self)
        try:
            self.assertIn("表情包", plugin.vision._system_prompt())
            raw = plugin.store.raw_world()
            raw["vision"] = {"prompt": "只描述颜色"}
            plugin.store.save_world(raw)
            plugin.engine.reload_config()
            self.assertEqual(plugin.vision._system_prompt(), "只描述颜色")
            # 清空则回到内置默认
            raw["vision"] = {"prompt": ""}
            plugin.store.save_world(raw)
            plugin.engine.reload_config()
            self.assertIn("表情包", plugin.vision._system_prompt())
        finally:
            plugin.db.raw.close()

    # ---------------- 图片转述缓存 ----------------

    def _vision_plugin(
        self,
        look: str = "熊猫头，摆烂、无语｜表情包｜文字：「今天也想躺平」",
        relation: str = "与话题的关系：在附和想躺平",
        batch: str = "",
    ):
        """带计数的假转述模型：带图调用返回"看图"结果，纯文本调用返回"关系"。"""

        # 图片缓存是跨会话共享的，所以这里给每个用例一个独立的数据目录，
        # 免得用例之间互相捡到对方留下的缓存行。
        previous = os.environ.get("VIRTUAL_WORLD_DATA_DIR")
        os.environ["VIRTUAL_WORLD_DATA_DIR"] = tempfile.mkdtemp(prefix="vw-vision-")
        try:
            plugin = TestMainImport._plugin(self)
        finally:
            if previous is None:
                os.environ.pop("VIRTUAL_WORLD_DATA_DIR", None)
            else:
                os.environ["VIRTUAL_WORLD_DATA_DIR"] = previous
        plugin.vision.provider_id = "vision-model"
        calls: list[dict] = []

        class _Response:
            def __init__(self, text: str) -> None:
                self.completion_text = text

        async def fake_generate(**kwargs):
            calls.append(kwargs)
            images = [item for item in (kwargs.get("image_urls") or []) if item]
            if not images:
                return _Response(relation)
            if len(images) > 1 and batch:
                return _Response(batch)
            return _Response(look)

        plugin.context.llm_generate = fake_generate
        return plugin, calls

    @staticmethod
    def _image_calls(calls: list[dict]) -> list[dict]:
        return [item for item in calls if item.get("image_urls")]

    def test_same_image_is_described_only_once(self):
        """同一个表情包第二次出现：不再调多模态，只补一次关系分析。"""

        plugin, calls = self._vision_plugin()
        source = "base64://c2FtZS1zdGlja2Vy"
        try:
            first = asyncio.run(
                plugin.vision.describe([source], question="你看这个", context_lines=["在聊加班"])
            )
            second = asyncio.run(
                plugin.vision.describe([source], question="换个话题", context_lines=["在聊晚饭"])
            )
            # 只有一次带图的调用；第二次走缓存，但关系照旧现算
            self.assertEqual(len(self._image_calls(calls)), 1)
            self.assertIn("与话题的关系", first[0])
            self.assertIn("熊猫头", second[0])
            self.assertIn("与话题的关系", second[0])
            self.assertEqual(plugin.vision.hits, 1)
            stats = asyncio.run(plugin._vision_cache_stats())
            self.assertEqual(stats["entries"], 1)
            self.assertEqual(stats["hits"], 1)
        finally:
            plugin.db.raw.close()

    def test_the_look_call_never_gets_the_topic_context(self):
        """看图那一步不许带话题上下文。

        带上它，弱一点的多模态模型会照着话题脑补画面——实测一张"口蘑"表情包被
        描述成"群聊截图、两个人在争论蘑菇是不是植物"，而关系那一步本来就是干这个的。
        """

        plugin, calls = self._vision_plugin()
        source = "base64://dG9waWMtY29udGV4dA"
        try:
            asyncio.run(
                plugin.vision.describe(
                    [source],
                    question="这图什么意思",
                    quoted="引用的话",
                    context_lines=["在聊蘑菇是不是植物", "口蘑说话了"],
                )
            )
            image_calls = self._image_calls(calls)
            self.assertEqual(len(image_calls), 1)
            prompt = str(image_calls[0].get("prompt") or "")
            for leaked in ("蘑菇", "口蘑", "这图什么意思", "引用的话"):
                self.assertNotIn(leaked, prompt)
            # 关系那一步照样拿得到上下文（它才是判断"跟话题什么关系"的地方）
            text_calls = [item for item in calls if not item.get("image_urls")]
            self.assertTrue(text_calls)
            self.assertIn("蘑菇", str(text_calls[-1].get("prompt") or ""))
        finally:
            plugin.db.raw.close()

    def test_relation_label_is_not_written_twice(self):
        """模型自己带了「图1：」和「与话题的关系」时，拼出来也不能有两份。"""

        vision = load_plugin_module().AstrBotVision
        look = "熊猫头，摆烂｜表情包｜文字：今天也想躺平"
        combined = vision._combine(look, "图1：与话题的关系：在附和想躺平")
        self.assertEqual(combined.count("与话题的关系"), 1)
        self.assertNotIn("图1：", combined)
        self.assertIn("在附和想躺平", combined)
        # 各种写法都收敛成同一份
        for extra in ("与话题的关系：在附和", "图 2：在附和", "2. 在附和", "在附和"):
            text = vision._combine(look, extra)
            self.assertEqual(text.count("与话题的关系"), 1, extra)
            self.assertIn("在附和", text)

    def test_image_index_prefix_is_stripped(self):
        """「图1：」「图 1：」「1.」「一、」这类编号前缀都要剥掉。"""

        module = load_plugin_module()
        for raw in ("图1：回应", "图 2：回应", "1. 回应", "2）回应", "一、回应", "回应"):
            self.assertEqual(module._strip_image_index(raw), "回应", raw)

    def test_multi_images_are_looked_at_in_one_call(self):
        """一条消息里的多张图合并成一次多模态调用。"""

        plugin, calls = self._vision_plugin(
            batch="图1：一只猫在键盘上｜照片｜文字：无\n图2：熊猫头｜表情包｜文字：「躺平」"
        )
        first = "base64://aW1nLTE"
        second = "base64://aW1nLTI"
        try:
            captions = asyncio.run(plugin.vision.describe([first, second], question="看这两张"))
            self.assertEqual(len(self._image_calls(calls)), 1)
            self.assertEqual(len(self._image_calls(calls)[0]["image_urls"]), 2)
            self.assertIn("一只猫在键盘上", captions[0])
            self.assertIn("熊猫头", captions[1])
            # 两张都各自缓存了
            stats = asyncio.run(plugin._vision_cache_stats())
            self.assertEqual(stats["entries"], 2)
        finally:
            plugin.db.raw.close()

    def test_multi_image_parse_failure_falls_back_to_one_by_one(self):
        """模型没按「图1：」格式输出时，退回逐张调用（稳优先）。"""

        plugin, calls = self._vision_plugin(batch="两张图我一起说了：一只猫和一个熊猫头")
        first = "base64://aW1nLTE"
        second = "base64://aW1nLTI"
        try:
            captions = asyncio.run(plugin.vision.describe([first, second]))
            # 一次失败的批量 + 两次逐张
            self.assertEqual(len(self._image_calls(calls)), 3)
            self.assertTrue(all(captions))
        finally:
            plugin.db.raw.close()

    def test_relation_can_be_turned_off(self):
        """关掉关系分析：只给画面/类型/文字，关系交给主模型。"""

        plugin, calls = self._vision_plugin()
        raw = plugin.store.raw_world()
        raw.setdefault("vision", {})["relation_enabled"] = False
        plugin.store.save_world(raw)
        plugin.engine.reload_config()
        source = "base64://bm8tcmVsYXRpb24"
        try:
            captions = asyncio.run(plugin.vision.describe([source]))
            self.assertIn("熊猫头", captions[0])
            self.assertNotIn("与话题的关系", captions[0])
            self.assertEqual(len(self._image_calls(calls)), 1)
            self.assertEqual(len(calls), 1)
        finally:
            plugin.db.raw.close()

    def test_cache_survives_a_restart(self):
        """缓存是持久的：插件重载后同一张图仍然不用重新识别。"""

        plugin, calls = self._vision_plugin()
        # 每个用例用各自的图，避免互相捡到对方留下的缓存行
        source = "base64://cmVzdGFydC1zdGlja2Vy"
        try:
            asyncio.run(plugin.vision.describe([source]))
            plugin.vision._negative.clear()
            fresh = plugin.vision
            fresh.hits = 0
            again = asyncio.run(fresh.describe([source]))
            self.assertEqual(len(self._image_calls(calls)), 1)
            self.assertIn("熊猫头", again[0])
        finally:
            plugin.db.raw.close()

    def test_failed_description_is_not_cached(self):
        """偶发失败不该被永久记成"看不出"：第二次要重新识别。"""

        plugin, calls = self._vision_plugin(look="", relation="")
        source = "base64://ZmFpbGVkLXN0aWNrZXI"
        try:
            self.assertEqual(asyncio.run(plugin.vision.describe([source])), [""])
            plugin.vision._negative.clear()  # 模拟过了一阵子
            asyncio.run(plugin.vision.describe([source]))
            self.assertEqual(len(self._image_calls(calls)), 2)
            self.assertEqual(
                asyncio.run(plugin.db.call("image_cache_stats"))["entries"], 0
            )
        finally:
            plugin.db.raw.close()

    def test_cache_can_be_turned_off(self):
        plugin, calls = self._vision_plugin()
        raw = plugin.store.raw_world()
        raw.setdefault("vision", {})["cache_enabled"] = False
        plugin.store.save_world(raw)
        plugin.engine.reload_config()
        source = "base64://b2ZmLXN0aWNrZXI"
        try:
            asyncio.run(plugin.vision.describe([source]))
            asyncio.run(plugin.vision.describe([source]))
            self.assertEqual(len(self._image_calls(calls)), 2)
        finally:
            # 配置是模块级共享的：改完要放回去，别影响别的用例
            raw.setdefault("vision", {})["cache_enabled"] = True
            plugin.store.save_world(raw)
            plugin.engine.reload_config()
            plugin.db.raw.close()

    def test_fingerprint_ignores_temporary_tokens(self):
        """同一张图换个临时链接（带 token）也要认成同一张。"""

        fingerprint = self.module._image_fingerprint
        self.assertEqual(
            fingerprint("https://cdn.example/a.jpg?token=1&t=2"),
            fingerprint("https://cdn.example/a.jpg?token=9"),
        )
        self.assertNotEqual(
            fingerprint("https://cdn.example/a.jpg"), fingerprint("https://cdn.example/b.jpg")
        )
        self.assertEqual(
            fingerprint("base64://abc"), fingerprint("base64://abc")
        )
        self.assertNotEqual(fingerprint("base64://abc"), fingerprint("base64://abd"))

    def test_caption_prefix_drops_the_relation_part(self):
        prefix = self.module._caption_prefix("一只猫在键盘上｜照片｜与话题的关系：在吐槽加班")
        self.assertIn("一只猫在键盘上", prefix)
        self.assertNotIn("与话题的关系", prefix)
        # 切不出来就整条返回，别把描述弄丢
        self.assertEqual(self.module._caption_prefix("什么都没写的描述"), "什么都没写的描述")


class TestReplyHookResponse(unittest.TestCase):
    """回复钩子要拿到「像真的」的模型响应：别的插件会读 usage / id 这些字段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def test_hook_response_copies_the_real_response(self):
        _install_reply_hook_stubs()
        plugin = TestMainImport._plugin(self)
        try:
            class Raw:
                role = "assistant"
                completion_text = '{"actions":[]}'
                usage = "tokens"
                reasoning_content = "草稿"

            plugin.remember_llm_response("aiocqhttp:GroupMessage:1", Raw())
            event = types.SimpleNamespace(unified_msg_origin="aiocqhttp:GroupMessage:1")
            response = plugin._hook_response(event, "她要说的话", object)
        finally:
            plugin.db.raw.close()
        self.assertEqual(response.completion_text, "她要说的话")
        self.assertEqual(response.usage, "tokens")
        self.assertEqual(response.reasoning_content, "")

    def test_hook_response_falls_back_when_there_is_no_real_one(self):
        _install_reply_hook_stubs()
        from astrbot.core.provider.entities import LLMResponse

        plugin = TestMainImport._plugin(self)
        try:
            event = types.SimpleNamespace(unified_msg_origin="aiocqhttp:GroupMessage:9")
            response = plugin._hook_response(event, "临时一句", LLMResponse)
        finally:
            plugin.db.raw.close()
        self.assertEqual(response.role, "assistant")
        self.assertEqual(response.completion_text, "临时一句")

    def test_llm_response_is_kept_per_session(self):
        plugin = TestMainImport._plugin(self)
        try:
            plugin.remember_llm_response("s1", "一")
            plugin.remember_llm_response("s2", "二")
            self.assertEqual(plugin.take_llm_response("s1"), "一")
            # 取过一次就没了：钩子只补送一次，避免重复触发别人的副作用
            self.assertIsNone(plugin.take_llm_response("s1"))
            self.assertEqual(plugin.take_llm_response("s2"), "二")
        finally:
            plugin.db.raw.close()


class TestPokeSending(unittest.TestCase):
    """戳一戳：走 OneBot 的 poke 消息段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def test_poke_sends_a_poke_segment(self):
        plugin = TestMainImport._plugin(self)
        try:
            result = asyncio.run(plugin.messenger.poke("aiocqhttp:GroupMessage:1", "42"))
        finally:
            plugin.db.raw.close()
        self.assertTrue(result.ok)
        chain = plugin.context.sent_chains[-1]
        self.assertEqual(chain.chain[0], {"type": "poke", "id": "42"})

    def test_poke_without_target_reports_why(self):
        plugin = TestMainImport._plugin(self)
        try:
            result = asyncio.run(plugin.messenger.poke("aiocqhttp:GroupMessage:1", ""))
        finally:
            plugin.db.raw.close()
        self.assertFalse(result.ok)
        self.assertIn("没有指定", result.reason)

    def test_private_poke_uses_friend_poke_instead_of_a_segment(self):
        """私聊里 poke 消息段不被支持（客户端会显示成破图）：直接走 friend_poke。"""

        plugin = TestMainImport._plugin(self)
        calls: list[tuple[str, dict]] = []

        class _Bot:
            async def call_action(self, name, **kwargs):
                calls.append((name, kwargs))
                return {"status": "ok"}

        class _Ev:
            bot = _Bot()

        session = "aiocqhttp:FriendMessage:2692047521"

        async def scenario():
            async def boom(*_args, **_kwargs):
                raise AssertionError("私聊不该走 poke 消息段")

            plugin.context.send_message = boom
            plugin._last_events[session] = _Ev()
            return await plugin.messenger.poke(session, "2692047521")

        try:
            result = asyncio.run(scenario())
        finally:
            plugin.db.raw.close()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.route, "friend_poke")
        self.assertEqual([name for name, _ in calls], ["friend_poke"])
        self.assertEqual(calls[0][1], {"user_id": 2692047521})

    def test_private_poke_falls_back_and_reports_the_route(self):
        """friend_poke 不通时退到 send_poke；都记不住原因就带回去让她说一句。"""

        plugin = TestMainImport._plugin(self)
        calls: list[str] = []

        class _Bot:
            async def call_action(self, name, **kwargs):
                calls.append(name)
                if name == "friend_poke":
                    raise RuntimeError("协议端没有这个接口")
                return {"status": "ok"}

        class _Ev:
            bot = _Bot()

        session = "aiocqhttp:FriendMessage:2692047521"

        async def scenario():
            plugin._last_events[session] = _Ev()
            return await plugin.messenger.poke(session, "2692047521")

        try:
            result = asyncio.run(scenario())
        finally:
            plugin.db.raw.close()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.route, "send_poke")
        self.assertEqual(calls, ["friend_poke", "send_poke"])


class TestPersonaSource(unittest.TestCase):
    """人设从哪来：跟着 AstrBot 走 / 用插件自己那份 / 两份接起来。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def _plugin_with_persona(self, text: str = "AstrBot 里那份人设"):
        plugin = self.module.VirtualWorldPlugin(
            self.module.Context(),
            self.module.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        self.addCleanup(plugin.db.raw.close)

        class _Persona:
            prompt = text

        class _Manager:
            async def get_default_persona_v3(self, _umo=None):
                return _Persona()

        plugin.context.persona_manager = _Manager()
        return plugin

    def _set_persona(self, plugin, mode: str, text: str) -> None:
        raw = plugin.store.raw_world()
        raw["persona"] = {"mode": mode, "text": text}
        plugin.store.save_world(raw)
        plugin.engine.reload_config()

    def test_astrbot_mode_follows_the_session_persona(self):
        plugin = self._plugin_with_persona()
        self._set_persona(plugin, "astrbot", "插件自己那份")
        got = asyncio.run(plugin.persona_port.get_persona_text("aiocqhttp:GroupMessage:1"))
        self.assertEqual(got, "AstrBot 里那份人设")

    def test_plugin_mode_uses_our_own_card(self):
        plugin = self._plugin_with_persona()
        self._set_persona(plugin, "plugin", "插件自己那份")
        got = asyncio.run(plugin.persona_port.get_persona_text("aiocqhttp:GroupMessage:1"))
        self.assertEqual(got, "插件自己那份")

    def test_plugin_mode_with_empty_card_falls_back(self):
        """选了"用这一份"但没填：回落到 AstrBot 那份，别把她的人设弄空。"""

        plugin = self._plugin_with_persona()
        self._set_persona(plugin, "plugin", "   ")
        got = asyncio.run(plugin.persona_port.get_persona_text("aiocqhttp:GroupMessage:1"))
        self.assertEqual(got, "AstrBot 里那份人设")

    def test_append_mode_keeps_both(self):
        plugin = self._plugin_with_persona()
        self._set_persona(plugin, "append", "插件自己那份")
        got = asyncio.run(plugin.persona_port.get_persona_text("aiocqhttp:GroupMessage:1"))
        self.assertIn("AstrBot 里那份人设", got)
        self.assertIn("插件自己那份", got)
        self.assertLess(got.index("AstrBot 里那份人设"), got.index("插件自己那份"))

    def test_persona_travels_with_the_preset(self):
        """人设写在 world 里，所以存成预设时会一起带走。"""

        plugin = self._plugin_with_persona()
        self._set_persona(plugin, "plugin", "要带走的角色卡")
        preset = plugin.store.new_default_preset("with_persona")[0]
        data = plugin.store.read_preset(preset)
        self.assertEqual(
            ((data.get("world") or {}).get("persona") or {}).get("text"),
            "",  # 预设用的是内置默认世界，不是当前配置
        )
        saved, _warnings = plugin.store.save_preset("carry_persona")
        carried = plugin.store.read_preset(saved.stem)
        self.assertEqual(
            ((carried.get("world") or {}).get("persona") or {}).get("text"),
            "要带走的角色卡",
        )


if __name__ == "__main__":
    unittest.main()
