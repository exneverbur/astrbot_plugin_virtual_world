"""存储自检 + 写失败留痕（2026-10 磁盘满事故的现场取证）。

**真实事故**：生产机磁盘写满 → 每轮回复都要写 ``world_state`` /
``decision_batches`` / 记忆 → sqlite 抛 ``database or disk is full`` → 这些异常在
``except Exception: pass`` 里被吞掉 → 她**整轮不说话**，用户只看到「她不理我」，
完全不知道是磁盘满了。现场数据：``AstrBot/data/logs`` 只有几百 K，而
``AstrBot/data/plugin_data/astrbot_plugin_virtual_world`` 有 179 MB。

所以这个模块做三件事，前三步都**只读**、失败只记进报告、绝不抛：

1. 量清楚「磁盘还能写多少 / 数据目录有多大 / 谁最占地方」，给启动日志和
   ``/vw status`` 用——出事的当下要能一眼看出来；
2. 把写失败记在一个能被看见的地方（最近一次 + 连续次数），而不是吞掉；
3. **（下半段，唯一会删东西的一步）** 清旧快照：每类只留最近 ``keep_snapshots`` 份；
   ``.vwtest_repair/`` 例外——那是测试修复残留，**整类清空**（见 ``PURGE_CATEGORIES``）。
   那 340 MB 里 279 MB 是插件自己留下的一次性残留（修复前的库、升级前快照、
   测头像备份），用户不会自己去删，于是一直堆着。删之前有两条硬约束：
   类别是**白名单精确匹配**（不是宽泛 glob），待删路径 ``resolve()`` 之后必须仍在
   数据目录之内（防 ``..`` / 大小写 / junction），符号链接一律跳过。

阈值一律做成参数：单测用临时目录 + 假的 ``disk_usage``，不依赖真实磁盘大小。
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

MIB = 1024 * 1024

DEFAULT_MIN_FREE_BYTES = 500 * MIB
"""分区可用空间低于这个值就告警（500 MB，留够 sqlite 写 WAL + 临时文件）。"""

DEFAULT_MAX_DATA_BYTES = 300 * MIB
"""数据目录超过这个值就告警（179 MB 那次就是这么被忽略掉的，阈值必须低于它才看得见）。"""

STATUS_TOP_FILES = 5
"""``/vw status`` 里列几个最大的文件。"""

ESCALATE_AFTER = 3
"""连续这么多次写失败就额外 WARN 一次「可能是磁盘满/只读」。"""

_TEMPLATE_LIMIT = 200
"""异常文本只留前 200 字（一行日志放得下，也够定位）。"""


def human_bytes(size: Any) -> str:
    """把字节数写成人看得懂的 ``49.4 MB``（永不抛）。"""

    try:
        value = float(size)
    except (TypeError, ValueError):
        return "?"
    if value < 0:
        return "?"
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"


def _truncate(text: Any, limit: int = _TEMPLATE_LIMIT) -> str:
    blob = " ".join(str(text or "").split())
    if len(blob) <= limit:
        return blob
    return blob[:limit] + "…"


def _stamp(at: Any) -> str:
    """时间戳 → ``2026-10-22 18:10:05``（转不了就给原值）。"""

    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(at)))
    except Exception:  # noqa: BLE001 - 显示用的，转不了无所谓
        return str(at or "")


@dataclass(frozen=True)
class FileSize:
    """数据目录里的一个文件（``path`` 是相对数据目录的写法，日志短一点）。"""

    path: str
    size: int


@dataclass
class StorageReport:
    """一次自检的结论（只读快照，构造后不再变）。"""

    data_dir: str = ""
    free_bytes: int | None = None
    """数据目录所在分区的可用字节数；量不到就是 ``None``（例如目录还没建）。"""

    total_bytes: int = 0
    """数据目录总大小（递归，含 ``.bak`` / ``.broken`` / 导出 zip / 隐藏目录）。"""

    db_bytes: int = 0
    wal_bytes: int = 0
    shm_bytes: int = 0
    top_files: list[FileSize] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # ---------------- 判断 ----------------

    def low_space(
        self,
        *,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        max_data_bytes: int = DEFAULT_MAX_DATA_BYTES,
    ) -> bool:
        """要不要告警：可用空间不够 / 数据目录太大，任意一条成立。"""

        if self.free_bytes is not None and self.free_bytes < int(min_free_bytes):
            return True
        return int(self.total_bytes) > int(max_data_bytes)

    def warning_text(
        self,
        *,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        max_data_bytes: int = DEFAULT_MAX_DATA_BYTES,
    ) -> str:
        """启动时那**一行** WARN（文件清单不进日志，清单只给 ``/vw status``）。

        正常时回空串，调用方直接 `if text:` 就够。
        """

        reasons: list[str] = []
        if self.free_bytes is not None and self.free_bytes < int(min_free_bytes):
            reasons.append(f"磁盘可用只剩 {human_bytes(self.free_bytes)}")
        if int(self.total_bytes) > int(max_data_bytes):
            reasons.append(
                f"数据目录 {human_bytes(self.total_bytes)}"
                f"（超 {human_bytes(max_data_bytes)}）"
            )
        if not reasons:
            return ""
        return (
            "[virtual_world] 存储告警："
            + "；".join(reasons)
            + f"。磁盘满会让世界状态/决策写不进去，她就整轮不说话（路径 {self.data_dir}）。"
            "清掉 .bak/.broken/快照可以立刻腾出空间；明细看 /vw status。"
        )

    def status_lines(
        self,
        *,
        write_health: "WriteFailureTracker | None" = None,
        top_files: int = STATUS_TOP_FILES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        max_data_bytes: int = DEFAULT_MAX_DATA_BYTES,
        snapshot_line: str = "",
        vacuum_line: str = "",
    ) -> list[str]:
        """``/vw status`` 里那段「存储」：可用空间 / 目录 / db / wal / TOP N / 最近写失败。

        **已启用和未启用两条路都要带上它**——出故障时最需要确认的恰恰是"没启用"那条。
        ``snapshot_line`` 是引擎补的「旧快照」那一行（这里只管排版，不碰磁盘）。
        ``vacuum_line`` 同理：引擎补的「库空间」一行（空闲页 + 上次回收），空串不占行。
        """

        free_text = human_bytes(self.free_bytes) if self.free_bytes is not None else "量不到"
        lines: list[str] = []
        if self.low_space(min_free_bytes=min_free_bytes, max_data_bytes=max_data_bytes):
            lines.append(
                f"⚠ 存储告急：磁盘可用 {free_text}，数据目录 "
                f"{human_bytes(self.total_bytes)}"
                "（可以手动删下面列出的 .bak/.broken/快照腾空间，插件不会自己删）"
            )
        lines.append(
            f"存储：磁盘可用 {free_text}；数据目录 {human_bytes(self.total_bytes)}"
        )
        lines.append(
            f"数据库：state.db {human_bytes(self.db_bytes)}"
            f"　-wal {human_bytes(self.wal_bytes)}"
            f"　-shm {human_bytes(self.shm_bytes)}"
        )
        if vacuum_line:
            lines.append(vacuum_line)
        last_error = (
            write_health.status_line() if write_health is not None else "无"
        )
        lines.append(f"最近一次写失败：{last_error}")
        if snapshot_line:
            lines.append(snapshot_line)
        if top_files > 0 and self.top_files:
            lines.append(f"最大的 {min(int(top_files), len(self.top_files))} 个文件：")
            for item in self.top_files[: int(top_files)]:
                lines.append(f"· {human_bytes(item.size)}　{item.path}")
        return lines


def _existing_anchor(path: str) -> str:
    """找最近的、确实存在的上级目录（目录还没建时也能量分区）。"""

    probe = os.path.abspath(path or ".")
    for _ in range(20):
        if os.path.exists(probe):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return probe


def _walk_files(root: str, errors: list[str]) -> list[FileSize]:
    """递归列出 ``root`` 下所有文件（含隐藏 / ``.bak`` / ``.broken`` / zip）。

    单个目录读不动只记一笔继续走：自检本身**绝不许**因为权限或路径太长而抛。
    """

    found: list[FileSize] = []
    if not root or not os.path.isdir(root):
        return found
    base = os.path.abspath(root)
    for current, _dirs, names in os.walk(base):
        for name in names:
            full = os.path.join(current, name)
            try:
                size = os.path.getsize(full)
            except OSError as exc:
                errors.append(f"读不到 {full}：{exc}")
                continue
            try:
                relative = os.path.relpath(full, base)
            except ValueError:
                relative = full
            found.append(FileSize(path=relative.replace(os.sep, "/"), size=int(size)))
    return found


def collect_report(
    data_dir: str,
    *,
    top_files: int = STATUS_TOP_FILES,
    disk_usage: Callable[[str], Any] | None = None,
) -> StorageReport:
    """量一次数据目录（**只读、绝不抛**；量不到的项记进 ``errors``）。

    ``disk_usage`` 可注入（默认 ``shutil.disk_usage``）：单测要的是"可用空间只剩
    100 MB 时会发生什么"，不该去挪真实磁盘。
    """

    report = StorageReport(data_dir=str(data_dir or ""))
    usage = disk_usage or shutil.disk_usage
    try:
        report.free_bytes = int(usage(_existing_anchor(report.data_dir)).free)
    except Exception as exc:  # noqa: BLE001 - 量不到分区不算故障，报告里写明
        report.free_bytes = None
        report.errors.append(f"磁盘可用空间读不到：{exc}")

    files = _walk_files(report.data_dir, report.errors)
    report.total_bytes = sum(item.size for item in files)
    by_name = {item.path: item.size for item in files}
    report.db_bytes = int(by_name.get("state.db", 0))
    report.wal_bytes = int(by_name.get("state.db-wal", 0))
    report.shm_bytes = int(by_name.get("state.db-shm", 0))
    ordered = sorted(files, key=lambda item: (-item.size, item.path))
    report.top_files = ordered[: max(0, int(top_files))]
    return report


# ---------------------------------------------------------------------------
# 写失败留痕
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteFailure:
    """一次写失败：什么时候、哪条写、什么错。"""

    at: float = 0.0
    op: str = "write"
    error: str = ""
    path: str = ""

    def line(self) -> str:
        """一行给 ``/vw status``：``2026-10-22 18:10:05 insert:world_state · 异常``。"""

        return f"{_stamp(self.at)} {self.op} · {self.error}"

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at, "op": self.op, "error": self.error, "path": self.path}


class WriteFailureTracker:
    """最近一次写失败 + 连续失败次数（**只记账，不抛**）。

    为什么需要它：写失败以前被 ``except Exception: pass`` 吞掉，用户看不到任何
    症状；现在失败至少留下「时间 + 异常类名 + 前 200 字」，``/vw status`` 一眼能看。
    同一个异常对象被上层重复上报只算**一次**（sqlite 的写失败会从 ``Database``
    冒到引擎，两层都会记，不合并的话"连续 3 次"会被一次回复凑满）。
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._last: WriteFailure | None = None
        self._last_exc: BaseException | None = None
        self._consecutive = 0
        self._escalated = False
        self._total = 0

    def record(self, op: str, exc: BaseException, *, path: str = "") -> WriteFailure:
        """记一次写失败，返回记下来的那条（调用方可以挂到引擎上）。"""

        text = f"{type(exc).__name__}: {_truncate(str(exc))}"
        try:
            at = float(self._clock())
        except Exception:  # noqa: BLE001 - 时钟坏了也要把失败记下来
            at = 0.0
        failure = WriteFailure(
            at=at, op=str(op or "write"), error=text, path=str(path or "")
        )
        same = exc is self._last_exc
        self._last = failure
        self._last_exc = exc
        self._total += 1
        if not same:
            self._consecutive += 1
        return failure

    def record_success(self) -> None:
        """一次写成功就把连续计数清零（"连续"必须真的是连续）。"""

        self._consecutive = 0
        self._escalated = False

    @property
    def last(self) -> WriteFailure | None:
        return self._last

    def failed_since(self, started_at: float) -> WriteFailure | None:
        """``started_at`` 之后有没有写失败过：有就返回最近那条，没有就 ``None``。

        用「这一轮开始时的时间戳」而不是「历史上失败过」判断：昨天失败过、今天
        这一轮全是成功写，不该再补提示（用户要的是"这一轮是不是没记住"）。
        """

        last = self._last
        if last is None:
            return None
        try:
            if float(last.at or 0.0) < float(started_at or 0.0):
                return None
        except (TypeError, ValueError):
            return None
        return last

    @property
    def consecutive(self) -> int:
        return self._consecutive

    @property
    def total(self) -> int:
        return self._total

    def needs_escalation(self) -> bool:
        """连续失败到线了没有——**每一条连续只 WARN 一次**（靠 ``mark_escalated``）。"""

        return self._consecutive >= ESCALATE_AFTER and not self._escalated

    def mark_escalated(self) -> None:
        self._escalated = True

    def status_line(self) -> str:
        """``/vw status`` 里那句「最近一次写失败：…」；没失败过就是「无」。"""

        if self._last is None:
            return "无"
        text = self._last.line()
        if self._consecutive > 1:
            text += f"（连续 {self._consecutive} 次）"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "last": self._last.to_dict() if self._last is not None else None,
            "consecutive": self._consecutive,
            "total": self._total,
        }


