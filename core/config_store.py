"""配置读写：data/world.json、data/schedules.json、data/sessions.json、data/state.db。

规则（对应设计文档第 13 章）：
- 首次启动自动生成默认配置，零配置可用；
- 升级时只补字段、不覆盖用户已有的值；
- 只有用户主动「恢复默认」才覆盖。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .defaults import default_schedules, default_world
from .models import (
    SchedulesConfig,
    SessionsConfig,
    WorldConfig,
    parse_schedules,
    parse_sessions,
    parse_world,
)

WORLD_FILE = "world.json"
SCHEDULES_FILE = "schedules.json"
SESSIONS_FILE = "sessions.json"
DB_FILE = "state.db"

HISTORY_KEEP = 50
"""改动历史最多留几条（按条数滚动，不按天）。"""

HISTORY_PREFIX = "hist-"
"""历史快照的文件名前缀。早期版本留下的 ``before-apply-*.json`` 也会列出来，
但标成「旧版备份」且不参与滚动删除。"""

BLOCK_LABELS = {
    "map": "地图",
    "actions": "动作",
    "settings": "世界设置",
    "persona": "人设",
    "schedules": "日程",
    "sessions": "会话",
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把 base 作为默认值、override 作为用户值合并：用户值优先，缺失字段用默认值补全。"""

    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


TOOL_KEYS = ("tool_name", "tool_names", "tool_fallbacks", "tool_mode", "tool_flow")
"""只有"工具型"动作才该有的字段。"""


def _drop_tool_fields_for_commands(data: dict[str, Any]) -> int:
    """把"类型是指令、却还挂着工具绑定"的动作清干净，返回清掉几个。

    界面上把类型切回「指令」时不会自动抹掉 `tool_name` 这些字段，于是保存之后
    它仍然按工具动作走 —— 用户看到的就是"改不回指令"。
    """

    actions = data.get("actions")
    if not isinstance(actions, list):
        return 0
    dropped = 0
    for item in actions:
        if not isinstance(item, dict):
            continue
        if str(item.get("llm_level") or "") != "command":
            continue
        if not any(item.get(key) for key in TOOL_KEYS):
            continue
        for key in TOOL_KEYS:
            item.pop(key, None)
        dropped += 1
    return dropped


