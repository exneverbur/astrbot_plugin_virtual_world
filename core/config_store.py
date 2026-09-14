"""配置读写：data/world.json、data/schedules.json、data/sessions.json、data/state.db。

规则（对应设计文档第 13 章）：
- 首次启动自动生成默认配置，零配置可用；
- 升级时只补字段、不覆盖用户已有的值；
- 只有用户主动「恢复默认」才覆盖。
"""

from __future__ import annotations

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


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把 base 作为默认值、override 作为用户值合并：用户值优先，缺失字段用默认值补全。"""

    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


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

    def save_world(self, data: dict[str, Any]) -> list[str]:
        world, warnings = parse_world(data)
        self._write_json(self.world_path, data if isinstance(data, dict) else {})
        self._world = world
        return warnings

    def save_schedules(self, data: dict[str, Any]) -> list[str]:
        _, warnings = parse_schedules(data)
        self._write_json(self.schedules_path, data if isinstance(data, dict) else {})
        return warnings

    def save_sessions(self, data: dict[str, Any]) -> list[str]:
        _, warnings = parse_sessions(data)
        self._write_json(self.sessions_path, data if isinstance(data, dict) else {})
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
    ) -> Path:
        """把当前三份配置打包成一个预设文件。"""

        clean = self._clean_preset_id(preset_id)
        if not clean:
            raise ValueError("预设 id 不能为空")
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "id": clean,
            "name": name or clean,
            "note": note,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "world": self.raw_world(),
            "schedules": self.raw_schedules(),
            "sessions": self.raw_sessions(),
        }
        path = self.preset_path(clean)
        self._write_json(path, payload)
        self.set_active_preset(clean)
        return path

    def read_preset(self, preset_id: str) -> dict[str, Any]:
        data = self._read_json(self.preset_path(preset_id), {})
        return data if isinstance(data, dict) else {}

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

    def apply_preset(self, preset_id: str) -> dict[str, Any]:
        """把预设写进当前配置（不含状态：状态由调用方决定要不要清）。"""

        data = self.read_preset(preset_id)
        if not data:
            raise ValueError("找不到这个预设")
        self.backup_current()
        world, warnings = parse_world(data.get("world") or {})
        self.save_world(world.model_dump(mode="json"))
        self.save_schedules(data.get("schedules") or {})
        self.save_sessions(data.get("sessions") or {})
        self.set_active_preset(preset_id)
        return {"warnings": warnings}

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