# ---------------------------------------------------------------------------
# 旧快照清理（本模块唯一会删东西的一段）
# ---------------------------------------------------------------------------

KEEP_SNAPSHOTS = 1
"""每类旧快照默认保留几份（``StorageConfig.keep_snapshots`` 的默认值跟着它走）。

拍板结论是「每类只留最近 1 份」：留 3 份实测首次只能腾 1.8 MB，涨得比清得快；
留 1 份仍然保住「最近一次可以回退的东西」。所有给用户看的文案都按实际
``keep`` 动态渲染，别在这里写死数字，否则改默认值就会出现假说明。
"""

SNAPSHOT_CATEGORIES: tuple[str, ...] = (
    "state_bak",
    "state_broken",
    "vwtest_repair",
    "backups_snapshot",
    "world_bak",
    "vacuum_backup",
)
"""六类快照的固定顺序（顺序本身没有语义，只用来让日志/测试稳定）。"""

CATEGORY_LABELS: dict[str, str] = {
    "state_bak": "state.db 备份（头像 / 修复前）",
    "state_broken": "损坏库留存",
    "vwtest_repair": "测试修复残留（整类清空）",
    "backups_snapshot": "升级前快照目录",
    "world_bak": "world.json 备份",
    "vacuum_backup": "VACUUM 前的库备份",
}
"""类别 → 给人看的中文标签（``/vw status`` 与文档共用，别的模块不要再另起一套叫法）。"""

