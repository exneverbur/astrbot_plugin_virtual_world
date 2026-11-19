"""插件工坊：她自己写一个 AstrBot 插件，装上去，再挂成她能用的动作。

用户 m31581 问的是"她能不能自己开发/改造个插件装上，再配成动作来调用"。
口径（用户 m31604 三选一里选的是 **L1 + L2**）：

* **L1 草稿**：她把插件文件写进 ``<插件数据>/drafts/<slug>/``，页面上能看全文、
  一键装、一键丢。这一步不碰宿主，最坏结果只是几个废文件。
* **L2 自装**：真装进 ``data/plugins/`` 并让 AstrBot 加载，再把 ``plugin.json``
  里声明的动作写进世界配置。**默认关着**（``agent.plugin_forge_auto_install``），
  打开了她才能在任务里自己装；关着时只能写草稿、由人在页面上按下"装上"。

这个模块只做**纯逻辑**：校验她写的东西、生成动作、给提示词用的说明。真正的落盘、
调宿主加载器、改世界配置都在引擎里（要 IO 与 await）。这样校验可以单测。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Mapping

# 允许出现在草稿里的文件后缀：能跑的东西只有 .py，其余是说明与配置。
ALLOWED_SUFFIXES = frozenset({".py", ".md", ".yaml", ".yml", ".json", ".txt"})
# 必带的两个文件：没有 main.py 装上去也加载不出插件；没有 metadata.yaml 宿主不认名字。
REQUIRED_FILES = ("main.py", "metadata.yaml")
MAX_FILES = 24
MAX_FILE_BYTES = 120_000
MAX_TOTAL_BYTES = 400_000
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
# 动作 id 统一加前缀，避免跟内置动作（say/walk_to…）撞名。
ACTION_PREFIX = "ext_"
# 插件目录名要能被 AstrBot 当 Python 包导入，所以只留小写字母/数字/下划线。
DIR_RE = re.compile(r"^astrbot_plugin_[a-z0-9_]{1,60}$")


@dataclass
class Report:
    """校验结果。``ok`` 为假时 ``errors`` 一定有内容。"""

    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


def slugify(name: Any) -> str:
    """把插件名（中文也行）压成一个能当目录名的 slug。

    中文没有直接音译，所以取"字母数字保留、其余变下划线"，再把连续下划线压一个；
    结果为空或太短就退回 ``plugin``，前缀补 ``p_`` 保证以字母开头。
    """

    text = str(name or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text or len(text) < 3:
        text = f"p_{text}" if text else "plugin"
    text = text[:36].strip("_") or "plugin"
    return text if text[0].isalpha() else f"p_{text}"


def dir_name_of(slug: Any) -> str:
    """slug → 插件目录名（AstrBot 认的那种 ``astrbot_plugin_xxx``）。"""

    key = slugify(slug)
    name = f"astrbot_plugin_{key}"
    return name[:70]


def valid_dir_name(name: Any) -> bool:
    return bool(DIR_RE.match(str(name or "").strip()))


def safe_relpath(rel: Any) -> str | None:
    """草稿里的相对路径 → 规范化后的 posix 路径；越界或后缀不允许返回 None。"""

    raw = str(rel or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ":" in raw:
        return None
    path = PurePosixPath(raw)
    if any(part in ("", ".", "..") for part in path.parts):
        return None
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        return None
    return str(path)


def coerce_files(value: Any) -> dict[str, str] | None:
    """把模型给的 ``files`` 掰成「相对路径 → 正文」的表；掰不动回 ``None``。

    规划模型写 JSON 时手的形状不固定：大多数时候是对象，但也见过
    ``[{"path": "main.py", "content": "..."}]``、``[{"filename": ..., "code": ...}]``
    这种列表（2026-10-19 真机 S9a：她第二步就是这么交的，工具当场回
    "files 要是一个表"，任务停在半路）。这里只做**形状**兼容，路径合法性
    与大小仍然交给 `validate()`——别把宽松变成放水。
    """

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        try:
            value = json.loads(text)
        except ValueError:
            return None
    if isinstance(value, Mapping):
        out: dict[str, str] = {}
        for key, body in value.items():
            if isinstance(body, Mapping):
                body = body.get("content", body.get("text", body.get("body", "")))
            out[str(key)] = body if isinstance(body, str) else str(body)
        return out
    if isinstance(value, (list, tuple)):
        out = {}
        for item in value:
            if not isinstance(item, Mapping):
                return None
            path = item.get("path") or item.get("file") or item.get("filename") or item.get("name")
            body = item.get("content")
            if body is None:
                body = item.get("text", item.get("body", item.get("code", "")))
            if not path:
                return None
            out[str(path)] = body if isinstance(body, str) else str(body)
        return out
    return None


def validate(files: Mapping[str, Any], *, tool_name: str = "") -> Report:
    """校验一份草稿：文件清单、必带文件、语法、大小、以及与工具名的对应关系。

    ``tool_name`` 是 ``plugin.json`` 里声明的那个工具名。给了就要求 ``main.py``
    里真的出现过这个字符串——否则她写完插件却挂了一个对不上的工具名，
    挂上去的动作永远调不通。
    """

    report = Report(ok=True)
    if not isinstance(files, Mapping) or not files:
        return Report(ok=False, errors=["草稿里一个文件都没有"])
    if len(files) > MAX_FILES:
        report.errors.append(f"文件太多了（{len(files)} 个，最多 {MAX_FILES} 个）")

    total = 0
    cleaned: dict[str, str] = {}
    for rel, body in files.items():
        path = safe_relpath(rel)
        if path is None:
            report.errors.append(f"这个文件不能放进来：{rel}")
            continue
        text = body if isinstance(body, str) else str(body or "")
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            report.errors.append(f"{path} 太大了（{size} 字节，单个最多 {MAX_FILE_BYTES}）")
        total += size
        cleaned[path] = text
    if total > MAX_TOTAL_BYTES:
        report.errors.append(f"整份草稿太大了（{total} 字节，最多 {MAX_TOTAL_BYTES}）")

    for name in REQUIRED_FILES:
        if name not in cleaned:
            report.errors.append(f"少了 {name}")

    main_text = cleaned.get("main.py", "")
    if main_text:
        try:
            ast.parse(main_text)
        except SyntaxError as exc:
            report.errors.append(f"main.py 第 {exc.lineno} 行有语法错误：{exc.msg}")

    meta = cleaned.get("metadata.yaml", "")
    if meta:
        fields = {}
        for line in meta.splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip().strip("'\"")
        for key in ("name", "desc", "version", "author"):
            if not fields.get(key):
                report.warnings.append(f"metadata.yaml 里没写 {key}（宿主会显示成空）")
        version = fields.get("astrbot_version", "")
        match = re.search(r"(?:^|[<>=!~,])\s*v(?=\d)", version)
        if match:
            report.errors.append("astrbot_version 里不要带 v 前缀（写 '>=4.0.0'）")
        if fields.get("version") and not re.match(r"^v?\d+\.\d+", fields["version"]):
            report.warnings.append("version 看起来不是 v1.0.0 这种形式")

    if main_text:
        if not re.search(r"class\s+\w+\s*\(\s*Star\s*\)", main_text):
            report.errors.append("main.py 里没有 Star 子类（class Xxx(Star):）")
        if "@register(" in main_text:
            report.warnings.append(
                "4.28 会自动发现插件，新插件不用写 @register（写了也能跑，但没必要）"
            )

    schema_text = cleaned.get("_conf_schema.json", "")
    if schema_text:
        try:
            schema = json_loads(schema_text)
        except ValueError as exc:
            report.errors.append(f"_conf_schema.json 不是合法 JSON：{exc}")
        else:
            if not isinstance(schema, dict):
                report.errors.append("_conf_schema.json 最外层要是一个对象")
            else:
                errors = check_schema(schema)
                report.errors.extend(errors)

    tool = str(tool_name or "").strip()
    if tool and main_text and tool not in main_text:
        report.errors.append(f"main.py 里没有出现工具名 {tool}（挂上去的动作会调不通）")

    report.ok = not report.errors
    return report


def describe_files(files: Mapping[str, Any], *, limit: int = 1600) -> str:
    """把草稿渲染成"给模型看/给页面预览"的清单（先文件树，再正文）。"""

    rows = [f"- {name}（{len(str(body or '').encode('utf-8'))} 字节）" for name, body in files.items()]
    head = "文件：\n" + "\n".join(rows)
    body_parts: list[str] = []
    budget = max(200, limit - len(head))
    for name, body in files.items():
        text = str(body or "")
        if budget <= 0:
            body_parts.append(f"--- {name} ---\n（太长，没放进来）")
            continue
        chunk = text[:budget]
        budget -= len(chunk)
        body_parts.append(f"--- {name} ---\n{chunk}")
    return head + "\n\n" + "\n\n".join(body_parts)


def action_spec(
    *,
    action_id: str,
    name: str,
    tool_name: str,
    desc: str = "",
    node: str = "",
    category: str = "instant",
    llm_level: str = "tool",
) -> dict[str, Any]:
    """生成一条能直接写进 ``world["actions"]`` 的动作。

    这类动作的实质是"让宿主里的某个工具跑一次"，所以 ``llm_level`` 固定 tool、
    只认 ``tool_names`` 里那一个工具。``node`` 给了就限定只在那个地点可用。
    """

    key = str(action_id or "").strip()
    if not key.startswith(ACTION_PREFIX):
        key = ACTION_PREFIX + slugify(key)
    spec: dict[str, Any] = {
        "id": key,
        "name": str(name or key).strip()[:40],
        # 世界配置里这个键叫 ``description``（``ActionDef.description``），不是 ``desc``：
        # 2026-10-19 真机验收逮到过——写成 ``desc`` 时模型读不到，提示词里只剩
        # ``- ext_plugin：记事本（工具型…）``，她认不出这是自己刚装的什么。
        "description": str(desc or "").strip()[:200],
        "category": category,
        "scope": "node" if node else "global",
        "llm_level": llm_level,
        "tool_names": [str(tool_name or "").strip()],
        "tool_fallbacks": [],
        "duration": 30,
        "enabled": True,
    }
    if node:
        spec["allowed_nodes"] = [str(node)]
    return spec


COMPLEX_HINTS = (
    "插件",
    "安装",
    "装上",
    "装个",
    "改配置",
    "配置文件",
    "删除",
    "删掉",
    "卸载",
    "写脚本",
    "运行脚本",
    "执行代码",
    "跑个命令",
    "重构",
)
"""会动到系统本身（而不是"在世界里做点什么"）的字眼。

