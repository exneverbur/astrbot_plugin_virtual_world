"""AstrBot 虚拟世界插件（Star 入口）。

两条互斥的路径：
- **接管回复**（默认，也是唯一一种回复方式）：用户 @Bot 时本插件把整轮接过来，
  自己组装提示词、自己调 LLM、自己执行动作并发送，然后 `stop_event()` 阻止主人格重复回复；
  顺带按区域裁剪可用工具（`on_llm_request`）。她说不出来或模型出错时这一条就不回了，
  只写日志——**不会交回主人格**。
- **自主行为**：日程、发呆、搜索、主动搭话由插件自己调 LLM 并用 JSON 约束输出，自己发送消息。
  自己发起的调用按协程打标记，绝不会被自己重复接管。

配置热加载、Web 编辑器、状态与记忆都在 core/ 里实现，本文件只做 AstrBot 适配。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import shutil
import threading
import time
import types
import urllib.parse
import uuid
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from pydantic import ValidationError

from .core.config_store import HISTORY_KEEP, ConfigStore
from .core.models import WorldConfig
from .core.agent_artifacts import guess_mime as _guess_mime
from .core.db import AsyncDatabase
from .core.defaults import (
    DEFAULT_CAPTION_PROMPT,
    DEFAULT_FORWARD_PROMPT,
    DEFAULT_WORLD,
)
# B1/WP4：语义索引队列。只有真要装配时才构造，未配置时 import 本身不碰任何东西。
from .core.embedding_index import TopicIndexQueue
from .core import knowledge
from .core import knowledge_card
from .core import plugin_forge as _forge_module
from .core import meme_pack as _meme_pack
from .core import skills as skill_policy
from .core import world_change
from .core.engine import (
    PENDING_IMAGE_KEEP,
    REPLY_INTERRUPTED,
    REPLY_MUTED,
    MessageContext,
    VirtualWorldEngine,
)
from .core.extensions import ExtensionHost, ExtensionSpec
from .core.experience import (
    ExperienceError,
    TopicNotFound,
    TopicVersionConflict,
)
from .core.meme_settings import meme_settings
from .core.meme_store import (
    PHASE_REPLY,
    STATE_ENABLED,
    MemeStore,
    allocate_image_budget,
    asset_facts,
    budget_coverage_text,
    occurrence_id_for,
)
from .core.media_store import looks_like_media_id
from .core import action_discovery as action_discovery_policy
from .core import action_registry as action_registry_policy
from .core import migrations as migration_policy
from .core.config_index_queue import ConfigIndexHandler, ConfigIndexQueue
from .core.models import (
    DEFAULT_INTIMACY,
    EmbeddingSpace,
    FIELD_LABELS,
    normalize_edge_keys,
    normalize_legacy_keys,
    pronoun_for,
    repeat_preview,
)
from .core.nickname import compute_nickname
from .core.ports import (
    SEND_ACCEPTED,
    SEND_DELIVERED,
    SEND_FAILED,
    SEND_UNKNOWN,
    AstrBotEmbedding,
    CardResult,
    LLMReply,
    PokeResult,
    SendReceipt,
    ToolCallResult,
    ToolInfo,
    as_receipt,
)
from .core.json_actions import join_wrapped, loads_json_repaired, raw_output_fields, scrub_secrets
from .core.prompt import clip_line, prompt_section_index, readable_caption
from .core.state import FORWARD_SUMMARY_MARK
from .core.timeline import TIMELINE_SPAN_LABELS, build_agent_runs, build_timeline

PLUGIN_NAME = "astrbot_plugin_virtual_world"
# 状态页那些按钮（推进 tick / 触发决策 / 打断 / 叫醒）最多等这么久：
# 她正在检索或调模型时可能占着会话，超时就明确回一句，别让前端"点了没反应"
STATE_ACTION_TIMEOUT = 6.0
# 她正在调模型时点「推进 tick」：这次推进不排队，但愿意等这一轮跑完（秒）
STATE_ACTION_BUSY_TIMEOUT = 90.0
# 「推进 tick」这一下最多等多久（秒）：这一轮里可能夹着大模型调用（刚走到新地点要就地决定、
# 检索…），等不到就把它留在后台跑完，**绝不中途取消**——取消会把这一轮砍在半路。
STATE_TICK_WAIT_SECONDS = 45.0
# 钩子优先级：比默认 0 低，让其他插件先写完 system_prompt / 先决定要不要接管。
# AstrBot 按 priority 从高到低执行钩子，并且一旦某个钩子 stop 了事件，后面的就不再执行。
LLM_HOOK_PRIORITY = -100
# 睡觉门禁要抢在「意图路由」这类消息级插件前面：它们通常注册在 100 左右，
# 取值比它们高才会先执行；一旦这里 stop_event()，后面的处理器都不会跑。
SLEEP_GUARD_PRIORITY = 200
# 「旁观记录」也要抢在它们前面。意图路由会把消息**先吞下去再挑一部分重投**，
# 被吞掉的那几条如果排在它后面才记录，就永远记不到——她看到的聊天记录会缺一大段。
# 排在门禁之后（门禁要第一个拦睡眠）、路由之前。
PRESENCE_PRIORITY = 190
# 同一条会话的前一条回复最多让后一条等这么久；超时就不再排队（宁可多说一句，也别一直不说话）
REPLY_TURN_WAIT_SECONDS = 25.0
# 一整轮回复最多生成多久；到点就放弃这一轮、把世界状态锁放掉（用户 m10704：
# 搜索之后的模型调用挂住，@ 她也没反应的根子就是"这一轮永远不结束"）
REPLY_HARD_SECONDS = 180.0
# 真的 @ 她（不是路由补的假 @）多给一点时间：用户 m19362 要「被直接@触发时
# 应和普通触发的计时区别开」。排队等待本来就不在这份预算里（它从抢到锁、
# 开始生成算起），这里只是把"她还忙着上一轮"的那点余量留出来。
REPLY_HARD_SECONDS_AT = 240.0
# 同一条消息的重投（意图路由把消息吞下去再重投 / 旁观与 LLM 请求两个钩子各记一次）
# 认同一个身份：这段时间里见过就不当新消息（用户 m19362：同一条消息把自己打断，
# 然后谁都没回她）。取 120 秒：比一轮最长生成时间短，不会吃掉真正的连发。
REPLY_DUPLICATE_TTL_SECONDS = 120.0
# 被打断的那一批没人接手时，过多久再试一次（用户 m19948 第③条）。太短会和刚发来的
# 那条抢，太长就变成"她半天之后突然回一句"；6 秒留给新消息自己的那轮先跑。
REPLY_RETRY_DELAY_SECONDS = 6.0
# 事件上挂的「这条消息已经在待回队列里了」标记（连发合并用）
VW_INCOMING_EXTRA = "vw_incoming_record"
#: 阶段A3：这条事件上"发完消息"的钩子是**我们自己补发**的（`_fire_hook`）。
#: 同一条话已经按发送回执记过捕获账了，钩子处理器再看见要跳过，不能记第二遍。
VW_SELF_SENT_EXTRA = "vw_self_sent_hook"
#: 这条事件上的 `on_decorating_result` 是**我们自己补发**的（`_bridge_reply_hooks`）：
#: 为了别的插件能改写她的话，我们把她的回复包成模型结果又送进了一遍钩子链——那一份
#: 是她自己说的，不是主人格。`on_host_result` 看见这个标记必须放行，不然她每说一句
#: 都会被自己的守卫拦一次。标记只活在 `_bridge_reply_hooks` 的 try/finally 里。
VW_BRIDGED_REPLY_EXTRA = "vw_bridged_reply_hook"
# 平台不给 message_id 时，同一条消息的**稳定内部事件 id** 挂在这里：
# 旁观监听和 LLM 请求两个钩子拿到的是同一个 event 对象，因此复用同一个 id。
VW_EVENT_ID_EXTRA = "vw_event_id"
# 这一条消息的打点记录（到达 / 走到模型钩子 / 拿到回复锁 / 模型起止 / 发送）挂在这里
VW_TIMELINE_EXTRA = "vw_timeline_record"
# 从"消息到达"到"发完"超过这么久，就无条件记一条（哪怕没开调试）：
# 「@ 了她好几分钟才回」这种只有把时间点摊开才查得动
TIMELINE_SLOW_SECONDS = 20.0
# 「到达 → 走到模型钩子」这一段超过这么久也单独记：这段是宿主在排队
# （会话锁 / 路由缓冲 / 上一条还没跑完），不是她在生成
TIMELINE_HOST_WAIT_SECONDS = 10.0
# 「等上一条回复说完」超过这么久也单独记
TIMELINE_QUEUE_WAIT_SECONDS = 5.0
# 打点记录最多留这么久：隔了很久的另一条消息不要借到上一条的"到达"时刻
TIMELINE_ARRIVAL_TTL_SECONDS = 600.0
TOKEN_TTL_SECONDS = 24 * 3600
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
DATA_DIR_ENV = "VIRTUAL_WORLD_DATA_DIR"
# B1 收尾：``_agent_status_for_engine`` 报向量空间指纹时，"装配缓存里还没有向量空间"的
# **明确表达**。这个字段正常是一个十六进制指纹（``EmbeddingSpace.fingerprint``），
# 空串会被读成"指纹就是空的"——等于装作有值。同步回调里拿不到真值时就报这个字面量。
SPACE_FINGERPRINT_UNBUILT = "未装配（还没有向量空间）"

# 表情包管理页的缩略图：长边缩到这个像素以内（用户 m20：「图片不要我点击查看了才能显示」）。
# 常量而不是配置项——配大了卡片白占流量、配小了看不清，没有值得让用户学的旋钮。
MEME_THUMB_MAX_PX = 320
# 缩略图内存缓存条数（一次最多也就看几十张卡片，别无限长）
MEME_THUMB_CACHE = 128


def _resolve_data_dir(plugin_dir: str) -> str:
    """数据目录：AstrBot 的 data/plugin_data/<插件名>/（可用环境变量覆盖）。"""

    override = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if override:
        return override
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    except Exception:
        # 极少数情况下拿不到宿主路径，退回插件目录，保证插件仍然可用
        return os.path.join(plugin_dir, "data")


# ======================================================================
# 构建戳（build stamp）：判定实机跑的是哪一份代码
# ======================================================================
# 本仓没有构建步骤：检出什么就跑什么。所以下面的 mtime 是「被 hash 的文件里
# 最后改动的那一个的时间」，**不是构建时间**（别改名成 build_time，那会让人
# 以为有个构建流程）。
BUILD_STAMP_HASH_UNKNOWN = "未计算"
BUILD_STAMP_HASH_PREFIX = 12
# 白名单：只有这些路径会改变运行时行为。
BUILD_STAMP_FILES = (
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
)
# 目录类白名单：(顶层目录, 参与 hash 的后缀)。core/ 只算 .py；
# pages/ 后缀为空 = 全收：前端资源不只是 js/css/html，图片 / 字体 / json
# 变了同样意味着「实机跑的不是同一份代码」。噪声靠下面的黑名单和大小上限挡。
BUILD_STAMP_DIRS = (
    ("core", (".py",)),
    ("pages", ()),
)
# 明确排除：缓存、版本库、运行时数据、测试与文档、工具、本机测试残留、
# 二进制/压缩包/编辑器临时文件。它们在实机上可能变，但变了不代表代码变了。
BUILD_STAMP_EXCLUDED_DIRS = frozenset(
    {"__pycache__", ".git", "data", "tests", "docs", "tools", ".vwtest"}
)
BUILD_STAMP_EXCLUDED_SUFFIXES = (".pyc", ".bak", ".zip", ".docx", ".swp")
# 小写；macOS / Windows 的目录噪声文件。
BUILD_STAMP_EXCLUDED_NAMES = frozenset({".ds_store", "thumbs.db"})
BUILD_STAMP_EXCLUDED_PREFIXES = (".graphflow", "~$")
# 单文件大小上限：超过就跳过并计数（别让一个误放的大文件把启动拖成秒级）。
BUILD_STAMP_MAX_FILE_BYTES = 8 * 1024 * 1024

# 进程内只算一次（模块级）：同一进程里插件重载不重复扫盘。
_BUILD_STAMP_CACHE: dict[str, Any] | None = None
_BUILD_STAMP_LOCK = threading.Lock()


def _build_stamp_rel_wanted(rel: str) -> bool:
    """这条 posix 相对路径要不要进 hash（只按白名单判断，不做 glob）。"""

    if rel in BUILD_STAMP_FILES:
        return True
    for top, suffixes in BUILD_STAMP_DIRS:
        if rel.startswith(top + "/"):
            # 后缀为空 = 该目录全收（pages/）。
            return not suffixes or rel.lower().endswith(suffixes)
    return False


def _build_stamp_rel_excluded(rel: str) -> bool:
    parts = rel.split("/")
    if any(part in BUILD_STAMP_EXCLUDED_DIRS for part in parts[:-1]):
        return True
    if any(part.startswith(BUILD_STAMP_EXCLUDED_PREFIXES) for part in parts):
        return True
    name = parts[-1].lower()
    if name in BUILD_STAMP_EXCLUDED_NAMES:
        return True
    return name.endswith(BUILD_STAMP_EXCLUDED_SUFFIXES)


def _build_stamp_collect(root: Path, notes: list[str] | None = None) -> list[str]:
    """扫出要参与 hash 的 posix 相对路径（顺序由调用方按字节序再排）。

    ``notes`` 非空时，把「真实路径跑到 root 外面」的文件记进去（junction /
    symlink）。Windows 上 junction 未必被 ``islink`` 认出来，所以用 realpath
    前缀兜底：不属于这份代码的文件宁可跳过，也不能算进 hash。
    """

    rels: list[str] = []
    real_root = os.path.normcase(os.path.realpath(root))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in BUILD_STAMP_EXCLUDED_DIRS
            and not name.startswith(BUILD_STAMP_EXCLUDED_PREFIXES)
        )
        for name in filenames:
            try:
                rel = (Path(dirpath) / name).relative_to(root).as_posix()
            except ValueError:
                continue
            if _build_stamp_rel_excluded(rel):
                continue
            if not _build_stamp_rel_wanted(rel):
                continue
            full = Path(dirpath) / name
            try:
                real = os.path.normcase(os.path.realpath(full))
                inside = os.path.commonpath([real_root, real]) == real_root
            except (OSError, ValueError):
                inside = False
            if not inside:
                if notes is not None:
                    notes.append(f"越界跳过：{rel}")
                continue
            rels.append(rel)
    return rels


def _compute_build_stamp(
    root: str | os.PathLike[str], version: str
) -> dict[str, Any]:
    """扫 root 下白名单文件算构建戳。

    **fail-soft 是硬要求**：任何异常都只写进 ``error`` 并返回，绝不抛出——
    这是诊断设施，不能反过来把插件启动搞挂。
    """

    stamp: dict[str, Any] = {
        "version": str(version or ""),
        "hash": BUILD_STAMP_HASH_UNKNOWN,
        "files": 0,
        "mtime": "",
        "dir": "",
        "error": None,
        "skipped": 0,
        "notes": "",
    }
    try:
        # 传 None / 非法对象时也在 try 内，别让 TypeError 逃出去。
        base = Path(root)
        # 保留 resolve()：实机上插件目录可能是 junction / symlink，这里要暴露真身。
        # resolve() 自己也可能失败（权限 / 超长路径），所以单独兜底。
        try:
            stamp["dir"] = str(base.resolve())
        except Exception:  # noqa: BLE001
            stamp["dir"] = str(base)
        if not base.is_dir():
            raise FileNotFoundError(f"目录不存在：{base}")
        notes: list[str] = []
        rels = sorted(
            _build_stamp_collect(base, notes), key=lambda item: item.encode("utf-8")
        )
        digest = hashlib.sha256()
        newest = 0.0
        skipped = 0
        for rel in rels:
            path = base / rel
            # 先 stat 再读：读之前按大小上限跳过，避免把大文件整个读进内存。
            # （stat 与 read 之间仍有理论竞态，但比「读完再 stat」窗口小。）
            stat = path.stat()
            if stat.st_size > BUILD_STAMP_MAX_FILE_BYTES:
                skipped += 1
                continue
            data = path.read_bytes()
            # CRLF 归一：Windows 的 core.autocrlf / .gitattributes 会让同一份代码
            # 在不同机器上字节不同。构建戳是用来判断「跑的是哪份代码」的，所以
            # 折掉 \r\n → \n 再喂 hash。**删掉这一行，换行风格不同的两台机器就会
            # 算出不同 hash，部署漂移诊断直接失效。**
            data = data.replace(b"\r\n", b"\n")
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
            newest = max(newest, stat.st_mtime)
        stamp["hash"] = digest.hexdigest()[:BUILD_STAMP_HASH_PREFIX]
        stamp["files"] = len(rels) - skipped
        stamp["skipped"] = skipped
        if notes:
            # 越界不是「算不出来」：hash 照算，只是少算了几个文件。单独放
            # ``notes``，别污染 ``error``——否则状态页会误报「hash 未计算」。
            stamp["notes"] = "；".join(notes)[:200]
        if newest:
            stamp["mtime"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(newest)
            )
        return stamp
    except Exception as exc:  # noqa: BLE001 - 诊断设施绝不能把插件带崩
        stamp["hash"] = BUILD_STAMP_HASH_UNKNOWN
        stamp["files"] = 0
        stamp["mtime"] = ""
        stamp["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return stamp


def _build_stamp_lines(build: dict[str, Any]) -> list[str]:
    """构建戳 → 给用户看的两三行（与启动日志是同一套事实）。"""

    version = str(build.get("version") or "未知版本")
    hash_text = str(build.get("hash") or BUILD_STAMP_HASH_UNKNOWN)
    directory = str(build.get("dir") or "")
    if build.get("error"):
        return [
            f"构建戳：{version} · hash {hash_text}（{build['error']}）",
            f"代码目录：{directory}",
        ]
    first = f"构建戳：{version} · {build.get('files', 0)} files · hash {hash_text}"
    extras: list[str] = []
    if build.get("skipped"):
        extras.append(f"跳过 {build['skipped']} 个超过 8MB 的文件")
    notes = str(build.get("notes") or "")
    if notes:
        # 越界跳过不是算不出来：与 skipped 并列显示，别互相覆盖。
        extras.append(f"未计入：{notes}")
    if extras:
        first += f"（{'；'.join(extras)}）"
    return [
        first,
        f"代码目录：{directory}",
        f"文件最后改动：{build.get('mtime') or ''}",
    ]


# 打点名 → 给用户看的中文见 `core/timeline.TIMELINE_SPAN_LABELS`（日志页共用同一份）


def _timeline_marks(event: Any) -> list[Any]:
    """取（必要时建）挂在这一条消息上的打点列表。

    挂在事件自身（``get_extra``）而不是插件上：同一个 event 对象会依次经过
    旁观钩子和 LLM 钩子，打点自然连成一条线。
    """

    record = event.get_extra(VW_TIMELINE_EXTRA, None)
    if not isinstance(record, dict):
        record = {"marks": []}
        event.set_extra(VW_TIMELINE_EXTRA, record)
    marks = record.get("marks")
    if not isinstance(marks, list):
        marks = []
        record["marks"] = marks
    return marks


def _timeline_mark(event: Any, name: str) -> None:
    """记一个时间点。同名的只记第一次——同一条消息可能从多个出口报到。"""

    try:
        marks = _timeline_marks(event)
    except Exception:  # noqa: BLE001 - 打点出问题绝不影响回话
        return
    if any(isinstance(item, dict) and item.get("name") == name for item in marks):
        return
    marks.append({"name": str(name), "at": time.time()})


def _timeline_detail(marks: Any) -> dict[str, Any]:
    """把打点压成「总时长 + 每一段多少秒」。数据不够（少于两个点）返回空。"""

    if not isinstance(marks, list):
        return {}
    ordered = sorted(
        (item for item in marks if isinstance(item, dict) and item.get("name")),
        key=lambda item: float(item.get("at") or 0.0),
    )
    if len(ordered) < 2:
        return {}
    start = float(ordered[0].get("at") or 0.0)
    spans: dict[str, float] = {}
    previous = start
    for item in ordered[1:]:
        at = float(item.get("at") or 0.0)
        spans[str(item.get("name"))] = round(at - previous, 2)
        previous = at
    return {"total": round(previous - start, 2), "spans": spans}


def _timeline_text(marks: Any) -> str:
    """给人看的一行：``共 12.4s｜到达→模型钩子 3.2s｜调模型与动作 8.4s``。"""

    detail = _timeline_detail(marks)
    spans = detail.get("spans") or {}
    parts = [
        f"{TIMELINE_SPAN_LABELS.get(str(key), str(key))} {float(value):.1f}s"
        for key, value in spans.items()
        if isinstance(value, (int, float))
    ]
    total = float(detail.get("total") or 0.0)
    return f"共 {total:.1f}s" + ("｜" + "｜".join(parts) if parts else "")


def _timeline_spans_text(marks: Any) -> str:
    """分段单独一行，最长的那段带标记：``安静期 0.2s｜调模型 30.0s←最长｜动作与写库 3.4s``。

    总时长和分段挤在一行时会被读成"其中某一段 36.1s"（用户 m30538 的误读），
    所以告警里总时长单独一行、分段另起一行；加标记是为了不用自己比数字。
    """

    detail = _timeline_detail(marks)
    spans = detail.get("spans") or {}
    measured = [
        (str(key), float(value))
        for key, value in spans.items()
        if isinstance(value, (int, float))
    ]
    if not measured:
        return ""
    slowest = max(measured, key=lambda item: item[1])[0]
    parts = []
    for key, value in measured:
        piece = f"{TIMELINE_SPAN_LABELS.get(key, key)} {value:.1f}s"
        if key == slowest:
            piece += "←最长"
        parts.append(piece)
    return "｜".join(parts)


# ======================================================================
# 适配器：把 AstrBot 的能力包成 core/ports.py 里的 Protocol
# ======================================================================


def node_label(world: Any, node_id: str) -> str:
    """节点 id -> 中文名（拿不到就退回 id）。"""

    node = world.node_map().get(node_id) if world is not None else None
    return (node.name or node.id) if node is not None else (node_id or "？")


def _tool_owner(tool: Any) -> str:
    """这个工具是哪个插件注册的（拿不到线索时返回空串，编辑器会归到「插件 · 其它」）。"""

    module_path = str(getattr(tool, "handler_module_path", "") or "")
    if not module_path:
        return ""
    try:
        from astrbot.core.star.star import star_map

        metadata = star_map.get(module_path)
    except Exception:
        return ""
    return str(getattr(metadata, "name", "") or "")


def _is_at_bot(event: Any) -> bool:
    """这条消息里是不是真的 @ 了她。

    不能只看 ``event.is_wake_up()``：意图路由那类插件会在放行时把 ``is_wake`` 置 True，
    那是"要交给大模型"，不等于"有人在跟她说话"。

    同样不能只看消息链里有没有 @：意图路由为了绕过宿主门禁，会往链头**补一个假的 @ 她自己**
    （见 :func:`_is_soft_wake`）。那个 @ 不是人打的，不剔掉的话 ``is_mentioned`` 会一直是
    True，``reply_addressing`` 第一行就返回 ``direct``——"顺着话题对她说"（``soft``）那条
    分支永远走不到，提示词还会写成「xxx 对你说：」，她就端着一副被点名的口气回话。
    2026-10-19 现场：群里有人发图、没人叫她，她回得像被点名了。
    """

    if _is_soft_wake(event):
        return False
    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    if not self_id:
        return False
    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "At":
            continue
        qq = str(getattr(component, "qq", "") or "")
        if qq and qq == self_id:
            return True
    return False


def _is_soft_wake(event: Any) -> bool:
    """意图路由放行时补的 @ 是假的：原话里没人 @ 她，只是"顺着话题对她说"。"""

    try:
        return bool(event.get_extra("intent_router_no_at", False))
    except Exception:
        return False


def _router_float(value: Any) -> float:
    """路由给的分数（拿不到就当 0）。"""

    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _router_view(event: Any) -> dict:
    """意图路由放行时交过来的事前判定（拿不到 / 形状不对就是空 dict）。

    路由在重投那一刻把**整批**判定挂在事件上（见它的 README「交给她的事前判定」）：
    ``batch`` 按到达顺序列出这一批每一条，含被合并成一次回复、正文根本没进请求的
    那几条，``released`` 标出真正放行的。价值在于「这句是不是冲她说的」这件事
    **回复之前**就有答案了——不用等她回完再让主模型自己猜（那是 ``addressing``，
    见 :meth:`Engine._mark_reply_addressing`）。

    这里只做形状校验和取值：判定本身是路由给的，本插件不重新解释。
    """

    try:
        raw = event.get_extra("intent_router_decision", None)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    rows = raw.get("batch")
    if not isinstance(rows, list):
        return {}
    batch: list[dict] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        batch.append(
            {
                "sender": str(entry.get("sender") or ""),
                "sender_id": str(entry.get("sender_id") or ""),
                "message_id": str(entry.get("message_id") or ""),
                "text": str(entry.get("text") or ""),
                "released": bool(entry.get("released")),
                "worth": bool(entry.get("worth")),
                "directed": bool(entry.get("directed")),
                "to": str(entry.get("to") or ""),
                "reply_score": _router_float(entry.get("reply_score")),
                "reason": str(entry.get("reason") or ""),
            }
        )
    if not batch:
        return {}
    return {"policy": str(raw.get("policy") or ""), "batch": batch}


def _group_name(event: Any) -> str:
    """平台报的群名（拿不到就返回空串，提示词里就只显示群号 / 备注）。"""

    getter = getattr(event, "get_group_name", None)
    if callable(getter):
        try:
            value = " ".join(str(getter() or "").split())
        except Exception:
            value = ""
        if value:
            return value[:40]
    group = getattr(getattr(event, "message_obj", None), "group", None)
    for attr in ("group_name", "name"):
        value = " ".join(str(getattr(group, attr, "") or "").split())
        if value:
            return value[:40]
    return ""


#: 宿主内置的 ``astrbot`` 星在「只 @ 了她、正文一个字没有」时走 ``empty_mention``
#: 分支，把一段**写给我们这些插件作者看的提示**当成用户输入塞进 LLM 请求
#: （``AstrBot/astrbot/builtin_stars/astrbot/main.py`` 的 ``empty_mention``）。
#: 她照着那段话回答就成了客服腔：「请问有什么可以帮您」。用户要的只是她知道
#: 「被 @ 了一下」，上下文她自己会联系（用户 m00001）。
_HOST_EMPTY_MENTION_MARKERS = ("只是通过@来唤醒你", "仅需要输出要回复用户的内容")
#: 删的时候认宽一点：宿主哪怕改了措辞（或中间的标点变了、整句匹配不上），
#: 靠这些碎片按句兜底，也不能漏下一句「回复要符合人设」。
_HOST_EMPTY_MENTION_TELLS = (
    *_HOST_EMPTY_MENTION_MARKERS,
    "你正在社交媒体上中与用户进行聊天",
    "但并未在这条消息中输入内容",
    "他可能会在接下来一条",
    "回复要符合人设",
    "不要太过机械化",
    "用户想要聊些什么",
    "不要输出其他任何东西",
)
#: 宿主那三句（首尾相接直接拼起来，中间没有分隔符）。整句删比按特征串切更干净。
_HOST_EMPTY_MENTION_SENTENCES = (
    "注意，你正在社交媒体上中与用户进行聊天，用户只是通过@来唤醒你，"
    "但并未在这条消息中输入内容，他可能会在接下来一条发送他想发送的内容。",
    "你友好地询问用户想要聊些什么或者需要什么帮助，回复要符合人设，不要太过机械化。",
    "请注意，你仅需要输出要回复用户的内容，不要输出其他任何东西",
)
#: 剔掉宿主提示后替她填的这一轮「用户文本」：只说她被叫了一下，不催她发问，
#: 也不许她照着这句话回（那是旁白，不是台词）。
HOST_EMPTY_MENTION_TEXT = "（TA 只 @ 了你一下，还没说事）"


def strip_host_empty_mention_note(text: str) -> tuple[str, bool]:
    """剔掉宿主「只 @ 了一下」的人机提示，返回 ``(清洗后文本, 有没有剔到)``。

    宿主在「只 @ 了她、没有任何文字」时会把一段提示当成用户输入丢进来。这段话
    是写给开发者的（「你友好地询问用户想要聊些什么」），她照做就成了机器人。

    还有真实文字（``@她 在吗``）时**只删提示、正文原样留下、不加说明句**——加了
    她就以为那句「在吗」不是人说的。
    """

    raw = str(text or "")
    if not any(marker in raw for marker in _HOST_EMPTY_MENTION_MARKERS):
        return raw, False
    cleaned = raw
    for sentence in _HOST_EMPTY_MENTION_SENTENCES:
        # 字符间允许空白：宿主拼串时可能顺手加了空格/换行
        pattern = r"\s*".join(re.escape(char) for char in sentence)
        cleaned = re.sub(pattern, " ", cleaned)
    # 兜底：宿主改了措辞时按句切，带特征串的句子整句删掉
    if any(marker in cleaned for marker in _HOST_EMPTY_MENTION_TELLS):
        parts = re.split(r"(?<=[。！？!?\n])", cleaned)
        cleaned = "".join(
            part
            for part in parts
            if not any(marker in part for marker in _HOST_EMPTY_MENTION_TELLS)
        )
    # 删完留下的多空格并掉，但保留原来的分行（多行正文别被压成一行）
    cleaned = "\n".join(" ".join(line.split()) for line in cleaned.splitlines()).strip()
    # 只剩标点/空白 → 这一轮她其实没收到任何文字
    if not re.sub(r"[\s。，、；：！？…—－·,.;:!?~～（）()\[\]【】\"'“”‘’]", "", cleaned):
        return HOST_EMPTY_MENTION_TEXT, True
    return cleaned, True


def _mention_note(event: Any) -> str:
    """这条消息 @ 了谁——只说结论，不讲平台怎么处理正文。

    她不知道自己的 QQ 号，所以别人 @ 她的时候，光看文本里的 ``@昵称`` 她分不清
    那是不是在叫她。这里直接用「你」标出"有没有 @ 她"，再点出"@ 的别人不是在
    叫她"，别把别人的话揽到自己身上。

    第三个坑：**意图路由补的那个 @ 是假的**（它只是为了让消息重新走通管道）。
    照着它写"这条消息 @ 了你"，她会以为对方真的点了她——而用户看到的是一句
    平白多出来的"@ 了你"。这种消息只留一句"是顺着话题说到你的"。

    以前这里还会解释「平台把开头的 @ 自己省掉了」「你说的这段正文里可能看不到
    那一下」——那段是写给开发者看的，她只要知道结论就够了，去掉。
    """

    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    soft = _is_soft_wake(event)
    message_obj = getattr(event, "message_obj", None)
    others: list[str] = []
    mine = False
    everyone = False
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ not in ("At", "AtAll"):
            continue
        qq = str(getattr(component, "qq", "") or "").strip()
        if soft and qq == self_id:
            # 路由补的假 @：不算"点了她"，也别写进记录
            continue
        name = str(getattr(component, "name", "") or "").strip()
        if qq == "all":
            everyone = True
            continue
        label = _at_label(name, qq)
        if not label:
            continue
        if qq and self_id and qq == self_id:
            mine = True
            continue
        # @ 的是别人：这段不能让她以为是在叫她
        others.append(label)
    if soft and not others and not everyone:
        # 别写成"说到你的"：群里可能在聊别的人别的事（甚至另一个机器人），
        # 说死了会让她把别人的话当成在叫她（用户 m07347 的现场就是这样）。
        return "这条没 @ 你：是群里在聊的话，可能根本没说到你；要接就顺着聊，别当成在叫你"
    if not mine and not others and not everyone:
        return ""
    named = "、".join(dict.fromkeys(others))
    all_note = "；还 @ 了所有人" if everyone else ""
    if mine:
        if named:
            return f"这条消息 @ 了你；同一条里 @ 的 {named} 不是你{all_note}"
        return "这条消息 @ 了你，也 @ 了所有人" if everyone else "这条消息 @ 了你"
    if everyone:
        return (
            f"这条消息 @ 了所有人，还有 {named}（都不是在 @ 你）"
            if named
            else "这条消息 @ 了所有人（包括你）"
        )
    return f"这条消息 @ 的 {named} 不是你（别揽到自己身上）"


def _at_label(name: str, qq: str) -> str:
    """@ 目标的标签：名字和号码尽量都给，但别写成 ``2829449702(2829449702)``。"""

    clean_name = " ".join(str(name or "").split())
    clean_qq = str(qq or "").strip()
    if not clean_name:
        return clean_qq
    if not clean_qq or clean_name == clean_qq:
        return clean_name
    return f"{clean_name}({clean_qq})"


def _message_id(event: Any) -> str:
    """平台给的消息 id（拿不到就是空串）。

    这个 id 只用来**认"是不是同一个事件"**，不参与"这个平台支不支持某功能"的判定，
    所以不像引用段那样先按平台名挡一道。
    """

    message_obj = getattr(event, "message_obj", None)
    return str(getattr(message_obj, "message_id", "") or "").strip()


def _stable_event_id(event: Any) -> str:
    """没有平台 message_id 时，为这条消息生成一个**稳定**的内部事件 id。

    存在 event extra 里：旁观监听和 LLM 请求两个钩子拿到的是同一个 event 对象，
    所以两边拿到的是同一个 id——正文被注解改过也不会变成"另一条消息"。
    """

    getter = getattr(event, "get_extra", None)
    if callable(getter):
        try:
            existing = str(getter(VW_EVENT_ID_EXTRA, "") or "")
        except Exception:
            existing = ""
        if existing:
            return existing
    created = f"{int(time.time_ns())}-{id(event)}"
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        try:
            setter(VW_EVENT_ID_EXTRA, created)
        except Exception:
            return created
    return created


def _sender_card(event: Any) -> str:
    """这个人在**当前这个群**的群名片（拿不到就退成昵称）。

    用户 m14014：同一个人在 A 群叫「阿清」、在 B 群叫「清神」，通讯录要按群各存一份，
    她才认得出"这几张名片是同一个人"。协议端的 sender 里 ``card`` 就是群名片。
    """

    sender = getattr(getattr(event, "message_obj", None), "sender", None)
    for key in ("card", "nickname"):
        value = sender.get(key) if isinstance(sender, dict) else getattr(sender, key, "")
        text = " ".join(str(value or "").split())
        if text:
            return text[:40]
    getter = getattr(event, "get_sender_name", None)
    text = ""
    if callable(getter):
        try:
            text = " ".join(str(getter() or "").split())
        except Exception:  # noqa: BLE001 - 拿不到就当没有
            text = ""
    return text[:40]


def _incoming_identity(event: Any, raw_text: str) -> dict:
    """一条消息的事件身份 + 结构化标注（登记待回队列、写留档都用它）。

    有平台 ``message_id`` 时按 ``origin + message_id`` 认同一个事件；没有就用
    :func:`_stable_event_id` 生成的内部 id。``raw_text`` 是注解**之前**的原文——
    事件身份认的是这条消息本身，注解（图片说明 / @ / 转发摘要）改不了它。
    """

    message_id = _message_id(event)
    event_id = "" if message_id else _stable_event_id(event)
    targets = _at_targets(event)
    reply_to = _reply_target(event)
    self_id = ""
    getter = getattr(event, "get_self_id", None)
    if callable(getter):
        try:
            self_id = str(getter() or "")
        except Exception:
            self_id = ""
    sender_id = ""
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        try:
            sender_id = str(getter() or "")
        except Exception:
            sender_id = ""
    sender_name = ""
    getter = getattr(event, "get_sender_name", None)
    if callable(getter):
        try:
            sender_name = str(getter() or "")
        except Exception:
            sender_name = ""
    return {
        "message_id": message_id,
        "event_id": event_id,
        "user_id": sender_id,
        "user_name": sender_name,
        "raw_text": str(raw_text or ""),
        "at_targets": targets,
        "reply_to": reply_to,
        "addressing": _addressing_kind(targets, reply_to, self_id),
    }


def _reply_identity(event: Any) -> str:
    """这条消息的身份（回复去重用）：优先平台 ``message_id``，没有就用内部稳定 id。

    用户 m19362 的现场：意图路由把同一条消息吞下去再重投、旁观与 LLM 请求两个钩子
    又各记一次，第二次进来时上一轮还握着锁——于是它把**自己**打断了，然后自己又被
    "已并入上一条"挡掉，谁都没回。这里给它一个能在两次投递之间对上的身份。
    """

    if event is None:
        return ""
    message_id = _message_id(event)
    if message_id:
        return "m:" + message_id
    try:
        stable = _stable_event_id(event)
    except Exception:  # noqa: BLE001 - 拿不到身份就当"认不出"，照旧走老路
        stable = ""
    return ("e:" + stable) if stable else ""


def _at_targets(event: Any) -> list[dict]:
    """这条消息 @ 了谁（含她自己），结构化一份。

    意图路由为了让消息重新走通管道，会往消息链里**补一个假的 @ 她自己**
    （见 `_is_soft_wake`）。那个 @ 不是用户打的，必须剔掉——否则她的记录里
    会出现"这条消息 @ 了你"，而她明明只是顺着话题被叫起来。
    """

    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    soft = _is_soft_wake(event)
    message_obj = getattr(event, "message_obj", None)
    targets: list[dict] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ not in ("At", "AtAll"):
            continue
        qq = str(getattr(component, "qq", "") or "").strip()
        if soft and qq == self_id:
            continue
        name = " ".join(str(getattr(component, "name", "") or "").split())
        if qq == "all":
            targets.append({"id": "all", "name": "所有人", "self": False})
            continue
        if not qq and not name:
            continue
        targets.append(
            {
                "id": qq,
                "name": name or qq,
                "self": bool(qq and self_id and qq == self_id),
            }
        )
    return targets


def _reply_target(event: Any) -> dict:
    """这条消息引用 / 回复的是谁（拿不到就是空的）。"""

    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "Reply":
            continue
        uid = str(
            getattr(component, "sender_id", "") or getattr(component, "qq", "") or ""
        ).strip()
        name = " ".join(
            str(
                getattr(component, "sender_nickname", "")
                or getattr(component, "sender_name", "")
                or ""
            ).split()
        )
        if uid or name:
            return {"id": uid, "name": name or uid}
    return {}


def _addressing_kind(
    targets: list[dict], reply_to: dict, self_id: str
) -> str:
    """这句是冲谁说的：``me``（点了她）/ ``others``（在叫别人）/ ``""``（看不出来）。

    只用来标注，不参与"要不要回复"的判定——那个还是走 ``ctx.is_mentioned`` 那一套。
    """

    mine = any(item.get("self") for item in targets or [])
    if str((reply_to or {}).get("id") or "") and str((reply_to or {}).get("id")) == str(
        self_id or ""
    ):
        mine = True
    if mine:
        return "me"
    if targets or reply_to:
        return "others"
    return ""


def _is_command(text: str) -> bool:
    """看起来是一条指令（/xxx、！xxx 这类）。指令永远不该被她睡觉挡住。"""

    stripped = (text or "").lstrip()
    return bool(stripped) and stripped[0] in "/!！.。"


_ASTRBOT_PERSONA_HEAD = "# Persona Instructions"
# AstrBot 自己的「这个会话不设人格」哨兵（`astrbot/core/persona_mgr.py:110-135`）。
ASTRBOT_NO_PERSONA = "[%None]"


def _astrbot_persona_block(persona_prompt: str) -> str:
    """宿主追加到 system_prompt 里的那一段人设，逐字（`astr_main_agent.py:574`）。"""

    return f"\n{_ASTRBOT_PERSONA_HEAD}\n\n{persona_prompt}\n"


_HOST_CONTEXT_BRACKET_TOOLS = (
    "send_ai_voice",
    "search_wyc_tools",
    "call_wyc_tools",
    "run_wyc_tool",
)
"""``[重要工具使用规范]`` / ``[AI语音配置]`` 这类段落提名的工具。

2026-10-20 实机取证：某台的提示词里有 ``[重要工具使用规范]``，要求她先
``search_wyc_tools`` 再 ``call_wyc_tools`` 再 ``run_wyc_tool``——可她手上一个都没有。
这种"教她调用不存在的工具"的段落只会让她空转，按工具存在性决定去留。
"""


def _filter_host_context(text: str, available: Any = None) -> str:
    """摘掉「其他插件提供的上下文」里跟她无关的段落，认不出的原样保留。

    只处理三类确定的噪声（2026-10-20 实机取证，见 ``ContextConfig.filter_host_context``）：

    1. AstrBot 内置技能清单 ``## Skills``（documents / pdf / skill-creator / spreadsheets），
       以及紧跟其后的「必须先用 shell ``cat`` 读 SKILL.md」规则；
    2. 宿主运行环境说明 ``You have access to the host local environment`` 到
       ``Current workspace: …`` 那几行（Linux、shell 会话、workspace 路径）；
    3. ``[重要工具使用规范]``：里面点名的工具一个都不在 ``available`` 里就整段删；
       ``[AI语音配置]``：没有 ``send_ai_voice`` 时删。

    ``[System Note…]``（昵称不是身份）、``[系统状态]``、``[当前群身份]`` 一律保留。
    ``available`` 拿不到（``None``）时保守处理：工具相关的段落都留着。
    """

    body = str(text or "")
    if not body.strip():
        return ""
    names: set[str] = set(available) if isinstance(available, dict) else set()
    lines = body.split("\n")
    out: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        head = line.strip()
        if head == "## Skills":
            index += 1
            while index < total:
                nxt = lines[index].strip()
                if nxt.startswith("[") or nxt.startswith("# ") or nxt.startswith(
                    "You have access to the host local environment"
                ):
                    break
                index += 1
            continue
        if head.startswith("You have access to the host local environment"):
            index += 1
            while index < total:
                cur = lines[index].strip()
                index += 1
                if cur.startswith("["):
                    break
                if cur.startswith("Current workspace:"):
                    # workspace 那行后面还跟着两句"文件工具怎么解析相对路径"，一起丢
                    while index < total and lines[index].strip() and not lines[index].strip().startswith("["):
                        index += 1
                    break
            continue
        if head.startswith("[AI语音配置]"):
            if names and "send_ai_voice" not in names:
                index += 1
                continue
        if head.startswith("[重要工具使用规范]"):
            block: list[str] = [line]
            index += 1
            while index < total and not lines[index].strip().startswith("["):
                block.append(lines[index])
                index += 1
            if names and not (names & set(_HOST_CONTEXT_BRACKET_TOOLS)):
                continue
            out.extend(block)
            continue
        out.append(line)
        index += 1
    cleaned = "\n".join(out)
    # 段落删掉之后常留下三四个空行：收成最多一个空行。
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned.strip()


def _strip_persona_instructions(text: str, persona_prompt: str = "") -> str:
    """按精确文本摘掉 AstrBot 那份「人格设定」，认不出来就原样不动。

    AstrBot 是 ``req.system_prompt += f"\\n# Persona Instructions\\n\\n{prompt}\\n"``
    逐字追加的，所以只要拿到这一轮真正生效的那份 persona 就能精确删掉。
    拿不到就别猜——以前那版从标记砍到「下一个一级标题」：人设自己带一级标题时
    会漏（半段人设留在「其他插件提供的上下文」里念给她听），后面没有一级标题时
    又会把别的插件写在 persona 之后的内容整段删掉。

    其余插件写进 system_prompt 的内容（上下文理解、记忆…）原样保留。
    """

    body = str(text or "")
    prompt = str(persona_prompt or "")
    if prompt and _ASTRBOT_PERSONA_HEAD in body:
        body = body.replace(_astrbot_persona_block(prompt), "", 1)
    return body.strip()


def _strip_webchat_default_persona(text: str) -> str:
    """webchat 那条「特殊默认人格」不带标记，只能按常量精确删。"""

    body = str(text or "")
    try:
        from astrbot.core.astr_main_agent_resources import (  # noqa: PLC0415
            CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT,
        )
    except Exception:
        return body
    marker = str(CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT or "")
    if marker and marker in body:
        body = body.replace(marker, "", 1)
    return body.strip()


def _shared_preferences() -> Any:
    """AstrBot 的会话偏好存储（`astrbot.api.sp`）；拿不到就当没有。"""

    try:
        from astrbot.api import sp  # noqa: PLC0415
    except Exception:
        return None
    return sp


def _persona_begin_dialogs(persona: Any) -> list[Any]:
    """宿主拼进 ``req.contexts`` 开头的那段人设示例对话（`astr_main_agent.py:576`）。"""

    if isinstance(persona, dict):
        dialogs = persona.get("_begin_dialogs_processed")
    else:
        dialogs = getattr(persona, "_begin_dialogs_processed", None)
        if dialogs is None and hasattr(persona, "get"):
            dialogs = persona.get("_begin_dialogs_processed")
    return list(dialogs) if isinstance(dialogs, (list, tuple)) else []


def _drop_begin_dialogs(history: Any, dialogs: list[Any]) -> list[Any]:
    """把历史开头那段人设示例对话摘掉；对不上就一条都不动。

    宿主是 ``req.contexts[:0] = copy.deepcopy(begin_dialogs)`` 把它们插在最前面的，
    所以从第 0 条起逐条按**整体内容**比对；一旦对不上立刻停下——绝不按"看起来像对话"
    去猜，宁可让她多看一眼，也不能把真实历史删掉。
    """

    items = list(history) if isinstance(history, (list, tuple)) else []
    head = 0
    while dialogs and head < len(items) and head < len(dialogs):
        if items[head] != dialogs[head]:
            break
        head += 1
    return items[head:]


_ASTRBOT_CHECKPOINT_ROLE = "_checkpoint"


def _history_role(message: Any) -> str:
    """历史里一条记录的 role（dict 和 Message 两种形态都认）。"""

    if isinstance(message, dict):
        return str(message.get("role") or "")
    return str(getattr(message, "role", "") or "")


def _drop_orphan_checkpoint_rounds(history: Any) -> list[Any]:
    """摘掉宿主留下的"孤儿轮"：一条 user 后面直接跟 `_checkpoint`，中间没有 assistant。

    宿主在**模型调用失败/被打断**时也会把这一轮落库（`internal.py:478-500`：`user` +
    `_checkpoint`，没有 assistant）。送到 provider 之前，框架只丢掉 `_checkpoint` 那一条
    （`provider.py:193`），于是那条 user 会以纯文本留在她的上下文里——她会看到一句
    "自己没回过的话"，还以为是没理人。
    正常一轮是 `user → assistant → _checkpoint`，所以只认这一种形状；认不出来就一条都不动。
    """

    items = list(history) if isinstance(history, (list, tuple)) else []
    kept: list[Any] = []
    index = 0
    while index < len(items):
        item = items[index]
        nxt = items[index + 1] if index + 1 < len(items) else None
        if (
            _history_role(item) == "user"
            and _history_role(nxt) == _ASTRBOT_CHECKPOINT_ROLE
        ):
            index += 2
            continue
        kept.append(item)
        index += 1
    return kept


def _history_text(message: Any) -> str:
    """历史里一条记录的正文（抠不出来就返回空串，**绝不猜**）。"""

    if isinstance(message, dict):
        raw = message.get("content")
    else:
        raw = getattr(message, "content", None)
        if raw is None:
            raw = getattr(message, "message_str", None)
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
                continue
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if text:
                parts.append(str(text))
        return "\n".join(part for part in parts if part).strip()
    return ""


def _note_unknown_direct_sends(engine: Any, history: Any, session_id: str) -> int:
    """阶段A3「未知直发」：历史里她那一侧的话，**不是我们发的**就标 bot_external。

    宿主历史里的 assistant 一条，可能是别的东西直接发出去的（另一个插件自己调平台、
    宿主兜底回复……）。这些不是她"主动选的动作"，所以既不能算进她自己的发言账，
    也要按 ``bot_external`` 留痕，免得下次当成"我说过这句"。
    认人的办法只有一种：这条正文在捕获账里出现过（我们发的 / 扩展发的 / 钩子见过的）。
    认不出来的时候宁可不记，也不改历史内容。
    """

    if engine is None:
        return 0
    try:
        from .core.capture import CAPTURE_UNKNOWN_DIRECT, capture_identity
    except Exception:  # pragma: no cover - 导入失败就当没这功能
        return 0
    captures = getattr(engine, "captures", None)
    if captures is None:
        return 0
    noted = 0
    for item in list(history or []):
        if _history_role(item) != "assistant":
            continue
        text = _history_text(item)
        if not text:
            continue
        identity = capture_identity(session_id=session_id, texts=[text])
        if not identity or captures.seen(identity):
            continue
        if captures.note(
            CAPTURE_UNKNOWN_DIRECT,
            session_id=session_id,
            texts=[text],
            text=text,
            origin="host_history",
            aliases=(identity,),
        ):
            noted += 1
    return noted


def _result_chain_texts(event: Any) -> list[str]:
    """这条事件"刚刚发出去"的正文（从结果链里抠；抠不出来返回空列表）。

    只读，不改任何东西：钩子跑在 ``event.clear_result()`` 之前，结果还在。
    """

    try:
        result = event.get_result()
    except Exception:
        return []
    chain = list(getattr(result, "chain", None) or [])
    texts: list[str] = []
    for comp in chain:
        text = getattr(comp, "text", None)
        if text is None and isinstance(comp, dict):
            text = comp.get("text")
        if text and str(text).strip():
            texts.append(str(text).strip())
    return texts


def _is_command_event(event: Any) -> bool:
    """这条消息是不是一条插件指令（`/vw …`、`/help` …）。

    AstrBot 在"唤醒检查"那一步就把唤醒前缀从 ``message_str`` 里摘掉了
    （`/vw event h` 到了插件手里只剩 `vw event h`），所以光看开头是不是 `/` 认不出来。
    框架会把**命中的指令参数**塞进 ``handlers_parsed_params``，用它来认最准。
    """

    try:
        if event.get_extra("handlers_parsed_params"):
            return True
    except Exception:
        pass
    try:
        text = str(event.get_message_str() or "")
    except Exception:
        text = ""
    return _is_command(text)


_IMAGE_COMPONENT_NAMES = (
    "Image",
    "MarketFace",
    "Marketface",
    "Mface",
    "Sticker",
    "CustomFace",  # 协议端自定义表情：有的会带 url/file，能当图片读
)
"""会被当成"图"处理的组件：普通图片 + QQ 商城表情 / 表情包。

QQ 的表情包（大表情）在协议端不是 ``Image``：它常常只有一个 ``summary``（那句文案）、
``emoji_id`` 和一个拿不到的 ``key``，所以以前"这张图"就整个丢了——识图模型收到空地址，
只能回一句"未提供图片"。这里把它一起收进来，取不到图时至少把文案当文字用。
"""

_FACE_COMPONENT_NAMES = ("Face", "FaceEmoji", "MarketFace", "Marketface", "Mface")
"""系统小黄脸 / 表情包：没有图可读时，用它的名字或文案当文字描述。"""


def _component_named(component: Any, name: str) -> bool:
    return type(component).__name__ == name


def _is_image_component(component: Any) -> bool:
    return type(component).__name__ in _IMAGE_COMPONENT_NAMES


#: OneBot v11 里"表情包"的两种出现方式：`image` 段带 ``sub_type=1``，或者
#: 干脆是 ``mface`` / ``marketface`` / ``face`` 段（商城表情、小黄脸）。
#: 归一化里这两种都会变成 AstrBot 的 ``Image`` 组件，所以只能在原始段里认。
_STICKER_SEGMENTS = frozenset({"mface", "marketface", "face", "faceemoji"})
"""原始消息段里代表"表情"的类型名（小写比较）。"""


def _raw_segments(event: Any) -> list[dict[str, Any]]:
    """取这条消息的原始 OneBot 段（拿不到就给空表：任何平台都能退化成"认不出"）。"""

    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw is None:
        raw = getattr(event, "raw_message", None)
    for source in (raw, getattr(raw, "message", None), getattr(raw, "data", None)):
        if isinstance(source, dict):
            segments = source.get("message")
            if isinstance(segments, list):
                return [item for item in segments if isinstance(item, dict)]
        elif isinstance(source, list) and source and isinstance(source[0], dict):
            return [item for item in source if isinstance(item, dict)]
    return []


def _segment_is_sticker(segment: Mapping[str, Any]) -> bool:
    """这一段是不是表情包（`sub_type=1` / `mface` / 带「表情」文案的 summary）。"""

    kind = str(segment.get("type") or "").strip().lower()
    if kind in _STICKER_SEGMENTS:
        return True
    data = segment.get("data")
    if not isinstance(data, dict):
        return False
    sub_type = str(data.get("sub_type", "") or "").strip()
    if kind == "image" and sub_type == "1":
        return True
    summary = str(data.get("summary", "") or "")
    if kind == "image" and summary and "表情" in summary:
        return True
    return False


def _sticker_marks(event: Any, count: int) -> list[bool]:
    """按 ``_image_components`` 的顺序给出「这张是表情包」的标记。

    两边的顺序是一致的（归一化按原始段顺序产出组件）；对不上（数量不等、或被引用
    消息里的图掺进来）就整批当"认不出"——宁可漏标，也不能把普通照片标成表情包。
    """

    marks = [False for _ in range(max(0, int(count)))]
    if count <= 0:
        return marks
    positions = [
        index
        for index, segment in enumerate(_raw_segments(event))
        if str(segment.get("type") or "").strip().lower() in {"image", *_STICKER_SEGMENTS}
    ]
    if len(positions) != count:
        return marks
    for slot, position in enumerate(positions):
        segment = _raw_segments(event)[position]
        marks[slot] = _segment_is_sticker(segment)
    return marks


def _sticker_hint(marks: list[bool]) -> str:
    """给看图模型的一句话：哪几张是平台标记的表情包。"""

    picked = [str(index + 1) for index, flag in enumerate(marks) if flag]
    if not picked:
        return ""
    where = "、".join(picked)
    return (
        f"这批图里第 {where} 张是平台标记的**表情包**（QQ 的 mface / emoji，不是照片）："
        "类型按表情包写，别当实拍照片描述。"
    )


def _image_components(event: Any) -> list[Any]:
    """这条消息里的图片组件，以及**被引用消息**里的图片组件。

    引用（回复）消息的图片挂在 ``Reply.chain`` 上，只看当前消息会漏掉
    「引用了一张图再问她」这种最常见的用法。
    """

    message_obj = getattr(event, "message_obj", None)
    found: list[Any] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if _is_image_component(component):
            found.append(component)
            continue
        if _component_named(component, "Reply"):
            for inner in list(getattr(component, "chain", None) or []):
                if _is_image_component(inner):
                    found.append(inner)
    return found


def _image_ref_candidates(component: Any) -> list[str]:
    """组件身上所有可能是图片地址的字段（每样都留着，取不到就换下一个试）。

    QQ 那边同一条消息里出现的图，``url`` 往往带有时效签名（``rkey`` 之类），
    过几分钟就 403；而协议端同时给的 ``file`` / ``path`` 常是本地缓存副本。
    老写法只看第一个非空字段（``url`` 在最前），于是本地明明有图也跟着去下载
    一个过期地址。这里把所有字段都收下来，由调用方按"本地先、网上下载后"挑。
    """

    seen: list[str] = []
    for attr in ("file", "path", "url", "url_image", "image_url", "emoji_url"):
        value = str(getattr(component, attr, "") or "").strip()
        if not value:
            continue
        if attr in ("file", "path") and "://" not in value:
            try:
                value = Path(value).resolve().as_uri()
            except Exception:
                pass
        if value not in seen:
            seen.append(value)
    return seen


def _raw_image_ref(component: Any) -> str:
    """组件里最稳的现成地址：本地的真图片优先，其次 http(s)/base64。"""

    candidates = _image_ref_candidates(component)
    for ref in candidates:
        if _looks_like_image_file(ref):
            return ref
    for ref in candidates:
        if ref.startswith(("http://", "https://", "base64://", "data:")):
            return ref
    for ref in candidates:
        if _readable_local_file(ref):
            return ref
    return candidates[0] if candidates else ""


_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"\xff\xd8\xff",  # JPEG
    b"GIF87a",
    b"GIF89a",
    b"BM",  # BMP
    b"II*\x00",
    b"MM\x00*",  # TIFF
)


def _looks_like_image_file(ref: str) -> bool:
    """本地文件且头几个字节像图片（资产库里的文件名是内容 id，没有扩展名）。

    宿主判断"能不能当图片用"看的是扩展名；我们自己的资产库按内容 id 存盘，
    文件名天然没有 ``.png``。这里按内容嗅一遍，免得把刚存好的图当成"取不到"。
    """

    candidates: list[str] = []
    if ref.startswith("file://"):
        try:
            from urllib.parse import urlparse
            from urllib.request import url2pathname

            candidates.append(url2pathname(urlparse(ref).path))
        except Exception:
            candidates.append(ref[len("file://") :])
    elif "://" in ref:
        return False
    else:
        candidates.append(ref)
    for name in candidates:
        try:
            target = Path(name)
            if not target.is_file():
                continue
            with target.open("rb") as handle:
                head = handle.read(16)
        except OSError:
            continue
        if head.startswith(_IMAGE_MAGIC):
            return True
        # WEBP：RIFF....WEBP
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return True
    return False


def _local_path_of(ref: str) -> str:
    """``file:///C:/…`` → ``C:/…``（Windows 上直接切掉 ``file://`` 会剩一个前导斜杠）。

    2026-10-20 真机：SnowLuma/OneBot 的图都是 ``file:///C:/…/data/temp/*.png``，
    旧写法读成 ``/C:/…`` → `Path.is_file()` 恒为假 →「图片未能取得有效本地资产」。
    """

    text = str(ref or "")
    prefix = "file://"
    if not text.startswith(prefix):
        return text
    tail = text[len(prefix) :]
    try:
        from urllib.parse import unquote
        from urllib.request import url2pathname

        path = url2pathname(unquote(tail))
    except Exception:
        path = unquote(tail)
    if path[:1] in ("/", "\\") and len(path) > 2 and path[2:3] == ":":
        path = path[1:]
    return path


def _readable_local_file(ref: str, limit: int = 0) -> bool:
    """这个地址是不是「现在就读得到的本地文件」（``limit`` > 0 时还要求不太大）。"""

    name = _local_path_of(ref)
    if "://" in name:
        return False
    try:
        target = Path(name)
        if not target.is_file():
            return False
        if limit > 0 and target.stat().st_size > limit:
            return False
    except OSError:
        return False
    return True


def _local_file_problem(ref: str, limit: int = 0) -> str:
    """本地文件为什么读不成（用户 2026-10-23，问题3）：给可诊断的原因，不吞成一句。

    "文件不存在"和"文件太大"是完全不同的两件事：前者是临时文件被清理（主因），
    后者要调 ``media_max_file_bytes``——以前都报成"未能取得有效本地资产"。
    """

    name = _local_path_of(ref)
    if "://" in name:
        return "download_failed（没下载到本地）"
    try:
        target = Path(name)
        if not target.is_file():
            return "file_missing（本地临时文件不存在）"
        size = target.stat().st_size
    except OSError:
        return "file_missing（文件读不出来）"
    if limit > 0 and size > limit:
        return f"too_large（{size} 字节 > 上限 {limit} 字节）"
    return "file_missing（文件读不出来）"


def _is_usable_image_ref(ref: str) -> bool:
    """这个地址能不能直接交给 AstrBot 的模型调用（它自己会去下载/解码）。"""

    if not ref:
        return False
    try:
        from astrbot.core.utils.image_ref_utils import is_supported_image_ref
    except Exception:
        # 老宿主/测试桩里没有这个工具：网络地址与内联 base64 一律先信，
        # 本地文件仍然要按文件头认（"abc.image" 这种名字不能算图）。
        return ref.startswith(("http://", "https://", "base64://")) or (
            _looks_like_image_file(ref)
        )
    try:
        if is_supported_image_ref(ref):
            return True
    except Exception:
        return False
    return _looks_like_image_file(ref)


def _host_fetchable_ref(ref: str) -> bool:
    """这个地址宿主（AstrBot）自己还能去取吗——``http(s)`` 才算（用户 2026-10-23 续）。

    插件这边没取到本地字节时，远程地址仍然把原地址交出去让宿主再试一次（它自己会
    下载，今天就是这条链路）；本地地址 / 无 scheme 的地址没有字节就是真的没有，
    再交出去只会重现"图没到、注解却说送到了"。``base64://`` 是已经抓在手里的
    内容，不算"还要宿主去取"。
    """

    return str(ref or "").strip().lower().startswith(("http://", "https://"))


MAX_SENT_IMAGES = 9
"""一条消息里最多贴几张结果图（和引擎那边的上限对齐，防刷屏）。"""

IMG_CAPTION_WAIT_SECONDS = 10.0
"""老口径：一轮最多等转述多久（用户 2026-10-23 第二次调整：1.5 → 10 秒）。

**现在回复链不再用它**（见 :data:`IMG_CAPTION_GRACE_SECONDS`）：等 10 秒等于把
"回话速度"押在转述上，而转述是后台的活。留着这个常量是给"必须等完"的调用方
（后台协程传 ``None`` 表示一直等到看完）。
"""

IMG_CAPTION_GRACE_SECONDS = 1.5
"""**回复链**最多等转述多久（用户 2026-10-28：图还在转述就别卡着不说话）。

用户口径："还在转述就把描述暂时写成图片还在加载之类的，不要阻塞"。带图的那一轮
以前最多等 10 秒才去调模型，实测一条带图的群消息连转述带排队能拖出几分钟；
现在只给 1.5 秒（够命中缓存 / 够快的模型），没转述完就**先按"还在加载"注记一句**，
后台继续看完（描述落缓存，下一轮直接用）。纯文字轮次仍然传 0：一秒都不多等。
"""

IMAGE_REPLY_ROUNDS = 2
"""「字节已经不在」时，原图最多往回带几轮（用户 2026-10-23）。

以前是"聊天窗口里最近 ``image_max`` 张无条件重发"：今天这轮附过的图又被记回 pending，
下一轮再取回来再附一遍，永远退不出去（实测一张 33 小时前、临时文件早被删掉的图还在
被当成"最近 3 张"发给主模型）。

现在的第一判据是**原图字节还在不在**（本地临时文件 / 资产库副本，见
``_image_bytes_available``）：字节在、且这张图还在 :data:`IMAGE_ATTACH_ROUNDS` 轮以内，
就照原图附；字节已经不在时，只有"最近 2 轮内、而且还没转述过"的图才再试一次原图
（宿主自己会去下载远程地址），其余只给转述文字。
"""

IMAGE_ATTACH_ROUNDS = 3
"""原图作为附件往回带几轮（用户 m10816）。

2026-10-30 先按"只附带上来那一轮"改过一版，用户第二天改了口径：**最近几轮对话内
都该把原图发给她看**——刚到手看一眼，之后几轮还在聊这件事的时候，她得能回头再看
原图和细节；超过这个窗口就不再附，改用一次性转述的文字（``captions`` 逐条复用）。
2026-11-13 用户又收紧了：群友发的图只往回带 3 轮（6 轮时视觉 token 太贵）。

同一条口径还有后半句（m10817）：**一轮里新到的图全部附**，不占 ``image_max`` 的
额度——一次发十几张也要一次看完、不许漏看；从下一轮起才回到"最近 3 张"的上限。
"""

IMAGE_FRESH_MAX = 24
"""一轮里最多附几张新图（兜底，防止一次几百张把请求撑爆）。

用户 m10817 要的是"全部发送一次"，正常的连发（几张到十几张）都在这个数以内；
真超了就把多出来的那几张留到下一轮——它们还在窗口里，下一轮会按上限轮进附件。
"""

REPLY_ROUND_GAP_SECONDS = 120.0
"""她连发几条气泡算**同一轮**的间隔（秒）：超过这个间隔就算"她又回了一轮"。"""

IMG_CAPTION_FAILURE_TTL_SECONDS = 6 * 3600.0
"""转述失败后多久内不再重试、也不再重复注记（用户 2026-10-23）。

主因是临时文件已被清理 / 超过 10MB / 根本不是图片——这几件事在几小时内不会自己
变好。以前负缓存只有 60 秒，坏图每轮重试、每轮往用户文本里拼一句"这几张图没读出
内容"，听着像她一直没收到图。
"""


def _reply_round_starts(rows: list[dict[str, Any]]) -> list[float]:
    """她从最早到最近**回过的那几轮**的起点时间（秒）。

    "一轮" = 她连续发的几条气泡（相邻两条间隔 ≤ :data:`REPLY_ROUND_GAP_SECONDS`）；
    隔了一会儿再发就算新的一轮。只数 ``is_self`` 的行——对方说话不算她回过。
    """

    starts: list[float] = []
    last = 0.0
    for item in sorted(rows or [], key=lambda row: float(row.get("at") or 0.0)):
        if not item.get("is_self"):
            continue
        at = float(item.get("at") or 0.0)
        if not starts or at - last > REPLY_ROUND_GAP_SECONDS:
            starts.append(at)
        last = at
    return starts


def _rounds_since(at: float, starts: list[float]) -> int:
    """这张图之后她又回了几轮（``0`` = 她还没回过）。"""

    moment = float(at or 0.0)
    return len([start for start in (starts or []) if float(start) > moment + 1e-6])


def _round_recent_images(
    window: list[dict[str, Any]],
    all_urls: list[str],
    round_starts: list[float],
    rounds: int = IMAGE_REPLY_ROUNDS,
) -> list[str]:
    """``all_urls`` 里**最近 ``rounds`` 轮内**的那些图。

    留档里的图按它自己的到达时间算"几轮前"；不在留档里的（这一条消息刚带的、
    以及 pending 里还没入档的）一律算本轮——它们就是刚到的。

    用户 2026-10-23（问题1）：超过 2 轮的图一律只用转述文字，不再附原图。
    用户 m10816（2026-10-30）：往回带原图的窗口改成 :data:`IMAGE_ATTACH_ROUNDS`
    轮——同一个函数用 ``rounds`` 参数区分"字节还在"与"字节已经不在"两条判据。
    """

    at_of: dict[str, float] = {}
    for item in window or []:
        url = str(item.get("url") or "").strip()
        if url and url not in at_of:
            at_of[url] = float(item.get("at") or 0.0)
    fresh: list[str] = []
    for url in all_urls or []:
        text = str(url or "").strip()
        if not text:
            continue
        at = at_of.get(text)
        if at is None or _rounds_since(at, round_starts) < max(1, int(rounds)):
            fresh.append(text)
    return fresh


INVOCATION_GRACE_SECONDS = 900.0
"""借事件跑指令/工具时，处理器返回后还允许补发多久（秒）。

出图、合成语音这类插件常在处理器里返回、由后台任务稍后把结果发出来。以前
``close()`` 之后立刻抛「日程调用已结束」，于是"生成成功但发送失败"（2026-10-20
实机自拍事故）。宽限期内的补发直接走真事件发出去。
"""

AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100"
"""QQ 头像（用户 m13421）：通讯录里每个人配一张，取 100 像素那档。"""

AVATAR_TTL = 86400.0
"""头像缓存一天。换头像是很慢的事，没必要每次打开通讯录都去要。"""

AVATAR_MAX_BYTES = 512 * 1024
"""单张头像的上限；超过就当没取到（防止被塞个大文件进来）。"""


def _sendable_images(images: list[str] | None) -> list[str]:
    """挑出能真正发出去的图片地址：去重、限量、丢掉取不到的。"""

    picked: list[str] = []
    for item in list(images or []):
        ref = str(item or "").strip()
        if not ref or ref in picked:
            continue
        if not _is_usable_image_ref(ref):
            continue
        picked.append(ref)
        if len(picked) >= MAX_SENT_IMAGES:
            break
    return picked


def _result_message_ids(result: Any) -> list[str]:
    """从 AstrBot 的发送返回值里尽量抠出平台消息 id（有就记，没有不算失败）。

    返回值长什么样取决于适配器：可能是 True、一条消息 id、一个 id 列表，
    也可能是带 ``message_id`` 的对象。这里只做"能认就认"：认不出来返回空列表，
    **绝不**因为抠不到 id 就把这次发送判成失败（计划 §4.4）。
    """

    def _one(item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            for key in ("message_id", "id", "msg_id"):
                value = item.get(key)
                if value not in (None, ""):
                    return str(value).strip()
            return ""
        for attr in ("message_id", "id", "msg_id"):
            value = getattr(item, attr, None)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    found: list[str] = []
    items = result if isinstance(result, (list, tuple, set)) else [result]
    for item in items:
        mid = _one(item)
        if mid and mid not in found:
            found.append(mid)
    return found


async def _host_image_base64(ref: str) -> str:
    """请 AstrBot 自己的取图器把它变成 base64（会走它配置的下载与代理）。

    这是「多试一种办法」里的第二种：``urllib`` 直连失败（403 / 证书 / 代理 /
    IPv6）时，宿主那条路往往还是通的；它同时认 http、file、base64 三种写法。
    """

    if not ref:
        return ""
    try:
        from astrbot.core.utils.media_utils import MediaResolver
    except Exception:
        return ""
    try:
        data = await MediaResolver(ref, media_type="image").to_base64()
    except Exception:
        return ""
    text = str(data or "").strip()
    return f"base64://{text}" if text else ""


async def _resolve_image_ref(component: Any) -> str:
    """把一个图片组件换成「模型能吃到」的地址（依次试几种取法）。

    顺序：本地真图片 → 现成的 http/base64 → 组件的 ``convert_to_base64()``
    → 宿主的 ``MediaResolver``。越往后越慢，但越往后越不容易因为地址过期、
    协议端改名、代理设置而失败；全都失败就把原地址交回去，让注解去说明。
    """

    ref = _raw_image_ref(component)
    if _looks_like_image_file(ref) or _is_usable_image_ref(ref):
        return ref
    converter = getattr(component, "convert_to_base64", None)
    if callable(converter):
        try:
            data = await converter()
        except Exception:
            data = ""
        if data:
            return f"base64://{data}"
    for candidate in _image_ref_candidates(component):
        # 原始地址也要过一遍宿主的取图器：它认 http / file:// / base64，
        # 还走宿主自己配的代理与超时（协议端只给文件名时这是最后的办法）。
        converted = await _host_image_base64(candidate)
        if converted:
            return converted
    return ref


IMAGE_INBOX_DIR = "image_inbox"
"""插件自己的图片暂存目录（相对插件数据目录）。

协议端给的图是**临时文件**（``data/temp/media_image_*.png``）：等识图/转述真正跑起来时，
它可能已经被宿主清理掉了，日志里就是「图1 file_missing（本地临时文件不存在）」（用户 m07965）。
所以在收到消息的那一刻就把字节复制进这里，之后一直读这一份。
"""

_INBOX_KEEP_SECONDS = 7 * 24 * 3600
_INBOX_MAX_FILES = 400


def _keep_image_file(data_dir: str, ref: str, *, limit: int = 0, now: float = 0.0) -> str:
    """把本地临时图复制成插件目录里的一份副本，返回可长期读取的地址。

    不是本地文件（``http``/``base64``）、读不到、或超过上限时原样返回 ``ref``：
    这些情况本来就有别的路径处理，不该在这里把地址弄丢。
    """

    name = _local_path_of(str(ref or ""))
    if not name or "://" in name:
        return ref
    try:
        source = Path(name)
        if not source.is_file():
            return ref
        size = source.stat().st_size
        if size <= 0 or (limit > 0 and size > limit):
            return ref
        payload = source.read_bytes()
    except OSError:
        return ref
    digest = hashlib.sha256(payload).hexdigest()
    suffix = source.suffix.lower()
    if not suffix or len(suffix) > 6:
        suffix = ".png"
    root = Path(data_dir) / IMAGE_INBOX_DIR
    try:
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{digest}{suffix}"
        if not target.exists():
            tmp = root / f".{digest}.tmp"
            tmp.write_bytes(payload)
            os.replace(tmp, target)
    except OSError:
        return ref
    _trim_inbox(root, now=now)
    return str(target)


def _trim_inbox(root: Path, *, now: float = 0.0) -> None:
    """顺手清理：太旧的删掉，最多留 ``_INBOX_MAX_FILES`` 份。失败不影响取图。"""

    try:
        stamp = float(now or time.time())
        rows: list[tuple[float, Path]] = []
        for path in root.glob("*"):
            if path.name.startswith(".") or not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if _INBOX_KEEP_SECONDS > 0 and stamp - mtime > _INBOX_KEEP_SECONDS:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            rows.append((mtime, path))
        if len(rows) <= _INBOX_MAX_FILES:
            return
        rows.sort()
        for _mtime, path in rows[: len(rows) - _INBOX_MAX_FILES]:
            try:
                path.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - 清理是顺手的事
        return


async def _image_sources(event: Any) -> list[str]:
    """挑出这条消息里的图片，返回可以喂给多模态模型的地址。"""

    sources: list[str] = []
    for component in _image_components(event):
        ref = await _resolve_image_ref(component)
        # 只交"真的能取到"的地址：拿不到时留给 _face_note / 注解去说清，
        # 不然识图模型会收到一个取不到的名字，回一句"未提供图片"（等于白花一次调用）
        if ref and _is_usable_image_ref(ref) and ref not in sources:
            sources.append(ref)
    return sources


def _sticker_note(event: Any) -> str:
    """QQ 表情 / 表情包：没有图可读时，用它的名字或文案写成一句人话。

    ``Face`` 是系统小黄脸（只有 id / 名字），``MarketFace`` 是商城表情包
    （通常带一句 ``summary``，例如「笑死」）。这些以前直接丢失，她只看到空气。
    """

    message_obj = getattr(event, "message_obj", None)
    parts: list[str] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if not _component_named_any(component, _FACE_COMPONENT_NAMES):
            continue
        summary = " ".join(
            str(
                getattr(component, "summary", "")
                or getattr(component, "name", "")
                or getattr(component, "text", "")
                or ""
            ).split()
        )
        face_id = str(
            getattr(component, "face_id", "")
            or getattr(component, "id", "")
            or getattr(component, "emoji_id", "")
            or ""
        ).strip()
        kind = "表情包" if _component_named(component, "MarketFace") else "表情"
        label = summary or (f"{kind} {face_id}" if face_id else kind)
        parts.append(f"对方发了个{kind}：{label}" if summary else f"对方发了个{kind}（{label}）")
    if not parts:
        return ""
    return "；".join(dict.fromkeys(parts))


_ATTACHMENT_TEXTS = frozenset({"<attachment>", "[attachment]", "[附件]", "[文件]"})
"""协议端把「只发了一个文件」写成这些占位符（用户 m27012 的真机日志就是 ``<attachment>``）。"""

_ATTACHMENT_COMPONENTS = ("File", "Record", "Video", "Attachment")
"""这些组件里的内容插件读不到——图片是 ``Image``，走的是另一条识图的路。"""


def _attachment_note(event: Any, text: str) -> str:
    """只有附件、没有正文的一条消息：告诉她"这是文件，不是空消息"。

    她看到 ``<attachment>`` 这种占位符只会反问"你刚刚发了什么过来呀 我这边没显示出来"
    （用户 m27012 的真机日志就是这样）。留一句人话，她起码能说清"这个我打不开"。
    """

    body = " ".join(str(text or "").split())
    if body and body.lower() not in _ATTACHMENT_TEXTS:
        return ""
    if _image_components(event):
        return ""
    message_obj = getattr(event, "message_obj", None)
    kinds: list[str] = []
    for component in list(getattr(message_obj, "message", []) or []):
        name = type(component).__name__
        if any(word in name for word in _ATTACHMENT_COMPONENTS):
            kinds.append(name)
    if not kinds and not body:
        return ""
    kind = "语音" if any("Record" in name for name in kinds) else "文件"
    if any("Video" in name for name in kinds):
        kind = "视频"
    return (
        f"对方发的是一条附件（{kind}），插件这边拿不到里面的内容；"
        f"可以直接说没打开、请他发图片或把内容写成文字"
    )


def _component_named_any(component: Any, names: tuple[str, ...]) -> bool:
    return type(component).__name__ in names


def _provider_supports_images(provider: Any) -> bool | None:
    """这个 Provider 勾选的模态里有没有「图像」。

    AstrBot 把勾选结果记在 ``provider_config['modalities']``：空列表是"没配"
    （按老版本行为当作支持），没有这一项就读不到——那就交给配置里的开关决定。
    """

    config = getattr(provider, "provider_config", None)
    if not isinstance(config, dict):
        return None
    modalities = config.get("modalities", None)
    if modalities == []:
        return True
    if isinstance(modalities, list):
        return "image" in modalities
    return None


def _image_components_debug(event: Any) -> str:
    """取不到图片地址时留下的线索：协议端到底给了什么字段。"""

    parts: list[str] = []
    for component in _image_components(event):
        fields = {
            key: str(getattr(component, key, "") or "")
            for key in ("file", "url", "path", "_type")
        }
        parts.append(" ".join(f"{key}={value}" for key, value in fields.items() if value))
    return " / ".join(part for part in parts if part)


def _is_forwarded(event: Any) -> bool:
    """这条消息是不是「合并转发」。"""

    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ in ("Forward", "Node", "Nodes"):
            return True
        if type(component).__name__ == "Json" and _multimsg_text(component):
            return True
    text = str(getattr(message_obj, "message_str", "") or "")
    return "[合并转发]" in text or "[聊天记录]" in text


# ---------------- 合并转发：读内容 → 交给多模态模型压成摘要 ----------------

MAX_FORWARD_NODES = 60
"""一条转发最多读多少条消息（再多也没人在意，摘要也放不下）。"""

MAX_FORWARD_IMAGES = 6
"""转发里的图最多送几张给多模态模型。"""

MAX_FORWARD_TEXT_CHARS = 1500
"""送进摘要提示词的原文上限（防一条转发把上下文撑爆）。"""

_FORWARD_PLACEHOLDER_RE = re.compile(
    r"[\[【(（]\s*(?:合并转发|转发消息|转发|聊天记录|message\s*record|forward(?:\s*message)?)\s*[\]】)）]",
    re.IGNORECASE,
)
"""渲染出来的转发占位符：摘要替换正文时要把这些清掉。"""


def _multimsg_text(component: Any) -> str:
    """QQ 的「合并转发」有时是一个 multimsg JSON：把里面的条目读成文本。"""

    data = getattr(component, "data", None)
    if isinstance(data, str):
        try:
            data = json.loads(data.replace("&#44;", ","))
        except Exception:
            return ""
    return _multimsg_text_from(data)


def _multimsg_text_from(data: Any) -> str:
    """multimsg 的 JSON 负载（dict）→ 里面的条目文本。"""

    if not isinstance(data, dict):
        return ""
    if str(data.get("app") or "") != "com.tencent.multimsg":
        return ""
    meta = data.get("meta")
    detail = meta.get("detail") if isinstance(meta, dict) else None
    news = detail.get("news") if isinstance(detail, dict) else None
    if not isinstance(news, list):
        return ""
    lines: list[str] = []
    for item in news:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").replace("[图片]", "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def _strip_forward_placeholders(text: str) -> str:
    """去掉正文里的「[转发消息]」这类占位符，剩下的才是对方自己写的话。

    用户 m00001 的 M4：以前占位符一律换成**半角空格**，于是 ``看看[转发消息]吧``
    被改成 ``看看 吧``——中文句子中间凭空多一个空格。现在按接缝两侧的类型决定：
    任一侧是中文就直粘（:func:`join_wrapped`，和折行修复同一套规则），两侧都是
    ASCII 才补一个空格；占位符在开头 / 结尾时不补任何东西。
    """

    def _drop(match: "re.Match[str]") -> str:
        whole = match.string
        start, end = match.span()
        prev_char = whole[start - 1] if start > 0 else ""
        next_char = whole[end] if end < len(whole) else ""
        if not prev_char or not next_char:
            # 占位符顶在开头 / 结尾：直接删掉，别留前导或尾随空格
            return ""
        return join_wrapped(prev_char, next_char)

    cleaned = _FORWARD_PLACEHOLDER_RE.sub(_drop, str(text or ""))
    return " ".join(cleaned.split()).strip()


def _component_to_segment(component: Any) -> dict[str, Any] | None:
    """AstrBot 的消息组件 → OneBot 那样的 ``{"type":…, "data":…}``。

    转发内容的两种来源（协议端拉回来的 JSON、AstrBot 已经解析好的组件）
    归一成同一种写法，后面只需要一套走法。
    """

    if isinstance(component, dict):
        return component
    name = type(component).__name__
    if name == "Plain":
        return {"type": "text", "data": {"text": str(getattr(component, "text", "") or "")}}
    if name == "Image":
        ref = _raw_image_ref(component)
        return {"type": "image", "data": {"file": ref, "url": ref}}
    if name == "At":
        return {
            "type": "at",
            "data": {
                "name": str(getattr(component, "name", "") or ""),
                "qq": str(getattr(component, "qq", "") or ""),
            },
        }
    if name == "Face":
        return {"type": "face", "data": {}}
    if name == "File":
        return {"type": "file", "data": {"name": str(getattr(component, "name", "") or "")}}
    if name == "Json":
        return {"type": "json", "data": {"data": getattr(component, "data", "")}}
    if name:
        return {"type": name.lower(), "data": {}}
    return None


def _as_node(item: Any) -> dict[str, Any]:
    """AstrBot 的 Node 组件 / OneBot 的节点字典 → 统一的节点字典。"""

    if isinstance(item, dict):
        return item
    name = type(item).__name__
    if name == "Node":
        sender = str(getattr(item, "name", "") or getattr(item, "uin", "") or "").strip()
        content = list(getattr(item, "content", None) or [])
        return {
            "sender": {"nickname": sender},
            "content": [
                segment for segment in (_component_to_segment(c) for c in content)
                if segment is not None
            ],
        }
    return {}


def _node_list(value: Any) -> list[dict[str, Any]]:
    """把各种形态的转发内容归一成节点列表。"""

    if isinstance(value, dict):
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        nodes = (
            data.get("messages")
            or data.get("message")
            or data.get("nodes")
            or data.get("nodeList")
        )
        if isinstance(nodes, list):
            return [_as_node(item) for item in nodes if _as_node(item)]
        return [_as_node(value)] if _as_node(value) else []
    if isinstance(value, (list, tuple)):
        return [_as_node(item) for item in value if _as_node(item)]
    return []


def _node_segments(node: dict[str, Any]) -> list[dict[str, Any]]:
    """节点的内容（可能是段列表，也可能是一段 JSON 文本）。"""

    raw = node.get("message") or node.get("content") or []
    if isinstance(raw, str):
        body = raw.strip()
        if not body:
            return []
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            raw = parsed
        else:
            raw = [{"type": "text", "data": {"text": body}}]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _collect_forward(
    nodes: list[dict[str, Any]],
    *,
    out_text: list[str],
    out_images: list[str],
    out_ids: list[str],
    depth: int = 0,
) -> None:
    """把节点列表摊成「谁说了什么」+ 图片地址（嵌套转发只记 id，等着再去拉）。"""

    if depth > 3:
        return
    for node in nodes:
        if len(out_text) >= MAX_FORWARD_NODES:
            return
        sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
        who = str(
            sender.get("nickname")
            or sender.get("card")
            or sender.get("user_id")
            or node.get("name")
            or node.get("uin")
            or ""
        ).strip()
        parts: list[str] = []
        for segment in _node_segments(node):
            kind = str(segment.get("type") or "").lower()
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if kind in ("text", "plain"):
                parts.append(str(data.get("text") or ""))
            elif kind == "image":
                ref = str(data.get("url") or data.get("file") or "").strip()
                if ref:
                    out_images.append(ref)
                    parts.append(f"［图片{len(out_images)}］")
                else:
                    parts.append("［图片］")
            elif kind == "at":
                parts.append(f"@{str(data.get('name') or data.get('qq') or '').strip()}")
            elif kind == "file":
                parts.append(f"［文件:{str(data.get('name') or data.get('file') or '').strip()}］")
            elif kind == "face":
                parts.append("［表情］")
            elif kind == "video":
                parts.append("［视频］")
            elif kind == "record":
                parts.append("［语音］")
            elif kind in ("forward", "forward_msg"):
                fid = data.get("id") or data.get("message_id")
                if fid:
                    out_ids.append(str(fid))
                    parts.append("［里面还有一条转发］")
                else:
                    _collect_forward(
                        _node_list(data.get("content")),
                        out_text=out_text,
                        out_images=out_images,
                        out_ids=out_ids,
                        depth=depth + 1,
                    )
            elif kind == "json":
                nested = _multimsg_text_from(data.get("data"))
                if nested:
                    parts.append(nested)
            elif kind == "nodes":
                _collect_forward(
                    _node_list(data.get("content")),
                    out_text=out_text,
                    out_images=out_images,
                    out_ids=out_ids,
                    depth=depth + 1,
                )
        body = "".join(parts).strip()
        if body:
            out_text.append(f"{who}：{body}" if who else body)


async def _fetch_forward(event: Any, forward_id: str) -> dict[str, Any] | None:
    """去协议端把一条转发拉回来（拿不到就返回 None，调用方保持原样）。"""

    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    call_action = getattr(api, "call_action", None)
    if not callable(call_action):
        return None
    candidates: list[dict[str, Any]] = [{"message_id": forward_id}, {"id": forward_id}]
    if str(forward_id).isdigit():
        candidates.extend([{"message_id": int(forward_id)}, {"id": int(forward_id)}])
    for params in candidates:
        try:
            result = await call_action("get_forward_msg", **params)
        except Exception:
            continue
        if isinstance(result, dict):
            return result
    return None


async def _forward_digest(event: Any) -> tuple[str, list[str], list[str]]:
    """这条消息里的合并转发 → (原文摘要, 图片地址, 转发 id)。

    三种形态都认：自带内容的 Node/Nodes、只给了 id 的 Forward（去协议端拉一次）、
    QQ 的 multimsg JSON。读不出来就返回空串，调用方保持原来的那句提示。
    """

    forward_ids: list[str] = []
    nodes: list[dict[str, Any]] = []
    multimsg: list[str] = []
    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        name = type(component).__name__
        if name == "Forward":
            fid = str(getattr(component, "id", "") or "").strip()
            if fid:
                forward_ids.append(fid)
        elif name == "Node":
            nodes.extend(_node_list(component))
        elif name == "Nodes":
            nodes.extend(_node_list(list(getattr(component, "nodes", None) or [])))
        elif name == "Json":
            text = _multimsg_text(component)
            if text:
                multimsg.append(text)
    for fid in list(forward_ids):
        payload = await _fetch_forward(event, fid)
        if payload is None:
            continue
        nodes.extend(_node_list(payload))
    out_text: list[str] = []
    out_images: list[str] = []
    nested_ids: list[str] = []
    _collect_forward(nodes, out_text=out_text, out_images=out_images, out_ids=nested_ids)
    for fid in nested_ids:
        payload = await _fetch_forward(event, fid)
        if payload is None:
            continue
        _collect_forward(
            _node_list(payload), out_text=out_text, out_images=out_images, out_ids=[]
        )
    if not out_text and not out_images and multimsg:
        out_text = list(multimsg)
    digest = "\n".join(out_text).strip()[:MAX_FORWARD_TEXT_CHARS]
    return digest, out_images, forward_ids


def _forward_fingerprint(forward_ids: list[str], digest: str, images: list[str]) -> str:
    """同一条转发的指纹：协议端给了 id 就用 id，否则按内容算。"""

    ids = sorted({str(item).strip() for item in forward_ids if str(item).strip()})
    key = "|".join(ids) if ids else f"{digest}#{'|'.join(images)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _clean_forward_summary(text: str, limit: int) -> str:
    """转发摘要的收尾：压成一行、去掉模型爱加的「摘要：」开头、按配置截断。"""

    body = " ".join(str(text or "").split()).strip()
    if not body:
        return ""
    for prefix in ("摘要：", "摘要:", "内容摘要：", "内容摘要:", "总结：", "总结:"):
        if body.startswith(prefix):
            body = body[len(prefix) :].strip()
            break
    body = body.strip("「」“”\"'` ")
    if not body:
        return ""
    if len(body) > limit:
        body = body[: max(1, limit - 1)].rstrip() + "…"
    return body


def _with_forward_summary(text: str, summary: str) -> str:
    """把转发摘要拼进正文：清掉占位符，摘要跟在对方自己写的话后面。"""

    base = _strip_forward_placeholders(text)
    body = f"{FORWARD_SUMMARY_MARK}{summary}"
    return f"{base}\n{body}" if base else body


def _quoted_parts(event: Any) -> tuple[str, str]:
    """这条消息引用了谁说的什么：返回 ``(谁, 内容)``；没有引用就返回两个空串。

    两件容易漏的事：

    - 引用的那条可能**只有图片**（她刚发的照片），这时 ``message_str`` 是空的，
      只照着文本读会得到空串——她会完全不知道对方在指什么；
    - 引用的那条可能就是**她自己**发的：昵称她并不认识，要明确写成「你自己」。
    """

    message_obj = getattr(event, "message_obj", None)
    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "Reply":
            continue
        text = " ".join(str(getattr(component, "message_str", "") or "").split())
        if not text:
            text = _chain_plain_text(component)
        if not text:
            continue
        sender_id = str(getattr(component, "sender_id", "") or "").strip()
        who = str(getattr(component, "sender_nickname", "") or "").strip()
        if self_id and sender_id and sender_id == self_id:
            who = "你自己"
        return who, text
    return "", ""


def _quoted_text(event: Any) -> str:
    """引用消息压成一行（``小明：晚上吃鱼``）：给看图那一步当背景用。"""

    who, text = _quoted_parts(event)
    if not text:
        return ""
    return f"{who}：{text}" if who else text


def _chain_plain_text(component: Any) -> str:
    """把被引用消息的消息链压成一行（图片写［图片］，表情写［表情］）。

    用户 m00001 的 M4：以前每段之间一律补半角空格，两段中文（``在海`` + ``滨步道``）
    拼出来就是 ``在海 滨步道``。现在接缝交给 :func:`join_wrapped`：任一侧是中文就
    直粘，两侧都是 ASCII 才补一个空格（英文 / 数字的分隔照旧）。

    但 ``@某人`` 是这条中文规则唯一的例外：``@Tom`` + ``你好`` 曾按"中文直粘"
    拼成 ``@Tom你好``，把 @ 贴进了正文。现在**只看前一段是不是以 ``@`` 开头**，
    是就一律补一个半角空格（与后一段是什么语言无关），且后一段先 ``lstrip()``
    再接，避免本来就带前导空白时冒出双空格。
    """

    parts: list[str] = []
    for inner in list(getattr(component, "chain", None) or []):
        if _component_named(inner, "Plain"):
            text = str(getattr(inner, "text", "") or "").strip()
            if text:
                parts.append(text)
        elif _component_named(inner, "Image"):
            parts.append("［图片］")
        elif _component_named(inner, "Face"):
            parts.append("［表情］")
        elif _component_named(inner, "At"):
            name = str(getattr(inner, "name", "") or getattr(inner, "qq", "") or "").strip()
            if name:
                parts.append(f"@{name}")
    merged = ""
    prev_at = False
    """上一段是不是 ``@某人``：是则接缝强制一个半角空格（与下一段的语言无关）。"""
    for part in (" ".join(part.split()) for part in parts if part):
        if not merged:
            merged = part
        elif prev_at:
            merged = merged + " " + part.lstrip()
        else:
            merged = merged + join_wrapped(merged[-1], part[0]) + part
        prev_at = part.startswith("@")
    return merged


def _finish_reason_of(response: Any) -> str:
    """从 AstrBot 的 ``LLMResponse`` 里尽量抠出 ``finish_reason``（用户 m00001）。

    AstrBot 只把原始响应塞在 ``raw_completion`` 里（OpenAI 兼容是 ``choices[0]``，
    其它 Provider 结构各不相同），所以这里一律**防御式**取：拿不到就返回空串，
    由调用方按 ``null`` 记日志——**绝不**编一个 ``stop`` 出来假装正常收尾。
    """

    if response is None:
        return ""
    seen: list[Any] = [getattr(response, "raw_completion", None), response]
    for item in seen:
        if item is None:
            continue
        reason = getattr(item, "finish_reason", None)
        if isinstance(item, dict):
            reason = item.get("finish_reason", reason)
        if isinstance(reason, str) and reason.strip():
            return reason.strip()[:40]
        choices = getattr(item, "choices", None)
        if choices is None and isinstance(item, dict):
            choices = item.get("choices")
        try:
            first = choices[0] if choices else None
        except Exception:
            first = None
        if first is None:
            continue
        reason = getattr(first, "finish_reason", None)
        if isinstance(first, dict):
            reason = first.get("finish_reason", reason)
        if isinstance(reason, str) and reason.strip():
            return reason.strip()[:40]
    return ""


class AstrBotLLM:
    """LLMPort 实现。"""

    def __init__(
        self,
        plugin: "VirtualWorldPlugin",
        provider_id: str = "",
        role: str = "main",
        sampling: dict[str, float] | None = None,
    ) -> None:
        self.plugin = plugin
        self.provider_id = provider_id
        self.role = role
        """``main`` = 说话用的主模型（它的原始响应要留给别的插件看），其余是辅助模型。"""
        self.sampling = dict(sampling or {})
        """采样参数（温度 / top_p / 重复惩罚）。空 = 不覆盖，用 Provider 自己的设置。

        这些参数在最外层适配器上统一加：多轮聊天的复读和"每轮都一个腔调"，
        部分原因在采样参数，而插件自己的每次调用都不该各写一遍。
        """
        self._sampling_rejected = False
        """Provider 不认这些参数时，退回只传温度，别让主回复跟着挂掉。"""

    async def generate(
        self,
        *,
        session_id: str,
        system_prompt: str,
        prompt: str,
        contexts: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        image_urls: list[str] | None = None,
    ) -> LLMReply:
        context = self.plugin.context
        provider_id = (self.provider_id or self.plugin.llm_provider_id or "").strip()
        if not provider_id:
            try:
                provider_id = await context.get_current_chat_provider_id(session_id)
            except Exception as exc:
                return LLMReply(ok=False, error=f"没有可用的 LLM Provider: {exc}")
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        # 采样参数由适配器统一加（配置关掉时 self.sampling 是空的，等于不覆盖）
        if self.sampling and not self._sampling_rejected:
            for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
                if key in self.sampling and (key != "temperature" or temperature is None):
                    kwargs[key] = self.sampling[key]
        images = [str(item) for item in (image_urls or []) if str(item).strip()]
        token = self.plugin.set_self_initiated()
        try:
            response = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                contexts=list(contexts) if contexts else None,
                image_urls=images or None,
                **kwargs,
            )
        except Exception as exc:
            # 上游按内容安全拒了这次生成：同一段内容再要一次还是会被拒，
            # 别再把它当成"Provider 不认采样参数"反复重试——那会把真正的原因埋掉，
            # 而且白白多打几次请求（露骨内容这条路上这条日志误导过一次排查）。
            blocked = any(
                marker in str(exc)
                for marker in ("content_filter", "PROHIBITED_CONTENT", "内容安全", "content policy")
            )
            # Provider 不认采样参数：退回只传温度，别让主回复跟着挂掉
            if self.sampling and not self._sampling_rejected and len(kwargs) > 1 and not blocked:
                self._sampling_rejected = True
                self.plugin.logger.warning(
                    f"[virtual_world] Provider 不接受采样参数（{exc}），"
                    "已退回只用温度；要调就去 Panel 的 Provider 里调"
                )
                kwargs = {key: value for key, value in kwargs.items() if key == "temperature"}
                try:
                    response = await context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        contexts=list(contexts) if contexts else None,
                        image_urls=images or None,
                        **kwargs,
                    )
                    if self.role == "main":
                        self.plugin.remember_llm_response(session_id, response)
                    text = getattr(response, "completion_text", "") or ""
                    return LLMReply(
                        text=text, ok=True, finish_reason=_finish_reason_of(response)
                    )
                except Exception as retry_exc:
                    return LLMReply(ok=False, error=f"LLM 调用失败: {retry_exc}")
            # Provider 不支持图片（或图片取不回来）时，退回纯文字再来一次
            if images:
                try:
                    response = await context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        contexts=list(contexts) if contexts else None,
                        **kwargs,
                    )
                    self.plugin.logger.warning(
                        f"[virtual_world] 带图片调用失败（{exc}），已退回纯文字"
                    )
                    if self.role == "main":
                        self.plugin.remember_llm_response(session_id, response)
                    text = getattr(response, "completion_text", "") or ""
                    return LLMReply(
                        text=text, ok=True, finish_reason=_finish_reason_of(response)
                    )
                except Exception as retry_exc:
                    return LLMReply(ok=False, error=str(retry_exc))
            if blocked:
                self.plugin.logger.warning(
                    f"[virtual_world] 上游按内容安全拒了这次生成（{str(exc)[:160]}）："
                    "换一段说法、或换一个 Provider（Panel → Provider）"
                )
                return LLMReply(ok=False, error=str(exc))
            # 历史上下文格式不被某些 Provider 接受时，退化成不带历史再试一次
            if contexts:
                try:
                    response = await context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        **kwargs,
                    )
                except Exception as retry_exc:
                    return LLMReply(ok=False, error=str(retry_exc))
            else:
                return LLMReply(ok=False, error=str(exc))
        finally:
            self.plugin.reset_self_initiated(token)
        if self.role == "main":
            # 主模型的原始响应留给「回复钩子」用：别的插件可能要看 usage / id 这些字段，
            # 自己拼一个假的响应它们会读不到而报错。
            self.plugin.remember_llm_response(session_id, response)
        text = getattr(response, "completion_text", "") or ""
        return LLMReply(text=text, ok=True, finish_reason=_finish_reason_of(response))


CAPTION_SYSTEM_PROMPT = DEFAULT_CAPTION_PROMPT
"""看图提示词的内置默认值；「全局设置 → 图片转述」里可以改成自己的一套。"""

# 多图合并成一次调用时，要模型按「图1：…」逐行输出；解析不出来就退回逐张。
_MULTI_IMAGE_RULE = (
    "\n\n这次一次给你 {count} 张图：每张图各一行，"
    "以「图1：」「图2：」这样开头，顺序和图片顺序一致，不要合并成一行。"
)


def _split_multi_caption(text: str, count: int) -> list[str]:
    """把「图1：… 图2：…」拆成逐图结果；对不上就返回空列表（调用方退回逐张）。"""

    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < count:
        return []
    if len(lines) != count:
        return []
    picked: list[str] = []
    for index, line in enumerate(lines, start=1):
        prefixes = (f"图{index}：", f"图{index}:", f"{index}：", f"{index}.", f"{index})", f"{index}）")
        prefix = next((prefix for prefix in prefixes if line.startswith(prefix)), None)
        if prefix is None:
            return []
        line = line[len(prefix) :].strip()
        picked.append(line)
    return picked if all(picked) else []

# 老版本转述里拼过「与话题的关系：…」这半句（2026-10-21 已删掉这个功能）：
# 读老数据时把它剔掉，只留画面/类型/文字。
_CAPTION_RELATION_MARKERS = ("与话题的关系", "和话题的关系", "与当前话题", "关系：")


def _caption_prefix(caption: str) -> str:
    """从一整条转述里切出"与话题无关"的那半句。切不出来就整条返回。"""

    text = " ".join(str(caption or "").split())
    if not text:
        return ""
    cut = len(text)
    for marker in _CAPTION_RELATION_MARKERS:
        index = text.find(marker)
        if index > 0:
            cut = min(cut, index)
    prefix = text[:cut].strip(" ｜|，,。;；")
    return prefix or text


def _images_head(count: int) -> str:
    """这条消息带了几张图（收图那一步写进留档的中性说法）。

    交付决策在准备回复时才做（`_reply_images`），所以这里**不写编号、也不承诺**
    "会一起发给你"——那一步会再给一句带编号的说明。
    """

    total = max(0, int(count or 0))
    if total <= 0:
        return ""
    return f"这条消息带了 {total} 张图片"


def _scope_kind_for_session(session_id: str) -> str:
    """会话 → 收藏范围口径（``meme_sources.allowed_scopes`` 里那套 kind）。

    历史上平台字段就是 ``GroupMessage`` / ``FriendMessage``（见 ``consolidate.py``），
    插件里判群聊一直是 ``":GroupMessage:" in session_id``（``main.py`` 与
    ``engine.py`` 的既有写法），这里沿用同一口径。
    """

    return "group" if ":GroupMessage:" in str(session_id or "") else "private"


def _meme_status_text(status: dict[str, Any] | None) -> str:
    """一张图的收藏状态 → 提示词里那句括号标注（方案 §5）。

    **编号只能由代码给**：这里用的是 ``MemeStore.image_status`` 查出来的
    ``entry_id``，模型不许自己编（"已收藏，编号 meme_000028"）。四种情况分别说清：

    * 没收藏过 → ``未收藏``；
    * 收藏过、现在可用 → ``已收藏，编号 meme_000028，名称：猫猫探头``；
    * 收藏过、现在不可用（停用 / 记录删了 / 文件缺失 / 范围不可用）→ 如实说"用不了"，
      不能糊成"已收藏"（那样她会照着用一条停掉的收藏）；
    * 空状态 → ``未收藏``。
    """

    data = dict(status or {})
    if not data.get("collected"):
        return "未收藏"
    entry_id = str(data.get("entry_id") or "").strip()
    name = str(data.get("name") or "").strip()
    if data.get("usable"):
        text = "已收藏"
        if entry_id:
            text += f"，编号 {entry_id}"
        if name:
            text += f"，名称：{name}"
        return text
    label = str(data.get("label") or "").strip() or "现在用不了"
    head = f"收藏过（编号 {entry_id}）" if entry_id else "收藏过"
    return f"{head}，但现在不可用：{label}"


def _strip_image_index(text: str) -> str:
    """剥掉模型自己加的「图1：」「图 1：」「1.」这类编号前缀。"""

    body = str(text or "").strip()
    for _ in range(2):
        match = re.match(
            r"^(?:图\s*[0-9一二三四五六七八九十]+|[0-9]+|[一二三四五六七八九十]+)\s*[：:.、)）]\s*",
            body,
        )
        if not match:
            break
        body = body[match.end() :].strip()
    return body


def _image_fingerprint(source: str) -> str:
    """一张图的稳定指纹：同一个文件换链接也能认出来。

    - ``base64://`` 直接对内容算 sha1（协议端把图转成 base64 时最可靠）；
    - http(s) 去掉 query（临时签名/token 每次都变）、只留路径；
    - 其它（本地文件、协议端给的 md5 名）原样取用。
    """

    raw = str(source or "").strip()
    if not raw:
        return ""
    digest = ""
    if raw.startswith("base64://"):
        payload = raw[len("base64://") :]
        digest = hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()
    elif raw.startswith("data:"):
        digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()
    elif raw.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(raw)
        digest = hashlib.sha1(
            f"{parsed.netloc}{parsed.path}".encode("utf-8", "ignore")
        ).hexdigest()
    else:
        digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()
    return f"img:{digest}"


_CAPTION_SCHEMA_VERSION = "verified-caption-v2"
_CAPTION_OUTPUT_RULE = (
    '\n返回 JSON 对象：{"status":"ok|unreadable|no_image","description":"画面描述"}。'
    '只有实际看到了画面才用 ok；没有图片用 no_image；无法读取用 unreadable。'
)
# M2：表情包总开关开着时才追加这一段，让**同一次**看图调用顺带判定"值不值得补附"
# （方案 §3.1 的 ``replenish``）。关着时提示词与从前**逐字节一致**——多出来的字段
# 不会自己冒出来，也就没有"系统不存在"之外的痕迹。
_REPLENISH_OUTPUT_RULE = (
    '再补一个字段 "replenish"（true 或 false）：这张图里有没有**以后还能再用一次**的'
    '表达或反应（表情、梗、值得学着用的说法）。'
    '注意问的不是"它算不算表情包"，而是"以后还用得上用不上"；'
    '私人照片、聊天截图、证件票据这类默认 false。'
)


def _validated_caption(value: str) -> str:
    """兼容旧文本输出，但拒绝缺图报告；图中文字本身不会触发拒绝。"""
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    if text.startswith("{"):
        # 视觉模型的 JSON 也会被上游折行：同一套修复（裸换行 / 被劈开的键名）
        result, _repaired, _newlines = loads_json_repaired(text)
        if not isinstance(result, dict) or result.get("status") != "ok":
            return ""
        if not isinstance(result.get("description"), str):
            return ""
        text = result["description"].strip()
    body = _strip_image_index(_caption_prefix(text))
    # 只识别开头的缺图自述，避免误伤“截图上写着‘未收到图片’”等画面事实。
    if re.match(
        r"^(?:(?:抱歉|对不起)[，,：:\s]*)?(?:(?:我|模型|系统|目前|当前|这里|这次|您|你)[，,：:\s]*)*"
        r"(?:未收到(?:任何)?(?:图片|图像)|(?:并)?没有(?:收到|看到|提供|上传)(?:任何)?(?:图片|图像)|"
        r"未提供(?:任何)?(?:图片|图像)|(?:图片|图像)(?:未显示|无法显示|未上传|未提供|未收到|不可见)|"
        r"无法(?:查看|读取|识别|看到)(?:这张|该|任何)?(?:图片|图像)|"
        r"(?:I\s+)?(?:cannot|can't|did not|didn't)\s+(?:see|receive|view|access)\s+(?:the\s+|an?\s+|any\s+)?image|"
        r"no\s+image\s+(?:was\s+)?(?:provided|received|attached))",
        body, re.IGNORECASE,
    ):
        return ""
    return " ".join(text.split())[:200] if body else ""


def _caption_replenish(value: str) -> bool:
    """转述输出里「值得补附」的候选标记（方案 §3.1，字段名 ``replenish``）。

    只认 JSON 那一层：非 JSON 的老输出、缓存里翻出来的旧描述、字段缺失或不是布尔
    一律 False。**这不是"再问一次模型"**——它跟画面描述来自同一次看图的返回，
    所以没有额外调用；判错的代价也只是少补一张、或补一张不该补的图。
    """

    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    if not text.startswith("{"):
        return False
    result, _repaired, _newlines = loads_json_repaired(text)  # 视觉模型同样会被折行
    if not isinstance(result, dict) or result.get("status") != "ok":
        return False
    return result.get("replenish") is True


class AstrBotVision:
    """把图片转成一段文字，让不带视觉能力的模型也能"看到"。"""

    def __init__(self, plugin: "VirtualWorldPlugin", provider_id: str = "") -> None:
        self.plugin = plugin
        self.provider_id = provider_id
        self._negative: dict[str, float] = {}
        """失败过的指纹 → 时间：短时内不再重试，但**不**写进持久缓存。"""

        self._inflight: dict[str, asyncio.Future] = {}
        """同一张图正在识别时，其它消息先等它——不然一张图会被认好几遍。"""

        self.hits = 0
        """本次运行命中了多少次缓存（省下的转述调用）。"""

        self.last_error: str = ""
        """最近一次转述失败的原因（写进日志，方便排查"模型看不见图片"）。"""

        self._op_seq = 0
        """图片转述的序号（§4.1 分层观测）：用来拼 ``cap<毫秒>-<序号>`` 这条 op id。"""

        self._replenish: dict[str, bool] = {}
        """看过的图 → 这次转述顺带判定的「值得补附」标记（M2，方案 §3.1）。

        按**传给看图那一步的地址**记（这一层看到的就是它），所以调用方拿自己那份
        地址列表就能对上；缓存命中的旧描述没有这个字段，查不到就是 False。
        """

    @property
    def enabled(self) -> bool:
        return bool(self.provider_id.strip())

    def _meme_enabled(self) -> bool:
        """表情包总开关（M2）：关着时转述配方与从前**逐字节一致**。"""

        try:
            return bool(meme_settings(self.plugin.engine.world).enabled)
        except Exception:
            return False

    def _output_rule(self) -> str:
        """转述的输出规则：总开关开着才要求 ``replenish`` 字段（方案 §3.1）。

        这一段挂在**用户提示**上、不进 :meth:`recipe_key`，所以加它不会让已经
        写进缓存的画面描述失效（``_CAPTION_SCHEMA_VERSION`` 一个字都不动）。
        """

        if self._meme_enabled():
            return _CAPTION_OUTPUT_RULE + _REPLENISH_OUTPUT_RULE
        return _CAPTION_OUTPUT_RULE

    def _system_prompt(self) -> str:
        """当前生效的**看图**提示词：**一律内置默认**。

        用户裁定（m20632 第 3 条）：编辑器里不再提供这三个提示词的编辑框，运行时就
        只用内置默认——配置字段保留但**不读**，这样用户升级版本后表情包识别不会因为
        手上留着旧提示词而失效。
        """

        return CAPTION_SYSTEM_PROMPT

    def _new_op(self) -> str:
        """一次图片转述的 operation id：把同一张图在各阶段的日志串起来（§4.1）。

        排查"她到底看见没看见这张图"要能按一个 id 把**五个阶段**连起来看，而不是
        在几百行日志里靠时间猜。形状 ``cap<毫秒尾数>-<序号>``，肉眼可排序。
        """

        self._op_seq = (self._op_seq + 1) % 100000
        return f"cap{int(time.time() * 1000) % 100000000}-{self._op_seq:05d}"

    def _stage(self, op: str, stage: str, **fields: Any) -> None:
        """结构化阶段日志（debug 级）。

        只出**状态与计数**：参考图数、有效资产数、图片块数、缓存命中、状态词。
        **绝不出**密钥、带鉴权的完整 URL、base64 正文——这些一旦进日志就等于泄露
        （§4.1 的硬要求）。

        ``op`` 留空时用 :attr:`_current_op`：*_describe_batch* / *_prepare_sources*
        这两个内部方法的**签名不许动**——测试会用 ``vision._describe_batch = 假函数``
        这种两个位置参数的形式替换它们，多一个关键字参数就会让假函数收不到调用
        （真机上表现为"这一轮卡死"，2026-10-16 自己踩过一次）。
        """

        key = op or getattr(self, "_current_op", "")
        if not key:
            return
        detail = " ".join(
            f"{name}={value}" for name, value in fields.items() if value not in ("", None)
        )
        self.plugin.logger.debug(
            f"[virtual_world] 图片转述 {key} {stage}{' ' + detail if detail else ''}"
        )

    async def describe(
        self,
        image_sources: list[str],
        *,
        question: str = "",
        quoted: str = "",
        context_lines: list[str] | None = None,
        session_id: str = "",
        flags: list[bool] | None = None,
        scene_hint: str = "",
    ) -> list[str]:
        """把图片逐个转述成文字。失败或未配置时返回空串（调用方自己降级）。

        只做**看图**（多模态，多张图合并成一次，按图片指纹缓存），输出
        `画面描述｜类型｜文字`。「这张图跟正在聊的事有什么关系」不再单独算
        （2026-10-21 删掉：真机上 56% 的结果是「看不出直接关系」，却每批多一次
        2~5 秒的纯文本调用；交给主模型自己判断更准）。

        ``session_id``（B1 收尾）只往下传、不参与看图结果：收图失败要在事件账里
        归到会话上（细节见 :meth:`_media_ids` / ``AstrBotVision._prepare_sources``）。

        ``flags``（M2）：给了就按入参顺序填「值得补附」的候选标记（方案 §3.1）——
        与画面描述同一次调用产出，不额外多花一次模型调用。

        ``scene_hint``（2026-10-29）：**唯一允许塞给看图模型的额外信息**——只放
        「这几张里哪些是平台标记的表情包」这种**来自平台的事实**，不放聊天内容
        （放话题会让弱模型照着话题脑补画面）。
        """

        # question/quoted/context_lines 仍然收下（调用方接口不变），但不再参与看图：
        # 把"最近群里在聊什么"塞给看图模型，弱一点的模型会照着话题脑补画面。
        _ = (question, quoted, context_lines)
        # §4.1 分层观测：这一条 op id 贯穿"发现引用 → 有效资产 → 组装图片块 → 请求接受
        # → 获得有效描述"五段，排查时按它 grep 一次就能定位断在哪一段。
        op = self._new_op()
        self._current_op = op
        self._stage(
            op,
            "发现引用",
            refs=len(image_sources),
            provider=str(self.provider_id or "(会话默认)"),
            enabled=bool(self.enabled),
        )
        # **看图那一步不带话题上下文**：把"最近群里在聊什么"塞给它，弱一点的模型
        # 会照着话题脑补画面（实测：一张"口蘑"表情包被描述成"群聊截图、两个人在
        # 争论蘑菇是不是植物"）。
        looks = await self._look_many(
            image_sources,
            str(scene_hint or ""),
            session_id=session_id,
            flags=flags,
            op=op,
        )
        described = len([item for item in looks if item])
        if described:
            self._stage(op, "获得有效描述", 描述=described, 参考图=len(looks))
        else:
            self._stage(
                op,
                "没有可用描述",
                参考图=len(looks),
                原因=str(self.last_error or "模型没给出可用画面描述")[:80],
            )
        return [" ".join(str(look or "").split()) for look in looks]

    async def _look_many(
        self,
        image_sources: list[str],
        scene: str,
        *,
        key_extra: str = "",
        session_id: str = "",
        flags: list[bool] | None = None,
        op: str = "",
    ) -> list[str]:
        """看图：命中的走缓存，未命中的合并成一次调用（失败退回逐张）。

        ``key_extra`` 非空 = 这次看图是**工具专用提问**：结果单独存一格缓存，
        不覆盖通用画面描述（见 :meth:`_look_key`）。

        ``session_id``（B1 收尾）只递给 :meth:`_prepare_sources`——合并调用是跨会话
        共用的（缓存键只认图片内容），但"这张图没缓存上"要记得住是哪个会话。

        ``flags``（M2）：给了就按**入参顺序**填「值得补附」的候选标记（方案 §3.1）。
        缓存命中的旧描述没有这个字段，算 False——除非真的再花钱看一遍，否则无从得知。
        """

        self._current_op = op or getattr(self, "_current_op", "")
        # 用户 2026-10-23（问题3）：**每次尝试都要重置** last_error。以前它只在
        # __init__ 里清一次，失败原因会一直粘着——下一轮明明看成了，报出来的还是
        # 上一轮那句"图片未能取得有效本地资产"，排查时无从判断。
        self.last_error = ""
        image_sources = await self._prepare_sources(image_sources, session_id=session_id)
        results: list[str] = ["" for _ in image_sources]
        if flags is not None:
            flags[:] = [False for _ in image_sources]
        pending: list[int] = []
        cached_hits = 0
        for index, source in enumerate(image_sources):
            if not source:
                continue
            cached = await self._cached_look(source, key_extra)
            if cached:
                results[index] = cached
                cached_hits += 1
                continue
            failed_at = float(self._negative.get(self._look_key(source, key_extra)) or 0.0)
            if failed_at and time.time() - failed_at < 60:
                continue
            pending.append(index)
        self._stage(
            op,
            "缓存与待看",
            图片块=len([item for item in image_sources if item]),
            缓存命中=cached_hits,
            待看=len(pending),
        )
        cap = 0
        try:
            cap = int(
                getattr(self.engine.world.media, "caption_max_per_round", 0) or 0
            )
        except Exception:  # noqa: BLE001
            cap = 0
        if cap > 0 and len(pending) > cap:
            # 一轮十张全转述既慢又贵：只把最近这几张看清楚，其余的留编号给下一轮。
            self._stage(op, "超出本轮转述上限", 待看=len(pending), 上限=cap)
            pending = pending[:cap]
        if not pending:
            return results
        if not self.enabled:
            self._stage(op, "未启用看图模型", 待看=len(pending))
            return results
        if len(pending) == 1:
            index = pending[0]
            results[index] = await self._describe_cached(
                image_sources[index], scene, key_extra
            )
            if flags is not None:
                flags[index] = bool(self._replenish.get(image_sources[index]))
            self._stage(op, "单图请求完成", 成功=bool(results[index]))
            return results
        # 多张一起看：一次多模态调用。也走同一份 inflight 注册——
        # 两轮同时要看同样几张图时只调一次模型，后来者等前一轮把描述写进缓存
        for _attempt in range(2):
            batch_key = "batch|" + "|".join(
                self._look_key(image_sources[index], key_extra) for index in pending
            )
            running = self._inflight.get(batch_key)
            if running is None:
                break
            try:
                await asyncio.shield(running)
            except Exception:
                pass
            for index in list(pending):
                cached = await self._cached_look(image_sources[index], key_extra)
                if cached:
                    results[index] = cached
                    pending.remove(index)
            if not pending:
                return results
        sources = [image_sources[index] for index in pending]
        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        self._inflight[batch_key] = ready
        try:
            self._stage(op, "组装图片块", 图片块=len(sources), 一次调用="是")
            merged = await self._describe_batch(sources, scene)
            fell_back = 0
            for slot, index in enumerate(pending):
                caption = _validated_caption(merged[slot]) if slot < len(merged) else ""
                if not caption:
                    # 合并解析失败：这张退回逐张调用，稳妥优先
                    fell_back += 1
                    caption = await self._describe_cached(
                        image_sources[index], scene, key_extra
                    )
                else:
                    await self._store_look(image_sources[index], caption, key_extra)
                results[index] = caption
                if flags is not None:
                    flags[index] = bool(self._replenish.get(image_sources[index]))
            self._stage(
                op,
                "多图请求完成",
                成功=len([item for index, item in enumerate(results) if item and index in pending]),
                退回逐张=fell_back,
            )
        finally:
            # 无论如何都要唤醒等同一批的人，不能留一个永远不完成的 Future
            self._inflight.pop(batch_key, None)
            if not ready.done():
                ready.set_result("")
        return results

    async def _prepare_sources(
        self, sources: list[str], *, session_id: str = ""
    ) -> list[str]:
        """复用现有有界下载及资产校验；原引用留在调用方用于回挂。

        ``session_id``（B1 收尾）交给 ``cache_media_list``：这一批里没缓存上的图要在
        事件账里记下**是哪个会话**收图失败——不给会话的失败行归不了档。
        """
        engine = self.plugin.engine
        # 用户 2026-10-23（问题3）：每次尝试都从干净状态开始，上一轮那句
        # "图片没读成：…"不能糊到这一轮的结果上。
        self.last_error = ""
        refs = len([item for item in sources if item])
        if not callable(getattr(engine, "cache_media_list", None)):
            # 走不到资产库也要说一声：不然"有效地资产"这一段在日志里凭空消失，
            # 排查时会误以为图根本没进来
            self._stage("", "有效本地资产", 参考图=refs, 说明="宿主没有资产库，直接用原引用")
            return list(sources)  # 兼容精简宿主/测试桩
        if not engine.media_settings()["enabled"]:
            self._stage("", "有效本地资产", 参考图=refs, 说明="资产库关着，直接用原引用")
            return list(sources)
        resolved = ["" for _ in sources]
        # 用户 2026-10-23（问题3）：每一张没读成的图都要说清**是哪张、为什么**
        # （文件不存在 / 超过 10MB / 不是图片 / download_failed）。以前所有原因都被
        # 收成一句"图片未能取得有效本地资产"，主因（临时文件被清理、超上限）在日志里
        # 完全看不出来。
        problems: dict[int, str] = {}
        store = engine.media_store()
        manifest = store.manifest()
        # 清单加载顺序不代表使用顺序；同来源多份内容优先最近使用且文件有效的资产。
        by_source: dict[str, str] = {}
        for media_id, asset in sorted(
            manifest.items(),
            key=lambda item: (float(item[1].get("last_used_at") or 0), float(item[1].get("created_at") or 0)),
            reverse=True,
        ):
            relevant = [source for source in asset.get("sources", []) if source in sources and source not in by_source]
            if relevant and engine.media_file_source(media_id):
                for source in relevant:
                    by_source[source] = media_id
        for index, source in enumerate(sources):
            media_id = by_source.get(source, "")
            local = engine.media_file_source(media_id) if media_id else ""
            if local:
                resolved[index] = local
        # 新一轮先有界刷新来源；同地址换内容取得新的内容身份，过期来源退回已有资产。
        refresh = [index for index, source in enumerate(sources) if source]
        ids = await engine.cache_media_list(
            [sources[index] for index in refresh], session_id=session_id
        )
        settings = engine.media_settings()
        limit = int(settings.get("max_file_bytes") or 0)
        for index, media_id in zip(refresh, ids):
            if media_id:
                resolved[index] = engine.media_file_source(media_id)
            elif not sources[index].lower().startswith(("http://", "https://")):
                # 收不进资产库，但**本地文件还在**时别直接丢掉：协议端给的临时图
                # （``data/temp/media_image_….png``）经常在收图失败后仍然读得到，
                # 而这里一丢，识图就必然回「图片未能取得有效本地资产」（用户 m28627）。
                # 读字节与格式判定交给下面的快照那一段，读不到自然还是空。
                readable = _readable_local_file(sources[index], limit)
                resolved[index] = sources[index] if readable else ""
                if not readable:
                    problems[index] = _local_file_problem(sources[index], limit)
        # 拍成稳定字节快照，避免资产 GC/文件替换发生在组装输入与宿主读图之间。
        from .core.media_store import is_image_mime, sniff_mime
        for index, local in enumerate(resolved):
            if not local:
                problems.setdefault(index, "download_failed（没下载到本地有效文件）")
                continue
            try:
                payload = Path(local).read_bytes()
            except OSError:
                resolved[index] = ""
                problems[index] = "file_missing（文件读不出来，可能已被清理）"
                continue
            if not payload:
                resolved[index] = ""
                problems[index] = "empty_file（0 字节）"
                continue
            if limit > 0 and len(payload) > limit:
                resolved[index] = ""
                problems[index] = f"too_large（{len(payload)} 字节 > 上限 {limit} 字节）"
                continue
            if not is_image_mime(sniff_mime(payload)):
                resolved[index] = ""
                problems[index] = "not_image（文件头不是图片）"
                continue
            resolved[index] = "base64://" + base64.b64encode(payload).decode("ascii")
        detail = "；".join(
            f"图{index + 1} {reason}" for index, reason in sorted(problems.items())
        )
        if detail:
            # 用户 2026-10-23（问题3）：可诊断的字符串——哪张图、什么原因；同时写事件账
            self.last_error = f"图片没读成：{detail}"
            await self._note_media_problems(session_id, problems, detail)
        # 「下载成功 / 有效资产」这一段：只报计数，**不报地址**（地址可能带鉴权串）
        self._stage(
            "",
            "有效本地资产",
            参考图=refs,
            可用=len([item for item in resolved if item]),
            没读成=len(problems),
            原因=detail,
        )
        return resolved

    async def _note_media_problems(
        self, session_id: str, problems: dict[int, str], detail: str
    ) -> None:
        """把「哪张图没读成、为什么」写进事件账（用户 2026-10-23，问题3）。"""

        engine = getattr(self.plugin, "engine", None)
        target = str(session_id or "").strip()
        if engine is None or not target:
            return
        try:
            await engine.note_vision(
                target,
                ok=False,
                images=len(problems),
                detail=f"图片没读成：{detail}",
            )
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响这一轮看图
            self.plugin.logger.debug(f"[virtual_world] 图片失败明细写事件账失败：{exc}")

    async def describe_to_text(
        self, image_sources: list[str], prompt: str = "", *, session_id: str = ""
    ) -> str:
        """给引擎用的看图入口（天气工具返回的图要读出来）：带缓存、多图合并成一次调用。

        ``session_id``（B1 收尾，可选、位置参数不变）：只往下传给收图那一步，
        让"这张工具返回的图没缓存上"能归到发起这次工具的会话上。
        """

        sources = [str(item) for item in (image_sources or []) if str(item).strip()]
        if not sources or not self.enabled:
            return ""
        question = str(prompt or "").strip()
        # 工具提问单独一格缓存：它的"与这个问题的关系"不是通用画面描述
        key_extra = hashlib.sha1(question.encode("utf-8", "ignore")).hexdigest()[:12] if question else ""
        looks = await self._look_many(
            sources, question, key_extra=key_extra, session_id=session_id
        )
        return "\n".join(text for text in looks if text).strip()

    async def summarize_forward(self, digest: str, images: list[str], *, fingerprint: str) -> str:
        """把一段合并转发压成摘要（含里面的图）：带缓存，失败返回空串。

        调用方拿到空串时保持原来的做法（只在聊天记录里落一句"这是转发"）。
        """

        body = " ".join(str(digest or "").split())
        if not body or not self.enabled:
            return ""
        cached = await self._cached_forward(fingerprint)
        if cached:
            return cached
        refs = [
            str(item).strip()
            for item in list(images or [])[:MAX_FORWARD_IMAGES]
            if str(item).strip()
        ]
        try:
            response = await self.plugin.context.llm_generate(
                chat_provider_id=self.provider_id,
                system_prompt=self._forward_prompt(),
                prompt=f"这段转发的内容：\n{body}",
                image_urls=refs or None,
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 转发摘要失败：{exc}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            self.last_error = "摘要模型没有返回文字"
            return ""
        summary = _clean_forward_summary(text, self._forward_max_chars())
        if not summary:
            return ""
        await self._store_forward(fingerprint, summary)
        return summary

    def _forward_prompt(self) -> str:
        """当前生效的转发摘要提示词：**一律内置默认**（同 :meth:`_system_prompt`）。"""

        return DEFAULT_FORWARD_PROMPT

    def _forward_max_chars(self) -> int:
        try:
            limit = int(self.plugin.engine.world.vision.forward_max_chars or 0)
        except Exception:
            limit = 0
        return max(80, limit or 300)

    async def _cached_forward(self, fingerprint: str) -> str:
        cache = self._cache_config()
        if not fingerprint or not bool(cache.get("enabled")):
            return ""
        entry = await self._cache_get_forward(fingerprint, float(cache.get("max_age") or 0))
        if entry is None:
            return ""
        self.hits += 1
        try:
            await self.plugin.db.call("touch_forward_summary", fingerprint=fingerprint)
        except Exception:
            pass
        return str(entry.get("summary") or "")

    async def _store_forward(self, fingerprint: str, summary: str) -> None:
        if not fingerprint or not summary:
            return
        cache = self._cache_config()
        try:
            await self.plugin.db.call(
                "put_forward_summary",
                fingerprint=fingerprint,
                summary=summary,
                model=self.provider_id,
            )
            await self.plugin.db.call(
                "trim_forward_cache",
                keep=int(cache.get("max") or 500),
                max_age_seconds=float(cache.get("max_age") or 0),
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 写转发缓存失败：{exc}")

    async def _cache_get_forward(
        self, fingerprint: str, max_age: float
    ) -> dict[str, Any] | None:
        try:
            return await self.plugin.db.call(
                "get_forward_summary", fingerprint=fingerprint, max_age_seconds=max_age
            )
        except Exception:
            return None

    def recipe_key(self) -> str:
        """当前这套"看图配方"的版本号：模型 + 系统提示词。

        引擎那边的"这张已经转述过"是**按引用/版本**记的（阶段B2）：配方换了，
        旧描述就该重做一遍，而不是因为"同一个地址见过"就永远不再看。
        """

        prompt = self._system_prompt()
        return hashlib.sha1(
            f"{_CAPTION_SCHEMA_VERSION}|{self.provider_id}|{self._model_identity()}|{prompt}".encode("utf-8", "ignore")
        ).hexdigest()[:12]

    def _model_identity(self) -> str:
        try:
            provider = self.plugin.context.get_provider_by_id(self.provider_id)
            return str(provider.get_model() or "")
        except Exception:
            return ""

    def _look_key(self, source: str, key_extra: str = "") -> str:
        """看图缓存的 key：图片指纹 + 模型 + 提示词版本（+ 工具提问的作用域）。

        图片是内容，"模型 + 提示词"是生产这段描述的配方：换了看图模型、改了提示词，
        旧描述就不该接着用（资产本身留着，见 core/media_store.py 里的 ``caption_key``）。
        ``key_extra`` 给**工具专用提问**用：它写的"和这个问题的关系"不是通用画面描述，
        不能覆盖通用那一格（这正是以前"天气工具问过之后，聊天里那张图只剩天气答话"
        的原因）。
        """

        fingerprint = _image_fingerprint(source)
        try:
            if not source.startswith(("base64://", "data:", "http://", "https://")):
                path = Path(source)
                if path.is_file():
                    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            pass
        if not fingerprint:
            return ""
        return f"{fingerprint}|v:{self.recipe_key()}" + (
            f"|q:{key_extra}" if key_extra else ""
        )

    async def _cached_look(self, source: str, key_extra: str = "") -> str:
        """命中缓存就返回"看图结果"（不含关系）。"""

        fingerprint = self._look_key(source, key_extra)
        cache = self._cache_config()
        if not fingerprint or not bool(cache.get("enabled")):
            return ""
        entry = await self._cache_get(fingerprint, float(cache.get("max_age") or 0))
        if entry is None:
            return ""
        caption = str(entry.get("caption") or "")
        prefix = str(entry.get("prefix") or "")
        if "与话题的关系" in caption:
            caption = prefix or _caption_prefix(caption)
        caption = _validated_caption(caption)
        if not caption:
            return ""
        self.hits += 1
        await self._cache_touch(fingerprint)
        return caption

    async def _store_look(self, source: str, caption: str, key_extra: str = "") -> None:
        caption = _validated_caption(caption)
        fingerprint = self._look_key(source, key_extra)
        cache = self._cache_config()
        if not fingerprint or not bool(cache.get("enabled")) or not caption:
            return
        await self._cache_put(fingerprint, caption=caption, prefix=caption)

    async def _describe_batch(self, sources: list[str], scene: str) -> list[str]:
        """一次看多张图：要求逐行输出，解析不出来返回空列表。"""

        if len(sources) < 2:
            return []
        prompt = (scene + "\n\n" if scene else "") + _MULTI_IMAGE_RULE.format(
            count=len(sources)
        ).strip() + self._output_rule() + "每行编号后放该图的 JSON 对象。"
        try:
            response = await self.plugin.context.llm_generate(
                chat_provider_id=self.provider_id,
                system_prompt=self._system_prompt(),
                prompt=prompt,
                image_urls=list(sources),
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 多图转述失败：{exc}")
            self._stage("", "请求失败", 图片块=len(sources), 原因=f"{type(exc).__name__}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        text = str(getattr(response, "completion_text", "") or "").strip()
        self._stage("", "请求接受", 图片块=len(sources), 回复字数=len(text))
        if not text:
            self.last_error = "转述模型没有返回文字（可能不支持图片输入）"
            return []
        parts = _split_multi_caption(text, len(sources))
        validated = [_validated_caption(part) for part in parts]
        # 一次多图调用里每张图的「值得补附」标记：按位置对回各自的地址
        for slot, part in enumerate(parts):
            if slot < len(sources):
                self._replenish[sources[slot]] = _caption_replenish(part)
        if parts and not all(validated):
            self.last_error = "转述模型报告缺图或无法读取图片"
        return validated

    async def _describe_cached(self, source: str, scene: str, key_extra: str = "") -> str:
        """看单张图（缓存由调用方先查过）：带并发去重、失败不落盘。"""

        if not source:
            return ""
        fingerprint = self._look_key(source, key_extra)
        if not self.enabled:
            return ""
        # 同一张图正在识别：等它，别再调一次模型
        pending = self._inflight.get(fingerprint) if fingerprint else None
        if pending is not None:
            try:
                return str(await asyncio.shield(pending))
            except Exception:
                return ""
        failed_at = float(self._negative.get(fingerprint) or 0.0)
        if failed_at and time.time() - failed_at < 60:
            return ""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        if fingerprint:
            self._inflight[fingerprint] = future
        caption = ""
        try:
            caption = _validated_caption(await self._describe_one(source, scene))
        except asyncio.CancelledError:
            # 等这张图的人不能一直被挂着：谁在看的那一轮被取消，等待者也一起结束
            if not future.done():
                future.cancel()
            raise
        except Exception as exc:
            # 转述本身炸了也要给等待者一个着落，不能留一个永远不完成的 Future
            self.last_error = f"{type(exc).__name__}: {exc}"
            caption = ""
        finally:
            if fingerprint:
                self._inflight.pop(fingerprint, None)
        try:
            if not caption:
                # 失败不写持久缓存：偶发超时不该被永久记成"看不出"
                if fingerprint:
                    self._negative[fingerprint] = time.time()
                return ""
            await self._store_look(source, caption, key_extra)
            return caption
        finally:
            if not future.done():
                future.set_result(caption)

    def _cache_config(self) -> dict[str, Any]:
        try:
            vision = self.plugin.engine.world.vision
            days = max(0, int(getattr(vision, "cache_days", 30) or 0))
            return {
                "enabled": bool(getattr(vision, "cache_enabled", True)),
                "max": max(1, int(getattr(vision, "cache_max", 500) or 500)),
                "max_age": days * 86400 if days else 0,
            }
        except Exception:
            return {"enabled": True, "max": 500, "max_age": 30 * 86400}

    async def _cache_get(self, fingerprint: str, max_age: float) -> dict[str, Any] | None:
        try:
            return await self.plugin.db.call(
                "get_image_caption", fingerprint=fingerprint, max_age_seconds=max_age
            )
        except Exception:
            return None

    async def _cache_touch(self, fingerprint: str) -> None:
        try:
            await self.plugin.db.call("touch_image_caption", fingerprint=fingerprint)
        except Exception:
            pass

    async def _cache_put(self, fingerprint: str, *, caption: str, prefix: str) -> None:
        cache = self._cache_config()
        try:
            await self.plugin.db.call(
                "put_image_caption",
                fingerprint=fingerprint,
                caption=caption,
                prefix=prefix,
                model=self.provider_id,
            )
            await self.plugin.db.call(
                "trim_image_cache",
                keep=int(cache.get("max") or 500),
                max_age_seconds=float(cache.get("max_age") or 0),
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 写图片缓存失败：{exc}")

    @staticmethod
    def _scene_text(
        *,
        question: str = "",
        quoted: str = "",
        context_lines: list[str] | None = None,
    ) -> str:
        parts: list[str] = []
        if question:
            parts.append(f"这条消息里说的话：{question}")
        if quoted:
            parts.append(f"引用的消息：{quoted}")
        lines = [str(item).strip() for item in (context_lines or []) if str(item).strip()]
        if lines:
            parts.append("最近群里在聊：\n" + "\n".join(lines[-6:]))
        return "\n".join(parts)

    async def _describe_one(self, source: str, scene: str) -> str:
        try:
            response = await self.plugin.context.llm_generate(
                chat_provider_id=self.provider_id,
                system_prompt=self._system_prompt(),
                prompt=(scene + "\n\n" if scene else "") + "请描述这张图片。" + self._output_rule(),
                image_urls=[source],
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 图片转述失败：{exc}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            self.last_error = "转述模型没有返回文字（可能不支持图片输入）"
        # 「值得补附」的候选标记要趁 JSON 还在的时候取（校验那一步只留画面描述）
        self._replenish[source] = _caption_replenish(text)
        caption = _validated_caption(text)
        if text and not caption:
            self.last_error = "转述模型报告缺图或无法读取图片"
        return caption

    async def describe_meme_fields(self, source: str) -> dict[str, str]:
        """表情包收藏专用的"三栏"看图：是什么 / 什么时候用 / 什么时候不用。

        不走 :meth:`_describe_cached`：那条管道会把结果压成**单行 200 字**的画面描述
        （:func:`_validated_caption`），三栏到那儿就被揉成一团了；而且它的提示词是
        给聊天转述用的，只看画面、不问用途（用户 m18616：补描述只写了含义，
        「什么时候用」「什么时候不用」全是空的）。

        失败或没认出来就回空字典，调用方按"没补成"处理。
        """

        if not source:
            return {}
        text = ""
        try:
            response = await self.plugin.context.llm_generate(
                chat_provider_id=self.provider_id,
                # 必须换掉聊天转述的那份 system（用户 m18938）：它把输出格式钉成
                # 「画面描述｜类型｜文字」，而「类型」就是「表情包 / 梗图」，
                # 于是模型照 system 走，三栏 JSON 根本没有机会出现。
                system_prompt=MEME_DESCRIBE_SYSTEM,
                prompt=MEME_DESCRIBE_SCENE + _MEME_DESCRIBE_RULE,
                image_urls=[source],
            )
        except Exception as exc:  # noqa: BLE001
            self.plugin.logger.debug(f"[virtual_world] 表情包看图失败：{exc}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {}
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            self.last_error = "转述模型没有返回文字（可能不支持图片输入）"
            return {}
        fields = _meme_describe_fields(text)
        if not fields:
            self.last_error = "转述模型报告缺图或无法读取图片"
        return fields


MEME_DESCRIBE_SCENE = (
    "这张图是表情包收藏里的一张，主人以后要靠「含义 / 什么时候用 / 什么时候不用」"
    "这三栏检索和挑图。看图，按下面三件事写，**别写用法以外的客套**：\n"
    "- 含义：画面里有什么（谁、什么动作、什么表情），图上的字照抄；"
    "别写「表情包」「梗图」这种对每张图都成立的标签。\n"
    "- 什么时候用：什么样的时刻会想发这一张（回应什么、表达什么情绪）。\n"
    "- 什么时候不用：什么场合 / 什么事情发它不合适。\n"
    "写具体、短，别复述文件名，别编图上看不到的东西。"
)
_MEME_DESCRIBE_RULE = (
    '\n返回 JSON 对象：{"status":"ok|unreadable|no_image",'
    '"meaning":"含义，40 字内",'
    '"use_when":"什么时候用，25 字内",'
    '"avoid_when":"什么时候不用，25 字内"}。'
    "只有实际看到了画面才用 ok；没有图片用 no_image；无法读取用 unreadable。"
)


MEME_DESCRIBE_SYSTEM = (
    "你是表情包收藏的看图助手。只做一件事：看清这张图，给出三栏短文字，"
    "让主人以后能按这三栏把它找出来。\n"
    "只输出一个 JSON 对象，不要输出任何别的字，不要分栏排版，"
    "不要写「画面描述｜类型｜文字」这种转述格式。\n"
    "不要写「表情包」「梗图」「截图」「照片」这类对整张图下分类的词——"
    "这里每一条都是表情包，标了等于没标。\n"
    "别编图上看不到的东西；看不清就写看不清。"
)


def _meme_meaning_from_line(text: str) -> str:
    """把「画面｜类型｜文字」这种转述行收拾成一句人话。

    用户 m18938 报"还是会有表情包梗图这种标识"：旧的转述提示词要求
    ``画面描述｜类型｜文字``，模型照做，整行被当 `meaning` 存下来。这里按竖线切开，
    把"类型"那一段（表情包/梗图/截图/照片/其它/无）和"文字："前缀的段扔掉，
    剩下的拼回一句。
    """

    flat = " ".join(str(text or "").split())
    if not flat:
        return ""
    parts = [part.strip() for part in re.split(r"[|｜]", flat) if part.strip()]
    kept: list[str] = []
    for part in parts:
        bare = part.strip(" ：:，,。")
        if _MEME_TYPE_TAGS.match(bare):
            continue
        if re.match(r"^(文字|类型|画面描述)\s*[：:]", part):
            body = re.sub(r"^(文字|类型|画面描述)\s*[：:]\s*", "", part).strip()
            if body and not _MEME_TYPE_TAGS.match(body.strip(" ：:，,。")):
                kept.append(body)
            continue
        kept.append(part)
    line = "，".join(kept) if kept else flat
    return line[:120]


_MEME_TYPE_TAGS = re.compile(
    r"^(表情包|梗图|表情包\s*/\s*梗图|截图|照片|其它|其他|无|疑似梗图.*)$"
)

# 补描述时往回翻的速度（用户 m19069：以前只读最近 200 条，超过 200 张的老收藏补不到）。
# 一页 200 条、最多翻 10 页（2000 条）；凑够这一批要的量就提前停。
MEME_DESCRIBE_SCAN_PAGE = 200
MEME_DESCRIBE_SCAN_CAP = 2000

# 「全部导出」回溯上限：再多也只是内存里攒一堆图片字节，没有意义。
MEME_EXPORT_SCAN_CAP = 5000


def _meme_describe_fields(value: Any) -> dict[str, str]:
    """把一次"三栏看图"的返回拆成 ``{"meaning","use_when","avoid_when"}``。

    认 JSON；模型退化成一整句话时，用 :func:`_meme_meaning_from_line` 收拾成一句
    ``meaning``（宁可少写两栏，也别把描述丢掉——旧口径的缓存里就是这种纯文本）。
    ``status`` 不是 ok 一律算失败。
    """

    text = str(value or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    payload: Any = None
    if text.startswith("{"):
        payload, _repaired, _newlines = loads_json_repaired(text)
    if not isinstance(payload, dict):
        flat = _meme_meaning_from_line(text)
        return {"meaning": flat} if flat else {}
    if str(payload.get("status") or "ok").strip().lower() != "ok":
        return {}
    fields: dict[str, str] = {}
    for key, limit in (("meaning", 120), ("use_when", 60), ("avoid_when", 60)):
        raw = payload.get(key)
        cleaned = " ".join(str(raw or "").split())
        if cleaned:
            fields[key] = cleaned[:limit]
    if fields.get("meaning"):
        fields["meaning"] = _meme_meaning_from_line(fields["meaning"]) or fields["meaning"]
    return fields


def _meme_meaning_missing(value: Any) -> bool:
    """这一条是不是"还没有一句人话描述"。

    只认 ``meaning``。占位文案也算空——历史上有过把文件名 / 「（还没有描述）」写进
    ``meaning`` 的版本，那些条目在页面上看起来"有描述"，实际检索不到，也永远进不了
    补描述的名单（用户 m18371 就卡在这里）。
    """

    text = " ".join(str(value or "").split())
    if not text:
        return True
    if text in _MEME_MEANING_PLACEHOLDERS:
        return True
    # 「…｜表情包｜梗图｜收到」这种转述行也算没写好（用户 m18938）：类型标签是零信息，
    # 拿它当"含义"看着有字，检索和挑图都用不上。
    if re.search(r"[|｜]", text):
        return True
    return bool(_MEME_TYPE_TAGS.match(text.strip(" ：:，,。")))


_MEME_MEANING_PLACEHOLDERS = {
    "（还没有描述）",
    "(还没有描述)",
    "还没有描述",
    "暂无",
    "无",
    "—",
    "-",
}


def _meme_usage_missing(view: Any) -> bool:
    """这一条还缺"含义"或"什么时候用/不用"。

    老版本只写了 ``meaning``（图上有什么），另外两栏一直是空的——按旧口径它们算
    "写好了"，于是永远进不了补描述的名单（用户 m18616 截图里那条就是这样）。
    现在三栏**任一为空都算缺**（用户 m18938：“有的写了什么时候用什么时候不用 有的就没有”），
    这样只写了一半的老条目也会被补齐；代价是模型确实想不出某栏时，那一条会一直留在
    名单里——补描述是手动点的，不会自己跑。
    """

    if _meme_meaning_missing((view or {}).get("meaning")):
        return True
    for key in ("use_when", "avoid_when"):
        if not " ".join(str((view or {}).get(key) or "").split()):
            return True
    return False


def _meme_describe_targets(
    views: Any, *, ids: Any = (), force: bool = False
) -> list[dict[str, str]]:
    """挑出"还没有一句人话描述"的收藏（给了 ``ids`` 就只看这几条）。

    只认 ``meaning``：上传接口只写文件名，``use_when``/``avoid_when`` 也是空的，
    但检索与索引文本都以 ``meaning`` 为主（见 ``core/meme_store.py`` 的
    ``_match_row`` 与 ``core/engine.py`` 的索引配方），补这一格收益最大。
    已经软删的跳过。

    ``force=True`` 时不看有没有描述——选中某几张想"重写"就走这条。
    """

    wanted = {str(item).strip() for item in (ids or ()) if str(item or "").strip()}
    picks: list[dict[str, str]] = []
    for view in views or ():
        if not isinstance(view, dict):
            continue
        entry_id = str(view.get("entry_id") or "").strip()
        if not entry_id:
            continue
        if wanted and entry_id not in wanted:
            continue
        if str(view.get("status") or "") == "deleted":
            continue
        if not force and not _meme_usage_missing(view):
            continue
        picks.append(
            {
                "id": entry_id,
                "name": str(view.get("name") or ""),
                "media_id": str(view.get("media_id") or ""),
            }
        )
    return picks


async def _caption_result_images(
    plugin: "VirtualWorldPlugin",
    images: list[str],
    *,
    scene: str = "",
    session_id: str = "",
) -> tuple[list[str], list[str]]:
    """工具 / 指令返回的图片：配了转述模型就先转述，没配就原样带回去。

    返回 ``(转述后的描述, 还需要交给多模态模型的图片)``——转述成功就不必再传图片，
    失败或没配转述模型时把地址原样交上去，由 ``llm_generate`` 那条路处理。

    ``session_id``（B1 收尾）：调用方（指令 / 工具执行器）手里就有会话，传给
    ``describe`` 让收图失败能归到发起这次工具调用的会话上。
    """

    if not images:
        return [], []
    limit = 3
    try:
        limit = max(1, int(plugin.engine.world.context.image_max))
    except Exception:
        pass
    picked = images[:limit]
    vision = getattr(plugin, "vision", None)
    if vision is None or not vision.enabled:
        return [], picked
    try:
        captions = await vision.describe(picked, question=scene, session_id=session_id)
    except Exception as exc:
        plugin.logger.debug(f"[virtual_world] 返回内容里的图片转述失败：{exc}")
        captions = []
    described = [
        f"图片{index + 1}：{caption}"
        for index, caption in enumerate(captions or [])
        if caption
    ]
    if not described:
        return [], picked
    return described, []


def _safe_deepcopy(value: Any) -> Any:
    """能深拷就深拷，拷不动就原样用。

    事件对象里偶尔挂着拷不了的东西（2026-10-19 真机 S9a：宿主 webchat 事件的某个
    字段里带着 ``_asyncio.Future``，``copy.deepcopy`` 直接抛
    ``cannot pickle '_asyncio.Future' object``，于是她连记事本都写不进去）。
    这些字段只是给工具当上下文用的，拷不了时共用一份不会伤到分发。
    """

    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _invocation_event(event: Any, session_id: str):
    """Isolate an exact-session event and collect cooperative sends.

    Args:
        event: A real event from the execution session.
        session_id: Frozen execution target.

    Returns:
        Independent event, collected messages, and lifetime closer.

    Raises:
        ValueError: The event does not match the actual target route.
    """
    wanted = str(session_id or "").strip()
    origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
    if event is None or (wanted and origin != wanted):
        raise ValueError("目标会话没有匹配的事件上下文，不能借用其他会话执行")
    parts = wanted.split(":", 2)
    group_getter = getattr(event, "get_group_id", None)
    sender_getter = getattr(event, "get_sender_id", None)
    if len(parts) == 3 and callable(group_getter):
        group = str(group_getter() or "")
        if parts[1] == "GroupMessage" and group != parts[2]:
            raise ValueError("目标群与事件实际群不一致，拒绝执行")
        if parts[1] in ("FriendMessage", "PrivateMessage"):
            sender = str(sender_getter() or "") if callable(sender_getter) else ""
            # 私聊的第三段不一定是裸 user_id：webchat 那边是
            # ``webchat:FriendMessage:webchat!astrbot!vwprivate``，人只是其中一节。
            # 以前要求整段相等，于是私聊里**所有**宿主工具（查资料、记事本、出图）
            # 都被判"路由不一致"拒掉——2026-10-19 真机 S9a 逮到的。
            segments = [seg for seg in parts[2].replace(":", "!").split("!") if seg]
            if group or (sender and sender not in segments):
                raise ValueError("目标私聊与事件实际路由不一致，拒绝执行")
    cloned = copy.copy(event)
    for field in ("message_obj", "session", "new_session", "_extras", "_result", "raw_message"):
        if hasattr(event, field):
            setattr(cloned, field, _safe_deepcopy(getattr(event, field)))
    sent: list[Any] = []
    active = True

    async def collect(message: Any, *args: Any, **kwargs: Any) -> Any:
        """收下这次发送。

        返回**消息本身**而不是 ``None``：不少插件会写 ``if not await event.send(...)``
        来判断成没成，返回 None 会被当成失败（2026-10-20 实机：拍照插件"生成成功但
        发送失败"，就是它拿 VW 借给它的这个假 send 的返回值当真了）。

        ``close()`` 之后还有一小段宽限期：插件常在处理器返回后由后台任务补发结果
        （出图、合成语音）。宽限期内的补发**直接走真事件发出去**——收进 ``sent``
        已经没人读了，转发才是它真的想做的事；超过宽限期才报错。
        """

        sent.append(message)
        if active:
            return message
        remains = INVOCATION_GRACE_SECONDS - (time.monotonic() - float(closed_at[0] or 0.0))
        if remains <= 0:
            raise RuntimeError("日程调用已结束，不支持后台延迟发送")
        forward = getattr(original, "send", None)
        if callable(forward):
            try:
                await forward(message)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"日程调用已结束，补发失败：{exc}") from exc
        return message

    async def no_stream(generator: Any = None, *args: Any, **kwargs: Any) -> Any:
        """流式发送：收下不抛。

        以前这里直接 ``raise``，插件一旦用 ``send_streaming`` 报进度（出图插件很常见），
        就会把整次调用当失败（2026-10-20 实机自拍事故）。现在把流读完、只留最后一段，
        当成一次普通发送记下来。
        """

        last: Any = None
        if generator is not None:
            try:
                async for chunk in generator:
                    last = chunk
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"流式发送失败：{exc}") from exc
        if last is not None:
            await collect(last)
        return last

    def close() -> None:
        nonlocal active
        active = False
        closed_at[0] = time.monotonic()

    original = event
    closed_at = [0.0]
    cloned.send = collect
    cloned.send_streaming = no_stream
    return cloned, sent, close


def _swap_event_session(event: Any, session_id: str):
    """临时把事件的会话换成真正要跑的那条，返回还原用的回调。

    指令处理器和工具（出图、发语音这类）常常按事件的会话把结果直接发出去。
    借来的事件如果还挂在别的会话上，结果就会跑错地方——私聊里让她拍照、
    图却发到群里就是这么来的。借同组事件时把它指到真正的落点上再跑，
    跑完立刻还原（事件还连着 AstrBot 的分发，不能一直留着改动）。
    """

    wanted = str(session_id or "").strip()
    previous = getattr(event, "session", None)
    previous_new = getattr(event, "new_session", None)
    try:
        current = str(getattr(event, "unified_msg_origin", "") or "")
    except Exception:
        current = ""
    if not wanted or current == wanted:
        return lambda: None

    def restore() -> None:
        # 真实事件里 ``unified_msg_origin`` 是从 ``session`` 算出来的，换回
        # session 就够了；有的实现（以及测试替身）只是个普通属性，所以两边
        # 都还原一次，哪种都不会留下改过的会话。
        try:
            event.session = previous
        except Exception:
            pass
        try:
            event.new_session = previous_new
        except Exception:
            pass
        try:
            if str(getattr(event, "unified_msg_origin", "") or "") != current:
                event.unified_msg_origin = current
        except Exception:
            pass

    try:
        event.unified_msg_origin = wanted
    except Exception:
        return lambda: None
    return restore


class AstrBotCommands:
    """把「指令触发」型动作转成真正的指令，交给别的插件执行。

    寻找方式与 AstrBot 的指令分发一致：从 handler 注册表里按指令名（含别名）匹配，
    借用这个会话最近一条真实事件，把消息文本换成指令本身，再调用它的处理器。
    处理器 yield 出来的文本会被收集起来交回给大模型。
    """

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin

    def session_with_context(self, candidates: list[str] | None = None) -> str:
        """最近收到过消息的那条会话（在 ``candidates`` 里挑，没给就全局挑）。"""

        events = getattr(self.plugin, "_last_events", None) or {}
        if not events:
            return ""
        if candidates is None:
            return next(reversed(events), "")
        wanted = {str(item) for item in candidates}
        for session_id in reversed(events):
            if session_id in wanted:
                return session_id
        return ""

    async def trigger(
        self, session_id: str, command: str, *, event: Any = None
    ) -> ToolCallResult:
        text = " ".join(str(command or "").split())
        if not text:
            return ToolCallResult(ok=False, error="指令是空的")
        if not text.startswith("/"):
            text = "/" + text
        word = text.split()[0].lstrip("/")
        matched = self._find(text)
        if matched is None:
            return ToolCallResult(ok=False, error=f"没找到指令「{word}」")
        record, command_filter = matched
        # 只使用冻结目标自身的事件；同组群聊/私聊也不能互相借用。
        # 没有目标上下文时明确失败，独立副本负责收集处理器的合作发送。
        real_event = event or self.plugin.nearby_event(session_id)
        if real_event is None:
            return ToolCallResult(
                ok=False, error="还没有收到过这个会话的消息，指令没有上下文可用"
            )
        try:
            params = self._params(record, command_filter, text)
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc))
        try:
            real_event, direct_results, close_invocation = _invocation_event(real_event, session_id)
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc), tool=word)
        restore = self._swap_text(real_event, text)
        try:
            result = record.handler(real_event, **params)
            texts: list[str] = []
            images: list[str] = []
            if inspect.isasyncgen(result):
                async for item in result:
                    body, item_images = await _result_text(item)
                    if body:
                        texts.append(body)
                    images.extend(item_images)
            elif inspect.isawaitable(result):
                body, item_images = await _result_text(await result)
                if body:
                    texts.append(body)
                images.extend(item_images)
            else:
                body, item_images = await _result_text(result)
                if body:
                    texts.append(body)
                images.extend(item_images)
        except Exception as exc:
            # 指令跑挂时把"最后落在哪个文件哪一行"写进日志与返回值：
            # 报错来自别的插件（例如它拿到的参数类型不对）时，一眼能看出是谁的锅
            where = _exception_where(exc)
            detail = f"{type(exc).__name__}: {exc}"
            if where:
                detail += f"（{where}）"
            self.plugin.logger.warning(
                f"[virtual_world] 指令「{text}」执行失败：{detail}",
                exc_info=True,
            )
            return ToolCallResult(ok=False, error=detail, tool=word)
        finally:
            close_invocation()
            restore()
        for item in direct_results:
            body, item_images = await _result_text(item)
            if body and body not in texts:
                texts.append(body)
            images.extend(image for image in item_images if image not in images)
        texts = list(dict.fromkeys(texts))
        images = list(dict.fromkeys(images))
        captions, pending_images = await _caption_result_images(
            self.plugin, images, scene=text, session_id=session_id
        )
        if captions:
            texts.append("；".join(captions))
        body = "\n".join(part for part in texts if part).strip()
        if not body:
            body = (
                "（指令执行了，返回了一张图片）"
                if pending_images
                else f"（指令「{word}」执行了，但没有返回内容）"
            )
        return ToolCallResult(
            ok=True,
            text=body,
            tool=word,
            image_urls=pending_images,
            # 原图另存一份：转述成功时 image_urls 会空，但这张图还是得发出去
            attachments=list(images),
        )

    def _find(self, text: str) -> tuple[Any, Any] | None:
        """按指令名找处理器（含别名）；跳过本插件自己的指令，避免绕回自己。"""

        try:
            from astrbot.core.star.star_handler import (
                EventType,
                star_handlers_registry,
            )
        except Exception:
            return None
        try:
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent
            )
        except Exception:
            return None
        for record in handlers:
            module = str(getattr(record, "handler_module_path", "") or "")
            if PLUGIN_NAME in module:
                continue
            for event_filter in list(getattr(record, "event_filters", []) or []):
                names = getattr(event_filter, "get_complete_command_names", None)
                if not callable(names):
                    continue
                try:
                    candidates = [str(name) for name in names()]
                except Exception:
                    continue
                for name in candidates:
                    if event_filter.equals(text) or text.split()[0].lstrip("/") == name.lstrip("/"):
                        return record, event_filter
        return None

    @staticmethod
    def _params(record: Any, command_filter: Any, text: str) -> dict[str, Any]:
        """按处理器签名解析参数（和 AstrBot 指令分发同一套转换）。

        参数类型优先取 ``CommandFilter.handler_params``——那是 AstrBot 自己解析好的
        （它用 ``inspect.signature(..., eval_str=True)``，会把
        ``from __future__ import annotations`` 留下的**字符串注解**还原成真类型）。

        以前这里是自己再 inspect 一遍、**没有 eval_str**：注解是字符串时，
        `validate_and_convert_params` 会把它当成"默认值"原样透传，
        于是需要 ``MessageChain`` 这类对象的指令收到一个 str，接着就报
        ``'str' object has no attribute 'chain'``。
        """

        param_type = dict(getattr(command_filter, "handler_params", None) or {})
        if not param_type:
            try:
                signature = inspect.signature(record.handler, eval_str=True)
            except (TypeError, ValueError):
                signature = inspect.signature(record.handler)
            for name, parameter in signature.parameters.items():
                if name in ("self", "event"):
                    continue
                param_type[name] = (
                    parameter.annotation
                    if parameter.annotation is not inspect.Parameter.empty
                    else parameter.default
                )
        if not param_type:
            return {}
        args = text.split()[1:]
        return command_filter.validate_and_convert_params(args, param_type)

    @staticmethod
    def _swap_text(event: Any, text: str):
        """临时把事件的消息文本换成指令本身，返回还原用的回调。"""

        message_obj = getattr(event, "message_obj", None)
        previous_str = getattr(event, "message_str", None)
        previous_obj_str = getattr(message_obj, "message_str", None)
        previous_chain = getattr(message_obj, "message", None)
        try:
            event.message_str = text
        except Exception:
            pass
        if message_obj is not None:
            try:
                message_obj.message_str = text
                message_obj.message = [Plain(text=text)]
            except Exception:
                pass

        def restore() -> None:
            if previous_str is not None:
                try:
                    event.message_str = previous_str
                except Exception:
                    pass
            if message_obj is not None:
                if previous_obj_str is not None:
                    try:
                        message_obj.message_str = previous_obj_str
                    except Exception:
                        pass
                if previous_chain is not None:
                    try:
                        message_obj.message = previous_chain
                    except Exception:
                        pass

        return restore

class AstrBotMessenger:
    """MessagePort 实现。"""

    # 发送失败后，这个会话多少秒内不再尝试发送。
    # 协议端（NapCat）掉线/卡住时，一次发送可能要等 AstrBot 那边的超时才失败（默认 180 秒），
    # 期间不断重试只会把更多消息塞进协议端队列，恢复后一次性喷出来刷屏。
    SEND_FAIL_COOLDOWN_SECONDS = 180
    # 好友名单缓存多久（秒）：好友直接私聊，非好友才走群临时会话
    FRIEND_CACHE_SECONDS = 300.0
    GROUP_DISCOVERY_SECONDS = 600.0

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin
        self._blocked_until: dict[str, float] = {}
        self._fail_notes: dict[str, str] = {}
        # 想私聊的人 -> {user_id, group_id}（用户 m31460：群临时会话要带上群号）。
        # 由引擎在"这个人是谁"解析出来时填进来；这里只负责用。
        self._person_hints: dict[str, dict[str, Any]] = {}
        # 好友名单缓存：好友能直接私聊，非好友才需要群临时会话（用户 m05766 的顺序）
        self._friend_cache: dict[str, tuple[float, set[str]]] = {}
        # 临时会话每个群为什么没成（诊断用：失败原因要能看见，不能只剩一句空话）
        self._temp_fail_notes: dict[str, str] = {}
        # 适配器直发私聊失败的原因：和群临时会话的原因分开记，报错时才说得清
        self._direct_fail_notes: dict[str, str] = {}
        # 平台侧查到的"这个号在哪些群"（档案里没有群号时的兜底），十分钟缓存
        self._group_discovery: dict[str, tuple[float, list[str]]] = {}

    # ---------------- 群临时会话 / 群里 @ 人 ----------------

    def note_person_hint(
        self,
        session_id: str,
        *,
        user_id: Any = "",
        group_id: Any = "",
        groups: Any = None,
    ) -> None:
        """记下"这个私聊会话对应哪个 QQ、在哪个群见过他"。

        群临时会话（没加好友也能私聊）在 OneBot 里是
        ``send_private_msg(user_id=…, group_id=…)``：**群号是必需的**，没有它就只能
        走普通私聊（非好友通常发不出去）。所以引擎解析出这个人时顺手记一笔。
        """

        target = str(session_id or "").strip()
        uid = str(user_id or "").strip()
        if not target or not uid:
            return
        candidates: list[str] = []
        for item in list(groups or ()):
            text = str(item or "").strip()
            if text and text not in candidates:
                candidates.append(text)
        gid = str(group_id or "").strip()
        if gid and gid not in candidates:
            candidates.insert(0, gid)
        hint: dict[str, Any] = {"user_id": uid}
        if gid:
            hint["group_id"] = gid
        if candidates:
            hint["groups"] = candidates
        self._person_hints[target] = hint

    def person_hint(self, session_id: str) -> dict[str, Any]:
        return dict(self._person_hints.get(str(session_id or ""), {}))

    async def _friend_ids(self, session_id: str) -> set[str] | None:
        """他的好友名单（None = 问不到）。

        好友能直接私聊，非好友才需要"从共同群发起临时会话"（用户 m05766 的顺序）。
        名单五分钟缓存一次：每发一句都拉一遍好友列表太重。
        """

        platform_id = str(session_id or "").split(":", 1)[0]
        if not platform_id:
            return None
        cached = self._friend_cache.get(platform_id)
        now = time.time()
        if cached is not None and now - cached[0] < self.FRIEND_CACHE_SECONDS:
            return cached[1]
        get_inst = getattr(self.plugin.context, "get_platform_inst", None)
        if get_inst is None:
            return None
        try:
            inst = get_inst(platform_id)
        except Exception:
            return None
        call = getattr(getattr(inst, "bot", None), "call_action", None)
        if not callable(call):
            return None
        try:
            rows = await call("get_friend_list")
        except Exception as exc:  # noqa: BLE001 - 不是 QQ / 协议端不认：当作问不到
            self.plugin.logger.debug(
                f"[virtual_world] 拉好友列表失败（{platform_id}）：{type(exc).__name__}: {exc}"
            )
            return None
        ids: set[str] = set()
        for row in rows or ():
            if isinstance(row, dict):
                uid = str(row.get("user_id") or row.get("uin") or "").strip()
            else:
                uid = str(getattr(row, "user_id", "") or "").strip()
            if uid:
                ids.add(uid)
        self._friend_cache[platform_id] = (now, ids)
        return ids

    async def _looks_like_friend(self, session_id: str) -> bool:
        """这个私聊对象是不是好友（问不到就当作不是：照样试临时会话）。"""

        hint = self.person_hint(session_id)
        uid = str(hint.get("user_id") or "").strip()
        if not uid:
            return False
        friends = await self._friend_ids(session_id)
        if friends is None:
            return False
        return uid in friends

    async def _discover_common_groups(
        self, session_id: str, uid: str, *, limit: int = 30
    ) -> list[str]:
        """档案里没有共同群时，问平台：这个号在哪些群里说过话。

        临时会话必须要群号（OneBot 的 ``send_private_msg(user_id=…, group_id=…)``）。
        人物档案里没有群号时，以前就直接放弃了；现在退一步问平台：
        ``get_group_list`` 拿到号所在的群，逐个 ``get_group_member_info`` 试，
        命中即认为"共同群"。结果缓存十分钟，失败就当作没有。
        """

        uid = str(uid or "").strip()
        if not uid:
            return []
        cached = self._group_discovery.get(uid)
        now = time.time()
        if cached is not None and now - cached[0] < self.GROUP_DISCOVERY_SECONDS:
            return list(cached[1])
        platform_id = str(session_id or "").split(":", 1)[0]
        get_inst = getattr(self.plugin.context, "get_platform_inst", None)
        if get_inst is None:
            return []
        try:
            inst = get_inst(platform_id)
        except Exception:
            return []
        call = getattr(getattr(inst, "bot", None), "call_action", None)
        if not callable(call):
            return []
        try:
            rows = await call("get_group_list")
        except Exception as exc:  # noqa: BLE001 - 不是 QQ / 协议端不认
            self.plugin.logger.debug(
                f"[virtual_world] 拉群列表失败（{platform_id}）：{type(exc).__name__}: {exc}"
            )
            return []
        found: list[str] = []
        for row in list(rows or ())[:limit]:
            if isinstance(row, dict):
                gid = str(row.get("group_id") or row.get("group") or "").strip()
            else:
                gid = str(getattr(row, "group_id", "") or "").strip()
            if not gid or gid in found:
                continue
            try:
                await call(
                    "get_group_member_info",
                    group_id=int(gid) if gid.isdigit() else gid,
                    user_id=int(uid) if uid.isdigit() else uid,
                    no_cache=True,
                )
            except Exception:  # noqa: BLE001 - 不在这个群
                continue
            found.append(gid)
        self._group_discovery[uid] = (now, found)
        if found:
            self.plugin.logger.info(f"[virtual_world] 平台侧查到 {uid} 在群 {found}")
        return found

    def _umo_user_id(self, session_id: str) -> str:
        """从会话 id 末尾抄出 QQ 号（`mika:FriendMessage:997017474` → 997017474）。

        2026-10-29 实机：那个私聊 umo 在通讯录里没有档案，所以 `_person_hints`
        里一条都没有，`_temp_session_call()` 直接返回 None——"从共同群发临时会话"
        这条路连尝试的机会都没有。号码本身就在 umo 里，不该再依赖提示。
        """

        parts = str(session_id or "").split(":")
        if len(parts) < 3 or "friend" not in parts[1].lower():
            return ""
        tail = parts[-1].strip()
        return tail if tail.isdigit() else ""

    async def _temp_session_call(
        self, session_id: str
    ) -> tuple[Any, dict[str, Any]] | None:
        """拿到"能发临时会话"的那个协议端调用（拿不到就 None）。

        走宿主的平台适配器：``context.get_platform_inst(platform_id).bot``。
        适配器不一定是 aiocqhttp（webchat / 别的平台没有这个概念），所以全程
        ``getattr`` + 失败返回 None，绝不因为"不是 QQ"报错。
        """

        hint = self.person_hint(session_id)
        # 只要知道"这是谁"就够：群号可以在这里没有——`_send_temp_session` 会先问
        # 平台（`_discover_common_groups`）再试。以前要求"必须有群号"，
        # 结果档案里没群号的人一次都没尝试过临时会话（2026-10-29 实机）。
        if not hint.get("user_id"):
            uid = self._umo_user_id(session_id)
            if not uid:
                return None
            hint = {"user_id": uid}
        platform_id = str(session_id or "").split(":", 1)[0]
        get_inst = getattr(self.plugin.context, "get_platform_inst", None)
        if get_inst is None:
            return None
        try:
            inst = get_inst(platform_id)
        except Exception:
            return None
        bot = getattr(inst, "bot", None)
        call = getattr(bot, "call_action", None)
        if not callable(call):
            return None
        return call, hint

    async def _send_temp_session(
        self, session_id: str, texts: list[str]
    ) -> SendReceipt | None:
        """先试群临时会话；发不出去就返回 None，让调用方退回普通私聊。"""

        found = await self._temp_session_call(session_id)
        if found is None:
            return None
        call, hint = found
        uid = str(hint.get("user_id") or "")
        candidates: list[str] = []
        for item in list(hint.get("groups") or ()):
            text = str(item or "").strip()
            if text and text not in candidates:
                candidates.append(text)
        single = str(hint.get("group_id") or "").strip()
        if single and single not in candidates:
            candidates.insert(0, single)
        if not uid:
            return None
        if not candidates:
            # 档案里没有共同群：问平台这个号在哪些群，再挨个试（用户 m06694）
            candidates = await self._discover_common_groups(session_id, uid)
        if not candidates:
            return None

        def _num(value: str) -> Any:
            return int(value) if value.isdigit() else value

        # 群主可能把"临时会话"关了：一个群不行就换下一个共同群（用户 m05766）
        fail_notes: list[str] = []
        for gid in candidates:
            sent_ids: list[str] = []
            failed = False
            for text in texts:
                segment = [{"type": "text", "text": str(text)}]
                try:
                    result = await call(
                        "send_private_msg",
                        user_id=_num(uid),
                        group_id=_num(gid),
                        message=segment,
                    )
                except Exception as exc:  # noqa: BLE001 - 换下一个群再试
                    note = f"{type(exc).__name__}: {exc}".strip()
                    # 不同协议端对"群临时会话"的接口名不一样：再试一次 send_msg
                    # （message_type=private + group_id），失败原因记下来给人看
                    try:
                        result = await call(
                            "send_msg",
                            message_type="private",
                            user_id=_num(uid),
                            group_id=_num(gid),
                            message=segment,
                        )
                    except Exception as exc2:  # noqa: BLE001 - 换下一个群再试
                        fail_notes.append(
                            f"群 {gid}：{note or 'send_private_msg 不通'}"
                            f"；send_msg：{type(exc2).__name__}: {exc2}"
                        )
                        self.plugin.logger.info(
                            f"[virtual_world] 群 {gid} 的临时会话不通：{note} / {exc2}"
                        )
                        failed = True
                        break
                    else:
                        self.plugin.logger.info(
                            f"[virtual_world] 群 {gid} 用 send_msg 走了临时会话（{note}）"
                        )
                message_id = ""
                if isinstance(result, dict):
                    message_id = str(result.get("message_id") or "")
                if message_id:
                    sent_ids.append(message_id)
                else:
                    fail_notes.append(f"群 {gid}：平台没有返回 message_id")
            if failed or len(sent_ids) < len(texts):
                continue
            self._temp_fail_notes.pop(session_id, None)
            self.plugin.logger.info(
                f"[virtual_world] 用群临时会话发给了 {uid}（群 {gid}）：{len(texts)} 段"
            )
            return SendReceipt(
                target=session_id,
                status=SEND_ACCEPTED,
                platform_message_ids=sent_ids,
                segments=len(texts),
                sent_segments=len(texts),
                # 这一条走的是**群临时会话**：引擎据此把临时会话的 source 记成
                # group_temp（用户 m00001 需求 11）。
                via_temp=True,
            )
        # 全都不成：把每个群的原因留给 send_text，写成"发不出去"的具体原因
        self._temp_fail_notes[session_id] = "；".join(fail_notes) or "没有可用的共同群"
        return None

    async def _revive_temp_sessions(self, session_ids: list[str] | None = None) -> int:
        """把 ended / expired 的临时会话复位成 active（审查 H2 的第二个出口）。

        用户把会话重新启用时（``/vw session enable``、编辑器保存白名单）必须跟着
        做，否则会出现"白名单启用着、她的钩子却把消息全吞了"这种自相矛盾的状态。
        """

        try:
            return await self.engine.revive_temp_sessions(session_ids)
        except Exception as exc:  # noqa: BLE001 - 复位失败不连累启用动作
            self.logger.debug(f"[virtual_world] 临时会话没复位：{exc}")
            return 0

    async def send_mention(
        self, session_id: str, user_id: Any, text: str
    ) -> SendReceipt:
        """在群里 @ 某个人说一句（不依赖好友，也不依赖私聊能不能发）。"""

        uid = str(user_id or "").strip()
        body = str(text or "").strip()
        if not uid or not body:
            return SendReceipt(target=session_id, status=SEND_FAILED, error="没写要说什么")
        if self.blocked(session_id):
            return SendReceipt(
                target=session_id, status=SEND_FAILED, error="刚失败过，这一批不再尝试"
            )
        try:
            chain = MessageChain(chain=[At(qq=int(uid) if uid.isdigit() else uid), Plain(f" {body}")])
            result = await self.plugin.context.send_message(session_id, chain)
        except Exception as exc:
            self.mark_failed(session_id, exc)
            return SendReceipt(
                target=session_id, status=SEND_FAILED, error=f"{type(exc).__name__}: {exc}"
            )
        if not result:
            # 平台返回空 = 它没接手这条消息。原因在适配器那边（不支持这个 umo、
            # 临时会话没开、群主关了临时会话…），这里至少要说清"是平台没收"，
            # 别让它看起来像插件自己放弃了。
            return SendReceipt(
                target=session_id,
                status=SEND_FAILED,
                error="平台没有接收（适配器返回空：多半是这个会话不接受私聊，或者临时会话没开）",
            )
        self.mark_sent(session_id)
        return SendReceipt(
            target=session_id,
            status=SEND_ACCEPTED,
            platform_message_ids=_result_message_ids(result),
            segments=1,
            sent_segments=1,
        )

    # ---------------- 失败冷却（不重试） ----------------

    def blocked(self, session_id: str) -> bool:
        """这个会话现在是不是处于"刚发送失败"的冷却里。"""

        return self._blocked_until.get(session_id, 0.0) > time.time()

    def blocked_seconds(self, session_id: str) -> int:
        """还要等多少秒才会重新尝试发送（没在冷却里就是 0）。"""

        remain = self._blocked_until.get(session_id, 0.0) - time.time()
        return max(0, int(remain))

    def take_fail_note(self, session_id: str) -> str:
        """取走一次「发送失败」的说明（每个冷却窗口只会有一条）。"""

        return self._fail_notes.pop(session_id, "")

    def mark_sent(self, session_id: str) -> None:
        self._blocked_until.pop(session_id, None)
        self._fail_notes.pop(session_id, None)

    def mark_failed(self, session_id: str, exc: Any) -> None:
        """记一次失败：本会话进入冷却，日志每个窗口只打一条。"""

        self._blocked_until[session_id] = time.time() + self.SEND_FAIL_COOLDOWN_SECONDS
        note = f"{type(exc).__name__}: {exc}"
        if session_id in self._fail_notes:
            return
        self._fail_notes[session_id] = note
        self.plugin.logger.warning(
            f"[virtual_world] 发送失败，{self.SEND_FAIL_COOLDOWN_SECONDS} 秒内不再尝试"
            f"（失败就失败，不重发）：{note}"
        )

    def _platform_ids(self) -> list[str]:
        """宿主注册了哪些平台实例（诊断用：umo 前缀对不上时能说清是谁）。"""

        manager = getattr(self.plugin.context, "platform_manager", None)
        insts = list(getattr(manager, "platform_insts", ()) or ())
        out: list[str] = []
        for inst in insts:
            try:
                meta = inst.meta()
            except Exception:  # noqa: BLE001 - 适配器没实现 meta
                continue
            pid = str(getattr(meta, "id", "") or "")
            if pid:
                out.append(pid)
        return out

    def _adapter_for_private(self, session_id: str) -> Any:
        """找一个能给这个号发私聊的适配器（先按 umo 前缀，再按平台类型兜底）。

        `context.send_message()` 只在 **umo 前缀与平台实例 id 完全一致** 时才投递，
        否则直接返回 False（宿主日志：``cannot find platform for session ...``）。
        实机上出现过 ``mika:FriendMessage:997017474`` 这种前缀对不上的情况——
        消息根本没离开插件，却看起来像"平台拒收"。
        """

        platform = str(session_id or "").split(":", 1)[0].strip()
        get_inst = getattr(self.plugin.context, "get_platform_inst", None)
        if get_inst is not None and platform:
            try:
                inst = get_inst(platform)
            except Exception:  # noqa: BLE001 - 找不到就当没有
                inst = None
            if inst is not None and callable(
                getattr(getattr(inst, "bot", None), "call_action", None)
            ):
                return inst
        manager = getattr(self.plugin.context, "platform_manager", None)
        for inst in list(getattr(manager, "platform_insts", ()) or ()):
            bot = getattr(inst, "bot", None)
            if not callable(getattr(bot, "call_action", None)):
                continue
            try:
                name = str(inst.meta().name or "").lower()
            except Exception:  # noqa: BLE001
                name = ""
            if name in ("aiocqhttp", "qq", "onebot", "napcat", "llonebot"):
                return inst
        return None

    async def _send_private_direct(
        self, session_id: str, texts: list[str]
    ) -> SendReceipt | None:
        """绕过 ``context.send_message`` 的 umo 前缀匹配，直接让适配器发私聊。

        什么时候用：宿主返回 False（前缀对不上）或适配器静默丢掉时。
        走的是 OneBot 的 ``send_private_msg``；不通再试 ``send_msg``。
        """

        platform = str(session_id or "").split(":", 1)[0].strip()
        get_inst = getattr(self.plugin.context, "get_platform_inst", None)
        if platform and get_inst is not None:
            try:
                matched = get_inst(platform)
            except Exception:  # noqa: BLE001 - 查不到就继续走兜底
                matched = None
            if matched is not None:
                # 前缀能对上平台实例：宿主会投递，轮不到我们绕过它。
                # （适配器自己静默丢掉的情况由"再试群临时会话"那条路兜）
                return None
        hint = self.person_hint(session_id)
        uid = str(hint.get("user_id") or "") or self._umo_user_id(session_id)
        if not uid:
            return None
        inst = self._adapter_for_private(session_id)
        if inst is None:
            return None
        call = inst.bot.call_action
        sent: list[str] = []
        errors: list[str] = []
        for text in texts:
            payload = {"user_id": int(uid) if uid.isdigit() else uid,
                       "message": [{"type": "text", "text": str(text)}]}
            result = None
            try:
                result = await call("send_private_msg", **payload)
            except Exception as exc:  # noqa: BLE001 - 换接口再试
                errors.append(f"send_private_msg: {type(exc).__name__}: {exc}")
                try:
                    result = await call(
                        "send_msg", message_type="private", **payload
                    )
                except Exception as exc2:  # noqa: BLE001 - 两条都不通
                    errors.append(f"send_msg: {type(exc2).__name__}: {exc2}")
                    break
            message_id = ""
            if isinstance(result, dict):
                message_id = str(result.get("message_id") or "")
            if message_id:
                sent.append(message_id)
        if not sent:
            if errors:
                self.plugin.logger.info(
                    "[virtual_world] 适配器直发私聊也没成：" + "；".join(errors)
                )
                self._direct_fail_notes[session_id] = "；".join(errors)
            return None
        self.plugin.logger.info(
            f"[virtual_world] 适配器直发私聊成功：{uid}（{len(sent)} 段）"
        )
        return SendReceipt(
            target=session_id,
            status=SEND_ACCEPTED,
            platform_message_ids=sent,
            segments=len(texts),
            sent_segments=len(sent),
        )

    async def send_text(self, session_id: str, messages: list[str]) -> SendReceipt:
        texts = [m for m in messages if m and str(m).strip()]
        if not texts:
            return SendReceipt(
                target=session_id, status=SEND_FAILED, segments=0, error="没有要发的内容"
            )
        if self.blocked(session_id):
            # 刚失败过：直接放弃这一批，连平台都不碰
            return SendReceipt(
                target=session_id,
                status=SEND_FAILED,
                segments=len(texts),
                error="刚失败过，这一批不再尝试",
            )
        # 落点顺序（用户 m05766）：先看这个人是谁 -> 是不是好友。
        # 好友直接普通私聊；非好友（或问不到好友名单）才试"从共同群发起临时会话"，
        # 一个群不通就换下一个共同群，全不通才退回普通私聊。
        temp = None
        if not await self._looks_like_friend(session_id):
            temp = await self._send_temp_session(session_id, texts)
        if temp is not None:
            return temp
        # 一条消息一个消息链：平台侧才会显示成多条（分段回复）。
        # 全部塞进同一个 chain 的话，多数平台会拼成一条发出去。
        # 分段是分别投递的，所以"哪几段真的出去了"要分开记：
        # 否则"第一段成功、第二段失败"会被整批记成成功（计划 §5 A2）。
        sent_ids: list[str] = []
        sent_segments = 0
        error = ""
        operation_id = secrets.token_hex(16)
        segment_receipts = [{"segment_id": f"{operation_id}:{i}", "index": i, "status": SEND_FAILED} for i in range(len(texts))]
        for index, text in enumerate(texts):
            try:
                result = await self.plugin.context.send_message(
                    session_id, MessageChain(chain=[Plain(text=str(text))])
                )
            except Exception as exc:
                # 失败就失败：不重试、不补发，后面的几条也不再试（平台已经不通了）
                self.mark_failed(session_id, exc)
                error = f"{type(exc).__name__}: {exc}"
                break
            if result:
                sent_segments += 1
                ids = _result_message_ids(result)
                sent_ids.extend(ids)
                segment_receipts[index].update(status=SEND_ACCEPTED, platform_message_ids=ids)
            elif not error:
                error = f"第 {index + 1} 段平台没有接收"
            if index == len(texts) - 1:
                break  # 最后一条不用再等
            delay = self.plugin.typing_delay_for(text)
            if delay > 0:
                # 分段之间停一下，群里看起来像她在一句句打字
                await asyncio.sleep(delay)
        if not sent_segments and ":FriendMessage:" in str(session_id):
            # umo 前缀对不上任何平台实例时，`context.send_message()` 直接返回
            # False（消息压根没出去）。这时让适配器按 QQ 号直发一次。
            direct = await self._send_private_direct(session_id, texts)
            if direct is not None:
                self.mark_sent(session_id)
                return direct
            self.plugin.logger.warning(
                "[virtual_world] 私聊发不出去：umo=%s，已注册的平台=%s"
                % (session_id, self._platform_ids())
            )
        if not sent_segments and temp is None:
            # 好友名单可能问错（协议端拿不到名单、或临时会话权限更宽）：
            # 普通私聊被平台静默丢了，就再从共同群试一次（用户 m06694 的实测：
            # QQ 侧发起群临时会话是通的）。
            retry = await self._send_temp_session(session_id, texts)
            if retry is not None:
                if retry.sent_segments:
                    self.mark_sent(session_id)
                return retry
        if sent_segments:
            self.mark_sent(session_id)
        partial = 0 < sent_segments < len(texts)
        base_error = error or "平台没有接收"
        if not sent_segments:
            direct_note = self._direct_fail_notes.pop(session_id, "")
            if direct_note:
                base_error = f"{base_error}（适配器直发：{direct_note}）"
            temp_note = self._temp_fail_notes.pop(session_id, "")
            if temp_note:
                base_error = f"{base_error}（群临时会话：{temp_note}）"
        return SendReceipt(
            target=session_id,
            status=SEND_ACCEPTED if sent_segments else SEND_FAILED,
            platform_message_ids=sent_ids,
            error="" if sent_segments and not partial else base_error,
            segments=len(texts),
            sent_segments=sent_segments,
            partial=partial,
            operation_id=operation_id,
            segment_receipts=segment_receipts,
        )

    async def send_images(self, session_id: str, images: list[str]) -> SendReceipt:
        """把工具 / 指令生成出来的图片发到会话里。

        多张图放**同一条消息**：群里显示成一组，不会被拆成刷屏的连发。
        地址可以是 http、file://、base64://，AstrBot 发送时会自己转成平台要的格式。
        """

        refs = _sendable_images(images)
        if not refs:
            return SendReceipt(
                target=session_id, status=SEND_FAILED, segments=0, error="没有可发送的图片"
            )
        if self.blocked(session_id):
            # 刚失败过：连同图片一起放弃这一批
            return SendReceipt(
                target=session_id,
                status=SEND_FAILED,
                segments=1,
                attachments=len(refs),
                error="刚失败过，这一批不再尝试",
            )
        chain = [Image(file=ref) for ref in refs]
        try:
            result = await self.plugin.context.send_message(
                session_id, MessageChain(chain=chain)
            )
        except Exception as exc:
            self.mark_failed(session_id, exc)
            return SendReceipt(
                target=session_id,
                status=SEND_FAILED,
                segments=1,
                attachments=len(refs),
                error=f"{type(exc).__name__}: {exc}",
            )
        if result:
            self.mark_sent(session_id)
        return SendReceipt(
            target=session_id,
            status=SEND_ACCEPTED if result else SEND_FAILED,
            platform_message_ids=_result_message_ids(result),
            error="" if result else "平台没有接收这批图片",
            segments=1,
            sent_segments=1 if result else 0,
            attachments=len(refs),
        )

    async def send_record(self, session_id: str, audios: list[str]) -> SendReceipt:
        """把语音发到会话里（合成语音的工具 / 指令走这条路，用户 m20677）。

        一条消息一条语音；平台不认 ``Record`` 时退成"当文件发"，至少内容还能听到。
        """

        refs = [str(item or "").strip() for item in (audios or [])]
        refs = [item for item in refs if item]
        if not refs:
            return SendReceipt(
                target=session_id, status=SEND_FAILED, segments=0, error="没有可发送的语音"
            )
        if self.blocked(session_id):
            return SendReceipt(
                target=session_id,
                status=SEND_FAILED,
                segments=1,
                attachments=len(refs),
                error="刚失败过，这一批不再尝试",
            )
        sent = 0
        ids: list[str] = []
        error = ""
        for ref in refs:
            chain = MessageChain(chain=[Record(file=ref)])
            try:
                result = await self.plugin.context.send_message(session_id, chain)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                result = None
            if not result:
                # 平台不认 Record（或文件不在）：退成当文件发，别把这一段丢在地上
                fallback = await self.send_file(session_id, ref)
                if getattr(fallback, "status", "") == SEND_ACCEPTED:
                    sent += 1
                else:
                    error = error or str(getattr(fallback, "error", "") or "平台没有接收这段语音")
                continue
            sent += 1
            ids.extend(_result_message_ids(result))
        if sent:
            self.mark_sent(session_id)
        return SendReceipt(
            target=session_id,
            status=SEND_ACCEPTED if sent else SEND_FAILED,
            platform_message_ids=ids,
            error="" if sent else (error or "平台没有接收这段语音"),
            segments=len(refs),
            sent_segments=sent,
            attachments=len(refs),
        )

    async def send_file(
        self, session_id: str, path: str, *, name: str = "", mime: str = ""
    ) -> SendReceipt:
        """把**一个本机文件**当附件发出去（B4 生成的产物走这条路）。

        契约见 ``core/ports.py:MessagePort.send_file``。这里做三件事：
        路径与存在性先自己查（不存在就如实回失败，绝不发空消息）、文件名/类型按需补齐、
        返回值与 :meth:`send_images` 同一套口径（accepted 不等于 delivered）。
        """

        target = str(path or "").strip()
        if not target:
            return SendReceipt(target=session_id, status=SEND_FAILED, segments=0, error="没有给文件路径")
        candidate = Path(target)
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        if not is_file:
            return SendReceipt(
                target=session_id,
                status=SEND_FAILED,
                segments=1,
                error=f"文件不在了：{candidate.name}",
            )
        shown = str(name or "").strip() or candidate.name
        kind = str(mime or "").strip() or _guess_mime(shown)
        if self.blocked(session_id):
            return SendReceipt(
                target=session_id,
                status=SEND_FAILED,
                segments=1,
                attachments=1,
                error="刚失败过，这一批不再尝试",
            )
        chain = [File(name=shown, file=str(candidate), url="")]
        try:
            result = await self.plugin.context.send_message(
                session_id, MessageChain(chain=chain)
            )
        except Exception as exc:  # 平台拒绝、路径不可读、协议不支持……都从这里出来
            self.mark_failed(session_id, exc)
            return SendReceipt(
                target=session_id,
                status=SEND_FAILED,
                segments=1,
                attachments=1,
                error=f"{type(exc).__name__}: {exc}",
            )
        if result:
            self.mark_sent(session_id)
        return SendReceipt(
            target=session_id,
            status=SEND_ACCEPTED if result else SEND_FAILED,
            platform_message_ids=_result_message_ids(result),
            error="" if result else "平台没有接收这个文件",
            segments=1,
            sent_segments=1 if result else 0,
            attachments=1,
        )

    async def set_group_card(self, session_id: str, card: str) -> CardResult:
        """设置群名片。只有 aiocqhttp（OneBot）这类支持 call_action 的平台才生效。"""

        if not card:
            return CardResult(ok=False, reason="算出来的名片是空的")
        if ":GroupMessage:" not in session_id:
            return CardResult(ok=False, reason="这不是群聊，没有群名片")
        event = self.plugin.last_event(session_id)
        if event is None:
            # 这个群还没来过消息时，拿别处收到过的事件取机器人句柄也行——
            # 句柄是同一个 Bot，真正决定改哪个群的还是上面的 session_id。
            event = self.plugin.last_event_any()
        if event is None:
            return CardResult(ok=False, reason="还没收到过这个群的消息，拿不到机器人句柄")
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return CardResult(ok=False, reason="当前平台不支持改群名片（需要 OneBot/aiocqhttp）")
        group_id = session_id.split(":")[-1]
        self_id = event.get_self_id()
        try:
            await bot.call_action(
                "set_group_card",
                group_id=int(group_id) if str(group_id).isdigit() else group_id,
                user_id=int(self_id) if str(self_id).isdigit() else self_id,
                card=card,
            )
            return CardResult(ok=True, card=card)
        except Exception as exc:
            # 常见原因：机器人不是群管理/群主、群号不对、协议端不支持这个接口
            # 退避由引擎负责，这里只留 debug 日志，避免协议端挂掉时每分钟刷一条
            self.plugin.logger.debug(f"[virtual_world] 设置群名片失败：{exc}")
            return CardResult(ok=False, reason=f"平台拒绝了：{exc}", card=card)

    async def fetch_group_card(self, session_id: str) -> str:
        """读她当前在群里的名片，用作"原名"的兜底。"""

        if ":GroupMessage:" not in session_id:
            return ""
        event = self.plugin.last_event(session_id)
        if event is None:
            return ""
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return ""
        group_id = session_id.split(":")[-1]
        self_id = event.get_self_id()
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id) if str(group_id).isdigit() else group_id,
                user_id=int(self_id) if str(self_id).isdigit() else self_id,
                no_cache=True,
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 读取群名片失败：{exc}")
            return ""
        if not isinstance(info, dict):
            return ""
        return str(info.get("card") or info.get("nickname") or "").strip()

    async def poke(self, session_id: str, user_id: str) -> PokeResult:
        """戳一戳某个人。

        群聊和私聊走的是**两条不同的路**：

        - **群聊**：先发 OneBot 的 ``poke`` 消息段（群里的标准做法），不通再退到
          ``send_poke`` / ``group_poke`` 接口；
        - **私聊**：``poke`` 消息段是给群聊用的，私聊里发出去客户端会显示成一个认不出的
          占位（红叉破图），所以这里**不试消息段**，直接调 ``friend_poke``。

        每次都会把"走了哪条路"记进 ``PokeResult.route``：两条都不通就把原因带回去，
        她会退化成一句话说，而日志里要能看出究竟是为什么。
        """

        target = str(user_id or "").strip()
        if not target:
            return PokeResult(False, "没有指定要戳谁")
        if self.blocked(session_id):
            return PokeResult(False, "上一次发送失败，正在冷却")
        group_id = ""
        parts = str(session_id).split(":")
        if len(parts) >= 3 and "group" in parts[1].lower():
            group_id = parts[-1]
        errors: list[str] = []
        # 路线一：消息段。**只在群聊用**——私聊里这个段协议端不认。
        if group_id:
            try:
                from astrbot.api.message_components import Poke

                await self.plugin.context.send_message(
                    session_id, MessageChain(chain=[Poke(id=target)])
                )
                return PokeResult(True, route="poke 消息段")
            except Exception as exc:
                errors.append(f"消息段：{type(exc).__name__}: {exc}")
        # 路线二：直接调协议端接口（私聊只有这一条：friend_poke）
        event = getattr(self.plugin, "_last_events", {}).get(session_id)
        bot = getattr(event, "bot", None)
        if bot is not None:
            payload: dict[str, Any] = {
                "user_id": int(target) if target.isdigit() else target
            }
            if group_id.isdigit():
                payload["group_id"] = int(group_id)
            names = (
                ("send_poke", "group_poke")
                if group_id
                else ("friend_poke", "send_poke")
            )
            for name in names:
                try:
                    await bot.call_action(name, **payload)
                    return PokeResult(True, route=name)
                except Exception as exc:
                    errors.append(f"{name}：{type(exc).__name__}: {exc}")
        else:
            errors.append("拿不到协议端连接（这条会话还没收到过消息）")
        return PokeResult(False, "；".join(errors) or "没有可用的戳一戳通道")


class AstrBotTools:
    """ToolPort 实现。"""

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin

    def has_context(self) -> bool:
        """手上有没有一条真实消息事件：工具调用要拿它当上下文，没有就只能等。"""

        return self.plugin.last_event_any() is not None

    def list_tools(self) -> list[ToolInfo]:
        manager = self._manager()
        if manager is None:
            return []
        tools: list[ToolInfo] = []
        seen: set[str] = set()
        for tool in list(getattr(manager, "func_list", []) or []):
            name = getattr(tool, "name", "") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            tools.append(
                ToolInfo(
                    name=name,
                    description=str(getattr(tool, "description", "") or ""),
                    parameters=dict(getattr(tool, "parameters", {}) or {}),
                    source="plugin",
                    plugin=_tool_owner(tool),
                )
            )
        # AstrBot 自带的内置工具不在 func_list 里（官方明确把它们单独放），
        # 但搜索、知识库这些正好是最常用的，所以一并列出来，标成「官方」。
        for tool in self._builtin_tools(manager):
            name = getattr(tool, "name", "") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            tools.append(
                ToolInfo(
                    name=name,
                    description=str(getattr(tool, "description", "") or ""),
                    parameters=dict(getattr(tool, "parameters", {}) or {}),
                    source="official",
                    plugin=_tool_owner(tool),
                )
            )
        return tools

    @staticmethod
    def _builtin_tools(manager) -> list[Any]:
        """AstrBot 的内置工具列表；老版本没有这个接口时返回空。"""

        iterator = getattr(manager, "iter_builtin_tools", None)
        if not callable(iterator):
            return []
        try:
            return list(iterator() or [])
        except Exception:
            return []

    def _manager(self):
        try:
            return self.plugin.context.get_llm_tool_manager()
        except Exception:
            return None

    async def call_tool(
        self, name: str, params: dict[str, Any], session_id: str = ""
    ) -> ToolCallResult:
        """调用工具。返回结构化结果，失败时带上原因（会写进事件日志，方便排查）。"""

        manager = self._manager()
        if manager is None:
            return ToolCallResult(ok=False, error="拿不到 AstrBot 的工具管理器", tool=name)
        tool = None
        getter = getattr(manager, "get_func", None)
        if callable(getter):
            try:
                tool = getter(name)
            except Exception:
                tool = None
        if tool is None:
            for candidate in list(getattr(manager, "func_list", []) or []):
                if getattr(candidate, "name", "") == name:
                    tool = candidate
                    break
        if tool is None:
            # 没注册 = 环境没这个工具，不是她调错了：算 infrastructure，不扣心情。
            return ToolCallResult(
                ok=False,
                error=f"工具「{name}」没有注册",
                tool=name,
                infrastructure=True,
            )
        # 工具及钩子只使用明确执行目标自身的事件，缺失时不能借同组其他会话。
        event = self.plugin.nearby_event(session_id)
        if event is None:
            # 插件刚启动 / 刚重载、群里还没人说话时，一个真实事件都没有。
            # 这时候硬调只会让工具内部炸出「'NoneType' object has no attribute ...」，
            # 看不出到底为什么失败——直接说清楚，日志里一眼能懂。
            return ToolCallResult(
                ok=False,
                error=(
                    "刚启动还没收到过消息，工具需要一条消息当上下文；"
                    "等群里有人说一句，或者先让她做别的事"
                ),
                tool=name,
                # 工具一次都没被调用：这是环境没就绪，不是她动作失败。文案原样
                # 留给模型看（她得知道为什么没成，好改走别的动作），只是别拿它
                # 去扣心情、锁变奏闸。
                infrastructure=True,
            )
        call_params = self._filter_params(name, params, tool)
        # AstrBot 的工具其实有三种写法：@filter.llm_tool 的 handler、新版 call()、
        # 以及老的 run()。MCP 工具和新式工具都没有 handler，只能走 call()。
        # 这里跟 AstrBot 自己的执行器保持同一套优先级，否则只会报「没有可调用的 handler」。
        invoke = _tool_invoker(tool)
        if invoke is None:
            # 工具在册却没有可调用的入口：同样是环境问题，算 infrastructure。
            return ToolCallResult(
                ok=False,
                error=f"工具「{name}」没有可调用的 handler",
                tool=name,
                infrastructure=True,
            )
        # 借来的事件要指到这个会话上再调：工具可能自己往事件的会话发东西
        try:
            event, direct_results, close_invocation = _invocation_event(event, session_id)
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc), tool=name)
        try:
            # 工具前后也补一遍钩子：有些插件靠它们显示"正在用工具 / 用完了"
            await self.plugin._fire_hook(
                event, "OnUsingLLMToolEvent", tool, dict(call_params)
            )
            result = invoke(event, call_params, self.plugin)
            images: list[str] = []
            if inspect.isasyncgen(result):
                pieces: list[str] = []
                async for item in result:
                    body, item_images = await _result_text(item, tool=name)
                    if body and body not in pieces:
                        pieces.append(body)
                    images.extend(image for image in item_images if image not in images)
                text = "\n".join(pieces)
            elif inspect.isawaitable(result):
                text, images = await _result_text(await result, tool=name)
            else:
                text, images = await _result_text(result, tool=name)
            for item in direct_results:
                body, item_images = await _result_text(item, tool=name)
                if body and body not in text.split("\n"):
                    text = "\n".join(part for part in (text, body) if part)
                images.extend(image for image in item_images if image not in images)
            images = list(dict.fromkeys(images))
            await self.plugin._fire_hook(
                event,
                "OnLLMToolRespondEvent",
                tool,
                dict(call_params),
                _as_tool_result(text),
            )
        except Exception as exc:
            self.plugin.logger.warning(f"[virtual_world] 工具 {name} 调用失败：{exc}")
            # 工具的报错经常是「event 是 None」这种内部细节，补一句人话更好排查
            hint = ""
            if isinstance(exc, AttributeError) and "NoneType" in str(exc):
                hint = "（工具拿不到消息上下文：插件刚启动或刚重载过，等群里有人说一句再试）"
            return ToolCallResult(
                ok=False, error=f"调用出错：{exc}{hint}", tool=name, params=call_params
            )
        finally:
            close_invocation()
        for item in direct_results:
            body, item_images = await _result_text(item, tool=name)
            if body and body not in text.split("\n"):
                text = "\n".join(part for part in (text, body) if part)
            images.extend(image for image in item_images if image not in images)
        audios: list[str] = list(await _audio_refs(result))
        for item in direct_results:
            for ref in await _audio_refs(item):
                if ref not in audios:
                    audios.append(ref)
        captions, pending_images = await _caption_result_images(
            self.plugin, images, scene=str(name), session_id=session_id
        )
        if captions:
            text = "\n".join([part for part in (text, "；".join(captions)) if part])
        text = (text or "").strip()
        if audios and (not text or text.strip() in {"［Record］", "[Record]", "［File］"}):
            # 只回了一条语音：别让她读到 "［Record］" 这种占位，直接说清这是什么
            text = "（用语音说了一句）"
        if not text and not pending_images and not audios:
            return ToolCallResult(
                ok=False, error="工具返回了空结果", tool=name, params=call_params
            )
        if not text:
            text = "（工具返回了一张图片）" if pending_images else "（工具返回了一段语音）"
        return ToolCallResult(
            ok=True,
            text=text,
            tool=name,
            params=call_params,
            image_urls=pending_images,
            # 原图另存一份：转述成功时 image_urls 会空，但这张图还是得发到群里
            attachments=list(images),
            audios=audios,
        )

    def _filter_params(
        self, name: str, params: dict[str, Any], tool: Any = None
    ) -> dict[str, Any]:
        """只把工具自己声明过的参数传给它。

        参数是辅助模型按 schema 填的，难免多塞一两个没声明的键；
        直接透传会让工具的 handler 抛 TypeError，最后表现为"工具没返回任何东西"。
        """

        given = dict(params or {})
        if not given:
            return {}
        if tool is None:
            manager = self._manager()
            for candidate in list(getattr(manager, "func_list", []) or []):
                if getattr(candidate, "name", "") == name:
                    tool = candidate
                    break
        schema = dict(getattr(tool, "parameters", {}) or {})
        properties = dict(schema.get("properties") or {})
        if not properties:
            return given
        return {key: value for key, value in given.items() if key in properties}


class AstrBotPersona:
    """PersonaPort 实现。拿不到人格时降级为空字符串。"""

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin

    def _config(self):
        engine = getattr(self.plugin, "engine", None)
        world = getattr(engine, "world", None) if engine is not None else None
        return getattr(world, "persona", None)

    async def astrbot_persona_text(self, session_id: str) -> str:
        """AstrBot 给这个会话选的那份人格（按会话 / 配置文件解析）。"""

        manager = getattr(self.plugin.context, "persona_manager", None)
        if manager is None:
            return ""
        persona = None
        resolver = getattr(manager, "get_default_persona_v3", None)
        if callable(resolver):
            try:
                persona = await resolver(session_id)
            except Exception:
                persona = None
        if persona is None:
            return ""
        for field in ("prompt", "system_prompt", "text"):
            value = getattr(persona, field, None)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(persona, dict):
            for field in ("prompt", "system_prompt", "text"):
                value = persona.get(field)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    async def get_persona_text(self, session_id: str) -> str:
        """实际交给她的人设：按配置里的模式决定用哪一份。"""

        config = self._config()
        mode = str(getattr(config, "mode", "astrbot") or "astrbot")
        own = str(getattr(config, "text", "") or "").strip()
        if mode == "plugin" and own:
            return own
        base = await self.astrbot_persona_text(session_id)
        if mode == "append" and own:
            return f"{base}\n\n{own}".strip() if base.strip() else own
        # astrbot 模式，或者 plugin 模式但没填：都用 AstrBot 那份
        return base


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for attr in ("completion_text", "text", "message"):
        found = getattr(value, attr, None)
        if isinstance(found, str) and found:
            return found
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_result_text(value: Any) -> str:
    """把工具返回值变成文本。

    MCP 工具和 AstrBot 的部分内置工具返回的是 ``CallToolResult``（内容是若干 TextContent），
    直接 ``str()`` 会得到一串对象表示，这里把里面的文本挑出来。
    """

    if value is None:
        return ""
    content = getattr(value, "content", None)
    if isinstance(content, (list, tuple)) and content:
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if parts:
            return "\n".join(parts)
    return _stringify(value)


MAX_RESULT_CHARS = 800
#: 读网页这类工具一次会带回来上万字。800 字的上限是给"聊天里的工具回执"用的，
#: 套在正文抓取上等于只看到开头，整理时只能猜（用户 m14731 的「内容过长已截断」）。
#: 阅读工具单独放宽，抓下来之后由整理那一步再压。
MAX_PAGE_CHARS = 12000
#: 名字里带这些词的，按"阅读类工具"放宽截断。
READER_TOOL_HINTS = ("fetch", "read_web", "read_page", "read_url", "browse", "crawl", "webpage", "web_page")
"""工具 / 指令返回内容进提示词前的截断长度（日志里另有一份更短的）。"""


async def _chain_text_and_images(value: Any) -> tuple[str, list[str]] | None:
    """把 ``MessageChain`` / ``MessageEventResult`` 拆成「人话」和「图片地址」。

    直接 ``str()`` 会得到一坨 dataclass 表示：里面虽然夹着正文，但也塞满了
    ``Plain(type=...)`` 之类的噪音，既费 token，模型还容易照抄。这里按组件类型
    老老实实还原——文本归文本、@ 归 @、图片挑出来单独交给多模态模型看。

    返回 ``None`` 表示"这个值不是消息链"，交给别的分支处理。
    """

    chain = getattr(value, "chain", None)
    if not isinstance(chain, (list, tuple)):
        return None
    texts: list[str] = []
    images: list[str] = []
    for component in chain:
        if _component_named(component, "Plain"):
            text = str(getattr(component, "text", "") or "")
            if text:
                texts.append(text)
        elif _component_named(component, "At"):
            who = str(getattr(component, "name", "") or "").strip()
            qq = str(getattr(component, "qq", "") or "").strip()
            texts.append(f"@{who or qq}")
        elif _component_named(component, "Image"):
            ref = await _resolve_image_ref(component)
            if ref and ref not in images:
                images.append(ref)
            texts.append(f"［图片{len(images)}］" if images else "［图片］")
        elif _component_named(component, "Face"):
            texts.append("［表情］")
        elif _component_named(component, "Reply"):
            continue
        else:
            # 其它组件（语音、文件、转发…）只报个类型名，别把对象表示塞进提示词
            name = type(component).__name__
            if name:
                texts.append(f"［{name}］")
    return "".join(texts).strip(), images


_AUDIO_SUFFIXES = (".wav", ".mp3", ".silk", ".amr", ".ogg", ".flac", ".m4a", ".aac", ".opus")
"""语音文件的常见后缀：工具回一个文件路径时，靠它认出"这是语音"。"""


def _looks_like_audio(ref: str) -> bool:
    body = str(ref or "").strip().lower()
    if not body:
        return False
    return body.split("?")[0].split("#")[0].endswith(_AUDIO_SUFFIXES)


async def _resolve_audio_ref(component: Any) -> str:
    """``Record`` / 语音 ``File`` 组件 → 一个能发出去的地址（file:// / http / 本机路径）。"""

    for name in ("file", "path", "url"):
        raw = str(getattr(component, name, "") or "").strip()
        if raw:
            return raw
    return ""


async def _audio_refs(value: Any) -> list[str]:
    """工具 / 指令返回值里的语音（合成语音的工具走这条路）。

    三种形态都认：``Record`` 组件、带音频后缀的 ``File``、以及直接返回的音频路径。
    用户 m20677：他想接生成语音的插件当动作，结果里必须是"语音"，不能只是
    ``［Record］`` 这段占位文字。
    """

    if value is None:
        return []
    name = type(value).__name__
    if name == "Record":
        ref = await _resolve_audio_ref(value)
        return [ref] if ref else []
    if name == "File":
        ref = await _resolve_audio_ref(value)
        return [ref] if ref and _looks_like_audio(ref) else []
    chain = getattr(value, "chain", None)
    if isinstance(chain, (list, tuple)):
        found: list[str] = []
        for component in chain:
            kind = type(component).__name__
            if kind == "Record":
                ref = await _resolve_audio_ref(component)
            elif kind == "File":
                ref = await _resolve_audio_ref(component)
                if not _looks_like_audio(ref):
                    ref = ""
            else:
                ref = ""
            if ref and ref not in found:
                found.append(ref)
        return found
    if isinstance(value, str) and _looks_like_audio(value):
        return [value]
    return []


def _exception_where(exc: BaseException) -> str:
    """异常最后落在哪个文件:行（哪个函数）：给别人看的"这是谁的锅"。"""

    tb = exc.__traceback__
    last = None
    while tb is not None:
        last = tb
        tb = tb.tb_next
    if last is None:
        return ""
    frame = last.tb_frame
    name = os.path.basename(str(frame.f_code.co_filename or ""))
    return f"{name}:{last.tb_lineno} {frame.f_code.co_name}"


def _is_reader_tool(name: str) -> bool:
    """这个工具是不是"读网页"这一类（决定正文留多长）。"""

    lowered = str(name or "").lower()
    return any(hint in lowered for hint in READER_TOOL_HINTS)


async def _result_text(value: Any, *, tool: str = "") -> tuple[str, list[str]]:
    """工具 / 指令的返回值 → (文字, 图片地址)。

    ``tool`` 只影响截断长度：阅读类工具留 :data:`MAX_PAGE_CHARS`，其余留
    :data:`MAX_RESULT_CHARS`。
    """

    limit = MAX_PAGE_CHARS if _is_reader_tool(tool) else MAX_RESULT_CHARS
    if value is None:
        return "", []
    parts = await _chain_text_and_images(value)
    if parts is not None:
        text, images = parts
        return _clip_brief(text, limit=limit), images
    if _component_named(value, "Image"):
        ref = await _resolve_image_ref(value)
        return "", ([ref] if ref else [])
    return _clip_brief(_tool_result_text(value), limit=limit), []


def _clip_brief(text: Any, *, limit: int = MAX_RESULT_CHARS) -> str:
    """返回内容截断：进提示词和进日志都用它，别让一坨 base64 撑爆上下文。"""

    body = str(text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit] + "…（内容过长已截断）"


def _as_tool_result(text: str) -> Any:
    """把工具返回的文字包装成 AstrBot 钩子认的 ``CallToolResult``。"""

    try:
        from mcp.types import CallToolResult, TextContent
    except Exception:
        return None
    try:
        return CallToolResult(
            content=[TextContent(type="text", text=str(text or ""))],
            isError=False,
        )
    except Exception:
        return None


class _ToolRunContext:
    """新版工具的 ``call(context, **kwargs)`` 需要的运行上下文。

    等价于 AstrBot 的 ``ContextWrapper[AstrAgentContext]``：工具真正用到的只有
    ``context.context.event``、``context.context.context`` 和 ``context.tool_call_timeout``。
    拿得到 AstrBot 自己的类时用真的，拿不到（老版本 / 单测环境）就用这个替身。
    """

    def __init__(self, plugin: "VirtualWorldPlugin", event: Any) -> None:
        try:
            from astrbot.core.agent.run_context import ContextWrapper
            from astrbot.core.astr_agent_context import AstrAgentContext

            inner = AstrAgentContext(context=plugin.context, event=event)
            self._wrapper = ContextWrapper(context=inner, tool_call_timeout=120)
        except Exception:
            self._wrapper = None
        self.context = types.SimpleNamespace(context=plugin.context, event=event)
        self.messages: list[Any] = []
        self.tool_call_timeout = 120

    def __getattr__(self, name: str) -> Any:
        wrapper = self.__dict__.get("_wrapper")
        if wrapper is not None:
            return getattr(wrapper, name)
        raise AttributeError(name)


def _tool_invoker(tool: Any):
    """按 AstrBot 的优先级挑出调用方式，返回 ``fn(event, params, plugin)``。"""

    handler = getattr(tool, "handler", None)
    if callable(handler):

        def call_handler(event: Any, params: dict[str, Any], plugin: Any):
            return handler(event, **params)

        return call_handler

    call = getattr(tool, "call", None)
    if callable(call) and _is_call_override(call):

        def call_method(event: Any, params: dict[str, Any], plugin: Any):
            return call(_ToolRunContext(plugin, event), **params)

        return call_method

    run = getattr(tool, "run", None)
    if callable(run):

        def call_run(event: Any, params: dict[str, Any], plugin: Any):
            return run(event, **params)

        return call_run

    return None


def _is_call_override(call: Any) -> bool:
    """判断 ``call`` 是工具自己实现的，而不是 ``FunctionTool`` 那个只抛错的默认版。"""

    func = getattr(call, "__func__", None)
    if func is None:
        return True
    try:
        from astrbot.core.agent.tool import FunctionTool

        return func is not FunctionTool.call
    except Exception:
        return not str(getattr(func, "__qualname__", "")).endswith("FunctionTool.call")


# ======================================================================
# 编辑器访问密码（AstrBot Dashboard 之外的二次保护，可选）
# ======================================================================


class EditorAuth:
    """独立访问密码：密码哈希存 sqlite，token 只在内存里，含失败锁定。"""

    def __init__(self, plugin: "VirtualWorldPlugin", config_password: str = "") -> None:
        self.plugin = plugin
        self.config_password = (config_password or "").strip()
        self.tokens: dict[str, float] = {}
        self.failures: list[float] = []
        self.lock_until = 0.0

    # ---------------- 密码 ----------------

    def stored_hash(self) -> dict[str, str]:
        return self.plugin.db.raw.kv_get("editor_password", {}) or {}

    def has_password(self) -> bool:
        return bool(self.config_password or self.stored_hash().get("hash"))

    def set_password(self, raw: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            self.plugin.db.raw.kv_delete("editor_password")
            return
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt.encode("utf-8"), 100_000)
        self.plugin.db.raw.kv_set(
            "editor_password", {"salt": salt, "hash": digest.hex()}
        )

    def verify(self, raw: str) -> bool:
        raw = (raw or "").strip()
        if not raw:
            return False
        stored = self.stored_hash()
        if stored.get("hash"):
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                raw.encode("utf-8"),
                str(stored.get("salt", "")).encode("utf-8"),
                100_000,
            ).hex()
            if hmac.compare_digest(digest, str(stored["hash"])):
                return True
        if self.config_password:
            return hmac.compare_digest(raw, self.config_password)
        return False

    # ---------------- 锁定与 token ----------------

    def locked(self) -> int:
        if self.lock_until and time.time() < self.lock_until:
            return int(self.lock_until - time.time())
        return 0

    def note_failure(self) -> None:
        now = time.time()
        self.failures = [ts for ts in self.failures if now - ts < LOCKOUT_SECONDS]
        self.failures.append(now)
        if len(self.failures) >= MAX_FAILED_ATTEMPTS:
            self.lock_until = now + LOCKOUT_SECONDS
            self.failures.clear()

    def issue_token(self) -> str:
        token = secrets.token_urlsafe(24)
        self.tokens[token] = time.time() + TOKEN_TTL_SECONDS
        self._prune()
        return token

    def revoke(self, token: str) -> None:
        self.tokens.pop(token, None)

    def check(self, token: str) -> bool:
        if not self.has_password():
            return True
        if not token:
            return False
        expires = self.tokens.get(token)
        if not expires or expires < time.time():
            self.tokens.pop(token, None)
            return False
        return True

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, exp in self.tokens.items() if exp < now]:
            self.tokens.pop(token, None)


# ======================================================================
# 插件主体
# ======================================================================


@register(
    PLUGIN_NAME,
    "exneverbur",
    "给 Bot 一个私有空间、动作、日程、场景记忆和工具能力，让 ta 像住在群里一样生活。",
    "v2.2",
)
class VirtualWorldPlugin(Star):
    @staticmethod
    def _plugin_version() -> str:
        """插件版本（`metadata.yaml` 的 version）：只用来给她一句"你跑在哪个版本"。"""

        meta = Path(__file__).resolve().parent / "metadata.yaml"
        try:
            for line in meta.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
        except Exception:  # noqa: BLE001 - 读不到就不提版本
            return ""
        return ""

    def build_stamp(self) -> dict[str, Any]:
        """本次进程的构建戳（模块级缓存，进程内只扫一次盘）。

        启动时 ``initialize()`` 会主动算一次；这里留了「没算过就补算」的兜底，
        这样即便 ``initialize()`` 因 ``enabled=false`` 早退，``/vw status`` 仍能
        报出实机跑的是哪份代码。注意：**不要**在 ``__init__`` 里调用——那是同步
        扫盘，会拖慢宿主加载。
        """

        global _BUILD_STAMP_CACHE
        try:
            if _BUILD_STAMP_CACHE is not None:
                return dict(_BUILD_STAMP_CACHE)
            with _BUILD_STAMP_LOCK:
                if _BUILD_STAMP_CACHE is None:
                    _BUILD_STAMP_CACHE = _compute_build_stamp(
                        Path(__file__).resolve().parent, self._plugin_version()
                    )
                return dict(_BUILD_STAMP_CACHE)
        except Exception as exc:  # noqa: BLE001 - 最后一道兜底：算不出也不能拦启动
            version = ""
            try:
                version = self._plugin_version()
            except Exception:  # noqa: BLE001
                pass
            return {
                "version": version,
                "hash": BUILD_STAMP_HASH_UNKNOWN,
                "files": 0,
                "mtime": "",
                "dir": "",
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "skipped": 0,
                "notes": "",
            }

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config

        self.enabled = _cfg_bool(config.get("enabled"), True)
        self.web_enabled = _cfg_bool(config.get("web_enabled"), True)
        self.llm_provider_id = str(config.get("llm_provider_id") or "").strip()
        # 打杂模型（文本、便宜）：补工具参数、压上下文、判断图和话题的关系
        self.vision_provider_id = str(config.get("vision_provider_id") or "").strip()
        self.helper_provider_id = (
            str(config.get("helper_provider_id") or "").strip()
            # 老配置里"上下文压缩"和"关系分析"是两个独立模型：并进打杂模型，
            # 让用户只需要理解两个便宜档（看图 / 文本）
            or str(config.get("context_provider_id") or "").strip()
        )
        self.utility_provider_id = self.helper_provider_id
        # 判断模型：只接"挑一个 / 打分 / 抽字段"这类纯判断。留空跟随打杂模型。
        self.judge_provider_id = (
            str(config.get("judge_provider_id") or "").strip() or self.helper_provider_id
        )
        self.consolidate_provider_id = (
            str(config.get("consolidate_provider_id") or "").strip()
            or self.utility_provider_id
        )
        # 事件模型：写"这件事的结果"那个模型。留空跟随打杂模型。
        self.event_provider_id = (
            str(config.get("event_provider_id") or "").strip() or self.utility_provider_id
        )
        # 内容生成模型：只在编辑器里"批量生成动作 / 地点"时用，留空回落到主模型
        self.creator_provider_id = str(config.get("creator_provider_id") or "").strip()
        self._sampling_config = {
            "enabled": _cfg_bool(config.get("sampling_enabled"), False),
            "temperature": config.get("sampling_temperature"),
            "top_p": config.get("sampling_top_p"),
            "frequency_penalty": config.get("sampling_frequency_penalty"),
            "presence_penalty": config.get("sampling_presence_penalty"),
        }
        self.tick_interval = max(5, _cfg_int(config.get("tick_interval"), 60))
        self.decider_interval = max(
            self.tick_interval, _cfg_int(config.get("decider_interval"), 300)
        )
        self.debug = _cfg_bool(config.get("debug"), False)
        # 接管回复也要过一遍 AstrBot 的回复钩子，别的插件（好感度之类）才看得见
        self.reply_hook_bridge = _cfg_bool(config.get("reply_hook_bridge"), True)
        self.log_level = str(config.get("log_level") or "INFO").upper()

        # agent 的模型槽位与五个开关**已经搬进世界配置**（world.agent.*，在世界编辑器里
        # 改）。这里不再读插件配置的同名键，只在引擎装好之后做一次种子迁移，把老插件
        # 配置里改过的值搬过去（见 _migrate_agent_config_into_world()）。
        # 语义检索用的向量模型：留空＝关闭语义检索（B1 才消费）。按用户裁定只搬 agent，
        # embedding 仍留在插件配置里，所以它是这里唯一还读插件配置的模型槽位。
        self.embedding_provider_id = str(
            config.get("embedding_provider_id") or ""
        ).strip()

        data_dir = _resolve_data_dir(os.path.dirname(os.path.abspath(__file__)))
        # P0-8：成长资产（growth/）与快照（backups/）都挂在这个目录下，跟着 state.db 一起备份
        self.data_dir = data_dir
        self.store = ConfigStore(data_dir)
        # 扩展挂载点：独立插件（如果装了）会通过 `star_cls.extension_host` 找到它
        self.extension_host = ExtensionHost(self)
        created = self.store.ensure_files()
        self.db = AsyncDatabase(self.store.db_path)
        self.auth = EditorAuth(self, str(config.get("web_password") or ""))

        self._last_events: "OrderedDict[str, AstrMessageEvent]" = OrderedDict()
        self._llm_responses: "OrderedDict[str, Any]" = OrderedDict()
        """最近一次主模型调用的原始响应（按会话存，供回复钩子使用）。"""
        self._tick_task: asyncio.Task | None = None
        self._self_initiated_depth = 0
        self._self_initiated_tasks: set[asyncio.Task] = set()
        """正在调模型的那几条协程（我们自己的调用）。"""
        self._self_initiated_untracked = 0
        self._reply_locks: dict[str, asyncio.Lock] = {}
        """每个会话一条：同一条会话的接管排队执行，避免两条回复互相不知道对方。"""
        self._reply_owner: dict[str, str] = {}
        """锁被哪条消息占着（身份，见 :func:`_reply_identity`）：同一条消息重投时
        不能再打断它自己（用户 m19362）。"""
        self._reply_session: dict[str, str] = {}
        """锁被**哪个会话**占着（用户 m19936）：只有同一个会话的新消息才打断它。"""
        self._reply_retries: dict[str, asyncio.Task] = {}
        """被打断之后排着的补偿重试（每个会话一条），见 :meth:`_schedule_interrupt_retry`。"""
        self._reply_seen: dict[str, dict[str, float]] = {}
        """刚处理过的消息身份 → 时间：重投去重，见 :meth:`_is_duplicate_delivery`。"""
        self._reaction_tasks: set[asyncio.Task] = set()
        # QQ 头像（用户 m13421）：通讯录里给人配个头像，按 qq 缓存一天
        self._avatars: dict[str, tuple[float, str]] = {}
        # 群的位分缓存（用户 m12215）：30 分钟刷一次，失败就沿用旧的
        self._group_role_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._group_role_tasks: set[asyncio.Task] = set()
        """正在后台看图的那几条协程（S6：看图接话，不阻塞收消息）。"""
        self._caption_tasks: set[asyncio.Task] = set()
        """正在后台转述的协程（用户 m08448：转述不占用回复路径）。"""
        self._meme_collect_tasks: set[asyncio.Task] = set()
        """正在把这一轮看到的图投进候选池的协程（用户 2026-10-24：不占回复路径）。"""
        self._live_say_tail: dict[str, str] = {}
        """刚即时发出去的那一句：连着说下一句时按它的字数停一下，保持打字的节奏。"""
        self._timeline_arrivals: dict[str, dict[str, Any]] = {}
        """每个会话「上一条消息什么时候到」：路由会把消息重投一遍（可能换了个事件
        对象），靠它才能把"到达 → 走到模型钩子"这段等待算出来。"""
        # 发送方持有「失败冷却」，所以自己留一份（回退响应路径也要用它判断）
        self.messenger = AstrBotMessenger(self)
        # 看图（多模态）：图片转述、天气工具返回的图都走它
        self.vision = AstrBotVision(self, self.vision_provider_id)
        # 人设：默认沿用 AstrBot 给这个会话选的那份，也可以在「全局设置 → 她是谁」里
        # 换成插件自己的一份（跟着预设走，不再随会话组漂移）
        self.persona_port = AstrBotPersona(self)

        # B1/WP4：把语义检索的两个注入点装出来。只读、绝不抛异常：任何一步失败
        # （没配 / 取不到实例 / 问不到维度）都退回 (None, None)，此时引擎侧 B1 保持
        # 关闭，召回路径与 A 阶段**逐字节相同**。装配**失败不缓存**，状态端点会重试
        # 一次（插件 __init__ 时宿主可能还没把 provider 挂上）。
        (
            self._embedding_runtime,
            self._topic_index_queue,
        ) = self._assemble_embedding_runtime()

        self.engine = VirtualWorldEngine(
            store=self.store,
            db=self.db,
            llm=AstrBotLLM(self, sampling=self.sampling_params()),
            # Agent 出去办事用的模型：插件配置 `agent_provider_id` 是正主；
            # 没配就传 None，引擎会回退主模型并记下"这是回退"（P0-4）。
            agent_llm=(
                AstrBotLLM(self, self._configured_agent_provider_id(), sampling=self.sampling_params())
                if self._configured_agent_provider_id()
                else None
            ),
            helper_llm=AstrBotLLM(self, self.helper_provider_id, sampling=self.sampling_params()),
            judge_llm=AstrBotLLM(self, self.judge_provider_id, sampling=self.sampling_params()),
            context_llm=AstrBotLLM(self, self.utility_provider_id, sampling=self.sampling_params()),
            creator_llm=AstrBotLLM(self, self.creator_provider_id, sampling=self.sampling_params()),
            event_llm=AstrBotLLM(self, self.event_provider_id, sampling=self.sampling_params()),
            consolidate_llm=AstrBotLLM(self, self.consolidate_provider_id, sampling=self.sampling_params()),
            describer=self.vision,
            messenger=self.messenger,
            tools=AstrBotTools(self),
            commands=AstrBotCommands(self),
            persona=self.persona_port,
            clock=None,
            tick_seconds=float(self.tick_interval),
            decider_interval=float(self.decider_interval),
            debug=self.debug,
            logger=self.logger,
            # 扩展挂载点：扩展包（独立插件）拿到这个实例后自己 register
            extensions=self.extension_host,
            # B0/WP8b：agent 能力观测段的同步回调（只读、绝不抛异常）。
            # 引擎那边是浅合并，这里交回去的段会整块覆盖同名段。
            agent_status=self._agent_status_for_engine,
            # B1/WP4：语义检索的两个注入点。两个都是 None ＝ 未配置，
            # 引擎不查向量表、不问宿主、不改一句话（与 A 阶段逐字节相同）。
            embedding_runtime=self._embedding_runtime,
            topic_index_queue=self._topic_index_queue,
            # A5/A6：配置整理队列也注入同一份（插件侧保存入口与引擎侧每拍驱动共用
            # 一个实例，而不是各自懒建一个——同一张表靠 DB 租约虽然不会双跑，
            # 但"谁在跑"只该有一份答案）。
            config_index_queue=self._action_discovery_queue(),
        )
        # 调试回显实时发：工具/指令调用在发生的那一下就走这条通道，
        # 不再等整轮动作跑完才一起推给群里
        # 「你的处境」要说清"跑在哪个版本、用哪个模型"（用户 m14211）：
        # 版本取 metadata.yaml，模型取插件配置；改配置时随 `_apply_config` 重新写。
        self.engine.plugin_version = self._plugin_version()
        self.engine.llm_label = self.llm_provider_id
        self.engine.debug_sink = self._send_debug_live
        # 即时发言：慢动作（联网检索、生图、录视频这类）开始之前她已经说出口的那几句，
        # 当场发出去，不再等整段流水线跑完才一起冒出来
        self.engine.say_sink = self._send_live_say
        self.engine.routing_sent_sink = self._notify_routing_sent_context
        # 插件工坊（用户 m31581）：引擎只管校验与草稿落盘；"拷进 data/plugins、
        # 让宿主加载、把动作写进世界配置"这三件只有这里能做（要 StarManager）。
        # 没接这个端口时引擎会如实回"这个外壳不支持安装插件"。
        self.engine.plugin_installer = self._install_forged_plugin
        # 正在处理「推进 tick」的会话：连点时只认第一次，别攒成一串 tick
        self._advancing: set[str] = set()

        # 一次性种子迁移：老插件配置里的 agent_* 值搬进世界配置（幂等、只搬一次）。
        # 放在引擎装配之后：引擎的 world 就是迁移的"看板"，写盘后按需热加载一次。
        self._migrate_agent_config_into_world()

        self._register_web_apis()

        for name in created:
            self.logger.info(f"[virtual_world] 首次启动，已生成默认配置 {name}")
        for warning in self.engine.load_warnings:
            self.logger.warning(f"[virtual_world] 配置提醒：{warning}")
        if not self.engine.enabled_session_ids():
            self.logger.info(
                "[virtual_world] 会话白名单为空，请在群聊里发送「/vw session add」"
                "或在 WebUI 的SOLIS世界编辑器里添加会话。"
            )

    # 世界配置里 agent 段的**五个开关**，以及它们在老插件配置里的同名键（多了 agent_ 前缀）。
    # 顺序与 core/models.py 的 AgentConfig 一致；一次性种子迁移与只读视图共用这张表。
    # **provider_id 不在这张表里**：用户裁定「agent 用哪个模型」属于插件配置
    # （``_conf_schema.json`` 的 ``agent_provider_id``），它是正主；世界配置里那个老值
    # 只在插件配置留空时静默回退（见 :meth:`_configured_agent_provider_id`）——
    # 既不搬进世界配置，也不被清掉，用户这一轮在编辑器里选过的模型不会丢。
    AGENT_SWITCH_WORLD_KEYS = (
        ("enabled", "agent_enabled"),
        ("history_lookup_enabled", "agent_history_lookup_enabled"),
        ("artifact_generation_enabled", "agent_artifact_generation_enabled"),
        ("file_send_enabled", "agent_file_send_enabled"),
        ("code_execution_enabled", "agent_code_execution_enabled"),
    )

    def _legacy_agent_seed(self) -> dict[str, Any]:
        """从插件配置里挑出「用户确实改过」的 agent 旧开关值。

        开关的 False＝没开，不算改过。模型的槽位不在这一趟里（见
        :meth:`_configured_agent_provider_id`：插件配置才是它的正主）。

        Returns:
            dict[str, Any]: 世界配置键 → 旧值；一个都没改过时是空字典。
        """
        config = getattr(self, "config", None)
        seed: dict[str, Any] = {}
        if config is None:
            return seed
        for name, legacy in self.AGENT_SWITCH_WORLD_KEYS:
            if _cfg_bool(config.get(legacy), False):
                seed[name] = True
        return seed

    def _clear_legacy_agent_config(self, *, reason: str) -> None:
        """把插件配置里那**五个** agent 旧开关重置回默认值并落盘。

        迁移把旧值读走之后它们就没用了，留着反而有害：用户哪天把世界配置的 agent
        段改回全默认，这些旧键又会被当成「刚改过」重新种子进来。世界配置里已经有
        设置时也要清，否则同样的洞照样存在。

        ``agent_provider_id`` **不在这一趟里**：它现在是"agent 用哪个模型"的正主
        （面板上可见），清了等于把用户选的模型删掉。

        这五个键在 ``_conf_schema.json`` 里标了 ``invisible``：宿主不会因为 schema
        里没有它们而在插件 init 之前就把旧值剪掉（那样迁移根本读不到），面板上也
        不显示。

        Args:
            reason: 写进日志的原因，说明这次为什么清。

        Returns:
            None
        """
        config = getattr(self, "config", None)
        if config is None:
            return
        try:
            for _name, legacy in self.AGENT_SWITCH_WORLD_KEYS:
                config[legacy] = False
            save = getattr(config, "save_config", None)
            if callable(save):
                save()
        except Exception as exc:
            self.logger.warning(
                f"[virtual_world] 清掉插件配置里的 agent 旧键失败（{reason}）：{exc}"
            )
            return
        self.logger.info(
            "[virtual_world] 已把插件配置里的 agent 旧键重置回默认（"
            + reason
            + "）：这六个键已经搬进世界配置，面板上也不再显示。"
        )

    def _migrate_agent_config_into_world(self) -> None:
        """把老插件配置里的 agent **开关**值一次性搬进世界配置（幂等）。

        只有「世界配置的 agent 段还是出厂默认」**且**「插件配置里确实有非默认值」
        才写盘；写的是盘上那份原始世界配置，只有 agent 段按种子改写，其余段不做任何
        语义改动（和编辑器保存一样会走一次 ``parse_world`` 规范化，它给出的提醒都写
        进日志）。世界配置里已有任何非默认值就直接返回，绝不回写覆盖用户的改动；插件
        配置里没有非默认值时也不写盘。

        **模型槽位（``provider_id``）不参与这趟迁移**：它属于插件配置，世界配置里
        那个老值只是回退（见 :meth:`_configured_agent_provider_id`），既搬不走也清不掉。

        两条路径都会把插件配置里的五个旧开关重置回默认并落盘（见
        :meth:`_clear_legacy_agent_config`）：旧值留着，等世界配置被改回全默认之后
        会被重新种子回来。

        Returns:
            None
        """
        try:
            agent = self.engine.world.agent
        except Exception:
            return
        # 世界配置里已经有非默认值 → 世界一个字节都不写（幂等，同时保护用户的改动）
        world_has_values = False
        for name, _legacy in self.AGENT_SWITCH_WORLD_KEYS:
            if bool(getattr(agent, name, False)):
                world_has_values = True
                break
        seed = self._legacy_agent_seed()
        if world_has_values:
            if seed:
                self._clear_legacy_agent_config(
                    reason="世界配置里已经有 agent 设置，旧键留着以后会被重新种子"
                )
            return
        if not seed:
            return
        try:
            raw = self.store.raw_world()
        except Exception as exc:
            self.logger.warning(f"[virtual_world] agent 配置迁移读取世界配置失败：{exc}")
            return
        if not isinstance(raw, dict):
            return
        section = raw.get("agent")
        merged = dict(section) if isinstance(section, dict) else {}
        merged.update(seed)
        raw["agent"] = merged
        try:
            warnings = self.store.save_world(
                raw, reason="agent 配置从插件配置搬进世界配置"
            )
        except Exception as exc:
            self.logger.warning(f"[virtual_world] agent 配置迁移写入世界配置失败：{exc}")
            return
        for warning in warnings:
            self.logger.warning(f"[virtual_world] 配置提醒：{warning}")
        self.logger.info(
            "[virtual_world] 已把插件配置里的 agent 设置搬进世界配置："
            + "、".join(sorted(seed))
            + "（以后在「世界编辑器 → 设置 → Agent（自主能力）」里改）"
        )
        # 引擎手上的 world 还是迁移前那份，热加载一次让内存与盘上一致。
        try:
            self.engine.reload_config()
        except Exception as exc:
            self.logger.warning(f"[virtual_world] agent 配置迁移后热加载失败：{exc}")
        self._clear_legacy_agent_config(reason="agent 设置已经搬进世界配置")

    # ================= 生命周期 =================

    async def initialize(self) -> None:
        if not self.enabled:
            self.logger.info("[virtual_world] 插件已禁用（enabled=false）")
            return
        started = time.perf_counter()
        stamp = self.build_stamp()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if stamp.get("error"):
            self.logger.warning(
                f"[virtual_world] build stamp 未计算：{stamp.get('error')}"
            )
        else:
            # 固定顺序、key=value、dir 放最后（路径可能含空格），便于以后 grep / 脚本解析
            self.logger.info(
                "[virtual_world] build version=%s files=%s hash=%s mtime=%s dir=%s",
                stamp.get("version") or "",
                stamp.get("files"),
                stamp.get("hash"),
                stamp.get("mtime"),
                stamp.get("dir"),
            )
        if stamp.get("skipped"):
            self.logger.warning(
                "[virtual_world] build stamp 跳过 %s 个超过 8MB 的文件",
                stamp.get("skipped"),
            )
        if stamp.get("notes"):
            self.logger.warning(
                "[virtual_world] build stamp 有文件未计入：%s",
                stamp.get("notes"),
            )
        self.logger.debug(f"[virtual_world] build stamp 计算耗时 {elapsed_ms:.1f} ms")
        # ---- 存储自检（2026-10 磁盘满事故）----
        # 磁盘满时她整轮不说话，而日志里只有几百 K、看不出异常；所以启动时先量一次
        # 分区可用空间和数据目录大小，不够就**一行** warning（文件清单只给 /vw status）。
        try:
            storage_warn = self.engine.storage_warning_text()
            if storage_warn:
                self.logger.warning(storage_warn)
        except Exception as exc:  # noqa: BLE001 - 自检失败绝不许挡住启动
            self.logger.debug(f"[virtual_world] 存储自检失败（不影响使用）：{exc}")
        # 主人语义（用户 2026-10-24 拍板）：没配主人 = 谁都不是主人。启动提醒一次，
        # 不每 tick 刷（tick 里也不该再碰这个判断）。
        self._warn_missing_owner_once()
        # T1：这次启动补进 world.json 的默认键 → 日志页留一条（记不上不影响启动）
        try:
            await self.engine.log_world_backfill_event()
        except Exception as exc:  # noqa: BLE001 - 事件日志坏着也不许挡住启动
            self.logger.debug(f"[virtual_world] 补键日志没记上：{exc}")
        try:
            await self.engine.recover_topic_exposures()
            # P2-3：把上一次没做完的任务收一遍（**不重放副作用**）。失败只记日志，
            # 绝不让"恢复"反过来把插件起不来。
            try:
                recovered = await self.engine.recover_agent_tasks()
                if recovered.get("interrupted") or recovered.get("deferred"):
                    self.logger.info(
                        "[virtual_world] 启动恢复：标记中断 %s 条，转暂缓 %s 条",
                        recovered.get("interrupted"),
                        recovered.get("deferred"),
                    )
            except Exception as exc:
                self.logger.debug(f"[virtual_world] 启动恢复失败（不影响使用）：{exc}")
        except Exception as exc:
            self.logger.warning(f"[virtual_world] 已确认发送的曝光恢复失败，保留持久记录待恢复：{exc}")
        # 启动顺手瘦一次（幂等、按天去重）：刚重启时最该腾空间，不用等第一轮 tick。
        try:
            trimmed = await self.engine.run_storage_maintenance()
            if trimmed:
                self.logger.debug(f"[virtual_world] 启动瘦身：{trimmed}")
        except Exception as exc:  # noqa: BLE001 - 瘦身失败绝不许挡住启动
            self.logger.debug(f"[virtual_world] 启动瘦身失败（不影响使用）：{exc}")
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop())
        self.logger.info(
            f"[virtual_world] 世界时钟已启动：每 {self.tick_interval} 秒 1 tick，"
            f"决策间隔 {self.decider_interval} 秒"
        )
        if self.web_enabled:
            self.logger.info(
                "[virtual_world] 网页编辑器：WebUI 插件详情页 → 「SOLIS世界编辑器」"
            )

    async def terminate(self) -> None:
        # 宿主重载插件时是「先调 terminate()，再解绑事件处理器」，
        # 这个窗口里进来的消息还会走到这个实例上。标记一下，让所有入口直接装死。
        self._retired = True
        task, self._tick_task = self._tick_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.db.close()
        except Exception:
            pass
        self.logger.info("[virtual_world] 已停止世界时钟")

    @property
    def retired(self) -> bool:
        """这个实例已经被终止（重载/卸载）了。"""

        return bool(getattr(self, "_retired", False))

    ORPHAN_EVENT_KEEP_DAYS = 30
    """无会话事件行的保留天数（页面级操作的失败记录留这么久够排查了）。"""

    async def _prune_orphan_events_once(self, *, now: float | None = None) -> int:
        """清一次没有会话归属的旧事件行（**一天只做一次**，失败只记日志）。

        为什么挂在插件这一层而不是引擎的日结里：会话级裁剪在引擎里是按会话走的，
        而这里的对象恰恰是"没有会话"的行——它属于插件的日常维护，不该混进世界那一拍。
        返回删掉的条数（今天已经清过 / 库出错都回 0）。
        """

        try:
            day = time.strftime("%Y-%m-%d", time.localtime(float(now or time.time())))
        except Exception:
            day = ""
        if day and str(getattr(self, "_orphan_prune_day", "") or "") == day:
            return 0
        cutoff = float(now or time.time()) - self.ORPHAN_EVENT_KEEP_DAYS * 86400
        try:
            removed = int(await self.db.call("prune_orphan_events", before=cutoff) or 0)
        except Exception as exc:  # noqa: BLE001 - 维护任务不该把世界时钟带崩
            self.logger.debug(f"[virtual_world] 清理无会话事件行失败：{exc}")
            return 0
        self._orphan_prune_day = day
        if removed:
            self.logger.info(f"[virtual_world] 清掉了 {removed} 条没有会话归属的旧事件行")
        return removed

    async def _tick_loop(self) -> None:
        # 固定节拍：按「上一次唤醒 + 间隔」推算下一次唤醒，而不是跑完再睡 60 秒。
        # 后者会被每轮 tick 自身的耗时（调模型可能要好几秒）一直往后推，
        # 攒着攒着就整分钟跳过去，那一分钟到点的日程永远等不到。
        next_at = time.time() + self.tick_interval
        while True:
            try:
                await asyncio.sleep(max(0.5, next_at - time.time()))
                next_at += self.tick_interval
                if next_at < time.time():
                    # 落后太多（宿主机卡顿 / 长时间睡眠）就重新对齐，别追赶
                    next_at = time.time() + self.tick_interval
                # 宿主登记供应商比插件 __init__ 晚：装配那次可能拿到 (None, None)，
                # 这里补装（30 秒最多试一次），否则 tick 里的批量索引永远是空转
                self._retry_embedding_assembly()
                # 一天一次：清掉**没有会话归属**的旧事件行（B1 收尾第 3 条的后半截）。
                # 会话级裁剪管不到它们（`prune_session_events` 遇空会话直接返回 0），
                # 页面级操作（管理员上传表情包失败）写下的那些行会一直堆着。
                await self._prune_orphan_events_once()
                # 一天一次：给库瘦身（decision_batches 留 14 天、state_history /
                # event_log 留 30 天）。放在 tick 里而不是消息路径上：一天跑一次就够，
                # 每条消息都跑只会把事件循环拖慢。幂等 + 自带按天去重 + 失败只记日志。
                try:
                    await self.engine.run_storage_maintenance()
                except Exception as exc:  # noqa: BLE001 - 维护任务不该把世界时钟带崩
                    self.logger.debug(f"[virtual_world] 存储瘦身失败（下次再试）：{exc}")
                outcomes = await self.engine.tick()
                if self.debug and outcomes:
                    for outcome in outcomes:
                        self.logger.info(
                            f"[virtual_world] tick {outcome.session_id}: "
                            f"{'; '.join(outcome.notes)} "
                            f"messages={outcome.messages}"
                        )
                # 一个会话组只催一次决策：按"会话"逐个催的话，同一个她会被催好几遍，
                # 而且第二次很可能挂在别的会话名下决定（落点就跟着跑了）
                for session_id in self.engine.tick_session_ids():
                    await self.engine.maybe_decide(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(f"[virtual_world] 世界时钟出错：{exc}")

    # ================= 事件记录 =================

    def last_event(self, session_id: str) -> AstrMessageEvent | None:
        return self._last_events.get(session_id)

    async def pending_intervention(self, session_id: str) -> float:
        """联动接口：她正在等群友拿主意吗？返回等待截止时间戳（0 = 不在等）。

        「意图路由」在等待期间会把这个会话的消息**合并放行、跳过回复冷却**，
        让群友的回应尽快交到她手里；查不到或出错一律返回 0，路由照旧工作。
        """

        if not session_id:
            return 0.0
        try:
            return float(await self.engine.pending_intervention(session_id))
        except Exception:
            return 0.0

    def last_event_any(self) -> AstrMessageEvent | None:
        """最近一次见过的消息事件（拿不到会话 id 时给工具调用兜底）。"""

        if not self._last_events:
            return None
        return next(reversed(self._last_events.values()))

    def nearby_event(self, session_id: str) -> AstrMessageEvent | None:
        """Return only an exact target context, never a sibling chat.

        Args:
            session_id: Explicit execution session.

        Returns:
            Cached target event, or None if missing or disabled.
        """
        target = str(session_id or "").strip()
        if not target or not self.engine.is_enabled(target):
            return None
        return self.last_event(target)

    def remember_llm_response(self, session_id: str, response: Any) -> None:
        """记下主模型的原始响应（回复钩子要拿它给别的插件看）。"""

        if response is None or not session_id:
            return
        self._llm_responses[session_id] = response
        self._llm_responses.move_to_end(session_id)
        while len(self._llm_responses) > 50:
            self._llm_responses.popitem(last=False)

    def take_llm_response(self, session_id: str) -> Any:
        response = self._llm_responses.pop(session_id, None)
        return response

    def _pronoun(self) -> str:
        """由全局设置里的性别决定称呼：她 / 他 / ta。"""

        return pronoun_for(self.engine.world.gender)

    def _warn_missing_owner_once(self) -> None:
        """没配**账号主人**时启动打**一次** WARN（与 ``engine.is_owner()`` 同一口径）。

        口径只有一条：``engine.owner_user_id()`` 为空 = 谁都不是主人（用户 2026-10-24
        拍板）。关系表里的「主人 / 守护者」只是世界设定，不构成账号权限，所以这里也
        不去看它。需要主人权限的功能（拍板、更详细的写库失败提示）会一律按"外人"
        处理——这件事必须让用户知道。只打一次：这个判断不进 tick。
        """

        if getattr(self, "_owner_warned", False):
            return
        self._owner_warned = True
        try:
            owner = str(self.engine.owner_user_id() or "").strip()
        except Exception:  # noqa: BLE001 - 读不到就当没配，提示不该反过来挡住启动
            owner = ""
        if owner:
            return
        self.logger.warning(
            "[virtual_world] 账号主人未配置（world.json → agent.owner_user_id 为空）："
            "审批与告警不会发给任何人，需要主人权限的功能一律按“外人”处理"
        )

    def _can_admin(self, event: AstrMessageEvent) -> bool:
        """能不能执行管理类指令。

        AstrBot 自己的管理员永远可以；另外「全局设置 → 管理员 QQ」里列出来的人也可以
        （有些群不希望为了用几条指令就去动 AstrBot 的管理员配置）。
        """

        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        listed = {str(item).strip() for item in (self.engine.world.admin_ids or [])}
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        return bool(sender) and sender in listed

    # ================= 给其他插件用的只读接口 =================

    async def get_routing_context(self, session_id: str, *, exclude_event_ids=(), max_messages: int = 20, max_chars: int = 8000) -> dict[str, Any] | None:
        """Exact-session read-only context; disabled sessions have no snapshot."""
        if not self.enabled or not session_id or session_id not in self.engine.enabled_session_ids():
            return {"api_version": 1, "status": "not_managed", "session_id": session_id, "messages": []}
        return self.engine.routing_context.snapshot(session_id, exclude_event_ids=exclude_event_ids,
            max_messages=max_messages, max_chars=max_chars)

    def _notify_routing_sent_context(self, session_id: str, text: str, *, operation_id: str, segment_id: str, status: str, message_ids=()) -> None:
        if status not in {SEND_ACCEPTED, SEND_DELIVERED}:
            return
        try:
            from astrbot.core.star.star import star_map
            for meta in star_map.values():
                peer = getattr(meta, "star_cls", None)
                callback = getattr(peer, "record_sent_context", None)
                if callable(callback):
                    callback(session_id, send_operation_id=operation_id, segment_id=segment_id,
                        text=text, message_id=str(next(iter(message_ids or ()), "")), delivery=status,
                        occurred_at=time.time())
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 路由发送上下文通知失败：{exc}")

    async def social_snapshot(self, session_id: str) -> dict[str, Any] | None:
        """某个会话当前的状态快照，供其他插件联动（例如意图路由按社交欲调阈值）。

        会话没启用本插件时返回 None，调用方应当回落到自己的默认行为。
        """

        if not self.enabled or not session_id:
            return None
        try:
            if not self.engine.is_enabled(session_id):
                return None
            state = await self.engine.load_state(session_id, cold_start=False)
        except Exception:
            return None
        return {
            # affect 是「心潮」；social 是老名字，保留给还没更新的联动方
            "affect": round(state.affect, 4),
            "social": round(state.affect, 4),
            # 效价：心情好坏（0.5 中性）。联动方可以据此区分"孤独想聊"和"恼火想怼"
            "valence": round(state.valence, 4),
            "storm": bool(state.storm),
            "loneliness": round(state.loneliness, 4),
            "energy": round(state.energy, 4),
            "curiosity": round(state.curiosity, 4),
            "boredom": round(state.boredom, 4),
            "willingness": round(self.engine.willingness(state), 4),
            "mood": state.mood,
            "state": state.state,
            "style_cell": state.last_style_cell,
            # 睡着时联动方最好直接不判断（意图路由会拿它当硬门）
            "sleeping": bool(self.engine.is_asleep(state)),
            "node_id": state.node_id,
            "world_time": state.world_time,
            "interject_enabled": bool(self.engine.world.decider.enabled),
        }

    async def reply_willingness(self, session_id: str) -> float | None:
        """她对「现在该不该开口」的整体意愿（0~1）。

        综合孤独感、无聊、好奇、心潮与疲惫、被冷落等因素，
        不启用本插件的会话返回 None，调用方应回落到自己的默认行为。
        """

        snapshot = await self.social_snapshot(session_id)
        if snapshot is None:
            return None
        return float(snapshot.get("willingness") or 0.0)

    def _remember_event(self, event: AstrMessageEvent) -> None:
        session_id = event.unified_msg_origin
        self._last_events[session_id] = event
        self._last_events.move_to_end(session_id)
        while len(self._last_events) > 200:
            self._last_events.popitem(last=False)

    async def _annotate_message(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        sources: list[str] | None = None,
        summarize_forward: bool = False,
        image_labels: dict[str, str] | None = None,
    ) -> str:
        """给消息补上模型看不到的部分：图片里有什么、这是不是一条转发。

        ``sources``：图片地址已经取过了就传进来，省得同一个组件解析两遍
        （NapCat 这类协议端解析一次可能要真去取图）。

        ``summarize_forward``：这条转发值得读一遍时（她要处理的 @ / 私聊 / 唤醒），
        用多模态模型把转发内容（含里面的图）压成摘要替换正文；关掉或读不出来
        就退回原来那句「这是一条转发的聊天记录」。
        """

        notes: list[str] = []
        if _is_forwarded(event):
            summary = await self._forward_summary_text(event) if summarize_forward else ""
            if summary:
                text = _with_forward_summary(text, summary)
                notes.append(
                    "这是一条转发消息，上面那段是它的内容摘要，不是当前群里正在说的话"
                )
            else:
                notes.append("这是一条转发的聊天记录，不是当前群里正在说的话")
        mention = _mention_note(event)
        if mention:
            notes.append(mention)
        quoted = await self._quoted_note(event)
        if quoted:
            notes.append(quoted)
        sticker = _sticker_note(event)
        if sticker:
            notes.append(sticker)
        attachment = _attachment_note(event, text)
        if attachment:
            notes.append(attachment)
        notes.extend(
            await self._annotate_images(
                event, text, sources=sources, labels=image_labels
            )
        )
        if not notes:
            return text
        return f"{text}\n［{'；'.join(notes)}］".strip()

    async def _quoted_note(self, event: AstrMessageEvent) -> str:
        """他引用了哪一条：聊天记录里还看得到就给个开头，看不到才写原文。

        引用内容本来完全没进正文（只喂给了看图那一步），于是「如何评价」这种
        靠引用才有意义的话，她只能对着聊天记录里被掐成半句的旧消息猜。
        """

        who, text = _quoted_parts(event)
        body = " ".join(str(text or "").split())
        if not body:
            return ""
        someone = who or "有人"
        context = self.engine.world.context
        line_limit = max(40, int(getattr(context, "chat_line_chars", 500) or 500))
        found = await self.engine.quote_in_records(event.unified_msg_origin, body)
        # 记录里就有**而且那一行没被截断**时，只点出开头让她自己往上对照；
        # 那条本身已经被截断（或者干脆不在留档里）时，还是把原文写全——
        # 否则她对着"还有 N 字没显示"的那半句，根本不知道被引用的是哪一段。
        if found and len(body) <= line_limit:
            head = body[:40] + ("…" if len(body) > 40 else "")
            return (
                f"他引用的是上面聊天记录里的那条（{someone}：「{head}」）"
                "——照上面那条看，这不是他这次打的字"
            )
        limit = max(0, int(getattr(context, "quote_chars", 1000) or 0))
        if limit <= 0:
            return f"他引用了一条更早的消息（{someone}），那条已经不在聊天记录里了"
        shown = body if len(body) <= limit else clip_line(body, limit)
        where = "上面聊天记录里那条太长，只显示了开头" if found else "聊天记录里已经没有了"
        return (
            f"他引用了一条更早的消息（{someone}），{where}，"
            f"原文：「{shown}」——这是引用，不是他这次打的字"
        )

    async def _forward_summary_text(self, event: AstrMessageEvent) -> str:
        """把这条转发读成摘要：关掉、没配多模态模型、读不出来都返回空串。"""

        vision = getattr(self, "vision", None)
        if vision is None or not vision.enabled:
            return ""
        try:
            settings = self.engine.world.vision
            if not bool(getattr(settings, "forward_summary", True)):
                return ""
            digest, images, forward_ids = await _forward_digest(event)
            if not digest.strip() and not images:
                return ""
            summary = await vision.summarize_forward(
                digest,
                images,
                fingerprint=_forward_fingerprint(forward_ids, digest, images),
            )
            if summary:
                await self.engine.note_vision(
                    event.unified_msg_origin,
                    ok=True,
                    images=len(images),
                    detail=f"转发聊天记录已压成摘要（{len(summary)} 字）",
                )
            return summary
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 转发摘要失败：{exc}")
            return ""

    def _inbox_sources(self, sources: list[str]) -> list[str]:
        """把临时文件地址换成插件自己目录里的副本（同步，调用处用 to_thread 也行）。

        用户 m07965：协议端的临时图在识图/转述之前就可能被清理，报 ``file_missing``。
        在收到消息的这一刻把字节留下来，后面读的就是这一份。
        """

        data_dir = str(getattr(self.store, "data_dir", "") or "")
        if not data_dir:
            return list(sources)
        try:
            limit = int(self.engine.media_settings().get("max_file_bytes") or 0)
        except Exception:  # noqa: BLE001
            limit = 0
        return [_keep_image_file(data_dir, item, limit=limit) for item in sources]

    async def _media_ids(
        self, sources: list[str], *, session_id: str = ""
    ) -> list[str]:
        """把这一轮的图收进本地资产库（阶段B1），返回与 ``sources`` 对齐的 id。

        收不进来（没开开关、地址取不到、超预算、格式不认）的那一格是空串：
        调用方照旧拿原地址用，图还是看得见的，只是留档里多一份能长期翻出来的副本。
        异常一律吞掉——这是"顺手多做的一步"，不该把回消息拖下水。

        ``session_id``（B1 收尾）原样交给 ``cache_media_list``：非空时每一张没收进来的图
        都在事件账里留一条 ``media_cache``。带上它才答得出"这张图是哪个会话没缓存上"；
        不给会话的失败行归不了档，也不参与会话级裁剪。
        """

        items = [str(item or "") for item in (sources or [])]
        if not items:
            return []
        try:
            return await self.engine.cache_media_list(items, session_id=session_id)
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 收图失败：{exc}")
            return ["" for _ in items]

    async def _annotate_images(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        sources: list[str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[str]:
        """图片这一段的处理：转了述就写描述，没配转述模型就交给多模态主模型。

        图片本来在消息里是看不见的，出了任何岔子都要留下一条日志——
        否则用户只会看到"模型好像没看到图"，却没有任何线索。

        ``labels``：**准备回复时**才有的编号（``图片地址 -> 图1``，见 `_reply_images`）。
        给了它就说明交付决策已经做完了，这里只负责把"这一条消息的图是第几号"讲清楚，
        不再自己转述一遍。
        """

        components = _image_components(event)
        resolved = (
            [str(item) for item in sources if str(item).strip()]
            if sources is not None
            else self._inbox_sources(await _image_sources(event))
        )
        if not components and not resolved:
            return []
        session_id = event.unified_msg_origin
        sources = resolved
        if labels is not None:
            mine = [str(labels[str(url)]) for url in sources if str(url) in labels]
            if not mine:
                # 这条消息的图这一轮没交给主模型看（比如被转述了）：转述那句由
                # `_reply_images` 的 notes 交回来，这里不重复说
                return []
            listed = "、".join(mine)
            # 编号说给她听，就得同时说清"这是内部编号、别照着念"——不然她会
            # 直接回"图2那张…"，而对方根本看不到编号（见 core/prompt.py 里同一句）
            guard = "（这套编号只给你自己对上用，回话里别写「图1」这种说法）"
            if len(mine) >= len(sources):
                return [
                    f"这条消息带了 {len(mine)} 张图片，就是{listed}，已经一起发给你了{guard}"
                ]
            # 「已经一起发给你」只对**这次真的交出去**的那几张说（2026-10-23 续：
            # 取不到字节的图会被移出 refs，不能再说"其余几张只有文字描述"——
            # 那几张可能连转述都没有）。其余几张如实说"这次没发出去"。
            return [
                f"这条消息带了 {len(sources)} 张图片，其中 {listed} 已经一起发给你，"
                f"其余几张这次没有发出去{guard}"
            ]
        vision = getattr(self, "vision", None)
        if not sources:
            detail = "收到图片但拿不到可用地址"
            debug = _image_components_debug(event)
            if debug:
                detail = f"{detail}（组件字段：{debug}）"
            await self.engine.note_vision(
                session_id, ok=False, images=len(components), detail=detail
            )
            # QQ 表情包（大表情）几乎都取不到图：别让她以为"什么都没收到"，
            # 后面 _sticker_note 会把它的文案写进消息里。
            if components and all(
                _component_named_any(item, _FACE_COMPONENT_NAMES)
                for item in components
            ):
                return ["对方发的是 QQ 表情 / 表情包，这张看不到内容"]
            return ["对方发了一张图片，但只看到它在这儿，内容没拿到"]

        captions: list[str] = []
        if vision is None or not vision.enabled:
            return [_images_head(len(sources))]
        # 收到的图**现在就**看看是不是已经超过「一次带几张」的上限了：
        # 超出的那几张（先来的）立刻转成文字，不等调主模型那一刻。
        notes = await self._caption_overflow(event, sources)
        return [_images_head(len(sources)), *notes]

    async def _caption_overflow(
        self, event: AstrMessageEvent, sources: list[str]
    ) -> list[str]:
        """收到的图已经超过「一次带几张」：把更早、还没转述的那几张先转成文字。

        上限之内那几张会随这次请求交给主模型看，不必转述（同一张图只看一遍）；
        超出的那几张反正不会被带上，趁**收到图的时候**就转掉——等调主模型那一刻
        再转，她已经在等回复了。返回要写进这条消息注解的那几句。
        """

        session_id = event.unified_msg_origin
        cap = max(1, int(self.engine.world.context.image_max))
        history = await self.engine.chat_images_for_reply(session_id, PENDING_IMAGE_KEEP)
        candidates = list(
            dict.fromkeys(
                [
                    *[
                        str(item.get("url") or "").strip()
                        for item in history
                        if str(item.get("url") or "").strip()
                    ],
                    *[str(item).strip() for item in sources if str(item).strip()],
                ]
            )
        )
        # 主模型看不见图（或配置里写死不附图）时，这里**不提前转述**：
        # 那时候这一条还没入档，转述挂不上去，回头还会被当成"已经看过"而跳过。
        # 留给回复那一轮统一转述，见 _reply_images（用户 m11963）。
        mode = str(
            getattr(self.engine.world.context, "chat_image_inline", "auto") or "auto"
        ).lower()
        if mode == "never":
            return []
        try:
            if not bool(await self._chat_images_inline(session_id)):
                return []
        except Exception as exc:  # noqa: BLE001 - 判断不了就按"看不见"处理
            self.logger.debug(f"[virtual_world] 判断主模型能否看图失败：{exc}")
            return []
        keep = set(candidates[-cap:])
        older = [url for url in candidates if url not in keep]
        if not older:
            return []
        return await self._caption_old_images(
            event, older, sources=sources, wait=IMG_CAPTION_GRACE_SECONDS
        )

    async def _reply_images(
        self,
        event: AstrMessageEvent,
        sources: list[str],
        *,
        media_ids: list[str] | None = None,
    ) -> tuple[list[str], dict[str, str], list[str], list[dict[str, Any]]]:
        """这一轮直接交给模型看的图、它们的编号（``图1``/``图2``…）、要转成文字的那几张，
        以及**本轮图片清单**（编号 → 内容身份 + 收藏状态，M2 给提示词与展示记账用）。

        **规则**：发送范围（这次真的会进提示词的聊天记录）里**最近的 ``image_max`` 张图**
        都交给主模型看——不管它是这一条消息带的、还是前面几条带的，按时间序编号 图1/图2/…，
        和附件顺序一一对应；比它们更旧、但仍在发送范围内的图只以文字转述出现
        （被压成概览的老图压根不在这个范围里，不用管）。

        - 主模型看不见图时：这几张全部转述成文字（一次打包），不附图；
        - **没配转述模型**时：主模型看不看得见都照样附图（否则这些图就彻底没了）；
        - 已经转述过的图这次被附上了：它的转述不再渲染出来（见
          ``engine.attach_image_captions`` 与 ``prompt._render_rows``）。

        转述只发生一次：本来就要交给主模型看的图不会再转述一遍（白花钱）。

        M2 追加（方案 §3.1 / §4 / §3.3）：

        - 转述时顺带判定「值得补附」的老图，从新到旧补进附件（每轮 ≤ ``meme_image_max``）；
        - 三级图片共用一个预算分配器（``allocate_image_budget``）：直接附图**不可被挤掉**，
          不够就按优先级砍后面的，并如实记 coverage（不假装没超）；
        - 附件按**时间序统一重排**后再编号——补附进来的老图序号在前，编号、附件顺序、
          ``image_marks`` 三者一一对应；
        - **没真正进模型的图不给编号、不记展示**；进模型的图在请求构造时 ``claimed``。
        """

        session_id = event.unified_msg_origin
        context = self.engine.world.context
        cap = max(1, int(context.image_max))
        vision = getattr(self, "vision", None)
        captioned = bool(vision is not None and vision.enabled)
        mode = str(getattr(context, "chat_image_inline", "auto") or "auto").lower()
        inline = await self._chat_images_inline(session_id)
        pending = await self.engine.take_pending_images(session_id)
        # 按时间排好（先来的在前）：被搭话的那几条消息里的图排前面，最新那张在最后
        own = list(
            dict.fromkeys(
                [str(ref).strip() for ref in [*pending, *sources] if str(ref).strip()]
            )
        )
        # 「发送范围里」的图 = 这次真的会进提示词的聊天记录里的图（被压成概览的早就不在这里了，
        # 那种不用管）。取其中**最近的 ``image_max`` 张**交给主模型看，**不管它是这一条消息带的
        # 还是前面几条带的**——编号按时间序 图1/图2/…，和附件顺序一一对应。
        window = await self.engine.chat_images_for_reply(session_id, PENDING_IMAGE_KEEP)
        all_urls = list(
            dict.fromkeys(
                [
                    *[
                        str(item.get("url") or "").strip()
                        for item in window
                        if str(item.get("url") or "").strip()
                    ],
                    *own,
                ]
            )
        )
        # 显式关掉（``never``）就一张都不附；检测不出主模型能不能看图、又没配转述模型时，
        # 照样附过去（否则这些图就彻底没人看了）
        attach = bool(inline) or (not captioned and mode != "never")
        # 用户 2026-10-23（问题1）：原图只在**原图字节还在、而且还没有转述文本**时才附。
        # 以前是"窗口里最近 cap 张无条件重发"：这一轮附过的图又被回灌进 pending，
        # 下一轮再取回来再附一遍，同一张图永远退不出去（实测一张 33 小时前、临时
        # 文件早被删掉的图还在被当成"最近 3 张"发出去）。
        caption_key = str(getattr(vision, "recipe_key", lambda: "")() or "")
        described: set[str] = set()
        if captioned:
            described = {
                str(ref)
                for item in window
                if str(item.get("captions_key") or "") == caption_key
                for ref in (item.get("captions") or {})
                if str(ref)
            }
        starts_reader = getattr(self, "_reply_round_starts", None)
        round_starts = (
            await starts_reader(session_id) if callable(starts_reader) else []
        )
        source_set = {str(ref).strip() for ref in sources if str(ref).strip()}
        # 用户 m10816 / m10817（2026-10-30 第二次改口径）：附件不再是"只附带上来那一轮"，
        # 而是**最近 IMAGE_ATTACH_ROUNDS 轮内都附原图**——她刚看完之后几轮还在聊这件事时，
        # 得能回头再看原图，而不是只剩一句转述。超过窗口才转成一次转述的文字。
        # 另一半是"一轮里新到的图全部附"：这一条消息带的 + pending 里还没入档的都算本轮，
        # 不占 ``image_max`` 的额度，一次来十几张也要一次看完、不许漏看；从下一轮起才
        # 回到"最近 3 张"的上限（``own`` 之外的图那一支）。
        fresh = [url for url in own if url]
        fresh_set = set(fresh)
        # 第一判据还是**原图字节还在不在**（用户 2026-10-23 续）：本地临时文件还读得到、
        # 或者资产库里还有副本，就照原图附。字节已经不在时，才退回老规矩：只有最近
        # IMAGE_REPLY_ROUNDS 轮内、还没转述过的图再试一次原图（让宿主自己去下载远程地址）。
        # 取不到这个能力（测试桩 / 老宿主）就按"都还在"处理，退回老规矩，不误伤。
        bytes_reader = getattr(self, "_image_bytes_available", None)
        try:
            available = bytes_reader(all_urls) if callable(bytes_reader) else set()
        except Exception as exc:  # pragma: no cover - 判字节失败不该挡回复
            self.logger.debug(f"[virtual_world] 图片字节判据失败：{exc}")
            available = set()
        recent = [
            url
            for url in _round_recent_images(window, all_urls, round_starts)
            if url not in described
        ]
        recent_set = set(recent)
        within_attach = set(
            _round_recent_images(
                window, all_urls, round_starts, IMAGE_ATTACH_ROUNDS
            )
        )
        # 老图（不是本轮新到的）：窗口内 + 字节还在（或字节不在但可以再试一次远程），
        # 最多 cap 张按时间序排；**已经有转述描述的不再附**——描述已经是事实来源。
        elders = [
            url
            for url in all_urls
            if url
            and url not in fresh_set
            and url in within_attach
            and (url in available or url in recent_set)
            and (url not in described or url in source_set)
        ]
        # 本轮新到的图全部附（超过 IMAGE_FRESH_MAX 的留给下一轮）；老图只取最近 cap 张。
        primary = (
            list(dict.fromkeys([*elders[-cap:], *fresh[:IMAGE_FRESH_MAX]]))
            if attach
            else []
        )
        primary = [url for url in all_urls if url in set(primary)]
        rest = [url for url in all_urls if url and url not in set(primary)]
        # 描述已经挂在它自己那条聊天记录上的老图，不再重新转述（2026-10-20）：
        # 只看内存里的"转述过"不够——重启、或者地址从临时文件换成资产库路径之后
        # 就又认不出来，于是同一句描述每轮重刷、一次回复被拖到 30~40 秒。
        # 落盘的 captions 是事实来源：那一行已经有描述，就说明这张已经看过了。
        if captioned and rest and described:
            rest = [url for url in rest if url not in described]
        # 第二级：更老的图先转述一次（顺带拿到「值得补附」的候选标记，不额外多花调用）
        flags: dict[str, bool] = {}
        notes: list[str] = []
        caption_state: dict[str, int] = {}
        if captioned and rest:
            notes = await self._caption_old_images(
                event,
                rest,
                sources=sources,
                flags=flags,
                outcome=caption_state,
                # 用户 2026-10-23（问题2）：「图没读出来」这句注记只在**这一轮确实
                # 带了新图**时才拼；纯文字轮次里挂一句"这几张图没读出内容"，她
                # 会以为对方又发了图，越说越像"我看不见"。
                note_failures=bool(source_set),
                # 用户 2026-10-28：带图的轮次也只等 1.5 秒（``IMG_CAPTION_GRACE_SECONDS``），
                # 没转述完就先注一句"图还在加载"，后台接着看；纯文字轮次 0，一秒不等。
                wait=IMG_CAPTION_GRACE_SECONDS if source_set else 0.0,
            )
        meme = self._meme_settings()
        meme_on = bool(meme is not None and getattr(meme, "enabled", False))
        if (
            captioned
            and not int(caption_state.get("captioned") or 0)
            and rest
            and mode != "never"
            and (not attach or not primary)
        ):
            # 转述一张都没成功，而这一轮又没有任何可附的图——`attach` 是假，**或者**
            # `attach` 是真但一张都没选上（字节已经不在）——就把最近 2 轮内、还没转述
            # 过的原图补上。2026-10-20 加：之前这里 attach 保持 False，表现就是"图在，
            # 但她只会说看不见"。2026-10-23 续：条件从 `not attach` 放宽成
            # `(not attach or not primary)`——attach 现在基本都是真，老条件等于死代码。
            rescue = [url for url in recent if url not in set(primary)]
            if rescue:
                attach = True
                primary = list(dict.fromkeys([*primary, *rescue]))[-cap:]
                rest = [url for url in all_urls if url and url not in set(primary)]
                if described:
                    rest = [url for url in rest if url not in described]
            else:
                self.logger.debug(
                    "[virtual_world] 转述一张都没成功，也没有可补的原图"
                    "（字节已经不在）：这一轮只按文字聊"
                )
        overflow: list[str] = []
        coverage: dict[str, Any] | None = None
        if meme_on and attach and rest:
            # 补附只针对**不是这条消息带的**老图：这条消息自己的图已经在第一级算过一遍，
            # 再补一遍会和注解里的「图N」互相打架（注解按它在消息里的位置编号）。
            candidates = [
                url
                for url in reversed(rest)
                if flags.get(url) and url not in source_set
            ]
            if candidates:
                # 单图字节限制要真的生效，就得带上本机文件的真实大小（取不到就是
                # "不知道"，按 0 算——不假装它很小，也不因此把它砍掉）
                facts = self._media_facts([*primary, *candidates])

                def _budget_item(url: str) -> dict[str, Any]:
                    fact = facts.get(str(url)) or {}
                    return {
                        "url": str(url),
                        "media_id": str(fact.get("media_id") or ""),
                        "size_bytes": int(fact.get("size_bytes") or 0),
                    }

                # 用户 m10817：这一轮新到的图**不占** ``image_max`` 的额度——上限只约束
                # 老图。所以预算的"主图上限"至少放到这一轮实际要附的张数，否则一轮发
                # 十几张时预算会把多出来的砍掉，正好砍成"漏看"。
                primary_cap = max(cap, min(IMAGE_FRESH_MAX + cap, len(primary)))
                budget = allocate_image_budget(
                    [_budget_item(url) for url in primary],
                    [_budget_item(url) for url in candidates],
                    (),
                    image_max=primary_cap,
                    meme_image_max=int(getattr(meme, "meme_image_max", 0) or 0),
                    max_candidate_images_per_prompt=int(
                        getattr(meme, "max_candidate_images_per_prompt", 0) or 0
                    ),
                    max_prompt_images=int(getattr(meme, "max_prompt_images", 10) or 10),
                    max_asset_bytes=int(getattr(meme, "max_asset_bytes", 0) or 0),
                    max_preview_bytes=int(
                        getattr(meme, "max_candidate_preview_bytes", 0) or 0
                    ),
                )
                primary = [
                    str(item.get("url") or "") for item in budget.get("primary") or []
                ]
                overflow = [
                    str(item.get("url") or "") for item in budget.get("overflow") or []
                ]
                coverage = dict(budget.get("coverage") or {})
        # 最终附件 = 三级入选的图**按时间序统一重排**（补附进来的老图在前）
        chosen = set(primary) | set(overflow)
        refs = [url for url in all_urls if url in chosen] if attach else []
        refs = self._order_round_images(refs, window=window, own=own)
        images: list[dict[str, Any]] = []
        if refs and meme_on:
            try:
                refs, images = await self._prepare_round_images(
                    session_id,
                    refs,
                    window=window,
                    own_media_ids=self._own_media_ids(sources, media_ids),
                    primary=set(primary),
                    flags=flags,
                    store=self._meme_store(),
                )
            except Exception as exc:
                self.logger.debug(f"[virtual_world] 本轮图片清单整理失败：{exc}")
                images = []
        # ---- 图必达（用户 2026-10-23 真机事故）----------------------------------
        # AstrBot 拿到原始地址后自己解析，解析失败只 warning 一句「图片预处理结果
        # 为空，将忽略。」，插件完全不知情，注解却照样说「已经一起发给你了」。
        # 所以交给模型之前先把字节抓在自己手里：交出去的是 ``base64://`` 快照，
        # 取不到的那几张移出附件、按张记一条 ``note_vision(ok=False)``。
        snapshotter = getattr(self, "_snapshot_for_model", None)
        if callable(snapshotter):
            try:
                handed, handed_raw, lost, lost_detail = await snapshotter(
                    refs, session_id=session_id
                )
            except Exception as exc:  # 快照失败退回原图地址：不能因此把回复搞挂
                self.logger.debug(f"[virtual_world] 图片快照流程异常，退回原图：{exc}")
                handed, handed_raw, lost, lost_detail = list(refs), list(refs), [], ""
        else:
            handed, handed_raw, lost, lost_detail = list(refs), list(refs), [], ""
        # 没取到本地字节时，**远程地址**还是把原地址交给宿主再试一次（它自己会下载，
        # 这是今天就在走的链路）；本地地址（file://、磁盘路径、无 scheme）没字节就是
        # 真的没有，直接移出附件、注解不替它担保。
        retry = [url for url in lost if _host_fetchable_ref(url)]
        if retry:
            self.logger.debug(
                f"[virtual_world] {len(retry)} 张图没取到本地字节，"
                "仍把原地址交给宿主自己再试一次"
            )
        retry_set = set(retry)
        if lost and not handed and not retry:
            # 一张都没交出去：不许再空口承诺，改成一句实话（有图 → 说图没取到）
            if source_set & set(lost):
                notes.append(f"这条消息带了图，但图没取到（{lost_detail}），先按文字聊")
            else:
                notes.append(f"这一轮没能把图发出去（{lost_detail}），先按文字聊")
            return [], {}, notes, []
        delivered = {raw: snapshot for raw, snapshot in zip(handed_raw, handed)}
        final_refs: list[str] = []
        final_raw: list[str] = []
        for url in refs:
            if url in delivered:
                final_refs.append(delivered[url])
                final_raw.append(url)
            elif url in retry_set:
                final_refs.append(url)
                final_raw.append(url)
        if not final_refs:
            return [], {}, notes, []
        refs = final_refs
        # 编号跟着**真正交出去的那几张的原地址**走（聊天记录里的 captions 也按原地址键）
        marks: dict[str, str] = {
            url: f"图{index}" for index, url in enumerate(final_raw, start=1)
        }
        snapshot_bytes = sum(
            max(0, len(item) - len("base64://")) * 3 // 4
            for item in refs
            if item.startswith("base64://")
        )
        # 她自己发过的图也在里面（阶段 B3）：得说清楚那是她自己的图，
        # 别让她把"我上次发的照片"当成对方刚递给她的东西重新感慨一遍
        hers = {
            str(item.get("url") or "").strip()
            for item in window
            if str(item.get("who") or "") == "her"
        }
        own_refs = [url for url in final_raw if url and url in hers]
        if own_refs:
            labels = "、".join(marks[url] for url in own_refs if url in marks)
            if labels:
                notes.append(f"［{labels} 是她自己发过的图，不是对方这次发的］")
        detail = f"把 {len(refs)} 张图交给多模态主模型"
        if retry:
            detail += f"（其中 {len(retry)} 张只是原地址，让宿主自己下）"
        if snapshot_bytes:
            detail += f"（已快照 {snapshot_bytes} 字节）"
        if marks:
            detail += f"（聊天记录里的 {len(marks)} 张已编号）"
        if coverage is not None:
            detail += "；" + budget_coverage_text(coverage)
        await self.engine.note_vision(
            session_id, ok=True, images=len(refs), detail=detail
        )
        # 候选池入池在后台跑：测试里的假插件没有这个方法，所以先取再调。
        note_collect = getattr(self, "_note_meme_collect_seen", None)
        if callable(note_collect):
            try:
                note_collect(session_id, window, images)
            except Exception as exc:  # pragma: no cover - 入池永远不该影响回复
                self.logger.debug(f"[virtual_world] 候选池入池跳过：{exc}")
        return refs, marks, notes, images

    @staticmethod
    def _meme_collect_caption(item: dict[str, Any]) -> str:
        """候选池要的画面描述：转述过的图取第一段转述，没有就空着（评审时写"没看清"）。"""

        captions = item.get("captions")
        if isinstance(captions, dict):
            for value in captions.values():
                text = str(value or "").strip()
                if text:
                    return text
        return ""

    def _note_meme_collect_seen(
        self,
        session_id: str,
        window: list[dict[str, Any]] | None = None,
        images: list[dict[str, Any]] | None = None,
        *,
        sources: list[str] | None = None,
        media_ids: list[str] | None = None,
    ) -> None:
        """把这一轮她"看到过"的图按概率投进候选池（后台协程，fail-soft）。

        「看到过」= 这张图真的进了她的上下文。两个入口都走这里：
        ``on_any_message``（每一条消息都跑，她回不回话都算——用户 2026-10-24：
        原来只挂在 ``_reply_images`` 上，她那一轮不回复的图就永远错过了）和
        ``_reply_images``（补上窗口里重新交给模型看的旧图）。候选池只负责攒，
        不收；收与不收等攒够了一批由 tick 里的评审一次性决定。入池失败只记
        debug，绝不影响这一轮的回复。
        """

        try:
            settings = self.engine.meme_collect_settings()
            if not getattr(settings, "active", False):
                return
            captions: dict[str, str] = {}
            for item in window or ():
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if url and url not in captions:
                    captions[url] = self._meme_collect_caption(item)
            rows: list[dict[str, Any]] = []
            for record in images or ():
                if not isinstance(record, dict):
                    continue
                media_id = str(record.get("media_id") or "").strip()
                if not media_id:
                    continue
                url = str(record.get("url") or "").strip()
                rows.append({"media_id": media_id, "caption": captions.get(url, "")})
            if not rows:
                # 消息刚到（还没转述、也没有窗口记录）这条路径：地址与资产库 id 一一对应
                for url, media_id in zip(sources or (), media_ids or ()):
                    if not str(media_id or "").strip():
                        continue
                    rows.append(
                        {
                            "media_id": str(media_id).strip(),
                            "caption": captions.get(str(url or "").strip(), ""),
                        }
                    )
            if not rows:
                return
            task = asyncio.get_running_loop().create_task(
                self.engine.note_meme_collect_seen(session_id, rows)
            )
            self._meme_collect_tasks.add(task)
            task.add_done_callback(self._meme_collect_tasks.discard)
        except Exception as exc:  # pragma: no cover - 入池永远不该炸
            self.logger.debug(f"[virtual_world] 候选池入池跳过：{exc}")

    @staticmethod
    def _order_round_images(
        urls: list[str], *, window: list[dict[str, Any]], own: list[str]
    ) -> list[str]:
        """把这一轮的附件按**时间序**统一重排（方案 §3.1）。

        聊天记录里的图用留档行的时间排；这一条消息刚带的图（``own``）排在最后——
        它一定是最新的（先来的在前，最新那张在最后）。编号、附件顺序、``image_marks``
        都从这一份顺序出，所以三者天然一一对应。
        """

        at_of: dict[str, float] = {}
        for item in window or []:
            url = str(item.get("url") or "").strip()
            if url and url not in at_of:
                at_of[url] = float(item.get("at") or 0.0)
        own_index = {str(url): index for index, url in enumerate(own or [])}

        def sort_key(pair: tuple[int, str]) -> tuple[int, float, int]:
            index, url = pair
            if url in at_of:
                return (0, at_of[url], index)
            return (1, float(own_index.get(url, index)), index)

        return [url for _, url in sorted(enumerate(urls), key=sort_key)]

    def _meme_settings(self) -> Any:
        """表情包配置的**唯一读取点**（方案 §2.1）。

        关掉时（``world.meme.enabled=False``）这里返回的 settings 自带 ``enabled=False``，
        调用方一律据此走"系统不存在"的老路径——提示词里不出现收藏标注、不附表情包、
        不写展示记录、不发起任何检索。
        """

        try:
            return meme_settings(getattr(self.engine, "world", None))
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 表情包配置读取失败：{exc}")
            return None

    @staticmethod
    def _own_media_ids(
        sources: list[str], media_ids: list[str] | None
    ) -> dict[str, str]:
        """这条消息自己的图 → 内容身份（``_media_ids`` 给的那一份，一一对应）。"""

        out: dict[str, str] = {}
        for source, media_id in zip(sources or [], list(media_ids or [])):
            key = str(source or "").strip()
            value = str(media_id or "").strip()
            if key and value and key not in out:
                out[key] = value
        return out

    def _media_facts(self, urls: list[str]) -> dict[str, dict[str, Any]]:
        """图片地址 → 本机资产的实情（``media_id`` 与字节数）。

        身份是**内容哈希，不是 URL**（方案 §1）：同一个地址换了图就是另一张。
        取不到就返回空——调用方必须按"不知道"处理，不许猜（不许编编号、也不许
        假装知道它有多大）。同地址多份时取最近用过、且本机文件还在的那一份
        （与 ``AstrBotVision._prepare_sources`` 同口径）。字节数给预算分配器用：
        超过单图上限的图**进不了请求**，那就不能给它编号、更不能记展示（方案 §4）。
        """

        wanted = {str(url).strip() for url in (urls or []) if str(url).strip()}
        found: dict[str, dict[str, Any]] = {}
        if not wanted:
            return found
        try:
            store = self.engine.media_store()
            manifest = store.manifest()
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 资产清单读取失败：{exc}")
            return found
        for media_id, asset in sorted(
            manifest.items(),
            key=lambda item: (
                float(item[1].get("last_used_at") or 0),
                float(item[1].get("created_at") or 0),
            ),
            reverse=True,
        ):
            relevant = [
                str(source).strip()
                for source in asset.get("sources", []) or []
                if str(source).strip() in wanted
                and str(source).strip() not in found
            ]
            if not relevant or not self.engine.media_file_source(media_id):
                continue
            for source in relevant:
                found[source] = {
                    "media_id": str(media_id),
                    "size_bytes": int(asset.get("size") or 0),
                }
        return found

    def _media_id_map(self, urls: list[str]) -> dict[str, str]:
        """``_media_facts`` 的薄封装：只要内容身份的那几处用它。"""

        return {
            url: str(fact.get("media_id") or "")
            for url, fact in self._media_facts(urls).items()
        }

    def _image_bytes_available(self, urls: list[str]) -> set[str]:
        """这些图片地址里，**原图字节现在还在本机**的那些（用户 2026-10-23 续）。

        判据从「她回过几轮」换成「字节还在不在」：只要本地临时文件还读得到
        （``_readable_local_file``），或者资产库里有它的副本（``_media_facts``
        命中，``media_file_source`` 会返回真实路径），就照原图附——不管过去了
        多少轮。判断本身失败一律当"不在"：宁可不附，也不要为了一张已经没了的
        图拖慢这一轮。
        """

        found: set[str] = set()
        for url in urls or []:
            text = str(url or "").strip()
            if not text:
                continue
            try:
                if _readable_local_file(text):
                    found.add(text)
            except Exception as exc:  # pragma: no cover - 路径判断不该炸
                self.logger.debug(f"[virtual_world] 图片本地文件检查失败：{exc}")
        missing = [
            str(url or "").strip()
            for url in urls or []
            if str(url or "").strip() and str(url).strip() not in found
        ]
        if missing:
            try:
                found.update(self._media_facts(missing))
            except Exception as exc:
                self.logger.debug(f"[virtual_world] 图片资产库检查失败：{exc}")
        return found

    async def _snapshot_for_model(
        self, refs: list[str], *, session_id: str
    ) -> tuple[list[str], list[str], list[str], str]:
        """把要交给模型的图先取成本地字节快照，返回
        ``(交给模型看的地址, 成功的原地址, 失败的原地址, 失败说明)``。

        为什么非要先抓自己手里：AstrBot 那边拿到原始地址后自己解析，失败只
        ``logger.warning("图片预处理结果为空，将忽略。")``，插件完全不知情，注解
        却照样说「已经一起发给你了」（2026-10-23 真机事故）。这里换成
        ``base64://`` 快照之后，成功与否都是我们自己的事实：取不到的移出附件，
        并按张记一条 ``note_vision(ok=False)``。

        fail-soft：没有快照能力（老宿主 / 测试替身）或 ``_prepare_sources`` 抛异常
        时**原样返回**，等于退回今天的行为，不因为图片处理把整轮回复搞挂。
        **base64 内容永远不进日志/事件**（只记条数、字节数）。
        """

        wanted = [str(ref or "").strip() for ref in refs or [] if str(ref or "").strip()]
        if not wanted:
            return [], [], [], ""
        vision = getattr(self, "vision", None)
        prepare = getattr(vision, "_prepare_sources", None)
        if not callable(prepare):
            return list(wanted), list(wanted), [], ""
        try:
            snapshots = list(await prepare(list(wanted), session_id=session_id) or [])
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 图片快照失败，退回原图地址：{exc}")
            return list(wanted), list(wanted), [], ""
        handed: list[str] = []
        kept: list[str] = []
        lost: list[str] = []
        for index, raw in enumerate(wanted):
            snapshot = ""
            if index < len(snapshots):
                snapshot = str(snapshots[index] or "").strip()
            if snapshot:
                handed.append(snapshot)
                kept.append(raw)
            else:
                lost.append(raw)
        detail = ""
        if lost:
            reasons: list[str] = []
            for raw in lost:
                position = wanted.index(raw) + 1
                try:
                    problem = _local_file_problem(raw)
                except Exception:  # pragma: no cover - 归因失败也别挡路
                    problem = "没取到字节"
                reasons.append(f"图{position} {problem}")
            detail = "；".join(reasons)
            try:
                await self.engine.note_vision(
                    session_id,
                    ok=False,
                    images=len(lost),
                    detail=(
                        f"这批 {len(wanted)} 张里有 {len(lost)} 张没取到图字节：{detail}"
                    ),
                )
            except Exception as exc:
                self.logger.debug(f"[virtual_world] 图片失败事件写入失败：{exc}")
        return handed, kept, lost, detail

    async def _collected_references(
        self, urls: list[str], *, session_id: str
    ) -> dict[str, str]:
        """这几张图里**已经收藏过**的，各给一行"引用"（收藏编号 + 内容 hash）。

        返回 ``{图片地址: 引用行}``；收藏功能关着、取不到内容身份、或那条收藏没有
        可用语义时都不返回它——调用方回退到转述（设计 §13.1）。
        """

        settings = self._meme_settings()
        if not getattr(settings, "enabled", False):
            # 总开关关着：连资产库都不碰（与"系统不存在"逐字节一致）
            return {}
        store = self._meme_store()
        known: dict[str, str] = {}
        ids = self._media_id_map(urls)
        for url in urls:
            media_id = ids.get(str(url).strip(), "")
            if not media_id:
                continue
            try:
                caption = await asyncio.to_thread(
                    store.reference_caption,
                    media_id,
                    scope_kind=_scope_kind_for_session(session_id),
                    scope_id=session_id,
                )
            except Exception as exc:
                self.logger.debug(f"[virtual_world] 收藏查询失败：{exc}")
                continue
            if caption:
                known[str(url).strip()] = str(caption)
        return known

    async def _prepare_round_images(
        self,
        session_id: str,
        refs: list[str],
        *,
        window: list[dict[str, Any]],
        own_media_ids: dict[str, str],
        primary: set[str],
        flags: dict[str, bool],
        store: Any,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """把这一轮真正要交给模型的图整理成清单，并把展示记账认领下来（M2）。

        清单是**编号的唯一来源**（方案 §5）：``图N`` 只出现在这里，模型只能照抄；
        收藏状态一律查 ``MemeStore``，不许凭"好像见过"自己编编号。

        记账（方案 §3.3）：请求构造时 ``claimed``；``delivered`` 封死这次出现、
        ``failed`` 下一轮可重试、并发靠主键事务只让一个请求认领成功。**直接附图
        不受认领结果影响**（用户这一轮的图不能被挤掉）；补附图认领不到就不附。
        """

        seat: dict[str, dict[str, Any]] = {}
        for item in window or []:
            url = str(item.get("url") or "").strip()
            if url:
                seat.setdefault(url, item)
        scope_kind = _scope_kind_for_session(session_id)
        records: list[dict[str, Any]] = []
        for url in refs:
            row = seat.get(url) or {}
            media_id = str(row.get("media_id") or own_media_ids.get(url) or "")
            seq = int(row.get("seq") or 0)
            position = int(row.get("index") or 0)
            status: dict[str, Any] = {}
            if media_id:
                try:
                    status = await asyncio.to_thread(
                        store.image_status,
                        media_id,
                        scope_kind=scope_kind,
                        scope_id=session_id,
                    )
                except Exception as exc:
                    self.logger.debug(f"[virtual_world] 收藏状态查询失败：{exc}")
                    status = {}
            records.append(
                {
                    "label": "",
                    "url": url,
                    "media_id": media_id,
                    "seq": seq,
                    "index": position,
                    "tier": "primary" if url in primary else "overflow",
                    "replenish": bool(flags.get(url)),
                    "collected": bool(status.get("collected")),
                    "usable": bool(status.get("usable")),
                    "entry_id": str(status.get("entry_id") or ""),
                    "name": str(status.get("name") or ""),
                    "state": str(status.get("state") or ""),
                    "status_text": _meme_status_text(status),
                    "occurrence_id": (
                        occurrence_id_for(session_id, seq, position) if seq else ""
                    ),
                    "phase_id": PHASE_REPLY,
                    "claim_ok": False,
                    "claim_reason": "",
                }
            )
        claims: list[dict[str, Any]] = []
        if store is not None:
            claims = await asyncio.to_thread(
                store.claim_exposures,
                [
                    {
                        "occurrence_id": item["occurrence_id"],
                        "media_id": item["media_id"],
                        "session_id": session_id,
                        "url": item["url"],
                    }
                    for item in records
                    if item["occurrence_id"]
                ],
                phase_id=PHASE_REPLY,
            )
        by_url = {str(item.get("url") or ""): item for item in claims or []}
        kept: list[dict[str, Any]] = []
        for item in records:
            claim = by_url.get(item["url"])
            if claim is None:
                # 这条消息的行还没落库（没有留档坐标）：没有 occurrence，也就没有
                # "一次性机会"可言——照样附上，只是不记账
                kept.append(item)
                continue
            item["claim_ok"] = bool(claim.get("ok"))
            item["claim_reason"] = str(claim.get("reason") or "")
            if item["claim_ok"] or item["tier"] == "primary":
                kept.append(item)
        final = [str(item["url"]) for item in kept]
        for index, item in enumerate(kept, start=1):
            item["label"] = f"图{index}"
        return final, kept


    def _caption_refs(self, urls: list[str]) -> dict[str, str]:
        """把图片地址换成"内容身份"引用，供"这张看过了"记账用。

        同一张图会被不同的地址指向：OneBot 给的是每次都不同的临时文件，过期后
        ``pick_chat_images`` 又换成资产库路径。按地址记账等于每轮都认不出来，
        所以能查到资产就用 ``media:<内容 id>``，查不到才退回地址。
        """

        try:
            store = self.engine.media_store()
        except Exception:  # noqa: BLE001 - 资产库不可用就别拦着转述
            store = None
        out: dict[str, str] = {}
        for url in urls or []:
            text = str(url or "").strip()
            if not text:
                continue
            ref = text
            if store is not None:
                try:
                    asset = store.by_source(text)
                except Exception:  # noqa: BLE001
                    asset = None
                media_id = str(getattr(asset, "media_id", "") or "")
                if media_id:
                    ref = f"media:{media_id}"
            out[text] = ref
        return out

    async def _reply_round_starts(self, session_id: str) -> list[float]:
        """她在**这个会话**里已经回过的那几轮（起点时间，旧的在前）。

        只用来算"这张图是几轮前的"（用户 2026-10-23：超过 2 轮的图不附原图）。
        读不到就返回空表——数不出来不该反过来少给她看图。
        """

        try:
            async with self.engine.session_state(session_id) as state:
                rows = self.engine.chat_window(state, session_id)
        except Exception as exc:  # noqa: BLE001 - 数不出来按"她还没回过"算
            self.logger.debug(f"[virtual_world] 取她的回复轮次失败：{exc}")
            return []
        return _reply_round_starts(list(rows or ()))

    async def _caption_old_images(
        self,
        event: AstrMessageEvent,
        urls: list[str],
        *,
        sources: list[str] | None = None,
        flags: dict[str, bool] | None = None,
        wait: float | None = IMG_CAPTION_WAIT_SECONDS,
        outcome: dict[str, int] | None = None,
        note_failures: bool = True,
    ) -> list[str]:
        """把这几张图打包转述一次，返回要写进这条消息注解的那几句。

        挂在哪：属于**这条消息**的图（她正看着的那几张）写进这条消息的注解；
        更早的图把描述挂回它们原来那条聊天记录上——不然描述会串到别人头上，
        下一轮她也对不上"哪张图是什么"。

        ``flags``（M2）：给了就把每张图「值不值得补附」的候选标记回填进去
        （方案 §3.1）——跟画面描述同一次调用判定，**不额外多花一次模型调用**。

        ``wait``（用户 m08448 / 2026-10-23 续）：最多等转述多久。**带图的那一轮**
        给 ``IMG_CAPTION_WAIT_SECONDS``（10 秒）——1.5 秒经常不够一次多模态调用，
        超时就算了等于把这一轮的老图扔进黑洞；**纯文字轮次**给 0（调用方传，见
        ``_reply_images``），一点都不多等。等不到也不是"当没发生"：后台继续看完
        （描述写进缓存，下一轮直接用），这一轮返回一句"还没转述完"，并记一条
        ``caption_timeout`` 事件。后台协程（:meth:`_caption_in_background`）传
        ``None``，一直等到看完。

        ``outcome``（用户 2026-10-23）：给了就回填这一轮的结果计数
        （``captioned`` / ``failed`` / ``blocked``）——调用方按"一张都没成功"决定
        要不要退回附图，不能靠"返回的注记是不是空的"（注记现在会被条件挡住）。

        ``note_failures``（用户 2026-10-23，问题2）：失败时要不要返回那句「没读出来」。
        只有**这一轮确实带了新图**时才该为真；纯文字轮次挂一句"这几张图没读出内容"，
        她会以为对方又发了图。

        **已经收藏过的图不再重转述**（设计 §13.1）：改写一行引用（收藏编号 +
        内容 hash）。这样 ``captions`` 槽里放的是收藏语义（"这是猫猫探头表情包"）
        而不是重新描述的画面——记忆压缩拿到的就是这句，是故意的。
        """

        session_id = event.unified_msg_origin
        vision = getattr(self, "vision", None)
        if vision is None or not vision.enabled:
            return []

        def _mark(captioned: int = 0, failed: int = 0, blocked: int = 0) -> None:
            if outcome is None:
                return
            outcome["captioned"] = int(outcome.get("captioned") or 0) + int(captioned)
            outcome["failed"] = int(outcome.get("failed") or 0) + int(failed)
            outcome["blocked"] = int(outcome.get("blocked") or 0) + int(blocked)

        caption_key = str(getattr(vision, "recipe_key", lambda: "")() or "")
        await self.engine.invalidate_image_captions(session_id, key=caption_key)
        # 先解析当前字节再查内容缓存，同一个 URL 也可能换图。
        targets = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))
        if not targets:
            return []
        # 收藏查询要排在转述之前：命中的图只写引用，不调转述模型（设计 §13.3）。
        # 收藏这一层是**旁路**：查不动就回退转述，绝不因为表情包把转述拖垮。
        try:
            collected = await self._collected_references(targets, session_id=session_id)
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 收藏查询失败，这几张按未收藏转述：{exc}")
            collected = {}
        pending = [url for url in targets if url not in collected]
        # 已经转述过的那几张不再重转述（2026-10-20）：
        # 以前每一轮都把窗口里所有老图重看一遍，表现是同一句描述反复刷、
        # 一次回复被拖到 30~40 秒，而且"新图到底有没有被看"根本看不出来。
        # 记账用**内容身份**（资产库 media id）而不是地址：临时文件的地址会变、
        # 过期后还会被换成资产库路径，按地址记等于每次都是"没见过"。
        refs = self._caption_refs(targets)
        fresh = set(self.engine.uncaptioned_images(list(refs.values()), key=caption_key))
        # 留档里已经挂着描述的那些也算"看过"：进程里那份记录重启就没了，
        # 但描述还挂在聊天记录上（跨会话共享留档，重启后不该整窗重看一遍）。
        try:
            covered = set(
                await self.engine.captioned_image_refs(session_id, key=caption_key)
            )
        except Exception as exc:  # noqa: BLE001 - 读不到就只靠内存那份记账
            self.logger.debug(f"[virtual_world] 读已转述清单失败，只按内存记账：{exc}")
            covered = set()
        # 留档里记的是地址，这里要换算成同一套"内容身份"再比
        covered = {refs.get(url, url) for url in covered} | covered

        def _seen(url: str) -> bool:
            ref = refs.get(url, url)
            return ref not in fresh or ref in covered or url in covered

        skipped = [url for url in pending if _seen(url)]
        pending = [url for url in pending if not _seen(url)]
        # 用户 2026-10-23（问题2）：**转述失败**过的图（临时文件被清理 / 超过 10MB /
        # 根本不是图片）在 ``IMG_CAPTION_FAILURE_TTL_SECONDS`` 内不再重试、也不再
        # 重复注记。以前只有 60 秒的进程内负缓存，坏图每轮重看一次、每轮跟她说一遍
        # "看不见"，听上去像她一直没收到图。失败标记按**内容身份**落库，重启也认。
        if pending:
            failed_recently = await self._load_caption_failures(
                [refs.get(url, url) for url in pending]
            )
            blocked = [url for url in pending if refs.get(url, url) in failed_recently]
            if blocked:
                self.logger.debug(
                    f"[virtual_world] {len(blocked)} 张图 "
                    f"{IMG_CAPTION_FAILURE_TTL_SECONDS / 3600:.0f} 小时内转述失败过，"
                    "这次不再重试"
                )
                pending = [
                    url for url in pending if refs.get(url, url) not in failed_recently
                ]
            _mark(blocked=len(blocked))
        if skipped:
            self.logger.debug(
                f"[virtual_world] {len(skipped)} 张图已经转述过（配方 {caption_key}），跳过；"
                f"这次要看的 {len(pending)} 张"
            )
        if not pending and not collected:
            # 没有新图、也没有需要补写的收藏引用：这一轮不用看，也不必记事件
            _mark()
            return []
        captions: list[str] = []
        looked: list[bool] = []
        if pending:
            call = vision.describe(
                pending,
                question=str(event.get_message_str() or ""),
                quoted=_quoted_text(event),
                context_lines=await self._recent_chat_lines(event),
                session_id=session_id,
                flags=looked,
                scene_hint=_sticker_hint(_sticker_marks(event, len(pending))),
            )
            try:
                if wait is None:
                    captions = await call
                else:
                    # 超时**不取消**，让它继续在后台跑完（描述写进缓存，下一轮直接用）
                    task = asyncio.ensure_future(call)
                    # 记账只为"别让它被回收"；测试桩不走 __init__ 时没有这个集合
                    tasks = getattr(self, "_caption_tasks", None)
                    if tasks is not None:
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    captions = await asyncio.wait_for(
                        asyncio.shield(task), timeout=max(0.05, float(wait))
                    )
            except asyncio.TimeoutError:
                # 用户 2026-10-23（问题3）：超时**不再 return [] 当没发生**。
                # 后台继续看（描述写进缓存，下一轮直接用），但这一轮要留下明确
                # 痕迹：一条 ``caption_timeout`` 事件 + 一句"还没转述完"的注记。
                # 纯文字轮次（``note_failures=False``）不写事件、不注记，免得每轮刷。
                self.logger.debug(
                    f"[virtual_world] 转述还没看完（{len(pending)} 张，"
                    f"等了 {wait} 秒），后台继续；这一轮按未完成处理"
                )
                _mark()
                if not note_failures:
                    return []
                try:
                    await self.engine.note_vision(
                        session_id,
                        ok=False,
                        images=len(pending),
                        detail=(
                            f"caption_pending：这 {len(pending)} 张还在转述，"
                            "这一轮先按「还在加载」注记，不阻塞回复（后台继续看）"
                        ),
                    )
                except Exception as exc:  # pragma: no cover - 事件写不进去也别挡回复
                    self.logger.debug(f"[virtual_world] 超时事件写入失败：{exc}")
                # 用户 2026-10-28：不阻塞、也不说"没读出来"——就当她还在加载。
                # 这句是给主模型看的：这一轮先按图里大概有什么理解，别编细节。
                return [
                    f"这轮的 {len(pending)} 张图还在加载（后台正在转述）：先按图里"
                    "大概的样子接话，别编画面细节，也别反复说看不见；下一轮描述就补上了"
                ]
            except Exception as exc:
                self.logger.debug(f"[virtual_world] 图片转述异常：{exc}")
                captions = []
                looked = []
        if flags is not None:
            for url, flag in zip(pending, looked or []):
                flags[url] = bool(flag)
            # 已经收藏过的图不用再"补附"：收藏记录已经在手，收它的机会不需要再给
            for url in collected:
                flags[url] = False
        pairs = list(collected.items())
        pairs.extend(
            (url, text)
            for url, caption in zip(pending, captions or [])
            if (text := _validated_caption(caption))
        )
        failed_urls = [
            url for url in pending if url not in {target for target, _text in pairs}
        ]
        if not pairs:
            reason = str(getattr(vision, "last_error", "") or "转述没有返回内容")
            labels = self._caption_failure_labels(failed_urls or targets, sources=sources)
            # 失败要**落库记账**（带 TTL）：这几张在 6 小时内不再重试也不再重复注记
            await self._remember_caption_failures(
                [refs.get(url, url) for url in (failed_urls or targets)],
                self._caption_reason_text(reason),
            )
            await self.engine.note_vision(
                session_id,
                ok=False,
                images=len(targets),
                detail=f"{labels} 没读出来：{self._caption_reason_text(reason)}",
            )
            _mark(failed=len(failed_urls or targets))
            if not note_failures:
                # 用户 2026-10-23（问题2）：这一轮没有新图（或者这几张不是这一轮的
                # 新图），就**不往用户文本里拼**这句。以前无条件拼——纯文字消息后面
                # 也挂一句"这几张图没读出内容"，而失败又不记账，坏图每轮重试、
                # 每轮说一次，自激成"她反复说看不见"。
                return []
            return [
                f"{labels} 没读出来（{self._caption_reason_text(reason)}）；"
                "别编画面，也别反复说看不见"
            ]
        failed_urls = [
            url
            for url in pending
            if url not in {target for target, _text in pairs}
        ]
        if failed_urls:
            # 一部分成功、一部分失败：成功的清标记，失败的下次不再重试
            await self._remember_caption_failures(
                [refs.get(url, url) for url in failed_urls],
                self._caption_reason_text(
                    str(getattr(vision, "last_error", "") or "转述没有返回内容")
                ),
            )
        here = {str(item).strip() for item in (sources or [])}
        # 这条消息里的第几张：编号要跟「图1/图2」对得上，不能按"这次转述了几张"重排
        order = {
            str(url).strip(): index + 1
            for index, url in enumerate(sources or [])
            if str(url).strip()
        }
        notes: list[str] = []
        elsewhere: dict[str, str] = {}
        for url, caption in pairs:
            text = readable_caption(caption)
            if not text:
                continue
            if url in here:
                notes.append(f"图{order.get(url, len(notes) + 1)}：{text}")
            else:
                elsewhere[url] = text
        attached = await self.engine.attach_image_captions(session_id, elsewhere, key=caption_key)
        self.engine.mark_images_captioned(
            [refs.get(url, url) for url, _text in pairs], key=caption_key
        )
        # 看成了：把之前的失败标记清掉（下次换的新图不该被旧标记挡住）
        await self._forget_caption_failures([refs.get(url, url) for url, _text in pairs])
        _mark(captioned=len(pairs))
        await self.engine.note_vision(
            session_id,
            ok=True,
            images=len(pairs),
            detail=(
                f"转述了 {len(pairs)} 张（没随这次请求交给主模型的那几张）"
                + (f"，其中 {attached} 张挂回了它们原来那条消息" if attached else "")
                + "："
                + "；".join(text for _url, text in pairs)
            ),
        )
        return notes

    async def _load_caption_failures(self, fingerprints: list[str]) -> set[str]:
        """这些图（按内容身份）里，:data:`IMG_CAPTION_FAILURE_TTL_SECONDS` 内转述失败过的。

        用户 2026-10-23（问题2）：失败必须**落库**——进程内那份负缓存只有 60 秒，
        重启就没了，而"文件已被清理 / 超过 10MB / 不是图片"这几件几小时内不会变好。
        读不到库就按"没失败过"处理（不能因为记账坏了就看不到图）。
        """

        keys = [str(item) for item in fingerprints or [] if str(item)]
        if not keys:
            return set()
        db = getattr(self, "db", None)
        if db is None:  # 替身/测试里没有库：按"没失败过"处理
            return set()
        try:
            rows = await db.call(
                "recent_image_caption_failures", keys, IMG_CAPTION_FAILURE_TTL_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - 记账读不出来不该拦住转述
            self.logger.debug(f"[virtual_world] 读转述失败标记失败（按没失败处理）：{exc}")
            return set()
        return {str(item) for item in (rows or []) if str(item)}

    async def _remember_caption_failures(
        self, fingerprints: list[str], reason: str
    ) -> None:
        """记下"这几张看不成"（带 TTL）：TTL 内不再重试、也不再重复注记。"""

        keys = [str(item) for item in fingerprints or [] if str(item)]
        if not keys:
            return
        db = getattr(self, "db", None)
        if db is None:
            return
        try:
            await db.call(
                "record_image_caption_failures",
                keys,
                str(reason or ""),
                IMG_CAPTION_FAILURE_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 记转述失败标记失败：{exc}")

    async def _forget_caption_failures(self, fingerprints: list[str]) -> None:
        """这几张看成了：清掉失败标记。"""

        keys = [str(item) for item in fingerprints or [] if str(item)]
        if not keys:
            return
        db = getattr(self, "db", None)
        if db is None:
            return
        try:
            await db.call("clear_image_caption_failures", keys)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 清转述失败标记失败：{exc}")

    @staticmethod
    def _caption_failure_labels(urls: list[str], *, sources: list[str] | None) -> str:
        """失败的是**哪几张**（用户 2026-10-23，问题2）。

        这条消息里带来的按「图N」（和附件编号一致）；更早的图这一轮根本没进附件，
        再叫它「图N」会和真的附件编号打架，所以按「记录里的第N张」编号。
        """

        order = {
            str(url).strip(): index + 1
            for index, url in enumerate(sources or [])
            if str(url).strip()
        }
        labels: list[str] = []
        older = 0
        for url in urls or []:
            key = str(url).strip()
            if not key:
                continue
            if key in order:
                labels.append(f"图{order[key]}")
            else:
                older += 1
                labels.append(f"记录里的第 {older} 张")
        return "、".join(dict.fromkeys(labels)) or "这几张图"

    @staticmethod
    def _caption_reason_text(reason: str) -> str:
        """把 ``last_error`` 修成一句能直接读的原因（去掉重复的"图片没读成："前缀）。"""

        text = " ".join(str(reason or "").split())
        for prefix in ("图片没读成：", "图片没读成:", "看图失败：", "看图失败:"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
        if not text or text == "图片未能取得有效本地资产，不能判断画面":
            return "转述没有返回内容"
        return text

    async def _chat_images_inline(self, session_id: str) -> bool:
        """聊天记录里的图要不要直接发给主模型（配置 + Provider 勾选的模态）。"""

        mode = str(
            getattr(self.engine.world.context, "chat_image_inline", "auto") or "auto"
        ).lower()
        if mode == "never":
            sees = False
        elif mode == "always":
            sees = True
        else:
            sees = bool(await self._main_model_sees_images(session_id))
        # 引擎也要知道（用户 m14731）：她能不能"看见图再挑表情包"取决于这个。
        # 纯文字模型时表情包候选图附上也白附，引擎那边会改成按检索分替她挑。
        try:
            self.engine.sees_images = sees
        except Exception:  # noqa: BLE001 - 只是记账，失败不影响这一轮
            pass
        return sees

    async def _main_model_sees_images(self, session_id: str) -> bool | None:
        """主模型能不能吃图：读 AstrBot 里这个 Provider 勾选的模态。

        读不到（老版本、没有这个字段、不是 AstrBot 的 Provider）返回 ``None``——
        调用方按"不能"处理，用户可以把它改成 ``always`` 强制打开。
        """

        try:
            provider_id = (getattr(self, "llm_provider_id", "") or "").strip()
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(session_id)
            if not provider_id:
                return None
            provider = self.context.get_provider_by_id(provider_id)
            return _provider_supports_images(provider)
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 读主模型的模态失败：{exc}")
            return None

    async def _recent_chat_lines(self, event: AstrMessageEvent) -> list[str]:
        """最近几句群聊，用来给图片转述提供话题背景。"""

        session_id = event.unified_msg_origin
        try:
            state = await self.engine.load_state(session_id, cold_start=False)
            items = self.engine.chat_context(state)
        except Exception:
            return []
        lines: list[str] = []
        for item in items:
            who = "她" if item.get("is_self") else (item.get("name") or item.get("user_id") or "")
            content = " ".join(str(item.get("text") or "").split())
            if content:
                lines.append(f"{who}: {content[:80]}")
        return lines

    def set_self_initiated(self) -> int:
        """标记"接下来这段是我们自己发起的调用"。"""

        self._self_initiated_depth += 1
        task = self._current_task()
        if task is not None:
            self._self_initiated_tasks.add(task)
            # 防御：万一某次调用没走到 reset（异常被吞、任务被取消……），
            # 集合里堆着死任务会让后面的钩子判断出现意外，定期清一次只留当前任务。
            if len(self._self_initiated_tasks) > 8:
                self._self_initiated_tasks.clear()
                self._self_initiated_tasks.add(task)
        return self._self_initiated_depth

    def reset_self_initiated(self, _token: int = 0) -> None:
        self._self_initiated_depth = max(0, self._self_initiated_depth - 1)
        task = self._current_task()
        if task is not None:
            self._self_initiated_tasks.discard(task)

    @staticmethod
    def _current_task() -> asyncio.Task | None:
        try:
            return asyncio.current_task()
        except RuntimeError:
            # 没有正在跑的事件循环（同步收尾之类），当作"不在任务里"
            return None

    def in_self_initiated_call(self) -> bool:
        """当前这条协程是不是"我们自己正在调模型"的那一条。

        按协程判断（而不是"只要有调用在跑"）：我们自己的请求依旧不会被自己重复接管，
        但同一时间到达的真人消息走的是另一条协程，照常处理。
        """

        task = self._current_task()
        if task is None:
            # 没有正在跑的事件循环（同步收尾之类）：只能按"有没有调用在飞"粗判
            return self._self_initiated_depth > 0
        return task in self._self_initiated_tasks

    def _should_expect_reply(self, event: Any) -> bool:
        """这条消息"看起来会走到回复"吗（用来提前登记连发合并）。

        只有会被回复的消息才登记：群里没点名她的闲聊不登记，
        否则群一直有人说话，她的安静期就永远刷新不完。
        """

        text = str(event.get_message_str() or "")
        if _is_command(text) or _is_command_event(event):
            return False
        return bool(
            event.is_wake_up() or _is_at_bot(event) or event.is_private_chat()
        )

    async def _she_is_asleep(self, session_id: str) -> bool:
        """她现在是不是睡着（只读一次会话状态；读不到就当醒着，别把回复卡掉）。"""

        try:
            async with self.engine.session_state(session_id) as state:
                return bool(self.engine.is_asleep(state))
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 读睡眠状态失败：{exc}")
            return False

    def _reply_lock_key(self, session_id: str) -> str:
        """排队 / 去重用的那把锁的名字：按"她"分（会话组的代表）。

        同一个她的两个会话不能同时开两条回复管线，否则两边会轮流改同一份状态，
        计划和动作互相踩。
        """

        return self.engine.merge_scope(session_id) if self.engine else session_id

    def _is_duplicate_delivery(self, session_id: str, event: Any = None) -> bool:
        """这条消息是不是刚刚已经处理过的那一条（重投 / 两个钩子各记一次）。

        用户 m19362：意图路由把消息吞下去再重投时，同一句话会第二次进到回复管线；
        它既会打断上一轮（上一轮就是它自己），又被"已并入上一条"挡掉，最后谁都没回。
        这里在开一轮回复之前认一次身份，命中就整条跳过——不排队、不打断、不重发。

        只在真要开一轮时调：身份记录留着 ``REPLY_DUPLICATE_TTL_SECONDS`` 那么久。
        """

        identity = _reply_identity(event)
        if not identity:
            return False
        key = self._reply_lock_key(session_id)
        now = time.time()
        seen = self._reply_seen.setdefault(key, {})
        for item, stamp in list(seen.items()):
            if now - float(stamp or 0.0) > REPLY_DUPLICATE_TTL_SECONDS:
                seen.pop(item, None)
        if identity in seen:
            return True
        seen[identity] = now
        return False

    def _reply_lock(self, session_id: str) -> asyncio.Lock:
        key = self._reply_lock_key(session_id)
        lock = self._reply_locks.get(key)
        if lock is not None:
            return lock
        if len(self._reply_locks) > 128:
            # 会话多起来以后顺手回收没在用的那些，别一直攒着
            for key in [
                key
                for key, value in self._reply_locks.items()
                if not value.locked()
            ]:
                self._reply_locks.pop(key, None)
        lock = asyncio.Lock()
        self._reply_locks[key] = lock
        return lock

    def _reply_retry_running(self, session_id: str) -> bool:
        task = self._reply_retries.get(str(session_id or ""))
        return task is not None and not task.done()

    @contextlib.asynccontextmanager
    async def _reply_turn(self, session_id: str, event: Any = None):
        """同一会话里，接管按到达顺序排队执行。

        前一条还在等她开口时，后一条先等着——不然两条回复都是照着"回复前"的状态写出来的，
        会互相不知道对方说了什么、重复回应同一件事。等太久（前一条卡住了）就不再等，
        宁可多说一句，也别一直不说话。

        ``event`` 只是用来打点：排队等了多久会记进这一条消息的时间线。
        """

        lock = self._reply_lock(session_id)
        key = self._reply_lock_key(session_id)
        identity = _reply_identity(event)
        acquired = False
        waited = time.time()
        # 握着锁的就是这条消息自己（重投 / 双钩子各记一次）：那不是"新消息"，
        # 不能打断自己（用户 m19362）。
        same_message = bool(identity and identity == self._reply_owner.get(key))
        # 上一轮还在生成时又来了新消息：按用户要的语义，上一轮作废并回退水位线，
        # 这一轮看到的就是「上一轮回复之前」的状态（等于那一轮没发生过）。
        #
        # 但「新消息」必须是**同一个会话里**的新消息（用户 m19936）：分过组的会话
        # 共用一把锁（她只有一个），以前群里来一句就把私聊里正在生成的那轮作废，
        # 私聊那句再也等不到回复。现在只有正握着锁的那个会话才打断它自己，
        # 别的会话安静排队（等到就有锁，等不到就这一条不回，与以前一致）。
        same_session = (
            str(getattr(self, "_reply_session", {}).get(key) or "")
            == str(session_id or "")
        )
        if (
            lock.locked()
            and not same_message
            and same_session
            and getattr(self, "engine", None) is not None
        ):
            # 连着被打断太多次就先让她说完（上限与窗口在 engine 里：3 次 / 180 秒）。
            # 用户 m09726：不要在提示词里加限制，把原来那条上限复用上就行。
            allowed = True
            try:
                allowed = bool(self.engine.interrupt_allowed(session_id))
            except Exception:  # noqa: BLE001 - 判断不了就当允许，别挡住这一条
                allowed = True
            if allowed:
                try:
                    self.engine.request_cancel(session_id, reason="新消息打断上一轮")
                except Exception as exc:  # noqa: BLE001 - 取消请求发不出去不该挡住这一条
                    if self.debug:
                        self.logger.info(f"[virtual_world] 发不出取消请求：{exc}")
            elif self.debug:
                self.logger.info("[virtual_world] 连着打断太多次，这一轮让她先说完")
        try:
            await asyncio.wait_for(lock.acquire(), timeout=REPLY_TURN_WAIT_SECONDS)
            acquired = True
            if identity:
                self._reply_owner[key] = identity
            self._reply_session[key] = str(session_id or "")
            # 拿到锁说明上一轮已经结束了：那条取消请求不该继续留在表里，
            # 否则这一轮自己一开口就被它作废。
            if getattr(self, "engine", None) is not None:
                try:
                    self.engine.clear_cancel_request(session_id)
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.TimeoutError:
            if self.debug:
                self.logger.info(
                    f"[virtual_world] 上一条回复还没结束，这条不再排队 session={session_id}"
                )
        if event is not None and waited:
            # 这里记的是"等了多久才轮到我"，不是"持锁多久"——前者才是 @ 她半天没回的原因
            _timeline_mark(event, "reply_lock")
            try:
                marks = _timeline_marks(event)
                for item in marks:
                    if isinstance(item, dict) and item.get("name") == "reply_lock":
                        item["waited"] = round(time.time() - waited, 2)
                        item["got"] = acquired
                        break
            except Exception:  # noqa: BLE001
                pass
        try:
            yield
        finally:
            if identity and self._reply_owner.get(key) == identity:
                self._reply_owner.pop(key, None)
            if acquired:
                self._reply_session.pop(key, None)
                lock.release()

    # ---- 被打断之后的补偿重试（用户 m19948 第③条） ----

    def _schedule_interrupt_retry(self, session_id: str) -> None:
        """被打断、且一条都没发出去：过几秒替这一批再生成一次。

        通常不需要它：打断它的那条新消息自己那一轮会把这一批一起带上
        （水位线回退 → ``_adopt_interrupted_records``）。这里补的只是"让路之后
        没人再来"的空洞——比如那条新消息自己又被"并入上一条"挡掉、或者接管失败。
        每个会话只排一条、只试一次，拿不到锁（有人已经在回）就放弃。
        """

        key = str(session_id or "")
        if not key or self._reply_retry_running(key):
            return
        task = asyncio.create_task(self._interrupt_retry(key))
        self._reply_retries[key] = task

        def _done(finished: asyncio.Task) -> None:
            if self._reply_retries.get(key) is finished:
                self._reply_retries.pop(key, None)

        task.add_done_callback(_done)

    async def _interrupt_retry(self, session_id: str) -> None:
        try:
            await asyncio.sleep(REPLY_RETRY_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        engine = getattr(self, "engine", None)
        if engine is None:
            return
        try:
            if not engine.is_enabled(session_id):
                return
            if int(engine.pending_reply_count(session_id)) <= 0:
                return  # 已经被别的轮次带走了
            if not engine.interrupt_allowed(session_id):
                return  # 连着被打断太多次：这一轮让她安静下来，别再抢
            rows = engine.pending_reply_texts(session_id)
        except Exception as exc:  # noqa: BLE001 - 前置检查失败就当不重试
            self.logger.debug(f"[virtual_world] 补偿回复的前置检查失败：{exc}")
            return
        texts = [str(row.get("text") or "").strip() for row in rows]
        texts = [text for text in texts if text]
        if not texts or self._reply_lock(session_id).locked():
            return
        last = rows[-1] if rows else {}
        ctx = MessageContext(
            session_id=session_id,
            user_id=str(last.get("user_id") or ""),
            user_name=str(last.get("user_name") or ""),
            text="\n".join(texts),
            raw_text="\n".join(texts),
            is_wake=True,
            is_mentioned=True,
            is_private=":FriendMessage:" in session_id
            or ":PrivateMessage:" in session_id,
        )
        try:
            async with self._reply_turn(session_id, None):
                outcome = await asyncio.wait_for(
                    engine.handle_reply(ctx, history=[]),
                    timeout=REPLY_HARD_SECONDS,
                )
        except asyncio.TimeoutError:
            self.logger.info(
                f"[virtual_world] 补偿回复超时，这一批不再自动重试 session={session_id}"
            )
            return
        except Exception as exc:  # noqa: BLE001 - 补偿失败只记日志
            self.logger.debug(f"[virtual_world] 补偿回复失败：{exc}")
            return
        lines = [
            str(item).strip()
            for item in (getattr(outcome, "messages", []) or [])
            if str(item).strip()
        ]
        if not getattr(outcome, "ok", False) or not lines:
            # 又被新消息抢了 / 模型没给话：这一批还在队列里，下一轮会带上，不再重试
            return
        receipt = await self.messenger.send_text(session_id, lines)
        if not receipt:
            self.logger.debug(
                f"[virtual_world] 补偿回复没发出去：{getattr(receipt, 'error', '')}"
            )
            return
        try:
            await engine.mark_decision_replied(outcome, session_id)
        except Exception as exc:  # noqa: BLE001 - 水位线推不动也不能重发
            self.logger.debug(f"[virtual_world] 补偿回复推进水位线失败：{exc}")
        try:
            await engine.note_own_speech(
                session_id,
                lines,
                incoming=ctx.text,
                expecting=getattr(outcome, "expects_reply", None),
                target_user=str(getattr(outcome, "addressed_user", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 追问道没起算不连累这一句
            self.logger.debug(f"[virtual_world] 补偿回复的追问道没起算：{exc}")
        try:
            async with engine.session_state(session_id) as state:
                await engine._log_event(
                    state,
                    "reply_retry",
                    {"pending": len(texts), "sent": len(lines)},
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 补偿回复的日志没写上：{exc}")
        self.logger.info(
            f"[virtual_world] 被打断的那一批补回了 {len(lines)} 句 session={session_id}"
        )

    # ================= 回复路径（接管） =================

    def _reply_history(self, req: Any) -> list[dict[str, Any]]:
        """这一轮要不要把她自己留档之外、**宿主的会话历史**也交给她。

        默认不带（``context.use_host_history``）：宿主那份里有别的插件 / 别的机器人
        说过的话，也可能已经是几个小时前的——2026-10-20 实机，群里有人和另一个 bot
        聊过「不谈朋友的话，一个人这个开支确实不多」，她那轮只拿到这两条历史，
        于是接了句「我哪谈朋友啦 一个人账本清清爽爽的」，看着完全答非所问。
        """

        contexts = list(getattr(req, "contexts", None) or [])
        if not contexts:
            return []
        engine = getattr(self, "engine", None)
        world = getattr(engine, "world", None) if engine is not None else None
        context = getattr(world, "context", None)
        if context is not None and not bool(
            getattr(context, "use_host_history", False)
        ):
            return []
        return _drop_orphan_checkpoint_rounds(contexts)

    async def _host_other_context(self, event: AstrMessageEvent, req: Any) -> str:
        """宿主 system_prompt 里真正属于「别的插件」的那部分。

        AstrBot 会把自己那份人格（`# Persona Instructions`）追加进 ``req.system_prompt``，
        而插件自己已经按 `persona.mode` 把人设放进了她的提示词——所以这一份不带过去，
        免得同一份人设出现两次、还被挂在「其他插件提供的上下文」下面念给她听。

        用宿主同一个 `resolve_selected_persona` 拿这一轮**真正生效**的那份 persona
        做精确删除（拿不到就原样保留）；`[%None]` 那个正常路径下宿主压根不会追加。

        同一份 persona 的**示例对话**也要摘：宿主会把它们 deepcopy 到 ``req.contexts``
        开头（`astr_main_agent.py:576`），光删 system_prompt 里那段是删不掉的，
        她会在历史里看到另一套身份的示范对话。
        """

        text = getattr(req, "system_prompt", "") or ""
        manager = getattr(self.context, "persona_manager", None)
        resolver = getattr(manager, "resolve_selected_persona", None)
        if not callable(resolver):
            return self._host_context_clean(_strip_persona_instructions(text))
        platform_name = ""
        platform_getter = getattr(event, "get_platform_name", None)
        if callable(platform_getter):
            platform_name = str(platform_getter() or "")
        try:
            _persona_id, persona, _forced, use_webchat_default = await resolver(
                umo=event.unified_msg_origin,
                conversation_persona_id=getattr(
                    getattr(req, "conversation", None), "persona_id", None
                ),
                platform_name=platform_name,
            )
        except Exception:
            return self._host_context_clean(_strip_persona_instructions(text))
        if use_webchat_default:
            text = _strip_webchat_default_persona(text)
        if persona is None:
            persona_prompt = ""
        elif isinstance(persona, dict):
            persona_prompt = str(persona.get("prompt") or "")
        else:
            persona_prompt = str(getattr(persona, "prompt", "") or "")
        # 人设自带的示例对话也摘掉：宿主把它们插在 `req.contexts` 最前面当历史，
        # 她的提示词里就会同时出现「她的人设」和「另一套身份的示范对话」。
        dialogs = _persona_begin_dialogs(persona)
        if dialogs:
            contexts = getattr(req, "contexts", None)
            if isinstance(contexts, list):
                trimmed = _drop_begin_dialogs(contexts, dialogs)
                if len(trimmed) != len(contexts):
                    if self.debug:
                        self.logger.info(
                            f"[virtual_world] 摘掉宿主拼进来的人设示例对话 "
                            f"{len(contexts) - len(trimmed)} 条"
                        )
                    contexts[:] = trimmed
        return self._host_context_clean(
            _strip_persona_instructions(text, persona_prompt)
        )

    def _host_context_clean(self, text: str) -> str:
        """按 ``world.context.filter_host_context`` 摘掉宿主那几段跟她无关的话。"""

        body = str(text or "")
        if not body.strip():
            return ""
        engine = getattr(self, "engine", None)
        world = getattr(engine, "world", None)
        context = getattr(world, "context", None)
        if context is not None and not bool(
            getattr(context, "filter_host_context", True)
        ):
            return body
        available: Any = None
        getter = getattr(engine, "available_tools", None)
        if callable(getter):
            try:
                available = getter()
            except Exception:  # noqa: BLE001
                available = None
        cleaned = _filter_host_context(body, available)
        if self.debug and len(cleaned) != len(body):
            self.logger.info(
                f"[virtual_world] 摘掉宿主无关提示词 {len(body) - len(cleaned)} 字符"
            )
        return cleaned

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """阶段A3「标准回复链」：宿主（或别的插件）刚把一条回复发出去。

        宿主在 ``AstrBot/astrbot/core/pipeline/respond/stage.py:328`` 于 ``event.send(chain)``
        之后触发这个钩子。我们自己的发送也**补发**这个钩子（``_fire_hook``），
        所以先认 `VW_SELF_SENT_EXTRA`：那一条已经按发送回执记过捕获账，
        这里必须跳过——同一句话记两遍就是 X08 明令禁止的双重记账。
        """

        try:
            if event is not None and event.get_extra(VW_SELF_SENT_EXTRA):
                return
        except Exception:
            pass
        engine = getattr(self, "engine", None)
        captures = getattr(engine, "captures", None) if engine is not None else None
        if captures is None:
            return
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        texts = _result_chain_texts(event)
        if not texts and not session_id:
            return
        try:
            from .core.capture import CAPTURE_REPLY, capture_identity
        except Exception:  # pragma: no cover
            return
        operation_id = ""
        try:
            operation_id = str(event.get_extra("vw_send_operation_id") or "")
            if not operation_id:
                operation_id = secrets.token_hex(16)
                event.set_extra("vw_send_operation_id", operation_id)
        except Exception:
            operation_id = secrets.token_hex(16)
        captures.note(
            CAPTURE_REPLY,
            session_id=session_id,
            operation_id=operation_id,
            texts=texts,
            text="\n".join(texts),
            origin="host",
            detail={"hook": "after_message_sent", "receipt_status": SEND_UNKNOWN, "attempted": True},
            aliases=(),
        )

    @filter.on_decorating_result()
    async def on_host_result(self, event: AstrMessageEvent) -> None:
        """主人格（或别的插件）要把一条大模型回复发出去了——在这个会话里替掉它。

        用户 m29195：聊天里突然冒出一段"聊天助手"式的长回复（不是她的口吻），
        说明有某条路径绕过了接管、让 AstrBot 自己的大模型答了。AstrBot 的默认
        回复会先走到这个钩子（``astrbot/core/pipeline/result_decorate/stage.py``），
        我们自己的发送走 ``event.send()``、不经过结果链。

        **但"走结果链"不等于"别人说的"**：`_bridge_reply_hooks` 为了让别的插件能在
        发送前改写她的话，会把她的回复包成 ``MessageEventResult``、再补发一次这个钩子
        （`EventType.OnDecoratingResultEvent`）——那一份是她自己刚写的，不是主人格。
        所以判据必须带上"这条事件正处在桥接里"这个来源标记
        （``VW_BRIDGED_REPLY_EXTRA``），否则她每说一句都会被自己拦一次：WARN 刷屏、
        时间线误记「拦下主人格的回复」、后面的钩子插件也拿不到内容。标记由
        `_bridge_reply_hooks` 用 try/finally 清掉，不会留到下一个事件。

        正常情况一条都不该收到（接管路径一律 ``stop_event()``）。真收到了：
        这条不发，并留下日志点名是哪个会话、内容开头什么样——先把"她突然换人"
        堵住，再靠这条日志找到漏的那条路。
        """

        # 桥接补发的那一份是她自己刚说的话（见 docstring）：放行，别当成主人格。
        # 用 try/except 包住，和本文件其它读 extra 的地方一致（替身事件可能没有 get_extra）。
        try:
            if event.get_extra(VW_BRIDGED_REPLY_EXTRA):
                return
        except Exception:
            pass
        if not self.enabled or self.retired:
            return
        if self.in_self_initiated_call():
            return
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        if not session_id:
            return
        try:
            if not self.engine.is_enabled(session_id):
                return
        except Exception:
            return
        result = event.get_result()
        if result is None:
            return
        try:
            is_llm = bool(result.is_llm_result())
        except Exception:
            is_llm = False
        if not is_llm:
            return
        try:
            preview = str(result.get_plain_text() or "")[:40]
        except Exception:
            preview = ""
        self.logger.warning(
            f"[virtual_world] 拦下一条不是她说的模型回复（会话 {session_id}）："
            f"{preview}｜这个会话归她，主人格不该开口；如果反复出现，请把这条日志发出来"
        )
        try:
            event.set_result(None)
        except Exception:
            pass
        event.stop_event()
        await self._report_timeline(event, session_id, "拦下主人格的回复")

    def _clean_user_text(self, text: str) -> str:
        """剔掉宿主「只 @ 了一下」那段人机提示（见 ``strip_host_empty_mention_note``）。

        用户 m00001 的原话是「太人机」：她收到那段「你友好地询问用户想要聊些什么」
        之后，回出来就是「请问有什么可以帮您」。这里只留「被 @ 了一下」这个事实，
        剩下的她自己会从上下文里联系。
        """

        cleaned, stripped = strip_host_empty_mention_note(text)
        if stripped:
            self.logger.debug(
                f"[virtual_world] empty_mention_host_note_stripped：剔掉宿主 empty_mention "
                f"提示，这一轮她看到的用户文本是 {cleaned[:60]!r}"
            )
        return cleaned

    @filter.on_llm_request(priority=LLM_HOOK_PRIORITY)
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """消息走到大模型时触发。

        - 本插件接管这次回复，自己调大模型、按 JSON 动作执行并发送，然后 ``stop_event()``
          阻止主人格重复回复；
        - **接管失败（没配模型 / 调用出错 / 模型没给动作）就不回这一条**：绝不交回主人格——
          那是另一个人设，顶下来用户会看到「她」突然换了个说法；
        - 如果消息在这之前已经被别的插件处理掉（事件已 stop、或根本没走到大模型），我们什么都不做。
        """

        if not self.enabled:
            return
        if self.in_self_initiated_call():
            # 正常情况下这里只该拦下"我们自己发起的请求"。
            # 真有人 @ 她却被拦在这里，说明那道标记泄漏了——留一条日志，好排查。
            if event.get_sender_id() and event.get_sender_id() != event.get_self_id():
                self.logger.warning(
                    f"[virtual_world] 这条消息被「插件自己在调模型」的标记拦下了"
                    f"（depth={self._self_initiated_depth}）："
                    f"{str(event.get_message_str() or '')[:30]}"
                )
            return
        if event.is_stopped():
            return
        if self.retired:
            return
        if _is_command_event(event):
            # 指令不进她的上下文，也不让她回话（`/vw event h 在浴室` 这种只让她照做）
            return
        # 时间线：这一钩是"宿主管线终于走到模型请求"的一刻；
        # 它与「消息到达」之间隔着的就是宿主排队 / 会话锁 / 路由缓冲的时间
        self._timeline_note_hook(event)
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            # **别悄悄返回**：群里 @ 了她却没反应、日志一片空白，最难查的就是这种
            # （用户 m28075 报"群里直接 @ 无法触发，日志无内容"）。这里说清是哪个
            # 会话没启用、还是被内容安全挡了，一眼能对上「会话」页里那一行。
            self.logger.info(
                f"[virtual_world] 这条消息到了模型请求，但这个会话没启用，所以不回："
                f"{session_id}（{self.engine.session_block_reason(session_id)}）"
                f"｜{str(event.get_message_str() or '')[:30]}"
            )
            return
        # 临时会话已经收尾（``ended``）：这条消息由她**吞掉**——不回复、不注入上下文、
        # 不写 recent_chat、不碰档案，也不交给主人格（用户 m00001 需求 5）。
        # 放在 ``is_enabled`` 之后：接管的临时会话在白名单里是 enabled 的，不带这一条
        # 就会被她（或者主人格）当成普通私聊照常回。
        try:
            await self.engine.load_temp_sessions()
        except Exception as exc:
            # 这里还在"先占位"的 stop_event 之前：异常抛出去 AstrBot 只会记一笔，
            # 然后继续往下跑——主人格就顶上来回复了。临时会话状态读不出来时，宁可
            # 这一条不回（她可能正处在收尾的会话里），也绝不换人。
            event.stop_event()
            self.logger.warning(
                f"[virtual_world] 临时会话状态没读出来，这条不接（也不会交给主人格）："
                f"{session_id}｜{exc}"
            )
            return
        if self.engine.temp_ended(session_id):
            # 宿主的默认回复必须被抑制：stop 之后这条消息走不到主人格
            event.stop_event()
            self.logger.info(
                f"[virtual_world] 临时会话已收尾，这条不接：{session_id}"
                f"｜{str(event.get_message_str() or '')[:30]}"
            )
            return
        self._remember_event(event)
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        # 这一轮归她：**先占位，再干活**（用户 m29195：封堵所有会让主人格顶替的入口）。
        # 接管剩下的事情有取图、拼提示词、调模型、发消息，任何一步抛异常 AstrBot
        # 都会记一笔然后继续往下跑；那时主人格就顶上来了，用户会看到「她」突然换成
        # 一个聊天助手（人格还可能被我们摘掉了，于是答得又空又泛）。先 stop，后面
        # 无论成败都只是"这条不回"，绝不会换人。
        event.stop_event()

        # `req.prompt` 是管线处理过的用户消息（可能已带上图片描述等），优先用它
        user_text = (getattr(req, "prompt", "") or "").strip() or (
            event.get_message_str() or ""
        )
        # 宿主「只 @ 了一下」时把一段人机提示当用户输入塞进 prompt：先剔掉，
        # 免得她照着那段话发问（用户 m00001）。
        user_text = self._clean_user_text(user_text)
        # 注解**之前**的原文：ctx.text 是注解后的文本，事件身份认的是这条消息本身
        raw_text = event.get_message_str() or ""
        # 每个群各记一份群名片（用户 m14014）：跨群对得上同一个人
        card = _sender_card(event)
        sender_id = str(event.get_sender_id() or "")
        if card and sender_id:
            try:
                self.engine.profiles.set_card(session_id, sender_id, card)
            except Exception as exc:  # noqa: BLE001 - 记不到不影响这一条
                self.logger.debug(f"[virtual_world] 记群名片失败：{exc}")
        identity = _incoming_identity(event, raw_text)
        sources = self._inbox_sources(await _image_sources(event))
        media_ids = await self._media_ids(sources, session_id=session_id)
        # 先决定"这一轮把哪几张图交给主模型看"（编号也在这儿定），
        # 再拼注解——这样注解里的"就是图2、图3"和聊天记录里的编号是同一套
        image_urls, image_marks, image_notes, meme_images = await self._reply_images(
            event, sources, media_ids=media_ids
        )
        user_text = await self._annotate_message(
            event,
            user_text,
            sources=sources,
            summarize_forward=True,
            image_labels=image_marks,
        )
        if image_notes:
            user_text = f"{user_text}\n［{'；'.join(image_notes)}］".strip()
        ctx = MessageContext(
            session_id=session_id,
            user_id=event.get_sender_id(),
            user_name=event.get_sender_name(),
            text=user_text,
            raw_text=raw_text,
            message_id=str(identity.get("message_id") or ""),
            event_id=str(identity.get("event_id") or ""),
            is_wake=bool(event.is_wake_up()),
            is_mentioned=_is_at_bot(event) or bool(event.is_private_chat()),
            is_soft_wake=_is_soft_wake(event),
            is_private=bool(event.is_private_chat()),
            group_name=_group_name(event),
            persona_id=_event_persona_id(event),
            # 其他插件（上下文理解、图片转文字、记忆…）写进 system_prompt 的内容原样带过去
            # （AstrBot 自己的「人格设定」除外：她的人设由插件这边给，见 _host_other_context）
            other_context=await self._host_other_context(event, req),
            image_urls=image_urls,
            chat_images=sources,
            chat_media_ids=media_ids,
            image_marks=image_marks,
            meme_images=meme_images,
            at_targets=_at_targets(event),
            reply_to=_reply_target(event),
            addressing=_addressing_kind(
                _at_targets(event), _reply_target(event), str(event.get_self_id() or "")
            ),
            # 意图路由放行时交过来的事前判定（这批里哪几条是冲她说的）
            router=_router_view(event),
            # 引擎不认宿主的事件对象：把"往这条消息的 extra 里追加一个打点"包成
            # 回调带进去，`handle_reply` 才能把 llm_start / llm_done 打进同一条时间线。
            timeline_marker=lambda name: _timeline_mark(event, name),
        )

        # 睡觉时的门禁：没被明确叫醒就只回固定文案（或保持安静），
        # 不调大模型、不执行动作，也不交给主人格替她熬夜聊天。
        sleep_reply = await self.engine.sleep_gate(ctx)
        if sleep_reply is not None:
            echo = self.engine.take_pending_echo(session_id)
            outgoing = list(sleep_reply.messages) + echo
            # 「她这条已经答过了」= 文案真的发出去了（或压根没什么可发）。发送失败/抛异常时
            # 不能算——那条还留在待回队列里，下一轮还能回。
            answered = not outgoing
            sent_ok = False
            if outgoing:
                try:
                    receipt = await self._send_reply(event, outgoing)
                except Exception as exc:  # noqa: BLE001 - 发不出去就别吞，下次还能回
                    receipt = None
                    self.logger.warning(
                        f"[virtual_world] 睡觉文案发送失败，这条先不吞：{exc}"
                    )
                else:
                    await self._fire_hook(event, "OnAfterMessageSentEvent")
                sent_ok = bool(receipt)
                answered = sent_ok
            if answered:
                # 睡觉门替这条"答过话"了（或明确选择安静）：把它从待回队列里收掉。
                # 不收的话它会在十几秒后被下一次 _take_merge_texts 扫进正常回复——
                # 同一句话被回两遍（先 zzz，再认真答一轮）。
                incoming = event.get_extra(VW_INCOMING_EXTRA, None)
                try:
                    await self.engine.absorb_incoming(
                        incoming if isinstance(incoming, dict) else None,
                        reason=f"sleep_{sleep_reply.mode}",
                        session_id=session_id,
                        replied=sent_ok,
                    )
                except Exception as exc:  # noqa: BLE001 - 收不掉也不该影响"已经发出去"
                    self.logger.warning(f"[virtual_world] 睡觉门吞并失败：{exc}")
            if self.debug:
                self.logger.info(
                    f"[virtual_world] 睡觉门禁 {session_id}："
                    f"{sleep_reply.mode}（{sleep_reply.reason}）"
                )
            event.stop_event()
            await self._report_timeline(event, session_id, "睡觉门禁")
            return

        # 会话没启用 / 命中内容安全：本插件完全不干预，交给宿主
        if not await self.engine.handle_incoming(ctx):
            # 走到这儿只剩"内容安全挡下"这一种（会话没启用上面已经返回了）。
            # 事件已经 stop 过，所以这条谁都不回——挡下来的话不该被主人格答回去。
            self.logger.info(
                f"[virtual_world] 内容安全挡下了这条消息，这一轮谁都不回："
                f"{session_id}｜{str(ctx.text or '')[:30]}"
            )
            return

        # ---- 接管回复：自己调模型、自己发，然后阻止主人格重复回复 ----
        # AstrBot 会在「准备调模型」时发一次这个钩子（贴表情的小插件靠它开始处理）。
        # 我们自己调模型，这里补一遍；被拦下就整条消息都不接管。
        #
        # 2026-10-19 真机踩到：上面那句 `event.stop_event()` 是**我们自己**占的位，
        # 而宿主的 `call_event_hook()` 结尾直接回 `event.is_stopped()`——于是这个钩子
        # 只要补发就一定回报"被拦下"，每一句回话都在这里静默返回（她的消息一条也发不出去，
        # 日志里连"接管没生效"都没有）。发钩子之前先把自己那次占位撤掉、按真实结果判断，
        # 再补回占位；两步之间没有 await 之外的机会让主人格插进来。
        try:
            event.continue_event()
        except Exception:
            pass
        blocked = await self._fire_hook(event, "OnWaitingLLMRequestEvent")
        event.stop_event()
        if blocked:
            return
        # 同一条会话同一时间只跑一条接管：连发的第二条会排队，
        # 等前一条说完再开口，这样它能看到对方刚说过什么
        # 排队期间如果前一条还在调模型、而且这条来得够快，会被**并进那一次回复**，
        # 那就轮到自己时直接跳过（不能同一句话答两遍）
        # 「收到消息」那一刻就登记过了（见 on_any_message）：这里只取回来，
        # 拿不到（老事件 / 被别的插件重建过）就补登记一条
        incoming = event.get_extra(VW_INCOMING_EXTRA, None)
        if not isinstance(incoming, dict):
            incoming = self.engine.register_incoming(
                session_id,
                ctx.text,
                identity={
                    **_incoming_identity(event, ctx.raw_text or ctx.text),
                    "model_text": ctx.text,
                },
            )
        else:
            # 监听侧登记的那条要补成"LLM 请求看到的样子"（注解后的正文、
            # 图片说明等），但**事件身份不变**——所以这里只刷新正文和缺的字段
            self.engine.refresh_incoming(
                incoming,
                ctx.text,
                identity={
                    **_incoming_identity(event, ctx.raw_text or ctx.text),
                    "model_text": ctx.text,
                },
            )
        # 输入事实落库（阶段 A1）：同一条消息被两个钩子记过 / 被路由重投时身份一样，
        # 库里仍然只有一行；**不据此跳过处理**——路由重投的那一份正是要回的那一份
        # 本轮起点：写库失败的提示只认「这一轮」的失败（见 engine.notify_write_failure）。
        # 放在第一条落库之前，覆盖从收消息到投递的全部写操作。
        write_round_started = self.engine._now()
        await self.engine.remember_source_event(incoming)
        # 按当前区域裁一层宿主工具（`tool_filter_enabled` 关着就什么都不做）。
        # 放在调模型之前：宿主是在这个钩子之后才装配工具的，裁完她这一轮就看不到
        # 这个地点不该有的动作。
        await self._trim_tools(session_id, req)
        # 这一轮开始时她醒着吗：模型有时会磨很久（真机跑过 24 分钟），
        # 期间日程可能已经把她送去睡了。醒来再说的规矩只对"醒着开始"的这一轮生效。
        awake_at_start = not await self._she_is_asleep(session_id)
        # 同一条消息的重投：刚才已经回过（或正在回）了，整条跳过——不排队、不打断、
        # 不重发（用户 m19362）。
        if self._is_duplicate_delivery(session_id, event):
            if self.debug:
                self.logger.info(
                    f"[virtual_world] 同一条消息又投递了一次，跳过 session={session_id}"
                )
            event.stop_event()
            await self._report_timeline(event, session_id, "重复投递，跳过")
            return
        # 真的 @ 她（不是路由补的假 @）多给一点生成时间（用户 m19362）
        at_bot = _is_at_bot(event)
        budget = REPLY_HARD_SECONDS_AT if at_bot else REPLY_HARD_SECONDS
        async with self._reply_turn(session_id, event):
            if self.engine.was_absorbed(incoming):
                if self.debug:
                    self.logger.info(
                        f"[virtual_world] 这条被合并进上一次回复了，不单独回 session={session_id}"
                    )
                event.stop_event()
                await self._report_timeline(event, session_id, "并入上一条")
                return
            _timeline_mark(event, "reply_start")
            history = self._reply_history(req)
            # 阶段A3：她那一侧不是我们发的那几句，按 bot_external 留痕
            # （只记账，不改历史、不据此跳过这一轮）。
            _note_unknown_direct_sends(self.engine, history, session_id)
            try:
                outcome = await asyncio.wait_for(
                    self.engine.handle_reply(ctx, history=history),
                    timeout=budget,
                )
            except asyncio.TimeoutError:
                # 不能让她把世界状态锁一直攥着：这一轮放弃了，后面的 @ 才排得进来
                self.logger.warning(
                    f"[virtual_world] 这一轮超过 {budget:.0f} 秒还没生成完，放弃这一轮 session={session_id}"
                )
                event.stop_event()
                await self._report_timeline(
                    event,
                    session_id,
                    f"生成超过 {budget:.0f} 秒，这一轮放弃（锁已放开）",
                )
                return
            _timeline_mark(event, "reply_done")
            # 醒着开始、回出来时她已经睡了：这句不发。
            # 2026-10-19 真机：23:28 有人 @ 她，模型卡了 1454 秒，23:52 回出来时她已经在睡，
            # 群里照样收到一句「这花田配白墙黑瓦有点好看」——就是漏了这个复查。
            if awake_at_start and await self._she_is_asleep(session_id):
                for target in list(getattr(outcome, "replied_sessions", []) or []) or [session_id]:
                    try:
                        await self.engine.mark_reply_batch_processed(
                            target, getattr(outcome, "input_event_ids", ())
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.logger.debug(
                            f"[virtual_world] 登记这条静默处理失败：{exc}"
                        )
                self.logger.info(
                    f"[virtual_world] 生成期间她睡着了，这句话不发了 session={session_id}"
                )
                event.stop_event()
                await self._report_timeline(
                    event, session_id, "生成期间她睡着了，这句话不发了"
                )
                return
            # 刚才那句话是不是在回答她问出去的问题（P2-2 / 主人裁决）：
            # 谁答的、是不是本人都由引擎判；答上了就把等着的任务接着跑。
            # 放在 handle_reply 之后：先让她这一轮正常回话，别因为管道吞掉一句。
            try:
                await self.engine.consume_question_reply(
                    session_id,
                    str(event.get_sender_id() or ""),
                    str(ctx.text or ""),
                )
            except Exception as exc:  # 记账/恢复失败不该连累这一轮
                self.logger.debug(f"[virtual_world] 问答回执没记上：{exc}")
            echo = list(getattr(outcome, "debug_messages", []) or [])
            # 这一轮工具 / 指令生成出来的图（生图动作走的就是这里）
            made_images = list(getattr(outcome, "images", []) or [])
            # 她这一轮要说的话：当前会话一份，另外可能还有"只说给别处"的
            routed = {
                str(key): list(value)
                for key, value in (getattr(outcome, "routed", {}) or {}).items()
                if list(value)
            }
            routed_images = {
                str(key): list(value)
                for key, value in (getattr(outcome, "routed_images", {}) or {}).items()
                if list(value)
            }
            # 慢动作之前她已经说的那几句已经当场发出去了（见 _send_live_say）：
            # 这一轮算成立，但只发"还没发出去的部分"
            live_lines = [
                str(item)
                for item in (getattr(outcome, "live_messages", []) or [])
                if str(item).strip()
            ]
            pending_lines = [
                str(item) for item in (outcome.messages or []) if str(item).strip()
            ]
            if getattr(outcome, "silenced", False) and not (made_images or routed_images or routed or live_lines or pending_lines):
                # 这一轮**算她回过话**，但一条都不发。两种情况：
                #   - 扩展把她的独立发言静默了（正文由扩展自己写、自己发）；
                #   - 她自己写了 response: false（不想接这句话，原因记在 silent_reason）。
                # 水位线都必须照推，否则世界以为这批消息没被回过，下一轮会把同一批原样再喂一遍。
                targets = list(getattr(outcome, "replied_sessions", []) or [])
                for target in targets or [session_id]:
                    try:
                        await self.engine.mark_reply_batch_processed(target, getattr(outcome, "input_event_ids", ()))
                    except Exception as exc:
                        self.logger.debug(
                            f"[virtual_world] 登记静默处理失败：{exc}"
                        )
                by_model = bool(getattr(outcome, "silent_by_model", False))
                reason = str(getattr(outcome, "silent_reason", "") or "")
                if self.debug or by_model:
                    self.logger.info(
                        f"[virtual_world] {session_id}："
                        + (
                            f"她这一轮没接话（{reason or '模型没写原因'}），什么都不发"
                            if by_model
                            else "这一轮被扩展静默，什么都不发"
                        )
                    )
                event.stop_event()
                await self._report_timeline(
                    event, session_id, "她自己没接话" if by_model else "被扩展静默"
                )
                return
            if outcome.ok and (pending_lines or live_lines or routed or routed_images or made_images):
                # Ownership is established before hooks, network and receipt persistence.
                # AstrBot catches handler exceptions and consults this flag: leaving it
                # unset after an external send would reopen the default model runner.
                # This does not confirm delivery or advance any durable watermark.
                event.stop_event()
                # 先让别的插件的回复钩子过一遍（它们可能在回复里读写标记）
                # 钩子看的是她这一轮说过的**全部**话（含已经发出去的那几句），
                # 不然"等我两分钟"之后再接结果时，靠标记工作的插件会少读到前半句
                heard = live_lines + pending_lines
                bridged = await self._bridge_reply_hooks(
                    event,
                    heard,
                    str(getattr(outcome, "tail", "") or ""),
                    str(getattr(outcome, "leading_text", "") or ""),
                )
                if len(bridged) == len(heard):
                    bridged = bridged[len(live_lines) :]
                else:
                    # 钩子改了条数：位置对不上，就用原样的"还没发的部分"
                    bridged = list(pending_lines)
                # 再按换行拆段：一段一条消息，别把好几句挤成一坨；
                # 调试回显按它实际发生的位置插进去（先调工具、再说话，群里也是这个顺序）
                phase_mode = bool(getattr(outcome, 'requires_generation_bindings', False))
                payload_bindings = None
                if phase_mode:
                    try:
                        payload, payload_bindings = self._phase_payload(bridged, outcome, echo, session_id)
                    except ValueError as exc:
                        # 身份对不上时宁可少一层耐久身份，也不能让这一轮直接 500（用户 m12524）。
                        # 扩展改写正文、或钩子换了条数时，旧代码在这里抛 bindings lost、消息发不出去；
                        # 现在退回「可信身份」：按 decision 重新落一条 outbox 段，照常发。
                        self.logger.warning(
                            f"[virtual_world] {session_id}：这条回复的生成身份对不上（{exc}），改走可信身份发送"
                        )
                        payload = self._merge_with_echo(bridged, outcome, echo)
                        payload_bindings = None
                else:
                    payload = self._merge_with_echo(bridged, outcome, echo)
                sent_ok = True
                # Freeze every deferred artifact before the first receipt can terminalize the batch.
                if payload_bindings is not None:
                    for index, binding in enumerate(payload_bindings):
                        if binding is not None:
                            await self.engine.prepare_generated_speech(binding)
                        else:
                            await self.engine.db.call('prepare_outbox_segment',
                                f'{outcome.decision_id}:trusted:{index}:0', decision_id=outcome.decision_id,
                                target_session_id=session_id, text=payload[index], topic_mappings=[], now=self.engine._now())
                else:
                    payload = await self.engine.prepare_topic_reply(outcome, session_id, payload)
                for target, texts in list(routed.items()):
                    bindings = outcome.pending_speech_bindings.get(target)
                    if bindings is not None:
                        if len(bindings) != len(texts) or any(binding is None for binding in bindings):
                            raise ValueError('routed generation bindings lost')
                        for binding in bindings:
                            await self.engine.prepare_generated_speech(binding)
                        continue
                    prefix = f"{outcome.decision_id}:route:{hashlib.sha256(target.encode()).hexdigest()[:16]}"
                    routed[target] = await self.engine.prepare_topic_reply(outcome, target, texts, segment_prefix=prefix)
                if made_images:
                    await self.engine.send_topic_images(outcome, session_id, made_images, prepare_only=True)
                for target, refs in routed_images.items():
                    await self.engine.send_topic_images(outcome, target, refs, prepare_only=True)
                if getattr(outcome, "decision_id", ""):
                    await self.engine.db.call("seal_outbox", outcome.decision_id, now=self.engine._now())
                if payload or made_images:
                    receipt = await self._send_reply(
                        event,
                        payload,
                        # 不默认引用（用户 m09933）：只有她自己写了编号才引用
                        quote_id=str(getattr(outcome, "quote_message_id", "") or ""),
                        images=made_images,
                        operation_id=getattr(outcome, "decision_id", ""),
                        topic_outcome=outcome,
                        generation_bindings=payload_bindings,
                    )
                    sent_ok = bool(receipt) and not receipt.partial
                    if payload_bindings is None:
                        await self.engine.record_topic_reply_receipt(outcome, session_id, payload, receipt, finish=False)
                # 说给别处的那些：逐条投到那个会话（不带引用——那条消息不在这里）
                # 投失败的目标**不算回过话**：水位线不推进，它下一轮还会被当成新的
                failed_targets: set[str] = {session_id} if not sent_ok else set()
                # 失败原因（用户 m00001 需求 E）：补话要写真实原因，别再写含糊的
                # "那边现在发不出去"。SEND_FAILED 也要算失败——B 的提前判定就是
                # 直接回一个 failed 回执，不带 partial。
                failed_reasons: dict[str, str] = {}
                for target, texts in routed.items():
                    try:
                        routed_receipt = await self.engine.send_topic_routed_text(outcome, target, list(texts))
                        if (
                            not routed_receipt
                            or routed_receipt.partial
                            or routed_receipt.status == SEND_FAILED
                        ):
                            failed_targets.add(target)
                            failed_reasons[target] = str(getattr(routed_receipt, "error", "") or "")
                            sent_ok = False
                    except Exception as exc:
                        failed_targets.add(target)
                        failed_reasons[target] = str(exc)
                        sent_ok = False
                        self.logger.warning(
                            f"[virtual_world] 发到 {target} 失败：{exc}"
                        )
                for target, refs in routed_images.items():
                    try:
                        image_receipt = await self.engine.send_topic_images(outcome, target, list(refs))
                        if not image_receipt or image_receipt.partial:
                            failed_targets.add(target)
                            sent_ok = False
                    except Exception as exc:
                        failed_targets.add(target)
                        sent_ok = False
                        self.logger.warning(
                            f"[virtual_world] 发图到 {target} 失败：{exc}"
                        )
                # 这一轮写库失败过 → 在她的话**发完之后**补一条系统提示（单独一条，
                # 不进 messages / recent_chat：她没"说过"这句）。提示自己发不出去时
                # notify_write_failure 只记日志，不重试、不递归。
                try:
                    await self.engine.notify_write_failure(
                        session_id,
                        user_id=str(getattr(ctx, "user_id", "") or ""),
                        started_at=write_round_started,
                    )
                except Exception as exc:  # noqa: BLE001 - 提示失败不连累这一轮
                    self.logger.debug(f"[virtual_world] 写库失败提示没发出去：{exc}")
                # Durable receipts and the sealed complete outbox exclusively own
                # terminal status. A routed disclosure is not a reply to its audience's
                # unrelated pending decision.
                if session_id not in failed_targets:
                    # 临时会话（用户 m00001 需求 2 / 4 / 8）：这一轮回复**真的发出去了**
                    # → 记 1 个会话轮（她一句话分几段也只算 1 轮）。到期或到轮数上限时，
                    # 就在这里收尾：刚说完的那句是最后一句，摘要与状态当场落库。
                    try:
                        await self.engine.note_temp_round(
                            session_id,
                            user_id=str(getattr(ctx, "user_id", "") or ""),
                            # 刚发出去的这几句还没进聊天留档，收尾摘要得算上最后一句
                            # （审查 P2：不然她记住的永远是"说到一半"）
                            last_lines=[*live_lines, *pending_lines],
                        )
                    except Exception as exc:  # noqa: BLE001 - 记不上不连累这一轮
                        self.logger.debug(f"[virtual_world] 临时会话轮数没记上：{exc}")
                    try:
                        await self.engine.mark_decision_replied(outcome, session_id)
                    except Exception as exc:
                        self.logger.debug(f"[virtual_world] 推进水位线失败：{exc}")
                    # 追问（用户 m27236 / m28281）：等不等由她写在回话的 JSON 里
                    # （`expects_reply`），这里只照着起算。放在"确认发出去了"之后：
                    # 平台没收下的话不该起算，不然她会追问一句对方根本没看见的话。
                    expecting = getattr(outcome, "expects_reply", None)
                    try:
                        frow = await self.engine.note_own_speech(
                            session_id,
                            [*live_lines, *pending_lines],
                            incoming=str(getattr(ctx, "text", "") or ""),
                            expecting=expecting,
                            target_user=str(getattr(outcome, "addressed_user", "") or ""),
                        )
                    except Exception as exc:
                        frow = None
                        self.logger.debug(f"[virtual_world] 追问道没起算：{exc}")
                    # 只有真的起了追问道才写日志（用户 2026-10-26：日志页里她每说一句
                    # 都挂一条「追问（等回话）」，可绝大多数根本没起算）。没起算的只留
                    # 一条 debug：要看诊断时开 debug 就有，时间线不占地方。
                    try:
                        await self.engine.log_follow_up_decision(
                            session_id,
                            frow,
                            expecting=expecting,
                            target_user=str(getattr(outcome, "addressed_user", "") or ""),
                        )
                    except Exception as exc:
                        self.logger.debug(f"[virtual_world] 追问日志没记上：{exc}")
                if not sent_ok:
                    self.logger.warning(
                        f"[virtual_world] {session_id}：有目标没收到，"
                        "水位线只推了真正送到的那些"
                    )
                    # 她刚才多半已经说了「发过去了」——那边其实没收到（用户 2026-10-22：
                    # webchat 里让她私聊 QQ 好友，这句当场送不出去）。不替她补话：
                    # 记成她下一轮的一次性旁白，提不提由她自己决定（用户 m00001）。
                    missed = sorted(item for item in failed_targets if item and item != session_id)
                    if missed:
                        await self._announce_delivery_failure(
                            session_id, missed, failed_reasons
                        )
                # 「发完了」也要补一遍：靠它摘掉"处理中"标记的插件才不会一直挂着
                await self._fire_hook(event, "OnAfterMessageSentEvent")
                if self.debug:
                    self.logger.info(
                        f"[virtual_world] 接管回复 {session_id}："
                        f"reasoning={outcome.reasoning} "
                        f"messages={live_lines + pending_lines}"
                    )
                event.stop_event()
                await self._report_timeline(event, session_id, "已回复")
                return
            if str(getattr(outcome, "error", "")) == REPLY_INTERRUPTED:
                # 这一轮被新消息打断：作废，不补一句也不交给主人格——
                # 新那条自己的回复管线马上会接手（它能看到刚才这几条）。
                if echo:
                    await self._send_reply(event, echo)
                # 用户 m19362 的「打断之后一直不说话」：被打断的这一批已经还给
                # 下一次接管了（引擎的 _abort_records → 下一轮 _adopt_interrupted_records），
                # 但如果打断它的那条自己也被"并入上一条"挡掉，就没人触发下一轮。
                # 这里把"还在等回几条"记进事件日志，配合同一条消息的重投去重，
                # 至少不会再出现"自己打断自己、然后谁都没回"。
                pending = 0
                try:
                    pending = int(self.engine.pending_reply_count(session_id))
                except Exception:  # noqa: BLE001 - 只是诊断数字
                    pending = 0
                if pending and not (live_lines or pending_lines or made_images):
                    try:
                        reason = str(
                            (self.engine.cancel_requested(session_id) or {}).get("reason") or ""
                        )
                        async with self.engine.session_state(session_id) as state:
                            await self.engine._log_event(
                                state,
                                "reply_interrupted",
                                {"reason": reason, "unreplied": pending},
                            )
                    except Exception as exc:  # noqa: BLE001 - 记不上不连累这一轮
                        self.logger.debug(f"[virtual_world] 记打断事件失败：{exc}")
                    # 用户 m19948 第③条：让路之后如果没人再来接手，过几秒替这一批
                    # 再生成一次（只排一条、只试一次，拿不到锁就放弃）。
                    try:
                        self._schedule_interrupt_retry(session_id)
                    except Exception as exc:  # noqa: BLE001 - 排不上就按老样子
                        self.logger.debug(f"[virtual_world] 补偿重试没排上：{exc}")
                event.stop_event()
                await self._report_timeline(event, session_id, "被打断")
                return
            if str(getattr(outcome, "error", "")) == REPLY_MUTED:
                # 本小时被动回复触顶：接管但静默（连调试回显也不发）
                event.stop_event()
                await self._report_timeline(event, session_id, "触顶静默")
                return
            # 接管没成功：**这一条就不回了**（不交回主人格——那是另一个人设，
            # 顶下来的话用户会看到"她"突然换了个说法）。调试回显照样发，
            # 原因写进日志页，方便排查到底是模型挂了还是没给出动作。
            if echo or made_images:
                await self._send_reply(event, echo, images=made_images)
                await self._fire_hook(event, "OnAfterMessageSentEvent")
            await self._note_takeover_fallback(session_id, outcome)
            event.stop_event()
            await self._report_timeline(event, session_id, "接管没生效")
            return

    async def _trim_tools(self, session_id: str, req) -> None:
        """按当前区域裁剪 req.func_tool（`tool_filter_enabled` 关着就直接返回）。"""

        if not self.engine.world.tool_filter_enabled:
            return
        tool_set = getattr(req, "func_tool", None)
        if tool_set is None or not getattr(tool_set, "tools", None):
            return
        try:
            state = await self.engine.load_state(session_id, cold_start=False)
            node_id = state.node_id or self.engine.default_node_id()
        except Exception:
            node_id = self.engine.default_node_id()
        allowed = self.engine.allowed_tool_names(node_id)
        kept = [tool for tool in tool_set.tools if getattr(tool, "name", "") in allowed]
        if len(kept) == len(tool_set.tools):
            return
        tool_set.tools = kept

    async def _send_debug_live(self, session_id: str, message: str) -> bool:
        """把调试行立刻发到群里（引擎那边一发生就调它）。"""

        try:
            return bool(await self.messenger.send_text(session_id, [str(message)]))
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 调试回显发送失败：{exc}")
            return False

    async def _send_live_say(self, session_id: str, message: str) -> SendReceipt:
        """把她说出口的这一句立刻发出去（引擎在慢动作开始前调它）。

        检索、生图、录视频这类要花一会儿的动作排在后面时，「坐好等我两分钟～」先到群里，
        群里才是"她说了一句 → 过一会儿带着结果回来"，而不是半天不出声、然后一口气说三句。
        发失败返回 False：引擎会把这几句留到收尾按原顺序一起发，不会丢内容。
        """

        body = str(message or "").strip()
        if not body:
            return False
        # 连着的两句之间按字数停一下：和收尾发送时的节奏保持一致，
        # 免得"先说的这一句"排成一串同一瞬间冒出来
        previous = self._live_say_tail.get(session_id, "")
        if previous:
            delay = self.typing_delay_for(previous)
            if delay > 0:
                await asyncio.sleep(delay)
        try:
            sent = as_receipt(await self.messenger.send_text(session_id, [body]), target=session_id)
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 即时发言发送失败：{exc}")
            sent = SendReceipt(target=session_id, status=SEND_UNKNOWN if isinstance(exc, (TimeoutError, ConnectionError)) else SEND_FAILED)
        self._live_say_tail[session_id] = body if sent else ""
        return sent

    async def _send_reply(
        self,
        event: AstrMessageEvent,
        messages: list[str],
        *,
        quote_id: str = "",
        images: list[str] | None = None,
        operation_id: str = "",
        topic_outcome: Any = None,
        generation_bindings: list | None = None,
    ) -> SendReceipt:
        """把接管产生的消息发回当前会话（逐条发送 = 天然分段回复）。

        分段之间按上一条的字数停一下：群里看起来就像她在一条条打字，
        而不是一整坨同时冒出来。停顿有上限，长句子不会等到天荒地老。
        ``quote_id`` 是她自己在动作里写的编号解析出来的那条（用户 m07419 方案 B）。
        用户 m09933 定的口径：**不默认引用**——没有 ``quote_id`` 就一条都不带引用段，
        也不按内容猜、不引用"触发她的那条"。
        ``images`` 是这一轮工具 / 指令生成出来的图：说完再贴上去（一次一条消息，多张放一起）。
        """

        session_id = event.unified_msg_origin
        pending = [str(item).strip() for item in messages if str(item).strip()]
        refs = _sendable_images(images)
        operation_id = operation_id or secrets.token_hex(16)
        segment_receipts = [{"segment_id": f"{operation_id}:{i}", "index": i, "status": SEND_FAILED} for i in range(len(pending))]
        if not pending and not refs:
            return SendReceipt(target=session_id, status=SEND_FAILED, segments=0, operation_id=operation_id)
        try:
            event.set_extra("vw_send_operation_id", operation_id)
        except Exception:
            pass
        # 时间线：第一次真的往外发是什么时候（她这一轮可能先即时说过几句，
        # 那些在 handle_reply 里就发出去了，这一段量的是"收尾发送"）
        _timeline_mark(event, "send_start")
        for index, content in enumerate(pending):
            if self.messenger.blocked(session_id):
                # 刚发送失败过：不再往下试，免得把队列越堆越长
                break
            chain: list[Any] = [Plain(text=content)]
            if quote_id and index == 0:
                # 引用段必须排在前面，平台才认得出"这条是在回哪一句"
                chain.insert(0, Reply(id=quote_id))
            if generation_bindings is not None and generation_bindings[index] is not None:
                binding = generation_bindings[index]
                async def phase_send():
                    result = await event.send(MessageChain(chain))
                    return result if isinstance(result, SendReceipt) else SendReceipt(
                        target=session_id, status=SEND_FAILED if result is False else SEND_ACCEPTED,
                        platform_message_ids=_result_message_ids(result))
                try:
                    phase_receipt = await self.engine.send_generated_speech(binding, sender=phase_send)
                    segment_receipts[index].update(segment_id=f'{binding.segment_id}:0',
                        status=phase_receipt.status, platform_message_ids=phase_receipt.platform_message_ids)
                    if phase_receipt.status in {SEND_ACCEPTED, SEND_DELIVERED}:
                        self.messenger.mark_sent(session_id)
                        if index < len(pending) - 1:
                            delay = self.typing_delay_for(content)
                            if delay > 0:
                                await asyncio.sleep(delay)
                except Exception as exc:
                    segment_receipts[index].update(segment_id=f'{binding.segment_id}:0', status=SEND_UNKNOWN)
                    self.messenger.mark_failed(session_id, exc)
                continue
            legacy_segment_id = f'{operation_id}:trusted:{index}:0' if generation_bindings is not None else f'{operation_id}:{index}'
            send_attempted = False
            decision = None
            try:
                if generation_bindings is None and topic_outcome is not None and not await self.engine.prepare_topic_reply(topic_outcome, session_id, [content], validate_only=True):
                    break
                decision = await self.engine.db.call("get_decision", operation_id)
                if decision and not await self.engine.db.call("begin_segment_send", legacy_segment_id, now=time.time()):
                    segment_receipts[index]["status"] = SEND_UNKNOWN
                    continue
                send_attempted = True
                result = await event.send(MessageChain(chain))
                ids = _result_message_ids(result)
                send_status = result.status if isinstance(result, SendReceipt) else (SEND_FAILED if result is False else SEND_ACCEPTED)
                segment_receipts[index].update(status=send_status, platform_message_ids=ids)
                if decision:
                    await self.engine.db.call("record_segment_receipt", legacy_segment_id, status=send_status,
                        platform_message_ids=ids, now=time.time())
                self.messenger.mark_sent(session_id)
            except Exception as exc:
                # 失败就失败：以前这里会立刻换一条通道再发一次，遇到"超时但其实发出去了"
                # 的情况就会重复刷屏，所以只保留一次尝试。
                if segment_receipts[index]["status"] not in {SEND_ACCEPTED, SEND_DELIVERED}:
                    segment_receipts[index]["status"] = SEND_UNKNOWN if send_attempted or isinstance(exc, (TimeoutError, ConnectionError)) else SEND_FAILED
                self.messenger.mark_failed(session_id, exc)
                continue
            if index == len(pending) - 1:
                break  # 最后一条不用再等
            delay = self.typing_delay_for(content)
            if delay > 0:
                await asyncio.sleep(delay)

        image_receipt = None
        if refs and not self.messenger.blocked(session_id):
            async def send_media():
                # 图片也只在她说要引用时带引用段（用户 m09933：没有默认引用）
                return await self._send_images(event, refs, quote_id=quote_id if not pending else "")
            if topic_outcome is not None and topic_outcome.decision_id:
                image_receipt = await self.engine.send_topic_images(topic_outcome, session_id, refs, sender=send_media)
            else:
                image_receipt = as_receipt(await send_media(), target=session_id)
        _timeline_mark(event, "send_done")
        media_statuses = [image_receipt.status if image_receipt is not None else SEND_FAILED] if refs else []
        statuses = [item["status"] for item in segment_receipts] + media_statuses
        accepted = sum(status in {SEND_ACCEPTED, SEND_DELIVERED} for status in statuses)
        unknown = SEND_UNKNOWN in statuses
        return SendReceipt(target=session_id, status=SEND_ACCEPTED if accepted else (SEND_UNKNOWN if unknown else SEND_FAILED),
                           operation_id=operation_id, segments=len(statuses), sent_segments=accepted,
                           partial=0 < accepted < len(statuses), segment_receipts=segment_receipts,
                           platform_message_ids=[message_id for item in segment_receipts for message_id in item.get("platform_message_ids", [])])

    async def _send_images(
        self,
        event: AstrMessageEvent,
        images: list[str],
        *,
        quote_id: str = "",
    ) -> bool:
        """把结果图贴到群里（多张放同一条消息，不会拆成连发刷屏）。"""

        refs = _sendable_images(images)
        if not refs:
            return False
        session_id = event.unified_msg_origin
        if self.messenger.blocked(session_id):
            return False
        chain: list[Any] = [Image(file=ref) for ref in refs]
        if quote_id:
            chain.insert(0, Reply(id=quote_id))
        try:
            await event.send(MessageChain(chain))
            self.messenger.mark_sent(session_id)
            return True
        except Exception as exc:
            # 和文字一样：失败就失败，不换通道重发（重发容易变成两条）
            self.messenger.mark_failed(session_id, exc)
            return False

    def typing_delay_for(self, text: str) -> float:
        """按字数算这一次的「打字」停顿（秒）。0 表示不等待。"""

        style = getattr(self.engine.world, "reply_style", None)
        if style is None or not bool(getattr(style, "typing_delay_enabled", True)):
            return 0.0
        per_char = max(0.0, float(getattr(style, "typing_delay_per_char", 0.0) or 0.0))
        cap = max(0.0, float(getattr(style, "typing_delay_max", 0.0) or 0.0))
        if per_char <= 0 or cap <= 0:
            return 0.0
        return min(cap, len(str(text)) * per_char)

    @staticmethod
    def split_messages(messages: list[str], *, limit: int = 8) -> list[str]:
        """把每条消息按换行拆开：一段一条。

        模型经常把几句话、甚至「话 + 动作文案」写在同一个字符串里，
        整段发出去在群里就是一坨；拆开之后读起来才像人一句一句说的。
        """

        result: list[str] = []
        for item in messages:
            for line in str(item or "").splitlines():
                text = line.strip()
                if text:
                    result.append(text)
        return result[:limit] if result else [str(item).strip() for item in messages if str(item).strip()]

    def _phase_payload(self, bridged, outcome, echo, session_id):
        """Preserve positional generation identity through hook edits and splitting."""
        bindings = list(outcome.pending_speech_bindings.get(session_id, []))
        if len(bindings) != len(bridged):
            raise ValueError('generation bindings lost before deferred send')
        positions = list(getattr(outcome, 'debug_positions', []) or [])
        buckets = {}
        for index, text in enumerate(echo):
            slot = positions[index] if index < len(positions) else len(bridged)
            buckets.setdefault(min(int(slot), len(bridged)), []).append(text)
        payload, projected = [], []
        for slot in range(len(bridged) + 1):
            for text in buckets.get(slot, []):
                payload.append(text)
                projected.append(None)
            if slot < len(bridged):
                for part, text in enumerate(self.split_messages([bridged[slot]])):
                    binding = bindings[slot]
                    payload.append(text)
                    projected.append(replace(binding, text=text,
                        segment_id=f'{binding.segment_id}:part:{part}') if binding is not None else None)
        return payload, projected

    def _merge_with_echo(
        self, bridged: list[str], outcome, echo: list[str]
    ) -> list[str]:
        """把正式回复和调试回显按"实际发生的先后"合成一串消息。

        她这一轮可能是"先调工具、拿到结果才开口"：那种情况下调试行本来就该排在
        她的话前面。没有回显时就是普通的分段回复。
        """

        if not echo:
            return self.split_messages(bridged)
        raw = list(getattr(outcome, "messages", []) or [])
        if len(bridged) != len(raw):
            # 钩子改过条数，位置对不上了：宁可把回显放在后面，也不要错位
            return self.split_messages(bridged) + list(echo)
        positions = list(getattr(outcome, "debug_positions", []) or [])
        buckets: dict[int, list[str]] = {}
        for index, line in enumerate(echo):
            slot = positions[index] if index < len(positions) else len(raw)
            buckets.setdefault(min(int(slot), len(raw)), []).append(line)
        result: list[str] = []
        for slot in range(len(raw) + 1):
            result.extend(buckets.get(slot, []))
            if slot < len(raw):
                result.extend(self.split_messages([raw[slot]]))
        return result

    async def _bridge_reply_hooks(
        self,
        event: AstrMessageEvent,
        messages: list[str],
        tail: str = "",
        leading: str = "",
    ) -> list[str]:
        """让别的插件也能"看到"这次接管的回复。

        本插件接管时是自己调模型、自己发送的，AstrBot 的回复钩子轮不到跑，
        那些靠在回复里解析标记的插件（好感度、统计、改写语气之类）就永远收不到内容。
        这里把回复补送进同一条钩子链：先过 ``on_llm_response``，
        再过 ``on_decorating_result``（发送前的最后一道），拿回它们改过的文本再发。

        ``tail`` 是模型写在 JSON 之后的那一小截（例如 `[好感度 持平]`）：只跟这次钩子调用走，
        给靠标记工作的插件解析用，**不会**跟着她的话发到群里。
        ``leading`` 是模型写在 JSON 之前的那一段（不听话的先说正文）：同一条路透传，
        放在钩子文本最前面，发出去之前按行摘掉。
        """

        if not self.reply_hook_bridge or not messages:
            return messages
        try:
            from astrbot.core.pipeline.context_utils import call_event_hook
            from astrbot.core.provider.entities import LLMResponse
            from astrbot.core.star.star_handler import EventType
        except Exception:
            return messages

        joined = "\n".join(messages)
        # 标记只加在"给钩子看的文本"里：插件解析完（通常会把标记抹掉）再返回，
        # 真正发到群里的 messages 不会带上它。
        # 位置不能乱：``tail`` 一直是**跟在回复后面**的（靠后缀标记的插件就认那个位置），
        # ``leading`` 是模型多写在前面的正文，**放在最前面**。
        prefix = leading.strip()
        suffix = tail.strip()
        extras = [line for line in (prefix, suffix) if line]
        parts = [joined]
        if prefix:
            parts.insert(0, prefix)
        if suffix:
            parts.append(suffix)
        hook_text = "\n".join(parts).strip() if extras else joined
        try:
            response = self._hook_response(event, hook_text, LLMResponse)
            await call_event_hook(event, EventType.OnLLMResponseEvent, response)
            edited = str(getattr(response, "completion_text", "") or "")
            if edited and edited != hook_text:
                lines = [line.strip() for line in edited.splitlines() if line.strip()]
                # 插件可能把标记那一行抹掉了：只用它给的内容，标记本身不发
                cleaned = [line for line in (lines or [edited]) if line not in extras]
                messages = cleaned or lines or [edited]
        except Exception as exc:
            self.logger.warning(f"[virtual_world] 回复钩子（LLM 响应）执行出错：{exc}")

        marked = False
        try:
            from astrbot.core.message.message_event_result import (
                MessageEventResult,
                ResultContentType,
            )

            previous = event.get_result()
            result = MessageEventResult().message("\n".join(messages))
            result.set_result_content_type(ResultContentType.LLM_RESULT)
            # 这一步会重新跑一遍 `on_decorating_result`，本插件的守卫 `on_host_result`
            # 也在那条链里。先打标：这一份是她**自己**刚写的话，守卫不能拦——否则她
            # 每说一句都被自己拦一次（WARN、时间线误记、后面的钩子插件拿不到内容）。
            try:
                event.set_extra(VW_BRIDGED_REPLY_EXTRA, True)
                marked = True
            except Exception:
                pass
            event.set_result(result)
            await call_event_hook(event, EventType.OnDecoratingResultEvent)
            current = event.get_result()
            joined_result = "\n".join(messages)
            texts = (
                [
                    str(component.text)
                    for component in list(getattr(current, "chain", []) or [])
                    if isinstance(component, Plain) and str(component.text).strip()
                ]
                if current is not None
                else []
            )
            # 没有插件改动内容时，保持原来的分段：合并成一条会把
            # 「say 的话」和「（…抱了你一下）」这类动作文案挤进同一条消息里。
            if texts and (len(texts) > 1 or texts[0] != joined_result):
                messages = texts
            if previous is None:
                event.clear_result()
            else:
                event.set_result(previous)
        except Exception as exc:
            self.logger.warning(f"[virtual_world] 回复钩子（发送前）执行出错：{exc}")
        finally:
            # 标记只属于这一次桥接。异常、别的钩子清空/改回 result、正常恢复原结果，
            # 每一条退路都要清；AstrMessageEvent 没有 del_extra，置 False 即可（守卫只认真值），
            # 绝不能留到下一个事件——那会让下一个主人格回复被放行。
            if marked:
                try:
                    event.set_extra(VW_BRIDGED_REPLY_EXTRA, False)
                except Exception:
                    pass
        return messages

    def _hook_response(self, event: AstrMessageEvent, text: str, response_cls: Any) -> Any:
        """给回复钩子准备一个「像真的」的模型响应。

        别的插件可能读 ``usage`` / ``id`` / ``role`` 这些字段，自己拼一个空的响应它们
        会直接抛异常（异常被宿主吞掉，表现就是"收不到钩子"）。所以优先拿主模型
        真实响应的浅拷贝，只把文本换成最终要说的话。
        """

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        raw = self.take_llm_response(session_id) if session_id else None
        if raw is not None:
            try:
                response = copy.copy(raw)
                response.completion_text = text
                if hasattr(response, "reasoning_content"):
                    response.reasoning_content = ""
                return response
            except Exception:
                pass
        return response_cls(role="assistant", completion_text=text)

    # ---------------- 把接管的流程补进 AstrBot 的钩子链 ----------------

    @staticmethod
    def _hook_type(name: str) -> Any:
        """按名字取 AstrBot 的钩子类型（老版本没有的钩子就跳过）。"""

        try:
            from astrbot.core.star.star_handler import EventType
        except Exception:
            return None
        return getattr(EventType, name, None)

    async def _fire_hook(self, event: Any, name: str, *args: Any) -> bool:
        """补发一个 AstrBot 钩子；返回事件是否被别的插件拦下。

        本插件接管时是自己调模型、自己调工具、自己发消息的，AstrBot 管线里那些
        「等 LLM」「用完工具」「发完消息」的钩子轮不到跑——靠这些钩子工作的小插件
        （比如处理中贴表情、用完摘掉）就会漏掉一半。这里在同样的时机补一遍。
        """

        if not self.reply_hook_bridge or event is None:
            return False
        if name == "OnAfterMessageSentEvent":
            # 阶段A3：这是我们**自己**发完消息补发的钩子。同一条话已经按发送回执
            # 记进捕获账了，给事件打上标记，让 on_after_message_sent 跳过它，
            # 免得同一句话被记两遍（X08 无双重记账）。
            try:
                event.set_extra(VW_SELF_SENT_EXTRA, True)
            except Exception:
                pass
        hook_type = self._hook_type(name)
        if hook_type is None:
            return False
        try:
            from astrbot.core.pipeline.context_utils import call_event_hook
        except Exception:
            return False
        try:
            return bool(await call_event_hook(event, hook_type, *args))
        except Exception as exc:
            self.logger.warning(f"[virtual_world] 钩子 {name} 执行出错：{exc}")
            return False

    async def _note_takeover_fallback(self, session_id: str, outcome) -> None:
        """接管没生效：这一条她不说话，但**必须留下痕迹**。

        以前只在开着调试时打一行 stdout，日志页里什么都没有——用户只看到
        "她没回"，完全猜不到是模型挂了、还是模型没给出能执行的动作。
        """

        reason = str(getattr(outcome, "error", "") or "没有对外输出")
        warnings = [str(item) for item in (getattr(outcome, "warnings", None) or [])]
        self.logger.warning(
            f"[virtual_world] 接管没生效（{reason}），这一条保持安静 "
            f"session={session_id}"
        )
        detail = {"reason": reason}
        if warnings:
            detail["warnings"] = warnings[:4]
        if getattr(outcome, "tail", ""):
            detail["model_tail"] = str(outcome.tail)[:200]
        # 用户 m00001：「模型没有给出任何动作 → 她保持安静」在事件库里的唯一痕迹就是
        # 这一条 takeover_failed，以前只有一句 reason，看不出模型到底给了什么。
        # 原始输出只在失败路径由 ReplyOutcome 带上来（成功的那一轮不记，避免翻倍）。
        raw_text = str(getattr(outcome, "raw_text", "") or "")
        if raw_text:
            detail.update(raw_output_fields(raw_text))
            self.logger.warning(
                "[virtual_world] 模型这一轮的输出没法用（{}）：{}".format(
                    session_id, clip_line(scrub_secrets(raw_text), 500)
                )
            )
        if getattr(outcome, "finish_reason", ""):
            detail["finish_reason"] = str(outcome.finish_reason)[:40]
        elif raw_text:
            # 拿不到 finish_reason 就写 null（不编一个 "stop"）
            detail["finish_reason"] = None
        # 用户 m00001：这一轮的 JSON 是靠修复才读出来的（裸换行 / 被劈开的键名）的话，
        # 把修复方式和换掉的换行数带上——折行是间歇性的，两条事件能对着看。
        if getattr(outcome, "parse_repaired", ""):
            detail["parse_repaired"] = str(outcome.parse_repaired)
            detail["in_string_newlines"] = int(
                getattr(outcome, "in_string_newlines", 0) or 0
            )
        try:
            async with self.engine.session_state(session_id) as state:
                await self.engine._log_event(state, "takeover_failed", detail)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 写接管失败日志失败：{exc}")

    async def _announce_delivery_failure(
        self,
        session_id: str,
        missed: list[str],
        reasons: dict[str, str] | None = None,
    ) -> str:
        """有目标没收到：**不自动补话**，只记成她下一轮的一次性旁白（用户 m00001）。

        她刚才多半已经说了「发过去了」——那边其实没收到（用户 2026-10-22：webchat 里
        让她私聊 QQ 好友，这句当场送不出去）。以前这里会立刻回一句「刚才想跟…单独说
        两句，话没发出去（…）」；用户截图说那句太像系统回执，要**她自己说**。所以现在
        写进 ``WorldState.delivery_miss_note``（见 ``engine.note_delivery_miss``），下一轮
        回复当旁白注入一次，注入即清空；时间线照旧记一条 ``delivery_failed``（要诚实，
        别让日志里只剩"她以为发过去了"）。

        返回那句旁白原文（给日志 / 测试看），实际没有发出任何消息。
        """

        reasons = reasons or {}
        reason = next((reasons[item] for item in missed if reasons.get(item)), "")
        note = self.engine.delivery_miss_hint(missed, reason=reason)
        try:
            stored = await self.engine.note_delivery_miss(session_id, note)
        except Exception as exc:  # noqa: BLE001 - 记不上不该连累这一轮
            self.logger.debug(f"[virtual_world] 投递失败的旁白没记上：{exc}")
            stored = False
        if not stored:
            return ""
        try:
            async with self.engine.session_state(session_id) as state:
                await self.engine._log_event(
                    state,
                    "delivery_failed",
                    {
                        "target": missed[0] if len(missed) == 1 else missed,
                        "reason": reason,
                        "raw": {item: reasons.get(item, "") for item in missed},
                    },
                    target=session_id,
                )
        except Exception as exc:  # noqa: BLE001 - 记不上不连累这一轮
            self.logger.debug(f"[virtual_world] 投递失败事件没记上：{exc}")
        return note

    # ================= 单条消息的时间线 =================
    # 「@ 了她好几分钟才回」这种问题，光看她说了什么查不出来：得把这一条消息
    # 从到达、到宿主把请求交到模型钩子、到排队、到真的调模型、到发出去，摊开看。

    def _timeline_note_arrival(self, event: Any, session_id: str) -> None:
        """消息到达：记下这一刻（会话上也留一份，见 `_timeline_note_hook`）。"""

        _timeline_mark(event, "arrive")
        self._timeline_arrivals[str(session_id)] = {
            "at": time.time(),
            "text": str(event.get_message_str() or "")[:40],
        }

    def _timeline_note_hook(self, event: Any) -> None:
        """宿主的管线终于走到「要调模型」这一钩了。

        与"到达"那一刻的差，就是宿主在队列里等的时间：会话锁、意图路由缓冲、
        上一条回复还没跑完……这些全落在这段里。
        """

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        arrival = self._timeline_arrivals.get(session_id)
        if arrival is not None:
            age = time.time() - float(arrival.get("at") or 0.0)
            if age > TIMELINE_ARRIVAL_TTL_SECONDS:
                arrival = None
        if arrival is not None:
            try:
                marks = _timeline_marks(event)
            except Exception:  # noqa: BLE001
                marks = []
            if not any(
                isinstance(item, dict) and item.get("name") == "arrive"
                for item in marks
            ):
                # 意图路由会把消息吞下去再重投（可能换了个事件对象）：
                # 把"到达"补回来，这段等待才算得出来
                marks.insert(
                    0, {"name": "arrive", "at": float(arrival["at"]), "reused": True}
                )
        _timeline_mark(event, "hook")

    async def _report_timeline(
        self, event: Any, session_id: str, reason: str = ""
    ) -> None:
        """把这一条消息的时间线收口：只在"慢"的时候落库。

        正常几秒一条的消息不必把事件日志刷满；开着调试时另记一行 stdout。
        """

        self._timeline_arrivals.pop(str(session_id), None)
        try:
            marks = list(_timeline_marks(event))
            detail = _timeline_detail(marks)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 时间线打点读不出来：{exc}")
            return
        if not detail:
            return
        if reason:
            detail["reason"] = str(reason)
        # 这条时间线是组里哪条会话触发的：``session_id`` 是真实 umo，不是
        # ``state_key()`` 映射后的状态键（用户 m30538：同组共用一个状态键，
        # 只看 ``session_id`` 分不出是谁）。
        detail["session"] = str(session_id)
        if self.debug:
            self.logger.info(
                f"[virtual_world] 时间线 session={session_id} {_timeline_text(marks)}"
                + (f"（{reason}）" if reason else "")
            )
        spans = detail.get("spans") or {}
        total = float(detail.get("total") or 0.0)
        host_wait = float(spans.get("hook") or 0.0)
        queue_wait = float(spans.get("reply_lock") or 0.0)
        if (
            total < TIMELINE_SLOW_SECONDS
            and host_wait < TIMELINE_HOST_WAIT_SECONDS
            and queue_wait < TIMELINE_QUEUE_WAIT_SECONDS
        ):
            return
        if host_wait >= TIMELINE_HOST_WAIT_SECONDS:
            self.logger.warning(
                f"[virtual_world] 这条消息到达后等了 {host_wait:.1f} 秒才走到模型钩子"
                f"（宿主排队／会话锁／路由缓冲）session={session_id}"
            )
        elif queue_wait >= TIMELINE_QUEUE_WAIT_SECONDS:
            self.logger.warning(
                f"[virtual_world] 这条消息等了 {queue_wait:.1f} 秒才轮到自己回"
                f"（上一条还没说完）session={session_id}"
            )
        else:
            # 用户 m30538：总时长和分段挤在同一行，"共 36.1s｜安静期 0.2s｜…"被读成
            # "安静期 36.1s"。总时长独占一行，分段另起一行，并标出最长的那段。
            self.logger.warning(
                f"[virtual_world] 这条消息从到达发完共 {total:.1f} 秒 session={session_id}\n"
                f"[virtual_world] 分段：{_timeline_spans_text(marks) or '（没有分段数据）'}"
            )
        try:
            async with self.engine.session_state(session_id) as state:
                await self.engine._log_event(
                    state, "timeline", detail, origin_session=str(session_id)
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 写时间线日志失败：{exc}")

    # ================= 旁观记录（不产生 LLM 调用） =================

    @filter.event_message_type(
        filter.EventMessageType.ALL, priority=SLEEP_GUARD_PRIORITY
    )
    async def on_sleep_guard(self, event: AstrMessageEvent) -> None:
        """睡觉门禁：挡在其它插件（含意图路由）之前。

        优先级的取值比意图路由的 100 更高，所以 ``stop_event()`` 之后它不会被执行——
        既不浪费它那次判断，也不会把睡着的她拖进对话。
        """

        if not self.enabled:
            return
        if self.in_self_initiated_call():
            return
        if self.retired:
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        text = event.get_message_str() or ""
        if _is_command(text):
            return  # 指令照常走（/vw status、/vw 叫醒 这些要能用）
        sources = self._inbox_sources(await _image_sources(event))
        if _is_forwarded(event) and self._should_expect_reply(event):
            # 这条转发是冲她来的：先把内容压成摘要，她醒来看到的才是"事情"而不是占位符
            summary = await self._forward_summary_text(event)
            if summary:
                text = _with_forward_summary(text, summary)
        if _image_components(event):
            # 她在睡觉：这里不额外调转述模型（醒来后那条消息会照常转述），
            # 但留档里要能看出"有人发了张图"，否则这条消息会是空的。
            text = f"{text}\n［对方发了一张图片］".strip()
        ctx = MessageContext(
            session_id=session_id,
            user_id=event.get_sender_id(),
            user_name=event.get_sender_name(),
            text=text,
            is_wake=bool(event.is_wake_up()),
            is_mentioned=_is_at_bot(event) or bool(event.is_private_chat()),
            is_private=bool(event.is_private_chat()),
            # 记下这条消息带的图片，等下次回复时一起给多模态主模型
            image_urls=list(sources),
            chat_images=list(sources),
            router=_router_view(event),
        )
        if not await self.engine.should_block_sleep(ctx):
            return
        # 留档已经由门禁自己记好了（她醒来还得知道群里发生过什么），这里只负责截住事件
        if self.debug:
            self.logger.info(
                f"[virtual_world] 她在睡觉，挡下这条消息 session={session_id}：{text[:30]}"
            )
        event.stop_event()
        # 挡下的这条也算"有归宿了"：登记钩子（PRESENCE_PRIORITY=190）在门禁
        # （SLEEP_GUARD_PRIORITY=200）之前就跑过了，`block_scope=all` 时它已经进了待回
        # 队列，而这条消息再也不会走到 LLM 钩子去收——留着它几个小时后就可能被扫进
        # 下一次正常回复（她会突然答一句早就过去的群消息）。这里收掉，但**不算答复**
        # （确实没回）：她醒来后同一条再送进来，仍然照常回。
        try:
            await self.engine.absorb_incoming(
                event.get_extra(VW_INCOMING_EXTRA, None),
                reason="sleep_blocked",
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001 - 收不掉也不能影响"挡住"这件事
            self.logger.warning(f"[virtual_world] 睡觉门禁吞并失败：{exc}")

    async def _pin_astrbot_no_persona(self, session_id: str) -> None:
        """让宿主别再往这个会话的请求里塞人格：她的人设只由插件这一份给。

        AstrBot 解析出来的人格会被追加进 ``req.system_prompt``（`# Persona Instructions`），
        插件再把它当「其他插件提供的上下文」带给她——同一份人设两处出现，
        而且宿主的默认人格是英文的「You are a helpful and friendly assistant.」，
        混进角色提示词里就是"窜味"。这里用 AstrBot 自己的哨兵 ``[%None]``
        （`persona_mgr.resolve_selected_persona`：这个会话不设人格，连 webchat 那条
        特殊默认人格也一并跳过），写在会话服务配置里，WebUI 上可见也可改。

        写入失败不影响回话：'on_llm_request' 那边还有一道精确摘除兜底。
        """

        preferences = _shared_preferences()
        if preferences is None:
            return
        key = "session_service_config"
        try:
            config = (
                await preferences.get_async(
                    scope="umo",
                    scope_id=str(session_id),
                    key=key,
                    default={},
                )
                or {}
            )
        except Exception:
            return
        if str(config.get("persona_id") or "") == ASTRBOT_NO_PERSONA:
            return
        updated = dict(config)
        updated["persona_id"] = ASTRBOT_NO_PERSONA
        try:
            await preferences.put_async(
                scope="umo",
                scope_id=str(session_id),
                key=key,
                value=updated,
            )
        except Exception:
            if self.debug:
                self.logger.warning(
                    f"[virtual_world] 没能把会话设成「不设人格」session={session_id}",
                    exc_info=True,
                )
            return
        if self.debug:
            self.logger.info(
                f"[virtual_world] 会话已设为不设 AstrBot 人格（她的人设由插件给）session={session_id}"
            )

    @filter.event_message_type(
        filter.EventMessageType.ALL, priority=PRESENCE_PRIORITY
    )
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        if not self.enabled or self.retired:
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            # 消息到了、但这个会话没启用：说清是哪一种，别让人对着空白日志猜
            # （用户 m28075：群里 @ 了没反应、日志里一个字都没有）
            self.logger.info(
                f"[virtual_world] 收到消息但这个会话没启用，先不管它：{session_id}"
                f"（{self.engine.session_block_reason(session_id)}）"
                f"｜{str(event.get_message_str() or '')[:30]}"
            )
            return
        # 临时会话已经收尾（``ended``）：这条消息由她吞掉，什么都不记——不登记待回、
        # 不留档、不写 recent_chat、不碰档案、也不起追问（用户 m00001 需求 5）。
        await self.engine.load_temp_sessions()
        if self.engine.temp_ended(session_id):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        if _is_command_event(event):
            # 指令不进她的上下文：不登记待回、不留档、不进记忆，也不该由她回话
            return
        # 人设来源在这里就收回来：再晚（落到 on_llm_request 那一钩）宿主人格已经拼进请求了
        await self._pin_astrbot_no_persona(session_id)
        # 时间线：这一刻才算"这条消息到了"。她被 @ 之后要等多久才开口，
        # 起点在这里——中间那段是宿主 / 路由 / 前一条回复占着管线
        self._timeline_note_arrival(event, session_id)
        # 用户 m09264：这里**不**记"旧台词作废"——来了条消息不等于她要回。作废的
        # 触发点是"又发起了一次新的模型请求"，那个标记写在 engine.handle_reply
        # 真正发请求之前（以及自主 / 日程 / 事件各自那一轮的开头）。
        # 「连发合并」要在这里就登记：落到 on_llm_request 那一钩时，同一会话的
        # 上一条回复可能还占着管线（AstrBot 里那条管线是串行的），等她登记时
        # 早就过了安静期，两条消息就各回一次了。
        if self._should_expect_reply(event):
            try:
                raw_text = event.get_message_str() or ""
                incoming = self.engine.register_incoming(
                    session_id,
                    raw_text,
                    identity=_incoming_identity(event, raw_text),
                )
                event.set_extra(VW_INCOMING_EXTRA, incoming)
                # 输入事实落库（阶段 A1）：身份相同就是同一个事件，重复投递只留一行；
                # 登记失败也照常往下走（它只是留痕，不影响要不要回这一条）
                await self.engine.remember_source_event(incoming)
            except Exception as exc:
                self.logger.debug(f"[virtual_world] 登记待回消息失败：{exc}")
        self._remember_event(event)
        # 有人说话了：她还在等回话的那条追问道就到此为止（用户 m27236）。放在这里
        # 而不是"她回完之后"——对方已经接了，追不追都不该再催。
        # 群里要分清是谁说的（用户 m28281）：她在等 A 回话时 B 插两句不算回她。
        try:
            await self.engine.cancel_follow_up(
                session_id,
                "对方说话了",
                speaker=str(event.get_sender_id() or ""),
                is_group=bool(event.get_group_id()),
            )
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 收口追问道失败：{exc}")
        sources = self._inbox_sources(await _image_sources(event))
        raw_text = event.get_message_str() or ""
        text = await self._annotate_message(
            event,
            raw_text,
            sources=sources,
            summarize_forward=self._should_expect_reply(event),
        )
        media_ids = await self._media_ids(sources, session_id=session_id)
        await self._refresh_group_roles(event, session_id)
        # 她自己的 QQ：状态页头像要用。群里从群资料拿，私聊直接问事件。
        self_qq = str(event.get_self_id() or "")
        if self_qq:
            try:
                async with self.engine.session_state(session_id) as state:
                    if state.bot_qq != self_qq:
                        state.bot_qq = self_qq
            except Exception as exc:  # noqa: BLE001 - 记不到不影响这一条
                self.logger.debug(f"[virtual_world] 记自己的 QQ 失败：{exc}")
        identity = _incoming_identity(event, raw_text)
        await self.engine.note_presence(
            MessageContext(
                session_id=session_id,
                user_id=event.get_sender_id(),
                user_name=event.get_sender_name(),
                text=text,
                raw_text=raw_text,
                message_id=str(identity.get("message_id") or ""),
                event_id=str(identity.get("event_id") or ""),
                is_wake=bool(event.is_wake_up()),
                is_private=bool(event.is_private_chat()),
                group_name=_group_name(event),
                chat_images=list(sources),
                chat_media_ids=list(media_ids),
                at_targets=_at_targets(event),
                reply_to=_reply_target(event),
                addressing=_addressing_kind(
                    _at_targets(event),
                    _reply_target(event),
                    str(event.get_self_id() or ""),
                ),
                # 意图路由放行时交过来的事前判定：记档时就按事件身份认领给对应那几条
                router=_router_view(event),
            )
        )
        # 看图接话（S6）：她自己看一眼图，觉得有意思就下一拍接一句——不依赖意图路由
        # （用户 m01267：用户可能根本没装路由插件）。这里只**登记意愿**，
        # 说不说由引擎的拍子按冷却 / 每小时上限 / 安静期决定。
        self._offer_image_reaction(event, session_id, sources)
        # 后台转述（用户 m08448）：收图就开始看，别等回复那一刻再等几十秒
        self._queue_image_captions(event, sources)
        # 攒图评审的入池（用户 2026-10-24 补充）：这条钩子对**每一条**消息都跑，
        # 她这一轮回不回复都会走到；挂在 _reply_images 上会漏掉"没回复就错过"的图。
        self._note_meme_collect_seen(
            session_id, sources=sources, media_ids=media_ids
        )

    GROUP_ROLES_TTL = 1800.0

    async def _refresh_group_roles(self, event: AstrMessageEvent, session_id: str) -> None:
        """取群主 / 管理员（用户 m12215）：群里才有，缓存半小时。

        宿主 `event.get_group()` 会顺手把 owner/admin 的 id 和成员列表带回来；
        取不到就沿用上一次的（她照旧知道谁是群主，只是可能过时）。
        """

        if event.is_private_chat():
            return
        stamp, cached = self._group_role_cache.get(session_id, (0.0, {}))
        if cached and (time.time() - stamp) < self.GROUP_ROLES_TTL:
            self.engine.set_group_roles(session_id, cached)
            return
        getter = getattr(event, "get_group", None)
        if not callable(getter):
            return
        try:
            group = await asyncio.wait_for(getter(), timeout=8.0)
        except Exception as exc:  # noqa: BLE001 - 取不到不影响这一条消息
            self.logger.debug(f"[virtual_world] 取群资料失败（沿用旧的）：{exc}")
            if cached:
                self.engine.set_group_roles(session_id, cached)
            return
        if group is None:
            return
        self_id = str(event.get_self_id() or "")
        names: dict[str, str] = {}
        for member in list(getattr(group, "members", None) or []):
            uid = str(getattr(member, "user_id", "") or "")
            if uid:
                names[uid] = str(getattr(member, "nickname", "") or "")
        self_name = names.get(self_id) or str(
            getattr(getattr(event, "message_obj", None), "self_name", "") or ""
        )
        owner_id = str(getattr(group, "group_owner", "") or "")
        admin_ids = [str(uid) for uid in (getattr(group, "group_admins", None) or []) if uid]
        role_of = lambda uid: (
            "owner" if uid and uid == owner_id else ("admin" if uid in admin_ids else "member")
        )
        roles = {
            "group_id": str(getattr(group, "group_id", "") or event.get_group_id() or ""),
            "group_name": str(getattr(group, "group_name", "") or _group_name(event) or ""),
            "member_count": int(getattr(group, "member_count", 0) or 0),
            "owner": {"id": owner_id, "name": names.get(owner_id, "")},
            "admins": [{"id": uid, "name": names.get(uid, "")} for uid in admin_ids],
            "self_id": self_id,
            "self_name": self_name,
            "self_role": role_of(self_id),
        }
        self._group_role_cache[session_id] = (time.time(), roles)
        self.engine.set_group_roles(session_id, roles)

    def _offer_image_reaction(
        self, event: AstrMessageEvent, session_id: str, sources: list[str]
    ) -> None:
        """群里有人发了图、没点她名：后台看一眼，觉得有意思就交给她下一拍接一句。"""

        if not sources:
            return
        if not event.get_group_id():
            # 私聊里有人发图本来就会回，不算"看图主动开口"
            return
        if _at_targets(event) or _reply_target(event):
            # 点了她名 / 引用她：那是接话，不是看图主动冒泡
            return
        # 用户 m19362：表情包不触发"看图接话"——她收到一张梗图就跑出来点评一句
        # 很不像人。这里按平台标记（mface / 平台 summary）逐张剔掉；认不出哪张是
        # 表情包时标记会全是 False，那就一张都不剔（宁可多看一眼，也别把照片当梗图跳过）。
        marks = _sticker_marks(event, len(sources))
        if any(marks):
            sources = [
                url for url, sticker in zip(sources, marks) if not sticker
            ]
            if not sources:
                return
        media = getattr(self.engine.world, "media", None)
        if media is None or not bool(getattr(media, "reaction_enabled", False)):
            return
        if not getattr(self.vision, "enabled", False):
            return
        task = asyncio.create_task(
            self._deliver_image_reaction(event, session_id, list(sources))
        )
        self._reaction_tasks.add(task)
        task.add_done_callback(self._reaction_tasks.discard)

    def _queue_image_captions(
        self, event: AstrMessageEvent, sources: list[str]
    ) -> None:
        """收图那一刻就把转述排到后台（用户 m08448）。

        以前转述发生在**回复路径**里：9 张图要等 20~40 秒才轮到主模型，而这段时间
        她只是在等。现在改成收消息时就起一个后台协程去看，回复路径最多等
        ``IMG_CAPTION_WAIT_SECONDS``；这一轮没赶上就用原图直接交给多模态主模型，
        描述写进缓存，下一轮（以及挂回聊天记录的那份）就有了。

        只转述**这一轮用不上**的图：能直接交给多模态主模型看的那几张不花这笔钱
        （``chat_image_inline`` 为 ``never`` 时她看不见图，那就全都得转）。
        """

        sources = [str(item).strip() for item in sources if str(item).strip()]
        if not sources:
            return
        vision = getattr(self, "vision", None)
        if vision is None or not getattr(vision, "enabled", False):
            return
        wanted = list(dict.fromkeys(sources))
        task = asyncio.create_task(self._prepare_image_captions(event, wanted))
        self._caption_tasks.add(task)
        task.add_done_callback(self._caption_tasks.discard)

    async def _prepare_image_captions(
        self, event: AstrMessageEvent, sources: list[str]
    ) -> None:
        """挑出「这一轮看不到、只能靠文字描述」的图，交给后台转述。"""

        context = getattr(self.engine.world, "context", None)
        mode = str(getattr(context, "chat_image_inline", "auto") or "auto")
        try:
            cap = max(0, int(getattr(context, "image_max", 3) or 0))
        except Exception:  # noqa: BLE001 - 读不到就按 0（等于全转）
            cap = 0
        wanted: list[str] = []
        # 主模型能不能直接看图：不能看的时候，上限以内那几张也一样只能靠文字，
        # 必须一起转述（用户 m11963）。以前这里只挑「超出上限」的那张，遇到
        # 纯文字主模型就会出现「既没附原图、也没转述」的空档。
        try:
            sees_images = bool(await self._chat_images_inline(event.unified_msg_origin))
        except Exception as exc:  # noqa: BLE001 - 判断不了就按"看不见"处理，宁可多转
            self.logger.debug(f"[virtual_world] 判断主模型能否看图失败：{exc}")
            sees_images = False
        if mode == "never" or cap <= 0 or not sees_images:
            # 主模型看不见图时这里不再提前转述：转述要落在聊天记录那一行上才会
            # 进提示词，而这时候这一条还没入档（会丢）。留给回复那一轮统一转，
            # 见 _caption_overflow（用户 m11963）。
            return
        else:
            # 窗口里超过 cap 的那张（最旧的一张）就是这一轮只能给文字的那张
            try:
                picks = await self.engine.chat_images_for_reply(
                    event.unified_msg_origin, cap + 1
                )
            except Exception:  # noqa: BLE001 - 取不到窗口就不转
                picks = []
            if len(picks) > cap:
                oldest = str(picks[0].get("url") or "")
                if oldest and oldest in sources:
                    wanted = [oldest]
        if not wanted:
            return
        try:
            limit = int(
                getattr(self.engine.world.media, "caption_max_per_round", 0) or 0
            )
        except Exception:  # noqa: BLE001 - 读不到就不限
            limit = 0
        if limit > 0:
            wanted = wanted[:limit]
        if not wanted:
            return
        task = asyncio.create_task(self._caption_in_background(event, wanted))
        self._caption_tasks.add(task)
        task.add_done_callback(self._caption_tasks.discard)

    async def _caption_in_background(
        self, event: AstrMessageEvent, urls: list[str]
    ) -> None:
        """后台跑一次转述：只往缓存和聊天记录里写，不产生任何要发出去的话。"""

        try:
            await self._caption_old_images(event, urls, wait=None)
        except Exception as exc:  # noqa: BLE001 - 后台任务不许把异常抛到事件循环
            self.logger.debug(f"[virtual_world] 后台转述失败：{exc}")

    async def _deliver_image_reaction(
        self, event: AstrMessageEvent, session_id: str, sources: list[str]
    ) -> None:
        """转述这几张图，把「这张有点意思」交给引擎（有趣分由引擎那一拍评）。"""

        try:
            captions = await self.vision.describe(
                sources,
                question=str(event.get_message_str() or ""),
                quoted=_quoted_text(event),
                context_lines=await self._recent_chat_lines(event),
                session_id=session_id,
                scene_hint=_sticker_hint(_sticker_marks(event, len(sources))),
            )
            await self.engine.offer_image_reaction(
                session_id,
                images=sources,
                captions=captions,
                event_id=str(
                    _incoming_identity(event, event.get_message_str() or "").get(
                        "event_id"
                    )
                    or ""
                ),
                sender=str(event.get_sender_id() or ""),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 看图接话登记失败：{exc}")

    # ================= 用户 / 管理员命令 =================

    @filter.command("vw", alias={"世界", "virtualworld"})
    async def cmd_vw(self, event: AstrMessageEvent):
        if self.retired:
            return
        args = _command_args(event)
        action = args[0].lower() if args else "help"
        rest = args[1:]

        if action in ("help", "帮助", "?"):
            yield event.plain_result(_help_text(self._pronoun()))
            return

        if action in ("status", "状态", "where", "在哪"):
            session_id = event.unified_msg_origin
            stamp = self.build_stamp()
            # 存储段（2026-10 磁盘满事故）：**两条路径都带**——出故障时最需要确认
            # 「到底写没写进去 / 磁盘还有没有地方」的恰恰是"看着没启用"的那条。
            storage_lines = self.engine.storage_status_lines()
            # T17：还有几项旧配置等用户确认（没启用的会话也给这一行：先知道有东西要处理）
            migration_lines = self._migrations_status_line()
            if not self.engine.is_enabled(session_id):
                # 正是「没启用」的时候最需要确认实机跑的是哪份代码：早退也带上构建戳
                lines = ["这个会话还没有启用虚拟世界。用 /vw session add 启用。"]
                lines.extend(_build_stamp_lines(stamp)[:2])
                lines.extend(migration_lines)
                lines.extend(storage_lines)
                yield event.plain_result("\n".join(lines))
                return
            snapshot = await self.engine.snapshot(session_id)
            # T12 Step 2：「她自己的打算」那一行。读不到就退化成"暂时没有"，
            # 不让观测把 /vw status 弄挂（snapshot 里没有 agency 就传空面板）。
            agency_panel = snapshot.get("agency")
            lines = [
                _render_status(
                    snapshot,
                    self._pronoun(),
                    build=stamp,
                    agency=agency_panel if isinstance(agency_panel, dict) else {},
                    # T12 Step 3：话题层挂着的那条"拐走没下文"（没有就不加这一行）
                    topic_steer=snapshot.get("topic_steer"),
                )
            ]
            lines.extend(migration_lines)
            lines.extend(storage_lines)
            yield event.plain_result("\n".join(lines))
            return

        if action in ("recall", "回忆"):
            if not rest:
                yield event.plain_result("用法：/vw recall <话题>")
                return
            session_id = event.unified_msg_origin
            topic = " ".join(rest)
            memories = self.engine.memory.recall(
                session_id=session_id,
                persona_id=_event_persona_id(event),
                node_id=(await self.engine.load_state(session_id, cold_start=False)).node_id,
                limit=5,
            )
            hits = [
                item
                for item in self.engine.memory.db.query_memories(
                    session_id=session_id,
                    # 「我记得」必须按**当前人格**分桶（WP6 fail-closed）：以前这里直查
                    # 整张表，于是同一会话里别人格的记忆也会被翻出来念给群里听。
                    persona_id=_event_persona_id(event),
                    limit=300,
                )
                if topic in str(item.get("content", ""))
            ]
            lines = [f"关于「{topic}」我记得："]
            lines += [f"· {item['content']}" for item in hits[:5]]
            if not hits:
                lines.append("· 好像没什么印象……")
            if memories:
                lines.append("此刻想起的：")
                lines += [f"· {item.content}" for item in memories[:3]]
            yield event.plain_result("\n".join(lines))
            return

        if action in ("memory", "记忆"):
            session_id = event.unified_msg_origin
            user_id = event.get_sender_id()
            rows = self.engine.memory.export_user(
                session_id=session_id, persona_id=_event_persona_id(event), user_id=user_id
            )
            if not rows:
                yield event.plain_result("我暂时没有关于你的场景记忆。")
                return
            lines = [f"我记得关于你的 {len(rows)} 件事："]
            lines += [f"· {item['content']}" for item in rows[:10]]
            lines.append("想让我忘掉，可以发 /vw forget me")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("forget", "忘记"):
            session_id = event.unified_msg_origin
            user_id = event.get_sender_id()
            topic = " ".join(rest) if rest and rest[0].lower() not in ("me", "我") else ""
            removed = self.engine.memory.forget_user(
                session_id=session_id,
                persona_id=_event_persona_id(event),
                user_id=user_id,
                topic=topic,
            )
            yield event.plain_result(f"好，我忘记了 {removed} 条相关的记忆。")
            return

        if action in ("pending", "待批", "待办", "审批"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员能看待批的事。")
                return
            rows = await self.engine.owner_pending_tasks(limit=10)
            if not rows:
                yield event.plain_result("现在没有等她动手、等你拍板的事。")
                return
            lines = [f"有 {len(rows)} 件事在等你拍板："]
            for row in rows:
                lines.append(
                    f"· {row.get('task_id')}　{str(row.get('goal') or '')[:40]}"
                    f"（{row.get('session_id') or ''}）"
                )
            lines.append("同意就发 /vw approve <id>，不放心就发 /vw deny <id>")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("approve", "同意", "准", "deny", "拒绝", "拒", "驳回"):
            if not self._can_admin(event) and not self.engine.is_owner(
                event.get_sender_id()
            ):
                yield event.plain_result("只有主人能拍这个板。")
                return
            if not rest:
                yield event.plain_result(f"用法：/vw {action} <任务 id>")
                return
            approved = action in ("approve", "同意", "准")
            result = await self.engine.owner_decide(
                rest[0], approve=approved, answer=" ".join(rest[1:])
            )
            if not result.get("ok"):
                yield event.plain_result(f"没办成：{result.get('reason') or '不知道原因'}")
                return
            status = str(result.get("status") or "")
            if result.get("spoken"):
                yield event.plain_result(f"好，我跟他说了（任务现在是 {status}）。")
            else:
                yield event.plain_result(
                    f"已记录（任务现在是 {status}）：{str(result.get('note') or '')[:80]}"
                )
            return

        if action in ("schedule", "日程"):
            if rest and rest[0].lower() in ("run", "执行", "立即"):
                if not self._can_admin(event):
                    yield event.plain_result("只有管理员可以让日程立刻执行。")
                    return
                if len(rest) < 2:
                    yield event.plain_result("用法：/vw schedule run <日程 id>")
                    return
                manual_operation = str(event.get_extra('vw_manual_schedule_operation_id') or '')
                if not manual_operation:
                    manual_operation = uuid.uuid4().hex
                    event.set_extra('vw_manual_schedule_operation_id', manual_operation)
                result = await self.engine.run_schedule_now(
                    event.unified_msg_origin, rest[1], operation_id=manual_operation
                )
                yield event.plain_result(
                    result.get("note") or result.get("reason") or "已执行"
                )
                return
            lines = ["当前日程："]
            for schedule in (self.engine.schedules.schedules if self.engine.schedules else []):
                mark = "✅" if schedule.enabled else "⛔"
                lines.append(
                    f"{mark} {schedule.time} {schedule.id} → "
                    f"{'/'.join(str(getattr(step, 'type', '')) for step in schedule.action_chain)}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("map", "地图"):
            world = self.engine.world
            lines = ["虚拟世界地图（按区域）："]
            for zone in world.zones:
                here = world.nodes_in_zone(zone.id)
                lines.append(f"【{zone.name}】{len(here)} 个地点")
                for node in here:
                    names = [
                        action.name or action.id
                        for action in world.actions_in(node.id)
                        if action.id != "walk_to"
                    ]
                    lines.append(
                        f"· {node.name}（{node.id}）：{'/'.join(names) or '只有通用动作'}"
                    )
            lines.append("区域内连线：")
            for edge in world.edges:
                lines.append(f"· {edge.from_} ↔ {edge.to}（{edge.ticks} tick）")
            if world.zone_edges:
                lines.append("跨区连线：")
                for edge in world.zone_edges:
                    lines.append(
                        f"· {node_label(world, edge.from_node)} → {node_label(world, edge.to_node)}"
                        f"（{edge.ticks} tick）"
                    )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("nickname", "名片"):
            yield event.plain_result(await self._handle_nickname_command(event, rest))
            return

        if action in ("debug", "调试"):
            yield event.plain_result(await self._handle_debug_command(event, rest))
            return

        if action in ("session", "会话"):
            yield event.plain_result(await self._handle_session_command(event, rest))
            return

        if action in ("reload", "重载"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以重载配置。")
                return
            warnings = self.engine.reload_config()
            message = "配置已热加载。"
            if warnings:
                message += "\n提醒：" + "；".join(warnings)
            yield event.plain_result(message)
            return

        if action in ("reset", "重置"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以重置会话状态。")
                return
            target = rest[0] if rest else event.unified_msg_origin
            await self.db.call("delete_state", target)
            yield event.plain_result(f"已重置 {target} 的世界状态（记忆保留）。")
            return

        if action in ("restore-default", "恢复默认"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以恢复默认配置。")
                return
            restored = self.store.restore_default("all")
            self.engine.reload_config()
            yield event.plain_result("已恢复默认配置：" + "、".join(restored))
            return

        if action in ("event", "事件"):
            # 成功时**不回话**：她自己会在群里说这件事，指令再回一句就是噪音
            text = await self._cmd_event(event, rest)
            if text:
                yield event.plain_result(text)
            return

        if action in ("ability", "能力", "能力值"):
            # 能力值不公开：只有管理员能看（群友要看只能翻日志页）
            if not self._can_admin(event):
                yield event.plain_result("这项不公开，只有管理员可以看。")
                return
            yield event.plain_result(await self._cmd_ability(event))
            return

        if action in ("thread", "线索", "未了"):
            if not self._can_admin(event):
                yield event.plain_result("这项只有管理员可以看。")
                return
            yield event.plain_result(await self._cmd_thread(event))
            return

        yield event.plain_result(f"未知指令：{action}\n\n{_help_text(self._pronoun())}")

    async def _cmd_event(self, event: AstrMessageEvent, rest: list[str]) -> str:
        """`/vw event [要发生的事]`：用户直接给她安排一件事。"""

        if not self._can_deliver_event(event):
            return "只有管理员可以投递事件（可以在全局设置里改成所有人）。"
        session_id = event.unified_msg_origin
        sub = (rest[0] if rest else "").lower()
        if sub in ("list", "列表"):
            overview = await self.engine.event_overview(session_id)
            lines = ["最近的事件线索："]
            rows = [item for item in (overview.get("threads") or []) if item.get("steps")]
            if not rows:
                lines.append("· 还没有发生过什么")
            for item in rows:
                mark = "进行中" if str(item.get("status")) == "open" else "已结束"
                lines.append(f"· [{mark}] {item.get('title') or '一件事'}")
                for step in list(item.get("steps") or [])[:4]:
                    lines.append(f"   - {step}")
            return "\n".join(lines)
        if sub in ("help", "用法", "?"):
            return (
                "/vw event <一句话>\n"
                "　给她安排一件「会发生的事」，她会自己决定怎么处理。\n"
                "　例子：/vw event 出门忘了带伞\n"
                "　/vw event list  看最近的事件线索"
            )
        text = " ".join(rest)
        # 先问一句扩展：有没有谁认领这条（例如「/vw event h」交给扩展起它自己的一段流程）
        handled = await self.extension_host.command_event(session_id, text)
        if handled is not None:
            return handled
        note = await self.engine.submit_event(session_id, text)
        # 成功（note 为空）时不回话：她马上就会在群里说这件事
        return note

    def _can_deliver_event(self, event: AstrMessageEvent) -> bool:
        """谁能用 `/vw event` 投递事件（默认只有管理员）。"""

        if self._can_admin(event):
            return True
        return str(getattr(self.engine.world.events, "event_actor", "admin") or "admin") == "all"

    async def _cmd_ability(self, event: AstrMessageEvent) -> str:
        overview = await self.engine.event_overview(event.unified_msg_origin)
        rows = overview.get("abilities") or {}
        if not rows:
            return "能力值没有启用。"
        detail = "、".join(
            f"{item.get('label')}{item.get('hint')}" for item in rows.values()
        )
        spent = overview.get("ability_spent_today") or {}
        if spent:
            detail += "\n今天已经变化：" + "、".join(
                f"{key} {value:+.3f}" for key, value in spent.items()
            )
        return f"她现在的本事：{detail}"

    async def _cmd_thread(self, event: AstrMessageEvent) -> str:
        overview = await self.engine.event_overview(event.unified_msg_origin)
        pending = overview.get("pending_help") or {}
        lines: list[str] = []
        if str(pending.get("state") or "") == "active":
            lines.append(f"她正在等群友拿主意：{pending.get('title') or '一件事'}")
        elif str(pending.get("state") or "") == "idle":
            lines.append(f"这件事还没解决，她没再提：{pending.get('title') or '一件事'}")
        rows = [item for item in (overview.get("threads") or []) if str(item.get("status")) == "open"]
        if not rows and not lines:
            return "她手头没有没完的事。"
        for item in rows:
            lines.append(f"· {item.get('title') or '一件事'}")
            for step in list(item.get("steps") or [])[:5]:
                lines.append(f"   - {step}")
            if item.get("pending_followup"):
                lines.append(f"   → 还有下一步：{item.get('pending_followup')}")
        return "\n".join(lines)

    # ---------------- 给主人格 / 别的插件用的日程工具 ----------------

    @filter.llm_tool(name="vw_schedule_list")
    async def tool_schedule_list(self, event: AstrMessageEvent) -> str:
        """查看虚拟世界里 Bot 自己的日程表（什么时候会自动做什么）。

        没有参数，直接调用。
        """

        return self.engine.schedule_text()

    @filter.llm_tool(name="vw_schedule_add")
    async def tool_schedule_add(
        self,
        event: AstrMessageEvent,
        time: str,
        actions: str,
        days: str = "",
    ) -> str:
        """给虚拟世界里的 Bot 加一条日程：到点她会自动做一串动作。

        Args:
            time(string): 触发时间，24 小时制，例如 07:30
            actions(string): 动作链，用 > 表示先后顺序，例如 say>walk_to>search_web；需要先去某地时写成 walk_to@书房 id
            days(string): 星期，用逗号分隔，可选 mon,tue,wed,thu,fri,sat,sun；留空表示每天
        """

        chain: list[dict[str, Any]] = []
        for raw in str(actions or "").replace("，", ",").split(">"):
            part = raw.strip()
            if not part:
                continue
            node = ""
            if "@" in part:
                part, node = (piece.strip() for piece in part.split("@", 1))
            step: dict[str, Any] = {"type": part}
            if node:
                step["target_node"] = node
            chain.append(step)
        ok, note = await self.engine.schedule_add(
            {
                "time": time,
                "days": [day.strip() for day in str(days or "").replace("，", ",").split(",") if day.strip()],
                "action_chain": chain,
                "auto_travel": True,
            }
        )
        return note

    @filter.llm_tool(name="vw_schedule_remove")
    async def tool_schedule_remove(
        self,
        event: AstrMessageEvent,
        schedule_id: str = "",
        time: str = "",
        keyword: str = "",
    ) -> str:
        """删掉虚拟世界里 Bot 自己加的一条日程（用户手动配的日程删不掉）。

        Args:
            schedule_id(string): 日程 id，最准；不知道就留空
            time(string): 按时间匹配，例如 07:30
            keyword(string): 按里面出现的动作名匹配，例如 新闻
        """

        ok, note = await self.engine.schedule_remove(
            {"id": schedule_id, "time": time, "keyword": keyword}
        )
        return note

    async def _handle_nickname_command(self, event: AstrMessageEvent, rest: list[str]) -> str:
        sub = rest[0].lower() if rest else ""
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return "这个会话还没有启用虚拟世界。"
        if not self._can_admin(event):
            return "只有管理员可以控制群名片。"
        text = " ".join(rest[1:]).strip()
        async with self.engine.session_state(session_id) as state:
            if sub in ("", "status", "状态"):
                node = self.engine.node(state.node_id)
                desired = compute_nickname(
                    self.engine.world, state, node, base=state.bot_base_nickname
                )
                lines = [
                    f"原名（base）：{state.bot_base_nickname or '（空，还没抓到）'}",
                    f"当前名片：{state.bot_current_nickname or '（未知）'}",
                    f"按当前状态应该显示：{desired or '（算不出来）'}",
                    f"状态：{state.state}　地点：{state.node_id}　改名冷却：{int(self.engine.world.nickname_sync.cooldown_seconds)} 秒",
                    f"锁定：{'是' if state.bot_nickname_locked else '否'}　"
                    f"按地点/状态改名片：{'开' if self.engine.world.nickname_sync.enabled else '关'}",
                ]
                if not desired:
                    lines.append(
                        "⚠ 算不出名片：去「全局设置 → Bot 名称」填一个名字，"
                        "或者在群名片同步里给当前状态/地点配一条文案。"
                    )
                return "\n".join(lines)
            if sub in ("lock", "锁定"):
                state.bot_nickname_locked = True
                return "已锁定群名片，不再自动修改。"
            if sub in ("unlock", "解锁"):
                state.bot_nickname_locked = False
                return "已解锁群名片，会随状态自动变化。"
            if sub in ("set", "设置"):
                if not text:
                    return "用法：/vw nickname set <文本>"
                state.bot_current_nickname = text
                state.bot_base_nickname = text
                state.bot_nickname_locked = True
                changed, failed, reason = await self._apply_card_to_group(session_id, text)
                if not changed:
                    return f"已记录为「{text}」并锁定，但改名片失败：{reason}"
                note = f"已把 {changed} 个群的群名片设为「{text}」并锁定。"
                if failed:
                    note += f"（{failed} 个没改成：{reason}）"
                return note
            if sub in ("reset", "重置"):
                state.bot_nickname_locked = False
                state.bot_current_nickname = state.bot_base_nickname
                if state.bot_base_nickname:
                    changed, failed, reason = await self._apply_card_to_group(
                        session_id, state.bot_base_nickname
                    )
                    if not changed:
                        return f"已解锁，但恢复原名失败：{reason}"
                    if failed:
                        return f"已解锁并改回原名（{changed} 个群，{failed} 个没改成：{reason}）"
                return "已恢复原名并解锁。"
        return "用法：/vw nickname status|lock|unlock|set <文本>|reset"

    # agent 能力的五个子开关：调试输出与注入回调共用的固定顺序与中文名（B0/WP8b）
    # 「开启Agent功能」是与编辑器同一套叫法（用户 m20632 第 2 条：不要叫「总闸」）
    AGENT_SWITCH_LABELS = (
        ("agent_enabled", "开启Agent功能"),
        ("agent_history_lookup_enabled", "历史查询"),
        ("agent_artifact_generation_enabled", "产物生成"),
        ("agent_file_send_enabled", "文件发送"),
        ("agent_code_execution_enabled", "代码执行"),
    )

    async def _handle_debug_command(self, event: AstrMessageEvent, rest: list[str]) -> str:
        if not self._can_admin(event):
            return "只有管理员可以使用调试命令。"
        sub = rest[0].lower() if rest else "state"
        session_id = rest[1] if len(rest) > 1 else event.unified_msg_origin
        if sub in ("on", "开"):
            self.debug = True
            self.engine.debug = True
            return "调试模式已开启（接管详情与动作校验会写入日志）。"
        if sub in ("off", "关"):
            self.debug = False
            self.engine.debug = False
            return "调试模式已关闭。"
        if sub in ("state", "状态"):
            snapshot = await self.engine.snapshot(session_id)
            return json.dumps(snapshot, ensure_ascii=False, indent=2)
        if sub in ("plan", "计划"):
            state = await self.engine.load_state(session_id, cold_start=False)
            from .core import planner as planner_module

            return planner_module.describe(state)
        if sub in ("stop", "停", "停下", "打断"):
            # 不依赖大模型的兜底：立刻停手 + 放弃剩下的安排
            stopped = await self.engine.interrupt(session_id, force=True)
            cleared = await self.engine.clear_plan(session_id)
            parts = []
            parts.append("停掉了手头的动作" if stopped else "她本来就没在做事")
            if cleared:
                parts.append("放弃了还没做的安排")
            return "，".join(parts) + "。"
        if sub in ("memories", "记忆"):
            # 默认只看**当前人格**那个桶（WP6 的 fail-closed 口径）。排查跨桶问题时
            # 在参数里显式写 all / 全部 / * 才看整张表——每行都印出人格桶，
            # 因为"无主"和"某个人格"是两回事，混着看最容易看错。
            everything = any(
                str(item).strip().lower() in ("all", "全部", "*") for item in rest[1:]
            )
            rows = self.engine.memory.db.query_memories(
                session_id=session_id,
                persona_id=None if everything else _event_persona_id(event),
                limit=20,
            )
            if not rows:
                return "（没有记忆）"
            return "\n".join(
                f"[{row['id']}] p={row.get('persona_id') or '无主'} "
                f"{row['scope']}/{row['node_id']} "
                f"w={row['weight']:.2f} {row['content']}"
                for row in rows
            )
        if sub in ("session", "会话"):
            ids = self.engine.enabled_session_ids()
            return "启用中的会话：\n" + "\n".join(f"· {item}" for item in ids)
        if sub in ("tools", "工具"):
            available = self.engine.available_tools()
            state = await self.engine.load_state(session_id, cold_start=False)
            allowed = self.engine.allowed_tool_names(state.node_id)
            return (
                f"当前位置 {state.node_id} 能用：{sorted(allowed)}"
                f"（= 通用工具 + 这里的动作绑定的工具）\n"
                f"AstrBot 已注册工具：{sorted(available)}"
            )
        if sub in ("prompt", "提示词"):
            return await self._prompt_report(session_id, "takeover")
        if sub in ("autoprompt", "自主提示词"):
            return await self._prompt_report(session_id, "autonomous")
        if sub in ("tick", "推进"):
            outcomes = await self.engine.tick()
            return "\n".join(
                f"{item.session_id}: {'; '.join(item.notes)} {item.messages}"
                for item in outcomes
            ) or "（无输出）"
        if sub in ("decide", "决策"):
            outcome = await self.engine.maybe_decide(session_id)
            if outcome is None:
                return "决策器跳过（未到间隔 / 正在忙）。"
            return f"{'; '.join(outcome.notes)} {outcome.messages}"
        if sub in ("agent", "能力", "智能体"):
            # B0/WP8b：agent 能力的只读观测（不发消息、不写库、不创建任务）
            return await self._agent_debug_report(session_id)
        if sub in ("knowledge", "经验", "知识"):
            # P3-1：经验账（只读；按当前会话的作用域列）
            try:
                rows = await self.db.call(
                    "list_capability_experiences", scope=session_id, limit=20
                )
            except Exception as exc:
                return f"经验账读不出来：{exc}"
            if not rows:
                return "这个会话里还没有攒下经验（做成一件事之后才会有候选）。"
            lines = [f"经验：{len(rows)} 条"]
            for row in rows:
                item = knowledge.from_row(row)
                if item is None:
                    continue
                label = knowledge.STATUS_LABELS.get(item.status, item.status)
                lines.append(
                    f"- {label}｜{item.situation}：{item.method}"
                    + (f"（注意：{item.limitations}）" if item.limitations else "")
                    + f"｜证据 {len(item.evidence)} 条｜v{item.revision}"
                )
            return chr(10).join(lines)
        if sub in ("assets", "资产"):
            report = self._growth_store().ownership()
            lines = [
                f"成长资产：{report.get('total', 0)} 条",
                "按来源：" + (", ".join(f"{k} {v}" for k, v in (report.get("by_source") or {}).items()) or "（空）"),
                report.get("note", ""),
            ]
            for row in (report.get("rows") or [])[:12]:
                lines.append(
                    f"- {row.get('asset_id')}｜v{row.get('version')}｜{row.get('source_label')}"
                    f"{'｜可被升级覆盖' if row.get('overwritable') else '｜本地所有'}"
                )
            return chr(10).join(lines)
        if sub in ("ledger", "经历", "经历账"):
            # E2/E3：今天到现在她都经历过什么（只读，账在 `daily_experiences`）
            return await self._debug_ledger_report(session_id)
        if sub in ("index", "索引", "整理"):
            # A4/A5/A6 + M4：动作说明整理队列、每轮能力索引的账、表情包向量
            return await self._debug_index_report()
        return (
            "用法：/vw debug on|off|state|plan|stop|memories|session|tools|prompt|autoprompt|tick"
            "|decide|agent|ledger|index"
            "（memories 默认只看当前人格那个桶，加 all 看整张表；"
            "agent 会列出最近调用了哪些工具；ledger 是今日经历账；index 是动作说明整理与能力索引）"
        )

    # ---------------- 调试：最近工具调用 / 经历账 / 索引与整理 ----------------

    async def _debug_recent_runs(self, session_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """最近几次工具调用（**只读**，倒序）；读不出来回空列表、绝不抛。

        数据来自 `event_log` 的 `tool_receipt`（每次调用都有一条，带 `detail.tool`），
        经 `core.timeline.build_agent_runs` 归一成"名字 / 状态 / 时间 / 摘要"。
        参数与正文不往调试输出里放：那里可能夹着地址或用户内容。
        """

        try:
            from .core.timeline import build_agent_runs

            rows = await self.db.call(
                "list_agent_runs",
                session_id=session_id or None,
                limit=max(1, min(50, int(limit))),
            )
            return build_agent_runs(rows)
        except Exception:
            return []

    async def _debug_ledger_report(self, session_id: str) -> str:
        """`/vw debug ledger`：今日经历账 + 概览文本（只读）。"""

        engine = getattr(self, "engine", None)
        if engine is None:
            return "引擎还没起来，读不到经历账。"
        try:
            state = await engine.load_state(session_id, cold_start=False)
        except Exception as exc:
            return f"读不到这个会话的状态（{exc}）。"
        lines = ["【今天的经历账】"]
        try:
            rows = await self.db.call(
                "list_experiences",
                day=engine.world_day_key(),
                session_id=session_id,
                limit=20,
            )
        except Exception as exc:
            return f"经历账读不出来（{exc}）。"
        if not rows:
            lines.append("  今天还没有记录（动作有终态、工具拿到回执时才会写进来）。")
        else:
            lines.append(f"  今天记了 {len(rows)} 条（最近的在前）：")
            for row in rows:
                stamp = ""
                started = float(row.get("started_at") or 0.0)
                if started:
                    stamp = time.strftime("%H:%M", time.localtime(started))
                status = str(row.get("status") or "")
                label = {
                    "planned": "计划中",
                    "started": "进行中",
                    "succeeded": "已完成",
                    "failed": "没做成",
                    "cancelled": "取消了",
                    "paused": "暂停",
                    "unknown": "结果不确定",
                }.get(status, status or "未知")
                text = str(row.get("short_text") or row.get("activity_kind") or "一件事")
                note = str(row.get("result_summary") or "").strip()
                tail = f"（{note[:40]}）" if note else ""
                lines.append(f"    · {stamp} {text}：{label}{tail}")
        try:
            overview = await engine.daily_experience_overview(state)
        except Exception as exc:
            overview = ""
            lines.append(f"  概览算不出来（{exc}）")
        if overview:
            lines.append("")
            lines.append("【进提示词的那一段】")
            lines.extend(f"  {line}" for line in overview.splitlines())
        else:
            lines.append("  概览：还没到能成段的时候（没有经历 / 读不出来）")
        return "\n".join(lines)

    async def _debug_index_report(self) -> str:
        """`/vw debug index`：动作说明整理队列 + 每轮能力索引的账 + 表情包向量（只读）。"""

        lines = ["【动作说明整理（A4/A5/A6）】"]
        try:
            report = await self._action_discovery_report()
        except Exception as exc:
            report = {}
            lines.append(f"  读不出来（{exc}）")
        if report:
            queue = report.get("queue") or {}
            lines.append(f"  动作总数：{report.get('total')}｜已有简述：{report.get('ready')}")
            lines.append(f"  还缺简述：{len(report.get('missing') or [])}｜人工锁定：{report.get('locked')}")
            lines.append(
                f"  队列：压着 {queue.get('pending', 0)} 条"
                + (f"｜上次错误：{queue.get('last_error')}" if queue.get("last_error") else "")
            )
            dropped = queue.get("dropped_fields") or []
            if dropped:
                lines.append(f"  被丢掉的字段（像密钥的，不进指纹）：{'、'.join(str(x) for x in dropped)}")
        try:
            engine = getattr(self, "engine", None)
            batch = engine.config_index_debug() if engine is not None else {}
        except Exception:
            batch = {}
        if batch:
            lines.append(
                "  最近一批："
                + "，".join(
                    f"{key}={batch.get(key)}"
                    for key in ("scanned", "applied", "skipped", "failed", "stopped", "last_error")
                    if batch.get(key) not in (None, "", 0)
                )
            )
        lines.append("")
        lines.append("【每轮能力索引的账】")
        try:
            info = engine.capability_index_debug() if engine is not None else {}
        except Exception as exc:
            info = {}
            lines.append(f"  读不出来（{exc}）")
        if info:
            lines.append(
                f"  最近渲染：地点 {info.get('node_id') or '未知'}｜总数 {info.get('total')}"
                f"｜进了 {info.get('kept')}｜砍掉 {len(info.get('dropped') or [])}"
                f"｜{info.get('chars')} 字符（约 {info.get('approx_tokens')} token，上限 {info.get('max_chars')}）"
            )
            if info.get("agent_block"):
                lines.append("  含 Agent 独立区块：是")
            if info.get("truncated"):
                lines.append("  这一段被预算截断过（被砍的每一条都有原因，见下）")
            for item in (info.get("dropped") or [])[:8]:
                pair = item if isinstance(item, (list, tuple)) else (item, "")
                lines.append(f"    · 没进 {pair[0]}：{pair[1] or '没写原因'}")
            if info.get("error"):
                lines.append(f"  渲染错误：{info.get('error')}")
        else:
            lines.append("  还没有渲染过（她这一轮还没走到决策）")
        lines.append("")
        lines.append("【表情包向量（M4）】")
        try:
            store = engine.meme_store() if engine is not None else None
        except Exception:
            store = None
        if store is None:
            lines.append("  表情包收藏层没起来")
        else:
            settings = getattr(store, "settings", None)
            lines.append(
                f"  总开关：{'开' if getattr(settings, 'enabled', False) else '关'}"
                f"｜检索档：{getattr(settings, 'resolved_retrieval_mode', '') or '未知'}"
            )
            try:
                count = await self.db.call("count_meme_vectors", status="ready")
                lines.append(f"  已就绪向量：{count} 条")
            except Exception as exc:
                lines.append(f"  向量条数读不出来（{exc}）")
        return "\n".join(lines)

    # ---------------- B0/WP8b：agent 能力观测（只读调试输出） ----------------

    def _agent_status_for_engine(self) -> dict[str, Any]:
        """注入引擎 ``agent_status`` 的同步回调：只读、永不抛异常。

        引擎那边（``core/engine.py`` 的 ``agent_section``）对这一段是**浅合并**——
        同名键整个替换，所以 ``switches`` 必须一次给全，不能只报变了的那几个。

        这里只做同步就能定的两级回退（agent 配置 → 插件用主模型）：会话默认模型要
        await 宿主接口，只有 ``/vw debug agent`` 那条 async 路径问得动，所以
        ``source=session`` 只会在那条路径上出现。这里给的 provider 段只是同步视图，
        真正的解析以 :meth:`resolve_agent_provider_id` 为准。

        ``index`` 段里的向量空间指纹只读**内存里已有的值**（B1 收尾）：先看
        :meth:`_assemble_embedding_runtime` 的装配缓存（``self._embedding_space``），
        再退一步看引擎手上那个索引队列（``engine.topic_index_queue``）——装配成功时
        两处都写，但真机点亮的教训是"状态说好了"与"引擎真的在跑"是两条路，能让
        读观测的人看见引擎实际在用的那个空间才对得上账。两处都没有时**明确报未装配**
        （``SPACE_FINGERPRINT_UNBUILT``），不再用空串装作有值。这个回调是同步的：
        不重装配、不 await 队列、不发嵌入请求。
        """

        settings: dict[str, Any] = {}
        try:
            raw = self.agent_settings()
            if isinstance(raw, dict):
                settings = raw
        except Exception:
            settings = {}

        def text(value: Any) -> str:
            try:
                return str(value or "").strip()
            except Exception:
                return ""

        def flag(name: str) -> bool:
            try:
                return bool(settings.get(name))
            except Exception:
                return False

        configured = text(settings.get("agent_provider_id"))
        fallback = text(getattr(self, "llm_provider_id", ""))
        if configured:
            provider = {
                "configured": configured,
                "resolved": configured,
                "source": "agent",
                "reason": "",
            }
        elif fallback:
            provider = {
                "configured": "",
                "resolved": fallback,
                "source": "llm",
                "reason": "",
            }
        else:
            provider = {
                "configured": "",
                "resolved": "",
                "source": "none",
                "reason": "agent 模型没配，插件用主模型也没配；会话默认模型要按会话来问",
            }

        embedding_id = ""
        try:
            resolver = getattr(self, "resolve_embedding_provider_id", None)
            if callable(resolver):
                resolved = resolver()
                if isinstance(resolved, dict):
                    embedding_id = text(resolved.get("id"))
        except Exception:
            embedding_id = ""

        # 向量空间指纹：先看装配缓存，装配缓存空着时退一步看**引擎真正在用的**索引队列
        # （同一个装配结果，但引擎字段与插件字段是两处写入，会分岔）。两处都没有就报
        # "未装配"这个明确字面量——空串在这个字段上是装作有值：读的人分不清"还没装配"
        # 与"指纹就是一个空字符串"。这个回调是同步的，所以只读内存里的字段：
        # 不现场重装配、不 await 索引队列、不发嵌入请求。
        def space_fingerprint_of(holder: Any) -> str:
            # ``EmbeddingSpace`` 叫 fingerprint，``TopicIndexQueue`` 叫 space_fingerprint
            for name in ("fingerprint", "space_fingerprint"):
                try:
                    value = text(getattr(holder, name, ""))
                except Exception:
                    value = ""
                if value:
                    return value
            return ""

        space_fingerprint = ""
        for holder in (
            getattr(self, "_embedding_space", None),
            getattr(getattr(self, "engine", None), "topic_index_queue", None),
        ):
            space_fingerprint = space_fingerprint_of(holder)
            if space_fingerprint:
                break
        if not space_fingerprint:
            space_fingerprint = SPACE_FINGERPRINT_UNBUILT

        return {
            "enabled": flag("agent_enabled"),
            "switches": {name: flag(name) for name, _label in self.AGENT_SWITCH_LABELS},
            # 配置原文（可能是空串）：字段存在 ≠ 能力开启
            "agent_provider_id": configured,
            "embedding_provider_id": text(settings.get("embedding_provider_id")),
            "provider": provider,
            "index": {
                "embedding_configured": bool(embedding_id),
                "space_fingerprint": space_fingerprint,
                "indexed": 0,
                "pending": 0,
                "last_error": text(getattr(self, "embedding_last_error", "")),
                # 维度与它的来路：0 表示还没装配好；来源 "" 表示没问成
                "dim": int(getattr(self, "_embedding_dim", 0) or 0),
                "dim_source": text(getattr(self, "_embedding_dim_source", "")),
                # 没开的原因（装配失败时的原文），前端「语义索引」卡可展开看
                "problem": text(getattr(self, "_embedding_assembly_problem", "")),
            },
        }

    async def _agent_debug_report(self, session_id: str | None = None) -> str:
        """``/vw debug agent``：把 agent 能力的观测段渲染成中文文本。

        只读：不发消息、不写库、不创建任务、不碰大模型。会话那一级回退要 await
        宿主接口，所以完整解析链在这里问，再交给 :meth:`_render_agent_report` 渲染。
        """

        chain: dict[str, str] = {"id": "", "source": "none", "reason": ""}
        try:
            resolver = getattr(self, "resolve_agent_provider_id", None)
            if callable(resolver):
                resolved = await resolver(session_id)
                if isinstance(resolved, dict):
                    chain = {
                        "id": str(resolved.get("id") or "").strip(),
                        "source": str(resolved.get("source") or "none").strip()
                        or "none",
                        "reason": str(resolved.get("reason") or ""),
                    }
            else:
                chain = {
                    "id": "",
                    "source": "none",
                    "reason": "插件里没有 resolve_agent_provider_id，问不到解析链",
                }
        except Exception as exc:
            chain = {"id": "", "source": "none", "reason": f"解析 agent 模型失败：{exc}"}

        embedding: dict[str, str] = {"id": "", "source": "none", "reason": ""}
        try:
            resolver = getattr(self, "resolve_embedding_provider_id", None)
            if callable(resolver):
                resolved = resolver()
                if isinstance(resolved, dict):
                    embedding = {
                        "id": str(resolved.get("id") or "").strip(),
                        "source": str(resolved.get("source") or "none").strip()
                        or "none",
                        "reason": str(resolved.get("reason") or ""),
                    }
        except Exception as exc:
            embedding = {
                "id": "",
                "source": "none",
                "reason": f"解析 embedding 模型失败：{exc}",
            }

        section: Any = None
        reader = None
        try:
            engine = getattr(self, "engine", None)
            reader = getattr(engine, "agent_section", None)
        except Exception:
            reader = None
        if callable(reader):
            try:
                section = reader()
            except Exception as exc:
                section = {"error": f"读 agent 观测段失败：{exc}"}
        else:
            # 引擎没有 agent_section（旧引擎 / 手搓的假引擎）：不崩，按缺省渲染
            section = None

        try:
            taken = await self._debug_recent_runs(session_id) if session_id else []
        except Exception:
            taken = []
        try:
            return self._render_agent_report(section, chain, embedding, taken)
        except Exception as exc:  # 渲染兜底：调试命令绝不因观测段挂掉
            return f"agent 能力状态读不出来（{exc}）。"

    def _render_agent_report(
        self,
        section: Any,
        chain: dict[str, str] | None = None,
        embedding: dict[str, str] | None = None,
        runs: list[dict[str, Any]] | None = None,
    ) -> str:
        """把观测段渲染成人能读的中文：缺数据写「无」，只印白名单字段。

        只印下面明确列出来的键——观测段将来要是混进了供应商配置对象，也不会顺着
        调试输出漏出去（不印 api_key / token / secret / password / base_url）。
        """

        def value(item: Any) -> dict[str, Any]:
            return item if isinstance(item, dict) else {}

        def on(flag_value: Any) -> str:
            try:
                return "已启用" if bool(flag_value) else "未启用"
            except Exception:
                return "未启用"

        def shown(item: Any, default: str = "无") -> str:
            if item is None:
                return default
            if isinstance(item, bool):
                return "是" if item else "否"
            if isinstance(item, str):
                return item.strip() or default
            if isinstance(item, (list, tuple, set, dict)):
                return f"{len(item)} 条" if item else default
            try:
                return str(item)
            except Exception:
                return default

        def names(item: Any, default: str = "无") -> str:
            """名字列表：只认字符串元素（工具名之类），别的一概不印。"""

            if isinstance(item, (list, tuple, set)):
                picked = [
                    entry.strip()
                    for entry in item
                    if isinstance(entry, str) and entry.strip()
                ]
                return "、".join(picked) if picked else default
            if isinstance(item, str) and item.strip():
                return item.strip()
            return default

        def rows(item: Any) -> int:
            return len(item) if isinstance(item, (list, tuple, set, dict)) else 0

        def ledger(item: Any) -> int:
            """账本条目数：dict 段看 items，直接给 list 的就看长度。"""

            if isinstance(item, dict):
                return rows(item.get("items"))
            return rows(item)

        try:
            block = value(section)
            chain = value(chain)
            embedding = value(embedding)
            lines = ["🤖 Agent 能力（B0 只读观测：字段存在 ≠ 能力开启）", ""]

            error = block.get("error")
            if isinstance(error, str) and error.strip():
                lines.append(f"⚠️ 观测段异常：{error.strip()}（其余字段按缺省显示）")
                lines.append("")

            lines.append(f"【开启Agent功能 enabled】{on(block.get('enabled'))}")

            switches = value(block.get("switches"))
            lines.append("【子开关 switches】")
            if switches:
                for name, label in self.AGENT_SWITCH_LABELS:
                    lines.append(f"  {label} {name}：{on(switches.get(name))}")
            else:
                lines.append("  无（观测段没给这一段）")

            lines.append("【供应商 provider】")
            lines.append(
                f"  配置原文 agent_provider_id：{shown(block.get('agent_provider_id'))}"
            )
            lines.append(
                f"  配置原文 embedding_provider_id：{shown(block.get('embedding_provider_id'))}"
            )
            source = str(chain.get("source") or "none").strip() or "none"
            source_label = {
                "agent": "来自 agent 模型配置",
                "llm": "来自插件用主模型",
                "session": "来自当前会话默认模型",
            }.get(source, "没解析到可用模型")
            lines.append(
                f"  解析链 agent → 主模型 → 会话模型：{shown(chain.get('id'))}"
                f"（source={source}，{source_label}）"
            )
            reason = str(chain.get("reason") or "").strip()
            if reason:
                lines.append(f"  解析说明：{reason}")
            elif source == "none":
                lines.append("  解析说明：没解析到可用的模型 id")
            else:
                lines.append("  解析说明：无")

            engine_view = value(block.get("provider"))
            if engine_view:
                lines.append(
                    f"  引擎同步视图：{shown(engine_view.get('resolved'))}"
                    f"（source={shown(engine_view.get('source'), 'none')}）"
                )
                view_reason = str(engine_view.get("reason") or "").strip()
                if view_reason:
                    lines.append(f"  引擎同步视图说明：{view_reason}")

            budgets = value(block.get("budgets"))
            lines.append("【预算 budgets】")
            if budgets:
                lines.append(
                    f"  整件事最多跑多久 query_total_seconds："
                    f"{shown(budgets.get('query_total_seconds'))} 秒"
                )
                lines.append(
                    f"  一次调用最多等多久 request_timeout_seconds："
                    f"{shown(budgets.get('request_timeout_seconds'))} 秒"
                )
                lines.append(
                    f"  后台批处理 index_batch_seconds："
                    f"{shown(budgets.get('index_batch_seconds'))} 秒"
                )
            else:
                lines.append("  无（观测段没给这一段）")

            lines.append(f"【步数上限 max_steps】{shown(block.get('max_steps'))}")

            tools = value(block.get("tools"))
            lines.append("【工具 tools】")
            if tools:
                lines.append(
                    f"  允许 allowed（{rows(tools.get('allowed'))} 个）："
                    f"{names(tools.get('allowed'))}"
                )
                lines.append(
                    f"  可用 available（{rows(tools.get('available'))} 个）："
                    f"{names(tools.get('available'))}"
                )
                lines.append(
                    f"  来源 sources：{rows(tools.get('sources'))} 条（只给数量，不给参数）"
                )
            else:
                lines.append("  无（观测段没给这一段）")

            tasks = value(block.get("tasks"))
            lines.append("【任务 tasks】")
            if tasks:
                lines.append(f"  支持 supported：{shown(tasks.get('supported'))}")
                lines.append(f"  说明 note：{shown(tasks.get('note'))}")
                lines.append(f"  账本 items：{ledger(tasks)} 条")
            else:
                lines.append("  无（观测段没给这一段）")

            results = value(block.get("tool_results"))
            lines.append("【工具结果 tool_results】")
            if results:
                lines.append(f"  已记录：{ledger(results)} 条")
            elif block.get("tool_results") is not None:
                lines.append("  已记录：0 条")
            else:
                lines.append("  无（观测段没给这一段）")
            # 她调了什么工具：只印 名字 / 状态 / 时间 / 一句话摘要（**不印参数与正文**，
            # 那些可能带地址或用户内容）。这是"能不能检测到她调了哪个工具"的正解：
            # 每次调用在 `event_log` 里都有一条 `tool_receipt`，这里读最近的几条。
            if runs:
                lines.append("  最近调用（名字 / 状态 / 什么时候）：")
                for item in runs:
                    name = str(item.get("name") or "（没记名字）")
                    status = str(item.get("status_label") or item.get("status") or "未知")
                    when = float(item.get("at") or 0.0)
                    stamp = (
                        time.strftime("%m-%d %H:%M:%S", time.localtime(when)) if when else "时间未知"
                    )
                    tail = ""
                    summary = str(item.get("summary") or "").strip().replace("\n", " ")
                    if summary:
                        tail = f"｜{summary[:40]}"
                    task = str(item.get("task_id") or "")
                    if task:
                        tail += f"｜任务 {task}"
                    lines.append(f"    · {name}：{status}｜{stamp}{tail}")
            elif runs is not None:
                lines.append("  最近调用：还没有记录")

            artifacts = value(block.get("artifacts"))
            lines.append("【产物 artifacts】")
            if artifacts:
                lines.append(f"  支持 supported：{shown(artifacts.get('supported'))}")
                lines.append(f"  说明 note：{shown(artifacts.get('note'))}")
                lines.append(f"  账本 items：{ledger(artifacts)} 条")
            else:
                lines.append("  无（观测段没给这一段）")

            trace = value(block.get("trace_id"))
            lines.append("【追踪 trace_id】")
            if trace:
                lines.append(f"  最近一条 last：{shown(trace.get('last'))}")
                lines.append(f"  步数 steps：{shown(trace.get('steps'))}")
            else:
                lines.append("  无（观测段没给这一段）")

            isolation = value(block.get("isolation"))
            lines.append("【隔离 isolation】")
            if isolation:
                lines.append(
                    f"  记忆范围 memory_scope_mode："
                    f"{shown(isolation.get('memory_scope_mode'))}"
                )
                lines.append(
                    f"  空人设范围 empty_persona_scope："
                    f"{shown(isolation.get('empty_persona_scope'))}"
                )
            else:
                lines.append("  无（观测段没给这一段）")

            index = value(block.get("index"))
            lines.append("【索引 index】")
            if index:
                lines.append(
                    f"  embedding 是否配置 embedding_configured："
                    f"{shown(index.get('embedding_configured'))}"
                )
                lines.append(
                    f"  向量空间指纹 space_fingerprint："
                    f"{shown(index.get('space_fingerprint'))}"
                )
                lines.append(f"  已索引 indexed：{shown(index.get('indexed'))} 条")
                lines.append(f"  待索引 pending：{shown(index.get('pending'))} 条")
                lines.append(f"  最近错误 last_error：{shown(index.get('last_error'))}")
            else:
                lines.append("  无（观测段没给这一段）")
            lines.append(
                f"  embedding 解析：{shown(embedding.get('id'))}"
                f"（source={shown(embedding.get('source'), 'none')}）"
            )
            embed_reason = str(embedding.get("reason") or "").strip()
            if embed_reason:
                lines.append(f"  embedding 说明：{embed_reason}")

            lines.append("")
            lines.append("（只读：不发消息、不写库、不创建任务；真正的执行器在 B3）")
            return "\n".join(lines)
        except Exception as exc:  # 渲染兜底：调试命令绝不因观测段挂掉
            return f"agent 能力观测段渲染失败（{exc}）。"

    async def _prompt_report(self, session_id: str, mode: str) -> str:
        """把提示词写成文件，并回一份「分段索引」。

        提示词三四千字，直接丢进聊天窗口会被平台截掉尾巴（正好是群聊上下文那几段）。
        这里只发索引 + 文件路径，原文让用户去编辑器或文件里看。
        """

        if mode == "autonomous":
            text = await self.engine.preview_autonomous_prompt(session_id)
            name = "prompt_autonomous.txt"
            label = "自主提示词"
        else:
            text = await self.engine.preview_last_reply_request(session_id)
            name = "prompt_takeover.txt"
            label = "接管请求（上一轮实际发出去的原文）"
        path = Path(self.store.data_dir) / name
        try:
            path.write_text(text, encoding="utf-8")
            written = str(path)
        except Exception as exc:
            written = f"（写文件失败：{exc}）"
        lines = [f"{label}：{len(text)} 字符，共 {len(prompt_section_index(text))} 段"]
        for item in prompt_section_index(text):
            lines.append(f"· {item['title']}：{item['chars']} 字符")
        lines.append(f"完整内容已写入：{written}")
        lines.append("（编辑器「调试 → 预览接管请求 / 预览自主提示词」里能看全文）")
        return "\n".join(lines)

    async def _handle_session_command(self, event: AstrMessageEvent, rest: list[str]) -> str:
        if not self._can_admin(event):
            return "只有管理员可以管理会话白名单。"
        sub = rest[0].lower() if rest else "list"
        if sub in ("list", "列表"):
            sessions = self.engine.sessions.sessions if self.engine.sessions else []
            if not sessions:
                return "白名单为空。"
            lines = [
                f"{'✅' if item.enabled else '⛔'} {item.session_id} ({item.type})"
                + (f" - {item.note}" if item.note else "")
                for item in sessions
            ]
            groups = list(getattr(self.engine.sessions, "groups", None) or [])
            if groups:
                lines.append("")
                lines.append("会话组（同一个她，共享状态 / 记忆 / 上下文）：")
                lines.extend(
                    f"🔗 {group.name or group.id}："
                    + "、".join(group.sessions or [])
                    + (f"（代表：{group.main_session}）" if group.main_session else "")
                    for group in groups
                )
            return "会话白名单：\n" + "\n".join(lines)
        if sub in ("add", "添加"):
            target = rest[1] if len(rest) > 1 else event.unified_msg_origin
            session_type = "private" if event.is_private_chat() and target == event.unified_msg_origin else (
                "group" if ":GroupMessage:" in target else "private"
            )
            added = self.store.add_session(target, session_type=session_type)
            self.engine.reload_config()
            return f"{'已添加' if added else '已在白名单中'}：{target}"
        if sub in ("remove", "移除", "delete"):
            if len(rest) < 2:
                return "用法：/vw session remove <会话 ID>"
            removed = self.store.remove_session(rest[1])
            self.engine.reload_config()
            return "已移除。" if removed else "没找到这个会话。"
        if sub in ("enable", "启用", "disable", "禁用"):
            if len(rest) < 2:
                return "用法：/vw session enable|disable <会话 ID>"
            enabled = sub in ("enable", "启用")
            target = rest[1]
            ok = self.store.set_session_enabled(target, enabled)
            self.engine.reload_config()
            if ok and enabled:
                # 审查 H2：临时会话被 ended / expired 掐住时，光启用白名单会出现
                # "启用着但消息被吞"。这里顺手把它复位成 active，她接着上次说。
                await self._revive_temp_sessions([target])
            return ("已更新。" if ok else "没找到这个会话。")
        return "用法：/vw session list|add [ID]|remove <ID>|enable|disable <ID>"

    # ================= Web API =================

    def sampling_params(self) -> dict[str, float]:
        """给适配器用的采样参数（关着就返回空字典＝不覆盖 Provider 自己的设置）。"""

        config = dict(getattr(self, "_sampling_config", None) or {})
        if not config.get("enabled"):
            return {}
        result: dict[str, float] = {}
        for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            try:
                number = float(config.get(key))
            except (TypeError, ValueError):
                continue
            result[key] = number
        return result

    def _register_web_apis(self) -> None:
        register = self.context.register_web_api
        p = PLUGIN_NAME
        register(f"/{p}/auth/status", self.api_auth_status, ["GET"], "编辑器鉴权状态")
        register(f"/{p}/auth/login", self.api_auth_login, ["POST"], "编辑器登录")
        register(f"/{p}/auth/logout", self.api_auth_logout, ["POST"], "退出登录")
        register(f"/{p}/auth/password", self.api_auth_password, ["POST"], "修改访问密码")
        register(f"/{p}/config", self.api_get_config, ["GET"], "读取全部配置")
        register(f"/{p}/config/world", self.api_put_world, ["POST"], "保存世界配置")
        # T17：内容迁移先出清单，用户勾过才写盘（apply 里 save_world + reload_config）
        register(
            f"/{p}/migrations", self.api_get_migrations, ["GET"], "列出可升级的旧配置"
        )
        register(
            f"/{p}/migrations/apply",
            self.api_apply_migrations,
            ["POST"],
            "应用勾中的旧配置升级",
        )
        register(
            f"/{p}/migrations/skip",
            self.api_skip_migrations,
            ["POST"],
            "这些项别再问",
        )
        register(
            f"/{p}/config/reset",
            self.api_reset_config,
            ["POST"],
            "把选中的几块配置恢复成这一版的默认值",
        )
        # A5：动作简述（派生信息）的三个运维入口——看缺什么、批量重整理、预览会动哪些
        register(
            f"/{p}/actions/discovery", self.api_action_discovery_status, ["GET"],
            "动作简述整理状态",
        )
        register(
            f"/{p}/actions/discovery/rebuild", self.api_action_discovery_rebuild, ["POST"],
            "批量重整理动作简述",
        )
        register(
            f"/{p}/actions/discovery/preview", self.api_action_discovery_preview, ["POST"],
            "预览要整理哪些动作",
        )
        register(
            f"/{p}/actions/discovery/tidy", self.api_action_discovery_tidy, ["POST"],
            "现在扫一次并排进整理队列",
        )
        register(f"/{p}/config/schedules", self.api_put_schedules, ["POST"], "保存日程")
        register(f"/{p}/schedule/repeat/preview", self.api_schedule_repeat_preview, ["POST"], "把日程频率翻译成 cron 与下一次触发时间")
        register(f"/{p}/config/sessions", self.api_put_sessions, ["POST"], "保存会话白名单")
        # 临时会话（用户 m00001 需求 12）：列表 + 归档/恢复
        register(
            f"/{p}/temp_sessions",
            self.api_temp_sessions,
            ["GET"],
            "临时会话列表（活跃 / 已结束 / 已归档）",
        )
        register(
            f"/{p}/temp_sessions/archive",
            self.api_temp_session_archive,
            ["POST"],
            "归档 / 恢复一条临时会话",
        )
        register(
            f"/{p}/temp_sessions/release",
            self.api_temp_session_release,
            ["POST"],
            "解除接管（消息回到宿主默认路由）",
        )
        register(f"/{p}/tools", self.api_tools, ["GET"], "可用工具列表")
        register(f"/{p}/generate/actions", self.api_generate_actions, ["POST"], "按区域生成动作")
        register(f"/{p}/generate/nodes", self.api_generate_nodes, ["POST"], "按区域生成地点")
        register(f"/{p}/reload", self.api_reload, ["POST"], "热加载配置")
        register(f"/{p}/restore-default", self.api_restore, ["POST"], "恢复默认配置")
        register(f"/{p}/state", self.api_state, ["GET"], "实时状态")
        register(f"/{p}/states", self.api_states, ["GET"], "所有会话的简要状态")
        register(f"/{p}/state/action", self.api_state_action, ["POST"], "手动触发动作")
        register(f"/{p}/logs", self.api_logs, ["GET"], "事件日志")
        register(f"/{p}/logs/export", self.api_logs_export, ["GET"], "导出事件日志")
        register(f"/{p}/export", self.api_export, ["GET"], "一键导出：现状/对话/计划/记忆")
        register(f"/{p}/memories", self.api_memories, ["GET"], "记忆列表")
        register(f"/{p}/memories/create", self.api_memory_create, ["POST"], "新增记忆")
        register(f"/{p}/memories/update", self.api_memory_update, ["POST"], "修改记忆")
        register(f"/{p}/memories/delete", self.api_memory_delete, ["POST"], "删除记忆")
        register(f"/{p}/memories/delete-batch", self.api_memory_delete_batch, ["POST"], "批量删除记忆")
        register(f"/{p}/memories/clear", self.api_memory_clear, ["POST"], "清空记忆")
        register(f"/{p}/memories/tidy", self.api_memory_tidy, ["POST"], "整理重复记忆")
        register(f"/{p}/summaries/save", self.api_summary_save, ["POST"], "改会话摘要")
        register(f"/{p}/logs/delete-batch", self.api_logs_delete_batch, ["POST"], "批量删除日志")
        register(f"/{p}/logs/clear", self.api_logs_clear, ["POST"], "清空日志")
        register(f"/{p}/memories/stats", self.api_memory_stats, ["GET"], "记忆统计")
        register(f"/{p}/memories/export", self.api_memory_export, ["GET"], "导出记忆")
        register(f"/{p}/memories/import", self.api_memory_import, ["POST"], "导入记忆")
        register(f"/{p}/prompt", self.api_prompt, ["GET"], "预览提示词")
        register(f"/{p}/growth/assets", self.api_growth_assets, ["GET"], "成长资产与所有权")
        register(f"/{p}/skills", self.api_skills, ["GET"], "Skill 清单（内置 + 提炼的）")
        register(f"/{p}/learning", self.api_learning, ["GET"], "她学到了什么（学习状态）")
        register(
            f"/{p}/skills/status",
            self.api_skill_status,
            ["POST"],
            "发布 / 撤销一个 skill",
        )
        register(
            f"/{p}/skills/create",
            self.api_skill_create,
            ["POST"],
            "手动新增一条流程",
        )
        register(
            f"/{p}/skills/delete",
            self.api_skill_delete,
            ["POST"],
            "删掉一条自己加的流程",
        )
        register(
            f"/{p}/skills/files",
            self.api_skill_files,
            ["GET"],
            "内置流程的文件（可直接改）",
        )
        register(
            f"/{p}/skills/files/save",
            self.api_skill_file_save,
            ["POST"],
            "保存一份内置流程",
        )
        register(
            f"/{p}/skills/files/reset",
            self.api_skill_file_reset,
            ["POST"],
            "把内置流程还原成默认",
        )
        register(f"/{p}/skills/folder", self.api_skill_folder, ["POST"], "打开内置流程文件夹")
        register(
            f"/{p}/knowledge/tidy",
            self.api_knowledge_tidy,
            ["POST"],
            "现在把今天学到的整理一遍",
        )
        # 知识卡（她查过的事）在编辑器里"看得见"：以前只有 tidy，卡片永远不可见。
        register(f"/{p}/knowledge/cards", self.api_knowledge_cards, ["GET"], "知识卡列表（可搜索 / 分页）")
        register(
            f"/{p}/knowledge/cards/delete",
            self.api_knowledge_card_delete,
            ["POST"],
            "删掉若干张知识卡",
        )
        register(f"/{p}/manual", self.api_manual, ["GET"], "她自己的说明书（可编辑）")
        register(
            f"/{p}/manual/save",
            self.api_manual_save,
            ["POST"],
            "保存说明书",
        )
        register(f"/{p}/plugin-drafts", self.api_plugin_drafts, ["GET"], "她写的插件草稿")
        register(
            f"/{p}/plugin-drafts/detail",
            self.api_plugin_draft_detail,
            ["GET", "POST"],
            "看一份草稿的全文",
        )
        register(
            f"/{p}/plugin-drafts/install",
            self.api_plugin_draft_install,
            ["POST"],
            "把一份草稿装进 AstrBot（人来按）",
        )
        register(
            f"/{p}/plugin-drafts/delete",
            self.api_plugin_draft_delete,
            ["POST"],
            "删掉一份草稿",
        )
        register(f"/{p}/world/change/preview", self.api_world_change_preview, ["POST"], "预览世界扩建")
        register(f"/{p}/world/change/apply", self.api_world_change_apply, ["POST"], "应用世界扩建")
        register(f"/{p}/growth/backup", self.api_growth_backup, ["POST"], "备份数据库与成长资产")
        register(f"/{p}/growth/verify", self.api_growth_verify, ["GET"], "恢复演练：校验快照")
        # 注意：配置改动历史占用了 /history（还有 /history/item 等子路径），
        # 数值历史必须另起一条路径，否则两条会互相顶掉。
        register(f"/{p}/state/history", self.api_state_history, ["GET"], "数值历史")
        register(f"/{p}/defaults", self.api_defaults, ["GET"], "内置默认文案")
        register(f"/{p}/backup", self.api_backup, ["POST"], "备份配置")
        register(f"/{p}/presets", self.api_presets, ["GET"], "预设列表")
        register(f"/{p}/presets/save", self.api_preset_save, ["POST"], "把当前配置存成预设")
        register(f"/{p}/presets/new-default", self.api_preset_new_default, ["POST"], "用内置默认世界新建预设")
        register(f"/{p}/presets/apply", self.api_preset_apply, ["POST"], "应用预设")
        register(f"/{p}/presets/delete", self.api_preset_delete, ["POST"], "删除预设")
        register(f"/{p}/presets/rename", self.api_preset_rename, ["POST"], "重命名预设")
        register(f"/{p}/presets/json", self.api_preset_json, ["GET", "POST"], "读写预设 JSON")
        register(f"/{p}/history", self.api_history, ["GET"], "配置改动历史")
        register(f"/{p}/history/item", self.api_history_item, ["GET"], "看一条历史")
        register(f"/{p}/history/restore", self.api_history_restore, ["POST"], "恢复到这个版本")
        register(f"/{p}/history/delete", self.api_history_delete, ["POST"], "删除一条历史")
        register(f"/{p}/history/clear", self.api_history_clear, ["POST"], "清空历史")
        register(f"/{p}/events", self.api_events, ["GET"], "事件线索与能力值")
        register(f"/{p}/persona-brief", self.api_persona_brief, ["GET", "POST"], "简易人设")
        register(f"/{p}/voice-samples", self.api_voice_samples, ["GET"], "声音样例")
        register(
            f"/{p}/voice-samples/generate",
            self.api_voice_samples_generate,
            ["POST"],
            "生成声音样例候选",
        )
        register(
            f"/{p}/voice-samples/from-chat",
            self.api_voice_samples_from_chat,
            ["POST"],
            "从近期聊天里挑样例候选",
        )
        register(
            f"/{p}/voice-samples/save",
            self.api_voice_samples_save,
            ["POST"],
            "保存挑中的声音样例",
        )
        register(f"/{p}/persona/review", self.api_persona_review, ["POST"], "体检人设")
        register(
            f"/{p}/persona/apply", self.api_persona_apply, ["POST"], "应用挑中的改动"
        )
        register(
            f"/{p}/eval-script", self.api_eval_script, ["POST"], "生成测评剧本"
        )
        register(f"/{p}/eval-run", self.api_eval_run, ["POST"], "跑测评（并发）")
        register(f"/{p}/persona-source", self.api_persona_source, ["GET"], "读 AstrBot 当前给这个会话的人设")
        register(f"/{p}/profile/people", self.api_profile_people, ["GET"], "通讯录：认识的人")
        register(f"/{p}/avatars", self.api_avatars, ["POST"], "QQ 头像（通讯录用）")
        register(f"/{p}/profile/person", self.api_profile_person, ["GET", "POST"], "通讯录：一个人的画像")
        register(f"/{p}/profile/fact", self.api_profile_fact, ["POST"], "通讯录：改事实")
        register(f"/{p}/profile/bond", self.api_profile_bond, ["POST"], "通讯录：改关系")
        register(f"/{p}/profile/affinity", self.api_profile_affinity, ["POST"], "通讯录：改好感度")
        register(
            f"/{p}/profile/grudge",
            self.api_profile_grudge,
            ["POST"],
            "通讯录：她记着的一笔账",
        )
        register(f"/{p}/profile/forget", self.api_profile_forget, ["POST"], "通讯录：忘掉一个人")
        register(
            f"/{p}/profile/forget-all",
            self.api_profile_forget_all,
            ["POST"],
            "通讯录：清空（忘掉全部）",
        )
        register(f"/{p}/profile/consolidate", self.api_profile_consolidate, ["POST"], "立刻整理一次（记忆 + 画像）")
        register(f"/{p}/extensions", self.api_extensions, ["GET", "POST"], "扩展包：设置与数据")
        register(f"/{p}/topics", self.api_topics, ["GET"], "谈资列表")
        register(f"/{p}/topics/item", self.api_topic_detail, ["GET"], "一条谈资")
        register(f"/{p}/topics/create", self.api_topic_create, ["POST"], "手工加一条谈资")
        register(f"/{p}/topics/update", self.api_topic_update, ["POST"], "改一条谈资")
        register(f"/{p}/topics/delete", self.api_topic_delete, ["POST"], "删一条谈资（软删）")
        register(f"/{p}/topics/shares", self.api_topic_shares, ["GET"], "这条谈资说过没有")
        register(f"/{p}/topics/recall", self.api_topic_recall, ["GET"], "最近几次谈资召回")
        register(f"/{p}/experience", self.api_experience, ["GET"], "一条来源事件的原始记录")
        register(f"/{p}/media", self.api_media, ["GET"], "本机图片资产库存")
        register(f"/{p}/media/gc", self.api_media_gc, ["POST"], "清理图片资产（可先预览）")
        # 存储自检（用户 2026-10-24）：后端一直有 storage_health，但页面上看不到磁盘。
        register(f"/{p}/storage", self.api_storage, ["GET"], "存储自检：磁盘 / 库文件 / 快照 / 回收")
        register(
            f"/{p}/storage/maintenance",
            self.api_storage_maintenance,
            ["POST"],
            "立刻跑一次存储维护（不吃每日节流）",
        )
        # M1 包：表情包收藏的管理页（读多写少；写总开关不在这里，走 /config/world）
        register(f"/{p}/meme/list", self.api_meme_list, ["GET"], "表情包收藏列表")
        register(f"/{p}/meme/detail", self.api_meme_detail, ["GET"], "一条表情包收藏的详情")
        register(f"/{p}/meme/preview", self.api_meme_preview, ["GET"], "表情包缩略图（首帧）")
        register(f"/{p}/meme/edit", self.api_meme_edit, ["POST"], "改一条收藏的语义与备注")
        register(f"/{p}/meme/describe", self.api_meme_describe, ["POST"], "给没写描述的收藏批量补一句")
        register(f"/{p}/meme/toggle", self.api_meme_toggle, ["POST"], "禁用 / 启用一条收藏")
        register(f"/{p}/meme/delete", self.api_meme_delete, ["POST"], "删收藏记录（可只删记录）")
        register(f"/{p}/meme/cleanup", self.api_meme_cleanup, ["POST"], "清理过期展示记录与孤儿资产")
        register(f"/{p}/meme/backfill", self.api_meme_backfill, ["POST"], "补齐图片的宽高与帧数（默认预览）")
        register(f"/{p}/meme/upload", self.api_meme_upload, ["POST"], "用户主动上传一张表情包")
        register(f"/{p}/meme/import", self.api_meme_import, ["POST"], "把一个目录里的图片整批收进收藏")
        register(f"/{p}/meme/export", self.api_meme_export, ["GET"], "把选中的表情包导出成压缩包")
        register(f"/{p}/meme/import-pack", self.api_meme_import_pack, ["POST"], "把导出的压缩包导回来")
        register(f"/{p}/meme/export/packs", self.api_meme_export_packs, ["GET"], "列出 data/exports 里现成的压缩包")
        register(f"/{p}/meme/import-pack/file", self.api_meme_import_pack_file, ["POST"], "从 data/exports 里直接导一个压缩包")
        # Agent 页：她调用过哪些工具的只读账（只认 tool_receipt），不写库、不新增表
        register(f"/{p}/agent/runs", self.api_agent_runs, ["GET"], "她调用过哪些工具")
        # B0/WP4：embedding 只读状态（只有 GET，没有任何写操作，也不回密钥）
        register(f"/{p}/embedding/providers", self.api_embedding_providers, ["GET"], "可选的向量模型")
        register(
            f"/{p}/embedding/provider",
            self.api_embedding_select,
            ["POST"],
            "选一个向量模型（写插件配置并立即重新装配）",
        )
        register(f"/{p}/embedding/status", self.api_embedding_status, ["GET"], "语义检索与索引状态")
        # B1/WP4：两个写操作——清索引、重建索引。前者只清索引队列 + 该 scope 的向量行，
        # 后者把"缺向量"的老谈资重新入队；两个都绝不动谈资本体与来源事件。
        register(
            f"/{p}/embedding/index/clear",
            self.api_embedding_index_clear,
            ["POST"],
            "清空语义索引（不动谈资本体）",
        )
        register(
            f"/{p}/embedding/index/rescan",
            self.api_embedding_index_rescan,
            ["POST"],
            "重建语义索引（把缺向量的老谈资渐进入队）",
        )
        register(
            f"/{p}/embedding/memory/rebuild",
            self.api_embedding_memory_rebuild,
            ["POST"],
            "重建记忆向量缓存（清掉旧空间的向量，并起一次预热）",
        )
        register(
            f"/{p}/embedding/memory/warm",
            self.api_embedding_memory_warm,
            ["GET"],
            "记忆向量预热进度",
        )

    # ---------------- 谈资（阶段C 页面接口）----------------

    def _topic_error(self, exc: Exception) -> Any:
        """把谈资层的异常翻成 HTTP：冲突 409、找不到 404、其它 400。"""

        if isinstance(exc, TopicVersionConflict):
            return error_response(str(exc), status_code=409)
        if isinstance(exc, TopicNotFound):
            return error_response(str(exc), status_code=404)
        if isinstance(exc, ExperienceError):
            return error_response(str(exc), status_code=400)
        return error_response(f"谈资操作失败：{exc}")

    async def api_topics(self):
        query = request.query
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(query.get("session", ""))
        limit = _positive_int(query.get("limit"), 20)
        try:
            payload = await self.engine.topics_page(
                session_id=session_id,
                status=str(query.get("status", "") or ""),
                source=str(query.get("source", "") or ""),
                cursor=str(query.get("cursor", "") or ""),
                limit=limit,
            )
        except ExperienceError as exc:
            return self._topic_error(exc)
        payload["session"] = session_id
        return json_response(payload)

    async def api_topic_recall(self):
        """最近几次谈资召回的轨迹（只读）：编辑器「最近召回」拿它给管理员看。

        读不到、功能关着、还没记录都不算错，一律 ``ok=False`` + HTTP 200，绝不 500。
        """

        query = request.query
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(query.get("session", ""))
        limit = max(1, min(20, _positive_int(query.get("limit"), 5)))
        try:
            if not self.engine.topics_enabled():
                return json_response({"ok": False, "reason": "谈资功能没开", "session": session_id})
            if not session_id:
                return json_response({"ok": False, "reason": "要指定是哪个会话", "session": session_id})
            stored = await self.db.call("kv_get", f"topic-recall:{session_id}")
            records = stored.get("records") if isinstance(stored, dict) else None
            rows = [row for row in records if isinstance(row, dict)][:limit] if isinstance(records, list) else []
            if not rows:
                return json_response({"ok": False, "reason": "还没有召回记录", "session": session_id})
            return json_response({"ok": True, "session": session_id, "records": rows, "count": len(rows)})
        except Exception as exc:
            return json_response({"ok": False, "reason": f"读不到召回记录：{exc}", "session": session_id})

    async def api_topic_detail(self):
        query = request.query
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(query.get("session", ""))
        topic_id = str(query.get("topic", "") or "")
        if not topic_id:
            return error_response("要指定是哪一条谈资", status_code=400)
        try:
            payload = await self.engine.topic_detail(
                session_id=session_id, topic_id=topic_id
            )
        except (ExperienceError, TopicNotFound, TopicVersionConflict) as exc:
            return self._topic_error(exc)
        payload["session"] = session_id
        return json_response(payload)

    async def api_topic_create(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session", ""))
        summary = str(payload.get("summary", "") or "").strip()
        if not summary:
            return error_response("写点什么再存", status_code=400)
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        try:
            topic = await self.engine.create_manual_topic(
                session_id=session_id,
                summary=summary,
                details=details,
                importance=_positive_float(payload.get("importance"), 0.0),
                pinned=bool(payload.get("pinned")),
            )
        except (ExperienceError, TopicNotFound, TopicVersionConflict) as exc:
            return self._topic_error(exc)
        return json_response({"ok": True, "topic": self.engine.topic_row(topic)})

    async def api_topic_update(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session", ""))
        topic_id = str(payload.get("topic", "") or "")
        if not topic_id:
            return error_response("要指定是哪一条谈资", status_code=400)
        version = _positive_int(payload.get("version"), 0)
        if version <= 0:
            # 计划 §7 C6：改/删必须带 version，否则"覆盖别人刚改过的版本"没法防
            return error_response("要带上这条谈资的版本号（version）", status_code=400)
        fields: dict[str, Any] = {}
        if "summary" in payload:
            fields["summary"] = str(payload.get("summary", "") or "")
        if isinstance(payload.get("details"), dict):
            fields["details"] = dict(payload["details"])
        if "status" in payload:
            fields["status"] = str(payload.get("status", "") or "")
        if "pinned" in payload:
            fields["pinned"] = bool(payload.get("pinned"))
        if "importance" in payload:
            fields["importance"] = _positive_float(payload.get("importance"), 0.0)
        try:
            topic = await self.engine.update_topic(
                session_id=session_id,
                topic_id=topic_id,
                version=version,
                **fields,
            )
        except (ExperienceError, TopicNotFound, TopicVersionConflict) as exc:
            return self._topic_error(exc)
        return json_response({"ok": True, "topic": self.engine.topic_row(topic)})

    async def api_topic_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session", ""))
        topic_id = str(payload.get("topic", "") or "")
        if not topic_id:
            return error_response("要指定是哪一条谈资", status_code=400)
        version = _positive_int(payload.get("version"), 0)
        if version <= 0:
            return error_response("要带上这条谈资的版本号（version）", status_code=400)
        try:
            topic = await self.engine.delete_topic(
                session_id=session_id,
                topic_id=topic_id,
                version=version,
            )
        except (ExperienceError, TopicNotFound, TopicVersionConflict) as exc:
            return self._topic_error(exc)
        return json_response({"ok": True, "topic": self.engine.topic_row(topic)})

    async def api_topic_shares(self):
        query = request.query
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(query.get("session", ""))
        topic_id = str(query.get("topic", "") or "")
        if not topic_id:
            return error_response("要指定是哪一条谈资", status_code=400)
        try:
            shares = await self.engine.topic_shares(
                session_id=session_id, topic_id=topic_id
            )
        except (ExperienceError, TopicNotFound, TopicVersionConflict) as exc:
            return self._topic_error(exc)
        return json_response({"shares": shares})

    async def api_experience(self):
        query = request.query
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(query.get("session", ""))
        source_id = str(query.get("id", "") or "")
        if not source_id:
            return error_response("要指定是哪一条来源", status_code=400)
        try:
            payload = await self.engine.experience_detail(
                session_id=session_id, source_id=source_id
            )
        except (ExperienceError, TopicNotFound, TopicVersionConflict) as exc:
            return self._topic_error(exc)
        payload["session"] = session_id
        return json_response(payload)

    # ---------------- 本机图片资产库（阶段B4）----------------

    async def api_media(self):
        """资产库现状：多少件、占多少、预算多少（编辑器「媒体」一节用）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(self.engine.media_stats())

    async def api_media_gc(self):
        """清理资产。默认先给"会删掉什么"（dry_run），带 ``confirm`` 才真删。"""

        guard = self._guard()
        if guard is not None:
            return guard
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            body = {}
        report = self.engine.media_gc(dry_run=not bool(body.get("confirm")))
        report["ok"] = True
        return json_response(report)

    # ---------------- 存储自检（用户 2026-10-24：「磁盘到底还剩多少」）----------------
    #
    # 后端这一套（core/storage_health.py + engine.storage_report / run_storage_maintenance）
    # 早就写好了，但只有 `/vw status` 能看到，编辑器里完全没有入口。这两个接口只做
    # "把它摊开成 JSON"：**口径与 `/vw status` 完全一致**（同一个 storage_report、同一套
    # 阈值），不在这里另算一份，否则两处数字对不上又没人说得清哪个对。
    #
    # 一律 HTTP 200：失败用 ``{"ok": False, "error": "中文原因"}``，前端不用看状态码。

    _STORAGE_MIB = 1024 * 1024

    def _storage_payload(self) -> dict[str, Any]:
        """跑一次自检并压成前端能直接用的 JSON（读不到的部分给 ``null``，不抛）。"""

        engine = self.engine
        report = engine.storage_report(force=True)
        min_free, max_data, _top_files = engine._storage_limits()
        free_bytes = report.free_bytes
        data_bytes = int(report.total_bytes or 0)
        failure = getattr(engine, "last_write_error", None)

        try:
            raw = getattr(self.db, "raw", None)
            freelist = raw.freelist_snapshot() if raw is not None else {}
            freelist_pages = int(freelist.get("freelist") or 0)
        except Exception:  # noqa: BLE001 - 量不到就给 0，不能让整份报告失败
            freelist_pages = 0
        try:
            vacuum_last = dict(getattr(engine, "_vacuum_last", None) or {})
        except (TypeError, ValueError):
            vacuum_last = {}

        records = list(engine._snapshot_cleanup_log().records)
        latest = records[0] if records else None
        maintained_at = float(getattr(engine, "_storage_maintain_at", 0.0) or 0.0)
        vacuum_at = float(vacuum_last.get("at") or 0.0)

        return {
            "ok": True,
            "free_bytes": free_bytes,
            "free_mb": None if free_bytes is None else round(free_bytes / self._STORAGE_MIB, 1),
            "data_bytes": data_bytes,
            "data_mb": round(data_bytes / self._STORAGE_MIB, 1),
            "db_bytes": int(report.db_bytes or 0),
            "wal_bytes": int(report.wal_bytes or 0),
            "shm_bytes": int(report.shm_bytes or 0),
            "top_files": [
                {"name": os.path.basename(str(item.path)), "bytes": int(item.size or 0)}
                for item in report.top_files
            ],
            "last_write_error": None if failure is None else str(getattr(failure, "error", "") or ""),
            "last_write_error_at": (
                None if failure is None else float(getattr(failure, "at", 0.0) or 0.0)
            ),
            "last_maintenance_at": maintained_at or None,
            "snapshot": {
                "deleted": int(getattr(latest, "deleted", 0) or 0),
                "freed_bytes": int(getattr(latest, "freed_bytes", 0) or 0),
                "keep": int(engine.snapshot_cleanup_keep()),
                "last_at": None if latest is None else float(getattr(latest, "at", 0.0) or 0.0),
            },
            "low_space": bool(
                report.low_space(min_free_bytes=min_free, max_data_bytes=max_data)
            ),
            "over_limit": bool(max_data > 0 and data_bytes > max_data),
            "vacuum": {
                "enabled": bool(engine.vacuum_enabled()),
                "mode": engine.vacuum_mode(),
                "freelist_pages": freelist_pages,
                "last_run_at": vacuum_at or None,
            },
            "report_lines": [str(line) for line in engine.storage_status_lines()],
        }

    async def api_storage(self):
        """存储自检（只读）：磁盘可用 / 数据目录 / 库文件 / TOP N / 旧快照 / 空闲页。"""

        guard = self._guard()
        if guard is not None:
            return guard
        try:
            payload = await asyncio.to_thread(self._storage_payload)
        except Exception as exc:  # noqa: BLE001 - 自检坏了也必须 200，前端好处理
            return json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json_response(payload)

    async def api_storage_maintenance(self):
        """立刻跑一次存储维护（``force``：用户手动点的，同一天也要真跑）。"""

        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            body = {}
        guard = self._guard(body)
        if guard is not None:
            return guard
        try:
            removed = await self.engine.run_storage_maintenance(force=True)
        except Exception as exc:  # noqa: BLE001 - 维护失败是"这次没清"，不是 500
            return json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        try:
            payload = await asyncio.to_thread(self._storage_payload)
        except Exception as exc:  # noqa: BLE001
            return json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        payload["removed"] = {
            str(key): int(value or 0) for key, value in (removed or {}).items()
        }
        return json_response(payload)

    # ---------------- 表情包收藏的管理页（阶段M1）----------------
    #
    # 这一节只做"看"与"整理"：总开关（``world.meme.enabled``）的写入走已有的
    # ``/config/world``，不在这里再开一个开关接口。所有接口一律 HTTP 200，
    # 失败用 ``{"ok": False, "reason": "中文原因"}``，绝不 500。

    def _meme_store(self) -> Any:
        """收藏记录层（懒建；世界配置换了就换一份快照）。

        这里交出去的是**同步库** ``self.db.raw``：``MemeStore`` 的接口是同步的
        （它同时被 M2 的回复路径复用，不适合改成 async），而运行时 ``self.db``
        是 ``AsyncDatabase`` —— 那上面只有 ``call/run/transaction/raw``，把它
        直接交过去，``entry_views`` 这类调用在真机上一律 ``AttributeError``
        （单元测试里手工构造的是同步 ``Database``，所以看不出这个错）。
        处理器里每一次碰库都统一用 ``asyncio.to_thread`` 挪出事件循环。
        """

        store = getattr(self, "_meme_store_cache", None)
        world = getattr(self.engine, "world", None)
        if store is None or getattr(self, "_meme_store_world", None) is not world:
            store = MemeStore(
                self.db.raw, world=world, media_store=self.engine.media_store()
            )
            self._meme_store_cache = store
            self._meme_store_world = world
        return store

    def _meme_thumb_cache(self) -> Any:
        """缩略图缓存（``{(media_id, 像素): (bytes, mime)}``，LRU，按实例存一份）。"""

        cache = getattr(self, "_meme_thumbs", None)
        if cache is None:
            from collections import OrderedDict  # noqa: PLC0415

            cache = OrderedDict()
            self._meme_thumbs = cache
        return cache

    def _meme_error(self, exc: BaseException, what: str = "表情包操作失败") -> Any:
        return json_response({"ok": False, "reason": f"{what}：{exc}"})

    @staticmethod
    def _meme_size_text(value: Any) -> str:
        """字节数 → 人话（页面提示用；后端没有现成的，这里只写一份）。"""

        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        for unit in ("B", "KB", "MB", "GB"):
            if number < 1024 or unit == "GB":
                if unit == "B":
                    return f"{int(number)} B"
                return f"{number:.1f} {unit}"
            number /= 1024.0
        return f"{number:.1f} GB"

    @staticmethod
    def _meme_edit_fields(body: Any) -> tuple[dict[str, Any], Any]:
        """挑出允许编辑的字段（白名单，不认 ``__class__`` 这类花招）。"""

        from .core.db import MEME_ENTRY_EDITABLE_COLUMNS

        allowed = set(MEME_ENTRY_EDITABLE_COLUMNS)
        fields: dict[str, Any] = {}
        for key, value in (body or {}).items():
            name = str(key)
            if name not in allowed:
                continue
            if name == "pinned":
                fields[name] = bool(value)
            elif name == "preference":
                try:
                    fields[name] = max(0.0, min(100.0, float(value)))
                except (TypeError, ValueError):
                    return {}, error_response("偏好得是数字", status_code=400)
            else:
                fields[name] = str(value if value is not None else "")
        if not fields:
            return {}, error_response("没有要修改的字段", status_code=400)
        return fields, None

    def _meme_export_dir(self) -> Path:
        """导出目录：``<插件数据目录>/exports``（没有就顺手建上）。"""

        root = Path(str(self.store.data_dir)) / _meme_pack.EXPORTS_DIR
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def _meme_export_payload(self, wanted: set[str]) -> dict[str, Any]:
        """把选中的收藏打成压缩包，返回给页面的载荷（不含 Response）。

        包落在 ``<插件数据目录>/exports/`` 下（用户 m07533：与其搬几十 MB 的
        base64 过页面桥，不如让用户直接去宿主里取）。这里只回文件名、大小与路径，
        外加一句"写到哪儿了"，页面照抄给用户。
        """

        # 以前只取最近 500 条：收藏多了以后"全部导出"其实只导了最近 500 张
        # （用户 m19069 问上限）。现在一页页翻到底（最多 5000 条兜底内存）。
        views: list[dict[str, Any]] = []
        offset = 0
        while offset < MEME_EXPORT_SCAN_CAP:
            page = await asyncio.to_thread(
                self._meme_store().entry_views,
                limit=MEME_DESCRIBE_SCAN_PAGE,
                offset=offset,
                include_deleted=False,
            )
            if not page:
                break
            views.extend(page)
            if len(page) < MEME_DESCRIBE_SCAN_PAGE:
                break
            offset += MEME_DESCRIBE_SCAN_PAGE
        if wanted:
            matched = [
                item for item in views if str(item.get("entry_id") or "") in wanted
            ]
            # 选中的 id 一条都不在库里：页面上的勾选集过期了（删掉的那条还留着）。
            # 这跟"图不在本机"是两回事，得分开说，不然用户去翻资产库白找一趟。
            if not matched:
                return {"ok": False, "reason": "选中的这几条已经不在库里了"}
            views = matched
        rows: list[dict[str, Any]] = []
        for view in views:
            if str(view.get("state") or "") != STATE_ENABLED:
                continue
            media_id = str(view.get("media_id") or "")
            source = self.engine.media_file_source(media_id) if media_id else ""
            if not source or not os.path.isfile(source):
                continue
            try:
                with open(source, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            if data:
                rows.append({**dict(view), "data": data})
        if not rows:
            # 「一条都没收藏」和「有收藏但文件都不在盘」要分开说。原先两种情况共用
            # 一句"图都不在本机了"，真机上库里明明有图、文件也在盘时也这么提示，
            # 用户只能一头雾水（这条就是被那个提示带偏的）。
            if views:
                return {"ok": False, "reason": "表情包的文件都不在本机了"}
            return {"ok": False, "reason": "还没有收藏过表情包"}
        filename, blob, count = await asyncio.to_thread(_meme_pack.pack, rows)
        target = await asyncio.to_thread(
            _meme_pack.write_pack, self._meme_export_dir(), blob, filename
        )
        return {
            "ok": True,
            "filename": target.name,
            "count": count,
            "size": len(blob),
            # 相对插件数据目录的路径（页面直接显示这个，用户在宿主里找得到）
            "relative": f"{_meme_pack.EXPORTS_DIR}/{target.name}",
            "path": str(target),
        }

    async def api_meme_export(self):
        """把选中的收藏（没选就是全部）打成一个压缩包：图片 + 描述 json。

        用户 m14731：想选几张导出来分享给别人，别人能直接导回去。压缩包里
        ``manifest.json`` 带名字/描述/用法/校验和，图片按内容哈希命名，不会互相覆盖。
        返回 base64（而不是二进制流）：前端下载和"存到工作区再传给别人"共用一份 JSON。
        用户 m07533：包直接写到 ``<插件数据目录>/exports/`` 下，页面只回路径，
        不再把 base64 搬过桥（几十 MB 的响应会把那条桥弄断）。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        wanted = {
            item.strip()
            for item in str(request.query.get("ids", "") or "").split(",")
            if item.strip()
        }
        try:
            payload = await self._meme_export_payload(wanted)
        except Exception as exc:  # noqa: BLE001
            # 以前这里直接冒泡成 500，页面只拿到一句「Internal Server Error」，
            # 用户看到的永远是"导出失败"而不知道为什么。现在把原因带回去。
            return json_response(
                {"ok": False, "reason": f"导出失败：{type(exc).__name__}: {exc}"}
            )
        return json_response(payload)

    def _meme_exports_dir(self) -> "Path":
        """``<data>/exports/``：导出的压缩包落在这里，导入也先从这儿找。"""

        return Path(self.store.data_dir) / _meme_pack.EXPORTS_DIR

    async def api_meme_export_packs(self):
        """列出 ``data/exports/`` 里现成的压缩包（用户 m07774：导入别再走页面桥）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        try:
            root = self._meme_exports_dir()
            packs = await asyncio.to_thread(_meme_pack.list_packs, root)
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "读不出导出目录")
        return json_response(
            {
                "ok": True,
                "relative": _meme_pack.EXPORTS_DIR,
                "packs": packs,
            }
        )

    async def api_meme_import_pack_file(self):
        """从 ``data/exports/`` 里直接读一个压缩包导进来。

        客户端只传文件名：几十 MB 的包不再是 base64 过页面桥，读盘最稳。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        name = str(payload.get("name") or "").strip()
        on_conflict = "skip" if str(payload.get("on_conflict") or "") == "skip" else "rename"
        try:
            path = _meme_pack.resolve_pack(self._meme_exports_dir(), name)
        except ValueError as exc:
            return json_response({"ok": False, "reason": str(exc)})
        try:
            blob = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            return self._meme_error(exc, "读不出这个压缩包")
        return await self._meme_import_blob(blob, on_conflict)

    async def _meme_import_blob(self, blob: bytes, on_conflict: str):
        """导入一个压缩包的字节（文件选择与导出目录列表共用这一条路）。"""

        try:
            manifest, files = await asyncio.to_thread(_meme_pack.unpack, blob)
        except Exception as exc:  # noqa: BLE001
            text = str(exc or "")
            # 截断的包最常见：浏览器下载/页面桥半路断了，zip 的目录就读不出来
            if "BadZipFile" in type(exc).__name__ or "not a zip" in text.lower() or "central directory" in text.lower():
                return json_response(
                    {
                        "ok": False,
                        "reason": "压缩包不完整（多半是下载或传输时被截断了）：重新导出一次，"
                        "或者把 data/exports 里的那个包直接拿过来导",
                    }
                )
            return json_response({"ok": False, "reason": f"这个压缩包不认识：{exc}"})
        return await self._meme_import_entries(manifest, files, on_conflict)

    async def _meme_import_entries(
        self, manifest: dict, files: dict, on_conflict: str = "rename"
    ):
        """把解出来的清单一条条收进收藏（用户 m08043：导入成功必须给出结果）。

        之前这条成功路径**整个漏了**——函数在 `try` 之后直接结束，正常包会返回
        `None`，页面拿到的就是 `data is null`（用户反复看到的
        `can't access property "ok", data is null`），只有坏包才有 JSON。
        """

        rows = [item for item in (manifest or {}).get("entries") or [] if isinstance(item, dict)]
        if not rows:
            return json_response({"ok": False, "reason": "这个包里没有收藏记录（manifest 是空的）"})
        store = self._meme_store()
        media = self.engine.media_store()
        now = time.time()
        imported = 0
        duplicates = 0
        missing: list[str] = []
        failed: list[str] = []
        for index, row in enumerate(rows):
            path = str(row.get("file") or "").strip()
            blob = files.get(path)
            if not blob:
                missing.append(path or f"第 {index + 1} 条")
                continue
            name = " ".join(str(row.get("name") or "").split())[:40] or "导入的表情"
            try:
                encoded = base64.b64encode(blob).decode("ascii")
                media_id = await self.engine.cache_media(f"base64://{encoded}")
                if not media_id:
                    failed.append(name)
                    continue
                result = await asyncio.to_thread(
                    store.collect_image,
                    media_id=media_id,
                    scope_id="*",
                    name=name,
                    meaning=str(row.get("meaning") or ""),
                    use_when=str(row.get("use_when") or ""),
                    avoid_when=str(row.get("avoid_when") or ""),
                    personal_note=str(row.get("note") or ""),
                    source={
                        "origin": "pack",
                        "origin_kind": "import",
                        "occurred_at": now,
                    },
                    asset=asset_facts(media, media_id),
                    now=now,
                )
            except Exception:  # noqa: BLE001 - 单条坏了不该让整包失败
                failed.append(name)
                continue
            if result.get("created"):
                imported += 1
            elif result.get("ok"):
                duplicates += 1
            else:
                failed.append(name)
        data: dict[str, Any] = {
            "ok": True,
            "imported": imported,
            "duplicates": duplicates,
            "missing": missing[:20],
            "failed": failed[:20],
            "total": len(rows),
        }
        parts = [f"导入 {imported} 张"]
        if duplicates:
            parts.append(f"{duplicates} 张之前就有")
        if missing:
            parts.append(f"{len(missing)} 张包内缺图")
        if failed:
            parts.append(f"{len(failed)} 张没导进来")
        data["note"] = "，".join(parts)
        return json_response(data)

    async def api_meme_import_pack(self):
        """把导出的压缩包收回来（用户 m14731：要考虑同名和冲突）。

        冲突处理：同一张图（内容哈希一样）已在库里 → 跳过；名字撞了 → 改成
        「名字（2）」，除非 ``on_conflict`` 写 ``skip``。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        data = str(payload.get("data") or "").strip()
        if not data:
            return error_response("要选一个导出的压缩包", status_code=400)
        on_conflict = "skip" if str(payload.get("on_conflict") or "") == "skip" else "rename"
        try:
            blob = base64.b64decode(data.split(",")[-1])
        except Exception:  # noqa: BLE001
            return error_response("压缩包读不出来（不是有效的 base64）", status_code=400)
        return await self._meme_import_blob(blob, on_conflict)

    async def api_meme_list(self):
        """收藏列表 + 顶部统计（总开关关着也照常给，页面要能看与清理）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        query = request.query
        status = str(query.get("status", "") or "").strip()
        scope_id = str(query.get("scope", "") or "").strip()
        try:
            store = self._meme_store()
            # 一页 30 条（用户 m18846/m18961）：页面上「加载更多」翻下一页，
            # 不再一次把 200 条塞给浏览器。上限仍按 200 兜住。
            limit = min(200, _positive_int(query.get("limit"), 30))
            # 翻页的 offset 允许是 0（``_positive_int`` 会把 0 当垃圾），所以单独收。
            try:
                offset = max(0, int(str(query.get("offset") or "0").strip() or "0"))
            except (TypeError, ValueError):
                offset = 0
            # 排序（用户 m21273）：按收藏时间 / 已用次数，正序倒序都能切；默认收藏时间倒序。
            order = str(query.get("order", "") or "").strip().lower()
            if order not in ("collected", "used", "updated"):
                order = "collected"
            desc_raw = str(query.get("desc", "") or "").strip().lower()
            desc = desc_raw not in ("0", "false", "asc", "no")
            rows = await asyncio.to_thread(
                store.entry_views,
                scope_id=scope_id or None,
                status=status or None,
                include_deleted=True,
                limit=limit,
                offset=offset,
                order=order,
                desc=desc,
            )
            summary = await asyncio.to_thread(store.summary, scope_id=scope_id or None)
        except Exception as exc:  # noqa: BLE001 - 统一翻成中文原因
            return self._meme_error(exc, "读不到表情包收藏")
        try:
            total = int((summary or {}).get("count") or 0)
        except (TypeError, ValueError):
            total = 0
        payload = {
            "ok": True,
            "enabled": bool(store.settings.enabled),
            "global_reason": (
                "总开关关着，聊天里不会出现表情包动作。"
                if not store.settings.enabled
                else "总开关开着，表情包动作可用。"
            ),
            "count": len(rows),
            # 分页信息（用户 m18961）：页面按 offset+limit 一页一页往上翻
            "limit": limit,
            "offset": offset,
            "order": order,
            "desc": desc,
            "total": total or len(rows),
            "has_more": bool(total and offset + len(rows) < total),
            "entries": rows,
            "summary": summary,
            "settings": store.settings.values,
            "settings_sources": dict(store.settings.sources),
        }
        return json_response(payload)

    async def api_meme_detail(self):
        """一条收藏的完整样子（语义、来源、变体、最近用法、可用状态）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        entry_id = str(request.query.get("id", "") or "").strip()
        if not entry_id:
            return json_response({"ok": False, "reason": "要给一个收藏编号"})
        try:
            store = self._meme_store()
            view = await asyncio.to_thread(store.entry_view, entry_id)
            if view is None:
                return json_response(
                    {"ok": False, "reason": f"没有这个收藏：{entry_id}", "id": entry_id}
                )
            view["ok"] = True
            view["id"] = entry_id
            # 编号用行里存的那个（``format_meme_id`` 收的是序号，不是编号字符串）。
            view["display_id"] = str(
                (view.get("entry") or {}).get("entry_id") or entry_id
            )
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "读不到这条收藏")
        return json_response(view)

    async def api_meme_preview(self):
        """缩略图：给管理页卡片直接显示用。

        用户裁定（m20「图片不要我点击查看了才能显示」）：卡片要**一进来就有图**。
        所以这里优先用 Pillow（AstrBot 自带，见 requirements.txt）把图缩到
        :data:`MEME_THUMB_MAX_PX` 以内再给 data URL —— 一张 1.7MB 的截图缩完只剩
        几十 KB，一次列二十几张也不卡。GIF 取第一帧（帧动画是 M5 的事）。

        Pillow 不在 / 图片解不开（少见格式、坏文件）时退回老口径：原图字节 +
        ``max_candidate_preview_bytes`` 上限；超限就明确说"太大不显示"，不编内容。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        media_id = str(request.query.get("media", "") or "").strip()
        if not looks_like_media_id(media_id):
            return json_response({"ok": False, "reason": "图片身份不合法"})
        try:
            store = self._meme_store()
            media = self.engine.media_store()
            data = await asyncio.to_thread(media.open_bytes, media_id)
            if not data:
                return json_response(
                    {"ok": False, "reason": "底层图片文件不见了", "missing": True}
                )
            raw = len(data)
            mime = "image/png"
            asset = await asyncio.to_thread(media.asset, media_id)
            if asset is not None and getattr(asset, "mime", ""):
                mime = str(asset.mime)
            # ① 先试缩略图（同样的 media_id 只缩一次，缓存在内存里）
            thumb = await asyncio.to_thread(
                self._meme_thumb_bytes, media_id, data, int(self._meme_thumb_px())
            )
            if thumb is not None:
                thumb_data, thumb_mime = thumb
                return json_response(
                    {
                        "ok": True,
                        "media_id": media_id,
                        "mime": thumb_mime,
                        "size_bytes": raw,
                        "bytes": len(thumb_data),
                        "first_frame_only": mime == "image/gif",
                        "thumbnail": True,
                        "data_url": f"data:{thumb_mime};base64,{base64.b64encode(thumb_data).decode('ascii')}",
                    }
                )
            # ② 缩不了就按老口径：原图字节（受 preview 上限约束）
            cap = int(store.settings.max_candidate_preview_bytes or 0)
            if cap > 0 and raw > cap:
                return json_response(
                    {
                        "ok": False,
                        "reason": (
                            "这张图太大，页面里先不显示"
                            f"（{self._meme_size_text(raw)}）"
                        ),
                        "too_large": True,
                        "size_bytes": raw,
                    }
                )
            first_frame = False
            if mime == "image/gif":
                cut = _gif_first_frame(data)
                if cut is not None:
                    data = cut
                    first_frame = True
            return json_response(
                {
                    "ok": True,
                    "media_id": media_id,
                    "mime": mime,
                    "size_bytes": raw,
                    "bytes": len(data),
                    "first_frame_only": first_frame,
                    "thumbnail": False,
                    "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
                }
            )
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "取不到这张图")

    def _meme_thumb_px(self) -> int:
        """缩略图长边像素上限（常量，不给用户配：配错了卡片就糊或白占流量）。"""

        return MEME_THUMB_MAX_PX

    def _meme_thumb_bytes(self, media_id: str, data: bytes, max_px: int) -> Any:
        """把原图缩成长边 ``max_px`` 的缩略图（内存缓存；缩不了返回 ``None``）。

        Pillow 是 AstrBot 自带依赖（本插件 requirements.txt 明说不额外装包），
        但**仍按可选处理**：import 不到、或者是 Pillow 解不开的格式，就返回 ``None``
        让调用方退回"原图字节"那条路，绝不让管理页因为一张怪图整页报错。
        """

        key = (str(media_id), int(max_px))
        cache = self._meme_thumb_cache()
        hit = cache.get(key)
        if hit is not None:
            cache.move_to_end(key)
            return hit
        try:
            from io import BytesIO  # noqa: PLC0415

            from PIL import Image  # noqa: PLC0415
        except Exception:
            return None
        try:
            with Image.open(BytesIO(data)) as image:
                image.seek(0)  # GIF 只取第一帧
                frame = image.convert("RGBA" if image.mode in ("P", "LA") else "RGB")
                frame.thumbnail((int(max_px), int(max_px)))
                buffer = BytesIO()
                if frame.mode == "RGBA":
                    frame.save(buffer, format="PNG", optimize=True)
                    out_mime = "image/png"
                else:
                    frame.save(buffer, format="JPEG", quality=82, optimize=True)
                    out_mime = "image/jpeg"
        except Exception:
            return None
        result = (buffer.getvalue(), out_mime)
        cache[key] = result
        # 缓存别无限长：管理页一次最多也就看几十张
        while len(cache) > MEME_THUMB_CACHE:
            cache.popitem(last=False)
        return result

    async def api_meme_describe(self):
        """给缺「含义 / 什么时候用 / 什么时候不用」的收藏补上（多模态模型看图写）。

        上传接口只写文件名，``meaning``/``use_when``/``avoid_when`` 都是空的；而关键词
        检索、语义索引全靠这几个字段——没描述的表情包等于占了库位却检索不到（用户
        m01267 第 1 条：不可能手工挨个填）。点一下按钮，把这几条按批交给图片转述模型，
        一次写三栏（用户 m18616：只写含义、两栏用途是空的）。

        ``confirm`` 为真才真的调模型（先给一份预览，避免一次点了八十张）。
        看图走 :meth:`AstrBotVision.describe_meme_fields`：它跟聊天转述**不是**同一条
        管道——那条会把结果压成单行 200 字，三栏到那儿就被揉成一团了。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        # 批量补描述是后台任务：页面只轮询进度/停一次（用户 m18541：一百多张不能一次只跑 12 张）。
        if payload.get("status") is True:
            return json_response(
                {"ok": True, "job": self._meme_describe_job_view(self._meme_describe_job())}
            )
        if payload.get("stop") is True:
            job = self._meme_describe_job()
            if job is not None and job.get("status") == "running":
                job["status"] = "cancelling"
            return json_response(
                {"ok": True, "job": self._meme_describe_job_view(job)}
            )
        raw_ids = payload.get("ids") or []
        ids = [str(item).strip() for item in raw_ids if str(item or "").strip()]
        try:
            limit = max(1, min(12, int(payload.get("limit") or 6)))
        except (TypeError, ValueError):
            limit = 6
        force = payload.get("force") is True
        try:
            store = self._meme_store()
            # 以前只读最近 200 条：收藏过了 200 张以后，更早的那些永远补不到
            # （用户 m19069 问"表情包是不是有上限 200"）。现在按页往回翻，
            # 凑够这一批要的量就停；真正到底了（返回不满一页）才停。
            views: list = []
            blanks: list = []
            want = max(limit * 4, 60)
            offset = 0
            while offset < MEME_DESCRIBE_SCAN_CAP:
                page_rows = await asyncio.to_thread(
                    store.entry_views, limit=MEME_DESCRIBE_SCAN_PAGE, offset=offset
                )
                if not page_rows:
                    break
                views.extend(page_rows)
                blanks = _meme_describe_targets(views, ids=ids, force=force)
                if len(blanks) >= want or len(page_rows) < MEME_DESCRIBE_SCAN_PAGE:
                    break
                offset += MEME_DESCRIBE_SCAN_PAGE
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "读不出表情包收藏")
        blanks = _meme_describe_targets(views, ids=ids, force=force)
        targets = (
            blanks if not force else _meme_describe_targets(views, ids=ids, force=True)
        )[:limit]
        if not targets:
            # 「都有描述了」要能被核对（用户 m18371：导入的纯图只有名字，两个按钮却说没东西可补）。
            # 这里把「扫了多少条、其中多少条还没写齐」回给页面，不再只回一句结论。
            # 扫描上限 MEME_DESCRIBE_SCAN_CAP 条，按创建时间倒序：扫满上限说明更早的没看。
            if force:
                reason = f"选中的 {len(ids)} 条里没有能补的（可能文件不在了）"
            elif not views:
                reason = "库里一条收藏都没读到"
            elif blanks:
                reason = f"有 {len(blanks)} 条还没写含义或用法，但都缺本地文件，补不了"
            else:
                reason = f"看了 {len(views)} 条收藏，含义和用法都是齐的"
            return json_response(
                {
                    "ok": True,
                    "total": 0,
                    "updated": 0,
                    "failed": [],
                    "scanned": len(views),
                    "blank": len(blanks),
                    "force": force,
                    "reason": reason,
                }
            )
        if not self.vision.enabled:
            return error_response(
                "先在插件配置里选「图片转述模型」，不然没法看图", status_code=400
            )
        if payload.get("all") is True:
            # 一次补完剩下的（页面上百张时走这条），并发看图、后台跑、可停。
            pool = _meme_describe_targets(
                views, ids=ids, force=payload.get("force") is True
            )
            return json_response(
                {"ok": True, "job": self._meme_describe_batch_start(payload, pool)}
            )
        if payload.get("confirm") is not True:
            return json_response(
                {
                    "ok": True,
                    "preview": True,
                    "total": len(targets),
                    "targets": targets,
                    "scanned": len(views),
                    "blank": len(blanks),
                    "force": force,
                }
            )
        updated = 0
        failed: list[dict[str, str]] = []
        for item in targets:
            entry_id = str(item.get("id") or "")
            source = self.engine.media_file_source(item.get("media_id")) if item.get("media_id") else ""
            if not source:
                failed.append({"id": entry_id, "reason": "找不到这张图的本地副本"})
                continue
            fields = await self.vision.describe_meme_fields(source)
            if not fields.get("meaning"):
                failed.append({"id": entry_id, "reason": "模型没给出可用的描述"})
                continue
            try:
                result = await self.db.call(
                    "update_meme_entry",
                    entry_id,
                    meaning=fields.get("meaning") or "",
                    use_when=fields.get("use_when") or "",
                    avoid_when=fields.get("avoid_when") or "",
                )
            except Exception as exc:  # noqa: BLE001
                failed.append({"id": entry_id, "reason": f"写不回去：{exc}"[:80]})
                continue
            if isinstance(result, dict) and result.get("ok"):
                updated += 1
            else:
                failed.append(
                    {
                        "id": entry_id,
                        "reason": str((result or {}).get("reason") or "写不回去")[:80],
                    }
                )
        return json_response(
            {
                "ok": True,
                "total": len(targets),
                "updated": updated,
                "failed": failed,
                "reason": "" if updated else "一条都没写成",
            }
        )

    def _meme_describe_job(self) -> dict[str, Any] | None:
        """当前这轮「批量补描述」任务（一次只允许一个，重复点是"接着看进度"）。"""

        return getattr(self, "_meme_describe_current", None)

    def _meme_describe_job_view(self, job: dict[str, Any] | None) -> dict[str, Any]:
        """交给页面的那份进度快照（不含 Task、只回最近几条失败原因）。"""

        if not job:
            return {}
        failed = job.get("failed") or []
        return {
            "id": str(job.get("id") or ""),
            "status": str(job.get("status") or ""),
            "total": int(job.get("total") or 0),
            "done": int(job.get("done") or 0),
            "updated": int(job.get("updated") or 0),
            "failed": len(failed),
            "failures": list(failed)[:5],
            "workers": int(job.get("workers") or 0),
            "started_at": float(job.get("started_at") or 0),
            "finished_at": float(job.get("finished_at") or 0),
            "error": str(job.get("error") or ""),
        }

    def _meme_describe_batch_start(
        self, payload: dict[str, Any], targets: list[dict[str, str]]
    ) -> dict[str, Any]:
        """把一批「没写含义」的收藏丢进后台任务，返回进度快照。

        一百多张不能靠页面点十几次「一次 12 张」（用户 m18541）：这里并发跑图，
        页面只轮询进度。一次最多 500 张，``workers`` 1~6（默 3）——转述模型是外部
        服务，并发再高只是排队，反而更容易撞限流。
        """

        running = self._meme_describe_job()
        if running is not None and str(running.get("status") or "") in {
            "running",
            "cancelling",
        }:
            return self._meme_describe_job_view(running)
        queue = list(targets or ())[:500]
        try:
            workers = int(payload.get("workers") or 3)
        except (TypeError, ValueError):
            workers = 3
        workers = max(1, min(6, workers))
        job: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "status": "running",
            "total": len(queue),
            "done": 0,
            "updated": 0,
            "failed": [],
            "workers": workers,
            "started_at": time.time(),
            "finished_at": 0.0,
            "error": "",
        }
        self._meme_describe_current = job
        if not queue:
            job["status"] = "done"
            job["finished_at"] = time.time()
            return self._meme_describe_job_view(job)
        task = asyncio.create_task(self._meme_describe_batch_run(job, queue))
        job["task"] = task
        task.add_done_callback(lambda _task: job.pop("task", None))
        return self._meme_describe_job_view(job)

    async def _meme_describe_batch_run(
        self, job: dict[str, Any], targets: list[dict[str, str]]
    ) -> None:
        """并发看图写描述；每完成一张就更新进度，停止时让在飞的那几张跑完。"""

        workers = int(job.get("workers") or 3)
        sem = asyncio.Semaphore(max(1, workers))
        lock = asyncio.Lock()

        async def one(item: dict[str, str]) -> None:
            async with sem:
                if str(job.get("status") or "") != "running":
                    return
                entry_id = str(item.get("id") or "")
                media_id = str(item.get("media_id") or "")
                caption = ""
                reason = ""
                source = ""
                try:
                    source = self.engine.media_file_source(media_id) if media_id else ""
                except Exception as exc:  # noqa: BLE001
                    reason = f"找不到本地文件：{exc}"[:80]
                if not source and not reason:
                    reason = "找不到这张图的本地副本"
                fields: dict[str, str] = {}
                if source:
                    try:
                        fields = await self.vision.describe_meme_fields(source)
                    except Exception as exc:  # noqa: BLE001
                        reason = f"看图失败：{exc}"[:80]
                    if not fields.get("meaning") and not reason:
                        reason = "模型没给出可用的描述"
                if fields.get("meaning"):
                    try:
                        result = await self.db.call(
                            "update_meme_entry",
                            entry_id,
                            meaning=fields.get("meaning") or "",
                            use_when=fields.get("use_when") or "",
                            avoid_when=fields.get("avoid_when") or "",
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = {"ok": False, "reason": f"写不回去：{exc}"}
                    if isinstance(result, dict) and result.get("ok"):
                        caption = fields.get("meaning") or ""
                    else:
                        reason = str((result or {}).get("reason") or "写不回去")[:80]
                async with lock:
                    job["done"] = int(job.get("done") or 0) + 1
                    if caption:
                        job["updated"] = int(job.get("updated") or 0) + 1
                    else:
                        job.setdefault("failed", []).append(
                            {
                                "id": entry_id,
                                "name": str(item.get("name") or ""),
                                "reason": reason or "没补成",
                            }
                        )

        try:
            await asyncio.gather(*(one(item) for item in targets))
            job["status"] = (
                "cancelled" if str(job.get("status") or "") == "cancelling" else "done"
            )
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = f"{exc}"[:200]
        finally:
            job["finished_at"] = time.time()

    async def api_meme_edit(self):
        """改语义与备注（乐观锁：页面带着 ``revision`` 来，撞了就报冲突）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        entry_id = str(payload.get("id", "") or "").strip()
        if not entry_id:
            return error_response("要给一个收藏编号", status_code=400)
        fields, problem = self._meme_edit_fields(payload.get("fields") or {})
        if problem is not None:
            return problem
        try:
            expected = int(payload.get("revision") or 0)
        except (TypeError, ValueError):
            expected = 0
        try:
            result = await self.db.call(
                "update_meme_entry",
                entry_id,
                expected_revision=expected or None,
                **fields,
            )
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "改不动这条收藏")
        if not result.get("ok"):
            reason = str(result.get("reason") or "")
            if reason == "not_found":
                return json_response(
                    {"ok": False, "reason": f"没有这个收藏：{entry_id}", "id": entry_id}
                )
            if reason == "version_conflict":
                return json_response(
                    {
                        "ok": False,
                        "reason": "这条收藏刚在别处改过，刷新一下再改",
                        "id": entry_id,
                        "revision": result.get("revision"),
                    }
                )
            return json_response({"ok": False, "reason": f"改不动：{reason}", "id": entry_id})
        return json_response(
            {
                "ok": True,
                "id": entry_id,
                "revision": result.get("revision"),
                "changed": bool(result.get("changed")),
                "entry": result.get("entry"),
            }
        )

    async def api_meme_toggle(self):
        """禁用 / 启用一条收藏（只动收藏记录，不碰底层文件）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        entry_id = str(payload.get("id", "") or "").strip()
        if not entry_id:
            return error_response("要给一个收藏编号", status_code=400)
        enabled = payload.get("enabled")
        if enabled is None:
            return error_response("要说清是禁用还是启用", status_code=400)
        status = "enabled" if bool(enabled) else "disabled"
        try:
            result = await self.db.call("set_meme_entry_status", entry_id, status)
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "改不动这条收藏")
        if not result:
            return json_response(
                {"ok": False, "reason": f"没有这个收藏：{entry_id}", "id": entry_id}
            )
        return json_response(
            {
                "ok": True,
                "id": entry_id,
                "enabled": status == "enabled",
                "file_present": await asyncio.to_thread(
                    self._meme_store().file_present, (result or {}).get("media_id")
                ),
            }
        )

    def _spawn_meme_media_discard(self, media_ids: Any) -> None:
        """把"删图片文件"丢到后台线程（用户 m21273：删除不该把页面卡住）。

        用线程而不是 ``asyncio.create_task``：这个动作不碰事件循环，而且页面请求
        返回之后任务还得继续跑完（用 task 的话，某些宿主在请求结束就回收循环）。
        """

        wanted = [str(item).strip() for item in list(media_ids or ()) if str(item or "").strip()]
        if not wanted:
            return
        threads = getattr(self, "_meme_discard_threads", None)
        if threads is None:
            threads = self._meme_discard_threads = []
        thread = threading.Thread(
            target=self._meme_media_discard,
            args=(wanted,),
            name="vw-meme-discard",
            daemon=True,
        )
        threads.append(thread)
        thread.start()

    def _meme_media_discard(self, media_ids: Any) -> int:
        """后台删图：一张一张来，失败只记日志（记录已经删掉了，文件不是关键路径）。"""

        removed = 0
        try:
            store = self._meme_store()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 后台删图跳过：{exc}")
            return 0
        for media_id in list(media_ids or ()):
            try:
                if store.discard_media(media_id):
                    removed += 1
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(f"[virtual_world] 后台删图失败 {media_id}：{exc}")
        return removed

    def flush_meme_deletions(self, timeout: float = 10.0) -> int:
        """等后台删图线程跑完（测试与脚本用；页面路径不调它）。"""

        threads = list(getattr(self, "_meme_discard_threads", ()) or ())
        for thread in threads:
            thread.join(timeout)
        self._meme_discard_threads = [item for item in threads if item.is_alive()]
        return len(threads)

    async def api_meme_delete(self):
        """**彻底删除**收藏：记录、来源、变体、用法，以及底层图片文件。

        用户 2026-10-29 的口径：删就是删，不要"删了记录但文件还在"；
        只是不想让她用就点「停用」（``meme/toggle``），那条路不删任何东西。

        ``ids`` 一批就走批量；``id`` 单条照旧（老前端与脚本不动）。
        文件只在确实没人再用时才删：同一份内容还挂在别的收藏上、或被 pin 住
        （正在发送 / 任务里在用）都留着，返回里的 ``file_removed`` 说明删没删。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        store = self._meme_store()
        entry_id = str(payload.get("id", "") or "").strip()
        raw_ids = payload.get("ids") or []
        batch: list[str] = []
        if not entry_id and isinstance(raw_ids, (list, tuple)):
            batch = [str(item).strip() for item in raw_ids if str(item or "").strip()]
            if not batch:
                return error_response("要选几条收藏再删", status_code=400)
        elif entry_id:
            batch = [entry_id]
        else:
            return error_response("要给一个收藏编号", status_code=400)
        done: list[str] = []
        missed: list[str] = []
        pending: list[str] = []
        try:
            for item_id in batch[:200]:
                # 用户 m21273：「删除好卡，你后台异步删就可以了，把删除记录结果返回就行」。
                # 记录走同步（毫秒级），图片文件丢后台删（删文件才是慢的那一步）。
                outcome = await asyncio.to_thread(store.forget, item_id)
                if outcome.get("ok"):
                    done.append(item_id)
                    media_id = str(outcome.get("media_id") or "")
                    if outcome.get("file_pending") and media_id:
                        pending.append(media_id)
                else:
                    missed.append(item_id)
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "删不掉这条收藏")
        if pending:
            self._spawn_meme_media_discard(pending)
        files = 0
        note = f"删掉 {len(done)} 条收藏"
        if pending:
            note += f"，{len(pending)} 张图在后台清理"
        if missed:
            note += f"，{len(missed)} 条没找到"
        result: dict[str, Any] = {
            "ok": bool(done),
            "ids": done,
            "missing": missed,
            "files_removed": files,
            "files_pending": len(pending),
            "note": note,
            "message": note,
        }
        if entry_id:
            result["id"] = entry_id
            result["hard"] = True
            result["file_kept"] = False
        if not done:
            result["ok"] = False
            result["reason"] = f"没有这个收藏：{entry_id or ','.join(batch[:3])}"
        return json_response(result)

    async def api_meme_backfill(self):
        """M5：把已收图的**资产事实**（宽高 / 帧数）补齐。默认只预览。

        以前只存了 mime 与字节数，宽高和帧数一直是 0（卡片上就只剩个"动图"），
        所以给一个能重复跑、只补空字段、且**默认不写**的入口：``confirm=true`` 才落盘。
        探不出来的（坏图 / 超大 / 没装 Pillow）如实计数，绝不编尺寸。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        confirm = bool(payload.get("confirm"))
        try:
            limit = int(payload.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        store = self._meme_store()
        try:
            result = await asyncio.to_thread(
                store.backfill_asset_facts,
                media_store=self.engine.media_store(),
                limit=max(1, min(1000, limit)),
                dry_run=not confirm,
                only_missing=True,
                now=self.engine._now(),
            )
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "补齐图片信息失败")
        if not isinstance(result, dict):
            return json_response({"ok": False, "reason": "补齐没有返回结果"})
        payload_out = dict(result)
        payload_out["dry_run"] = not confirm
        if not confirm:
            payload_out["note"] = "这是预览：确认之后再点一次才会写进去。"
        return json_response(payload_out)

    async def api_meme_cleanup(self):
        """整理：清过期的展示记录与没人引用的资产元数据。默认只预览。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        store = self._meme_store()
        try:
            keep_days = int(store.settings.exposure_keep_days or 30)
            plan = {
                "exposure_keep_days": keep_days,
                "exposures": await self.db.call(
                    "count_exposures"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "算不出要清理什么")
        if not bool(payload.get("confirm")):
            return json_response({"ok": True, "dry_run": True, "plan": plan})
        try:
            removed_exposures = await self.db.call(
                "cleanup_meme_exposures", keep_days=keep_days
            )
            removed_assets = await self.db.call("cleanup_meme_assets")
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "清理失败")
        return json_response(
            {
                "ok": True,
                "dry_run": False,
                "plan": plan,
                "removed_exposures": removed_exposures,
                "removed_assets": removed_assets,
                "note": "只清了过期的展示记录与没人引用的资产元数据，图片文件一件没动。",
            }
        )

    async def api_meme_upload(self):
        """用户主动上传一张表情包（编辑器里点「上传表情包」）。

        与聊天里收图走**同一条数据路径**（``MemeStore.collect_image``）：先把字节落进
        资产库拿到 ``media_id``，再建收藏记录。所以同一张图再传一次就是"已经收过了"，
        不会多出第二条记录；上传成功后名字与含义都能在右侧详情里改。

        总开关关着也允许上传：这一页本来就"关着也能看、能改、能清理"，收进来等打开
        就能用；反过来，上传本身绝不会往聊天里发图。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        data = str(payload.get("data") or "").strip()
        if not data:
            return error_response("要选一张图片", status_code=400)
        # 文件名去掉扩展名当默认名字；真名留空就等用户在详情里填
        raw_name = " ".join(str(payload.get("name") or "").split())
        name = raw_name[:40]
        # 收下的表情一律通用（用户 m20：去掉会话限制），不再问"哪个会话能用"
        scope_id = "*"
        source = data if data.startswith("data:") else f"base64://{data}"
        try:
            limit = int(self.engine.media_settings().get("max_file_bytes") or 0)
        except Exception:
            limit = 0
        # base64 大约比原字节大 4/3：先粗筛一次，省得白解码再被资产库拒
        if limit > 0 and len(data) > limit * 2:
            return json_response(
                {
                    "ok": False,
                    "reason": f"这张图太大了（上限 {self._meme_size_text(limit)}）",
                }
            )
        try:
            media_id = await self.engine.cache_media(source)
        except Exception as exc:  # noqa: BLE001 - 统一翻成中文原因
            return self._meme_error(exc, "存不进资产库")
        if not media_id:
            return json_response({"ok": False, "reason": "存不进资产库：格式不支持或超出大小上限"})
        now = time.time()
        try:
            store = self._meme_store()
            result = await asyncio.to_thread(
                store.collect_image,
                media_id=media_id,
                name=name,
                scope_id=scope_id,
                # 来源写清楚是"人手动传的"：跟聊天里收下的图区分得开
                source={
                    "origin": "manual",
                    "origin_kind": "manual",
                    "occurred_at": now,
                },
                # 资产库实情（类型/大小/动图）与时间戳都要给：少一样，卡片上就是空的
                asset=asset_facts(self.engine.media_store(), media_id),
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            return self._meme_error(exc, "收藏写不进去")
        if not result.get("ok"):
            return json_response(
                {"ok": False, "reason": "这张图没能收进收藏，看看它还在不在资产库里"}
            )
        created = bool(result.get("created"))
        return json_response(
            {
                "ok": True,
                "created": created,
                "id": str(result.get("entry_id") or ""),
                "state": str(result.get("state") or ""),
                "entry": result.get("entry") or {},
                "note": "已经收进收藏" if created else "这张图之前就收藏过了",
            }
        )

    async def _meme_collect_path(
        self,
        path: "Path",
        *,
        store: Any,
        size_limit: int,
        now: float,
    ) -> tuple[str, str]:
        """把服务器目录里的一个文件收进收藏：``("created"|"existed"|"failed", 原因)``。

        上传、目录导入、目录导入的后台任务共用这一条，省得三处各写一遍（也就不会
        出现"批量里少写一个字段"这种偏差）。
        """

        name = path.stem.strip()[:40]
        try:
            body = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:  # noqa: BLE001
            return "failed", f"{path.name}：读不出来（{exc}）"
        if size_limit > 0 and len(body) > size_limit:
            return "failed", f"{path.name}：太大了（上限 {self._meme_size_text(size_limit)}）"
        # 用户 m21273「导入好慢」：这条路以前把字节 base64 编码一遍、再让 cache_media
        # 解码回来——多一次全量拷贝与一趟字符串驻留。本地文件直接进资产库，
        # 编码/解码这一趟就免了（校验、sha256、落盘仍在 MediaStore.put 里）。
        try:
            asset = await asyncio.to_thread(
                self.engine.media_store().put, body, source="meme_import"
            )
            media_id = str(getattr(asset, "media_id", "") or "")
        except Exception as exc:  # noqa: BLE001
            return "failed", f"{path.name}：存不进资产库（{exc}）"
        if not media_id:
            return "failed", f"{path.name}：格式不支持或超出大小上限"
        try:
            result = await asyncio.to_thread(
                store.collect_image,
                media_id=media_id,
                name=name,
                scope_id="*",
                source={
                    "origin": "manual_folder",
                    "origin_kind": "manual_folder",
                    "occurred_at": now,
                },
                asset=asset_facts(self.engine.media_store(), media_id),
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            return "failed", f"{path.name}：收藏写不进去（{exc}）"
        if result.get("ok") and result.get("created"):
            return "created", ""
        if result.get("ok"):
            return "existed", ""
        return "failed", f"{path.name}：没能收进收藏"

    def _meme_import_job(self) -> dict[str, Any] | None:
        """当前这轮「从文件夹导入」任务（一次只允许一个）。"""

        return getattr(self, "_meme_import_current", None)

    def _meme_import_job_view(self, job: dict[str, Any] | None) -> dict[str, Any]:
        """进度快照（页面轮询这个）：只回数字，不回 asyncio 任务对象。"""

        if not isinstance(job, dict):
            return {}
        return {
            "id": str(job.get("id") or ""),
            "status": str(job.get("status") or ""),
            "path": str(job.get("path") or ""),
            "total": int(job.get("total") or 0),
            "done": int(job.get("done") or 0),
            "created": int(job.get("created") or 0),
            "existed": int(job.get("existed") or 0),
            "failed": int(job.get("failed") or 0),
            "reasons": [str(item)[:120] for item in (job.get("reasons") or [])][:5],
            "started_at": float(job.get("started_at") or 0.0),
            "finished_at": float(job.get("finished_at") or 0.0),
            "error": str(job.get("error") or "")[:200],
        }

    def _meme_import_batch_start(
        self,
        *,
        files: list["Path"],
        folder: str,
        store: Any,
        size_limit: int,
    ) -> dict[str, Any]:
        """起一个后台导入任务（页面只轮询进度；用户 m18755 要进度条）。"""

        current = self._meme_import_job()
        if isinstance(current, dict) and str(current.get("status") or "") in {
            "running",
            "cancelling",
        }:
            return current
        job: dict[str, Any] = {
            "id": f"imp{int(time.time() * 1000) % 100000000}",
            "status": "running",
            "path": str(folder),
            "total": len(files),
            "done": 0,
            "created": 0,
            "existed": 0,
            "failed": 0,
            "reasons": [],
            "started_at": time.time(),
            "finished_at": 0.0,
            "error": "",
        }
        if not files:
            job["status"] = "done"
            job["finished_at"] = time.time()
            self._meme_import_current = job
            return job
        task = asyncio.create_task(
            self._meme_import_batch_run(job, files, store, size_limit)
        )
        job["task"] = task
        task.add_done_callback(lambda _t, j=job: j.pop("task", None))
        self._meme_import_current = job
        return job

    async def _meme_import_batch_run(
        self,
        job: dict[str, Any],
        files: list["Path"],
        store: Any,
        size_limit: int,
    ) -> None:
        """逐张收；每收一张跳一次进度，点停止就让当前这张做完再收。"""

        now = time.time()
        # 用户 m21273「导入好慢」：一张一张串行等于把 IO 排在一条队里。
        # 读盘/哈希/落盘都在线程里做，这里三路并发就够（再多会跟聊天抢磁盘）。
        gate = asyncio.Semaphore(3)

        async def take(path: "Path") -> None:
            if str(job.get("status") or "") != "running":
                return
            async with gate:
                if str(job.get("status") or "") != "running":
                    return
                state, reason = await self._meme_collect_path(
                    path, store=store, size_limit=size_limit, now=now
                )
            job["done"] = int(job.get("done") or 0) + 1
            if state == "created":
                job["created"] = int(job.get("created") or 0) + 1
            elif state == "existed":
                job["existed"] = int(job.get("existed") or 0) + 1
            else:
                job["failed"] = int(job.get("failed") or 0) + 1
                if reason and len(job.get("reasons") or []) < 5:
                    job.setdefault("reasons", []).append(reason)

        try:
            await asyncio.gather(*(take(path) for path in files))
            job["status"] = (
                "cancelled" if str(job.get("status") or "") == "cancelling" else "done"
            )
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = f"{exc}"[:200]
        finally:
            job["finished_at"] = time.time()

    async def api_meme_import(self):
        """把一个目录里的图片整批收进表情包收藏（用户 m08008：不想一张张导）。

        这个目录是**运行 AstrBot 那台机器上的**（不是浏览器所在的电脑），所以界面上
        写的就是"服务器上的目录"。按扩展名挑图，``recursive`` 决定要不要进子目录；
        每一张都走跟「上传表情包」同一条路（``cache_media`` → ``collect_image``），
        所以已经收过的只会记成"重复"，不会多出第二条收藏。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        # 批量导入是后台任务：页面只轮询进度/停一次（用户 m18755：要进度条）。
        if payload.get("status") is True:
            return json_response(
                {"ok": True, "job": self._meme_import_job_view(self._meme_import_job())}
            )
        if payload.get("stop") is True:
            job = self._meme_import_job()
            if job is not None and str(job.get("status") or "") == "running":
                job["status"] = "cancelling"
            return json_response(
                {"ok": True, "job": self._meme_import_job_view(job)}
            )
        raw = " ".join(str(payload.get("path") or "").split())
        if not raw:
            return error_response("要写一个目录（服务器上存图的那个文件夹）", status_code=400)
        folder = Path(os.path.expanduser(raw))
        try:
            if not folder.is_dir():
                return error_response(f"这不是一个目录：{raw}", status_code=400)
        except OSError:
            return error_response(f"打不开这个目录：{raw}", status_code=400)
        recursive = payload.get("recursive") is not False
        try:
            # 默认一次 500 张（用户 m19069 问上限）：导入只是落盘+写库，不调模型，
            # 所以不必按 200 卡；实在更多就再点一次（每次都会跳过已有的）。
            limit = max(1, min(2000, int(payload.get("limit") or 500)))
        except (TypeError, ValueError):
            limit = 500
        suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        try:
            walker = folder.rglob("*") if recursive else folder.glob("*")
            files = sorted(
                (item for item in walker if item.is_file() and item.suffix.lower() in suffixes),
                key=lambda item: str(item).lower(),
            )
        except OSError as exc:  # noqa: BLE001 - 统一翻成中文原因
            return error_response(f"读目录出错：{exc}", status_code=400)
        total = len(files)
        picked = files[:limit]
        store = self._meme_store()
        try:
            size_limit = int(self.engine.media_settings().get("max_file_bytes") or 0)
        except Exception:  # noqa: BLE001
            size_limit = 0
        created = 0
        existed = 0
        failed = 0
        reasons: list[str] = []
        now = time.time()
        if payload.get("all") is True:
            job = self._meme_import_batch_start(
                files=picked, folder=str(folder), store=store, size_limit=size_limit
            )
            return json_response(
                {
                    "ok": True,
                    "job": self._meme_import_job_view(job),
                    "total": total,
                    "picked": len(picked),
                }
            )
        for path in picked:
            state, reason = await self._meme_collect_path(
                path, store=store, size_limit=size_limit, now=now
            )
            if state == "created":
                created += 1
            elif state == "existed":
                existed += 1
            else:
                failed += 1
                if reason and len(reasons) < 5:
                    reasons.append(reason)
        return json_response(
            {
                "ok": True,
                "path": str(folder),
                "total": total,
                "picked": len(picked),
                "created": created,
                "existed": existed,
                "failed": failed,
                "reasons": reasons,
                "note": f"收下 {created} 张"
                + (f"、{existed} 张以前就有" if existed else "")
                + (f"、{failed} 张没成" if failed else ""),
            }
        )

    # ---------------- 通讯录（用户画像）----------------

    def _profile_scope(self, value: Any) -> str:
        return self._scope(value)

    async def _profile_person_payload(self, session_id: str, user_id: str) -> dict[str, Any]:
        """一个人的完整画像：字段 + 事实 + 关系（含历史与"他自称"）+ 好感日志。"""

        store = getattr(self.engine, "profiles", None)
        payload: dict[str, Any] = {"session": session_id, "user_id": user_id}
        if store is None:
            return payload
        payload["person"] = self.engine.person_payload(session_id, user_id)
        payload["facts"] = store.facts(session_id, user_id, statuses=["active", "candidate", "past"])
        # 垫底的"陌生人"和同类里被顶掉的旧关系都不往外露（老数据靠这一层收干净）
        bonds = store.visible_bonds(session_id, user_id)
        for item in bonds:
            item["since_text"] = self.engine._date_text(item.get("since"))
            item["until_text"] = self.engine._date_text(item.get("until"))
        payload["bonds"] = bonds
        payload["affinity_logs"] = store.affinity_logs(session_id, user_id, limit=50)
        # 「她想不想这个人」：通讯录里一眼能看到，不用去状态页翻
        state = await self.engine.load_state(session_id, cold_start=False)
        # 「她记着他一笔账」：记的账是分人的，这里只回这个人的那条
        grudge = self.engine.grudge_for(state, user_id)
        payload["grudge"] = (
            {
                **grudge,
                "at_text": self.engine._date_text(grudge.get("at")),
                "until_text": self.engine._date_text(grudge.get("until")),
                "days": max(
                    0.0,
                    (self.engine._now() - float(grudge.get("at") or 0.0)) / 86400.0,
                ),
            }
            if grudge
            else None
        )
        # 「她自己记着的事」：只列跟这个人相关的（没写对谁的就是她自己的打算）
        payload["own_topics"] = [
            dict(item)
            for item in (state.own_topics or [])
            if isinstance(item, dict)
            and str(item.get("who") or "") in ("", user_id)
        ]
        overview = self.engine.miss_overview(state)
        payload["miss"] = next(
            (item for item in overview.get("people", []) if item.get("user_id") == user_id),
            {
                "user_id": user_id,
                "value": 0.0,
                "waiting": False,
                "ready_in_minutes": 0,
                "will_reach_out": False,
            },
        )
        payload["miss_threshold"] = overview.get("threshold")
        config = self.engine.world.profile
        payload["bonds_config"] = [
            {
                "name": bond.name,
                "slot": bond.slot,
                "group": bond.group,
                "cap": bond.cap,
                "unique": bool(bond.unique),
                "negative": bool(bond.negative),
                "aliases": list(bond.aliases or []),
            }
            for bond in config.bonds
        ]
        payload["levels_config"] = [
            {
                "name": level.name,
                "min_affinity": level.min_affinity,
                "max_affinity": level.max_affinity,
                "deny": [store.action_label(item) for item in (level.deny or [])],
                "prompt": level.prompt,
            }
            for level in config.levels
        ]
        return payload

    @staticmethod
    def _qq_avatar_sync(qq: str) -> str:
        """同步取一张 QQ 头像，转成 ``data:`` URL（取不到就是空串）。

        放在线程里跑：这是纯网络 IO，别占事件循环（用户 m13421）。
        """

        import urllib.request

        request_obj = urllib.request.Request(
            AVATAR_URL.format(qq=qq),
            headers={"User-Agent": "Mozilla/5.0 (VirtualWorld avatar fetch)"},
        )
        with urllib.request.urlopen(request_obj, timeout=6) as response:
            raw = response.read(AVATAR_MAX_BYTES + 1)
            content_type = str(response.headers.get("Content-Type") or "image/jpeg")
        if not raw or len(raw) > AVATAR_MAX_BYTES:
            return ""
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        return f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"

    async def _qq_avatar(self, qq: str) -> str:
        """带缓存的头像读取：号码不合法、或网络不给就直接空串，不抛。"""

        qq = str(qq or "").strip()
        if not (qq.isdigit() and 5 <= len(qq) <= 12):
            return ""
        stamp = time.time()
        cached = self._avatars.get(qq)
        if cached and stamp - cached[0] < AVATAR_TTL:
            return cached[1]
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(self._qq_avatar_sync, qq), timeout=8.0
            )
        except Exception:  # noqa: BLE001 - 头像只是装饰，失败就算了
            data = ""
        if data:
            self._avatars[qq] = (stamp, data)
            if len(self._avatars) > 200:
                for key, _ in sorted(self._avatars.items(), key=lambda item: item[1][0])[:50]:
                    self._avatars.pop(key, None)
        return data

    async def api_avatars(self):
        """一次要一批头像：通讯录渲染完再补图，别让列表等网络。"""

        guard = self._guard()
        if guard is not None:
            return guard
        body = await request.json(default={}) or {}
        raw = body.get("qqs") if isinstance(body, dict) else None
        if not isinstance(raw, list):
            return error_response("需要 qqs 列表")
        wanted: list[str] = []
        for item in raw[:24]:
            qq = str(item or "").strip()
            if qq.isdigit() and 5 <= len(qq) <= 12 and qq not in wanted:
                wanted.append(qq)
        results = await asyncio.gather(*(self._qq_avatar(qq) for qq in wanted))
        return json_response(
            {"ok": True, "avatars": {qq: url for qq, url in zip(wanted, results) if url}}
        )

    async def api_profile_people(self):
        """通讯录列表：这个人叫啥、什么关系、好感多少、最近说过话没有。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._profile_scope(request.query.get("session", ""))
        if not session_id:
            return error_response("缺少 session 参数")
        store = getattr(self.engine, "profiles", None)
        if store is None:
            return json_response({"people": [], "enabled": False})
        people: list[dict[str, Any]] = []
        for row in store.list_people(session_id, limit=200):
            user_id = str(row.get("user_id") or "")
            view = store.view(session_id, user_id)
            if view is None:
                continue
            people.append(
                {
                    "user_id": user_id,
                    "name": view.name,
                    "qq_name": view.qq_name,
                    # 在哪些群/私聊、各叫什么（用户 m14014：跨群昵称要能对上同一个人）
                    "cards": {
                        str(sid): str(card)
                        for sid, card in dict(view.cards or {}).items()
                        if str(card)
                    },
                    "affinity": view.affinity,
                    "level": view.level.name,
                    "bonds": list(view.affinities),
                    "claims": [str(item.get("type") or "") for item in view.claims],
                    "digest": row.get("digest") or "",
                    "message_count": int(row.get("message_count") or 0),
                    "last_seen_at": float(row.get("last_seen_at") or 0.0),
                    "first_seen_at": float(row.get("first_seen_at") or 0.0),
                    "days": len(
                        [
                            item
                            for item in ((row.get("payload") or {}).get("days") or [])
                            if str(item)
                        ]
                    ),
                }
            )
        return json_response(
            {
                "people": people,
                "enabled": bool(self.engine.world.profile.enabled),
                "consolidate_enabled": bool(
                    self.engine.world.profile.consolidate_enabled
                ),
                "digest_limit": int(self.engine.world.profile.digest_limit),
            }
        )

    async def api_profile_person(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(
            payload.get("session") or request.query.get("session", "")
        )
        user_id = str(payload.get("user_id") or request.query.get("user_id", "") or "")
        if not session_id or not user_id:
            return error_response("缺少 session 或 user_id 参数")
        store = getattr(self.engine, "profiles", None)
        if store is None:
            return error_response("画像功能还没就绪")
        method = str(getattr(request, "method", "") or "").upper()
        if method == "POST" or str(request.query.get("_method", "")).upper() == "POST":
            if "note" in payload:
                store.set_note(session_id, user_id, str(payload.get("note") or ""))
            call_me = str(payload.get("call_me") or "").strip()
            call_him = str(payload.get("call_him") or "").strip()
            if call_me or call_him:
                result = store.set_call_names(
                    session_id,
                    user_id,
                    call_me=call_me,
                    call_him=call_him,
                    explicit=True,
                )
                if result.get("rejected"):
                    reason = result["rejected"][0].get("reason") or "这个称呼不能用"
                    return error_response(reason)
            if "digest" in payload:
                store.set_digest(session_id, user_id, str(payload.get("digest") or ""))
        return json_response(await self._profile_person_payload(session_id, user_id))

    async def api_profile_fact(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        action = str(payload.get("action") or "add")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        db = store.db
        group_id = store.group_key(session_id)
        if action == "add":
            result = store.note_fact(
                session_id,
                user_id,
                text=str(payload.get("text") or ""),
                kind=str(payload.get("kind") or "other"),
                evidence=str(payload.get("evidence") or "主人手动加的"),
                context=str(payload.get("context") or ""),
                confidence=1.0,
                status=str(payload.get("status") or "active"),
                pinned=bool(payload.get("pinned")),
            )
            if not result.get("ok"):
                return error_response(str(result.get("reason") or "没记下来"))
        elif action == "update":
            fact_id = int(payload.get("id") or 0)
            if not fact_id:
                return error_response("缺少 id")
            db.update_user_fact(
                fact_id=fact_id,
                text=str(payload.get("text")) if "text" in payload else None,
                kind=str(payload.get("kind")) if "kind" in payload else None,
                status=str(payload.get("status")) if "status" in payload else None,
                pinned=bool(payload["pinned"]) if "pinned" in payload else None,
            )
        elif action == "delete":
            db.delete_user_fact(fact_id=int(payload.get("id") or 0))
        else:
            return error_response("不认识的操作")
        return json_response(
            {
                "ok": True,
                "facts": db.list_user_facts(group_id=group_id, user_id=user_id),
                "person": self.engine.person_payload(session_id, user_id),
            }
        )

    async def api_profile_bond(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        action = str(payload.get("action") or "note")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        if action == "note":
            result = store.note_bond(
                session_id,
                user_id,
                type=str(payload.get("type") or ""),
                evidence=str(payload.get("evidence") or "主人手动设的"),
                confidence=1.0,
                asserted_by=str(payload.get("asserted_by") or "她的判断"),
                policy=str(payload.get("policy") or ""),
            )
            if not result.get("ok"):
                return error_response(str(result.get("reason") or "没记下来"))
        elif action == "close":
            store.close_bond(session_id, user_id, bond_id=int(payload.get("id") or 0))
        elif action == "accept":
            result = store.accept_claim(
                session_id,
                user_id,
                bond_id=int(payload.get("id") or 0),
                policy=str(payload.get("policy") or ""),
            )
            if not result.get("ok"):
                return error_response(str(result.get("reason") or "认不下来"))
        elif action == "delete":
            store.db.delete_user_bond(bond_id=int(payload.get("id") or 0))
        else:
            return error_response("不认识的操作")
        return json_response(
            {
                "ok": True,
                "person": self.engine.person_payload(session_id, user_id),
            }
        )

    async def api_profile_affinity(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        if "value" in payload:
            row = store.profile(session_id, user_id)
            before = float((row or {}).get("affinity") or 0.0)
            delta = float(payload.get("value") or 0.0) - before
        else:
            delta = float(payload.get("delta") or 0.0)
        result = store.adjust_affinity(
            session_id,
            user_id,
            delta,
            reason=str(payload.get("reason") or "主人调的"),
            source="manual",
        )
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "没改成"))
        return json_response(
            {
                "ok": True,
                "affinity": result.get("value"),
                "person": self.engine.person_payload(session_id, user_id),
            }
        )

    async def api_profile_grudge(self):
        """通讯录里手动管她记着的账：加一笔 / 算了 / 删掉。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        action = str(payload.get("action") or "add")
        if not session_id or not user_id:
            return error_response("缺少参数")
        async with self.engine.session_state(session_id) as state:
            if action == "add":
                reason = str(payload.get("reason") or payload.get("text") or "").strip()
                if not reason:
                    return error_response("写一句她记着的事")
                ok = self.engine.note_grudge(
                    state,
                    reason,
                    user_id=user_id,
                    session_id=session_id,
                    # 手动加的不吃"每天最多一笔"那道闸（那是拦模型的）
                    force=True,
                )
                if not ok:
                    return error_response("这笔账没能记下来（记仇关着吗？）")
            elif action == "resolve":
                self.engine.resolve_grudge(state, user_id=user_id)
            elif action == "delete":
                state.grudges = [
                    item
                    for item in (state.grudges or [])
                    if not (
                        isinstance(item, dict)
                        and str(item.get("user_id") or "") == user_id
                    )
                ]
            else:
                return error_response("不认识的操作")
        return json_response(await self._profile_person_payload(session_id, user_id))

    async def api_profile_forget(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        removed = store.db.delete_user_profile(
            group_id=store.group_key(session_id), user_id=user_id
        )
        return json_response({"ok": True, "removed": removed})

    async def api_profile_forget_all(self):
        """通讯录上方那个「清空」：把这个会话组里她认识的人全忘掉。

        只删这个 group_key 下的画像 / 事实 / 关系 / 好感日志；世界配置里的
        关系档位（profile.bonds）和会话白名单都不动。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id:
            return error_response("缺少参数")
        group_id = store.group_key(session_id)
        people = store.list_people(session_id, limit=5000)
        removed = 0
        forgotten = 0
        for item in people:
            user_id = str(item.get("user_id") or "")
            if not user_id:
                continue
            removed += int(
                store.db.delete_user_profile(group_id=group_id, user_id=user_id) or 0
            )
            forgotten += 1
        # 她"今天还能主动找几个人"的额度也跟着清，免得留下没人认领的计数
        store.drop_daily_people(group_id)
        return json_response({"ok": True, "people": forgotten, "removed": removed})

    async def api_profile_consolidate(self):
        """立刻整理一次（编辑器上的"现在整理一次"）：消化这段时间的经历。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session 参数")
        mode = str(payload.get("mode") or "full")
        dry_run = bool(payload.get("dry_run"))
        state_key = self.engine.state_key(session_id)
        async with self.engine.session_state(state_key) as state:
            result = await self.engine.consolidate_now(
                state, mode=mode, dry_run=dry_run
            )
        if result is None:
            return error_response("整理没跑起来：先确认「睡眠整理模型」可用")
        if not result.get("ok"):
            return json_response({"ok": False, "note": result.get("note") or "没整理出东西"})
        return json_response(result)

    async def api_events(self):
        """事件系统概览：能力值、未了的事、最近的线索。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = str(request.query.get("session", "") or "")
        if not session_id:
            return error_response("缺少 session 参数")
        session_id = self._scope(session_id)
        overview = await self.engine.event_overview(session_id)
        return json_response(overview)

    async def api_persona_source(self):
        """编辑器「从 AstrBot 导入」用：把 AstrBot 给这个会话的人设读出来。

        插件默认就是用它（``world.persona.mode = astrbot``）；这里只是给个入口，
        让用户能把手上那份人格一键抄进配置里，从此跟着预设走。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        persona = getattr(self, "persona_port", None)
        if persona is None or not hasattr(persona, "astrbot_persona_text"):
            return error_response("人设通道还没就绪")
        text = await persona.astrbot_persona_text(session_id)
        return json_response(
            {
                "session": session_id,
                "text": text,
                "chars": len(text),
                "mode": str(getattr(self.engine.world.persona, "mode", "astrbot")),
            }
        )

    async def api_persona_brief(self):
        """简易人设：按人格缓存。POST 时 ``generate=true`` 表示用模型从主人设生成。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = str(
            payload.get("session") or request.query.get("session", "") or ""
        )
        if not session_id:
            return error_response("缺少 session 参数")
        if request.method == "POST" or str(request.query.get("_method", "")).upper() == "POST":
            if payload.get("generate"):
                # 编辑器刚改过、还没保存的角色卡优先：向导里写完人设就点生成时，
                # 读配置只会拿到旧那份。
                brief = await self.engine.generate_persona_brief(
                    session_id, persona_text=str(payload.get("persona") or "")
                )
                if not brief:
                    return error_response(
                        "生成失败：先确认「生成器模型」可用，而且这个会话能读到人设"
                    )
            else:
                await self.engine.save_persona_brief(
                    session_id, str(payload.get("brief") or "")
                )
            state = await self.engine.persona_brief_state(session_id)
            return json_response({"ok": True, "persona_brief": state})
        return json_response({"persona_brief": await self.engine.persona_brief_state(session_id)})

    def _voice_sample_payload(self) -> dict[str, Any]:
        persona = self.engine.world.persona
        return {
            "samples": self.engine.persona_samples(),
            "candidates": [
                dict(item)
                for item in list(getattr(persona, "sample_candidates", None) or [])
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ],
            "scenes": [
                {"id": key, "label": label}
                for key, label in self.engine.prompts.VOICE_SCENES
            ],
            "per_turn": int(getattr(persona, "samples_per_turn", 3) or 3),
            "max": int(getattr(persona, "samples_max", 12) or 12),
            "candidates_max": int(getattr(persona, "candidates_max", 80) or 80),
        }

    async def api_voice_samples(self):
        """看样例库 + 可用的场景清单。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(self._voice_sample_payload())

    async def api_voice_samples_generate(self):
        """让内容生成模型按场景出候选（草稿，不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session（要拿它的角色卡去生成）")
        scenes = payload.get("scenes")
        picked = [str(item) for item in scenes] if isinstance(scenes, list) else None
        result = await self.engine.generate_voice_samples(
            session_id, picked, persona_text=str(payload.get("persona") or "")
        )
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "生成失败"))
        return json_response({**result, **{"scenes_meta": self._voice_sample_payload()["scenes"]}})

    async def api_voice_samples_from_chat(self):
        """从近期聊天留档里挑她说过、而且被接住了的话，当样例候选（不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        try:
            limit = max(1, min(40, int(payload.get("limit") or 12)))
        except (TypeError, ValueError):
            limit = 12
        async with self.engine.session_state(session_id) as state:
            found = self.engine.voice_sample_candidates_from_chat(state, limit=limit)
        return json_response(
            {"ok": True, "candidates": found, "note": "" if found else "这段时间没挑出合适的"}
        )

    async def api_voice_samples_save(self):
        """把挑中的样例写进世界配置（落库前先留一份历史）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        raw_samples = payload.get("samples")
        if not isinstance(raw_samples, list):
            return error_response("samples 必须是数组")
        persona = self.engine.world.persona
        limit = max(1, int(getattr(persona, "samples_max", 12) or 12))
        candidates_limit = max(1, int(getattr(persona, "candidates_max", 80) or 80))
        cleaned: list[dict[str, Any]] = []
        for index, item in enumerate(raw_samples):
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            cleaned.append(
                {
                    "id": str(item.get("id") or f"s{index + 1}"),
                    "scene": str(item.get("scene") or ""),
                    "label": str(item.get("label") or ""),
                    "move": " ".join(str(item.get("move") or "").split())[:20],
                    "text": text[:120],
                    "source": str(item.get("source") or "model"),
                }
            )
        if len(cleaned) > limit:
            return error_response(f"最多留 {limit} 条，先删掉几条再存")
        # 候选池：可选一起存。攒着不动，只有「采用」才会挪进上面的样例库
        raw_candidates = payload.get("candidates")
        candidates: list[dict[str, Any]] | None = None
        if isinstance(raw_candidates, list):
            candidates = []
            for index, item in enumerate(raw_candidates):
                if not isinstance(item, dict):
                    continue
                text = " ".join(str(item.get("text") or "").split())
                if not text:
                    continue
                candidates.append(
                    {
                        "id": str(item.get("id") or f"c{index + 1}"),
                        "scene": str(item.get("scene") or ""),
                        "label": str(item.get("label") or ""),
                        "move": " ".join(str(item.get("move") or "").split())[:20],
                        "text": text[:120],
                        "context": " ".join(str(item.get("context") or "").split())[:60],
                        "source": str(item.get("source") or "model"),
                    }
                )
            candidates = candidates[-candidates_limit:]
        world = self.store.raw_world()
        persona_raw = dict(world.get("persona") or {})
        persona_raw["samples"] = cleaned
        if candidates is not None:
            persona_raw["sample_candidates"] = candidates
        if payload.get("per_turn") is not None:
            try:
                persona_raw["samples_per_turn"] = max(1, min(6, int(payload["per_turn"])))
            except (TypeError, ValueError):
                pass
        world["persona"] = persona_raw
        warnings = self.store.save_world(world, reason="改动声音样例")
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings, **self._voice_sample_payload()})

    async def api_eval_run(self):
        """把剧本并发跑一遍：带着完整提示词，收集她的真实回复（不写任何状态）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        rounds = payload.get("rounds")
        if not isinstance(rounds, list) or not rounds:
            return error_response("缺少 rounds（要按剧本跑的问题）")
        if len(rounds) > 30:
            return error_response("一次最多跑 30 轮")
        try:
            concurrency = int(payload.get("concurrency") or 3)
        except (TypeError, ValueError):
            concurrency = 3
        provider_id = str(payload.get("provider_id") or "").strip()
        llm = AstrBotLLM(self, provider_id or self.llm_provider_id, sampling=self.sampling_params())
        director_id = str(payload.get("director_provider_id") or "").strip()
        result = await self.engine.run_eval(
            session_id,
            [dict(item) for item in rounds if isinstance(item, dict)],
            llm=llm,
            concurrency=concurrency,
            variant=str(payload.get("variant") or "full"),
            director_llm=AstrBotLLM(
                self,
                director_id or self.judge_provider_id,
                sampling=self.sampling_params(),
            ),
        )
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "跑失败"))
        return json_response(result)

    async def api_eval_script(self):
        """生成测评剧本：同一个剧本拿去跑不同模型，比"谁更像她"。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        try:
            rounds = int(payload.get("rounds") or 20)
        except (TypeError, ValueError):
            rounds = 20
        result = await self.engine.generate_eval_script(session_id, rounds)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "生成失败"))
        return json_response(result)

    async def api_persona_review(self):
        """体检人设：返回问题清单 + 可逐条采纳的改动（**不落库**）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        result = await self.engine.review_persona(session_id)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "体检失败"))
        return json_response(result)

    async def api_persona_apply(self):
        """把挑中的改动写进角色卡（写前自动留一份历史）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        rewrites = payload.get("rewrite") if isinstance(payload.get("rewrite"), list) else []
        adds = payload.get("add") if isinstance(payload.get("add"), list) else []
        result = self.engine.apply_persona_changes(rewrites, adds)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "应用失败"))
        return json_response(result)

    def _guard(self, payload: dict[str, Any] | None = None) -> Any:
        """统一鉴权：返回错误响应或 None。"""

        token = request.query.get("token", "") or ""
        if not token and isinstance(payload, dict):
            token = str(payload.get("token", "") or "")
        if self.auth.check(token):
            return None
        return error_response("需要先登录编辑器", status_code=401)

    def _scope(self, value: Any) -> str:
        """编辑器里选的可能是会话，也可能是会话组。

        会话组要落到它的代表会话上（她的状态、日志、日程都挂在那儿）。
        """

        text = str(value or "").strip()
        if not text:
            return ""
        engine = getattr(self, "engine", None)
        if engine is None:
            return text
        try:
            return engine.scope_session(text)
        except Exception:
            return text

    def _memory_sessions(self, value: Any) -> list[str]:
        """记忆按会话各存一份；查的时候把同组的会话一起当条件。

        编辑器里可能选的是会话组：那就展开成组里的所有会话。
        """

        engine = getattr(self, "engine", None)
        if engine is None:
            text = str(value or "").strip()
            return [text] if text else []
        try:
            return engine.group_sessions(self._scope(value))
        except Exception:
            text = str(value or "").strip()
            return [text] if text else []

    async def api_auth_status(self):
        return json_response(
            {
                "password_required": self.auth.has_password(),
                "locked_seconds": self.auth.locked(),
                "llm_provider_id": self.llm_provider_id,
                "tick_interval": self.tick_interval,
                "decider_interval": self.decider_interval,
            }
        )

    async def api_auth_login(self):
        payload = await request.json(default={}) or {}
        wait = self.auth.locked()
        if wait > 0:
            return error_response(f"失败次数过多，请 {wait} 秒后再试", status_code=429)
        if not self.auth.has_password():
            return json_response({"token": "", "password_required": False})
        if not self.auth.verify(str(payload.get("password", ""))):
            self.auth.note_failure()
            return error_response("密码不正确", status_code=401)
        return json_response({"token": self.auth.issue_token(), "password_required": True})

    async def api_auth_logout(self):
        payload = await request.json(default={}) or {}
        self.auth.revoke(str(payload.get("token", "") or ""))
        return json_response({"ok": True})

    async def api_auth_password(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        new_password = str(payload.get("new_password", "") or "")
        if self.auth.has_password() and not self.auth.verify(
            str(payload.get("old_password", ""))
        ):
            return error_response("旧密码不正确", status_code=403)
        self.auth.set_password(new_password)
        return json_response({"ok": True, "password_required": self.auth.has_password()})

    async def api_extensions(self):
        """扩展包：读设置与数据 / 保存设置。

        插件自己不认识任何扩展；这里只是把挂上来的扩展的字段定义和值原样交给设置页。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        if request.method.upper() == "POST":
            payload = await request.json(default={}) or {}
            problems: list[str] = []
            saved = self.extension_host.save_settings(
                dict(payload.get("values") or {}), errors=problems
            )
            if problems:
                # 口径和别的接口一样：失败也是 HTTP 200，错误放在 body 里
                return json_response(
                    {"ok": False, "error": "；".join(problems), "saved": saved}
                )
            return json_response({"ok": True, "saved": saved})
        session_id = self._scope(request.query.get("session", ""))
        state = None
        if session_id:
            try:
                state = await self.engine.load_state(session_id, cold_start=False)
            except Exception:
                state = None
        return json_response(self.extension_host.snapshot(state))

    # ------------------------------------------------------------------
    # 内容迁移（T17）：先列清单，用户确认了才写盘
    # ------------------------------------------------------------------

    def _raw_world_for_migrations(self) -> dict:
        """盘上那份 world（读不到就按空对象算：迁移只是清单，不阻断任何请求）。"""

        try:
            return self.store.raw_world()
        except Exception:
            return {}

    def _raw_schedules_for_migrations(self) -> dict:
        """盘上那份日程。日程在 ``schedules.json``，不在 ``world.json`` 里，得单独读。"""

        try:
            return self.store.raw_schedules()
        except Exception:
            return {}

    def _pending_migrations(self) -> list:
        raw = self._raw_world_for_migrations()
        skipped = migration_policy.load_skipped(self.store.data_dir)
        return migration_policy.detect_all(raw, skipped, self._raw_schedules_for_migrations())

    def _migrations_status_line(self) -> list[str]:
        """``/vw status`` 那一行：还有几项旧配置等着确认。"""

        try:
            count = len(self._pending_migrations())
        except Exception:
            return []
        if not count:
            return []
        return [f"有 {count} 项旧配置可以升级（在编辑器里确认）"]

    async def api_get_migrations(self):
        guard = self._guard()
        if guard is not None:
            return guard
        raw = self._raw_world_for_migrations()
        skipped = migration_policy.load_skipped(self.store.data_dir)
        items = migration_policy.detect_all(raw, skipped, self._raw_schedules_for_migrations())
        return json_response(
            {
                "items": [item.to_dict() for item in items],
                "skipped": sorted(skipped),
                "world_rev": migration_policy.world_rev(raw),
            }
        )

    async def api_apply_migrations(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        ids = payload.get("ids")
        if not isinstance(ids, list):
            return error_response("ids 必须是数组")
        raw = self._raw_world_for_migrations()
        raw_schedules = self._raw_schedules_for_migrations()
        new_raw, new_schedules, applied, failed = migration_policy.apply_batch(
            raw, raw_schedules, ids
        )
        if new_raw != raw:
            try:
                self.store.save_world(new_raw)
            except ValidationError as exc:
                return self._save_error("世界配置", exc)
        if new_schedules != raw_schedules:
            try:
                self.store.save_schedules(new_schedules)
            except ValidationError as exc:
                return self._save_error("日程", exc)
        if applied:
            self.engine.reload_config()
        return json_response(
            {
                "ok": not failed,
                "applied": applied,
                "failed": failed,
                "world_rev": migration_policy.world_rev(new_raw),
            }
        )

    async def api_skip_migrations(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        ids = payload.get("ids")
        if not isinstance(ids, list):
            return error_response("ids 必须是数组")
        raw = self._raw_world_for_migrations()
        skipped, failed = migration_policy.skip_ids(
            raw,
            self.store.data_dir,
            ids,
            world_rev_value=migration_policy.world_rev(raw),
            raw_schedules=self._raw_schedules_for_migrations(),
        )
        return json_response({"ok": not failed, "skipped": skipped, "failed": failed})

    async def api_get_config(self):
        guard = self._guard()
        if guard is not None:
            return guard
        # 老版本写下的配置文件里可能缺新增的键（例如心潮回落速度），
        # 直接返回原始 JSON 会让编辑器把它们显示成 0，一保存就把 0 写回磁盘。
        # 这里用校验后的配置补齐已知键，同时保留用户自己加的未知键。
        # 必须 by_alias：连线端点在模型里叫 from_（from 是 Python 关键字），
        # 但文件里和前端读的都是 from——按字段名 dump 会让地图连线整个消失。
        world = normalize_legacy_keys(
            normalize_edge_keys(
                {
                    **self.store.raw_world(),
                    **self.engine.world.model_dump(mode="json", by_alias=True),
                }
            )
        )
        schedules = self.store.raw_schedules()
        if self.engine.schedules is not None:
            schedules = {
                **schedules,
                **self.engine.schedules.model_dump(mode="json", by_alias=True),
            }
        sessions = self.store.raw_sessions()
        if self.engine.sessions is not None:
            sessions = {
                **sessions,
                **self.engine.sessions.model_dump(mode="json", by_alias=True),
            }
        # 动作的「亲密程度」留空 = 按内置对照表算。编辑器里要能看见**实际会用多少**，
        # 不然用户填了空白，永远不知道这一步到底会不会满足欲求。
        for action in world.get("actions") or []:
            if not isinstance(action, dict):
                continue
            explicit = action.get("intimacy")
            if explicit in (None, ""):
                action["intimacy_effective"] = round(
                    float(DEFAULT_INTIMACY.get(str(action.get("id") or ""), 0.0)), 3
                )
            else:
                try:
                    action["intimacy_effective"] = round(
                        max(0.0, min(1.0, float(explicit))), 3
                    )
                except (TypeError, ValueError):
                    action["intimacy_effective"] = 0.0
        return json_response(
            {
                "world": world,
                "schedules": schedules,
                "sessions": sessions,
                "warnings": self.engine.load_warnings,
                # 模型槽位当前用的是哪个（向导里做"模型配了吗"的检查用；只有 id，没有 key）
                "providers": self._provider_slots(),
                # 图片转述缓存的使用情况（编辑器里显示"省了多少次识别"）
                "vision_cache": await self._vision_cache_stats(),
                # 本机图片资产库的现状（阶段B4：编辑器里显示占用与"清理会删掉什么"）
                "media": self.engine.media_stats(),
                # 当前天气：地图页顶部横幅直接用它
                "weather": await self.engine.weather_payload(),
                # 动作库里"哪些动作是扩展带来的"：编辑器按它把动作库分成两半
                "extension_actions": self.extension_host.action_groups(),
            }
        )

    def _world_agent_config(self) -> Any:
        """世界配置里的 agent 段（读不到就返回 ``None``）。

        五个能力开关住在这里（世界编辑器里改）；**模型的槽位不住这里**——它是插件配置
        的 ``agent_provider_id``，这里那个 ``provider_id`` 只是老值的静默回退。

        Returns:
            ``AgentConfig`` 实例或 ``None``（引擎还没装好时）。
        """

        try:
            return self.engine.world.agent
        except Exception:
            return None

    def _configured_agent_provider_id(self) -> str:
        """agent 用哪个模型：**插件配置是正主**，世界配置里的老值是静默回退。

        用户裁定（m20632 第 1 条）：模型选择属于「插件配置」，不该搬到全局/世界配置里。
        回退保留是为了不丢掉用户这一轮在编辑器里选过的值——只读，绝不回写。

        Returns:
            配置里的 provider id；两处都空就是空串（后续级联回退接手）。
        """

        config = getattr(self, "config", None)
        if config is not None:
            value = str(config.get("agent_provider_id") or "").strip()
            if value:
                return value
        agent = self._world_agent_config()
        return str(getattr(agent, "provider_id", "") or "").strip()

    def _provider_slots(self) -> dict[str, str]:
        """各个模型槽位现在指向哪个 Provider（只给 id，不含 key）。

        向导里要能一眼看出"哪个槽位还没配"——除 agent 之外这些槽位都还在 AstrBot
        的插件配置里，编辑器本身改不了，所以只做展示 + 告诉用户去哪配。
        """

        def show(value: str) -> str:
            return str(value or "").strip()

        return {
            "llm": show(self.llm_provider_id) or "（跟会话默认）",
            "helper": show(self.helper_provider_id) or "（跟看图模型）",
            "judge": show(self.judge_provider_id) or "（跟打杂模型）",
            "vision": show(self.vision_provider_id) or "（不转述图片）",
            "creator": show(self.creator_provider_id) or "（跟插件用模型）",
            "event": show(self.event_provider_id) or "（跟打杂模型）",
            "consolidate": show(self.consolidate_provider_id) or "（跟打杂模型）",
            # agent 模型：插件配置里的 agent_provider_id 是正主（世界配置里那个老值
            # 只是静默回退），留空时的级联回退（agent → 主模型 → 会话）由 WP4 做
            "agent": show(self._configured_agent_provider_id()) or "（跟插件用模型）",
            "embedding": show(self.embedding_provider_id) or "（关闭语义检索）",
        }

    def agent_settings(self) -> dict[str, Any]:
        """agent 相关配置的只读快照（WP8 调试输出、B3 消费用）。

        **字段存在 ≠ 能力开启**：这里只把用户配了什么原样读回来，真正的
        工具注册与任务创建在 B3——本包不实现执行器，也不拿这些值做任何决策。
        ``agent_provider_id`` 返回**插件配置**里的原文（留空时回退到世界配置里的老值，
        两处都空就是空串），空串到会话模型的级联回退留给 WP4，不在这里解析。

        键名沿用 agent 的**调试快照输出契约**（``agent_provider_id`` / 五个
        ``agent_*`` 开关）：它们以前就是这名字，改了会打断调试页与快照测试。
        值的来源是「模型在插件配置、五个开关在世界配置」，键名不变。

        Returns:
            恰好七个键的只读字典：``agent_provider_id``、``embedding_provider_id``
            与五个 ``agent_*`` 开关。
        """

        agent = self._world_agent_config()
        settings: dict[str, Any] = {
            # 模型的槽位在插件配置里；世界配置里那个老值是静默回退
            "agent_provider_id": self._configured_agent_provider_id(),
            # embedding 仍住在插件配置里
            "embedding_provider_id": str(self.embedding_provider_id),
        }
        for name, legacy in self.AGENT_SWITCH_WORLD_KEYS:
            settings[legacy] = bool(getattr(agent, name, False))
        return settings

    # ---------------- B0/WP4：供应商解析（只读，绝不抛异常） ----------------

    async def resolve_agent_provider_id(
        self, session_id: str | None = None
    ) -> dict[str, str]:
        """agent 模型的级联回退（只读）：agent → 插件用主模型 → 当前会话模型。

        三级都问不到就落到 ``none`` 并给出中文原因。这里只回答「该用哪个 id」，
        实例在不在由 :meth:`get_provider_instance` 回答——B0 不做执行器。

        Args:
            session_id: 会话号；第三级回退要用它问宿主当前用的是哪个模型。

        Returns:
            ``{"id", "source", "reason"}``；``source`` 取
            ``agent`` / ``llm`` / ``session`` / ``none``，解析不到时 ``id`` 是空串。
        """

        def text(value: Any) -> str:
            return str(value or "").strip()

        # agent 模型槽位住在插件配置里（``agent_provider_id``）；世界配置里那个老值
        # 只是静默回退，让这一轮在编辑器里选过模型的用户不掉配置
        configured = text(self._configured_agent_provider_id())
        if configured:
            return {"id": configured, "source": "agent", "reason": ""}
        fallback = text(getattr(self, "llm_provider_id", ""))
        if fallback:
            return {"id": fallback, "source": "llm", "reason": ""}
        session = text(session_id)
        if not session:
            return {
                "id": "",
                "source": "none",
                "reason": (
                    "agent 模型没配，插件用主模型也没配，又不知道是哪个会话，"
                    "问不到会话默认模型"
                ),
            }
        context = getattr(self, "context", None)
        getter = getattr(context, "get_current_chat_provider_id", None)
        if not callable(getter):
            return {
                "id": "",
                "source": "none",
                "reason": "宿主不支持 get_current_chat_provider_id，取不到当前会话模型",
            }
        try:
            current = text(await getter(session))
        except Exception as exc:
            return {"id": "", "source": "none", "reason": f"取当前会话模型失败：{exc}"}
        if not current:
            return {"id": "", "source": "none", "reason": "当前会话没有可用的模型"}
        return {"id": current, "source": "session", "reason": ""}

    def resolve_embedding_provider_id(self) -> dict[str, str]:
        """embedding 供应商解析（只读、单级）：没配就是 ``none``。

        语义检索的门槛是显式配置：这里的 ``none`` 是明确原因说明，不是「猜一个」——
        空串也绝不拿插件用主模型去顶。

        Returns:
            ``{"id", "source", "reason"}``；``source`` 取 ``embedding`` 或 ``none``。
        """

        configured = str(getattr(self, "embedding_provider_id", "") or "").strip()
        if not configured:
            return {
                "id": "",
                "source": "none",
                "reason": (
                    "未配置 embedding 供应商（配置项 embedding_provider_id 是空的），"
                    "语义检索关闭"
                ),
            }
        return {"id": configured, "source": "embedding", "reason": ""}

    def get_provider_instance(self, provider_id: str) -> Any | None:
        """按 id 取宿主 Provider 实例（只读）：取不到一律 ``None``，**不抛异常**。

        当心别 await 错对象：``Context.get_provider_by_id``（``star/context.py:440``）
        是**同步**方法，宿主的 ``ProviderManager.get_provider_by_id`` 才是 async。
        """

        wanted = str(provider_id or "").strip()
        if not wanted:
            return None
        context = getattr(self, "context", None)
        getter = getattr(context, "get_provider_by_id", None)
        if not callable(getter):
            return None
        try:
            return getter(wanted)
        except Exception:
            return None

    @staticmethod
    def _embedding_public_info(provider: Any) -> dict[str, str]:
        """embedding 供应商的公开信息：**只抽 id 与显示名**，绝不碰 key / base。"""

        config = getattr(provider, "provider_config", None)
        config = config if isinstance(config, dict) else {}
        provider_id = str(config.get("id") or "").strip()
        if not provider_id:
            try:
                provider_id = str(provider.meta().id or "").strip()
            except Exception:
                provider_id = ""
        if not provider_id:
            return {}
        label = str(config.get("name") or config.get("display_name") or "").strip()
        return {"id": provider_id, "label": label or provider_id}

    # ---------------- B1/WP4：语义检索运行时装配 ----------------

    @staticmethod
    def _embedding_model_name(provider: Any) -> str:
        """宿主供应商的模型名（只读）：常见三个位置，取不到回空串。"""

        for attr in ("model_name", "model"):
            value = str(getattr(provider, attr, "") or "").strip()
            if value:
                return value
        config = getattr(provider, "provider_config", None)
        config = config if isinstance(config, dict) else {}
        return str(config.get("embedding_model") or "").strip()

    def _note_embedding_problem(self, message: str, *, level: str = "warning") -> None:
        """记一条中文日志：装配失败只降级，**绝不让插件启动失败**。

        ``level="info"`` 用于"宿主还没登记供应商"这种启动期时序问题：插件加载比
        供应商登记早，第一轮必然拿不到实例，之后 ``/state`` 会自己重试（见
        :meth:`_embedding_runtime_view`）。这种情况打 WARN 只会让人以为配置坏了。
        """

        logger = getattr(self, "logger", None)
        if logger is None:
            return
        emit = getattr(logger, "info" if level == "info" else "warning", None)
        if not callable(emit):
            return
        try:
            emit(f"[virtual_world] {message}")
        except Exception:
            pass

    def _assemble_embedding_runtime(self) -> tuple[Any, Any]:
        """装配 ``(embedding_runtime, topic_index_queue)``；失败一律 ``(None, None)``。

        装配规则（B1/WP4 §10.8）：

        1. 供应商解析拿不到（``none``）→ 两个注入参数都传 ``None``，
           B1 保持关闭，行为与今天**逐字节相同**；
        2. 拿到了 → ``AstrBotEmbedding(provider, provider_id=<宿主供应商 id>)``；
        3. ``get_dim()`` 拿不到正整数维度 → 回退两个 ``None``（**不许猜维度**：
           猜错会让之后所有相似度都算错）；
        4. ``EmbeddingSpace(provider_id=<宿主 id>, model=<宿主模型名，取不到就用 id>,
           dim=dim)`` → ``TopicIndexQueue(self.db, provider_id=<宿主 id>, space=space)``
           → 两个都返回去注入引擎构造。

        全过程只读：不写库、不发向量化请求、不改配置；任何异常都降级成
        ``(None, None)`` 并记一条中文日志。
        """

        self._embedding_assembly_problem = ""
        self._embedding_adapter = None
        self._embedding_space = None
        self._embedding_model = ""
        self._embedding_dim = 0
        self._embedding_dim_source = ""
        try:
            resolved = self.resolve_embedding_provider_id()
            provider_id = resolved["id"]
            if not provider_id:
                return None, None
            provider = self.get_provider_instance(provider_id)
            adapter = AstrBotEmbedding(provider, provider_id=provider_id)
            if provider is None:
                self._embedding_assembly_problem = (
                    f"配置了 embedding 供应商 {provider_id}，但宿主里取不到它的实例"
                    "（可能已被改名或删除；插件加载早于供应商登记时也会这样，稍后会自动重试），"
                    "语义检索保持关闭"
                )
                # 启动期时序：插件比宿主供应商登记早，这一轮必然拿不到实例
                self._note_embedding_problem(self._embedding_assembly_problem, level="info")
                return None, None
            try:
                dim = int(adapter.get_dim())
            except Exception as exc:
                self._embedding_assembly_problem = (
                    f"问不到 embedding 供应商 {provider_id} 的向量维度（{exc}），"
                    "语义检索保持关闭（不猜维度）"
                )
                self._note_embedding_problem(self._embedding_assembly_problem)
                return None, None
            model = self._embedding_model_name(provider) or provider_id
            space = EmbeddingSpace(provider_id, model, dim)
            queue = TopicIndexQueue(self.db, provider_id=provider_id, space=space)
            self._embedding_adapter = adapter
            self._embedding_space = space
            self._embedding_model = model
            self._embedding_dim = dim
            # 维度是问 get_dim() 来的，还是读供应商配置来的（用户 m26700：宿主不一定实现 get_dim）
            self._embedding_dim_source = str(getattr(adapter, "dim_source", "") or "")
            # 装好就往引擎里推一份：tick 里的批量索引（``_drive_topic_index``）读的是
            # 引擎自己那两个字段，光在这里返回是不够的（见 :meth:`_install_embedding_runtime`）
            self._install_embedding_runtime(adapter, queue)
            return adapter, queue
        except Exception as exc:
            self._embedding_assembly_problem = (
                f"装配语义检索运行时失败（语义检索保持关闭）：{exc}"
            )
            self._note_embedding_problem(self._embedding_assembly_problem)
            return None, None

    def _install_embedding_runtime(self, runtime: Any, queue: Any) -> None:
        """把装配好的运行时装到引擎上（**这一步才是真点亮**）。

        为什么需要它：宿主登记供应商比插件 ``__init__`` 晚。``main.py`` 装配那次
        常常拿到 ``(None, None)``，而引擎构造只收一次这两个参数——不补安装的话，
        ``/embedding/status`` 会（靠 :meth:`_embedding_runtime_view` 当场重试）报
        ``configured=True``，可 tick 里的批量索引一直拿着 ``None`` 空转，
        队列永远停在 pending（真机自检就是这么翻车的）。

        只在引擎已经存在时写；初始化阶段还没造引擎，就等构造注入那一步带走。
        任何异常都吞掉：装不上不该让插件起不来。
        """

        engine = getattr(self, "engine", None)
        if engine is None:
            return
        try:
            engine.embedding_runtime = runtime
            engine.topic_index_queue = queue
        except Exception as exc:
            self._note_embedding_problem(f"把语义检索运行时装到引擎上失败：{exc}")

    def _retry_embedding_assembly(self) -> None:
        """宿主供应商晚挂上时补一次装配（tick 循环每拍问一次，30 秒最多试一次）。

        装配是只读且便宜的（一次 dict 查找 + 问一次维度），但宿主没挂上时
        ``_note_embedding_problem`` 会记日志——所以节流，别把日志刷满。
        装成功就缓存 + 推给引擎，之后这里直接返回。
        """

        if getattr(self, "_embedding_runtime", None) is not None:
            return
        if not self.resolve_embedding_provider_id()["id"]:
            return
        now = time.time()
        if now - float(getattr(self, "_embedding_assembly_retry_at", 0.0) or 0.0) < 30.0:
            return
        self._embedding_assembly_retry_at = now
        runtime, queue = self._assemble_embedding_runtime()
        if runtime is None or queue is None:
            return
        self._embedding_runtime = runtime
        self._topic_index_queue = queue

    def _embedding_runtime_view(self) -> dict[str, Any]:
        """状态 / 清理端点看到的当前装配结果（只读）。

        缓存里没有成功结果时**当场重试一次**：插件 ``__init__`` 时宿主可能还没把
        provider 挂上。失败不缓存，所以重试只可能把成功结果补上；重试成功就顺手
        缓存并推给引擎（:meth:`_install_embedding_runtime`）；未配置时这里
        只看配置，**不做任何实例查找**。
        """

        resolved = self.resolve_embedding_provider_id()
        runtime = getattr(self, "_embedding_runtime", None)
        queue = getattr(self, "_topic_index_queue", None)
        if resolved["id"] and runtime is None:
            runtime, queue = self._assemble_embedding_runtime()
            if runtime is not None and queue is not None:
                # 重试成功就缓存（装配内部已推给引擎）：下一个端点调用不再问一次宿主
                self._embedding_runtime = runtime
                self._topic_index_queue = queue
        return {
            "resolved": resolved,
            "runtime": runtime,
            "queue": queue,
            "problem": str(getattr(self, "_embedding_assembly_problem", "") or ""),
            "space": getattr(self, "_embedding_space", None),
            "model": str(getattr(self, "_embedding_model", "") or ""),
            "dim": int(getattr(self, "_embedding_dim", 0) or 0),
        }

    async def api_agent_runs(self):
        """她调用过哪些工具（Agent 页的只读账）：一行一次 ``tool_receipt``。

        **只收工具调用**：世界里的动作（移动、小睡这类）是她的日常生活，归「日志 /
        事件」页，不在这一页出现（用户 m20782 的纠正）。

        参数：``session``（留空 = 全部会话）、``limit``（1~100，默认 50）、
        ``before``（游标 = 上一页最后一行的 id）、``only_failed``（真值字符串）。

        **只读且不 500**：读不到、参数是垃圾都只是 ``ok=False`` + HTTP 200，
        这一页是观测，出问题不该把编辑器变成一个错误页。
        """

        query = request.query
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(query.get("session", ""))
        limit = max(1, min(100, _positive_int(query.get("limit"), 50)))
        before_id = _positive_int(query.get("before"), 0)
        only_failed = str(query.get("only_failed", "") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        try:
            rows = await self.db.call(
                "list_agent_runs",
                session_id=session_id or None,
                limit=limit,
                before_id=before_id or None,
                only_failed=only_failed,
            )
            items = build_agent_runs(rows)
        except Exception as exc:  # noqa: BLE001 - 统一翻成中文原因
            return json_response(
                {"ok": False, "reason": f"读不到工具调用记录：{exc}", "session": session_id}
            )
        has_more = len(items) >= limit
        return json_response(
            {
                "ok": True,
                "session": session_id,
                "only_failed": only_failed,
                "limit": limit,
                "count": len(items),
                "items": items,
                # 游标只在真可能还有下一页时给，省得前端白跑一次
                "next_cursor": (items[-1]["id"] if items else 0) if has_more else 0,
                "has_more": has_more,
            }
        )

    async def api_embedding_providers(self):
        """可选的 embedding 供应商列表：**只回 id 与显示名**。

        密钥、API base 之类的机密一律不出门——前端只需要知道有哪几家、叫什么。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        context = getattr(self, "context", None)
        getter = getattr(context, "get_all_embedding_providers", None)
        found: list[Any] = []
        if callable(getter):
            try:
                raw = getter()
            except Exception:
                raw = None
            if isinstance(raw, dict):
                found = list(raw.values())
            elif isinstance(raw, (list, tuple)):
                found = list(raw)
        providers: list[dict[str, str]] = []
        seen: set[str] = set()
        for provider in found:
            info = self._embedding_public_info(provider)
            if info and info["id"] not in seen:
                seen.add(info["id"])
                providers.append(info)
        resolved = self.resolve_embedding_provider_id()
        return json_response(
            {
                "providers": providers,
                "count": len(providers),
                "current": resolved["id"],
                "configured": bool(resolved["id"]),
                "reason": "" if providers else "宿主里没有登记任何 embedding 供应商",
            }
        )

    # ---------------- embedding 供应商：编辑器里真选的入口 ----------------

    def _embedding_provider_choices(self) -> list[dict[str, str]]:
        """宿主里登记的全部 embedding 供应商（只抽 id 与显示名）。"""

        context = getattr(self, "context", None)
        getter = getattr(context, "get_all_embedding_providers", None)
        found: list[Any] = []
        if callable(getter):
            try:
                raw = getter()
            except Exception:
                raw = None
            if isinstance(raw, dict):
                found = list(raw.values())
            elif isinstance(raw, (list, tuple)):
                found = list(raw)
        choices: list[dict[str, str]] = []
        seen: set[str] = set()
        for provider in found:
            info = self._embedding_public_info(provider)
            if info and info["id"] not in seen:
                seen.add(info["id"])
                choices.append(info)
        return choices

    async def api_embedding_select(self):
        """在编辑器里换 embedding 供应商：写插件配置，并立即重新装配。

        为什么需要这个端点：宿主插件配置页的「选供应商」下拉只列**聊天**供应商——
        ``AstrBot/dashboard/src/components/shared/ConfigItemRenderer.vue:4`` 把
        ``_special == "select_provider"`` 的 provider-type 写死成 ``chat_completion``
        （只有 stt / tts 有单独分支），embedding 供应商在那一页**根本选不到**
        （用户 2026-10-20 反馈）。编辑器里给一个真能选的入口，配置照样落回插件配置，
        宿主那页显示的值也跟着变。

        换完要重新装配：向量空间指纹里带着供应商 id，换了就是新空间，旧索引按
        "指纹变了"自动重攒（不清谈资本体）。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        wanted = str(payload.get("provider_id") or "").strip()
        choices = self._embedding_provider_choices()
        known = {item["id"] for item in choices}
        if wanted and wanted not in known:
            return json_response(
                {
                    "ok": False,
                    "reason": f"宿主里没有这个 embedding 供应商：{wanted}",
                    "providers": choices,
                },
                status_code=400,
            )
        config = getattr(self, "config", None)
        before = str(getattr(self, "embedding_provider_id", "") or "")
        if config is not None:
            try:
                config["embedding_provider_id"] = wanted
            except Exception as exc:  # noqa: BLE001
                return json_response(
                    {"ok": False, "reason": f"写插件配置失败：{exc}"}, status_code=500
                )
        self.embedding_provider_id = wanted
        saved = False
        save_error = ""
        save = getattr(config, "save_config", None) if config is not None else None
        if callable(save):
            try:
                save()
                saved = True
            except Exception as exc:  # noqa: BLE001
                save_error = str(exc)
        # 重新装配：清掉缓存，让 _assemble_embedding_runtime 按新 id 走一遍
        self._embedding_runtime = None
        self._topic_index_queue = None
        self._embedding_space = None
        self._embedding_dim = 0
        self._embedding_dim_source = ""
        self._embedding_model = ""
        runtime, queue = self._assemble_embedding_runtime()
        self._embedding_runtime = runtime
        self._topic_index_queue = queue
        self._install_embedding_runtime(runtime, queue)
        space = getattr(self, "_embedding_space", None)
        return json_response(
            {
                "ok": True,
                "provider_id": wanted,
                "previous": before,
                "saved": saved,
                "save_error": save_error,
                "configured": bool(wanted),
                "model": str(getattr(self, "_embedding_model", "") or ""),
                "dim": int(getattr(self, "_embedding_dim", 0) or 0),
                "space_fingerprint": str(getattr(space, "fingerprint", "") or ""),
                "reason": "" if runtime is not None else str(
                    getattr(self, "_embedding_assembly_problem", "") or ""
                ),
            }
        )

    async def api_embedding_status(self):
        """检索与索引状态（只读）：配置、向量空间指纹、条数、最近一次错误。

        键名与 B0 **一个都没改**，只是把占位的计数换成了真值：``indexed_count``
        走 ``count_topic_vectors``，``pending_count`` 走索引队列的 ``pending_count()``。
        未配置时 ``configured=False``、两个计数恒 0、不查向量表、**不做任何实例
        查找**；这个端点只读，不写任何东西、也不发消息。字段全用英文小写下划线，
        **任何秘钥类字段都不出现在响应里**。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        view = self._embedding_runtime_view()
        resolved = view["resolved"]
        runtime = view["runtime"]
        queue = view["queue"]
        space = view["space"]
        payload: dict[str, Any] = {
            "configured": bool(resolved["id"]),
            "provider_id": resolved["id"],
            "provider_source": resolved["source"],
            "model": view["model"],
            "dim": view["dim"],
            "space_fingerprint": str(getattr(space, "fingerprint", "") or ""),
            "indexed_count": 0,
            "pending_count": 0,
            "pending_note": "",
            # 最近一次错误：优先队列的（索引真失败才是用户要看的），
            # 其次适配器自己的，最后回落到引擎写在插件上的那条。
            "last_error": "",
            "reason": resolved["reason"] or view["problem"],
        }
        if not resolved["id"]:
            payload["pending_note"] = "未配置 embedding 供应商，语义检索关闭，待索引条数恒为 0"
            return json_response(payload)

        payload["pending_note"] = "语义检索没装起来，待索引条数按 0 报"
        if runtime is None:
            # 配置了但装配不起来（取不到实例 / 问不到维度），原因已经写在 reason 里
            payload["last_error"] = view["problem"]
            return json_response(payload)

        if queue is not None and bool(getattr(queue, "configured", False)):
            try:
                payload["pending_count"] = int(await queue.pending_count())
                payload["pending_note"] = ""
            except Exception as exc:
                payload["pending_note"] = f"取待索引条数失败（按 0 报）：{exc}"

        last_error = ""
        if queue is not None:
            last_error = str(getattr(queue, "last_error", "") or "")
        if not last_error:
            last_error = str(getattr(runtime, "last_error", "") or "")
        if not last_error:
            last_error = str(getattr(self, "embedding_last_error", "") or "")
        payload["last_error"] = last_error

        # 已索引 = **当前这个向量空间里可用的**向量条数（按空间过滤，
        # 换了模型/维度是换空间，旧行不该算进新空间的头）
        try:
            payload["indexed_count"] = int(
                await self.db.call(
                    "count_topic_vectors",
                    provider_id=resolved["id"],
                    space_fingerprint=str(getattr(space, "fingerprint", "") or ""),
                    status="ready",
                )
            )
        except Exception as exc:
            payload["indexed_count"] = 0
            payload["reason"] = payload["reason"] or f"读向量表条数失败（按 0 报）：{exc}"
        return json_response(payload)

    async def api_embedding_index_clear(self):
        """清空语义索引（唯一的写操作）：只清索引队列 + 该 scope 的向量行。

        管理员校验沿用本文件既有的 ``_guard()``（编辑器登录＝管理员）；可选
        ``scope_id``（缺省＝全部会话组，因为 B1 **只按会话组隔离**）。
        **绝不动谈资本体与来源事件**：只调队列的 ``clear``，它内部只碰
        ``topic_index_queue`` 与 ``topic_vectors`` 两张表。
        """

        try:
            payload = await request.json(default={}) or {}
        except Exception:
            payload = {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        scope_raw = str(payload.get("scope_id") or "").strip()
        scope_id = self._scope(scope_raw) if scope_raw else ""
        view = self._embedding_runtime_view()
        resolved = view["resolved"]
        queue = view["queue"]
        reason = ""
        cleared_queue = 0
        cleared_vectors = 0
        if not resolved["id"]:
            reason = "未配置 embedding 供应商，语义检索关闭，没有索引可清"
        elif queue is None:
            reason = view["problem"] or "语义检索没装起来，没有索引可清"
        else:
            # 两张表分开数：queue 只报队列行数，向量行单独报，两个计数才分得清。
            # 队列的 clear() 返回的是「队列行 + 向量行」的和，这里不用它报数。
            fingerprint = str(getattr(view["space"], "fingerprint", "") or "")
            try:
                cleared_queue = int(
                    await self.db.call("clear_topic_index", scope_id=scope_id or None)
                )
                cleared_vectors = int(
                    await self.db.call(
                        "delete_topic_vectors_for_scope",
                        provider_id=resolved["id"],
                        space_fingerprint=fingerprint,
                        scope_id=scope_id,
                    )
                )
            except Exception as exc:
                reason = f"清索引失败：{exc}"
        return json_response(
            {
                "cleared_queue": cleared_queue,
                "cleared_vectors": cleared_vectors,
                "scope_id": scope_id,
                "reason": reason,
            }
        )

    async def api_embedding_index_rescan(self):
        """重建语义索引：把"当前有效、但当前空间里还没有向量"的谈资重新入队。

        这是 ``TopicIndexQueue.rescan`` 的**唯一触发路径**（改动前宿主侧只注册了
        providers / status / index/clear，换 provider、换模型维度、或者早于索引功能建
        的老谈资都永远补不上向量）。可选 ``scope_id`` 缺省＝全部会话组，给了就只重扫
        那一组；可选 ``limit`` 限制一次入队的条数（缺省走队列自己的 ``DEFAULT_RESCAN_LIMIT``），
        想一次扫完就传个大值。

        **渐进不阻塞**：这里只负责把作业行排上，真正的向量由队列后台 tick 算（和素材登记
        、手工谈资同一条路），所以返回的 ``added`` 是"这次排上的条数"，不是"算完的条数"。
        没配 provider / 队列没装起来时**返回 ok + 中文说明、不 500**，前端照常显示。
        """

        try:
            payload = await request.json(default={}) or {}
        except Exception:
            payload = {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        scope_raw = str(payload.get("scope_id") or "").strip()
        scope_id = self._scope(scope_raw) if scope_raw else ""
        view = self._embedding_runtime_view()
        resolved = view["resolved"]
        queue = view["queue"]
        reason = ""
        added = 0
        cleared = 0
        if not resolved["id"]:
            reason = "未配置 embedding 供应商，语义检索关闭，没有索引可重建"
        elif queue is None:
            reason = view["problem"] or "语义检索没装起来，没有索引可重建"
        else:
            limit = payload.get("limit")
            kwargs: dict[str, Any] = {"scope_id": scope_id}
            if limit is not None:
                kwargs["limit"] = limit
            try:
                outcome = await queue.rescan(**kwargs) or {}
                added = int(outcome.get("added") or 0)
                cleared = int(outcome.get("cleared") or 0)
            except Exception as exc:
                reason = f"重建索引失败：{exc}"
        return json_response(
            {
                "ok": True,
                "added": added,
                "cleared": cleared,
                "scope_id": scope_id,
                "reason": reason,
            }
        )

    async def api_embedding_memory_rebuild(self):
        """一键重建记忆向量：清掉"不属于当前向量空间"的旧行，返回清掉的条数。

        记忆的向量是**按轮现算 + 缓存**的（不排队、不落索引），所以"重建"不等于"重新
        算一遍"：

        * 换了 embedding 模型 → 空间指纹变了 → 旧空间的缓存下次召回一律不会命中，留着
          只是占地方；这个端点把它们清掉，下一轮自然按新模型算。
        * 没换模型时它几乎什么都不做（当前空间的行原样保留），所以可以随便点。

        没配 provider 时返回 ok + 中文说明、不 500；``force`` 传 true 时连当前空间的缓存
        一起清（想彻底重算时用）。
        """

        try:
            payload = await request.json(default={}) or {}
        except Exception:
            payload = {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            payload = {}
        force = bool(payload.get("force"))
        view = self._embedding_runtime_view()
        resolved = view["resolved"]
        queue = view["queue"]
        cleared = 0
        remaining = 0
        reason = ""
        if not resolved["id"]:
            reason = "未配置 embedding 供应商，记忆不走向量通道，没有缓存要清"
        else:
            provider_id = str(resolved["id"])
            space = view.get("space")
            fingerprint = str(getattr(space, "fingerprint", "") or "")
            if not fingerprint:
                reason = "向量空间还没定下来（缺空间指纹），这次不清缓存"
            else:
                try:
                    if force:
                        cleared = int(
                            await self.db.call(
                                "delete_memory_vectors_for_space",
                                provider_id=provider_id,
                                space_fingerprint=fingerprint,
                            )
                            or 0
                        )
                    else:
                        cleared = int(
                            await self.db.call(
                                "delete_memory_vectors_not_in_space",
                                provider_id=provider_id,
                                space_fingerprint=fingerprint,
                            )
                            or 0
                        )
                    remaining = int(
                        await self.db.call(
                            "count_memory_vectors", provider_id=provider_id
                        )
                        or 0
                    )
                except Exception as exc:
                    reason = f"清记忆向量缓存失败：{exc}"
        warm: dict[str, Any] = {"started": False, "running": False, "reason": "未配置 embedding 供应商"}
        if resolved["id"] and bool(payload.get("warm", True)):
            engine = getattr(self, "engine", None)
            starter = getattr(engine, "start_memory_warm", None)
            if starter is not None:
                try:
                    warm = starter(limit=int(payload.get("limit") or 0) or 0)
                except Exception as exc:
                    warm = {"started": False, "running": False, "reason": f"预热没起来：{exc}"}
        return json_response(
            {
                "ok": True,
                "cleared": cleared,
                "remaining": remaining,
                "force": force,
                "provider_id": str(resolved["id"]),
                "reason": reason,
                "warm": warm,
            }
        )

    async def api_embedding_memory_warm(self):
        """记忆向量预热进度：按钮点完轮询这一个端点就够了（只读，不触发任何工作）。"""

        engine = getattr(self, "engine", None)
        reader = getattr(engine, "memory_warm_state", None)
        if reader is None:
            return json_response({"ok": True, "running": False, "reason": "引擎还没起来"})
        try:
            state = reader()
        except Exception as exc:
            return json_response({"ok": False, "running": False, "reason": f"读预热进度失败：{exc}"})
        return json_response({"ok": True, **dict(state or {})})

    async def _vision_cache_stats(self) -> dict[str, Any]:
        try:
            stats = await self.db.call("image_cache_stats")
        except Exception:
            stats = {"entries": 0, "hits": 0}
        vision = getattr(self, "vision", None)
        stats["session_hits"] = int(getattr(vision, "hits", 0) or 0)
        return stats

    # ---- A5：保存与生成入口统一（动作的 discovery 派生信息） ----------------
    #
    # 设计口径（`.vwtest/embedding-b-work-arrangement.md` §11.3 A5 那条 + 用户拍板）：
    # 保存世界配置时**只比较影响派生信息的字段**（A3 的 `DISCOVERY_INPUT_FIELDS` 白名单，
    # 25 个权威字段），指纹变了才入队——只改图标 / 排序 / 名字以外的展示字段一律不花钱；
    # 删除 / 停用**立即失效**；批量保存靠队列的唯一键 `(kind, target_id, input_hash)` 去重；
    # 人工锁定的字段由 A3 的 `merge_derived` 兜住（冲突要如实回报，不静默覆盖）。
    #
    # 模型生成的草稿走同一条路：`/generate/actions` 只返回草稿，用户勾选保存之后同样经过
    # 这里入队——两个入口共用一套校验、一份缓存、一个队列（A5 第 5 条要求）。

    ACTION_DISCOVERY_KIND = "action_discovery"

    def _action_discovery_queue(self) -> ConfigIndexQueue:
        """插件自己的配置整理队列（懒建、只有 A5 用；队列内部不起定时器）。

        `db` 给的是 ``AsyncDatabase``（`ConfigIndexQueue` 两种协议都认）。处理器只有
        一个：`action_discovery`，模型槽位走既有的 `consolidate`（**不新增配置项**）。
        """

        queue = getattr(self, "_config_index_queue", None)
        if queue is None:
            queue = ConfigIndexQueue(
                self.db,
                handlers=[self._action_discovery_handler()],
                owner="actions",
            )
            self._config_index_queue = queue
        return queue

    def _action_definition(self, action_id: str) -> dict[str, Any] | None:
        """从**当前配置**里取一条动作的原始定义（找不到 / 停用了回 ``None``）。

        三处都找：用户配置的 `actions`、内置默认、扩展带来的动作。找不到就是
        "这条动作没了"——队列会把在跑的作业作废（`build_input` 返回 None 的语义）。

        **这里返回的是原始 dict**（队列的指纹要用它，因为认领时重读的也是原始 dict，
        两边形状必须一致）；判断"要不要重排"另走 :meth:`_action_view_definition`。
        """

        key = str(action_id or "").strip()
        if not key:
            return None
        try:
            raw = self.store.raw_world()
        except Exception:
            raw = {}
        for item in list((raw or {}).get("actions") or ()):
            if isinstance(item, dict) and str(item.get("id") or "") == key:
                return item if bool(item.get("enabled", True)) else None
        try:
            for item in list(self.extension_host.actions() or ()):
                if isinstance(item, dict) and str(item.get("id") or "") == key:
                    return item
        except Exception:
            pass
        return None

    def _action_view_definition(self, action_id: str) -> Any:
        """同一动作的**校验后定义**（`ActionDef`），给"要不要重排"这一类判断用。

        为什么不能拿原始 dict 判：A3 的 `discovery_input_hash` / `needs_refresh` 只收
        "载荷里出现过的字段"，而校验后的定义会补上默认值（`tool_names=[]` 之类）——
        同一份动作两种形状算出来的指纹**必然不同**，于是刚整理好的动作下次保存又被判
        "需要重排"、`/actions/discovery` 也一直把它列成"缺简述"。判断与视图统一用
        引擎里那份（`world.action_map()`），写回与队列指纹仍用原始 dict。
        """

        key = str(action_id or "").strip()
        if not key:
            return None
        try:
            return self.engine.world.action_map().get(key)
        except Exception:
            return None

    def _action_discovery_handler(self) -> ConfigIndexHandler:
        """`action_discovery` 处理器：入队判据、指纹、锁、生成全在这一处声明。"""

        async def generate(payload, *, kind, target_id, model_slot):
            """调打杂 / 整理模型，产出 A3 允许写的那几个字段（坏输出回 None → 计一次失败）。"""

            definition = self._action_definition(target_id)
            if not definition:
                return None
            prompt = self._action_discovery_prompt(definition, payload)
            reply = await self.engine._ask_consolidate("", prompt[0], prompt[1])
            if not reply:
                return None
            parsed = _parse_action_discovery_reply(reply)
            if not parsed:
                return None
            return {key: value for key, value in parsed.items()
                    if key in action_discovery_policy.ALLOWED_WRITE_FIELDS}

        return ConfigIndexHandler(
            kind=self.ACTION_DISCOVERY_KIND,
            input_fields=tuple(action_discovery_policy.DISCOVERY_INPUT_FIELDS),
            allowed_fields=tuple(action_discovery_policy.ALLOWED_WRITE_FIELDS),
            generator_version=action_discovery_policy.DISCOVERY_GENERATOR_VERSION,
            model_slot="consolidate",
            build_input=self._action_definition,
            generate=generate,
            current_fingerprint=self._action_discovery_fingerprint,
            locked_fields=self._action_discovery_locked_fields,
        )

    def _action_discovery_fingerprint(self, action_id: str) -> str:
        """这条动作现在的权威指纹（排队期间又改过 → 队列把迟到结果丢掉）。

        用**校验后的定义**算：A3 存进 `discovery.input_hash` 的就是这个口径，两边一致
        才比得出来（拿原始 dict 算会永远对不上，见 :meth:`_action_view_definition`）。
        """

        definition = self._action_view_definition(action_id)
        if not definition or not bool(getattr(definition, "enabled", True)):
            # 停用的动作等于"没了"：在跑的作业会被作废，没有"现在的指纹"可言
            return ""
        try:
            return str(action_discovery_policy.discovery_input_hash(definition) or "")
        except Exception:
            return ""

    def _action_discovery_locked_fields(self, action_id: str) -> tuple[str, ...]:
        """人工锁住的字段（交给队列**早点扣掉**，不必等落配置才发现写不进去）。"""

        definition = self._action_view_definition(action_id)
        if not definition:
            return ()
        try:
            return tuple(
                field
                for field in action_discovery_policy.LOCKABLE_FIELDS
                if action_discovery_policy.is_locked(definition, field)
            )
        except Exception:
            return ()

    @staticmethod
    def _action_discovery_prompt(
        definition: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[str, str]:
        """给整理模型的两段话（纯字符串，不含密钥；坏输入也不抛）。

        索引文本要说清"她要能一眼判断这条动作现在能不能用"，所以**代码先算出硬约束**
        （`system_constraints`：停用 / 地点 / 工具 / 指令 / 时长 / 状态槽…），模型只负责
        把它写成人话——不让模型去猜约束，是 A3 §"无法安全简述"口径的地基。
        """

        from .core.action_discovery import system_constraints

        constraints = []
        try:
            constraints = list(system_constraints(definition) or ())
        except Exception:
            constraints = []
        system = (
            "你在给一个虚拟世界角色的动作清单写简述。只输出一个 JSON 对象，不要解释。\n"
            "字段：summary（一句话，≤40 字，说明这个动作是做什么的）、"
            "when_to_use（什么时候用得上，一句话）、"
            "key_constraints（字符串数组，最多 3 条，必须是给定的硬约束里的人话说法）、"
            "tags（字符串数组，最多 4 个短词）、"
            "detail_policy（只能是 auto / always / on_demand 三选一）。\n"
            "禁止编造硬约束里没有的限制；不许把约束写反；拿不准就少写。"
        )
        lines = [f"动作 id：{definition.get('id') or ''}", f"名字：{definition.get('name') or ''}"]
        desc = " ".join(str(definition.get("description") or "").split())
        if desc:
            lines.append(f"完整说明：{desc[:600]}")
        if constraints:
            lines.append("硬约束（只能照这些写，不能加别的）：" + "；".join(constraints[:8]))
        inputs = {
            key: payload.get(key)
            for key in ("category", "llm_level", "scope", "target_type", "duration")
            if payload.get(key) not in (None, "", [], {})
        }
        if inputs:
            lines.append("其它字段：" + json.dumps(inputs, ensure_ascii=False))
        return system, "\n".join(lines)

    async def _sync_action_discovery(
        self, before: dict[str, Any], after: dict[str, Any]
    ) -> dict[str, int]:
        """保存世界配置之后：把"派生信息该重算"的动作排队、把没了/停用的立即作废。

        ``before`` / ``after`` 是**保存前后**的原始世界配置（`store.raw_world()` 那种 dict）。
        返回 ``{'queued': n, 'invalidated': m, 'skipped': k}``——调用方把它当人话报回去。

        两段式：**先纯比较算出"要做什么"**（只改图标 / 排序的那种保存连队列都不建、
        一次库调用都不发），再执行。**任何失败都只记日志**：保存本身绝不该因为整理队列
        失败而失败（用户点了保存就必须保存成功）。
        """

        outcome = {"queued": 0, "invalidated": 0, "skipped": 0}

        def index(world: dict[str, Any]) -> dict[str, dict[str, Any]]:
            found: dict[str, dict[str, Any]] = {}
            for item in list((world or {}).get("actions") or ()):
                if isinstance(item, dict) and str(item.get("id") or ""):
                    found[str(item["id"])] = item
            return found

        was, now = index(before), index(after)
        to_queue: list[str] = []
        to_drop: list[str] = []
        for action_id, item in now.items():
            if not bool(item.get("enabled", True)):
                # 停用 = 立刻失效（不是等过期）；从未排过队的自然什么都不用做
                if was.get(action_id) is not None:
                    to_drop.append(action_id)
                outcome["skipped"] += 1
                continue
            previous = was.get(action_id)
            # 只比较影响派生信息的字段（A3 的 25 个权威字段）：指纹没变就不入队。
            # **两份原始 dict 都先校成定义再比**：A3 的指纹只收"载荷里出现过的字段"，
            # 同一个动作拿原始 dict 和拿校验后的定义算出来的指纹必然不同（校验会补默认值），
            # 混着用会让刚整理好的动作下次保存又被判"要重排"（A6 的接线报告里踩过）。
            previous_def = _validated_action(previous) if previous is not None else None
            current_def = _validated_action(item)
            try:
                changed = (
                    previous is None
                    or previous_def is None
                    or current_def is None
                    or action_discovery_policy.discovery_input_hash(previous_def)
                    != action_discovery_policy.discovery_input_hash(current_def)
                )
            except Exception:
                changed = True
            if not changed:
                outcome["skipped"] += 1
                continue
            try:
                needs = action_discovery_policy.needs_refresh(
                    current_def if current_def is not None else item,
                    generator_version=action_discovery_policy.DISCOVERY_GENERATOR_VERSION,
                )
            except Exception:
                needs = True
            if not needs:
                outcome["skipped"] += 1
                continue
            to_queue.append(action_id)
        for action_id in was:
            if action_id not in now:
                to_drop.append(action_id)
        if not to_queue and not to_drop:
            return outcome

        try:
            queue = self._action_discovery_queue()
        except Exception as exc:
            self.logger.warning(f"动作整理队列建不出来（保存照常）：{exc}")
            return outcome
        for action_id in to_drop:
            try:
                outcome["invalidated"] += int(
                    await queue.invalidate(
                        kind=self.ACTION_DISCOVERY_KIND, target_id=action_id
                    )
                    or 0
                )
            except Exception as exc:
                self.logger.warning(f"作废动作整理作业失败（{action_id}）：{exc}")
        for action_id in to_queue:
            item = now.get(action_id) or {}
            try:
                # 唯一键 (kind, target_id, input_hash) 天然去重：批量保存/重复保存不会排两次
                queued = await queue.enqueue(
                    self.ACTION_DISCOVERY_KIND,
                    action_id,
                    payload=item,
                    source_revision=str(item.get("source_revision") or ""),
                )
            except Exception as exc:
                self.logger.warning(f"动作整理入队失败（{action_id}）：{exc}")
                continue
            outcome["queued"] += 1 if queued else 0
        return outcome

    async def api_action_discovery_status(self):
        """`GET /actions/discovery`：谁还缺派生信息、队列里压了多少。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(await self._action_discovery_report())

    def _action_sources(self) -> dict[str, str]:
        """每个动作**是谁给的**：world（自己配的）/ builtin（内置回退）/ extension（扩展包）。

        为什么不问运行时那一份：运行时视图里三种来源已经混在一起了，只看它永远解释不了
        "为什么总数对不上"（复核方案 §10.10 / CNT01）。这里以**原始配置 + 扩展清单**为底，
        再补上"只在运行时存在、配置里没有"的特殊动作。
        """

        sources: dict[str, str] = {}
        try:
            raw = self.store.raw_world() or {}
        except Exception:
            raw = {}
        for item in list(raw.get("actions") or ()):
            if isinstance(item, dict) and item.get("id"):
                sources[str(item["id"])] = "builtin" if item.get("builtin") else "world"
        try:
            for item in self.engine.extensions.actions():
                action_id = str(item.get("id") or "")
                if action_id:
                    # 撞名的以配置为准：扩展不该悄悄盖掉用户自己那份
                    sources.setdefault(action_id, "extension")
        except Exception:
            pass
        try:
            for action_id in (self.engine.world.action_map() or {}):
                sources.setdefault(str(action_id), "runtime")
        except Exception:
            pass
        return sources

    async def _action_discovery_report(self) -> dict[str, Any]:
        """缺失补齐清单 + **来源/状态对账**（只读，坏输入也不抛）。

        ``total`` 与 ``ready + missing`` 一定对得上；而"按来源拆分"的计数是**另一根轴**，
        两者不该相加——报告里写清这一点，才不会又出现"报告说 73、界面说 70"这种对不上的账。
        """

        report: dict[str, Any] = {
            "ok": True,
            "kind": self.ACTION_DISCOVERY_KIND,
            "generator_version": action_discovery_policy.DISCOVERY_GENERATOR_VERSION,
            "total": 0,
            "ready": 0,
            "missing": [],
            "locked": 0,
            "queue": {},
            "note": "",
            # —— P0-2 新增：来源/状态对账 ——
            # 来源轴只数**启用的**（total == sum(sources) 才对得上）；全部另给 sources_all。
            "sources": {"world": 0, "builtin": 0, "extension": 0, "runtime": 0},
            "sources_all": {"world": 0, "builtin": 0, "extension": 0, "runtime": 0},
            "total_all": 0,
            "enabled": 0,
            "disabled": 0,
            "missing_or_stale": 0,
            "unsupported_or_readonly": 0,
            "source_of": {},
            "overlaps": (
                "来源计数与 ready/missing 是两根轴：同一个动作既算在某个来源里，也算在 ready 或 missing 里，"
                "所以两边不相加。"
            ),
        }
        sources = self._action_sources()
        # 一律走**校验后的定义**（`world.action_map()`）：指纹与 needs_refresh 的口径
        # 与 A3 落盘时一致，否则刚整理好的动作会被永远列成"缺简述"。
        views: dict[str, Any] = {}
        try:
            views = dict(self.engine.world.action_map() or {})
        except Exception:
            views = {}
        # 全部（含停用）先按来源摊开：raw 里停用的动作可能根本不在 action_map 里
        for action_id, source in sources.items():
            if source in report["sources_all"]:
                report["sources_all"][source] += 1
        report["total_all"] = len(sources)
        for action_id, view in views.items():
            source = sources.get(str(action_id), "runtime")
            report["source_of"][str(action_id)] = source
            if not bool(getattr(view, "enabled", True)):
                continue
            report["enabled"] += 1
            report["total"] += 1
            if source in report["sources"]:
                report["sources"][source] += 1
            discovery = action_discovery_policy.discovery_of(view)
            if bool(getattr(discovery, "locked", False)):
                report["locked"] += 1
            try:
                needs = action_discovery_policy.needs_refresh(
                    view, generator_version=action_discovery_policy.DISCOVERY_GENERATOR_VERSION
                )
            except Exception:
                needs = True
            if not needs:
                report["ready"] += 1
                continue
            # 扩展提供的动作**写不回扩展源**：它是只读的，只能用派生存储（这里照实标出来）
            readonly = source == "extension"
            if readonly:
                report["unsupported_or_readonly"] += 1
            summary, needs_expand = action_discovery_policy.fallback_summary(view)
            report["missing"].append(
                {
                    "id": str(action_id),
                    "name": str(getattr(view, "name", "") or ""),
                    "status": str(getattr(discovery, "status", "") or "missing"),
                    "reason": (
                        "扩展提供的动作，源文件不可写（用派生存储）"
                        if readonly
                        else str(getattr(discovery, "status", "") or "missing")
                    ),
                    "source": source,
                    "readonly": readonly,
                    "fallback_summary": summary,
                    "needs_expand": bool(needs_expand),
                    "input_hash": self._action_discovery_fingerprint(str(action_id)),
                }
            )
        report["missing_or_stale"] = len(report["missing"])
        # 停用 = 全部 - 启用（扩展与运行时也含在内）：这样"配置里 76、报告 73"能解释了
        report["disabled"] = max(0, int(report["total_all"]) - int(report["enabled"]))
        try:
            queue = self._action_discovery_queue()
            report["queue"] = {
                "pending": int(await queue.pending_count(kind=self.ACTION_DISCOVERY_KIND) or 0),
                "last_error": str(getattr(queue, "last_error", "") or ""),
                "dropped_fields": list(getattr(queue, "dropped_fields", ()) or ()),
            }
        except Exception as exc:
            report["queue"] = {"pending": 0, "last_error": f"队列读不出来：{exc}"}
        # 自动整理的状态（用户 m29086）：页面要能说清"它自己什么时候会整理"，
        # 而不是只留一个要人点的按钮。
        try:
            report["auto_tidy"] = self.engine.auto_tidy_section()
        except Exception as exc:
            report["auto_tidy"] = {"enabled": False, "note": f"读不出来：{exc}"}
        if report["missing"]:
            report["note"] = (
                f"{len(report['missing'])} 个动作还没有可用的简述"
                "（聊天里她照样能做这些动作，只是清单里写得长一点）"
            )
        elif not report["total"]:
            report["note"] = "还没有任何动作"
        else:
            report["note"] = "所有动作都有简述了"
        return report

    async def api_action_discovery_rebuild(self):
        """`POST /actions/discovery/rebuild`：批量重整理（可只给 `ids`）。

        ``mode``：
        - ``fill``（默认）：**补缺**——已经有同样作业在队列里就算重复跳过，不重复花模型钱；
        - ``force``：**强制重算**——已经整理好（ready 且指纹对得上）的也重排一遍。

        返回逐类计数（P0-2 / CNT01），别再用一个 ``queued`` 把"停用跳过 / 只读 / 重复"混在一起：
        用户看见的就是「本次新增排队 N / 重复跳过 M / 停用 K / 扩展只读 R」。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        wanted = payload.get("ids")
        ids = [str(item) for item in wanted] if isinstance(wanted, list) else []
        mode = "force" if str(payload.get("mode") or "").lower() == "force" else "fill"
        queue = self._action_discovery_queue()
        queued = 0
        skipped = 0
        disabled = 0
        readonly = 0
        not_found = 0
        sources = self._action_sources()
        try:
            raw = self.store.raw_world()
        except Exception:
            raw = {}
        candidates = [item for item in list((raw or {}).get("actions") or ()) if isinstance(item, dict)]
        seen: set[str] = set()
        for item in candidates:
            action_id = str(item.get("id") or "")
            if not action_id:
                continue
            if ids and action_id not in ids:
                continue
            seen.add(action_id)
            if not bool(item.get("enabled", True)):
                disabled += 1
                continue
            if await queue.enqueue(
                self.ACTION_DISCOVERY_KIND,
                action_id,
                payload=item,
                source_revision=str(item.get("source_revision") or ""),
                force=mode == "force",
            ):
                queued += 1
            else:
                skipped += 1
        # 只想重排某几个，但那几个只在扩展里 / 运行时才有：说清楚，不装作"已完成"
        for action_id in ids:
            if action_id in seen:
                continue
            source = sources.get(action_id, "")
            if source == "extension":
                readonly += 1
            elif not source:
                not_found += 1
        bits = [f"本次新增排队 {queued} 个"]
        if skipped:
            bits.append(f"重复跳过 {skipped} 个")
        if disabled:
            bits.append(f"停用跳过 {disabled} 个")
        if readonly:
            bits.append(f"扩展只读 {readonly} 个（源文件不可写）")
        if not_found:
            bits.append(f"没有这个动作 {not_found} 个")
        return json_response(
            {
                "ok": True,
                "mode": mode,
                "queued": queued,
                "skipped": skipped,
                "disabled": disabled,
                "readonly": readonly,
                "not_found": not_found,
                "note": "，".join(bits) if (queued or skipped or disabled or readonly) else "没有需要重排的动作",
            }
        )

    async def api_action_discovery_tidy(self):
        """`POST /actions/discovery/tidy`：**现在扫一次**（用户 m29086 的「立即整理一次」）。

        平时它自己会扫（睡着每 5 分钟、醒着每小时），这个接口只是让人不用等：
        扫完把结果交回去——扫了几条、缺几条、排了几条进队列。**全都有简述时
        一条都不排、也不调模型**，所以点它不会白花钱。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        try:
            outcome = await self.engine.auto_tidy_actions(force=True)
        except Exception as exc:
            return json_response({"ok": False, "error": f"扫不动：{exc}"}, status_code=500)
        return json_response({"ok": True, **outcome, "auto_tidy": self.engine.auto_tidy_section()})

    async def api_action_discovery_preview(self):
        """`POST /actions/discovery/preview`：**预览差异**（不改任何东西）。

        预览的是"代码现在能自己算出来什么"：每个待整理动作给出**回退简述**（不花钱的那份）
        与当前已存的简述、以及它为什么会过期（指纹对不上 / 整理器版本变了 / 上次失败）。
        真正的模型产出要在重整理之后才看得到——这一步只是让用户先知道"会动哪些"。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        report = await self._action_discovery_report()
        rows: list[dict[str, Any]] = []
        for item in report.get("missing") or []:
            action_id = str(item.get("id") or "")
            definition = self._action_definition(action_id) or {}
            discovery = action_discovery_policy.discovery_of(definition)
            rows.append(
                {
                    "id": action_id,
                    "name": item.get("name") or "",
                    "before_summary": str(getattr(discovery, "summary", "") or ""),
                    "before_when": str(getattr(discovery, "when_to_use", "") or ""),
                    "after_summary": item.get("fallback_summary") or "",
                    "reason": _action_discovery_reason(definition),
                    "needs_expand": bool(item.get("needs_expand")),
                }
            )
        return json_response(
            {
                "ok": True,
                "count": len(rows),
                "rows": rows,
                "note": (
                    "这些动作整理后会换一段更短的简述；现在列的是代码能算出来的那份，"
                    "真正的模型产出在重整理之后生效"
                    if rows
                    else "没有需要整理的动作"
                ),
            }
        )

    #: 「恢复默认」能单独重置的几块（用户 m08826：新版默认值老用户享受不到）。
    RESET_SECTIONS: dict[str, tuple[str, ...]] = {
        "persona": ("persona", "gender", "bot_name"),
        "memory": ("memory", "persons", "relationships", "profile"),
        "topics": ("topics",),
        "meme": ("meme",),
        "actions": ("actions",),
        "basic": (
            "limits",
            "context",
            "media",
            "engagement",
            "state_dynamics",
            "tool_result_reply",
            "remote_action_travel",
            "echo_types",
        ),
        "agent": ("agent",),
    }

    async def api_reset_config(self):
        """把勾中的那几块配置恢复成默认值，没勾的原样保留。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        wanted = payload.get("sections")
        if not isinstance(wanted, list) or not wanted:
            return error_response("sections 要给出要恢复的那几块")
        picked = [str(item).strip() for item in wanted if str(item).strip()]
        unknown = [item for item in picked if item not in self.RESET_SECTIONS]
        if unknown:
            return error_response(f"认不出这几块：{'、'.join(unknown)}")
        try:
            raw = dict(self.store.raw_world() or {})
        except Exception:
            raw = {}
        # DEFAULT_WORLD 里只有一部分键（limits/context/memory 这些走模型默认值），
        # 所以先拿 DEFAULT_WORLD，缺的键再从 WorldConfig 的默认值补。
        model_defaults = WorldConfig().model_dump(mode="json", by_alias=True)
        fresh = json.loads(json.dumps(DEFAULT_WORLD))
        for key, value in model_defaults.items():
            fresh.setdefault(key, value)
        touched: dict[str, list[str]] = {}
        for name in picked:
            keys = self.RESET_SECTIONS[name]
            for key in keys:
                if key in fresh:
                    raw[key] = fresh[key]
                    touched.setdefault(name, []).append(key)
        try:
            warnings = self.store.save_world(raw)
        except ValidationError as exc:
            return self._save_error("世界配置", exc)
        self.engine.reload_config()
        return json_response(
            {
                "ok": True,
                "applied": picked,
                "keys": touched,
                "warnings": warnings,
                "note": "只重置了勾中的几块；人设、记忆、谈资没勾的都没动。",
            }
        )

    async def api_put_world(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        world = payload.get("world")
        if not isinstance(world, dict):
            return error_response("world 必须是对象")
        world = self._without_extension_actions(world)
        try:
            before = self.store.raw_world()
        except Exception:
            before = {}
        try:
            warnings = self.store.save_world(world)
        except ValidationError as exc:
            return self._save_error("世界配置", exc)
        self.engine.reload_config()
        # A5：保存之后顺手把"派生信息该重算"的动作排进整理队列（只比较 25 个权威字段，
        # 只改图标/排序不入队）。失败只记日志——保存本身已经成功了。
        try:
            counts = await self._sync_action_discovery(before, world)
        except Exception as exc:
            self.logger.warning(f"动作整理入队失败（保存已完成）：{exc}")
            counts = {}
        if counts.get("queued"):
            warnings = list(warnings or []) + [
                f"有 {int(counts['queued'])} 个动作的说明要重新整理（后台慢慢做，不影响她用）"
            ]
        return json_response({"ok": True, "warnings": warnings, "discovery": counts})

    def _without_extension_actions(self, world: dict[str, Any]) -> dict[str, Any]:
        """存世界配置前把扩展带来的动作摘掉。

        这些动作是扩展在加载时提供的，不属于用户的配置：留着的话，
        扩展一旦停用 / 卸载，它们就会变成一堆没人认领、也删不干净的动作。
        """

        owned = self.extension_host.owned_actions()
        actions = world.get("actions")
        if not owned or not isinstance(actions, list):
            return world
        kept = [
            item
            for item in actions
            if not (isinstance(item, dict) and str(item.get("id") or "") in owned)
        ]
        if len(kept) == len(actions):
            return world
        return {**world, "actions": kept}

    async def api_schedule_repeat_preview(self):
        """编辑器改频率时先问一句：这套配置到底会翻译成什么时候触发。

        前端只能猜「每天 08:30」这一层；「工作日」「每 90 分钟」「跨午夜」各有边界，
        所以 cron 表达式与「下一次」都由后端用同一套模型算（见 ``models.repeat_preview``）。
        参数不对返回 ``ok: false`` + 中文原因，不抛 500。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload, dict):
            return error_response("频率必须是对象")
        try:
            result = repeat_preview(payload)
        except Exception as exc:  # 模型或 cron 里没想到的错，也不能 500
            return error_response(f"频率预览失败：{exc}")
        # ok 与否都走 200：这不是请求失败，是「你写的这个频率对不对」的答案
        return json_response(result)

    async def api_put_schedules(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        schedules = payload.get("schedules")
        if not isinstance(schedules, dict):
            return error_response("schedules 必须是对象")
        # 工具 / 指令型步骤缺「想干什么」时，让内容生成模型补一句写进配置：
        # 运行时就不用再猜，也不会因为缺意图被跳过。
        schedules, filled = await self.engine.fill_schedule_intents(schedules)
        # 落点归一化：旧存档里「会话组 + 具体会话」混着存的，保存前统一成一种，
        # 并把"我替你按什么存了"作为人话提示交回编辑器（不静默丢用户的选择）。
        schedules, landing_warnings = self.engine.normalize_schedule_sessions(schedules)
        try:
            warnings = self.store.save_schedules(schedules)
        except ValidationError as exc:
            return self._save_error("日程", exc)
        self.engine.reload_config()
        return json_response(
            {
                "ok": True,
                "warnings": [*landing_warnings, *warnings],
                "filled": filled,
                "schedules": schedules,
            }
        )

    async def api_put_sessions(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            return error_response("sessions 必须是对象")
        before = set(self.engine.enabled_session_ids())
        try:
            warnings = self.store.save_sessions(sessions)
        except ValidationError as exc:
            return self._save_error("会话列表", exc)
        self.engine.reload_config()
        # 这次新启用的会话：如果临时会话被 ended / expired 掐着，跟着复位成 active
        # （审查 H2：不能留下"启用着但消息被吞"的状态）
        newly_enabled = [
            umo for umo in self.engine.enabled_session_ids() if umo not in before
        ]
        if newly_enabled:
            await self._revive_temp_sessions(newly_enabled)
        return json_response({"ok": True, "warnings": warnings})

    async def api_temp_sessions(self):
        """临时会话列表（用户 m00001 需求 12）：活跃 / 已结束 / 已归档。

        只读一张表：``state`` 决定分到哪一栏，``archived`` 决定要不要默认收起来。
        摘要与来源都在行里，编辑器不再二次查询。
        """

        try:
            rows = list(
                await self.engine.db.call("list_temp_sessions", limit=1000) or []
            )
        except Exception as exc:  # noqa: BLE001 - 打不开就回空表，别让页面报错
            self.logger.debug(f"[virtual_world] 临时会话列表没读出来：{exc}")
            rows = []
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                item = dict(row)
            except Exception:  # noqa: BLE001
                continue
            items.append(
                {
                    "umo": str(item.get("umo") or ""),
                    "origin": str(item.get("origin") or ""),
                    "source": str(item.get("source") or "private"),
                    "state": str(item.get("state") or "ended"),
                    "created_at": int(item.get("created_at") or 0),
                    "expires_at": int(item.get("expires_at") or 0),
                    "rounds": int(item.get("rounds") or 0),
                    "ended_at": int(item.get("ended_at") or 0),
                    "end_reason": str(item.get("end_reason") or ""),
                    "summary": str(item.get("summary") or ""),
                    "relation_at_end": str(item.get("relation_at_end") or ""),
                    "archived": bool(int(item.get("archived") or 0)),
                }
            )
        cfg = getattr(self.engine.world, "temp_session", None)
        return json_response(
            {
                "ok": True,
                "items": items,
                "config": {
                    "enabled": bool(getattr(cfg, "enabled", True)),
                    "ttl_minutes": int(getattr(cfg, "ttl_minutes", 30) or 0),
                    "max_rounds": int(getattr(cfg, "max_rounds", 20) or 0),
                    "familiar_keep": bool(getattr(cfg, "familiar_keep", True)),
                    "familiar_level": int(getattr(cfg, "familiar_level", 4) or 0),
                    "group_temp": bool(getattr(cfg, "group_temp", True)),
                },
            }
        )

    async def api_temp_session_archive(self):
        """归档 / 恢复一条临时会话（body: ``umo``、``archived``）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        umo = str(payload.get("umo") or "").strip()
        if not umo:
            return error_response("umo 不能为空")
        archived = bool(payload.get("archived", True))
        try:
            changed = await self.engine.db.call(
                "set_temp_session_archived", umo=umo, archived=archived
            )
        except Exception as exc:  # noqa: BLE001
            return error_response(f"归档没写进去：{exc}")
        if not changed:
            return error_response("没有这条临时会话")
        revived = 0
        if not archived:
            # 「恢复」不只是把 archived 抹掉（审查 H2-3）：ended / expired 的行还得
            # 清 state、把白名单那一行重新启用，否则恢复完照样被吞。
            row = await self.engine.db.call("get_temp_session", umo=umo) or {}
            if str(row.get("state") or "") in ("ended", "expired"):
                self.store.add_session(
                    umo,
                    session_type="private",
                    temporary=True,
                    # 来源照原样带过去：空串 = 不知道（用户 m00001 追加需求）。
                    # 别折回 umo 自己——"自己指自己"是历史写法，会被当成无效来源。
                    temp_origin=str(row.get("origin") or ""),
                    temp_source=str(row.get("source") or "private"),
                    snapshot=True,
                )
                self.engine.reload_config()
                revived = await self._revive_temp_sessions([umo])
        # 内存镜像跟着改，页面不用等下一次热加载
        row = self.engine.temp_sessions.get(umo)
        if row is not None:
            row["archived"] = 1 if archived else 0
        return json_response(
            {
                "ok": True,
                "umo": umo,
                "archived": archived,
                "state": str(
                    (await self.engine.db.call("get_temp_session", umo=umo) or {}).get(
                        "state"
                    )
                    or ""
                ),
                "revived": revived,
            }
        )

    async def api_temp_session_release(self):
        """解除接管（审查 H2 / 需求 12）：body ``umo``。

        语义：这条临时会话置 ``released``、白名单里的 ``enabled`` 关掉并摘掉临时
        标记。之后这个 umo 的消息不再被她吞，回到宿主默认路由（其它插件 / 默认
        LLM 照常接）。摘要与轮数留着给编辑器看。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        umo = str(payload.get("umo") or "").strip()
        if not umo:
            return error_response("umo 不能为空")
        try:
            ok = await self.engine.release_temp_session(umo)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"解除接管没写进去：{exc}")
        if not ok:
            return error_response("没有这条临时会话")
        return json_response({"ok": True, "umo": umo, "state": "released"})

    @staticmethod
    def _save_error(label: str, exc: Exception):
        """保存没过校验：给一句人话 + 具体是哪一项，别把英文校验串直接丢给用户。"""

        problems: list[str] = []
        try:
            for item in exc.errors():  # type: ignore[attr-defined]
                where = " / ".join(str(part) for part in (item.get("loc") or []))
                message = str(item.get("msg") or "").strip()
                problems.append(f"{where}：{message}" if where else message)
        except Exception:
            problems = []
        detail = "；".join(part for part in problems if part)[:500]
        return error_response(
            f"{label}没通过校验，这次没有保存（改动还在编辑器里，改完再存一次）。"
            + (f"\n{detail}" if detail else f"\n{exc}")
        )

    async def api_generate_actions(self):
        """批量生成动作草稿（只返回草稿，用户在编辑器里勾选后才保存）。

        传 ``node`` 就是"只给这个地点生成"，否则按 ``zone`` 里的每个地点生成。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        zone_id = str(payload.get("zone", "") or "").strip()
        node_id = str(payload.get("node", "") or "").strip()
        if not zone_id and node_id:
            zone_id = self.engine.world.zone_of(node_id)
        if not zone_id:
            return error_response("缺少 zone 参数")
        result = await self.engine.generate_actions_for_zone(
            zone_id,
            payload.get("per_node"),
            node_ids=[node_id] if node_id else None,
        )
        return json_response(result)

    async def api_generate_nodes(self):
        """按区域批量生成地点草稿（含自动摆位与自动连线）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        zone_id = str(payload.get("zone", "") or "").strip()
        if not zone_id:
            return error_response("缺少 zone 参数")
        result = await self.engine.generate_nodes_for_zone(
            zone_id, payload.get("count")
        )
        return json_response(result)

    async def api_tools(self):
        guard = self._guard()
        if guard is not None:
            return guard
        tools = self.engine.available_tools()
        schemas = self.engine.tool_schemas()
        sources = self.engine.tool_sources()
        plugins = self.engine.tool_owners()
        return json_response(
            {
                "tools": [
                    {
                        "name": name,
                        "description": desc,
                        "parameters": schemas.get(name, {}),
                        "param_text": self.engine.tool_param_text(name),
                        "source": sources.get(name, "plugin"),
                        "plugin": plugins.get(name, ""),
                    }
                    for name, desc in tools.items()
                ],
                "global_allowed": self.engine.world.global_allowed_tools,
            }
        )

    async def api_reload(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        warnings = self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

    async def api_restore(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        scope = str(payload.get("scope", "all"))
        restored = self.store.restore_default(scope)
        self.engine.reload_config()
        return json_response({"ok": True, "restored": restored})

    async def api_state(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        if not session_id:
            return error_response("缺少 session 参数")
        snapshot = await self.engine.snapshot(session_id)
        if self._coverage_panel_enabled():
            # 阶段A3：面板上那四类发言捕获的计数（谁在这个会话里说过话、我们怎么知道的）。
            try:
                snapshot["capture"] = self.engine.capture_snapshot(
                    session_id=session_id
                )
            except Exception:  # pragma: no cover - 面板数据不该影响状态接口
                pass
        # 收图失败流水（2026-10-20）：她"看不见图"的时候，用户得有个地方看是哪一步
        # 掉的（地址失效 / 超了这一轮上限 / 超时 / 入库失败），别只给她一句兜底话。
        try:
            snapshot["media_cache"] = self.engine.recent_media_cache_failures(limit=10)
        except Exception:  # pragma: no cover
            pass
        # 她手上那件事（用户 m10183）：页面要能看出"正在跑"以及"收到叫停/改口没有"，
        # 否则用户说了「算了」只能靠猜。
        try:
            agent = snapshot.get("agent")
            if isinstance(agent, dict):
                agent["busy"] = self.engine.agent_task_busy(session_id)
                agent["control"] = self.engine.agent_task_control(session_id)
        except Exception:  # pragma: no cover
            pass
        return json_response(snapshot)

    def _coverage_panel_enabled(self) -> bool:
        """``capture.coverage_panel``（默认开）。认不出的配置一律当开。"""

        try:
            section = self.store.raw_world().get("capture") or {}
        except Exception:  # pragma: no cover
            return True
        if not isinstance(section, dict):
            return True
        value = section.get("coverage_panel", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    async def api_state_action(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        action = str(payload.get("action", "") or "")
        if not action:
            return error_response("缺少 action")
        if action == "refresh_weather":
            # 天气本身不分会话，但「查天气」配成指令型时要借一条真实消息当上下文，
            # 所以前端选中的那条会话优先，没带就按白名单第一条
            note = await self.engine.maybe_refresh_weather(
                force=True, session_id=self._scope(payload.get("session"))
            )
            return json_response(
                {
                    "ok": True,
                    "note": note,
                    "weather": await self.engine.weather_payload(),
                }
            )
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        if action == "wake":
            try:
                was = await asyncio.wait_for(
                    self.engine.wake_up(session_id), timeout=STATE_ACTION_TIMEOUT
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "她正在忙，稍等一下再叫醒"}
                )
            return json_response({"ok": True, "was_sleeping": was})
        if action == "interrupt":
            # 先发取消信号（**不拿世界状态锁**），再尽量清一次状态：慢工具正握着锁时，
            # 旧写法只能等到超时、回一句"她正在忙，稍等一下再打断"——那不是打断成功，
            # 用户看到的就是按钮点了没用。现在这条路会去停正在等的那一步。
            result = await self.engine.interrupt_with_cancel(session_id, force=True)
            return json_response(result)
        if action == "reset_tools":
            # 手动解除工具熔断（状态页上的「立即重试」）
            name = str(payload.get("tool") or "").strip()
            cleared = self.engine.reset_tool_breakers(name)
            return json_response({"ok": True, "cleared": cleared})
        if action == "event":
            # 编辑器上「给她来件事」：等于用户投递一个事件
            note = await self.engine.submit_event(
                session_id, str(payload.get("text") or "")
            )
            return json_response(
                {
                    "ok": True,
                    "note": note,
                    "events": await self.engine.event_overview(session_id),
                }
            )
        if action in ("advance_event", "close_event"):
            # 编辑器上「立即推进一幕」/「立刻完结」：手动改事件节奏
            if self.engine.is_busy(session_id):
                return json_response(
                    {
                        "ok": False,
                        "note": "她正在忙（动作或检索还没结束），等这一轮跑完再点；"
                        "要立刻停就点「中断动作」",
                    }
                )
            thread_id = str(payload.get("thread") or "")
            handler = (
                self.engine.advance_thread
                if action == "advance_event"
                else self.engine.close_thread
            )
            try:
                note = await asyncio.wait_for(
                    handler(session_id, thread_id), timeout=STATE_ACTION_TIMEOUT
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "这件事还在演（模型还没回），稍等一下再看"}
                )
            return json_response(
                {
                    "ok": True,
                    "note": note,
                    "events": await self.engine.event_overview(session_id),
                }
            )
        if action == "tick":
            # 连点几下不能攒成一串 tick：同一会话同一时刻只放行一次推进，
            # 后来的这几次直接忽略（前端也有同样的防护，这里是兜底）。
            # 推进是**全局**的（一次 tick 走所有会话），所以别按会话判重。
            if self._advancing:
                return json_response(
                    {"ok": False, "note": "上一次推进还在跑，这次点击已忽略"}
                )
            self._advancing.add(session_id)
            tick_task = asyncio.ensure_future(self.engine.tick())

            def _tick_done(task: asyncio.Task) -> None:
                self._advancing.discard(session_id)
                if task.cancelled():
                    return
                error = task.exception()
                if error is not None:
                    self.logger.warning(f"[virtual_world] 推进 tick 失败：{error}")

            tick_task.add_done_callback(_tick_done)
            try:
                # 她正在调模型（例如刚走到新地点要就地决定）时不会直接失败，
                # 而是等这一轮跑完再推进——这段时间本来就该发生一个 tick。
                # 用 shield：等不到就把它留在后台跑完，**不中途取消**
                # （取消会把这一轮砍在半路，日志和状态都会变得莫名其妙）。
                await asyncio.wait_for(
                    asyncio.shield(tick_task), timeout=STATE_TICK_WAIT_SECONDS
                )
            except asyncio.TimeoutError:
                return json_response(
                    {
                        "ok": False,
                        "note": "这一轮还在跑（她在调模型或检索）：后台任务还挂着，"
                        "过几秒刷新状态页就能看到结果；要立刻停就点「中断动作」",
                    }
                )
            except Exception as exc:
                return json_response({"ok": False, "note": f"推进失败：{exc}"})
            outcomes = tick_task.result()
            return json_response(
                {
                    "ok": True,
                    "outcomes": [
                        {
                            "session_id": item.session_id,
                            "messages": item.messages,
                            "notes": item.notes,
                        }
                        for item in outcomes
                    ],
                }
            )
        if action == "decide":
            if self.engine.is_busy(session_id):
                return json_response(
                    {
                        "ok": False,
                        "note": "她正在忙（动作或检索还没结束），这次决策已跳过",
                    }
                )
            # 手动触发：绕过评估间隔，但仍然尊重"正在忙 / 在睡觉"这些硬条件
            try:
                outcome = await asyncio.wait_for(
                    self.engine.maybe_decide(
                        session_id, force=bool(payload.get("force"))
                    ),
                    timeout=STATE_ACTION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "她正在忙（上一个动作还没结束），稍等一下再点"}
                )
            return json_response(
                {
                    "ok": True,
                    "notes": outcome.notes if outcome else [],
                    "messages": outcome.messages if outcome else [],
                }
            )
        if action == "set_values":
            applied = await self.engine.set_values(
                session_id, dict(payload.get("values") or {})
            )
            return json_response({"ok": True, "applied": applied})
        if action == "run_schedule":
            result = await self.engine.run_schedule_now(
                session_id,
                str(payload.get("schedule_id") or ""),
                force=bool(payload.get("force", True)),
                operation_id=str(payload.get('operation_id') or ''),
            )
            return json_response(result)
        if action == "set_nickname":
            text = str(payload.get("text") or "").strip()
            if not text:
                return error_response("名片不能为空")
            async with self.engine.session_state(session_id) as state:
                state.bot_current_nickname = text
                state.bot_base_nickname = text
                state.bot_nickname_locked = True
            changed, failed, reason = await self._apply_card_to_group(session_id, text)
            ok = changed > 0
            note = f"已改 {changed} 个群的群名片"
            if failed:
                note += f"，{failed} 个没改成（{reason or '没说明原因'}）"
            return json_response(
                {
                    "ok": ok,
                    "reason": "" if ok else (reason or "这个会话没有能改的群名片"),
                    "card": text,
                    "changed": changed,
                    "failed": failed,
                    "note": note,
                }
            )
        if action in ("lock_nickname", "unlock_nickname", "reset_nickname"):
            async with self.engine.session_state(session_id) as state:
                if action == "lock_nickname":
                    state.bot_nickname_locked = True
                    return json_response({"ok": True, "note": "已锁定群名片"})
                if action == "unlock_nickname":
                    state.bot_nickname_locked = False
                    state.last_nickname_update_at = 0.0
                    return json_response({"ok": True, "note": "已解锁，会随状态自动变"})
                base = state.bot_base_nickname or self.engine.world.bot_name
                if not base:
                    return error_response("还不知道她原来的名片，先在群里让她说过话")
                state.bot_nickname_locked = True
                state.bot_current_nickname = base
            changed, failed, reason = await self._apply_card_to_group(session_id, base)
            if changed:
                note = f"已改回原名（{changed} 个群）"
                if failed:
                    note += f"，{failed} 个没改成（{reason or '没说明原因'}）"
                return json_response({"ok": True, "note": note})
            return json_response(
                {"ok": False, "note": f"改回原名失败：{reason or '这个会话没有能改的群名片'}"}
            )
        if action == "refresh_nickname":
            return json_response(await self.engine.refresh_nickname(session_id))
        if action == "reset_state":
            # 清掉这个会话的全部状态（位置、数值、计划、留档）。名字里必须带 state：
            # 以前它就叫 "reset"，和编辑器的「恢复原名」按钮撞名，一点就把状态删了。
            await self.db.call("delete_state", session_id)
            return json_response({"ok": True})
        if action == "clear_context":
            result = await self.engine.clear_chat_context(session_id)
            return json_response({"ok": True, **result})
        if action == "nickname":
            snapshot = await self.engine.load_state(session_id, cold_start=False)
            changed, failed, reason = await self._apply_card_to_group(
                session_id,
                snapshot.bot_current_nickname or snapshot.bot_base_nickname,
            )
            return json_response(
                {"ok": bool(changed), "changed": changed, "failed": failed, "reason": reason}
            )
        return error_response(f"未知动作：{action}")

    async def _apply_card_to_group(self, session_id: str, card: str) -> tuple[int, int, str]:
        """把名片写到「她这一组里的所有群」。

        返回 ``(改成功几个群, 失败几个, 最后一次失败原因)``。

        - **只在当前会话组里改**：别的会话组是另一个她，一个字都不碰
          （老版本是从哪个会话点的就只改那一个群，从私聊点还会直接报"这不是群聊"）；
        - 私聊没有群名片，跳过；当前会话自己就是群、又没进组时就改它自己。
        """

        targets = [
            str(item)
            for item in self.engine.group_sessions(session_id)
            if ":GroupMessage:" in str(item) and self.engine.is_enabled(str(item))
        ]
        if not targets:
            result = await self.messenger.set_group_card(session_id, card)
            if result.ok:
                return 1, 0, ""
            return 0, 1, str(result.reason or "")
        changed = 0
        failed = 0
        reason = ""
        for target in targets:
            result = await self.messenger.set_group_card(target, card)
            if result.ok:
                changed += 1
            else:
                failed += 1
                reason = str(result.reason or reason)
        return changed, failed, reason

    async def api_states(self):
        guard = self._guard()
        if guard is not None:
            return guard
        return json_response({"sessions": await self.engine.overview()})

    async def api_logs(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        limit = min(1000, max(1, request.query.get("limit", 200, type=int) or 200))
        event_type = request.query.get("type", "") or ""
        keyword = request.query.get("q", "") or ""
        before_id = request.query.get("before", 0, type=int) or 0
        events = await self.db.call(
            "query_events",
            session_id=session_id or None,
            limit=limit,
            event_type=event_type or None,
            keyword=keyword or None,
            before_id=before_id or None,
        )
        types = await self.db.call("event_types", session_id=session_id or None)
        return json_response(
            {
                "events": build_timeline(
                    events,
                    self.engine.world,
                    ext_render=self.engine.extensions.debug_event_text,
                ),
                "types": types,
                "has_more": len(events) >= limit,
            }
        )

    async def api_export(self):
        """一键导出：把「她的现状 / 最近对话 / 计划 / 记忆」拼成一份 Markdown（用户 m11096）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        limit = min(300, max(1, request.query.get("limit", 80, type=int) or 80))
        text = await self.engine.export_bundle(session_id, limit=limit)
        return json_response({"ok": True, "text": text})

    async def api_logs_export(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        limit = min(5000, max(1, request.query.get("limit", 2000, type=int) or 2000))
        events = await self.db.call(
            "query_events", session_id=session_id or None, limit=limit
        )
        payload = {
            "session": session_id,
            "exported_at": time.time(),
            "events": build_timeline(
                events,
                self.engine.world,
                ext_render=self.engine.extensions.debug_event_text,
            ),
        }
        return json_response(
            payload,
            headers={
                "Content-Disposition": 'attachment; filename="virtual-world-logs.json"'
            },
        )

    async def api_memories(self):
        guard = self._guard()
        if guard is not None:
            return guard
        # 记忆按会话各存一份，但会话组要一起看（组里几个群共用同一段经历）
        session_ids = self._memory_sessions(request.query.get("session", ""))
        node_id = request.query.get("node_id", "") or None
        memory_type = request.query.get("type", "") or None
        scope = request.query.get("scope", "") or None
        keyword = request.query.get("q", "") or ""
        rows = await self.db.call(
            "query_memories",
            session_ids=session_ids or None,
            node_id=node_id,
            memory_type=memory_type,
            scope=scope,
            limit=500,
        )
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("content", ""))]
        return json_response({"memories": rows})

    async def api_memory_create(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session_id"))
        content = str(payload.get("content", "") or "").strip()
        if not session_id or not content:
            return error_response("缺少 session_id 或 content")
        memory_id = self.engine.memory.remember(
            session_id=session_id,
            persona_id=str(payload.get("persona_id", "") or ""),
            node_id=str(payload.get("node_id", "") or ""),
            content=content,
            memory_type=str(payload.get("type", "scene") or "scene"),
            related_users=[str(u) for u in payload.get("related_users", []) or []],
            emotion=str(payload.get("emotion", "") or ""),
            weight=float(payload.get("weight", 0.5) or 0.5),
            scope=payload.get("scope") or None,
            source="manual",
        )
        return json_response({"ok": True, "id": memory_id})

    async def api_summary_save(self):
        """手改某一份会话摘要（用户 m11096 第 12 条；传空文本就是删掉）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        target = str(payload.get("target") or session_id)
        result = await self.engine.set_chat_summary(
            session_id, target, str(payload.get("text") or "")
        )
        return json_response(result)

    async def api_memory_tidy(self):
        """把同一句话记了好几遍的合并掉（用户 m11096 第 16 条）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_ids = self._memory_sessions(str(payload.get("session", "") or ""))
        if not session_ids:
            return error_response("缺少 session")
        result = self.engine.memory.tidy(session_ids=session_ids)
        merged = int(result.get("merged") or 0)
        return json_response(
            {
                "ok": True,
                "checked": int(result.get("checked") or 0),
                "merged": merged,
                "message": f"整理了 {int(result.get('checked') or 0)} 条，"
                + (f"合并掉 {merged} 条重复的。" if merged else "没发现重复的。"),
            }
        )

    async def api_memory_update(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        memory_id = payload.get("id")
        if not memory_id:
            return error_response("缺少 id")
        fields = {
            key: payload[key]
            for key in ("content", "emotion", "weight", "node_id", "type", "scope")
            if key in payload
        }
        # 空作用域在召回时匹配不上任何模式，等于把这条记忆"藏起来"，
        # 所以编辑器选了「用默认作用域」时不要真的写空串。
        if not str(fields.get("scope", "") or "").strip():
            fields.pop("scope", None)
        await self.db.call("update_memory", int(memory_id), **fields)
        return json_response({"ok": True})

    async def api_memory_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        memory_id = payload.get("id")
        if not memory_id:
            return error_response("缺少 id")
        removed = await self.db.call("delete_memories", memory_id=int(memory_id))
        return json_response({"ok": True, "removed": removed})

    async def api_memory_delete_batch(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("缺少 ids")
        removed = await self.db.call("delete_memories_by_ids", [int(item) for item in ids])
        return json_response({"ok": True, "removed": removed})

    async def api_memory_clear(self):
        """清空记忆。带 session 只清这个会话，不带则清全部。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        # 记忆按会话各存一份：选了会话组就把组里每个会话的都清掉
        sessions = self._memory_sessions(payload.get("session"))
        if sessions:
            removed = 0
            for item in sessions:
                removed += int(await self.db.call("clear_memories", item) or 0)
        else:
            removed = int(await self.db.call("clear_memories", None) or 0)
        return json_response({"ok": True, "removed": removed})

    async def api_logs_delete_batch(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("缺少 ids")
        removed = await self.db.call("delete_events_by_ids", [int(item) for item in ids])
        return json_response({"ok": True, "removed": removed})

    async def api_logs_clear(self):
        """清空事件日志。带 session 只清这个会话，不带则清全部。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        removed = await self.db.call("clear_events", session_id or None)
        return json_response({"ok": True, "removed": removed})

    async def api_memory_stats(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or None
        stats = await self.db.call(
            "memory_stats",
            session_ids=self._memory_sessions(session_id) or None,
        )
        return json_response(stats)

    async def api_memory_export(self):
        guard = self._guard()
        if guard is not None:
            return guard
        data = {
            "world": self.store.raw_world(),
            "schedules": self.store.raw_schedules(),
            "sessions": self.store.raw_sessions(),
            "memories": await self.db.call("query_memories", limit=100000),
        }
        return json_response(data)

    async def api_memory_import(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        memories = payload.get("memories")
        if not isinstance(memories, list):
            return error_response("memories 必须是数组")
        imported = 0
        for item in memories:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            self.engine.memory.remember(
                session_id=str(item.get("session_id", "") or ""),
                persona_id=str(item.get("persona_id", "") or ""),
                node_id=str(item.get("node_id", "") or ""),
                content=str(item.get("content")),
                memory_type=str(item.get("type", "scene") or "scene"),
                related_users=[str(u) for u in item.get("related_users", []) or []],
                emotion=str(item.get("emotion", "") or ""),
                weight=float(item.get("weight", 0.5) or 0.5),
                scope=item.get("scope") or None,
                source="import",
            )
            imported += 1
        return json_response({"ok": True, "imported": imported})

    # ---------------- 成长资产（P0-8） ----------------

    def _growth_store(self):
        """成长资产的目录（与 state.db 同级的 ``growth/``）。"""

        from .core.growth_assets import AssetStore

        return AssetStore(os.path.join(self.data_dir, "growth"))

    async def api_skills(self):
        """只读：内置 skill + 库里自己加的（含状态、步骤与缺哪些能力）。

        ``Skill.view_dict()`` 已经是接口形状（列表、步骤 ``{"text","action"}``），
        但 rows 这份是给 ``/skills`` 单用的；``learning`` 里那份字段一样。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        pool = self.engine.skill_capability_pool()
        builtins = self.engine.builtin_skills(now=self.engine._now())
        rows = []
        for skill in builtins:
            item = skill.view_dict()
            item["missing_capabilities"] = list(
                skill_policy.missing_capabilities(skill, pool)
            )
            rows.append(item)
        builtin_ids = {row["skill_id"] for row in rows}
        try:
            stored = await self.db.call("list_vw_skills", limit=100)
        except Exception:
            stored = []
        for row in stored or []:
            skill = skill_policy.from_row(row)
            if skill is None or skill.skill_id in builtin_ids:
                continue
            if skill.builtin:
                # 库里的 official 行只是迁移镜像；真相是 skills/*.md
                continue
            item = skill.view_dict()
            item["missing_capabilities"] = list(
                skill_policy.missing_capabilities(skill, pool)
            )
            rows.append(item)
        return json_response({"total": len(rows), "skills": rows})

    async def api_learning(self):
        """她学到了什么：经验计数、最近几条、skill 清单、上次整理干了什么。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(await self.engine.learning_section())

    async def api_skill_status(self):
        """页面上管理一个 skill：``{skill_id, action}``，action 为 promote / revoke。"""

        guard = self._guard()
        if guard is not None:
            return guard
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        skill_id = str((payload or {}).get("skill_id") or "")
        action = str((payload or {}).get("action") or "promote")
        result = await self.engine.set_skill_status(skill_id, action)
        return json_response(result, status_code=200 if result.get("ok") else 400)

    @staticmethod
    def _skill_list(value: Any) -> list[str]:
        """页面来的「一步一行」「逗号/竖线分开」都能收（用户 m5221）。"""

        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "")
        return [part.strip() for part in re.split(r"[\n|；;]", text) if part.strip()]

    async def api_manual(self):
        """读说明书（用户 m9693 D7：写成可编辑文件）。"""

        return json_response(
            {
                "ok": True,
                "text": self.engine.manual_text(),
                "path": str(self.engine.manual_path()),
            }
        )

    async def api_manual_save(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        result = self.engine.save_manual_text(payload.get("text"))
        return json_response(result, status_code=200 if result.get("ok") else 400)

    async def api_skill_files(self):
        """内置流程的文件列表：``[{file, title, steps, text}]``（用户 m12067）。"""

        files = []
        for item in self.engine.skill_file_items():
            name = str(item.get("file") or "")
            body = ""
            if name:
                try:
                    body = (self.engine.skills_dir() / name).read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    body = ""
            files.append(
                {
                    "file": name,
                    "title": str(item.get("title") or ""),
                    "steps": list(item.get("steps") or ()),
                    "text": body,
                }
            )
        return json_response(
            {"ok": True, "path": str(self.engine.skills_dir()), "files": files}
        )

    async def api_skill_file_save(self):
        """保存一份内置流程：``{file, text}``——写回 ``skills/<file>.md``。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        result = self.engine.save_skill_file(payload.get("file"), payload.get("text"))
        return json_response(result, status_code=200 if result.get("ok") else 400)

    async def api_skill_file_reset(self):
        """把内置流程还原成默认（用户改坏了时的兜底）。"""

        result = self.engine.reset_skill_files()
        return json_response(result, status_code=200 if result.get("ok") else 400)

    async def api_skill_folder(self):
        """在资源管理器里打开内置流程文件夹。"""

        result = self.engine.open_skill_folder()
        return json_response(result, status_code=200 if result.get("ok") else 400)

    async def api_skill_create(self):
        """手动新增一条流程：``{title, steps, triggers?, capabilities?, note?, summary?, when_not?, verify?, tags?, scope?}``。

        ``steps`` 一行一步；想给某一步对上动作/工具，就写成 ``文字 → 动作 id``。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        title = str(payload.get("title") or "").strip()
        steps: list[Any] = []
        for part in self._skill_list(payload.get("steps")):
            if " → " in part:
                text, _, action = part.partition(" → ")
                steps.append({"text": text.strip(), "action": action.strip()})
            else:
                steps.append(part)
        if not title or not steps:
            return json_response(
                {"ok": False, "reason": "标题和至少一步都要写"}, status_code=400
            )
        result = await self.engine.create_skill(
            title=title,
            steps=steps,
            when_to_use=self._skill_list(payload.get("when_to_use") or payload.get("triggers")),
            capabilities=self._skill_list(payload.get("capabilities")),
            note=str(payload.get("note") or ""),
            summary=str(payload.get("summary") or ""),
            when_not=str(payload.get("when_not") or ""),
            verify=str(payload.get("verify") or ""),
            tags=self._skill_list(payload.get("tags")),
            scope=str(payload.get("scope") or ""),
        )
        return json_response(result, status_code=200 if result.get("ok") else 400)

    async def api_skill_delete(self):
        """删掉一条自己加的/她提炼的流程（内置的不许删）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        result = await self.engine.delete_skill(str(payload.get("skill_id") or ""))
        return json_response(result, status_code=200 if result.get("ok") else 400)

    async def api_knowledge_tidy(self):
        """现在把"今天学到的"整理一遍（与睡觉时那次是同一个函数，只是不等节拍）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        result = await self.engine.tidy_knowledge(force=True)
        return json_response(result)

    # ---------------- 知识卡（她查过的事）----------------
    #
    # 表在 ``core/db.py``（``knowledge_card``），纯逻辑在 ``core/knowledge_card.py``。
    # db.py 只有"按会话列全表"的 ``list_knowledge_cards``（没有分页 / 搜索，签名不许动），
    # 所以分页查询与删除放在 ``knowledge_card`` 末尾那节，这里只负责鉴权 + 调用 + 兜底。
    #
    # 硬规矩：GET 只读（绝不借 GET 做写），删除按 id、幂等，不物理清表。

    def _knowledge_store(self) -> Any:
        """知识卡查询要用的同步库（``AsyncDatabase.raw``）；还没初始化就给 ``None``。"""

        return getattr(getattr(self, "db", None), "raw", None)

    async def api_knowledge_cards(self):
        """知识卡列表：``?limit=&offset=&q=&session=&include_expired=``。"""

        guard = self._guard()
        if guard is not None:
            return guard
        limit = min(500, max(1, request.query.get("limit", 200, type=int) or 200))
        offset = max(0, request.query.get("offset", 0, type=int) or 0)
        keyword = str(request.query.get("q", "") or "").strip()
        include_expired = str(request.query.get("include_expired", "") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        session_id = self._scope(request.query.get("session", ""))
        store = self._knowledge_store()
        if store is None:
            return json_response({"ok": False, "error": "数据库还没初始化"})
        try:
            result = await self.db.run(
                knowledge_card.list_cards,
                store,
                q=keyword,
                session_id=session_id,
                limit=limit,
                offset=offset,
                include_expired=include_expired,
            )
        except Exception as exc:  # noqa: BLE001 - 列表读失败也 200，前端按空表提示
            return json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json_response(
            {
                "ok": True,
                "total": int(result.get("total") or 0),
                "items": list(result.get("items") or []),
            }
        )

    async def api_knowledge_card_delete(self):
        """删掉若干张知识卡：``{"ids": [1, 2, 3]}`` → ``{"ok": true, "deleted": n}``。

        幂等：不存在 / 已经删掉的 id 直接跳过（不报错、不算进 ``deleted``）。
        """

        payload = await request.json(default={}) or {}
        if not isinstance(payload, dict):
            payload = {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        ids = payload.get("ids")
        if isinstance(ids, (str, int)):
            ids = [ids]
        if not isinstance(ids, (list, tuple)):
            ids = []
        store = self._knowledge_store()
        if store is None:
            return json_response({"ok": False, "error": "数据库还没初始化"})
        try:
            deleted = await self.db.run(knowledge_card.delete_cards, store, list(ids))
        except Exception as exc:  # noqa: BLE001
            return json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json_response({"ok": True, "deleted": int(deleted or 0)})

    async def api_plugin_drafts(self):
        """她写的插件草稿清单（用户 m31581）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        drafts = await asyncio.to_thread(self.engine.plugin_drafts)
        return json_response(
            {
                "ok": True,
                "drafts": drafts,
                "forge": self.engine.plugin_forge_section(),
                "host": self._plugin_host_dir(),
            }
        )

    async def api_plugin_draft_detail(self):
        """一份草稿的全文 + 二次校验结果（页面预览用）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        slug = str(payload.get("slug") or request.query.get("slug") or "").strip()
        detail = await asyncio.to_thread(self.engine.plugin_draft_detail, slug)
        return json_response(detail)

    async def api_plugin_draft_install(self):
        """人按下「装上」：这份草稿由**人**决定装，不看自装开关。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        slug = str(payload.get("slug") or "").strip()
        if not slug:
            return json_response({"ok": False, "reason": "要装哪一份？缺少 slug"}, status_code=400)
        result = await self.engine.install_plugin_draft(slug, who="editor")
        return json_response(result, status_code=200 if result.get("ok") else 400)

    async def api_plugin_draft_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        slug = str(payload.get("slug") or "").strip()
        result = await asyncio.to_thread(self.engine.delete_plugin_draft, slug)
        return json_response(result, status_code=200 if result.get("ok") else 400)

    @staticmethod
    def _plugin_host_dir() -> str:
        """宿主的插件目录（页面显示"装到哪去了"）。"""

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

            return str(get_astrbot_plugin_path())
        except Exception:
            return ""

    async def _install_forged_plugin(
        self, path: Path, spec: dict[str, Any], dir_name: str
    ) -> dict[str, Any]:
        """插件工坊的安装端口：拷目录 → 让宿主加载 → 把动作写进世界配置。

        用户 m31581 的 L2 落点。三条安全口径：

        * 目标目录**必须是空位**，或者里面有一份 ``forge.json``（上一次她自己装的）——
          绝不去覆盖宿主里同名的别人的插件；
        * 只拷允许的后缀（:mod:`core.plugin_forge` 的白名单），不搬 ``data/`` 之类；
        * 加载失败就把刚拷进去的目录删掉，世界配置一个字都不改 —— 宁可"没装成"，
          也不留半个装不上的插件让宿主每次启动都报错。
        """

        if not dir_name or not str(dir_name).startswith("astrbot_plugin_"):
            return {"ok": False, "reason": f"插件目录名不合法：{dir_name}"}
        root = Path(self._plugin_host_dir() or "")
        if not str(root):
            return {"ok": False, "reason": "找不到宿主的插件目录（老版本 AstrBot？）"}
        target = root / dir_name
        if target.exists():
            if not (target / "forge.json").is_file():
                return {
                    "ok": False,
                    "reason": f"宿主里已经有 {dir_name} 了，而且不是她装的：不覆盖别人的插件",
                }
            shutil.rmtree(target, ignore_errors=True)
        copied: list[str] = []
        try:
            for item in sorted(path.rglob("*")):
                if not item.is_file():
                    continue
                rel = item.relative_to(path).as_posix()
                if rel == "plugin.json":
                    continue
                if _forge_module.safe_relpath(rel) is None:
                    continue
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(item.read_bytes())
                copied.append(rel)
            meta = {
                "made_by": "astrbot_plugin_virtual_world",
                "slug": path.name,
                "action_id": str(spec.get("id") or ""),
                "installed_at": time.time(),
            }
            (target / "forge.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            shutil.rmtree(target, ignore_errors=True)
            return {"ok": False, "reason": f"拷文件失败：{exc}"}

        manager = getattr(self.context, "_star_manager", None)
        if manager is None:
            shutil.rmtree(target, ignore_errors=True)
            return {"ok": False, "reason": "宿主没给出插件管理器，装不了（老版本 AstrBot）"}
        try:
            success, error = await manager.load(
                specified_dir_name=dir_name, ignore_version_check=True
            )
        except Exception as exc:
            shutil.rmtree(target, ignore_errors=True)
            return {"ok": False, "reason": f"加载插件时出错：{exc}"}
        if not success:
            shutil.rmtree(target, ignore_errors=True)
            return {
                "ok": False,
                "reason": f"宿主加载失败，已经退回（没留下半个插件）：{error or '未知错误'}",
            }

        added = self.engine.add_action_to_world(spec)
        if not added.get("ok"):
            return {
                "ok": False,
                "reason": f"插件装上了，但动作没挂上：{added.get('reason')}（插件目录 {dir_name} 还留着）",
            }
        return {
            "ok": True,
            "dir": dir_name,
            "action_id": added.get("action_id"),
            "action": dict(spec),
            "updated": bool(added.get("updated")),
            "files": copied,
            "warnings": list(added.get("warnings") or []),
        }

    async def api_world_change_preview(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        change = world_change.from_payload(payload)
        return json_response(world_change.preview(change, self.store.raw_world()))

    async def api_world_change_apply(self):
        """应用世界扩建：校验 → 纯函数算出新世界 → 落盘 + reload（幂等）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        change = world_change.from_payload(payload)
        before = self.store.raw_world()
        after, report = world_change.apply(change, before)
        if not report.ok:
            # 校验没过有两种可能：**第一次来**（内容真有问题）或**同一条变更又提交了一次**
            # （第一次已经应用过，世界里有那个 id 了）。后者要回 already_applied，
            # 不能报成"id 撞了"——页面/调用方重试时不该看到一个吓人的错误。
            applied_before = False
            try:
                applied_before = bool(
                    await self.db.call("has_operation", change.operation_id)
                )
            except Exception:
                applied_before = False
            if applied_before:
                return json_response(
                    {
                        "ok": True,
                        "operation_id": change.operation_id,
                        "already_applied": True,
                        "report": report.as_dict(),
                    }
                )
            return json_response({"ok": False, "report": report.as_dict()})
        # 幂等：先抢 operation key（INSERT OR IGNORE），抢不到 = 已经应用过
        claimed = set()
        try:
            claimed = set(
                await self.db.call(
                    "record_operations",
                    [change.operation_id],
                    kind="world_change",
                    now=self.engine._now(),
                )
                or ()
            )
        except Exception:
            claimed = {change.operation_id}  # 记不上也照做：幂等只是省事，不是安全边界
        if not claimed:
            return json_response(
                {
                    "ok": True,
                    "operation_id": change.operation_id,
                    "already_applied": True,
                    "report": report.as_dict(),
                }
            )
        self.store.save_world(after)
        self.engine.reload_config()
        return json_response(
            {
                "ok": True,
                "operation_id": change.operation_id,
                "already_applied": False,
                "report": report.as_dict(),
                "nodes": len(after.get("nodes") or ()),
            }
        )

    async def api_growth_assets(self):
        """只读：资产所有权清单（谁的、版本、能不能被升级覆盖）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        try:
            return json_response(self._growth_store().ownership())
        except Exception as exc:
            return json_response({"ok": False, "error": f"读资产清单失败：{exc}"})

    async def api_growth_backup(self):
        """备份：数据库 + 成长资产打进一个快照目录（只用只读拷贝，不动源文件）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        import time as _time

        from .core.growth_assets import backup

        stamp = _time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(self.data_dir, "backups", f"snapshot-{stamp}")
        report = backup(
            db_path=os.path.join(self.data_dir, "state.db"),
            growth_root=os.path.join(self.data_dir, "growth"),
            dest=dest,
        )
        return json_response(report.as_dict())

    async def api_growth_verify(self):
        """恢复演练：把快照读回来核对行数与资产条数（不覆盖现有数据）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        dest = str(request.query.get("dest", "") or "").strip()
        if not dest:
            return error_response("缺少 dest 参数（快照目录）")
        from .core.growth_assets import verify_backup

        return json_response(verify_backup(dest))

    async def api_prompt(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or ""
        mode = request.query.get("mode", "takeover") or "takeover"
        if not session_id:
            return error_response("缺少 session 参数")
        session_id = self._scope(session_id)
        if mode == "autonomous":
            text = await self.engine.preview_autonomous_prompt(session_id)
        else:
            text = await self.engine.preview_last_reply_request(session_id)
        trace = getattr(self.engine, "call_trace", None)
        return json_response(
            {
                "prompt": text,
                "chars": len(text),
                # 分段索引：编辑器里直接显示，避免"某一段是不是没进去"只能靠肉眼找
                "sections": prompt_section_index(text),
                # 状态槽：调试页要能看出"槽里有东西但没进提示词"（过期了）这种情形
                "state_slots": await self.engine.preview_state_slots(session_id),
                # 这一轮抽到哪几条声音样例：提示词两万字，不点出来根本找不着
                "voice_samples": await self.engine.preview_voice_samples(session_id),
                # 收图失败流水（2026-10-20）：她"看不见图"时，调试页要能说出是哪一步掉的
                "media_cache": self.engine.recent_media_cache_failures(limit=8),
                # P0-1：最近几类**真实**请求（回复 / 自主 / Agent / 续说 / 辅助 / 生成）
                # 与"模拟预览"分开放，页面才不会把预览当成实跑
                "calls": trace.rows(limit=12) if trace is not None else [],
                "call_summary": trace.summary() if trace is not None else {},
                "config_revision": int(getattr(self.engine, "config_revision", 0) or 0),
            }
        )

    # ---------------- 预设：成套的世界配置 ----------------

    async def api_state_history(self):
        """数值历史：给编辑器画「她最近过得怎么样」。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or ""
        if not session_id:
            return error_response("缺少 session 参数")
        session_id = self._scope(session_id)
        try:
            hours = int(request.query.get("hours", 24) or 24)
        except (TypeError, ValueError):
            hours = 24
        return json_response(await self.engine.state_history(session_id, hours=hours))

    async def api_defaults(self):
        """内置默认文案：内置动作的说明/名片文案，以及两段图片转述提示词。

        编辑器里的「恢复默认」图标都从这里取值——默认文案只有 core/defaults.py 一份。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        actions: dict[str, dict[str, str]] = {}
        for item in DEFAULT_WORLD.get("actions") or []:
            action_id = str(item.get("id") or "")
            if not action_id:
                continue
            actions[action_id] = {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "nickname_text": str(item.get("nickname_text") or ""),
            }
        return json_response(
            {
                "actions": actions,
                # 配置字段的中文说明：滑块明细和旧默认值提醒都用它，
                # 免得同一份字段名要在前端再抄一遍。
                "field_labels": dict(FIELD_LABELS),
                "captions": {
                    "look": DEFAULT_CAPTION_PROMPT,
                    "forward": DEFAULT_FORWARD_PROMPT,
                },
                # 扩展注册的事件类型：日志页的「类型」筛选和图标用它
                "debug_events": self.engine.extensions.debug_events(),
            }
        )

    async def api_presets(self):
        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(
            {
                "presets": self.store.list_presets(),
                "active": self.store.active_preset(),
            }
        )

    async def api_preset_save(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not preset_id:
            return error_response("预设 id 不能为空")
        try:
            path, warnings = self.store.save_preset(
                preset_id,
                name=str(payload.get("name") or "").strip(),
                note=str(payload.get("note") or "").strip(),
            )
        except Exception as exc:
            return error_response(f"保存预设失败：{exc}")
        return json_response({"ok": True, "id": path.stem, "warnings": warnings})

    async def api_preset_new_default(self):
        """用内置的默认世界新建一份预设（不动当前配置）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip() or "default_world"
        try:
            clean, warnings = self.store.new_default_preset(
                preset_id,
                name=str(payload.get("name") or "").strip(),
                note=str(payload.get("note") or "").strip(),
            )
        except Exception as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "id": clean, "warnings": warnings})

    async def api_preset_apply(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not preset_id:
            return error_response("缺少预设 id")
        blocks = payload.get("blocks")
        if blocks is not None and not isinstance(blocks, list):
            return error_response("blocks 必须是数组")
        # 默认**不清状态**：切预设本来只想换世界，上下文留着更符合直觉
        clear_state = bool(payload.get("clear_state", False))
        try:
            result = self.store.apply_preset(preset_id, blocks=blocks)
        except Exception as exc:
            return error_response(f"应用预设失败：{exc}")
        warnings = list(result.get("warnings") or [])
        warnings.extend(self.engine.reload_config())
        repairs: list[str] = []
        if clear_state:
            cleared = await self.engine.clear_all_states()
        else:
            cleared = 0
            # 不清状态就得把状态修一遍：地图/动作换过之后，旧的地点与计划可能已经不存在
            repairs = await self.engine.repair_states_after_config_change()
        return json_response(
            {
                "ok": True,
                "id": preset_id,
                "blocks": list(result.get("blocks") or []),
                "cleared_sessions": cleared,
                "repairs": repairs,
                "warnings": warnings,
            }
        )

    async def api_preset_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not self.store.delete_preset(preset_id):
            return error_response("找不到这个预设")
        return json_response({"ok": True})

    async def api_preset_rename(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        data = self.store.read_preset(preset_id)
        if not data:
            return error_response("找不到这个预设")
        data["name"] = str(payload.get("name") or "").strip() or preset_id
        if payload.get("note") is not None:
            data["note"] = str(payload.get("note") or "")
        warnings = self.store.write_preset(preset_id, data)
        return json_response({"ok": True, "warnings": warnings})

    async def api_preset_json(self):
        """GET：读出预设原文；POST：整段写回（导入 / 直接编辑都走这里）。"""

        if request.method == "POST":
            payload = await request.json(default={}) or {}
            guard = self._guard(payload)
            if guard is not None:
                return guard
            preset_id = str(payload.get("id") or "").strip()
            if not preset_id:
                return error_response("缺少预设 id")
            raw = payload.get("payload")
            if isinstance(raw, dict):
                data = raw
            else:
                text = str(payload.get("json") or "").strip()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError as exc:
                    return error_response(f"JSON 格式不对：{exc}")
            try:
                warnings = self.store.write_preset(preset_id, data)
            except Exception as exc:
                return error_response(f"写入预设失败：{exc}")
            return json_response({"ok": True, "id": preset_id, "warnings": warnings})
        guard = self._guard()
        if guard is not None:
            return guard
        preset_id = request.query.get("id", "") or ""
        data = self.store.read_preset(preset_id)
        if not data:
            return error_response("找不到这个预设")
        return json_response({"preset": data})

    async def api_backup(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        path = self.store.backup(str(payload.get("tag", "") or ""))
        return json_response({"ok": True, "file": path.name})

    # ---------------- 改动历史 ----------------

    async def api_history(self):
        """改动历史列表（新的在前）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(
            {
                "items": self.store.list_history(),
                "keep": HISTORY_KEEP,
            }
        )

    async def api_history_item(self):
        """一条历史：快照内容 + 当前配置 + 逐块差异（前端据此画左右对比）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        snapshot_id = request.query.get("id", "") or ""
        if not snapshot_id:
            return error_response("缺少 id")
        try:
            return json_response(self.store.read_history(snapshot_id))
        except Exception as exc:
            return error_response(str(exc))

    async def api_history_restore(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        snapshot_id = str(payload.get("id") or "").strip()
        if not snapshot_id:
            return error_response("缺少 id")
        blocks = payload.get("blocks")
        picked = (
            [str(item) for item in blocks] if isinstance(blocks, list) else None
        )
        try:
            result = self.store.restore_history(snapshot_id, blocks=picked)
        except Exception as exc:
            return error_response(f"恢复失败：{exc}")
        self.engine.reload_config()
        return json_response({"ok": True, **result})

    async def api_history_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        snapshot_id = str(payload.get("id") or "").strip()
        if not snapshot_id:
            return error_response("缺少 id")
        return json_response({"ok": self.store.delete_history(snapshot_id)})

    async def api_history_clear(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return json_response({"ok": True, "removed": self.store.clear_history()})


# ======================================================================
# 小工具
# ======================================================================


def _help_text(pronoun: str = "她") -> str:
    """指令帮助文案。pronoun 由全局设置里的性别决定（她/他/ta）。"""

    return (
        "虚拟世界指令：\n"
        f"· /vw status            {pronoun}现在在哪、在做什么、心情如何\n"
        f"· /vw recall <话题>     让{pronoun}回忆某个话题\n"
        f"· /vw memory            看看{pronoun}记得你什么\n"
        f"· /vw forget me [话题]  让{pronoun}忘记关于你的记忆\n"
        f"· /vw event <一句话>   给她安排一件会发生的事（看她怎么处理）\n"
        f"· /vw event list       看最近的事件线索\n"
        f"· /vw ability          看{pronoun}的本事（能力值，管理员）\n"
        f"· /vw thread           看{pronoun}手头没完的事（管理员）\n"
        "· /vw pending          看在等你拍板的事（管理员）\n"
        "· /vw approve|deny <id> 准 / 不让她做（主人）\n"
        "· /vw schedule          查看日程\n"
        "· /vw schedule run <id> 立刻跑一遍某条日程（管理员）\n"
        "· /vw map               查看地图\n"
        "· /vw nickname lock|unlock|set <文本>|reset   群名片控制（管理员）\n"
        "· /vw session list|add [ID]|remove <ID>|enable|disable <ID>   会话白名单（管理员）\n"
        "· /vw reload            热加载配置（管理员）\n"
        "· /vw reset [会话]      重置某个会话的状态（管理员）\n"
        "· /vw restore-default   恢复默认配置（管理员）\n"
        "· /vw debug on|off|state|plan|stop|memories|session|tools|prompt|autoprompt|tick|decide|agent|ledger|index   调试（管理员）"
    )


def _cfg_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "开启", "是")
    return bool(value)


def _cfg_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _positive_int(value: Any, default: int) -> int:
    """页面传来的数字：认不出或不是正数就用默认值（不把垃圾读成 0）。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _command_args(event: AstrMessageEvent) -> list[str]:
    text = (event.get_message_str() or "").strip()
    if not text:
        return []
    parts = text.split()
    if parts and parts[0].lstrip("/").lower() in ("vw", "世界", "virtualworld"):
        parts = parts[1:]
    return parts


def _gif_first_frame(data: bytes) -> bytes | None:
    """把一张 GIF 截到**第一帧就结束**（管理页 M1 只显示静态首帧，不做播放）。

    做法是按 GIF 结构走一遍：文件头 + 逻辑屏幕描述符 + 全局色表，然后一路
    跳过扩展块，找到第一个图像描述符（``0x2C``），把"宽高 + 局部色表 + 亮码
    长度 + 第一段 LZW 数据子块"收完就补一个 ``0x3B`` 收尾。解析不动就返回
    ``None``，调用方原样发整张图——宁可让浏览器自己解，也不发坏字节。
    """

    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    flags = data[10]
    pos = 13
    if flags & 0x80:
        pos += 3 * (2 ** ((flags & 0x07) + 1))
    while pos < len(data):
        marker = data[pos]
        if marker == 0x3B:  # 文件结尾：本来就只有一帧
            return None
        if marker == 0x21:  # 扩展块：标签 + 一串子块
            if pos + 2 > len(data):
                return None
            pos += 2
            if data[pos - 1] == 0xF9:  # 图形控制扩展是固定长度
                if pos + 6 > len(data):
                    return None
                pos += 6
            while pos < len(data) and data[pos]:
                pos += data[pos] + 1
            pos += 1
            continue
        if marker != 0x2C:  # 认不出的块，别硬猜
            return None
        if pos + 10 > len(data):
            return None
        local = data[pos + 9]
        end = pos + 10
        if local & 0x80:
            end += 3 * (2 ** ((local & 0x07) + 1))
        if end + 1 > len(data):
            return None
        block = data[end]
        end += 1 + block
        if end > len(data):
            return None
        return data[:end] + b"\x3b"
    return None

    text = (event.get_message_str() or "").strip()
    if not text:
        return []
    parts = text.split()
    if parts and parts[0].lstrip("/").lower() in ("vw", "世界", "virtualworld"):
        parts = parts[1:]
    return parts


def _event_persona_id(event: AstrMessageEvent) -> str:
    for attr in ("persona_id",):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value:
            return value
    session = getattr(event, "session", None)
    value = getattr(session, "persona_id", None) if session is not None else None
    return value if isinstance(value, str) else ""


def _render_status(
    snapshot: dict[str, Any],
    pronoun: str = "她",
    *,
    build: dict[str, Any] | None = None,
    agency: dict[str, Any] | None = None,
    topic_steer: dict[str, Any] | None = None,
) -> str:
    values = snapshot.get("values", {})
    action = snapshot.get("current_action") or {}
    action_text = action.get("desc") or action.get("type") or "没在做什么"
    travel = snapshot.get("travel") or []
    lines = [
        f"{pronoun}现在在【{snapshot.get('node_name') or snapshot.get('node_id')}】",
        f"状态：{snapshot.get('state')}　心情：{snapshot.get('mood')}",
        f"正在做：{action_text}",
        "数值："
        f"精力 {values.get('energy', 0):.2f}　孤独 {values.get('loneliness', 0):.2f}　"
        f"好奇 {values.get('curiosity', 0):.2f}　心潮 {values.get('affect', 0):.2f}　"
        f"无聊 {values.get('boredom', 0):.2f}　欲求 {values.get('desire', 0):.2f}",
        f"世界时间：{snapshot.get('world_time')} tick"
        f"（1 tick = {int(snapshot.get('tick_seconds', 60))} 秒）",
    ]
    # T12 Step 2：「她自己的打算」加一行——只在调用方显式传了 agency 时才加；
    # 不传（老签名）时输出逐字节不变，`build is None` 那条老断言照样成立。
    head: list[str] = []
    if agency is not None:
        head.append(_agency_status_line(agency))
    # T12 Step 3：话题层那行（「话头：拐到了「X」，还没接着说」）。没有挂着的事就不加，
    # 所以关掉话题层、或者她这轮没拐过时，输出与以前逐字节相同。
    topic_line = _topic_steer_status_line(topic_steer)
    if topic_line:
        head.append(topic_line)
    if head:
        lines[3:3] = head
    if travel:
        lines.append(
            "从这里出发："
            + "、".join(f"{item['name']} {item['ticks']} tick" for item in travel)
        )
    if snapshot.get("unanswered_count"):
        lines.append(f"连续无人回应：{snapshot['unanswered_count']} 次")
    # build is None 时必须逐字节保持原有输出（老用例按旧签名断言）
    if build is not None:
        lines.extend(_build_stamp_lines(build))
    return "\n".join(lines)


def _agency_status_line(agency: dict[str, Any]) -> str:
    """`/vw status` 里那行「打算：<意图>（来源/强度）｜本周让位 N 次」。

    读的是 ``engine.agency_section`` 给的同一份快照（``snapshot["agency"]``）；
    面板缺字段一律降级成"暂时没有"，绝不给状态命令抛异常。
    """

    data = agency if isinstance(agency, dict) else {}
    intention = data.get("intention")
    panel = intention if isinstance(intention, dict) else {}
    text = str(panel.get("text") or "").strip()
    try:
        yielded = int(data.get("yield_week") or 0)
    except (TypeError, ValueError):
        yielded = 0
    if not text:
        return f"打算：暂时没有｜本周让位 {yielded} 次"
    origin = str(panel.get("origin_label") or "来路不明")
    strength = str(panel.get("strength_label") or "说不清")
    return f"打算：{text}（{origin}/{strength}）｜本周让位 {yielded} 次"


def _topic_steer_status_line(panel: Any) -> str:
    """`/vw status` 里那行「话头：拐到了「X」，还没接着说」（没挂着的事返回空串）。

    读的是 ``engine.snapshot`` 给的 ``snapshot["topic_steer"]["line"]``；字段缺、
    类型不对一律当"没有这一行"，绝不给状态命令抛异常。
    """

    data = panel if isinstance(panel, dict) else {}
    return str(data.get("line") or "").strip()


def _parse_action_discovery_reply(reply: Any) -> dict[str, Any]:
    """整理模型的原话 → A3 允许写的字段 dict（坏输出回空 dict，绝不抛）。

    模型经常把 JSON 包在 ```json 里、或者在前后多说一句，所以这里只做"从第一个 `{`
    到最后一个 `}` 截一段再解析"，再按 A3 的字段类型粗归一。**不许**把解析失败当成
    "整理成功但没内容"——返回空 dict 之后调用方会记一次失败并重试。
    """

    raw = str(reply or "").strip()
    if not raw:
        return {}
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    parsed, _repaired, _newlines = loads_json_repaired(raw[start : end + 1])
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, Any] = {}
    summary = " ".join(str(parsed.get("summary") or "").split())
    if summary:
        out["summary"] = summary[:80]
    when = " ".join(str(parsed.get("when_to_use") or "").split())
    if when:
        out["when_to_use"] = when[:120]
    for key, limit in (("key_constraints", 3), ("tags", 4)):
        values = parsed.get(key)
        if isinstance(values, str):
            values = [values]
        if isinstance(values, (list, tuple)):
            cleaned = [" ".join(str(item).split())[:40] for item in values]
            picked = [item for item in cleaned if item][:limit]
            if picked:
                out[key] = picked
    policy = str(parsed.get("detail_policy") or "").strip()
    if policy in ("auto", "always", "on_demand"):
        out["detail_policy"] = policy
    return out


def _validated_action(raw: Any) -> Any:
    """原始动作 dict → 校验后的定义（认不出回 ``None``，绝不抛）。

    A3 的指纹/判据都只认"载荷里出现过的字段"，原始 dict 与校验后的定义算出来不一样；
    所有"要不要重排""指纹变了没"的比较都必须**先过这一道**，否则两边永远对不上。
    """

    if raw is None:
        return None
    try:
        from .core.action_discovery import _as_definition

        return _as_definition(dict(raw) if isinstance(raw, dict) else raw)
    except Exception:
        return None


def _action_discovery_reason(definition: Any) -> str:
    """为什么这条动作要整理：一句中文（预览页给人看）。"""

    try:
        from .core.action_discovery import (
            DISCOVERY_GENERATOR_VERSION,
            discovery_of,
            discovery_input_hash,
        )

        discovery = discovery_of(definition)
        if not str(getattr(discovery, "summary", "") or ""):
            return "还没有简述"
        if str(getattr(discovery, "input_hash", "") or "") != discovery_input_hash(definition):
            return "这条动作改过（名字、说明或约束变了）"
        if str(getattr(discovery, "generator_version", "") or "") != DISCOVERY_GENERATOR_VERSION:
            return "整理方式升级了，旧结果作废"
        status = str(getattr(discovery, "status", "") or "")
        if status in ("failed", "stale"):
            return "上次没整理成，需要重来"
    except Exception:
        pass
    return "需要重新整理"