PURGE_CATEGORIES = frozenset({"vwtest_repair"})
"""整类清空的类别：**不占保留名额**，候选直接进删除。

为什么只有它：``.vwtest_repair/`` 是「库坏了、跑修复脚本时拷出来的试验品」
（``state.db`` / ``state_fixed.db`` / ``state_rebuild.db``），不是任何时刻的
运行时数据，也不存在「回退到某一份」的语义——留着只是白占 30 MB×N。用户已经
拍板整类清空；别的四类仍然按 ``keep_snapshots`` 留最近 N 份。
"""

PURGE_REASON = "测试修复残留：整类清空（不占保留名额）"
"""删除理由文案：让日志/计划里一眼看出「这是测试残留，不是状态备份」。"""

PROTECTED_NAMES = frozenset(
    {"state.db", "state.db-wal", "state.db-shm", "world.json", "sessions.json"}
)
"""顶层绝对禁区：这五个名字无论多旧、多大都不许删。"""

PROTECTED_DIRS = frozenset(
    {"presets", "media", "meme_sent", "memes", "meme_assets", "docs", "tests"}
)
"""路径里只要出现这些目录名（任何一层）就整条拒绝。"""

VTEST_REPAIR_DIR = ".vwtest_repair"
BACKUPS_DIR = "backups"
SNAPSHOT_DIR_PREFIX = "snapshot-"

