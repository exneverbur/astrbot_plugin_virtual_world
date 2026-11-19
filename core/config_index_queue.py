"""配置异步整理队列（A4）：按类型注册处理器、租约防重、过期结果丢弃。

这一层管什么
------------

配置（动作发现信息、以后别的派生信息）**保存不阻塞**：保存时只把"哪一条配置该重新整理"
排进队列表，后台慢慢跑。设计 §5 的流程落在这里：

```text
编辑 / 生成 / 导入
    -> 保存原始定义（上层的事，A5）
    -> 比较影响派生信息的字段（指纹，本模块的 enqueue）
    -> 把需要整理的有效定义加入队列（config_index_queue 表）
    -> 后台辅助模型生成简述与使用时机（本模块的 run_batch）
    -> 校验结果与当前版本（重读权威指纹 + 更新的行 → 过期就丢）
    -> 原子发布派生信息（complete → ready；真正落配置由上层 apply）
```

为什么独立成模块、独立成表
--------------------------

范式照抄 ``core/embedding_index.py``（租约之外的幂等键、批处理、超时、退避、``kv`` 里记
``last_error`` 都同源），但**表独立**：谈资向量与配置派生是两笔账，混在一张表里清空 /
重建 / 计数都会互相牵连。

三条硬要求，逐条兑现
--------------------

1. **指纹只含必需字段，绝不含密钥**。处理器自己声明 ``input_fields``（白名单），
   :func:`pick_input_fields` 再按 :data:`SENSITIVE_FIELD_MARKS` 扣一道；
   **落库的只有指纹**，原始配置与密钥都不进库（队列行里没有 payload 列）。
2. **租约与多 worker 防重**。认领是"回收过期租约 + 原子 UPDATE + 按令牌取回"三步（在
   ``core/db.py`` 里），写回一律带令牌：令牌对不上就当没这回事，绝不拿旧结果盖新编辑。
3. **过期结果丢弃**。模型回来之后重算权威指纹、并且查"同一目标有没有更新的活跃行"；
   任一不符就标 ``stale``，**不落结果**。

本模块**不加载配置、不碰世界模型、不调模型、不自己开数据库连接、不自己取时钟**：
生成器（``generate``）与"从目标身份取当前输入"（``build_input``）都由上层（A5）注入，
所以这一层可以完全用假生成器单测。

认不出 / 坏输入一律降级、绝不外抛
----------------------------------

所有对外方法内部兜底：失败写 :attr:`ConfigIndexQueue.last_error`（并同步到 kv
``config_index_last_error``），返回值退化成"这一轮什么都没做"。处理器没注册、生成器没接、
目标已经不在了、模型超时、模型返回垃圾——都是**正常路径**，不是异常。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .action_discovery import SENSITIVE_FIELD_MARKS, is_sensitive_field
from .db import CONFIG_INDEX_QUEUE_MAX_BACKOFF


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CONFIG_INDEX_LAST_ERROR_KV_KEY = "config_index_last_error"
"""最近一次整理失败的原因写进 kv 的键（状态接口读它，不用把队列实例端过去）。"""

DEFAULT_BATCH_SIZE = 4
DEFAULT_MAX_CONCURRENCY = 2
"""一次批处理里**同时在跑的整理请求**上限（有界并发，别把供应商打爆）。"""

DEFAULT_TIMEOUT_SECONDS = 30.0
"""单次整理请求的超时（秒）。整理不在回复路径上，给足余量；真机慢模型可再调。"""

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BATCH_BUDGET_SECONDS = 60.0
"""单批墙钟预算（秒）：超过就收工，还没跑的行交还租约留给下一批。"""

MODEL_SLOT_CONSOLIDATE = "consolidate"
"""默认复用的模型槽位，**不新增配置项**（设计 §11.4：A 流不为整理另开供应商槽位）。"""

ERROR_NO_HANDLER = "no_handler"
ERROR_UNKNOWN_KIND = "unknown_kind"
ERROR_INPUT_MISSING = "input_missing"
ERROR_FINGERPRINT_CHANGED = "fingerprint_changed"
ERROR_TIMEOUT = "provider_timeout"
ERROR_PROVIDER = "provider_error"
ERROR_EMPTY_RESULT = "empty_result"
ERROR_INTERNAL = "internal_error"


def backoff_seconds(attempts: Any) -> float:
    """退避多久再试：``min(60, 2 ** attempts)``（``attempts`` = 已经加过这一次的值）。

    与 ``core/embedding_index.py`` 同一个公式：整理是"有空再算"的后台活，重试必须收敛，
    不能变成一个永远在打电话的循环。转不成数按 1 次算。
    """

    try:
        value = int(attempts)
    except (TypeError, ValueError):
        value = 1
    return min(CONFIG_INDEX_QUEUE_MAX_BACKOFF, float(2 ** max(1, min(value, 30))))


def fingerprint(payload: Any, *, fields: Sequence[str]) -> str:
    """输入指纹：``fields`` 白名单里的值的稳定哈希前 32 位；没有可用字段回空串。

    **绝不包含密钥**：字段名像密钥的（:func:`is_sensitive_field`）直接扣掉。
    掉字段这件事要能被发现，所以另有 :func:`pick_input_fields` 会把它报出来。
    """

    picked, _dropped = pick_input_fields(payload, fields=fields)
    if not picked:
        return ""
    try:
        text = json.dumps(picked, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def pick_input_fields(
    payload: Any, *, fields: Sequence[str]
) -> tuple[dict[str, Any], list[str]]:
    """按白名单取字段，返回 ``(数据, 被剔掉的敏感字段名)``。

    第二个返回值是给"指纹绝不含密钥"兜底的：即使调用方把 ``*_api_key`` 写进了白名单，
    这里也会剔掉并如实报出来（好让上层发现白名单写歪了，而不是悄悄把密钥算进指纹）。
    非 dict 的 payload 一律当空处理，不抛。
    """

    if not isinstance(payload, dict):
        return ({}, [])
    picked: dict[str, Any] = {}
    dropped: list[str] = []
    for name in fields or ():
        key = str(name)
        if key not in payload:
            continue
        if is_sensitive_field(key):
            dropped.append(key)
            continue
        picked[key] = payload[key]
    return (picked, dropped)


def filter_allowed_fields(
    payload: Any, *, allowed: Sequence[str], locked: Iterable[str] = ()
) -> tuple[dict[str, Any], list[str], list[str]]:
    """只留**允许回写**的派生字段，返回 ``(结果, 被拒的键, 因人工锁跳过的键)``。

    这是"回写只改允许的派生字段"的落地点：模型返回的整份 dict 绝不可能原样落配置——
    多出来的键（含它自己想改的权威字段）进第二个返回值，人工锁住的进第三个。
    空值（``None`` / 空串 / 空列表）一律**不写**：模型返回空不该把已有内容抹掉。
    """

    if not isinstance(payload, dict):
        return ({}, [], [])
    allow = {str(item) for item in (allowed or ())}
    guarded = {str(item) for item in (locked or ())}
    kept: dict[str, Any] = {}
    rejected: list[str] = []
    skipped: list[str] = []
    for name, value in payload.items():
        key = str(name)
        if key not in allow:
            rejected.append(key)
            continue
        if key in guarded:
            skipped.append(key)
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, dict)) and not value:
            continue
        kept[key] = value
    return (kept, sorted(set(rejected)), sorted(set(skipped)))


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------


@dataclass
class ConfigIndexHandler:
    """一种配置类型的整理处理器（按 ``kind`` 注册到队列上）。

    它是**纯声明 + 三个回调**，不含任何模型客户端：真正的调用由上层注入，于是
    "指纹只含必需字段""只回写允许字段""过期结果丢弃"这些规则都能在没有模型、
    没有数据库单进程的情况下单测。

    字段：

    * ``kind``：类型名（队列行的第一列，也是唯一键的一半）；
    * ``input_fields``：参与指纹的字段白名单（**只放必需字段**，密钥永远不该写进来）；
    * ``allowed_fields``：允许回写的派生字段白名单；
    * ``generator_version``：整理器版本（换提示词 / 换字段取舍时改它，旧结果整体作废）；
    * ``model_slot``：用哪个模型槽位——默认 ``"consolidate"``，**不新增配置项**；
    * ``build_input``：``(target_id) -> dict | None``，从**当前权威配置**里取待整理输入
      （返回 ``None`` 表示目标已经不在了 / 被禁用 → 队列会把作业作废，不调模型）；
    * ``generate``：``(payload, *, kind, target_id, model_slot) -> dict | None``，真正的整理；
      同步或异步都认；
    * ``parse_result``：模型原话 → 派生字段 dict（不给就要求 ``generate`` 直接返回 dict）；
    * ``current_fingerprint``：``(target_id) -> str``，重读现在的权威指纹（给了就与队列行
      的指纹对一遍，不一致 = 目标在排队期间又改过 → 迟到结果丢弃）；
    * ``locked_fields``：``(target_id) -> 可迭代``，人工锁住的字段名（给了就**早点扣掉**，
      不必等落配置那一步才发现写不进去）。
    """

    kind: str
    input_fields: tuple[str, ...] = ()
    allowed_fields: tuple[str, ...] = ()
    generator_version: str = ""
    model_slot: str = MODEL_SLOT_CONSOLIDATE
    build_input: Callable[[str], Any] | None = None
    generate: Callable[..., Any] | None = None
    parse_result: Callable[[Any], Any] | None = None
    current_fingerprint: Callable[[str], Any] | None = None
    locked_fields: Callable[[str], Any] | None = None

    def ready(self) -> bool:
        """这个处理器现在能不能跑（取输入 + 生成两件事都接上了才算）。"""

        return bool(str(self.kind or "").strip()) and callable(self.build_input) and callable(
            self.generate
        )


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------


class ConfigIndexQueue:
    """配置异步整理队列：入队、按预算批量整理、有界重试、迟到结果丢弃。

    构造：``ConfigIndexQueue(db, handlers=[...], owner="", clock=time.time)``。

    * ``db``：``core.db.Database``（直接调同名方法），或者任何带
      ``call(name, *args, **kwargs)`` 的替身（``core.db.AsyncDatabase`` 就是后者）；
    * ``handlers``：:class:`ConfigIndexHandler` 的序列（同名后者覆盖前者）；
    * ``owner``：写进租约的"谁在跑"，纯观测用；不给就用进程号。**防重靠令牌不靠它**；
    * ``clock``：取"现在"的函数（默认 ``time.time``），测试注入假时钟用。

    线程 / 协程模型：单个队列实例同一时刻只跑一批（:meth:`run_batch` 重入直接返回
    ``stopped='busy'``）。这不是为了防写坏数据（库层自己会串行化），而是为了让
    "一批发几次模型请求、最多花多久"可预测。
    """

    def __init__(
        self,
        db: Any,
        *,
        handlers: Iterable[ConfigIndexHandler] = (),
        owner: str = "",
        clock: Callable[[], float] = time.time,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        lease_seconds: float = 0.0,
        budget_seconds: float = DEFAULT_BATCH_BUDGET_SECONDS,
    ) -> None:
        self.db = db
        self.owner = str(owner or "").strip() or f"pid{os.getpid()}"
        self._clock = clock if callable(clock) else time.time
        self.max_concurrency = max(1, _int_or(max_concurrency, DEFAULT_MAX_CONCURRENCY))
        self.timeout_seconds = _positive_or(timeout_seconds, DEFAULT_TIMEOUT_SECONDS)
        self.max_attempts = max(1, _int_or(max_attempts, DEFAULT_MAX_ATTEMPTS))
        self.batch_size = max(1, _int_or(batch_size, DEFAULT_BATCH_SIZE))
        # 租约至少要是单次超时的两倍：否则一个还在正常跑的请求会被别人当成死掉的回收掉。
        self.lease_seconds = _positive_or(lease_seconds, self.timeout_seconds * 2.0)
        self.budget_seconds = _positive_or(budget_seconds, DEFAULT_BATCH_BUDGET_SECONDS)
        self._handlers: dict[str, ConfigIndexHandler] = {}
        self._last_error = ""
        self._in_batch = False
        self.dropped_fields: list[str] = []
        """被敏感字段闸扣掉的字段名（``"kind:field"``）。**空列表 = 从没发生过**。"""
        for handler in handlers or ():
            self.register(handler)

    # -- 注册与只读状态 ---------------------------------------------------

    def register(self, handler: ConfigIndexHandler) -> ConfigIndexHandler:
        """注册（或替换）一个处理器；``kind`` 为空的一律拒收并记一笔错误。"""

        if not isinstance(handler, ConfigIndexHandler):
            self._record_error("处理器类型不对，已忽略")
            return handler
        kind = str(handler.kind or "").strip()
        if not kind:
            self._record_error("处理器没有 kind，已忽略")
            return handler
        handler.kind = kind
        self._handlers[kind] = handler
        return handler

    @property
    def handlers(self) -> dict[str, ConfigIndexHandler]:
        """``{kind: 处理器}``（只读副本）。"""

        return dict(self._handlers)

    @property
    def configured(self) -> bool:
        """有没有至少一个**现在就能跑**的处理器（能取输入 + 能生成）。"""

        return any(item.ready() for item in self._handlers.values())

    @property
    def last_error(self) -> str:
        """最近一次失败的原因；``''`` = 目前没有未清掉的错误。

        一批全部处理干净之后会清空（并同步把 kv 里那条也写回空串）：状态接口不该永远
        挂着一个已经不存在的旧错误。
        """

        return self._last_error

    # -- 底层：时间 / 数据库 / 错误记账 ------------------------------------

    def _now(self) -> float:
        """取当前时间；时钟坏掉（抛异常 / 非有限）退回 ``time.time()``，绝不外抛。"""

        try:
            value = float(self._clock())
        except Exception:
            return time.time()
        return value if math.isfinite(value) else time.time()

    def _deadline(self, started: float, budget_seconds: Any) -> float:
        """单批墙钟截止时刻（``inf`` = 不限）。坏值 / 非正数一律当不限。"""

        try:
            budget = float(budget_seconds)
        except (TypeError, ValueError):
            return float("inf")
        if not math.isfinite(budget) or budget <= 0.0:
            return float("inf")
        return started + budget

    def _record_error(self, message: Any) -> None:
        """同步记一次错误（不改 kv；异步路径用 :meth:`_note_error`）。"""

        self._last_error = str(message or "").strip()

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """调一次数据库，两种协议都认（``call()`` 优先，其次同名方法）。"""

        caller = getattr(self.db, "call", None)
        if callable(caller):
            outcome = caller(name, *args, **kwargs)
        else:
            target = getattr(self.db, name, None)
            if not callable(target):
                raise AttributeError(f"数据库对象没有 {name}()")
            outcome = target(*args, **kwargs)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

    async def _note_error(self, message: Any) -> None:
        """记一次失败：更新 :attr:`last_error`，变了就同步写 kv（best-effort）。"""

        text = str(message or "").strip()
        if text == self._last_error:
            return
        self._last_error = text
        try:
            await self._call("kv_set", CONFIG_INDEX_LAST_ERROR_KV_KEY, text)
        except Exception:
            pass

    def _empty_result(self) -> dict[str, Any]:
        """一次批处理的标准返回骨架（失败路径也用它，键一个不少）。"""

        return {
            "scanned": 0,
            "leased": 0,
            "ready": 0,
            "stale": 0,
            "cancelled": 0,
            "failed": 0,
            "permanent_failures": 0,
            "skipped": 0,
            "lease_lost": 0,
            "results": [],
            "failures": [],
            "errors": [],
            "last_error": self._last_error,
            "configured": self.configured,
            "stopped": "",
        }

    # -- 入队 -------------------------------------------------------------

    async def enqueue(
        self,
        kind: str,
        target_id: str,
        *,
        payload: Any,
        scope_id: str = "",
        source_revision: str = "",
        force: bool = False,
    ) -> bool:
        """把一条配置排进整理队列：``True`` = 这次真的新入队（或把死行拉起来）。

        ``payload`` 是**当前权威配置**（上层从配置里取的那份 dict），这里按处理器声明的
        ``input_fields`` 算出指纹——所以**只有指纹进库**，配置本体与密钥都不落库。

        返回 ``False``（不抛）的情形：``kind`` 没注册、``target_id`` 空、指纹算不出来
        （白名单字段一个都不在）。重复入队同一份指纹不产生第二行（库层幂等）。
        """

        handler = self._handlers.get(str(kind or "").strip())
        label = str(target_id or "").strip()
        if handler is None:
            await self._note_error(f"{ERROR_NO_HANDLER}: 没注册 {kind!r} 的处理器，这次入队忽略")
            return False
        if not label:
            await self._note_error(f"{ERROR_INPUT_MISSING}: 目标身份是空的，这次入队忽略")
            return False
        picked, dropped = pick_input_fields(payload, fields=handler.input_fields)
        if dropped:
            self._note_sensitive(kind, dropped)
        digest = fingerprint(payload, fields=handler.input_fields)
        if not digest:
            return False
        try:
            inserted = await self._call(
                "enqueue_config_index",
                kind=handler.kind,
                target_id=label,
                input_hash=digest,
                scope_id=str(scope_id or ""),
                source_revision=str(source_revision or ""),
                now=self._now(),
                force=bool(force),
            )
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 入队失败：{exc!r}")
            return False
        return bool(inserted)

    def _note_sensitive(self, kind: str, dropped: Sequence[str]) -> None:
        """记下"白名单里有敏感字段被扣掉"（观测点；不当成错误，否则日志会刷屏）。"""

        for name in dropped:
            item = f"{kind}:{name}"
            if item not in self.dropped_fields:
                self.dropped_fields.append(item)

    # -- 批处理 -----------------------------------------------------------

    async def run_batch(self, *, kind: str = "") -> dict[str, Any]:
        """跑一批整理作业，返回 :meth:`_empty_result` 那套计数键。

        流程（每一步失败都**只降级、不外抛**）：

        1. **没有能跑的处理器直接收工**：``stopped='no_handler'`` / ``'not_ready'``，
           一次数据库调用都不发（这保证"没接生成器 = 与没有这个功能逐字节相同"）；
        2. **按 kind 认领**：``claim_config_index_due``（回收过期租约 + 原子 UPDATE +
           按令牌取回），一次最多 ``batch_size`` 行；
        3. **逐行重读权威输入**：``handler.build_input(target_id)``。返回 ``None`` =
           目标已被删 / 禁用 → 作业标 ``cancelled``（**这不是失败**，不烧重试次数）；
        4. **重算指纹**：与队列行不一致（排队期间又改过）→ 标 ``stale``，不调模型
           （这就是"过期结果丢弃"的入口）；``handler.current_fingerprint`` 给了就再核一遍；
        5. **有界并发 + 单次超时**：最多 ``max_concurrency`` 个请求同时在跑，每个套
           ``asyncio.wait_for(timeout_seconds)``；
        6. **只留允许回写的字段**：:func:`filter_allowed_fields` 扣掉越权键与人工锁；
        7. **带令牌写回**：``complete_config_index``；
           ``superseded``（更新的作业已经在等）→ 标 ``stale``；
           ``lease_lost``（租约被回收给别人）→ 什么都不写（记 ``lease_lost``）；
        8. **失败**：``attempts`` 到顶标 ``failed``（记 ``permanent_failures``），
           否则退回 ``pending`` 并退避 ``min(60, 2 ** attempts)``；
        9. ``budget_seconds`` 用完：还没跑的行**交还租约**（并把认领时那次 ``attempts + 1``
           抵掉），留给下一批，记 ``skipped``。

        返回值里 ``results`` 是"算好等落配置"的清单（``row_id`` / ``kind`` /
        ``target_id`` / ``payload``），``failures`` 是失败清单（供上层把派生信息标成
        ``failed``）。两个清单都只有本批的量级。
        """

        outcome = self._empty_result()
        if not self._handlers:
            outcome["stopped"] = ERROR_NO_HANDLER
            return outcome
        wanted = str(kind or "").strip()
        if wanted:
            handler = self._handlers.get(wanted)
            if handler is None:
                outcome["stopped"] = ERROR_UNKNOWN_KIND
                return outcome
            kinds = [handler]
        else:
            kinds = list(self._handlers.values())
        runnable = [item for item in kinds if item.ready()]
        if not runnable:
            outcome["stopped"] = "not_ready"
            outcome["errors"].append("没有可用的整理处理器（缺 build_input / generate），这一批不发请求")
            return outcome
        if self._in_batch:
            outcome["stopped"] = "busy"
            return outcome
        self._in_batch = True
        try:
            return await self._run_kinds(runnable, outcome)
        except Exception as exc:  # 兜底：这个函数永远不往外抛
            outcome["errors"].append(f"整理批失败：{exc!r}")
            outcome["stopped"] = "error"
            await self._note_error(outcome["errors"][-1])
            outcome["last_error"] = self._last_error
            return outcome
        finally:
            self._in_batch = False

    async def _run_kinds(
        self, kinds: Sequence[ConfigIndexHandler], outcome: dict[str, Any]
    ) -> dict[str, Any]:
        """按类型依次认领 + 并发跑完一批（正身，已经过了两道门）。"""

        started = self._now()
        deadline = self._deadline(started, self.budget_seconds)
        semaphore = asyncio.Semaphore(self.max_concurrency)
        tasks: list[Awaitable[None]] = []
        stops: list[str] = []
        for handler in kinds:
            token = uuid.uuid4().hex
            try:
                rows = await self._call(
                    "claim_config_index_due",
                    kind=handler.kind,
                    owner=self.owner,
                    token=token,
                    now=started,
                    limit=self.batch_size,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                outcome["errors"].append(f"认领失败（{handler.kind}）：{exc!r}")
                stops.append("claim_error")
                continue
            picked = [row for row in (rows or ()) if isinstance(row, dict)]
            outcome["scanned"] += len(picked)
            outcome["leased"] += len(picked)
            for row in picked:
                tasks.append(
                    self._run_one(
                        handler, row, token, semaphore, deadline=deadline, outcome=outcome
                    )
                )
        if tasks:
            await asyncio.gather(*tasks)
        if not outcome["failed"] and not outcome["lease_lost"]:
            # 一批全部处理干净（"越权字段被扣掉"这种提示不算失败）：把旧错误清掉。
            await self._note_error("")
        outcome["last_error"] = self._last_error
        outcome["stopped"] = _batch_stop(outcome, stops)
        return outcome

    async def _run_one(
        self,
        handler: ConfigIndexHandler,
        row: dict[str, Any],
        token: str,
        semaphore: asyncio.Semaphore,
        *,
        deadline: float,
        outcome: dict[str, Any],
    ) -> None:
        """处理一行作业（**永远不抛**：所有异常都变成计数与 last_error）。"""

        row_id = _int_or(row.get("id"), 0)
        target_id = str(row.get("target_id") or "")
        if row_id <= 0 or not target_id:
            outcome["errors"].append("队列行缺 id / target_id，已跳过")
            return
        if self._now() >= deadline:
            # 预算用完：把这一行原样交还（抵掉认领时那次 attempts + 1），下一批再来。
            await self._release(row, token, attempts=_int_or(row.get("attempts"), 1) - 1)
            outcome["skipped"] += 1
            return
        async with semaphore:
            await self._generate_and_store(handler, row, row_id, target_id, token, outcome)

    async def _generate_and_store(
        self,
        handler: ConfigIndexHandler,
        row: dict[str, Any],
        row_id: int,
        target_id: str,
        token: str,
        outcome: dict[str, Any],
    ) -> None:
        """取输入 → 核指纹 → 调模型 → 过滤字段 → 带令牌写回（一行的全部逻辑）。"""

        now = self._now()
        try:
            payload = await _maybe_await(handler.build_input(target_id))
        except Exception as exc:
            await self._fail(handler, row, token, f"{ERROR_INTERNAL}: 取输入失败：{exc!r}", outcome)
            return
        if payload is None:
            # 目标没了 / 被禁用：作废作业，**不调模型**（删除/禁用即失效）。
            await self._cancel(row_id, now=now)
            outcome["cancelled"] += 1
            return
        picked, dropped = pick_input_fields(payload, fields=handler.input_fields)
        if dropped:
            self._note_sensitive(handler.kind, dropped)
        digest = fingerprint(payload, fields=handler.input_fields)
        if not digest or digest != str(row.get("input_hash") or ""):
            # 排队期间定义又改过（或者根本算不出指纹）：这一行的结果不该落。
            await self._stale(row_id, now=now)
            outcome["stale"] += 1
            return
        if callable(handler.current_fingerprint):
            try:
                current = await _maybe_await(handler.current_fingerprint(target_id))
            except Exception:
                current = ""
            if current and str(current) != str(row.get("input_hash") or ""):
                await self._stale(row_id, now=now)
                outcome["stale"] += 1
                return
        locked = await self._locked_fields(handler, target_id)
        try:
            raw = await asyncio.wait_for(
                _maybe_await(
                    handler.generate(
                        picked,
                        kind=handler.kind,
                        target_id=target_id,
                        model_slot=handler.model_slot,
                    )
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._fail(
                handler,
                row,
                token,
                f"{ERROR_TIMEOUT}: {self.timeout_seconds:g}s 内没有返回",
                outcome,
            )
            return
        except Exception as exc:
            await self._fail(handler, row, token, f"{ERROR_PROVIDER}: {exc!r}", outcome)
            return
        parsed = raw
        if callable(handler.parse_result):
            try:
                parsed = handler.parse_result(raw)
            except Exception as exc:
                await self._fail(handler, row, token, f"{ERROR_PROVIDER}: 解析结果失败：{exc!r}", outcome)
                return
        allowed, rejected, skipped_locked = filter_allowed_fields(
            parsed, allowed=handler.allowed_fields, locked=locked
        )
        if rejected or skipped_locked:
            outcome["errors"].append(
                f"整理结果有 {len(rejected)} 个越权字段、{len(skipped_locked)} 个被人工锁跳过"
                f"（{handler.kind}/{target_id}）"
            )
        if not allowed:
            await self._fail(handler, row, token, f"{ERROR_EMPTY_RESULT}: 没有可回写的字段", outcome)
            return
        reason = await self._complete(row_id, token, allowed, now=self._now())
        if reason == "superseded":
            await self._stale(row_id, now=self._now())
            outcome["stale"] += 1
            return
        if reason != "ready":
            outcome["lease_lost"] += 1
            outcome["errors"].append(f"写回被拒（{handler.kind}/{target_id}）：{reason}")
            return
        outcome["ready"] += 1
        outcome["results"].append(
            {
                "row_id": row_id,
                "kind": handler.kind,
                "target_id": target_id,
                "scope_id": str(row.get("scope_id") or ""),
                "source_revision": str(row.get("source_revision") or ""),
                "input_hash": str(row.get("input_hash") or ""),
                "payload": allowed,
            }
        )

    async def _locked_fields(self, handler: ConfigIndexHandler, target_id: str) -> tuple[str, ...]:
        """问一下这个目标现在有哪些字段被人工锁住（问不到就当没有锁）。"""

        if not callable(handler.locked_fields):
            return ()
        try:
            raw = await _maybe_await(handler.locked_fields(target_id))
        except Exception:
            return ()
        try:
            return tuple(str(item) for item in (raw or ()) if str(item))
        except TypeError:
            return ()

    # -- 状态写回（全部带令牌、全部不外抛） --------------------------------

    async def _complete(
        self, row_id: int, token: str, payload: dict[str, Any], *, now: float
    ) -> str:
        """带令牌写回结果（``ready`` / ``superseded`` / ``lease_lost``；出错误记 ``lease_lost``）。"""

        try:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            body = "{}"
        try:
            return str(
                await self._call(
                    "complete_config_index", row_id, lease_token=token, result_json=body, now=now
                )
                or ""
            )
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 写回失败：{exc!r}")
            return "lease_lost"

    async def _stale(self, row_id: int, *, now: float) -> None:
        """标 ``stale``（迟到结果丢弃）。"""

        try:
            await self._call(
                "mark_config_index", row_id, status="stale", now=now, error_code=ERROR_FINGERPRINT_CHANGED
            )
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 标 stale 失败：{exc!r}")

    async def _cancel(self, row_id: int, *, now: float) -> None:
        """标 ``cancelled``（目标删除 / 禁用即失效）。"""

        try:
            await self._call("mark_config_index", row_id, status="cancelled", now=now)
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 作废失败：{exc!r}")

    async def _release(self, row: dict[str, Any], token: str, *, attempts: int) -> None:
        """把租约交还成 ``pending``（预算用完时用；抵掉认领时那次 ``attempts + 1``）。"""

        row_id = _int_or(row.get("id"), 0)
        if row_id <= 0:
            return
        try:
            await self._call(
                "release_config_index_lease",
                row_id,
                lease_token=token,
                now=self._now(),
                attempts=max(0, attempts),
                next_attempt_at=self._now() + backoff_seconds(attempts),
            )
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 交还租约失败：{exc!r}")

    async def _fail(
        self,
        handler: ConfigIndexHandler,
        row: dict[str, Any],
        token: str,
        reason: str,
        outcome: dict[str, Any],
    ) -> None:
        """记一次失败（或到顶标 ``failed``），并记 :attr:`last_error`。"""

        row_id = _int_or(row.get("id"), 0)
        attempts = max(1, _int_or(row.get("attempts"), 1))
        terminal = attempts >= self.max_attempts
        error_code = str(reason).split(":", 1)[0].strip() or ERROR_PROVIDER
        try:
            marked = await self._call(
                "fail_config_index",
                row_id,
                lease_token=token,
                error_code=error_code,
                attempts=attempts,
                next_attempt_at=0.0
                if terminal
                else self._now() + backoff_seconds(attempts),
                terminal=terminal,
                now=self._now(),
            )
        except Exception as exc:
            marked = False
            outcome["errors"].append(f"记失败失败：{exc!r}")
        outcome["failed"] += 1
        if terminal and marked:
            outcome["permanent_failures"] += 1
        outcome["failures"].append(
            {
                "row_id": row_id,
                "kind": handler.kind,
                "target_id": str(row.get("target_id") or ""),
                "error_code": error_code,
                "detail": str(reason),
                "terminal": terminal,
            }
        )
        outcome["errors"].append(f"{handler.kind}/{row.get('target_id')}: {reason}")
        await self._note_error(reason)

    # -- 队列管理 ---------------------------------------------------------

    async def invalidate(
        self, *, kind: str = "", target_id: str = "", target_ids: Iterable[str] = ()
    ) -> int:
        """目标**删除 / 禁用**时立即失效：活跃行标 ``cancelled``、清结果与租约，返回作废条数。

        ``kind`` / ``target_id`` 都给不出目标时返回 0（不抛）。
        """

        try:
            return int(
                await self._call(
                    "invalidate_config_index",
                    kind=str(kind or ""),
                    target_id=str(target_id or ""),
                    target_ids=tuple(str(item) for item in (target_ids or ())),
                    now=self._now(),
                )
                or 0
            )
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 作废失败：{exc!r}")
            return 0

    async def apply_ready(
        self,
        apply: Callable[[dict[str, Any]], Any],
        *,
        kind: str = "",
        limit: int = DEFAULT_BATCH_SIZE * 4,
    ) -> dict[str, Any]:
        """把 ``ready`` 的结果交给上层**落进配置**，成功才翻成 ``done``。

        ``apply(row) -> bool``（同步 / 异步都认，``row`` 是队列行 dict，``result_json``
        里就是**已经过滤过的**派生字段）。返回 ``True`` 才 :meth:`apply_config_index_result`
        翻状态——"同一份结果落两遍"天然不可能，因为第二次已经拿不到 ``ready`` 行了。

        返回 ``{"applied", "skipped", "failed", "errors"}``；任何异常只记账不外抛。
        """

        outcome = {"applied": 0, "skipped": 0, "failed": 0, "errors": []}
        try:
            rows = await self._call(
                "list_config_index_rows",
                kind=str(kind or ""),
                statuses=("ready",),
                limit=max(1, _int_or(limit, 16)),
            )
        except Exception as exc:
            outcome["errors"].append(f"列 ready 行失败：{exc!r}")
            return outcome
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            row_id = _int_or(row.get("id"), 0)
            if row_id <= 0:
                continue
            try:
                done = await _maybe_await(apply(row))
            except Exception as exc:
                outcome["failed"] += 1
                outcome["errors"].append(f"落配置失败（{row.get('target_id')}）：{exc!r}")
                continue
            if not done:
                outcome["skipped"] += 1
                continue
            try:
                applied = await self._call(
                    "apply_config_index_result", row_id, now=self._now()
                )
            except Exception as exc:
                outcome["failed"] += 1
                outcome["errors"].append(f"标记已落失败（{row.get('target_id')}）：{exc!r}")
                continue
            if applied:
                outcome["applied"] += 1
            else:
                outcome["skipped"] += 1
        if not outcome["errors"] and not outcome["failed"]:
            await self._note_error("")
        return outcome

    async def recover(self, *, force: bool = True, kind: str = "") -> int:
        """**重启可恢复**：把 ``leased`` 的行收回来重新排队，返回回收条数。

        ``force=True``（默认）收**所有**租约——单进程插件启动时那些租约一定是上一个已经
        死掉的进程留下的；多 worker 部署要用 ``force=False``（只收过期的）。
        """

        try:
            return int(
                await self._call(
                    "recover_config_index_leases",
                    kind=str(kind or ""),
                    now=self._now(),
                    force=bool(force),
                )
                or 0
            )
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 回收租约失败：{exc!r}")
            return 0

    async def pending_count(self, *, kind: str = "", status: str = "pending") -> int:
        """还有多少行没处理完（``status`` 给空串 = 不分状态全数）；查不到回 0。"""

        try:
            return int(
                await self._call(
                    "count_config_index", kind=str(kind or ""), status=str(status or "")
                )
                or 0
            )
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 取待整理条数失败：{exc!r}")
            return 0

    async def rows(
        self,
        *,
        kind: str = "",
        target_id: str = "",
        statuses: Sequence[str] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列队列行（编辑器 / 状态接口用）；读不到回空表。"""

        try:
            picked = await self._call(
                "list_config_index_rows",
                kind=str(kind or ""),
                target_id=str(target_id or ""),
                statuses=tuple(statuses or ()),
                limit=limit,
            )
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 列队列行失败：{exc!r}")
            return []
        return [item for item in (picked or ()) if isinstance(item, dict)]

    async def clear(self, *, kind: str = "") -> int:
        """**删行**（所有状态），返回删掉的条数；``kind`` 给了就只删那一类。"""

        try:
            return int(await self._call("clear_config_index", kind=str(kind or "")) or 0)
        except Exception as exc:
            await self._note_error(f"{ERROR_INTERNAL}: 清队列失败：{exc!r}")
            return 0


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    """同步返回值原样返回，awaitable 就 await（处理器回调两种都认）。"""

    if inspect.isawaitable(value):
        return await value
    return value


