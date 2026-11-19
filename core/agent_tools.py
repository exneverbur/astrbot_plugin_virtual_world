"""B3：**谁能被调、文件能写到哪** —— 纯策略，不碰 IO、不碰模型。

方案见 `.vwtest/b3-executor-plan.md` §2。这一层只回答三个问题：

1. 这个工具属于哪一档（只读 / 生成文件 / 发文件 / 执行代码）？**认不出来的一律不许**；
2. 当前这几个子开关下，宿主里那批工具**实际允许**的是哪几个？
3. 她自己生成的产物文件**允许落在哪**（路径规范化 + 越界拒绝）？

引擎只负责把 `world.agent` 的开关、`available_tools()` 的清单、以及产物根目录喂进来；
判断本身在这里，所以可以脱离引擎单测。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

TIER_READ = "read"
TIER_ARTIFACT = "artifact"
TIER_SEND = "send"
TIER_CODE = "code"

TIERS = (TIER_READ, TIER_ARTIFACT, TIER_SEND, TIER_CODE)

ACTION_TIER = "action"
"""**动作点名要的工具**（用户 m31581 问的「只有我配置到 vw 的动作里的工具吗」）。

不进 ``TIERS``：那四档是"这个工具能干什么"的风险分级，这一类是"主人在世界配置里
亲手写的动作要用它"。按动作放行，但**分级优先**：名字认得出是执行代码 / 写文件 /
发文件，照旧吃那个子开关，一条动作不能当后门。
"""

TIER_LABELS = {
    TIER_READ: "只读",
    TIER_ARTIFACT: "生成文件",
    TIER_SEND: "发送文件",
    TIER_CODE: "执行代码",
}

# 档位 → 管它的那个子开关（`world.agent` 的字段名）
TIER_SWITCHES = {
    TIER_READ: "history_lookup_enabled",
    TIER_ARTIFACT: "artifact_generation_enabled",
    TIER_SEND: "file_send_enabled",
    TIER_CODE: "code_execution_enabled",
}

# 精确名优先（这几个名字前缀互不包含，写全最稳）
CODE_TOOL_NAMES = frozenset(
    {
        "astrbot_execute_python",
        "astrbot_execute_shell",
        "astrbot_execute_ipython",
        "astrbot_shell_session",
        "astrbot_execute_browser",
        "astrbot_execute_browser_batch",
        "astrbot_run_browser_skill",
    }
)

ARTIFACT_TOOL_NAMES = frozenset(
    {
        "astrbot_file_write_tool",
        "astrbot_file_edit_tool",
    }
)

SEND_TOOL_NAMES = frozenset(
    {
        "astrbot_upload_file",
        "astrbot_download_file",
        "send_message_to_user",
    }
)

# 只读类：只要宿主注册了就允许（前缀匹配，版本升级加了新工具也自动覆盖）
READ_TOOL_PREFIXES = (
    "web_search",
    "tavily",
    "firecrawl",
    "astr_kb_search",
    "astrbot_file_read_tool",
    "astrbot_grep_tool",
    "astrbot_get_execution_history",
    # 宿主那三个 skill 只读工具（list/get/evaluate）已经不在宿主清单里了，
    # 放行名单再留着就是自相矛盾：她会被允许调一件根本不存在的工具。
)

RESEARCH_TOOL_PREFIXES = (
    "web_search",
    "tavily",
    "firecrawl",
    "astr_kb_search",
)
"""能"查资料"的那几个（用户 m30450 第 7 条：办不成时先查再办）。

它们本来就在只读档里，这里单列一份，是为了让执行器在**某一步失败之后**知道
"还有一招没试"——用这几样去找做法，而不是照着同一个错参数再来一遍。
"""


def is_research_tool(name: Any) -> bool:
    """这个工具是不是"查资料"用的（网页搜索 / 抓网页 / 知识库检索）。

    名字对不上前缀也要认（``deepseek_web_search``、``bocha_web_search`` 这类
    带别的插件前缀的写法），所以 ``web_search`` 用**包含**判断。
    """

    key = str(name or "").strip().lower()
    if not key:
        return False
    if "web_search" in key or "web_fetch" in key:
        return True
    return key.startswith(RESEARCH_TOOL_PREFIXES)


HISTORY_LOOKUP_TOOL = "vw_history_lookup"
"""**插件自己提供的**只读工具（B2 的聊天记录查询）：宿主里没有它，不走前缀匹配。