VACUUM_BACKUP_PREFIX = "vacuum-"
"""``storage_vacuum`` 转换前留的库备份：``backups/vacuum-YYYYmmdd-HHMMSS.db``。

每次从 ``auto_vacuum=NONE`` 切到 ``INCREMENTAL`` 都会拷一份**约等于库大小**的
完整副本（见 ``core/storage_vacuum.py`` 的 ``BACKUP_PREFIX``）。只有第一次切换才
会留下它，但每装一次插件/每换一个数据目录就会再来一份，所以它和别的快照一样
按「每类留最近 N 份」清，否则会无限累积。
"""

VACUUM_STAMP_HEAD = 8
"""时间戳前半（``YYYYmmdd``）位数。"""

VACUUM_STAMP_TAIL = 6
"""时间戳后半（``HHMMSS``）位数。"""

_COMPANION_SUFFIXES = ("-wal", "-shm")
"""sqlite 的伴生文件后缀：跟着**被删的那个库**走，不单独算一份快照。"""


@dataclass(frozen=True)
class SnapshotEntry:
    """扫到的一条「可能该清」的路径（还没决定留还是删）。"""

    path: str
    relative: str
    """相对数据目录的写法（日志/status 里短一点，也方便和用户核对）。"""

    category: str
    size: int
    mtime: float
    is_dir: bool = False
    is_link: bool = False


@dataclass(frozen=True)
class CleanupItem:
    """清理计划里的一项：``action`` 只有 ``keep`` / ``delete``。

    计划是**纯数据**：算的时候一个文件都不动，删的时候再照着重验一遍路径规则
    （见 :func:`apply_snapshot_cleanup`）——计划可能被跨进程传、也可能隔了很久才执行。
    """

    path: str
    category: str
    size: int
    mtime: float
    action: str
    reason: str = ""
    relative: str = ""
    data_dir: str = ""
    """所属数据目录（硬性包含断言的基准；空 = 拒绝删除）。"""

    is_companion: bool = False
    """``-wal`` / ``-shm`` 伴生文件：跟着被删的库一起删，不占保留名额。"""

    is_dir: bool = False

    @property
    def will_delete(self) -> bool:
        return self.action == "delete"


@dataclass
class CleanupFailure:
    """一项没删成（权限 / 占用 / 路径规则拒绝），其它项照删不误。"""

    path: str
    category: str = ""
    error: str = ""

    def line(self) -> str:
        return f"{self.category or '?'}：{self.path} · {self.error}"


@dataclass
class CleanupResult:
    """一次清理的结果（``/vw status`` 那行和日志都用它）。"""

    deleted: int = 0
    freed_bytes: int = 0
    planned: int = 0
    """计划里本来要删几项（``dry_run`` 时是"会删几项"）。"""

    kept: int = 0
    failed: list[CleanupFailure] = field(default_factory=list)
    dry_run: bool = False
    purged: int = 0
    """其中属于「整类清空」类别（``PURGE_CATEGORIES``）的项数，给人看清是测试残留。"""

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def summary(self, *, keep: int = KEEP_SNAPSHOTS) -> str:
        return snapshot_cleanup_summary(self, keep=keep)

    def record(self, *, at: float = 0.0) -> "SnapshotCleanupRecord":
        return SnapshotCleanupRecord(
            at=float(at),
            deleted=int(self.deleted),
            freed_bytes=int(self.freed_bytes),
            failed=self.failed_count,
        )


@dataclass(frozen=True)
class SnapshotCleanupRecord:
    """一次真实清理的留档（``/vw status`` 的「本次 / 上次」就靠它）。"""

    at: float = 0.0
    deleted: int = 0
    freed_bytes: int = 0
    failed: int = 0

    def line(self) -> str:
        text = f"{_stamp(self.at)} 清理 {int(self.deleted)} 项 / {human_bytes(self.freed_bytes)}"
        if self.failed:
            text += f"（失败 {int(self.failed)} 项）"
        return text


class SnapshotCleanupLog:
    """最近两次清理结果（新的在前，只留两条——够 `status` 写「本次 / 上次」）。"""

    def __init__(self, limit: int = 2) -> None:
        self._limit = max(1, int(limit))
        self._records: list[SnapshotCleanupRecord] = []

    def remember(self, record: SnapshotCleanupRecord) -> None:
        self._records.insert(0, record)
        del self._records[self._limit :]

    @property
    def records(self) -> list[SnapshotCleanupRecord]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)


