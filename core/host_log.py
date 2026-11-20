"""插件里唯一的日志入口：宿主给的 ``astrbot.api.logger``。

为什么不直接在 ``core`` 的每个模块里写 ``from astrbot.api import logger``：
这些模块会被单元测试在**还没装 AstrBot** 的进程里导入（测试用
``tests/fake_astrbot.py`` 的替身，而替身是在导入之后才塞进 ``sys.modules`` 的），
模块级硬 import 会让 ``import core.events`` 直接 ``ModuleNotFoundError``。

所以这里做成懒取：

- 真宿主里第一次写日志时才去拿 ``astrbot.api.logger``。它是一个代理，按调用方
  模块解析出 ``astrbot.plugin.<插件名>`` 那个 stdlib Logger，``%s`` 占位与
  ``exc_info=True`` 都照旧有效；
- 拿不到（纯单测、离线跑脚本）就静默：日志不值得让整轮回复挂掉。

插件市场审核的规则是「logger 必须来自 ``astrbot.api``、不许用内置 logging」。
整个仓库只有这一处提到宿主日志器，换入口时只改这里。
"""

from __future__ import annotations

from typing import Any


class _SilentLogger:
    """没有宿主时的兜底：任何 ``logger.xxx(...)`` 什么都不做。"""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        return _noop


class _HostLogger:
    """懒解析的宿主日志器（第一次用时才 ``from astrbot.api import logger``）。"""

    __slots__ = ("_impl",)

    def __init__(self) -> None:
        self._impl: Any = None

    def _resolve(self) -> Any:
        if self._impl is None:
            try:
                from astrbot.api import logger as host_logger  # noqa: PLC0415
            except Exception:  # pragma: no cover - 只在没有宿主的脚本里走到
                host_logger = _SilentLogger()
            self._impl = host_logger
        return self._impl

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


logger = _HostLogger()
"""模块级单例：``from .host_log import logger`` 之后照常 ``logger.warning(...)``。"""