它在引擎里由插件自己执行（和"生成文件 / 发送文件"两档一样不走 `_call_tool`），
所以 :func:`allowed_tools` 只管宿主工具清单，而 :func:`internal_tools` 单独按
``history_lookup_enabled`` 开关把它加进来。名字带 ``vw_`` 前缀，在工具清单里一眼
看得出"这是虚拟世界自己给的"，也不会与宿主或别的插件撞名。
"""

EXPERIENCE_LOOKUP_TOOL = "vw_experience_lookup"
"""**插件自己提供的**只读工具（E3 的"翻自己的经历账"）。

与 `vw_history_lookup` 同一档、同一个子开关（`history_lookup_enabled` = "允许她查记录"）：
两件事都是"翻过去的记录"，分成两个开关只会让人多关一个。区别在数据源：
前者翻聊天留档，后者翻每日经历账（她今天做过什么、成没成、为什么没成）。
"""

PLUGIN_DRAFT_TOOL = "vw_plugin_draft"
"""**插件自己提供的**工具（用户 m31581）：把一份 AstrBot 插件草稿写进 ``drafts/``。

只写草稿、不碰宿主，所以默认跟着 ``plugin_forge_enabled`` 一起开——真正危险的是
下面那个"装进 data/plugins 并加载"的 :data:`PLUGIN_INSTALL_TOOL`。
"""

PLUGIN_INSTALL_TOOL = "vw_plugin_install"
"""**插件自己提供的**工具：把草稿真装进宿主、加载，并挂成她能用的动作。

两个开关同时开着她才看得见这个工具：``plugin_forge_enabled``（允许她写插件）与
``plugin_forge_auto_install``（允许她**自己装**）。后者默认关，关着时她只能写草稿、
由人在页面上按下"装上"——这一层是用户 m31581 说的"要在主人的许可下"。
"""

INTERNAL_TOOLS: dict[str, str] = {
    HISTORY_LOOKUP_TOOL: TIER_READ,
    EXPERIENCE_LOOKUP_TOOL: TIER_READ,
    PLUGIN_DRAFT_TOOL: TIER_ARTIFACT,
    PLUGIN_INSTALL_TOOL: TIER_ARTIFACT,
}
"""插件自带工具 → 档位（档位决定它吃哪个子开关）。"""

INTERNAL_REQUIRED_SWITCHES: dict[str, tuple[str, ...]] = {
    PLUGIN_DRAFT_TOOL: ("plugin_forge_enabled",),
    PLUGIN_INSTALL_TOOL: ("plugin_forge_enabled", "plugin_forge_auto_install"),
}
"""少数工具要**同时**满足几个开关（按档位只够用一个）。