只当模型没给 ``owner_check`` 时的兜底判断：命中就当成"需要主人点头"。
"""


def looks_risky(goal: Any) -> bool:
    """目标里有没有"会改到系统/文件/配置"的字眼（兜底用）。"""

    text = str(goal or "").lower()
    return any(word in text for word in COMPLEX_HINTS)


PLUGIN_GUIDE = """\
**写 AstrBot 插件（4.28）的硬规矩**（照抄骨架，别自创结构）：

- 目录名必须形如 `astrbot_plugin_xxx`（小写字母/数字/下划线），里面至少两个文件：
  `main.py`（插件本体）与 `metadata.yaml`（名称/说明/版本/作者）。
- `main.py` 里定义一个 `Star` 子类（4.28 会自动发现，**新插件不用写 @register**）：
  `class XxxPlugin(Star):`，`__init__(self, context: Context, config: AstrBotConfig)`。
- 能被虚拟世界当动作调用的是一个**LLM 函数工具**：
  ```python
  @filter.llm_tool(name="my_tool")
  async def my_tool(self, event: AstrMessageEvent, text: str = "") -> str:
      \"\"\"一句话说明这个工具干什么。

      Args:
          text(string): 参数说明
      \"\"\"
      return "结果写在这里"
  ```
  工具名就是动作里 `tool_names` 要填的那个名字，两处必须一模一样。
