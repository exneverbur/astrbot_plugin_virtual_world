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

## 扩展挂载点

插件本体不认任何具体扩展，只提供一组钩子；功能由**独立安装的扩展插件**挂上来：

- 扩展在它自己的 `__init__` 里找到主插件实例（`star_cls.extension_host`）再 `register(spec)`；
- `spec` 能挂的东西：额外动作、设置字段、每拍回调、提示词层、动作门控（不让做就跳过并记日志）、
  动作执行回调、以及给面板用的只读数据；
- 主插件负责把它们接到该接的地方：动作合并进动作库（撞名让位、坏定义丢弃）、
  `gate` 在动作开始前判一次、「这个会话不让做」的动作连名字都不进提示词、
  提示词层只在该扩展自己允许的会话里拼、状态里给每个扩展一格 `ext_data`；
- **扩展带来的动作不写进世界配置**：它们只在加载时并进来（编辑器保存时会自动摘掉）。
  否则扩展一停用 / 卸载，世界里就会剩下一堆没人认领、也删不干净的动作；
- 编辑器里这些动作单独归到动作库左侧菜单的「扩展动作」那一截，在那儿不能删、不能停用
  （它们不属于用户配置，开关在扩展自己手里）；
- **页面与接口都住在扩展里**：扩展在自己的 `pages/<名字>/index.html` 放一页（AstrBot 官方
  插件页机制，会自动出现在面板侧栏的「插件 WebUI」分组里），页面用
  `context.register_web_api` 注册自己的接口；主插件不留任何扩展专属的界面；
- 扩展自己的配置默认存在数据目录的 `extensions.json`（走挂载点的 `settings()` /
  `save_settings()`），不写进世界配置和预设；
- 挂载点还给扩展几件通用的能力：`sessions()`（当前启用的会话）、`state()` / `update_state()`
  （读、带锁改世界状态）、`node()` / `place_text()`（她现在在哪）、`adjust()`（推数值，0~1 夹住）、
  `remember()`（写一条记忆）、`log()` / `note_self()`（写日志与聊天留档）、`generate()`（打杂模型写一段）；
- **扩展要主模型额外标出来的字段**：扩展不改提示词，也改不动 JSON 协议，只能**声明**。
  声明写在 `ExtensionSpec.json_fields` 里：`{"name","kind","prompt","empty","max_items","max_chars"}`，
  `kind` 是 `list` / `str` / `num` / `bool` 之一。主插件负责三件事——把说明拼进
  「输出格式」那一层（**全是静态文本**，不随时间/地点变化，所以不影响前缀缓存）、
  按声明的形状把模型给的值收拾干净（截断 / 去重 / 限长 / 转类型）、把结果发给
  `ExtensionSpec.on_json(state, values, host)`。字段名必须是 `[a-z][a-z0-9_]*` 且不能占主插件
  已有的键（`actions`、`reasoning`、`tone`、`touch`…），最多 4 个字段；声明写错就安静丢掉。
  「他这一轮碰了她哪儿」（`touch`）是主插件自带的同类字段，用 `wants=("touch",)` 申请即可，
  形状由主插件定。**没人声明时提示词里一个字都不多**，代码里也没有任何扩展的痕迹。
- 没装扩展时，以上全部是空的，插件行为与从前完全一样。
