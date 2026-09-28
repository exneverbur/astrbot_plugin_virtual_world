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
RUNTIME_IDS = {"dialog-error", "persona-brief-text"}

# 悬停提示的写法要求：一句话说清这个配置干什么、取值与默认值；
# 不超过 60 字；不出现"改动过程"的口吻（那是给维护者看的，不是给用户看的）。
HINT_MAX_CHARS = 60
HINT_BANNED_WORDS = ("以前", "改成", "我们", "刚才", "上次", "原来")
_HINT_PATTERNS = (
    re.compile(r"hint:((?:\s*(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\+))+)"),
    re.compile(r"tipBox\(\s*((?:\"(?:[^\"\\]|\\.)*\"\s*\+?\s*)+)"),
    re.compile(r"\.title\s*=\s*((?:\s*(?:\"(?:[^\"\\]|\\.)*\"|\+))+)"),
)


def _tooltip_texts(source: str) -> list[str]:
    """把 app.js 里所有会变成悬停提示的字符串抠出来（拼接的按拼好的算）。"""

    texts: list[str] = []
    for pattern in _HINT_PATTERNS:
        for match in pattern.finditer(source):
            raw = "".join(re.findall(r'\"((?:[^\"\\]|\\.)*)\"', match.group(1)))
            texts.append(
                raw.replace("\\\\", "\\").replace('\\"', '"').replace("\\n", "\n")
            )
    return texts


