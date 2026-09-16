"""前端页面的静态检查。

``app.js`` 是以 ``<script type="module">`` 加载的，模块作用域比脚本作用域严格：
顶层重名的 ``function`` / ``const`` 在脚本里合法，在模块里会直接 SyntaxError，
整个页面变成白屏。这里用 Node 按模块模式解析一遍，把这类问题挡在提交之前。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "pages" / "world_editor"
APP_JS = PAGES / "app.js"
INDEX_HTML = PAGES / "index.html"
STYLE_CSS = PAGES / "style.css"

# 这些元素由脚本在运行时插入，index.html 里查不到是正常的。
RUNTIME_IDS = {"dialog-error"}


class FrontendSyntaxTest(unittest.TestCase):
    def test_app_js_parses_as_module(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("没有找到 node，跳过前端语法检查")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "app.mjs"
            target.write_bytes(APP_JS.read_bytes())
            done = subprocess.run(
                [node, "--check", str(target)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(
            done.returncode,
            0,
            f"app.js 不能作为 ES module 解析：\n{done.stderr or done.stdout}",
        )

    def test_referenced_element_ids_exist(self) -> None:
        used = set(re.findall(r'\$\("([A-Za-z0-9_\-]+)"\)', APP_JS.read_text("utf-8")))
        html = INDEX_HTML.read_text("utf-8")
        declared = set(re.findall(r'id="([^"]+)"', html))
        missing = sorted(used - declared - RUNTIME_IDS)
        self.assertEqual(missing, [], f"index.html 里缺少这些 id：{missing}")

    def test_page_assets_exist(self) -> None:
        html = INDEX_HTML.read_text("utf-8")
        for asset in ("app.js", "style.css"):
            self.assertIn(asset, html, f"index.html 没有引用 {asset}")
            self.assertTrue((PAGES / asset).is_file(), f"缺少 {asset}")


if __name__ == "__main__":
    unittest.main()