def _emit(log: Callable[[str, str], None] | None, level: str, message: str) -> None:
    """日志回调（引擎传 ``self._log``）；回调本身出错也绝不影响清理。"""

    if log is None:
        return
    try:
        log(level, message)
    except Exception:  # noqa: BLE001 - 日志坏了不能把清理带崩
        pass


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _is_link_like(info: os.stat_result | None) -> bool:
    """符号链接 / Windows 目录联接（junction）/ 其它重解析点——**一律不跟进、不删除**。

    junction 在 Python 3.11 上 ``os.path.islink()`` 是 ``False``，但它指向别处：
    真让它进 ``shutil.rmtree`` 会顺着联接把**目标目录**里的东西删掉。所以这里不看
    "叫不叫 link"，只看文件属性里的重解析位。
    """

    if info is None:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    flags = getattr(info, "st_file_attributes", 0) or 0
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(flags & reparse)


def _is_reparse(path: str) -> bool:
    return _is_link_like(_lstat(path))


def _split_companion(name: str) -> tuple[str, bool]:
    """``x.db-wal`` → ``("x.db", True)``；没有伴生后缀就是 ``(name, False)``。"""

    lowered = name.lower()
    for suffix in _COMPANION_SUFFIXES:
        if lowered.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], True
    return name, False


def _path_parts(relative: str) -> list[str]:
    return [part for part in str(relative or "").replace("\\", "/").split("/") if part]


def _ascii_digits(text: str, length: int) -> bool:
    """``text`` 是不是恰好 ``length`` 位 ASCII 数字（``str.isdigit`` 会认全角数字）。"""

    return len(text) == length and all("0" <= char <= "9" for char in text)


def _is_vacuum_backup_name(name: str) -> bool:
    """``vacuum-YYYYmmdd-HHMMSS.db`` 的形状（时间戳必须是数字，别的名字不认）。

    为什么卡死形状而不是 ``vacuum-*.db``：这里的类别判定是删除的**唯一**依据，
    宽一位就多一分误伤；生产端（``storage_vacuum``）就是 ``%Y%m%d-%H%M%S``，
    认不出来的名字最坏是"不清理"，绝不该是"删掉"。
    """

    lowered = name.lower()
    if not lowered.startswith(VACUUM_BACKUP_PREFIX) or not lowered.endswith(".db"):
        return False
    stamp = lowered[len(VACUUM_BACKUP_PREFIX) : -len(".db")]
    head, separator, tail = stamp.partition("-")
    if not separator:
        return False
    return _ascii_digits(head, VACUUM_STAMP_HEAD) and _ascii_digits(tail, VACUUM_STAMP_TAIL)


def _classify_relative(relative: str, *, is_dir: bool) -> tuple[str, bool]:
    """把一条「相对数据目录的路径」归到某个快照类别。

    返回 ``(类别, 是否伴生文件)``；不匹配返回 ``("", False)``。
    **白名单式精确匹配**：只认下面这几条形状，绝不做 ``*.bak*`` 那种宽泛 glob——
    宽 glob 在整个数据目录上跑一次，迟早会命中 ``world.json`` 或某个用户的 ``.db``。
    """

    parts = _path_parts(relative)
    if not parts:
        return "", False
    name = parts[-1]
    lowered = name.lower()

    if is_dir:
        # 只认 backups/snapshot-* 这一层目录，不递归认里面的东西（整目录一起删）
        if (
            len(parts) == 2
            and parts[0] == BACKUPS_DIR
            and lowered.startswith(SNAPSHOT_DIR_PREFIX)
        ):
            return "backups_snapshot", False
        return "", False

    base, is_companion = _split_companion(name)
    base_lower = base.lower()

    if (
        len(parts) == 2
        and parts[0] == BACKUPS_DIR
        and _is_vacuum_backup_name(base_lower)
    ):
        # ``backups/vacuum-<时间戳>.db``：转换前的整库副本，和别的快照一样按份数清
        return "vacuum_backup", is_companion

    if len(parts) == 1:
        if base.startswith("state.db.bak-"):
            return "state_bak", is_companion
        if base.startswith("state.db.broken-"):
            return "state_broken", is_companion
        if base_lower.startswith(".vwtest-state-broken-") and base_lower.endswith(".db"):
            return "state_broken", is_companion
        if base.startswith("world.json.bak-"):
            return "world_bak", is_companion
        return "", False

    if (
        len(parts) == 2
        and parts[0] == VTEST_REPAIR_DIR
        and base_lower.endswith(".db")
    ):
        return "vwtest_repair", is_companion
    return "", False


def _protection_reason(relative: str) -> str:
    """禁区判定：返回原因，空串 = 这条路径可以碰。"""

    parts = _path_parts(relative)
    if not parts:
        return "空路径"
    name = parts[-1]
    if len(parts) == 1 and name in PROTECTED_NAMES:
        return f"{name} 是主库/主配置"
    if name.lower().endswith(".py"):
        return "python 源文件"
    for part in parts:
        if part.lower() in PROTECTED_DIRS:
            return f"{part}/ 是禁区目录"
    return ""


def _within_root(path: str, root: str) -> bool:
    """硬性断言：``path`` 解析（符号链接 + ``..`` + 大小写）之后必须仍在 ``root`` 之内。"""

    if not root or not path:
        return False
    try:
        base = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        target = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except OSError:
        return False
    if not base or not target:
        return False
    return target != base and target.startswith(base + os.sep)