插件工坊这两件：写草稿要"允许她写插件"，真装还要"允许她自己装"。两个开关都在
``world.agent`` 上，默认都是关的。
"""

INTERNAL_TOOL_SPECS: dict[str, dict[str, Any]] = {
    PLUGIN_DRAFT_TOOL: {
        "description": (
            "写一份 AstrBot 插件草稿（只写进 drafts/，不装）。"
            "先只给 tool_name 就能拿到一份能装上的骨架；之后每次只给 files 里要改的那个文件，"
            "没提到的文件保留上一版。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "草稿名（中文即可，用来生成 slug）"},
                "tool_name": {
                    "type": "string",
                    "description": "这个插件提供的工具名（小写字母/数字/下划线，形如 vw_memo_note）",
                },
                "files": {
                    "type": "object",
                    "description": (
                        "相对路径 → 文件正文；如 {\"main.py\": \"...\", \"metadata.yaml\": \"...\"}。"
                        "也接受 [{\"path\": \"main.py\", \"content\": \"...\"}] 这种列表。"
                    ),
                },
                "action_name": {
                    "type": "string",
                    "description": (
                        "装好后挂在世界里的动作名，**10 个字以内的中文**（例如「记事本」）。"
                        "她以后在提示词里就靠这个名字认出这项本事。"
                    ),
                },
                "desc": {
                    "type": "string",
                    "description": (
                        "这个动作**什么时候该用**的一句话（例如「把一件要做的事记下来，"
                        "之后能查、能删」）。这句会原样进提示词，别写成「配合某工具使用」。"
                    ),
                },
                "note": {"type": "string", "description": "给自己留的一句话（为什么做它）"},
            },
            "required": ["tool_name"],
        },
    },
    PLUGIN_INSTALL_TOOL: {
        "description": "把写好的草稿真装进 AstrBot、加载它，并挂成她能调的动作。",
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "要装的草稿 slug（vw_plugin_draft 回执里那个）"},
            },
            "required": ["slug"],
        },
    },
}
"""插件自带工具的参数说明书（宿主里没有它们，规划模型得靠这份知道怎么调）。

