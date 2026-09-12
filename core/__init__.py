"""虚拟世界插件核心层。

本包内的模块不导入 AstrBot，任何与宿主的交互都通过 ``core.ports`` 里的 Protocol 完成，
因此可以在没有 AstrBot 运行环境的情况下用 unittest 直接测试（见 tests/）。
"""

__all__ = ["engine"]