class ConfigStore:
    """配置与数据的统一入口。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.warnings: list[str] = []
        self._world: WorldConfig | None = None
        self._schedules: SchedulesConfig | None = None
        self._sessions: SessionsConfig | None = None
        self.created_files: list[str] = []

    # ---------------- 路径 ----------------

    @property
    def world_path(self) -> Path:
        return self.data_dir / WORLD_FILE

    @property
    def schedules_path(self) -> Path:
        return self.data_dir / SCHEDULES_FILE

    @property
    def sessions_path(self) -> Path:
        return self.data_dir / SESSIONS_FILE

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILE

    # ---------------- 文件读写 ----------------

    def _read_json(self, path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            self._write_json(path, fallback)
            self.created_files.append(path.name)
            return json.loads(json.dumps(fallback))
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.warnings.append(f"读取 {path.name} 失败：{exc}，已使用默认配置")
            return json.loads(json.dumps(fallback))
        if not raw.strip():
            self.warnings.append(f"{path.name} 是空文件，已使用默认配置")
            return json.loads(json.dumps(fallback))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            backup = path.with_suffix(path.suffix + f".broken.{int(time.time())}")
            try:
                path.replace(backup)
                self.warnings.append(
                    f"{path.name} 不是合法 JSON（{exc}），已备份为 {backup.name} 并重建默认配置"
                )
                self._write_json(path, fallback)
            except OSError:
                self.warnings.append(f"{path.name} 不是合法 JSON，且无法备份，已使用默认配置")
            return json.loads(json.dumps(fallback))
        if not isinstance(data, dict):
            self.warnings.append(f"{path.name} 顶层必须是对象，已使用默认配置")
            return json.loads(json.dumps(fallback))
        return data

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)

    # ---------------- 世界配置 ----------------

    def ensure_files(self) -> list[str]:
        """确保所需文件存在，返回本次新建的文件名列表。"""

        self._read_json(self.world_path, default_world())
        self._read_json(self.schedules_path, default_schedules())
        self._read_json(self.sessions_path, {"sessions": []})
        return list(self.created_files)

    def load_world(self) -> WorldConfig:
        raw = self._read_json(self.world_path, default_world())
        # 升级补字段：默认值 + 用户值（用户值优先）
        merged = _deep_merge(default_world(), raw)
        merged["nodes"] = raw.get("nodes", merged.get("nodes", []))
        merged["edges"] = raw.get("edges", merged.get("edges", []))
        merged["actions"] = raw.get("actions", merged.get("actions", []))
        world, warnings = parse_world(merged)
        self.warnings.extend(warnings)
        self._world = world
        return world

    def load_schedules(self) -> SchedulesConfig:
        raw = self._read_json(self.schedules_path, default_schedules())
        schedules, warnings = parse_schedules(raw)
        self.warnings.extend(warnings)
        self._schedules = schedules
        return schedules

    def load_sessions(self) -> SessionsConfig:
        raw = self._read_json(self.sessions_path, {"sessions": []})
        sessions, warnings = parse_sessions(raw)
        self.warnings.extend(warnings)
        self._sessions = sessions
        return sessions

    def reload(self) -> tuple[WorldConfig, SchedulesConfig, SessionsConfig, list[str]]:
        """热加载全部配置，返回 (世界, 日程, 会话, 警告)。"""

        self.warnings = []
        world = self.load_world()
        schedules = self.load_schedules()
        sessions = self.load_sessions()
        return world, schedules, sessions, list(self.warnings)

    # ---------------- 保存 ----------------

    def save_world(
        self,
        data: dict[str, Any],
        *,
        snapshot: bool = True,
        reason: str = "手动保存",
    ) -> list[str]:
        """写世界配置。``snapshot=True``（默认）时先给改动前的那一份留个历史。"""

        world, warnings = parse_world(data)
        if snapshot:
            self.snapshot_before(reason)
        clean = data if isinstance(data, dict) else {}
        dropped = _drop_tool_fields_for_commands(clean)
        if dropped:
            warnings = [
                *(warnings or []),
                f"{dropped} 个动作的类型是「指令」，已清掉它们身上残留的工具绑定"
                "（不清的话保存之后还会被当成工具动作）。",
            ]
        self._write_json(self.world_path, clean)
        self._world = world
        return warnings

    def save_schedules(
        self,
        data: dict[str, Any],
        *,
        snapshot: bool = True,
        reason: str = "手动保存",
    ) -> list[str]:
        _, warnings = parse_schedules(data)
        if snapshot:
            self.snapshot_before(reason)
        self._write_json(self.schedules_path, data if isinstance(data, dict) else {})
        return warnings

    def save_sessions(
        self,
        data: dict[str, Any],
        *,
        snapshot: bool = True,
        reason: str = "手动保存",
    ) -> list[str]:
        """写会话白名单：**先过一遍校验再落盘**。

        以前是"原样写、读的时候才校验"，于是会出现这种半坏状态：
        会话组里还写着某个已经从白名单里删掉的会话 —— 读盘时那条会被跳过，
        组里没人了就变成一个空组，界面上看着就是"私聊和会话组一起没了"。
        现在按校验后的结果落盘，并把提示交回给调用方。
        """

        parsed, warnings = parse_sessions(data if isinstance(data, dict) else {})
        if snapshot:
            self.snapshot_before(reason)
        self._write_json(self.sessions_path, parsed.model_dump(mode="json"))
        return warnings

    def raw_world(self) -> dict[str, Any]:
        return self._read_json(self.world_path, default_world())

    def raw_schedules(self) -> dict[str, Any]:
        return self._read_json(self.schedules_path, default_schedules())

    def raw_sessions(self) -> dict[str, Any]:
        return self._read_json(self.sessions_path, {"sessions": []})

    # ---------------- 预设（成套的世界配置） ----------------

    @property
    def presets_dir(self) -> Path:
        return self.data_dir / "presets"

    @property
    def backups_dir(self) -> Path:
        return self.presets_dir / "backups"

    @property
    def active_preset_path(self) -> Path:
        return self.presets_dir / "active.json"

    @staticmethod
    def _clean_preset_id(preset_id: str) -> str:
        text = str(preset_id or "").strip()
        keep = [ch for ch in text if ch.isalnum() or ch in "._-"]
        return "".join(keep)[:64]

    def preset_path(self, preset_id: str) -> Path:
        return self.presets_dir / f"{self._clean_preset_id(preset_id)}.json"

    def list_presets(self) -> list[dict[str, Any]]:
        """列出所有预设（按更新时间倒序）。"""

        if not self.presets_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.presets_dir.glob("*.json")):
            if path.name == "active.json":
                continue
            data = self._read_json(path, {})
            if not isinstance(data, dict):
                continue
            world = data.get("world") if isinstance(data.get("world"), dict) else {}
            items.append(
                {
                    "id": path.stem,
                    "name": str(data.get("name") or path.stem),
                    "note": str(data.get("note") or ""),
                    "updated_at": str(data.get("updated_at") or ""),
                    "actions": len(world.get("actions") or []),
                    "nodes": len(world.get("nodes") or []),
                    "schedules": len(
                        (data.get("schedules") or {}).get("schedules") or []
                    ),
                    "sessions": len(
                        (data.get("sessions") or {}).get("sessions") or []
                    ),
                }
            )
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return items

    def active_preset(self) -> str:
        data = self._read_json(self.active_preset_path, {})
        return str((data or {}).get("id") or "") if isinstance(data, dict) else ""

    def set_active_preset(self, preset_id: str) -> None:
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self.active_preset_path, {"id": preset_id})

    def save_preset(
        self,
        preset_id: str,
        *,
        name: str = "",
        note: str = "",
    ) -> tuple[Path, list[str]]:
        """把当前三份配置打包成一个预设文件（顺手过一遍校验，返回提示）。"""

        clean = self._clean_preset_id(preset_id)
        if not clean:
            raise ValueError("预设 id 不能为空")
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        sessions_raw, session_warnings = parse_sessions(self.raw_sessions())
        warnings.extend(session_warnings)
        _, schedule_warnings = parse_schedules(self.raw_schedules())
        warnings.extend(schedule_warnings)
        payload = {
            "schema_version": 1,
            "id": clean,
            "name": name or clean,
            "note": note,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "world": self.raw_world(),
            "schedules": self.raw_schedules(),
            "sessions": sessions_raw.model_dump(mode="json"),
        }
        path = self.preset_path(clean)
        self._write_json(path, payload)
        self.set_active_preset(clean)
        return path, warnings

    def read_preset(self, preset_id: str) -> dict[str, Any]:
        data = self._read_json(self.preset_path(preset_id), {})
        return data if isinstance(data, dict) else {}

    def new_default_preset(
        self, preset_id: str, *, name: str = "", note: str = ""
    ) -> tuple[str, list[str]]:
        """用**内置默认世界**新建一份预设（不是把当前配置存下来）。

        内置世界会随版本升级变（加了新房间、新动作），但已经存过盘的老配置不会自动变——
        这份预设就是"升级时想要新默认长什么样"的入口：不覆盖当前配置，
        存好以后在预设列表里点「应用」才会切过去。
        """

        clean = self._clean_preset_id(preset_id)
        if not clean:
            raise ValueError("预设 id 不能为空")
        taken = {item.get("id") for item in self.list_presets()}
        if clean in taken:
            raise ValueError(f"已经有叫「{clean}」的预设了，换个 id 或先删掉它")
        payload = {
            "schema_version": 1,
            "id": clean,
            "name": name or "默认世界",
            "note": note or "内置的默认世界（房间 / 动作 / 日程都是出厂设置）",
            "world": default_world(),
            "schedules": default_schedules(),
            "sessions": {"sessions": [], "groups": []},
        }
        warnings = self.write_preset(clean, payload)
        return clean, warnings

    def write_preset(self, preset_id: str, payload: dict[str, Any]) -> list[str]:
        """整段写一个预设（导入 / 直接编辑 JSON 走这里）。返回校验提示。"""

        clean = self._clean_preset_id(preset_id)
        if not clean:
            raise ValueError("预设 id 不能为空")
        if not isinstance(payload, dict):
            raise ValueError("预设必须是一个 JSON 对象")
        world = payload.get("world")
        if not isinstance(world, dict) or not world.get("nodes"):
            raise ValueError("预设里必须有 world.nodes")
        warnings: list[str] = []
        parsed, world_warnings = parse_world(world)
        warnings.extend(world_warnings)
        schedules_raw = payload.get("schedules") or {}
        _, schedule_warnings = parse_schedules(schedules_raw)
        warnings.extend(schedule_warnings)
        sessions_raw = payload.get("sessions") or {}
        _, session_warnings = parse_sessions(sessions_raw)
        warnings.extend(session_warnings)
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(
            self.preset_path(clean),
            {
                "schema_version": 1,
                "id": clean,
                "name": str(payload.get("name") or clean),
                "note": str(payload.get("note") or ""),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "world": parsed.model_dump(mode="json"),
                "schedules": schedules_raw,
                "sessions": sessions_raw,
            },
        )
        return warnings

    def delete_preset(self, preset_id: str) -> bool:
        path = self.preset_path(preset_id)
        if not path.is_file():
            return False
        path.unlink()
        if self.active_preset() == self._clean_preset_id(preset_id):
            self.set_active_preset("")
        return True

    def backup_current(self, tag: str = "before-apply") -> Path:
        """应用预设前先把手头的配置备份一份，随时能翻回来。"""

        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.backups_dir / f"{tag}-{stamp}.json"
        self._write_json(
            path,
            {
                "schema_version": 1,
                "id": path.stem,
                "name": f"自动备份 {stamp}",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "world": self.raw_world(),
                "schedules": self.raw_schedules(),
                "sessions": self.raw_sessions(),
            },
        )
        return path

    # ---------------- 改动历史（每次写配置前留一份，可翻回去） ----------------

    @property
    def history_dir(self) -> Path:
        """历史快照与旧版备份同一个目录（``presets/backups``）。"""

        return self.backups_dir

    def _snapshot_body(self) -> dict[str, Any]:
        """当前盘上那三份配置（快照的内容就是它们）。"""

        return {
            "world": self.raw_world(),
            "schedules": self.raw_schedules(),
            "sessions": self.raw_sessions(),
        }

    @staticmethod
    def _block_of_world_key(key: str) -> str:
        """世界配置里某个键属于哪一块（和预设的分块保持一致）。"""

        if key in ("zones", "zone_edges", "nodes", "edges"):
            return "map"
        if key == "actions":
            return "actions"
        if key == "persona":
            return "persona"
        return "settings"

    @classmethod
    def _block_payload(cls, body: dict[str, Any], block: str) -> Any:
        if block == "schedules":
            return body.get("schedules") or {}
        if block == "sessions":
            return body.get("sessions") or {}
        world = body.get("world") or {}
        return {
            key: value
            for key, value in world.items()
            if cls._block_of_world_key(key) == block
        }

    @classmethod
    def _changed_blocks(
        cls, current: dict[str, Any], previous: dict[str, Any] | None
    ) -> list[str]:
        """两份快照之间哪几块变了（给历史列表写一行改动摘要）。"""

        if not previous:
            # 第一版没有可比的对象：不写"全都变了"，那是误导
            return []

        def digest(value: Any) -> str:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            return hashlib.sha1(text.encode("utf-8")).hexdigest()

        changed = []
        for block in cls.PRESET_BLOCKS:
            if digest(cls._block_payload(current, block)) != digest(
                cls._block_payload(previous, block)
            ):
                changed.append(block)
        return changed

    def _history_entries(self) -> list[dict[str, Any]]:
        """目录里所有历史快照，**新的在前**。

        只认 ``hist-`` 前缀；早期版本留下的 ``before-apply-*.json`` 也一起列出来
        （标成旧版备份），但不参与滚动删除——那是用户自己的老东西。
        """

        if not self.history_dir.exists():
            return []
        entries: list[dict[str, Any]] = []
        for path in self.history_dir.glob("*.json"):
            payload = self._read_json(path, {})
            if not isinstance(payload, dict):
                continue
            entries.append(
                {
                    "id": path.stem,
                    "name": str(payload.get("name") or payload.get("reason") or path.stem),
                    "reason": str(payload.get("reason") or ""),
                    "summary": str(payload.get("summary") or ""),
                    "created_at": str(payload.get("created_at") or ""),
                    "stamp": float(payload.get("stamp") or 0.0),
                    "legacy": not path.stem.startswith(HISTORY_PREFIX),
                    "world": payload.get("world") if isinstance(payload.get("world"), dict) else {},
                    "schedules": payload.get("schedules") or {},
                    "sessions": payload.get("sessions") or {},
                }
            )
        entries.sort(key=lambda item: (item["stamp"], item["id"]), reverse=True)
        return entries

    def _last_snapshot_body(self) -> dict[str, Any] | None:
        """最近一条**可比较**的快照内容（滚动的那批，不含旧版备份之外的东西）。"""

        entries = [item for item in self._history_entries() if not item["legacy"]]
        if not entries:
            return None
        newest = entries[0]
        return {
            "world": newest["world"],
            "schedules": newest["schedules"],
            "sessions": newest["sessions"],
        }

    def _trim_history(self, keep: int = HISTORY_KEEP) -> int:
        """只留最近 ``keep`` 条（按条数滚动），返回删掉几条。"""

        entries = [item for item in self._history_entries() if not item["legacy"]]
        removed = 0
        for item in entries[keep:]:
            path = self.history_dir / f"{item['id']}.json"
            if path.exists():
                path.unlink()
                removed += 1
        return removed

    def snapshot_before(self, reason: str, *, summary: str = "") -> Path | None:
        """改动前留一份快照。内容跟最近一条完全一样就不留（省着 50 个位置）。

        ``reason`` 是人看的来源标签（手动保存 / 应用预设 / 大模型改写 / 恢复历史…）。
        """

        body = self._snapshot_body()
        previous = self._last_snapshot_body()

        def digest(value: dict[str, Any]) -> str:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            return hashlib.sha1(text.encode("utf-8")).hexdigest()

        fingerprint = digest(body)
        if previous is not None and digest(previous) == fingerprint:
            return None

        changed = self._changed_blocks(body, previous)
        now = time.time()
        stamp_text = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        self.history_dir.mkdir(parents=True, exist_ok=True)
        path = self.history_dir / f"{HISTORY_PREFIX}{stamp_text}-{int(now * 1000) % 100000:05d}-{fingerprint[:8]}.json"
        self._write_json(
            path,
            {
                "schema_version": 1,
                "id": path.stem,
                "name": f"{reason} {stamp_text}",
                "reason": reason,
                "summary": summary or self._summary_text(changed),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                "stamp": now,
                "changed": changed,
                "world": body["world"],
                "schedules": body["schedules"],
                "sessions": body["sessions"],
            },
        )
        self._trim_history()
        return path

    @staticmethod
    def _summary_text(changed: list[str]) -> str:
        if not changed:
            # 真正的"内容没变"在 snapshot_before 里已经被去重挡掉了，
            # 走到这里只可能是第一版（没有可比的对象）
            return "第一版留档"
        labels = [BLOCK_LABELS.get(item, item) for item in changed]
        return "变动：" + "、".join(labels)

    def list_history(self, limit: int = HISTORY_KEEP) -> list[dict[str, Any]]:
        """历史列表（不含快照正文，正文用 read_history 单独取）。"""

        result = []
        for item in self._history_entries()[: max(1, int(limit or HISTORY_KEEP))]:
            result.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "reason": item["reason"],
                    "summary": item["summary"],
                    "created_at": item["created_at"],
                    "legacy": bool(item["legacy"]),
                }
            )
        return result

    def read_history(self, snapshot_id: str) -> dict[str, Any]:
        """取一条快照的完整内容 + 与当前配置的逐块差异。"""

        path = self.history_dir / f"{self._clean_preset_id(snapshot_id)}.json"
        payload = self._read_json(path, {})
        if not isinstance(payload, dict) or "world" not in payload:
            raise ValueError("找不到这条历史")
        body = {
            "world": payload.get("world") or {},
            "schedules": payload.get("schedules") or {},
            "sessions": payload.get("sessions") or {},
        }
        current = self._snapshot_body()
        return {
            "id": path.stem,
            "name": str(payload.get("name") or path.stem),
            "reason": str(payload.get("reason") or ""),
            "summary": str(payload.get("summary") or ""),
            "created_at": str(payload.get("created_at") or ""),
            "legacy": not path.stem.startswith(HISTORY_PREFIX),
            "snapshot": body,
            "current": current,
            "changed": self._changed_blocks(current, body),
        }

    def restore_history(
        self, snapshot_id: str, *, blocks: list[str] | None = None
    ) -> dict[str, Any]:
        """把某一版写回当前配置（默认整份；也可以只恢复某几块）。

        恢复之前会先把**当前**这版也存一条历史——所以"恢复"本身也能再恢复回去。
        """

        detail = self.read_history(snapshot_id)
        snapshot = detail["snapshot"]
        picked = [
            item
            for item in (blocks if blocks is not None else list(self.PRESET_BLOCKS))
            if item in self.PRESET_BLOCKS
        ]
        if not picked:
            raise ValueError("没有选中要恢复的部分")
        self.snapshot_before("恢复历史", summary=f"恢复前留档（来自 {detail['created_at']}）")
        warnings: list[str] = []
        world_keys = {"map", "actions", "settings", "persona"}
        if any(item in picked for item in world_keys):
            merged = self._merge_world_blocks(
                self.raw_world(), snapshot.get("world") or {}, picked
            )
            warnings.extend(self.save_world(merged, snapshot=False))
        if "schedules" in picked:
            warnings.extend(
                self.save_schedules(snapshot.get("schedules") or {}, snapshot=False)
            )
        if "sessions" in picked:
            warnings.extend(
                self.save_sessions(snapshot.get("sessions") or {}, snapshot=False)
            )
        return {"warnings": warnings, "blocks": picked, "id": detail["id"]}

    def delete_history(self, snapshot_id: str) -> bool:
        path = self.history_dir / f"{self._clean_preset_id(snapshot_id)}.json"
        if not path.exists() or not path.stem.startswith(HISTORY_PREFIX):
            return False
        path.unlink()
        return True

    def clear_history(self) -> int:
        """清空历史（只清 ``hist-`` 那批；旧版备份留着）。"""

        entries = [item for item in self._history_entries() if not item["legacy"]]
        removed = 0
        for item in entries:
            path = self.history_dir / f"{item['id']}.json"
            if path.exists():
                path.unlink()
                removed += 1
        return removed

    # 预设里可以被单独挑走的部分
    PRESET_BLOCKS = ("map", "actions", "settings", "persona", "schedules", "sessions")
    """``map``=区域/地点/连线，``actions``=动作，``settings``=世界设置（作息、情绪、画像…），
    ``persona``=她是谁，``schedules``=日程，``sessions``=会话白名单与会话组。"""

    def apply_preset(
        self, preset_id: str, *, blocks: list[str] | None = None
    ) -> dict[str, Any]:
        """把预设里挑中的部分写进当前配置（不含状态：状态由调用方决定要不要清）。

        ``blocks`` 不传 = 全部；**会话白名单与会话组建议默认不选**——
        切预设本来是想换世界，把会话一起换掉等于顺手清空了她的聊天场所。
        """

        data = self.read_preset(preset_id)
        if not data:
            raise ValueError("找不到这个预设")
        picked = [
            item
            for item in (blocks if blocks is not None else list(self.PRESET_BLOCKS))
            if item in self.PRESET_BLOCKS
        ]
        if not picked:
            raise ValueError("没有选中任何要应用的部分")
        # 改动前留一份历史（和"手动保存"走同一套；旧版 before-apply 不再新写）
        self.snapshot_before("应用预设", summary=f"应用预设「{preset_id}」前留档")
        warnings: list[str] = []
        world_keys = {"map", "actions", "settings", "persona"}
        if any(item in picked for item in world_keys):
            merged = self._merge_world_blocks(
                self.raw_world(), data.get("world") or {}, picked
            )
            warnings.extend(self.save_world(merged, snapshot=False))
        if "schedules" in picked:
            warnings.extend(
                self.save_schedules(data.get("schedules") or {}, snapshot=False)
            )
        if "sessions" in picked:
            warnings.extend(
                self.save_sessions(data.get("sessions") or {}, snapshot=False)
            )
        self.set_active_preset(preset_id)
        return {"warnings": warnings, "blocks": picked}

    @staticmethod
    def _merge_world_blocks(
        current: dict[str, Any], preset: dict[str, Any], picked: list[str]
    ) -> dict[str, Any]:
        """按块把预设的世界并到当前世界上（没挑的块保持原样）。"""

        map_keys = ("zones", "zone_edges", "nodes", "edges")
        merged = {**current}
        if "map" in picked:
            for key in map_keys:
                if key in preset:
                    merged[key] = preset[key]
        if "actions" in picked and "actions" in preset:
            merged["actions"] = preset["actions"]
        if "persona" in picked and "persona" in preset:
            merged["persona"] = preset["persona"]
        if "settings" in picked:
            skip = {*map_keys, "actions", "persona"}
            for key, value in preset.items():
                if key not in skip:
                    merged[key] = value
        return merged

    # ---------------- 会话白名单 ----------------

    def add_session(
        self,
        session_id: str,
        *,
        session_type: str = "group",
        platform: str = "",
        note: str = "",
        cold_start_mode: str = "awakening",
        cold_start_node: str = "",
    ) -> bool:
        raw = self.raw_sessions()
        sessions = raw.setdefault("sessions", [])
        for item in sessions:
            if item.get("session_id") == session_id:
                item["enabled"] = True
                self.save_sessions(raw)
                return False
        sessions.append(
            {
                "session_id": session_id,
                "type": session_type,
                "platform": platform or session_id.split(":", 1)[0],
                "enabled": True,
                "cold_start_mode": cold_start_mode,
                "cold_start_node": cold_start_node
                or (self._world.default_node_id() if self._world else "bedroom"),
                "added_at": int(time.time()),
                "note": note,
            }
        )
        self.save_sessions(raw)
        return True

    def remove_session(self, session_id: str) -> bool:
        raw = self.raw_sessions()
        sessions = raw.get("sessions", [])
        kept = [item for item in sessions if item.get("session_id") != session_id]
        if len(kept) == len(sessions):
            return False
        raw["sessions"] = kept
        # 会话从白名单里删掉时，别再让它留在会话组里：组里留着"已经不存在的会话"
        # 会成为半坏状态（读盘时成员被跳过，组里没人就看着像整个组没了）
        groups: list[dict[str, Any]] = []
        for group in raw.get("groups") or []:
            if not isinstance(group, dict):
                continue
            members = [
                str(item)
                for item in (group.get("sessions") or [])
                if str(item) and str(item) != session_id
            ]
            if not members:
                continue
            main = str(group.get("main_session") or "")
            groups.append(
                {
                    **group,
                    "sessions": members,
                    "main_session": main if main in members else members[0],
                }
            )
        raw["groups"] = groups
        self.save_sessions(raw)
        return True

    def set_session_enabled(self, session_id: str, enabled: bool) -> bool:
        raw = self.raw_sessions()
        for item in raw.get("sessions", []):
            if item.get("session_id") == session_id:
                item["enabled"] = bool(enabled)
                self.save_sessions(raw)
                return True
        return False

    # ---------------- 恢复默认 ----------------

    def restore_default(self, scope: str = "all") -> list[str]:
        """恢复默认配置。state.db（记忆）永远不动。"""

        # 恢复默认是"一键毁所有"里最容易手滑的一个：先留历史
        self.snapshot_before("恢复默认配置", summary=f"恢复默认（{scope}）前留档")
        restored: list[str] = []
        if scope in ("all", "world"):
            self._write_json(self.world_path, default_world())
            restored.append(WORLD_FILE)
        if scope in ("all", "schedules"):
            self._write_json(self.schedules_path, default_schedules())
            restored.append(SCHEDULES_FILE)
        if scope in ("all", "sessions"):
            self._write_json(self.sessions_path, {"sessions": []})
            restored.append(SESSIONS_FILE)
        return restored

    # ---------------- 备份 ----------------

    def backup(self, tag: str = "") -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.data_dir / "backup"
        target.mkdir(parents=True, exist_ok=True)
        bundle = {
            "world": self.raw_world(),
            "schedules": self.raw_schedules(),
            "sessions": self.raw_sessions(),
            "tag": tag,
            "created_at": time.time(),
        }
        path = target / f"config-{stamp}.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
