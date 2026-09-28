# 本地验证与目录结构

[文档目录](README.md) · [按主题拆开的详细说明](GUIDE.md) · [返回 README](../README.md)

不装进群也能先跑起来验证，以及仓库里各个文件是干什么的。

## 本地验证与测试

核心层（`core/`）不 import AstrBot，宿主能力通过 `core/ports.py` 的 Protocol 注入，
因此可以在没有 AstrBot 的环境里直接跑测试：

```bash
python -m unittest discover -s tests -t .        # 107 个单元/集成测试
python tools/simulate_day.py --days 2            # 模拟她"在空群里过两天"
```

`tools/stub_llm_server.py` 是一个假的 OpenAI 兼容服务，配合 AstrBot 内置的 webchat 平台，
可以**不接 QQ** 就把整条链路跑通（它会把每个请求的原文写进日志，用来核对提示词）。


## 目录结构

```
astrbot_plugin_virtual_world/
├── main.py                 # AstrBot 适配：钩子、命令、Web API、适配器
├── metadata.yaml
├── _conf_schema.json
├── core/                   # 与宿主解耦的核心实现
│   ├── engine.py           # 状态机主循环（接管回复 / 注入 / tick / 日程 / 自主行为）
│   ├── state_dynamics.py   # 数值演化、事件影响、mood 推导
│   ├── memory.py           # 场景记忆：创建 / 召回 / 冲突 / 衰减
│   ├── decider.py          # 规则决策（含插话）
│   ├── prompt.py           # 五层提示词 + reasoning 契约
│   ├── json_actions.py     # LLM JSON 解析与校验降级
│   ├── planner.py          # 计划队列
│   ├── tool_policy.py      # 工具可用范围（通用工具 + 该地点动作绑定的工具）
│   ├── engagement.py       # 无人回应保护
│   ├── nickname.py         # 群名片文案
│   ├── config_store.py     # 配置生成 / 修补 / 热加载
│   ├── db.py               # sqlite 持久层
│   ├── models.py           # Pydantic 配置模型
│   ├── ports.py            # 宿主能力 Protocol（测试 seam）
│   └── defaults.py         # 开箱即用默认世界
├── pages/world_editor/     # 网页编辑器（官方插件 Page）
├── tests/                  # unittest 测试与测试替身
├── tools/                  # 本地模拟与假 LLM 服务
└── docs/                   # 本文件、API 事实核查、seam 清单、决策记录
```