2026-10-19 真机验收逮到过：只把名字放进"允许清单"、不补说明书，规划提示里它们会被
写成"宿主里不可用"——她于是停下来跟主人说"我没有能写插件的那一块"。
"""


def internal_tools(
    *, switches: Mapping[str, Any] | None = None, master: bool = True
) -> dict[str, str]:
    """插件自带的那几个工具 → 当前允不允许（口径与 :func:`allowed_tools` 一致）。

    ``master=False``（总开关关着）一律回空表——关着就是"系统不存在"。
    """

    if not master:
        return {}
    flags = switches if isinstance(switches, Mapping) else {}
    picked: dict[str, str] = {}
    for name, tier in INTERNAL_TOOLS.items():
        required = INTERNAL_REQUIRED_SWITCHES.get(name)
        if required:
            if all(flags.get(key) is True for key in required):
                picked[name] = tier
            continue
        switch = switch_of_tier(tier)
        if switch and flags.get(switch) is not True:
            continue
        picked[name] = tier
    return picked



# 这几个名字属于"造东西/跑代码"，就算前缀命中只读也要让位（顺序：代码 > 产物 > 发送 > 只读）
def tool_tier(name: Any) -> str:
    """工具名 → 档位；**认不出来返回空串**（调用方按"不许"处理）。"""

    key = str(name or "").strip()
    if not key:
        return ""
    if key in CODE_TOOL_NAMES:
        return TIER_CODE
    if key in ARTIFACT_TOOL_NAMES:
        return TIER_ARTIFACT
    if key in SEND_TOOL_NAMES:
        return TIER_SEND
    if any(key.startswith(prefix) for prefix in READ_TOOL_PREFIXES):
        return TIER_READ
    return ""


def switch_of_tier(tier: Any) -> str:
    """档位 → 子开关名（认不出返回空串：调用方按"永远不许"处理）。"""

    return TIER_SWITCHES.get(str(tier or ""), "")


def allowed_tools(
    available: Any,
    *,
    switches: Mapping[str, Any] | None = None,
    master: bool = True,
    declared: Any = None,
) -> dict[str, str]:
    """宿主里那批工具 → **当前允许调用的**那批（``名字 → 档位``）。

    ``available`` 是引擎 ``available_tools()`` 的键（名字 -> 描述，或者就是名字序列，
    两种都给，别让调用方先转换）。``master=False``（总开关关着）一律回空表——
    关着就是"系统不存在"，不给出半个工具。

    ``declared`` 是**世界配置里那些动作点名要的工具**（用户 m31581 问的那个口子：
    「只有我配置到 vw 的动作里的工具吗」）。它们不在四档里、按动作放行；但**分级优先**：
    名字认得出是执行代码 / 写文件 / 发文件，照旧要那个子开关——一条动作不能当后门。
    """

    if not master:
        return {}
    if isinstance(available, Mapping):
        names = [str(key) for key in available]
    elif isinstance(available, (list, tuple, set, frozenset)):
        names = [str(item) for item in available]
    else:
        return {}
    flags = switches if isinstance(switches, Mapping) else {}
    picked: dict[str, str] = {}
    for name in names:
        tier = tool_tier(name)
        if not tier:
            continue
        switch = switch_of_tier(tier)
        if switch and flags.get(switch) is not True:
            continue
        picked[str(name).strip()] = tier
    if isinstance(declared, Mapping):
        wanted = [str(key) for key in declared]
    elif isinstance(declared, (list, tuple, set, frozenset)):
        wanted = [str(item) for item in declared]
    else:
        wanted = []
    for name in wanted:
        key = str(name or "").strip()
        if not key or key not in names:
            # 宿主没注册这个名字 = 插件没装上 / 没加载，给个空档只会骗模型
            continue
        tier = tool_tier(key) or ACTION_TIER
        switch = switch_of_tier(tier)
        if switch and flags.get(switch) is not True:
            continue
        picked[key] = tier
    return picked


def tier_counts(allowed: Mapping[str, str] | None) -> dict[str, int]:
    """按档位计数（调试输出与页面都用它，别在别处再数一遍）。"""

    counts = {tier: 0 for tier in TIERS}
    for tier in (allowed or {}).values():
        if tier in counts:
            counts[tier] += 1
    return counts


def artifact_root(base: Any, task_id: Any) -> Path:
    """产物根目录：``<base>/artifacts/<task_id>``（``base`` 一般是插件 data 目录）。"""

    return Path(str(base)) / "artifacts" / _safe_segment(task_id)


def _safe_segment(value: Any) -> str:
    """目录名只留安全字符：字母数字、下划线、连字符、点（点不能开头）。"""

    text = str(value or "").strip()
    kept = [char for char in text if char.isalnum() or char in ("_", "-", ".")]
    safe = "".join(kept).strip(".")
    return safe[:64] or "unknown"


def safe_artifact_path(base: Any, task_id: Any, name: Any) -> Path | None:
    """她自己生成的产物**允许写到哪**：越界、绝对路径、空名一律 ``None``。

    规则（照媒体资产那套"路径必须落在根里"的纪律，**从严**）：
    - 只收**纯文件名**：带 ``/`` 或 ``\\`` 的一律拒绝（模型想写子目录就让它改名重试，
      不做"悄悄取最后一段"这种好事——那会让"我明明写到别处了"变成说不清）；
    - 含 ``..``、以点开头（隐藏文件）、空名的拒绝；
    - 允许中文与常见全角括号；其余字符滤掉；
    - 长度夹到 120（保留扩展名）；最终路径必须真的在 :func:`artifact_root` 里面。
    """

    raw = str(name or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return None
    leaf = raw
    if leaf in (".", "..") or leaf.startswith("."):
        return None
    cleaned = "".join(
        char for char in leaf if char.isalnum() or char in ("_", "-", ".", " ", "（", "）", "(", ")")
    ).strip()
    if not cleaned or cleaned.startswith("."):
        return None
    if len(cleaned) > 120:
        stem, _, suffix = cleaned.rpartition(".")
        cleaned = (stem[: max(1, 120 - len(suffix) - 1)] + "." + suffix) if suffix else cleaned[:120]
    root = artifact_root(base, task_id)
    target = root / cleaned
    if not within(root, target):
        return None
    return target


def within(root: Any, target: Any) -> bool:
    """``target`` 是否真的落在 ``root`` 里面（解析软链/相对段之后再比）。"""

    try:
        root_path = Path(root).resolve()
        target_path = Path(target).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if target_path == root_path:
        return False
    return root_path in target_path.parents


def artifact_expiry(now: Any, keep_days: Any) -> float:
    """产物过期时间：``now + keep_days`` 天；天数<=0 表示不过期（回 0）。"""

    try:
        days = float(keep_days)
    except (TypeError, ValueError):
        days = 0.0
    if days <= 0:
        return 0.0
    try:
        stamp = float(now)
    except (TypeError, ValueError):
        stamp = 0.0
    return stamp + days * 86400.0
