"""接口注册表的体检：路径不撞车、方法名不重名、前端调的接口真的存在。

这三种错法都不会报错，只会让页面「安安静静地坏掉」，所以要有测试盯着：

- 同一个方法名在类体里定义两次，后者静默覆盖前者；
- 同一个路由注册两次，后注册的顶掉先注册的；
- 前端写了 ``apiGet("xxx")``，后端从没注册过，取到的永远是空。
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN_DIR)
sys.path.insert(0, HERE)

# 实例化插件会写数据目录，指到临时目录去，别碰真数据。
os.environ.setdefault("VIRTUAL_WORLD_DATA_DIR", tempfile.mkdtemp(prefix="virtual-world-routes-"))

import fake_astrbot  # noqa: E402

fake_astrbot.install()

PLUGIN_NAME = "astrbot_plugin_virtual_world"
MAIN_PY = os.path.join(PLUGIN_DIR, "main.py")
APP_JS = os.path.join(PLUGIN_DIR, "pages", "world_editor", "app.js")

# app.js 里三种取接口的写法：本文件的 apiGet/apiPost，以及直接调 bridge 的那几处。
_ENDPOINT_PATTERNS = (
    re.compile(r"\bapi(?:Get|Post)\(\s*\"([^\"]+)\""),
    re.compile(r"\bbridge\.(?:apiGet|apiPost|download)\(\s*\"([^\"]+)\""),
)
# 抽少了说明正则没跟上代码写法，测试要吵起来，而不是悄悄放过。
MIN_ENDPOINTS_SEEN = 40


def load_plugin_module():
    """把插件目录当包加载，相对导入才生效（与 test_main_import 共用一份）。"""

    if "vw_plugin_under_test" in sys.modules:
        return sys.modules["vw_plugin_under_test"]
    spec = importlib.util.spec_from_file_location(
        "vw_plugin_under_test",
        MAIN_PY,
        submodule_search_locations=[PLUGIN_DIR],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["vw_plugin_under_test"] = module
    spec.loader.exec_module(module)
    return module


def plugin_method_names() -> list[str]:
    """用 AST 读类体，找出「同一个方法名定义了两遍」的情况。

    运行起来之后是看不出来的——第二个定义已经把第一个覆盖了。
    """

    with open(MAIN_PY, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "VirtualWorldPlugin":
            return [
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
    raise AssertionError("main.py 里找不到 VirtualWorldPlugin")


def frontend_endpoints() -> list[str]:
    with open(APP_JS, encoding="utf-8") as handle:
        source = handle.read()
    found = {match for pattern in _ENDPOINT_PATTERNS for match in pattern.findall(source)}
    return sorted(found)


class WebRouteRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_plugin_module()

    def registered_routes(self) -> list[str]:
        module = self.module
        context = module.Context()
        plugin = module.VirtualWorldPlugin(
            context,
            module.AstrBotConfig({"enabled": True, "web_enabled": True, "tick_interval": 60}),
        )
        try:
            return [str(item[0]) for item in context.registered_web_apis]
        finally:
            raw = getattr(getattr(plugin, "db", None), "raw", None)
            if raw is not None:
                raw.close()

    def test_no_route_registered_twice(self) -> None:
        """同一个路径只能注册一次，否则后注册的把先注册的顶掉。"""

        routes = self.registered_routes()
        seen: dict[str, int] = {}
        for route in routes:
            seen[route] = seen.get(route, 0) + 1
        repeated = {route: count for route, count in seen.items() if count > 1}
        self.assertEqual({}, repeated, f"有路由被注册了多次：{repeated}")

    def test_no_method_defined_twice_in_plugin_class(self) -> None:
        """类体里同名方法会静默覆盖，前端拿到的就不是它要的那个。"""

        names = plugin_method_names()
        repeated = sorted({name for name in names if names.count(name) > 1})
        self.assertEqual([], repeated, f"VirtualWorldPlugin 里有重名方法：{repeated}")

    def test_frontend_endpoints_all_exist(self) -> None:
        """前端每一处 apiGet/apiPost 都能在后端注册表里找到。"""

        endpoints = frontend_endpoints()
        self.assertGreaterEqual(
            len(endpoints),
            MIN_ENDPOINTS_SEEN,
            f"只从 app.js 里抽出 {len(endpoints)} 个接口，正则大概没跟上写法",
        )
        routes = set(self.registered_routes())
        missing = [
            endpoint
            for endpoint in endpoints
            if f"/{PLUGIN_NAME}/{endpoint}" not in routes
        ]
        self.assertEqual([], missing, f"前端调了没注册的接口：{missing}")

    def test_state_history_and_config_history_do_not_share_a_path(self) -> None:
        """数值历史（画曲线）和配置改动历史是两回事，路径必须分开。"""

        routes = set(self.registered_routes())
        self.assertIn(f"/{PLUGIN_NAME}/state/history", routes)
        self.assertIn(f"/{PLUGIN_NAME}/history", routes)
        self.assertIn(f"/{PLUGIN_NAME}/history/item", routes)
        with open(APP_JS, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('apiGet("state/history"', source)


if __name__ == "__main__":
    unittest.main()