def _int_or(value: Any, default: int) -> int:
    """整数容错：``int()`` 不了就用默认值。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _positive_or(value: Any, default: float) -> float:
    """正有限浮点容错：非正 / 非有限 / 转不成一律用默认值（``0`` 不是"不限"）。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0.0:
        return default
    return number


def _batch_stop(outcome: dict[str, Any], stops: Sequence[str]) -> str:
    """这一批为什么结束（逐行终态看那几个计数键，别只看这里）。

    ``errors`` 里既有真错误也有提示（"结果里有越权字段被扣掉"），所以**不拿它判成败**；
    成败看 ``failed`` / ``lease_lost`` 这两个逐行计数。
    """

    if outcome["ready"]:
        return "failed" if (outcome["failed"] or outcome["lease_lost"]) else "ok"
    if outcome["failed"]:
        return "failed"
    if outcome["lease_lost"]:
        return "lease_lost"
    if outcome["stale"]:
        return "stale"
    if outcome["cancelled"]:
        return "cancelled"
    if outcome["skipped"]:
        return "over_budget"
    if stops:
        return str(stops[0])
    return "empty"


__all__ = [
    "CONFIG_INDEX_LAST_ERROR_KV_KEY",
    "ConfigIndexHandler",
    "ConfigIndexQueue",
    "DEFAULT_BATCH_BUDGET_SECONDS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_TIMEOUT_SECONDS",
    "ERROR_EMPTY_RESULT",
    "ERROR_FINGERPRINT_CHANGED",
    "ERROR_INPUT_MISSING",
    "ERROR_INTERNAL",
    "ERROR_NO_HANDLER",
    "ERROR_PROVIDER",
    "ERROR_TIMEOUT",
    "ERROR_UNKNOWN_KIND",
    "MODEL_SLOT_CONSOLIDATE",
    "SENSITIVE_FIELD_MARKS",
    "backoff_seconds",
    "filter_allowed_fields",
    "fingerprint",
    "pick_input_fields",
]