def _dir_facts(path: str) -> tuple[int, float]:
    """目录整棵的大小与**最新** mtime（不跟进符号链接）。"""

    total = 0
    newest = 0.0
    info = _lstat(path)
    if info is not None:
        newest = float(info.st_mtime)
    try:
        walker = os.walk(path)
        for current, dirs, names in walker:
            # 符号链接 / 联接目录一律不进去：体积算不对事小，跟着跑到区外事大
            for dirname in list(dirs):
                if _is_reparse(os.path.join(current, dirname)):
                    dirs.remove(dirname)
            for name in names:
                child = os.path.join(current, name)
                child_info = _lstat(child)
                if _is_link_like(child_info) or child_info is None:
                    continue
                total += int(child_info.st_size)
                newest = max(newest, float(child_info.st_mtime))
    except OSError:
        pass
    return total, newest


def _candidates(
    root: str,
    *,
    warnings: list[str],
    log: Callable[[str, str], None] | None = None,
) -> list[tuple[str, str, bool, bool]]:
    """列出所有「形状上可能属于六类之一」的路径：``(path, relative, 像目录, 是链接)``。

    只扫**两层固定位置**（顶层 / ``.vwtest_repair`` 一层 / ``backups`` 一层），
    不做递归 glob——目录越深越容易误伤。
    """

    found: list[tuple[str, str, bool, bool]] = []
    try:
        top = list(os.scandir(root))
    except OSError as exc:
        warnings.append(f"旧快照：读不到数据目录（{exc}）")
        return found

    for entry in top:
        name = entry.name
        is_link = _is_reparse(entry.path)
        if name in (VTEST_REPAIR_DIR, BACKUPS_DIR):
            if is_link:
                message = f"旧快照：跳过符号链接/联接目录 {name}/（不跟进、不删除）"
                warnings.append(message)
                _emit(log, "warning", message)
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                inner = list(os.scandir(entry.path))
            except OSError as exc:
                warnings.append(f"旧快照：读不到 {name}/（{exc}）")
                continue
            for child in inner:
                child_link = _is_reparse(child.path)
                if name == VTEST_REPAIR_DIR:
                    # 只认直接子文件；子目录不碰
                    if child_link or child.is_file(follow_symlinks=False):
                        found.append((child.path, f"{name}/{child.name}", False, child_link))
                    continue
                # backups/：目录（snapshot-*）/ 普通文件（vacuum-*.db）都可能是快照。
                # 链接一律按「不知道指向什么」放进候选，交给分类 + 路径校验去拒；
                # 名字不是这两类的文件照旧在这里就落在分类里被判空，不会进计划。
                if child_link:
                    found.append((child.path, f"{name}/{child.name}", True, True))
                elif child.is_dir(follow_symlinks=False):
                    found.append((child.path, f"{name}/{child.name}", True, False))
                elif child.is_file(follow_symlinks=False):
                    found.append((child.path, f"{name}/{child.name}", False, False))
            continue
        if is_link or entry.is_file(follow_symlinks=False):
            found.append((entry.path, name, False, is_link))
    return found


def _build_entries(
    root: str,
    *,
    warnings: list[str],
    log: Callable[[str, str], None] | None = None,
) -> list[SnapshotEntry]:
    """把候选路径分类、量体积，产出一份「待决清单」；链接/禁区在这里就出局。"""

    entries: list[SnapshotEntry] = []
    for path, relative, looks_dir, is_link in _candidates(root, warnings=warnings, log=log):
        category, is_companion = _classify_relative(relative, is_dir=looks_dir)
        if is_companion:
            # 伴生文件跟着主文件走：主文件被删时才连带删，不单独占一份保留名额
            continue
        if not category:
            continue
        blocked = _protection_reason(relative)
        if blocked:
            # 双保险：正常分类不会走到这里，真走到了就说明规则被人改松了
            warnings.append(f"旧快照：{relative} 命中禁区（{blocked}），跳过")
            continue
        if not _within_root(path, root):
            warnings.append(f"旧快照：{relative} 解析后不在数据目录内，跳过")
            continue
        if is_link:
            message = f"旧快照：跳过符号链接/联接 {relative}（不跟进、不删除）"
            warnings.append(message)
            _emit(log, "warning", message)
            info = _lstat(path)
            entries.append(
                SnapshotEntry(
                    path=path,
                    relative=relative,
                    category=category,
                    size=0,
                    mtime=float(info.st_mtime) if info is not None else 0.0,
                    is_dir=looks_dir,
                    is_link=True,
                )
            )
            continue
        if looks_dir:
            size, mtime = _dir_facts(path)
        else:
            info = _lstat(path)
            if info is None:
                continue
            size, mtime = int(info.st_size), float(info.st_mtime)
        entries.append(
            SnapshotEntry(
                path=path,
                relative=relative,
                category=category,
                size=size,
                mtime=mtime,
                is_dir=looks_dir,
            )
        )
    return entries


def _keep_item(entry: SnapshotEntry, root: str, reason: str) -> CleanupItem:
    return CleanupItem(
        path=entry.path,
        category=entry.category,
        size=int(entry.size),
        mtime=float(entry.mtime),
        action="keep",
        reason=reason,
        relative=entry.relative,
        data_dir=root,
        is_dir=entry.is_dir,
    )