class FrontendSyntaxTest(unittest.TestCase):
    def test_knobs_map_to_real_fields_without_overlapping(self) -> None:
        """「手感」滑块：管的字段必须真存在、互不重叠、中间那档 = 内置默认值。

        这三条是这个设计能成立的前提——字段重叠了就会互相覆盖，
        中位不是默认值则老用户一拖就变样。
        """

        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from core.defaults import default_world  # noqa: PLC0415
        from core.models import parse_world  # noqa: PLC0415

        source = APP_JS.read_text("utf-8")
        start = source.index("const KNOBS = [")
        end = source.index("\n];", start)
        block = source[start:end]

        # 按 "id": ... levels: [ {…}, … ] 切出每个滑块
        knobs = re.split(r"\n  \{\n    id: ", block)[1:]
        self.assertTrue(len(knobs) >= 7, f"滑块太少：{len(knobs)}")
        # 以"校验后的世界"为准：编辑器拿到的配置就是它（模型默认值已经补全）
        world = parse_world(default_world())[0].model_dump(mode="json", by_alias=True)
        seen: dict[str, str] = {}
        for raw in knobs:
            knob_id = raw.split('"', 2)[1]
            level_blocks = re.findall(r"\{([^{}]*)\}", raw)
            self.assertEqual(len(level_blocks), 5, f"{knob_id} 不是 5 档")
            parsed = []
            for body in level_blocks:
                values = {}
                for path, value in re.findall(r'"([\w.]+)":\s*(-?[\d.]+)', body):
                    values[path] = float(value)
                parsed.append(values)
            self.assertTrue(parsed[0], f"{knob_id} 第一档没有字段")
            # 五档的字段集合必须一致（不能某一档少调一个，否则拖回来值会残留）
            keys = set(parsed[0])
            for level in parsed[1:]:
                self.assertEqual(keys, set(level), f"{knob_id} 各档字段不一致")
            for path in sorted(keys):
                # 字段真的存在
                node = world
                for part in path.split("."):
                    self.assertIn(part, node, f"{knob_id} 指向了不存在的字段 {path}")
                    node = node[part]
                # 不跟别的滑块抢同一个字段
                self.assertNotIn(path, seen, f"{path} 同时被 {seen.get(path)} 和 {knob_id} 管")
                seen[path] = knob_id
                # 中间那档 = 默认值
                default = world
                for part in path.split("."):
                    default = default[part]
                self.assertEqual(
                    float(default),
                    parsed[2][path],
                    f"{knob_id} 的中间档跟默认值不一致：{path}",
                )

    def test_wizard_step_never_calls_back_into_the_renderer(self) -> None:
        """向导的步骤 build 里不许回调 render。

        ``render`` 会再调一次 build，build 再调 render —— 两边互相递归，
        弹窗直接白屏（栈溢出）。第一步就是这么坏的。
        """

        source = APP_JS.read_text("utf-8")
        step = source.index("function wizardSessionStep(")
        end = source.index("\nfunction ", step + 10)
        body = source[step:end]
        self.assertNotIn("redraw", body)
        self.assertNotIn("render(", body)

    def test_knob_fields_speak_chinese(self) -> None:
        """滑块明细不能只甩英文字段名——每个受影响的参数都要有中文说明。

        说明表只有后端一份（``core/models.py`` 的 ``FIELD_LABELS``，跟着 ``/defaults``
        下发），前端按路径取。这里钉住三件事：滑块管的字段都有说明、
        说明是人话不是字段名、说明指向的字段真的存在。
        """

        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from core.defaults import default_world  # noqa: PLC0415
        from core.models import FIELD_LABELS, parse_world  # noqa: PLC0415

        source = APP_JS.read_text("utf-8")
        start = source.index("const KNOBS = [")
        end = source.index("\n];", start)
        block = source[start:end]
        paths = set(re.findall(r'"([a-z_]+\.[a-z_]+)":\s*-?[\d.]+', block))
        self.assertTrue(paths, "没从 KNOBS 里抽出字段路径")
        missing = sorted(path for path in paths if path not in FIELD_LABELS)
        self.assertEqual([], missing, f"这些字段还没有中文说明：{missing}")

        world = parse_world(default_world())[0].model_dump(mode="json", by_alias=True)
        for path, label in FIELD_LABELS.items():
            self.assertTrue(
                any("\u4e00" <= char <= "\u9fff" for char in label),
                f"{path} 的说明不是中文：{label}",
            )
            self.assertLessEqual(len(label), 20, f"{path} 的说明太长：{label}")
            node = world
            for part in path.split("."):
                self.assertIn(part, node, f"{path} 指向了不存在的字段")
                node = node[part]

        # 前端认的是后端下发的表，不再自己抄一份
        self.assertIn("field_labels", source)
        self.assertIn("function fieldLabel(", source)

    def test_persona_page_is_named_after_the_gender_setting(self) -> None:
        """「她/他/ta」这一页的标题跟着性别走，不能再写死"她是谁"。"""

        source = APP_JS.read_text("utf-8")
        self.assertIn('settingsSection(\n    pronoun(),', source)
        self.assertIn('"persona",', source)
        # tab 归口按稳定 key，不按标题（标题会变）
        self.assertIn('sections: ["基础", "persona", "persona_brief", "她的初始状态"]', source)

    def test_relationship_settings_have_a_real_ui(self) -> None:
        """关系表 / 亲密度分级不能再是一整块 JSON——那是让用户对着括号数数。"""

        source = APP_JS.read_text("utf-8")
        self.assertNotIn("关系表（JSON）", source)
        self.assertNotIn("亲密度分级（JSON）", source)
        self.assertIn("function bondsEditor(", source)
        self.assertIn("function levelsEditor(", source)

    def test_history_shows_what_changed_by_path(self) -> None:
        """改动历史要按**路径**列出改了哪几处，不是把两边 JSON 逐行倒出来。"""

        source = APP_JS.read_text("utf-8")
        self.assertIn("function historyDiffPairs(", source)
        self.assertIn("history-path", source)
        self.assertNotIn("function historyDiff(", source)

    def test_no_bare_function_reference_in_forEach(self) -> None:
        """`.forEach(fn)` 会把「下标」当第二个参数传给 fn。

        回调如果带第二个参数（例如 `(item, target = box)`），拿到的是数字 0、1、2…
        于是 `target.appendChild` 直接报错——通讯录「画像读取失败」就是这么来的。
        """

        source = APP_JS.read_text("utf-8")
        bad = re.findall(r"\.forEach\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", source)
        self.assertEqual(
            bad,
            [],
            f"这些地方把函数名直接交给了 forEach，改成 `.forEach((item) => fn(item))`：{bad}",
        )

    def test_persona_review_has_its_own_modal(self) -> None:
        """人设改动要逐条对照显示（原来挤成一行长文本，看不出来改了什么）。"""

        source = APP_JS.read_text("utf-8")
        html = INDEX_HTML.read_text("utf-8")
        self.assertIn('id="review-modal"', html)
        self.assertIn("function openReviewModal(", source)
        self.assertIn("review-before", source)
        self.assertIn("review-after", source)

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

    def test_tooltips_are_short_and_describe_the_setting(self) -> None:
        """悬停提示写「这个配置干什么」，一句话讲完，不超过 60 字。"""

        texts = _tooltip_texts(APP_JS.read_text("utf-8"))
        self.assertGreater(len(texts), 300, "没扫到悬停提示，检查解析规则")
        too_long = sorted({text for text in texts if len(text) > HINT_MAX_CHARS})
        self.assertEqual(
            too_long,
            [],
            f"这些提示超过 {HINT_MAX_CHARS} 字，请改短：" + str(too_long[:5]),
        )
        bad = sorted(
            {
                text
                for text in texts
                if any(word in text for word in HINT_BANNED_WORDS)
            }
        )
        self.assertEqual(bad, [], f"这些提示带着改动过程的口吻，请改写：" + str(bad[:5]))


if __name__ == "__main__":
    unittest.main()
