"""在本地模拟"她的一天"，用于人工验收与回归观察。

不需要 AstrBot：用测试替身驱动引擎，把一天 24 小时按 tick 推进，
打印时间线（移动、睡觉、说话、数值变化），用来判断行为是否符合预期。

用法：
    python tools/simulate_day.py            # 默认模拟 1 天，tick=60 秒
    python tools/simulate_day.py --days 3   # 模拟 3 天
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

from core.config_store import ConfigStore  # noqa: E402
from core.db import AsyncDatabase  # noqa: E402
from core.engine import MessageContext, VirtualWorldEngine  # noqa: E402
from stub_ports import StubClock, StubLLM, StubMessenger, StubPersona, StubTools  # noqa: E402

SESSION = "aiocqhttp:GroupMessage:1001"
MESSAGES = [
    "早上好呀，今天也一起加油吧",
    "刚看到一条挺有意思的事，等我想想怎么说",
    "有人在吗？我有点无聊",
    "窗外的云好像在动，我看了好久",
    "今天天气不错，适合出门",
]


class CyclingLLM(StubLLM):
    """每次返回下一句预设台词，模拟"她会说话"的状态。"""

    def __init__(self) -> None:
        super().__init__([])
        self.index = 0

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        text = MESSAGES[self.index % len(MESSAGES)]
        self.index += 1
        payload = {"actions": [{"type": "say", "messages": [text]}]}
        reply = await super().generate(
            session_id=kwargs["session_id"],
            system_prompt=kwargs["system_prompt"],
            prompt=json.dumps(payload, ensure_ascii=False),
            temperature=None,
        )
        reply.text = json.dumps(payload, ensure_ascii=False)
        return reply


async def run(days: int, tick_seconds: int) -> None:
    tmp = tempfile.TemporaryDirectory()
    data_dir = Path(tmp.name)
    store = ConfigStore(data_dir)
    store.ensure_files()
    store.load_world()
    store.add_session(SESSION, cold_start_node="bedroom")

    db = AsyncDatabase(store.db_path)
    clock = StubClock(now=datetime(2026, 9, 10, 0, 0).timestamp(),
                      struct=datetime(2026, 9, 10, 0, 0))
    llm = CyclingLLM()
    messenger = StubMessenger()
    engine = VirtualWorldEngine(
        store=store,
        db=db,
        llm=llm,
        messenger=messenger,
        tools=StubTools({"web_search": "搜索网页"}, {"web_search": "鲸鱼一天要吃很多吨磷虾。"}),
        persona=StubPersona("你是一个温柔黏人的少女。"),
        clock=clock,
        tick_seconds=float(tick_seconds),
        decider_interval=float(tick_seconds * 5),
    )

    ticks = int(days * 86400 / tick_seconds)
    timeline: list[str] = []
    last_node = ""
    last_state = ""
    for index in range(ticks):
        outcomes = await engine.tick()
        for session_id in engine.enabled_session_ids():
            await engine.maybe_decide(session_id)
        for outcome in outcomes:
            for note in outcome.notes:
                if note.startswith("t="):
                    state = await engine.load_state(SESSION, cold_start=False)
                    if state.node_id != last_node:
                        last_node = state.node_id
                        node = engine.node(last_node)
                        timeline.append(
                            f"{clock.now_struct():%m-%d %H:%M}  走到 {node.name if node else last_node}"
                        )
                    if state.state != last_state:
                        last_state = state.state
                        timeline.append(
                            f"{clock.now_struct():%m-%d %H:%M}  状态 -> {state.state}"
                        )
            for message in outcome.messages:
                timeline.append(f"{clock.now_struct():%m-%d %H:%M}  她说：{message}")
        clock.advance(tick_seconds)
        clock.set_struct(
            datetime.fromtimestamp(clock.now())
        )

    state = await engine.load_state(SESSION, cold_start=False)
    print(f"模拟 {days} 天（tick={tick_seconds}s，共 {ticks} tick）")
    print("=" * 60)
    for line in timeline[:120]:
        print(line)
    if len(timeline) > 120:
        print(f"…（省略 {len(timeline) - 120} 条）")
    print("=" * 60)
    print(
        f"最终：位置={state.node_id} 状态={state.state} 心情={state.mood} "
        f"精力={state.energy:.2f} 孤独={state.loneliness:.2f} "
        f"好奇={state.curiosity:.2f} 社交={state.social:.2f} 无聊={state.boredom:.2f}"
    )
    print(f"累计发言 {len(messenger.flat_messages)} 条，LLM 调用 {len(llm.calls)} 次")
    print(f"记忆条数 {engine.memory.stats(SESSION)['total']}")
    await db.close()
    tmp.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟虚拟世界的一天")
    parser.add_argument("--days", type=float, default=1.0)
    parser.add_argument("--tick", type=int, default=60)
    args = parser.parse_args()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    asyncio.run(run(args.days, args.tick))


if __name__ == "__main__":
    main()