- **`action_name` 和 `desc` 一定要写人话**：装进世界以后，提示词里她只能看到这两样
  （`- ext_xxx：<desc>`）。`action_name` 写 10 字以内的中文（「记事本」），`desc`
  写一句"什么时候该用它"（「把要做的事记下来，之后能查、能删」）。留空就会变成
  `- ext_plugin：plugin`，她自己都认不出这是刚装的什么；写「给虚拟世界调用的一个工具」
  这种空话也一样——那等于没写，她照样不会用。
- 配置用 `_conf_schema.json`（每个键 `{"description","type","default"}`，类型只写
  string/text/int/float/bool/object/list）；读配置一律 `self.config.get(key, 默认)`，
  不要把默认值在代码里再抄一遍、也不要打印整份配置或密钥。
- 需要持久化的东西写到插件自己的数据目录（`data/plugin_data/<插件名>`），不要写当前
  工作目录；网络请求必须带超时、用 async 客户端，不要打印 token。
- 长连接/后台任务在 `initialize()` 里建、在 `terminate()` 里关；**不要**在 `__init__`
  里起任务。
- `metadata.yaml` 里 `version` 是字符串（如 `v1.0.0`），`astrbot_version` 里不要带 `v`
  前缀（写 `>=4.0.0`）。
"""


def scaffold_files(
    *,
    plugin_name: str,
    tool_name: str,
    desc: str = "",
    author: str = "virtual-world",
    version: str = "v1.0.0",
    params: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """一份能直接过 :func:`validate`、也能被宿主加载的插件骨架。

    内容借的是社区 skill ``astrbot-plugin-maker`` 的模板口径（用户 m31660：那个
    SKILL.md 可以借用），裁到"一个 LLM 工具 + 一份配置 + 说明"的最小形状：
    她要做的往往正是"给虚拟世界多一个能调用的工具"。
    """

    tool = str(tool_name or "").strip() or "my_tool"
    title = str(plugin_name or tool).strip() or tool
    summary = str(desc or f"{title}：给虚拟世界调用的一个工具。").strip()
    args = dict(params or {"text": "这个工具的输入（一句话说明）"})
    lines = [
        "from astrbot.api import AstrBotConfig",
        "from astrbot.api.event import AstrMessageEvent, filter",
        "from astrbot.api.star import Context, Star",
        "",
        "",
        f"class {_class_name(tool)}Plugin(Star):",
        f'    """{summary[:60]}"""',
        "",
        "    def __init__(self, context: Context, config: AstrBotConfig):",
        "        super().__init__(context)",
        "        self.config = config",
        "",
        f'    @filter.llm_tool(name="{tool}")',
        f"    async def {tool}(",
        "        self,",
        "        event: AstrMessageEvent,",
        *[f"        {key}: str = \"\"," for key in args],
        "    ) -> str:",
        f'        """{summary[:120]}',
        "",
        "        Args:",
        *[f"            {key}(string): {text}" for key, text in args.items()],
        '        """',
        '        return "还没实现：把这里换成真正要做的事。"',
        "",
    ]
    metadata = (
        f"name: {title}\n"
        f"desc: {summary[:100]}\n"
        f"version: {version}\n"
        f"author: {author}\n"
        f"astrbot_version: '>=4.0.0'\n"
    )
    schema = {}
    for key, text in args.items():
        schema[key] = {"description": str(text)[:60], "type": "string", "default": ""}
    readme = f"# {title}\n\n{summary}\n\n工具名：`{tool}`（虚拟世界动作里的 `tool_names` 要写它）。\n"
    return {
        "main.py": "\n".join(lines),
        "metadata.yaml": metadata,
        "_conf_schema.json": json_dumps(schema),
        "README.md": readme,
    }


