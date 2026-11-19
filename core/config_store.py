"""配置读写：data/world.json、data/schedules.json、data/sessions.json、data/state.db。

规则（对应设计文档第 13 章）：
- 首次启动自动生成默认配置，零配置可用；
- 升级时只补字段、不覆盖用户已有的值；
- 只有用户主动「恢复默认」才覆盖。
"""

from __future__ import annotations

import copy
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


def backfill_world_dict(
    raw: dict[str, Any], defaults: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """把 ``raw`` 里缺的默认键补上，返回 ``(新 dict, 补上的路径列表)``。

    T1（用户 2026-10-24）：``world.json`` 里很多新配置从来没落盘过，运行时靠
    ``_deep_merge(default_world(), raw)`` 兜着——用户在编辑器里看到的是默认值，
    盘上却没有，``/vw debug``、导出、手工编辑都看不到真实生效值。这里把缺的键
    真正写进文件，边界如下：

    * **只补缺失键**：用户已有的值一律不动——显式写成 ``null`` / ``0`` / ``""`` /
      ``[]`` 也算"他表达过意思"，同样保留；
    * 两边都是 dict 才递归；类型对不上（默认是 dict、盘上是标量，或反过来）保持
      用户那份，不往里补，更不抛异常；
    * 缺的是整段 dict 时**只记这一段的路径**（``agent``），不再把里面每个叶子列
      一遍——不然多一个新段就能刷满几十条路径；
    * 顺序稳定：先按默认值的书写顺序，再补用户自己多出来的键（保持原顺序）。
    """

    added: list[str] = []
    source = raw if isinstance(raw, dict) else {}
    merged = _backfill_merge(source, defaults or {}, "", added)
    return merged, added


def _backfill_merge(
    current: dict[str, Any], defaults: dict[str, Any], prefix: str, added: list[str]
) -> dict[str, Any]:
    """``backfill_world_dict`` 的递归体：返回补过键的新 dict，路径记进 ``added``。"""

    result: dict[str, Any] = {}
    for key, default in defaults.items():
        path = f"{prefix}{key}"
        if key not in current:
            result[key] = copy.deepcopy(default)
            added.append(path)
            continue
        value = current[key]
        if isinstance(default, dict) and isinstance(value, dict):
            result[key] = _backfill_merge(value, default, f"{path}.", added)
        else:
            result[key] = value
    for key, value in current.items():
        if key not in defaults:
            result[key] = value
    return result


def backfill_action_idle_activity(
    raw: dict[str, Any], defaults: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """T20：给老存档里的**内置动作**补 ``idle_activity``（只补这一个缺失键）。

    为什么需要单独一层：``backfill_world_dict`` 只补"整段缺失"的键，``actions``
    在盘上是一整段列表（老存档里是默认动作的序列化副本），列表里的每个动作不会
    因为它缺字段就被补——``world.json`` 里 77 条内置动作都没有 ``idle_activity``，
    而 ``search_web`` 的声明只写在 ``core/defaults.py`` 里，运行时永远长不出来。

    边界（和 T1 的回填保持一致）：

    * 只补 ``idle_activity`` 一个键，其它字段一律不动；
    * 只补 ``builtin`` 为真、或 id 出现在默认动作表里的动作，扩展动作不碰；
    * 用户已经写过的值一律保留——显式 ``null`` / ``{"weight": 0}`` 都算"他表达过
      意思"，不会再被默认声明覆盖；
    * 不写盘，盘上落点仍由 :meth:`ConfigStore.load_world` 现有的回填保存流程决定。
    """

    if not isinstance(raw, dict):
        return raw, []
    actions = raw.get("actions")
    default_actions = (defaults or {}).get("actions")
    if not isinstance(actions, list) or not isinstance(default_actions, list):
        return raw, []
    declared: dict[str, Any] = {}
    for item in default_actions:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("id") or "")
        declaration = item.get("idle_activity")
        if action_id and declaration is not None:
            declared[action_id] = declaration
    if not declared:
        return raw, []
    result = dict(raw)
    filled: list[Any] = []
    added: list[str] = []
    changed = False
    for item in actions:
        if not isinstance(item, dict):
            filled.append(item)
            continue
        action_id = str(item.get("id") or "")
        declaration = declared.get(action_id)
        is_builtin = bool(item.get("builtin")) or action_id in declared
        if (
            action_id
            and declaration is not None
            and is_builtin
            and "idle_activity" not in item
        ):
            item = {**item, "idle_activity": copy.deepcopy(declaration)}
            added.append(f"actions.{action_id}.idle_activity")
            changed = True
        filled.append(item)
    if not changed:
        return raw, []
    result["actions"] = filled
    return result, added


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
        self.last_backfill: list[str] = []
        """上一次 ``load_world()`` 补进 ``world.json`` 的路径（没补就是空表，给事件日志用）。"""

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
        raw = self._upgrade_agent_budgets(raw)
        raw = self._upgrade_event_genres(raw)
        # T1：盘上缺的默认键补回去（只补缺失键，不动用户值），补过才写盘。
        # 顺序：先迁移检测（``core.migrations.detect_all`` 读的是盘上原文，语义不动），
        # 再回填——补进来的都是"当前默认值"，而 ``detect_all`` 对"已经是新默认"的路径
        # 本来就不列，所以「有待确认的迁移」这个判定不会因为回填而变化。
        raw, backfilled = backfill_world_dict(raw, default_world())
        # T20：老存档里的内置动作是默认动作的**序列化副本**，不会自动长出
        # ``idle_activity``；这里按字段再补一层（只补缺失键，用户写过的 null /
        # weight 0 一律保留，扩展动作不碰）。写盘仍走下面同一条回填流程。
        raw, idle_backfilled = backfill_action_idle_activity(raw, default_world())
        backfilled = list(backfilled) + idle_backfilled
        self.last_backfill = list(backfilled)
        if backfilled:
            self._persist_backfill(raw, backfilled)
        # 升级补字段：默认值 + 用户值（用户值优先）
        merged = _deep_merge(default_world(), raw)
        merged["nodes"] = raw.get("nodes", merged.get("nodes", []))
        merged["edges"] = raw.get("edges", merged.get("edges", []))
        merged["actions"] = raw.get("actions", merged.get("actions", []))
        world, warnings = parse_world(merged)
        self.warnings.extend(warnings)
        self._world = world
        return world

    def _upgrade_agent_budgets(self, raw: dict[str, Any]) -> dict[str, Any]:
        """结构补全：盘上**根本没有 agent 段**时按当前默认写一份。

        这一节原本住在插件配置里（老版本），文件里没有它是「结构缺失」而不是「用户调过
        的值」—— 读盘这一层直接把默认写进去，不等用户确认。

        **内容迁移不在这里做**（T17）：``agent.query_total_seconds`` 还是最初那版的
        6 秒、``max_steps`` 还是 5，都属于「我们发过的默认值变了」，会被
        :func:`core.migrations.detect_all` 列进清单，用户在编辑器里勾过之后才落盘。
        以前这里识别出来就静默写回，用户既没看见改了什么，也没法拒绝。
        """

        if not isinstance(raw, dict):
            return raw
        if isinstance(raw.get("agent"), dict):
            return raw  # 有这一节：内容迁移交给 core.migrations
        fresh = default_world().get("agent")
        if not isinstance(fresh, dict) or not fresh:
            return raw
        result = dict(raw)
        result["agent"] = fresh
        try:
            self._write_json(self.world_path, result)
        except OSError as exc:
            self.warnings.append(f"Agent 段没能补进 world.json（{exc}），这次先按默认值跑")
        return result

    def _upgrade_event_genres(self, raw: dict[str, Any]) -> dict[str, Any]:
        """结构补全：``events`` 段在、但**题材表不完整**时按这一版的默认写一份。

        ``events.genres`` 缺失 / 是空表 / 根本不是列表，属于「文件不完整」——没有可逆的
        用户值可丢，读盘这一层直接补上默认。整段 ``events`` 都没有的情况不写盘：
        ``parse_world`` 会用模型默认补齐，那种世界照样跑得起来。

        **内容迁移不在这里做**（T17）：最初那 7 类（一个 weight 管三档、examples 是名词
        串）要换成按档位分权的一版，这件事改的是用户看得见的内容，所以交给
        :func:`core.migrations.detect_all` 列进清单，用户在编辑器里勾过才落盘。以前这里
        认出旧表就静默写回（还带一条 warning）：用户既没得选，warning 又因为
        ``save_world`` 写的是入参原始 dict 而永远清不掉。
        """

        if not isinstance(raw, dict):
            return raw
        events = raw.get("events")
        if not isinstance(events, dict):
            return raw
        genres = events.get("genres")
        if isinstance(genres, list) and genres:
            return raw  # 有表：内容迁移交给 core.migrations
        upgraded = dict(events)
        upgraded["genres"] = copy.deepcopy(WorldConfig().events.genres)
        if not str(events.get("genre_scale") or "").strip():
            upgraded["genre_scale"] = WorldConfig().events.genre_scale
        result = dict(raw)
        result["events"] = upgraded
        try:
            self._write_json(self.world_path, result)
        except OSError as exc:
            self.warnings.append(f"事件题材表没能补进 world.json（{exc}），这次先按默认值跑")
        return result

    def _persist_backfill(self, data: dict[str, Any], added: list[str]) -> None:
        """把补过键的 ``world.json`` 写回盘，写之前先留一份 ``.bak-backfill-<ts>``。

        只在真补到键时调用（幂等：第二次启动 ``added`` 为空，不写盘也不备份）。
        写失败只记一条 warning，**绝不挡住启动**——内存里那份已经补好了，这次照样按
        默认值跑。
        """

        path = self.world_path
        try:
            if path.exists():
                backup = path.with_name(f"{path.name}.bak-backfill-{int(time.time())}")
                backup.write_bytes(path.read_bytes())
            self._write_json(path, data)
        except OSError as exc:
            self.warnings.append(
                f"world.json 补的 {len(added)} 个默认键没写回（{exc}），这次先按默认值跑"
            )

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
        # 注意：**不要**在这里按类型去清 `tool_*` 字段。
        # 「指令 + 工具」是主插件里合法（而且必要）的组合——自拍 / 拍照那几条就是
        # 用指令触发一次工具调用；清掉 `tool_names` / `tool_mode` 之后图就出不来了。
        self._write_json(self.world_path, data if isinstance(data, dict) else {})
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
        temporary: bool = False,
        temp_origin: str = "",
        temp_source: str = "",
        snapshot: bool = True,
    ) -> bool:
        """往白名单里加一条会话（已有同 id 的正常行就重新启用）。

        ``temporary=True`` 是临时会话自动接管用的：多写几个来源字段，并且调用方
        会传 ``snapshot=False``——每私聊出去一个人就存一份全量快照太吵，而这条
        路径不涉及用户手改配置，回滚价值不大。

        **不覆盖用户意图**：命中一条"用户自己停用的普通会话"（``enabled`` 为假、
        也不是临时行）时什么都不做、返回 ``False``——自动接管不能把用户关掉的
        会话偷偷打开（审查 P2）。真要让用户重新启用，走 ``set_session_enabled``。
        期限只在 ``temp_sessions`` 表里存一份（``expires_at`` 曾在这里也写一份，
        两处必然分叉，已删）。
        """

        raw = self.raw_sessions()
        sessions = raw.setdefault("sessions", [])
        for item in sessions:
            if item.get("session_id") == session_id:
                if not item.get("enabled") and not item.get("temporary"):
                    return False
                item["enabled"] = True
                if temporary:
                    item["temporary"] = True
                    # 空串 = 不知道这条临时会话是从哪儿发起的（用户 m00001 追加需求）。
                    # 以前这里折回 session_id 自己，那是"自己指自己"，永远映射不出东西。
                    item["temp_origin"] = temp_origin or ""
                    item["temp_source"] = temp_source or "private"
                self.save_sessions(raw, snapshot=snapshot)
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
                "temporary": bool(temporary),
                # 同上：空串原样留着，别再折回 session_id 自己
                "temp_origin": (temp_origin or "") if temporary else "",
                "temp_source": temp_source if temporary else "",
            }
        )
        self.save_sessions(raw, snapshot=snapshot)
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

    def release_temp_session(self, session_id: str) -> bool:
        """解除临时会话接管：关掉白名单那一行、摘掉临时标记，行本身留着。

        这是 ``ended`` 黑洞的出口（审查 H2）：状态只在 DB 里改（``released``），
        这里负责让消息回到宿主默认路由。返回"是否真的有这一行"。
        """

        raw = self.raw_sessions()
        for item in raw.get("sessions", []):
            if item.get("session_id") == session_id:
                item["enabled"] = False
                if item.get("temporary"):
                    item["temporary"] = False
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