def _delete_item(
    entry: SnapshotEntry, root: str, reason: str, *, is_companion: bool = False
) -> CleanupItem:
    return CleanupItem(
        path=entry.path,
        category=entry.category,
        size=int(entry.size),
        mtime=float(entry.mtime),
        action="delete",
        reason=reason,
        relative=entry.relative,
        data_dir=root,
        is_companion=is_companion,
        is_dir=entry.is_dir,
    )


def _companions(entry: SnapshotEntry, root: str) -> list[CleanupItem]:
    """被删的那个库的 ``-wal`` / ``-shm``：跟着一起删（主库的伴生文件永远不在计划里）。"""

    if entry.is_dir:
        return []
    found: list[CleanupItem] = []
    for suffix in _COMPANION_SUFFIXES:
        target = entry.path + suffix
        info = _lstat(target)
        if info is None or _is_link_like(info):
            continue
        if not _within_root(target, root):
            continue
        found.append(
            CleanupItem(
                path=target,
                category=entry.category,
                size=int(info.st_size),
                mtime=float(info.st_mtime),
                action="delete",
                reason=f"随 {entry.relative} 一起删",
                relative=entry.relative + suffix,
                data_dir=root,
                is_companion=True,
            )
        )
    return found


def plan_snapshot_cleanup(
    data_dir: str,
    keep: int = KEEP_SNAPSHOTS,
    *,
    log: Callable[[str, str], None] | None = None,
    warnings: list[str] | None = None,
) -> list[CleanupItem]:
    """算一份清理计划（**一个文件都不动**）：每类留最近 ``keep`` 份，其余标 ``delete``。

    排序口径统一 **mtime 降序**（新→旧），目录类按目录内最新 mtime 算。
    ``keep <= 0`` = 自动清理关闭：全部标 ``keep``（照旧出清单，方便 dry-run 看）。
    ``PURGE_CATEGORIES`` 里的类别（``.vwtest_repair/*.db``，测试修复残留）**整类清空**、
    不占保留名额——这不是运行时数据，没有「留最近一份」的语义。
    ``warnings`` 传一个 list 进来可以拿到「符号链接跳过 / 读不动」这类信息。
    """

    items: list[CleanupItem] = []
    text = str(data_dir or "").strip()
    if not text:
        return items
    root = os.path.abspath(text)
    if not os.path.isdir(root):
        return items
    warn_list: list[str] = warnings if warnings is not None else []
    try:
        keep_n = int(keep)
    except (TypeError, ValueError):
        keep_n = KEEP_SNAPSHOTS
    if keep_n < 0:
        keep_n = 0

    grouped: dict[str, list[SnapshotEntry]] = {}
    for entry in _build_entries(root, warnings=warn_list, log=log):
        grouped.setdefault(entry.category, []).append(entry)

    for category in SNAPSHOT_CATEGORIES:
        group = grouped.get(category) or []
        if not group:
            continue
        group.sort(key=lambda item: (-item.mtime, item.relative))
        if category in PURGE_CATEGORIES:
            # 整类清空：不占保留名额，候选直接标 delete（链接仍然跳过）
            for entry in group:
                if keep_n <= 0:
                    items.append(_keep_item(entry, root, "自动清理已关闭（keep_snapshots<=0）"))
                elif entry.is_link:
                    items.append(_keep_item(entry, root, "符号链接：不跟进、不删除"))
                else:
                    items.append(_delete_item(entry, root, PURGE_REASON))
                    items.extend(_companions(entry, root))
            continue
        for index, entry in enumerate(group):
            if keep_n <= 0:
                items.append(_keep_item(entry, root, "自动清理已关闭（keep_snapshots<=0）"))
            elif entry.is_link:
                items.append(_keep_item(entry, root, "符号链接：不跟进、不删除"))
            elif index < keep_n:
                items.append(
                    _keep_item(entry, root, f"最近第 {index + 1} 份（保留最近 {keep_n} 份）")
                )
            else:
                items.append(_delete_item(entry, root, f"超出最近 {keep_n} 份的旧快照"))
                items.extend(_companions(entry, root))
    return items


def _deletion_blocker(item: CleanupItem) -> str:
    """删前重验：返回拒绝原因，空串 = 允许删（**任何一条不过就不删**）。"""

    root = str(item.data_dir or "")
    if not root:
        return "计划里没有数据目录（拒绝删除）"
    if not _within_root(item.path, root):
        target = os.path.abspath(item.path)
        return f"路径不在数据目录内（{target}）"
    info = _lstat(item.path)
    if info is None:
        return ""  # 已经不在了：当 no-op 处理（幂等）
    if _is_link_like(info):
        return "符号链接 / 目录联接（不跟进、不删除）"
    try:
        relative = os.path.relpath(item.path, root).replace(os.sep, "/")
    except ValueError:
        return "算不出相对路径"
    blocked = _protection_reason(relative)
    if blocked:
        return blocked
    category, is_companion = _classify_relative(
        relative, is_dir=stat.S_ISDIR(info.st_mode)
    )
    if category != item.category:
        return f"类别不匹配（现算出 {category or '无'}）"
    if bool(is_companion) != bool(item.is_companion):
        return "伴生标记不匹配"
    return ""