def _class_name(tool: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", str(tool or "").strip())
    name = "".join(part[:1].upper() + part[1:] for part in parts if part) or "Forge"
    return name if name[0].isalpha() else f"X{name}"


def json_dumps(data: Any) -> str:
    return __import__("json").dumps(data, ensure_ascii=False, indent=2) + "\n"


def json_loads(text: str) -> Any:
    return __import__("json").loads(text)


# `_conf_schema.json` 允许的类型 → 默认值的合法形状（借 astrbot-plugin-maker 的
# validate_plugin.py，砍掉 PyYAML/packaging 依赖：只查我们真会看的那几项）
CONFIG_TYPES: dict[str, Any] = {
    "string": str,
    "text": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "object": dict,
    "dict": dict,
    "list": list,
    "template_list": list,
    "file": list,
}


def check_schema(schema: Any, *, prefix: str = "_conf_schema.json") -> list[str]:
    """检查配置 schema 的形状（类型名认不认、默认值对不对得上）。"""

    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{prefix}：最外层要是一个对象"]
    for key, definition in schema.items():
        label = f"{prefix}.{key}"
        if not isinstance(definition, dict) or not isinstance(definition.get("type"), str):
            errors.append(f"{label}：要写 type（string / int / bool …）")
            continue
        kind = definition["type"]
        expected = CONFIG_TYPES.get(kind)
        if expected is None:
            errors.append(f"{label}：认不出的类型 {kind!r}")
            continue
        if "default" in definition:
            value = definition["default"]
            ok = isinstance(value, expected)
            if kind in ("int", "float") and isinstance(value, bool):
                ok = False
            if not ok:
                errors.append(f"{label}：default 与类型 {kind} 对不上")
        if kind == "object":
            errors.extend(check_schema(definition.get("items"), prefix=f"{label}.items"))
    return errors

