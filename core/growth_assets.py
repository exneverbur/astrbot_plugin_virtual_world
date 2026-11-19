"""成长资产的地基（P0-8）：身份、来源、版本、墓碑、三方比较、备份与恢复演练。

为什么先做这一层：知识、skill、世界扩建**全都要落成"资产"**，而一旦落成资产，
就必然会遇到三个问题——升级官方版本时会不会盖掉用户的东西、Agent 提议的东西能不能
冒充官方、删掉的东西会不会又冒出来。这三件事必须在**第一个资产写下来之前**定好，
否则后面每加一种资产都要重新发一次版本来补救（方案 §11.1–11.5 / G01–G13）。

这里只定义"文件长什么样、怎么算身份、谁有权覆盖谁、备份怎么验"，**不决定**知识/skill
的具体字段（那是 P3/P4 的事）。所以本模块的 API 刻意很小：

* :class:`AssetStore` —— 一个目录（默认 ``<plugin_data>/growth``）+ 一个清单文件；
* 每条资产一个 JSON 文件，文件头 ``_asset`` 记身份；正文是资产自己的内容；
* **身份稳定**：``kind + identity`` 决定 id，改内容不改身份（版本 +1），改身份才是新资产；
* **来源**：``official`` / ``local`` / ``derived`` / ``agent`` —— 只有 official 和
  local 参与"覆盖"判定，agent 的东西永远是候选（与 P4 的 candidate 一致）；
* **墓碑**：删掉只打标记，不真删——升级时靠它知道"这个是被用户删的，别复活"；
* **三方比较**：官方基线 / 本地当前 / 派生（agent）各自一个 hash，给出四种结论之一；
* **备份**：把 ``state.db`` + ``growth/`` 一起打包成一次快照，并留一份可校验的清单；
  恢复演练 = 把快照读回来核对行数与资产 hash（真恢复由用户决定，这里只证明"可恢复"）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

#: 资产类别（后面 P3/P4/P5 各用一种；先定死，避免各自发明字符串）
KIND_KNOWLEDGE = "knowledge"
KIND_SKILL = "skill"
KIND_ACTION = "action"
KIND_NODE = "node"
KIND_PRESET = "preset"
KINDS = (KIND_KNOWLEDGE, KIND_SKILL, KIND_ACTION, KIND_NODE, KIND_PRESET)

#: 来源：官方基线 / 用户本地 / 官方派生的本地副本 / Agent 候选
SOURCE_OFFICIAL = "official"
SOURCE_LOCAL = "local"
SOURCE_DERIVED = "derived"
SOURCE_AGENT = "agent"
SOURCES = (SOURCE_OFFICIAL, SOURCE_LOCAL, SOURCE_DERIVED, SOURCE_AGENT)

SOURCE_LABELS = {
    SOURCE_OFFICIAL: "官方",
    SOURCE_LOCAL: "本地",
    SOURCE_DERIVED: "派生副本",
    SOURCE_AGENT: "Agent 候选",
}

#: 三方比较的四种结论
DECISION_KEEP_LOCAL = "keep_local"
DECISION_TAKE_OFFICIAL = "take_official"
DECISION_CONFLICT = "conflict"
DECISION_NO_CHANGE = "no_change"

DECISION_LABELS = {
    DECISION_KEEP_LOCAL: "保留本地（官方没变）",
    DECISION_TAKE_OFFICIAL: "采用官方（本地没改）",
    DECISION_CONFLICT: "冲突（两边都改过，要人决定）",
    DECISION_NO_CHANGE: "两边一致，不用动",
}

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1


def _now() -> float:
    return time.time()


def content_hash(payload: Any) -> str:
    """正文指纹：只对**内容**算，不含身份/时间/版本这些元数据。

    为什么要排除 ``_asset``：同一份内容在官方与本地两边，元数据必然不同
    （来源、时间、版本都不一样），把它们算进 hash 会让"两边其实一样"永远判成冲突。
    """

    body = _body_of(payload)
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _body_of(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        return {key: value for key, value in payload.items() if key != "_asset"}
    return payload


def _slug(text: Any) -> str:
    body = str(text or "").strip().lower()
    picked = [ch if (ch.isalnum() or ch in "-_") else "-" for ch in body]
    slug = "".join(picked).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:60] or "asset"


@dataclass
class AssetRecord:
    """一条资产的元数据（就是文件里的 ``_asset`` 头）。"""

    asset_id: str
    kind: str
    identity: str
    source: str = SOURCE_LOCAL
    version: int = 1
    hash: str = ""
    baseline_hash: str = ""
    """官方基线的正文指纹；空 = 这条不是从官方派生的。"""
    deleted: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    note: str = ""
    path: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "identity": self.identity,
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "version": int(self.version),
            "hash": self.hash,
            "baseline_hash": self.baseline_hash,
            "deleted": bool(self.deleted),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "note": self.note,
            "path": self.path,
            "meta": dict(self.meta),
        }


class AssetStore:
    """一个目录里的全部成长资产（官方基线 + 本地覆盖 + Agent 候选）。"""

    def __init__(self, root: os.PathLike[str] | str) -> None:
        self.root = Path(root)

    # ---------------- 路径 ----------------

    def kind_dir(self, kind: str) -> Path:
        return self.root / _slug(kind)

    def path_of(self, kind: str, identity: str) -> Path:
        return self.kind_dir(kind) / f"{_slug(identity)}.json"

    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    # ---------------- 读 ----------------

    def read(self, kind: str, identity: str) -> dict[str, Any] | None:
        path = self.path_of(kind, identity)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def records(self, kind: str = "", *, include_deleted: bool = False) -> list[AssetRecord]:
        out: list[AssetRecord] = []
        kinds = [kind] if kind else list(KINDS)
        for name in kinds:
            folder = self.kind_dir(name)
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                record = self._record_of(payload, path=path)
                if record is None:
                    continue
                if record.deleted and not include_deleted:
                    continue
                out.append(record)
        return out

    def _record_of(self, payload: Mapping[str, Any], *, path: Path | None = None) -> AssetRecord | None:
        head = payload.get("_asset") if isinstance(payload, Mapping) else None
        if not isinstance(head, Mapping):
            return None
        try:
            return AssetRecord(
                asset_id=str(head.get("asset_id") or ""),
                kind=str(head.get("kind") or ""),
                identity=str(head.get("identity") or ""),
                source=str(head.get("source") or SOURCE_LOCAL),
                version=int(head.get("version") or 1),
                hash=str(head.get("hash") or ""),
                baseline_hash=str(head.get("baseline_hash") or ""),
                deleted=bool(head.get("deleted")),
                created_at=float(head.get("created_at") or 0.0),
                updated_at=float(head.get("updated_at") or 0.0),
                note=str(head.get("note") or ""),
                path=str(path or ""),
                meta=dict(head.get("meta") or {}),
            )
        except Exception:
            return None

    # ---------------- 写 ----------------

    def save(
        self,
        kind: str,
        identity: str,
        body: Mapping[str, Any],
        *,
        source: str = SOURCE_LOCAL,
        baseline_hash: str = "",
        note: str = "",
        meta: Mapping[str, Any] | None = None,
    ) -> AssetRecord:
        """写入一条资产。**身份稳定**：同一 ``kind + identity`` 只改版本，不换 id。

        内容没变就原样返回（版本也不动）——重复保存不该让版本号乱跳。
        """

        if str(kind) not in KINDS:
            raise ValueError(f"认不出的资产类别：{kind}")
        if str(source) not in SOURCES:
            raise ValueError(f"认不出的来源：{source}")
        clean = {key: value for key, value in dict(body).items() if key != "_asset"}
        digest = content_hash(clean)
        existing = self.read(kind, identity)
        previous = self._record_of(existing) if existing else None
        if previous is not None and previous.hash == digest and not previous.deleted:
            return previous
        now = _now()
        version = (previous.version + 1) if previous is not None else 1
        asset_id = f"{kind}:{_slug(identity)}"
        record = AssetRecord(
            asset_id=asset_id,
            kind=str(kind),
            identity=str(identity),
            source=str(source),
            version=version,
            hash=digest,
            baseline_hash=str(baseline_hash or (previous.baseline_hash if previous else "")),
            deleted=False,
            created_at=(previous.created_at if previous else now),
            updated_at=now,
            note=str(note or ""),
            path=str(self.path_of(kind, identity)),
            meta=dict(meta or (previous.meta if previous else {})),
        )
        payload = {"_asset": record.as_dict(), **clean}
        payload["_asset"].pop("path", None)
        path = self.path_of(kind, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._rewrite_manifest()
        return record

    def tombstone(self, kind: str, identity: str, *, note: str = "") -> bool:
        """删除 = **打标记**（不真删文件）：升级时靠它知道"别复活这个"。"""

        payload = self.read(kind, identity)
        if not payload:
            return False
        head = dict(payload.get("_asset") or {})
        head["deleted"] = True
        head["updated_at"] = _now()
        head["version"] = int(head.get("version") or 1) + 1
        if note:
            head["note"] = str(note)
        payload["_asset"] = head
        self.path_of(kind, identity).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._rewrite_manifest()
        return True

    def is_deleted(self, kind: str, identity: str) -> bool:
        payload = self.read(kind, identity)
        record = self._record_of(payload) if payload else None
        return bool(record and record.deleted)

    # ---------------- 三方比较 ----------------

    @staticmethod
    def three_way(
        *, official_hash: str, local_hash: str, baseline_hash: str
    ) -> tuple[str, str]:
        """官方基线 / 本地当前 / 官方新版三方比较，返回 ``(结论, 一句人话)``。

        ``baseline_hash`` 是"上一次采用官方时的正文指纹"：
        * 官方新版 == 基线 → 官方没变 → 保留本地；
        * 本地 == 基线 → 本地没改 → 可以直接采用官方；
        * 两边都和基线不同 → **冲突**，交给人决定（不静默覆盖）；
        * 三者相同 → 不用动。
        """

        official = str(official_hash or "")
        local = str(local_hash or "")
        baseline = str(baseline_hash or "")
        if not official and not local:
            return DECISION_NO_CHANGE, DECISION_LABELS[DECISION_NO_CHANGE]
        if local == official:
            return DECISION_NO_CHANGE, DECISION_LABELS[DECISION_NO_CHANGE]
        if official == baseline:
            return DECISION_KEEP_LOCAL, DECISION_LABELS[DECISION_KEEP_LOCAL]
        if local == baseline:
            return DECISION_TAKE_OFFICIAL, DECISION_LABELS[DECISION_TAKE_OFFICIAL]
        return DECISION_CONFLICT, DECISION_LABELS[DECISION_CONFLICT]

    def conflict_report(self, kind: str, identity: str, official_payload: Mapping[str, Any]) -> dict[str, Any]:
        """给编辑器/调试看的冲突说明（不做任何写操作）。"""

        current = self.read(kind, identity)
        record = self._record_of(current) if current else None
        official_hash = content_hash(official_payload)
        decision, label = self.three_way(
            official_hash=official_hash,
            local_hash=(record.hash if record else ""),
            baseline_hash=(record.baseline_hash if record else ""),
        )
        return {
            "kind": kind,
            "identity": identity,
            "decision": decision,
            "label": label,
            "local_hash": record.hash if record else "",
            "official_hash": official_hash,
            "baseline_hash": record.baseline_hash if record else "",
            "deleted": bool(record.deleted) if record else False,
            # 用户删过的资产**不许被升级自动复活**（G04）
            "resurrect_blocked": bool(record.deleted) if record else False,
        }

    # ---------------- 所有权清单 ----------------

    def ownership(self) -> dict[str, Any]:
        """所有权清单：每一类资产各几条、谁的、能不能被升级覆盖。

        这份东西是给人和升级程序同时看的：升级只允许动 ``official`` 且本地没改过的；
        ``local`` / ``derived`` / ``agent`` 一律不许静默覆盖。
        """

        rows: list[dict[str, Any]] = []
        for record in self.records(include_deleted=True):
            rows.append(
                {
                    **record.as_dict(),
                    "overwritable": record.source == SOURCE_OFFICIAL and not record.deleted,
                    "owned_by": SOURCE_LABELS.get(record.source, record.source),
                }
            )
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["source"]] = counts.get(row["source"], 0) + 1
        return {
            "root": str(self.root),
            "total": len(rows),
            "by_source": counts,
            "rows": rows,
            "note": (
                "只有「官方」且本地没改过的资产允许被升级覆盖；本地/派生/Agent 候选一律不静默覆盖，"
                "删掉的东西带墓碑，升级不会复活。"
            ),
        }

    def _rewrite_manifest(self) -> None:
        """清单：身份 → 来源/版本/hash。**不带正文**，所以可以安全地看/比/备份。"""

        payload = {
            "version": MANIFEST_VERSION,
            "updated_at": _now(),
            "assets": [
                {
                    "asset_id": record.asset_id,
                    "kind": record.kind,
                    "identity": record.identity,
                    "source": record.source,
                    "version": record.version,
                    "hash": record.hash,
                    "baseline_hash": record.baseline_hash,
                    "deleted": record.deleted,
                }
                for record in self.records(include_deleted=True)
            ],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def manifest(self) -> dict[str, Any]:
        if not self.manifest_path().exists():
            self._rewrite_manifest()
        try:
            return json.loads(self.manifest_path().read_text(encoding="utf-8"))
        except Exception:
            return {"version": MANIFEST_VERSION, "assets": []}


# ---------------- 备份 / 恢复演练 ----------------


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class BackupReport:
    ok: bool
    dest: str
    files: tuple[str, ...] = ()
    db_rows: dict[str, int] = field(default_factory=dict)
    asset_count: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "dest": self.dest,
            "files": list(self.files),
            "db_rows": dict(self.db_rows),
            "asset_count": int(self.asset_count),
            "note": self.note,
        }


#: 备份里要核对行数的表（只挑"丢了会真的疼"的那几张）
DRILL_TABLES = ("agent_tasks", "event_log", "memories", "profiles", "config_index_queue")


def backup(
    *,
    db_path: os.PathLike[str] | str,
    growth_root: os.PathLike[str] | str,
    dest: os.PathLike[str] | str,
) -> BackupReport:
    """把数据库与成长资产**一起**拷到一个快照目录，并写出可校验的清单。

    为什么要一起：只备 ``state.db`` 会丢成长资产（它们在文件系统上）；只备 ``growth/``
    又会丢"这些资产属于哪条经历"。所以两者必须同一个快照、同一份清单。

    做法刻意保守：**只读源文件**，目标目录必须不存在（避免把旧快照覆盖成半新半旧）。
    """

    source_db = Path(db_path)
    source_growth = Path(growth_root)
    target = Path(dest)
    if target.exists():
        return BackupReport(False, str(target), note="目标目录已存在，不覆盖（换个名字重试）")
    if not source_db.exists():
        return BackupReport(False, str(target), note=f"数据库不存在：{source_db}")
    target.mkdir(parents=True, exist_ok=False)
    files: list[str] = []
    try:
        copied = target / "state.db"
        # 备份时必须用 sqlite 的备份 API：直接拷 .db 可能漏掉 WAL 里还没落盘的页
        with sqlite3.connect(str(source_db)) as src, sqlite3.connect(str(copied)) as dst:
            src.backup(dst)
        files.append("state.db")
        if source_growth.is_dir():
            shutil.copytree(source_growth, target / "growth")
            files.append("growth")
        rows: dict[str, int] = {}
        with sqlite3.connect(str(copied)) as conn:
            for table in DRILL_TABLES:
                try:
                    rows[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    rows[table] = -1  # 这张表本来就没有：如实记成 -1，不是 0
        manifest = {
            "version": MANIFEST_VERSION,
            "created_at": _now(),
            "source_db": str(source_db),
            "source_growth": str(source_growth),
            "files": files,
            "db_rows": rows,
            "hashes": {
                name: _hash_file(target / name)
                for name in ["state.db"]
                if (target / name).exists()
            },
            "asset_count": len(AssetStore(source_growth).records(include_deleted=True))
            if source_growth.is_dir()
            else 0,
        }
        (target / "backup.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return BackupReport(
            True,
            str(target),
            files=tuple(files),
            db_rows=rows,
            asset_count=int(manifest["asset_count"]),
            note="快照完成：数据库用 sqlite backup API 拷的，成长资产整目录拷",
        )
    except Exception as exc:
        return BackupReport(False, str(target), files=tuple(files), note=f"快照失败：{exc}")


def verify_backup(dest: os.PathLike[str] | str) -> dict[str, Any]:
    """恢复**演练**：把快照读回来核对（真恢复由用户决定，这里只证明"能恢复"）。

    核对三件事：清单在不在、数据库能不能打开且行数与清单一致、资产文件数与清单一致。
    """

    target = Path(dest)
    result: dict[str, Any] = {
        "ok": False,
        "dest": str(target),
        "db_rows": {},
        "asset_count": 0,
        "problems": [],
    }
    manifest_path = target / "backup.json"
    if not manifest_path.exists():
        result["problems"].append("没有 backup.json")
        return result
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        result["problems"].append(f"清单读不出来：{exc}")
        return result
    db_file = target / "state.db"
    if not db_file.exists():
        result["problems"].append("快照里没有 state.db")
    else:
        expected = str((manifest.get("hashes") or {}).get("state.db") or "")
        actual = _hash_file(db_file)
        if expected and expected != actual:
            result["problems"].append("state.db 的指纹和清单对不上（快照被改过？）")
        try:
            with sqlite3.connect(str(db_file)) as conn:
                for table, want in (manifest.get("db_rows") or {}).items():
                    try:
                        got = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    except sqlite3.Error:
                        got = -1
                    result["db_rows"][table] = got
                    if int(want) != got:
                        result["problems"].append(f"{table} 行数不一致：清单 {want} / 实读 {got}")
        except Exception as exc:
            result["problems"].append(f"快照库打不开：{exc}")
    growth = target / "growth"
    if growth.is_dir():
        result["asset_count"] = len(AssetStore(growth).records(include_deleted=True))
        want_assets = int(manifest.get("asset_count") or 0)
        if want_assets != result["asset_count"]:
            result["problems"].append(
                f"资产条数不一致：清单 {want_assets} / 实读 {result['asset_count']}"
            )
    result["ok"] = not result["problems"]
    return result
