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
        self.assertIn("你（小鲸鱼(10001)）", note)
        self.assertIn("小明(42)", note)
        self.assertEqual(self.module._mention_note(_Ev([])), "")

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

        event = _GateEvent("今天好热啊", wake=False)
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

    def test_two_messages_in_one_session_never_run_takeover_at_the_same_time(self):
        """同一会话里两条消息的接管要排队：第二条等第一条说完再开口。"""

        plugin, context, session = self._plugin_with_session(
            session="aiocqhttp:GroupMessage:probe-serial"
        )
        busy = {"now": 0, "peak": 0}

        async def llm_generate(**_kwargs):
            busy["now"] += 1
            busy["peak"] = max(busy["peak"], busy["now"])
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
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(len(second.sent), 1)
        plugin.db.raw.close()


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


class _GateRequest:
    """驱动 on_llm_request 的最小请求桩。"""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self.system_prompt = ""
        self.contexts: list[dict] = []
        self.func_tool = None


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

    def test_napcat_filename_falls_back_to_the_raw_field(self):
        """NapCat 只给了个文件名时，至少要把原始值留下来（能查、能记日志）。"""

        event = self._event([self.Image(file="abc.jpg")])
        self.assertEqual(asyncio.run(self.module._image_sources(event)), ["abc.jpg"])

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
        self.assertIn("与话题的关系", DEFAULT_CAPTION_RELATION_PROMPT)

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


if __name__ == "__main__":
    unittest.main()