def apply_snapshot_cleanup(
    plan: Sequence[CleanupItem],
    *,
    dry_run: bool = False,
    log: Callable[[str, str], None] | None = None,
) -> CleanupResult:
    """照着计划删；**删前再验一次路径规则**，单项失败记 WARN 继续，绝不中断。

    ``dry_run=True`` 只统计"会删几项、能腾多少"，一个文件都不动。
    """

    result = CleanupResult(dry_run=bool(dry_run))
    for item in list(plan or []):
        if not isinstance(item, CleanupItem):
            continue
        if not item.will_delete:
            result.kept += 1
            continue
        result.planned += 1
        blocker = _deletion_blocker(item)
        if blocker:
            result.failed.append(
                CleanupFailure(path=item.path, category=item.category, error=blocker)
            )
            _emit(
                log,
                "warning",
                f"旧快照：拒绝删除 {item.relative or item.path}（{blocker}）",
            )
            continue
        size = int(item.size)
        if dry_run:
            result.freed_bytes += size
            if item.category in PURGE_CATEGORIES:
                result.purged += 1
            _emit(
                log,
                "debug",
                f"旧快照（预演）：会删除 {item.relative or item.path}（{human_bytes(size)}）",
            )
            continue
        try:
            if os.path.isdir(item.path) and not _is_reparse(item.path):
                shutil.rmtree(item.path)
            else:
                os.remove(item.path)
        except FileNotFoundError:
            _emit(log, "debug", f"旧快照：{item.relative or item.path} 已经不在了")
            continue
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
            result.failed.append(
                CleanupFailure(path=item.path, category=item.category, error=error)
            )
            _emit(
                log,
                "warning",
                f"旧快照：删除 {item.relative or item.path} 失败（下次再试）：{error}",
            )
            continue
        result.deleted += 1
        result.freed_bytes += size
        if item.category in PURGE_CATEGORIES:
            result.purged += 1
        _emit(
            log,
            "debug",
            f"旧快照：删除 {item.relative or item.path}（{human_bytes(size)}）",
        )
    return result


def _purge_note(purged: int) -> str:
    """「其中测试残留 N 项」那一小段：只在真的删了测试残留时才出现。"""

    if int(purged) <= 0:
        return ""
    return f"（含测试残留 {int(purged)} 项，整类清空）"


def snapshot_cleanup_plan_summary(
    plan: Sequence[CleanupItem], *, keep: int = KEEP_SNAPSHOTS
) -> str:
    """apply **之前**那行汇总：只按计划算（日志顺序是「先汇总、后动手」）。"""

    targets = [item for item in plan if getattr(item, "will_delete", False)]
    freed = sum(int(item.size) for item in targets)
    purged = sum(1 for item in targets if item.category in PURGE_CATEGORIES)
    return (
        f"清理旧快照：删除 {len(targets)} 项{_purge_note(purged)}，"
        f"释放 {human_bytes(freed)}（保留每类最近 {keep} 份）"
    )


def snapshot_cleanup_summary(result: CleanupResult, *, keep: int = KEEP_SNAPSHOTS) -> str:
    """一行汇总（启动/每日维护的那条日志、dry-run 的预览都用它）。"""

    count = result.planned if result.dry_run else result.deleted
    if result.dry_run:
        return (
            f"旧快照（预演，未删）：将删除 {count} 项{_purge_note(result.purged)}，"
            f"释放 {human_bytes(result.freed_bytes)}（保留每类最近 {keep} 份）"
        )
    text = (
        f"清理旧快照：删除 {count} 项{_purge_note(result.purged)}，"
        f"释放 {human_bytes(result.freed_bytes)}（保留每类最近 {keep} 份）"
    )
    if result.failed:
        text += f"，失败 {result.failed_count} 项"
    return text


def snapshot_cleanup_status_line(
    *,
    enabled: bool = True,
    keep: int = KEEP_SNAPSHOTS,
    records: Sequence[SnapshotCleanupRecord] = (),
) -> str:
    """``/vw status`` 里那行「旧快照：…」；没清理过就一行「无」，不刷屏。"""

    try:
        keep_n = int(keep)
    except (TypeError, ValueError):
        keep_n = KEEP_SNAPSHOTS
    if not enabled or keep_n <= 0:
        return "旧快照：自动清理已关闭（只算清单，不动文件）"
    # 这里故意不 `isinstance` 死卡：插件被以别的包名二次加载（测试就是这样加载
    # main.py 的）时，同一个类会存在两个类对象，卡类型会让真实记录被当成空气。
    history = [
        record
        for record in records
        if getattr(record, "deleted", None) is not None
        and getattr(record, "freed_bytes", None) is not None
    ]
    if not history:
        return "旧快照：无"
    current = history[0]
    if current.deleted <= 0 and current.failed <= 0:
        return "旧快照：无"
    previous = history[1] if len(history) > 1 else None
    previous_text = previous.line() if previous is not None else "无"
    text = (
        f"旧快照：本次清理 {current.deleted} 项 / 释放 "
        f"{human_bytes(current.freed_bytes)}（上次：{previous_text}）"
    )
    if current.failed:
        text += f"；失败 {current.failed} 项（详见日志）"
    return text
