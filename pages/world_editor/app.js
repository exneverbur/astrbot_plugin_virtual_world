/**
 * SOLIS世界编辑器前端。
 *
 * 设计原则：能点就不要手输、每一个输入框都有悬停说明、按动作类型显示/隐藏无关字段。
 * 运行在 AstrBot 插件 Page 的沙箱 iframe 里，只能通过 window.AstrBotPluginPage bridge
 * 调用插件后端，不能直接访问 Dashboard 的 cookie（所以插件二次密码的 token 只放内存）。
 */

const bridge = window.AstrBotPluginPage;

const ui = {
  status: {},
  config: { world: {}, schedules: {}, sessions: {} },
  sessions: [],
  // 临时会话（用户 m00001）：她私聊出去的陌生人，来自 GET /temp_sessions
  tempSessions: [],
  tools: [],
  overview: [],
  selectedNode: "",
  selectedAction: "",
  selectedZone: "",
  mapLevel: "world",
  // 全局设置：当前选中的分类标签，以及哪些标签里有未保存的改动
  settingsTab: "profile",
  // 设置首页默认只显示 5 张常用卡；切到 advanced 才铺开全部分类
  settingsMode: "common",
  passwordRequired: false,
  dirtyTabs: new Set(),
  // 每改一次 +1：保存只清"提交那一刻"的脏标记，保存期间的新编辑不许被 loadAll 冲掉
  editRevision: 0,
  // 保存正在进行（防重复提交；不是"退出编辑"的意思）
  saving: false,
  // 有未保存改动的**域**：world / schedules / sessions（保存只提交真正改过的）
  dirtyDomains: new Set(),
  events: {},
  // 素材页当前页签：谈资 / 表情包（离开再回来保持上次选择）
  materialPane: "topics",
  // 「数值」默认只读展示（彩色条），点「编辑数值」才切成滑杆
  valuesEdit: false,
  valueDraft: {},
  selectedSchedule: "",
  selectedGroup: "",
  // 动作库左侧菜单选中的那一组（""=全部；"group:名字" / "ext:扩展名"）
  actionGroup: "",
  presets: [],
  activePreset: "",
  token: "",
  statusTimer: null,
  historyHours: 24,
  historyBusy: false,
  defaults: { actions: {}, captions: {} },
  // 内容迁移（T17）：GET /migrations 的清单 + 这一次启动里已经问过的条目指纹
  // （保存会改 world_rev，但同一批迁移不该再问一遍，所以按条目 id 去重）
  migrations: { items: [], skipped: [], world_rev: "" },
  migrationPromptShown: "",
  // 通讯录：认识的人 + 当前选中的人
  contacts: [],
  contactUser: "",
  contactDetail: null,
  // 谈资：列表、翻页游标、功能开关，以及当前选中的那一条（详情 + 曝光记录）
  topics: {
    session: "",
    status: "",
    source: "",
    items: [],
    total: 0,
    nextCursor: "",
    enabled: null,
    selected: "",
    detail: null,
    shares: [],
    loading: false,
    // 「最近召回」：只读的观测面板，跟会话走（records 就是后端存的那几条轨迹）
    recall: { session: "", records: [], note: "", loading: false },
  },
  // 表情包：收藏列表、筛选条件、当前选中的那一条（详情在右边）
  meme: {
    scope: "",
    state: "",
    search: "",
    items: [],
    selected: "",
    detail: null,
    loading: false,
    summary: null,
    previews: {},
    missing: {},
    enabled: null,
    reason: "",
    // 用户 m14731：多选（删除 / 导出都认这份集合，换筛选条件不清空）
    checked: new Set(),
    // 批量补描述的后台任务（页面只轮询；见 memeDescribeMissing）
    describeJob: null,
    describeTimer: 0,
    // 「从文件夹导入」的后台任务（用户 m18755：要进度条）
    importJob: null,
    importTimer: 0,
    // 列表现在铺了多少张（用户 m18846：先 30 张，点「加载更多」再往上加）
    shown: 30,
    // 排序（用户 m21273）：collected=收藏时间 / used=使用次数；desc=true 为倒序
    order: "collected",
    desc: true,
    // 后端一共多少条、还能不能翻（用户 m18961：后端也分页）
    total: 0,
    hasMore: false,
  },
  // Agent 页的工具调用记录：她调用过哪些工具（倒序分页，游标按事件 id）
  agent: {
    items: [],
    cursor: 0,
    hasMore: false,
    onlyFailed: false,
    note: "",
    loading: false,
    // 分页（用户 m45903）：cursors[i] 是第 i+1 页的 before 游标，第 1 页是 0
    page: 1,
    cursors: [0],
    // 当前停在哪一页签：work / learning / drafts / runs
    pane: "work",
  },
};

/* ================================================================== */
/* 常量：中文标签与说明                                                 */
/* ================================================================== */

const ATTRS = [
  { key: "energy", label: "精力", hint: "越低越想睡觉，睡觉会恢复" },
  { key: "loneliness", label: "孤独感", hint: "越高越想找人说话" },
  { key: "curiosity", label: "好奇心", hint: "越高越想掏手机查点什么；门槛会随无聊/精力浮动" },
  {
    key: "affect",
    label: "心潮",
    hint: "情绪被激起的强度（不是开心程度）：越高越容易冲动、表达越浓；被夸、吵架、被冷落都会推高，之后慢慢回落。",
  },
  {
    key: "valence",
    label: "效价",
    hint: "心情好坏（0.5 为中性，越高越正面）：基线由精力 / 孤独 / 无聊 / 好奇推导，事件只造成短期偏移，之后回落。",
  },
  { key: "boredom", label: "无聊", hint: "越高越想换个地方待着" },
  {
    key: "desire",
    label: "欲求",
    hint: "想被人实实在在地碰一下（摸摸头、抱抱、亲亲）：没人碰就慢慢涨，被亲近一次落一截。",
  },
];

/** 地点「氛围」六个维度：中文名 + 影响说明（键名照旧写进提示词，方便和 JSON 对照）。 */
const ATMOSPHERES = [
  { key: "calm", label: "安静", hint: "越安静，精力恢复越快、无聊增长越慢" },
  { key: "intimacy", label: "私密", hint: "越私密，越适合休息和说心里话" },
  { key: "visibility", label: "显眼", hint: "越高越容易被大家注意到" },
  { key: "liveliness", label: "热闹", hint: "越热闹，心潮回落得越慢、无聊下降" },
  { key: "loneliness", label: "孤单", hint: "越高，在这里越容易觉得孤单" },
  { key: "curiosity", label: "新鲜", hint: "越高，在这里越容易好奇想找新鲜事" },
];

const GENDERS = [
  { key: "male", label: "男", hint: "文案里用「他」" },
  { key: "female", label: "女", hint: "文案里用「她」" },
  { key: "other", label: "塑料袋", hint: "不分性别，文案里用「ta」" },
];

/**
 * 「调试输出」可以勾选的事件类型。
 * key 必须和 core/models.py 里的 ECHO_EVENT_TYPES 一致（有一条单测做对齐检查）。
 */
const ECHO_TYPE_CHOICES = [
  {
    key: "incoming",
    icon: "📨",
    label: "收到消息",
    hint:
      "每一条到达插件的消息都记一笔（谁说的、说了什么、有没有喊她）。" +
      "选「完整」才发到群里，「精简」只留在日志页。",
    group: "core",
  },
  {
    key: "plan",
    icon: "🧠",
    label: "她的决定",
    hint: "这一轮打算做什么、为什么这么做、是谁定的（规则 / 大模型 / 日程 / 极端保护）。",
    group: "core",
  },
  {
    key: "action_start",
    icon: "▶️",
    label: "开始动作",
    hint: "开始一个动作，带预计耗时；移动还会写明去哪个地点。",
    group: "core",
  },
  {
    key: "action_done",
    icon: "✅",
    label: "动作完成",
    hint: "持续动作做完；精简模式下只显示做完了哪个动作。",
    group: "core",
  },
  {
    key: "action",
    icon: "🎬",
    label: "静默动作的内容",
    hint: "想事情这类本来不发到群里的动作写了什么。她自己说过的话不会重复发。",
    group: "core",
  },
  {
    key: "tool_call",
    icon: "🔧",
    label: "调用工具",
    hint: "她调了哪个工具、实际传出去的参数；精简模式下只留工具名。",
    group: "tool",
  },
  {
    key: "tool_result",
    icon: "📥",
    label: "工具返回",
    hint: "工具返回了什么、失败了报什么错，用来核对她说的是不是编的。",
    group: "tool",
  },
  {
    key: "command_call",
    icon: "🧩",
    label: "触发指令",
    hint: "她把哪条 AstrBot 指令发出去了（指令型动作）。",
    group: "tool",
  },
  {
    key: "command_result",
    icon: "📤",
    label: "指令返回",
    hint: "那条指令返回了什么、有没有跑失败。",
    group: "tool",
  },
  {
    key: "skip",
    icon: "⏭️",
    label: "被跳过的动作",
    hint: "某个动作没做成的具体原因（缺参数、地点不对、工具不存在…）。",
    group: "core",
  },
  {
    key: "memory",
    icon: "📝",
    label: "写了一条记忆",
    hint: "一段对话被总结成记忆时会发出来，便于核对是否准确。",
    group: "mind",
  },
  {
    key: "remember",
    icon: "🗂️",
    label: "她主动记进通讯录",
    hint: "她当场用「记住」动作写进通讯录的东西：记住了什么、没记成的原因。",
    group: "mind",
  },
  {
    key: "drowsy",
    icon: "😪",
    label: "困了（临睡期）",
    hint: "到点了先迷糊一会儿再睡，这段时间她还没躺下。",
    group: "sleep",
  },
  {
    key: "goodnight",
    icon: "🌙",
    label: "睡前晚安",
    hint: "睡前她自己决定要不要说晚安、发给谁；不说也会记一笔。",
    group: "sleep",
  },
  {
    key: "goodmorning",
    icon: "☀️",
    label: "睡醒早安",
    hint: "睡醒后她自己决定要不要说早安。",
    group: "sleep",
  },
  {
    key: "nickname",
    icon: "🏷️",
    label: "群名片变化",
    hint: "群名片变动的目标文案与是否成功。",
    group: "misc",
  },
  {
    key: "cancel",
    icon: "🛑",
    label: "按你说的停下",
    hint: "对方明确说别做了时，她停掉了哪个动作、放弃了哪些安排。",
    group: "sleep",
  },
  {
    key: "vision",
    icon: "🖼️",
    label: "图片内容",
    hint: "她是怎么看图的：转述成功时写了什么、失败是为什么、或者直接把图片交给多模态主模型。",
    group: "misc",
  },
  {
    key: "recall_start",
    icon: "💭",
    label: "回想开始",
    hint: "她想回忆什么（大模型给的意图），以及解析出来的检索范围（地点 / 区域 / 主题词）。",
    group: "mind",
  },
  {
    key: "recall_done",
    icon: "📖",
    label: "回想完成",
    hint: "她从记忆里翻出了什么；什么都没翻到时会写明。",
    group: "mind",
  },
  {
    key: "schedule_edit",
    icon: "🗓️",
    label: "改日程",
    hint: "她自己查看 / 添加 / 删除了哪条日程，成功了还是被拒了（用户配的日程她删不掉）。",
    group: "schedule",
  },
  {
    key: "schedule",
    icon: "📅",
    label: "日程开始执行",
    hint: "哪条日程开始执行了、是到点触发的还是你点了「立即执行」、这一串要跑哪些动作。",
    group: "schedule",
  },
  {
    key: "engagement",
    icon: "💤",
    label: "进入安静期",
    hint: "连续主动说话没人回应，触发无人回应保护。",
    group: "sleep",
  },
  {
    key: "extreme",
    icon: "🚨",
    label: "极端保护",
    hint: "精力透支、太久没人说话这类兜底规则被触发。",
    group: "sleep",
  },
  {
    key: "context",
    icon: "🗜️",
    label: "上下文压缩",
    hint: "群聊留档攒太多、被压成摘要的时候。",
    group: "misc",
  },
  {
    key: "wake_up",
    icon: "🌅",
    label: "被叫醒",
    hint: "她睡觉时被唤醒词叫起来。",
    group: "sleep",
  },
  {
    key: "sleep_reply",
    icon: "😴",
    label: "睡着时的回话",
    hint: "睡着时被 @，只回了一句固定文案。",
    group: "sleep",
  },
  {
    key: "sleep_skip",
    icon: "🤐",
    label: "睡着时没回复",
    hint: "睡着时被消息叫到，但按配置保持安静。",
    group: "sleep",
  },
  {
    key: "mood_reset",
    icon: "🌤️",
    label: "心情缓过来了",
    hint: "心情低落持续太久时，她自己缓一缓：效价朝基线拉回一半。",
    group: "sleep",
  },
  {
    key: "soothed",
    icon: "🫂",
    label: "被安抚 / 被理解",
    hint: "她低落时，抱一抱或有人听懂她那几句带来的那份心情回升（不占日常聊天额度）。",
    group: "mind",
  },
  {
    key: "poke",
    icon: "👉",
    label: "戳一戳",
    hint: "她戳某个群友的结果；没戳成时会写明原因（协议端不支持 / 冷却中…）。",
    group: "tool",
  },
  {
    key: "search_sources",
    icon: "🔗",
    label: "检索来源",
    hint: "这一轮联网查到了哪几条、来自哪些链接（默认不在群里发，只在日志里）。",
    group: "tool",
  },
  {
    key: "weather",
    icon: "🌤️",
    label: "天气",
    hint: "她查到 / 后台静默查到的天气写成了什么（也会写进提示词当背景）。",
    group: "misc",
  },
  {
    key: "search",
    icon: "🌐",
    label: "联网搜索",
    hint:
      "她开始联网检索时发一条。**检索期间的工具调用不会逐条发到群里**" +
      "（只在「日志」页里逐条保留），免得一次检索刷出十几行。",
    group: "tool",
  },
  {
    key: "search_digest",
    icon: "🧾",
    label: "检索整理",
    hint:
      "检索到的材料（条数、开头几条的预览）与压缩模型返回的要点。" +
      "用来分辨「源站只有入口页」和「压缩丢内容」这两种情况。",
    group: "tool",
  },
  {
    key: "event",
    icon: "🎰",
    label: "遇到事件",
    hint: "她自己遇上了什么事（做饭糊了、锅盖卡住、群里吵起来了…）。",
    group: "event",
  },
  {
    key: "event_choice",
    icon: "🌙",
    label: "事件抉择",
    hint: "这件事她打算怎么处理、派哪项能力值上。",
    group: "event",
  },
  {
    key: "event_action",
    icon: "🧰",
    label: "事件中调用动作",
    hint:
      "她为了这件事去调了动作（查资料、拍张照…），以及动作拿回来的结果。" +
      "这类结果默认只用来帮她判断，不直接发到群里。",
    group: "event",
  },
  {
    key: "event_check",
    icon: "🎲",
    label: "掷骰判定",
    hint: "能力值 × 情境修正 × 难度 = 概率 → 掷骰 → 四档结果（大成功 / 成功 / 勉强成功 / 失败）。",
    group: "event",
  },
  {
    key: "event_result",
    icon: "📖",
    label: "事件结果",
    hint: "这件事的结果、能力值变化，以及线索还有没有后续。",
    group: "event",
  },
  {
    key: "help",
    icon: "🆘",
    label: "求助与建议",
    hint: "她开口求助、收到群友的建议、等超时自己收尾。",
    group: "event",
  },
  {
    key: "event_idle",
    icon: "⏳",
    label: "挂起与收尾",
    hint: "线索挂起 / 过期 / 收尾：这件事暂时告一段落。",
    group: "event",
  },
  {
    key: "timeline",
    icon: "⏱️",
    label: "消息时间线",
    hint: "从收到到发完各段用了多久——排查「@ 了她很久才回」用。",
    group: "core",
  },
];

/** 调试输出的分组：类型多了以后按用途分块，找起来快。 */
const ECHO_GROUPS = [
  { key: "core", label: "决定与动作", hint: "她这一轮想做什么、做成了没有" },
  { key: "tool", label: "工具与指令", hint: "调用了哪个工具 / 指令，拿回了什么" },
  { key: "mind", label: "记忆与回想", hint: "记忆写成什么样、主动回想翻到了什么" },
  { key: "schedule", label: "日程", hint: "日程什么时候被触发、她怎么改自己的日程" },
  { key: "sleep", label: "睡眠与保护", hint: "睡觉门禁、被叫醒、打断、兜底保护" },
  { key: "event", label: "事件与线索", hint: "遇上什么事、怎么判定、求助与后续" },
  { key: "misc", label: "图片 · 上下文 · 名片", hint: "看图、上下文压缩、群名片变化" },
];

/** 日志页：事件类型 → 图标与中文名。 */
const LOG_TYPES = {
  reply: { icon: "💬", label: "回复（含推理草稿）" },
  plan: { icon: "🧠", label: "决策 / 计划" },
  action: { icon: "🎬", label: "执行动作" },
  action_start: { icon: "▶️", label: "开始持续动作" },
  action_done: { icon: "✅", label: "动作完成" },
  bot_message: { icon: "📣", label: "主动发言" },
  user_message: { icon: "👤", label: "用户消息" },
  tool: { icon: "🔧", label: "工具调用" },
  move: { icon: "🚶", label: "移动" },
  schedule: { icon: "⏰", label: "日程到点" },
  skip: { icon: "⏭️", label: "被跳过" },
  image_reaction: { icon: "🖼️", label: "看图接话（说了）" },
  image_reaction_skip: { icon: "🤐", label: "看图接话（没插话）" },
  nickname: { icon: "🪪", label: "群名片" },
  engagement: { icon: "🔇", label: "进入安静期" },
  extreme: { icon: "⚠️", label: "极端保护" },
  wake_up: { icon: "👋", label: "被叫醒" },
  interrupt: { icon: "✋", label: "打断" },
  pause: { icon: "⏸️", label: "先暂停" },
  resume: { icon: "▶️", label: "接着做" },
  trouble: { icon: "⚠️", label: "出错了" },
  dm_send: { icon: "📨", label: "主动私聊" },
  delivery_skip: { icon: "🚫", label: "没发出去" },
  cancel: { icon: "🛑", label: "按你说的停下" },
  vision: { icon: "🖼️", label: "图片内容" },
  recall_start: { icon: "💭", label: "回想开始" },
  recall_done: { icon: "📖", label: "想起了什么" },
  remember: { icon: "🗂️", label: "她主动记进通讯录" },
  drowsy: { icon: "😪", label: "困了（临睡期）" },
  goodnight: { icon: "🌙", label: "睡前晚安" },
  goodmorning: { icon: "☀️", label: "睡醒早安" },
  schedule_edit: { icon: "🗓️", label: "改日程" },
  command: { icon: "🧩", label: "触发指令" },
  command_call: { icon: "🧩", label: "触发指令" },
  command_result: { icon: "📤", label: "指令返回" },
  tool_call: { icon: "🔧", label: "调用工具" },
  tool_result: { icon: "📥", label: "工具返回" },
  mood_reset: { icon: "🌤️", label: "心情缓过来" },
  plugin_forge_draft: { icon: "🧪", label: "写了插件草稿" },
  plugin_forge_install: { icon: "🧩", label: "装了插件" },
  day_mood: { icon: "🌅", label: "今天的调子" },
  desire_push: { icon: "🫂", label: "想被碰一碰" },
  soothed: { icon: "🫂", label: "被安抚 / 被理解" },
  storm: { icon: "🌩️", label: "情绪上头 / 平复" },
  poke: { icon: "👉", label: "戳一戳" },
  search_sources: { icon: "🔗", label: "检索来源" },
  weather: { icon: "🌤️", label: "天气" },
  search: { icon: "🌐", label: "联网搜索" },
  event: { icon: "🎰", label: "遇到事件" },
  event_choice: { icon: "🌙", label: "事件抉择" },
  event_check: { icon: "🎲", label: "掷骰判定" },
  event_result: { icon: "📖", label: "事件结果" },
  help: { icon: "🆘", label: "求助与建议" },
  event_idle: { icon: "⏳", label: "挂起与收尾" },
  chain: { icon: "🔗", label: "动作链" },
  cold_start: { icon: "🌅", label: "冷启动" },
  bot_spoke: { icon: "🗣️", label: "发言等待回应" },
  timeline: { icon: "⏱️", label: "消息时间线" },
  follow_up: { icon: "⏳", label: "追问（等回话）" },
};

/**
 * 一条事件在日志页里显示成什么。
 *
 * `follow_up` 不能一律叫「追问（等回话）」：老版本把她每说一句都记一条，
 * 没起算的也记（`detail.armed === false`），时间线上就成了"她每句都在追人"。
 * 现在只记真起算的，盘上的老记录按 `armed` 分开显示。
 */
function eventMeta(event) {
  const type = String((event && event.event_type) || "");
  const meta = LOG_TYPES[type] || { icon: "•", label: type };
  const detail = event && event.detail && typeof event.detail === "object" ? event.detail : {};
  if (type === "follow_up" && detail.armed === false) {
    // 到点该追、模型却说不用等了：那是"没追"，不是"她还在等人接话"
    if (detail.closed) return { icon: "💤", label: "到点没追（不用等了）" };
    return { icon: "💤", label: "不追问（这句不用等）" };
  }
  return meta;
}

/** 扩展注册的事件类型（`/config` 里的 `debug_events`）：并进上面那张表。 */
let EXT_DEBUG_TYPES = [];
/** 扩展注册的调试类型在「调试输出」清单里长什么样（每种一行）。 */
let EXT_ECHO_CHOICES = [];
/** 它们各自归到哪一组（一个扩展一组）。 */
let EXT_ECHO_GROUPS = [];

function applyDebugEvents(rows) {
  EXT_DEBUG_TYPES = [];
  EXT_ECHO_CHOICES = [];
  EXT_ECHO_GROUPS = [];
  const byGroup = new Map();
  (Array.isArray(rows) ? rows : []).forEach((item) => {
    const key = String((item && item.type) || "").trim();
    if (!key) return;
    EXT_DEBUG_TYPES.push(key);
    if (!LOG_TYPES[key]) {
      LOG_TYPES[key] = {
        icon: String((item && item.icon) || "•"),
        label: String((item && item.label) || key),
      };
    }
    const owner = String((item && item.ext) || "扩展");
    const groupKey = `ext:${owner}`;
    if (!byGroup.has(groupKey)) {
      byGroup.set(groupKey, true);
      EXT_ECHO_GROUPS.push({
        key: groupKey,
        label: `扩展：${owner}`,
        hint: "扩展自己注册的调试类型：勾上就把它们也发到群里（完整 / 精简各选一次）。",
      });
    }
    EXT_ECHO_CHOICES.push({
      key,
      icon: String((item && item.icon) || "•"),
      label: String((item && item.label) || key),
      hint: String((item && item.hint) || "扩展注册的调试类型。"),
      group: groupKey,
    });
  });
}

const OPS = [
  { key: "+", label: "增加" },
  { key: "-", label: "减少" },
  { key: "=", label: "设为" },
  { key: "×", label: "乘以" },
];

const WEEKDAYS = [
  { key: "mon", label: "周一" },
  { key: "tue", label: "周二" },
  { key: "wed", label: "周三" },
  { key: "thu", label: "周四" },
  { key: "fri", label: "周五" },
  { key: "sat", label: "周六" },
  { key: "sun", label: "周日" },
];

/** 推理草稿的五个字段（和 core/prompt.py 里的 reasoning 协议一致）。 */
const REASONING_LABELS = [
  ["env", "在哪"],
  ["state", "状态"],
  ["mood", "心情"],
  ["who", "在和谁说话"],
  ["inner", "心里想的"],
  ["intent", "下一步计划"],
];

const STATES = [
  { key: "idle", label: "空闲" },
  { key: "awakening", label: "刚醒" },
  { key: "sleeping", label: "睡觉" },
  { key: "napping", label: "小睡" },
  { key: "drowsy", label: "犯困了" },
  { key: "staring", label: "发呆" },
  { key: "searching", label: "上网" },
  { key: "reading", label: "看书" },
  { key: "walking", label: "移动中" },
  { key: "thinking", label: "沉思" },
];

/**
 * 状态显示名：内置状态用内置中文；动作自己写的「执行期间状态标识」
 * （例如 watching）用「<动作名>中」，实在认不出来才退回原样——
 * 状态页不该出现让人看不懂的英文标识。
 */
function stateLabelOf(stateId, data) {
  const key = String(stateId || "");
  if (!key) return "";
  const known = STATES.find((item) => item.key === key);
  if (known) return known.label;
  // 扩展给的（"兴奋中"这种）优先于任何推断
  const fromExtension = String((data && data.extension_status) || "").trim();
  if (fromExtension && key === String((data && data.state) || "")) return fromExtension;
  const action = (actions() || []).find(
    (item) => String((item.during || {}).state || "") === key,
  );
  if (action) return `${action.name || action.id}中`;
  return key;
}

const CATEGORIES = [
  { key: "instant", label: "瞬时动作", hint: "立刻完成，不占用时间（例如说话、抱抱）" },
  { key: "continuous", label: "持续动作", hint: "要占用一段时间，期间她的状态会变成「执行期间状态」（例如睡觉、看书、上网）" },
];

const LLM_LEVELS = [
  {
    key: "template",
    label: "模板",
    hint: "不调用大模型，直接发固定文案（最省钱）。适合「伸懒腰」这类固定小动作",
  },
  {
    key: "single",
    label: "单轮",
    hint: "让大模型说一句话。适合说话、抱抱、分享见闻",
  },
  {
    key: "tool",
    label: "工具型",
    hint: "调用 AstrBot 里已注册的工具（搜索、天气等）。她只需要说明想干什么，参数由插件自动补全",
  },
  {
    key: "command",
    label: "指令触发",
    hint: "触发其他插件的一条指令（例如 /天气）：她只说想干什么，参数由辅助模型拼好，结果再交回给她说一句。",
  },
];

const TRIGGERS = [
  {
    key: "none",
    label: "什么都不做",
    hint: "动作做完即结束。工具 / 指令型动作看「工具结果回话」：打开则把结果交回大模型说一句，关闭则不开口。",
  },
  {
    key: "llm_followup",
    label: "让她接着说一句",
    hint: "把动作结果交给大模型，用她自己的人设说出来（例如搜索完把结果讲成见闻）",
  },
  {
    key: "schedule",
    label: "接着执行另一个日程",
    hint: "动作完成后立刻执行指定日程里的动作链",
  },
];

const TARGET_TYPES = [
  { key: "none", label: "自己 / 空间", hint: "动作只作用于她自己或环境，默认不发到群里" },
  { key: "user", label: "某个群友", hint: "动作的目标是人（例如抱抱、挥手）" },
  { key: "group", label: "整个群", hint: "动作面向所有人（例如说话、分享）" },
];

/** 一个动作挂了多个工具时的用法。 */
const TOOL_MODES = [
  { key: "sequence", label: "按顺序都调", hint: "装了的都调一遍，按顺序，结果合并（例如先搜索、再抓正文）" },
  { key: "fallback", label: "依次尝试", hint: "只用一个：按顺序挑第一个能用的，失败了换下一个" },
  { key: "smart", label: "智能选择", hint: "让辅助模型按她的意图挑一个（选择与补参数一次完成），失败就换下一个" },
];

/** 工具型动作的编排形态。 */
const TOOL_FLOWS = [
  {
    key: "simple",
    label: "直接调用",
    hint: "调完把结果交回给她说一句；多个工具按上面的「工具用法」执行。",
  },
  {
    key: "search",
    label: "联网检索",
    hint: "查东西专用：多条查询词 → 整理成带编号的证据 → 可选抓正文 → 不够再补查，她照着证据讲，不许编。",
  },
];

/** 检索深度。 */
const SEARCH_DEPTHS = [
  { key: "quick", label: "快查", hint: "只查一轮，不读正文、不补查。问一句立刻要答案时用。" },
  { key: "standard", label: "标准", hint: "查一轮 + 读前两篇正文 + 不够时补查一轮（默认）。" },
  { key: "deep", label: "深挖", hint: "至少读三篇、最多补查两轮，适合让她把一件事讲透。" },
];

const SCOPE_MODES = [
  { key: "global", label: "全局", hint: "任何地点都能做这个动作" },
  { key: "node", label: "仅特定地点", hint: "这个动作属于指定地点；她不在那儿时想用它，会被带过去再做" },
];

const EVENT_USABLE_MODES = [
  {
    key: "auto",
    label: "跟随规则",
    hint: "工具型 / 指令型动作可用；会主动往群里发东西的动作（分享那类）排除在外",
  },
  { key: "allow", label: "强制允许", hint: "即使不符合上面的规则，事件里也可以调用它" },
  { key: "deny", label: "事件中禁用", hint: "事件里不允许调用它" },
];

const DURATION_MODES = [
  { key: "fixed", label: "固定时长", hint: "每次都用同样的时长" },
  {
    key: "llm",
    label: "由大模型决定",
    hint: "把时长交给大模型在区间内自己定（例如「小睡一会儿」睡多久由她决定），超出区间会被自动夹住",
  },
];

const SCOPE_MODES_MEMORY = [
  { key: "group_persona", label: "群 + 人格（推荐）", hint: "同一个她在不同群里共享人格记忆，但不串群" },
  { key: "group", label: "只看本群", hint: "记忆严格限制在当前会话" },
  { key: "persona", label: "按人格共享", hint: "该人格在所有群的记忆都能想起" },
  { key: "global", label: "全局共享", hint: "所有会话共享记忆（慎用）" },
];

/* ================================================================== */
/* 基础工具                                                            */
/* ================================================================== */

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function option(value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  return node;
}

function num(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/**
 * 读一个「必须为正」的配置数字：留空 / 0 / 认不出的值一律退回默认值。
 *
 * 记忆那几项（上限、保护期、衰减）在后端就是 ``or 默认值`` 的写法——0 不是什么
 * 「关掉」，而是「没填」。设置页必须按同一套口径显示，否则用户看到 0 会以为
 * 容量闸/衰减被关掉了（记忆上限填 0 还可能让人以为一条都不留）。
 */
function positiveNum(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

/** 字节数给人看（图片资产库的占用）。 */
function formatBytes(value) {
  const size = num(value, 0);
  if (size < 1024) return `${Math.round(size)} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.remove("hidden");
  window.clearTimeout(toast._timer);
  // 4 秒：多行提示（配置提醒、保存失败的几行摘要）按 2.6 秒根本读不完
  toast._timer = window.setTimeout(() => node.classList.add("hidden"), 4000);
}

/**
 * 复制一段文本。
 * 插件页跑在沙箱 iframe 里，clipboard 权限不一定给，所以失败时退回"选中 + 复制"。
 */
function copyText(text) {
  const fallback = () => {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "readonly");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (error) {
      ok = false;
    }
    area.remove();
    toast(ok ? "已复制" : "复制失败：可以手动选中再复制");
  };
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    navigator.clipboard.writeText(text).then(() => toast("已复制"), fallback);
    return;
  }
  fallback();
}

/**
 * 让一个按钮进入"处理中"状态，返回恢复用的函数。

 * 那些要调模型的按钮（生成候选、出题、从聊天挑…）动辄几秒到几十秒，
 * 不置灰、不写"生成中"的话，用户看到的就是"点了没反应"。
 */
function busyButton(button, text = "处理中…") {
  if (!button) return () => {};
  const original = button.textContent;
  button.disabled = true;
  button.textContent = text;
  return () => {
    button.disabled = false;
    button.textContent = original;
  };
}

function setSaveState(text) {
  $("save-state").textContent = text || "";
}

/** 标记"有未保存的改动"（保存成功后会自动热加载）。 */
/**
 * 这个改动落在哪个域：`world`（世界配置）/ `schedules`（日程）/ `sessions`（会话）。
 *
 * 以前这里一律当成"当前设置标签"，于是改日程也会在「全局设置」的某个标签上点圆点、
 * 保存时还把世界配置一起提交（复核报告 P1-2 后半 + 脏点问题）。
 * 现在按**改动发生在哪一页**判断：用户正在操作的控件所在的 section 就是答案。
 */
function dirtyDomainNow() {
  const active = typeof document !== "undefined" ? document.activeElement : null;
  const host = active && active.closest ? active.closest("section.tab") : null;
  const tab = host ? String(host.id || "").replace(/^tab-/, "") : "";
  if (tab === "schedules") return "schedules";
  if (tab === "sessions") return "sessions";
  return "world";
}

const DIRTY_DOMAIN_LABELS = { world: "世界配置", schedules: "日程", sessions: "会话" };

/** 未保存改动的域（给状态行用的小结）。 */
function dirtyDomainText() {
  const names = Array.from(ui.dirtyDomains || [])
    .filter((key) => DIRTY_DOMAIN_LABELS[key])
    .map((key) => DIRTY_DOMAIN_LABELS[key]);
  return names.join("、");
}

function markDirty(domain = "") {
  ui.dirty = true;
  // 版本号只增不减：保存成功时用它判断"这次提交之后有没有人又改过"
  ui.editRevision = (ui.editRevision || 0) + 1;
  const key = String(domain || dirtyDomainNow() || "world");
  if (ui.dirtyDomains) ui.dirtyDomains.add(key);
  setSaveState(`有未保存的改动（${dirtyDomainText() || DIRTY_DOMAIN_LABELS[key] || "世界配置"}）`);
  // 只有**世界配置**的改动才在设置标签上点圆点（日程 / 会话不在设置里，点了反而误导）；
  // 而且点的是"改动发生的那一档"，不是"当前焦点停在哪一档"。
  if (key === "world" && ui.settingsTab) {
    ui.dirtyTabs.add(ui.settingsTab);
    renderSettingsTabs();
  }
}

/**
 * 给一个表单容器挂一次「change 委托」：里面任何控件改过就算这个域有改动。
 *
 * 为什么要这么兜一层（用户 m29086 的日程描述改了存不下来）：`inputField` /
 * `checkboxField` 这些通用控件**本身不调 markDirty**——只有少数专用控件会在自己的
 * change 里调。于是像「日程描述」这种直接复用通用控件的字段，改完编辑器不认，
 * 保存按钮说「没有要保存的改动」，刷新就丢了。与其逐个控件打补丁（漏一个就再犯一次），
 * 不如在**容器**上统一听 change。`[data-no-dirty]` 是给纯展示的小开关留的口子。
 */
function bindDirtyChange(container, domain) {
  if (!container) return container;
  if (container.dataset && container.dataset.dirtyBound === "1") return container;
  if (container.dataset) container.dataset.dirtyBound = "1";
  container.addEventListener("change", (event) => {
    const target = event.target;
    if (!target || (target.closest && target.closest("[data-no-dirty]"))) return;
    markDirty(domain || "");
  });
  return container;
}

/** 当前性别对应的称呼：她 / 他 / ta。 */
function pronoun() {
  const gender = (ui.config.world && ui.config.world.gender) || "female";
  if (gender === "male") return "他";
  if (gender === "other") return "ta";
  return "她";
}

/**
 * 把编辑器界面上写死的「她」换成当前称呼。
 * 只处理自己写的说明文字；带 data-keep-text 的容器（记忆内容、状态、日志等）跳过，
 * 避免改动用户/模型产生的原文。
 */
function applyPronoun(root) {
  if (!root) return;
  const replacement = pronoun();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    let skip = false;
    // 一路查到最外层：data-keep-text 通常挂在列表容器上（如 #memory-list），
    // 而这里的 root 可能是刚插入的那一小块 DOM，所以不能在中途提前停。
    for (let parent = node.parentElement; parent; parent = parent.parentElement) {
      if (parent.dataset && parent.dataset.keepText) {
        skip = true;
        break;
      }
    }
    if (!skip) nodes.push(node);
  }
  nodes.forEach((node) => {
    const text = node.nodeValue;
    if (!text) return;
    // 只换界面文案里写死的「她」（默认称呼就是她）；
    // 「他」在用户自己的内容里到处都是（动作名「提醒他喝水」、群友说的话），不能动。
    const next = text.replace(/她/g, replacement);
    if (next !== text) node.nodeValue = next;
  });
}

/**
 * 表单是"点一下重绘一次"的，单次替换会漏掉之后新生成的文字，
 * 所以监听 DOM 变化，新增节点一出现就替换称呼。
 */
function watchPronoun() {
  const root = $("app");
  if (!root || watchPronoun._observer) return;
  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      mutation.addedNodes.forEach((node) => {
        if (node.nodeType === 1) applyPronoun(node);
        else if (node.nodeType === 3 && node.parentElement) applyPronoun(node.parentElement);
      });
    });
  });
  observer.observe(root, { childList: true, subtree: true });
  watchPronoun._observer = observer;
}

async function apiGet(endpoint, params = {}) {
  const query = { ...params };
  if (ui.token) query.token = ui.token;
  return bridge.apiGet(endpoint, query);
}

async function apiPost(endpoint, body = {}) {
  const payload = { ...body };
  if (ui.token) payload.token = ui.token;
  return bridge.apiPost(endpoint, payload);
}

/** 带悬停说明的问号图标。 */
function tipBox(text) {
  if (!text) return null;
  const span = el("span", "tip", "?");
  span.setAttribute("data-tip", text);
  span.setAttribute("title", text);
  return span;
}

function fieldHead(label, hint, onRestore) {
  const head = el("div", "field-head");
  head.appendChild(el("span", "", label));
  const tip = tipBox(hint);
  if (tip) head.appendChild(tip);
  if (onRestore) {
    // 「恢复默认」：默认文案来自后端（core/defaults.py），前端不复制一份
    const button = el("button", "icon-btn restore-btn", "↺");
    button.type = "button";
    button.title = "恢复成内置默认";
    button.addEventListener("click", (event) => {
      event.preventDefault();
      onRestore();
    });
    head.appendChild(button);
  }
  return head;
}

/* ---- 通用表单控件（每个都支持 hint 悬停说明） ---- */

/**
 * 组合框：点一下就把候选列表展开，同时也允许直接输入新值。
 * （原生 datalist 要先打字才会展开，这里换成"点开即显示全部候选 + 输入即过滤"。）
 */
function comboField(label, value, options, onChange, opts = {}) {
  const wrapper = el("div", "field combo-field");
  wrapper.appendChild(fieldHead(label, opts.hint));
  const box = el("div", "combo");
  const input = document.createElement("input");
  input.type = "text";
  input.value = value ?? "";
  if (opts.placeholder) input.placeholder = opts.placeholder;
  box.appendChild(input);

  const caret = el("button", "combo-caret", "▾");
  caret.type = "button";
  caret.title = "展开候选";
  box.appendChild(caret);

  const panel = el("div", "combo-panel hidden");
  box.appendChild(panel);
  wrapper.appendChild(box);

  const all = Array.from(new Set((options || []).map((item) => String(item))))
    .filter((item) => item)
    .sort();
  let cursor = -1;

  function visible() {
    const keyword = input.value.trim().toLowerCase();
    if (!keyword) return all;
    return all.filter((item) => item.toLowerCase().includes(keyword));
  }

  function commit(text) {
    input.value = text;
    onChange(text);
  }

  function close() {
    panel.classList.add("hidden");
    cursor = -1;
  }

  function draw() {
    const items = visible();
    panel.innerHTML = "";
    if (!items.length) {
      panel.appendChild(
        el(
          "div",
          "combo-empty",
          all.length ? "没有匹配的分组，直接输入就是新建一个" : "还没有别的分组，直接输入即可",
        ),
      );
    }
    items.forEach((item, index) => {
      const row = el("div", `combo-item${index === cursor ? " cursor" : ""}`, item);
      row.addEventListener("mousedown", (event) => {
        // mousedown 早于 input 的 blur，先抢下来再关面板
        event.preventDefault();
        commit(item);
        close();
      });
      panel.appendChild(row);
    });
    panel.classList.remove("hidden");
  }

  caret.addEventListener("mousedown", (event) => {
    event.preventDefault();
    if (panel.classList.contains("hidden")) draw();
    else close();
  });
  input.addEventListener("focus", draw);
  input.addEventListener("click", draw);
  input.addEventListener("input", () => {
    cursor = -1;
    draw();
  });
  input.addEventListener("change", () => commit(input.value.trim()));
  input.addEventListener("blur", () => window.setTimeout(close, 120));
  input.addEventListener("keydown", (event) => {
    const items = visible();
    if (event.key === "Escape") {
      close();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!items.length) return;
      event.preventDefault();
      cursor = (cursor + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      draw();
      return;
    }
    if (event.key === "Enter" && cursor >= 0 && items[cursor]) {
      event.preventDefault();
      commit(items[cursor]);
      close();
    }
  });
  return wrapper;
}

function inputField(label, value, onChange, opts = {}) {
  const wrapper = el("label");
  if (opts.adv) wrapper.dataset.adv = "1";
  wrapper.appendChild(
    fieldHead(label, opts.hint, opts.onRestore
      ? () => {
          const text = String(opts.onRestore() ?? "");
          input.value = text;
          onChange(text);
          markDirty();
        }
      : null),
  );
  const input = document.createElement("input");
  input.type = opts.type || "text";
  if (opts.min !== undefined) input.min = opts.min;
  if (opts.max !== undefined) input.max = opts.max;
  if (opts.step !== undefined) input.step = opts.step;
  if (opts.placeholder) input.placeholder = opts.placeholder;
  if (opts.list && opts.list.length) {
    const listId = `dl-${Math.random().toString(36).slice(2, 8)}`;
    const datalist = document.createElement("datalist");
    datalist.id = listId;
    opts.list.forEach((item) => datalist.appendChild(option(String(item), String(item))));
    wrapper.appendChild(datalist);
    input.setAttribute("list", listId);
  }
  input.value = value ?? "";
  input.addEventListener("change", () => onChange(input.value));
  if (opts.onInput) input.addEventListener("input", () => opts.onInput(input.value));
  wrapper.appendChild(input);
  return wrapper;
}

function textareaField(label, value, onChange, opts = {}) {
  const wrapper = el("label");
  if (opts.adv) wrapper.dataset.adv = "1";
  wrapper.appendChild(
    fieldHead(label, opts.hint, opts.onRestore
      ? () => {
          const text = String(opts.onRestore() ?? "");
          input.value = text;
          onChange(text);
          markDirty();
        }
      : null),
  );
  const input = document.createElement("textarea");
  input.value = value ?? "";
  if (opts.placeholder) input.placeholder = opts.placeholder;
  if (opts.rows) input.rows = opts.rows;
  input.addEventListener("change", () => onChange(input.value));
  wrapper.appendChild(input);
  return wrapper;
}

/** 一行里的小输入框（关系表 / 亲密度分级那种密集编辑用）。 */
function cellInput(value, onInput, opts = {}) {
  const input = document.createElement("input");
  input.type = opts.type || "text";
  input.value = value ?? "";
  if (opts.placeholder) input.placeholder = opts.placeholder;
  if (opts.min !== undefined) input.min = opts.min;
  if (opts.max !== undefined) input.max = opts.max;
  if (opts.step !== undefined) input.step = opts.step;
  if (opts.title) input.title = opts.title;
  if (opts.width) input.style.width = `${opts.width}px`;
  input.addEventListener("change", () => {
    onInput(input.value);
    markDirty();
  });
  return input;
}

function cellCheck(label, checked, onChange, title = "") {
  const wrap = el("label", "cell-check");
  if (title) wrap.title = title;
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = Boolean(checked);
  box.addEventListener("change", () => {
    onChange(box.checked);
    markDirty();
  });
  wrap.appendChild(box);
  wrap.appendChild(el("span", "", label));
  return wrap;
}

/** 一行里的小下拉框（关系表选档位这种"从固定几项里挑一个"用）。 */
function cellSelect(value, options, onChange, opts = {}) {
  const select = document.createElement("select");
  (options || []).forEach((item) => {
    select.appendChild(option(String(item.value), String(item.label)));
  });
  select.value = String(value ?? "");
  if (opts.title) select.title = opts.title;
  if (opts.width) select.style.width = `${opts.width}px`;
  select.addEventListener("change", () => {
    onChange(select.value);
    markDirty();
  });
  return select;
}

/**
 * 关系表：每行一种关系。以前是一整块 JSON，改一个上限都得对着括号数数。
 * 槽位决定"同类关系只能有一个"（例如 romance 槽位里男友和女友不能并存）。
 */
/* ==================== 「手感」滑块 ====================
 *
 * 新用户脑子里想的是"话多话少""粘不粘人"，不是 max_llm_text_per_hour。
 * 一个滑块 = 一组本来要一起调才自洽的字段（自主发言上限调高了、预算还卡着，
 * 行为就自相矛盾）。规矩：
 *
 * - **每个滑块管的字段互不重叠**，不然拖完 A 再拖 B，A 的效果就没了；
 * - **中间那档 = 内置默认值**，老用户拖到中间等于没改；
 * - 拖动只写它管的那几个字段，别的一律不碰；
 * - 明细摊开给用户看——只有滑块没有明细就是黑箱。
 */

const KNOB_LEVEL_NAMES = ["很低", "偏低", "标准", "偏高", "很高"];

const KNOBS = [
  {
    id: "chat_freq",
    label: "主动说话频率",
    hint: "她主动找你的频率；被叫到时回话不算。",
    levels: [
      { "limits.max_autonomous_per_hour": 2, "limits.max_llm_text_per_hour": 8,
        "limits.max_llm_plan_per_hour": 4 },
      { "limits.max_autonomous_per_hour": 4, "limits.max_llm_text_per_hour": 14,
        "limits.max_llm_plan_per_hour": 7 },
      { "limits.max_autonomous_per_hour": 6, "limits.max_llm_text_per_hour": 20,
        "limits.max_llm_plan_per_hour": 10 },
      { "limits.max_autonomous_per_hour": 10, "limits.max_llm_text_per_hour": 30,
        "limits.max_llm_plan_per_hour": 16 },
      { "limits.max_autonomous_per_hour": 16, "limits.max_llm_text_per_hour": 45,
        "limits.max_llm_plan_per_hour": 24 },
    ],
  },
  {
    id: "group_lively",
    label: "群聊活跃度",
    hint: "群里热闹时，她插话的意愿。",
    levels: [
      { "decider.interject_threshold": 0.75, "decider.interject_cooldown_minutes": 40,
        "decider.min_messages_to_interject": 4, "limits.max_share_per_hour": 1 },
      { "decider.interject_threshold": 0.68, "decider.interject_cooldown_minutes": 30,
        "decider.min_messages_to_interject": 3, "limits.max_share_per_hour": 2 },
      { "decider.interject_threshold": 0.6, "decider.interject_cooldown_minutes": 20,
        "decider.min_messages_to_interject": 2, "limits.max_share_per_hour": 4 },
      { "decider.interject_threshold": 0.5, "decider.interject_cooldown_minutes": 12,
        "decider.min_messages_to_interject": 2, "limits.max_share_per_hour": 6 },
      { "decider.interject_threshold": 0.4, "decider.interject_cooldown_minutes": 6,
        "decider.min_messages_to_interject": 1, "limits.max_share_per_hour": 10 },
    ],
  },
  {
    id: "clingy",
    label: "私聊粘人程度",
    hint: "她多久会想你、主动来找你。",
    levels: [
      { "profile.miss_growth_per_min": 0.0005, "profile.miss_threshold": 0.8,
        "profile.miss_push_daily_max": 1, "profile.miss_cooldown_min_minutes": 40,
        "profile.miss_cooldown_max_minutes": 120, "profile.miss_push_threshold": 0.9,
        "profile.miss_loneliness_weight": 0.4,
        "state_dynamics.desire_growth_per_min": 0.00012,
        "state_dynamics.desire_push_threshold": 0.9 },
      { "profile.miss_growth_per_min": 0.0008, "profile.miss_threshold": 0.7,
        "profile.miss_push_daily_max": 1, "profile.miss_cooldown_min_minutes": 25,
        "profile.miss_cooldown_max_minutes": 90, "profile.miss_push_threshold": 0.8,
        "profile.miss_loneliness_weight": 0.6,
        "state_dynamics.desire_growth_per_min": 0.00018,
        "state_dynamics.desire_push_threshold": 0.85 },
      { "profile.miss_growth_per_min": 0.0012, "profile.miss_threshold": 0.6,
        "profile.miss_push_daily_max": 2, "profile.miss_cooldown_min_minutes": 15,
        "profile.miss_cooldown_max_minutes": 60, "profile.miss_push_threshold": 0.70,
        "profile.miss_loneliness_weight": 0.8,
        "state_dynamics.desire_growth_per_min": 0.000231,
        "state_dynamics.desire_push_threshold": 0.75 },
      { "profile.miss_growth_per_min": 0.0018, "profile.miss_threshold": 0.5,
        "profile.miss_push_daily_max": 3, "profile.miss_cooldown_min_minutes": 10,
        "profile.miss_cooldown_max_minutes": 45, "profile.miss_push_threshold": 0.6,
        "profile.miss_loneliness_weight": 1.0,
        "state_dynamics.desire_growth_per_min": 0.0003,
        "state_dynamics.desire_push_threshold": 0.65 },
      { "profile.miss_growth_per_min": 0.0026, "profile.miss_threshold": 0.4,
        "profile.miss_push_daily_max": 5, "profile.miss_cooldown_min_minutes": 5,
        "profile.miss_cooldown_max_minutes": 30, "profile.miss_push_threshold": 0.5,
        "profile.miss_loneliness_weight": 1.2,
        "state_dynamics.desire_growth_per_min": 0.0004,
        "state_dynamics.desire_push_threshold": 0.55 },
    ],
  },
  {
    id: "verbosity",
    label: "一次说几句",
    hint: "一条回复最多拆成几句。",
    levels: [
      { "limits.max_messages_per_say": 1, "reply_style.dense_max_lines": 2,
        "reply_style.dense_window_minutes": 15 },
      { "limits.max_messages_per_say": 2, "reply_style.dense_max_lines": 3,
        "reply_style.dense_window_minutes": 12 },
      { "limits.max_messages_per_say": 3, "reply_style.dense_max_lines": 4,
        "reply_style.dense_window_minutes": 10 },
      { "limits.max_messages_per_say": 5, "reply_style.dense_max_lines": 6,
        "reply_style.dense_window_minutes": 8 },
      { "limits.max_messages_per_say": 6, "reply_style.dense_max_lines": 8,
        "reply_style.dense_window_minutes": 5 },
    ],
  },
  {
    id: "mood_swing",
    label: "情绪起伏",
    hint: "被逗乐或被惹到的难易，以及情绪回得快不快。",
    levels: [
      { "state_dynamics.chat_valence_cap": 0.02, "state_dynamics.chat_valence_daily_cap": 0.06,
        "state_dynamics.valence_decay_per_min": 0.022 },
      { "state_dynamics.chat_valence_cap": 0.035, "state_dynamics.chat_valence_daily_cap": 0.1,
        "state_dynamics.valence_decay_per_min": 0.018 },
      { "state_dynamics.chat_valence_cap": 0.05, "state_dynamics.chat_valence_daily_cap": 0.15,
        "state_dynamics.valence_decay_per_min": 0.014 },
      { "state_dynamics.chat_valence_cap": 0.08, "state_dynamics.chat_valence_daily_cap": 0.25,
        "state_dynamics.valence_decay_per_min": 0.010 },
      { "state_dynamics.chat_valence_cap": 0.12, "state_dynamics.chat_valence_daily_cap": 0.4,
        "state_dynamics.valence_decay_per_min": 0.006 },
    ],
  },
  {
    id: "life_rich",
    label: "生活丰富度",
    hint: "她独处时身边发生小事的频率；越高越热闹，也越费模型。",
    levels: [
      { "events.micro_per_hour": 0.3, "events.small_per_hour": 0.1,
        "events.big_per_hour": 0.01, "events.min_gap_minutes": 120 },
      { "events.micro_per_hour": 0.6, "events.small_per_hour": 0.2,
        "events.big_per_hour": 0.015, "events.min_gap_minutes": 60 },
      { "events.micro_per_hour": 1, "events.small_per_hour": 0.3,
        "events.big_per_hour": 0.028, "events.min_gap_minutes": 30 },
      { "events.micro_per_hour": 2, "events.small_per_hour": 0.6,
        "events.big_per_hour": 0.04, "events.min_gap_minutes": 15 },
      { "events.micro_per_hour": 4, "events.small_per_hour": 1.2,
        "events.big_per_hour": 0.08, "events.min_gap_minutes": 5 },
    ],
  },
  {
    id: "whimsy",
    label: "任性度",
    hint: "行为交给模型自由发挥的比例；越高越常临时起意。",
    levels: [
      { "decider.llm_rate_min": 0.01, "decider.llm_rate_max": 0.15,
        "state_dynamics.mood_override_duration": 180, "events.photo_tendency": 0.1,
        "events.genre_cooldown_minutes": 480 },
      { "decider.llm_rate_min": 0.03, "decider.llm_rate_max": 0.28,
        "state_dynamics.mood_override_duration": 360, "events.photo_tendency": 0.2,
        "events.genre_cooldown_minutes": 300 },
      { "decider.llm_rate_min": 0.05, "decider.llm_rate_max": 0.4,
        "state_dynamics.mood_override_duration": 600, "events.photo_tendency": 0.3,
        "events.genre_cooldown_minutes": 180 },
      { "decider.llm_rate_min": 0.09, "decider.llm_rate_max": 0.55,
        "state_dynamics.mood_override_duration": 900, "events.photo_tendency": 0.45,
        "events.genre_cooldown_minutes": 90 },
      { "decider.llm_rate_min": 0.15, "decider.llm_rate_max": 0.75,
        "state_dynamics.mood_override_duration": 1500, "events.photo_tendency": 0.6,
        "events.genre_cooldown_minutes": 30 },
    ],
  },
  {
    id: "memory_diligence",
    label: "记忆勤奋度",
    hint: "记忆和画像记多细；越高越完整，也越费模型。",
    levels: [
      { "profile.digest_chars": 40, "profile.digest_limit": 3,
        "context.chat_compress_threshold": 400, "context.summary_refresh_minutes": 30,
        "context.chat_answered_lines": 25 },
      { "profile.digest_chars": 50, "profile.digest_limit": 4,
        "context.chat_compress_threshold": 300, "context.summary_refresh_minutes": 20,
        "context.chat_answered_lines": 32 },
      { "profile.digest_chars": 60, "profile.digest_limit": 5,
        "context.chat_compress_threshold": 200, "context.summary_refresh_minutes": 10,
        "context.chat_answered_lines": 40 },
      { "profile.digest_chars": 80, "profile.digest_limit": 7,
        "context.chat_compress_threshold": 100, "context.summary_refresh_minutes": 6,
        "context.chat_answered_lines": 50 },
      { "profile.digest_chars": 100, "profile.digest_limit": 10,
        "context.chat_compress_threshold": 60, "context.summary_refresh_minutes": 3,
        "context.chat_answered_lines": 60 },
    ],
  },
  {
    id: "self_disclosure",
    label: "分享自己的事",
    hint: "谈资开着时，她多愿意讲自己的经历。",
    levels: [
      { "topics.recall_limit": 0, "topics.proactive_limit": 0,
        "topics.proactive_min_importance": 0.8 },
      { "topics.recall_limit": 1, "topics.proactive_limit": 1,
        "topics.proactive_min_importance": 0.65 },
      { "topics.recall_limit": 3, "topics.proactive_limit": 1,
        "topics.proactive_min_importance": 0.5 },
      { "topics.recall_limit": 5, "topics.proactive_limit": 2,
        "topics.proactive_min_importance": 0.35 },
      { "topics.recall_limit": 8, "topics.proactive_limit": 3,
        "topics.proactive_min_importance": 0.2 },
    ],
  },
];

/** 每个滑块 5 档的人话名字：滑块上只显示这个，不再显示“标准（3/5）”。 */
const KNOB_LEVEL_LABELS = {
  chat_freq: ["很少", "偶尔", "正常", "常主动", "很主动"],
  group_lively: ["安静", "少插话", "正常", "爱接话", "很活跃"],
  clingy: ["独立", "偶尔想", "正常", "黏人", "很黏"],
  verbosity: ["一句", "两句", "三句", "一小段", "长一点"],
  mood_swing: ["平稳", "稳", "正常", "敏感", "起伏大"],
  life_rich: ["安静", "偶尔", "正常", "热闹", "很热闹"],
  whimsy: ["规律", "偶尔", "正常", "随性", "很任性"],
  memory_diligence: ["少记", "简单", "正常", "细致", "很细"],
  self_disclosure: ["不说", "偶尔", "正常", "常分享", "很爱讲"],
};

/** 第一屏只放这 4 个；其余收进“更多”。 */
const KNOB_CORE_IDS = ["chat_freq", "group_lively", "clingy", "verbosity"];

/** 一键预设只改核心滑块，不碰任何功能开关。 */
const KNOB_PRESETS = [
  {
    id: "quiet",
    label: "安静",
    desc: "少主动、少插话、短句",
    levels: { chat_freq: 2, group_lively: 2, verbosity: 2, clingy: 2 },
  },
  {
    id: "standard",
    label: "标准",
    desc: "默认节奏",
    levels: { chat_freq: 3, group_lively: 3, verbosity: 3, clingy: 3 },
  },
  {
    id: "lively",
    label: "热闹",
    desc: "常开口、爱接话、长一点",
    levels: { chat_freq: 4, group_lively: 4, verbosity: 4, clingy: 3 },
  },
  {
    id: "clingy",
    label: "黏人",
    desc: "更常想你、更常找你",
    levels: { chat_freq: 4, group_lively: 3, verbosity: 3, clingy: 5 },
  },
];

/** 一个滑块当前档位的人话名字。 */
function knobLevelName(knob, level) {
  const labels = KNOB_LEVEL_LABELS[knob.id];
  const safe = Math.min(5, Math.max(1, Number(level) || 3));
  return labels ? labels[safe - 1] : KNOB_LEVEL_NAMES[safe - 1];
}

/** 这个预设是否正好等于当前核心档位。 */
function knobPresetMatches(world, preset) {
  return Object.keys(preset.levels).every((id) => {
    const knob = KNOBS.find((item) => item.id === id);
    return knob && knobLevel(world, knob) === preset.levels[id];
  });
}

/** 应用预设：只写它管的滑块，不改任何功能开关。 */
function applyKnobPreset(world, preset) {
  Object.keys(preset.levels).forEach((id) => {
    const knob = KNOBS.find((item) => item.id === id);
    if (!knob) return;
    world.ui_knobs = world.ui_knobs || {};
    world.ui_knobs[id] = preset.levels[id];
    applyKnobLevel(world, knob, preset.levels[id]);
  });
  markDirty();
}

/** 设置页摘要卡用的一句话。 */
function handfeelSummary(world) {
  const core = KNOB_CORE_IDS.map((id) => KNOBS.find((knob) => knob.id === id)).filter(Boolean);
  const standard = core.every((knob) => knobLevel(world, knob) === 3 && !knobTouched(world, knob));
  if (standard) return "当前：标准（推荐）";
  const short = { chat_freq: "主动", group_lively: "群聊", clingy: "粘人", verbosity: "句数" };
  return `当前：${core
    .map((knob) => `${short[knob.id] || knob.label} ${knobLevelName(knob, knobLevel(world, knob))}`)
    .join(" · ")}`;
}

function getPath(obj, path) {
  return String(path)
    .split(".")
    .reduce((node, key) => (node == null ? undefined : node[key]), obj);
}

function setPath(obj, path, value) {
  const keys = String(path).split(".");
  let node = obj;
  for (let i = 0; i < keys.length - 1; i += 1) {
    if (node[keys[i]] == null || typeof node[keys[i]] !== "object") node[keys[i]] = {};
    node = node[keys[i]];
  }
  node[keys[keys.length - 1]] = value;
}

/** 这一档写在配置里的位置（1~5）；没记过就是标准档。 */
function knobLevel(world, knob) {
  const stored = Number(((world.ui_knobs || {})[knob.id] || 3));
  return Math.min(5, Math.max(1, Number.isFinite(stored) ? stored : 3));
}

/** 用户是不是在这一档上手动改过字段（改过就把滑块旁标一句，别让人以为是滑块没生效）。 */
function topicSettings(world) {
  const topics = world.topics || {};
  const defaults = { recall_limit: 3, proactive_limit: 1, proactive_min_importance: 0.5, share_purpose_weight: 1, experience_willingness: 0.5, expression_length: 120 };
  const soft = {share_purpose_weight: ["chat_freq", [0.25,0.6,1,1.4,1.8]], experience_willingness: ["self_disclosure", [0,0.25,0.5,0.75,1]], expression_length: ["verbosity", [40,80,120,180,240]]};
  const overrides = topics.overrides || {};
  const knob = KNOBS.find((item) => item.id === "self_disclosure");
  const hasLevel = Number.isInteger((world.ui_knobs || {}).self_disclosure);
  const mapped = knob.levels[knobLevel(world, knob) - 1];
  const values = {}, sources = {};
  Object.keys(defaults).forEach((name) => {
    const item = overrides[name] || (topics.settings_version !== 1 && name in topics
      ? { mode: "custom", value: topics[name] } : { mode: "auto" });
    let value = num(topics[name], defaults[name]);
    let source = "base";
    if (soft[name]) {
      const [id, levels] = soft[name];
      const level = (world.ui_knobs || {})[id];
      if (Number.isInteger(level) && level >= 1 && level <= 5) { value = levels[level - 1]; source = "handfeel"; }
    } else if (hasLevel) { value = mapped[`topics.${name}`]; source = "handfeel"; }
    if (item.mode === "custom") { value = num(item.value, defaults[name]); source = "custom"; }
    value = ["proactive_min_importance", "experience_willingness"].includes(name) ? Math.min(1, Math.max(0, value)) : name === "share_purpose_weight" ? Math.max(0, value) : Math.max(0, Math.trunc(value));
    if (topics.enabled === false && name !== "proactive_min_importance") { value = 0; source = "hard_gate"; }
    values[name] = value; sources[name] = source;
  });
  return { values, sources };
}

function topicOverride(world, name, mode, value) {
  world.topics = world.topics || {};
  const topics = world.topics;
  // Preserve every legacy explicit field before marking the configuration migrated.
  topics.overrides = topics.overrides || {};
  ["recall_limit", "proactive_limit", "proactive_min_importance", "share_purpose_weight", "experience_willingness", "expression_length"].forEach((key) => {
    if (!topics.overrides[key]) topics.overrides[key] = topics.settings_version !== 1 && key in topics
      ? { mode: "custom", value: topics[key] } : { mode: "auto" };
  });
  // 新格式的版本号是 2（老格式才是 1）：1 表示字段是裸值、没有 overrides；
  // 写 2 之后后端与前端都按 overrides 说话，裸字段只剩"迁移前先保住"的作用。
  topics.settings_version = 2;
  topics.overrides[name] = mode === "custom" ? { mode, value: Number(value) } : { mode: "auto" };
}

function topicSettingControl(world, name, label) {
  const box = el("div", "field");
  const effective = topicSettings(world);
  const item = (world.topics.overrides || {})[name] || { mode: "auto" };
  const sourceText = { base: "基础配置", handfeel: "预设自动", custom: "高级自定义", hard_gate: "功能关闭硬限制" };
  const note = el("p", "muted", "");
  function draw() {
    const current = topicSettings(world);
    note.textContent = `实际有效：${current.values[name]} · 来源：${sourceText[current.sources[name]] || current.sources[name]}`;
  }
  box.appendChild(inputField(label, item.mode === "custom" ? item.value : effective.values[name], (value) => {
    topicOverride(world, name, "custom", value); markDirty(); draw();
  }, { type: "number", min: 0, max: ["proactive_min_importance", "experience_willingness"].includes(name) ? 1 : undefined,
       step: ["proactive_min_importance", "experience_willingness", "share_purpose_weight"].includes(name) ? "0.05" : "1", hint: "手动输入即保存为 custom，即使数值与默认相同也保留来源。" }));
  const reset = el("button", "ghost small", "恢复自动");
  reset.type = "button";
  reset.addEventListener("click", () => {
    topicOverride(world, name, "auto"); markDirty();
    const input = box.querySelector("input");
    if (input) input.value = String(topicSettings(world).values[name]);
    draw();
  });
  box.appendChild(reset); box.appendChild(note); draw();
  return box;
}

function knobTouched(world, knob) {
  if (knob.id === "self_disclosure") return Object.values(topicSettings(world).sources).includes("custom");
  const values = knob.levels[knobLevel(world, knob) - 1];
  return Object.keys(values).some((path) => Number(getPath(world, path)) !== Number(values[path]));
}

function applyKnobLevel(world, knob, level) {
  if (knob.id === "self_disclosure") {
    world.ui_knobs = world.ui_knobs || {};
    world.ui_knobs.self_disclosure = Math.min(5, Math.max(1, level));
    return;
  }
  const values = knob.levels[Math.min(5, Math.max(1, level)) - 1];
  Object.keys(values).forEach((path) => setPath(world, path, values[path]));
}

/**
 * 配置字段的中文人话。
 *
 * 说明文字只有后端一份（core/models.py 的 FIELD_LABELS，跟着 /defaults 一起下发），
 * 前端不另抄一张表；取不到就退回字段路径，至少不会显示成空的。
 */
function fieldLabel(path) {
  const map = (ui.defaults && ui.defaults.field_labels) || {};
  return map[path] || path;
}

/** 「性格与习惯」滑块。向导的「她的性格」那一步也直接嵌这个（compact 模式）。 */
function knobEditor(world, onChange, options = {}) {
  world.ui_knobs = world.ui_knobs || {};
  const compact = options.compact === true;
  const showDetails = options.showDetails !== false;
  const showTouched = options.showTouched !== false;
  const box = el("div", "full knob-box");

  function renderRow(knob) {
    const row = el("div", "knob-row");
    const head = el("div", "knob-head");
    head.appendChild(el("span", "knob-label", knob.label));
    const levelText = el("span", "knob-level", "");
    head.appendChild(levelText);
    row.appendChild(head);

    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "1";
    slider.max = "5";
    slider.step = "1";
    slider.value = String(knobLevel(world, knob));
    slider.className = "knob-slider";
    row.appendChild(slider);

    const scale = el("div", "knob-scale");
    scale.appendChild(el("span", "", knobLevelName(knob, 1)));
    scale.appendChild(el("span", "", knobLevelName(knob, 3)));
    scale.appendChild(el("span", "", knobLevelName(knob, 5)));
    row.appendChild(scale);

    if (knob.hint) row.appendChild(el("p", "muted knob-hint", knob.hint));

    let body = null;
    let custom = null;
    let reset = null;
    if (showDetails) {
      const detail = document.createElement("details");
      detail.className = "knob-detail";
      const summary = document.createElement("summary");
      summary.appendChild(el("span", "", "高级：影响哪些设置"));
      detail.appendChild(summary);
      custom = el("div", "knob-custom");
      custom.appendChild(el("span", "muted", "这项有几个参数被单独改过。"));
      reset = el("button", "knob-reset", "恢复标准");
      reset.type = "button";
      reset.title = "把这一项恢复成标准档";
      reset.addEventListener("click", () => {
        world.ui_knobs[knob.id] = 3;
        applyKnobLevel(world, knob, 3);
        markDirty();
        draw();
        if (typeof onChange === "function") onChange();
      });
      custom.appendChild(reset);
      detail.appendChild(custom);
      body = el("div", "knob-detail-body");
      detail.appendChild(body);
      row.appendChild(detail);
    }

    function draw() {
      const level = knobLevel(world, knob);
      const touchedNow = knobTouched(world, knob);
      slider.value = String(level);
      levelText.textContent = knobLevelName(knob, level);
      if (custom) custom.style.display = showTouched && touchedNow ? "" : "none";
      if (!body) return;
      const values = knob.levels[level - 1];
      body.innerHTML = "";
      Object.keys(values).forEach((path) => {
        const now = Number(
          knob.id === "self_disclosure"
            ? topicSettings(world).values[path.split(".")[1]]
            : getPath(world, path),
        );
        const want = Number(values[path]);
        const changed = now !== want;
        const item = el("div", "knob-detail-row");
        const nameCell = el("div", "knob-detail-name");
        nameCell.appendChild(el("span", "", fieldLabel(path)));
        item.appendChild(nameCell);
        item.appendChild(el("span", "", `${want}`));
        if (changed) item.appendChild(el("span", "muted", `现在是 ${now}`));
        body.appendChild(item);
      });
    }

    slider.addEventListener("input", () => {
      world.ui_knobs[knob.id] = Number(slider.value);
      applyKnobLevel(world, knob, Number(slider.value));
      markDirty();
      draw();
      if (typeof onChange === "function") onChange();
    });
    draw();
    return row;
  }

  KNOBS.filter((knob) => !compact || KNOB_CORE_IDS.includes(knob.id)).forEach((knob) => {
    box.appendChild(renderRow(knob));
  });

  if (compact) {
    const rest = KNOBS.filter((knob) => !KNOB_CORE_IDS.includes(knob.id));
    if (rest.length) {
      const more = document.createElement("details");
      more.className = "knob-more";
      const summary = document.createElement("summary");
      summary.appendChild(el("span", "", `更多（${rest.length} 项）`));
      more.appendChild(summary);
      const body = el("div", "knob-more-body");
      rest.forEach((knob) => body.appendChild(renderRow(knob)));
      more.appendChild(body);
      box.appendChild(more);
    }
  }
  return box;
}

/** 设置页里的摘要文字：面板里调完立刻能看见。 */
function updateHandfeelSummary() {
  const node = ui.handfeelSummaryNode;
  if (node && node.isConnected) node.textContent = handfeelSummary(ui.config.world);
}

/** 「性格与习惯」面板：预设 + 4 个核心滑块 + 更多 5 项。 */
//: 「恢复默认」能勾的那几块（用户 m08826）：只重置勾中的，人设/记忆/谈资默认不勾。
const RESET_SECTIONS = [
  {
    key: "basic",
    label: "全局基础值",
    desc: "名额、上下文、媒体这些数字（跟这一版的默认值走）",
    on: true,
  },
  {
    key: "actions",
    label: "动作库",
    desc: "动作的说明与参数回到内置版本（她自己整理过的会被覆盖）",
    on: true,
  },
  {
    key: "agent",
    label: "Agent 预算",
    desc: "步数、超时、审批档位、模型槽位",
    on: true,
  },
  { key: "topics", label: "谈资设置", desc: "谈资系统的开关与名额（素材不删）", on: false },
  { key: "meme", label: "表情包设置", desc: "额度与判定（收藏的图不删）", on: false },
  { key: "persona", label: "人设", desc: "名字、性别、角色卡", on: false },
  {
    key: "memory",
    label: "记忆与关系",
    desc: "人设记忆、人物档案、好感度（清了就找不回来）",
    on: false,
  },
];

async function openManualEditor() {
  let text = "";
  try {
    const out = await apiGet("manual");
    text = String((out && out.text) || "");
  } catch (error) {
    toast(`读不出说明书：${error && error.message ? error.message : error}`);
    return;
  }
  openFormDialog({
    title: "插件说明书（给她看的）",
    hint: "介绍这个插件：她跑在什么里、有哪些动作和本事、边界在哪。原样进提示词，保存后下一轮生效。",
    wide: true,
    fields: [{ key: "text", label: "说明书（Markdown）", type: "textarea", rows: 20 }],
    values: { text },
    confirmText: "保存",
    onSubmit: async (values) => {
      const result = await apiPost("manual/save", { text: String(values.text || "") });
      if (result && result.ok) {
        toast(`保存了（${result.chars || 0} 字）`);
        return true;
      }
      toast(`没保存：${(result && result.reason) || "不知道原因"}`);
      return false;
    },
  });
}

function openResetDefaults() {
  const backdrop = el("div", "wizard-backdrop handfeel-backdrop");
  const card = el("div", "wizard-card handfeel-card");
  const head = el("div", "wizard-head");
  head.appendChild(el("div", "wizard-title", "恢复默认"));
  const close = el("button", "icon-btn", "✕");
  close.type = "button";
  close.title = "关闭";
  close.addEventListener("click", () => backdrop.remove());
  head.appendChild(close);
  card.appendChild(head);
  card.appendChild(
    el(
      "p",
      "muted wizard-hint",
      "只把勾中的那几块变回这一版的默认值，没勾的一律不动。新版改过的动作说明、基础值也在里面——老配置享受不到的，在这里补回来。",
    ),
  );
  const body = el("div", "wizard-body");
  const picked = new Set();
  RESET_SECTIONS.forEach((item) => {
    const row = el("label", "reset-row");
    const box = el("input");
    box.type = "checkbox";
    box.checked = item.on === true;
    if (box.checked) picked.add(item.key);
    box.addEventListener("change", () => {
      if (box.checked) picked.add(item.key);
      else picked.delete(item.key);
      refresh();
    });
    const text = el("span", "reset-text");
    text.appendChild(el("strong", "", item.label));
    text.appendChild(el("span", "muted", item.desc));
    row.appendChild(box);
    row.appendChild(text);
    body.appendChild(row);
  });
  card.appendChild(body);
  const footer = el("div", "wizard-foot");
  const cancel = el("button", "ghost", "取消");
  cancel.type = "button";
  cancel.addEventListener("click", () => backdrop.remove());
  const go = el("button", "primary", "恢复这几块");
  go.type = "button";
  function refresh() {
    go.disabled = picked.size === 0;
    go.textContent = picked.size ? `恢复选中的 ${picked.size} 块` : "至少勾一块";
  }
  go.addEventListener("click", async () => {
    const sections = [...picked];
    go.disabled = true;
    go.textContent = "正在恢复…";
    try {
      await apiPost("config/reset", { sections });
      toast("已经恢复；页面重新读一遍配置");
      backdrop.remove();
      window.location.reload();
    } catch (err) {
      toast(`恢复失败：${err.message || err}`);
      refresh();
    }
  });
  footer.appendChild(cancel);
  footer.appendChild(go);
  card.appendChild(footer);
  refresh();
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);
}

function openHandfeel() {
  const world = ui.config.world;
  const backdrop = el("div", "wizard-backdrop handfeel-backdrop");
  const card = el("div", "wizard-card handfeel-card");
  const head = el("div", "wizard-head");
  head.appendChild(el("div", "wizard-title", "性格与习惯"));
  const close = el("button", "icon-btn", "✕");
  close.type = "button";
  close.title = "关闭";
  close.addEventListener("click", () => backdrop.remove());
  head.appendChild(close);
  card.appendChild(head);
  card.appendChild(
    el(
      "p",
      "muted wizard-hint",
      "先选整体感觉；想细调再拖下面的滑块。改完点「完成」，再点页面右上角「保存」才会生效。",
    ),
  );

  const presetRow = el("div", "wizard-preset-grid");
  const body = el("div", "wizard-body handfeel-body");

  function drawPresets() {
    presetRow.innerHTML = "";
    KNOB_PRESETS.forEach((preset) => {
      const button = el(
        "button",
        `wizard-preset${knobPresetMatches(world, preset) ? " on" : ""}`,
      );
      button.type = "button";
      button.appendChild(el("span", "wizard-preset-title", preset.label));
      button.appendChild(el("span", "wizard-preset-desc", preset.desc));
      button.addEventListener("click", () => {
        applyKnobPreset(world, preset);
        drawPresets();
        drawBody();
        updateHandfeelSummary();
      });
      presetRow.appendChild(button);
    });
  }

  function drawBody() {
    body.innerHTML = "";
    body.appendChild(
      knobEditor(
        world,
        () => {
          drawPresets();
          updateHandfeelSummary();
        },
        { compact: true, showDetails: true },
      ),
    );
  }

  card.appendChild(presetRow);
  card.appendChild(body);

  const foot = el("div", "wizard-foot");
  const done = el("button", "primary", "完成");
  done.type = "button";
  done.addEventListener("click", () => backdrop.remove());
  foot.appendChild(done);
  card.appendChild(foot);
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);

  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) backdrop.remove();
  });
  const onKey = (event) => {
    if (event.key === "Escape") {
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
    }
  };
  document.addEventListener("keydown", onKey);

  drawPresets();
  drawBody();
  updateHandfeelSummary();
}

/** 设置页摘要卡里的当前值 + 「调整」按钮。 */
function handfeelCard(world) {
  const box = el("div", "full handfeel-card-summary");
  const summary = el("p", "knob-summary", handfeelSummary(world));
  ui.handfeelSummaryNode = summary;
  box.appendChild(summary);
  const row = el("div", "row");
  const button = el("button", "ghost small", "调整…");
  button.type = "button";
  button.title = "打开完整面板，细调说话频率、群聊活跃度、粘人程度等";
  button.addEventListener("click", () => openHandfeel());
  row.appendChild(button);
  box.appendChild(row);
  return box;
}

/**
 * 可折叠的编辑块：平时收成一行摘要，点开才铺开改。
 *
 * 「关系表 / 亲密度分级」一屏放不下，又一年改不了几次——不该让它们把整页撑成一堵墙。
 * 标题仍然走 ``fieldHead``，所以设置搜索照样搜得到。
 */
function foldEditor(title, hint) {
  const box = el("details", "full fold-editor");
  const summary = document.createElement("summary");
  summary.appendChild(fieldHead(title, hint));
  const note = el("span", "fold-note muted", "");
  summary.appendChild(note);
  box.appendChild(summary);
  box._note = note;
  return box;
}

/** 关系表：一行一种关系（槽位 / 亲密度区间 / 别名）。 */
function bondsEditor(world) {
  world.profile = world.profile || {};
  if (!Array.isArray(world.profile.bonds)) world.profile.bonds = [];
  const box = foldEditor("关系表", "一行一种关系：槽位、亲密度区间、别名都在这儿改。");
  box.appendChild(
    el(
      "p",
      "muted",
      "每行一种关系。槽位相同的算一类（例如 romance 里男友和女友不能并存）。"
        + "「最低 / 最高档」是这个关系对应的亲密度区间：绑定男友之后，"
        + "哪怕好感还没养起来也能抱抱，而群友聊再久也上不去——生效档位 = 好感给的档位，"
        + "先被最低档抬起、再被最高档压下；绑上的那一刻好感也会被抬到最低档的下边界"
        + "（已经更高的不动）。「别名」用逗号分隔。",
    ),
  );
  const levelOptions = (world.profile.levels || [])
    .map((item, index) => ({ value: String(index), label: `${index} · ${item.name || "未命名"}` }));

  // 新认识的人默认挂哪一条关系：只认一个来源（profile.default_bond），
  // 跟"第一次见到他先挂什么"用的是同一个字段。
  const defaultRow = el("div", "list-row");
  defaultRow.appendChild(el("span", "muted", "新认识的人默认是"));
  const defaultSelect = document.createElement("select");
  (world.profile.bonds || []).forEach((item) => {
    defaultSelect.appendChild(option(String(item.name || ""), String(item.name || "（无名）")));
  });
  const fallbackName = String((world.profile.bonds[0] || {}).name || "");
  defaultSelect.value = String(world.profile.default_bond || fallbackName);
  defaultSelect.title = "第一次见到一个人时先给他这条关系；只能有一条";
  defaultSelect.addEventListener("change", () => {
    world.profile.default_bond = defaultSelect.value;
    // 亲手选过就记一笔：默认值以后升级也不再改这一项（用户 m11008）
    world.profile.bond_chosen = true;
    markDirty();
  });
  defaultRow.appendChild(defaultSelect);
  box.appendChild(defaultRow);
  const rows = el("div", "list-editor-rows");
  box.appendChild(rows);

  function redraw() {
    rows.innerHTML = "";
    box._note.textContent = world.profile.bonds.length
      ? `${world.profile.bonds.length} 种 · 点开编辑`
      : "还没有关系";
    world.profile.bonds.forEach((item, index) => {
      const row = el("div", "list-row");
      row.appendChild(
        cellInput(item.name || "", (value) => (item.name = value), {
          placeholder: "名称",
          width: 72,
        }),
      );
      row.appendChild(
        cellInput(item.slot || "", (value) => (item.slot = value), {
          placeholder: "槽位",
          title: "同类关系靠槽位判重：槽位相同的只能有一个",
          width: 84,
        }),
      );
      row.appendChild(
        el("span", "muted", "最低"),
      );
      row.appendChild(
        cellSelect(item.floor ?? 0, levelOptions, (value) => (item.floor = num(value)), {
          title: "这个关系至少到哪一档（关系本身就是一种态度）",
          width: 112,
        }),
      );
      row.appendChild(
        el("span", "muted", "最高"),
      );
      row.appendChild(
        cellSelect(item.cap ?? 0, levelOptions, (value) => (item.cap = num(value)), {
          title: "这个关系最多到哪一档（普通关系聊再久也上不去）",
          width: 112,
        }),
      );
      row.appendChild(
        cellInput((item.aliases || []).join("，"), (value) => {
          item.aliases = value
            .split(/[，,]/)
            .map((one) => one.trim())
            .filter(Boolean);
        }, { placeholder: "别名，逗号分隔", width: 130 }),
      );
      row.appendChild(
        cellCheck("唯一", item.unique, (value) => (item.unique = value), "同类里全球只能有一个"),
      );
      row.appendChild(
        cellCheck("负面", item.negative, (value) => (item.negative = value), "负面关系：不惦记、不主动"),
      );
      const del = el("button", "icon-btn", "✕");
      del.type = "button";
      del.title = "删掉这种关系";
      del.addEventListener("click", () => {
        world.profile.bonds.splice(index, 1);
        markDirty();
        redraw();
      });
      row.appendChild(del);
      rows.appendChild(row);
    });
    const add = el("button", "small ghost", "＋ 加一种关系");
    add.type = "button";
    add.addEventListener("click", () => {
      world.profile.bonds.push({
        name: "新关系",
        slot: "",
        cap: 3,
        aliases: [],
      });
      markDirty();
      redraw();
    });
    rows.appendChild(add);
  }
  redraw();
  box.appendChild(
    el(
      "p",
      "hint-line",
      "关系表字段：name（关系名）/ slot（槽位，同槽互斥）/ floor 与 cap（这个关系的" +
        "亲密度区间，都填分级表的下标）/ group（同类关系，同一个人身上只留一条，例如群友 / " +
        "朋友 / 闺蜜 / 男友 / 女友 都是 close；留空 = 能和别的并存）/ unique（只能有一个）/ " +
        "negative（负面关系）/ aliases（别名）。默认初始关系在表头那个下拉里选，只有一条。",
    ),
  );
  return box;
}

/**
 * 亲密度分级：每一级的称呼、这一档还不能做的动作、主动频率，以及给模型的提示词。
 *
 * 每一档自己也是一行摘要（点开才铺开改）——七八档全摊开就是一整屏。
 */
function levelsEditor(world) {
  world.profile = world.profile || {};
  if (!Array.isArray(world.profile.levels)) world.profile.levels = [];
  const box = foldEditor("亲密度分级", "按好感度分档：称呼、这一档还不能做的动作、主动频率。");
  box.appendChild(
    el(
      "p",
      "muted",
      "按好感度分档：每档自己的称呼、这一档**还不能做**的动作、每天最多主动找几次，"
        + "以及这一档给模型的提示词。动作写 id，逗号分隔。点一条展开改这一档。",
    ),
  );
  const rows = el("div", "list-editor-rows");
  box.appendChild(rows);

  function redraw() {
    rows.innerHTML = "";
    box._note.textContent = world.profile.levels.length
      ? `${world.profile.levels.length} 档 · 点开编辑`
      : "还没有分档";
    world.profile.levels.forEach((item, index) => {
      const card = el("details", "list-card level-card");
      const head = el("summary", "level-head");
      const title = el("b", "level-name", "");
      const range = el("span", "muted", "");
      const address = el("span", "muted", "");
      const freq = el("span", "muted", "");
      head.appendChild(title);
      head.appendChild(range);
      head.appendChild(address);
      head.appendChild(freq);
      card.appendChild(head);
      const syncHead = () => {
        title.textContent = item.name || "未命名";
        range.textContent = `好感 ${levelNum(item.min_affinity)} ~ ${levelNum(item.max_affinity)}`;
        address.textContent = `称呼「${item.address || "不指定"}」`;
        freq.textContent = Number(item.proactive_per_day)
          ? `每天主动 ${levelNum(item.proactive_per_day)} 次`
          : "不主动";
      };

      const body = el("div", "level-body");
      const edit = el("div", "list-row");
      edit.appendChild(
        cellInput(item.name || "", (value) => {
          item.name = value;
          syncHead();
        }, {
          placeholder: "档位名",
          width: 72,
        }),
      );
      edit.appendChild(el("span", "muted", "好感"));
      edit.appendChild(
        cellInput(item.min_affinity ?? 0, (value) => {
          item.min_affinity = num(value);
          syncHead();
        }, {
          type: "number",
          min: -100,
          max: 100,
          width: 64,
        }),
      );
      edit.appendChild(el("span", "muted", "~"));
      edit.appendChild(
        cellInput(item.max_affinity ?? 0, (value) => {
          item.max_affinity = num(value);
          syncHead();
        }, {
          type: "number",
          min: -100,
          max: 100,
          width: 64,
        }),
      );
      edit.appendChild(el("span", "muted", "称呼"));
      edit.appendChild(
        cellInput(item.address || "", (value) => {
          item.address = value;
          syncHead();
        }, { width: 56 }),
      );
      edit.appendChild(el("span", "muted", "每天主动"));
      edit.appendChild(
        cellInput(item.proactive_per_day ?? 0, (value) => {
          item.proactive_per_day = num(value);
          syncHead();
        }, {
          type: "number",
          min: 0,
          max: 20,
          width: 52,
        }),
      );
      const del = el("button", "icon-btn", "✕");
      del.type = "button";
      del.title = "删掉这一档";
      del.addEventListener("click", () => {
        world.profile.levels.splice(index, 1);
        markDirty();
        redraw();
      });
      edit.appendChild(del);
      body.appendChild(edit);

      const promptArea = document.createElement("textarea");
      promptArea.rows = 2;
      promptArea.placeholder = "这一档给模型的提示词";
      promptArea.value = item.prompt || "";
      promptArea.addEventListener("change", () => {
        item.prompt = promptArea.value;
        markDirty();
      });
      body.appendChild(promptArea);

      const io = el("div", "list-row");
      io.appendChild(el("span", "muted", "这一档还不能做"));
      io.appendChild(
        cellInput((item.deny || []).join(","), (value) => {
          item.deny = splitIds(value);
        }, {
          placeholder: "动作 id，逗号分隔（留空 = 不限制）",
          width: 260,
          title: "写进提示词的「这一档还不能做」，她说得出但不会做——分寸由她自己克制",
        }),
      );
      body.appendChild(io);
      card.appendChild(body);
      rows.appendChild(card);
      syncHead();
    });
    const add = el("button", "small ghost", "＋ 加一档");
    add.type = "button";
    add.addEventListener("click", () => {
      world.profile.levels.push({
        name: "新档位",
        min_affinity: 0,
        max_affinity: 10,
        address: "你",
        deny: [],
        proactive_per_day: 0,
        prompt: "",
      });
      markDirty();
      redraw();
    });
    rows.appendChild(add);
  }
  redraw();
  box.appendChild(
    el(
      "p",
      "hint-line",
      "分级表字段：name / min_affinity / max_affinity（好感区间）/ address（称呼）/ " +
        "deny（这一档**还不能做**的动作 id，会写成「这一档还不能做」给她看；留空 = 不限制）/ " +
        "proactive_per_day（每天最多主动找他几次）/ prompt（这一级写给模型的提示词文本，可自行修改）。",
    ),
  );
  box.appendChild(
    el(
      "p",
      "hint-line",
      "生效亲密度 = 好感度达到的档位，先被关系的「最低档」抬起、再被「最高档」压下：" +
        "绑成男友之后哪怕好感还没养起来也能抱抱，而群友聊再久也上不去。改完点右上角「保存」。",
    ),
  );
  return box;
}

/** 摘要行里显示的好感数字：空值当 0，别让摘要出现 "undefined"。 */
function levelNum(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? String(parsed) : "0";
}

function splitIds(value) {
  return String(value || "")
    .split(/[，,\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function selectField(label, value, choices, onChange, opts = {}) {
  const wrapper = el("label");
  if (opts.adv) wrapper.dataset.adv = "1";
  wrapper.appendChild(fieldHead(label, opts.hint));
  const node = document.createElement("select");
  choices.forEach((choice) => {
    // 三种写法都认：字符串、[值, 文案] 数组、{ key, label } 对象。
    // 以前只判 typeof === "object"，数组会走进对象分支取到 undefined，
    // 结果下拉框看着是空的（「从这一档算熟人」就这么空过）。
    let choiceValue;
    let choiceLabel;
    if (Array.isArray(choice)) {
      [choiceValue, choiceLabel] = choice;
    } else if (choice && typeof choice === "object") {
      choiceValue = choice.key;
      choiceLabel = choice.label;
    } else {
      choiceValue = choice;
      choiceLabel = choice;
    }
    node.appendChild(option(choiceValue, choiceLabel));
  });
  node.value = value ?? "";
  node.addEventListener("change", () => onChange(node.value));
  wrapper.appendChild(node);
  return wrapper;
}

/**
 * 会真的动这台机器上文件的开关：开启前必须先看声明、再点一次确认。
 *
 * 依据（用户 2026-10-19）：`astrbot_file_write_tool` / `astrbot_file_edit_tool`
 * 能写任意路径，VW 没给它们加沙箱；「执行代码」更是直接在这台机器上跑。
 * 光在提示里写一句"有安全风险"没人当回事，所以做成**开启前的拦截**：
 * 弹窗里说清楚"别在个人电脑或重要服务器上开"，点了才真的打开。
 */
const AGENT_FILE_POWER_WARNING =
  "开启后她能真的改动这台机器上的文件（写、改、删，甚至执行代码），出的问题就直接落在你自己的东西上。" +
  "建议只在隔离环境里用（虚拟机、容器、专用测试机）；不要在个人电脑或重要的服务器上打开。";

function checkboxField(label, checked, onChange, opts = {}) {
  const wrapper = el("label", "inline");
  if (opts.adv) wrapper.dataset.adv = "1";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = !!checked;
  input.addEventListener("change", async () => {
    const next = input.checked;
    if (next && opts.confirm) {
      const ok = await confirmDialog({
        title: opts.confirmTitle || `确定要开启「${label}」吗？`,
        message: opts.confirm,
        confirmText: "我明白风险，开启",
        danger: true,
      });
      if (!ok) {
        // 没点确认就回到关闭：不能让她莫名其妙拿到这份权限
        input.checked = false;
        return;
      }
    }
    onChange(next);
  });
  wrapper.appendChild(input);
  wrapper.appendChild(el("span", "", label));
  const tip = tipBox(opts.hint);
  if (tip) wrapper.appendChild(tip);
  return wrapper;
}

/** 分钟滑块：一个数字的连续档位（比如"同一个群两次之间至少隔几分钟"）。
 *  注意：和下面的 `sliderField`（0~1 的亲密度滑块）不是一回事，别同名。 */
function minuteSliderField(label, value, onChange, opts = {}) {
  const wrapper = el("div", "field");
  if (opts.adv) wrapper.dataset.adv = "1";
  const min = opts.min ?? 0;
  const unit = opts.unit || "分钟";
  const head = el("div", "knob-head");
  head.appendChild(el("span", "knob-label", label));
  const readout = el("span", "knob-level", `${num(value, min)} ${unit}`);
  head.appendChild(readout);
  wrapper.appendChild(head);
  if (opts.hint) wrapper.appendChild(el("p", "muted knob-hint", opts.hint));
  const slider = document.createElement("input");
  slider.type = "range";
  slider.className = "knob-slider";
  slider.min = String(min);
  slider.max = String(opts.max ?? 240);
  slider.step = String(opts.step ?? 5);
  slider.value = String(num(value, min));
  slider.addEventListener("input", () => {
    readout.textContent = `${slider.value} ${unit}`;
  });
  slider.addEventListener("change", () => onChange(num(slider.value, min)));
  wrapper.appendChild(slider);
  return wrapper;
}

/** 单选按钮组（比下拉更直观）。 */
function pillsField(label, value, choices, onChange, opts = {}) {
  const wrapper = el("div", "field");
  if (opts.adv) wrapper.dataset.adv = "1";
  wrapper.appendChild(fieldHead(label, opts.hint));
  const row = el("div", "pill-row");
  choices.forEach((choice) => {
    const button = el("button", `pill${choice.key === value ? " on" : ""}`, choice.label);
    button.type = "button";
    if (choice.hint) {
      button.setAttribute("data-tip", choice.hint);
      button.setAttribute("title", choice.hint);
    }
    button.addEventListener("click", () => onChange(choice.key));
    row.appendChild(button);
  });
  wrapper.appendChild(row);
  return wrapper;
}

/** 「调试输出」：按用途分块列出，想发哪几类就勾哪几类。 */
function echoTypesField(world) {
  const wrapper = el("div", "field echo-field");
  wrapper.appendChild(
    fieldHead(
      "调试输出",
      "把群里本来看不见的事作为消息发出来，用来排查「她为什么这么做」。\n" +
        "只补上听不到的部分：她自己说过的话照常只发一次，不会重复。\n" +
        "一个都不勾 = 关闭调试输出。\n" +
        "这些消息不会被当成「她说的话」：不进聊天上下文、不计无人回应保护、不影响数值。",
    ),
  );

  /** 每个类型各自的显示方式：关（不在表里）/ 完整 full / 精简 compact。 */
  const modes = () => {
    const table =
      world.echo_modes && typeof world.echo_modes === "object" ? world.echo_modes : {};
    if (Object.keys(table).length) return table;
    // 老配置（勾选列表 + 全局精简）在页面里直接迁过来
    const migrated = {};
    (Array.isArray(world.echo_types) ? world.echo_types : []).forEach((name) => {
      migrated[name] = world.echo_compact ? "compact" : "full";
    });
    world.echo_modes = migrated;
    return migrated;
  };
  const chosen = () => Object.keys(modes());
  const setMode = (key, value) => {
    const table = { ...modes() };
    if (!value || value === "off") delete table[key];
    else table[key] = value;
    world.echo_modes = table;
  };
  const nextMode = (key) => {
    const now = modes()[key];
    if (now === "full") return "compact";
    if (now === "compact") return "off";
    return "full";
  };
  const MODE_LABELS = { full: "完整", compact: "精简" };

  /** 常用的一批：排查"她为什么这么做"最需要的几类。 */
  const COMMON_ECHO_TYPES = [
    "plan",
    "action_start",
    "action_done",
    "action",
    "search",
    "tool_call",
    "tool_result",
    "skip",
  ];

  const toolbar = el("div", "echo-toolbar");
  const counted = el(
    "span",
    "hint",
    `已选 ${chosen().length} / ${ECHO_TYPE_CHOICES.length} 类`,
  );
  const buttons = el("div", "row-item");
  const quick = [
    ["全选（完整）", () => setAll("full", true)],
    ["常用", () => setAll("full", false, COMMON_ECHO_TYPES)],
    ["清空", () => setAll("off")],
  ];
  function setAll(mode, everything, only = null) {
    const table = {};
    if (mode !== "off") {
      ECHO_TYPE_CHOICES.forEach((item) => {
        if (everything || (only || []).includes(item.key)) table[item.key] = mode;
      });
    }
    world.echo_modes = table;
  }
  quick.forEach(([text, run]) => {
    const button = el("button", "ghost", text);
    button.type = "button";
    button.addEventListener("click", () => {
      run();
      renderSettings();
    });
    buttons.appendChild(button);
  });
  toolbar.appendChild(counted);
  toolbar.appendChild(buttons);

  /** 每一类一个可点的小卡片：图标 + 名字，说明放在悬停提示里。 */
  const typeButton = (choice) => {
    const mode = modes()[choice.key] || "";
    const box = el("button", `echo-chip${mode ? " on" : ""}`);
    box.type = "button";
    box.appendChild(el("span", "echo-icon", choice.icon));
    box.appendChild(el("span", "", choice.label));
    if (mode) box.appendChild(el("span", "echo-mode", MODE_LABELS[mode] || mode));
    const tip = `${choice.hint || ""}\n点击切换：关 → 完整 → 精简。`;
    box.setAttribute("data-tip", tip.trim());
    box.setAttribute("title", tip.trim());
    box.addEventListener("click", () => {
      setMode(choice.key, nextMode(choice.key));
      markDirty();
      renderSettings();
    });
    return box;
  };

  const groups = el("div", "echo-groups");
  // 扩展注册的类型排在最后，一个扩展一组
  const allGroups = [...ECHO_GROUPS, ...EXT_ECHO_GROUPS];
  const allChoices = [...ECHO_TYPE_CHOICES, ...EXT_ECHO_CHOICES];
  allGroups.forEach((group) => {
    const items = allChoices.filter((item) => (item.group || "core") === group.key);
    if (!items.length) return;
    const block = el("div", "echo-group");
    const head = el("div", "echo-group-head");
    const title = el("span", "echo-group-title", group.label);
    if (group.hint) {
      title.setAttribute("data-tip", group.hint);
      title.setAttribute("title", group.hint);
    }
    head.appendChild(title);
    const table = modes();
    const onCount = items.filter((item) => table[item.key]).length;
    head.appendChild(el("span", "hint", `${onCount}/${items.length}`));
    const toggle = el("button", "ghost tiny", onCount === items.length ? "取消本组" : "全选本组");
    toggle.type = "button";
    toggle.addEventListener("click", () => {
      const keys = items.map((item) => item.key);
      const next = { ...modes() };
      keys.forEach((key) => {
        if (onCount === items.length) delete next[key];
        else next[key] = "full";
      });
      world.echo_modes = next;
      renderSettings();
    });
    head.appendChild(toggle);
    block.appendChild(head);
    const row = el("div", "echo-grid");
    items.forEach((item) => row.appendChild(typeButton(item)));
    block.appendChild(row);
    groups.appendChild(block);
  });

  wrapper.appendChild(toolbar);
  wrapper.appendChild(
    el(
      "p",
      "muted",
      "点一个类型切换显示方式：关 → 完整（带参数与结果）→ 精简（只留要点）。",
    ),
  );
  wrapper.appendChild(groups);
  return wrapper;
}

/** 自由文本的标签编辑：加一个删一个，不用手写逗号分隔。 */
function tagField(label, values, onChange, opts = {}) {
  const wrapper = el("div", "field");
  wrapper.appendChild(fieldHead(label, opts.hint));
  let current = Array.isArray(values) ? values.slice() : [];
  const list = el("div", "pill-row");

  const emit = () => onChange(current.slice());

  const paint = () => {
    list.innerHTML = "";
    current.forEach((word, index) => {
      const chip = el("span", "pill on");
      chip.appendChild(el("span", "", String(word)));
      const remove = el("button", "ghost", "×");
      remove.type = "button";
      remove.title = "删掉这个词";
      remove.style.padding = "0 4px";
      remove.addEventListener("click", () => {
        current = current.slice();
        current.splice(index, 1);
        emit();
        paint();
      });
      chip.appendChild(remove);
      list.appendChild(chip);
    });
    if (!current.length) {
      list.appendChild(el("span", "hint", opts.emptyText || "（还没有，下面加一个）"));
    }
  };

  const row = el("div", "row-item");
  const input = document.createElement("input");
  input.type = "text";
  input.className = "grow";
  input.placeholder = opts.placeholder || "输入内容后按回车";
  const add = () => {
    const word = input.value.trim();
    input.value = "";
    if (!word || current.includes(word)) return;
    current = current.concat([word]);
    emit();
    paint();
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      add();
    }
  });
  const button = el("button", "ghost", "添加");
  button.type = "button";
  button.addEventListener("click", add);
  row.appendChild(input);
  row.appendChild(button);

  wrapper.appendChild(list);
  wrapper.appendChild(row);
  paint();
  return wrapper;
}

/** 时长输入：数字 + 单位（内部统一按秒存储）。 */
function timeField(label, seconds, onChange, opts = {}) {
  const wrapper = el("div", "field");
  wrapper.appendChild(fieldHead(label, opts.hint));
  const row = el("div", "row-item");
  const total = Math.max(0, num(seconds));
  let unit = "second";
  let value = total;
  if (total >= 3600 && total % 3600 === 0) {
    unit = "hour";
    value = total / 3600;
  } else if (total >= 60 && total % 60 === 0) {
    unit = "minute";
    value = total / 60;
  }
  const input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.className = "w-md";
  input.value = value;
  const unitSelect = document.createElement("select");
  unitSelect.className = "w-md";
  unitSelect.appendChild(option("second", "秒"));
  unitSelect.appendChild(option("minute", "分钟"));
  unitSelect.appendChild(option("hour", "小时"));
  unitSelect.value = unit;
  const emit = () => {
    const factor = unitSelect.value === "hour" ? 3600 : unitSelect.value === "minute" ? 60 : 1;
    onChange(Math.max(0, Math.round(num(input.value) * factor)));
  };
  input.addEventListener("change", emit);
  unitSelect.addEventListener("change", () => {
    // 切换单位时保持"读起来一样"的数值，避免 30 分钟变成 30 小时
    const factor = unitSelect.value === "hour" ? 3600 : unitSelect.value === "minute" ? 60 : 1;
    const previous = unitSelect.dataset.previous === "hour" ? 3600 : unitSelect.dataset.previous === "minute" ? 60 : 1;
    input.value = Math.round((num(input.value) * previous) / factor);
    unitSelect.dataset.previous = unitSelect.value;
    emit();
  });
  unitSelect.dataset.previous = unit;
  row.appendChild(input);
  row.appendChild(unitSelect);
  wrapper.appendChild(row);
  return wrapper;
}

/* ================================================================== */
/* 两列多选弹窗                                                        */
/* ================================================================== */

let pickerState = null;

function openPicker({
  title,
  hint = "",
  items,
  selected = [],
  multi = true,
  col1 = "名称",
  col2 = "说明",
  onConfirm,
  exclusiveValues = [],
}) {
  pickerState = {
    // 兜底再洗一遍：别的入口直接调 openPicker 时也不会带进没有 id 的条目
    items: pickerEntries(items),
    chosen: new Set(pickerValues(selected)),
    multi,
    onConfirm,
    exclusiveValues: pickerValues(exclusiveValues),
    collapsed: {},
    cursor: "",
    group: "",
  };
  $("modal-title").textContent = title;
  $("modal-hint").textContent = hint || "";
  $("modal-col1").textContent = col1;
  $("modal-col2").textContent = col2;
  $("modal-search").value = "";
  fillPickerGroups();
  $("modal").classList.remove("hidden");
  renderPickerList();
  $("modal-search").focus();
}

/** 选择器的「只看某一组」下拉：组名来自当前列表。 */
function fillPickerGroups() {
  const select = $("modal-group");
  if (!select || !pickerState) return;
  const groups = Array.from(
    new Set(pickerState.items.map((item) => String(item.group || "其它"))),
  );
  select.innerHTML = "";
  select.appendChild(option("", `全部分组（${groups.length}）`));
  groups.forEach((group) => {
    const count = pickerState.items.filter(
      (item) => String(item.group || "其它") === group,
    ).length;
    select.appendChild(option(group, `${group}（${count}）`));
  });
  select.value = groups.includes(pickerState.group) ? pickerState.group : "";
  pickerState.group = select.value;
}

/** 当前筛选条件下看得见的条目（搜索词 + 分组），键盘操作和列表渲染共用。 */
function visiblePickerItems() {
  if (!pickerState) return [];
  const keyword = $("modal-search").value.trim().toLowerCase();
  const onlyGroup = (pickerState.group || "").trim();
  return pickerState.items.filter((item) => {
    if (onlyGroup && String(item.group || "其它") !== onlyGroup) return false;
    if (!keyword) return true;
    return [item.name, item.desc, item.id, item.tag, item.group]
      .map((value) => String(value || "").toLowerCase())
      .join(" ")
      .includes(keyword);
  });
}

function renderPickerList() {
  if (!pickerState) return;
  const list = $("modal-list");
  list.innerHTML = "";
  const visible = visiblePickerItems();

  // 已选置顶：不用在长列表里翻找自己勾了什么
  const chosenRows = visible.filter((item) => pickerState.chosen.has(item.id));
  if (chosenRows.length) {
    list.appendChild(pickGroupHeader(`已选（${chosenRows.length}）`, null));
    chosenRows.forEach((item) => list.appendChild(pickRow(item)));
  }

  // 其余按分组折叠
  const rest = visible.filter((item) => !pickerState.chosen.has(item.id));
  const groups = [];
  rest.forEach((item) => {
    const group = String(item.group || "其它");
    if (!groups.includes(group)) groups.push(group);
  });
  groups.forEach((group) => {
    const rows = rest.filter((item) => String(item.group || "其它") === group);
    list.appendChild(pickGroupHeader(group, rows));
    const collapsed = pickerState.collapsed[group] === true;
    if (!collapsed) rows.forEach((item) => list.appendChild(pickRow(item)));
  });
  if (!visible.length) {
    list.appendChild(el("p", "muted", "没有匹配的条目。"));
  }
  $("modal-count").textContent = `已选 ${pickerState.chosen.size} 项`;
}

/** 分组的表头：可折叠 + 全选本组。 */
function pickGroupHeader(title, rows) {
  const head = el("div", "pick-group");
  const group = rows ? String(rows[0].group || "其它") : "";
  const toggle = el("button", "pick-group-toggle", title);
  toggle.type = "button";
  if (group) {
    toggle.title = pickerState.collapsed[group] ? "展开这一组" : "收起这一组";
    toggle.addEventListener("click", () => {
      pickerState.collapsed[group] = !pickerState.collapsed[group];
      renderPickerList();
    });
  }
  head.appendChild(toggle);
  if (rows && rows.length) {
    const all = el("button", "ghost small", "全选本组");
    all.type = "button";
    all.addEventListener("click", () => {
      rows.forEach((item) => pickerState.chosen.add(item.id));
      renderPickerList();
    });
    head.appendChild(all);
  }
  return head;
}

function pickRow(item) {
  const row = el("div", "pick-row");
  if (pickerState.cursor === item.id) row.classList.add("cursor");
  const nameCell = el("div", "pick-name");
  const box = document.createElement("input");
  box.type = pickerState.multi ? "checkbox" : "radio";
  box.name = "modal-pick";
  box.checked = pickerState.chosen.has(item.id);
  nameCell.appendChild(box);
  nameCell.appendChild(el("span", "", item.name || item.id));
  if (item.tag) nameCell.appendChild(el("span", "tag", item.tag));
  row.appendChild(nameCell);
  row.appendChild(el("div", "pick-desc", item.desc || ""));
  row.addEventListener("click", (event) => {
    if (event.target === box) return;
    box.checked = pickerState.multi ? !box.checked : true;
    togglePick(item.id, box.checked);
    if (!pickerState.multi) renderPickerList();
  });
  box.addEventListener("change", () => {
    togglePick(item.id, box.checked);
    if (!pickerState.multi) renderPickerList();
  });
  return row;
}

function togglePick(id, on) {
  if (!pickerState) return;
  if (!pickerState.multi) pickerState.chosen.clear();
  if (on && pickerState.exclusiveValues.length) {
    const exclusive = new Set(pickerState.exclusiveValues.map((item) => String(item)));
    const isExclusive = exclusive.has(String(id));
    for (const chosen of pickerState.chosen) {
      if (exclusive.has(String(chosen)) !== isExclusive) pickerState.chosen.delete(chosen);
    }
  }
  if (on) pickerState.chosen.add(id);
  else pickerState.chosen.delete(id);
  const list = $("modal-list");
  if (list) $("modal-count").textContent = `已选 ${pickerState.chosen.size} 项`;
}

function closePicker() {
  $("modal").classList.add("hidden");
  pickerState = null;
}

function bindPicker() {
  $("modal-close").addEventListener("click", closePicker);
  $("modal-cancel").addEventListener("click", closePicker);
  $("modal").addEventListener("click", (event) => {
    if (event.target === $("modal")) closePicker();
  });
  $("modal-search").addEventListener("input", renderPickerList);
  $("modal-group").addEventListener("change", () => {
    if (!pickerState) return;
    pickerState.group = $("modal-group").value;
    renderPickerList();
  });
  // 键盘操作：↑↓ 移动、空格/回车勾选、Esc 关闭、Ctrl/Cmd+回车直接确认
  $("modal-search").addEventListener("keydown", (event) => {
    if (!pickerState) return;
    const rows = visiblePickerItems();
    if (event.key === "Escape") {
      event.preventDefault();
      closePicker();
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        $("modal-ok").click();
        return;
      }
      const first = rows.find((item) => item.id === pickerState.cursor) || rows[0];
      if (first) {
        togglePick(first.id, !pickerState.chosen.has(first.id));
        renderPickerList();
      }
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!rows.length) return;
      const index = rows.findIndex((item) => item.id === pickerState.cursor);
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = rows[(index + step + rows.length) % rows.length];
      pickerState.cursor = next.id;
      renderPickerList();
    }
  });
  $("modal-all").addEventListener("click", () => {
    if (!pickerState) return;
    // 「全选」只选当前筛选出来的那些：先筛「只看某个插件」再全选，才是想要的效果
    visiblePickerItems().forEach((item) => pickerState.chosen.add(item.id));
    renderPickerList();
  });
  $("modal-none").addEventListener("click", () => {
    if (!pickerState) return;
    pickerState.chosen.clear();
    renderPickerList();
  });
  $("modal-ok").addEventListener("click", () => {
    if (!pickerState) return;
    const chosen = Array.from(pickerState.chosen);
    const callback = pickerState.onConfirm;
    closePicker();
    callback(chosen);
  });
}

/* ================================================================== */
/* 通用表单弹窗                                                        */
/* ================================================================== */

/* 插件页跑在 sandbox="allow-scripts allow-forms allow-downloads" 的 iframe 里，
   没有 allow-modals，window.prompt / window.confirm 会被浏览器直接拦掉（点了没反应）。
   所以所有输入和确认都必须在页面里自己画。 */

let dialogState = null;

function openFormDialog({
  title,
  hint = "",
  fields,
  values = {},
  confirmText = "确定",
  wide = false,
  onSubmit,
  onDismiss,
}) {
  dialogState = { fields, values: { ...values }, onSubmit, onDismiss };
  $("dialog-title").textContent = title;
  $("dialog-hint").textContent = hint || "";
  // 确认键是全局共用的一个按钮：上一次弹窗把它禁掉过（没勾隐私须知那种），
  // 这里必须显式放开，否则下一个弹窗点「确定」怎么点都没反应。
  $("dialog-ok").disabled = false;
  $("dialog-ok").textContent = confirmText;
  $("dialog-cancel").classList.remove("hidden");
  const body = $("dialog-body");
  body.innerHTML = "";
  const card = $("dialog-card");
  if (card) card.classList.toggle("wide", Boolean(wide));

  fields.forEach((field) => {
    const wrap = el("div", "field");
    wrap.appendChild(fieldHead(field.label, field.hint));
    let control;
    if (field.type === "textarea") {
      control = document.createElement("textarea");
      control.rows = field.rows || 3;
      control.value = values[field.key] ?? field.value ?? "";
    } else if (field.type === "select") {
      control = document.createElement("select");
      (field.options || []).forEach((choice) =>
        control.appendChild(option(String(choice.value), choice.label)),
      );
      control.value = String(
        values[field.key] ?? field.value ?? (field.options?.[0]?.value ?? ""),
      );
    } else if (field.type === "checkboxes") {
      // 多选一组的勾选框（分块应用预设那种）
      control = el("div", "picker-list");
      const picked = new Set(
        (values[field.key] ?? field.value ?? []).map((item) => String(item)),
      );
      (field.options || []).forEach((choice) => {
        const row = el("label", "picker-row");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = String(choice.value);
        box.checked = picked.has(String(choice.value));
        row.appendChild(box);
        row.appendChild(el("span", "", choice.label));
        control.appendChild(row);
      });
    } else if (field.type === "checkbox") {
      control = document.createElement("input");
      control.type = "checkbox";
      control.checked = Boolean(values[field.key] ?? field.value ?? false);
    } else {
      control = document.createElement("input");
      control.type = field.type || "text";
      if (field.step !== undefined) control.step = field.step;
      if (field.min !== undefined) control.min = field.min;
      if (field.max !== undefined) control.max = field.max;
      if (field.placeholder) control.placeholder = field.placeholder;
      control.value = values[field.key] ?? field.value ?? "";
    }
    control.dataset.dialogKey = field.key;
    if (typeof field.onChange === "function") {
      const handler = () => {
        const current = {};
        fields.forEach((item) => {
          const node = body.querySelector(`[data-dialog-key="${item.key}"]`);
          if (node) current[item.key] = node.value;
        });
        field.onChange({ control, values: current, body, fields });
      };
      control.addEventListener("change", handler);
      if (field.watchInput) control.addEventListener("input", handler);
    }
    wrap.appendChild(control);
    body.appendChild(wrap);
  });

  const error = el("div", "dialog-error", "");
  error.id = "dialog-error";
  body.appendChild(error);
  $("dialog-ok").classList.remove("danger");
  $("dialog").classList.remove("hidden");
  const first = body.querySelector("input, textarea, select");
  if (first) first.focus();
}

/** 关闭弹窗。fromSubmit=true 表示"确定"关的，不再触发 onDismiss。 */
function closeDialog(fromSubmit = false) {
  const dismiss = !fromSubmit && dialogState && dialogState.onDismiss;
  $("dialog").classList.add("hidden");
  const body = $("dialog-body");
  if (body) body.classList.remove("dialog-wide");
  const card = $("dialog-card");
  if (card) card.classList.remove("wide");
  dialogState = null;
  if (typeof dismiss === "function") dismiss();
}

function collectDialogValues() {
  const body = $("dialog-body");
  const result = {};
  if (!dialogState) return result;
  dialogState.fields.forEach((field) => {
    const control = body.querySelector(`[data-dialog-key="${field.key}"]`);
    if (!control) return;
    if (field.type === "checkboxes") {
      const boxes = body.querySelectorAll(
        `[data-dialog-key="${field.key}"] input[type="checkbox"]`,
      );
      result[field.key] = Array.from(boxes)
        .filter((box) => box.checked)
        .map((box) => box.value);
    } else if (field.type === "checkbox") {
      result[field.key] = Boolean(control.checked);
    } else if (field.type === "number") {
      const raw = control.value.trim();
      result[field.key] = raw === "" ? field.emptyValue ?? 0 : num(raw, field.emptyValue ?? 0);
    } else {
      result[field.key] = control.value.trim();
    }
  });
  return result;
}

async function submitDialog() {
  if (!dialogState) return;
  const error = $("dialog-error");
  if (error) error.textContent = "";
  const handler = dialogState.onSubmit;
  const values = dialogState.custom ? dialogState.values || {} : collectDialogValues();
  try {
    const done = await handler(values);
    if (done === false) return; // 校验没过，保持弹窗打开
    closeDialog(true);
  } catch (exception) {
    if (error) error.textContent = exception?.message || "操作失败";
  }
}

/**
 * 自定义内容的弹窗：生成预览这类"要自己画列表"的地方用它。
 * ``build(body, state)`` 里自己画内容，改动写进 ``state`` 对象，提交时会原样传给 onSubmit。
 */
function openCustomDialog({
  title,
  hint = "",
  build,
  confirmText = "确定",
  hideCancel = false,
  onSubmit,
  onDismiss,
}) {
  const state = {};
  dialogState = { custom: true, values: state, onSubmit, onDismiss };
  $("dialog-title").textContent = title;
  $("dialog-hint").textContent = hint || "";
  // 同上：隐私须知那个弹窗在没勾选时会把共用的确认键禁掉，开新弹窗时放开
  $("dialog-ok").disabled = false;
  $("dialog-ok").textContent = confirmText;
  // 「改一下就生效」的弹窗不需要取消按钮：留一个关闭就够
  const cancelButton = $("dialog-cancel");
  if (cancelButton) cancelButton.classList.toggle("hidden", Boolean(hideCancel));
  const body = $("dialog-body");
  body.innerHTML = "";
  body.classList.add("dialog-wide");
  const card = $("dialog-card");
  if (card) card.classList.add("wide");
  build(body, state);
  const error = el("div", "dialog-error", "");
  error.id = "dialog-error";
  body.appendChild(error);
  $("dialog-ok").classList.remove("danger");
  $("dialog").classList.remove("hidden");
  const first = body.querySelector("input, textarea, select");
  if (first) first.focus();
}

function confirmDialog({ title = "确认", message = "", confirmText = "确定", danger = true }) {
  return new Promise((resolve) => {
    const onNo = () => resolve(false);
    let settled = false;
    const settle = (answer) => {
      if (settled) return;
      settled = true;
      resolve(answer);
      $("dialog-cancel").removeEventListener("click", onNo);
    };
    $("dialog-cancel").addEventListener("click", onNo);
    openFormDialog({
      title,
      hint: message,
      fields: [],
      confirmText,
      // 点 ✕ 或点背景关掉弹窗，一律按"否"处理，避免调用方一直等
      onDismiss: () => settle(false),
      onSubmit: () => {
        settle(true);
        return true;
      },
    });
    if (danger) $("dialog-ok").classList.add("danger");
  });
}

function bindDialog() {
  $("dialog-close").addEventListener("click", closeDialog);
  $("dialog-cancel").addEventListener("click", closeDialog);
  $("dialog-ok").addEventListener("click", submitDialog);
  $("dialog").addEventListener("click", (event) => {
    if (event.target === $("dialog")) closeDialog();
  });
  $("dialog").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.tagName !== "TEXTAREA") submitDialog();
  });
}

/** 多选字段：按钮展示已选内容，点击弹出两列选择框。 */
function pickerField(label, values, items, onChange, opts = {}) {
  const wrapper = el("div", "field");
  wrapper.appendChild(fieldHead(label, opts.hint));
  const button = el("button", "picker");
  button.type = "button";
  const entries = pickerEntries(items);
  const selected = pickerValues(values);
  const text = el(
    "div",
    `picker-text${selected.length ? "" : " empty"}`,
    selected.length
      ? selected
          .map(
            (id) =>
              (entries.find((item) => item.id === id) || { name: id }).name || id,
          )
          .join("、")
      : opts.empty || "点击选择…",
  );
  button.appendChild(text);
  button.appendChild(el("span", "picker-caret", "▾"));
  button.title = selected.length
    ? selected
        .map((id) => (entries.find((item) => item.id === id) || { name: id }).name || id)
        .join("、")
    : opts.empty || "点击选择…";
  button.addEventListener("click", () => {
    openPicker({
      title: label,
      hint: opts.hint,
      items: entries,
      selected,
      multi: opts.multi !== false,
      col1: opts.col1 || "名称",
      col2: opts.col2 || "说明",
      exclusiveValues: opts.exclusiveValues || [],
      onConfirm: (chosen) => onChange(chosen),
    });
  });
  wrapper.appendChild(button);
  if (selected.length && opts.renderChips) {
    const chips = el("div", "chips");
    selected.forEach((id) => {
      const item = entries.find((entry) => entry.id === id);
      chips.appendChild(el("span", "chip-item", item ? item.name : id));
    });
    wrapper.appendChild(chips);
  }
  return wrapper;
}

/** 选择器的条目：既认 `{ id, name }`，也认 `{ value, label }`（「选会话」那类选项用的是后者）。
 *
 * 没有 id 的条目一律丢掉：勾选写进配置的就是 id，塞进 null 保存会直接报校验错误。
 */
function pickerEntries(items) {
  return (Array.isArray(items) ? items : [])
    .map((raw) => {
      const item = raw && typeof raw === "object" ? raw : {};
      const id =
        item.id !== undefined && item.id !== null ? item.id : item.value;
      const name = item.name || item.label || String(id ?? "");
      return { ...item, id: id === undefined || id === null ? "" : id, name };
    })
    .filter((item) => String(item.id).trim() !== "");
}

/** 选择器里已经勾上的值：过滤掉 null / 空串，显示的标题才对得上。 */
function pickerValues(values) {
  return (Array.isArray(values) ? values : [])
    .filter((id) => id !== undefined && id !== null && String(id).trim() !== "");
}

/* ================================================================== */
/* 效果行编辑器（把 JSON 变成"属性 + 方式 + 数值"）                      */
/* ================================================================== */

function effectsToRows(effects) {
  const rows = [];
  let mood = "";
  Object.entries(effects || {}).forEach(([attr, raw]) => {
    const text = String(raw).trim();
    if (attr === "mood" || text.startsWith("mood:")) {
      mood = text.replace(/^mood:/, "");
      return;
    }
    const match = text.match(/^([+\-=×*]?)\s*([0-9.]+)$/);
    if (!match) return;
    rows.push({ attr, op: match[1] || "+", value: num(match[2], 0) });
  });
  return { rows, mood };
}

function rowsToEffects(rows, mood) {
  const effects = {};
  rows.forEach((row) => {
    if (!row.attr) return;
    let op = row.op || "+";
    if (op === "*") op = "×";
    effects[row.attr] = `${op}${row.value}`;
  });
  if (mood) effects.mood = `mood:${mood}`;
  return effects;
}

function effectsEditor(label, hint, effects, onChange, opts = {}) {
  const wrapper = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", label));
  const tip = tipBox(hint);
  if (tip) title.appendChild(tip);
  wrapper.appendChild(title);

  const { rows, mood } = effectsToRows(effects);
  if (mood && !opts.perMinute) {
    rows.push({ mood: true, value: mood });
  }

  const list = el("div", "rows");
  const state = rows.map((row) => ({ ...row }));

  function emit() {
    const plain = [];
    let moodValue = "";
    state.forEach((row) => {
      if (row.mood) {
        moodValue = row.value;
        return;
      }
      plain.push(row);
    });
    onChange(rowsToEffects(plain, moodValue));
  }

  function render() {
    list.innerHTML = "";
    state.forEach((row, index) => {
      const line = el("div", "row-item");
      if (row.mood) {
        const label2 = el("span", "w-md", "完成后心情");
        line.appendChild(label2);
        const input = document.createElement("input");
        input.className = "grow";
        input.placeholder = "例如：温柔 / 开心 / 困倦";
        input.value = row.value || "";
        input.addEventListener("change", () => {
          row.value = input.value.trim();
          emit();
        });
        line.appendChild(input);
      } else {
        const attrSelect = document.createElement("select");
        ATTRS.forEach((attr) => attrSelect.appendChild(option(attr.key, attr.label)));
        attrSelect.value = row.attr || "energy";
        attrSelect.className = "w-md";
        attrSelect.addEventListener("change", () => {
          row.attr = attrSelect.value;
          emit();
        });
        line.appendChild(attrSelect);

        const opSelect = document.createElement("select");
        OPS.filter((op) => (opts.perMinute ? op.key === "+" || op.key === "-" : true)).forEach(
          (op) => opSelect.appendChild(option(op.key, op.label)),
        );
        opSelect.value = row.op || "+";
        opSelect.className = "w-sm";
        opSelect.addEventListener("change", () => {
          row.op = opSelect.value;
          emit();
        });
        line.appendChild(opSelect);

        const input = document.createElement("input");
        input.type = "number";
        input.step = "0.001";
        input.className = "w-sm";
        input.value = row.value ?? 0;
        input.addEventListener("change", () => {
          row.value = num(input.value, 0);
          emit();
        });
        line.appendChild(input);
      }
      const remove = el("button", "small danger", "删除");
      remove.type = "button";
      remove.addEventListener("click", () => {
        state.splice(index, 1);
        render();
        emit();
      });
      line.appendChild(remove);
      list.appendChild(line);
    });
    if (!state.length) list.appendChild(el("p", "muted", "（还没有设置效果）"));
    wrapper.insertBefore(list, wrapper.querySelector(".rows-actions"));
  }

  const actions = el("div", "row-item rows-actions");
  const add = el("button", "small ghost", "+ 添加一条");
  add.type = "button";
  add.addEventListener("click", () => {
    if (opts.perMinute) state.push({ attr: "energy", op: "+", value: 0.001 });
    else state.push({ attr: "energy", op: "+", value: 0.05 });
    render();
  });
  actions.appendChild(add);
  if (!opts.perMinute) {
    const addMood = el("button", "small ghost", "+ 完成后心情");
    addMood.type = "button";
    addMood.addEventListener("click", () => {
      if (state.some((row) => row.mood)) return;
      state.push({ mood: true, value: "" });
      render();
    });
    actions.appendChild(addMood);
  }
  wrapper.appendChild(actions);
  render();
  return wrapper;
}

/* ================================================================== */
/* 日程动作链编辑器                                                     */
/* ================================================================== */

/** 动作在「动作链」下拉里的显示名：带上"需要先在哪儿"的提示，避免选了才发现跑不了。 */
function actionChainLabel(action) {
  const base = action.name || action.id;
  const need = stepRequiredNode({ type: action.id });
  return need ? `${base}（需先在${nodeLabel(need)}）` : base;
}

/** 这个动作要求人在哪个地点：「仅特定地点」的第一个地点。 */
function stepRequiredNode(step) {
  const action = actions().find((item) => item.id === step.type);
  if (!action) return "";
  if (action.scope === "node" && Array.isArray(action.allowed_nodes) && action.allowed_nodes.length) {
    return action.allowed_nodes[0];
  }
  return "";
}

function nodeLabel(nodeId) {
  const node = nodes().find((item) => item.id === nodeId);
  return node ? node.name || node.id : nodeId || "？";
}

/** 这条链里在这一步之前，是否已经安排了走到目标地点。 */
function chainAlreadyMovesTo(chain, index, nodeId) {
  for (let i = index - 1; i >= 0; i -= 1) {
    const step = chain[i];
    if (step.type === "walk_to" && (step.target_node || step.target) === nodeId) return true;
  }
  return false;
}

/** 持续动作的时长：数字 + 单位（分钟 / 小时 / 秒），内部仍按秒存储。 */
function chainTimeRow(step, definition, emit) {
  const wrap = el("span", "row-item");
  const fallback =
    num(step.duration) ||
    num(definition.duration_mode === "llm" ? definition.duration_min : definition.duration) ||
    60;
  const unit = fallback >= 3600 && fallback % 3600 === 0 ? 3600 : 60;
  const input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.className = "w-sm";
  input.value = Math.max(1, Math.round(fallback / unit));
  input.title = "这一步持续多久（留空表示用动作自己的默认值）";
  const select = document.createElement("select");
  select.className = "w-sm";
  [["60", "分钟"], ["3600", "小时"], ["1", "秒"]].forEach(([value, label]) =>
    select.appendChild(option(value, label)),
  );
  select.value = String(unit);
  select.dataset.prev = String(unit);
  const write = () => {
    step.duration = Math.max(0, Math.round(num(input.value) * num(select.value, 60)));
    emit();
  };
  input.addEventListener("change", write);
  select.addEventListener("change", () => {
    const seconds = num(input.value) * num(select.dataset.prev || 60, 60);
    const next = num(select.value, 60);
    input.value = Math.max(0, Math.round(seconds / next));
    select.dataset.prev = select.value;
    write();
  });
  wrap.appendChild(input);
  wrap.appendChild(select);
  return wrap;
}

function chainEditor(chain, onChange, opts = {}) {
  const wrapper = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", "动作链"));
  title.appendChild(
    tipBox(
      "按顺序执行：持续动作先开始，做完再走下一步。地点是硬条件，" +
        "不在该地点的步骤会被跳过——可以在前面补一步「移动到」。" +
        (opts.smart
          ? "（已开「智能日程」：意图由大模型到点补写，这里不用填。）"
          : "工具型 / 指令型步骤要填「意图」，参数才补得出来。"),
    ),
  );
  wrapper.appendChild(title);

  const list = el("div", "rows");
  const state = (chain || []).map((step) => ({ ...step }));

  function emit() {
    onChange(state.map((step) => ({ ...step })));
  }

  function render() {
    list.innerHTML = "";
    state.forEach((step, index) => {
      const line = el("div", "row-item");
      // 动作多了以后下拉很难用：和地点里选动作一样，点开弹窗（可搜索、按分组筛）
      const currentAction = actions().find((item) => item.id === step.type);
      const actionButton = el(
        "button",
        "ghost grow action-pick",
        currentAction ? actionChainLabel(currentAction) : step.type || "选择动作…",
      );
      actionButton.type = "button";
      actionButton.title = "点开选择动作：可以搜索、按分组只看一类";
      actionButton.addEventListener("click", () => {
        openPicker({
          title: `第 ${index + 1} 步做什么？`,
          hint: "和地点里选动作是同一个窗口：可以搜索、按分组筛选；这里只能选一个。",
          items: actionItems(),
          selected: step.type ? [step.type] : [],
          multi: false,
          col1: "动作",
          col2: "说明",
          onConfirm: (chosen) => {
            const chosenId = chosen[0];
            if (!chosenId || chosenId === step.type) return;
            step.type = chosenId;
            if (chosenId === "walk_to" && !step.target_node) {
              step.target_node = nodes()[0]?.id || "";
            }
            render();
            emit();
          },
        });
      });
      line.appendChild(actionButton);

      const definition = actions().find((action) => action.id === step.type);
      if (step.type === "walk_to") {
        const nodeSelect = document.createElement("select");
        nodeSelect.className = "w-md";
        nodes().forEach((node) => nodeSelect.appendChild(option(node.id, node.name || node.id)));
        // 新建的移动步骤默认指向第一个节点，并立刻写回配置：
        // 否则保存下来的是没有目标的 {"type": "walk_to"}，运行时只会报「找不到目标，移动取消」。
        if (!step.target_node) step.target_node = nodes()[0]?.id || "";
        nodeSelect.value = step.target_node;
        nodeSelect.title = "要移动到哪个地点";
        nodeSelect.addEventListener("change", () => {
          step.target_node = nodeSelect.value;
          emit();
        });
        line.appendChild(nodeSelect);
        emit();
      } else if (definition && definition.category === "continuous") {
        line.appendChild(el("span", "muted", "持续"));
        line.appendChild(chainTimeRow(step, definition, emit));
      }

      if (step.type === "say") {
        const messageInput = document.createElement("input");
        messageInput.className = "grow";
        messageInput.placeholder = "要说的内容（留空则由大模型自己说）";
        messageInput.title = "固定台词。留空表示让大模型根据当下情境自己说。";
        messageInput.value = (step.messages || []).join(" / ");
        messageInput.addEventListener("change", () => {
          const text = messageInput.value.trim();
          step.messages = text ? [text] : [];
          emit();
        });
        line.appendChild(messageInput);
      }

      // 工具型 / 指令型步骤要一句「想干什么」，参数才补得出来。
      // 智能日程到点由大模型补这一步的意图，这里就不必填了。
      if (definition && ["tool", "command"].includes(definition.llm_level) && !opts.smart) {
        const intentInput = document.createElement("input");
        intentInput.className = "grow";
        intentInput.placeholder = "这一步想干什么（例如：看看今天有什么科技新闻）";
        intentInput.title =
          "交给辅助模型去补工具 / 指令参数的一句话。留空时保存会尝试自动生成一句，" +
          "运行时会用动作自己的说明兜底。";
        intentInput.value = step.intent || "";
        intentInput.addEventListener("change", () => {
          step.intent = intentInput.value.trim();
          emit();
        });
        line.appendChild(intentInput);
      }

      // 这一步需要某个地点，但前面没安排走过去 → 给一个一键补移动的入口
      const need = stepRequiredNode(step);
      if (need && !chainAlreadyMovesTo(state, index, need)) {
        const jump = el("button", "small ghost", `↩ 先移动到${nodeLabel(need)}`);
        jump.type = "button";
        jump.title =
          `这一步要求她在「${nodeLabel(need)}」。点击会在它前面插入一步「移动到${nodeLabel(need)}」，` +
          "否则执行到这里会因为地点不对被跳过。";
        jump.addEventListener("click", () => {
          state.splice(index, 0, { type: "walk_to", target_node: need });
          render();
          emit();
        });
        line.appendChild(jump);
      }

      const remove = el("button", "small danger", "删除");
      remove.type = "button";
      remove.addEventListener("click", () => {
        state.splice(index, 1);
        render();
        emit();
      });
      line.appendChild(remove);
      list.appendChild(line);
    });
    if (!state.length) list.appendChild(el("p", "muted", "（还没有步骤）"));
  }

  const actionsRow = el("div", "row-item");
  const add = el("button", "small ghost", "+ 添加一步");
  add.type = "button";
  add.addEventListener("click", () => {
    state.push({ type: actions()[0]?.id || "say", messages: [], duration: 0 });
    render();
    emit();
  });
  actionsRow.appendChild(add);
  wrapper.appendChild(list);
  wrapper.appendChild(actionsRow);
  render();
  return wrapper;
}

/* ================================================================== */
/* 键值行编辑器（群名片文案等）                                          */
/* ================================================================== */

function kvEditor(label, hint, map, onChange, opts = {}) {
  const wrapper = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", label));
  const tip = tipBox(hint);
  if (tip) title.appendChild(tip);
  wrapper.appendChild(title);

  const list = el("div", "rows");
  const state = Object.entries(map || {}).map(([key, value]) => ({ key, value }));

  // 键是"可下拉选择、也可手输"的：用 datalist 做候选，
  // 这样既能从动作里带出来的状态里挑，也能自己写一个没见过的状态 id。
  const listId = `kv-${Math.random().toString(36).slice(2, 9)}`;
  if (opts.keyChoices) {
    const datalist = document.createElement("datalist");
    datalist.id = listId;
    const known = new Set(opts.keyChoices.map((choice) => String(choice.key)));
    opts.keyChoices.forEach((choice) => {
      const opt = document.createElement("option");
      opt.value = String(choice.key);
      if (choice.label && choice.label !== choice.key) opt.label = choice.label;
      datalist.appendChild(opt);
    });
    // 已经写在配置里、但不在候选里的键也列出来，免得看着像丢了
    state.forEach((row) => {
      if (!row.key || known.has(row.key)) return;
      const opt = document.createElement("option");
      opt.value = row.key;
      opt.label = "（自定义，当前配置里在用）";
      datalist.appendChild(opt);
      known.add(row.key);
    });
    wrapper.appendChild(datalist);
  }

  function emit() {
    const result = {};
    state.forEach((row) => {
      if (row.key) result[row.key] = row.value;
    });
    onChange(result);
  }

  function render() {
    list.innerHTML = "";
    state.forEach((row, index) => {
      const line = el("div", "row-item");
      if (opts.keyChoices) {
        const keyInput = document.createElement("input");
        keyInput.className = "w-md";
        keyInput.setAttribute("list", listId);
        keyInput.placeholder = opts.keyPlaceholder || "可下拉选择，也可直接输入";
        keyInput.value = row.key || "";
        keyInput.addEventListener("change", () => {
          row.key = keyInput.value.trim();
          emit();
        });
        line.appendChild(keyInput);
      } else {
        const keyInput = document.createElement("input");
        keyInput.className = "w-md";
        keyInput.placeholder = opts.keyPlaceholder || "键";
        keyInput.value = row.key || "";
        keyInput.addEventListener("change", () => {
          row.key = keyInput.value.trim();
          emit();
        });
        line.appendChild(keyInput);
      }
      const valueInput = document.createElement("input");
      valueInput.className = "grow";
      valueInput.placeholder = opts.valuePlaceholder || "显示文案";
      valueInput.value = row.value || "";
      valueInput.addEventListener("change", () => {
        row.value = valueInput.value;
        emit();
      });
      line.appendChild(valueInput);
      const remove = el("button", "small danger", "删除");
      remove.type = "button";
      remove.addEventListener("click", () => {
        state.splice(index, 1);
        render();
        emit();
      });
      line.appendChild(remove);
      list.appendChild(line);
    });
    if (!state.length) list.appendChild(el("p", "muted", "（没有条目）"));
  }

  const actionsRow = el("div", "row-item");
  const add = el("button", "small ghost", "+ 添加一条");
  add.type = "button";
  add.addEventListener("click", () => {
    // 键是自由文本（可下拉可选、也可手输），所以新增时留空让用户自己填
    state.push({ key: "", value: "" });
    render();
  });
  actionsRow.appendChild(add);
  wrapper.appendChild(list);
  wrapper.appendChild(actionsRow);
  render();
  return wrapper;
}

/* ================================================================== */
/* 登录                                                                */
/* ================================================================== */

async function boot() {
  bindPicker();
  bindDialog();
  bindHistory();
  bindReview();
  bindEval();
  if (!bridge || typeof bridge.apiGet !== "function") {
    $("login").classList.remove("hidden");
    $("login-message").textContent =
      "没有检测到 AstrBot 的插件页面 bridge，请在 AstrBot WebUI 的插件详情页里打开本页面。";
    return;
  }
  if (window.parent === window) {
    // 直接访问页面时拿不到插件页的凭证，接口永远不会响应，这里先给个说法，别留一片空白。
    $("login").classList.remove("hidden");
    $("login-message").textContent =
      "请在 AstrBot WebUI 的插件详情页里打开本页面（插件管理 → 虚拟世界 → SOLIS世界编辑器）。";
    return;
  }
  try {
    await Promise.race([
      bridge.ready(),
      new Promise((resolve) => window.setTimeout(resolve, 5000)),
    ]);
  } catch (error) {
    /* 独立打开页面时 bridge 可能不可用，继续尝试 */
  }
  let status = null;
  try {
    status = await apiGet("auth/status");
  } catch (error) {
    $("login").classList.remove("hidden");
    $("login-message").textContent = `无法连接插件后端：${error.message || error}`;
    return;
  }
  if (!status || !status.password_required) {
    ui.token = "";
    ui.passwordRequired = false;
    await startApp();
    return;
  }
  ui.passwordRequired = true;
  $("login").classList.remove("hidden");
  $("login-button").addEventListener("click", doLogin);
  $("login-password").addEventListener("keydown", (event) => {
    if (event.key === "Enter") doLogin();
  });
}

async function doLogin() {
  $("login-message").textContent = "";
  try {
    const result = await bridge.apiPost("auth/login", {
      password: $("login-password").value,
    });
    ui.token = result.token || "";
    $("login").classList.add("hidden");
    await startApp();
  } catch (error) {
    $("login-message").textContent = error.message || "登录失败";
  }
}

/* ================================================================== */
/* 启动与数据加载                                                       */
/* ================================================================== */

async function startApp() {
  $("app").classList.remove("hidden");
  applyTheme(currentTheme(), false);
  ui.settingsMode = readSettingsMode();
  bindTabs();
  bindButtons();
  bindTopics();
  bindMemes();
  renderHistoryWindows();
  watchPronoun();
  await loadAll();
  // 第一次打开：把她"必须先配的"和"最影响手感的"集中问一遍（关掉也算看过，不反复弹）
  if (!ui.config || !ui.config.world) return;
  if (ui.config.world.wizard_done !== true) {
    openWizard();
  }
}

/* ================================================================== */
/* 主题：深色 / 浅色，选择记在 localStorage（下次刷新还在）              */
/* ================================================================== */

const THEME_KEY = "vw-theme";
const ACTION_VIEW_KEY = "vw-action-view";

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

/** 切换 / 应用主题。persist=false 用于启动时按缓存回填。 */
function applyTheme(theme, persist = true) {
  const next = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  const button = $("theme-toggle");
  if (button) {
    button.textContent = next === "light" ? "☀️ 浅色" : "🌙 深色";
    button.title =
      next === "light"
        ? "当前为浅色主题，点击切换到深色（会记住选择）"
        : "当前为深色主题，点击切换到浅色（会记住选择）";
  }
  if (persist) {
    try {
      window.localStorage.setItem(THEME_KEY, next);
    } catch (error) {
      /* 隐私模式下写不进缓存，本次仍然生效 */
    }
  }
  // 心情曲线是 canvas 画的，颜色取自 CSS 变量：换主题后按缓存的数据重画一次
  const canvas = $("history-canvas");
  if (canvas && Array.isArray(ui.historyPoints)) {
    drawHistoryChart(canvas, ui.historyPoints);
  }
}

function toggleTheme() {
  applyTheme(currentTheme() === "light" ? "dark" : "light");
}

async function loadAll(options = {}) {
  setSaveState("加载中…");
  try {
    const [config, tools, defaults] = await Promise.all([
      apiGet("config"),
      apiGet("tools"),
      apiGet("defaults"),
    ]);
    ui.config = config;
    ui.tools = tools.tools || [];
    ui.defaults = defaults || { actions: {}, captions: {} };
    // 扩展注册的调试类型跟内置默认文案一起给（同一份 /defaults 载荷）
    applyDebugEvents((ui.defaults || {}).debug_events);
    ui.sessions = (config.sessions && config.sessions.sessions) || [];
    // 配置提醒（config.warnings）不再 toast：读盘时发现的问题每次进站、每次保存都来
    // 一遍，弹窗关不掉还挡视线。改成常驻横幅，见 renderToolWarnings。
    renderEverything();
    setSaveState("已加载");
    await refreshMigrations({ prompt: options.promptMigrations !== false });
  } catch (error) {
    setSaveState("加载失败");
    toast(error.message || "加载配置失败");
  }
}

/** 数字/文字给人看：迁移弹窗里的「老值 → 新值」。 */
function formatMigrationValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return String(Math.round(value * 1e6) / 1e6);
  if (typeof value === "boolean") return value ? "开" : "关";
  return String(value);
}

/**
 * 拉一次待确认的内容迁移（T17），需要时开弹窗。
 *
 * ``prompt=false`` 时只记账不弹窗（保存后、应用后刷新用）：保存会改 ``world_rev``，
 * 但同一批条目不该被问第二次，所以去重按条目 id 的指纹（``ui.migrationPromptShown``）。
 */
async function refreshMigrations({ prompt = true } = {}) {
  let payload = null;
  try {
    payload = await apiGet("migrations");
  } catch (error) {
    return; // 还没有这条路由（旧后端）/ 读盘失败：不该让整个页面加载不起来
  }
  if (!payload || !Array.isArray(payload.items)) return;
  ui.migrations = payload;
  const signature = payload.items
    .map((item) => item.id)
    .sort()
    .join(",");
  const asked = ui.migrationPromptShown === signature;
  ui.migrationPromptShown = signature;
  if (!prompt || !signature || asked) return;
  await promptMigrationDialog(payload.items);
}

/**
 * 「有 N 项旧配置可以升级」：勾中的才落盘，其余原样不动。
 *
 * 三条硬要求：``conflict``（用户自己改过的）与 ``reversible=false``（改的是动作描述这类
 * 文字内容）默认不勾；同一批条目一次启动只问一次；「暂不」= 本次启动不再问。
 * iframe 里没有 allow-modals，所以只能用 ``openCustomDialog``（原生 confirm 会被吞掉）。
 */
function promptMigrationDialog(items) {
  return new Promise((resolve) => {
    const pending = items.slice();
    const checked = {};
    pending.forEach((item) => {
      checked[item.id] = item.default_on === true;
    });
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const refreshTitle = () => {
      const title = $("dialog-title");
      if (title) title.textContent = `有 ${pending.length} 项旧配置可以升级`;
    };
    openCustomDialog({
      title: `有 ${items.length} 项旧配置可以升级`,
      confirmText: "全部应用",
      // 共用的取消键写的就是"取消"；这里要的是「暂不（本次启动不再问）」，自己画在底部
      hideCancel: true,
      build(body) {
        body.appendChild(
          el(
            "p",
            "migration-lead",
            "这些是我们改过的默认，不影响你现在的设置，除非你勾选。",
          ),
        );
        const list = el("div", "migration-list");
        body.appendChild(list);
        let lastTitle = "";
        pending.forEach((item) => {
          if (item.title && item.title !== lastTitle) {
            lastTitle = item.title;
            list.appendChild(el("div", "migration-group", item.title));
          }
          const row = el("label", "migration-row");
          const box = document.createElement("input");
          box.type = "checkbox";
          box.className = "migration-box";
          box.checked = checked[item.id] === true;
          box.addEventListener("change", () => {
            checked[item.id] = box.checked;
          });
          row.appendChild(box);
          const text = el("div", "migration-text");
          text.appendChild(el("div", "migration-label", item.label || item.field_path));
          text.appendChild(
            el(
              "div",
              "migration-values",
              `${formatMigrationValue(item.before)} → ${formatMigrationValue(item.after)}`,
            ),
          );
          if (item.note) text.appendChild(el("div", "migration-note", item.note));
          row.appendChild(text);
          const later = el("button", "migration-later", "别再问");
          later.type = "button";
          later.addEventListener("click", async (event) => {
            event.preventDefault();
            try {
              await apiPost("migrations/skip", { ids: [item.id] });
            } catch (error) {
              toast(error.message || "这一项没能记住，下次再问");
              return;
            }
            checked[item.id] = false;
            const index = pending.findIndex((one) => one.id === item.id);
            if (index >= 0) pending.splice(index, 1);
            row.remove();
            refreshTitle();
          });
          row.appendChild(later);
          list.appendChild(row);
        });
        const foot = el("div", "migration-foot");
        foot.appendChild(
          el("span", "migration-foot-note", "只勾你想改的，其余保持你现在的设置。"),
        );
        const skip = el("button", "migration-skip", "暂不（本次启动不再问）");
        skip.type = "button";
        skip.addEventListener("click", () => {
          closeDialog(); // 触发 onDismiss → finish(false)
        });
        foot.appendChild(skip);
        body.appendChild(foot);
      },
      onDismiss: () => finish(false),
      onSubmit: async () => {
        const ids = pending
          .filter((item) => checked[item.id] === true)
          .map((item) => item.id);
        if (!ids.length) {
          toast("一项都没勾，那就先不动");
          return false;
        }
        const result = await apiPost("migrations/apply", { ids });
        const applied = (result && result.applied) || [];
        const failed = (result && result.failed) || [];
        if (failed.length) toast(`有 ${failed.length} 项没能应用：${failed.join("、")}`);
        else toast(`已应用 ${applied.length} 项`);
        // 应用后刷新，但不许立刻又弹一次（剩下的冲突项留着下次启动再问）
        await loadAll({ promptMigrations: false });
        finish(true);
        return true;
      },
    });
  });
}

function renderEverything() {
  if (!ui.selectedZone && zones().length) ui.selectedZone = zones()[0].id;
  renderSessionSelects();
  renderMap();
  renderNodeForm();
  renderActionGrid();
  renderActionForm();
  renderScheduleList();
  renderScheduleForm();
  renderSessionList();
  renderTempConfig();
  renderMemoryFilters();
  renderSettings();
  renderToolWarnings();
  refreshStatus();
  loadTools();
  loadOverview();
  applyPronoun($("app"));
}

function bindTabs() {
  $("tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    showTab(button.dataset.tab);
  });
}

/** 「更多筛选」：把次要筛选条件收起来，默认只留会话 + 关键词。 */

/**
 * 「适应画布」：把当前层的区域 / 地点整体挪到画布中间。
 * 只改坐标、不改缩放，所以拖拽的手感不变；也只在点按钮时跑一次，不会在拖动过程中抖。
 */
function fitMapToCanvas() {
  const canvas = $("canvas");
  if (!canvas) return;
  const level = ui.mapLevel === "zone" ? "zone" : "world";
  const list = level === "zone" ? nodesInZone(ui.selectedZone) : zones();
  if (!(list || []).length) {
    toast(level === "zone" ? "这个区域里还没有地点" : "还没有区域");
    return;
  }
  const boxW = 104; // .node 的宽度
  const boxH = 56;
  const xs = list.map((item) => num(item.x, 0));
  const ys = list.map((item) => num(item.y, 0));
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  const maxX = Math.max(...xs);
  const maxY = Math.max(...ys);
  // 目标：内容中心落在"当前可见区域"的中心（要算上滚动位置）
  const dx = Math.round(
    canvas.scrollLeft + (canvas.clientWidth - (maxX - minX + boxW)) / 2 - minX,
  );
  const dy = Math.round(
    canvas.scrollTop + (canvas.clientHeight - (maxY - minY + boxH)) / 2 - minY,
  );
  if (dx === 0 && dy === 0) {
    toast("已经在中间了");
    return;
  }
  list.forEach((item) => {
    item.x = Math.max(0, Math.round(num(item.x, 0) + dx));
    item.y = Math.max(0, Math.round(num(item.y, 0) + dy));
  });
  markDirty();
  renderMap();
  renderNodeForm();
  toast(level === "zone" ? "地点已经挪到中间" : "区域已经挪到中间");
}

function bindSegmented(segId, panes) {
  const seg = $(segId);
  if (!seg) return;
  const buttons = Array.from(seg.querySelectorAll("button[data-seg]"));
  const show = (key) => {
    buttons.forEach((item) => item.classList.toggle("on", item.dataset.seg === key));
    Object.entries(panes).forEach(([name, paneId]) => {
      const pane = $(paneId);
      if (pane) pane.classList.toggle("hidden", name !== key);
    });
  };
  buttons.forEach((item) => {
    item.addEventListener("click", () => show(item.dataset.seg));
  });
}

function bindFilterToggle(buttonId, scopeSelector) {
  const button = $(buttonId);
  const panel = document.querySelector(`${scopeSelector} .filter-extra`);
  if (!button || !panel) return;
  button.addEventListener("click", () => {
    const open = !panel.classList.toggle("hidden");
    button.classList.toggle("on", open);
    button.textContent = open ? "收起筛选 ▴" : "更多筛选 ▾";
  });
}

/** 切到某一页：导航高亮 + 顶栏标题 + 该页需要的数据。 */
function showTab(name, button) {
  const target = button || document.querySelector(`#tabs button[data-tab="${name}"]`);
  document.querySelectorAll("#tabs button").forEach((item) => {
    item.classList.toggle("active", item === target);
  });
  document.querySelectorAll(".tab").forEach((section) => {
    section.classList.toggle("active", section.id === `tab-${name}`);
  });
  const title = $("page-title");
  if (title && target) title.textContent = target.textContent.trim();
  if (name === "status") refreshStatus();
  if (name === "agent") refreshAgentPage();
  if (name === "events") loadEvents();
  if (name === "materials") showMaterialPane(ui.materialPane || "topics");
  else if (typeof closeMemeDrawer === "function") closeMemeDrawer();
  if (name === "contacts") loadContacts();
  if (name === "memories") loadMemories();
  if (name === "logs") loadLogs();
  // 临时会话（用户 m00001）：进会话页先取一次，切到那一栏再取一次
  if (name === "sessions") loadTempSessions();
  if (name === "map") loadOverview();
  if (name === "debug") loadTools();
  if (name === "presets") loadPresets();
}

function bindButtons() {
  $("save").addEventListener("click", saveAll);
  bindFilterToggle("memory-filter-toggle", "#tab-memories");

  // 用户 m11096 第 12 条：会话摘要可查、可改（传空就是删掉这一份）。
  $("memory-summaries").addEventListener("click", async () => {
    const session = $("memory-session").value || ui.session || "";
    if (!session) {
      toast("先选一个会话", "warn");
      return;
    }
    try {
      const data = await apiGet("state", { session });
      const rows = Array.isArray(data.chat_summaries) ? data.chat_summaries : [];
      if (!rows.length) {
        toast("还没有会话摘要（聊得多、攒得够长才会压成摘要）", "warn");
        return;
      }
      const fields = rows.map((row, index) => ({
        key: String(row.session || index),
        label: `${row.label || row.session || "这一份"}摘要`,
        hint: row.session || "",
        type: "textarea",
        rows: 5,
        value: row.text || "",
      }));
      const values = {};
      const ok = await new Promise((resolve) => {
        openFormDialog({
          title: "会话摘要",
          hint: "这些都是她自己压出来的「更早聊过的」。改完保存，下一轮就按新内容说；清空等于删掉这一段。",
          fields,
          values,
          confirmText: "保存全部",
          wide: true,
          onSubmit: async (next) => {
            for (const field of fields) {
              const text = String(next[field.key] ?? "").trim();
              if (text === String(field.value || "").trim()) continue;
              await apiPost("summaries/save", { session, target: field.key, text });
            }
            toast("改好了", "ok");
            resolve(true);
            return true;
          },
          onDismiss: () => resolve(false),
        });
      });
      if (ok) loadMemories();
    } catch (error) {
      toast(`读取摘要失败：${error.message || error}`, "danger");
    }
  });

  // 用户 m11096 第 16 条：记忆库里同一句话常被记好几遍，给一个手动整理按钮。
  $("memory-tidy").addEventListener("click", async () => {
    const button = $("memory-tidy");
    const session = $("memory-session").value || ui.session || "";
    if (!session) {
      toast("先选一个会话", "warn");
      return;
    }
    button.disabled = true;
    try {
      const data = await apiPost("memories/tidy", { session });
      toast(data.message || "整理好了", "ok");
      ui.saveState = "clean";
      loadMemories();
    } catch (error) {
      toast(`整理失败：${error.message || error}`, "danger");
    } finally {
      button.disabled = false;
    }
  });
  bindFilterToggle("log-filter-toggle", "#tab-logs");
  bindSegmented("session-seg", {
    sessions: "session-pane",
    groups: "group-pane",
    // 临时会话（用户 m00001 需求 12）：她私聊出去的陌生人，单独的第三栏
    temp: "temp-pane",
  });
  if ($("theme-toggle")) {
    $("theme-toggle").addEventListener("click", toggleTheme);
  }
  if ($("hero-goto-events")) {
    $("hero-goto-events").addEventListener("click", () => showTab("events"));
  }
  ["map-fit", "map-fit-zone"].forEach((id) => {
    if ($(id)) $(id).addEventListener("click", fitMapToCanvas);
  });
  if ($("session-seg")) {
    // 切到「临时会话」那一栏才去取数据（这一页其余两栏是本地数据，不用请求）
    $("session-seg").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-seg]");
      if (button && button.dataset.seg === "temp") loadTempSessions();
    });
  }
  if ($("debug-expand")) {
    $("debug-expand").addEventListener("click", () => {
      const sections = Array.from(document.querySelectorAll("#debug-output .debug-section"));
      const open = $("debug-expand").dataset.expanded !== "1";
      sections.forEach((item) => {
        if (open) item.setAttribute("open", "open");
        else item.removeAttribute("open");
      });
      applyDebugExpandState();
    });
  }
  if ($("debug-copy")) {
    $("debug-copy").addEventListener("click", () => {
      const text = String(ui.debugPrompt || "");
      if (!text) {
        toast("先点一次「预览接管请求」或「预览自主提示词」");
        return;
      }
      copyText(text);
    });
  }
  if ($("action-view-cards")) {
    ui.actionView = readActionView();
    $("action-view-cards").addEventListener("click", () => {
      ui.actionView = "cards";
      applyActionView();
    });
    $("action-view-table").addEventListener("click", () => {
      ui.actionView = "table";
      applyActionView();
    });
  }
  if ($("settings-show-common")) {
    $("settings-show-common").addEventListener("click", () => setSettingsMode("common"));
  }
  if ($("wizard-open")) {
    $("wizard-open").addEventListener("click", () => openWizard());
  }
  if ($("handfeel-open")) {
    $("handfeel-open").addEventListener("click", () => openHandfeel());
  }
  if ($("reset-defaults-open")) {
    $("reset-defaults-open").addEventListener("click", () => openResetDefaults());
  }
  if ($("manual-open")) {
    $("manual-open").addEventListener("click", () => openManualEditor());
  }
  if ($("settings-search")) {
    $("settings-search").addEventListener("input", (event) =>
      searchSettings(event.target.value),
    );
  }
  if ($("event-refresh")) {
    $("event-refresh").addEventListener("click", loadEvents);
  }
  if ($("contacts-refresh")) {
    $("contacts-refresh").addEventListener("click", loadContacts);
  }
  if ($("contacts-consolidate")) {
    $("contacts-consolidate").addEventListener("click", async () => {
      const session = $("contacts-session") ? $("contacts-session").value : "";
      if (!session) return;
      toast("正在整理…（会调用「睡眠整理模型」）");
      try {
        const result = await apiPost("profile/consolidate", { session });
        toast(result.note || result.summary || "整理完了");
        loadContacts();
      } catch (error) {
        toast(error.message || "整理失败");
      }
    });
  }
  if ($("contacts-preview")) {
    $("contacts-preview").addEventListener("click", async () => {
      const session = $("contacts-session") ? $("contacts-session").value : "";
      if (!session) return;
      toast("正在跑一遍整理（只看结果，不写库）…");
      try {
        const result = await apiPost("profile/consolidate", {
          session,
          dry_run: true,
        });
        renderConsolidatePreview(result);
      } catch (error) {
        toast(error.message || "预览失败");
      }
    });
  }
  if ($("contacts-search")) {
    $("contacts-search").addEventListener("input", renderContactsList);
  }
  if ($("contacts-clear")) {
    $("contacts-clear").addEventListener("click", async () => {
      const session = $("contacts-session") ? $("contacts-session").value : "";
      if (!session) return;
      const count = (ui.contacts || []).length;
      const sure = await confirmDialog({
        title: "清空通讯录",
        message:
          `这个会话组里她认识的人（现在 ${count} 个）会全部被忘掉：` +
          "画像、事实、关系、好感记录一起删，没法撤回；关系档位、会话白名单、聊天记录都不动。",
        confirmText: count ? `忘掉这 ${count} 个人` : "清空",
      });
      if (!sure) return;
      try {
        const result = await apiPost("profile/forget-all", { session });
        ui.contactUser = "";
        toast(
          result.people
            ? `清空好了：忘掉 ${result.people} 个人`
            : "本来就没人，通讯录已经是空的",
        );
        await loadContacts();
      } catch (error) {
        toast(error.message || "清空失败");
      }
    });
  }
  if ($("contacts-session")) {
    $("contacts-session").addEventListener("change", () => {
      ui.contactUser = "";
      loadContacts();
    });
  }
  if ($("contacts-goto-sessions")) {
    $("contacts-goto-sessions").addEventListener("click", () => {
      const button = document.querySelector('#tabs button[data-tab="sessions"]');
      if (button) button.click();
    });
  }
  if ($("contacts-list")) {
    $("contacts-list").addEventListener("click", (event) => {
      const row = event.target.closest(".contact-row");
      if (!row) return;
      ui.contactUser = row.dataset.user;
      loadContactDetail(ui.contactUser);
    });
  }
  if ($("contacts-detail")) {
    $("contacts-detail").addEventListener("click", (event) => {
      const node = event.target.closest("[data-act]");
      if (!node) return;
      contactAction(node);
    });
  }
  if ($("event-history")) {
    $("event-history").addEventListener("click", () => openEventModal());
  }
  if ($("event-modal-close")) {
    $("event-modal-close").addEventListener("click", closeEventModal);
  }
  if ($("event-modal-done")) {
    $("event-modal-done").addEventListener("click", closeEventModal);
  }
  if ($("event-modal")) {
    // 点遮罩关掉弹窗（和动作抽屉一个手感）
    $("event-modal").addEventListener("click", (event) => {
      if (event.target === $("event-modal")) closeEventModal();
    });
  }
  if ($("event-submit")) {
    $("event-submit").addEventListener("click", async () => {
      const box = $("event-seed");
      const text = box ? box.value.trim() : "";
      if (!text) {
        toast("先写一件要发生的事，例如：出门忘了带伞");
        return;
      }
      try {
        const data = await apiPost("state/action", { action: "event", text, session: $("status-session").value });
        toast((data && data.note) || "这件事发生了，看她怎么处理");
        if (box) box.value = "";
        ui.events = (data && data.events) || ui.events;
        renderEventsPanel();
        refreshStatus();
      } catch (error) {
        toast(`投递失败：${error.message || error}`);
      }
    });
  }

  $("status-refresh").addEventListener("click", refreshStatus);
  if ($("agent-refresh")) $("agent-refresh").addEventListener("click", refreshAgentPage);
  if ($("agent-export")) $("agent-export").addEventListener("click", exportAgentBundle);

async function exportAgentBundle() {
  // 用户 m11096：一键导出「现状 / 最近对话 / 计划 / 记忆」，整份拿走看
  const button = $("agent-export");
  const old = button.textContent;
  button.disabled = true;
  button.textContent = "导出中…";
  try {
    const data = await apiGet("export", { session: ui.session || "", limit: 120 });
    const text = String((data && data.text) || "");
    if (!text) throw new Error("没有内容");
    const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `virtual-world-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.md`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
  } catch (error) {
    toast(`导出失败：${error.message || error}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = old;
  }
}
  if ($("agent-seg")) {
    $("agent-seg").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-seg]");
      if (button) showAgentPane(button.dataset.seg, true);
    });
  }
  if ($("agent-runs-failed")) {
    $("agent-runs-failed").addEventListener("change", () => {
      ui.agent.onlyFailed = $("agent-runs-failed").checked;
      loadAgentRuns();
    });
  }
  if ($("agent-runs-prev")) {
    $("agent-runs-prev").addEventListener("click", () => {
      loadAgentRunsPage(Math.max(1, (ui.agent.page || 1) - 1));
    });
  }
  if ($("agent-runs-more")) {
    $("agent-runs-more").addEventListener("click", () => {
      loadAgentRunsPage((ui.agent.page || 1) + 1);
    });
  }
  $("status-session").addEventListener("change", () => {
    setScopeSession(scopeSession(), "status-session");
    refreshScopePage();
    refreshStatus();
    // 换了会话，Agent 页那一份快照与工具调用记录也跟着作废
    if ($("tab-agent") && $("tab-agent").classList.contains("active")) refreshAgentPage();
    // 换会话 = 可能换了人格：简易人设跟着刷新一遍
    if (typeof ui.loadPersonaBrief === "function") {
      ui.loadPersonaBrief({ overwrite: true });
    }
    // 谈资是按会话（scope）存的：换会话先丢掉上一个会话的数据，
    // 否则点开谈资会看到别的会话的内容。
    topicForget();
    topicSyncFlag();
    const materialsActive =
      $("tab-materials") && $("tab-materials").classList.contains("active");
    if (materialsActive && ui.materialPane === "topics") {
      loadTopics();
      loadTopicRecall();
    } else {
      renderTopicsList();
      renderTopicDetail();
      renderTopicRecall();
    }
  });
  $("values-edit").addEventListener("click", () => {
    ui.valuesEdit = !ui.valuesEdit;
    ui.valueDraft = {};
    renderValueRows(((ui.status || {}).values) || {}, ui.status || {});
  });
  $("state-tick").addEventListener("click", () => stateAction("tick"));
  $("state-decide").addEventListener("click", () => stateAction("decide", { force: true }));
  $("state-interrupt").addEventListener("click", () => stateAction("interrupt"));
  $("state-wake").addEventListener("click", () => stateAction("wake"));
  $("state-clear-context").addEventListener("click", clearChatContext);
  $("status-nickname-save").addEventListener("click", saveNickname);
  $("status-nickname-fetch").addEventListener("click", () => nicknameAction("refresh_nickname"));
  // 锁定 / 解锁合成一个按钮：按钮文字跟着当前状态走
  $("status-nickname-toggle").addEventListener("click", () => {
    const locked = Boolean((ui.status || {}).nickname_locked);
    nicknameAction(locked ? "unlock_nickname" : "lock_nickname");
  });
  $("status-nickname-reset").addEventListener("click", () => nicknameAction("reset_nickname"));

  $("map-back").addEventListener("click", backToWorldMap);
  const weatherRefresh = $("map-weather-refresh");
  if (weatherRefresh) weatherRefresh.addEventListener("click", refreshWeatherNow);
  $("zone-add").addEventListener("click", addZone);
  $("zone-delete").addEventListener("click", deleteZone);
  $("zone-edge-add").addEventListener("click", addPortal);
  $("map-json").addEventListener("click", editMapJson);
  $("zone-enter").addEventListener("click", () => {
    if (!ui.selectedZone) {
      toast("先选中一个区域");
      return;
    }
    enterZone(ui.selectedZone);
  });
  $("node-add").addEventListener("click", addNode);
  $("node-delete").addEventListener("click", deleteNode);
  $("edge-add").addEventListener("click", addEdge);
  $("portal-add").addEventListener("click", addPortal);
  $("map-session").addEventListener("change", renderMap);
  $("map-show-all").addEventListener("change", renderMap);
  $("action-add").addEventListener("click", addAction);
  $("action-search").addEventListener("input", renderActionGrid);
  $("action-json").addEventListener("click", editActionsJson);
  $("action-drawer-close").addEventListener("click", closeActionDrawer);
  $("action-cancel").addEventListener("click", closeActionDrawer);
  // 点遮罩等同「取消」：草稿丢掉，不写回列表
  $("drawer-backdrop").addEventListener("click", closeActionDrawer);
  $("action-save").addEventListener("click", () => {
    if (saveActionDraft()) closeActionDrawer();
  });
  $("schedule-add").addEventListener("click", addSchedule);
  // 「立即执行用哪个会话」也决定落点里能勾哪些：换了就重画表单
  $("schedule-session").addEventListener("change", () => renderScheduleForm());
  $("session-add").addEventListener("click", addSession);
  $("group-add").addEventListener("click", addGroup);
  $("memory-search").addEventListener("click", loadMemories);
  $("memory-add").addEventListener("click", addMemory);
  $("memory-select-all").addEventListener("click", () =>
    toggleSelectAll("memory-list", "memorySelection"),
  );
  $("memory-delete-selected").addEventListener("click", () =>
    deleteSelected("memory"),
  );
  $("memory-clear").addEventListener("click", () => clearAll("memory"));

  $("log-refresh").addEventListener("click", loadLogs);
  $("log-select-all").addEventListener("click", () =>
    toggleSelectAll("log-list", "logSelection"),
  );
  $("log-delete-selected").addEventListener("click", () => deleteSelected("log"));
  $("log-clear").addEventListener("click", () => clearAll("log"));
  $("log-session").addEventListener("change", loadLogs);
  $("log-type").addEventListener("change", loadLogs);
  $("log-limit").addEventListener("change", loadLogs);
  $("log-keyword").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadLogs();
  });
  $("log-export").addEventListener("click", async () => {
    const session = $("log-session").value;
    if (!session) return;
    try {
      await bridge.download("logs/export", { session }, "virtual-world-logs.json");
      toast("已开始下载日志");
    } catch (error) {
      toast(error.message || "导出失败");
    }
  });
  $("memory-export").addEventListener("click", () => {
    const session = $("memory-session").value;
    bridge
      .download("memories/export", { session }, "virtual-world-memories.json")
      .then(() => toast("已开始下载记忆"))
      .catch((error) => toast(error.message || "导出失败"));
  });
  $("pwd-save").addEventListener("click", savePassword);
  $("restore-default").addEventListener("click", restoreDefault);
  $("debug-inject").addEventListener("click", () => loadPrompt("takeover"));
  $("preset-save").addEventListener("click", saveCurrentAsPreset);
  $("preset-import").addEventListener("click", importPreset);
  $("preset-new-default").addEventListener("click", newDefaultPreset);
  $("preset-refresh").addEventListener("click", loadPresets);
  $("debug-auto").addEventListener("click", () => loadPrompt("autonomous"));
  $("debug-backup").addEventListener("click", async () => {
    try {
      const result = await apiPost("backup", {});
      toast(`已备份：${result.file}`);
    } catch (error) {
      toast(error.message || "备份失败");
    }
  });

  ui.statusTimer = window.setInterval(() => {
    if ($("tab-status").classList.contains("active")) refreshStatus();
  }, 10000);
  ui.logTimer = window.setInterval(() => {
    if ($("log-auto").checked && $("tab-logs").classList.contains("active")) loadLogs();
  }, 5000);
  ui.mapTimer = window.setInterval(() => {
    if ($("tab-map").classList.contains("active")) loadOverview();
  }, 10000);
}

async function saveAll() {
  // 防重复提交：保存中再点一次不排队、不并行（双击 / 回车）
  if (ui.saving) return null;
  // 提交快照 + 编辑版本号：**只清"提交那一刻"的脏标记**，保存期间的新编辑必须留下
  const revision = ui.editRevision || 0;
  // **只提交真正改过的域**（复核报告 P1-2 后半）：改日程不该顺带把世界配置再提交一遍
  // （那会触发动作说明重排、还会把"缺少内置动作"之类的补回提示塞给用户）。
  const dirtyDomains = Array.from(ui.dirtyDomains || []);
  if (!dirtyDomains.length) {
    setSaveState("没有要保存的改动");
    toast("没有要保存的改动");
    return { ok: true, parts: [], warnings: [], editLost: false, skipped: true };
  }
  const payloads = {
    world: JSON.parse(JSON.stringify(ui.config.world || {})),
    schedules: JSON.parse(JSON.stringify(ui.config.schedules || {})),
    sessions: JSON.parse(JSON.stringify(ui.config.sessions || [])),
  };
  ui.saving = true;
  setSaveState("保存中…");
  // 逐段回执：世界 / 日程 / 会话各自成败，**不再用一句"保存失败"盖住已经生效的那段**
  const parts = [];
  const warnings = [];
  try {
    const allAttempts = [
      ["world", "世界配置", () => apiPost("config/world", { world: payloads.world })],
      ["schedules", "日程", () => apiPost("config/schedules", { schedules: payloads.schedules })],
      ["sessions", "会话", () => apiPost("config/sessions", { sessions: payloads.sessions })],
    ];
    const attempts = allAttempts.filter(([domain]) => dirtyDomains.includes(domain));
    let scheduleFilled = [];
    for (const [domain, label, call] of attempts) {
      try {
        const result = await call();
        if (result && result.ok === false) {
          parts.push({ domain, label, ok: false, error: String(result.reason || "被拒绝") });
          continue;
        }
        parts.push({ domain, label, ok: true, warnings: (result && result.warnings) || [] });
        if (result && Array.isArray(result.warnings)) warnings.push(...result.warnings);
        if (domain === "schedules" && result && Array.isArray(result.filled)) {
          scheduleFilled = result.filled;
        }
      } catch (error) {
        parts.push({ domain, label, ok: false, error: String((error && error.message) || error) });
      }
    }
    const failed = parts.filter((item) => !item.ok);
    // 三段都失败就不必热加载（没东西生效）
    let reloadWarnings = [];
    if (parts.some((item) => item.ok)) {
      try {
        const reloadResult = await apiPost("reload", {});
        reloadWarnings = (reloadResult && reloadResult.warnings) || [];
      } catch (error) {
        parts.push({
          domain: "reload",
          label: "热加载",
          ok: false,
          error: String((error && error.message) || error),
        });
      }
    }
    warnings.push(...toolActionWarnings());
    // 成功的域从"脏"里移掉；失败 / 没提交的留着重试
    const savedOk = new Set(
      parts.filter((item) => item.ok && item.domain !== "reload").map((item) => item.domain),
    );
    Array.from(ui.dirtyDomains || []).forEach((key) => {
      if (savedOk.has(key)) ui.dirtyDomains.delete(key);
    });
    // 脏标记只在"提交之后没人再改"时才清；期间改过的保留，绝不 loadAll 覆盖
    if (!failed.length && (ui.editRevision || 0) === revision) {
      ui.dirty = false;
      ui.dirtyTabs.clear();
      ui.dirtyDomains.clear();
    } else if (failed.length) {
      ui.dirty = true;
      dirtyDomains.forEach((key) => ui.dirtyDomains.add(key));
    }
    renderSettingsTabs();
    const submitted = parts.filter((item) => item.domain !== "reload").map((item) => item.label);
    const summary = failed.length
      ? `保存失败：${failed.map((item) => `${item.label}（${item.error}）`).join("；")}` +
        (parts.some((item) => item.ok)
          ? `；已生效：${parts.filter((item) => item.ok).map((item) => item.label).join("、")}`
          : "")
      : `已保存并生效（${submitted.join("、")}）`;
    setSaveState(summary);
    // 缩图与动图是"下一张才生效"的：保存成功后单独说一句，别让人以为页面上写着就已经变了
    // 保存失败时保留标记，等真的写进配置再提示一次
    const memeScalePending = !!ui.memeScalePending && !failed.length;
    if (!failed.length) ui.memeScalePending = false;
    toast(
      failed.length
        ? summary
        : warnings.length
          ? `已保存并生效，提醒：${warnings.join("；")}`
          : scheduleFilled.length
            ? `已保存并生效，同时补全了这些步骤的意图：${scheduleFilled.join("；")}`
            : reloadWarnings.length
              ? `已保存并生效，提醒：${reloadWarnings.join("；")}`
              : memeScalePending
                ? "已保存，下一张表情包生效"
                : "已保存并生效",
    );
    // 只在"提交之后没人再改"时才整体重载：否则会把刚打的新内容冲掉
    if ((ui.editRevision || 0) === revision) {
      // 保存后不许因为「还有旧配置可升级」再弹一次窗：这一批条目进站时已经问过了
      await loadAll({ promptMigrations: false });
      // 临时会话的规则存在 world 里：保存完顺手把列表一起回显（到期状态/轮数）
      await loadTempSessions();
    }
    return { ok: !failed.length, parts, warnings, editLost: (ui.editRevision || 0) !== revision };
  } finally {
    ui.saving = false;
  }
}

/**
 * 由系统维护的工具型动作（``HIDDEN_ACTION_IDS``）不进「还没选工具」这类提醒。
 *
 * 这两个动作在动作库里是**藏起来**的（见 ``visibleActions``），工具统一在
 * 「设置 → 高级功能 → 联网搜索」里配，引擎优先读全局的 ``world.search``、只有全局为空
 * 才回退到动作上残留的字段（``core/engine.py`` 的 ``search_option``）。
 * 所以对用户说「这些工具型动作还没选工具：用手机看网页」既没法照做、又已经过时了。
 */
function isManagedToolAction(action) {
  return !!action && HIDDEN_ACTION_IDS.has(String(action.id || ""));
}

/** 工具型动作必须选工具；没选的在这里统一提醒（仍然允许保存，运行时会跳过并写日志）。 */
function toolActionWarnings() {
  // 停用的动作运行时会被直接跳过，别再拿它来烦用户
  const live = actions().filter(
    (action) => action.enabled !== false && !isManagedToolAction(action),
  );
  const missing = live
    .filter(
      (action) =>
        action.llm_level === "tool" &&
        !actionToolNames(action).length &&
        !usesGlobalSearch(action),
    )
    .map((action) => action.name || action.id);
  const follows = live.filter(
    (action) => action.llm_level === "tool" && usesGlobalSearch(action),
  );
  const noCommand = live
    .filter((action) => action.llm_level === "command" && !String(action.trigger_command || "").trim())
    .map((action) => action.name || action.id);
  const warnings = [];
  if (missing.length) warnings.push(`这些工具型动作还没选工具，会被跳过：${missing.join("、")}`);
  if (follows.length && !effectiveSearchTools().length) {
    warnings.push(
      `这些动作写着「${GLOBAL_SEARCH_LABEL}」，但联网搜索里还没配工具，会被跳过：` +
        follows.map((action) => action.name || action.id).join("、"),
    );
  }
  if (noCommand.length) {
    warnings.push(`这些指令型动作还没填要触发的指令，会被跳过：${noCommand.join("、")}`);
  }
  return warnings.concat(missingToolActionWarnings());
}

/**
 * 工具型动作里，配的工具在 AstrBot 里一个都不存在的那些。
 *
 * 「找不到工具」只会体现在运行时的跳过日志里，用户不点开日志根本不知道，
 * 所以在状态页和动作页都挂一条横幅直接说出来。
 */
function missingToolActions() {
  const installed = new Set((ui.tools || []).map((item) => item.name));
  const installedList = Array.from(installed);
  // 只配了一个工具的动作允许按前缀对上（官方搜索工具叫 web_search_tavily 这类）
  const usable = (names) => {
    if (!names.length) return false;
    if (names.some((name) => installed.has(name))) return true;
    if (names.length > 1) return false;
    const wanted = String(names[0]).toLowerCase();
    return wanted.length >= 5 && installedList.some((name) => name.toLowerCase().startsWith(wanted));
  };
  return actions()
    .filter((action) => action.enabled !== false)
    .filter((action) => action.llm_level === "tool")
    // 系统维护的那两个动作在动作库里根本不存在，让人「去动作里换工具」是无解的；
    // 它们的工具在「联网搜索」里配，那边自己会标出没装上的工具
    .filter((action) => !isManagedToolAction(action))
    // 写着「跟随全局联网搜索」的动作不在这一条里报：全局为空时上面那条提醒更准确
    .filter((action) => !usesGlobalSearch(action))
    .map((action) => {
      const names = actionToolNames(action);
      const fallbacks = Array.isArray(action.tool_fallbacks)
        ? action.tool_fallbacks.filter(Boolean)
        : [];
      const all = Array.from(new Set(names.concat(fallbacks)));
      if (!all.length) return null; // 一个工具都没选：上面那条提醒负责
      if (usable(names) || fallbacks.some((name) => usable([name]))) return null;
      return { name: action.name || action.id, tools: all };
    })
    .filter(Boolean);
}

function missingToolActionWarnings() {
  const missing = missingToolActions();
  if (!missing.length) return [];
  const detail = missing
    .map((item) => `${item.name}（${item.tools.join(" / ")}）`)
    .join("；");
  return [
    `有 ${missing.length} 个工具型动作对应的工具在 AstrBot 里不存在，点了会被跳过：${detail}`,
  ];
}

/**
 * 状态页 / 动作页 / 调试页的横幅：工具缺失 + 读配置时的提醒。
 *
 * 两样都是常驻的：读盘时的提醒（``config.warnings``）以前是一进站就 toast 一次、
 * 保存完再来一次，关不掉也看不完；现在跟「工具不存在」并排摆在横幅里，改好就消失。
 */
function renderToolWarnings() {
  const blocks = [];
  const missing = missingToolActions();
  if (missing.length) {
    blocks.push({
      title: `有 ${missing.length} 个工具型动作对应的工具不存在，执行时会被跳过`,
      detail: missing
        .map((item) => `${item.name} → ${item.tools.join(" / ")}`)
        .join("　·　"),
      foot: "在动作里把工具换成 AstrBot 里已经装好的那个（搜索、天气这类内置动作也带备选，装任意一个搜索工具就能用）。",
    });
  }
  const warnings = (ui.config && ui.config.warnings) || [];
  if (warnings.length) {
    blocks.push({
      title: `配置提醒（${warnings.length}）`,
      detail: warnings.join("　·　"),
      foot: "这些是读配置时发现的问题，改好对应那项就会消失。",
    });
  }
  const targets = [
    $("status-banner"),
    $("action-banner"),
    $("debug-banner"),
  ].filter(Boolean);
  targets.forEach((box) => {
    box.innerHTML = "";
    if (!blocks.length) {
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    blocks.forEach((block) => {
      box.appendChild(el("span", "banner-icon", "⚠"));
      const body = el("div", "banner-body");
      body.appendChild(el("div", "banner-title", block.title));
      body.appendChild(el("div", "banner-detail", block.detail));
      body.appendChild(el("div", "banner-hint", block.foot));
      box.appendChild(body);
    });
  });
}

/** 一个动作挂了哪些工具（新写法 tool_names 优先，兼容老的 tool_name）。 */
function actionToolNames(action) {
  const list = Array.isArray(action.tool_names) ? action.tool_names.filter(Boolean) : [];
  if (list.length) return list;
  return action.tool_name ? [action.tool_name] : [];
}

/**
 * 「用全局联网搜索」的假工具名（跟 `core/models.py` 的 ``GLOBAL_SEARCH_TOOL`` 一致）。
 *
 * 自己新建一个"联网搜索"动作时，工具名其实该跟着「设置 → 高级功能 → 联网搜索」走；
 * 以前那种"必须在这儿选一个具体工具"的要求既重复又会在全局换工具时失效。
 */
const GLOBAL_SEARCH_TOOL = "@global_search";
const GLOBAL_SEARCH_LABEL = "联网搜索（跟随全局设置）";

function usesGlobalSearch(action) {
  return actionToolNames(action).includes(GLOBAL_SEARCH_TOOL);
}

/**
 * 联网搜索**实际**会用到的工具：全局 ``world.search.tool_names`` 优先，
 * 全局为空才回退到 ``search_web`` 动作上残留的老字段（跟引擎的 ``search_option`` 一致）。
 *
 * 空数组 = 这项能力真的不可用（她查不了网上的东西）。
 */
function effectiveSearchTools(world = (ui.config && ui.config.world) || {}) {
  const search = world.search || {};
  const names = Array.isArray(search.tool_names) ? search.tool_names.filter(Boolean) : [];
  if (names.length) return names;
  const action = actions().find((item) => String(item.id || "") === "search_web");
  return action ? actionToolNames(action) : [];
}

/* ================================================================== */
/* 实时状态                                                             */
/* ================================================================== */

/** 手改群名片（走平台的 set_group_card）。 */
async function saveNickname() {
  const sessionId = $("status-session").value;
  const text = String($("status-nickname").value || "").trim();
  if (!sessionId) return;
  if (!text) {
    toast("名片不能为空");
    return;
  }
  try {
    const result = await apiPost("state/action", {
      session: sessionId,
      action: "set_nickname",
      text,
    });
    // 这一组里的每个群都会改一遍，回执里说清改了几个
    toast(result.ok ? result.note || "群名片已改" : result.reason || "改名片失败");
    refreshStatus();
  } catch (error) {
    toast(error.message || "改名片失败");
  }
}

/** 锁定 / 解锁 / 恢复原名。 */
async function nicknameAction(action) {
  const sessionId = $("status-session").value;
  if (!sessionId) return;
  try {
    const result = await apiPost("state/action", { session: sessionId, action });
    if (action === "refresh_nickname") {
      toast(result.ok ? `读到了她现在的群名片：「${result.card}」` : result.note || "没读到群名片");
    } else {
      toast(result.note || "已执行");
    }
    refreshStatus();
  } catch (error) {
    toast(error.message || "执行失败");
  }
}

/** 所有"选会话"的控件（顶栏那个是**唯一真相**，其余都跟它走）。 */
const SCOPE_SESSION_IDS = [
  "status-session",
  "memory-session",
  "debug-session",
  "log-session",
  "map-session",
  "schedule-session",
  "contacts-session",
];

function scopeSession() {
  const top = $("status-session");
  return top ? String(top.value || "") : "";
}

/**
 * 把"当前会话"同步到所有页面自己的选择器上。
 *
 * 为什么要有这一层（复核报告 P2-3）：以前顶栏选了 B，记忆页可能还停在 A——
 * 页面看着同一个会话，查的却是另一条。现在**只有一个 scope**：
 * 任何一个选择器改了，其余全部跟上，正在看的那一页立刻重读。
 */
function setScopeSession(value, sourceId = "") {
  const wanted = String(value || "");
  SCOPE_SESSION_IDS.forEach((id) => {
    if (id === sourceId) return;
    const select = $(id);
    if (!select) return;
    if (String(select.value || "") !== wanted) select.value = wanted;
  });
  updateScopeNote();
  return wanted;
}

/** 顶栏那一行小字：这个会话属于哪一档（角色组 / 具体会话）。 */
function updateScopeNote() {
  const note = $("scope-note");
  if (!note) return;
  const value = scopeSession();
  if (!value) {
    note.textContent = "";
    return;
  }
  // 组代表会话 → 说她"这一处"共享；组里的成员会话 → 说明它就是这条会话本身
  const group = typeof groupOwning === "function" ? groupOwning(value) : null;
  const label = (typeof sessionLabel === "function" && sessionLabel(value)) || value;
  if (group && String(group.representative || group.representative_session || "") === value) {
    note.textContent = `角色组共享（${group.name || "这一组"}）`;
  } else if (group) {
    note.textContent = `具体会话：${label}`;
  } else {
    note.textContent = `具体会话：${label}`;
  }
}

/** 换了会话之后，把**当前这一页**的数据重读一遍（别的页切过去时会自己按新会话读）。 */
function refreshScopePage() {
  // 导航是 `#tabs` 里一排 button[data-tab]（不是 .nav-item）
  const active = document.querySelector("#tabs button.active");
  const tab = active ? String(active.dataset.tab || "") : "";
  // 与 showTab() 里那张表保持同一套函数名（那些才是真正会按会话重读的入口）
  const loaders = {
    status: "refreshStatus",
    memories: "loadMemories",
    logs: "loadLogs",
    debug: "loadTools",
    contacts: "loadContacts",
    map: "loadOverview",
  };
  const name = loaders[tab];
  if (!name) return;
  const fn = window[name];
  if (typeof fn === "function") fn();
}

function renderSessionSelects() {
  const options = scopeOptions();
  SCOPE_SESSION_IDS.forEach((id) => {
    const select = $(id);
    if (!select) return;
    const previous = select.value;
    select.innerHTML = "";
    if (!options.length) {
      select.appendChild(option("", "（还没有白名单会话）"));
      return;
    }
    options.forEach((item) => select.appendChild(option(item.value, item.label)));
    select.value = options.some((item) => item.value === previous)
      ? previous
      : options[0].value;
  });
  // 全部先对齐到顶栏那一份，再刷新那句说明
  setScopeSession(scopeSession());
}

/**
 * 编辑器里所有"选会话"的地方都列这个：会话组 + 没进组的会话。
 *
 * ``withMembers``：连组里的成员会话也一起列（日程的落点需要——勾组是把话落在组代表那里，
 * 想指定"就说给这个群听"得能单独勾到它）。
 * ``onlyFor``：只列"这个会话那一组"的东西（它自己那个组 + 组里的会话）。
 * 日程的落点用它——落点只在"她这一处"里挑，跨组选了也没意义。
 */
function scopeOptions({ withMembers = false, onlyFor = "" } = {}) {
  const options = [];
  const grouped = new Set();
  const ownerOf = new Map();
  const scopeOwner = onlyFor ? groupOwning(onlyFor) : null;
  groups().forEach((group) => {
    if (onlyFor && (!scopeOwner || String(group.id) !== String(scopeOwner.id))) return;
    (group.sessions || []).forEach((id) => {
      grouped.add(id);
      ownerOf.set(id, group);
    });
    options.push({
      value: group.id,
      label: `组：${group.name || group.id}（${(group.sessions || []).length} 个会话）`,
      desc: (group.sessions || []).join("、") || "（还没有成员）",
    });
  });
  ui.sessions.forEach((session) => {
    const owner = ownerOf.get(session.session_id);
    if (onlyFor) {
      const belongs = owner && scopeOwner && String(owner.id) === String(scopeOwner.id);
      const isSelf = String(session.session_id) === String(onlyFor);
      if (!belongs && !isSelf) return;
    }
    if (owner && !withMembers) return;
    options.push({
      value: session.session_id,
      label: `${session.note ? session.note + " · " : ""}${session.session_id}`,
      desc:
        (session.type === "private" ? "私聊" : "群聊") +
        (owner ? ` · 属于「${owner.name || owner.id}」` : ""),
    });
  });
  // 兜底：onlyFor 指向一个已经不在名单里的会话（删过又加、id 变了）时，
  // 别给一个空列表让人没法选——退回"全都列出来"。
  if (onlyFor && !options.length) return scopeOptions({ withMembers });
  return options;
}

/**
 * 这里传进来的可能是**会话 id**，也可能是**组 id**。
 *
 * 「选会话」那一排下拉的第一个选项就是组（`options[0].value` = 组 id），
 * 只认会话 id 的话就会返回 null → 日程的落点列表直接空掉（删过会话又加回来时最容易撞上）。
 */
function groupOwning(sessionId) {
  const wanted = String(sessionId || "");
  if (!wanted) return null;
  const all = groups();
  return (
    all.find((group) => String(group.id) === wanted) ||
    all.find((group) =>
      (group.sessions || []).some((id) => String(id) === wanted),
    ) ||
    null
  );
}

const PLAN_SOURCES = {
  rule: "规则决策",
  llm: "大模型自己安排",
  schedule: "日程的后续步骤",
  forced: "极端保护（强制）",
};

/** 把计划渲染成人话，而不是一坨 JSON。 */
function formatPlan(plan) {
  if (!plan) return "（没有进行中的计划——她现在是自由状态，等下一轮 tick 再决定）";
  const lines = [];
  const source = PLAN_SOURCES[plan.source] || plan.source;
  if (source) lines.push(`来源：${source}`);
  if (plan.reason) lines.push(`原因：${plan.reason}`);
  const steps = plan.steps || [];
  const current = Number(plan.current_step || 0);
  if (steps.length) lines.push("步骤：");
  steps.forEach((step, index) => {
    const mark = index < current ? "✅" : index === current ? "▶" : "·";
    const definition = actions().find((item) => item.id === step.action);
    const name = definition ? definition.name || definition.id : step.action;
    const where = step.target_node ? ` → ${nodeLabel(step.target_node)}` : "";
    const what = (step.messages || []).length ? `：${step.messages.join(" / ")}` : "";
    lines.push(`${mark} ${name}${where}${what}`);
  });
  if (plan.valid_for) lines.push(`有效期：${Math.round(Number(plan.valid_for) / 60)} 分钟`);
  return lines.join("\n") || "（计划是空的）";
}

/** 数值条的颜色规则：精力越高越好，孤独/无聊越低越好，其余两种算中性。 */
const VALUE_TONES = {
  energy: "high",
  loneliness: "low",
  boredom: "low",
  curiosity: "neutral",
  affect: "neutral",
  valence: "polar",
  // 欲求是"想要多少"：高不算坏事，跟好奇心一样按中性上色
  desire: "neutral",
  // 睡眠压力跟孤独/无聊一样"越高越要留神"：0.40 有点困、0.70 快睡了
  sleep_pressure: "low",
};

function valueToneClass(key, value) {
  const tone = VALUE_TONES[key] || "neutral";
  if (tone === "neutral") return "neutral";
  // 换算成"该不该留神"：值越大越需要留神；
  // polar 是"以 0.5 为中性"的两极量（效价），只有偏负才需要留神。
  const worry =
    tone === "high" ? 1 - value : tone === "polar" ? Math.max(0, 0.5 - value) * 2 : value;
  if (worry < 0.4) return "good";
  if (worry < 0.7) return "mid";
  return "bad";
}

/** 「睡眠压力」那一条：不是常规数值（改不了、也不在 values 里），只在用得上的时候露出来。 */
const SLEEP_ROW = {
  key: "sleep_pressure",
  label: "睡眠压力",
  hint: "攒够了就撑不住要睡：0.40 有点困、0.70 快睡了；睡一觉会落下来。",
};

/** 什么时候才显示这一条：在夜间睡眠时段里，或者已经困了（压力 ≥ 0.40，即"有点困"档）。 */
function sleepRowVisible(data) {
  const sleep = (data || {}).sleep || {};
  const night = (data || {}).night || {};
  const pressure = num(sleep.pressure, 0);
  return Boolean(night.now) || Boolean(night.sleeping) || pressure >= 0.4;
}

function renderValueRows(values, data) {
  const box = $("status-values");
  if (!box) return;
  box.innerHTML = "";
  ui.valueDraft = {};
  ATTRS.forEach((attr) => {
    const value = num(values[attr.key], 0.5);
    const row = el("div", "value-row");
    // 语义色调交给 CSS 上色：数值大号字、进度条发光都跟着它走
    row.classList.add(`tone-${valueToneClass(attr.key, value)}`);
    row.appendChild(el("span", "value-label", attr.label));
    if (ui.valuesEdit) {
      const slider = document.createElement("input");
      slider.type = "range";
      slider.min = "0";
      slider.max = "1";
      slider.step = "0.01";
      slider.value = String(value);
      slider.title = attr.hint || "";
      const readout = el("span", "value-num", value.toFixed(2));
      slider.addEventListener("input", () => {
        const next = num(slider.value, 0.5);
        ui.valueDraft[attr.key] = next;
        readout.textContent = next.toFixed(2);
      });
      ui.valueDraft[attr.key] = value;
      row.appendChild(slider);
      row.appendChild(readout);
    } else {
      const bar = el("div", "value-bar");
      const fill = el("div", `value-fill ${valueToneClass(attr.key, value)}`);
      fill.style.width = `${Math.round(value * 100)}%`;
      bar.appendChild(fill);
      bar.title = attr.hint || "";
      row.appendChild(bar);
      row.appendChild(el("span", "value-num", value.toFixed(2)));
    }
    box.appendChild(row);
  });

  // 睡眠压力：接在那几条常规数值下面；不该显示的时候整条不进 DOM
  if (data && sleepRowVisible(data)) {
    const pressure = num((data.sleep || {}).pressure, 0);
    const tone = valueToneClass(SLEEP_ROW.key, pressure);
    const row = el("div", "value-row");
    row.classList.add(`tone-${tone}`);
    row.appendChild(el("span", "value-label", SLEEP_ROW.label));
    const bar = el("div", "value-bar");
    const fill = el("div", `value-fill ${tone}`);
    fill.style.width = `${Math.round(pressure * 100)}%`;
    bar.appendChild(fill);
    bar.title = SLEEP_ROW.hint;
    row.appendChild(bar);
    row.appendChild(el("span", "value-num", pressure.toFixed(2)));
    box.appendChild(row);
  }

  if (ui.valuesEdit) {
    const row = el("div", "row-item");
    const save = el("button", "ghost", "保存数值");
    save.type = "button";
    save.title = "把上面的数值写回她的状态（会记一条日志）";
    save.addEventListener("click", async () => {
      const sessionId = $("status-session").value;
      if (!sessionId) return;
      try {
        await apiPost("state/action", {
          session: sessionId,
          action: "set_values",
          values: ui.valueDraft,
        });
        ui.valuesEdit = false;
        toast("数值已更新");
        refreshStatus();
      } catch (error) {
        toast(error.message || "保存失败");
      }
    });
    const cancel = el("button", "ghost", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", () => {
      ui.valuesEdit = false;
      refreshStatus();
    });
    row.appendChild(save);
    row.appendChild(cancel);
    box.appendChild(row);
  }

  const button = $("values-edit");
  if (button) button.textContent = ui.valuesEdit ? "完成" : "编辑数值";
  const hint = $("status-values-hint");
  if (hint) {
    hint.textContent = ui.valuesEdit
      ? "改完点「保存数值」写回状态。"
      : "可以点击右上角「编辑数值」进行修改。";
  }
}

/** 状态卡的一行：左边一个灰色小标签，右边是内容。 */
function statusLine(box, label, value, tone = "") {
  const row = el("div", `status-line${tone ? ` ${tone}` : ""}`);
  if (label) row.appendChild(el("span", "status-key", label));
  const text = el("span", "status-val");
  text.textContent = value;
  row.appendChild(text);
  box.appendChild(row);
  return row;
}

/** 状态卡里带进度条的一行：进度条 + 后面的说明文字。 */
function statusMeter(box, label, ratio, note) {
  const row = el("div", "status-line");
  row.appendChild(el("span", "status-key", label));
  const meter = el("div", "status-meter");
  const bar = el("div", "bar");
  const fill = document.createElement("i");
  const safe = Math.min(1, Math.max(0, Number(ratio) || 0));
  fill.style.width = `${Math.round(safe * 100)}%`;
  bar.appendChild(fill);
  meter.appendChild(bar);
  meter.appendChild(el("span", "status-note", note));
  row.appendChild(meter);
  box.appendChild(row);
  return row;
}

/** 「还有多久」说成人话。 */
function countdownText(minutes) {
  const value = Math.max(0, Math.round(Number(minutes) || 0));
  if (value < 1) return "马上就到";
  if (value < 60) return `还有 ${value} 分钟`;
  const hours = Math.floor(value / 60);
  const rest = value % 60;
  return rest ? `还有 ${hours} 小时 ${rest} 分` : `还有 ${hours} 小时`;
}

/** 「多久以前」说成人话。 */
function agoText(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  if (value < 5) return "刚刚";
  if (value < 60) return `${value} 秒前`;
  if (value < 3600) return `${Math.floor(value / 60)} 分钟前`;
  return `${Math.floor(value / 3600)} 小时前`;
}

/** 「多长时间」说成人话（不加"还有"）。 */
function durationText(minutes) {
  const value = Math.max(0, Math.round(Number(minutes) || 0));
  if (value < 60) return `${value} 分钟`;
  const hours = Math.floor(value / 60);
  const rest = value % 60;
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
}

/** 一个动作 / 一步计划显示成活的名字。 */
function actionLabel(id) {
  if (!id) return "";
  const definition = actions().find((item) => item.id === id);
  return definition ? definition.name || definition.id : id;
}

/** 左栏「当前状态」：没选会话时的占位。 */
function setStatusEmpty(text) {
  $("status-head").innerHTML = "";
  renderHero(null, "");
  ["status-time", "status-progress", "status-runtime"].forEach((id) => {
    const box = $(id);
    if (!box) return;
    box.innerHTML = "";
    statusLine(box, "", text, "muted");
  });
  $("status-warn").innerHTML = "";
}

/**
 * 人物面板顶部：头像取名字首字，下面是「名字 + 一行摘要」。
 * 数据全部来自已有的状态快照，不额外请求。
 */
/** 状态页顶部头像：有 QQ 号就拉真头像，取不到退回名字首字（用户 m14014）。
 *  结果按 URL 记在内存里，来回切会话不重复请求。
 */
const HERO_AVATAR_CACHE = new Map();

function fillHeroAvatar(box, qq, label) {
  if (!box) return;
  const initial = label ? Array.from(label)[0] : "？";
  box.textContent = initial;
  box.classList.remove("photo");
  box.style.backgroundImage = "";
  if (!/^\d{5,12}$/.test(qq)) return;
  const url = `https://q1.qlogo.cn/g?b=qq&nk=${qq}&s=100`;
  const state = HERO_AVATAR_CACHE.get(url);
  const apply = () => {
    box.textContent = "";
    box.classList.add("photo");
    box.style.backgroundImage = `url("${url}")`;
  };
  if (state === true) {
    apply();
    return;
  }
  if (state === false) return;
  HERO_AVATAR_CACHE.set(url, "loading");
  const img = new Image();
  img.onload = () => {
    HERO_AVATAR_CACHE.set(url, true);
    apply();
  };
  img.onerror = () => {
    HERO_AVATAR_CACHE.set(url, false);
  };
  img.src = url;
}

function renderHero(data, stateLabel) {
  const avatar = $("hero-avatar");
  const nameBox = $("hero-name");
  const subBox = $("hero-sub");
  if (!avatar || !nameBox || !subBox) return;
  const kpiBox = $("hero-kpis");
  if (kpiBox) kpiBox.innerHTML = "";
  if (!data) {
    avatar.textContent = "？";
    nameBox.textContent = "（还没选会话）";
    subBox.textContent = "先去「会话」页把群或私聊加进白名单。";
    return;
  }
  const base = String(data.nickname_base || "").trim();
  const nickname = String(data.nickname || "").trim();
  const label = base || sessionShortName(data.session_id || "") || nickname;
  // 她自己有 QQ 号就用真头像（用户 m14014），没有 QQ 号（合成身份）才退回名字首字。
  fillHeroAvatar(avatar, String(data.self_id || "").trim(), label);
  nameBox.textContent = label || "（还没设置群名片）";
  const action = data.current_action || {};
  const where = [data.zone_name, data.node_name].filter(Boolean).join(" · ");
  const doing = action.desc || (action.type ? actionLabel(action.type) : "");
  subBox.textContent = [
    stateLabel || data.state || "",
    where,
    doing ? `在做：${doing}` : "没在做什么",
    data.mood ? `心情 ${data.mood}` : "",
  ]
    .filter(Boolean)
    .join(" ｜ ");

  // 人物面板下方四张指标卡：一眼看全"在哪、在做什么、心情、下一个节点"
  if (!kpiBox) return;
  const next = data.next_schedule || null;
  [
    { label: "地点", value: where || "（未知）", tone: "accent" },
    { label: "正在做", value: doing || "没在做什么", tone: "accent" },
    { label: "心情", value: data.mood || "（未记录）", tone: "violet" },
    {
      label: "下一条日程",
      value: next ? `${next.time} ${next.actions || ""}`.trim() : "没有启用的日程",
      note: next ? countdownText(next.in_minutes) : "",
      tone: "gold",
    },
  ].forEach((item) => {
    const tile = el("div", `kpi kpi-${item.tone}`);
    tile.appendChild(el("span", "kpi-label", item.label));
    tile.appendChild(el("span", "kpi-value", item.value));
    if (item.note) tile.appendChild(el("span", "kpi-note", item.note));
    // 卡片里放不下时折两行；再长就让鼠标悬停看全文（同时给 title 兜底）
    const full = [item.value, item.note].filter(Boolean).join(" · ");
    if (Array.from(full).length > 14) {
      tile.dataset.full = full;
      tile.title = full;
    }
    kpiBox.appendChild(tile);
  });
}

/**
 * 左栏「当前状态」三小节：
 *   时间与日程 → 现在几点/时段、世界时间对照、下一条日程倒计时
 *   正在进行   → 当前动作进度、计划进度、区域 · 地点
 *   互动与运行 → 最近活跃的人、未回应的群聊、刚聊过什么、本小时额度、通道状态
 */
function renderStatusSections(data, stateLabel) {
  const head = $("status-head");
  head.innerHTML = "";
  const style = data.style || {};
  const styleCell = String(style.cell || "").trim();
  const cellLabels = {
    "calm+positive": "舒坦",
    "calm+neutral": "平静",
    "calm+negative": "低落",
    "stirred+positive": "轻快",
    "stirred+neutral": "平静",
    "stirred+negative": "不痛快",
    "excited+positive": "兴奋",
    "excited+neutral": "心潮起伏",
    "excited+negative": "恼火",
  };
  // 后端给的格子机器名是 aXvY（心潮档 × 效价档）；上面那张表是语义别名，
  // 认得就用别名，认不出就按档位名拼一个，别把 a2v2 这种原样甩给用户
  const cellLabel = (key) => {
    if (!key) return "";
    if (cellLabels[key]) return cellLabels[key];
    const hit = /^a([0-4])v([0-4])$/.exec(key);
    if (!hit) return key;
    const arousal = ["静", "平", "微起", "起", "激动"][Number(hit[1])];
    const valence = ["很差", "偏差", "一般", "偏好", "很好"][Number(hit[2])];
    return [arousal, valence].filter(Boolean).join(" · ") || key;
  };
  const dayMood = data.day_mood || {};
  [
    `状态：${stateLabel}`,
    `心情：${data.mood}`,
    dayMood.label
      ? `今天的调子：${dayMood.label}${dayMood.enabled === false ? "（已关，不影响数值）" : ""}`
      : "",
    styleCell
      ? `这一轮：${cellLabel(styleCell)}${
          Number(style.say_limit || 0) ? ` · 最多 ${style.say_limit} 条` : ""
        }${data.storm ? " · 正在气头上" : ""}`
      : "",
    `tick：${data.world_time}`,
    `名片：${data.nickname || "（未设置）"}`,
  ]
    .filter(Boolean)
    .forEach((text) => head.appendChild(el("span", "chip", text)));
  $("status-warn").innerHTML = "";

  // ---------------- 时间与日程 ----------------
  const timeBox = $("status-time");
  timeBox.innerHTML = "";
  const clock = String(data.clock_text || "").replace(/^现在是：/, "");
  statusLine(timeBox, "现在", clock || "（读不到系统时间）");
  const tickSeconds = Math.max(1, Math.round(Number(data.tick_seconds || 60)));
  statusLine(
    timeBox,
    "世界时间",
    `第 ${data.world_time} 个 tick（1 tick ≈ ${tickSeconds} 秒）· 已推进 ${
      data.world_elapsed_text || "0 分钟"
    }`,
  );
  const next = data.next_schedule;
  if (next) {
    statusLine(
      timeBox,
      "下一条日程",
      `${countdownText(next.in_minutes)} · ${next.weekday} ${next.time} · ${
        next.actions || "（没配动作）"
      }${next.auto_travel ? " · 自动先走过去" : ""}`,
    );
  } else {
    statusLine(timeBox, "下一条日程", "没有启用的日程", "muted");
  }
  // 别的插件留下的状态（今日穿搭、背包…）：记在她这儿的状态槽
  const slots = data.external_state || {};
  const slotKeys = Object.keys(slots);
  if (slotKeys.length) {
    slotKeys.forEach((key) => {
      const info = slots[key] || {};
      const label = String(info.label || key);
      const text = String(info.text || "").trim();
      const at = Number(info.at || 0);
      const when = at ? `（${agoText(Math.max(0, Date.now() / 1000 - at))}拿到的）` : "";
      statusLine(timeBox, label, `${text}${when}`.trim() || "（空）", "muted");
    });
  }

  // ---------------- 今天做过的事（今日记） ----------------
  const journalBox = $("status-journal");
  journalBox.innerHTML = "";
  const journal = data.today_journal || {};
  const journalLines = journal.lines || [];
  if (journalLines.length) {
    statusLine(journalBox, "做了", `${journalLines.length} 件事`);
    journalLines.forEach((row) => {
      statusLine(journalBox, String(row.part || ""), String(row.text || ""));
    });
  } else {
    statusLine(journalBox, "", "今天还没做什么", "muted");
  }
  if (Number(journal.failed || 0) > 0) {
    statusLine(journalBox, "没做成", `${Number(journal.failed)} 次`, "muted");
  }
  const journalOpen = journal.open || [];
  const journalChanges = journal.changes || [];
  journalOpen.forEach((text) => statusLine(journalBox, "还没做完", String(text), "muted"));
  journalChanges.forEach((text) => statusLine(journalBox, "中途变了", String(text), "muted"));

  // ---------------- 正在进行 ----------------
  const progressBox = $("status-progress");
  progressBox.innerHTML = "";
  const action = data.current_action || {};
  if (action.type) {
    const done = Number(action.elapsed_ticks || 0);
    const total = Math.max(1, Number(action.duration_ticks || 1));
    const label = action.desc || actionLabel(action.type) || action.type;
    const totalText = durationText(Math.round((total * tickSeconds) / 60));
    const doneText = durationText(Math.round((done * tickSeconds) / 60));
    statusMeter(
      progressBox,
      "当前动作",
      done / total,
      `${label}（${doneText} / ${totalText}，${done}/${total} tick${
        action.interruptible === false ? "，不可打断" : ""
      }）`,
    );
  } else {
    statusMeter(progressBox, "当前动作", 0, "没在做什么");
  }
  const plan = data.current_plan;
  const steps = plan && plan.steps ? plan.steps : [];
  if (steps.length) {
    const index = Math.min(Number(plan.current_step || 0), steps.length);
    const step = steps[Math.min(index, steps.length - 1)] || {};
    const where = step.target_node ? ` → ${nodeLabel(step.target_node)}` : "";
    statusMeter(
      progressBox,
      "计划进度",
      index / steps.length,
      `第 ${Math.min(index + 1, steps.length)}/${steps.length} 步：${actionLabel(
        step.action,
      )}${where}`,
    );
  } else {
    statusMeter(progressBox, "计划进度", 0, "没有进行中的计划");
  }
  const zoneName = data.zone_name || "（未归属区域）";
  const nodeName = data.node_name || data.node_id || "未知";
  statusLine(progressBox, "区域 · 地点", `${zoneName} · ${nodeName}`);
  const travel = data.travel || [];
  statusLine(
    progressBox,
    "可以走到",
    travel.length
      ? travel.map((item) => `${item.name} ${item.ticks} tick`).join("、")
      : "（这里没有连到别的地方）",
    travel.length ? "" : "muted",
  );

  // ---------------- 互动与运行 ----------------
  const runtimeBox = $("status-runtime");
  runtimeBox.innerHTML = "";
  const presence = data.user_presence || [];
  const tickAgo = (item) => {
    const ticks = Number(data.world_time || 0) - Number(item.world_time || 0);
    return ticks > 0 ? `${durationText(Math.round((ticks * tickSeconds) / 60))}前` : "刚刚";
  };
  // 一行最多摆这么多人：再多就变成一堵墙，想知道全量去看日志
  const presenceShown = 4;
  const presenceTotal = Math.max(Number(data.user_presence_total || 0), presence.length);
  const presenceText = presence.length
    ? presence
        .slice(0, presenceShown)
        .map((item) => `${item.name || item.user_id}（${tickAgo(item)}）`)
        .join("、") +
      (presenceTotal > Math.min(presence.length, presenceShown)
        ? `　等 ${presenceTotal} 人`
        : "")
    : "还没人跟她说过话";
  statusLine(
    runtimeBox,
    `最近活跃${presenceTotal ? `（共 ${presenceTotal} 人，显示 ${Math.min(
      presence.length,
      presenceShown,
    )} 个）` : ""}`,
    presenceText,
    presence.length ? "" : "muted",
  );
  statusLine(
    runtimeBox,
    "未回应",
    `本会话 ${Number(data.chat_unreplied_count || 0)} 行没回、${
      Number(data.chat_replied_count || 0)
    } 行已回（留档 ${data.chat_history_count || 0} 条）`,
  );
  const chatNote = String(data.chat_note || "").trim();
  statusLine(
    runtimeBox,
    "刚聊过",
    chatNote || "（还没记下之前聊过什么）",
    chatNote ? "" : "muted",
  );
  statusLine(
    runtimeBox,
    "决策意愿",
    `${Number(data.willingness || 0).toFixed(2)}（这一轮问大模型的概率 ${Math.round(
      Number(data.llm_sample_rate || 0) * 100,
    )}%）`,
  );
  // 插话被哪道闸拦住：光看频率看不出瓶颈在哪
  const interjectLabels = {
    allowed: "开口",
    cooldown: "插话冷却",
    engage: "无人回应冷却",
    hourly: "每小时上限",
    reply_cd: "刚回过话",
    disabled: "插话已关闭",
  };
  const interject = data.interject || {};
  const interjectParts = Object.keys(interject).map(
    (key) => `${interjectLabels[key] || key} ${interject[key]}`,
  );
  if (interjectParts.length) {
    statusLine(
      runtimeBox,
      "插话（本小时）",
      `想插话时被拦住：${interjectParts.join(" · ")}`,
    );
  }
  // 工具被熔断时摆出来，并给一个"立即重试"，不用等退避结束
  const breakers = data.tool_breakers || [];
  if (breakers.length) {
    const box = el("div", "status-line");
    box.appendChild(el("span", "status-key", "暂不可用"));
    const body = el("span", "status-val");
    breakers.forEach((item, index) => {
      if (index) body.appendChild(el("span", "", "　"));
      const text = `${item.tool}（剩 ${item.seconds_left} 秒）`;
      body.appendChild(el("span", "", text));
      const retry = el("button", "ghost small", "重试");
      retry.type = "button";
      retry.title = item.reason || "立即解除这个工具的熔断";
      retry.addEventListener("click", async () => {
        try {
          await apiPost("state/action", {
            session: data.session_id,
            action: "reset_tools",
            tool: item.tool,
          });
          toast(`${item.tool} 已解除，可以再试`);
          refreshStatus();
        } catch (error) {
          toast(error.message || "解除失败");
        }
      });
      body.appendChild(retry);
    });
    box.appendChild(body);
    runtimeBox.appendChild(box);
  }
  const budget = data.budget || {};
  const budgetParts = [
    ["计划", "plan"],
    ["回话", "text"],
    ["补参", "tool_param"],
    ["分享", "share"],
    ["自主", "autonomous"],
  ]
    .filter(([, key]) => budget[key])
    .map(([label, key]) => `${label} ${budget[key].left}/${budget[key].limit}`);
  statusLine(
    runtimeBox,
    "本小时额度",
    budgetParts.length ? budgetParts.join(" · ") : "（没有额度配置）",
    budgetParts.length ? "" : "muted",
  );
  const chatMood = data.chat_mood || {};
  if (chatMood.daily_cap) {
    const spent = Number(chatMood.spent || 0);
    statusLine(
      runtimeBox,
      "聊天的情绪额度",
      `今天 ${spent >= 0 ? "+" : ""}${spent.toFixed(3)} / ${Number(chatMood.daily_cap).toFixed(2)}`
        + `（单轮最多 ${Number(chatMood.turn_cap || 0).toFixed(3)}）`
        + "｜日常聊天改的是好感度，心情的量程留给真发生的事",
      "",
    );
  }
  const miss = data.miss || {};
  const missPeople = miss.people || [];
  if (missPeople.length || miss.push_cap) {
    const parts = missPeople.slice(0, 3).map((item) => {
      if (item.waiting) {
        return `${item.name}（刚聊过，${item.ready_in_minutes} 分钟后才会开始想）`;
      }
      const flag = item.will_reach_out ? "，到点了会去找" : "";
      return `${item.name} ${Number(item.value || 0).toFixed(2)}${flag}`;
    });
    const quota = miss.push_cap
      ? `今天还能主动找 ${Number(miss.push_left || 0)}/${miss.push_cap} 次`
      : "";
    statusLine(
      runtimeBox,
      "想找的人",
      (parts.length ? parts.join(" · ") : "（暂时没惦记谁）")
        + `｜阈值 ${Number(miss.threshold || 0).toFixed(2)} 就会主动去找`
        + (quota ? `｜${quota}` : "")
        + (miss.enabled === false ? "｜（主动找人已关闭）" : ""),
      "",
    );
  }
  const topics = data.open_topics || [];
  if (topics.length) {
    statusLine(
      runtimeBox,
      "还没聊完的",
      topics
        .map(
          (item) =>
            `${item.who_name || item.who || "某人"}：${item.text}`
            + (Number(item.asked) ? `（已问 ${item.asked} 次）` : ""),
        )
        .join("；"),
      "",
    );
  }
  const channel = data.channel || {};
  const blocked = Number(channel.send_blocked_seconds || 0);
  statusLine(
    runtimeBox,
    "发送通道",
    blocked
      ? `⚠ 刚发送失败，${blocked} 秒内不再尝试（失败不重发）`
      : "正常",
    blocked ? "warn" : "",
  );
  const lastLLM = channel.last_llm || {};
  let llmText = "还没调用过";
  let llmTone = "muted";
  if (channel.has_llm === false) {
    llmText = "没有接上模型";
    llmTone = "warn";
  } else if (lastLLM.at) {
    llmText = `${agoText(lastLLM.ago_seconds)}调用${
      lastLLM.ok ? "成功" : `失败：${lastLLM.error || "未知原因"}`
    }`;
    llmTone = lastLLM.ok ? "" : "warn";
  }
  statusLine(
    runtimeBox,
    "模型通道",
    `${channel.llm_provider ? channel.llm_provider : "跟随当前会话供应商"} · ${llmText}`,
    llmTone,
  );
  renderAgency(data);
}

/* ---------------- T12 Step 2：她自己的打算 ---------------- */

const AGENCY_MODE_TONES = {
  comply: "",
  negotiate: "warn",
  ask_first: "warn",
  partial: "warn",
};

/**
 * 「她自己的打算」卡：当前那条打算（来源 / 强度 / 还剩多久）+ 最近十次选择。
 *
 * 读的是 `/state` 快照里现成的 `agency` 字段（后端 `engine.agency_section` 算好的），
 * 没有新增接口。整块在「正在进行」那一栏下面，跟着 `renderStatusSections` 每次重画；
 * 后端没给（老版本 / 读失败）就画一句"暂时没有"，不报错、不占位。
 */
function renderAgency(data) {
  const progress = $("status-progress");
  const card = progress ? progress.closest(".status-card") : null;
  if (!card) return;
  let block = card.querySelector(".agency-block");
  if (!block) {
    block = el("section", "status-block agency-block");
    block.appendChild(el("h3", "", "她自己的打算"));
    const host = progress.closest(".status-block");
    if (host && host.parentNode === card) card.insertBefore(block, host.nextSibling);
    else card.appendChild(block);
  }
  // 只留标题：正文每次重画（不能每次新建一块，不然卡片会越堆越多）
  while (block.children.length > 1) block.removeChild(block.lastChild);
  const body = el("div", "status-lines");
  block.appendChild(body);
  const agency = data && data.agency ? data.agency : {};
  const intention = agency.intention || null;
  if (intention && intention.text) {
    statusLine(body, "当前打算", String(intention.text), "");
    statusLine(
      body,
      "来路",
      `${intention.origin_label || "来路不明"} · ${intention.state_label || "刚立下"}`,
      "",
    );
    statusMeter(
      body,
      "还剩多少劲",
      num(intention.strength, 0),
      `${intention.strength_label || "说不清"}${
        Number(intention.expires_at || 0)
          ? `｜还有 ${formatCountdown(intention.expires_at)}`
          : ""
      }`,
    );
    if (intention.alternative) {
      statusLine(body, "另一条路", String(intention.alternative), "muted");
    }
  } else {
    statusLine(body, "当前打算", "（暂时没有，手头没什么非做不可的事）", "muted");
  }
  statusLine(
    body,
    "本周让位",
    `${num(agency.yield_week, 0)} 次（一共 ${num(agency.yield_total, 0)} 次没照做）`,
    "",
  );
  const recent = Array.isArray(agency.recent) ? agency.recent.slice(0, 10) : [];
  if (!recent.length) {
    statusLine(body, "最近的选择", "（还没记下什么）", "muted");
    return;
  }
  const list = el("div", "agency-recent");
  recent.forEach((item) => list.appendChild(agencyRow(item)));
  body.appendChild(list);
}

/** 一条选择 / 一次意图收尾画成一行：左边是个小牌，右边是他当时让她做的事。 */
function agencyRow(item) {
  const row = el("div", "agency-row");
  const at = num(item.at, 0);
  const when = at ? agoText(Math.max(0, Date.now() / 1000 - at)) : "";
  if (item.kind === "intention") {
    const tag = el("span", "agency-tag", item.state_label || "收尾");
    row.appendChild(tag);
    row.appendChild(el("span", "agency-text", item.text || "（那件事）"));
    row.appendChild(el("span", "agency-when", `${when}放下`));
    return row;
  }
  const mode = String(item.mode || "comply");
  const tag = el(
    "span",
    `agency-tag${AGENCY_MODE_TONES[mode] ? ` ${AGENCY_MODE_TONES[mode]}` : ""}`,
    item.mode_label || mode,
  );
  row.appendChild(tag);
  const text = item.request || "（他没说清楚要做什么）";
  row.appendChild(el("span", "agency-text", text));
  row.appendChild(
    el("span", "agency-when", item.changed ? `${when}真的改了动作` : when),
  );
  return row;
}

/* ---------------- 数值曲线：她的近期状态 ---------------- */

const HISTORY_WINDOWS = [
  { hours: 6, label: "6 小时" },
  { hours: 24, label: "24 小时" },
  { hours: 72, label: "3 天" },
];

function renderHistoryWindows() {
  const box = $("history-windows");
  if (!box) return;
  box.innerHTML = "";
  HISTORY_WINDOWS.forEach((item) => {
    const button = el("button", `pill${ui.historyHours === item.hours ? " on" : ""}`, item.label);
    button.type = "button";
    button.addEventListener("click", () => {
      ui.historyHours = item.hours;
      renderHistoryWindows();
      renderHistory();
    });
    box.appendChild(button);
  });
}

async function renderHistory() {
  const sessionId = $("status-session").value;
  const canvas = $("history-canvas");
  if (!canvas || !sessionId || ui.historyBusy) return;
  ui.historyBusy = true;
  let data = null;
  try {
    data = await apiGet("state/history", { session: sessionId, hours: ui.historyHours });
  } catch (error) {
    data = null;
  } finally {
    ui.historyBusy = false;
  }
  const points = (data && data.points) || [];
  ui.historyPoints = points;
  drawHistoryChart(canvas, points);
  const metrics = (data && data.metrics) || {};
  $("history-metrics").textContent = points.length
    ? `起伏 ${num(metrics.swing_per_hour, 0).toFixed(2)}/小时 · 峰值心潮 ${num(
        metrics.peak_arousal,
        0,
      ).toFixed(2)} · 低谷 ${Math.round(num(metrics.low_minutes, 0))} 分钟`
    : "还没有足够的数据";
  $("history-empty").classList.toggle("hidden", points.length >= 2);
}

/** 两条线：心潮（蓝）与效价（橙），虚线是效价的中性位。 */
function drawHistoryChart(canvas, points) {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, canvas.clientWidth || 640);
  const height = 184;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  // 图例放右上角：挤在左下角会和 0.0 这条网格线的刻度叠在一起
  const pad = { left: 30, right: 12, top: 30, bottom: 12 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const style = getComputedStyle(document.body);
  const line = style.getPropertyValue("--line").trim() || "#e2e6ec";
  const muted = style.getPropertyValue("--muted").trim() || "#7a8798";
  const accent = style.getPropertyValue("--accent").trim() || "#5b7cfa";
  const second = style.getPropertyValue("--chart-2").trim() || "#e08c3c";

  ctx.strokeStyle = line;
  ctx.fillStyle = muted;
  ctx.font = "10px ui-monospace, Menlo, Consolas, monospace";
  ctx.lineWidth = 1;
  [0, 0.5, 1].forEach((level) => {
    const y = pad.top + innerH * (1 - level);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText(level.toFixed(1), 4, y + 3);
  });
  if (points.length < 2) return;

  const first = points[0].at;
  const last = points[points.length - 1].at;
  const span = Math.max(1, last - first);
  const xOf = (at) => pad.left + (innerW * (at - first)) / span;
  const yOf = (value) => pad.top + innerH * (1 - Math.max(0, Math.min(1, value)));

  // 效价的中性位：0.5
  ctx.setLineDash([3, 3]);
  ctx.strokeStyle = line;
  ctx.beginPath();
  ctx.moveTo(pad.left, yOf(0.5));
  ctx.lineTo(width - pad.right, yOf(0.5));
  ctx.stroke();
  ctx.setLineDash([]);

  const series = [
    // 颜色跟着 CSS 的 token 走，换主题不用回来改这里
    { key: "affect", color: accent, label: "心潮" },
    { key: "valence", color: second, label: "效价" },
  ];
  series.forEach((item) => {
    ctx.strokeStyle = item.color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = xOf(point.at);
      const y = yOf(num(point[item.key], 0.5));
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
  // 图例：右上角一排，色块 + 文字
  ctx.font = "10px ui-monospace, Menlo, Consolas, monospace";
  const legendWidths = series.map((item) => 14 + ctx.measureText(item.label).width);
  let legendX = width - pad.right - legendWidths.reduce((acc, value) => acc + value + 12, -12);
  series.forEach((item, index) => {
    ctx.fillStyle = item.color;
    ctx.beginPath();
    ctx.roundRect(legendX, pad.top - 19, 10, 3, 2);
    ctx.fill();
    ctx.fillStyle = muted;
    ctx.fillText(item.label, legendX + 14, pad.top - 15);
    legendX += legendWidths[index] + 12;
  });
}

/* ================================================================== */
/* 她遇上什么事：能力值雷达 + 当前这件事 + 历史事件                      */
/* ================================================================== */

ui.radarValues = null;
ui.eventModalOpen = false;
ui.eventModalFocus = "";
ui.eventModalOpenRows = new Set();
ui.eventPending = false;
ui.renderSigs = {};

/**
 * 状态页每几秒会刷新一次：内容没变就别重画。
 *
 * 重画会把入场动画和弹窗滚动位置一起重置——看起来像页面在闪，
 * 而且鼠标停在历史事件里翻细节时突然跳回顶部。
 */
function renderOnce(key, signature) {
  ui.renderSigs = ui.renderSigs || {};
  if (ui.renderSigs[key] === signature) return false;
  ui.renderSigs[key] = signature;
  return true;
}

/** 一条线索此刻的状态：进行中 / 等群友 / 挂着没演完 / 已完结。 */
function eventStatusMeta(thread) {
  if (!thread || thread.status !== "open") {
    return { key: "closed", label: "已完结", cls: "closed" };
  }
  if (thread.waiting_help) {
    return { key: "help", label: "等群友拿主意", cls: "help" };
  }
  if (thread.suspended) {
    return { key: "idle", label: "挂着没演完", cls: "idle" };
  }
  return { key: "open", label: "进行中", cls: "open" };
}

function eventClockText(at) {
  const stamp = Number(at || 0);
  if (!stamp) return "";
  const date = new Date(stamp * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 事件里的时间：刚发生说人话，久远的写日期。 */
function eventTimeText(at, now) {
  const stamp = Number(at || 0);
  if (!stamp) return "";
  const diff = Math.max(0, num(now, Date.now() / 1000) - stamp);
  if (diff < 90) return "刚刚";
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  if (diff < 86400 * 3) return `${Math.round(diff / 86400)} 天前`;
  return eventClockText(stamp);
}

/** 「还有多久」说成人话（下一幕什么时候来）。 */
function eventWaitText(at, now) {
  const left = Number(at || 0) - num(now, Date.now() / 1000);
  if (!Number.isFinite(left) || left <= 0) return "马上就到";
  if (left < 90) return `${Math.round(left)} 秒`;
  if (left < 5400) return `${Math.round(left / 60)} 分钟`;
  return `${(left / 3600).toFixed(1)} 小时`;
}

function eventBadge(text, cls = "") {
  return el("span", `event-badge${cls ? ` ${cls}` : ""}`, text);
}

/* ---------------- 能力值雷达（替身面板那种） ---------------- */

const RADAR_KEYS = ["stamina", "wits", "dexterity", "composure"];

function radarPoints(values, cx, cy, radius) {
  const count = values.length || 1;
  return values.map((value, index) => {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / count;
    const safe = Math.max(0, Math.min(1, num(value, 0)));
    return [cx + Math.cos(angle) * radius * safe, cy + Math.sin(angle) * radius * safe];
  });
}

function radarPointText(points) {
  return points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
}

/**
 * 画四维能力雷达。值变了就从上一帧补间过去——直接重画会「啪」地跳一下，
 * 看起来像页面卡了一帧。
 */
function renderAbilityRadar(box, abilities) {
  if (!box) return;
  const items = RADAR_KEYS.map((key) => {
    const item = (abilities || {})[key] || {};
    return {
      key,
      label: item.label || key,
      value: num(item.value, 0),
      hint: item.hint || "",
    };
  });
  if (!items.some((item) => item.value > 0)) {
    box.innerHTML = "";
    box.appendChild(el("p", "muted", "还没有能力值数据。"));
    return;
  }

  const size = 210;
  const cx = size / 2;
  const cy = size / 2 + 2;
  const radius = size / 2 - 42;
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
  svg.setAttribute("class", "radar");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "能力值雷达图");

  const defs = document.createElementNS(svgNS, "defs");
  const gradient = document.createElementNS(svgNS, "linearGradient");
  gradient.setAttribute("id", "radar-fill");
  gradient.setAttribute("x1", "0");
  gradient.setAttribute("y1", "0");
  gradient.setAttribute("x2", "1");
  gradient.setAttribute("y2", "1");
  // 渐变用页面 token，和数值条/曲线的颜色保持一致
  const palette = getComputedStyle(document.body);
  [
    ["0%", palette.getPropertyValue("--accent").trim() || "#5b7cfa"],
    ["100%", palette.getPropertyValue("--ok").trim() || "#35a06b"],
  ].forEach(([offset, color]) => {
    const stop = document.createElementNS(svgNS, "stop");
    stop.setAttribute("offset", offset);
    stop.setAttribute("stop-color", color);
    gradient.appendChild(stop);
  });
  defs.appendChild(gradient);
  svg.appendChild(defs);

  [0.25, 0.5, 0.75, 1].forEach((ratio) => {
    const ring = document.createElementNS(svgNS, "polygon");
    ring.setAttribute("points", radarPointText(radarPoints(items.map(() => ratio), cx, cy, radius)));
    ring.setAttribute("class", ratio === 1 ? "radar-ring outer" : "radar-ring");
    svg.appendChild(ring);
  });

  const spokes = document.createElementNS(svgNS, "g");
  spokes.setAttribute("class", "radar-spokes");
  items.forEach((_item, index) => {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / items.length;
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", String(cx));
    line.setAttribute("y1", String(cy));
    line.setAttribute("x2", (cx + Math.cos(angle) * radius).toFixed(1));
    line.setAttribute("y2", (cy + Math.sin(angle) * radius).toFixed(1));
    spokes.appendChild(line);
  });
  svg.appendChild(spokes);

  const shape = document.createElementNS(svgNS, "polygon");
  shape.setAttribute("class", "radar-shape");
  svg.appendChild(shape);
  const dots = [];
  items.forEach((item, index) => {
    const dot = document.createElementNS(svgNS, "circle");
    dot.setAttribute("class", "radar-dot");
    dot.setAttribute("r", "3.2");
    svg.appendChild(dot);
    dots.push(dot);

    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / items.length;
    const text = document.createElementNS(svgNS, "text");
    text.setAttribute("class", "radar-label");
    text.setAttribute("x", (cx + Math.cos(angle) * (radius + 24)).toFixed(1));
    text.setAttribute("y", (cy + Math.sin(angle) * (radius + 18)).toFixed(1));
    text.setAttribute(
      "text-anchor",
      Math.abs(Math.cos(angle)) < 0.2 ? "middle" : Math.cos(angle) > 0 ? "start" : "end",
    );
    text.setAttribute("dominant-baseline", "middle");
    text.textContent = `${item.label} ${item.value.toFixed(2)}`;
    svg.appendChild(text);
  });

  const paint = (values) => {
    const points = radarPoints(values, cx, cy, radius);
    shape.setAttribute("points", radarPointText(points));
    points.forEach(([x, y], index) => {
      if (!dots[index]) return;
      dots[index].setAttribute("cx", x.toFixed(1));
      dots[index].setAttribute("cy", y.toFixed(1));
    });
  };

  const target = items.map((item) => item.value);
  const from =
    Array.isArray(ui.radarValues) && ui.radarValues.length === target.length
      ? ui.radarValues
      : target.map(() => 0);
  ui.radarValues = target;
  paint(from);

  box.innerHTML = "";
  box.appendChild(svg);
  const legend = el("div", "radar-legend");
  items.forEach((item) => {
    const row = el("div", "radar-legend-row");
    row.appendChild(el("span", "radar-legend-name", item.label));
    row.appendChild(el("span", "radar-legend-value", item.value.toFixed(2)));
    row.appendChild(el("span", "radar-legend-hint muted", item.hint || ""));
    legend.appendChild(row);
  });
  box.appendChild(legend);

  const changed = from.some((value, index) => Math.abs(value - target[index]) > 0.001);
  if (!changed) {
    paint(target);
    return;
  }
  const started = performance.now();
  const duration = 420;
  const step = (at) => {
    const t = Math.min(1, (at - started) / duration);
    const eased = 1 - (1 - t) ** 3;
    paint(target.map((value, index) => from[index] + (value - from[index]) * eased));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ---------------- 一幕：她选了什么 → 判定 → 结果 ---------------- */

function eventStepRow(step) {
  const row = el("li", "event-step");
  row.appendChild(el("span", "event-step-index", String(step.index || "")));
  const body = el("div", "event-step-body");
  const head = el("div", "event-step-head");
  head.appendChild(el("span", "event-step-desc", step.desc || "（没有选择）"));
  if (step.tier_label) head.appendChild(el("span", `tier-chip ${step.tier || ""}`, step.tier_label));
  body.appendChild(head);
  if (step.result) body.appendChild(el("p", "event-step-result", step.result));
  const meta = [];
  if (step.ability) meta.push(`拼的是${step.ability}`);
  const delta = Object.entries(step.ability_delta || {})
    .map(([name, value]) => `${name} ${Number(value) > 0 ? "+" : ""}${value}`)
    .join("、");
  if (delta) meta.push(delta);
  if (meta.length) body.appendChild(el("p", "event-step-meta muted", meta.join(" · ")));
  row.appendChild(body);
  return row;
}

function eventTimeline(steps) {
  const timeline = el("ol", "event-timeline");
  (steps || []).forEach((step) => timeline.appendChild(eventStepRow(step)));
  if (!(steps || []).length) {
    timeline.appendChild(el("li", "event-step empty muted", "还没有分幕（微事件只写结果）。"));
  }
  return timeline;
}

/** 当前这件事：详情 + 「立即推进一幕」/「立刻完结」。 */
function currentEventNote(thread, pending, now) {
  const meta = eventStatusMeta(thread);
  if (meta.key === "help") {
    return `在等群友拿主意，${formatCountdown((pending || {}).until)}后自己动手`;
  }
  if (thread.pending_followup) {
    return `下一幕：${eventWaitText(thread.next_step_at, now)}后（还没完的是「${thread.pending_followup}」）`;
  }
  return `${thread.step_count || 0} 幕 · 开始于 ${eventTimeText(thread.opened_at, now)}`;
}

function renderCurrentEvent(box, badgeBox, data) {
  if (!box) return;
  box.innerHTML = "";
  ui.eventNoteNode = null;
  const now = num(data.now, Date.now() / 1000);
  const threads = data.threads || [];
  const active = threads.find((item) => item.is_active && item.status === "open") || null;
  const pending = data.pending_help || {};
  const meta = active ? eventStatusMeta(active) : null;
  if (badgeBox) {
    badgeBox.textContent = meta ? meta.label : "";
    badgeBox.className = `pane-badge${meta ? ` ${meta.cls}` : ""}`;
  }
  if (!active) {
    box.appendChild(
      el(
        "p",
        "muted event-empty",
        "现在没遇上什么事。上面「给她安排一件事」可以直接投递一件，她会当场遇上。",
      ),
    );
    return;
  }

  const titleRow = el("div", "event-title-row");
  titleRow.appendChild(el("h4", "event-title", active.title || "一件事"));
  if (active.tier_label) titleRow.appendChild(eventBadge(active.tier_label, "tier"));
  if (active.place_name) titleRow.appendChild(eventBadge(active.place_name, "place"));
  if (active.genre) titleRow.appendChild(eventBadge(active.genre, "genre"));
  if (active.critical) titleRow.appendChild(eventBadge("危险", "danger"));
  box.appendChild(titleRow);
  if (active.hook) box.appendChild(el("p", "event-hook", active.hook));
  box.appendChild(eventTimeline(active.steps));

  const foot = el("div", "event-foot");
  const note = el("span", "event-foot-note muted", currentEventNote(active, pending, now));
  ui.eventNoteNode = note;
  foot.appendChild(note);

  const actions = el("span", "row-item");
  if (active.can_advance) {
    const advance = el("button", "ghost small", "立即推进一幕");
    advance.type = "button";
    advance.title = "不等那个间隔了，现在就往下演一幕";
    advance.addEventListener("click", () => eventAction("advance_event", active.id));
    actions.appendChild(advance);
  }
  if (active.can_close) {
    const close = el("button", "danger small", "立刻完结");
    close.type = "button";
    close.title = "这件事就此收尾，写一句总结后结束";
    close.addEventListener("click", () => closeEventThread(active));
    actions.appendChild(close);
  }
  if (actions.childNodes.length) foot.appendChild(actions);
  box.appendChild(foot);
}

/** 最近的线索：一行一条，点开进历史弹窗看细节。 */
function renderRecentThreads(box, data) {
  if (!box) return;
  box.innerHTML = "";
  const now = num(data.now, Date.now() / 1000);
  const activeId = data.active_id || "";
  const others = (data.threads || []).filter((item) => item.id !== activeId).slice(0, 4);
  if (!others.length) {
    box.appendChild(el("p", "muted", "还没有别的线索。"));
    return;
  }
  others.forEach((thread) => {
    const meta = eventStatusMeta(thread);
    const row = el("button", "event-recent-row");
    row.type = "button";
    row.appendChild(el("span", `event-dot ${meta.cls}`));
    row.appendChild(el("span", "event-recent-title", thread.title || "一件事"));
    row.appendChild(eventBadge(meta.label, `status ${meta.cls}`));
    row.appendChild(
      el(
        "span",
        "event-recent-meta muted",
        `${thread.step_count || 0} 幕 · ${eventTimeText(thread.updated_at || thread.opened_at, now)}`,
      ),
    );
    row.addEventListener("click", () => openEventModal(thread.id));
    box.appendChild(row);
  });
}

/* ---------------- 历史事件弹窗 ---------------- */

/* ==================== 改动历史 ==================== */
/* ==================== 人设体检 ==================== */

/* ==================== 测评 ==================== */

function openEvalModal(rounds) {
  const modal = $("eval-modal");
  if (!modal) return;
  ui.evalRounds = rounds || [];
  if ($("eval-modal-hint")) {
    $("eval-modal-hint").textContent =
      `下面 ${ui.evalRounds.length} 轮就是要问的问题。确认没问题再点「开始跑」——`
      + "它带着完整提示词（人设 + 声音样例 + 状态 + 画像）走真实调用，但**不写任何状态**，"
      + "跑多少遍都不会改动她的世界数据。";
  }
  if ($("eval-status")) $("eval-status").textContent = "";
  renderEvalQuestions();
  modal.classList.remove("hidden");
}

function closeEvalModal() {
  const modal = $("eval-modal");
  if (modal) modal.classList.add("hidden");
}

function renderEvalQuestions() {
  const box = $("eval-body");
  if (!box) return;
  box.innerHTML = "";
  ui.evalRounds.forEach((item, index) => {
    const card = el("div", "eval-card");
    const head = el("div", "eval-card-head");
    head.appendChild(el("span", "review-tag", item.scene || `第 ${index + 1} 轮`));
    head.appendChild(el("span", "muted", `第 ${index + 1} 轮`));
    card.appendChild(head);
    card.appendChild(el("div", "eval-user", `用户：${item.user_text}`));
    if (item.watch) card.appendChild(el("div", "muted eval-note", `看什么：${item.watch}`));
    if (item.taboo) card.appendChild(el("div", "muted eval-note", `禁忌：${item.taboo}`));
    box.appendChild(card);
  });
}

function renderEvalResults(data) {
  const box = $("eval-body");
  if (!box) return;
  box.innerHTML = "";
  (data.results || []).forEach((item) => {
    const card = el("div", "eval-card");
    const head = el("div", "eval-card-head");
    head.appendChild(el("span", "review-tag", item.scene || `第 ${item.index} 轮`));
    head.appendChild(el("span", "muted", `第 ${item.index} 轮`));
    if ((item.actions || []).length) {
      head.appendChild(el("span", "muted", `动作：${item.actions.join("、")}`));
    }
    card.appendChild(head);
    card.appendChild(el("div", "eval-user", `用户：${item.user_text}`));
    if (item.error) {
      card.appendChild(el("div", "eval-error", `这一轮挂了：${item.error}`));
    } else if ((item.reply || []).length) {
      (item.reply || []).forEach((text) => {
        card.appendChild(el("div", "eval-reply", text));
      });
    } else {
      card.appendChild(el("div", "muted", "她这一轮没说活（只做了动作，或者保持安静）"));
    }
    if (item.reasoning && Object.keys(item.reasoning).length) {
      const details = el("details", "eval-reason");
      details.appendChild(el("summary", "", "她的判断"));
      details.appendChild(
        el(
          "div",
          "muted",
          Object.entries(item.reasoning)
            .map(([key, value]) => `${key}：${value}`)
            .join("\n"),
        ),
      );
      card.appendChild(details);
    }
    if (item.watch) card.appendChild(el("div", "muted eval-note", `看什么：${item.watch}`));
    if (item.taboo) card.appendChild(el("div", "muted eval-note", `禁忌：${item.taboo}`));
    box.appendChild(card);
  });
}

async function runEvalNow() {
  if (!(ui.evalRounds || []).length) return;
  const sessionId = $("status-session") ? $("status-session").value : "";
  const concurrency = Number($("eval-concurrency").value || 3);
  const providerId = $("eval-provider") ? $("eval-provider").value.trim() : "";
  const button = $("eval-run");
  button.disabled = true;
  if ($("eval-status")) {
    $("eval-status").textContent =
      `跑着呢：${ui.evalRounds.length} 轮，并发 ${concurrency}…（真实调用，会花点时间）`;
  }
  try {
    const data = await apiPost("eval-run", {
      session: sessionId,
      rounds: ui.evalRounds,
      concurrency,
      provider_id: providerId,
    });
    renderEvalResults(data);
    const failed = (data.results || []).filter((item) => item.error).length;
    if ($("eval-status")) {
      $("eval-status").textContent = failed
        ? `跑完 ${data.count} 轮，其中 ${failed} 轮出错`
        : `跑完 ${data.count} 轮`;
    }
  } catch (error) {
    if ($("eval-status")) $("eval-status").textContent = error.message || "跑失败";
  } finally {
    button.disabled = false;
  }
}

function bindEval() {
  if ($("eval-modal-close")) $("eval-modal-close").addEventListener("click", closeEvalModal);
  if ($("eval-modal-done")) $("eval-modal-done").addEventListener("click", closeEvalModal);
  if ($("eval-run")) $("eval-run").addEventListener("click", runEvalNow);
  if ($("eval-modal")) {
    $("eval-modal").addEventListener("click", (event) => {
      if (event.target === $("eval-modal")) closeEvalModal();
    });
  }
}

function openReviewModal(report, issueLines) {
  const modal = $("review-modal");
  if (!modal) return;
  ui.reviewReport = report;
  ui.reviewPicked = new Set();
  const hint = $("review-modal-hint");
  if (hint) {
    hint.textContent =
      `原文 ${report.persona_chars} 字｜${report.length_hint || ""}。`
      + "下面每条勾了才会写进角色卡，没勾的原样保留。";
  }
  const issueBox = $("review-issues");
  const changeBox = $("review-changes");
  issueBox.innerHTML = "";
  changeBox.innerHTML = "";

  const okPoints = report.ok_points || [];
  const issues = report.issues || [];
  if (okPoints.length || issues.length || (report.questions || []).length) {
    const card = el("div", "review-card");
    card.appendChild(el("div", "review-card-title", "体检结果"));
    okPoints.forEach((text) => {
      const row = el("div", "review-line ok");
      row.appendChild(el("span", "review-tag ok", "好"));
      row.appendChild(el("span", "", text));
      card.appendChild(row);
    });
    issues.forEach((item) => {
      const row = el("div", "review-line");
      row.appendChild(
        el("span", `review-tag ${item.level.includes("冲突") ? "warn" : ""}`, item.level),
      );
      const body = el("div", "review-line-body");
      body.appendChild(el("div", "", `${item.kind}：${item.detail}`));
      if (item.quote) body.appendChild(el("div", "review-quote", item.quote));
      row.appendChild(body);
      card.appendChild(row);
    });
    (report.questions || []).forEach((text) => {
      const row = el("div", "review-line");
      row.appendChild(el("span", "review-tag", "待确认"));
      row.appendChild(el("span", "", text));
      card.appendChild(row);
    });
    issueBox.appendChild(card);
  }

  function updateCount() {
    if ($("review-count")) {
      $("review-count").textContent = `已选 ${ui.reviewPicked.size} / ${
        (report.rewrite || []).length + (report.add || []).length
      } 条`;
    }
  }

  function addChangeCard(kind, item, index) {
    const value = `${kind}${index}`;
    const card = el("label", "review-card change");
    const head = el("div", "review-card-head");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.addEventListener("change", () => {
      if (box.checked) ui.reviewPicked.add(value);
      else ui.reviewPicked.delete(value);
      updateCount();
    });
    head.appendChild(box);
    head.appendChild(
      el("span", "review-tag", kind === "rewrite" ? "改写" : `新增·${item.field || "补充"}`),
    );
    if (item.why) head.appendChild(el("span", "muted", item.why));
    card.appendChild(head);
    if (kind === "rewrite") {
      card.appendChild(el("div", "review-before", item.before));
      card.appendChild(el("div", "review-arrow", "↓"));
      card.appendChild(el("div", "review-after", item.after));
    } else {
      card.appendChild(el("div", "review-after", item.text));
    }
    changeBox.appendChild(card);
  }

  (report.rewrite || []).forEach((item, index) => addChangeCard("rewrite", item, index));
  (report.add || []).forEach((item, index) => addChangeCard("add", item, index));
  if (!changeBox.children.length) {
    changeBox.appendChild(
      el("p", "muted", "模型没给出可应用的改动——上面那些是它看到的问题，可以照着改。"),
    );
  }
  updateCount();
  modal.classList.remove("hidden");
}

function closeReviewModal() {
  const modal = $("review-modal");
  if (modal) modal.classList.add("hidden");
}

async function applyReviewPicked() {
  const report = ui.reviewReport || {};
  const picked = ui.reviewPicked || new Set();
  if (!picked.size) {
    toast("一条都没勾：那就什么都不改");
    return;
  }
  const rewrite = [];
  const add = [];
  (report.rewrite || []).forEach((item, index) => {
    if (picked.has(`rewrite${index}`)) rewrite.push(item);
  });
  (report.add || []).forEach((item, index) => {
    if (picked.has(`add${index}`)) add.push(item);
  });
  try {
    const result = await apiPost("persona/apply", { rewrite, add });
    const bits = [`改了 ${(result.applied || []).length} 处`];
    if ((result.skipped || []).length) bits.push(`跳过：${result.skipped.join("；")}`);
    bits.push(`现在 ${result.chars} 字`);
    toast(bits.join("｜"));
    closeReviewModal();
    ui.config = await apiGet("config");
    renderSettings();
  } catch (error) {
    toast(error.message || "应用失败");
  }
}

function bindReview() {
  if ($("review-modal-close")) {
    $("review-modal-close").addEventListener("click", closeReviewModal);
  }
  if ($("review-modal-cancel")) {
    $("review-modal-cancel").addEventListener("click", closeReviewModal);
  }
  if ($("review-modal-apply")) {
    $("review-modal-apply").addEventListener("click", applyReviewPicked);
  }
  if ($("review-modal")) {
    $("review-modal").addEventListener("click", (event) => {
      if (event.target === $("review-modal")) closeReviewModal();
    });
  }
}

const HISTORY_BLOCK_LABELS = {
  map: "地图",
  actions: "动作",
  settings: "世界设置",
  persona: "人设",
  schedules: "日程",
  sessions: "会话",
};

function historyBlockList() {
  return Object.keys(HISTORY_BLOCK_LABELS);
}

function relativeTime(text) {
  const stamp = Date.parse(String(text || "").replace(" ", "T"));
  if (!stamp) return "";
  const minutes = Math.round((Date.now() - stamp) / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}

async function openHistoryModal() {
  const modal = $("history-modal");
  if (!modal) return;
  ui.historySelected = ui.historySelected || "";
  ui.historyBlocks = ui.historyBlocks || new Set(historyBlockList());
  modal.classList.remove("hidden");
  await refreshHistory();
}

function closeHistoryModal() {
  const modal = $("history-modal");
  if (modal) modal.classList.add("hidden");
}

async function refreshHistory() {
  const list = $("history-list");
  if (!list) return;
  let data = {};
  try {
    data = await apiGet("history");
  } catch (error) {
    toast(error.message || "读历史失败");
    return;
  }
  const items = data.items || [];
  ui.historyItems = items;
  if ($("history-count")) {
    $("history-count").textContent = items.length
      ? `共 ${items.length} 条（上限 ${data.keep || 50}）`
      : "还没有历史";
  }
  list.innerHTML = "";
  if (!items.length) {
    list.appendChild(el("p", "muted", "还没有记录：改一次配置（保存 / 应用预设 / 生成）之后这里就会有一条。"));
    if ($("history-detail")) $("history-detail").innerHTML = "";
    return;
  }
  if (!items.some((item) => item.id === ui.historySelected)) {
    ui.historySelected = items[0].id;
  }
  items.forEach((item) => {
    const row = el("button", "history-row");
    row.type = "button";
    if (item.id === ui.historySelected) row.classList.add("active");
    const head = el("div", "history-row-head");
    head.appendChild(el("strong", "", item.created_at || item.name || item.id));
    const ago = relativeTime(item.created_at);
    if (ago) head.appendChild(el("span", "muted", ago));
    if (item.legacy) head.appendChild(el("span", "tag", "旧版备份"));
    row.appendChild(head);
    row.appendChild(
      el("span", "muted", `${item.reason || "改动"}｜${item.summary || ""}`),
    );
    row.addEventListener("click", () => {
      ui.historySelected = item.id;
      renderHistoryRows();
      loadHistoryDetail(item.id);
    });
    list.appendChild(row);
  });
  await loadHistoryDetail(ui.historySelected);
}

function renderHistoryRows() {
  const list = $("history-list");
  if (!list) return;
  Array.from(list.children).forEach((row, index) => {
    const item = (ui.historyItems || [])[index];
    if (!item) return;
    row.classList.toggle("active", item.id === ui.historySelected);
  });
}

function historyText(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function historyValueText(value) {
  if (typeof value === "string") return value;
  if (value === undefined) return "（没有）";
  return JSON.stringify(value, null, 2);
}

/**
 * 两份配置之间**改了哪几处**。
 *
 * 以前是把两边 JSON 逐行比一遍，然后把"删掉的行"和"新增的行"各列一坨——
 * 整块 settings 好几千行，根本看不出动的是哪一处。现在按**路径**递归比：
 * `persona.text`、`settings.profile.bonds[2].cap` 这样一条条列出来。
 */
function historyDiffPairs(before, after, limit = 60) {
  const rows = [];
  const push = (item) => {
    if (rows.length < limit) rows.push(item);
  };
  const walk = (left, right, path) => {
    if (rows.length >= limit) return;
    const bothArrays = Array.isArray(left) && Array.isArray(right);
    if (bothArrays) {
      const max = Math.max(left.length, right.length);
      for (let index = 0; index < max && rows.length < limit; index += 1) {
        const at = path ? `${path}[${index}]` : `[${index}]`;
        if (index >= left.length) push({ path: at, kind: "add", after: right[index] });
        else if (index >= right.length) push({ path: at, kind: "del", before: left[index] });
        else walk(left[index], right[index], at);
      }
      return;
    }
    const bothObjects =
      left && right && typeof left === "object" && typeof right === "object";
    if (bothObjects) {
      const keys = [...Object.keys(left)];
      Object.keys(right).forEach((key) => {
        if (!keys.includes(key)) keys.push(key);
      });
      keys.forEach((key) => {
        const at = path ? `${path}.${key}` : key;
        if (!(key in left)) push({ path: at, kind: "add", after: right[key] });
        else if (!(key in right)) push({ path: at, kind: "del", before: left[key] });
        else walk(left[key], right[key], at);
      });
      return;
    }
    if (JSON.stringify(left) !== JSON.stringify(right)) {
      push({ path: path || "（整块）", kind: "change", before: left, after: right });
    }
  };
  walk(before ?? {}, after ?? {}, "");
  return rows;
}

function historyBlockPayload(body, block) {
  const source = body || {};
  if (block === "schedules") return source.schedules || {};
  if (block === "sessions") return source.sessions || {};
  const world = source.world || {};
  if (block === "map") {
    return {
      zones: world.zones,
      zone_edges: world.zone_edges,
      nodes: world.nodes,
      edges: world.edges,
    };
  }
  if (block === "actions") return world.actions || {};
  if (block === "persona") return world.persona || {};
  const skip = new Set(["zones", "zone_edges", "nodes", "edges", "actions", "persona"]);
  const settings = {};
  Object.keys(world).forEach((key) => {
    if (!skip.has(key)) settings[key] = world[key];
  });
  return settings;
}

async function loadHistoryDetail(snapshotId) {
  const box = $("history-detail");
  if (!box) return;
  box.innerHTML = "";
  if (!snapshotId) return;
  let detail = {};
  try {
    detail = await apiGet("history/item", { id: snapshotId });
  } catch (error) {
    box.appendChild(el("p", "muted", error.message || "读不到这条历史"));
    return;
  }
  const head = el("div", "history-detail-head");
  head.appendChild(el("strong", "", detail.created_at || detail.id));
  head.appendChild(el("span", "muted", `｜${detail.reason || "改动"}｜${detail.summary || ""}`));
  box.appendChild(head);

  const picked = ui.historyBlocks || new Set(historyBlockList());
  const pick = el("div", "history-blocks");
  historyBlockList().forEach((key) => {
    const changed = (detail.changed || []).includes(key);
    const label = el("label", "history-block");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = picked.has(key);
    input.addEventListener("change", () => {
      if (input.checked) picked.add(key);
      else picked.delete(key);
      ui.historyBlocks = picked;
    });
    label.appendChild(input);
    label.appendChild(
      el("span", changed ? "history-changed" : "", HISTORY_BLOCK_LABELS[key] || key),
    );
    if (changed) label.appendChild(el("span", "tag", "和现在不同"));
    pick.appendChild(label);
  });
  box.appendChild(pick);

  const diffs = el("div", "history-diff");
  let changedCount = 0;
  historyBlockList().forEach((key) => {
    if (!(detail.changed || []).includes(key)) return;
    changedCount += 1;
    const pairs = historyDiffPairs(
      historyBlockPayload(detail.snapshot, key),
      historyBlockPayload(detail.current, key),
    );
    const item = el("div", "history-diff-block");
    item.appendChild(
      el(
        "div",
        "history-diff-title",
        `${HISTORY_BLOCK_LABELS[key]}：${pairs.length} 处改动`,
      ),
    );
    if (!pairs.length) {
      item.appendChild(el("p", "muted", "（这一块内容相同）"));
    }
    pairs.forEach((pair) => {
      const card = el("div", "history-pair");
      const head = el("div", "history-pair-head");
      head.appendChild(el("code", "history-path", pair.path || "（整块）"));
      head.appendChild(
        el(
          "span",
          "review-tag",
          pair.kind === "add" ? "新增" : pair.kind === "del" ? "删除" : "改动",
        ),
      );
      card.appendChild(head);
      if (pair.kind !== "add") {
        card.appendChild(el("div", "review-before", historyValueText(pair.before).slice(0, 400)));
      }
      if (pair.kind !== "del") {
        card.appendChild(el("div", "review-after", historyValueText(pair.after).slice(0, 400)));
      }
      item.appendChild(card);
    });
    diffs.appendChild(item);
  });
  if (!changedCount) {
    diffs.appendChild(el("p", "muted", "这一版和现在的配置完全一样。"));
  }
  box.appendChild(diffs);

  const actions = el("div", "history-actions");
  const restore = el("button", "small primary", "恢复选中的部分");
  restore.type = "button";
  restore.addEventListener("click", () => restoreHistory(detail));
  actions.appendChild(restore);
  const remove = el("button", "small ghost danger", "删除这条");
  remove.type = "button";
  remove.disabled = Boolean(detail.legacy);
  if (detail.legacy) remove.title = "旧版备份不在这里删，请到 presets/backups 目录处理";
  remove.addEventListener("click", () => deleteHistoryItem(detail));
  actions.appendChild(remove);
  box.appendChild(actions);
}

async function restoreHistory(detail) {
  const blocks = Array.from(ui.historyBlocks || new Set(historyBlockList()));
  if (!blocks.length) {
    toast("至少选一块要恢复的内容");
    return;
  }
  const labels = blocks.map((key) => HISTORY_BLOCK_LABELS[key] || key).join("、");
  const ok = await confirmDialog({
    title: "恢复这一版？",
    message: `会把当前配置里的「${labels}」换成 ${detail.created_at} 那一版。恢复前的这一版会自动存进历史，随时能再恢复回来。`,
    confirmText: "恢复",
  });
  if (!ok) return;
  try {
    const result = await apiPost("history/restore", { id: detail.id, blocks });
    const bits = [`已恢复 ${(result.blocks || []).length} 块`];
    if ((result.warnings || []).length) bits.push(`提醒：${result.warnings.join("；")}`);
    toast(bits.join("；"));
    await loadAll();
    await refreshHistory();
  } catch (error) {
    toast(error.message || "恢复失败");
  }
}

async function deleteHistoryItem(detail) {
  const ok = await confirmDialog({
    title: "删除这条历史？",
    message: `删除后就翻不回 ${detail.created_at} 这一版了（当前配置不受影响）。`,
    confirmText: "删除",
  });
  if (!ok) return;
  try {
    await apiPost("history/delete", { id: detail.id });
    ui.historySelected = "";
    toast("已删除");
    await refreshHistory();
  } catch (error) {
    toast(error.message || "删除失败");
  }
}

async function clearHistory() {
  const ok = await confirmDialog({
    title: "清空改动历史？",
    message: "会删掉所有历史快照（早期那批 before-apply 备份不受影响）。当前配置不受影响。",
    confirmText: "清空",
  });
  if (!ok) return;
  try {
    const result = await apiPost("history/clear", {});
    ui.historySelected = "";
    toast(`已清空 ${result.removed || 0} 条`);
    await refreshHistory();
  } catch (error) {
    toast(error.message || "清空失败");
  }
}

function bindHistory() {
  if ($("history-open")) $("history-open").addEventListener("click", () => openHistoryModal());
  if ($("history-modal-close")) {
    $("history-modal-close").addEventListener("click", closeHistoryModal);
  }
  if ($("history-modal-done")) {
    $("history-modal-done").addEventListener("click", closeHistoryModal);
  }
  if ($("history-clear")) $("history-clear").addEventListener("click", clearHistory);
  if ($("history-modal")) {
    $("history-modal").addEventListener("click", (event) => {
      if (event.target === $("history-modal")) closeHistoryModal();
    });
  }
}

function openEventModal(focusId = "") {
  const modal = $("event-modal");
  if (!modal) return;
  ui.eventModalOpen = true;
  ui.eventModalFocus = focusId || "";
  ui.eventModalOpenRows = new Set(focusId ? [focusId] : []);
  modal.classList.remove("hidden");
  renderEventModal(true);
}

function closeEventModal() {
  const modal = $("event-modal");
  if (modal) modal.classList.add("hidden");
  ui.eventModalOpen = false;
}

function renderEventModal(force = false) {
  const body = $("event-modal-body");
  const hint = $("event-modal-hint");
  if (!body) return;
  const data = ui.events || {};
  const threads = data.threads || [];
  const sessionId = $("status-session") ? $("status-session").value : "";
  const sig = `${sessionId}|${threads
    .map((item) => `${item.id}:${item.status}:${item.step_count}:${item.updated_at}`)
    .join(",")}`;
  if (force && ui.renderSigs) ui.renderSigs.modal = "";
  // 刷新时内容没变就不重画：不然滚动位置会跳回顶部、入场动画也会重放一遍
  if (!renderOnce("modal", sig)) return;
  const now = num(data.now, Date.now() / 1000);
  const opened = ui.eventModalOpenRows || new Set();
  body.innerHTML = "";
  if (hint) {
    hint.textContent = threads.length
      ? "点一条展开看每一幕：她选了什么、判定如何、结果怎样。没完结的可以在这里手动推进或直接完结。"
      : "还没有经历过什么事。上面「给她安排一件事」可以主动投递一件。";
  }
  threads.forEach((thread) => {
    const meta = eventStatusMeta(thread);
    const card = el("div", `event-history-row${opened.has(thread.id) ? " open" : ""}`);
    const head = el("button", "event-history-head");
    head.type = "button";
    head.appendChild(el("span", "event-chevron", "▸"));
    head.appendChild(el("span", "event-history-title", thread.title || "一件事"));
    head.appendChild(eventBadge(meta.label, `status ${meta.cls}`));
    if (thread.tier_label) head.appendChild(eventBadge(thread.tier_label, "tier"));
    head.appendChild(
      el(
        "span",
        "event-history-meta muted",
        [thread.place_name, `${thread.step_count || 0} 幕`, eventClockText(thread.opened_at)]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    head.addEventListener("click", () => {
      if (opened.has(thread.id)) opened.delete(thread.id);
      else opened.add(thread.id);
      ui.eventModalOpenRows = opened;
      card.classList.toggle("open", opened.has(thread.id));
      if (opened.has(thread.id) && head.scrollIntoView) {
        // 展开后把它滚进可见区域：内容多的时候不至于"展开了一屏外的东西"
        head.scrollIntoView({ block: "nearest" });
      }
    });
    card.appendChild(head);

    const wrap = el("div", "event-history-panel");
    const inner = el("div", "event-history-inner");
    if (thread.hook) inner.appendChild(el("p", "event-hook", thread.hook));
    inner.appendChild(eventTimeline(thread.steps));
    if (thread.line) inner.appendChild(el("p", "event-history-line", `她自己的账：${thread.line}`));
    if (thread.pending_followup) {
      inner.appendChild(
        el(
          "p",
          "event-history-note muted",
          `还没完的是「${thread.pending_followup}」· 下一幕 ${eventWaitText(thread.next_step_at, now)}后`,
        ),
      );
    }
    if (thread.status === "closed") {
      inner.appendChild(
        el("p", "event-history-note muted", `完结于 ${eventClockText(thread.closed_at || thread.updated_at)}`),
      );
    }
    const actions = el("div", "event-history-actions");
    if (thread.can_advance) {
      const advance = el("button", "ghost small", "立即推进一幕");
      advance.type = "button";
      advance.addEventListener("click", () => eventAction("advance_event", thread.id));
      actions.appendChild(advance);
    }
    if (thread.can_close) {
      const close = el("button", "danger small", "立刻完结");
      close.type = "button";
      close.addEventListener("click", () => closeEventThread(thread));
      actions.appendChild(close);
    }
    if (actions.childNodes.length) inner.appendChild(actions);
    wrap.appendChild(inner);
    card.appendChild(wrap);
    body.appendChild(card);
  });
}

async function closeEventThread(thread) {
  const ok = await confirmDialog({
    title: "完结这件事？",
    message:
      `「${thread.title || "一件事"}」不再往下演了：她会写一句收尾，然后就翻过去。` +
      "已经演过的那几幕和记忆都留着。",
    confirmText: "完结",
  });
  if (!ok) return;
  eventAction("close_event", thread.id);
}

/** 手动推进 / 立刻完结：回来之后把最新的概览画一遍。 */
async function eventAction(action, threadId = "") {
  const sessionId = $("status-session") ? $("status-session").value : "";
  if (!sessionId) {
    toast("先选一个会话");
    return;
  }
  if (ui.eventPending) {
    toast("上一次还在处理，这次点击已忽略");
    return;
  }
  ui.eventPending = true;
  try {
    const data = await apiPost("state/action", {
      session: sessionId,
      action,
      thread: threadId,
    });
    if (data && data.ok === false && data.note) {
      toast(data.note);
      return;
    }
    toast((data && data.note) || "已执行");
    if (data && data.events) {
      ui.events = data.events;
      renderEventsPanel();
    }
    refreshStatus();
  } catch (error) {
    toast(error.message || "执行失败");
  } finally {
    ui.eventPending = false;
  }
}

/** 拉一次事件概览（能力值 / 未了的事 / 最近的线索）。 */
async function loadEvents() {
  const box = $("status-events");
  if (!box) return;
  const sessionId = $("status-session") ? $("status-session").value : "";
  if (!sessionId) {
    box.textContent = "还没有会话。";
    ui.events = null;
    renderEventsPanel();
    return;
  }
  try {
    ui.events = await apiGet("events", { session: sessionId });
    renderEventsPanel();
  } catch (error) {
    box.textContent = `读取事件失败：${error.message || error}`;
  }
}

function renderEventsPanel() {
  const data = ui.events || {};
  const abilityBox = $("status-abilities");
  const listBox = $("status-events");
  const hintBox = $("event-hint");
  if (!abilityBox || !listBox) return;
  const sessionId = $("status-session") ? $("status-session").value : "";
  const threads = data.threads || [];
  const threadSig = threads
    .map((item) => `${item.id}:${item.status}:${item.step_count}:${item.updated_at}`)
    .join(",");

  const radarSig = `${sessionId}|${RADAR_KEYS.map((key) => num((data.abilities || {})[key]?.value, 0)).join(",")}`;
  if (renderOnce("radar", radarSig)) renderAbilityRadar(abilityBox, data.abilities || {});
  const active = threads.find((item) => item.is_active && item.status === "open") || null;
  const currentSig = active
    ? [
        sessionId,
        active.id,
        active.status,
        active.step_count,
        active.pending_followup,
        active.can_advance,
        active.can_close,
        active.waiting_help,
        active.suspended,
        (data.pending_help || {}).state,
      ].join("|")
    : `${sessionId}|none`;
  if (renderOnce("current", currentSig)) {
    renderCurrentEvent($("event-current"), $("event-current-badge"), data);
  } else if (active && ui.eventNoteNode) {
    // 结构没变、只有"还有多久"在走：只改那一行字，不重画整块
    ui.eventNoteNode.textContent = currentEventNote(
      active,
      data.pending_help || {},
      num(data.now, Date.now() / 1000),
    );
  }
  if (renderOnce("recent", `${sessionId}|${threadSig}`)) {
    renderRecentThreads(listBox, data);
  }
  const countBox = $("event-history-count");
  if (countBox) countBox.textContent = String(threads.length);
  const historyButton = $("event-history");
  if (historyButton) historyButton.disabled = !threads.length;
  if (ui.eventModalOpen) renderEventModal();

  renderRecentThreads(listBox, data);

  if (hintBox) {
    hintBox.textContent = data.enabled === false
      ? "事件系统关着（在「全局设置 → 世界与事件 → 事件」里打开）。"
      : "她遇上的事都是按当前场景现编的；投递的事件会立刻发生，也会进日志。" +
        "雷达图是她的底子（只有事件结果会改它），每一幕的细节在上面的「现在这件事」和历史事件里。";
  }
}

function formatCountdown(until) {
  const left = Number(until || 0) - Date.now() / 1000;
  if (!Number.isFinite(left) || left <= 0) return "一会儿";
  if (left < 90) return `${Math.round(left)} 秒`;
  return `${Math.round(left / 60)} 分钟`;
}

/** 事件题材与权重：代码按权重抽，不让模型自己分配比例。 */
function genreEditor(world) {
  world.events = world.events || {};
  if (!Array.isArray(world.events.genres)) world.events.genres = [];
  const box = el("div", "full");
  box.appendChild(
    fieldHead(
      "事件题材与权重",
      "代码按权重抽一个题材，再让模型「就在这个题材里」编——不这样写，模型永远只生成温馨小事，" +
        "「被人跟着」这种偏暗的题材一次都不会出现。权重 0 = 不生成这一类。" +
        "带三个框的题材按档位分权（小事件 / 微事件 / 大事件各算各的）：大事件的权重放在" +
        "「日常小事」上就会被吃饭喝水占掉，所以大事默认只从人际、意外、危险、机会、旧账里抽。",
    ),
  );
  const list = el("div", "genre-list");
  const paint = () => {
    list.innerHTML = "";
    (world.events.genres || []).forEach((item, index) => {
      const row = el("div", "genre-row");
      const name = document.createElement("input");
      name.type = "text";
      name.value = item.name || "";
      name.placeholder = "题材名";
      name.addEventListener("change", () => {
        item.name = name.value.trim();
        markDirty();
      });
      const tierWeights =
        item.weights && typeof item.weights === "object" ? item.weights : null;
      const weight = document.createElement("input");
      weight.type = "number";
      weight.min = "0";
      weight.step = "1";
      weight.title = tierWeights
        ? "小事件权重（微事件 / 大事件在右边两个框里单独调）"
        : "权重：相对比例，0 就是不生成这一类";
      weight.value = tierWeights ? tierWeights.small ?? 0 : item.weight ?? 0;
      weight.addEventListener("change", () => {
        if (tierWeights) {
          tierWeights.small = num(weight.value, 0);
        } else {
          item.weight = num(weight.value, 0);
        }
        markDirty();
      });
      const examples = document.createElement("input");
      examples.type = "text";
      examples.value = item.examples || "";
      examples.placeholder = "例子：用 | 分开写形态，例如 她的{某样东西}被{别人}拿走";
      examples.addEventListener("change", () => {
        item.examples = examples.value.trim();
        markDirty();
      });
      const remove = el("button", "icon-btn", "✕");
      remove.type = "button";
      remove.title = "删掉这一类";
      remove.addEventListener("click", () => {
        world.events.genres.splice(index, 1);
        markDirty();
        paint();
      });
      row.appendChild(name);
      row.appendChild(weight);
      if (tierWeights) {
        [["micro", "微事件权重"], ["big", "大事件权重"]].forEach(([key, label]) => {
          const input = document.createElement("input");
          input.type = "number";
          input.min = "0";
          input.step = "1";
          input.title = label;
          input.value = tierWeights[key] ?? 0;
          input.addEventListener("change", () => {
            tierWeights[key] = num(input.value, 0);
            markDirty();
          });
          row.appendChild(input);
        });
      }
      row.appendChild(examples);
      row.appendChild(remove);
      list.appendChild(row);
    });
    const add = el("button", "small ghost", "＋ 加一类题材");
    add.type = "button";
    add.addEventListener("click", () => {
      world.events.genres.push({
        name: "新题材",
        weight: 5,
        weights: { micro: 5, small: 5, big: 0 },
        examples: "",
      });
      markDirty();
      paint();
    });
    list.appendChild(add);
  };
  paint();
  box.appendChild(list);
  return box;
}

async function refreshStatus() {
  loadEvents();
  const sessionId = $("status-session").value;
  if (!sessionId) {
    setStatusEmpty("还没有添加任何会话白名单。");
    return;
  }
  try {
    const data = await apiGet("state", { session: sessionId });
    ui.status = data;
    // 扩展想显示的状态优先（例如亲密扩展在戏里时说"兴奋中"）：
    // 这是编辑器里的一行字，群名片不受影响
    const stateLabel = stateLabelOf(data.state, data);
    renderHero(data, stateLabel);
    renderStatusSections(data, stateLabel);
    renderToolWarnings();
    renderHistory();
    // 地图上标出她此刻所在的地点
    if ($("tab-map")) renderMap();

    const values = data.values || {};
    renderValueRows(values, data);

    // 群名片：直接改、锁定/解锁、恢复原名
    const nicknameBox = $("status-nickname");
    if (nicknameBox) {
      nicknameBox.value = data.nickname || "";
      nicknameBox.title = `她现在：${data.state || ""}${data.node_name ? ` · ${data.node_name}` : ""}`;
    }
    const nickToggle = $("status-nickname-toggle");
    if (nickToggle) {
      const locked = Boolean(data.nickname_locked);
      nickToggle.textContent = locked ? "解锁" : "锁定";
      nickToggle.title = locked
        ? `已锁定，插件不会自动改名（原名：${data.nickname_base || "还没记下来"}）。点击解锁。`
        : "解锁状态下她会随状态自动改名。点击锁定。";
      nickToggle.classList.toggle("primary", locked);
    }
    const currentAction = data.current_action || {};
    const sleeping = data.state === "sleeping" || data.state === "napping";
    $("state-interrupt").disabled = !currentAction.type;
    $("state-wake").disabled = !sleeping;

    $("status-plan").textContent = formatPlan(data.current_plan);
    // 「内心活动」= 最近一次推理草稿（她动手前对处境/心情/对象的确认）。
    // 以前这里显示的是 think 动作，但模型很少每次都写，面板长期是空的。
    const reasoning = data.last_reasoning || {};
    const reasoningLines = REASONING_LABELS.filter(([key]) => reasoning[key]).map(
      ([key, label]) => `${label}：${reasoning[key]}`,
    );
    if (reasoning._source) {
      reasoningLines.push(`（来自 ${reasoning._source === "plan" ? "自主决策" : "回复"}）`);
    }
    const innerLines = reasoningLines.length
      ? reasoningLines
      : ["（她还没动过大模型，或者这次没写草稿）"];
    const thoughts = (data.thoughts || []).map((item) => item.content).filter(Boolean);
    if (thoughts.length) innerLines.push(`她主动留下的心里话：${thoughts.join("；")}`);
    $("status-thoughts").textContent = innerLines.join("\n");

    // 更早的群聊摘要（只有在「上下文 → 留档超出上限时的处理 = 压成摘要」时才有）
    const summaryBlock = $("status-context");
    // 摘要按会话分开存：这里把每个会话那一份都列出来（当前会话标成「这里」）
    const summaryRows = Array.isArray(data.chat_summaries)
      ? data.chat_summaries.filter((item) => String((item && item.text) || "").trim())
      : [];
    const head = `留档 ${Number(data.chat_history_count || 0)} 条（本会话）`;
    summaryBlock.textContent = summaryRows.length
      ? `${head}；更早的群聊摘要：\n` +
        summaryRows
          .map((item) => `〔${item.label || item.session}〕${String(item.text).trim()}`)
          .join("\n")
      : `${head}，暂无摘要。`;

    const logs = await apiGet("logs", { session: sessionId, limit: 60 });
    const events = logs.events || [];
    $("status-logs").textContent = (logs.events || [])
      .map((item) => `[t=${item.world_time}] ${item.text || item.event_type}`)
      .join("\n");

    // 她"什么都没做"的时候，最常见的原因是某一步被跳过了（缺工具 / 地点不对）。
    // 把最近一条跳过直接摆在状态卡上，省得用户去翻日志。
    const skipIndex = events.findIndex((item) => item.event_type === "skip");
    if (skipIndex >= 0) {
      const didSomethingAfter = events
        .slice(0, skipIndex)
        .some((item) => item.event_type === "action" || item.event_type === "action_start");
      if (!didSomethingAfter) {
        $("status-warn").appendChild(el("div", "warn-line", `⚠ ${events[skipIndex].text}`));
      }
    }
  } catch (error) {
    toast(error.message || "读取状态失败");
  }
}

/** 清空这个会话的群聊留档与摘要（调试用）。 */
async function clearChatContext() {
  const sessionId = $("status-session").value;
  if (!sessionId) return;
  const snapshot = ui.status || {};
  const count = Number(snapshot.chat_history_count || 0);
  const hasSummary = Boolean((snapshot.chat_summary || "").trim());
  const ok = await confirmDialog({
    title: "清空群聊上下文？",
    message:
      `会清掉「${sessionId}」保存的 ${count} 条群聊留档` +
      (hasSummary ? "和已有的摘要" : "") +
      "。她的世界状态、记忆、日程都不受影响，但接下来她看不到之前的群聊了。",
    confirmText: "清空",
  });
  if (!ok) return;
  try {
    const result = await apiPost("state/action", { session: sessionId, action: "clear_context" });
    toast(`已清空 ${result.removed ?? 0} 条群聊留档${result.had_summary ? "与摘要" : ""}`);
    refreshStatus();
  } catch (error) {
    toast(error.message || "清空失败");
  }
}

async function stateAction(action, extra = {}) {
  const sessionId = $("status-session").value;
  if (!sessionId) {
    // 以前这里直接 return：没选会话时点任何按钮都毫无反应，看起来像"按钮坏了"
    toast("先在上面选一个会话；如果列表是空的，去「会话白名单」把群加回来");
    return;
  }
  if (ui.stateActionPending) {
    // 上一次请求还没回来：这次直接忽略（连着点只是浪费，等会儿会一口气生效）
    toast("上一次还在处理，这次点击已忽略");
    return;
  }
  ui.stateActionPending = true;
  try {
    const result = await apiPost("state/action", {
      session: sessionId,
      action,
      ...extra,
    });
    if (result && result.ok === false && result.note) {
      // 后端明确说了"她正在忙，稍等"这类原因时，优先显示它
      toast(result.note);
      return;
    }
    if (result.messages && result.messages.length) {
      toast(`已发送：${result.messages.join(" / ")}`);
    } else if (result.notes && result.notes.length) {
      toast(result.notes.join("；"));
    } else if (action === "interrupt") {
      // 取消是两步：先发取消信号（立刻回），再等状态真的清干净。所以这里说清
      // 现在到底是"已请求取消/正在停"还是"已经停下"，而不是只丢一句"已打断"。
      toast(
        result.note ||
          (result.interrupted ? "已停止：手头的动作停下了" : "她现在没有在做动作")
      );
      if (result.cancelling) {
        // 还在停：过一会儿再拉一次状态，好让按钮/状态显示跟上（还在停 → 变成没在忙）
        setTimeout(refreshStatus, 1500);
      }
    } else if (action === "wake") {
      toast(result.was_sleeping ? "已把她叫醒" : "她本来就没在睡");
    } else {
      toast("已执行");
    }
    refreshStatus();
  } catch (error) {
    toast(error.message || "执行失败");
  } finally {
    ui.stateActionPending = false;
  }
}

/* ================================================================== */
/* 数据访问                                                            */
/* ================================================================== */

function nodes() {
  return ui.config.world.nodes || (ui.config.world.nodes = []);
}

function edges() {
  return ui.config.world.edges || (ui.config.world.edges = []);
}

function actions() {
  return ui.config.world.actions || (ui.config.world.actions = []);
}

function zones() {
  return ui.config.world.zones || (ui.config.world.zones = []);
}

function zoneEdges() {
  return ui.config.world.zone_edges || (ui.config.world.zone_edges = []);
}

function defaultZoneId() {
  const first = zones()[0];
  return first ? first.id : "";
}

/** 节点属于哪个区域（没写归属就算第一个区域）。 */
function zoneOfNode(node) {
  return (node && node.zone_id) || defaultZoneId();
}

function nodesInZone(zoneId) {
  return nodes().filter((node) => zoneOfNode(node) === zoneId);
}

function zoneItems() {
  return zones().map((zone) => ({
    id: zone.id,
    name: zone.name || zone.id,
    desc: zone.note || `${nodesInZone(zone.id).length} 个地点`,
  }));
}

function schedules() {
  return ui.config.schedules.schedules || (ui.config.schedules.schedules = []);
}

function nodeItems() {
  // 按区域分组：地点多了以后，选地点时先看区域再看地点
  const zoneNames = {};
  zones().forEach((zone) => {
    zoneNames[zone.id] = zone.name || zone.id;
  });
  return nodes().map((node) => ({
    id: node.id,
    name: node.name || node.id,
    desc: node.prompt || "",
    tag: node.id,
    group: zoneNames[zoneOfNode(node)] || "未分组",
  }));
}

/** 会话 id 的短名字，用在标记的悬浮提示里。 */
function sessionShortName(sessionId) {
  if (!sessionId) return "";
  const parts = String(sessionId).split(":");
  const platform = parts[0] || "";
  const type = (parts[1] || "").replace("Message", "");
  const id = parts.slice(2).join(":");
  return `${platform}/${type}/${id}`;
}

/** 拉取所有会话的简要状态（地图页一次看全「她分别在哪个群、在哪」）。 */
async function loadOverview() {
  try {
    const data = await apiGet("states");
    ui.overview = data.sessions || [];
    renderMap();
    renderSessionList();
  } catch (error) {
    ui.overview = [];
  }
}

function actionItems() {
  return actions().map((action) => ({
    id: action.id,
    name: action.name || action.id,
    desc: action.description || describeAction(action),
    tag:
      (action.scope === "node" ? "限地点" : "全局") +
      (action.enabled === false ? " · 已停用" : ""),
    group: groupOfAction(action),
  }));
}

function toolItems() {
  // 第一条是"引用全局配置"的选项：动作只说"我要联网搜索"，用哪个工具由全局决定
  const pinned = {
    id: GLOBAL_SEARCH_TOOL,
    name: GLOBAL_SEARCH_LABEL,
    desc:
      "用「设置 → 高级功能 → 联网搜索」里配的工具。以后换搜索工具只改那一处，" +
      "这个动作不用跟着改。\n",
    group: "本插件",
  };
  if (!ui.tools.length) {
    return [pinned, { id: "", name: "（AstrBot 还没有注册任何工具）", desc: "", group: "其它" }];
  }
  return [pinned, ...ui.tools.map((tool) => ({
    id: tool.name,
    name:
      (tool.source === "official" ? "官方 · " : tool.plugin ? `插件 · ` : "") + tool.name,
    desc:
      (tool.source === "official"
        ? "官方内置工具（AstrBot 自带，不用额外装插件）。\n"
        : tool.plugin
          ? `来自插件「${tool.plugin}」。\n`
        : "") +
      (isSelfSendTool(tool.name)
        ? "⚠ 这是「直接把消息发到当前会话」的内置工具：插件里选它不会生效（会绕过回复管线，导致表情包识别、分段回复、发送前插件都不生效）。\n"
        : "") + (tool.description || ""),
    group: toolGroup(tool),
  }))];
}

/** 工具按来源分组：官方搜索 / 官方其它 / 插件（按提供它的插件名分）。 */
function toolGroup(tool) {
  const name = String(tool.name || "");
  if (tool.source === "official") {
    return /^web_search_|^tavily_|^exa_|^firecrawl_|^bocha|^anysearch/i.test(name)
      ? "官方 · 搜索"
      : "官方 · 其它";
  }
  const owner = String(tool.plugin || "").trim();
  if (owner) return `插件 · ${owner}`;
  if (/^web_search_|search$/i.test(name)) return "插件 · 搜索";
  return "插件 · 其它";
}

/** AstrBot 内置的直发消息工具：插件不会调用它们。 */
function isSelfSendTool(name) {
  return ["send_message_to_user", "send_message", "send_msg", "reply_message"].includes(
    String(name || "").trim().toLowerCase(),
  );
}

function describeAction(action) {
  const category = action.category === "continuous" ? "持续" : "瞬时";
  const level = { template: "模板", single: "单轮", tool: "工具" }[action.llm_level] || "";
  const scope = action.scope === "node" ? `仅 ${(action.allowed_nodes || []).join("/")}` : "全局";
  return `${category} · ${level} · ${scope}`;
}

/* ================================================================== */
/* 地图                                                                */
/* ================================================================== */

function renderMap() {
  updateMapToolbar();
  renderWeatherBanner();
  return ui.mapLevel === "world" ? renderWorldMap() : renderZoneMap();
}

/** 天气图标：按描述里的关键词猜一个，猜不到就用通用的。 */
function weatherIcon(desc) {
  const text = String(desc || "");
  if (/雷/.test(text)) return "⛈";
  if (/雪|冰/.test(text)) return "❄️";
  if (/雨/.test(text)) return "🌧";
  if (/雾|霾/.test(text)) return "🌫";
  if (/阴/.test(text)) return "☁️";
  if (/云/.test(text)) return "⛅";
  if (/晴/.test(text)) return "☀️";
  return "🌤";
}

/** "多久之前"由前端算：她那边是按自然日算的（今天说小时、昨天前天直说）。 */
function weatherAge(at) {
  const stamp = Number(at);
  if (!Number.isFinite(stamp) || stamp <= 0) return "";
  const now = new Date();
  const then = new Date(stamp * 1000);
  const hours = (now.getTime() - then.getTime()) / 3600000;
  if (hours < 1) return "刚刚查的";
  const startOfDay = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const days = Math.round((startOfDay(now) - startOfDay(then)) / 86400000);
  if (days <= 0) return `${Math.floor(hours)} 小时前查的`;
  if (days === 1) return "昨天查的";
  if (days === 2) return "前天查的";
  return `${days} 天前查的`;
}

/** 地图页顶部的天气条：数据来自 /config 的 weather 字段。 */
function renderWeatherBanner() {
  const box = $("map-weather");
  if (!box) return;
  const weather = (ui.config && ui.config.weather) || {};
  const text = String(weather.text || "").trim();
  const main = $("map-weather-main");
  const age = $("map-weather-age");
  if (!text) {
    box.classList.remove("hidden");
    $("map-weather-icon").textContent = "🌤";
    main.textContent = "还没有天气记录";
    main.className = "weather-main muted";
    age.textContent = "";
    return;
  }
  box.classList.remove("hidden");
  box.setAttribute("title", text);
  $("map-weather-icon").textContent = weatherIcon(weather.desc);
  main.className = "weather-main";
  const head = [weather.city, weather.desc, weather.temp].filter(Boolean).join(" ");
  main.textContent = head || text;
  const rest = head && text !== head ? text : "";
  age.innerHTML = "";
  if (rest) {
    age.appendChild(el("span", "weather-line", rest));
    age.appendChild(el("span", "", " · "));
  }
  age.appendChild(el("span", "", weatherAge(weather.at)));
  if (!head) main.textContent = text;
}

async function refreshWeatherNow() {
  const button = $("map-weather-refresh");
  if (!button) return;
  button.disabled = true;
  button.textContent = "查询中…";
  try {
    // 天气是全局的（后端不要求 session），但顺手把当前选中的会话带上，便于排查
    const session =
      ($("map-session") && $("map-session").value) ||
      ($("status-session") && $("status-session").value) ||
      "";
    const data = await apiPost("state/action", {
      action: "refresh_weather",
      ...(session ? { session } : {}),
    });
    if (data && data.weather) {
      ui.config.weather = data.weather;
    }
    renderWeatherBanner();
    // 插件页在 sandbox iframe 里，window.alert 会被拦掉；统一用页面内的 toast
    if (data && data.note) {
      toast(data.note);
    } else {
      toast("天气已更新");
    }
  } catch (error) {
    toast(`查天气失败：${error.message || error}`);
  } finally {
    button.disabled = false;
    button.textContent = "刷新";
  }
}

/** 世界地图：只画区域节点和区域之间的连线。 */
function renderWorldMap() {
  const container = $("canvas-nodes");
  const svg = $("canvas-edges");
  container.innerHTML = "";
  svg.innerHTML = "";

  const focusSession = $("map-session") ? $("map-session").value : "";
  const showAll = $("map-show-all") ? $("map-show-all").checked : false;
  const positions = (ui.overview || []).filter(
    (item) => item.node_id && (showAll || item.session_id === focusSession),
  );
  const zoneOfSession = (item) => {
    const node = nodes().find((row) => row.id === item.node_id);
    return node ? zoneOfNode(node) : "";
  };

  const canvas = $("canvas");
  const maxX = zones().reduce((acc, zone) => Math.max(acc, num(zone.x)), 0);
  const maxY = zones().reduce((acc, zone) => Math.max(acc, num(zone.y)), 0);
  // 尺寸给"里面的内容层"，不给画布本身：
  // 给画布设 min-width 会让它撑破栅格列、盖住右侧属性面板（节点拉远时必现）。
  const mapWidth = Math.max(720, maxX + 200);
  const mapHeight = Math.max(380, maxY + 120);
  container.style.width = `${mapWidth}px`;
  container.style.height = `${mapHeight}px`;
  svg.setAttribute("width", String(mapWidth));
  svg.setAttribute("height", String(mapHeight));

  const byId = {};
  zones().forEach((zone) => {
    byId[zone.id] = zone;
  });

  const seen = {};
  zoneEdges().forEach((edge) => {
    const from = byId[edge.from_zone];
    const to = byId[edge.to_zone];
    if (!from || !to) return;
    // 连线跟着主题的灰阶走，别写死颜色
    const linkColor = getComputedStyle(document.body).getPropertyValue("--muted").trim() || "#7f8ea8";
    const key = [edge.from_zone, edge.to_zone].sort().join("|");
    seen[key] = (seen[key] || 0) + 1;
    const offset = (seen[key] - 1) * 12;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", num(from.x) + 48);
    line.setAttribute("y1", num(from.y) + 20);
    line.setAttribute("x2", num(to.x) + 48);
    line.setAttribute("y2", num(to.y) + 20);
    line.setAttribute("stroke", linkColor);
    line.setAttribute("stroke-width", "2");
    if (!edge.bidirectional) line.setAttribute("stroke-dasharray", "6 4");
    svg.appendChild(line);
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent =
      `${nodeLabel(edge.from_node)} ↔ ${nodeLabel(edge.to_node)}（${num(edge.ticks, 1)} tick）`;
    line.appendChild(title);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", (num(from.x) + num(to.x)) / 2 + 48 + offset);
    label.setAttribute("y", (num(from.y) + num(to.y)) / 2 + 16 + offset);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "edge-ticks");
    label.textContent = `${nodeLabel(edge.from_node)} ↔ ${nodeLabel(edge.to_node)}`;
    svg.appendChild(label);
  });

  zones().forEach((zone) => {
    const box = el("div", "node");
    if (zone.id === ui.selectedZone) box.classList.add("selected");
    const inside = nodesInZone(zone.id);
    const markers = positions.filter((item) => zoneOfSession(item) === zone.id);
    if (markers.length) {
      box.classList.add("here");
      const focused = markers.some((item) => item.session_id === focusSession);
      const label = focused
        ? markers.length > 1
          ? `她在这个区域（共 ${markers.length} 个会话）`
          : "她在这个区域"
        : `${markers.length} 个会话在这个区域`;
      if (!focused) box.classList.add("other");
      const badge = el("div", "node-here", label);
      badge.setAttribute(
        "title",
        markers
          .map(
            (item) =>
              `${sessionShortName(item.session_id)}：${nodeLabel(item.node_id)}`,
          )
          .join("\n"),
      );
      box.appendChild(badge);
    }
    box.style.left = `${num(zone.x)}px`;
    box.style.top = `${num(zone.y)}px`;
    box.style.borderColor = zone.color || "";
    box.dataset.id = zone.id;
    box.dataset.kind = "zone";
    box.appendChild(el("div", "node-name", zone.name || zone.id));
    box.appendChild(el("div", "node-id", `${zone.id}｜${inside.length} 个地点`));
    box.addEventListener("mousedown", (event) => startDrag(event, zone));
    // 单击只切选中态（重建 DOM 会让双击永远打不到同一个元素）
    box.addEventListener("click", () => selectZone(zone.id));
    box.addEventListener("dblclick", (event) => {
      event.preventDefault();
      enterZone(zone.id);
    });
    container.appendChild(box);
  });
}

/** 选中一个区域：只更新高亮和右侧表单，不重画地图。 */
function selectZone(zoneId) {
  ui.selectedZone = zoneId;
  document.querySelectorAll("#canvas-nodes .node[data-kind='zone']").forEach((item) => {
    item.classList.toggle("selected", item.dataset.id === zoneId);
  });
  renderNodeForm();
}

/** 选中一个地点（区域地图里用）。 */
function selectNode(nodeId) {
  ui.selectedNode = nodeId;
  document.querySelectorAll("#canvas-nodes .node[data-kind='node']").forEach((item) => {
    item.classList.toggle("selected", item.dataset.id === nodeId);
  });
  renderNodeForm();
}

/** 区域地图：只画这个区域里的地点、区域内的连线，以及通往其他区域的「出口」。 */
function renderZoneMap() {
  const container = $("canvas-nodes");
  const svg = $("canvas-edges");
  container.innerHTML = "";
  svg.innerHTML = "";
  const zoneId = ui.selectedZone;
  const inside = nodesInZone(zoneId);
  const insideIds = new Set(inside.map((node) => node.id));

  const focusSession = $("map-session") ? $("map-session").value : "";
  const showAll = $("map-show-all") ? $("map-show-all").checked : false;
  const positions = (ui.overview || []).filter(
    (item) =>
      insideIds.has(item.node_id) && (showAll || item.session_id === focusSession),
  );
  const hereMarkers = (nodeId) => positions.filter((item) => item.node_id === nodeId);

  const canvas = $("canvas");
  const maxX = inside.reduce((acc, node) => Math.max(acc, num(node.x)), 0);
  const maxY = inside.reduce((acc, node) => Math.max(acc, num(node.y)), 0);
  const mapWidth = Math.max(720, maxX + 240);
  const mapHeight = Math.max(380, maxY + 160);
  container.style.width = `${mapWidth}px`;
  container.style.height = `${mapHeight}px`;
  svg.setAttribute("width", String(mapWidth));
  svg.setAttribute("height", String(mapHeight));

  const byId = {};
  inside.forEach((node) => {
    byId[node.id] = node;
  });

  edges().forEach((edge) => {
    const from = byId[edge.from];
    const to = byId[edge.to];
    if (!from || !to) return;
    const linkColor = getComputedStyle(document.body).getPropertyValue("--muted").trim() || "#9aa7bd";
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", num(from.x) + 48);
    line.setAttribute("y1", num(from.y) + 20);
    line.setAttribute("x2", num(to.x) + 48);
    line.setAttribute("y2", num(to.y) + 20);
    line.setAttribute("stroke", linkColor);
    line.setAttribute("stroke-width", "2");
    if (!edge.bidirectional) line.setAttribute("stroke-dasharray", "6 4");
    svg.appendChild(line);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", (num(from.x) + num(to.x)) / 2 + 48);
    label.setAttribute("y", (num(from.y) + num(to.y)) / 2 + 16);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "edge-ticks");
    label.textContent = `${num(edge.ticks, 1)} tick`;
    svg.appendChild(label);
  });

  inside.forEach((node) => {
    const box = el("div", "node");
    if (node.id === ui.selectedNode) box.classList.add("selected");
    const markers = hereMarkers(node.id);
    if (markers.length) {
      box.classList.add("here");
      const focused = markers.some((item) => item.session_id === focusSession);
      let label = "";
      if (focused) {
        label = markers.length > 1 ? `她在这里（共 ${markers.length} 个会话）` : "她在这里";
      } else if (markers.length === 1) {
        label = sessionShortName(markers[0].session_id);
        box.classList.add("other");
      } else {
        label = `${markers.length} 个会话在这里`;
        box.classList.add("other");
      }
      const badge = el("div", "node-here", label);
      badge.setAttribute(
        "title",
        markers.map((item) => sessionShortName(item.session_id)).join("\n"),
      );
      box.appendChild(badge);
    }
    box.style.left = `${num(node.x)}px`;
    box.style.top = `${num(node.y)}px`;
    box.style.borderColor = node.color || "";
    box.dataset.id = node.id;
    box.dataset.kind = "node";
    box.appendChild(el("div", "node-name", node.name || node.id));
    box.appendChild(el("div", "node-id", node.id));
    box.addEventListener("mousedown", (event) => startDrag(event, node));
    box.addEventListener("click", () => selectNode(node.id));
    container.appendChild(box);
  });

  // 通往其他区域的出口：标在那一端地点旁边，点一下跳到对面区域
  const exits = zoneEdges().filter(
    (edge) => edge.from_zone === zoneId || edge.to_zone === zoneId,
  );
  exits.forEach((edge, index) => {
    const here = edge.from_zone === zoneId ? edge.from_node : edge.to_node;
    const thereZone = edge.from_zone === zoneId ? edge.to_zone : edge.from_zone;
    const thereNode = edge.from_zone === zoneId ? edge.to_node : edge.from_node;
    const anchor = byId[here];
    if (!anchor) return;
    const zone = zones().find((item) => item.id === thereZone);
    const chip = el(
      "div",
      "portal-chip",
      `→ ${zone ? zone.name || zone.id : thereZone}·${nodeLabel(thereNode)}（${num(edge.ticks, 1)} tick）`,
    );
    chip.style.left = `${num(anchor.x) + 8}px`;
    chip.style.top = `${num(anchor.y) + 62 + index * 22}px`;
    chip.title = "点击查看对面区域";
    chip.addEventListener("click", (event) => {
      event.stopPropagation();
      enterZone(thereZone);
    });
    container.appendChild(chip);
  });
}

function enterZone(zoneId) {
  ui.mapLevel = "zone";
  ui.selectedZone = zoneId;
  ui.selectedNode = nodesInZone(zoneId)[0] ? nodesInZone(zoneId)[0].id : "";
  renderMap();
  renderNodeForm();
}

function backToWorldMap() {
  ui.mapLevel = "world";
  renderMap();
  renderNodeForm();
}

function updateMapToolbar() {
  const world = ui.mapLevel === "world";
  const zone = zones().find((item) => item.id === ui.selectedZone);
  $("toolbar-world").classList.toggle("hidden", !world);
  $("toolbar-zone").classList.toggle("hidden", world);
  $("map-back").classList.toggle("hidden", world);
  $("map-crumbs").textContent = world
    ? "世界地图"
    : `世界地图 / ${zone ? zone.name || zone.id : ""}`;
  $("map-crumbs-note").textContent = world
    ? "世界地图上是各个区域；点区域进去看它里面的地点。"
    : "区域地图：只显示这个区域里的地点，左上角可以返回世界地图。";
  $("node-title").textContent = world ? "区域属性" : "地点属性";
}

function startDrag(event, node) {
  event.preventDefault();
  const startX = event.clientX;
  const startY = event.clientY;
  const originX = num(node.x);
  const originY = num(node.y);
  const canvas = $("canvas");

  function onMove(moveEvent) {
    const dx = moveEvent.clientX - startX + canvas.scrollLeft;
    const dy = moveEvent.clientY - startY + canvas.scrollTop;
    node.x = Math.max(0, Math.round(originX + dx));
    node.y = Math.max(0, Math.round(originY + dy));
    renderMap();
    renderNodeForm();
  }

  function onUp() {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  }

  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
  ui.selectedNode = node.id;
}

/* ================================================================== */
/* 节点表单                                                            */
/* ================================================================== */

/* ---------------- 区域属性（世界地图那一层） ---------------- */

function renderZoneForm(form) {
  const zone = zones().find((item) => item.id === ui.selectedZone);
  $("node-title").textContent = zone ? `区域属性：${zone.name || zone.id}` : "区域属性";
  if (!zone) {
    form.appendChild(
      el("p", "muted", "世界地图上是各个区域。点一个区域进行编辑，或点「新增区域」。"),
    );
    return;
  }
  const inside = nodesInZone(zone.id);
  form.appendChild(
    inputField("区域 ID", zone.id || "", (value) => renameZone(zone.id, value), {
      hint: "内部标识，房间靠它归属区域；改名会自动同步房间里记的归属。",
    }),
  );
  form.appendChild(
    inputField("名称", zone.name || "", (value) => (zone.name = value), {
      hint: "世界地图上显示的名字，也会出现在提示词里。",
    }),
  );
  form.appendChild(
    textareaField("区域说明", zone.note || "", (value) => (zone.note = value), {
      hint: "一句话描述这个区域（例如「外面人来人往，容易遇到新鲜事」），会写进提示词。",
      rows: 2,
    }),
  );
  form.appendChild(
    inputField("颜色", zone.color || "#7FB2E5", (value) => (zone.color = value), {
      hint: "只影响世界地图上的显示。",
      type: "color",
    }),
  );
  form.appendChild(
    inputField("图标", zone.icon || "", (value) => (zone.icon = value), {
      hint: "可选，写个 emoji 也行。",
    }),
  );
  form.appendChild(
    checkboxField(
      "这是她的家",
      zone.is_home === true,
      (value) => {
        // 只能有一个家：勾了这个，别的区域自动取消
        zones().forEach((item) => {
          item.is_home = item.id === zone.id ? Boolean(value) : false;
        });
        renderNodeForm();
        renderZoneList();
      },
      {
        hint: "久待会想出去走走；一个区域就够，不勾就按卧室所在区域算。",
      },
    ),
  );
  const position = el("div", "row-item");
  position.appendChild(
    inputField("世界地图 X", num(zone.x), (value) => {
      zone.x = num(value);
      renderMap();
    }),
  );
  position.appendChild(
    inputField("世界地图 Y", num(zone.y), (value) => {
      zone.y = num(value);
      renderMap();
    }),
  );
  form.appendChild(position);

  form.appendChild(portalEditor(zone));

  form.appendChild(zoneGeneratorBox(zone));

  form.appendChild(el("div", "section-title", `区域内的地点（${inside.length}）`));
  const list = el("div", "rows");
  inside.forEach((node) => {
    const line = el("div", "row-item");
    line.appendChild(el("span", "grow", `${node.name || node.id}（${node.id}）`));
    const go = el("button", "small ghost", "查看");
    go.type = "button";
    go.addEventListener("click", () => {
      ui.selectedNode = node.id;
      enterZone(zone.id);
      ui.selectedNode = node.id;
      renderMap();
      renderNodeForm();
    });
    line.appendChild(go);
    list.appendChild(line);
  });
  if (!inside.length) {
    list.appendChild(el("p", "muted", "这个区域还没有地点，点「新增节点」加一个。"));
  }
  form.appendChild(list);
}

/** 用大模型给这个区域批量生成动作 / 地点。 */
function zoneGeneratorBox(zone) {
  const wrapper = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", "用大模型生成地点"));
  title.appendChild(
    tipBox(
      "按这个区域的样子生成新地点，自动摆位并接上路线。结果先列在弹窗，" +
        "勾选后才写进配置；动作另到各地点里生成。",
    ),
  );
  wrapper.appendChild(title);

  const row2 = el("div", "row-item");
  const countInput = document.createElement("input");
  countInput.type = "number";
  countInput.min = "1";
  countInput.max = "5";
  countInput.value = "3";
  countInput.className = "w-sm";
  countInput.title = "一次生成几个地点（最多 5 个）";
  const runNodes = el("button", "ghost", "生成地点");
  runNodes.type = "button";
  runNodes.addEventListener("click", () =>
    generateZoneNodes(zone.id, countInput.value, runNodes),
  );
  row2.appendChild(el("span", "muted", "一次生成"));
  row2.appendChild(countInput);
  row2.appendChild(el("span", "muted", "个地点"));
  row2.appendChild(runNodes);
  wrapper.appendChild(row2);

  // 批量给这个区域里的每个地点生成动作（每个地点各生成几个）
  const row3 = el("div", "row-item");
  const actionCount = document.createElement("input");
  actionCount.type = "number";
  actionCount.min = "1";
  actionCount.max = "5";
  actionCount.value = "3";
  actionCount.className = "w-sm";
  actionCount.title = "每个地点各生成几个动作（最多 5 个）";
  const runActions = el("button", "ghost", "生成动作");
  runActions.type = "button";
  runActions.title = "给这个区域里的每个地点分别生成几个动作，弹窗里勾选后才写进配置";
  runActions.addEventListener("click", () =>
    generateZoneActions(zone.id, actionCount.value, runActions),
  );
  row3.appendChild(el("span", "muted", "每个地点生成"));
  row3.appendChild(actionCount);
  row3.appendChild(el("span", "muted", "个动作"));
  row3.appendChild(runActions);
  wrapper.appendChild(row3);
  return wrapper;
}

/** 地点自己的生成入口：按这个地点的样子生成几个动作。 */
function nodeGeneratorBox(node) {
  const wrapper = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", "用大模型生成动作"));
  title.appendChild(
    tipBox(
      "按这个地点（及其区域）的样子生成动作，结果先列在弹窗里，" +
        "改好名字、勾选后才写进配置。模型用「内容生成模型」。",
    ),
  );
  wrapper.appendChild(title);
  const row = el("div", "row-item");
  const countInput = document.createElement("input");
  countInput.type = "number";
  countInput.min = "1";
  countInput.max = "5";
  countInput.value = "3";
  countInput.className = "w-sm";
  countInput.title = "生成几个动作（最多 5 个）";
  const run = el("button", "ghost", "生成动作");
  run.type = "button";
  run.addEventListener("click", () =>
    generateNodeActions(node.id, countInput.value, run),
  );
  row.appendChild(el("span", "muted", "生成"));
  row.appendChild(countInput);
  row.appendChild(el("span", "muted", "个动作"));
  row.appendChild(run);
  wrapper.appendChild(row);
  return wrapper;
}

/** 生成按钮的「正在跑」状态：禁用 + 换文案，防止连点。 */
async function withLoading(button, text, task) {
  if (!button) return task();
  if (button.dataset.busy === "1") return undefined;
  const original = button.textContent;
  button.dataset.busy = "1";
  button.disabled = true;
  button.textContent = text;
  try {
    return await task();
  } finally {
    button.dataset.busy = "";
    button.disabled = false;
    button.textContent = original;
  }
}

/** 生成动作：拉草稿 → 弹窗预览（可改名、改归属、勾选）→ 确认后才写进配置。 */
async function generateNodeActions(nodeId, perNode, button) {
  return withLoading(button, "生成中…", async () => {
    try {
      const result = await apiPost("generate/actions", {
        node: nodeId,
        per_node: num(perNode, 3),
      });
      const drafts = result.actions || [];
      if (!drafts.length) {
        toast((result.problems || []).join("；") || "没有生成出可用的动作");
        return;
      }
      openCustomDialog({
        title: `生成的动作（${drafts.length} 个）`,
        hint:
          "先看清楚再决定：可以改名字、改归属地点、勾掉不要的。「确认加入」会直接写进配置并生效，不用再点保存。",
        confirmText: "确认加入选中的",
        build: (body) => buildActionDraftList(body, drafts, result.problems || []),
        onSubmit: () => commitGeneratedActions(drafts),
      });
    } catch (error) {
      toast(error.message || "生成失败");
    }
  });
}

/** 区域级批量生成动作：和单个地点走同一套草稿弹窗。 */
async function generateZoneActions(zoneId, perNode, button) {
  return withLoading(button, "生成中…", async () => {
    try {
      const result = await apiPost("generate/actions", {
        zone: zoneId,
        per_node: num(perNode, 3),
      });
      const drafts = result.actions || [];
      if (!drafts.length) {
        toast((result.problems || []).join("；") || "没有生成出可用的动作");
        return;
      }
      openCustomDialog({
        title: `生成的动作（${drafts.length} 个）`,
        hint: "每个地点各自的候选动作；改完、勾选之后才会写进配置。",
        confirmText: "确认加入选中的",
        build: (body) => buildActionDraftList(body, drafts, result.problems || []),
        onSubmit: () => commitGeneratedActions(drafts),
      });
    } catch (error) {
      toast(error.message || "生成失败");
    }
  });
}

function buildActionDraftList(body, drafts, problems) {
  drafts.forEach((draft) => {
    draft._pick = draft._pick !== false;
  });
  const toolbar = el("div", "row-item");
  const all = el("button", "ghost small", "全选");
  all.type = "button";
  all.addEventListener("click", () => {
    drafts.forEach((item) => (item._pick = true));
    renderDraftRows();
  });
  const none = el("button", "ghost small", "全不选");
  none.type = "button";
  none.addEventListener("click", () => {
    drafts.forEach((item) => (item._pick = false));
    renderDraftRows();
  });
  toolbar.appendChild(all);
  toolbar.appendChild(none);
  const count = el("span", "muted", "");
  toolbar.appendChild(count);
  body.appendChild(toolbar);

  const list = el("div", "draft-list");
  body.appendChild(list);

  if (problems.length) {
    const note = el("div", "hint", `模型那边有这些情况：\n${problems.join("\n")}`);
    body.appendChild(note);
  }

  function renderDraftRows() {
    list.innerHTML = "";
    count.textContent = `已勾选 ${drafts.filter((item) => item._pick !== false).length} 个`;
    drafts.forEach((draft, index) => {
      const row = el("div", "draft-row");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = draft._pick !== false;
      box.addEventListener("change", () => {
        draft._pick = box.checked;
        count.textContent = `已勾选 ${drafts.filter((item) => item._pick !== false).length} 个`;
      });
      row.appendChild(box);

      const nameInput = document.createElement("input");
      nameInput.className = "grow";
      nameInput.value = draft.name || draft.id;
      nameInput.title = "动作名称";
      nameInput.addEventListener("input", () => (draft.name = nameInput.value));
      row.appendChild(nameInput);

      const nodeButton = el(
        "button",
        "ghost small",
        (draft.allowed_nodes || []).map((id) => nodeLabel(id)).join("、") || "选地点",
      );
      nodeButton.type = "button";
      nodeButton.title = "这个动作属于哪些地点";
      nodeButton.addEventListener("click", () => {
        openPicker({
          title: "这个动作属于哪些地点？",
          items: nodeItems(),
          selected: draft.allowed_nodes || [],
          multi: true,
          onConfirm: (chosen) => {
            draft.allowed_nodes = chosen;
            draft.scope = chosen.length ? "node" : "global";
            nodeButton.textContent = chosen.map((id) => nodeLabel(id)).join("、") || "全局";
          },
        });
      });
      row.appendChild(nodeButton);

      const remove = el("button", "icon-btn danger", "🗑");
      remove.type = "button";
      remove.title = "不要这个";
      remove.addEventListener("click", () => {
        drafts.splice(index, 1);
        renderDraftRows();
      });
      row.appendChild(remove);

      row.appendChild(
        el("div", "draft-desc", `${draft.id}｜${draft.description || ""}`),
      );
      list.appendChild(row);
    });
    if (!drafts.length) list.appendChild(el("p", "muted", "都删掉了。"));
  }

  renderDraftRows();
}

async function commitGeneratedActions(drafts) {
  const picked = drafts.filter((item) => item._pick !== false);
  if (!picked.length) {
    toast("一个都没勾");
    return false;
  }
  const used = new Set(actions().map((item) => item.id));
  let added = 0;
  picked.forEach((draft) => {
    const payload = { ...draft };
    delete payload._pick;
    if (used.has(payload.id)) {
      let id = `${payload.id}_2`;
      let index = 3;
      while (used.has(id)) id = `${payload.id}_${index++}`;
      payload.id = id;
    }
    used.add(payload.id);
    payload.created_at = payload.created_at || Date.now();
    actions().push(payload);
    added += 1;
  });
  markDirty();
  renderActionGrid();
  // 生成结果确认即保存：不然用户以为没添加成功，还要再找一次右上角的「保存」。
  // 但**保存成没成要看回执**：失败时草稿留着、把原因说清，不许报"并保存"。
  const saved = await saveAll();
  toast(
    saved && saved.ok
      ? `已加入 ${added} 个动作并保存`
      : `已加入 ${added} 个动作，但没保存成功：改动还在编辑器里，点右上角「保存」重试`,
  );
  // 草稿已经进了配置列表，关窗是必须的（留着再点一次"确认"会重复加入）
  return true;
}

/** 生成地点：草稿带自动摆位与自动连线，确认后才加进来。 */
async function generateZoneNodes(zoneId, count, button) {
  return withLoading(button, "生成中…", async () => {
    try {
      const result = await apiPost("generate/nodes", {
        zone: zoneId,
        count: num(count, 3),
      });
      const drafts = result.nodes || [];
      if (!drafts.length) {
        toast((result.problems || []).join("；") || "没有生成出可用的地点");
        return;
      }
      const problems = result.problems || [];
      openCustomDialog({
        title: `生成的地点（${drafts.length} 个）`,
        hint:
          "自动摆好位置并按区域连成链（首个连最近的地点，默认 1 tick）。「确认加入」写入配置后立即生效。" +
          (problems.length ? `模型那边有这些情况：${problems.join("；")}` : ""),
        confirmText: "确认加入选中的",
        build: (body) => buildNodeDraftList(body, drafts),
        onSubmit: () => commitGeneratedNodes(drafts, result.edges || []),
      });
    } catch (error) {
      toast(error.message || "生成失败");
    }
  });
}

function buildNodeDraftList(body, drafts) {
  drafts.forEach((draft) => {
    draft._pick = draft._pick !== false;
  });
  const list = el("div", "draft-list");
  drafts.forEach((draft, index) => {
    const row = el("div", "draft-row");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = true;
    box.addEventListener("change", () => (draft._pick = box.checked));
    row.appendChild(box);

    const nameInput = document.createElement("input");
    nameInput.className = "grow";
    nameInput.value = draft.name || draft.id;
    nameInput.title = "地点名称";
    nameInput.addEventListener("input", () => (draft.name = nameInput.value));
    row.appendChild(nameInput);

    const remove = el("button", "icon-btn danger", "🗑");
    remove.type = "button";
    remove.title = "不要这个";
    remove.addEventListener("click", () => {
      drafts.splice(index, 1);
      renderNodeDraftRows();
    });
    row.appendChild(remove);

    const desc = document.createElement("input");
    desc.className = "grow";
    desc.value = draft.prompt || "";
    desc.title = "这里是什么样（会进提示词）";
    desc.addEventListener("input", () => (draft.prompt = desc.value));
    row.appendChild(desc);

    row.appendChild(el("div", "draft-desc", draft.id));
    list.appendChild(row);
  });
  body.appendChild(list);
  if (!drafts.length) list.appendChild(el("p", "muted", "都删掉了。"));
}

async function commitGeneratedNodes(drafts, newEdges) {
  const picked = drafts.filter((item) => item._pick !== false);
  if (!picked.length) {
    toast("一个都没勾");
    return false;
  }
  const usedNodes = new Set(nodes().map((item) => item.id));
  picked.forEach((draft) => {
    const payload = { ...draft };
    delete payload._pick;
    if (usedNodes.has(payload.id)) {
      let id = `${payload.id}_2`;
      let index = 3;
      while (usedNodes.has(id)) id = `${payload.id}_${index++}`;
      payload.id = id;
    }
    usedNodes.add(payload.id);
    nodes().push(payload);
  });
  const kept = new Set(picked.map((item) => item.id));
  (newEdges || [])
    .filter((edge) => kept.has(edge.from) && kept.has(edge.to))
    .forEach((edge) => edges().push({ ...edge }));
  markDirty();
  renderMap();
  renderNodeForm();
  // 生成结果确认即保存（同样以回执为准：失败要留草稿、说实话）
  const saved = await saveAll();
  toast(
    saved && saved.ok
      ? `已加入 ${picked.length} 个地点（含自动连线）并保存`
      : `已加入 ${picked.length} 个地点，但没保存成功：改动还在编辑器里，点右上角「保存」重试`,
  );
  return true;
}

function portalEditor(zone) {
  const wrapper = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", "跨区连线（通往其他区域的通道）"));
  title.appendChild(
    tipBox(
      "每条线自带两端地点，例如「北门 ↔ 商场大门」。同一对区域可以有多条线，" +
        "她按最短路线自动挑一条。",
    ),
  );
  wrapper.appendChild(title);

  const list = el("div", "rows");
  const mine = zoneEdges().filter(
    (edge) => edge.from_zone === zone.id || edge.to_zone === zone.id,
  );
  mine.forEach((edge) => list.appendChild(portalRow(edge, zone)));
  if (!mine.length) {
    list.appendChild(el("p", "muted", "还没有通往其他区域的通道。"));
  }
  wrapper.appendChild(list);
  return wrapper;
}

function portalRow(edge, zone) {
  const line = el("div", "row-item edge-editor");
  const isFrom = edge.from_zone === zone.id;
  const hereZone = () => (isFrom ? edge.from_zone : edge.to_zone);
  const thereZone = () => (isFrom ? edge.to_zone : edge.from_zone);
  const hereNode = () => (isFrom ? edge.from_node : edge.to_node);
  const thereNode = () => (isFrom ? edge.to_node : edge.from_node);

  const hereSelect = document.createElement("select");
  hereSelect.title = "这条通道在本区域的哪一端";
  nodesInZone(hereZone()).forEach((node) => {
    hereSelect.appendChild(option(node.id, `${node.name || node.id}`));
  });
  hereSelect.value = hereNode();
  hereSelect.addEventListener("change", () => {
    if (isFrom) edge.from_node = hereSelect.value;
    else edge.to_node = hereSelect.value;
    markDirty();
    renderMap();
  });
  line.appendChild(hereSelect);
  line.appendChild(el("span", "muted", "↔"));

  const zoneSelect = document.createElement("select");
  zoneSelect.title = "对面是哪个区域";
  zones()
    .filter((item) => item.id !== zone.id)
    .forEach((item) => zoneSelect.appendChild(option(item.id, item.name || item.id)));
  zoneSelect.value = thereZone();
  zoneSelect.addEventListener("change", () => {
    const target = zoneSelect.value;
    const firstNode = nodesInZone(target)[0];
    if (isFrom) {
      edge.to_zone = target;
      edge.to_node = firstNode ? firstNode.id : "";
    } else {
      edge.from_zone = target;
      edge.from_node = firstNode ? firstNode.id : "";
    }
    markDirty();
    renderNodeForm();
    renderMap();
  });
  line.appendChild(zoneSelect);

  const thereSelect = document.createElement("select");
  thereSelect.title = "对面的哪一端";
  nodesInZone(thereZone()).forEach((node) => {
    thereSelect.appendChild(option(node.id, `${node.name || node.id}`));
  });
  thereSelect.value = thereNode();
  thereSelect.addEventListener("change", () => {
    if (isFrom) edge.to_node = thereSelect.value;
    else edge.from_node = thereSelect.value;
    markDirty();
    renderMap();
  });
  line.appendChild(thereSelect);

  // 只有一个数字输入框时没人知道那是"走过去要几步"：补上前后文字说明
  const tickField = el("span", "row-item");
  tickField.appendChild(el("span", "muted", "走过去"));
  const tickInput = document.createElement("input");
  tickInput.type = "number";
  tickInput.min = "1";
  tickInput.className = "w-sm";
  tickInput.value = num(edge.ticks, 1);
  tickInput.title = "从这一端走到对面那一端要花几个 tick（1 tick = 世界时钟走一格，默认 60 秒）";
  tickInput.addEventListener("change", () => {
    edge.ticks = Math.max(1, Math.round(num(tickInput.value, 1)));
    markDirty();
    renderMap();
  });
  tickField.appendChild(tickInput);
  tickField.appendChild(el("span", "muted", "tick"));
  line.appendChild(tickField);

  const bidi = el("label", "inline");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = edge.bidirectional !== false;
  box.addEventListener("change", () => {
    edge.bidirectional = box.checked;
    markDirty();
    renderMap();
  });
  bidi.appendChild(box);
  bidi.appendChild(el("span", "", "双向"));
  line.appendChild(bidi);

  const remove = el("button", "icon-btn danger", "🗑");
  remove.type = "button";
  remove.title = "删除这条通道";
  remove.addEventListener("click", () => {
    ui.config.world.zone_edges = zoneEdges().filter((row) => row !== edge);
    markDirty();
    renderNodeForm();
    renderMap();
  });
  line.appendChild(remove);
  return line;
}

function addPortal() {
  const zone = zones().find((item) => item.id === ui.selectedZone);
  if (!zone) return;
  const others = zones().filter((item) => item.id !== zone.id);
  if (!others.length) {
    toast("至少要有两个区域才能跨区连线");
    return;
  }
  const here = nodesInZone(zone.id);
  if (!here.length) {
    toast(`「${zone.name || zone.id}」里还没有地点，先加一个出口地点`);
    return;
  }
  const firstThere = nodesInZone(others[0].id);
  if (!firstThere.length) {
    toast(`「${others[0].name || others[0].id}」里还没有地点，先给它加一个入口`);
    return;
  }

  const draft = {
    to_zone: others[0].id,
    from_node: here[0].id,
    to_node: firstThere[0].id,
    ticks: 1,
    bidirectional: true,
  };

  const field = (label, hint, control) => {
    const wrap = el("label", "field");
    wrap.appendChild(fieldHead(label, hint));
    wrap.appendChild(control);
    return wrap;
  };
  const makeSelect = (choices, value, onChange) => {
    const select = document.createElement("select");
    choices.forEach((item) => select.appendChild(option(item.id, item.label)));
    select.value = value;
    select.addEventListener("change", () => onChange(select.value));
    return select;
  };

  openCustomDialog({
    title: `从「${zone.name || zone.id}」连到别的区域`,
    hint: "跨区连线是一对「门户」：两端各指定一个具体地点。同一对区域可以有多条，走路时自动挑最近的一条。",
    confirmText: "添加这条线",
    build: (body) => {
      body.appendChild(
        field(
          "去哪个区域",
          "跨到对面的哪个区域。",
          makeSelect(
            others.map((item) => ({ id: item.id, label: item.name || item.id })),
            draft.to_zone,
            (value) => {
              draft.to_zone = value;
              const list = nodesInZone(value);
              if (list.length) draft.to_node = list[0].id;
              render();
            },
          ),
        ),
      );
      const detail = el("div");
      body.appendChild(detail);

      function render() {
        detail.innerHTML = "";
        detail.appendChild(
          field(
            `本区域的出口（${zone.name || zone.id}）`,
            "她从这个地点跨出去。",
            makeSelect(
              here.map((item) => ({ id: item.id, label: `${item.name || item.id}（${item.id}）` })),
              draft.from_node,
              (value) => (draft.from_node = value),
            ),
          ),
        );
        const thereNodes = nodesInZone(draft.to_zone);
        const target = zones().find((item) => item.id === draft.to_zone);
        detail.appendChild(
          field(
            `对面的入口（${target ? target.name || target.id : draft.to_zone}）`,
            "她跨过去之后落在哪个地点。",
            makeSelect(
              thereNodes.length
                ? thereNodes.map((item) => ({
                    id: item.id,
                    label: `${item.name || item.id}（${item.id}）`,
                  }))
                : [{ id: "", label: "（这个区域里还没有地点）" }],
              draft.to_node,
              (value) => (draft.to_node = value),
            ),
          ),
        );
        const ticks = document.createElement("input");
        ticks.type = "number";
        ticks.min = "1";
        ticks.className = "w-sm";
        ticks.value = String(draft.ticks);
        ticks.addEventListener("change", () => {
          draft.ticks = Math.max(1, Math.round(num(ticks.value, 1)));
        });
        detail.appendChild(field("跨过去要几个 tick", "1 tick 就是世界时钟的间隔。", ticks));

        const bidi = el("label", "inline");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = draft.bidirectional !== false;
        box.addEventListener("change", () => (draft.bidirectional = box.checked));
        bidi.appendChild(box);
        bidi.appendChild(el("span", "", "双向（两边都能走）"));
        detail.appendChild(bidi);
      }

      render();
    },
    onSubmit: () => {
      if (!draft.from_node || !draft.to_node) {
        toast("两端都要选一个地点");
        return false;
      }
      zoneEdges().push({
        id: `z_${draft.from_node}_${draft.to_node}`,
        from_zone: zone.id,
        to_zone: draft.to_zone,
        from_node: draft.from_node,
        to_node: draft.to_node,
        ticks: buildPortalTicks(draft.ticks),
        bidirectional: draft.bidirectional !== false,
      });
      markDirty();
      renderNodeForm();
      renderMap();
      toast("跨区连线已加入，点右上角「保存」后生效。");
      return true;
    },
  });
}

function buildPortalTicks(value) {
  return Math.max(1, Math.round(num(value, 1)));
}

/* ---------------- 地点能做什么（动作归属的唯一数据源是动作的 allowed_nodes） ---------------- */

function nodeActionBinding(node) {
  const wrapper = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", "可用动作"));
  title.appendChild(
    tipBox(
      "和动作里的「限定地点」是同一份数据：这里勾上，动作那边就会多出这个地点；反之亦然。",
    ),
  );
  wrapper.appendChild(title);

  const scoped = actions().filter((item) => (item.scope || "global") === "node");
  const chosen = scoped
    .filter((item) => (item.allowed_nodes || []).includes(node.id))
    .map((item) => item.id);
  wrapper.appendChild(
    pickerField(
      "这个地点专属的动作",
      chosen,
      scoped.map((item) => ({
        id: item.id,
        name: item.name || item.id,
        desc: item.description || describeAction(item),
        group: groupOfAction(item),
        tag: item.enabled === false ? "已停用" : "",
      })),
      (picked) => {
        const wanted = new Set(picked);
        scoped.forEach((item) => {
          const list = Array.isArray(item.allowed_nodes) ? item.allowed_nodes.slice() : [];
          const has = list.includes(node.id);
          if (wanted.has(item.id) && !has) {
            item.allowed_nodes = list.concat([node.id]);
          } else if (!wanted.has(item.id) && has) {
            item.allowed_nodes = list.filter((id) => id !== node.id);
          }
        });
        markDirty();
        renderNodeForm();
      },
      {
        hint: "留空表示这个地点没有专属动作（通用动作照样能做）。",
        empty: "点击选择动作…",
        renderChips: true,
      },
    ),
  );

  const globalOnes = actions().filter((item) => (item.scope || "global") === "global");
  wrapper.appendChild(
    el(
      "p",
      "muted",
      globalOnes.length
        ? `通用动作（任何地点都能做，改范围请去动作库）：${globalOnes
            .map((item) => item.name || item.id)
            .join("、")}`
        : "还没有通用动作。",
    ),
  );
  const add = el("button", "ghost", "＋ 新建动作（自动归属本地点）");
  add.type = "button";
  add.addEventListener("click", () => createActionForNode(node.id));
  wrapper.appendChild(add);
  return wrapper;
}

/** 从地点出发新建动作：默认「仅此地点」。 */
function createActionForNode(nodeId) {
  const base = "new_action";
  let id = base;
  let index = 2;
  while (actions().some((item) => item.id === id)) {
    id = `${base}_${index++}`;
  }
  actions().push({
    id,
    name: "新动作",
    category: "instant",
    llm_level: "template",
    scope: "node",
    allowed_nodes: [nodeId],
    target_type: "none",
    template: "（{bot}做了点什么）",
    visible: true,
    description: "",
    enabled: true,
    created_at: Date.now(),
  });
  markDirty();
  openActionDrawer(id);
}

function renderNodeForm() {
  const form = $("node-form");
  form.innerHTML = "";
  if (ui.mapLevel === "world") {
    return renderZoneForm(form);
  }
  const node = nodes().find((item) => item.id === ui.selectedNode);
  $("node-title").textContent = node ? `地点属性：${node.name || node.id}` : "地点属性";
  if (!node) {
    form.appendChild(el("p", "muted", "点击画布上的地点进行编辑，或点「新增节点」。"));
    return;
  }
  node.atmosphere = node.atmosphere || {};

  form.appendChild(
    inputField("节点 ID", node.id, (value) => renameNode(node.id, value), {
      hint: "内部标识，用于日程和前置条件引用；改名会自动同步连线与动作引用。",
    }),
  );
  form.appendChild(
    inputField("名称", node.name || "", (value) => (node.name = value), {
      hint: "给她看的中文名，会出现在提示词里。",
    }),
  );
  form.appendChild(
    textareaField("地点描述", node.prompt || "", (value) => (node.prompt = value), {
      hint: "这个地点是什么样子的。会写进提示词，影响她在这里的行为和语气。",
    }),
  );
  form.appendChild(
    inputField("在这里时名片文案", node.nickname_text || "", (value) => (node.nickname_text = value.trim()), {
      hint: "她在这个地点且没有带文案的动作时，群名片显示的文字（例如「在书房」）。留空则不改名片。",
      placeholder: "例如 在书房",
    }),
  );

  const colorField = inputField("颜色", node.color || "#8B7DD8", (value) => (node.color = value), {
    hint: "只影响画布上的显示。",
    type: "color",
  });
  form.appendChild(colorField);

  const position = el("div", "row-item");
  const xField = inputField("X", num(node.x), (value) => (node.x = num(value)));
  const yField = inputField("Y", num(node.y), (value) => (node.y = num(value)));
  position.appendChild(xField);
  position.appendChild(yField);
  form.appendChild(position);

  form.appendChild(el("div", "section-title", "氛围（影响她的状态变化速度）"));
  ATMOSPHERES.forEach(({ key, label, hint }) => {
    form.appendChild(
      sliderField(
        `${label}（${key}）`,
        num(node.atmosphere[key], 0.5),
        (value) => {
          node.atmosphere[key] = value;
        },
        hint,
      ),
    );
  });

  form.appendChild(nodeGeneratorBox(node));

  form.appendChild(nodeActionBinding(node));

  form.appendChild(nodeEdgeEditor(node));

  const memoryBox = el("div", "subsection");
  const memoryTitle = el("div", "sub-title");
  memoryTitle.appendChild(el("span", "", "预设记忆"));
  memoryTitle.appendChild(
    tipBox("刚启用插件时她就已经记得的、和这个地点有关的事。会出现在提示词的「这里让你想起」里。"),
  );
  memoryBox.appendChild(memoryTitle);
  const memoryRows = el("div", "rows");
  node.preset_memories = node.preset_memories || [];
  node.preset_memories.forEach((memory, index) => {
    const line = el("div", "row-item");
    const contentInput = document.createElement("input");
    contentInput.className = "grow";
    contentInput.placeholder = "她记得的事，例如：在这里做过一个很温柔的梦";
    contentInput.value = memory.content || "";
    contentInput.addEventListener("change", () => {
      memory.content = contentInput.value.trim();
    });
    line.appendChild(contentInput);
    const emotionInput = document.createElement("input");
    emotionInput.className = "w-md";
    emotionInput.placeholder = "情绪";
    emotionInput.value = memory.emotion || "";
    emotionInput.addEventListener("change", () => {
      memory.emotion = emotionInput.value.trim();
    });
    line.appendChild(emotionInput);
    const weightInput = document.createElement("input");
    weightInput.type = "number";
    weightInput.step = "0.1";
    weightInput.min = "0";
    weightInput.max = "1";
    weightInput.className = "w-sm";
    weightInput.title = "权重：0~1，越大越容易被想起来";
    weightInput.value = memory.weight ?? 0.6;
    weightInput.addEventListener("change", () => {
      memory.weight = num(weightInput.value, 0.6);
    });
    line.appendChild(weightInput);
    const remove = el("button", "small danger", "删除");
    remove.type = "button";
    remove.addEventListener("click", () => {
      node.preset_memories.splice(index, 1);
      renderNodeForm();
    });
    line.appendChild(remove);
    memoryRows.appendChild(line);
  });
  if (!node.preset_memories.length) memoryRows.appendChild(el("p", "muted", "（还没有预设记忆）"));
  memoryBox.appendChild(memoryRows);
  const addMemoryRow = el("div", "row-item");
  const addMemory = el("button", "small ghost", "+ 添加一条记忆");
  addMemory.type = "button";
  addMemory.addEventListener("click", () => {
    node.preset_memories.push({ content: "", emotion: "", weight: 0.6, scope: "persona" });
    renderNodeForm();
  });
  addMemoryRow.appendChild(addMemory);
  memoryBox.appendChild(addMemoryRow);
  form.appendChild(memoryBox);
}

/** 节点连线编辑器：添加/删除路线，并设置走过去要花几个 tick。 */
function nodeEdgeEditor(node) {
  const wrapper = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", "连线（从这里出发要多久）"));
  title.appendChild(
    tipBox(
      "连线决定她能走哪里、走过去要花几个 tick（1 tick = 世界时钟的间隔，默认 60 秒）。双向表示两边都能走。",
    ),
  );
  wrapper.appendChild(title);

  const list = el("div", "rows");
  const mine = () => edges().filter((edge) => edge.from === node.id || edge.to === node.id);
  mine().forEach((edge) => {
    const line = el("div", "edge-line");
    const outbound = edge.from === node.id;
    const otherId = outbound ? edge.to : edge.from;
    const other = nodes().find((item) => item.id === otherId);
    const top = el("div", "row-item");
    top.appendChild(el("span", "w-sm", outbound ? "去 →" : "← 来自"));
    top.appendChild(el("span", "grow", other ? `${other.name}（${otherId}）` : otherId));
    line.appendChild(top);

    const bottom = el("div", "row-item");
    bottom.appendChild(el("span", "muted", "走过去"));
    const tickInput = document.createElement("input");
    tickInput.type = "number";
    tickInput.min = "1";
    tickInput.className = "w-sm";
    tickInput.value = num(edge.ticks, 1);
    tickInput.title = "走这条线要花几个 tick";
    tickInput.addEventListener("change", () => {
      edge.ticks = Math.max(1, Math.round(num(tickInput.value, 1)));
      renderMap();
    });
    bottom.appendChild(tickInput);
    bottom.appendChild(el("span", "muted", "tick"));

    const bidi = el("label", "inline");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = edge.bidirectional !== false;
    box.addEventListener("change", () => {
      edge.bidirectional = box.checked;
      renderMap();
    });
    bidi.appendChild(box);
    bidi.appendChild(el("span", "", "双向"));
    bottom.appendChild(bidi);

    const remove = el("button", "small danger", "删除");
    remove.type = "button";
    remove.addEventListener("click", () => {
      ui.config.world.edges = edges().filter((row) => row !== edge);
      renderMap();
      renderNodeForm();
    });
    bottom.appendChild(remove);
    line.appendChild(bottom);
    list.appendChild(line);
  });
  if (!mine().length) list.appendChild(el("p", "muted", "这个地点还没有连到任何地方。"));
  wrapper.appendChild(list);

  const addRow = el("div", "row-item");
  const add = el("button", "small ghost", "+ 添加连线");
  add.type = "button";
  add.addEventListener("click", () => {
    const items = nodeItems().filter((item) => item.id !== node.id);
    if (!items.length) {
      toast("至少要有两个节点");
      return;
    }
    openPicker({
      title: `从「${node.name || node.id}」连到哪里？`,
      hint: "选好目标地点后，可以在列表里改走过去要花几个 tick。",
      items,
      multi: false,
      onConfirm: (chosen) => {
        const target = chosen[0];
        if (!target) return;
        edges().push({
          id: `e_${node.id}_${target}`,
          from: node.id,
          to: target,
          ticks: 1,
          bidirectional: true,
        });
        renderMap();
        renderNodeForm();
      },
    });
  });
  addRow.appendChild(add);
  wrapper.appendChild(addRow);
  return wrapper;
}

function sliderField(label, value, onChange, hint) {
  const wrapper = el("label");
  const head = fieldHead(`${label}`, hint);
  const valueSpan = el("span", "muted", Number(value).toFixed(2));
  head.appendChild(valueSpan);
  wrapper.appendChild(head);
  const input = document.createElement("input");
  input.type = "range";
  input.min = "0";
  input.max = "1";
  input.step = "0.05";
  input.value = value;
  input.addEventListener("input", () => {
    valueSpan.textContent = Number(input.value).toFixed(2);
    onChange(Number(input.value));
  });
  wrapper.appendChild(input);
  return wrapper;
}

function renameNode(oldId, newId) {
  const clean = (newId || "").trim();
  if (!clean || clean === oldId) return;
  const node = nodes().find((item) => item.id === oldId);
  if (!node) return;
  if (nodes().some((item) => item.id === clean)) {
    toast("地点 ID 已存在");
    return;
  }
  node.id = clean;
  edges().forEach((edge) => {
    if (edge.from === oldId) edge.from = clean;
    if (edge.to === oldId) edge.to = clean;
  });
  zoneEdges().forEach((edge) => {
    if (edge.from_node === oldId) edge.from_node = clean;
    if (edge.to_node === oldId) edge.to_node = clean;
  });
  actions().forEach((action) => {
    if (Array.isArray(action.allowed_nodes)) {
      action.allowed_nodes = action.allowed_nodes.map((item) => (item === oldId ? clean : item));
    }
  });
  schedules().forEach((schedule) => {
    (schedule.action_chain || []).forEach((step) => {
      if (step.target_node === oldId) step.target_node = clean;
    });
  });
  ui.selectedNode = clean;
  markDirty();
  renderMap();
}

/** 区域改名：房间上的归属也要跟着改。 */
/** 地图（区域 + 地点 + 连线）直接编辑 JSON：只含地图结构，不含全局设置。 */
async function editMapJson() {
  const payload = {
    zones: zones(),
    zone_edges: zoneEdges(),
    nodes: nodes(),
    edges: edges(),
  };
  openFormDialog({
    title: "地图 JSON",
    hint: "整段替换地图结构：zones / zone_edges / nodes / edges。不影响全局设置、动作与日程。",
    wide: true,
    fields: [
      {
        key: "json",
        label: "地图（JSON）",
        type: "textarea",
        value: JSON.stringify(payload, null, 2),
        rows: 20,
      },
    ],
    onSubmit: (values) => {
      let parsed = null;
      try {
        parsed = JSON.parse(values.json || "{}");
      } catch (error) {
        toast(`JSON 格式不对：${error.message}`);
        return false;
      }
      if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.nodes)) {
        toast("至少要有一个 nodes 数组");
        return false;
      }
      if (!Array.isArray(parsed.zones) || !parsed.zones.length) {
        toast("至少要有一个 zones（区域）");
        return false;
      }
      const ids = new Set(parsed.nodes.map((node) => String(node.id || "").trim()));
      if (ids.has("")) {
        toast("每个地点都必须有 id");
        return false;
      }
      ui.config.world.zones = parsed.zones;
      ui.config.world.zone_edges = Array.isArray(parsed.zone_edges) ? parsed.zone_edges : [];
      ui.config.world.nodes = parsed.nodes;
      ui.config.world.edges = Array.isArray(parsed.edges) ? parsed.edges : [];
      ui.selectedZone = ui.config.world.zones[0].id;
      ui.selectedNode = "";
      ui.mapLevel = "world";
      markDirty();
      renderMap();
      renderNodeForm();
      renderActionGrid();
      toast("已替换地图，点右上角「保存」后生效。");
      return true;
    },
  });
}

function renameZone(oldId, newId) {
  const clean = (newId || "").trim();
  if (!clean || clean === oldId) return;
  const zone = zones().find((item) => item.id === oldId);
  if (!zone) return;
  if (zones().some((item) => item.id === clean)) {
    toast("区域 ID 已存在");
    return;
  }
  zone.id = clean;
  nodes().forEach((node) => {
    if (zoneOfNode(node) === oldId) node.zone_id = clean;
  });
  zoneEdges().forEach((edge) => {
    if (edge.from_zone === oldId) edge.from_zone = clean;
    if (edge.to_zone === oldId) edge.to_zone = clean;
  });
  ui.selectedZone = clean;
  markDirty();
  renderMap();
  renderNodeForm();
}

function addZone() {
  let id = "zone_1";
  let index = 2;
  while (zones().some((item) => item.id === id)) {
    id = `zone_${index++}`;
  }
  zones().push({
    id,
    name: "新区域",
    note: "",
    icon: "",
    color: "#7FB2E5",
    x: 80 + zones().length * 180,
    y: 80,
  });
  ui.selectedZone = id;
  markDirty();
  renderMap();
  renderNodeForm();
}

async function deleteZone() {
  const zone = zones().find((item) => item.id === ui.selectedZone);
  if (!zone) {
    toast("先选中一个区域");
    return;
  }
  if (zones().length <= 1) {
    toast("至少要保留一个区域");
    return;
  }
  const inside = nodesInZone(zone.id);
  const ok = await confirmDialog({
    title: "删除这个区域？",
    message:
      `「${zone.name || zone.id}」会被删掉，里面的 ${inside.length} 个地点和相关的跨区连线也一起删除；` +
      "动作里指向这些地点的限定地点会同步清理。",
    confirmText: "删除",
  });
  if (!ok) return;
  const doomed = new Set(inside.map((node) => node.id));
  ui.config.world.zones = zones().filter((item) => item.id !== zone.id);
  ui.config.world.nodes = nodes().filter((node) => !doomed.has(node.id));
  ui.config.world.edges = edges().filter(
    (edge) => !doomed.has(edge.from) && !doomed.has(edge.to),
  );
  ui.config.world.zone_edges = zoneEdges().filter(
    (edge) => edge.from_zone !== zone.id && edge.to_zone !== zone.id,
  );
  actions().forEach((action) => {
    if (Array.isArray(action.allowed_nodes)) {
      action.allowed_nodes = action.allowed_nodes.filter((id) => !doomed.has(id));
    }
  });
  ui.selectedZone = zones()[0].id;
  ui.selectedNode = "";
  markDirty();
  renderMap();
  renderNodeForm();
  renderActionGrid();
}

function addNode() {
  const id = `node_${Date.now().toString(36).slice(-4)}`;
  const zoneId = ui.selectedZone || defaultZoneId();
  const mate = nodesInZone(zoneId);
  nodes().push({
    id,
    name: "新地点",
    zone_id: zoneId,
    // 摆在这个区域里已有节点的旁边，避免和别的区域重叠
    x: mate.length ? Math.min(...mate.map((item) => num(item.x))) + mate.length * 40 : 60,
    y: mate.length ? Math.min(...mate.map((item) => num(item.y))) : 60,
    color: "#8B7DD8",
    prompt: "描述一下这个地方。",
    atmosphere: {
      calm: 0.5,
      intimacy: 0.5,
      visibility: 0.5,
      liveliness: 0.5,
      loneliness: 0.3,
      curiosity: 0.5,
    },
    preset_memories: [],
  });
  ui.selectedNode = id;
  markDirty();
  renderMap();
  renderNodeForm();
}

async function deleteNode() {
  if (!ui.selectedNode) {
    toast("先选中一个节点");
    return;
  }
  const node = nodes().find((item) => item.id === ui.selectedNode);
  const ok = await confirmDialog({
    title: "删除这个地点？",
    message: `「${node ? node.name || node.id : ui.selectedNode}」以及它的连线会被删掉。`,
    confirmText: "删除",
  });
  if (!ok) return;
  ui.config.world.nodes = nodes().filter((item) => item.id !== ui.selectedNode);
  ui.config.world.edges = edges().filter(
    (edge) => edge.from !== ui.selectedNode && edge.to !== ui.selectedNode,
  );
  // 跨区连线也要一起清掉，不然保存时会报"端点房间不存在"
  ui.config.world.zone_edges = zoneEdges().filter(
    (edge) =>
      edge.from_node !== ui.selectedNode && edge.to_node !== ui.selectedNode,
  );
  actions().forEach((action) => {
    if (Array.isArray(action.allowed_nodes)) {
      action.allowed_nodes = action.allowed_nodes.filter(
        (item) => item !== ui.selectedNode,
      );
    }
  });
  ui.selectedNode = "";
  markDirty();
  renderMap();
  renderNodeForm();
  renderActionGrid();
}

function addEdge() {
  if (!ui.selectedNode) {
    toast("先选中起点地点");
    return;
  }
  // 区域内连线：只在同一个区域里挑目标（跨区请用「连到其他区域…」）
  const zoneId = ui.selectedZone || defaultZoneId();
  const items = nodesInZone(zoneId)
    .filter((item) => item.id !== ui.selectedNode)
    .map((item) => ({
      id: item.id,
      name: item.name || item.id,
      desc: item.prompt || "",
    }));
  if (!items.length) {
    toast("这个区域里至少要有两个地点");
    return;
  }
  openPicker({
    title: "连线到哪个地点？",
    hint: "同区域内的路。双向连线会自动生成来回两条；要去别的区域请用「连到其他区域…」。",
    items,
    multi: false,
    onConfirm: (chosen) => {
      const target = chosen[0];
      if (!target) return;
      edges().push({
        id: `e_${ui.selectedNode}_${target}`,
        from: ui.selectedNode,
        to: target,
        ticks: 1,
        bidirectional: true,
      });
      renderMap();
    },
  });
}

/* ================================================================== */
/* 动作库                                                              */
/* ================================================================== */

/**
 * 固定参数：填过的参数不再交给模型猜。
 * 主要用来兜底「schema 把参数写成可选、实现却必须要」的第三方工具（例如查天气的 city）。
 */
function fixedParamsEditor(action, toolNames) {
  action.params =
    action.params && typeof action.params === "object" ? action.params : {};
  const wrapper = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", "固定参数"));
  title.appendChild(
    tipBox("填上后该参数固定使用此值，不由模型决定。适合「参数写着可选、实现却必须要」的工具。",
    ),
  );
  wrapper.appendChild(title);

  const declared = [];
  (toolNames || []).forEach((name) => {
    const item = ui.tools.find((tool) => tool.name === name);
    if (!item) return;
    Object.keys(normalizeToolSchema(item.parameters).properties || {}).forEach((key) => {
      if (!declared.includes(key)) declared.push(key);
    });
  });

  const rows = Object.entries(action.params).map(([key, spec]) => ({
    key,
    value: spec && typeof spec === "object" ? String(spec.value || "") : String(spec || ""),
    spec: spec && typeof spec === "object" ? { ...spec } : {},
  }));

  const list = el("div", "rows");
  function collect() {
    const next = {};
    rows.forEach((row) => {
      const name = String(row.key || "").trim();
      if (!name) return;
      const spec = { ...row.spec };
      spec.type = spec.type || "string";
      spec.value = String(row.value || "");
      next[name] = spec;
    });
    action.params = next;
    markDirty();
  }
  function draw() {
    list.innerHTML = "";
    rows.forEach((row, index) => {
      const line = el("div", "row-item");
      const nameInput = document.createElement("input");
      nameInput.className = "grow";
      nameInput.placeholder = "参数名，例如 city";
      nameInput.value = row.key || "";
      if (declared.length) {
        const listId = `fp-${index}-${Math.random().toString(36).slice(2, 6)}`;
        const datalist = document.createElement("datalist");
        datalist.id = listId;
        declared.forEach((name) => datalist.appendChild(option(name, name)));
        line.appendChild(datalist);
        nameInput.setAttribute("list", listId);
      }
      nameInput.addEventListener("input", () => {
        row.key = nameInput.value;
        collect();
      });
      line.appendChild(nameInput);

      const valueInput = document.createElement("input");
      valueInput.className = "grow";
      valueInput.placeholder = "固定值，例如 武汉";
      valueInput.value = row.value || "";
      valueInput.addEventListener("input", () => {
        row.value = valueInput.value;
        collect();
      });
      line.appendChild(valueInput);

      const remove = el("button", "icon-btn danger", "🗑");
      remove.type = "button";
      remove.title = "不要这个固定参数";
      remove.addEventListener("click", () => {
        rows.splice(index, 1);
        collect();
        draw();
      });
      line.appendChild(remove);
      list.appendChild(line);
    });
    if (!rows.length) list.appendChild(el("p", "muted", "（没有固定参数）"));
  }
  draw();
  wrapper.appendChild(list);

  const add = el("button", "small ghost", "+ 添加一条固定参数");
  add.type = "button";
  add.addEventListener("click", () => {
    rows.push({ key: "", value: "", spec: {} });
    draw();
  });
  wrapper.appendChild(add);
  return wrapper;
}

function renderActionForm() {
  const form = $("action-form");
  form.innerHTML = "";
  const action = ui.actionDraft;
  if (!action) {
    form.appendChild(el("p", "muted", "点动作卡片打开编辑。"));
    return;
  }
  action.params = action.params || {};
  action.preconditions = action.preconditions || {};
  action.during = action.during || {};
  action.on_complete = action.on_complete || {};
  action.on_complete.effects = action.on_complete.effects || {};
  action.on_complete.effects_per_minute = action.on_complete.effects_per_minute || {};
  const isContinuous = action.category === "continuous";
  const isTool = action.llm_level === "tool";
  const isCommand = action.llm_level === "command";
  const isNodeScoped = action.scope === "node";

  form.appendChild(
    inputField("动作 ID", action.id, (value) => {
      action.id = value.trim();
    }, { hint: "内部标识，日程和大模型都按它引用动作。建议用英文，例如 sleep、search_web。" }),
  );
  form.appendChild(
    inputField("名称", action.name || "", (value) => (action.name = value), {
      hint: "给她看的中文名，会出现在「你可以执行的动作」列表里。",
    }),
  );
  form.appendChild(
    textareaField("说明", action.description || "", (value) => (action.description = value), {
      hint: "一句话说明这个动作是干什么的。这段文字会直接给大模型看，写得越清楚她越不容易用错。",
      // 内置动作有出厂说明：改乱了可以一键还原（只还原这一栏，不动其它配置）
      onRestore: action.builtin
        ? () => ((ui.defaults.actions || {})[action.id] || {}).description || ""
        : null,
    }),
  );
  const knownGroups = Array.from(
    new Set(actions().map((item) => groupOfAction(item))),
  ).sort();
  form.appendChild(
    comboField("分组", action.group || "", knownGroups, (value) => (action.group = value.trim()), {
      hint:
        "动作库里的归类，名称自定（下拉里是已有分组，也可以直接写新名称）。" +
        "留空就按用途自动归类。",
      placeholder: "例如 互动",
    }),
  );

  form.appendChild(
    pillsField(
      "动作类型",
      action.category || "instant",
      CATEGORIES,
      (value) => {
        action.category = value;
        renderActionForm();
      },
      { hint: "决定这个动作是「立刻完成」还是「占用一段时间」。" },
    ),
  );
  form.appendChild(
    pillsField(
      "执行层级",
      action.llm_level || "template",
      LLM_LEVELS,
      (value) => {
        action.llm_level = value;
        renderActionForm();
      },
      { hint: "决定这个动作要不要调用大模型，以及要不要调用工具。" },
    ),
  );
  form.appendChild(
    pillsField(
      "生效范围",
      action.scope || "global",
      SCOPE_MODES,
      (value) => {
        action.scope = value;
        renderActionForm();
      },
      { hint: "「全局」= 任何地点都能做；「仅特定地点」= 这个动作属于指定地点，她想去就会被带过去再做。" },
    ),
  );
  form.appendChild(
    pillsField(
      "提示词里怎么写它",
      action.desc_mode === "brief" ? "brief" : "full",
      [
        { key: "full", label: "带说明", hint: "在动作清单里写出这条描述（默认）。" },
        {
          key: "brief",
          label: "只写名字",
          hint: "只写「id（名称）」：含义一看就懂的动作（点头、抱抱、亲亲）用这个，可节省提示词。",
        },
      ],
      (value) => {
        action.desc_mode = value;
        renderActionForm();
      },
      { hint: "工具型 / 时长自定 / 只有这个地点才能做：这些标记不受影响。" },
    ),
  );
  form.appendChild(
    pillsField(
      "事件中可调用",
      action.event_usable || "auto",
      EVENT_USABLE_MODES,
      (value) => {
        action.event_usable = value;
        // 药丸的选中态是我们自己画的：不重绘就看不到点了哪一项
        renderActionForm();
      },
      {
        hint: "她遇上事时能否为那件事调用这个动作。默认跟随规则：工具 / 指令型可用；也可强制允许或强制禁止。",
      },
    ),
  );
  action.quota = action.quota || { day: 0, week: 0, month: 0 };
  form.appendChild(
    inputField(
      "亲密程度",
      action.intimacy === null || action.intimacy === undefined ? "" : action.intimacy,
      (value) => {
        const text = String(value ?? "").trim();
        action.intimacy = text === "" ? null : num(text, 0);
      },
      {
        hint: "这一步算多亲密的肢体接触（0~1）：做完按它满足「欲求」。留空 = 自动判断。",
        type: "number",
        min: "0",
        max: "1",
        step: "0.1",
        placeholder:
          action.intimacy_effective > 0
            ? `自动（${action.intimacy_effective}）`
            : "自动（不算）",
      },
    ),
  );
  [
    ["day", "每天最多几次"],
    ["week", "每周最多几次"],
    ["month", "每月最多几次"],
  ].forEach(([key, label]) => {
    form.appendChild(
      inputField(
        label,
        num(action.quota[key], 0),
        (value) => (action.quota[key] = num(value, 0)),
        {
          hint: "超出后该动作不写进提示词，排到也会跳过。0 = 不限制；生图类默认每天 5 次、录视频 2 次。",
          type: "number",
          min: "0",
          max: "999",
          step: "1",
        },
      ),
    );
  });
  // 把结果记进状态槽：别的插件的状态（今日穿搭、背包…）就不会说完就忘
  form.appendChild(
    inputField(
      "结果记进状态槽",
      action.state_slot || "",
      (value) => {
        action.state_slot = value.trim();
        renderActionForm();
      },
      {
        hint: "填一个槽名（例如 outfit）就把这个动作的结果存下来，之后每轮提示词都带着；留空 = 不记。",
        placeholder: "outfit",
      },
    ),
  );
  if (action.state_slot) {
    form.appendChild(
      inputField(
        "状态槽的称呼",
        action.state_label || "",
        (value) => (action.state_label = value.trim()),
        { hint: "提示词里怎么叫它，例如「今日穿搭」。留空就用槽名。", placeholder: "今日穿搭" },
      ),
    );
    form.appendChild(
      inputField(
        "状态槽有效期（分钟）",
        num(action.state_ttl_minutes, 0),
        (value) => (action.state_ttl_minutes = num(value, 0)),
        {
          hint: "过期后不再写进提示词。0 = 不过期；「今日穿搭」这类可以填 720（半天）。",
          type: "number",
          min: "0",
          step: "10",
        },
      ),
    );
    form.appendChild(
      checkboxField(
        "用打杂模型压成一句话再存",
        action.state_summarize === true,
        (value) => (action.state_summarize = value),
        { hint: "返回是一大段时压一下更省上下文；返回本来就短就不用开。" },
      ),
    );
  }
  if (isNodeScoped) {
    form.appendChild(
      pickerField(
        "限定地点",
        action.allowed_nodes || [],
        nodeItems(),
        (chosen) => {
          action.allowed_nodes = chosen;
          renderActionForm();
        },
        {
          hint: "这个动作只在这些地点可用。",
          empty: "点击选择地点…",
          renderChips: true,
        },
      ),
    );
  }
  form.appendChild(
    pillsField(
      "目标",
      action.target_type || "none",
      TARGET_TYPES,
      (value) => {
        action.target_type = value;
        renderActionForm();
      },
      { hint: "动作指向谁。指向人的动作会以动作描述的形式发到群里。" },
    ),
  );

  if (isContinuous) {
    const durationBox = el("div", "subsection");
    const durationTitle = el("div", "sub-title");
    durationTitle.appendChild(el("span", "", "持续时间"));
    durationTitle.appendChild(
      tipBox("持续动作要占用多久。可以固定，也可以交给大模型在区间内自己决定（例如小睡多久）。"),
    );
    durationBox.appendChild(durationTitle);
    durationBox.appendChild(
      pillsField(
        "时长由谁决定",
        action.duration_mode || "fixed",
        DURATION_MODES,
        (value) => {
          action.duration_mode = value;
          renderActionForm();
        },
        {},
      ),
    );
    if ((action.duration_mode || "fixed") === "llm") {
      durationBox.appendChild(
        timeField("最短", action.duration_min || 600, (value) => (action.duration_min = value), {
          hint: "大模型给的时长不会短于这个值。",
        }),
      );
      durationBox.appendChild(
        timeField("最长", action.duration_max || 1800, (value) => (action.duration_max = value), {
          hint: "大模型给的时长不会超过这个值，防止她「睡一整天」。",
        }),
      );
    } else {
      durationBox.appendChild(
        timeField("固定时长", action.duration || 600, (value) => (action.duration = value), {
          hint: "每次执行都用这个时长。",
        }),
      );
    }
    durationBox.appendChild(
      inputField(
        "执行期间状态标识",
        action.during.state || "",
        (value) => (action.during.state = value.trim()),
        {
          hint:
            "执行期间她的状态会变成这个标识，会影响群名片文案与状态判断（例如 sleeping、reading）。可留空。",
          placeholder: "例如 sleeping",
        },
      ),
    );
    durationBox.appendChild(
      inputField(
        "执行中名片文案",
        action.nickname_text || "",
        (value) => (action.nickname_text = value.trim()),
        {
          hint: "她做这个动作时群名片显示的文字（例如「做饭中」）。留空则用「状态 → 文案」的兜底映射。",
          placeholder: "例如 做饭中",
        },
      ),
    );
    form.appendChild(durationBox);
  }

  if (action.llm_level === "template") {
    form.appendChild(
      inputField("模板文案", action.template || "", (value) => (action.template = value), {
        hint: "不调大模型时直接发到群里的固定文案。占位符：{bot}=她自己、{user}=目标群友、{node}=当前地点。",
        placeholder: "例如：（伸了个懒腰）",
      }),
    );
  }

  if (isTool) {
    const toolBox = el("div", "subsection");
    const toolTitle = el("div", "sub-title");
    toolTitle.appendChild(el("span", "", "工具设置"));
    toolTitle.appendChild(
      tipBox("参数由工具自己定义。她只需说明想干什么，插件用辅助模型补全，不必手写或配默认值。",
      ),
    );
    toolBox.appendChild(toolTitle);
    toolBox.appendChild(
      pillsField(
        "调用形态",
        action.tool_flow || "simple",
        TOOL_FLOWS,
        (value) => {
          action.tool_flow = value;
          renderActionForm();
        },
        {
          hint: "直接调用 = 把工具结果交回给她说一句；联网检索 = 多条查询词 → 证据 → 可选读正文 → 不够补查。",
        },
      ),
    );
    const currentTools = Array.isArray(action.tool_names) && action.tool_names.length
      ? action.tool_names.slice()
      : action.tool_name
        ? [action.tool_name]
        : [];
    toolBox.appendChild(
      pickerField(
        "使用的工具",
        currentTools,
        toolItems(),
        (chosen) => {
          action.tool_names = chosen;
          action.tool_name = chosen[0] || "";
          renderActionForm();
        },
        {
          hint: "必须选一个 AstrBot 里已注册的工具，没选则跳过。选多个时按顺序调用、结果合并；参数由辅助模型补全。",
          empty: "点击选择工具…",
          renderChips: true,
        },
      ),
    );
    toolBox.appendChild(
      pillsField(
        "工具用法",
        action.tool_mode || "sequence",
        TOOL_MODES,
        (value) => {
          action.tool_mode = value;
          renderActionForm();
        },
        {
          hint: "多工具的用法：按顺序都调 = 全调并合并；依次尝试 = 只用一个、失败换下一个；智能选择 = 辅助模型挑一个。",
        },
      ),
    );
    const paramBox = el("div", "subsection");
    paramBox.appendChild(el("div", "sub-title", "工具自带参数（只读）"));
    if (!currentTools.length) {
      paramBox.appendChild(el("p", "hint", "（选择工具后显示它的参数）"));
    }
    currentTools.forEach((name) => {
      const globalSearch = name === GLOBAL_SEARCH_TOOL;
      const tool = ui.tools.find((item) => item.name === name);
      paramBox.appendChild(
        el("div", "sub-title", globalSearch ? GLOBAL_SEARCH_LABEL : name),
      );
      paramBox.appendChild(
        el(
          "p",
          "hint",
          globalSearch
            ? "参数由「联网搜索」里的全局配置决定。"
            : tool
              ? tool.param_text || "（这个工具不需要参数）"
              : "（没找到这个工具）",
        ),
      );
    });
    const toolName = currentTools[0] || "";
    const tool = ui.tools.find((item) => item.name === toolName);
    toolBox.appendChild(paramBox);
    toolBox.appendChild(fixedParamsEditor(action, currentTools));

    // 工具型动作最容易踩的坑，直接在表单里点出来
    const missingTools = currentTools.filter(
      (name) => name !== GLOBAL_SEARCH_TOOL && !ui.tools.some((item) => item.name === name),
    );
    if (action.enabled === false) {
      // 动作已停用，运行时不会跑，不用提示它缺工具
    } else if (!currentTools.length) {
      toolBox.appendChild(
        el(
          "p",
          "warn-line",
          "⚠ 这是工具型动作，但还没选工具。没有工具的这一步会被直接跳过（日志里会写明原因）。",
        ),
      );
    } else if (usesGlobalSearch(action) && !effectiveSearchTools().length) {
      toolBox.appendChild(
        el(
          "p",
          "warn-line",
          `⚠ 这个动作写着「${GLOBAL_SEARCH_LABEL}」，但联网搜索里还没配工具，这一步会被跳过。`,
        ),
      );
    } else if (missingTools.length) {
      toolBox.appendChild(
        el(
          "p",
          "warn-line",
          `⚠ AstrBot 里现在没有这些工具：${missingTools.join("、")}。这一步会被跳过，请重新选。`,
        ),
      );
    }

    if ((action.tool_flow || "simple") === "search") {
      // 用户 m10393：联网搜索不再让用户在每个动作上配一遍——工具、档位、条数都搬到
      // 全局设置 → 高级功能 → 联网搜索（一份配置管所有地点）。这里只留一句指路。
      toolBox.appendChild(
        el(
          "p",
          "hint",
          "联网检索的参数统一在 全局设置 → 高级功能 → 联网搜索 里改；动作上不再单独配。",
        ),
      );
    }
    form.appendChild(toolBox);
  }

  if (isCommand) {
    const commandBox = el("div", "subsection");
    const commandTitle = el("div", "sub-title");
    commandTitle.appendChild(el("span", "", "指令设置"));
    commandTitle.appendChild(
      tipBox(
        "她会把「想干什么」交给辅助模型拼成一条指令，再由插件交给对应插件执行，" +
          "然后把那条指令返回的内容交回给她说一句。",
      ),
    );
    commandBox.appendChild(commandTitle);
    commandBox.appendChild(
      inputField(
        "要触发的指令",
        action.trigger_command || "",
        (value) => (action.trigger_command = value.trim().replace(/^\//, "")),
        {
          hint: "别的插件注册的指令名（不用写斜杠），例如 天气、查成分。运行时拼成 /指令 参数 交给它。",
          placeholder: "例如 天气",
        },
      ),
    );
    commandBox.appendChild(
      textareaField(
        "这条指令的参数说明（给模型看）",
        action.trigger_hint || "",
        (value) => (action.trigger_hint = value),
        {
          hint: "写清这条指令要什么参数、怎么给（例如「city：城市名」）。辅助模型只依据这里和她的意图拼参数。",
          rows: 3,
        },
      ),
    );
    if (!(action.trigger_command || "").trim()) {
      commandBox.appendChild(
        el("p", "warn-line", "⚠ 还没填要触发的指令，这个动作运行时会直接跳过。"),
      );
    }
    form.appendChild(commandBox);
  }

  form.appendChild(
    effectsEditor(
      "完成时一次性效果",
      "动作做完立刻生效的数值变化，例如「抱抱 +0.28 心潮」。设值（=）会直接覆盖当前值。",
      action.on_complete.effects,
      (effects) => {
        action.on_complete.effects = effects;
      },
    ),
  );

  if (isContinuous) {
    form.appendChild(
      effectsEditor(
        "每持续 1 分钟的效果",
        "按实际持续时间累加。例如小睡写「精力 增加 0.002」，大模型决定睡 30 分钟就加 0.06，睡 60 分钟就加 0.12。",
        action.on_complete.effects_per_minute,
        (effects) => {
          action.on_complete.effects_per_minute = effects;
        },
        { perMinute: true },
      ),
    );
  }

  const triggerBox = el("div", "subsection");
  const triggerTitle = el("div", "sub-title");
  triggerTitle.appendChild(el("span", "", "完成后"));
  triggerTitle.appendChild(
    tipBox(
      "动作结束后做什么：默认不额外开口；「接着说一句」让大模型把结果讲成人话。" +
        "工具型动作还受全局「工具结果回话」影响。",
    ),
  );
  triggerBox.appendChild(triggerTitle);
  triggerBox.appendChild(
    pillsField(
      "",
      action.on_complete.trigger || "none",
      TRIGGERS,
      (value) => {
        action.on_complete.trigger = value;
        renderActionForm();
      },
      {},
    ),
  );
  if (action.on_complete.trigger === "llm_followup") {
    triggerBox.appendChild(
      textareaField(
        "续说时的提示",
        action.on_complete.prompt_hint || "",
        (value) => (action.on_complete.prompt_hint = value),
        { hint: "告诉大模型怎么处理结果，例如「把搜索结果转成你的见闻，用第一人称」。", rows: 3 },
      ),
    );
  }
  if (action.on_complete.trigger === "schedule") {
    triggerBox.appendChild(
      pickerField(
        "接着执行哪个日程",
        action.on_complete.schedule_id ? [action.on_complete.schedule_id] : [],
        schedules().map((schedule) => ({
          id: schedule.id,
          name: `${schedule.time} ${schedule.id}`,
          desc: (schedule.action_chain || []).map((step) => step.type).join(" → "),
        })),
        (chosen) => {
          action.on_complete.schedule_id = chosen[0] || "";
          renderActionForm();
        },
        { multi: false, hint: "动作完成后立刻执行这个日程里的动作链。", empty: "点击选择日程…" },
      ),
    );
  }
  form.appendChild(triggerBox);

  const advanced = document.createElement("details");
  advanced.className = "raw-json";
  advanced.appendChild(el("summary", "", "高级：前置条件与其它设置"));
  const advancedBody = el("div", "form");
  advancedBody.appendChild(
    pickerField(
      "不允许在这些状态下执行",
      action.preconditions.not_state || [],
      STATES.map((item) => ({ id: item.key, name: item.label, desc: item.key })),
      (chosen) => {
        action.preconditions.not_state = chosen;
        renderActionForm();
      },
      { hint: "例如「睡觉中不要做这个动作」。", empty: "（不限制）" },
    ),
  );
  advancedBody.appendChild(
    inputField(
      "最低精力要求",
      action.preconditions.min_energy ?? "",
      (value) => {
        action.preconditions.min_energy = value === "" ? null : num(value, 0);
      },
      { hint: "精力低于这个值时不允许执行（0~1，留空表示不限制）。", type: "number", step: "0.05" },
    ),
  );
  advancedBody.appendChild(
    inputField("优先级", num(action.priority, 5), (value) => (action.priority = num(value, 5)), {
      hint: "数字越大越优先。日程里同时到点时按这个排序。",
      type: "number",
    }),
  );
  advancedBody.appendChild(
    checkboxField(
      "动作会发到群里",
      action.visible !== false,
      (value) => (action.visible = value),
      {
        hint: "关闭表示静默执行（换位置、发呆等）。「单轮」动作例外：目标为群或群友时，生成的话一定发出去。",
      },
    ),
  );
  advancedBody.appendChild(
    checkboxField(
      "可以被打断",
      action.interruptible !== false,
      (value) => (action.interruptible = value),
      { hint: "开着时用户说话可以打断她正在做的事（例如把她叫醒）。" },
    ),
  );
  advanced.appendChild(advancedBody);
  form.appendChild(advanced);
}

/* ---------------- 动作库：卡片网格 + 右侧编辑抽屉 ---------------- */

const ACTION_GROUPS = {
  builtin: "内置",
  interact: "互动",
  express: "表达",
  life: "生活",
  tool: "工具",
  custom: "自定义 / 其它",
};

/** 动作的分组标签：用户可以自己写 `group`，没写就按用途猜一个。 */
function groupOfAction(action) {
  const custom = String(action.group || "").trim();
  if (custom) return custom;
  // 内置动作单独一组：它们不能被删除，放在一起好管理
  if (action.builtin) return ACTION_GROUPS.builtin;
  if (action.llm_level === "tool") return ACTION_GROUPS.tool;
  const id = String(action.id || "");
  if (id === "say" || id === "share") return ACTION_GROUPS.express;
  if (["sleep", "nap", "read", "cook", "stare", "stretch"].includes(id)) {
    return ACTION_GROUPS.life;
  }
  if ((action.target_type || "none") !== "none") return ACTION_GROUPS.interact;
  return ACTION_GROUPS.custom;
}

/** 这个动作是哪个扩展带来的（不是扩展动作就返回空串）。 */
function actionOwnerOf(action) {
  const id = String((action && action.id) || "");
  if (!id) return "";
  const groups = (ui.config && ui.config.extension_actions) || [];
  const hit = groups.find((item) => (item.ids || []).includes(id));
  return hit ? String(hit.name || "") : "";
}

function actionOwnerTitle(name) {
  const groups = (ui.config && ui.config.extension_actions) || [];
  const hit = groups.find((item) => String(item.name || "") === String(name || ""));
  return hit ? String(hit.title || hit.name || "") : String(name || "");
}

/** 由系统自己维护、不给用户在动作库里配的动作（用户 m10393）。 */
const HIDDEN_ACTION_IDS = new Set(["search_web", "read_web_page"]);

function visibleActions() {
  const keyword = String(($("action-search") || {}).value || "").trim().toLowerCase();
  const picked = String(ui.actionGroup || "");
  return actions().filter((action) => {
    if (HIDDEN_ACTION_IDS.has(String(action.id || ""))) return false;
    const owner = actionOwnerOf(action);
    if (picked.startsWith("ext:")) {
      if (owner !== picked.slice(4)) return false;
    } else if (picked.startsWith("group:")) {
      // 原版那一半按分组筛；扩展带来的动作归它自己的那一组
      if (owner || groupOfAction(action) !== picked.slice(6)) return false;
    }
    if (!keyword) return true;
    const haystack = [
      action.id,
      action.name,
      action.description,
      actionToolNames(action).join(" "),
      groupOfAction(action),
      actionOwnerTitle(owner),
    ]
      .map((item) => String(item || "").toLowerCase())
      .join(" ");
    return haystack.includes(keyword);
  });
}

/** 左侧分组菜单：上半是原版动作，下半是各扩展带来的动作（没装扩展就没有下半截）。 */
function renderActionMenu() {
  const box = $("action-menu");
  if (!box) return;
  box.innerHTML = "";
  const list = actions().filter((action) => !HIDDEN_ACTION_IDS.has(String(action.id || "")));
  const originals = list.filter((action) => !actionOwnerOf(action));
  const counts = new Map();
  originals.forEach((action) => {
    const group = groupOfAction(action);
    counts.set(group, (counts.get(group) || 0) + 1);
  });
  box.appendChild(el("div", "menu-title", "原版动作"));
  box.appendChild(actionMenuItem("", "全部", originals.length));
  Array.from(counts.keys())
    .sort()
    .forEach((group) =>
      box.appendChild(actionMenuItem(`group:${group}`, group, counts.get(group))),
    );

  const extensions = (ui.config && ui.config.extension_actions) || [];
  const extItems = extensions
    .map((item) => ({
      name: String(item.name || ""),
      title: String(item.title || item.name || ""),
      count: list.filter((action) => actionOwnerOf(action) === String(item.name || "")).length,
    }))
    .filter((item) => item.count > 0);
  if (!extItems.length) return;
  box.appendChild(el("div", "menu-title", "扩展动作"));
  extItems.forEach((item) => {
    box.appendChild(actionMenuItem(`ext:${item.name}`, item.title, item.count));
  });
}

function actionMenuItem(key, label, count) {
  const button = el("button", "action-menu-item", "");
  button.type = "button";
  if (String(ui.actionGroup || "") === key) button.classList.add("active");
  button.appendChild(el("span", "grow", label));
  button.appendChild(el("span", "muted", String(count)));
  button.addEventListener("click", () => {
    ui.actionGroup = key;
    renderActionGrid();
  });
  return button;
}

function renderActionGrid() {
  renderActionMenu();
  ensureActionDiscovery();
  const grid = $("action-grid");
  grid.innerHTML = "";
  // 内置动作排最前（引擎专用，先让用户看到）；其余按加入时间倒序，没时间的保持原顺序。
  const list = visibleActions().slice().sort((a, b) => {
    const aBuiltin = a.builtin ? 1 : 0;
    const bBuiltin = b.builtin ? 1 : 0;
    if (aBuiltin !== bBuiltin) return bBuiltin - aBuiltin;
    return num(b.created_at, 0) - num(a.created_at, 0);
  });
  $("action-count").textContent = `共 ${actions().length} 个动作，当前显示 ${list.length} 个`;
  if (!list.length) {
    grid.appendChild(el("p", "muted", actions().length ? "没有匹配的动作。" : "还没有动作。"));
    renderActionTable([]);
    applyActionView();
    return;
  }
  list.forEach((action) => grid.appendChild(actionCard(action)));
  renderActionTable(list);
  applyActionView();
}

/* ---------------- 动作库：表格视图（和卡片看的是同一份数据） ---------------- */

function readActionView() {
  try {
    return window.localStorage.getItem(ACTION_VIEW_KEY) === "table" ? "table" : "cards";
  } catch (error) {
    return "cards";
  }
}

function applyActionView() {
  const table = ui.actionView === "table";
  const grid = $("action-grid");
  const wrap = $("action-table-wrap");
  if (grid) grid.classList.toggle("hidden", table);
  if (wrap) wrap.classList.toggle("hidden", !table);
  if ($("action-view-cards")) $("action-view-cards").classList.toggle("on", !table);
  if ($("action-view-table")) $("action-view-table").classList.toggle("on", table);
  try {
    window.localStorage.setItem(ACTION_VIEW_KEY, table ? "table" : "cards");
  } catch (error) {
    /* 记不住就记不住，不影响用 */
  }
}

/* ---------------- 动作说明整理：只读摘要 + 预览 + 排队 ---------------- */

/**
 * 动作说明整理的入口（挂在动作库顶上）。
 *
 * 三件事都**不改世界配置**：「开始整理」只是把动作排进后台队列，真正的说明由后台慢慢写。
 * 所以这里既不走「保存」，也不弹确认框——把它接进保存流程会让"看一眼摘要"变成一次写盘。
 */
function actionDiscoveryCard() {
  const card = el("div", "card action-discovery");
  card.appendChild(el("h2", "", "动作说明整理"));
  card.appendChild(
    el(
      "p",
      "hint",
      "她每一轮看到的动作清单里，每条只有一句话。这一句话由后台整理生成：" +
        "不占回复时间，也不影响动作本身能不能用。",
    ),
  );
  // 用户 m29086：讲清这个功能是干啥的、为什么要有、什么时候需要点。
  const explain = document.createElement("details");
  explain.dataset.role = "explain";
  const explainHead = document.createElement("summary");
  explainHead.textContent = "这是做什么的？为什么要有？要点吗？";
  explain.appendChild(explainHead);
  explain.appendChild(
    el(
      "p",
      "muted",
      "她做决定时，提示词里会列出「此刻能做的事」。每条动作原本只有一个名字（例如「蹭一蹭」），" +
        "名字看不出「什么时候值得做、做了会怎样」。整理就是让辅助模型对着动作的设定，补出一句像人话的说明" +
        "（比如「靠过去蹭一蹭，撒娇或者求安慰的时候用」），她挑动作时更准。",
    ),
  );
  explain.appendChild(
    el(
      "p",
      "muted",
      "为什么要有：不整理她也照样能做那些动作，只是清单里全是人名一样的短词，容易挑错、容易冷场。" +
        "整理是一次性的后台活，写完就存下来，不会每轮重写。",
    ),
  );
  explain.appendChild(
    el(
      "p",
      "muted",
      "无需手动点击——她自己会整理（睡着时每 5 分钟扫一次、醒着每小时一次，" +
        "只排还缺说明的动作，全都有说明就跳过）。只有两种情况值得手动点：" +
        "① 刚加了一批动作、想马上看到整理好的说明；② 改了动作设定之后想让说明跟着更新" +
        "（这时用「全部重算」）。「现在整理一次」只是替你按下扫描，没缺的就不会白花模型调用。",
    ),
  );
  // 默认折叠（details 自己就是折叠的），别用 hidden 把它藏死——用户 m29086 的第三问
  // 就是"要在页面讲明白这是干啥的"，藏在点不开的地方等于没写。
  card.appendChild(explain);
  const summary = el("p", "muted", "正在读取…");
  summary.dataset.role = "summary";
  card.appendChild(summary);
  // 自动整理的状态一行（用户 m29086：让它自己整理这件事要看得见）
  const autoLine = el("p", "muted auto-tidy");
  autoLine.dataset.role = "auto-tidy";
  card.appendChild(autoLine);
  const toolbar = el("div", "toolbar");
  const previewButton = document.createElement("button");
  previewButton.type = "button";
  previewButton.textContent = "看要整理哪些";
  previewButton.title = "列出还缺说明的动作，以及代码现在能算出来的那句话";
  const tidyButton = document.createElement("button");
  tidyButton.type = "button";
  tidyButton.textContent = "现在整理一次";
  tidyButton.title = "立刻扫一遍：只把还缺说明的动作排进后台队列";
  const rebuildButton = document.createElement("button");
  rebuildButton.type = "button";
  rebuildButton.textContent = "开始整理";
  rebuildButton.title = "把这些动作排进后台队列，由辅助模型慢慢写";
  const refreshButton = document.createElement("button");
  refreshButton.type = "button";
  refreshButton.textContent = "刷新";
  refreshButton.title = "重新读取当前的整理进度";
  // 补缺只排"还没有简述"的；全部重算连已经整理好的也再跑一遍（P0-2：按钮叫什么就做什么）
  const forceButton = document.createElement("button");
  forceButton.type = "button";
  forceButton.textContent = "全部重算";
  forceButton.title = "已经整理好的也重新写一遍（会多花模型调用）";
  toolbar.appendChild(previewButton);
  toolbar.appendChild(tidyButton);
  toolbar.appendChild(rebuildButton);
  toolbar.appendChild(forceButton);
  toolbar.appendChild(refreshButton);
  card.appendChild(toolbar);
  const detail = document.createElement("details");
  detail.dataset.role = "detail";
  const detailHead = document.createElement("summary");
  detailHead.textContent = "明细";
  detail.appendChild(detailHead);
  const rows = el("div", "rows");
  rows.dataset.role = "rows";
  detail.appendChild(rows);
  detail.classList.add("hidden");
  card.appendChild(detail);
  previewButton.addEventListener("click", () => previewActionDiscovery(card));
  tidyButton.addEventListener("click", () => tidyActionDiscovery(card));
  rebuildButton.addEventListener("click", () => rebuildActionDiscovery(card, "fill"));
  forceButton.addEventListener("click", () => rebuildActionDiscovery(card, "force"));
  refreshButton.addEventListener("click", () => loadActionDiscovery(card));
  return card;
}

/** 动作库渲染时确保这张卡片在场；只在第一次真的去读一次进度。 */
function ensureActionDiscovery() {
  const anchor = $("action-banner");
  if (!anchor || !anchor.parentNode) return null;
  let card = anchor.parentNode.querySelector(".action-discovery");
  if (!card) {
    card = actionDiscoveryCard();
    anchor.insertAdjacentElement("afterend", card);
    loadActionDiscovery(card);
  }
  return card;
}

function actionDiscoveryParts(card) {
  return {
    summary: card.querySelector('[data-role="summary"]'),
    detail: card.querySelector('[data-role="detail"]'),
    rows: card.querySelector('[data-role="rows"]'),
  };
}

function actionDiscoverySetSummary(card, text) {
  const parts = actionDiscoveryParts(card);
  if (parts.summary) parts.summary.textContent = text;
}

function actionDiscoveryAutoText(report) {
  const auto = (report && report.auto_tidy) || {};
  if (auto.enabled === false) {
    return "自动整理没开着：整理要用「打杂 / 睡眠整理」那个模型，" +
      "在「运行与模型」里配好之后她会自己整理（不用点）。";
  }
  const asleep = auto.sleeping === true;
  const every = asleep ? "她睡着，每 5 分钟扫一次" : "她醒着，每小时扫一次";
  const bits = [every];
  const age = num(auto.last_age, -1);
  const note = String(auto.note || "").trim();
  if (age >= 0) bits.push(`上次扫描 ${agoText(age)}`);
  if (note) {
    // 引擎自己会把这一扫的结果说清楚（排了几个 / 剩下的都是扩展动作 / 全都整理过了）。
    // 页面别再按数字猜一遍——猜出来的话和引擎的判断迟早对不上。
    bits.push(note);
  } else if (num(auto.missing, 0) > 0) {
    bits.push(`还缺 ${num(auto.missing, 0)} 个`);
  } else if (age >= 0) {
    bits.push("没有缺说明的动作，扫完就跳过");
  } else {
    bits.push("还没扫过，下一拍就会看一次");
  }
  return bits.join("；");
}

async function loadActionDiscovery(card) {
  if (!card) return;
  actionDiscoverySetSummary(card, "正在读取…");
  try {
    const report = await apiGet("actions/discovery");
    const missing = num(report.missing_count, (report.missing || []).length);
    const total = num(report.total, 0);
    const pending = num((report.queue || {}).pending, 0);
    const parts = [];
    if (!total) {
      parts.push("还没有任何动作");
    } else if (missing > 0) {
      parts.push(`${missing} 个动作还没有简述（共 ${total} 个）`);
    } else {
      parts.push(`全部 ${total} 个动作都有简述了`);
    }
    // P0-2：来源与停用分开说——"配置里 76 个、这里 73 个"的差额就在这一句里
    const sources = report.sources || {};
    const named = [
      ["world", "自己配的"],
      ["builtin", "内置"],
      ["extension", "扩展"],
      ["runtime", "运行时"],
    ]
      .map(([key, label]) => (num(sources[key], 0) ? `${label} ${num(sources[key], 0)}` : ""))
      .filter(Boolean);
    if (named.length) parts.push(`按来源：${named.join(" · ")}`);
    const disabled = num(report.disabled, 0);
    if (disabled > 0) parts.push(`停用 ${disabled} 个不算在内`);
    if (num(report.unsupported_or_readonly, 0) > 0) {
      parts.push(`${num(report.unsupported_or_readonly, 0)} 个是扩展提供的（只读，用派生存储）`);
    }
    if (pending > 0) parts.push(`后台还有 ${pending} 个在排队`);
    // 后端那句 note 与上面这行说的是同一件事（"N 个动作还没有简述"），**不重复说第二遍**
    // —— 只在数字算不出来、或者还有一句别的话（例如全都整理好了）时才用它兜底。
    const note = String(report.note || "").trim();
    const counted = total > 0 && /还没有简述|都有简述/.test(parts[0] || "");
    if (note && !counted) parts.push(note);
    actionDiscoverySetSummary(card, parts.join("；"));
    if ((report.queue || {}).last_error) {
      const parts2 = actionDiscoveryParts(card);
      if (parts2.summary) {
        parts2.summary.textContent += `（上一次整理没成功：${report.queue.last_error}）`;
      }
    }
    const autoLine = card.querySelector('[data-role="auto-tidy"]');
    if (autoLine) autoLine.textContent = `自动整理：${actionDiscoveryAutoText(report)}`;
  } catch (error) {
    actionDiscoverySetSummary(
      card,
      `读不到整理进度：${(error && error.message) || "接口没有响应"}`,
    );
  }
}

function actionDiscoveryPreviewRow(row) {
  const line = el("div", "row-item");
  const head = el("div", "grow");
  head.appendChild(el("strong", "", `${row.name || row.id || "（没有名字）"}（${row.id || ""}）`));
  line.appendChild(head);
  const body = el("div", "grow");
  const before = String(row.before_summary || "").trim();
  body.appendChild(el("div", "", `现在的那句：${before || "（还没有）"}`));
  const after = String(row.after_summary || "").trim();
  body.appendChild(
    el("div", "", `整理后大概会变成：${after || "（要等模型写出来才知道）"}`),
  );
  if (row.reason) body.appendChild(el("div", "muted", `原因：${row.reason}`));
  if (row.needs_expand) {
    body.appendChild(el("div", "muted", "这条的完整说明要展开才能看清，整理后会短一些。"));
  }
  line.appendChild(body);
  return line;
}

async function previewActionDiscovery(card) {
  if (!card) return;
  const parts = actionDiscoveryParts(card);
  if (parts.rows) parts.rows.innerHTML = "";
  try {
    const result = await apiPost("actions/discovery/preview", {});
    const rows = result.rows || [];
    if (parts.detail) parts.detail.classList.remove("hidden");
    if (!rows.length) {
      if (parts.rows) parts.rows.appendChild(el("p", "muted", "没有需要整理的动作。"));
      if (parts.detail) parts.detail.open = true;
      return;
    }
    rows.forEach((row) => {
      if (parts.rows) parts.rows.appendChild(actionDiscoveryPreviewRow(row));
    });
    if (parts.detail) {
      const head = parts.detail.querySelector("summary");
      if (head) head.textContent = `明细（${rows.length} 条）`;
      parts.detail.open = true;
    }
  } catch (error) {
    if (parts.detail) parts.detail.classList.remove("hidden");
    if (parts.rows) {
      parts.rows.appendChild(
        el("p", "muted", `看不了要整理哪些：${(error && error.message) || "接口没有响应"}`),
      );
    }
  }
}

async function tidyActionDiscovery(card) {
  if (!card) return;
  try {
    const result = await apiPost("actions/discovery/tidy", {});
    if (result && result.ok === false) {
      toast(`扫不动：${result.error || "接口没有响应"}`);
      return;
    }
    const queued = num(result.queued, 0);
    if (queued > 0) {
      toast(`${result.note || `排上 ${queued} 个`}，后台慢慢做`);
    } else if (num(result.missing, 0) > 0) {
      toast(result.note || "缺说明的动作都已经在队列里了");
    } else {
      toast("扫完了：所有动作都有说明，这次一个都没排（也没花模型调用）");
    }
    await loadActionDiscovery(card);
  } catch (error) {
    toast(`扫不动：${(error && error.message) || "接口没有响应"}`);
  }
}

async function rebuildActionDiscovery(card, mode = "fill") {
  if (!card) return;
  try {
    // fill = 补缺（默认）；force = 连已整理好的也重算
    const result = await apiPost("actions/discovery/rebuild", { mode });
    const queued = num(result.queued, 0);
    const label = mode === "force" ? "全部重算：" : "";
    toast(
      queued > 0
        ? `${label}${result.note || `排上 ${queued} 个`}，后台慢慢做`
        : result.note || "没有需要重排的动作",
    );
    await loadActionDiscovery(card);
  } catch (error) {
    toast(`排不进队列：${(error && error.message) || "接口没有响应"}`);
  }
}

function actionDurationText(action) {
  const seconds = num(action.duration, 0) || num(action.duration_max, 0);
  if (action.category !== "continuous") return "—";
  if (!seconds) return "由模型决定";
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}

function renderActionTable(list) {
  const body = $("action-table-body");
  if (!body) return;
  body.innerHTML = "";
  if (!list.length) {
    const row = el("tr");
    const cell = el("td", "muted", actions().length ? "没有匹配的动作。" : "还没有动作。");
    cell.colSpan = 7;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  list.forEach((action) => {
    const owner = actionOwnerOf(action);
    const row = el("tr");
    if (action.enabled === false) row.classList.add("off");

    // 动作名 + id
    const nameCell = el("td", "cell-name", action.name || action.id);
    nameCell.appendChild(el("small", "", action.id));
    row.appendChild(nameCell);

    // 类别：把卡片上那几个 badge 平铺成两小行
    const kind = el("td");
    kind.appendChild(el("div", "", action.category === "continuous" ? "持续动作" : "瞬间动作"));
    const flags = [
      action.llm_level === "tool" ? "工具型" : "",
      action.builtin ? "内置" : "",
      owner ? "扩展" : "",
    ].filter(Boolean);
    if (flags.length) kind.appendChild(el("small", "muted", flags.join(" · ")));
    row.appendChild(kind);

    // 范围
    row.appendChild(
      el(
        "td",
        "",
        action.scope === "node"
          ? `限定 ${(action.allowed_nodes || []).length} 个地点`
          : owner
            ? `来自 ${actionOwnerTitle(owner)}`
            : "全局",
      ),
    );

    row.appendChild(el("td", "", actionDurationText(action)));

    const toolNames = actionToolNames(action);
    row.appendChild(el("td", toolNames.length ? "" : "muted", toolNames.join("、") || "—"));

    const stateCell = el("td");
    stateCell.appendChild(
      action.enabled === false
        ? el("span", "badge off", "已停用")
        : el("span", "badge on", "启用中"),
    );
    row.appendChild(stateCell);

    // 操作：和卡片右下角是同一组按钮
    const actionsCell = el("td", "col-actions");
    const box = el("div", "row-actions");
    const edit = el("button", "icon-btn", "✎");
    edit.type = "button";
    edit.title = "编辑";
    edit.addEventListener("click", () => openActionDrawer(action.id));
    const copy = el("button", "icon-btn", "⧉");
    copy.type = "button";
    copy.title = "复制这个动作";
    copy.addEventListener("click", () => copyAction(action.id));
    const remove = el("button", "icon-btn danger", "🗑");
    remove.type = "button";
    remove.title = owner
      ? "扩展带来的动作不能删（卸掉扩展就没了）"
      : action.builtin
        ? "内置动作只能停用、不能删除"
        : "删除";
    if (action.builtin || owner) {
      remove.disabled = true;
      remove.classList.add("disabled");
    }
    remove.addEventListener("click", () => deleteAction(action.id));
    box.appendChild(edit);
    box.appendChild(copy);
    box.appendChild(remove);
    actionsCell.appendChild(box);
    row.appendChild(actionsCell);

    row.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      openActionDrawer(action.id);
    });
    body.appendChild(row);
  });
}

/** 这条动作属于哪个用途（后端算好的那份；离线时退回它自己写的 use_purpose）。 */
function actionPurposeOf(action) {
  const id = String((action && action.id) || "");
  const map = (ui.status && ui.status.action_purposes) || {};
  const hit = map[id];
  if (hit && hit.label) return hit;
  const own = String((action && action.use_purpose) || "").trim();
  return own ? { purpose: own, label: own, recognized: false } : null;
}

function actionCard(action) {
  const card = el("div", "action-card");
  if (action.enabled === false) card.classList.add("off");
  // 扩展带来的动作由扩展自己在它的设置里管：这里不给删、也不给停用，
  // 不然"停用了"下次加载又冒出来，用户会以为开关坏了。
  const owner = actionOwnerOf(action);

  const head = el("div", "card-head");
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = action.enabled !== false;
  toggle.title = owner
    ? "扩展带来的动作：开关在扩展自己的那一页"
    : "停用后等于她根本没有这个动作";
  if (owner) {
    toggle.disabled = true;
    toggle.classList.add("disabled");
  }
  toggle.addEventListener("click", (event) => event.stopPropagation());
  toggle.addEventListener("change", () => {
    action.enabled = toggle.checked;
    markDirty();
    renderActionGrid();
  });
  head.appendChild(toggle);
  head.appendChild(el("span", "card-name", action.name || action.id));
  if (action.category === "continuous") head.appendChild(el("span", "badge", "持续"));
  if (action.llm_level === "tool") head.appendChild(el("span", "badge", "工具"));
  if (action.builtin) head.appendChild(el("span", "badge", "内置"));
  if (owner) head.appendChild(el("span", "badge", "扩展"));
  // 用途小牌（P0-5）：和提示词里的分组是同一个分类器算出来的，两边看到的一致
  const purpose = actionPurposeOf(action);
  if (purpose) head.appendChild(el("span", "badge purpose", purpose.label));
  card.appendChild(head);

  const meta = el("div", "card-meta");
  meta.appendChild(el("span", "", groupOfAction(action)));
  if (owner) meta.appendChild(el("span", "", `来自扩展：${actionOwnerTitle(owner)}`));
  meta.appendChild(
    el(
      "span",
      "",
      action.scope === "node"
        ? `限定 ${(action.allowed_nodes || []).length} 个地点`
        : "全局",
    ),
  );
  const toolNames = actionToolNames(action);
  if (toolNames.length) meta.appendChild(el("span", "", `工具：${toolNames.join("、")}`));
  if (action.enabled === false) meta.appendChild(el("span", "badge", "已停用"));
  card.appendChild(meta);
  card.appendChild(el("div", "card-desc", action.description || describeAction(action)));

  const tools = el("div", "card-actions");
  const copy = el("button", "icon-btn", "⧉");
  copy.type = "button";
  copy.title = "复制这个动作";
  copy.addEventListener("click", (event) => {
    event.stopPropagation();
    copyAction(action.id);
  });
  const edit = el("button", "icon-btn", "✎");
  edit.type = "button";
  edit.title = "编辑";
  edit.addEventListener("click", (event) => {
    event.stopPropagation();
    openActionDrawer(action.id);
  });
  const remove = el("button", "icon-btn danger", "🗑");
  remove.type = "button";
  remove.title = owner
    ? "扩展带来的动作不能删（卸掉扩展就没了）"
    : action.builtin
      ? "内置动作只能停用、不能删除（关掉左上角的开关即可）"
      : "删除";
  if (action.builtin || owner) {
    remove.disabled = true;
    remove.classList.add("disabled");
  }
  remove.addEventListener("click", (event) => {
    event.stopPropagation();
    deleteAction(action.id);
  });
  tools.appendChild(copy);
  tools.appendChild(edit);
  tools.appendChild(remove);
  card.appendChild(tools);

  card.addEventListener("click", () => openActionDrawer(action.id));
  return card;
}

function openActionDrawer(actionId) {
  const action = actions().find((item) => item.id === actionId);
  if (!action) return;
  ui.selectedAction = actionId;
  ui.actionDraftId = actionId;
  // 编辑的是一份草稿：点「取消」就丢掉，点「保存」才写回列表
  ui.actionDraft = JSON.parse(JSON.stringify(action));
  $("action-title").textContent = `动作属性：${action.name || action.id}`;
  $("action-drawer").classList.remove("hidden");
  $("drawer-backdrop").classList.remove("hidden");
  renderActionForm();
}

function closeActionDrawer() {
  $("action-drawer").classList.add("hidden");
  $("drawer-backdrop").classList.add("hidden");
  ui.actionDraft = null;
  ui.actionDraftId = "";
}

function saveActionDraft() {
  if (!ui.actionDraft) return false;
  const draft = ui.actionDraft;
  draft.id = String(draft.id || "").trim();
  if (!draft.id) {
    toast("动作 ID 不能为空");
    return false;
  }
  const list = actions();
  const index = list.findIndex((item) => item.id === ui.actionDraftId);
  const duplicated = list.some((item) => item.id === draft.id && item !== list[index]);
  if (duplicated) {
    toast(`已经有一个叫 ${draft.id} 的动作了，换个 ID`);
    return false;
  }
  if (index < 0) {
    list.push(draft);
  } else {
    list[index] = draft;
  }
  markDirty();
  closeActionDrawer();
  renderActionGrid();
  renderMap();
  return true;
}

function copyAction(actionId) {
  const source = actions().find((item) => item.id === actionId);
  if (!source) return;
  let id = `${source.id}_copy`;
  let index = 2;
  while (actions().some((item) => item.id === id)) {
    id = `${source.id}_copy${index++}`;
  }
  const clone = JSON.parse(JSON.stringify(source));
  clone.id = id;
  clone.name = `${source.name || source.id}（副本）`;
  // 复制出来的是她自己的动作：把「内置」标记摘掉，否则副本会被当成内置动作（删不掉）
  clone.builtin = false;
  clone.created_at = Date.now();
  actions().push(clone);
  markDirty();
  renderActionGrid();
  openActionDrawer(id);
}

async function deleteAction(actionId) {
  const action = actions().find((item) => item.id === actionId);
  if (!action) return;
  if (action.builtin) {
    toast("内置动作不能删除，用卡片左上角的开关停用就行");
    return;
  }
  const ok = await confirmDialog({
    title: "删除这个动作？",
    message:
      `「${action.name || action.id}」会被从动作库里删掉。` +
      "如果只是暂时不想让她做，用卡片左上角的开关停用即可。",
    confirmText: "删除",
  });
  if (!ok) return;
  ui.config.world.actions = actions().filter((item) => item.id !== actionId);
  if (ui.selectedAction === actionId) ui.selectedAction = "";
  markDirty();
  renderActionGrid();
  renderMap();
}

/** 动作库的 JSON 直接编辑：批量改 / 跨实例搬运用。 */
async function editActionsJson() {
  const json = JSON.stringify(actions(), null, 2);
  openFormDialog({
    title: "动作 JSON",
    hint: "整段替换动作列表。保存时会做一遍校验：必须是数组、每个动作要有唯一的 id。",
    wide: true,
    fields: [{ key: "json", label: "动作（JSON 数组）", type: "textarea", value: json, rows: 18 }],
    onSubmit: (values) => {
      let parsed = null;
      try {
        parsed = JSON.parse(values.json || "[]");
      } catch (error) {
        toast(`JSON 格式不对：${error.message}`);
        return false;
      }
      if (!Array.isArray(parsed)) {
        toast("最外层必须是一个数组");
        return false;
      }
      const ids = new Set();
      for (const item of parsed) {
        if (!item || typeof item !== "object" || !String(item.id || "").trim()) {
          toast("每个动作都必须有 id");
          return false;
        }
        const id = String(item.id).trim();
        if (ids.has(id)) {
          toast(`动作 id 重复：${id}`);
          return false;
        }
        ids.add(id);
      }
      ui.config.world.actions = parsed;
      markDirty();
      renderActionGrid();
      renderMap();
      toast(`已替换为 ${parsed.length} 个动作，点右上角「保存」后生效。`);
      return true;
    },
  });
}

function addAction() {
  const id = `action_${Date.now().toString(36).slice(-4)}`;
  actions().push({
    id,
    name: "新动作",
    description: "",
    category: "instant",
    llm_level: "template",
    scope: "global",
    target_type: "none",
    visible: false,
    interruptible: true,
    enabled: true,
    created_at: Date.now(),
    duration_mode: "fixed",
    duration: 600,
    duration_min: 600,
    duration_max: 1800,
    template: "",
    params: {},
    preconditions: {},
    on_complete: { trigger: "none", effects: {}, effects_per_minute: {} },
    during: {},
  });
  markDirty();
  renderActionGrid();
  openActionDrawer(id);
}

/* ================================================================== */
/* 日程                                                                */
/* ================================================================== */

/** 这条日程的落点写成名字（勾组 / 勾某个会话都认）；没勾就是"各自跑"。 */
function scheduleScopeNames(schedule) {
  const picked = (schedule && schedule.sessions) || [];
  if (!picked.length) return "每个会话各自跑";
  const options = scopeOptions({ withMembers: true });
  const byValue = new Map(options.map((item) => [item.value, item.label]));
  return picked.map((id) => byValue.get(id) || id).join("、");
}

function renderScheduleList() {
  const list = $("schedule-list");
  list.innerHTML = "";
  schedules().forEach((schedule) => {
    const item = el("div", "list-item");
    if (schedule.id === ui.selectedSchedule) item.classList.add("selected");
    const info = el("div", "list-main");
    // 一行给"什么时候触发"，一行给"做什么"：动作显示名字，不再只写 walk_to / stretch 这种 id
    const head = el("div", "schedule-row-head");
    const clock = el("span", `schedule-time${schedule.enabled === false ? " off" : ""}`, schedule.time);
    head.appendChild(clock);
    head.appendChild(el("span", "schedule-name", schedule.name || schedule.id));
    if (schedule.once) head.appendChild(el("span", "badge", "只做一次"));
    if (schedule.enabled === false) head.appendChild(el("span", "badge off", "已停用"));
    head.appendChild(
      el("span", "muted schedule-id", schedule.id === String(schedule.name || "") ? "" : schedule.id),
    );
    info.appendChild(head);
    const stepNames = (schedule.action_chain || [])
      .map((step) => actionLabel(step.type) || step.type)
      .join(" → ");
    const weekday = (schedule.days || []).length === 7 ? "每天" : (schedule.days || []).join(" ");
    info.appendChild(
      el(
        "div",
        "meta",
        [
          weekday,
          stepNames || "（没有动作）",
          schedule.auto_travel ? "自动先走过去" : "",
          schedule.once && schedule.date ? `日期 ${schedule.date}` : "",
          `落点：${scheduleScopeNames(schedule)}`,
        ]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    item.appendChild(info);
    // 两个按钮放一起、靠右贴边（不然「立即执行」会飘在中间）
    const actions = el("div", "list-actions");
    const run = el("button", "small", "▶ 立即执行");
    run.title =
      "现在就跑一遍这条动作链：忽略时间、星期和触发条件，" +
      "也不影响它今天到点的正常触发。";
    run.addEventListener("click", () => runScheduleNow(schedule));
    actions.appendChild(run);
    const del = el("button", "small danger", "删除");
    del.addEventListener("click", () => {
      ui.config.schedules.schedules = schedules().filter((row) => row.id !== schedule.id);
      ui.selectedSchedule = "";
      markDirty("schedules");
      renderScheduleList();
      renderScheduleForm();
    });
    actions.appendChild(del);
    item.appendChild(actions);
    item.addEventListener("click", (event) => {
      if (event.target.tagName === "BUTTON") return;
      ui.selectedSchedule = schedule.id;
      renderScheduleList();
      renderScheduleForm();
    });
    list.appendChild(item);
  });
  if (!schedules().length) list.appendChild(el("p", "muted", "还没有日程。"));
}

/** 「立即执行」：现在就跑一遍这条日程的动作链。 */
async function runScheduleNow(schedule) {
  const sessionId = $("schedule-session").value;
  if (!sessionId) {
    toast("先在右上角选一个会话");
    return;
  }
  const chain =
    (schedule.action_chain || []).map((step) => step.type).join(" → ") || "（没有动作）";
  const ok = await confirmDialog({
    title: "立即执行这条日程？",
    message:
      `会在「${sessionId}」里马上跑一遍：\n${schedule.time} ${schedule.id}：${chain}\n\n` +
      "忽略时间和星期（触发条件也一并忽略），也不会影响它今天到点的正常触发。" +
      "如果她正在忙别的，新安排会排队。",
    confirmText: "立即执行",
  });
  if (!ok) return;
  const operationId = crypto.randomUUID();
  let retry;
  do {
    retry = false;
    try {
      const result = await apiPost("state/action", {
        session: sessionId,
        action: "run_schedule",
        schedule_id: schedule.id,
        force: true,
        operation_id: operationId,
      });
      const replayStatus = {sent: "这次执行已完成", decided_silent: "这次执行已完成，未发言",
        ready_to_send: "这次执行正在等待发送确认", pending: "这次执行仍在处理中",
        generation_unknown: "这次执行尚未确认，请勿重复发起", failed: "这次执行失败，未重新执行"};
      toast(result.replayed ? replayStatus[result.status] || "已有执行记录，等待确认" :
        result.ok ? result.note || "已执行" : result.reason || "没有执行");
      if (result.ok || result.replayed) refreshStatus();
    } catch (error) {
      retry = await confirmDialog({
        title: "没有收到执行结果",
        message: `${error.message || "连接失败"}。重试会查询同一次执行，不会重复执行。`,
        confirmText: "重试",
      });
    }
  } while (retry);
}

const REPEAT_KINDS = [
  { key: "daily", label: "每天" },
  { key: "weekly", label: "每周的某几天" },
  { key: "monthly", label: "每月的某几号" },
  { key: "every", label: "每 N 分钟" },
  { key: "workdays", label: "工作日（周一到周五）" },
  { key: "once", label: "只做这一次" },
  { key: "cron", label: "cron 表达式（高级）" },
];

const REPEAT_DEFAULT_WINDOW = { start: "00:00", end: "23:59" };

/**
 * 这条日程的频率（`repeat`）：老的 `time` + `days` 会被推断成一次，
 * 之后一律以 `repeat` 为准 —— 否则「每天 08:00、22:00 各一次」这种写法
 * 在 `time` 只有一个值的旧结构里根本存不下。
 */
function repeatOf(schedule) {
  if (!schedule.repeat || typeof schedule.repeat !== "object") {
    const days = Array.isArray(schedule.days) ? schedule.days.slice() : [];
    let kind = "daily";
    if (schedule.once) kind = "once";
    else if (days.length && days.length < 7) {
      const sorted = days.slice().sort().join(",");
      kind = sorted === "mon,tue,wed,thu,fri" ? "workdays" : "weekly";
    }
    schedule.repeat = {
      kind,
      at: [schedule.time || "08:00"],
      weekdays: kind === "weekly" ? days : [],
      month_days: [],
      every_minutes: 60,
      window: { ...REPEAT_DEFAULT_WINDOW },
      expr: "",
    };
  }
  const repeat = schedule.repeat;
  if (!Array.isArray(repeat.at) || !repeat.at.length) repeat.at = [schedule.time || "08:00"];
  if (!Array.isArray(repeat.weekdays)) repeat.weekdays = [];
  if (!Array.isArray(repeat.month_days)) repeat.month_days = [];
  if (!repeat.window || typeof repeat.window !== "object") repeat.window = { ...REPEAT_DEFAULT_WINDOW };
  if (!REPEAT_KINDS.some((item) => item.key === repeat.kind)) repeat.kind = "daily";
  return repeat;
}

/** 把结构化频率投影回 `time` / `days` / `once`：提示词、日志、列表摘要都还在读它们。 */
function syncScheduleLegacy(schedule) {
  const repeat = repeatOf(schedule);
  schedule.once = repeat.kind === "once";
  schedule.time = String(repeat.at[0] || "08:00");
  if (schedule.once) return;
  if (repeat.kind === "workdays") {
    schedule.days = ["mon", "tue", "wed", "thu", "fri"];
  } else if (repeat.kind === "weekly") {
    const picked = new Set(repeat.weekdays || []);
    schedule.days = WEEKDAYS.filter((day) => picked.has(day.key)).map((day) => day.key);
  } else {
    schedule.days = WEEKDAYS.map((day) => day.key);
  }
}

const CLOCK_SPLIT = /[,，、\s]+/;
const CLOCK_TEXT = /^(\d{1,2}):(\d{2})$/;

/** 「08:00, 22:30」→ ["08:00", "22:30"]（乱写的部分直接丢掉，不让它进配置）。 */
function parseClockList(text) {
  const out = [];
  String(text || "")
    .split(CLOCK_SPLIT)
    .forEach((item) => {
      const match = CLOCK_TEXT.exec(item.trim());
      if (!match) return;
      const hour = Number(match[1]);
      const minute = Number(match[2]);
      if (hour > 23 || minute > 59) return;
      const clock = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
      if (!out.includes(clock)) out.push(clock);
    });
  return out.length ? out : ["08:00"];
}

/** 一句话预览：这条日程实际会在什么时候触发（与后端的 cron 推导同一套口径）。 */
function repeatPreview(schedule) {
  const repeat = repeatOf(schedule);
  const times = (repeat.at || []).filter((item) => /^\d{2}:\d{2}$/.test(item));
  const clocks = times.length ? times.join("、") : "08:00";
  if (repeat.kind === "cron") return `按 cron「${repeat.expr || "（空）"}」触发`;
  if (repeat.kind === "once") return `只在 ${schedule.date || "下一次到点"} ${clocks} 跑一次，跑完删掉`;
  if (repeat.kind === "every") {
    const window = repeat.window || REPEAT_DEFAULT_WINDOW;
    return `在 ${window.start}-${window.end} 之间，每 ${repeat.every_minutes || 60} 分钟一次`;
  }
  if (repeat.kind === "workdays") return `周一到周五 ${clocks}`;
  if (repeat.kind === "monthly") {
    const days = (repeat.month_days || []).join("、") || "1";
    return `每月 ${days} 号 ${clocks}`;
  }
  if (repeat.kind === "weekly") {
    const picked = new Set(repeat.weekdays || []);
    const label = WEEKDAYS.filter((day) => picked.has(day.key))
      .map((day) => day.label)
      .join(" ") || "（没勾）";
    return `${label} ${clocks}`;
  }
  return `每天 ${clocks}`;
}

function scheduleRepeatBox(schedule) {
  const repeat = repeatOf(schedule);
  const box = el("div", "subsection");
  const title = el("div", "sub-title");
  title.appendChild(el("span", "", "触发频率"));
  title.appendChild(tipBox("一天里可以有多个时间点；tick 间隔决定精度（默认 1 分钟一拍）。"));
  box.appendChild(title);
  box.appendChild(
    selectField(
      "频率",
      repeat.kind,
      REPEAT_KINDS.map((item) => ({ key: item.key, label: item.label })),
      (value) => {
        repeat.kind = value;
        if (value === "weekly" && !(repeat.weekdays || []).length) {
          repeat.weekdays = WEEKDAYS.map((day) => day.key);
        }
        if (value === "workdays") repeat.weekdays = ["mon", "tue", "wed", "thu", "fri"];
        if (value === "monthly" && !(repeat.month_days || []).length) repeat.month_days = [1];
        syncScheduleLegacy(schedule);
        markDirty();
        renderScheduleForm();
      },
      { hint: "「每 N 分钟」适合巡逻类的日常；太频繁会被限流（一天最多 24 次）。" },
    ),
  );
  if (repeat.kind === "cron") {
    box.appendChild(
      inputField(
        "cron 表达式",
        repeat.expr || "",
        (value) => {
          repeat.expr = value.trim();
          markDirty();
        },
        {
          hint: "五段：分 时 日 月 周，例 0 8 * * 1,3,5 即周一三五 08:00。",
          placeholder: "0 8 * * 1,3,5",
        },
      ),
    );
  } else {
    box.appendChild(
      inputField(
        "时间点",
        (repeat.at || []).join(", "),
        (value) => {
          repeat.at = parseClockList(value);
          syncScheduleLegacy(schedule);
          markDirty();
          renderScheduleForm();
        },
        {
          hint: "24 小时制 HH:MM，多个时间用逗号隔开，例如 08:00, 12:30, 22:30。",
          placeholder: "08:00, 22:30",
        },
      ),
    );
  }
  if (repeat.kind === "weekly") {
    const wrapper = el("div", "field");
    wrapper.appendChild(fieldHead("星期", "只在勾选的星期触发。"));
    const row = el("div", "weekdays");
    WEEKDAYS.forEach((day) => {
      const picked = (repeat.weekdays || []).includes(day.key);
      const label = el("label", picked ? "on" : "");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = picked;
      input.addEventListener("change", () => {
        const set = new Set(repeat.weekdays || []);
        if (input.checked) set.add(day.key);
        else set.delete(day.key);
        repeat.weekdays = WEEKDAYS.filter((item) => set.has(item.key)).map((item) => item.key);
        label.classList.toggle("on", input.checked);
        syncScheduleLegacy(schedule);
        markDirty();
        renderScheduleForm();
      });
      label.appendChild(input);
      label.appendChild(el("span", "", day.label));
      row.appendChild(label);
    });
    wrapper.appendChild(row);
    box.appendChild(wrapper);
  }
  if (repeat.kind === "monthly") {
    box.appendChild(
      inputField(
        "每月几号",
        (repeat.month_days || []).join(", "),
        (value) => {
          const days = String(value || "")
            .split(CLOCK_SPLIT)
            .map((item) => Number(item))
            .filter((item) => Number.isInteger(item) && item >= 1 && item <= 31);
          repeat.month_days = Array.from(new Set(days)).sort((a, b) => a - b);
          markDirty();
          renderScheduleForm();
        },
        {
          hint: "写 1~31，多个用逗号隔开；小月没有的日子会自动跳过。",
          placeholder: "1, 15",
        },
      ),
    );
  }
  if (repeat.kind === "every") {
    box.appendChild(
      inputField(
        "间隔分钟",
        repeat.every_minutes ?? 60,
        (value) => {
          repeat.every_minutes = Math.max(5, Math.min(1440, num(value, 60)));
          markDirty();
          renderScheduleForm();
        },
        {
          hint: "最小 5 分钟。间隔小于 tick 的话实际会被 tick 拍数限制。",
          type: "number",
          min: 5,
          step: 5,
        },
      ),
    );
    box.appendChild(
      inputField(
        "时间段 起",
        (repeat.window || REPEAT_DEFAULT_WINDOW).start,
        (value) => {
          repeat.window.start = parseClockList(value)[0];
          markDirty();
          renderScheduleForm();
        },
        { hint: "只在这段时间内触发，例：08:00。", placeholder: "08:00" },
      ),
    );
    box.appendChild(
      inputField(
        "时间段 止",
        (repeat.window || REPEAT_DEFAULT_WINDOW).end,
        (value) => {
          repeat.window.end = parseClockList(value)[0];
          markDirty();
          renderScheduleForm();
        },
        { hint: "例：22:30。跨午夜请写成两条日程。", placeholder: "22:30" },
      ),
    );
  }
  const preview = el("p", "muted", `实际触发：${repeatPreview(schedule)}`);
  box.appendChild(preview);
  refreshRepeatPreview(schedule, preview);
  return box;
}

// 编辑器只算得出「每天 08:30」这一层；工作日 / 每 N 分钟 / cron / 跨午夜都交给后端
// （`schedule/repeat/preview`）。先本地显示一个即时结果，后端回来再覆盖；请求晚到的
// 旧结果不能盖掉新结果，所以带一个自增序号。
let repeatPreviewSeq = 0;

function refreshRepeatPreview(schedule, node) {
  const token = ++repeatPreviewSeq;
  const local = repeatPreview(schedule);
  node.textContent = `实际触发：${local}`;
  const repeat = repeatOf(schedule);
  apiPost("schedule/repeat/preview", {
    repeat,
    time: schedule.time,
    days: schedule.days,
    once: Boolean(schedule.once),
    date: schedule.date || "",
  })
    .then((result) => {
      if (token !== repeatPreviewSeq || !node.isConnected) return;
      if (!result || result.ok === false) {
        node.textContent = `实际触发：${(result && result.error) || "这句算不出来"}（先按 ${local}）`;
        return;
      }
      const exprs = (result.exprs || []).join(" / ");
      const cron = exprs ? `（cron：${exprs}）` : "";
      const next = result.next_text ? ` · ${result.next_text}` : "";
      node.textContent = `实际触发：${result.when_text || local}${cron}${next}`;
    })
    .catch(() => {
      // 端点不可用（旧后端）就保留本地预览，不打扰用户
    });
}

/**
 * 触发频率收进「高级」：大多数日程就是「每天几点」，第一眼不该被 7 档频率、星期勾选、
 * 间隔分钟铺满。折叠标题上仍写着现在的实际触发时间，想知道随时一眼能看到。
 */
function scheduleRepeatDetails(schedule) {
  const box = el("details", "advanced");
  const summary = el("summary", "", `高级：触发频率（现在：${repeatPreview(schedule) || "每天"}）`);
  box.appendChild(summary);
  box.appendChild(scheduleRepeatBox(schedule));
  return box;
}

function renderScheduleForm() {
  const form = $("schedule-form");
  // 日程属性里绝大多数控件（描述、时间、按钮组、星期勾选）改完都不自己报改动，
  // 统一在这里听 change，免得"改了描述按保存却说没有要保存的改动"（用户 m29086）。
  bindDirtyChange(form, "schedules");
  form.innerHTML = "";
  const schedule = schedules().find((item) => item.id === ui.selectedSchedule);
  $("schedule-title").textContent = schedule ? `日程属性：${schedule.id}` : "日程属性";
  if (!schedule) {
    form.appendChild(el("p", "muted", "点击左侧日程进行编辑。"));
    return;
  }
  schedule.conditions = schedule.conditions || {};
  schedule.action_chain = schedule.action_chain || [];

  form.appendChild(
    inputField("日程 ID", schedule.id, (value) => (schedule.id = value.trim()), {
      hint: "内部标识，用于「接着执行另一个日程」。",
    }),
  );
  form.appendChild(scheduleRepeatDetails(schedule));
  if (repeatOf(schedule).kind === "once") {
    form.appendChild(
      inputField("日期", schedule.date || "", (value) => (schedule.date = value.trim()), {
        hint: "格式 YYYY-MM-DD；留空表示下一次到点就跑。日期过了的会自己清掉。",
        placeholder: "2026-09-22",
      }),
    );
  }
  form.appendChild(
    inputField(
      "描述（这条日程是干什么的）",
      schedule.note || "",
      (value) => (schedule.note = value.trim()),
      {
        hint: "到点时会连着动作链一起带给她：她知道自己现在在做什么、为什么做。留空就只有动作链。",
        placeholder: "睡前收尾：洗漱完回卧室躺下，别再摸手机（主人说下午可能下雨，记得收衣服）",
      },
    ),
  );
  form.appendChild(checkboxField("启用", schedule.enabled !== false, (value) => (schedule.enabled = value)));

  const conditionBox = el("div", "subsection");
  const conditionTitle = el("div", "sub-title");
  conditionTitle.appendChild(el("span", "", "触发条件"));
  conditionTitle.appendChild(
    tipBox("满足这些条件才会执行，不满足则跳过本次（例如睡觉时不执行早安）。"),
  );
  conditionBox.appendChild(conditionTitle);
  conditionBox.appendChild(
    pickerField(
      "不在这些状态下触发",
      schedule.conditions.not_state || [],
      STATES.map((item) => ({ id: item.key, name: item.label, desc: item.key })),
      (chosen) => {
        schedule.conditions.not_state = chosen;
        renderScheduleForm();
      },
      { hint: "例如「睡觉中」时跳过。", empty: "（不限制）" },
    ),
  );
  conditionBox.appendChild(
    inputField(
      "最低精力",
      schedule.conditions.min_energy ?? "",
      (value) => {
        schedule.conditions.min_energy = value === "" ? null : num(value, 0);
      },
      { hint: "精力低于这个值时跳过（0~1，留空表示不限制）。", type: "number", step: "0.05" },
    ),
  );
  conditionBox.appendChild(
    pickerField(
      "只在这些地点触发",
      schedule.conditions.node_in || [],
      nodeItems(),
      (chosen) => {
        schedule.conditions.node_in = chosen;
        renderScheduleForm();
      },
      { hint: "留空表示任何地点都可以触发。", empty: "（不限制）" },
    ),
  );
  form.appendChild(conditionBox);

  form.appendChild(
    checkboxField(
      "自动先走过去（推荐）",
      schedule.auto_travel === true,
      (value) => {
        schedule.auto_travel = value;
      },
      {
        hint: "开启后，某一步的地点不满足（例如上网要在书房）会先走到那里再执行；关闭则跳过该步骤。",
      },
    ),
  );

  form.appendChild(
    checkboxField(
      "智能日程（到点让大模型补每步的意图）",
      schedule.smart === true,
      (value) => {
        schedule.smart = value;
        renderScheduleForm();
      },
      {
        hint: "到点把这条动作链交给大模型补写每步意图，再照原样执行（步数、动作、时长不变）。代价是每次多一次调用。",
      },
    ),
  );

  // 这条日程的话说给谁（会话 / 会话组）：只在"她这一组"里挑
  const scopeFor = $("schedule-session") ? $("schedule-session").value : "";
  form.appendChild(
    pickerField(
      "落点（会话 / 会话组）",
      schedule.sessions || [],
      scopeOptions({ withMembers: true, onlyFor: scopeFor }),
      (chosen) => {
        // Groups and individual destinations are mutually exclusive here only.
        const groupIds = new Set(groups().map((item) => String(item.id)));
        const pickedGroups = chosen.filter((item) => groupIds.has(String(item)));
        schedule.sessions = pickedGroups.length ? pickedGroups : chosen;
        renderScheduleForm();
      },
      {
        hint:
          "组与单个会话不能混选：勾组 = 大模型在组内挑一处；勾会话 = 固定发到所选会话；留空 = 各自跑。",
        empty: "（每个会话各自跑）",
        exclusiveValues: groups().map((item) => item.id),
      },
    ),
  );

  form.appendChild(
    chainEditor(
      schedule.action_chain,
      (chain) => {
        schedule.action_chain = chain;
      },
      { smart: schedule.smart === true },
    ),
  );

  const runRow = el("div", "row");
  const runButton = el("button", "ghost", "▶ 立即执行这条日程");
  runButton.type = "button";
  runButton.title =
    "现在就跑一遍这条动作链：忽略时间、星期和触发条件，也不会影响它今天到点的正常触发。";
  runButton.addEventListener("click", () => runScheduleNow(schedule));
  runRow.appendChild(runButton);
  runRow.appendChild(
    el(
      "span",
      "muted",
      "调试用：不用等到点，直接看她这条动作链会怎么走；结果会真的发到群里。",
    ),
  );
  form.appendChild(runRow);
}

function weekdayField(schedule) {
  const wrapper = el("div", "field");
  wrapper.appendChild(fieldHead("星期", "只在勾选的星期触发。"));
  const row = el("div", "weekdays");
  const selected = new Set(schedule.days || []);
  WEEKDAYS.forEach((day) => {
    const label = el("label", selected.has(day.key) ? "on" : "");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = selected.has(day.key);
    box.addEventListener("change", () => {
      const days = new Set(schedule.days || []);
      if (box.checked) days.add(day.key);
      else days.delete(day.key);
      schedule.days = WEEKDAYS.filter((item) => days.has(item.key)).map((item) => item.key);
      label.classList.toggle("on", box.checked);
    });
    label.appendChild(box);
    label.appendChild(el("span", "", day.label));
    row.appendChild(label);
  });
  wrapper.appendChild(row);
  return wrapper;
}

function addSchedule() {
  const id = `schedule_${Date.now().toString(36).slice(-4)}`;
  schedules().push({
    id,
    enabled: true,
    time: "08:00",
    days: WEEKDAYS.map((day) => day.key),
    action_chain: [{ type: actions()[0]?.id || "say", messages: [] }],
    conditions: {},
    priority: 5,
    auto_travel: true,
  });
  ui.selectedSchedule = id;
  markDirty("schedules");
  renderScheduleList();
  renderScheduleForm();
}

/* ================================================================== */
/* 会话                                                                */
/* ================================================================== */

function renderSessionList() {
  const list = $("session-list");
  // 会话页同样只靠少数几个控件自己报改动；备注输入框之类的通用控件改了不算改动，
  // 统一在面板上听 change（用户 m29086）。
  bindDirtyChange($("session-pane"), "sessions");
  list.innerHTML = "";
  ui.sessions.forEach((session) => {
    const item = el("div", "list-item");
    const info = el("div");
    info.appendChild(el("div", "", `${session.enabled ? "✅" : "⛔"} ${session.session_id}`));
    info.appendChild(
      el(
        "div",
        "meta",
        `${session.type === "private" ? "私聊" : "群聊"} · ${session.platform || "未知平台"} · 冷启动 ${
          session.cold_start_mode || "awakening"
        } @ ${session.cold_start_node || "bedroom"}`,
      ),
    );
    // 备注：她在提示词里就按这个名字认这个会话（有备注就不显示群名）
    const noteRow = el("div", "session-note");
    const noteInput = document.createElement("input");
    noteInput.type = "text";
    noteInput.value = session.note || "";
    noteInput.placeholder = "备注（她看到的名字，留空用群名 / 号码）";
    noteInput.addEventListener("change", () => {
      session.note = noteInput.value.trim();
      ui.config.sessions.sessions = ui.sessions;
      markDirty();
      renderGroupList();
    });
    noteRow.appendChild(noteInput);
    info.appendChild(noteRow);
    const position = (ui.overview || []).find(
      (item) => item.session_id === session.session_id,
    );
    if (position) {
      const stateLabel = stateLabelOf(position.state, position);
      info.appendChild(
        el(
          "div",
          "meta",
          `现在在「${position.node_name || position.node_id}」，状态：${stateLabel}，心情：${position.mood}`,
        ),
      );
    }
    item.appendChild(info);
    const toggle = el("button", "small", session.enabled ? "禁用" : "启用");
    toggle.addEventListener("click", () => {
      session.enabled = !session.enabled;
      markDirty("sessions");
      renderSessionList();
      renderSessionSelects();
    });
    const del = el("button", "small danger", "删除");
    del.addEventListener("click", () => {
      ui.sessions = ui.sessions.filter((row) => row.session_id !== session.session_id);
      ui.config.sessions.sessions = ui.sessions;
      markDirty("sessions");
      renderSessionList();
      renderSessionSelects();
    });
    const box = el("div", "inline");
    box.appendChild(toggle);
    box.appendChild(del);
    item.appendChild(box);
    list.appendChild(item);
  });
  if (!ui.sessions.length) {
    list.appendChild(
      el("p", "muted", "还没有会话。填上面的输入框添加，或在群里发 /vw session add。"),
    );
  }
  renderGroupList();
}

/**
 * 临时会话（用户 m00001 需求 12）：她成功私聊出去的陌生人。
 *
 * 三栏：活跃（还没到期）/ 已结束（收尾了）/ 已归档（默认收起来）。
 * 「归档」只是把这张卡片收进第三栏，不动数据；「恢复」再放回去。
 */

function tempSourceText(source) {
  // 来源按 main.py `_send_temp_session` 有没有走临时会话发送算（需求 11）
  return source === "group_temp" ? "群临时会话" : "好友私聊";
}

function tempStateText(state) {
  if (state === "active") return "活跃";
  if (state === "expired") return "到期了（他再开口就收尾）";
  // 解除接管（审查 H2）：这是「已结束之后」唯一的人工出口，她的钩子不再吞这个 umo
  if (state === "released") return "已解除接管（消息回到宿主默认路由）";
  return "已结束";
}

function tempTimeText(stamp) {
  const value = Number(stamp) || 0;
  if (!value) return "—";
  try {
    return new Date(value * 1000).toLocaleString();
  } catch (error) {
    return "—";
  }
}

async function loadTempSessions() {
  const list = $("temp-session-list");
  if (!list) return;
  try {
    const data = await apiGet("temp_sessions");
    ui.tempSessions = Array.isArray(data.items) ? data.items : [];
  } catch (error) {
    list.innerHTML = "";
    list.appendChild(
      el("p", "muted", `临时会话读不出来：${error && error.message ? error.message : error}`),
    );
    return;
  }
  renderTempSessionList();
}

function renderTempSessionList() {
  const list = $("temp-session-list");
  if (!list) return;
  const items = Array.isArray(ui.tempSessions) ? ui.tempSessions : [];
  list.innerHTML = "";
  if (!items.length) {
    list.appendChild(
      el("p", "muted", "还没有临时会话。她成功私聊一个还没启用的人之后，这里才会出现记录。"),
    );
    return;
  }
  const buckets = [
    ["active", "活跃", items.filter((item) => !item.archived && item.state === "active")],
    [
      "ended",
      "已结束",
      items.filter((item) => !item.archived && item.state !== "active"),
    ],
    ["archived", "已归档", items.filter((item) => item.archived)],
  ];
  buckets.forEach(([_key, title, rows]) => {
    if (!rows.length) return;
    list.appendChild(el("div", "temp-group-title", `${title}（${rows.length}）`));
    rows.forEach((row) => {
      const item = el("div", "list-item");
      const info = el("div", "list-main");
      info.appendChild(el("div", "", `👤 ${row.umo}`));
      info.appendChild(
        el(
          "div",
          "meta",
          `${tempSourceText(row.source)} · 创建于 ${tempTimeText(row.created_at)} · ${Number(
            row.rounds,
          ) || 0} 轮 · ${tempStateText(row.state)}`,
        ),
      );
      const summary = String(row.summary || "").trim();
      if (summary) {
        info.appendChild(el("div", "meta", `摘要：${summary}`));
      }
      item.appendChild(info);
      const toggle = el("button", "small", row.archived ? "恢复" : "归档");
      toggle.addEventListener("click", async () => {
        toggle.disabled = true;
        try {
          await apiPost("temp_sessions/archive", {
            umo: row.umo,
            archived: !row.archived,
          });
          // 「恢复」会把 ended/expired 一并复位成 active（审查 H2-3），所以状态
          // 以服务端为准，整表重取一遍，别在前端自己猜。
          await loadTempSessions();
        } catch (error) {
          toast(`没改成：${error && error.message ? error.message : error}`, "danger");
        } finally {
          toggle.disabled = false;
        }
      });
      const box = el("div", "list-actions");
      box.appendChild(toggle);
      if (row.state !== "released") {
        // 解除接管（审查 H2）：ended 之后她本来会吞掉这个人的所有消息；点这里把
        // 临时会话置 released、白名单关掉，消息回到宿主默认路由。
        const release = el("button", "small", "解除接管");
        release.title = "不再由她接管这个人的私聊；消息回到宿主默认路由";
        release.addEventListener("click", async () => {
          // 编辑器跑在没有 allow-modals 的 sandbox iframe 里，裸 confirm 会被拦掉；
          // 统一走页面自己的弹窗（跟别处一样）。
          const sure = await confirmDialog({
            title: "解除接管",
            message: `解除对 ${row.umo} 的接管？之后这个人的消息不再由她回，回到宿主默认路由。`,
            confirmText: "解除接管",
          });
          if (!sure) {
            return;
          }
          release.disabled = true;
          try {
            await apiPost("temp_sessions/release", { umo: row.umo });
            toast("已解除接管：这个人的消息回到宿主默认路由了");
            await loadTempSessions();
          } catch (error) {
            toast(`没解开：${error && error.message ? error.message : error}`, "danger");
          } finally {
            release.disabled = false;
          }
        });
        box.appendChild(release);
      }
      item.appendChild(box);
      list.appendChild(item);
    });
  });
}

/**
 * 临时会话的 6 项规则（用户 m00001）：读写的是 ``world.temp_session``，
 * 跟手改 world.json、引擎里的那个 ``TempSessionConfig`` 是同一份。
 */
function tempSessionConfig() {
  if (!ui.config.world) ui.config.world = {};
  if (!ui.config.world.temp_session) ui.config.world.temp_session = {};
  return ui.config.world.temp_session;
}

/** 关系等级只认 ``world.profile.levels`` 里的真实档位，不在前端另抄一份。 */
function tempLevelChoices() {
  const profile = (ui.config.world && ui.config.world.profile) || {};
  const levels = Array.isArray(profile.levels) ? profile.levels : [];
  return levels.map((level, index) => [
    String(index),
    String((level && level.name) || `第 ${index + 1} 档`),
  ]);
}

/** 控件挂上稳定 id / name，既方便测试定位，也让人一眼看出存的是哪个字段。 */
function tempConfigInput(node, field) {
  const input = node.querySelector("input, select");
  if (input) {
    input.id = `temp-config-${field.replace(/_/g, "-")}`;
    input.name = `temp_session.${field}`;
  }
  return node;
}

/** 一行 = 控件 + 控件下面那句人话说明（label 里塞 p 不合法，所以包一层 .field）。 */
function tempConfigRow(control, hint) {
  const row = el("div", "field");
  row.appendChild(control);
  row.appendChild(el("p", "hint", hint));
  return row;
}

function renderTempConfig() {
  const host = $("temp-config");
  if (!host) return;
  const cfg = tempSessionConfig();
  host.innerHTML = "";
  const section = settingsSection(
    "临时会话规则",
    "这里决定她怎么接管临时会话；保存后立即生效，也可以直接手改 world.json。",
    "",
    "temp_session",
  );
  section.id = "temp-config-card";
  const fields = section._fields;
  const rows = [];

  const enabled = checkboxField(
    "自动接管临时会话",
    cfg.enabled !== false,
    (value) => {
      cfg.enabled = !!value;
      markDirty("world");
    },
  );
  rows.push(
    tempConfigRow(
      tempConfigInput(enabled, "enabled"),
      "关掉后，她给还没启用的人发消息也不会再登记；已经有的记录留在下面。",
    ),
  );

  const numberRow = (field, label, min, max, hint) => {
    let input = null;
    const node = inputField(
      label,
      cfg[field] ?? "",
      (raw) => {
        const next = Number(raw);
        if (!Number.isInteger(next) || next < min || next > max) {
          toast(`${label}要填 ${min}~${max} 的整数，已保留原来的值`, "danger");
          if (input) input.value = String(cfg[field] ?? "");
          return;
        }
        cfg[field] = next;
        markDirty("world");
      },
      { type: "number", min, max, step: 1 },
    );
    input = node.querySelector("input");
    return tempConfigRow(tempConfigInput(node, field), hint);
  };

  rows.push(
    numberRow(
      "ttl_minutes",
      "到期时间（分钟）",
      1,
      1440,
      "到期只把会话标成「到期」，她不会主动发消息；对方再来找她时，回完最后一句就收尾。",
    ),
  );
  rows.push(
    numberRow(
      "max_rounds",
      "最多聊几轮",
      1,
      200,
      "一条消息加一句回复算 1 轮；到最后一轮，她说完这句就收尾。",
    ),
  );

  const keep = checkboxField(
    "熟人永久保留",
    cfg.familiar_keep !== false,
    (value) => {
      cfg.familiar_keep = !!value;
      markDirty("world");
    },
  );
  rows.push(
    tempConfigRow(
      tempConfigInput(keep, "familiar_keep"),
      "打开后，达到下面那一档的人不参与到期，会一直保留；关掉就一样按分钟数到期。",
    ),
  );

  const choices = tempLevelChoices();
  const current = Number(cfg.familiar_level);
  const levelValue = String(Number.isInteger(current) ? current : 0);
  const level = selectField(
    "从这一档算熟人",
    levelValue,
    choices.length ? choices : [[levelValue, "关系等级没读到"]],
    (value) => {
      const next = Number(value);
      if (!Number.isInteger(next)) return;
      cfg.familiar_level = next;
      markDirty("world");
    },
  );
  rows.push(
    tempConfigRow(
      tempConfigInput(level, "familiar_level"),
      "这一档及以上算熟人：不参与到期，记忆也正常留下；低于它的按临时会话处理。",
    ),
  );

  const group = checkboxField(
    "群临时会话也接管",
    cfg.group_temp !== false,
    (value) => {
      cfg.group_temp = !!value;
      markDirty("world");
    },
  );
  rows.push(
    tempConfigRow(
      tempConfigInput(group, "group_temp"),
      "从群里发起的私聊同样登记成临时会话；关掉就只接管好友私聊。",
    ),
  );

  rows.forEach((row) => fields.appendChild(row));
  host.appendChild(section);
}

/** 会话组：组里的群 / 私聊共享聊天上下文与记忆。 */
function groups() {
  if (!ui.config.sessions) ui.config.sessions = { sessions: [], groups: [] };
  if (!Array.isArray(ui.config.sessions.groups)) ui.config.sessions.groups = [];
  return ui.config.sessions.groups;
}

function renderGroupList() {
  const list = $("group-list");
  if (!list) return;
  // 组名用的是通用输入框（自己不报改动），跟会话面板一样在容器上听 change。
  bindDirtyChange($("group-pane"), "sessions");
  list.innerHTML = "";
  groups().forEach((group) => {
    const item = el("div", "list-item");
    const info = el("div", "list-main");
    info.appendChild(el("div", "", `🔗 ${group.name || group.id}`));
    info.appendChild(
      el(
        "div",
        "meta",
        (group.sessions || []).join("、") +
          `　代表会话：${group.main_session || "（未指定）"}`,
      ),
    );
    item.appendChild(info);
    const box = el("div", "list-actions");
    const edit = el("button", "small", ui.selectedGroup === group.id ? "收起" : "编辑");
    edit.addEventListener("click", () => {
      ui.selectedGroup = ui.selectedGroup === group.id ? "" : group.id;
      renderGroupList();
    });
    box.appendChild(edit);
    const del = el("button", "small danger", "删除");
    del.addEventListener("click", () => {
      ui.config.sessions.groups = groups().filter((row) => row.id !== group.id);
      markDirty();
      renderGroupList();
    });
    box.appendChild(del);
    item.appendChild(box);
    list.appendChild(item);
    if (ui.selectedGroup === group.id) list.appendChild(buildGroupEditor(group));
  });
  if (!groups().length) {
    list.appendChild(
      el("p", "muted", "还没有会话组。想让几个群 / 私聊连着聊同一个话题，就加一个。"),
    );
  }
}

/** 展开的会话组编辑块：选成员、指定代表会话。 */

function buildGroupEditor(group) {
  const card = el("div", "card group-editor");
  card.appendChild(
    inputField("组名", group.name || "", (value) => (group.name = value.trim()), {
      hint: "只用来区分这个组；它也是提示词里「来自…」显示的名字。",
    }),
  );
  const picked = new Set(group.sessions || []);
  card.appendChild(
    fieldHead("成员", "勾上的会话共享聊天上下文与记忆；同一个会话只能属于一个组。"),
  );
  const members = el("div", "picker-list");
  ui.sessions.forEach((session) => {
    const row = el("label", "picker-row");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = picked.has(session.session_id);
    box.addEventListener("change", () => {
      if (box.checked) picked.add(session.session_id);
      else picked.delete(session.session_id);
      group.sessions = ui.sessions
        .map((item) => item.session_id)
        .filter((id) => picked.has(id));
      if (!group.sessions.includes(group.main_session)) {
        group.main_session = group.sessions[0] || "";
      }
      markDirty();
      renderGroupList();
    });
    row.appendChild(box);
    row.appendChild(
      el("span", "", `${session.session_id}${session.type === "private" ? "（私聊）" : ""}`),
    );
    members.appendChild(row);
  });
  card.appendChild(members);

  const mainBox = el("div", "field");
  mainBox.appendChild(
    fieldHead(
      "代表会话",
      "她的位置、数值、计划都存在它名下；这个会话停用时自动退到组里第一个可用的成员。",
    ),
  );
  const select = document.createElement("select");
  (group.sessions || []).forEach((id) => {
    const row = document.createElement("option");
    row.value = id;
    row.textContent = id;
    if (id === group.main_session) row.selected = true;
    select.appendChild(row);
  });
  select.addEventListener("change", () => {
    group.main_session = select.value;
    markDirty();
  });
  mainBox.appendChild(select);
  card.appendChild(mainBox);
  card.appendChild(privacyNoticeLink());
  return card;
}

/* ---------------- 会话组隐私须知 ---------------- */

/** 会话组隐私须知的正文（创建前必须先读，编辑块里的链接也是这一段）。 */
function privacyNoticeBody() {
  const box = el("div", "privacy-notice");
  box.appendChild(el("p", "", "请不要对这个 bot 说你的真实姓名、住址、电话、工作单位、证件号、账号密码，也不要发能定位到你的照片。"));
  box.appendChild(el("p", "", "一个会话组里可能有多个会话（不同的群、不同的私聊）共用同一个「她」和同一册记忆。"));
  box.appendChild(el("p", "", "别的会话里的人可以用套话、假装是你、或者换个名字来问，她记的是内容，不是「谁有权知道」——同一会话组内，谁来问都可能问出来。"));
  box.appendChild(el("p", "", "真要说私事，请在会话组设置里把她限制在单个会话可见，或者干脆别对她说。"));
  return box;
}

/**
 * 创建会话组前的隐私须知。
 *
 * 必须**主动勾选**再点「继续创建会话组」才 resolve(true)；Esc、点右上角 ✕、
 * 点遮罩、点「取消」都按 false 处理（onDismiss），调用方据此不创建。
 */
function confirmGroupPrivacyNotice() {
  return new Promise((resolve) => {
    let settled = false;
    let stopGuard = null;
    const settle = (answer) => {
      if (settled) return;
      settled = true;
      if (stopGuard) stopGuard();
      resolve(answer);
    };
    openCustomDialog({
      title: "创建会话组前，请先读这一段",
      build: (body) => {
        body.appendChild(privacyNoticeBody());
        const row = el("label", "picker-row privacy-ack");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.id = "privacy-ack";
        const ok = $("dialog-ok");
        ok.disabled = true;
        const sync = () => {
          ok.disabled = !box.checked;
        };
        box.addEventListener("change", sync);
        row.appendChild(box);
        row.appendChild(el("span", "", "我已阅读并理解上述风险"));
        body.appendChild(row);
        // Esc 在 #dialog 上被 closeDialog 吞掉，这里自己接管（取消 = 不创建）
        const onKey = (event) => {
          if (event.key === "Escape") settle(false);
        };
        document.addEventListener("keydown", onKey);
        stopGuard = () => document.removeEventListener("keydown", onKey);
      },
      confirmText: "继续创建会话组",
      onSubmit: () => {
        settle(true);
        return true;
      },
      onDismiss: () => settle(false),
    });
  });
}

/** 编辑块里的「隐私须知」链接：同一个内容，随时能回看。 */
function openPrivacyNoticeDialog() {
  openCustomDialog({
    title: "会话组隐私须知",
    build: (body) => body.appendChild(privacyNoticeBody()),
    confirmText: "知道了",
    hideCancel: true,
    onDismiss: () => {
      // 弹窗把编辑块重建过了（内容在 #dialog 里），关掉之后把列表画回来
      ui.selectedGroup = "";
      renderGroupList();
    },
  });
}

function privacyNoticeLink() {
  const link = el("button", "small ghost privacy-link", "隐私须知");
  link.type = "button";
  link.addEventListener("click", () => openPrivacyNoticeDialog());
  return link;
}

async function addGroup() {
  // 先过隐私须知：没点「继续创建会话组」就当没点过这个按钮，什么也不建
  if (!(await confirmGroupPrivacyNotice())) return;
  const id = `group_${Date.now().toString(36).slice(-4)}`;
  groups().push({ id, name: "新的会话组", sessions: [], main_session: "" });
  markDirty();
  ui.selectedGroup = id;
  renderGroupList();
}

function addSession() {
  const value = $("session-input").value.trim();
  if (!value) {
    toast("请填写会话 ID");
    return;
  }
  if (ui.sessions.some((item) => item.session_id === value)) {
    toast("这个会话已经在白名单里");
    return;
  }
  ui.sessions.push({
    session_id: value,
    type: $("session-type").value,
    platform: value.split(":")[0],
    enabled: true,
    cold_start_mode: "awakening",
    cold_start_node: nodes()[0]?.id || "bedroom",
    added_at: Math.floor(Date.now() / 1000),
    note: "",
  });
  ui.config.sessions.sessions = ui.sessions;
  $("session-input").value = "";
  renderSessionList();
  renderSessionSelects();
}

/* ================================================================== */
/* 记忆库                                                              */
/* ================================================================== */

function renderMemoryFilters() {
  const nodeSelect = $("memory-node");
  nodeSelect.innerHTML = "";
  nodeSelect.appendChild(option("", "全部"));
  nodes().forEach((node) => nodeSelect.appendChild(option(node.id, node.name || node.id)));
}

/* ---------------- 日志页：她的每一次决定与动作 ---------------- */

function fillLogTypes(types) {
  const select = $("log-type");
  const current = select.value;
  const existing = Array.from(select.options).map((item) => item.value);
  // 扩展注册过、但今天还没写过日志的类型也要列出来（不然筛选里看不到）
  const merged = [...new Set([...(types || []), ...EXT_DEBUG_TYPES])];
  const wanted = ["", ...merged];
  if (
    existing.length === wanted.length &&
    existing.every((value, index) => value === wanted[index])
  ) {
    return;
  }
  select.innerHTML = "";
  select.appendChild(option("", "全部"));
  merged.forEach((type) => {
    const meta = LOG_TYPES[type];
    select.appendChild(option(type, meta ? `${meta.icon} ${meta.label}` : type));
  });
  select.value = wanted.includes(current) ? current : "";
}

function formatClock(timestamp) {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return "";
  const date = new Date(value * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}:${pad(date.getSeconds())}`;
}

/** 时段：和提示词里给她的说法保持一致（凌晨 / 清晨 / 上午 / 中午 / 下午 / 傍晚 / 晚上 / 深夜）。 */
function periodOf(date) {
  const hour = date.getHours();
  if (hour < 5) return "凌晨";
  if (hour < 8) return "清晨";
  if (hour < 11) return "上午";
  if (hour < 13) return "中午";
  if (hour < 17) return "下午";
  if (hour < 19) return "傍晚";
  if (hour < 23) return "晚上";
  return "深夜";
}

/** 记忆的时间标签：月-日 + 时段（她看到的就是这个）。 */
function memoryStamp(timestamp) {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return "";
  const date = new Date(value * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${periodOf(date)}`;
}

/* ---------------- 列表的批量操作（记忆库 / 日志共用） ---------------- */

/** 列表行前面的勾选框，选中状态记在 ui[selectionKey] 里。 */
function rowCheckbox(row, id, selectionKey) {
  if (!ui[selectionKey]) ui[selectionKey] = new Set();
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "row-check";
  box.dataset.id = String(id);
  box.checked = ui[selectionKey].has(Number(id));
  // 勾选不该顺带触发"查看详情"
  box.addEventListener("click", (event) => event.stopPropagation());
  box.addEventListener("change", () => {
    if (box.checked) ui[selectionKey].add(Number(id));
    else ui[selectionKey].delete(Number(id));
    row.classList.toggle("checked", box.checked);
  });
  return box;
}

function toggleSelectAll(listId, selectionKey) {
  const boxes = Array.from($(listId).querySelectorAll("input.row-check"));
  if (!boxes.length) return;
  const allOn = boxes.every((box) => box.checked);
  ui[selectionKey] = new Set();
  boxes.forEach((box) => {
    box.checked = !allOn;
    const row = box.closest(".log-item") || box.closest(".list-item");
    if (row) row.classList.toggle("checked", !allOn);
    if (!allOn) ui[selectionKey].add(Number(box.dataset.id));
  });
}

async function deleteSelected(kind) {
  const isMemory = kind === "memory";
  const ids = Array.from(ui[isMemory ? "memorySelection" : "logSelection"] || []);
  if (!ids.length) {
    toast("先勾选要删除的条目");
    return;
  }
  const ok = await confirmDialog({
    title: `删除选中的 ${ids.length} 条${isMemory ? "记忆" : "日志"}？`,
    message: "删除后无法恢复。",
    confirmText: "删除",
  });
  if (!ok) return;
  const result = await apiPost(
    isMemory ? "memories/delete-batch" : "logs/delete-batch",
    { ids },
  );
  toast(`已删除 ${result.removed ?? ids.length} 条`);
  if (isMemory) loadMemories();
  else loadLogs();
}

async function clearAll(kind) {
  const isMemory = kind === "memory";
  const session = $(isMemory ? "memory-session" : "log-session").value;
  const label = isMemory ? "记忆" : "日志";
  const ok = await confirmDialog({
    title: `清空这个会话的全部${label}？`,
    message: session
      ? `只清空「${session}」的${label}，其它会话不受影响。删除后无法恢复。`
      : `清空全部${label}。删除后无法恢复。`,
    confirmText: "清空",
  });
  if (!ok) return;
  const result = await apiPost(isMemory ? "memories/clear" : "logs/clear", {
    session: session || "",
  });
  toast(`已清空 ${result.removed ?? 0} 条${label}`);
  if (isMemory) loadMemories();
  else loadLogs();
}

async function loadLogs() {
  const session = $("log-session").value;
  const list = $("log-list");
  if (!session) {
    list.textContent = "还没有白名单会话。";
    return;
  }
  ui.logSelection = new Set();
  try {
    const data = await apiGet("logs", {
      session,
      type: $("log-type").value,
      q: $("log-keyword").value.trim(),
      limit: $("log-limit").value,
    });
    fillLogTypes(data.types || []);
    list.innerHTML = "";
    (data.events || []).forEach((event) => {
      const meta = eventMeta(event);
      const item = el("div", "log-item");
      item.dataset.type = event.event_type;
      const time = el("div", "log-time");
      time.appendChild(el("div", "", `t=${event.world_time}`));
      time.appendChild(el("div", "", formatClock(event.created_at)));
      item.appendChild(time);
      item.appendChild(el("div", "log-icon", meta.icon));
      item.appendChild(el("div", "log-text", event.text || meta.label));
      item.addEventListener("click", () => showLogDetail(item, event));
      item.classList.add("selectable");
      item.insertBefore(rowCheckbox(item, event.id, "logSelection"), item.firstChild);
      list.appendChild(item);
    });
    if (!(data.events || []).length) {
      list.appendChild(el("p", "muted", "没有符合条件的记录。"));
    }
  } catch (error) {
    list.innerHTML = "";
    list.appendChild(el("p", "muted", error.message || "读取日志失败"));
  }
}

function showLogDetail(item, event) {
  document
    .querySelectorAll("#log-list .log-item")
    .forEach((node) => node.classList.remove("selected"));
  item.classList.add("selected");
  const box = $("log-detail");
  if (!box) return;
  const meta = eventMeta(event);
  const raw = {
    id: event.id,
    world_time: event.world_time,
    time: formatClock(event.created_at),
    event_type: event.event_type,
    explanation: event.text,
    detail: event.detail,
  };

  box.innerHTML = "";
  // 1) 先说人话
  const head = el("div", "detail-head");
  head.appendChild(el("span", "log-badge", `${meta.icon} ${meta.label}`));
  head.appendChild(el("span", "muted", `t=${event.world_time} · ${formatClock(event.created_at)}`));
  head.appendChild(el("span", "muted", `#${event.id}`));
  box.appendChild(head);
  box.appendChild(el("p", "detail-text", event.text || meta.label));

  // 2) 结构化字段：把 detail 里能读的都摊开
  const detail = event.detail && typeof event.detail === "object" ? event.detail : {};
  const pairs = Object.entries(detail).filter(
    ([, value]) => value !== null && value !== undefined && value !== "" && String(value) !== "{}",
  );
  if (pairs.length) {
    const list = el("dl", "detail-facts");
    pairs.forEach(([key, value]) => {
      list.appendChild(el("dt", "", key));
      list.appendChild(
        el(
          "dd",
          "",
          typeof value === "object" ? JSON.stringify(value, null, 0) : String(value),
        ),
      );
    });
    box.appendChild(list);
  }

  // 3) 原始数据折叠在最后，一个字段都不少
  const rawBox = el("details", "detail-raw");
  rawBox.appendChild(el("summary", "", "原始数据（JSON）"));
  rawBox.appendChild(el("pre", "pre", JSON.stringify(raw, null, 2)));
  box.appendChild(rawBox);
}

/* ================================================================== */
/* 通讯录：她认识的人（画像 / 关系 / 好感度）                            */
/* ================================================================== */

function contactBadge(text, className = "pill") {
  return el("span", className, text);
}

function formatStamp(seconds) {
  if (!seconds) return "还没说过话";
  const date = new Date(Number(seconds) * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function loadContacts() {
  const list = $("contacts-list");
  if (!list) return;
  const session = $("contacts-session") ? $("contacts-session").value : "";
  list.innerHTML = "<p class='muted'>加载中…</p>";
  try {
    const data = await apiGet("profile/people", { session });
    ui.contacts = data.people || [];
    if ($("contacts-note")) {
      const bits = [];
      bits.push(data.enabled ? "画像：开" : "画像：关");
      bits.push(
        data.consolidate_enabled ? "睡眠整理：开" : "睡眠整理：关",
      );
      bits.push(`提示词里最多带 ${data.digest_limit || 5} 个其他人`);
      $("contacts-note").textContent = bits.join(" · ");
    }
    if (ui.contactUser && !ui.contacts.some((item) => item.user_id === ui.contactUser)) {
      ui.contactUser = "";
    }
    if (!ui.contactUser && ui.contacts.length) {
      ui.contactUser = ui.contacts[0].user_id;
    }
    renderContactsList();
    renderContactPlaces();
    if (ui.contactUser) {
      await loadContactDetail(ui.contactUser);
    } else {
      $("contacts-title").textContent = "还没有认识的人";
      $("contacts-detail").innerHTML =
        "<p class='muted'>等她在群里见过人之后，这里就有画像了。</p>";
    }
  } catch (error) {
    list.innerHTML = `<p class='muted'>读取失败：${error.message || error}</p>`;
  }
}

/** 通讯录里那张小头像：先出头字，取到 QQ 头像再盖上去（用户 m13421）。 */
function contactAvatar(item) {
  const box = el("span", "contact-avatar-box");
  const label = String(item.name || item.user_id || "?").trim();
  box.appendChild(el("span", "contact-avatar-initial", label ? Array.from(label)[0] : "?"));
  const img = el("img", "contact-avatar");
  img.alt = "";
  img.dataset.qq = String(item.user_id || "");
  img.addEventListener("load", () => img.classList.add("loaded"));
  box.appendChild(img);
  return box;
}

/** 名单画完之后再补头像：网络慢不拖住列表，取不到就留着头字。 */
async function fillAvatars(root = document) {
  const images = Array.from(root.querySelectorAll("img[data-qq]")).filter(
    (img) => !img.dataset.asked && img.dataset.qq,
  );
  if (!images.length) return;
  const qqs = Array.from(new Set(images.map((img) => img.dataset.qq)));
  images.forEach((img) => {
    img.dataset.asked = "1";
  });
  try {
    const data = await apiPost("avatars", { qqs });
    const map = data.avatars || {};
    images.forEach((img) => {
      const url = map[img.dataset.qq];
      if (url) img.src = url;
    });
  } catch (error) {
    /* 头像只是装饰：取不到就留着头字，不打扰用户 */
  }
}

/** 一个会话在人话里叫什么：有备注用备注，没有就按「群/私聊 + 号码」。 */
function sessionPlaceLabel(sessionId) {
  const raw = String(sessionId || "");
  const found = (ui.sessions || []).find(
    (item) => String((item && item.session_id) || "") === raw,
  );
  const note = found ? String(found.note || "").trim() : "";
  if (note) return note;
  const parts = raw.split(":");
  const kind = (parts[1] || "").toLowerCase();
  const isPrivate = kind.includes("friend") || kind.includes("private");
  return `${isPrivate ? "私聊" : "群"} ${parts.slice(2).join(":")}`;
}

/** 同一个人在几个地方各叫什么（用户 m14014）。 */
function contactPlaces(cards) {
  return Object.entries(cards || {})
    .map(([sessionId, card]) => ({ where: sessionPlaceLabel(sessionId), card: String(card || "") }))
    .filter((item) => item.card);
}

function renderContactsList() {
  const box = $("contacts-list");
  const keyword = ($("contacts-search") ? $("contacts-search").value : "").trim();
  box.innerHTML = "";
  const rows = ui.contacts.filter((item) => {
    if (!keyword) return true;
    return (
      String(item.name || "").includes(keyword) ||
      String(item.user_id || "").includes(keyword) ||
      String(item.digest || "").includes(keyword) ||
      String(item.qq_name || "").includes(keyword)
    );
  });
  if (!rows.length) {
    box.appendChild(el("p", "muted", keyword ? "没有匹配的人。" : "还没有认识的人。"));
    return;
  }
  rows.forEach((item) => {
    const row = el("div", "contact-row");
    row.classList.toggle("active", item.user_id === ui.contactUser);
    row.dataset.user = item.user_id;
    const head = el("div", "contact-row-head");
    head.appendChild(contactAvatar(item));
    head.appendChild(el("strong", "", item.name || item.user_id));
    (item.bonds || []).forEach((bond) => {
      head.appendChild(contactBadge(bond, "pill pill-bond"));
    });
    (item.claims || []).forEach((bond) => {
      head.appendChild(contactBadge(`${bond}？`, "pill pill-claim"));
    });
    row.appendChild(head);
    const meta = el(
      "div",
      "contact-row-meta",
      `${item.level || "陌生人"} · 好感 ${Math.round(Number(item.affinity) || 0)} · 聊过 ${item.message_count || 0} 句 · ${formatStamp(item.last_seen_at)}`,
    );
    row.appendChild(meta);
    const places = contactPlaces(item.cards);
    if (places.length > 1) {
      row.appendChild(
        el(
          "div",
          "contact-row-cards",
          "在 " +
            places
              .map((place) => `${place.where}叫「${place.card}」`)
              .join(" · "),
        ),
      );
    }
    if (item.digest) {
      row.appendChild(el("div", "contact-row-digest", item.digest));
    }
    box.appendChild(row);
  });
  fillAvatars(box);
}

function renderContactPlaces() {
  const box = $("contacts-places");
  if (!box) return;
  box.innerHTML = "";
  const options = scopeOptions({ withMembers: true });
  if (!options.length) {
    box.appendChild(el("p", "muted", "还没有白名单会话。"));
    return;
  }
  options.forEach((item) => {
    box.appendChild(el("div", "contact-place", item.label));
  });
}

/** 好感日志里的来源标签：一眼看出这次是谁改的。 */
const AFFINITY_SOURCE_LABELS = {
  chat: "她自己判断",
  presence: "混脸熟",
  decay: "慢慢回落",
  consolidate: "睡眠整理",
  remember: "当场记住",
  manual: "手动改的",
};

async function loadContactDetail(userId) {
  const session = $("contacts-session") ? $("contacts-session").value : "";
  const box = $("contacts-detail");
  box.innerHTML = "<p class='muted'>加载中…</p>";
  try {
    const data = await apiGet("profile/person", { session, user_id: userId });
    ui.contactDetail = data;
    renderContactDetail(data);
  } catch (error) {
    box.innerHTML = `<p class='muted'>读取失败：${error.message || error}</p>`;
  }
}

function renderContactDetail(data) {
  const box = $("contacts-detail");
  const person = data.person || {};
  ui.contactUser = data.user_id;
  $("contacts-title").textContent = `${person.name || data.user_id}（QQ ${data.user_id}）`;
  box.innerHTML = "";
  renderContactsList();

  // 零、她对他已知的概况：认识多久、聊过多少、群名片、关系上限
  const metaCard = el("div", "contact-section");
  const metaBits = [];
  if (person.days_known) metaBits.push(`认识 ${person.days_known} 天`);
  if (person.message_count) metaBits.push(`聊过 ${person.message_count} 句`);
  if (person.qq_name) metaBits.push(`昵称「${person.qq_name}」`);
  const cards = Object.entries(person.cards || {});
  cards.forEach(([sessionId, card]) => {
    if (card) metaBits.push(`在「${sessionPlaceLabel(sessionId)}」叫「${card}」`);
  });
  const caps = (data.bonds_config || [])
    .filter((item) => (person.affinities || []).includes(item.name))
    .map((item) => Number(item.cap) || 0);
  const capIndex = caps.length ? Math.max(...caps) : null;
  const levels = (data.levels_config || [])
    .map((item) => item.name)
    .filter(Boolean);
  if (capIndex !== null && levels.length) {
    const capName = levels[Math.max(0, Math.min(capIndex, levels.length - 1))] || "";
    metaBits.push(`关系上限：${capName}级`);
  }
  metaCard.appendChild(
    el("h3", "", `概况${person.digest ? `｜${person.digest}` : ""}`),
  );
  metaCard.appendChild(
    el("p", "hint-line", metaBits.join("；") || "才刚认识，还没聊过几句。"),
  );
  metaCard.appendChild(
    el(
      "p",
      "hint-line",
      "好感度：说话时每轮由她自己判断（有每轮与每天上限），他露个面也会长一点（混脸熟），" +
        "长期不理则每天慢慢回落。",
    ),
  );
  box.appendChild(metaCard);

  // 一、关系：这一页只放摘要（一长条关系表会把整页顶下去），要改点「管理关系…」进弹窗
  const bondCard = el("div", "contact-section");
  const bondHead = el("div", "section-head");
  bondHead.appendChild(el("h3", "", "关系"));
  const bondEdit = el("button", "ghost tiny", "管理关系…");
  bondEdit.type = "button";
  bondEdit.addEventListener("click", openBondDialog);
  bondHead.appendChild(bondEdit);
  bondCard.appendChild(bondHead);
  const bonds = data.bonds || [];
  const bondSummary = el("div", "bond-summary");
  bonds
    .filter((item) => item.status === "current")
    .forEach((item) => bondSummary.appendChild(el("span", "pill pill-bond", item.type)));
  bonds
    .filter((item) => item.status === "claimed")
    .forEach((item) =>
      bondSummary.appendChild(el("span", "pill pill-claim", `${item.type}（他自称）`)),
    );
  const pastCount = bonds.filter((item) => item.status === "past").length;
  if (pastCount) bondSummary.appendChild(el("span", "pill pill-past", `曾经 ${pastCount} 段`));
  if (!bonds.length) bondSummary.appendChild(el("span", "muted", "还没定过关系。"));
  bondCard.appendChild(bondSummary);
  box.appendChild(bondCard);

  // 二、好感度：这一页只留一条概要（数值 + 当前档位），滑杆和分级说明点「调整…」进弹窗
  const affinityCard = el("div", "contact-section");
  const affinityHead = el("div", "section-head");
  affinityHead.appendChild(el("h3", "", "好感度"));
  const affinityEdit = el("button", "ghost tiny", "调整…");
  affinityEdit.type = "button";
  affinityEdit.addEventListener("click", openAffinityDialog);
  affinityHead.appendChild(affinityEdit);
  affinityCard.appendChild(affinityHead);
  const affinityValue = Math.round(Number(person.affinity) || 0);
  const affinityBar = el("div", "affinity-bar");
  const affinityFill = el("i");
  // -100~100 映射到 0~100%：中点 50% 就是"不好不坏"
  affinityFill.style.width = `${Math.max(0, Math.min(100, (affinityValue + 100) / 2))}%`;
  affinityBar.appendChild(affinityFill);
  const affinitySummary = el("div", "affinity-summary");
  affinitySummary.appendChild(affinityBar);
  affinitySummary.appendChild(el("span", "affinity-value", `${affinityValue} / 100`));
  affinitySummary.appendChild(
    el("span", "pill", person.level ? person.level.name : "陌生人"),
  );
  affinityCard.appendChild(affinitySummary);
  // 她想不想这个人：这是"正在发生的状态"，不是设置，所以留在页面上
  const missInfo = data.miss || {};
  const missValue = Number(missInfo.value || 0);
  const missThreshold = Number(data.miss_threshold || 0.7);
  affinityCard.appendChild(
    el(
      "p",
      "hint-line",
      missInfo.waiting
        ? `想念：0.00 —— 刚聊过（或者她刚主动找过他），`
          + `再过 ${Number(missInfo.ready_in_minutes || 0)} 分钟才开始攒`
          + "（这是设计如此：每次直接跟她说话都会清零重来）"
        : `想念：${missValue.toFixed(2)} / ${missThreshold.toFixed(2)}`
          + (missValue >= missThreshold
            ? "——到点了，她会主动去找这个人"
            : "（越接近阈值越想找人；孤独感越高涨得越快）"),
    ),
  );
  box.appendChild(affinityCard);

  // 二点二、信赖值（T15-S3）：-1 ~ +1，0 是"还没判断"。**只读**——
  // 它只靠事件与夜间整理里的小额评估变化，没有手动加减的入口（用户拍板）。
  const trustValue = Math.max(-1, Math.min(1, Number(person.trust) || 0));
  const trustLabel = (value) => {
    if (value >= 0.6) return "基本不用怀疑";
    if (value >= 0.3) return "信得过";
    if (value >= 0.1) return "多半会信";
    if (value > -0.1) return "还没判断";
    if (value > -0.3) return "多留个心眼";
    if (value > -0.6) return "不太信";
    return "基本不信";
  };
  const trustCard = el("div", "contact-section");
  const trustHead = el("div", "section-head");
  trustHead.appendChild(el("h3", "", "信赖值"));
  trustHead.appendChild(el("span", "pill", trustLabel(trustValue)));
  trustCard.appendChild(trustHead);
  const trustBar = el("div", "affinity-bar");
  const trustFill = el("i");
  // -1~+1 映射到 0~100%：中点 50% 就是"还没判断"
  trustFill.style.width = `${Math.max(0, Math.min(100, (trustValue + 1) * 50))}%`;
  if (trustValue < 0) trustFill.classList.add("negative");
  trustBar.appendChild(trustFill);
  const trustSummary = el("div", "affinity-summary");
  trustSummary.appendChild(trustBar);
  trustSummary.appendChild(
    el(
      "span",
      "affinity-value",
      `${trustValue > 0 ? "+" : ""}${trustValue.toFixed(1)} / ±1.0`,
    ),
  );
  trustCard.appendChild(trustSummary);
  const trustNotes = Array.isArray(person.trust_notes) ? person.trust_notes.slice(0, 3) : [];
  if (trustNotes.length) {
    const trustList = el("div", "list");
    trustList.appendChild(el("p", "hint-line", "最近几次变化："));
    trustNotes.forEach((item) => {
      const delta = Number(item.delta) || 0;
      trustList.appendChild(
        el(
          "div",
          "contact-line muted",
          `${formatStamp(item.at)} ${delta >= 0 ? "+" : ""}${delta.toFixed(2)}`
            + `（${item.reason || "没写原因"}）`,
        ),
      );
    });
    trustCard.appendChild(trustList);
  }
  trustCard.appendChild(
    el(
      "p",
      "hint-line",
      "这一项只靠日常相处和事件慢慢变：兑现过的承诺、给过的建议、帮过的忙才算，"
        + "而且要有原话对得上。30 天没互动，负信赖会慢慢淡回中间。"
        + "它只是她心里的印象，不是「听谁的」的概率。",
    ),
  );
  box.appendChild(trustCard);

  // 二点五、她记着的账：记了就会对他冷一档，所以这里要写清现在算不算数
  const grudge = data.grudge;
  const grudgeCard = el("div", "contact-section");
  grudgeCard.appendChild(el("h3", "", "她记着的账"));
  if (grudge) {
    const days = Number(grudge.days || 0);
    grudgeCard.appendChild(
      el(
        "p",
        "hint-line",
        days < 1
          ? `${grudge.reason}（今天记的）`
          : `${grudge.reason}（${formatStamp(grudge.at)} 记的，已经 ${Math.ceil(days)} 天）`,
      ),
    );
  } else {
    grudgeCard.appendChild(el("p", "hint-line", "没记：他没做过让她记着的事。"));
  }
  grudgeCard.appendChild(
    el(
      "p",
      "hint-line",
      grudge
        ? "她现在对他冷一档：只在跟他说话时提这件事，别人面前不说。"
        : "记了账就会让他这一档冷下来（只在跟他说话时）。",
    ),
  );
  const grudgeRow = el("div", "row");
  const grudgeInput = el("input");
  grudgeInput.placeholder = "手动记一笔：他答应的事又没做";
  grudgeInput.dataset.act = "grudge-text";
  const grudgeAdd = el("button", "small", "记一笔");
  grudgeAdd.dataset.act = "grudge-add";
  grudgeRow.appendChild(grudgeInput);
  grudgeRow.appendChild(grudgeAdd);
  if (grudge) {
    const grudgeResolve = el("button", "small primary", "算了");
    grudgeResolve.dataset.act = "grudge-resolve";
    const grudgeDelete = el("button", "small danger", "删掉");
    grudgeDelete.dataset.act = "grudge-delete";
    grudgeRow.appendChild(grudgeResolve);
    grudgeRow.appendChild(grudgeDelete);
  }
  grudgeCard.appendChild(grudgeRow);
  box.appendChild(grudgeCard);

  // 三、称呼与备注
  const fieldCard = el("div", "contact-section");
  fieldCard.appendChild(el("h3", "", "称呼与备注"));
  const callMe = el("input");
  callMe.placeholder = "他让你怎么称呼自己";
  callMe.value = person.call_me || "";
  callMe.dataset.act = "field-call-me";
  const callHim = el("input");
  callHim.placeholder = "你怎么称呼他";
  callHim.value = person.call_him || "";
  callHim.dataset.act = "field-call-him";
  const note = el("input");
  note.placeholder = "备注（只给你看，不进模型）";
  note.value = person.note || "";
  note.dataset.act = "field-note";
  const digest = el("input");
  digest.placeholder = "缩略版画像（给别人看的那一行）";
  digest.value = person.digest || "";
  digest.dataset.act = "field-digest";
  [callMe, callHim, note, digest].forEach((node) => fieldCard.appendChild(node));
  const saveFields = el("button", "primary tiny", "保存这些字段");
  saveFields.dataset.act = "fields-save";
  fieldCard.appendChild(saveFields);
  box.appendChild(fieldCard);

  // 四、事实列表
  const factCard = el("div", "contact-section");
  factCard.appendChild(el("h3", "", "关于他（事实）"));
  const factRows = el("div", "list");
  // 攒多了会很长：置顶的永远在，其余默认只显示最近几条，想看全部再展开
  const allFacts = data.facts || [];
  const pinnedFacts = allFacts.filter((item) => item.pinned);
  const restFacts = allFacts.filter((item) => !item.pinned);
  const FACT_PREVIEW = 8;
  const visibleFacts = [...pinnedFacts, ...restFacts.slice(0, FACT_PREVIEW)];
  const hiddenFacts = restFacts.slice(FACT_PREVIEW);
  const factRow = (item, target = factRows) => {
    const row = el("div", "contact-line");
    const kind = el("span", "pill", item.kind || "其他");
    row.appendChild(kind);
    const text = el("span", "", item.text || "");
    if (item.pinned) text.classList.add("pinned");
    row.appendChild(text);
    if (item.status && item.status !== "active") {
      row.appendChild(el("span", "muted", `（${item.status === "past" ? "已过期" : "待确认"}）`));
    }
    if (item.evidence) {
      row.appendChild(el("span", "muted", `｜原话：${item.evidence}`));
    }
    if (item.last_confirmed_at) {
      row.appendChild(
        el("span", "muted", `｜${formatStamp(item.last_confirmed_at)}`),
      );
    }
    const pin = el("button", "ghost tiny", item.pinned ? "取消置顶" : "置顶");
    pin.dataset.act = "fact-pin";
    pin.dataset.id = item.id;
    pin.dataset.pinned = item.pinned ? "0" : "1";
    row.appendChild(pin);
    const drop = el("button", "ghost tiny", "删");
    drop.dataset.act = "fact-delete";
    drop.dataset.id = item.id;
    row.appendChild(drop);
    target.appendChild(row);
  };
  // 注意：不能写成 forEach(factRow)——forEach 会把「下标」当第二个参数传进去，
  // target 就成了数字，appendChild 直接报错（画像整块读不出来）。
  visibleFacts.forEach((item) => factRow(item));
  if (hiddenFacts.length) {
    const more = el("details", "contact-more");
    more.appendChild(el("summary", "", `还有 ${hiddenFacts.length} 条（展开）`));
    const moreBox = el("div", "list");
    hiddenFacts.forEach((item) => factRow(item, moreBox));
    more.appendChild(moreBox);
    factRows.appendChild(more);
  }
  if (!(data.facts || []).length) {
    factRows.appendChild(el("p", "muted", "还没记下关于他的事。"));
  }
  factCard.appendChild(factRows);
  const factAdd = el("div", "contact-inline");
  const kindSelect = el("select");
  ["基本信息", "喜好", "厌恶", "习惯", "关系", "约定", "近况", "other"].forEach((kind) =>
    kindSelect.appendChild(option(kind, kind)),
  );
  const factInput = el("input");
  factInput.placeholder = "例如：喜欢猫 / 生日是 10 月 3 日";
  factInput.dataset.act = "fact-input";
  const factButton = el("button", "primary tiny", "记下");
  factButton.dataset.act = "fact-add";
  factAdd.appendChild(kindSelect);
  factAdd.appendChild(factInput);
  factAdd.appendChild(factButton);
  factCard.appendChild(factAdd);
  const forget = el("button", "danger tiny", "忘掉这个人");
  forget.dataset.act = "person-forget";
  factCard.appendChild(forget);
  box.appendChild(factCard);
}

/** 预览整理的结果：提示词、模型原话、解析出来的 JSON 都摆出来。 */
/**
 * 关系编辑弹窗：当前 / 他自称 / 曾经 + 加关系。
 * 每一行改完就地重画（数据已经由 loadContacts 重新取过），不用关掉再开。
 */
function openBondDialog() {
  openCustomDialog({
    title: "关系",
    hint: "「她认定」是她自己的判断；「他自称」只是他说过、还没被认下。标了唯一的类型，同一个人身上只会有一条。",
    confirmText: "关闭",
    hideCancel: true,
    onSubmit: () => true,
    build: (body) => {
      const paint = () => {
        body.innerHTML = "";
        const detail = ui.contactDetail || {};
        const bonds = detail.bonds || [];
        const groups = [
          { key: "current", title: "她认定的", hint: "" },
          { key: "claimed", title: "他自称的", hint: "认不认由她决定" },
          { key: "past", title: "曾经的", hint: "留着当记录，也可以删掉" },
        ];
        groups.forEach((group) => {
          const rows = bonds.filter((item) => item.status === group.key);
          if (!rows.length) return;
          const block = el("div", "bond-group");
          const head = el("div", "bond-group-title");
          head.appendChild(el("span", "", group.title));
          head.appendChild(el("span", "muted", `${rows.length}${group.hint ? ` · ${group.hint}` : ""}`));
          block.appendChild(head);
          rows.forEach((item) => {
            const row = el("div", "bond-row");
            const pillClass =
              group.key === "current"
                ? "pill pill-bond"
                : group.key === "claimed"
                  ? "pill pill-claim"
                  : "pill pill-past";
            const text =
              group.key === "past"
                ? `${item.type}（${item.since_text || "？"} → ${item.until_text || "？"}）`
                : group.key === "claimed"
                  ? `${item.type}（他自称${item.since_text ? `，${item.since_text}` : ""}）`
                  : item.since_text
                    ? `${item.type}（${item.since_text} 起）`
                    : item.type;
            row.appendChild(el("span", pillClass, text));
            const box = el("div", "row-item");
            if (group.key === "current") {
              const close = el("button", "small ghost", "解除");
              close.dataset.act = "bond-close";
              close.dataset.id = item.id;
              box.appendChild(close);
            } else if (group.key === "claimed") {
              const accept = el("button", "small primary", "认下");
              accept.dataset.act = "bond-accept";
              accept.dataset.id = item.id;
              box.appendChild(accept);
              const drop = el("button", "small danger", "删掉");
              drop.dataset.act = "bond-delete";
              drop.dataset.id = item.id;
              box.appendChild(drop);
            } else {
              const drop = el("button", "small danger", "删掉");
              drop.dataset.act = "bond-delete";
              drop.dataset.id = item.id;
              box.appendChild(drop);
            }
            box.querySelectorAll("[data-act]").forEach((button) => {
              button.addEventListener("click", async () => {
                await contactAction(button);
                paint();
              });
            });
            row.appendChild(box);
            block.appendChild(row);
          });
          body.appendChild(block);
        });
        if (!bonds.length) body.appendChild(el("p", "muted", "还没定过关系。"));

        const add = el("div", "bond-add");
        const bondSelect = el("select");
        (detail.bonds_config || []).forEach((item) =>
          bondSelect.appendChild(option(item.name, item.unique ? `${item.name}（唯一）` : item.name)),
        );
        const assertSelect = el("select");
        assertSelect.appendChild(option("她的判断", "她认定"));
        assertSelect.appendChild(option("他自称", "他自称"));
        const addButton = el("button", "primary small", "加关系");
        addButton.dataset.act = "bond-add";
        add.appendChild(bondSelect);
        add.appendChild(assertSelect);
        add.appendChild(addButton);
        addButton.addEventListener("click", async () => {
          await contactAction(addButton);
          paint();
        });
        body.appendChild(add);
      };
      paint();
    },
  });
}

/** 好感度弹窗：滑杆 + 当前档位说明 + 最近 8 次变化。 */
function openAffinityDialog() {
  openCustomDialog({
    title: "好感度",
    hint: "0 是陌生人，越高越亲近；说话时每轮由她自己判断（有每轮与每天上限），他露个面也会长一点，长期不理每天慢慢回落。",
    confirmText: "关闭",
    hideCancel: true,
    onSubmit: () => true,
    build: (body) => {
      const paint = () => {
        body.innerHTML = "";
        const detail = ui.contactDetail || {};
        const person = detail.person || {};
        const row = el("div", "contact-inline");
        const slider = el("input");
        slider.type = "range";
        slider.min = "-100";
        slider.max = "100";
        slider.value = String(Math.round(Number(person.affinity) || 0));
        slider.dataset.act = "affinity-slider";
        const valueLabel = el("span", "muted", `${slider.value} / 100`);
        slider.addEventListener("input", () => {
          valueLabel.textContent = `${slider.value} / 100`;
        });
        const save = el("button", "primary small", "保存好感度");
        save.dataset.act = "affinity-save";
        row.appendChild(slider);
        row.appendChild(valueLabel);
        row.appendChild(save);
        save.addEventListener("click", async () => {
          await contactAction(save);
          paint();
        });
        body.appendChild(row);

        body.appendChild(
          el(
            "p",
            "hint-line",
            `现在这一级：${person.level ? person.level.name : "陌生人"}` +
              (person.level && person.level.prompt ? `——${person.level.prompt}` : ""),
          ),
        );
        if (person.level && (person.level.deny || []).length) {
          body.appendChild(el("p", "hint-line", `还不能：${(person.level.deny || []).join("、")}`));
        }

        const logs = detail.affinity_logs || [];
        if (logs.length) {
          const list = el("div", "list");
          list.appendChild(el("p", "hint-line", "最近 8 次变化："));
          logs.slice(0, 8).forEach((item) => {
            const sourceText = AFFINITY_SOURCE_LABELS[String(item.source || "")] || "";
            list.appendChild(
              el(
                "div",
                "contact-line muted",
                `${formatStamp(item.at)} ${Number(item.delta) >= 0 ? "+" : ""}${Math.round(Number(item.delta) * 10) / 10}` +
                  `（${item.reason || "没写原因"}${sourceText ? `｜${sourceText}` : ""}）`,
              ),
            );
          });
          body.appendChild(list);
        }
      };
      paint();
    },
  });
}

function renderConsolidatePreview(result) {
  const box = $("contacts-detail");
  if (!box) return;
  const card = el("div", "contact-section contact-preview");
  card.appendChild(el("h3", "", "整理预览（没有写库）"));
  card.appendChild(el("p", "hint-line", result.note || ""));
  const parsed = result.parsed || {};
  const summary = [];
  if ((parsed.memories || []).length) summary.push(`要点 ${parsed.memories.length} 条`);
  if ((parsed.facts || []).length) summary.push(`关于他的事 ${parsed.facts.length} 条`);
  if ((parsed.relations || []).length) summary.push(`关系 ${parsed.relations.length} 条`);
  if ((parsed.digests || {}) && Object.keys(parsed.digests || {}).length) {
    summary.push(`缩略版 ${Object.keys(parsed.digests).length} 份`);
  }
  if (parsed.dream) summary.push("做了一个梦");
  card.appendChild(el("p", "", summary.join("、") || "模型没给出可用的改动"));
  const raw = el("pre", "contact-preview-raw");
  raw.textContent = (result.raw || "").slice(0, 1200);
  card.appendChild(el("p", "hint-line", "模型原话："));
  card.appendChild(raw);
  const promptBox = el("details", "contact-preview-prompt");
  promptBox.appendChild(el("summary", "", "看喂进去的提示词"));
  const user = el("pre", "contact-preview-raw");
  user.textContent = ((result.preview || {}).user || "").slice(0, 4000);
  promptBox.appendChild(user);
  card.appendChild(promptBox);
  box.insertBefore(card, box.firstChild);
}

async function contactAction(node) {
  const act = node.dataset.act;
  const session = $("contacts-session").value;
  const userId = ui.contactUser;
  if (!session || !userId) return;
  const detail = ui.contactDetail || {};
  try {
    if (act === "bond-add") {
      const selects = node.parentElement.querySelectorAll("select");
      const type = selects[0] ? selects[0].value : "";
      const asserted = selects[1] ? selects[1].value : "她的判断";
      await apiPost("profile/bond", {
        session,
        user_id: userId,
        action: "note",
        type,
        asserted_by: asserted,
      });
    } else if (act === "bond-close") {
      await apiPost("profile/bond", { session, user_id: userId, action: "close", id: node.dataset.id });
    } else if (act === "bond-accept") {
      await apiPost("profile/bond", { session, user_id: userId, action: "accept", id: node.dataset.id, policy: "replace" });
    } else if (act === "bond-delete") {
      await apiPost("profile/bond", { session, user_id: userId, action: "delete", id: node.dataset.id });
    } else if (act === "fact-add") {
      const box = node.parentElement;
      const kind = box.querySelector("select").value;
      const input = box.querySelector("input");
      if (!input.value.trim()) throw new Error("先写点什么");
      await apiPost("profile/fact", {
        session,
        user_id: userId,
        action: "add",
        kind,
        text: input.value.trim(),
      });
    } else if (act === "fact-pin") {
      await apiPost("profile/fact", {
        session,
        user_id: userId,
        action: "update",
        id: node.dataset.id,
        pinned: node.dataset.pinned === "1",
      });
    } else if (act === "fact-delete") {
      await apiPost("profile/fact", { session, user_id: userId, action: "delete", id: node.dataset.id });
    } else if (act === "affinity-save") {
      const slider = node.parentElement.querySelector("input[type=range]");
      await apiPost("profile/affinity", {
        session,
        user_id: userId,
        value: Number(slider.value),
        reason: "主人手动调的",
      });
    } else if (act === "grudge-add") {
      const input = node.parentElement.querySelector("[data-act=grudge-text]");
      const reason = (input && input.value.trim()) || "";
      if (!reason) {
        toast("先写一句她记着的事");
        return;
      }
      await apiPost("profile/grudge", { session, user_id: userId, action: "add", reason });
    } else if (act === "grudge-resolve") {
      await apiPost("profile/grudge", { session, user_id: userId, action: "resolve" });
    } else if (act === "grudge-delete") {
      await apiPost("profile/grudge", { session, user_id: userId, action: "delete" });
    } else if (act === "fields-save") {
      const box = node.parentElement;
      await apiPost("profile/person", {
        session,
        user_id: userId,
        call_me: box.querySelector("[data-act=field-call-me]").value,
        call_him: box.querySelector("[data-act=field-call-him]").value,
        note: box.querySelector("[data-act=field-note]").value,
        digest: box.querySelector("[data-act=field-digest]").value,
      });
    } else if (act === "person-forget") {
      // 插件页在 sandbox 的 iframe 里跑，没有 allow-modals：
      // window.confirm 会被浏览器拦掉（点了没反应），只能用自己的弹窗
      const sure = await confirmDialog({
        title: "忘掉这个人",
        message: "画像、事实、关系、好感记录都会删掉，而且没法撤回。",
        confirmText: "忘掉他",
      });
      if (!sure) return;
      await apiPost("profile/forget", { session, user_id: userId });
      ui.contactUser = "";
    } else {
      return;
    }
    toast("改好了");
    await loadContacts();
  } catch (error) {
    toast(error.message || "没改成");
  }
}

async function loadMemories() {
  const list = $("memory-list");
  list.innerHTML = "<p class='muted'>加载中…</p>";
  ui.memorySelection = new Set();
  try {
    const session = $("memory-session").value;
    const params = {
      session,
      node_id: $("memory-node").value,
      type: $("memory-type").value,
      scope: $("memory-scope").value,
      q: $("memory-keyword").value,
    };
    const [data, stats] = await Promise.all([
      apiGet("memories", params),
      apiGet("memories/stats", { session }),
    ]);
    const statsBox = $("memory-stats");
    statsBox.innerHTML = "";
    [
      `总数 ${stats.total}`,
      `场景 ${stats.by_type?.scene || 0}`,
      `内心 ${stats.by_type?.inner || 0}`,
      `互动 ${stats.by_type?.interaction || 0}`,
      `今日新增 ${stats.today_added}`,
      `今日召回 ${stats.today_recalled}`,
      `平均权重 ${stats.avg_weight}`,
    ].forEach((text) => statsBox.appendChild(el("span", "", text)));

    // 上次整理到底干了什么：不写这一行，"记忆没变化"永远只能靠猜
    try {
      const session = $("memory-session") ? $("memory-session").value : "";
      const logs = await apiGet("logs", { session, type: "consolidate", limit: 3 });
      const rows = logs.events || [];
      if (rows.length) {
        const last = rows[0];
        statsBox.appendChild(
          el("span", "", `上次整理：${last.text || "（没写说明）"}`),
        );
      } else {
        statsBox.appendChild(el("span", "", "上次整理：还没有跑过（她睡下 20 分钟后会整理一次）"));
      }
    } catch (error) {
      statsBox.appendChild(el("span", "", "上次整理：读不到"));
    }

    list.innerHTML = "";
    (data.memories || []).forEach((memory) => {
      const item = el("div", "list-item");
      item.classList.add("selectable");
      item.appendChild(rowCheckbox(item, memory.id, "memorySelection"));
      const info = el("div");
      info.appendChild(el("div", "", memory.content));
      info.appendChild(
        el(
          "div",
          "meta",
          `${memoryStamp(memory.created_at)} · [${memory.id}] ${memory.scope} · ${
            memory.node_id || "无节点"
          } · ${memory.type} · 权重 ${Number(memory.weight).toFixed(
            2,
          )} · 召回 ${memory.recall_count}` +
            // 整理过的记忆正文会换成要点、原文挪到 context：不标出来会以为"整理没跑"
            (memory.tier === "gist" ? "　· 已整理成要点" : "　· 原文"),
        ),
      );
      item.appendChild(info);
      const box = el("div", "inline");
      const more = el("button", "small", "详情");
      more.title = "看这条记忆的完整字段（含整理前的原文）";
      more.addEventListener("click", () => openMemoryDetail(memory));
      const edit = el("button", "small", "编辑");
      edit.addEventListener("click", () => openMemoryDialog(memory));
      const del = el("button", "small danger", "删除");
      del.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: "删除这条记忆？",
          message: `删掉之后就再也想不起来了：\n${memory.content}`,
          confirmText: "删除",
        });
        if (!ok) return;
        await apiPost("memories/delete", { id: memory.id });
        loadMemories();
      });
      box.appendChild(edit);
      box.appendChild(del);
      box.insertBefore(more, edit);
      item.appendChild(box);
      list.appendChild(item);
    });
    if (!(data.memories || []).length) list.appendChild(el("p", "muted", "没有符合条件的记忆。"));
  } catch (error) {
    list.innerHTML = "";
    list.appendChild(el("p", "muted", error.message || "加载失败"));
  }
}

async function addMemory() {
  const session = $("memory-session").value;
  if (!session) {
    toast("先选择一个会话");
    return;
  }
  openMemoryDialog(null, {
    session_id: session,
    node_id: $("memory-node").value || "",
    type: $("memory-type").value || "scene",
    scope: $("memory-scope").value || "",
    weight: 0.6,
  });
}

const MEMORY_TYPE_CHOICES = [
  { value: "scene", label: "场景（在这个地方发生过什么）" },
  { value: "interaction", label: "互动（和谁聊过什么）" },
  { value: "relation", label: "关系（对某个人的印象）" },
  { value: "inner", label: "内心（她自己的想法）" },
  { value: "event", label: "事件（某件具体的事）" },
];

const MEMORY_SCOPE_CHOICES = [
  { value: "", label: "用全局设置里的默认作用域" },
  { value: "node", label: "只属于这个地点" },
  { value: "group", label: "本会话都能想起" },
  { value: "persona", label: "同一个人格在哪都能想起" },
  { value: "group_persona", label: "本会话 + 该人格" },
  { value: "global", label: "所有会话共享" },
];

/** 记忆编辑 / 新增弹窗。memory 为 null 表示新增。 */
function openMemoryDialog(memory, preset = {}) {
  const base = memory || preset;
  const editing = Boolean(memory);
  openFormDialog({
    title: editing ? `编辑记忆 #${memory.id}` : "新增记忆",
    hint: "记忆会在「同一个会话 + 同一个地点」被召回，写清楚「谁、聊了什么、她怎么想」最有用。",
    confirmText: editing ? "保存" : "添加",
    fields: [
      {
        key: "content",
        label: "记忆内容",
        type: "textarea",
        rows: 3,
        placeholder: "例如：小明说他最近加班很累，我有点心疼",
        hint: "会原样写进提示词，尽量一句话说完。",
      },
      {
        key: "type",
        label: "类型",
        type: "select",
        options: MEMORY_TYPE_CHOICES,
        hint: "只是分类标签，影响筛选和提示词里的措辞。",
      },
      {
        key: "scope",
        label: "作用域",
        type: "select",
        options: MEMORY_SCOPE_CHOICES,
        hint: "决定这条记忆能被哪些会话/地点想起来。",
      },
      {
        key: "node_id",
        label: "绑定地点",
        type: "select",
        options: [{ value: "", label: "（不限地点）" }].concat(
          nodes().map((node) => ({ value: node.id, label: node.name || node.id })),
        ),
        hint: "绑到某个地点后，只有她在那儿时才会想起；不限地点则哪里都能想起。",
      },
      {
        key: "weight",
        label: "权重",
        type: "number",
        step: "0.05",
        min: "0",
        max: "1",
        hint: "0~1，越高越容易被想起来（0.5 左右比较合适）。",
      },
      {
        key: "emotion",
        label: "情绪标签",
        type: "text",
        placeholder: "例如：开心 / 心疼 / 有点介意",
        hint: "选填，会显示在记忆后面，影响她复述时的语气。",
      },
    ],
    values: {
      content: base.content || "",
      type: base.type || "scene",
      scope: base.scope || "",
      node_id: base.node_id || "",
      weight: base.weight ?? 0.6,
      emotion: base.emotion || "",
    },
    onSubmit: async (values) => {
      if (!values.content) {
        toast("记忆内容不能为空");
        return false;
      }
      if (editing) {
        const payload = { id: memory.id, ...values };
        // 「用默认作用域」在编辑时表示"不改"，否则会把 scope 写成空串，
        // 而空作用域在召回时匹配不上任何模式，等于这条记忆再也想不起来。
        if (!payload.scope) payload.scope = memory.scope || "";
        await apiPost("memories/update", payload);
      } else {
        if (!base.session_id) {
          toast("先选择一个会话");
          return false;
        }
        await apiPost("memories/create", { session_id: base.session_id, ...values });
      }
      toast(editing ? "已保存" : "已添加");
      loadMemories();
      return true;
    },
  });
}

/* ================================================================== */
/* 全局设置                                                            */
/* ================================================================== */

/** 全局设置的分组：小节标题 → 标签页。标题改了这里要跟着改。 */
const SETTINGS_TABS = [
  {
    key: "profile",
    label: "她是谁",
    hint: "名字、性别、角色卡、声音样例",
    sections: ["persona", "persona_brief"],
  },
  {
    key: "state",
    label: "状态与外观",
    hint: "数值怎么变、她的本事、睡觉、群名片",
    sections: ["状态变化速度", "她的本事（能力值）", "睡眠与打断", "群名片同步"],
  },
  {
    key: "talk",
    label: "说话与决策",
    hint: "多久开口、说什么、怎么分句",
    sections: [
      "性格与习惯",
      "自主决策",
      "孤独感与插话",
      "无人回应时的处理",
      "说话频率与预算",
      "说话节奏",
      "追问",
      "私聊",
      "表达变奏",
      "回复格式（精简/完整）",
      "谈资",
      // 语义检索（向量索引）属于"怎么翻旧事"，归这一档；以前它不在任何分类里，
      // 于是被兜底到「基础」页（复核报告 P2-5）。
      "语义检索（向量索引）",
      "action_selection",
      "boredom_online",
    ],
  },
  {
    key: "world",
    label: "世界与生活",
    hint: "世界规则、初始状态、事件、动作与地点、工具、天气",
    sections: [
      "世界与生活",
      "她的初始状态",
      "事件",
      "contributions",
      "作息与夜晚",
      "她自己的账",
      "干涉与线索",
      "动作与地点",
      "工具",
      "天气",
      // 表情包总开关与自动收图（用户 m00001）：以前素材页那几块面板保存后会跳到
      // world 档，而 world 档里根本没有表情包一节 —— 跳过去等于把设置页藏空。
      // 现在这里真的有一节，跳转目标存在，也顺手把两个总开关放进来看得见。
      "表情包",
    ],
  },
  {
    key: "mind",
    label: "记忆与上下文",
    hint: "记得什么、怎么把信息喂给模型",
    sections: [
      "记忆",
      // 记忆的容量/衰减参数（用户要求挂到设置页）：单独一段，挂在「记忆」下面
      "memory_budget",
      // 知识卡也归这一档（用户 m00001）：它是"记住的东西"，不该另开一级 tab
      "知识卡",
      "dream",
      "用户画像",
      "上下文",
      "图片转述",
    ],
  },
  {
    key: "advanced",
    label: "高级功能",
    hint: "不常改的：Agent 自主能力、联网搜索、存储",
    sections: ["Agent（自主能力）", "联网搜索", "存储"],
  },
  {
    key: "extensions",
    label: "扩展",
    hint: "装了哪些扩展包，以及它们各自的配置",
    sections: ["扩展"],
  },
  {
    key: "debug",
    label: "调试与安全",
    hint: "管理员、排查用，以及不许做的事",
    sections: ["管理员", "调试输出", "内容安全与隐私"],
  },
];

function settingsTabOf(section) {
  // 按"稳定 key"归口：标题是给人看的（会随性别变成她/他/ta），不能拿它当身份
  const key = typeof section === "string"
    ? section
    : String((section && section.dataset && section.dataset.sectionKey) || "");
  const title = typeof section === "string"
    ? section
    : String((section && section.dataset && section.dataset.sectionTitle) || "");
  const found = SETTINGS_TABS.find(
    (item) => item.sections.includes(key) || item.sections.includes(title),
  );
  return found ? found.key : SETTINGS_TABS[0].key;
}

const SETTINGS_MODE_KEY = "vw-settings-mode";

/** 常用 / 全部设置：默认常用，选择记在本机。 */
function readSettingsMode() {
  try {
    return window.localStorage.getItem(SETTINGS_MODE_KEY) === "advanced"
      ? "advanced"
      : "common";
  } catch (error) {
    return "common";
  }
}

function setSettingsMode(mode) {
  ui.settingsMode = mode === "advanced" ? "advanced" : "common";
  try {
    window.localStorage.setItem(SETTINGS_MODE_KEY, ui.settingsMode);
  } catch (error) {
    /* 隐私模式记不住就记不住 */
  }
  renderSettingsMode();
}

function renderSettingsMode() {
  const advanced = ui.settingsMode === "advanced";
  const hub = $("settings-hub");
  const form = $("settings-form");
  const passwordCard = $("settings-password-card");
  const bar = document.querySelector("#tab-settings .settings-bar");
  const hits = $("settings-search-hits");
  if (hub) hub.classList.toggle("hidden", advanced);
  if (bar) bar.classList.toggle("hidden", !advanced);
  if (form) form.classList.toggle("hidden", !advanced);
  if (passwordCard) passwordCard.classList.toggle("hidden", !advanced);
  if (hits && !advanced) hits.classList.add("hidden");
  if (advanced) {
    applySettingsTabs();
  } else {
    renderSettingsHub();
  }
}

/** 点常用卡片的「编辑」：切到全部设置并落到对应分类。 */
function openSettingsTab(tab) {
  ui.settingsTab = tab || SETTINGS_TABS[0].key;
  setSettingsMode("advanced");
  const form = $("settings-form");
  if (form && form.scrollIntoView) {
    form.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function renderSettingsHub() {
  const box = $("settings-hub");
  if (!box) return;
  const world = ui.config.world || {};
  const gender = (GENDERS.find((item) => item.key === world.gender) || {}).label || "未设置";
  const personaMode =
    { astrbot: "跟随 AstrBot", plugin: "用插件这份", append: "AstrBot + 插件" }[
      (world.persona || {}).mode || "astrbot"
    ] || "跟随 AstrBot";
  const model = (ui.config.providers || {}).llm || "跟随插件";
  const safety = [
    ...((world.content_safety || {}).blocked_words || []),
    ...((world.content_safety || {}).message_blocklist || []),
    ...((world.content_safety || {}).session_blocklist || []),
  ].length;
  const cards = [
    {
      title: "她是谁",
      desc: "名字、性别、角色卡",
      tab: "profile",
      values: [
        ["名字", world.bot_name || "未设置"],
        ["性别", gender],
        ["人设", personaMode],
      ],
    },
    {
      title: "世界与生活",
      desc: "世界名称、时区、初始状态",
      tab: "world",
      values: [
        ["世界", world.name || "小世界"],
        ["时区", world.timezone || "Asia/Shanghai"],
        ["初始状态", "默认"],
      ],
    },
    {
      title: "说话与决策",
      desc: "主动程度、群聊活跃度、自主能力",
      tab: "talk",
      values: [
        ["性格", handfeelSummary(world).replace(/^当前：/, "")],
        ["自主能力", world.agent && world.agent.enabled ? "开启" : "关闭"],
        ["表达变奏", world.variation && world.variation.enabled === false ? "关闭" : "开启"],
      ],
    },
    {
      title: "运行与模型",
      desc: "主模型、辅助模型、工具",
      tab: "talk",
      values: [
        ["主模型", String(model)],
        ["辅助模型", String((ui.config.providers || {}).helper || "跟随插件")],
        ["可用工具", `${(ui.tools || []).length} 个`],
      ],
    },
    {
      title: "安全与调试",
      desc: "访问密码、内容安全、调试",
      tab: "debug",
      values: [
        ["访问密码", ui.passwordRequired ? "独立密码" : "跟随 AstrBot"],
        ["内容安全", safety ? `${safety} 条规则` : "未配置"],
        ["调试输出", "在全部设置里"],
      ],
    },
  ];

  box.innerHTML = "";
  const head = el("div", "settings-hub-head");
  const copy = el("div");
  copy.appendChild(el("h2", "", "常用设置"));
  copy.appendChild(
    el("p", "muted", "只显示最常改的 5 组；完整参数在「全部设置」里，随时可以切回去。"),
  );
  head.appendChild(copy);
  const all = el("button", "ghost", "全部设置");
  all.type = "button";
  all.addEventListener("click", () => setSettingsMode("advanced"));
  head.appendChild(all);
  box.appendChild(head);

  const grid = el("div", "settings-hub-grid");
  cards.forEach((item) => {
    const card = el("div", "card settings-hub-card");
    const cardHead = el("div", "settings-hub-card-head");
    cardHead.appendChild(el("h3", "", item.title));
    cardHead.appendChild(el("span", "muted", item.desc));
    card.appendChild(cardHead);
    const values = el("div", "settings-hub-values");
    item.values.forEach(([key, value]) => {
      values.appendChild(el("span", "", key));
      values.appendChild(el("span", "", value));
    });
    card.appendChild(values);
    const foot = el("div", "settings-hub-card-foot");
    const edit = el("button", "ghost small", "编辑");
    edit.type = "button";
    edit.addEventListener("click", () => openSettingsTab(item.tab));
    foot.appendChild(edit);
    card.appendChild(foot);
    grid.appendChild(card);
  });
  box.appendChild(grid);
}

function renderSettingsTabs() {
  const box = $("settings-tabs");
  if (!box) return;
  box.innerHTML = "";
  const current = ui.settingsTab || SETTINGS_TABS[0].key;
  SETTINGS_TABS.forEach((tab) => {
    const button = el("button", `settings-tab${tab.key === current ? " active" : ""}`);
    button.type = "button";
    button.title = tab.hint;
    const label = tab.key === "profile" ? `${pronoun()}是谁` : tab.label;
    button.appendChild(el("span", "", label));
    if (ui.dirtyTabs.has(tab.key)) {
      button.appendChild(el("i", "dirty-dot", ""));
    }
    button.addEventListener("click", () => {
      ui.settingsTab = tab.key;
      applySettingsTabs();
    });
    box.appendChild(button);
  });
}

/** 按当前选中的标签显示/隐藏小节（分类只影响显示，world JSON 一点不动）。 */
function applySettingsTabs() {
  const form = $("settings-form");
  if (!form) return;
  const current = ui.settingsTab || SETTINGS_TABS[0].key;
  Array.from(form.children).forEach((section) => {
    if (!section.classList.contains("card-section")) return;
    section.classList.toggle("hidden", settingsTabOf(section) !== current);
  });
  const passwordCard = $("settings-password-card");
  if (passwordCard) {
    passwordCard.classList.toggle(
      "hidden",
      ui.settingsMode !== "advanced" || current !== "debug",
    );
  }
  const rawEditor = $("world-json-editor");
  if (rawEditor) rawEditor.classList.toggle("hidden", current !== "debug");
  // 「扩展」第一次被点开时才去拉数据（进页面不拉、也不轮询）：装了什么扩展
  // 跟 world.json 无关，没必要在别的分栏上付这次请求。
  if (current === "extensions") {
    const extensions = form.querySelector('[data-section-key="扩展"]');
    if (extensions && typeof extensions._loadExtensions === "function") {
      extensions._loadExtensions();
    }
  }
  renderSettingsTabs();
}

/** 设置搜索：按小节标题、字段名、字段说明找，点结果直接跳到对应标签。 */
function searchSettings(keyword) {
  const box = $("settings-search-hits");
  if (!box) return;
  const word = String(keyword || "").trim().toLowerCase();
  if (!word) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  const hits = [];
  const form = $("settings-form");
  Array.from(form.children).forEach((section) => {
    if (!section.classList.contains("card-section")) return;
    const title = String(section.dataset.sectionTitle || "");
    const tab = settingsTabOf(section);
    const headMatch = title.toLowerCase().includes(word);
    const fields = Array.from(section.querySelectorAll(".field-head")).map((head) =>
      String(head.textContent || "").trim(),
    );
    const tips = Array.from(section.querySelectorAll("[data-tip]")).map((node) =>
      String(node.getAttribute("data-tip") || ""),
    );
    const fieldHit = fields.find((item) => item.toLowerCase().includes(word));
    const tipHit = tips.find((item) => item.toLowerCase().includes(word));
    if (!headMatch && !fieldHit && !tipHit) return;
    // 命中的**具体字段**（没命中字段就是整节）：点结果要能落到它身上（P2-4）
    const fieldNode = fieldHit
      ? Array.from(section.querySelectorAll(".field-head")).find(
          (head) => String(head.textContent || "").trim() === fieldHit,
        )
      : null;
    hits.push({
      tab,
      title,
      label: headMatch ? title : fieldHit || title,
      tip: tipHit ? tipHit.slice(0, 80) + "…" : "",
      field: fieldHit || "",
      node: fieldNode ? fieldNode.closest(".field") || fieldNode : null,
    });
  });
  box.innerHTML = "";
  box.classList.remove("hidden");
  if (!hits.length) {
    box.appendChild(el("span", "muted", "没找到这个设置项"));
    return;
  }
  hits.slice(0, 12).forEach((hit) => {
    const item = el("button", "settings-hit");
    item.type = "button";
    item.appendChild(el("b", "", hit.label));
    item.appendChild(el("span", "muted", `（${hit.title}）`));
    if (hit.tip) item.title = hit.tip;
    item.addEventListener("click", () => {
      ui.settingsTab = hit.tab;
      applySettingsTabs();
      const form = $("settings-form");
      const target = Array.from(form.children).find(
        (section) => String(section.dataset.sectionTitle || "") === hit.title,
      );
      const focusNode = (hit.node && hit.node.isConnected ? hit.node : target) || target;
      // 高级字段默认收在 <details> 里：**先把它和所有祖先展开**，否则"定位到了看不见"
      if (focusNode) {
        let node = focusNode;
        while (node && node !== form) {
          if (node.tagName === "DETAILS") node.open = true;
          node = node.parentElement;
        }
        if (focusNode.scrollIntoView) {
          focusNode.scrollIntoView({ block: "center", behavior: "smooth" });
        }
        // 高亮两秒：列表里点完立刻能看出落点
        focusNode.classList.add("settings-hit-target");
        window.setTimeout(() => focusNode.classList.remove("settings-hit-target"), 2000);
        const focusable = focusNode.querySelector("input, select, textarea, button");
        if (focusable && focusable.focus) focusable.focus({ preventScroll: true });
      }
      box.classList.add("hidden");
    });
    box.appendChild(item);
  });
}

function settingsSection(title, note, tipText, key = "") {
  const section = el("div", "card-section");
  section.dataset.sectionKey = key || title;
  section.dataset.sectionTitle = title;
  const heading = el("h3");
  heading.appendChild(el("span", "", title));
  const tip = tipBox(tipText);
  if (tip) heading.appendChild(tip);
  section.appendChild(heading);
  if (note) section.appendChild(el("p", "section-note", note));
  const fields = el("div", "fields");
  section.appendChild(fields);
  section._fields = fields;
  return section;
}

/**
 * 把标了 ``adv`` 的字段收进小节底部的「高级设置」。
 *
 * 用得着的没几个、但一个都不能少的那些（频率、预算、上限、阈值）留在这儿：
 * 默认收起，页面第一眼只剩真正要改的东西。
 */
const ADVANCED_LABELS = {
  "事件": [
    "大事件两步之间至少隔多久（分钟）",
    "「最近发生在我身上的事」最多带几条",
    "一条线索最多几步",
    "一条线索最长多久（分钟）",
    "事件里她一次最多说几句",
    "事件发言的硬顶",
    "两件事之间至少隔多久（分钟）",
    "一条线索里最多调用几次动作",
    "每一幕出图的概率",
    "事件出图用哪些动作",
    "判定结果影响情绪",
    "题材的尺度边界（可留空）",
    "最近几件用过的题材先不重复",
    "推迟一次隔多久再检查（分钟）",
    "同一条日程一天最多推迟几次",
    "同一条日程一天最多推迟多久（分钟）",
  ],
  "上下文": [
    "别处同时听到的带多少行",
    "没回过的那批每条最多多少字",
    "已经回过的那批每条最多多少字",
    "「刚聊过什么」有效期（分钟）",
    "引用旧消息最多写多少字",
    "攒到多少条开始压缩",
    "压缩后保留多少条原文",
    "两次压缩至少间隔（分钟）",
    "一次最多带几张图（没配转述模型时）",
  ],
  "图片转述": [
    "最多记住多少张图",
    "缓存保留多少天",
    "摘要最多多少字",
  ],
  "说话节奏": [
    "每个字停顿（秒）",
    "单条最多停顿（秒）",
    "回答前先等几秒（安静期）",
    "安静期最长等多久（秒）",
    "群里 @ 她时先等几秒",
    "@ 时最多等几秒",
    "说话密度统计窗口（分钟）",
    "窗口内说几句算太密",
  ],
};

function fieldLabelOf(node) {
  if (!node || !node.querySelector) return "";
  const head = node.querySelector(".field-head > span");
  if (head) return String(head.textContent || "").trim();
  const own = node.querySelector(":scope > span");
  return own ? String(own.textContent || "").trim() : "";
}

function collapseAdvancedFields(form) {
  Array.from(form.querySelectorAll(":scope > .card-section")).forEach((section) => {
    const fields = section.querySelector(":scope > .fields");
    if (!fields) return;
    const wanted = new Set(ADVANCED_LABELS[String(section.dataset.sectionTitle || "")] || []);
    const advanced = Array.from(fields.children).filter((node) => {
      if (node.dataset && node.dataset.adv === "1") return true;
      const label = fieldLabelOf(node);
      return Boolean(label) && wanted.has(label);
    });
    if (!advanced.length) return;
    const box = document.createElement("details");
    box.className = "adv-fields";
    const summary = document.createElement("summary");
    summary.appendChild(el("span", "", `高级设置（${advanced.length} 项）`));
    box.appendChild(summary);
    const inner = el("div", "fields adv-inner");
    advanced.forEach((node) => inner.appendChild(node));
    box.appendChild(inner);
    fields.appendChild(box);
  });
}

/* --- 存储（高级功能） --- */

/**
 * 存储卡（用户 m00001）：只读状态 + 阈值/保留天数 + 「立即清理一次 / 刷新」。
 *
 * 状态只在建卡时取一次、点按钮再取一次 —— 不做 30 秒轮询：设置页可以挂很久，
 * 而「磁盘还剩多少、上次维护是什么时候」本来就是"要看的时候看一眼"的信息，
 * 定时拉只会在后台一直发请求。
 */
function storageCard(world) {
  const section = settingsSection(
    "存储",
    "数据目录占多大、磁盘还剩多少，旧快照和旧记录留多久。",
    "磁盘写满会让所有写入失败，她那一轮直接不说话。这里是只读快照，点「刷新」再看一次。",
  );
  if (!world.storage) world.storage = {};
  const storage = world.storage;
  const box = el("div", "status-box");
  const lines = {};
  [
    ["free", "磁盘可用"],
    ["data", "数据目录占用"],
    ["db", "state.db"],
    ["wal", "state.db-wal"],
    ["shm", "state.db-shm"],
    ["top", "最大的文件"],
    ["write", "最近一次写失败"],
    ["snapshot", "旧快照清理"],
    ["vacuum", "自动缩库"],
    ["error", "读取"],
  ].forEach(([key, label]) => {
    lines[key] = statusLine(box, label, "正在读…");
  });
  section._fields.appendChild(box);

  const say = (key, text, tone = "") => {
    const row = lines[key];
    if (!row) return;
    const node = row.querySelector(".status-val");
    if (node) node.textContent = text;
    row.classList.remove("bad", "warn", "muted");
    if (tone) row.classList.add(tone);
  };
  const clock = (value) => {
    const ts = Number(value) || 0;
    return ts > 0 ? new Date(ts * 1000).toLocaleString() : "";
  };
  const VACUUM_MODES = { "": "还没跑过", none: "不用回收", full: "全量回收", incremental: "增量回收" };

  const load = async () => {
    try {
      const data = await apiGet("storage");
      if (!data || data.ok === false) {
        throw new Error((data && data.error) || "接口没返回 ok");
      }
      say(
        "free",
        `${formatBytes(data.free_bytes)}${data.low_space ? "（低于告警线）" : ""}`,
        data.low_space ? "bad" : "",
      );
      say(
        "data",
        `${formatBytes(data.data_bytes)}${data.over_limit ? "（超过上限）" : ""}`,
        data.over_limit ? "bad" : "",
      );
      say("db", formatBytes(data.db_bytes));
      say("wal", formatBytes(data.wal_bytes));
      say("shm", formatBytes(data.shm_bytes));
      const top = Array.isArray(data.top_files) ? data.top_files : [];
      say(
        "top",
        top.length
          ? top.map((item) => `${item.name} ${formatBytes(item.bytes)}`).join("、")
          : "这一项没读到文件清单",
        top.length ? "" : "muted",
      );
      const writeError = String(data.last_write_error || "").trim();
      if (writeError) {
        const at = clock(data.last_write_error_at);
        say("write", `${writeError}${at ? `（${at}）` : ""}`, "bad");
      } else {
        say("write", "目前没有写失败", "muted");
      }
      const snapshot = data.snapshot || {};
      const cleanedAt = clock(snapshot.last_at);
      // 没跑过清理时 `deleted` / `freed_bytes` 都是 0；写成「上次删 0 个、腾出 0 B」
      // 读起来像"刚跑过、什么都没删"，所以先看有没有跑过
      say(
        "snapshot",
        cleanedAt
          ? `上次删 ${num(snapshot.deleted)} 个、腾出 ${formatBytes(snapshot.freed_bytes)}`
            + `（${cleanedAt}）；每类留 ${num(snapshot.keep)} 份`
          : `还没清理过；每类留 ${num(snapshot.keep)} 份`,
      );
      const vacuum = data.vacuum || {};
      const ranAt = clock(vacuum.last_run_at);
      say(
        "vacuum",
        `${vacuum.enabled ? "开着" : "关着"}；`
          + `${VACUUM_MODES[String(vacuum.mode || "")] || String(vacuum.mode || "还没跑过")}；`
          + `空闲页 ${num(vacuum.freelist_pages)}${ranAt ? `（${ranAt}）` : ""}`,
      );
      say("error", "正常", "muted");
      return true;
    } catch (error) {
      say("error", `暂时读不到存储状态：${error && error.message ? error.message : error}`, "bad");
      return false;
    }
  };

  section._fields.appendChild(el("div", "sub-title", "阈值与保留"));
  section._fields.appendChild(
    inputField(
      "磁盘至少留多少 MB",
      num(storage.min_free_mb, 500),
      (value) => (storage.min_free_mb = num(value)),
      { hint: "可用空间低于这个数就告警。默认 500。", type: "number", min: "0", max: "102400", step: "50" },
    ),
  );
  section._fields.appendChild(
    inputField(
      "数据目录最多多少 MB",
      num(storage.max_data_mb, 300),
      (value) => (storage.max_data_mb = num(value)),
      { hint: "数据目录超过这个大小就告警。默认 300。", type: "number", min: "0", max: "102400", step: "50" },
    ),
  );
  section._fields.appendChild(
    inputField(
      "每类旧快照留几份",
      num(storage.keep_snapshots, 1),
      (value) => (storage.keep_snapshots = num(value)),
      { hint: "五类快照各留几份；填 0 就不自动清。默认 1 份。", type: "number", min: "0", max: "50", step: "1" },
    ),
  );
  section._fields.appendChild(
    checkboxField(
      "自动清旧快照",
      storage.auto_clean_snapshots !== false,
      (value) => (storage.auto_clean_snapshots = value),
      { hint: "启动和每日维护时按上面的份数删；关掉只算不删。" },
    ),
  );
  section._fields.appendChild(
    checkboxField(
      "每天自动缩库",
      storage.vacuum_enabled !== false,
      (value) => (storage.vacuum_enabled = value),
      { hint: "把删掉的空闲页还给文件系统；关掉库只涨不缩。" },
    ),
  );
  section._fields.appendChild(
    inputField(
      "决策批次留几天",
      num(storage.decision_batch_keep_days, 14),
      (value) => (storage.decision_batch_keep_days = num(value)),
      { hint: "一天清一次，只删更老的；0 = 不清。默认 14 天。", type: "number", min: "0", max: "3650", step: "1" },
    ),
  );
  section._fields.appendChild(
    inputField(
      "状态历史留几天",
      num(storage.state_history_keep_days, 30),
      (value) => (storage.state_history_keep_days = num(value)),
      { hint: "另有按条数裁剪兜底；0 = 不清。默认 30 天。", type: "number", min: "0", max: "3650", step: "1" },
    ),
  );
  section._fields.appendChild(
    inputField(
      "事件记录留几天",
      num(storage.event_keep_days, 30),
      (value) => (storage.event_keep_days = num(value)),
      { hint: "另有按条数裁剪兜底；0 = 不清。默认 30 天。", type: "number", min: "0", max: "3650", step: "1" },
    ),
  );

  const actions = el("div", "row-item");
  const maintain = el("button", "small", "立即清理一次");
  maintain.type = "button";
  maintain.title = "清旧快照、按保留天数删旧记录，再回收空闲页";
  maintain.addEventListener("click", async () => {
    const sure = await confirmDialog({
      title: "立即清理一次",
      message:
        "会删掉超过保留份数的旧快照、超过保留天数的旧记录，并回收数据库空闲页。"
        + "主库 state.db、world.json、会话与图片都不动。",
      confirmText: "清理",
    });
    if (!sure) return;
    maintain.disabled = true;
    try {
      const result = await apiPost("storage/maintenance");
      if (!result || result.ok === false) {
        throw new Error((result && result.error) || "维护没返回 ok");
      }
      const removed = result.removed || {};
      const total = Object.keys(removed).reduce((sum, key) => sum + num(removed[key]), 0);
      const snapshot = result.snapshot || {};
      toast(`清理好了：删掉 ${total} 项，腾出 ${formatBytes(snapshot.freed_bytes)}`);
      await load();
    } catch (error) {
      toast(`清理失败：${error && error.message ? error.message : error}`);
    } finally {
      maintain.disabled = false;
    }
  });
  const refresh = el("button", "small", "刷新");
  refresh.type = "button";
  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    try {
      await load();
    } finally {
      refresh.disabled = false;
    }
  });
  actions.appendChild(maintain);
  actions.appendChild(refresh);
  section._fields.appendChild(actions);

  load();
  return section;
}

/* --- 扩展（「扩展」分栏：扩展包自己声明的配置） --- */

/**
 * 扩展设置：后端一直有 `GET/POST /extensions`，前端以前只画动作列表，
 * 所以搬史那三项（enabled / keywords / daily_limit）等于没有界面。
 *
 * 这里完全照扩展自己声明的 `settings` 画：`bool` → 复选框，`number` → 数字框
 * （带 min/max/step），`string` 和未知 kind → 单行文本。没装扩展就是一句空态，
 * 不报错、不轮询、不自动保存（点各自卡片上的「保存」才发）。
 */
function extensionsCard() {
  const section = settingsSection(
    "扩展",
    "装了哪些扩展包，以及它们各自的配置。字段由扩展包自己声明，这里只照着画。",
    "没装扩展时这里是空的；装了之后改完点卡片上的「保存」才生效。",
  );
  // 扩展的值不写在 world.json 里：别让它们点亮「有未保存的改动」
  section.dataset.noDirty = "1";
  const state = { loaded: false, loading: false };
  const list = el("div", "status-card");
  section._fields.appendChild(list);

  const showNote = (text) => {
    list.innerHTML = "";
    list.appendChild(el("p", "muted", text));
  };

  const extensionCard = (item) => {
    const card = el("div", "card");
    const head = el("h2", "", String(item.title || item.name || "扩展"));
    const version = String(item.version || "").trim();
    if (version) head.appendChild(el("span", "muted", `v${version}`));
    card.appendChild(head);

    // 只改这一张卡自己的副本：点「保存」时才拼成 {values: {扩展名: {...}}}
    const values = Object.assign({}, item.values || {});
    const fields = el("div", "fields");
    const specs = Array.isArray(item.settings) ? item.settings : [];
    specs.forEach((spec) => {
      const key = String((spec && spec.key) || "");
      if (!key) return;
      const label = String(spec.label || key);
      const hint = String(spec.hint || "");
      const kind = String((spec && spec.kind) || "string");
      const raw = values[key] !== undefined ? values[key] : spec.default;
      if (kind === "bool") {
        fields.appendChild(
          checkboxField(label, Boolean(raw), (next) => { values[key] = Boolean(next); }, { hint }),
        );
        return;
      }
      if (kind === "number") {
        const opts = { hint, type: "number" };
        if (spec.min !== undefined && spec.min !== null) opts.min = String(spec.min);
        if (spec.max !== undefined && spec.max !== null) opts.max = String(spec.max);
        if (spec.step !== undefined && spec.step !== null) opts.step = String(spec.step);
        fields.appendChild(
          inputField(
            label,
            raw === undefined || raw === null ? "" : num(raw),
            (next) => { values[key] = num(next); },
            opts,
          ),
        );
        return;
      }
      // string 与将来才有的未知 kind：一律单行文本，不替扩展猜控件
      fields.appendChild(
        inputField(
          label,
          raw === undefined || raw === null ? "" : String(raw),
          (next) => { values[key] = next; },
          { hint },
        ),
      );
    });
    card.appendChild(fields);

    const row = el("div", "row-item");
    const save = el("button", "small", "保存");
    save.type = "button";
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const result = await apiPost("extensions", {
          values: { [String(item.name || "")]: values },
        });
        if (!result || result.ok === false) {
          throw new Error((result && result.error) || "保存没返回 ok");
        }
        toast("已保存");
      } catch (error) {
        toast(`保存失败：${error && error.message ? error.message : error}`);
      } finally {
        save.disabled = false;
      }
    });
    row.appendChild(save);
    card.appendChild(row);
    return card;
  };

  const load = async (force = false) => {
    if (state.loading || (state.loaded && !force)) return;
    state.loading = true;
    showNote("正在读扩展…");
    try {
      const data = await apiGet("extensions");
      const items = Array.isArray(data && data.extensions) ? data.extensions : [];
      state.loaded = true;
      if (!items.length) {
        showNote("还没有装扩展包。");
        return;
      }
      list.innerHTML = "";
      items.forEach((item) => list.appendChild(extensionCard(item)));
    } catch (error) {
      showNote(`暂时读不到扩展：${error && error.message ? error.message : error}`);
    } finally {
      state.loading = false;
    }
  };

  section._loadExtensions = load;
  return section;
}

/* --- 知识卡（记忆与上下文） --- */

const KNOWLEDGE_PAGE_SIZE = 50;

/** 知识卡列表（用户 m00001）：搜索、翻页、多选删除。删卡走 confirmDialog。 */
function knowledgeCard() {
  const section = settingsSection(
    "知识卡",
    "她查过、记下来的东西：一条一个主题，带摘要和关键词。用之前会先看置信度与新鲜度。",
    "过期（expired）的卡不会再被翻出来用，但也不会自己消失；确认没用了就删掉。",
  );
  const state = { q: "", offset: 0, total: 0, items: [], selected: new Set(), loading: false };

  const bar = el("div", "row-item knowledge-bar");
  const search = document.createElement("input");
  search.type = "search";
  search.id = "knowledge-card-search";
  search.className = "knowledge-search";
  search.placeholder = "搜主题 / 摘要 / 关键词，回车";
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    state.q = String(search.value || "").trim();
    state.offset = 0;
    load();
  });
  // `type="search"` 的 ✕ 清空不会触发 keydown：清空了就按空条件重拉一次，
  // 否则列表还停在旧关键词的结果上，看起来像"清空没生效"。
  search.addEventListener("input", () => {
    if (String(search.value || "").trim()) return;
    if (!state.q) return;
    state.q = "";
    state.offset = 0;
    load();
  });
  bar.appendChild(search);
  const countText = el("span", "section-note", "");
  bar.appendChild(countText);
  section._fields.appendChild(bar);

  const list = el("div", "knowledge-list");
  list.id = "knowledge-card-list";
  section._fields.appendChild(list);

  const actions = el("div", "row-item");
  const prev = el("button", "small", "上一页");
  prev.type = "button";
  prev.disabled = true;
  prev.addEventListener("click", () => {
    if (state.offset <= 0) return;
    state.offset = Math.max(0, state.offset - KNOWLEDGE_PAGE_SIZE);
    load();
  });
  const next = el("button", "small", "下一页");
  next.type = "button";
  next.disabled = true;
  next.addEventListener("click", () => {
    if (state.offset + KNOWLEDGE_PAGE_SIZE >= state.total) return;
    state.offset += KNOWLEDGE_PAGE_SIZE;
    load();
  });
  const remove = el("button", "danger small", "删除选中");
  remove.type = "button";
  remove.id = "knowledge-card-delete";
  remove.disabled = true;
  remove.addEventListener("click", async () => {
    const ids = Array.from(state.selected);
    if (!ids.length) {
      toast("先勾选要删的知识卡");
      return;
    }
    const sure = await confirmDialog({
      title: "删除知识卡",
      message: `删掉这 ${ids.length} 张知识卡？删完她就想不起来了，也不能撤回。`,
      confirmText: "删除",
    });
    if (!sure) return;
    remove.disabled = true;
    try {
      const result = await apiPost("knowledge/cards/delete", { ids });
      if (!result || result.ok === false) {
        throw new Error((result && result.error) || "删除没返回 ok");
      }
      toast(`删掉了 ${num(result.deleted)} 张知识卡`);
      state.selected.clear();
      await load();
    } catch (error) {
      toast(`删除失败：${error && error.message ? error.message : error}`);
    } finally {
      remove.disabled = state.selected.size === 0;
    }
  });
  actions.appendChild(remove);
  actions.appendChild(prev);
  actions.appendChild(next);
  section._fields.appendChild(actions);

  const renderRows = () => {
    list.innerHTML = "";
    prev.disabled = state.offset <= 0;
    next.disabled = state.offset + KNOWLEDGE_PAGE_SIZE >= state.total;
    remove.disabled = state.selected.size === 0;
    countText.textContent = state.total
      ? `共 ${state.total} 张，这一页 ${state.offset + 1}–`
        + `${Math.min(state.total, state.offset + state.items.length)} 张`
      : "";
    if (!state.items.length) {
      list.appendChild(el("p", "section-note knowledge-empty", "还没有知识卡。她查到的东西会先落在这里。"));
      return;
    }
    state.items.forEach((item) => {
      const row = el("div", "knowledge-row");
      const pick = document.createElement("input");
      pick.type = "checkbox";
      pick.className = "knowledge-pick";
      pick.checked = state.selected.has(item.id);
      pick.addEventListener("change", () => {
        if (pick.checked) state.selected.add(item.id);
        else state.selected.delete(item.id);
        remove.disabled = state.selected.size === 0;
      });
      row.appendChild(pick);
      const main = el("div", "knowledge-main");
      const head = el("div", "knowledge-head");
      head.appendChild(el("span", "knowledge-subject", String(item.subject || "（没有主题）")));
      head.appendChild(
        el("span", "knowledge-conf", `置信度 ${Math.round(num(item.confidence) * 100)}%`),
      );
      const status = String(item.status || "active");
      if (status !== "active") head.appendChild(el("span", "knowledge-tag", status));
      main.appendChild(head);
      const summary = String(item.summary || "").trim();
      if (summary) main.appendChild(el("p", "knowledge-summary", summary));
      const meta = [];
      const keywords = Array.isArray(item.keywords) ? item.keywords.filter(Boolean) : [];
      if (keywords.length) meta.push(`关键词：${keywords.join(" / ")}`);
      const created = num(item.created_at);
      if (created > 0) meta.push(`记于 ${new Date(created * 1000).toLocaleString()}`);
      meta.push(`用过 ${num(item.use_count)} 次`);
      main.appendChild(el("div", "knowledge-meta", meta.join(" · ")));
      row.appendChild(main);
      list.appendChild(row);
    });
  };

  const load = async () => {
    if (state.loading) return;
    state.loading = true;
    countText.textContent = "正在读…";
    try {
      const params = { limit: KNOWLEDGE_PAGE_SIZE, offset: state.offset };
      if (state.q) params.q = state.q;
      const data = await apiGet("knowledge/cards", params);
      if (!data || data.ok === false) {
        throw new Error((data && data.error) || "接口没返回 ok");
      }
      state.items = Array.isArray(data.items) ? data.items : [];
      state.total = num(data.total);
      if (!state.items.length && state.offset > 0) {
        state.offset = 0;
        state.loading = false;
        return load();
      }
      state.selected.clear();
      renderRows();
    } catch (error) {
      list.innerHTML = "";
      list.appendChild(
        el(
          "p",
          "section-note",
          `暂时读不到知识卡：${error && error.message ? error.message : error}`,
        ),
      );
      countText.textContent = "";
    } finally {
      state.loading = false;
    }
  };

  load();
  return section;
}

/* --- 表情包（世界与生活） --- */

/**
 * 表情包总开关（用户 m00001 第 4 条）：素材页那几块面板保存后会 `ui.settingsTab = "world"`，
 * 以前 world 档里根本没有这一节，跳过去等于把设置页的可见小节藏空。
 * 现在 world 档真的有一节，这里放两个总开关 + 一个去素材页挑图的入口。
 */
function memeSettingsCard(world) {
  const section = settingsSection(
    "表情包",
    "她能不能发表情包、要不要自己收图。挑图、清洗和额度在「素材 → 表情包」里。",
    "总开关关掉后聊天里不会出现表情包动作；已经收的图和记录都不会被删。",
  );
  if (!world.meme) world.meme = {};
  const meme = world.meme;
  section._fields.appendChild(
    checkboxField(
      "打开表情包",
      meme.enabled !== false,
      (value) => {
        meme.enabled = value;
        memeMasterField();
        memeMasterBox();
      },
      { hint: "关掉之后聊天里不会出现表情包动作；已收的图与记录都不会被删除。" },
    ),
  );
  // 收图没有"看到就全收"这一档（用户 2026-10-29 拍板）：开关与字段一起删了，
  // 收图只有一条路——「收图与评审」里攒池、拼图、通过才收藏。
  const actions = el("div", "row-item");
  const jump = el("button", "small", "去素材页挑图");
  jump.type = "button";
  jump.addEventListener("click", () => {
    showTab("materials");
    showMaterialPane("meme");
  });
  actions.appendChild(jump);
  section._fields.appendChild(actions);
  return section;
}

function renderSettings() {
  const world = ui.config.world;
  world.default_state = world.default_state || {};
  world.state_dynamics = world.state_dynamics || {};
  world.limits = world.limits || {};
  world.engagement = world.engagement || {};
  world.nickname_sync = world.nickname_sync || {};
  world.content_safety = world.content_safety || {};
  world.reply_style = world.reply_style || {};
  world.variation = world.variation || {};
  world.vision = world.vision || {};
  const form = $("settings-form");
  // **设置页任何一处改动都要算"有未保存的改动"**：过去只在少数控件（滑块、文本域、按钮组）
  // 里自己调 markDirty，普通复选框和数字框（"要不要开这个功能""等几分钟"）改完是哑的——
  // 按保存只回一句「没有要保存的改动」，看着像没生效。这里统一挂一次（innerHTML 清空不会
  // 影响 form 自身的监听），标 `[data-no-dirty]` 的控件除外（那种是纯展示用的小开关）。
  bindDirtyChange(form, "world");
  form.innerHTML = "";
  form.className = "form settings";

  /* --- 世界与生活 --- */
  const worldBase = settingsSection(
    "世界与生活",
    "世界名称、时区、世界规则；她的初始状态也在这里。",
    "世界规则每次对话都会带上，用一两句话说明「她在过自己的生活、不要解释设定」就够了。",
  );
  const worldFull = el("div", "full");
  worldFull.appendChild(
    textareaField("世界规则（全局提示词）", world.global_prompt || "", (value) => (world.global_prompt = value), {
      hint:
        "每次和她对话都会带上这段规则。用一两句话说明「她在过自己的生活、不要解释设定」就够了，不要写太长。",
      rows: 4,
    }),
  );
  worldBase._fields.appendChild(worldFull);
  worldBase._fields.appendChild(
    inputField("世界名称", world.name || "小世界", (value) => (world.name = value), {
      hint: "只用于展示。",
    }),
  );
  worldBase._fields.appendChild(
    checkboxField(
      "要求先写推理草稿（reasoning）",
      world.reasoning_enabled !== false,
      (value) => (world.reasoning_enabled = value),
      {
        hint: "输出动作前先写「在哪 / 状态 / 心情 / 和谁说话 / 下一步计划」。草稿不进群、不占动作数，能减少答错对象。",
      },
    ),
  );
  worldBase._fields.appendChild(
    inputField("时区", world.timezone || "Asia/Shanghai", (value) => (world.timezone = value), {
      hint: "日程按这个时区判断几点。系统缺少时区数据时自动退回本机时间。",
    }),
  );
  form.appendChild(worldBase);

  /* --- 她 / 他 / ta（人设）：构成"这个人"的设置都收在这一节 --- */
  world.persona = world.persona || { mode: "astrbot", text: "" };
  const personaSection = settingsSection(
    pronoun(),
    "构成这个人的东西都在这一节：称呼与名字、说话的样子（角色卡）、给打杂模型的摘要。",
    "标题跟着性别走，默认沿用 AstrBot 那份人格；想让她不随会话漂移就选「用这一份」。",
    "persona",
  );
  personaSection._fields.appendChild(
    pillsField(
      "性别",
      world.gender || "female",
      GENDERS,
      (value) => {
        world.gender = value;
        renderSettings();
        applyPronoun($("app"));
      },
      {
        hint:
          "决定文案里的称呼：女→她、男→他、塑料袋→ta。这一页的标题和界面文字都会跟着变。",
      },
    ),
  );
  personaSection._fields.appendChild(
    inputField("Bot 名称", world.bot_name || "", (value) => (world.bot_name = value), {
      hint:
        "互动动作文案里的 {bot} 会替换成这个名字，例如「（小鲸鱼抱了你一下）」。留空时先用群名片原名，再回落到「她」。",
      placeholder: "例如：小鲸鱼",
    }),
  );
  personaSection._fields.appendChild(
    selectField(
      "人设来源",
      world.persona.mode || "astrbot",
      [
        { key: "astrbot", label: "跟随 AstrBot（每个会话各自的人格）" },
        { key: "plugin", label: "用下面这一份（所有会话共用）" },
        { key: "append", label: "AstrBot 那份 + 下面这一份（接在后面）" },
      ],
      (value) => {
        world.persona.mode = value;
        renderSettings();
      },
      {
        hint: "「跟随 AstrBot」= 现在的行为：换会话 / 改组时她的人设可能跟着变。",
      },
    ),
  );
  const personaText = textareaField(
    "角色卡（她是谁）",
    world.persona.text || "",
    (value) => (world.persona.text = value),
    {
      rows: 12,
      hint: "她是谁、怎么说话、在意什么。留空时自动回落到 AstrBot 那份，避免人格设定丢失。",
      placeholder: "例如：你是……（身份、说话习惯、喜欢和讨厌的事）",
    },
  );
  // 角色卡是这一页最该先看的东西：让它独占一行，别跟别的字段挤在网格里
  personaText.classList.add("full");
  const personaImport = el("button", "small ghost", "从 AstrBot 导入当前人设");
  personaImport.type = "button";
  personaImport.title = "把 AstrBot 给这个会话选的人格抄进上面的框里，之后它就跟着预设走";
  personaImport.addEventListener("click", async () => {
    try {
      // 借白名单里第一条会话去问 AstrBot 现在用哪份人格（人格一般是全局的，
      // 但 AstrBot 是"按会话 / 配置文件"解析的，所以随便挑一条能读到的）
      const session = String((ui.sessions[0] || {}).session_id || "");
      const data = await apiGet("persona-source", { session });
      const text = String(data.text || "");
      if (!text.trim()) {
        toast("AstrBot 这个会话没读到人格，先在 AstrBot 里给这个会话选一份");
        return;
      }
      world.persona.text = text;
      world.persona.mode = "plugin";
      markDirty();
      renderSettings();
      toast(`已导入 ${data.chars || text.length} 字，点右上角「保存」后生效。`);
    } catch (error) {
      toast(error.message || "读取失败");
    }
  });
  personaText.appendChild(personaImport);
  personaSection._fields.appendChild(personaText);

  /* --- 优化人设：先体检、再逐条接受（绝不自动覆盖） --- */
  const reviewFull = el("div", "full");
  const reviewRow = el("div", "row");
  const reviewButton = el("button", "small primary", "优化人设…");
  reviewButton.type = "button";
  reviewButton.title = "让生成器模型先体检这份角色卡，再把改动一条条列出来给你挑";
  const evalButton = el("button", "small ghost", "生成测评剧本…");
  evalButton.type = "button";
  evalButton.title = "写一份考卷：同一份剧本跑不同模型，盲选哪版更像她（不会进提示词）";
  const historyButton = el("button", "small ghost", "改动历史…");
  historyButton.type = "button";
  historyButton.title = "改坏了随时翻回去：每次保存 / 应用预设 / 优化人设之前都留了一版";
  const reviewNote = el("span", "muted", "先体检，再给你逐条挑；只写你点过的那些");
  reviewRow.appendChild(reviewButton);
  reviewRow.appendChild(evalButton);
  reviewRow.appendChild(historyButton);
  reviewRow.appendChild(reviewNote);
  reviewFull.appendChild(reviewRow);
  personaSection._fields.appendChild(reviewFull);
  historyButton.addEventListener("click", () => openHistoryModal());

  evalButton.addEventListener("click", async () => {
    const sessionId = $("status-session") ? $("status-session").value : "";
    let data = {};
    evalButton.disabled = true;
    reviewNote.textContent = "出题中…（要调一次生成器模型）";
    try {
      data = await apiPost("eval-script", { session: sessionId, rounds: 20 });
    } catch (error) {
      reviewNote.textContent = error.message || "生成失败";
      evalButton.disabled = false;
      return;
    }
    evalButton.disabled = false;
    reviewNote.textContent = `出了 ${data.count} 轮题，确认后开跑`;
    openEvalModal(data.rounds || []);
  });

  reviewButton.addEventListener("click", async () => {
    const sessionId = $("status-session") ? $("status-session").value : "";
    let report = {};
    reviewButton.disabled = true;
    reviewNote.textContent = "体检中…（要调一次生成器模型）";
    try {
      report = await apiPost("persona/review", { session: sessionId });
    } catch (error) {
      reviewNote.textContent = error.message || "体检失败";
      reviewButton.disabled = false;
      return;
    }
    reviewButton.disabled = false;
    reviewNote.textContent = `原文 ${report.persona_chars} 字｜${report.length_hint || ""}`;

    const lines = [];
    if ((report.ok_points || []).length) {
      lines.push("已经写得好的：");
      report.ok_points.forEach((item) => lines.push(`· ${item}`));
    }
    (report.issues || []).forEach((item) => {
      lines.push(`【${item.level}／${item.kind}】${item.detail}`);
      if (item.quote) lines.push(`    原文：${item.quote}`);
    });
    (report.questions || []).forEach((item) => lines.push(`？${item}`));
    if (!report.rewrite.length && !report.add.length) {
      openFormDialog({
        title: "体检结果",
        hint: lines.join("\n") || "没发现问题。",
        fields: [],
        confirmText: "知道了",
        onSubmit: () => true,
      });
      return;
    }
    openReviewModal(report, lines);
  });


  /* --- 声音样例：页面上只放"已采用"，候选池挪进弹窗 --- */
  const sampleFull = el("div", "full");
  sampleFull.appendChild(fieldHead("声音样例"));
  sampleFull.appendChild(
    el(
      "p",
      "muted",
      "只有下面这份「已采用」会进提示词（每轮抽 2~3 条相关的；一条都没有时整段不出现）。"
        + "候选先攒在弹窗里，挑中的才挪进来；每条都能改文字、换场景。",
    ),
  );
  const sampleList = el("div", "sample-list");
  sampleFull.appendChild(sampleList);
  const candidateRow = el("div", "row");
  const candidateOpen = el("button", "small primary", "挑选候选…");
  candidateOpen.type = "button";
  candidateOpen.title = "候选池：生成出来的、从聊天里挑出来的句子都先攒在这里";
  const candidateGenerate = el("button", "small primary", "生成候选…");
  candidateGenerate.type = "button";
  const candidateFromChat = el("button", "small", "从近期聊天挑");
  candidateFromChat.type = "button";
  candidateRow.appendChild(candidateOpen);
  candidateRow.appendChild(candidateGenerate);
  candidateRow.appendChild(candidateFromChat);
  const candidateNote = el("span", "muted", "");
  candidateRow.appendChild(candidateNote);
  sampleFull.appendChild(candidateRow);
  const sampleNote = el("span", "muted", "");
  sampleFull.appendChild(sampleNote);
  personaSection._fields.appendChild(sampleFull);

  ui.voiceSamples = [];
  ui.voiceCandidates = [];
  ui.voiceScenes = [];
  ui.voiceMax = 12;
  ui.voiceCandidatesMax = 80;
  const voicePicked = new Set();

  function voiceSceneLabel(scene) {
    const hit = ui.voiceScenes.find((one) => one.id === scene);
    return hit ? String(hit.label).split("：")[0] : "";
  }

  function renderVoiceSummary() {
    candidateNote.textContent =
      `候选池 ${ui.voiceCandidates.length}/${ui.voiceCandidatesMax} 条`;
  }

  /** 候选池弹窗：勾选要采用的，✕ 从候选里删掉。 */
  function openCandidatePicker() {
    openCustomDialog({
      title: "候选池",
      hint: "勾中的会加进「已采用」；✕ 是从候选里删掉。候选池里的句子不进提示词。",
      confirmText: "采用选中",
      build: (body) => {
        const list = el("div", "sample-list");
        body.appendChild(list);
        const foot = el("div", "row");
        const note = el("span", "muted", "");
        const clear = el("button", "small ghost", "清空候选池");
        clear.type = "button";
        clear.addEventListener("click", async () => {
          if (!ui.voiceCandidates.length) return;
          const yes = await confirmDialog({
            title: "清空候选池",
            message: `确定要删掉全部 ${ui.voiceCandidates.length} 条候选吗？（已采用的不受影响）`,
            confirmText: "清空",
          });
          if (!yes) return;
          ui.voiceCandidates = [];
          voicePicked.clear();
          persistVoiceSamples();
          draw();
        });
        foot.appendChild(clear);
        foot.appendChild(note);
        body.appendChild(foot);

        function draw() {
          list.innerHTML = "";
          if (!ui.voiceCandidates.length) {
            list.appendChild(
              el(
                "p",
                "muted",
                "还没有候选。关掉这个弹窗，用「生成候选」让她按场景说几句，" +
                  "或者「从近期聊天挑」翻她自己说过的话。",
              ),
            );
          }
          ui.voiceCandidates.forEach((item) => {
            const row = el("div", "sample-item");
            const head = el("div", "sample-item-head");
            const box = document.createElement("input");
            box.type = "checkbox";
            box.checked = voicePicked.has(item.id);
            box.addEventListener("change", () => {
              if (box.checked) voicePicked.add(item.id);
              else voicePicked.delete(item.id);
              draw();
            });
            head.appendChild(box);
            const label = voiceSceneLabel(item.scene);
            if (label) head.appendChild(el("span", "tag", label));
            if (item.source === "chat") head.appendChild(el("span", "tag", "来自聊天"));
            const del = el("button", "icon-btn", "✕");
            del.type = "button";
            del.title = "从候选里删掉这一条";
            del.addEventListener("click", () => {
              ui.voiceCandidates = ui.voiceCandidates.filter((one) => one !== item);
              voicePicked.delete(item.id);
              persistVoiceSamples();
              draw();
            });
            head.appendChild(del);
            row.appendChild(head);
            row.appendChild(el("div", "sample-item-text", item.text));
            if (item.context) {
              row.appendChild(el("div", "muted", `当时对方说：${item.context}`));
            }
            list.appendChild(row);
          });
          note.textContent =
            `${ui.voiceCandidates.length}/${ui.voiceCandidatesMax} 条` +
            (voicePicked.size ? `，选了 ${voicePicked.size} 条` : "");
        }
        draw();
      },
      onSubmit: async () => {
        const picked = ui.voiceCandidates.filter((item) => voicePicked.has(item.id));
        if (!picked.length) {
          $("dialog-error").textContent = "先勾几条要采用的";
          return false;
        }
        const room = Math.max(0, ui.voiceMax - ui.voiceSamples.length);
        if (picked.length > room) {
          $("dialog-error").textContent = `样例库最多 ${ui.voiceMax} 条，还能再加 ${room} 条`;
          return false;
        }
        adoptCandidates(picked);
        persistVoiceSamples();
        toast(`已采用 ${picked.length} 条，点右上角「保存」后生效。`);
        return true;
      },
    });
  }

  function adoptCandidates(picked) {
    picked.forEach((item) => {
      ui.voiceSamples.push({
        id: `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`,
        scene: item.scene || "",
        label: item.label || "",
        move: item.move || "",
        text: item.text,
        source: item.source || "model",
      });
    });
    const takenIds = new Set(picked.map((item) => item.id));
    ui.voiceCandidates = ui.voiceCandidates.filter((one) => !takenIds.has(one.id));
    voicePicked.clear();
  }

  /** 改一条已采用的样例：文字 / 场景 / 动作说明。 */
  function editVoiceSample(item) {
    openFormDialog({
      title: "编辑样例",
      hint: "这一句会作为她的口吻样本，宁缺毋滥；不宜写成指定她照说的台词。",
      fields: [
        { key: "text", label: "内容", type: "textarea", rows: 3, value: item.text || "" },
        {
          key: "scene",
          label: "场景（决定什么时候抽到它）",
          type: "select",
          value: item.scene || "",
          options: [
            { value: "", label: "不限（任何时候都可能抽到）" },
            ...ui.voiceScenes.map((one) => ({ value: one.id, label: one.label })),
          ],
        },
        {
          key: "move",
          label: "动作说明（可留空）",
          type: "text",
          value: item.move || "",
          placeholder: "例如：把脸埋进抱枕里",
        },
      ],
      confirmText: "保存",
      onSubmit: async (values) => {
        const text = String(values.text || "").trim();
        if (!text) {
          $("dialog-error").textContent = "内容不能为空";
          return false;
        }
        item.text = text.slice(0, 120);
        item.scene = String(values.scene || "");
        item.move = String(values.move || "").trim().slice(0, 20);
        item.source = item.source || "model";
        persistVoiceSamples();
        toast("已修改，点右上角「保存」后生效。");
        return true;
      },
    });
  }

  function renderVoiceSamples() {
    sampleList.innerHTML = "";
    if (!ui.voiceSamples.length) {
      sampleList.appendChild(
        el("p", "muted", "还没有样例。点上面的「挑选候选…」选几条「采用」，它们才会进提示词。"),
      );
    }
    ui.voiceSamples.forEach((item) => {
      const row = el("div", "sample-item");
      const head = el("div", "sample-item-head");
      const label = voiceSceneLabel(item.scene) || item.label || "样例";
      head.appendChild(el("span", "tag", String(label).split("：")[0]));
      if (item.move) head.appendChild(el("span", "muted", item.move));
      const edit = el("button", "icon-btn", "✎");
      edit.type = "button";
      edit.title = "改这一条的文案 / 场景 / 动作说明";
      edit.addEventListener("click", () => editVoiceSample(item));
      head.appendChild(edit);
      const del = el("button", "icon-btn", "✕");
      del.type = "button";
      del.title = "删掉这一条";
      del.addEventListener("click", () => {
        ui.voiceSamples = ui.voiceSamples.filter((one) => one !== item);
        persistVoiceSamples();
        renderVoiceSamples();
      });
      head.appendChild(del);
      row.appendChild(head);
      row.appendChild(el("div", "sample-item-text", item.text));
      sampleList.appendChild(row);
    });
    sampleNote.textContent = `${ui.voiceSamples.length}/${ui.voiceMax} 条`;
    renderVoiceSummary();
  }

  /**
   * 样例和候选池跟着**设置页那份内存配置**走，和别的字段一样：改完标脏，点保存才落盘。
   *
   * 以前这里直接调 `voice-samples/save` 写盘，编辑器的内存却不知道——随后点右上角
   * 「保存」就把旧的那份覆盖回去了，于是"挑完候选一保存就没了"。
   */
  function persistVoiceSamples() {
    world.persona = world.persona || {};
    world.persona.samples = ui.voiceSamples;
    world.persona.sample_candidates = ui.voiceCandidates;
    markDirty();
  }

  function pushCandidates(items, source) {
    const seen = new Set(ui.voiceCandidates.map((one) => String(one.text || "")));
    (items || []).forEach((item) => {
      const text = String(item.text || "").trim();
      if (!text || seen.has(text)) return;
      seen.add(text);
      ui.voiceCandidates.push({
        id: `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`,
        scene: item.scene || "",
        label: item.label || "",
        move: item.move || "",
        text,
        context: item.context || "",
        source: item.source || source,
      });
    });
    const over = ui.voiceCandidates.length - ui.voiceCandidatesMax;
    if (over > 0) ui.voiceCandidates = ui.voiceCandidates.slice(over);
    renderVoiceSummary();
  }

  async function generateVoiceSamples() {
    const sessionId = $("status-session") ? $("status-session").value : "";
    if (!sessionId) {
      toast("先在「实时状态」里选一个会话（她要拿这个会话的角色卡去写）");
      return;
    }
    let added = 0;
    // 生成要调一次模型，几秒到几十秒：**弹窗等生成完再关**，
    // 出错也留在弹窗里显示（渲染在 #dialog-error），不然看着就是"点了没反应"。
    await openFormDialog({
      title: "生成声音样例",
      hint: "选择若干场景，每个场景生成三条不同说法。结果先放入候选池，可再逐条挑选。",
      fields: [
        {
          key: "scenes",
          label: "场景",
          type: "checkboxes",
          value: ["tease", "snapped", "ignored", "night", "cant", "boundary"].filter(
            (key) => ui.voiceScenes.some((one) => one.id === key),
          ),
          options: ui.voiceScenes.map((one) => ({ value: one.id, label: one.label })),
        },
      ],
      confirmText: "生成",
      onSubmit: async (values) => {
        if (!(values.scenes || []).length) {
          const box = $("dialog-error");
          if (box) box.textContent = "至少选一个场景";
          return false;
        }
        const restore = busyButton($("dialog-ok"), "生成中…（要调一次生成模型）");
        try {
          const data = await apiPost("voice-samples/generate", {
            session: sessionId,
            scenes: values.scenes,
            // 同上：拿编辑器里这份角色卡（向导里可能还没保存）
            persona: ((ui.config.world || {}).persona || {}).text || "",
          });
          const items = [];
          (data.scenes || []).forEach((group) => {
            (group.candidates || []).forEach((cand) => {
              items.push({
                text: cand.text,
                move: cand.move || "",
                scene: group.scene,
                label: group.label,
                source: "model",
              });
            });
          });
          if (!items.length) {
            throw new Error("模型没给出可用的候选，可以再点一次，或者换个「内容生成模型」");
          }
          pushCandidates(items, "model");
          persistVoiceSamples();
          added = items.length;
          return true;
        } finally {
          restore();
        }
      },
    });
    if (added) {
      toast(`候选池新增 ${added} 条，可在「候选池」中挑选，点右上角「保存」后生效。`);
    }
  }

  async function pickCandidatesFromChat() {
    const sessionId = $("status-session") ? $("status-session").value : "";
    if (!sessionId) {
      toast("先在状态页选一个会话");
      return;
    }
    const restore = busyButton(candidateFromChat, "翻聊天记录…");
    let data = {};
    try {
      data = await apiPost("voice-samples/from-chat", { session: sessionId, limit: 12 });
    } catch (error) {
      restore();
      toast(error.message || "挑不出来");
      return;
    }
    restore();
    const found = data.candidates || [];
    if (!found.length) {
      toast(data.note || "这段时间没挑出合适的");
      return;
    }
    pushCandidates(found, "chat");
    persistVoiceSamples();
    toast(`已从聊天中挑出 ${found.length} 条候选，点右上角「保存」后生效。`);
  }

  candidateGenerate.addEventListener("click", generateVoiceSamples);
  candidateFromChat.addEventListener("click", pickCandidatesFromChat);
  candidateOpen.addEventListener("click", openCandidatePicker);
  // 向导要用同一套流程：生成的结果一样进候选池（走内存，点保存才落盘）
  ui.generateVoiceSamples = generateVoiceSamples;

  /**
   * 样例与候选池以**编辑器内存里的配置**为准（跟别的设置一样，点保存才落盘）；
   * 接口只用来拿"有哪些场景 / 上限多少"这类只读信息。
   */
  async function loadVoiceSamples() {
    const persona = world.persona || (world.persona = {});
    ui.voiceSamples = Array.isArray(persona.samples) ? persona.samples : [];
    ui.voiceCandidates = Array.isArray(persona.sample_candidates)
      ? persona.sample_candidates
      : [];
    renderVoiceSamples();
    try {
      const data = await apiGet("voice-samples");
      ui.voiceScenes = data.scenes || [];
      ui.voiceMax = Number(data.max || 12);
      ui.voiceCandidatesMax = Number(data.candidates_max || 80);
      renderVoiceSummary();
    } catch (error) {
      sampleNote.textContent = `读取失败：${error.message || error}`;
    }
  }
  loadVoiceSamples();

  form.appendChild(personaSection);

  /* --- 她的基础状态 --- */
  const stateSection = settingsSection(
    "她的初始状态",
    "刚启用插件时她的状态（0~1，0.5 是中间值）。",
    "数值会随时间自然变化，这里只决定起点。",
  );
  ATTRS.forEach((attr) => {
    stateSection._fields.appendChild(
      inputField(
        attr.label,
        num(world.default_state[attr.key], 0.5),
        (value) => (world.default_state[attr.key] = num(value, 0.5)),
        { hint: attr.hint, type: "number", step: "0.05", min: 0, max: 1 },
      ),
    );
  });
  stateSection._fields.appendChild(
    inputField("初始心情", world.default_state.mood || "平静", (value) => (world.default_state.mood = value), {
      hint: "会随数值变化自动推导出心情，这里只是起点。",
    }),
  );
  // 初始状态归「世界与生活」，不再留在人设页
  if (worldBase && worldBase.parentElement === form) {
    worldBase.insertAdjacentElement("afterend", stateSection);
  } else {
    form.appendChild(stateSection);
  }

  /* --- 她的本事（能力值） --- */
  const abilitySection = settingsSection(
    "她的本事（能力值）",
    "体力 / 智力 / 灵巧 / 心性：她遇上事时用来判断「这件事她做得到吗」，0~1。",
    "这四项是慢变量：只由事件结果改变，睡一觉不会回来。" +
      "精力管「今天累不累」，能力值管「她平时是什么底子」。",
  );
  const abilityFull = el("div", "full");
  abilityFull.appendChild(
    checkboxField(
      "启用能力值",
      world.abilities.enabled !== false,
      (value) => (world.abilities.enabled = value),
      { hint: "关闭后判定一律用固定值，能力值不变、也不写进提示词。" },
    ),
  );
  abilitySection._fields.appendChild(abilityFull);
  [
    ["stamina", "体力", "力气活、熬夜、跑腿"],
    ["wits", "智力", "想办法、应付复杂局面"],
    ["dexterity", "灵巧", "手工、做饭、手稳不稳"],
    ["composure", "心性", "扛压力、忍住不炸"],
  ].forEach(([key, label, hint]) => {
    abilitySection._fields.appendChild(
      inputField(
        label,
        world.abilities[key] ?? 0.6,
        (value) => (world.abilities[key] = Number(value) || 0),
        { hint: `${hint}。新会话从这个值开始，0~1。`, type: "number", min: "0.05", max: "1", step: "0.05" },
      ),
    );
  });
  abilitySection._fields.appendChild(
    inputField(
      "单次变化上限",
      world.abilities.step_limit ?? 0.05,
      (value) => (world.abilities.step_limit = Number(value) || 0),
      { hint: "一件事最多让某项能力值变化多少，默认 0.05。防止一件事就让她脱胎换骨。", type: "number", min: "0.01", max: "0.2", step: "0.01" },
    ),
  );
  abilitySection._fields.appendChild(
    inputField(
      "每日变化上限",
      world.abilities.daily_limit ?? 0.1,
      (value) => (world.abilities.daily_limit = Number(value) || 0),
      { hint: "同一项一天里累计最多变化多少，默认 0.1。数值膨胀是这类系统最容易崩的地方。", type: "number", min: "0.01", max: "1", step: "0.01" },
    ),
  );
  abilitySection._fields.appendChild(
    checkboxField(
      "失败涨经验",
      world.abilities.fail_growth !== false,
      (value) => (world.abilities.fail_growth = value),
      { hint: "失败比成功更容易长能力值——这是「失败了还能再试」的依据，不是靠文案鼓励。", },
    ),
  );
  form.appendChild(abilitySection);

  /* --- 状态变化速度 --- */
  const dynamics = settingsSection(
    "状态变化速度",
    "数值每分钟变化多少。默认值下，精力大约 11 小时掉到 0，孤独大约 20 小时涨满，心潮大约 50 分钟回落干净；"
      + "好奇心过了 0.7 之后涨得越来越慢，不会一路顶死在满值。",
    "觉得她太爱睡觉就调小精力衰减；觉得她太黏人就调小孤独增长；觉得她老跑去上网查东西就调小好奇增长。",
  );
  // 边界按字段语义给，不搞一刀切：变化速度（每分钟/每小时涨落多少）封到 0.5，
  // 比率与阈值（0~1 之间才有意义的那几个）封到 1；越界值写不进去，省得一个手滑
  // 把「每分钟涨 3 点」存下来——那会让她一分钟内从平静冲到满值。
  [
    ["energy_decay_per_min", "精力衰减/分钟", "越大越容易累", 0, 0.5],
    ["loneliness_growth_per_min", "孤独增长/分钟", "越大越容易想找人", 0, 0.5],
    ["curiosity_growth_per_min", "好奇增长/分钟", "越大越想上网查东西", 0, 0.5],
    ["affect_decay_per_min", "心潮回落/分钟", "越大情绪平复得越快（默认 0.02 ≈ 50 分钟从满值回到平静）", 0, 0.5],
    ["valence_decay_per_min", "效价回落/分钟", "越大心情平复得越快（默认 0.014 ≈ 50 分钟回落一半）", 0, 0.5],
    ["chat_valence_cap", "聊天单轮最多推动效价", "一轮聊天最多让心情变化多少。调大她会因为几句夸奖就明显开心；默认 0.05 偏向「心情靠经历，不靠嘴甜」", 0, 1],
    ["chat_valence_daily_cap", "聊天每天最多推动效价", "日常聊天一天最多把心情推多少（正负各算一份）。填 0 = 不限制；日常陪伴改的是好感度，不是心情的量程", 0, 1],
    ["boredom_growth_per_min", "无聊增长/分钟", "越大越想换地方", 0, 0.5],
    ["sleep_energy_recovery_per_min", "睡觉恢复精力/分钟", "越大睡一觉回得越多", 0, 0.5],
    ["nap_energy_recovery_per_min", "小睡恢复精力/分钟", "小睡时的恢复速度", 0, 0.5],
    ["sleep_curiosity_decay_per_min", "睡觉时好奇回落/分钟", "睡一觉把昨天攒的好奇放下（默认 0.0012 ≈ 整觉降 0.58）", 0, 0.5],
    ["sleep_curiosity_floor", "睡醒时最低的好奇", "睡一觉最多把好奇心压到这儿：醒来还是会对新鲜事感兴趣", 0, 1],
    ["atmosphere_multiplier", "地点氛围影响强度", "0 表示地点氛围完全不影响数值", 0, 1],
    ["desire_growth_per_min", "欲求增长/分钟", "越大越想要人碰：默认约三天攒满；累着或心情差时涨得更慢", 0, 0.5],
    ["desire_relief", "被亲近一次降多少", "再乘动作自己的「亲密程度」：抱一下比拍拍肩解渴", 0, 0.5],
    ["desire_tease", "被撩一下涨多少", "只是嘴上撩、没真碰到：再乘关系亲疏，越亲近越管用", 0, 0.5],
    ["desire_sleep_fall_per_hour", "睡觉时欲求回落/小时", "睡一觉起来没那么憋", 0, 0.5],
    ["desire_wake_keep", "睡醒时保留多少", "0~1，睡醒后欲求乘这个系数", 0, 1],
    ["desire_soft_top", "欲求涨到多少后减半", "过了这条线涨速减半，免得一直吊在顶上", 0, 1],
  ].forEach(([key, label, hint, low, high]) => {
    dynamics._fields.appendChild(
      inputField(label, num(world.state_dynamics[key]), (value) => (world.state_dynamics[key] = num(value)), {
        hint,
        type: "number",
        min: low,
        max: high,
        step: "0.0001",
      }),
    );
  });
  dynamics._fields.appendChild(el("div", "sub-title", "今天的基调"));
  dynamics._fields.appendChild(
    checkboxField(
      "每天掷一次今天的基调",
      world.state_dynamics.daily_mood_enabled !== false,
      (value) => {
        world.state_dynamics.daily_mood_enabled = value;
      },
      {
        hint: "每天掷一次：懒散 / 活跃 / 黏人 / 想独处 / 说不上来。只改数值涨落快慢，不改性格。",
      },
    ),
  );
  dynamics._fields.appendChild(
    inputField(
      "基调的作用强度",
      num(world.state_dynamics.daily_mood_strength, 1),
      (value) => (world.state_dynamics.daily_mood_strength = num(value, 1)),
      {
        hint: "0~1。0 = 照样掷、也照样显示，但完全不影响数值；1 = 全量生效。",
        type: "number",
        min: "0",
        max: "1",
        step: "0.1",
      },
    ),
  );
  form.appendChild(dynamics);

  /* --- 说话频率 --- */
  const knobs = settingsSection(
    "性格与习惯",
    "她主动找你的频率、群里接话的意愿、一次说几句、私聊里粘不粘人。",
    "先选一个整体感觉，想细调再打开完整面板。这里改的是全局配置，所有会话共用。",
  );
  knobs._fields.appendChild(handfeelCard(world));
  form.appendChild(knobs);

  const limits = settingsSection(
    "说话频率与预算",
    "控制她多久主动说一次话、每次最多说几句，以及大模型的调用预算。",
    "空群里最容易出问题的就是「自言自语刷屏」，这里的上限就是干这个用的。",
  );
  // 每一项都要有下限和上限（用户 m00001：以前只有 type=number，能填 -5 或 99999）。
  // 口径：次数类一律 0~200（后端把每小时上限当"别刷屏"的闸门，再大就等于没设）；
  // 秒数类的最大档是「一天」= 86400，够表示"基本不限"，又不至于写成天文数字。
  [
    ["max_autonomous_per_hour", "每小时最多自主行动次数", "包括主动搭话、去搜索、换地方", 200],
    ["max_share_per_hour", "每小时最多分享次数", "「分享见闻」这类动作的上限", 200],
    [
      "max_replies_per_hour",
      "每小时最多被动回复次数",
      "被 @ 到 / 私聊 / 明确对她说时给的回复；超过之后这一条不回（仍然由本插件接管，不落回主人格）。默认 200，填 0 = 完全不限制",
      200,
    ],
    ["max_messages_per_say", "每次最多说几句", "一次回复拆成几条消息发送", 200],
    ["max_actions_per_message", "一次最多执行几个动作", "一条消息里允许的动作数量", 200],
    [
      "reply_round_budget_seconds",
      "一次回复的总时限（秒）",
      "从等安静期开始算：到点就不再为刚到的消息重来一版，手上这版照发。默认 120，0 = 不限",
      86400,
    ],
    ["plan_valid_duration", "计划有效期（秒）", "一次 LLM 计划覆盖多长时间，默认 1800（30 分钟）", 86400],
    ["max_action_chain_depth", "动作链最大深度", "防止动作套动作无限循环", 200],
    ["llm_plan_min_interval_seconds", "问计划的间隔（秒）", "两次「问大模型要计划」之间至少隔多久，默认 900", 86400],
    ["max_llm_plan_per_hour", "每小时最多计划决策次数", "降低 token 消耗", 200],
    ["max_llm_text_per_hour", "每小时最多生成发言次数", "超出后她这一轮保持安静（不会再退到一句写死的通用台词）", 200],
    [
      "max_arrival_decisions_per_hour",
      "每小时最多抵达决策次数",
      "她走到一个新地方后立刻做一次决策的上限；这类决策不占自主行动额度，单独限流",
      200,
    ],
    [
      "max_tool_param_per_hour",
      "每小时最多补全工具参数次数",
      "「把意图翻译成工具参数」的辅助模型调用上限，超出后这类动作会跳过",
      200,
    ],
    ["forced_plan_min_interval_seconds", "极端保护最短间隔（秒）", "强制睡觉/强制找人这类兜底行为的最小间隔", 86400],
  ].forEach(([key, label, hint, high]) => {
    limits._fields.appendChild(
      inputField(label, num(world.limits[key]), (value) => (world.limits[key] = num(value)), {
        hint,
        type: "number",
        min: 0,
        max: high,
        step: "1",
      }),
    );
  });
  form.appendChild(limits);

  /* --- 说话节奏 --- */
  const style = settingsSection(
    "说话节奏",
    "她一句一句发消息时的停顿，以及「最近话太密」的判定。",
    "群聊分段回复若同时出现，容易被看出是程序生成；按字数延迟更接近真人打字的节奏。",
  );
  style._fields.appendChild(
    checkboxField(
      "让心情影响这一轮的说话形态",
      world.style_injection !== false,
      (value) => (world.style_injection = value),
      {
        hint: "开启后提示词多一段「这一轮的表达方式」：由心潮 × 效价决定条数、长度与是否用动作代替说话。",
      },
    ),
  );
  style._fields.appendChild(
    checkboxField(
      "分段回复之间模拟打字停顿",
      world.reply_style.typing_delay_enabled !== false,
      (value) => (world.reply_style.typing_delay_enabled = value),
      {
        hint:
          "她一次说好几句时，两条之间会按上一句的字数等一会儿再发。关掉就是几条消息一起冒出来。",
      },
    ),
  );
  style._fields.appendChild(
    inputField(
      "每个字停顿（秒）",
      num(world.reply_style.typing_delay_per_char, 0.16),
      (value) => (world.reply_style.typing_delay_per_char = num(value, 0.16)),
      {
        hint: "默认 0.03 秒/字：一句话 20 个字大概等 0.6 秒。",
        type: "number",
        step: "0.005",
        min: 0,
      },
    ),
  );
  style._fields.appendChild(
    inputField(
      "单条最多停顿（秒）",
      num(world.reply_style.typing_delay_max, 5),
      (value) => (world.reply_style.typing_delay_max = num(value, 5)),
      {
        hint: "上限。长句子不会一直等下去，默认 2.5 秒。",
        type: "number",
        step: "0.5",
        min: 0,
      },
    ),
  );
  // 引用档位已经去掉（用户 m09933）：不默认引用，只有她自己在说那句话时
  // 写了方括号编号才引用，所以这里没有开关可调了。
  style._fields.appendChild(
    checkboxField(
      "新消息打断还没回完的回复",
      world.reply_style.interrupt_pending !== false,
      (value) => (world.reply_style.interrupt_pending = value),
      {
        hint: "生成回复期间又来新消息：本次生成作废并重来，新消息仍能带上未回复的那几条。",
      },
    ),
  );
  style._fields.appendChild(
    inputField(
      "回答前先等几秒（安静期）",
      world.reply_style.merge_wait_seconds ?? 5,
      (value) => (world.reply_style.merge_wait_seconds = num(value, 5)),
      {
        hint:
          "收到消息先等这么久再开口：有新消息就重新计时，连着打字就等他说完一起回。" +
          "0 = 立即回答。",
        type: "number",
        step: "0.5",
        min: 0,
      },
    ),
  );
  style._fields.appendChild(
    inputField(
      "安静期最长等多久（秒）",
      world.reply_style.merge_wait_max_seconds ?? 30,
      (value) => (world.reply_style.merge_wait_max_seconds = num(value, 30)),
      {
        hint: "一直有新消息时也不会等超过这么久，免得永远不开口。",
        type: "number",
        step: "1",
        min: 0,
      },
    ),
  );
  style._fields.appendChild(
    inputField(
      "群里 @ 她时先等几秒",
      num(world.reply_style.mention_wait_seconds, 3),
      (value) => (world.reply_style.mention_wait_seconds = num(value, 3)),
      {
        hint:
          "群里真的 @ 了她时用的安静期，比上面那项短：" +
          "要留出对方把一句打完的时间。0 = 收到就答。",
        type: "number",
        step: "0.5",
        min: 0,
      },
    ),
  );
  style._fields.appendChild(
    inputField(
      "@ 时最多等几秒",
      num(world.reply_style.mention_wait_max_seconds, 10),
      (value) => (world.reply_style.mention_wait_max_seconds = num(value, 10)),
      {
        hint: "对方一直补话时，@ 这一轮的硬顶。0 = 只按上面那一项等，不再延长。",
        type: "number",
        step: "1",
        min: 0,
      },
    ),
  );
  style._fields.appendChild(
    inputField(
      "说话密度统计窗口（分钟）",
      num(world.reply_style.dense_window_minutes, 10),
      (value) => (world.reply_style.dense_window_minutes = num(value, 10)),
      {
        hint: "统计「她最近说了多少句」的时间范围。",
        type: "number",
        min: 1,
      },
    ),
  );
  style._fields.appendChild(
    inputField(
      "窗口内说几句算太密",
      num(world.reply_style.dense_max_lines, 4),
      (value) => (world.reply_style.dense_max_lines = num(value, 4)),
      {
        hint:
          "超过这个句数，提示词里会提醒她「这轮少说话、多做动作」。她仍然可以开口，只是会被提示收敛。",
        type: "number",
        min: 1,
      },
    ),
  );
  form.appendChild(style);

  /* --- 追问 --- */
  world.follow_up = world.follow_up || {};
  const followUp = settingsSection(
    "追问",
    "她问完在等你回话，隔几分钟没人接就再问一句。",
    "要不要等由模型自己写在回话里（不再按问号猜）；到点时它还会再看一眼——" +
      "把已经过了多久、追过几次都告诉它，由它判断还值不值得等。你回话、或它觉得不用等了，等待就取消。" +
      "默认等 10 分钟、最多问 1 次。",
    "follow_up",
  );
  followUp._fields.appendChild(
    checkboxField(
      "她问完没回音时追一句",
      world.follow_up.enabled !== false,
      (value) => (world.follow_up.enabled = value),
      {
        hint: "关掉就是问完不管：没人接她也不催。",
      },
    ),
  );
  followUp._fields.appendChild(
    inputField(
      "等多久没回就追（分钟）",
      num(world.follow_up.wait_minutes, 10),
      (value) => (world.follow_up.wait_minutes = num(value, 10)),
      {
        hint: "从她说出那句话算起。太短像是在催，太长她自己也忘了。",
        type: "number",
        min: 0.5,
        step: "0.5",
      },
    ),
  );
  followUp._fields.appendChild(
    inputField(
      "最多追几次",
      num(world.follow_up.max_times, 1),
      (value) => (world.follow_up.max_times = num(value, 1)),
      {
        hint: "追满这么多次还没人接，这件事她就放下了。",
        type: "number",
        min: 1,
        max: 5,
        step: "1",
      },
    ),
  );
  form.appendChild(followUp);

  /* --- 私聊 --- */
  world.profile = world.profile || {};
  const dm = settingsSection(
    "私聊",
    "她想跟某个人单独说两句时，可以直接去私聊，不用你先把它加进会话列表。",
    "以前她只能对「会话页里配好的地方」说话，所以「去私聊一个新朋友」永远发不出去——" +
      "模型写了目标也被解析回当前会话。现在通讯录里有过往来的人都能作为落点：" +
      "已配好的私聊直接用，没配过的按平台拼一个（例如 mika:FriendMessage:12345）。" +
      "拼错、或者平台不让陌生人私聊时会发送失败——那种情况她会在**原会话**说一句" +
      "「刚才想跟 X 单独说两句，消息没发出去」，不会静默。",
    "dm",
  );
  dm._fields.appendChild(
    checkboxField(
      "允许她主动私聊没配过的人",
      world.profile.dm_send_enabled !== false,
      (value) => (world.profile.dm_send_enabled = !!value),
      {
        hint: "关掉就只对会话页里配好的地方说话（老行为）。",
      },
    ),
  );
  dm._fields.appendChild(
    inputField(
      "每天最多主动私聊几次",
      num(world.profile.dm_daily_max, 3),
      (value) => (world.profile.dm_daily_max = num(value, 3)),
      {
        hint: "防止她一高兴挨个敲人；只算新开的私聊，回话不算。",
        type: "number",
        min: 0,
        max: 20,
        step: "1",
      },
    ),
  );
  dm._fields.appendChild(
    inputField(
      "私聊会话 id 怎么写",
      String(world.profile.dm_session_template || ""),
      (value) => (world.profile.dm_session_template = String(value || "").trim()),
      {
        hint: "留空按当前平台自动拼；平台特殊就写模板，用 {user} 代表对方号码。",
        placeholder: "例如 aiocqhttp:FriendMessage:{user}",
      },
    ),
  );
  form.appendChild(dm);

  /* --- 表达变奏 --- */
  world.variation = world.variation || {};
  const variation = settingsSection(
    "表达变奏",
    "同一件事换个说法：偶尔俏皮一点、干巴巴一点，或者故意把话说小。",
    "默认开着。开着时她聊天不会每句都一个腔调：关系够近、这一轮气氛又轻松，她偶尔换个说法；" +
      "正经事、气氛紧张、或者你们还不熟，她会照直说，不会为了一句俏皮话硬凹。" +
      "效果就体现在聊天记录里——嫌她太贫或者太木，调下面的「变奏频率」就行。",
  );
  variation._fields.appendChild(
    checkboxField(
      "允许她换一种说法",
      world.variation.enabled === true,
      (value) => (world.variation.enabled = value),
      {
        hint:
          "关系够近、这一轮气氛又轻松时，她偶尔把同一个意思换个说法；正经事一律直答。",
      },
    ),
  );
  variation._fields.appendChild(
    inputField(
      "变奏频率",
      num(world.variation.rate, 0.15),
      (value) => (world.variation.rate = num(value, 0.15)),
      {
        hint: "每一轮换说法的概率（默认 0.15）。连着几轮都在俏皮会很假，所以别调太高。",
        type: "number",
        step: "0.05",
        min: 0,
        max: 1,
      },
    ),
  );
  variation._fields.appendChild(
    inputField(
      "两次变奏至少隔几轮",
      num(world.variation.min_gap_turns, 3),
      (value) => (world.variation.min_gap_turns = num(value, 3)),
      {
        hint: "刚换过说法的这几轮先不换（默认 3 轮），避免连着俏皮。",
        type: "number",
        min: 0,
      },
    ),
  );
  variation._fields.appendChild(
    pillsField(
      "变奏力度上限",
      world.variation.magnitude || "strong",
      [
        { key: "none", label: "不发力", hint: "完全不换说法，一律直说" },
        { key: "light", label: "轻", hint: "稍微换个说法，不抢戏" },
        { key: "medium", label: "中", hint: "可以明显打趣，但仍然不改事实" },
        { key: "strong", label: "大", hint: "默认档：允许更放开，风险高的场合仍然会被拦下" },
      ],
      (value) => (world.variation.magnitude = value),
      {
        hint:
          "只是上限：关系一般或者这一轮风险偏高时，实际力度会被自动压到更小的一档。",
      },
    ),
  );
  form.appendChild(variation);

  /* --- 拐话头 --- */
  world.topic_steer = world.topic_steer || {};
  const topicSteer = settingsSection(
    "拐话头",
    "闲聊时她可能不顺着你说，而是把话拐到自己正在做的事、自己的打算、还没回的人身上。",
    "默认开着。开着时，只有闲聊/分享这类回合她才可能拐走（求助、急事、你在难受时一律照旧接话），" +
      "而且拐走之后那件事实在要接下去：到点还没接着说，下一轮会提醒她一次；再过一个窗口仍没提，就记成明确放弃，" +
      "不会只丢一句就没下文。",
  );
  topicSteer._fields.appendChild(
    checkboxField(
      "允许她拐自己的话头",
      world.topic_steer.enabled === true,
      (value) => (world.topic_steer.enabled = value),
      { hint: "关掉之后她一律接你的话，不会再拐到自己身上。" },
    ),
  );
  topicSteer._fields.appendChild(
    inputField(
      "两次拐话头至少隔几轮",
      num(world.topic_steer.cooldown_turns, 5),
      (value) => (world.topic_steer.cooldown_turns = num(value, 5)),
      {
        hint: "刚拐过这几轮先不拐（默认 5 轮），避免她每句话都在说自己的事。",
        type: "number",
        min: 0,
      },
    ),
  );
  topicSteer._fields.appendChild(
    inputField(
      "每天最多拐几次",
      num(world.topic_steer.daily_cap, 2),
      (value) => (world.topic_steer.daily_cap = num(value, 2)),
      {
        hint: "按人算，同一个人一天最多被拐走几次（默认 2 次）。",
        type: "number",
        min: 0,
      },
    ),
  );
  form.appendChild(topicSteer);

  /* --- 回复格式（精简/完整） --- */
  // 用户 2026-10-26：回话轮的 JSON 被新字段撑复杂了，性格被压平——精简只要求 request。
  // 这里只是那一个开关，读写的仍然是 world.reply_schema.legacy（老存档没有这一段就是 true）。
  world.reply_schema = world.reply_schema || {};
  const replySchema = settingsSection(
    "回复格式（精简/完整）",
    "精简：只要求她填 request，回复更短更自然；完整：额外要求 share/tease/topic/intent/check，信息更多但更长",
    "默认精简。字段越多她越像在填表：回复变长，人也跟着变平。精简只要求 request{kind, expect}，" +
      "同一份世界配置渲染出来的格式约束能少一千来字，回话更短、更像她自己。" +
      "旧输出里带着那些字段也照样解析、不会报警；想换回 2026-10-26 那版完整格式，把这个开关关掉就行。",
  );
  replySchema._fields.appendChild(
    checkboxField(
      "只要求她填 request（精简）",
      world.reply_schema.legacy !== false,
      (value) => (world.reply_schema.legacy = value),
      {
        hint: "精简：只要求 request，回话更短更自然；完整：多要五项，信息更多但更长",
      },
    ),
  );
  form.appendChild(replySchema);

  /* --- 谈资 --- */
  world.topics = world.topics || {};
  const topics = settingsSection(
    "谈资",
    "她把自己经历过的事、聊天里听到的新消息攒成一条条谈资，聊天时当话题讲出来；" +
      "同一件事在同一个会话里不会翻来覆去讲第二遍。",
    "默认开着。开着时她会挑几件跟当前话题搭得上的谈资顺口提起，一轮最多提几条、多要紧才提，" +
      "都写在下面；要不要开口还是由她自己决定——允许提不等于必须提。" +
      "效果在「事件」页的「谈资」页签里能看到：每条谈资从哪来、跟谁讲过、发过哪些话。",
  );
  topics._fields.appendChild(
    checkboxField(
      "允许她攒下自己的谈资",
      world.topics.enabled === true,
      (value) => (world.topics.enabled = value),
      { hint: "打开后，她的经历会变成一条条谈资，聊天时能当话题讲；关掉就不再提，页面也不给用。" },
    ),
  );
  topics._fields.appendChild(
    inputField(
      "一件事占住的时长（秒）",
      num(world.topics.claim_ttl_seconds, 120),
      (value) => (world.topics.claim_ttl_seconds = num(value, 120)),
      {
        hint: "她说要讲某件事时会先占住它；万一中途断了，最多占这么久就会放开。",
        type: "number",
        min: 1,
      },
    ),
  );
  topics._fields.appendChild(topicSettingControl(world, "recall_limit", "一轮最多想起几件"));
  topics._fields.appendChild(topicSettingControl(world, "proactive_limit", "一轮最多主动提几件"));
  topics._fields.appendChild(topicSettingControl(world, "proactive_min_importance", "多要紧的事才主动提"));
  topics._fields.appendChild(topicSettingControl(world, "share_purpose_weight", "分享经历目的软权重"));
  topics._fields.appendChild(topicSettingControl(world, "experience_willingness", "自然带经历的意愿（不影响明确追问）"));
  topics._fields.appendChild(topicSettingControl(world, "expression_length", "谈资展开字符建议（不是硬裁剪）"));
  form.appendChild(topics);

  /* --- 语义检索：装配状态与索引清理 --- */
  /* B1-WP4-BEGIN */
  const retrieval = settingsSection(
    "语义检索（向量索引）",
    "配好 embedding 供应商以后，“翻旧事”会走两路一起看：一路按地点和关键词精确找，" +
      "一路把候选送去比语义，两路合起来排序再交给大模型（记忆和谈资都吃这一路）；" +
      "没配就是关着的，检索还是老路子，聊天行为一点不变。",
    "向量模型就在下面选（只列宿主里真登记过的 embedding 供应商——" +
      "AstrBot 插件配置页那个下拉只列聊天供应商，选不到这里的）。" +
      "换供应商等于换向量空间：谈资的旧索引会被当成过期的，后台自动重攒，谈资本身一条不动。" +
      "记忆不用重建——它是每轮现算现比的，不落索引，换模型下一轮就按新的走。",
  );
  // 下拉是真能选的：宿主插件配置页的 `_special: select_provider` 写死了 chat_completion
  // （`dashboard/src/components/shared/ConfigItemRenderer.vue:4`），embedding 供应商在那一页
  // 根本出不来（用户 2026-10-20 反馈）。这里走 `embedding/providers` + `embedding/provider`
  // 两个端点，选完写回插件配置并立即重新装配。
  const retrievalPicker = el("label");
  retrievalPicker.appendChild(
    fieldHead(
      "换一个向量模型",
      "选完立刻生效；宿主里还没建 embedding 供应商的话，先去 AstrBot 的「服务提供商 → 嵌入」加一个。",
    ),
  );
  const retrievalSelect = document.createElement("select");
  retrievalPicker.appendChild(retrievalSelect);
  retrieval._fields.appendChild(retrievalPicker);

  const retrievalBox = el("div", "status-box");
  const retrievalProvider = statusLine(retrievalBox, "当前供应商", "正在读…");
  const retrievalIndexed = statusLine(retrievalBox, "已索引", "正在读…");
  const retrievalPending = statusLine(retrievalBox, "待索引", "正在读…");
  const retrievalSpace = statusLine(retrievalBox, "向量空间", "正在读…");
  const retrievalError = statusLine(retrievalBox, "最近错误", "正在读…");
  // 重建结果单独一行（刷新状态时**不清掉**它：`已索引/待索引` 只说明当前口径，重建的
  // added/cleared 是"这一次点了按钮发生了什么"，混在一起看会以为索引没建）。
  const retrievalRescanResult = statusLine(retrievalBox, "上次重建", "还没重建过");
  retrieval._fields.appendChild(retrievalBox);
  const setRetrievalValue = (row, value) => {
    const node = row.querySelector(".status-val");
    if (node) node.textContent = value;
  };

  const retrievalActions = el("div", "row-item");
  const retrievalClear = el("button", "danger small", "清空索引");
  retrievalClear.type = "button";
  retrievalClear.title = "只清排队记录和向量，谈资本身一条不动";
  retrievalClear.addEventListener("click", async () => {
    const session = $("status-session") ? $("status-session").value : "";
    const scopeText = session
      ? "只清当前选中的会话组的索引"
      : "当前没选会话组，会清掉全部会话组的索引";
    // 二次确认：清索引不可撤回，先说清楚清什么、留什么
    const sure = await confirmDialog({
      title: "清空语义索引",
      message:
        `谈资本身一条都不会少：只清掉「待索引」的排队记录和已经建好的向量，` +
        `之后会重新慢慢索引一遍。${scopeText}。`,
      confirmText: "清空索引",
    });
    if (!sure) return;
    try {
      const result = await apiPost("embedding/index/clear", { scope_id: session });
      toast(
        `清空好了：队列 ${num(result.cleared_queue)} 条、向量 ${num(result.cleared_vectors)} 行`,
      );
      await refreshRetrievalStatus();
    } catch (error) {
      toast(error.message || "清空失败");
    }
  });
  retrievalActions.appendChild(retrievalClear);

  // 「重建索引」：把缺向量的谈资重新排队。和「清空索引」不同，它**只加不减**——老谈资
  // （早于索引功能建的、或者换过向量空间的）靠它才补得上向量，慢是慢在后台一格格算。
  const retrievalRescan = el("button", "small", "重建索引");
  retrievalRescan.type = "button";
  retrievalRescan.title = "把缺向量的谈资重新排队，后台慢慢算，谈资本身不动";
  retrievalRescan.addEventListener("click", async () => {
    const session = $("status-session") ? $("status-session").value : "";
    retrievalRescan.disabled = true;
    try {
      const result = await apiPost("embedding/index/rescan", { scope_id: session });
      const text = result.reason
        ? String(result.reason)
        : `排上 ${num(result.added)} 条，清掉过期 ${num(result.cleared)} 条`;
      setRetrievalValue(retrievalRescanResult, text);
      toast(
        result.reason
          ? text
          : `重建好了：新排队 ${num(result.added)} 条、清掉过期 ${num(result.cleared)} 条，` +
            `向量会在后台慢慢算`,
      );
      await refreshRetrievalStatus();
    } catch (error) {
      setRetrievalValue(retrievalRescanResult, error.message || "重建失败");
      toast(error.message || "重建失败");
    } finally {
      retrievalRescan.disabled = false;
    }
  });
  retrievalActions.appendChild(retrievalRescan);

  // 「重建记忆向量」：记忆的向量是按轮现算 + 缓存，换了模型 = 换了向量空间，旧空间的缓存
  // 下次召回不会命中、留着只占地方。这个按钮只清“不属于当前空间”的行，下一轮自然按新
  // 模型算；没换模型时它几乎什么都不动，所以可以随便点。
  const retrievalMemoryRebuild = el("button", "small", "重建记忆向量");
  retrievalMemoryRebuild.type = "button";
  retrievalMemoryRebuild.title =
    "清掉不属于当前向量空间的记忆向量缓存（记忆本身不动），顺手把剩下的记忆预热一遍";
  retrievalMemoryRebuild.addEventListener("click", async () => {
    retrievalMemoryRebuild.disabled = true;
    try {
      const result = await apiPost("embedding/memory/rebuild", {});
      const text = result.reason
        ? String(result.reason)
        : `清掉旧空间 ${num(result.cleared)} 行，当前空间还有 ${num(result.remaining)} 行`;
      setRetrievalValue(retrievalRescanResult, text);
      toast(result.reason ? text : `记忆向量：${text}`);
      await refreshRetrievalStatus();
      // 「装完就预热」：清完顺手把已有记忆的向量算好，进度在这里一直更新到跑完
      if (result.warm && result.warm.started) {
        watchMemoryWarm();
      } else if (result.warm && result.warm.reason) {
        setRetrievalValue(retrievalRescanResult, `${text}；预热没跑：${result.warm.reason}`);
      }
    } catch (error) {
      setRetrievalValue(retrievalRescanResult, error.message || "重建失败");
      toast(error.message || "重建失败");
    } finally {
      retrievalMemoryRebuild.disabled = false;
    }
  });

  let retrievalWarmTimer = 0;

  /** 轮询预热进度：跑完就停；页面切走或出错也停，绝不留一个永远转的定时器。 */
  function watchMemoryWarm() {
    if (retrievalWarmTimer) window.clearTimeout(retrievalWarmTimer);
    const tick = async () => {
      retrievalWarmTimer = 0;
      let state = null;
      try {
        state = await apiGet("embedding/memory/warm");
      } catch (error) {
        return;
      }
      if (!state) return;
      const warmed = num(state.warmed);
      const total = num(state.total);
      const failed = num(state.failed);
      let line = "";
      if (state.running) {
        line = `预热中：${num(state.done)}/${total || "?"} 条（已算 ${warmed}${
          failed ? `，失败 ${failed}` : ""
        }）`;
      } else if (total) {
        line = `预热完成：算了 ${warmed} 条，跳过已有 ${num(
          state.skipped,
        )} 条${failed ? `，失败 ${failed}` : ""}`;
      } else if (state.reason) {
        line = `预热：${state.reason}`;
      }
      if (line) setRetrievalValue(retrievalRescanResult, line);
      if (state.running) {
        retrievalWarmTimer = window.setTimeout(tick, 1500);
      }
    };
    tick();
  }
  retrievalActions.appendChild(retrievalMemoryRebuild);
  retrieval._fields.appendChild(retrievalActions);

  async function refreshRetrievalProviders() {
    try {
      const data = await apiGet("embedding/providers");
      const choices = Array.isArray(data.providers) ? data.providers : [];
      const current = String(data.current || "");
      retrievalSelect.replaceChildren();
      if (!choices.length) {
        retrievalSelect.appendChild(option("", "宿主里还没有 embedding 供应商"));
        retrievalSelect.disabled = true;
        return;
      }
      if (!current) retrievalSelect.appendChild(option("", "还没选（语义检索关着）"));
      choices.forEach((item) => {
        const id = String(item.id || "");
        const label = String(item.label || "");
        retrievalSelect.appendChild(
          option(id, label && label !== id ? `${label}（${id}）` : id),
        );
      });
      retrievalSelect.disabled = false;
      retrievalSelect.value = current || "";
    } catch (error) {
      retrievalSelect.replaceChildren();
      retrievalSelect.appendChild(option("", error.message || "读不到供应商列表"));
      retrievalSelect.disabled = true;
    }
  }

  retrievalSelect.addEventListener("change", async () => {
    const wanted = retrievalSelect.value;
    retrievalSelect.disabled = true;
    try {
      const result = await apiPost("embedding/provider", { provider_id: wanted });
      if (!wanted) {
        toast("关掉语义检索了：谈资本身不动，只是不再建向量索引");
      } else if (result && num(result.dim) > 0) {
        toast(`换好了：${wanted}，${num(result.dim)} 维；旧索引会被当成过期的、后台重攒`);
      } else {
        toast(result && result.reason ? result.reason : "换好了，但还没装起来（看下面那行）");
      }
      await refreshRetrievalProviders();
      await refreshRetrievalStatus();
    } catch (error) {
      toast(error.message || "换供应商失败");
      await refreshRetrievalProviders();
    } finally {
      retrievalSelect.disabled = false;
    }
  });
  refreshRetrievalProviders();

  async function refreshRetrievalStatus() {
    try {
      const status = await apiGet("embedding/status");
      const provider = String(status.provider_id || "");
      setRetrievalValue(
        retrievalProvider,
        provider ? provider : "未配置（语义检索关着）",
      );
      const pending = num(status.pending_count);
      setRetrievalValue(retrievalIndexed, `${num(status.indexed_count)} 条`);
      setRetrievalValue(
        retrievalPending,
        pending > 0 ? `${pending} 条` : status.pending_note || "0 条",
      );
      setRetrievalValue(
        retrievalSpace,
        status.space_fingerprint || "未配置（关闭语义检索）",
      );
      setRetrievalValue(
        retrievalError,
        status.last_error || status.reason || "没有出错记录",
      );
    } catch (error) {
      setRetrievalValue(retrievalProvider, "读不到");
      setRetrievalValue(retrievalIndexed, "读不到");
      setRetrievalValue(retrievalPending, "读不到");
      setRetrievalValue(retrievalSpace, "读不到");
      setRetrievalValue(retrievalError, error.message || "读取失败");
    }
  }
  refreshRetrievalStatus();
  form.appendChild(retrieval);
  /* B1-WP4-END */

  /* --- Agent（自主能力）：五个开关，住在世界配置里 --- */
  /* B-AGENT-BEGIN */
  world.agent = world.agent || {};
  const agentCard = settingsSection(
    "Agent（自主能力）",
    "这些开关决定她能不能自己动手做事：翻历史、做文件、把文件发出来、跑代码。" +
      "它们与世界的其他配置一起存，在这里改。",
    "打开总开关之后她才会真的动手：按任务轮流调工具、写文件、把文件发出去。" +
      "四个子开关各自放权、默认全关，关着的能力她既看不到也用不了；" +
      "「执行代码」是在跑插件的这台机器上真跑，不是沙箱。改动在点右上角「保存」后生效。",
  );
  // Agent 用哪个模型属于插件配置，不在这里选：只读地说明一句，不发请求
  agentCard._fields.appendChild(
    el("p", "hint", "Agent 模型在插件配置里选，这里只管开关。"),
  );
  // 声明放在开关上方（用户 2026-10-19）：先说清楚能改什么，再让人自己决定
  // 要不要给这份权力；真正开启时还会再弹一次确认（见 AGENT_FILE_POWER_WARNING）。
  agentCard._fields.appendChild(
    el("p", "hint", `⚠️ ${AGENT_FILE_POWER_WARNING}`),
  );

  // 开关名去掉 agent_ 前缀以后就是世界配置里的键名：与 core/models.py 的
  // AgentConfig 一一对应（调试快照那边仍用带前缀的键名，契约不变）
  const AGENT_SWITCH_FIELDS = [
    ["enabled", "开启Agent功能", "关闭时她不会自己动手，下面几个开关都不生效。"],
    ["history_lookup_enabled", "查聊天历史", "允许她查阅群聊留档、自己的经历账与记忆，用于寻找线索。"],
    ["artifact_generation_enabled", "生成文件", "允许她把结果写成文件（表格、文档、图片等）。"],
    ["file_send_enabled", "发送文件", "允许她把生成好的文件发送到聊天里。"],
    ["code_execution_enabled", "执行代码", "允许她在所在机器上执行自己写的代码，有安全风险。"],
    [
      "research_enabled",
      "办不成先查资料",
      "某一步没做成时，允许她先用搜索/知识库查一次做法再试（只多给一次）。",
    ],
  ];
  // 会动文件的几个开关（用户 2026-10-19）：开启前弹一次确认，明细见
  // AGENT_FILE_POWER_WARNING。只拦「从关到开」，关掉不拦。
  const AGENT_FILE_POWER_KEYS = new Set([
    "artifact_generation_enabled",
    "code_execution_enabled",
  ]);
  AGENT_SWITCH_FIELDS.forEach(([name, label, hint]) => {
    agentCard._fields.appendChild(
      checkboxField(
        label,
        !!world.agent[name],
        (value) => {
          world.agent[name] = !!value;
          markDirty();
        },
        {
          hint,
          confirm: AGENT_FILE_POWER_KEYS.has(name)
            ? AGENT_FILE_POWER_WARNING
            : "",
        },
      ),
    );
  });

  // 只读的动作说明查看：**独立开关**（不是那五个能力子开关之一，所以不并进上面那张
  // 表——那张表与世界配置里的五个子开关一一对应）。默认关：关掉时她的提示词里
  // 不会出现这项能力，她写了也会被回一句中文原因。
  agentCard._fields.appendChild(
    checkboxField(
      "允许她查看动作说明",
      !!world.agent.inspect_capability_enabled,
      (value) => {
        world.agent.inspect_capability_enabled = !!value;
        markDirty();
      },
      {
        hint: "打开后她可以只读地查看某个动作的完整说明；关上与没有这项能力一样。",
      },
    ),
  );

  // 缺信息时问一句（用户 m00001 第 8 条）：以前这一项只在向导里能改，设置页的
  // 开关清单里没有它，想再调只能重跑一遍向导。和上面那条一样是平铺 bool、不进
  // AGENT_SWITCH_FIELDS（那张表与 AgentConfig 的五个子开关一一对应）。
  agentCard._fields.appendChild(
    checkboxField(
      "缺信息时问一句",
      !!world.agent.ask_user_enabled,
      (value) => {
        world.agent.ask_user_enabled = !!value;
        markDirty();
      },
      {
        hint: "拿不准就问她一句；这类追问不占说话的额度。",
      },
    ),
  );

  // 插件工坊（用户 m31581）：给她造新能力的开关，也是平铺 bool、不进上面那张表
  // （那张表只装与 AgentConfig 前五个子开关一一对应的键）。自装排在写草稿后面：
  // 先能写、再决定要不要让她直接装——都默认关，装上的东西会真的跑起来。
  // 插件市场审核提醒（用户 m23946 第 1 条）：先要一道显式的风险确认，才谈得上
  // 「允许她写插件」——这两个都关着时，草稿工具连影子都不会出现。
  agentCard._fields.appendChild(
    checkboxField(
      "确认插件工坊的风险",
      !!world.agent.plugin_forge_risk_accepted,
      (value) => {
        world.agent.plugin_forge_risk_accepted = !!value;
        markDirty();
      },
      {
        hint:
          "打开后她才会按你的要求生成插件代码、写进插件目录（未必装上）。这是高敏感的自扩展开关，确认再用。",
        confirm: AGENT_FILE_POWER_WARNING,
      },
    ),
  );
  agentCard._fields.appendChild(
    checkboxField(
      "允许她写插件",
      !!world.agent.plugin_forge_enabled,
      (value) => {
        world.agent.plugin_forge_enabled = !!value;
        markDirty();
      },
      {
        hint:
          "打开后她可以自己写一份插件草稿（落在插件数据目录里）；Agent 页的「插件草稿」能看到全文再决定装不装。",
        confirm: AGENT_FILE_POWER_WARNING,
      },
    ),
  );
  agentCard._fields.appendChild(
    checkboxField(
      "允许她自己装",
      !!world.agent.plugin_forge_auto_install,
      (value) => {
        world.agent.plugin_forge_auto_install = !!value;
        markDirty();
      },
      {
        hint:
          "要先打开上面那条。打开后她能直接把自己写的插件装进 AstrBot、挂成动作——会真的跑起来，所以默认关。",
        confirm: AGENT_FILE_POWER_WARNING,
      },
    ),
  );
  // 主人裁决（用户 m31581 / 三档 m01035 决定 D4）：空值读旧布尔开关，所以这里
  // 先把它折算成三档再显示，选完只写新字段（老字段跟着同步，免得旧代码读到相反的值）。
  agentCard._fields.appendChild(
    selectField(
      "动系统的事先问主人",
      ownerApprovalModeOf(world),
      OWNER_APPROVAL_MODES,
      (value) => {
        world.agent.owner_approval_mode = value;
        world.agent.owner_approval_enabled = value !== "off";
        markDirty();
      },
      {
        hint:
          "不是主人提的麻烦事先私聊主人问一句；回「做」才动手，回「别做」就用她自己的口气跟发起人说清楚。主人自己提的不拦。",
      },
    ),
  );

  // 「看详情」入口：把**已经存下来**的说明铺开看一眼。纯只读——不执行动作、
  // 不发请求、不改配置（所以这里也不碰 markDirty）。
  agentCard._fields.appendChild(
    inputField(
      "主人是谁（账号 id）",
      String((world.agent && world.agent.owner_user_id) || ""),
      (value) => {
        world.agent.owner_user_id = String(value || "").trim();
        markDirty();
      },
      { hint: "要问谁、谁说了算。留空＝视为没有主人（所有需要主人权限的功能关闭）。" },
    ),
  );

  agentCard._fields.appendChild(agentCapabilityLookup());
  form.appendChild(agentCard);
  /* B-AGENT-END */

  form.appendChild(searchCard(world));
  form.appendChild(storageCard(world));
  form.appendChild(extensionsCard());

  /**
   * 联网搜索（用户 m10393）：以前这些字段长在 `search_web` 动作上，每个地点配一遍。
   *
   * 现在动作藏起来由系统维护，配置只在这里一份；引擎读 `world.search`，动作上残留的
   * 值只在全局为空时兜底，所以升级前后行为一致。
   */
  function searchCard(worldData) {
    worldData.search = worldData.search || {};
    const search = worldData.search;
    const card = settingsSection(
      "联网搜索",
      "她查东西用一份全局配置：搜哪家、读不读正文、查多深、要不要出处。",
      "搜索工具与阅读工具都得先在 AstrBot 里装好；不选就等于没有这项能力。" +
        "档位是她自己判断的，这里设的是默认档与上限。改动点右上角「保存」后生效。",
    );
    card._fields.appendChild(
      checkboxField(
        "允许联网搜索",
        search.enabled !== false,
        (value) => {
          search.enabled = value === true;
          markDirty();
        },
        { hint: "关掉以后她就不能查网上的东西，问了也只能说不知道。" },
      ),
    );
    card._fields.appendChild(
      pickerField(
        "搜索工具",
        search.tool_names || [],
        toolItems(),
        (chosen) => {
          search.tool_names = chosen;
          markDirty();
          // 选完要重画这一页：不重画的话按钮上还是"点击选择工具…"，
          // 看起来就像点了没反应（2026-10-21 用户实测）。
          renderSettings();
        },
        {
          hint: "搜目录用（返回标题+摘要+链接）。选多个按顺序调、结果合并。",
          empty: "点击选择搜索工具…",
          renderChips: true,
        },
      ),
    );
    if (search.enabled !== false && !effectiveSearchTools(worldData).length) {
      // 「用手机看网页没配置工具」那条提醒以前挂在隐藏动作上，用户既改不了也没必要改；
      // 现在只在真不可用、而且就在能改的地方说这一句（别处不再提醒）。
      card._fields.appendChild(
        el(
          "p",
          "section-note search-empty-hint",
          "还没配搜索工具 —— 她现在查不了网上的东西。选一个就能用；" +
            "不选也没关系，只是没有这项能力。",
        ),
      );
    }
    card._fields.appendChild(
      pickerField(
        "阅读工具",
        search.reader_tool_names || [],
        toolItems(),
        (chosen) => {
          search.reader_tool_names = chosen;
          markDirty();
          // 选完要重画这一页：不重画的话按钮上还是"点击选择工具…"，
          // 看起来就像点了没反应（2026-10-21 用户实测）。
          renderSettings();
        },
        {
          hint: "能传网址、返回正文的工具（把网页转成 markdown）。留空只用摘要。",
          empty: "点击选择阅读工具…",
          renderChips: true,
        },
      ),
    );
    card._fields.appendChild(
      pillsField(
        "默认档位",
        search.depth || "standard",
        SEARCH_DEPTHS,
        (value) => {
          search.depth = value;
          markDirty();
          renderSettings();
        },
        { hint: "她自己也能按事情大小挑更浅或更深的档。" },
      ),
    );
    card._fields.appendChild(
      pillsField(
        "最多开到哪一档",
        search.depth_max || "deep",
        SEARCH_DEPTHS,
        (value) => {
          search.depth_max = value;
          markDirty();
          renderSettings();
        },
        { hint: "设成「标准」就是谁也别想深挖：省时间省钱，但复杂问题会答得浅。" },
      ),
    );
    card._fields.appendChild(
      inputField(
        "最多几条查询",
        search.max_queries ?? 3,
        (value) => {
          search.max_queries = Math.min(5, Math.max(1, num(value, 3)));
          markDirty();
        },
        { hint: "一次最多给几条查询词，1~5。", type: "number", min: 1, max: 5 },
      ),
    );
    card._fields.appendChild(
      inputField(
        "默认读几篇正文",
        search.max_reads ?? 2,
        (value) => {
          search.max_reads = Math.max(0, num(value, 2));
          markDirty();
        },
        {
          hint: "配了阅读工具才生效；读得越多越慢。标准档用这个数。",
          type: "number",
          min: 0,
          max: 6,
        },
      ),
    );
    card._fields.appendChild(
      inputField(
        "最多补查几轮",
        search.rounds ?? 1,
        (value) => {
          search.rounds = Math.max(0, num(value, 1));
          markDirty();
        },
        { hint: "结果太少时换个角度再查；标准档用这个数。", type: "number", min: 0, max: 3 },
      ),
    );
    card._fields.appendChild(
      inputField(
        "太旧的结果直接丢掉",
        search.freshness_days ?? 365,
        (value) => {
          search.freshness_days = Math.max(0, num(value, 365));
          markDirty();
        },
        {
          hint: "按标题/摘要里的日期算；0 = 不按时效过滤。",
          type: "number",
          min: 0,
          max: 3650,
        },
      ),
    );
    card._fields.appendChild(
      inputField(
        "一轮最多查多久（秒）",
        search.budget_seconds ?? 20,
        (value) => {
          search.budget_seconds = Math.max(5, num(value, 20));
          markDirty();
        },
        { hint: "超了就不再补查、直接用手上的材料回答。", type: "number", min: 5, max: 120 },
      ),
    );
    card._fields.appendChild(
      inputField(
        "搜索主题（可选）",
        search.topic || "",
        (value) => {
          search.topic = String(value || "").trim();
          markDirty();
        },
        { hint: "固定查某个主题时写这里；留空就按她当时的意图查。" },
      ),
    );
    card._fields.appendChild(
      inputField(
        "查询模板（可选）",
        search.query_template || "",
        (value) => {
          search.query_template = String(value || "").trim();
          markDirty();
        },
        { hint: "占位符 {topic} 与 {date}，例如「{date} 新闻热点」。" },
      ),
    );
    card._fields.appendChild(
      pillsField(
        "讲结果时带来源",
        search.cite !== false ? "yes" : "no",
        [
          { key: "no", label: "不带", hint: "来源只进日志与调试输出" },
          { key: "yes", label: "带一条", hint: "允许她在末尾补一条参考链接" },
        ],
        (value) => {
          search.cite = value === "yes";
          markDirty();
          renderSettings();
        },
        { hint: "群里通常不想看网址；需要核对来源时再打开。" },
      ),
    );
    return card;
  }

  /**
   * 动作说明的只读查看器。
   *
   * 为什么从 `ui.config.world.actions` 读而不是再拉一个接口：编辑器手里这份就是
   * 引擎真正读的那份配置，看它不会出现"页面显示的和实际生效的不是一回事"。
   */
  function agentCapabilityLookup() {
    const wrapper = el("div", "field");
    const head = document.createElement("div");
    head.appendChild(el("span", "", "看某个动作的说明"));
    head.appendChild(
      tipBox("只读：把这条动作存下来的说明摊开，不会执行它，也不会改配置。"),
    );
    wrapper.appendChild(head);
    const host = ui.config.world || {};
    const list = Array.isArray(host.actions) ? host.actions : [];
    const select = document.createElement("select");
    select.className = "grow";
    list
      .slice()
      .sort((a, b) => String(a.id || "").localeCompare(String(b.id || "")))
      .forEach((item) => {
        const option = document.createElement("option");
        option.value = String(item.id || "");
        option.textContent = `${item.name || item.id || ""}（${item.id || ""}）`;
        select.appendChild(option);
      });
    const output = el("div", "muted");
    const draw = () => {
      const picked = list.find((item) => String(item.id || "") === select.value);
      output.textContent = agentCapabilityText(picked);
    };
    select.addEventListener("change", draw);
    wrapper.appendChild(select);
    wrapper.appendChild(output);
    if (!list.length) {
      output.textContent = "还没有任何动作。";
    } else {
      select.value = String(list[0].id || "");
      draw();
    }
    return wrapper;
  }

  /** 一条动作的只读说明（认不出的字段就说"没写"，不猜、不编）。 */
  function agentCapabilityText(action) {
    if (!action || typeof action !== "object") return "这条动作读不出来。";
    const found = action.discovery && typeof action.discovery === "object" ? action.discovery : {};
    const lines = [`${action.name || action.id || ""}（${action.id || ""}）`];
    const summary = String(found.summary || "").trim();
    const when = String(found.when_to_use || "").trim();
    const limits = Array.isArray(found.key_constraints) ? found.key_constraints : [];
    lines.push(`简述：${summary || "还没有，后台整理之后会有"}`);
    if (when) lines.push(`什么时候用：${when}`);
    if (limits.length) lines.push(`关键限制：${limits.join("；")}`);
    if (found.locked) lines.push("这一段是人工写定的，后台整理不会覆盖它。");
    const description = String(action.description || "").trim();
    if (description) lines.push(`完整说明：${description}`);
    const params = action.params && typeof action.params === "object" ? Object.keys(action.params) : [];
    if (params.length) lines.push(`要填什么：${params.join("、")}`);
    return lines.join("\n");
  }

  /* --- 自主决策 --- */
  world.decider = world.decider || {};
  const decideCard = settingsSection(
    "自主决策",
    "每隔一段时间会评估一次「她现在想不想做点什么」，这里的概率决定要不要把这次决定交给大模型。",
    "规则决策（困了回卧室、孤独了找人、无聊了换地方、好奇了去查东西）不看这个概率，命中就会执行；" +
      "它只影响「要不要额外问一次大模型来安排更丰富的计划」。没被抽中、规则又给不出计划时，她这一轮就自己待着。",
  );
  decideCard._fields.appendChild(
    inputField(
      "问大模型的概率下限",
      num(world.decider.llm_rate_min, 0.05),
      (value) => (world.decider.llm_rate_min = num(value, 0.05)),
      {
        hint: "决策意愿接近 0（平静、不无聊、刚被冷落）时的触发概率，0.05 = 5%。调大则更常交大模型安排。",
        type: "number",
        min: 0,
        max: 1,
        step: "0.01",
      },
    ),
  );
  decideCard._fields.appendChild(
    inputField(
      "问大模型的概率上限",
      num(world.decider.llm_rate_max, 0.4),
      (value) => (world.decider.llm_rate_max = num(value, 0.4)),
      {
        hint: "决策意愿接近 1（孤独、无聊、心潮高）时的触发概率，0.4 = 40%；中间按意愿线性插值。",
        type: "number",
        min: 0,
        max: 1,
        step: "0.01",
      },
    ),
  );
  decideCard._fields.appendChild(
    el(
      "p",
      "hint",
      "评估间隔由「插件配置 → decider_interval」控制（默认 300 秒）；" +
        "每小时最多自主行动几次在下面的「说话频率与预算」里。",
    ),
  );
  form.appendChild(decideCard);
  /* --- 分层候选 --- */
  world.action_selection = world.action_selection || {};
  const selectionCard = settingsSection(
    "分层候选",
    "她一时能做的事常常不止一件（开口说话、走过去、拿起一样东西、查点东西）。" +
      "这一项管的是：从这一堆候选里怎么挑出真正要做的那件。",
    "默认开着。开着时先按硬规矩把不能做的剔掉，再按当下的目的把剩下的分成几层，" +
      "在合适的那层里带种子加权抽一件，最后复核一次；抽不出合适的就退回原来的顺序，" +
      "所以开着不会挡住该做的事。关掉就完全按老办法：在固定顺序里挑第一个能做的。",
    "action_selection",
  );
  selectionCard._fields.appendChild(
    checkboxField(
      "按目的挑一件要做的事",
      world.action_selection.enabled === true,
      (value) => (world.action_selection.enabled = value),
      { hint: "关掉就退回按固定顺序挑第一个能做的。" },
    ),
  );
  selectionCard._fields.appendChild(
    inputField(
      "指挥型动作的冷却（分钟）",
      world.action_selection.command_quiet_minutes ?? 30,
      (value) => (world.action_selection.command_quiet_minutes = num(value, 30)),
      {
        type: "number",
        min: 0,
        step: 5,
        hint: "拍照、拍腿、自拍算同一类：刚做过一次，这段时间内不再抽这一类。写 0 = 不冷却。",
      },
    ),
  );
  form.appendChild(selectionCard);

  /* --- 无聊上网池 --- */
  world.boredom_online = world.boredom_online || {};
  const idleCard = settingsSection(
    "无聊了会自己找点事做",
    "她一个人待着、没什么事干的时候，会自己上网找点事——查点东西、翻翻帖子。" +
      "具体做哪一件不由这里挑：动作自己声明了「无聊时可以顺手做」才会进这个池子。",
    "默认开着。无聊到这个值以上、又闲着的时候，从声明过的动作里按权重抽一件去做；" +
      "同样的动作有冷却，两件事之间也隔一会儿，不会一连串全是同一件。" +
      "池子里没有可做的就照原来的办法决定，所以开着不会让她杵着不动。",
    "boredom_online",
  );
  idleCard._fields.appendChild(
    checkboxField(
      "无聊到一定程度会自己上网找点事做",
      world.boredom_online.enabled !== false,
      (value) => (world.boredom_online.enabled = value),
      { hint: "关掉以后她就只在别人搭话、或按日程安排时才动。" },
    ),
  );
  idleCard._fields.appendChild(
    inputField(
      "无聊到多少会自己找点事上网做",
      world.boredom_online.min_boredom ?? 0.55,
      (value) => (world.boredom_online.min_boredom = num(value, 0.55)),
      {
        type: "number",
        min: 0,
        max: 1,
        step: 0.05,
        hint: "0~1：越大越难无聊。0.55 大概是「闲着没事，但还没到抓耳挠腮」。",
      },
    ),
  );
  idleCard._fields.appendChild(
    inputField(
      "两次上网找事至少隔几分钟",
      world.boredom_online.min_gap_minutes ?? 20,
      (value) => (world.boredom_online.min_gap_minutes = num(value, 20)),
      {
        type: "number",
        min: 0,
        step: 5,
        hint: "两件「自己找的事」之间的最短间隔。写 0 = 不限制。",
      },
    ),
  );
  form.appendChild(idleCard);

  /* --- 无人回应保护 --- */
  const engagement = settingsSection(
    "无人回应时的处理",
    "她主动说话但没人回应时的自我保护，避免一个人自说自话。",
    "默认：连续 3 次没人理就安静 2 小时，冷却结束后计数减半再试。",
  );
  [
    ["unanswered_threshold", "连续几次没人理就安静", "达到这个次数进入冷却"],
    ["silence_window_minutes", "多久算一次「没人理」（分钟）", "发出消息后等这么久还没人说话，就记一次"],
    ["cooldown_after_unanswered", "冷却时长（分钟）", "冷却期间不主动发言"],
    [
      "after_reply_cooldown_minutes",
      "刚回过话后的安静时间（分钟）",
      "被搭话、她也回过之后，这段时间内不主动开口（被动回复不受影响），免得跟刚才的回复挤在一起",
    ],
  ].forEach(([key, label, hint]) => {
    engagement._fields.appendChild(
      inputField(label, num(world.engagement[key]), (value) => (world.engagement[key] = num(value)), {
        hint,
        type: "number",
      }),
    );
  });
  engagement._fields.appendChild(
    checkboxField(
      "她求助没人接时，补一句自我圆场",
      world.events.remind_when_ignored !== false,
      (value) => (world.events.remind_when_ignored = value),
      { hint: "这一句由模型按人设现写（写不出来就不说）。关掉 = 她求助完就安静等，到点自己拿主意。" },
    ),
  );
  engagement._fields.appendChild(
    checkboxField(
      "冷却结束后计数减半",
      world.engagement.halve_on_cooldown_end !== false,
      (value) => (world.engagement.halve_on_cooldown_end = value),
      { hint: "开启后冷却结束会更容易重新尝试说话。" },
    ),
  );
  form.appendChild(engagement);

  /* --- 孤独感与插话 --- */
  world.decider = world.decider || {};
  const interject = settingsSection(
    "孤独感与插话",
    "控制她什么时候会主动接别人的话。",
    "插话需要同时满足：群里最近有人说话、孤独感高过阈值、没在冷却、且没超过每小时上限。",
  );
  interject._fields.appendChild(
    inputField(
      "孤独感阈值",
      num(world.decider.interject_threshold, 0.6),
      (value) => (world.decider.interject_threshold = num(value, 0.6)),
      {
        hint:
          "孤独感高于这个值（0~1）她才会想插话。调低=更爱接话，调高=更安静。" +
          "（心潮不参与这个判断——它只决定她说话有多动情。）",
        type: "number",
        step: "0.05",
        min: 0,
        max: 1,
      },
    ),
  );
  interject._fields.appendChild(
    checkboxField(
      "允许她主动接话（插话）",
      world.decider.enabled !== false,
      (value) => (world.decider.enabled = value),
      {
        hint:
          "关掉后她不会主动接别人的话，只会按日程和被 @ 时回应。下面的参数在关闭时不生效。",
      },
    ),
  );
  interject._fields.appendChild(
    inputField(
      "多久内算「正在聊」（分钟）",
      num(world.decider.chat_window_minutes, 180),
      (value) => (world.decider.chat_window_minutes = num(value, 180)),
      { hint: "这个时间窗内有人说话，才算群里在聊天。", type: "number" },
    ),
  );
  interject._fields.appendChild(
    inputField(
      "至少几条消息才插话",
      num(world.decider.min_messages_to_interject, 2),
      (value) => (world.decider.min_messages_to_interject = num(value, 2)),
      { hint: "窗口内至少有几条别人的消息，她才考虑接话。", type: "number" },
    ),
  );
  interject._fields.appendChild(
    inputField(
      "插话最短间隔（分钟）",
      num(world.decider.interject_cooldown_minutes, 20),
      (value) => (world.decider.interject_cooldown_minutes = num(value, 20)),
      { hint: "两次主动插话之间至少隔这么久，防止她变成话痨。", type: "number" },
    ),
  );
  // 「带多少条聊天进提示词」在下面的「上下文」卡片里统一配置
  form.appendChild(interject);

  /* --- 睡眠与打断 --- */
  world.sleep = world.sleep || {};
  const sleep = settingsSection(
    "睡眠与打断",
    "她睡觉（或小睡）时怎么回应消息，以及怎么把她叫起来。",
    "睡着时的判定只认「@ 她 + 唤醒词」：命中才会真的打断睡眠、正常回复。",
  );
  sleep._fields.appendChild(
    selectField(
      "睡眠中被叫的回应",
      world.sleep.reply_mode || "template",
      [
        { key: "template", label: "回一句固定文案（推荐）" },
        { key: "silent", label: "完全不回（也不让主人格回）" },
        { key: "normal", label: "照常回复（不拦）" },
      ],
      (value) => (world.sleep.reply_mode = value),
      {
        hint: "睡着时被 @ 但没有唤醒词：固定文案 = 只发模板；完全不回 = 群里不显示并挡下主人格；照常回复 = 不拦。",
      },
    ),
  );
  sleep._fields.appendChild(
    textareaField(
      "固定文案",
      world.sleep.reply_text ?? "zzz…（{bot}睡觉中，要叫她起来吗？）",
      (value) => (world.sleep.reply_text = value),
      {
        hint:
          "睡着时发出去的句子，支持 {bot}（她的名字）和 {user}（叫她的人）。" +
          "留空等于「完全不回」。",
        rows: 2,
      },
    ),
  );
  sleep._fields.appendChild(
    tagField(
      "唤醒词",
      world.sleep.wake_words || [],
      (value) => (world.sleep.wake_words = value),
      {
        hint:
          "消息里出现这些词（并且满足下面那条 @ 的要求）才真的把她叫醒、打断睡眠。" +
          "回车或点「添加」加一个，点词上的 × 删掉。",
        placeholder: "例如：醒醒",
      },
    ),
  );
  sleep._fields.appendChild(
    checkboxField(
      "必须 @ 她才算叫醒",
      world.sleep.wake_requires_mention !== false,
      (value) => (world.sleep.wake_requires_mention = value),
      {
        hint:
          "开启后：群聊中的普通消息（如「起床了」）不会唤醒她，必须 @ 她（私聊同样）。\n" +
          "关掉后：只要消息里出现唤醒词就打断睡眠。",
      },
    ),
  );
  sleep._fields.appendChild(
    inputField(
      "同一条回复的最短间隔（分钟）",
      num(world.sleep.reply_cooldown_minutes, 5),
      (value) => (world.sleep.reply_cooldown_minutes = num(value, 5)),
      {
        hint:
          "这段时间内再被 @ 就保持安静，免得她被连着 @ 时刷一屏 zzz。填 0 表示每次都回。",
        type: "number",
      },
    ),
  );
  sleep._fields.appendChild(
    inputField(
      "叫醒后多久内不再自己睡（分钟）",
      num(world.sleep.wake_grace_minutes, 12),
      (value) => (world.sleep.wake_grace_minutes = num(value, 12)),
      {
        hint:
          "刚被叫醒时精力往往还很低，没有这段保护期的话，规则几分钟内又会把她送回床上。",
        type: "number",
      },
    ),
  );
  sleep._fields.appendChild(
    checkboxField(
      "小睡（打个盹）也按睡觉处理",
      world.sleep.applies_to_nap !== false,
      (value) => (world.sleep.applies_to_nap = value),
      { hint: "关掉后只有「睡觉」的状态会被拦，小睡时照旧正常聊天。" },
    ),
  );
  sleep._fields.appendChild(
    checkboxField(
      "叫醒时清空排队的计划",
      world.sleep.clear_plan_on_wake !== false,
      (value) => (world.sleep.clear_plan_on_wake = value),
      {
        hint:
          "她被打断时手头可能还排着没做完的动作（比如几小时前想说的那句），" +
          "一起清掉就不会在睡醒后突然补发。",
      },
    ),
  );
  sleep._fields.appendChild(
    checkboxField(
      "睡着时连其他插件一起挡下（方案 A）",
      world.sleep.block_plugins !== false,
      (value) => (world.sleep.block_plugins = value),
      {
        hint: "开启（默认）：没 @ 她的消息在本插件被截住，后续插件也不执行。关闭：只保证本插件不出声。",
      },
    ),
  );
  sleep._fields.appendChild(
    selectField(
      "挡住哪些消息",
      world.sleep.block_scope || "unmentioned",
      [
        { key: "unmentioned", label: "只挡没 @ 她的（推荐）" },
        { key: "all", label: "除指令和唤醒词外全挡（连固定文案也不回）" },
      ],
      (value) => (world.sleep.block_scope = value),
      {
        hint: "只挡没 @ 她的：@ 她仍收到睡眠文案。全挡：只有 /指令 与带唤醒词的 @ 能进来。",
      },
    ),
  );
  sleep._fields.appendChild(el("div", "sub-title", "睡前与睡醒"));
  sleep._fields.appendChild(
    checkboxField(
      "睡前先迷糊一会儿（临睡期）",
      world.sleep.drowsy !== false,
      (value) => (world.sleep.drowsy = value),
      {
        hint: "开启后：到点不立刻躺下，先进入困倦的临睡期（说话断断续续），安静下来才真的睡。",
      },
    ),
  );
  sleep._fields.appendChild(
    inputField(
      "临睡期安静几分钟才睡",
      num(world.sleep.drowsy_minutes, 5),
      (value) => (world.sleep.drowsy_minutes = Math.max(1, Math.round(num(value, 5)))),
      {
        hint: "这段时间没人跟她说话就睡；中间有人来找，计时重新开始。",
        type: "number",
        step: "1",
      },
    ),
  );
  sleep._fields.appendChild(
    inputField(
      "临睡期最多拖多久（分钟）",
      num(world.sleep.drowsy_max_minutes, 30),
      (value) =>
        (world.sleep.drowsy_max_minutes = Math.max(1, Math.round(num(value, 30)))),
      {
        hint: "到点无论如何都睡：不然临睡前又聊起来，容易熬到天亮。",
        type: "number",
        step: "5",
      },
    ),
  );
  sleep._fields.appendChild(
    checkboxField(
      "睡前让她自己决定要不要说晚安",
      world.sleep.goodnight !== false,
      (value) => (world.sleep.goodnight = value),
      {
        hint: "会带着当前时间和背景问她一次：发给谁、发不发都由她定（不想发就安静地睡）。",
      },
    ),
  );
  sleep._fields.appendChild(
    checkboxField(
      "睡醒让她自己决定要不要说早安",
      world.sleep.goodmorning !== false,
      (value) => (world.sleep.goodmorning = value),
      { hint: "同样由她自己判断：给谁发、发不发都行，一天最多一次。" },
    ),
  );
  form.appendChild(sleep);

  /* --- 记忆 --- */
  const memory = settingsSection(
    "记忆",
    "记忆什么时候能被想起来。",
    "记忆始终绑定「会话 + 人格 + 地点 + 人」，默认不会跨群泄露。",
  );
  memory._fields.appendChild(
    pillsField(
      "记忆作用域",
      world.memory_scope_mode || "group_persona",
      SCOPE_MODES_MEMORY,
      (value) => {
        world.memory_scope_mode = value;
        renderSettings();
      },
      {},
    ),
  );
  memory._fields.appendChild(
    selectField(
      "记忆冲突时",
      world.memory_conflict_policy || "newest",
      [
        { key: "newest", label: "以新记忆为准" },
        { key: "highest_weight", label: "保留权重更高的" },
        { key: "skip_conflict", label: "冲突时保留旧的" },
      ],
      (value) => (world.memory_conflict_policy = value),
      { hint: "同一地点出现互相矛盾的两条记忆（例如「他喜欢我」和「他不喜欢我」）时怎么处理。" },
    ),
  );
  world.memory = world.memory || {};
  memory._fields.appendChild(
    checkboxField(
      "把对话总结成记忆",
      world.memory.dialogue_summary !== false,
      (value) => (world.memory.dialogue_summary = value),
      {
        hint: "开启后把一段对话攒起来交给大模型，压成一句以她视角写的记忆；关闭则聊天内容不进记忆。",
      },
    ),
  );
  memory._fields.appendChild(
    inputField(
      "攒够多少条总结一次",
      num(world.memory.summary_trigger_messages, 10),
      (value) => (world.memory.summary_trigger_messages = num(value, 10)),
      {
        hint: "同一段对话累计到这个条数（含她自己说的话）就总结一次。调小 = 更细碎，调大 = 更省调用。",
        type: "number",
      },
    ),
  );
  memory._fields.appendChild(
    inputField(
      "聊完多久算一段（分钟）",
      num(world.memory.summary_idle_minutes, 30),
      (value) => (world.memory.summary_idle_minutes = num(value, 30)),
      {
        hint: "安静超过这么久就当作这段聊完了，触发一次总结。填 0 表示不按时间触发。",
        type: "number",
      },
    ),
  );
  memory._fields.appendChild(
    checkboxField(
      "她换地点时也总结一次",
      world.memory.summary_on_move !== false,
      (value) => (world.memory.summary_on_move = value),
      {
        hint: "她走开时把该地点聊过的内容总结成一条记忆，挂在那里，下次回去能想起来。",
      },
    ),
  );
  memory._fields.appendChild(
    inputField(
      "记忆一句话最长多少字",
      num(world.memory.summary_max_chars, 60),
      (value) => (world.memory.summary_max_chars = num(value, 60)),
      { hint: "写进提示词里的长度约束，太长会挤占别的上下文。", type: "number" },
    ),
  );
  form.appendChild(memory);

  /* --- 记忆上限与淘汰 --- */
  const memoryBudget = settingsSection(
    "记忆上限与淘汰",
    "留多少条、满了先删谁、多久开始变淡。保存后下一轮对话就按新值算。",
    "容量到顶时按「重复 → 动作噪声 → 久未召回」的顺序淘汰，" +
      "最近被想起过的和钉住的都不会被删；衰减只把权重压下去，不删记录。" +
      "这几项填 0 或留空都按默认值处理。",
    "memory_budget",
  );
  memoryBudget._fields.appendChild(
    inputField(
      "记忆上限（每个会话最多几条）",
      positiveNum(world.memory.memory_limit, 1000),
      (value) => (world.memory.memory_limit = positiveNum(value, 1000)),
      {
        hint: "每个会话最多留多少条记忆；到顶按 重复→噪声→久未召回 淘汰。默认 1000。",
        type: "number",
        min: 1,
        step: 1,
      },
    ),
  );
  memoryBudget._fields.appendChild(
    inputField(
      "同一动作多久内合并成一条（分钟）",
      positiveNum(world.memory.action_merge_minutes, 30),
      (value) => (world.memory.action_merge_minutes = positiveNum(value, 30)),
      {
        hint: "同一地点重复做同一个动作，这段时间内只刷新时间与次数，不再新增一条。",
        type: "number",
        min: 1,
        step: 1,
      },
    ),
  );
  memoryBudget._fields.appendChild(
    inputField(
      "刚用过的记忆保护期（天）",
      positiveNum(world.memory.evict_protect_days, 7),
      (value) => (world.memory.evict_protect_days = positiveNum(value, 7)),
      {
        hint: "这段时间内被想起过的记忆，容量到顶也不淘汰。默认 7 天。",
        type: "number",
        min: 1,
        step: 1,
      },
    ),
  );
  memoryBudget._fields.appendChild(
    inputField(
      "多久没想起才开始变淡（天）",
      positiveNum(world.memory.decay_idle_days, 30),
      (value) => (world.memory.decay_idle_days = positiveNum(value, 30)),
      {
        hint: "这段时间内权重一动不动；超过之后才按半衰期慢慢变淡，只降不删。",
        type: "number",
        min: 1,
        step: 1,
      },
    ),
  );
  memoryBudget._fields.appendChild(
    inputField(
      "权重减半的周期（天）",
      positiveNum(world.memory.decay_half_life_days, 30),
      (value) => (world.memory.decay_half_life_days = positiveNum(value, 30)),
      {
        hint: "开始变淡后，每过这么多天权重减一半，最低停在衰减下限。",
        type: "number",
        min: 1,
        step: 1,
      },
    ),
  );
  memoryBudget._fields.appendChild(
    inputField(
      "衰减下限（0 到 1 之间）",
      positiveNum(world.memory.decay_floor, 0.1),
      (value) => (world.memory.decay_floor = positiveNum(value, 0.1)),
      {
        hint: "权重再低也停在这个值，记录不会被删掉。默认 0.1。",
        type: "number",
        min: 0.01,
        max: 1,
        step: 0.01,
      },
    ),
  );
  memoryBudget._fields.appendChild(
    inputField(
      "整理时看最近多少条旧记忆",
      positiveNum(world.memory.consolidate_memory_window, 240),
      (value) => (world.memory.consolidate_memory_window = positiveNum(value, 240)),
      {
        hint: "整理记忆时从这个窗口里挑能合并的旧条目，太小会看不见早先的记忆。",
        type: "number",
        min: 10,
        step: 10,
      },
    ),
  );
  form.appendChild(memoryBudget);
  form.appendChild(knowledgeCard());
  /* --- 做梦 --- */
  world.dream = world.dream || {};
  const dreamCard = settingsSection(
    "做梦",
    "她睡觉（小睡或整夜）时会一并整理记忆；本项决定整理时是否同时生成一条梦。",
    "默认开着。梦是单独一类素材：不算事实、不做关系证据、不会把现实改写掉，" +
      "只是低权重的记忆，可能出现在她「想起」的那一段里。梦的要紧程度是 0.4，" +
      "低于「多要紧的事才主动提」的默认门槛，所以她不会自己把梦当谈资讲出来。" +
      "关掉就只整理、不做梦。",
    "dream",
  );
  dreamCard._fields.appendChild(
    checkboxField(
      "睡觉时做一条梦",
      world.dream.enabled === true,
      (value) => (world.dream.enabled = value),
      { hint: "关掉后小睡和整夜整理照跑，只是不再生成梦。" },
    ),
  );
  dreamCard._fields.appendChild(
    inputField(
      "每次小睡做梦的概率",
      num(world.dream.chance, 0.3),
      (value) => (world.dream.chance = num(value, 0.3)),
      {
        hint: "0~1（默认 0.3）：掷不中就只整理、不做梦。",
        type: "number",
        step: "0.05",
        min: 0,
        max: 1,
      },
    ),
  );
  form.appendChild(dreamCard);

  /* --- 工具 --- */
  const toolsSection = settingsSection(
    "工具",
    "工具挂在动作上；这里配置哪些工具是「不分地点都能用」的，以及工具结果怎么处理。",
    "工具只挂在动作上：动作里选好工具就够用了，这里只配置「不分地点都能用」的通用工具。",
  );
  toolsSection._fields.appendChild(
    pickerField(
      "通用工具（任何地点都能用）",
      world.global_allowed_tools || [],
      toolItems(),
      (chosen) => {
        world.global_allowed_tools = chosen;
        renderSettings();
      },
      {
        hint: "这里勾选的工具直接列进提示词，任何地点都能调用。只在一处用的工具请绑到该地点的动作上。",
        empty: "（没有通用工具）",
      },
    ),
  );
  toolsSection._fields.appendChild(
    checkboxField(
      "按地点裁剪 AstrBot 工具集",
      world.tool_filter_enabled !== false,
      (value) => (world.tool_filter_enabled = value),
      {
        hint: "主人格可用工具 = 这里的通用工具 + 当前地点动作绑定的工具，避免在卧室里上网搜索。",
      },
    ),
  );
  toolsSection._fields.appendChild(
    checkboxField(
      "工具结果交回大模型说一句",
      world.tool_result_reply !== false,
      (value) => (world.tool_result_reply = value),
      {
        hint: "工具调用完成后把结果交回主模型，由她用自己的话说出来。关闭则结果只进日志、结束后不额外说话。",
      },
    ),
  );
  form.appendChild(toolsSection);

  /* --- 动作与地点 --- */
  const placeSection = settingsSection(
    "动作与地点",
    "她做一件事时，如果得先走到别的地方，怎么处理。",
    "动作可以限定「只在某个地点可用」（例如上网搜索只属于书房）。" +
      "她主动提到要做这类事时，提示词会让她先写一步移动；万一她只说不做，下面这个开关让插件替她走。",
  );
  placeSection._fields.appendChild(
    checkboxField(
      "她想去别处做某事时，自动带她过去",
      world.remote_action_travel !== false,
      (value) => (world.remote_action_travel = value),
      {
        hint: "开启后，她答应做某件事却漏写移动时，插件补一步移动再执行，日志标注「已自动前往」。",
      },
    ),
  );
  form.appendChild(placeSection);

  /* --- 事件 --- */
  world.events = world.events || {};
  const eventSection = settingsSection(
    "事件",
    "她一个人待着的时候会不会遇上点事。频率用「每小时大约几次」表示，不是定时器。",
    "微事件只改她自己的状态和记忆；小事件要她做选择、会掷骰；大事件会说，也可能找人商量。",
  );
  eventSection._fields.appendChild(
    checkboxField(
      "启用事件系统",
      world.events.enabled !== false,
      (value) => (world.events.enabled = value),
      { hint: "关掉之后她只按动作和日程生活，不会再有随机事件。" },
    ),
  );
  // 显示兜底不再就地写死：缺键时先看已加载的世界配置，再看「生活丰富度」标准档
  // （KNOBS 里那一档本来就是后端默认 1 / 0.3 / 0.028，同一个数不必抄第三份）。
  const lifeRichLevel = (() => {
    const knob = KNOBS.find((item) => item.id === "life_rich");
    return (knob && knob.levels && knob.levels[2]) || {};
  })();
  [
    ["micro_per_hour", "微事件：每小时几次", "只影响她自己的状态和记忆，群里不主动说。默认 1 次/小时。"],
    ["small_per_hour", "小事件：每小时几次", "她要做个选择、会掷骰。默认 0.3 次/小时（约三个小时一次）。"],
    ["big_per_hour", "大事件：每小时几次", "会说、可能需要找人商量。默认 0.028 次/小时（约一天半一件）。"],
  ].forEach(([key, label, hint]) => {
    eventSection._fields.appendChild(
      inputField(
        label,
        world.events[key] ?? lifeRichLevel[`events.${key}`] ?? 0,
        (value) => (world.events[key] = Number(value) || 0),
        { hint, type: "number", min: "0", max: "12", step: "0.01" },
      ),
    );
  });
  eventSection._fields.appendChild(
    inputField(
      "在一个地方待够多久才算「在这儿生活」（分钟）",
      num(world.events.dwell_minutes, 5),
      (value) => (world.events.dwell_minutes = num(value, 5)),
      { hint: "刚走到一个地方就出事会很假；待够这么久才有机会遇上什么。默认 5 分钟。", type: "number", min: "0", max: "240", step: "1" },
    ),
  );
  eventSection._fields.appendChild(
    checkboxField(
      "做持续动作时也能出事",
      world.events.while_busy !== false,
      (value) => (world.events.while_busy = value),
      { hint: "做饭、看书、发呆期间也会遇上事；关掉就只在她闲下来时才有。" },
    ),
  );
  eventSection._fields.appendChild(
    checkboxField(
      "睡觉时也出事",
      world.events.in_sleep === true,
      (value) => (world.events.in_sleep = value),
      { hint: "默认关闭：睡着还出事很出戏。" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "大事件最少演几幕",
      num(world.events.big_min_steps, 2),
      (value) => (world.events.big_min_steps = num(value, 2)),
      { hint: "大事件一步就收尾会显得潦草。到下限之前，模型没留伏笔也会被补上一步。默认 2。", type: "number", min: "1", max: "6", step: "1" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "大事件两步之间至少隔多久（分钟）",
      num(world.events.big_step_gap_minutes, 15),
      (value) => (world.events.big_step_gap_minutes = num(value, 15)),
      { hint: "大事件要演两幕以上，不拉开间隔就会连着刷屏。默认 15 分钟。", type: "number", min: "0", max: "1440", step: "5" },
    ),
  );
  // 可选值只有三个（后端 events.share_* 的枚举），用下拉而不是文本框：
  // 以前能随手打错一个字母，配置存下去就成了"哪一幕都不说"。
  const EVENT_SHARE_CHOICES = [
    ["silent", "从不说（silent）"],
    ["nodes", "只在开场 / 收尾说（nodes）"],
    ["always", "每一幕都能说（always）"],
  ];
  [
    ["share_micro", "微事件", "silent", "默认 silent：只进状态和记忆，不出声。"],
    ["share_small", "小事件", "nodes", "默认 nodes：只在开场和收尾那两幕说。"],
    ["share_big", "大事件", "always", "默认 always：大事件每一幕都可以说。"],
  ].forEach(([key, label, fallback, hint]) => {
    const current = String(world.events[key] || fallback);
    const known = EVENT_SHARE_CHOICES.some(([value]) => value === current);
    eventSection._fields.appendChild(
      selectField(
        `${label}：要不要说出来`,
        known ? current : fallback,
        EVENT_SHARE_CHOICES,
        (value) => (world.events[key] = value),
        { hint },
      ),
    );
  });
  eventSection._fields.appendChild(
    checkboxField(
      "事件结果写进聊天留档",
      world.events.event_into_chat_log !== false,
      (value) => (world.events.event_into_chat_log = value),
      { hint: "写成「她自己身上发生的事」——不是她说的，但以后聊天时能自然提起。默认开。" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "「最近发生在我身上的事」最多带几条",
      num(world.events.event_digest_lines, 12),
      (value) => (world.events.event_digest_lines = num(value, 12)),
      { hint: "续说时顺口带一句用的那份清单。默认 12 条。", type: "number", min: "1", max: "40", step: "1" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "谁能用 /vw event 投递事件",
      world.events.event_actor || "admin",
      (value) => (world.events.event_actor = value === "all" ? "all" : "admin"),
      { hint: "admin = 只有管理员（默认）；all = 群里所有人都可以给她安排事情。", placeholder: "admin / all" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "一条线索最多几步",
      num(world.events.max_steps, 6),
      (value) => (world.events.max_steps = num(value, 6)),
      { hint: "一件事最多连着演几步，到顶必须收尾，默认 6。", type: "number", min: "1", max: "20", step: "1" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "一条线索最长多久（分钟）",
      num(world.events.max_minutes, 120),
      (value) => (world.events.max_minutes = num(value, 120)),
      { hint: "超过这么久这件事就告一段落，默认 120 分钟。", type: "number", min: "5", max: "2880", step: "5" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "事件里她一次最多说几句",
      num(world.events.max_say_lines, 4),
      (value) => (world.events.max_say_lines = num(value, 4)),
      {
        hint: "求助分几条发，所以比平时（群聊 2 句）宽松，默认 4 条；结果那几句仍最多 2 条。",
        type: "number",
        min: "1",
        max: "20",
        step: "1",
      },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "事件发言的硬顶",
      num(world.events.max_say_lines_hard, 6),
      (value) => (world.events.max_say_lines_hard = num(value, 6)),
      { hint: "不管上面配多大，都不会超过这个数（防刷屏）。默认 6。", type: "number", min: "1", max: "20", step: "1" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "两件事之间至少隔多久（分钟）",
      num(world.events.min_gap_minutes, 30),
      (value) => (world.events.min_gap_minutes = num(value, 30)),
      {
        hint: "自动掷骰的两件事之间至少隔这么久，免得刚收尾又来一件。默认 30；填 0 = 不限制。",
        type: "number",
        min: "0",
        max: "1440",
        step: "5",
      },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "一条线索里最多调用几次动作",
      num(world.events.event_action_calls, 2),
      (value) => (world.events.event_action_calls = num(value, 2)),
      {
        hint:
          "她可以为了这件事先去做点什么（查资料、做点准备），动作结果交回给她再判断。" +
          "默认 2 次；填 0 = 事件里不给动作。",
        type: "number",
        min: "0",
        max: "10",
        step: "1",
      },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "每一幕出图的概率",
      num(world.events.photo_tendency ?? world.events.photo_chance, 0.3),
      (value) => (world.events.photo_tendency = num(value, 0.3)),
      {
        hint:
          "事件每推进一幕或完结时各掷一次：同一幕最多一张，" +
          "总张数由下面那项兜着。默认 0.3；填 0 = 关闭。",
        type: "number",
        min: "0",
        max: "1",
        step: "0.05",
      },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "一件事最多拍几张",
      num(world.events.photo_max_per_event, 3),
      (value) => (world.events.photo_max_per_event = num(value, 3)),
      {
        hint:
          "同一件事里出图的总张数上限，管着「这件事演太长会不会一路拍下去」。默认 3；填 0 = 不限。",
        type: "number",
        min: "0",
        max: "20",
        step: "1",
      },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "事件出图用哪些动作",
      (world.events.photo_actions || []).join(", "),
      (value) =>
        (world.events.photo_actions = String(value)
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean)),
      {
        hint: "按顺序挑第一个启用的动作，写动作 id，用逗号分隔。默认 selfie, take_photo。",
        placeholder: "selfie, take_photo",
      },
    ),
  );
  eventSection._fields.appendChild(
    checkboxField(
      "判定结果影响情绪",
      world.events.result_emotion !== false,
      (value) => (world.events.result_emotion = value),
      {
        hint:
          "大成功 / 成功轻微抬高兴致，失败往下压一点（心潮与效价，保守幅度）。" +
          "关掉 = 判定只改能力值，不改情绪。",
      },
    ),
  );
  eventSection._fields.appendChild(genreEditor(world));
  const scaleFull = el("div", "full");
  scaleFull.appendChild(
    textareaField(
      "题材的尺度边界（可留空）",
      world.events.genre_scale || "",
      (value) => (world.events.genre_scale = value),
      {
        hint: "留空不额外限制。例如「不要写受伤」。与「不生成哪些事件」一正一反，都写进生成提示词。",
        rows: 2,
      },
    ),
  );
  eventSection._fields.appendChild(scaleFull);
  eventSection._fields.appendChild(
    inputField(
      "最近几件用过的题材先不重复",
      num(world.events.genre_recency, 2),
      (value) => (world.events.genre_recency = num(value, 2)),
      { hint: "默认 2：接着两次不会撞同一个题材（不然会连着好几次都是「日常小事」）。", type: "number", min: "0", max: "7", step: "1" },
    ),
  );
  eventSection._fields.appendChild(
    checkboxField(
      "日程到点先问她一下（日程闸门）",
      world.events.schedule_gate !== false,
      (value) => (world.events.schedule_gate = value),
      {
        hint: "她正处理一件事时，睡觉 / 小睡 / 换地方这三类日程会先问她照做、推迟还是算了；其他日程照常执行。",
      },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "推迟一次隔多久再检查（分钟）",
      num(world.events.schedule_delay_minutes, 30),
      (value) => (world.events.schedule_delay_minutes = num(value, 30)),
      { hint: "默认 30 分钟。", type: "number", min: "5", max: "600", step: "5" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "同一条日程一天最多推迟几次",
      num(world.events.schedule_delay_limit_times, 2),
      (value) => (world.events.schedule_delay_limit_times = num(value, 2)),
      { hint: "到上限就必须执行——睡觉这件事没有商量余地。默认 2 次。", type: "number", min: "0", max: "20", step: "1" },
    ),
  );
  eventSection._fields.appendChild(
    inputField(
      "同一条日程一天最多推迟多久（分钟）",
      num(world.events.schedule_delay_limit_minutes, 180),
      (value) => (world.events.schedule_delay_limit_minutes = num(value, 180)),
      { hint: "和次数谁先到算谁。默认 180 分钟（3 小时）。", type: "number", min: "0", max: "1440", step: "10" },
    ),
  );
  form.appendChild(eventSection);
  form.appendChild(memeSettingsCard(world));
  /* --- 群友的回应 --- */
  world.contributions = world.contributions || {};
  const helpCard = settingsSection(
    "群友的回应",
    "她在群里求助时，群友会接话。这一项管的是：群里连着说的几句话，怎么算「这一轮的建议」。",
    "默认攒 120 秒：头一句有用的话之后先等两分钟，群里接话、补充、改口都算进同一轮，" +
      "免得一句话就让她重拿一次主意。写 0 就是收到就立刻处理。" +
      "追问上限管的是「一轮里最多追问几轮」，免得她揪着同一件事没完没了。",
    "contributions",
  );
  helpCard._fields.appendChild(
    inputField(
      "收话的短窗（秒）",
      num(world.contributions.window_seconds, 120),
      (value) => (world.contributions.window_seconds = num(value, 120)),
      { hint: "默认 120 秒。写 0 = 收到就立刻处理。", type: "number", min: 0 },
    ),
  );
  helpCard._fields.appendChild(
    inputField(
      "一轮里最多追问几轮",
      num(world.contributions.max_rounds, 3),
      (value) => (world.contributions.max_rounds = num(value, 3)),
      { hint: "默认 3：问不出结果就不再追着问。", type: "number", min: 0 },
    ),
  );
  form.appendChild(helpCard);

  /* --- 作息与夜晚 --- */
  world.night = world.night || {};
  const nightSection = settingsSection(
    "作息与夜晚",
    "夜晚时段决定「睡觉」这个动作什么时候可选，也决定熬夜的代价。",
    "默认 23:00–07:00 算夜里。起止小时填成一样表示没有夜晚时段：睡觉不受时段限制，熬夜代价也不生效。",
  );
  nightSection._fields.appendChild(
    inputField(
      "夜晚开始（小时，0~23）",
      num(world.night.start_hour, 23),
      (value) => (world.night.start_hour = num(value, 23)),
      {
        hint: "夜晚时段的起始整点，含该点。默认 23。",
        type: "number",
        min: "0",
        max: "23",
        step: "1",
      },
    ),
  );
  nightSection._fields.appendChild(
    inputField(
      "夜晚结束（小时，0~23）",
      num(world.night.end_hour, 7),
      (value) => (world.night.end_hour = num(value, 7)),
      {
        hint: "夜晚时段的结束整点，不含该点。默认 7，即 23:00–07:00 算夜里。",
        type: "number",
        min: "0",
        max: "23",
        step: "1",
      },
    ),
  );
  nightSection._fields.appendChild(
    checkboxField(
      "睡觉只在夜里可选",
      world.night.sleep_only_at_night !== false,
      (value) => (world.night.sleep_only_at_night = value),
      {
        hint:
          "开启后，非夜晚时段不向模型提供「睡觉」动作，模型写出也会被忽略。\n" +
          "关闭后任何时段都可以睡整觉。",
      },
    ),
  );
  nightSection._fields.appendChild(
    inputField(
      "熬夜时精力衰减倍数",
      num(world.events.stay_up_penalty, 1.5),
      (value) => (world.events.stay_up_penalty = num(value, 1.5)),
      {
        hint:
          "夜间醒着、以及为处理事件推迟睡觉时，精力衰减的倍数；两处取较大值，不叠加。\n" +
          "默认 1.5；填 1 表示不惩罚。",
        type: "number",
        min: "1",
        max: "3",
        step: "0.05",
      },
    ),
  );
  form.appendChild(nightSection);

  /* --- 她自己的账 --- */
  const journalSection = settingsSection(
    "她自己的账",
    "她记着自己的经历和还没了结的事，这几段会跟着提示词一起交给主模型。",
    "时间一律写成人话（今天 / 昨天 / 前天），她自己会换算；这里只决定「往回看多久、留几条」。",
  );
  journalSection._fields.appendChild(
    inputField(
      "往前看多久（小时）",
      num(world.events.recent_window_hours, 24),
      (value) => (world.events.recent_window_hours = num(value, 24)),
      { hint: "「最近发生在我身上的事」只写这段时间内的。默认 24 小时。", type: "number", min: "1", max: "168", step: "1" },
    ),
  );
  journalSection._fields.appendChild(
    inputField(
      "最近经历最多记几条",
      num(world.events.recent_max_lines, 8),
      (value) => (world.events.recent_max_lines = num(value, 8)),
      { hint: "超了先顶掉微事件（最旧的），再顶最旧的。默认 8 条。", type: "number", min: "1", max: "30", step: "1" },
    ),
  );
  journalSection._fields.appendChild(
    inputField(
      "下一步要等多久算「挂起」（分钟）",
      num(world.events.suspend_after_minutes, 30),
      (value) => (world.events.suspend_after_minutes = num(value, 30)),
      {
        hint:
          "某件事下一步要等到超过这么久以后，就不算「我正在经历」——中间她照常做饭看书，" +
          "那件事只留一行轻提示。默认 30 分钟。",
        type: "number",
        min: "0",
        max: "1440",
        step: "5",
      },
    ),
  );
  journalSection._fields.appendChild(
    checkboxField(
      "挂起的事留一行「我心里还挂着」",
      world.events.open_thread_hint !== false,
      (value) => (world.events.open_thread_hint = value),
      { hint: "关掉 = 挂起的事完全不进提示词（她仍然记得，只是不会主动提起）。" },
    ),
  );
  journalSection._fields.appendChild(
    inputField(
      "一件旧事最多隔多久还能接着演（小时）",
      num(world.events.thread_resume_max_hours, 24),
      (value) => (world.events.thread_resume_max_hours = num(value, 24)),
      {
        hint: "超过这个时间直接收尾，并写一条「那件事后来没再提」。默认 24 小时。",
        type: "number",
        min: "1",
        max: "336",
        step: "1",
      },
    ),
  );
  form.appendChild(journalSection);

  /* --- 干涉与线索 --- */
  const interveneSection = settingsSection(
    "干涉与线索",
    "需要拿主意的事，她会开口在群里求助——等群友回话，等不到就自己动手。",
    "等待不是冻结：她照常做自己的事、照常接群聊；同类的事不会反复问。",
  );
  interveneSection._fields.appendChild(
    checkboxField(
      "允许她开口求助",
      world.events.intervene_enabled !== false,
      (value) => (world.events.intervene_enabled = value),
      { hint: "关掉 = 这类事件她直接自己拿主意，不会在群里问。" },
    ),
  );
  interveneSection._fields.appendChild(
    inputField(
      "活跃等待（秒）",
      num(world.events.active_seconds, 180),
      (value) => (world.events.active_seconds = num(value, 180)),
      { hint: "刚求助完这段时间她注意力在这件事上：有人回就优先处理。默认 180 秒。", type: "number", min: "30", max: "1800", step: "10" },
    ),
  );
  interveneSection._fields.appendChild(
    inputField(
      "总超时（分钟）",
      num(world.events.idle_minutes, 60),
      (value) => (world.events.idle_minutes = num(value, 60)),
      { hint: "活跃等待结束后降为「轻等待」：不主动提，事情仍在；到总超时收尾。默认 60 分钟。", type: "number", min: "1", max: "1440", step: "1" },
    ),
  );
  interveneSection._fields.appendChild(
    inputField(
      "宽限期（秒）",
      num(world.events.grace_seconds, 30),
      (value) => (world.events.grace_seconds = num(value, 30)),
      { hint: "她已经决定不等了之后，这几十秒内到的建议还算数。默认 30 秒。", type: "number", min: "0", max: "600", step: "5" },
    ),
  );
  interveneSection._fields.appendChild(
    checkboxField(
      "群友的建议影响判定",
      world.events.suggest_tools !== false,
      (value) => (world.events.suggest_tools = value),
      { hint: "关掉 = 群友的回应只当普通聊天，不改概率也不改她的选择。" },
    ),
  );
  interveneSection._fields.appendChild(
    inputField(
      "线索里两步之间的间隔（秒）",
      num(world.events.step_gap_seconds, 60),
      (value) => (world.events.step_gap_seconds = num(value, 60)),
      { hint: "同一件事的下一步至少隔这么久，免得一口气连演三幕。默认 60 秒。", type: "number", min: "0", max: "3600", step: "10" },
    ),
  );
  const redLineFull = el("div", "full");
  redLineFull.appendChild(
    textareaField(
      "不生成哪些事件（每行一条）",
      (world.events.red_lines || []).join("\n"),
      (value) => {
        world.events.red_lines = value
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean);
      },
      {
        hint:
          "原样写进生成提示词，例如「不写自伤」「不写违法的事」。" +
          "建议留几条，免得模型把她卷进不该有的剧情。",
        rows: 3,
      },
    ),
  );
  interveneSection._fields.appendChild(redLineFull);
  form.appendChild(interveneSection);

  /* --- 调试输出 --- */
  const debugSection = settingsSection(
    "调试输出",
    "把插件内部的决定与动作也发到群里，方便直接观察她到底做了什么。",
    "平时不要开着：这些是给调试看的，会被群友看到。",
  );
  debugSection._fields.appendChild(
    echoTypesField(world),
  );
  form.appendChild(debugSection);

  /* --- 管理员 --- */
  const adminSection = settingsSection(
    "管理员",
    "除 AstrBot 管理员之外，额外允许使用管理指令的 QQ。",
    "这些 QQ 号能用重载、重置状态、改名片、跑日程等管理指令。",
    "管理员 QQ",
  );
  adminSection._fields.appendChild(
    tagField(
      "管理员 QQ",
      world.admin_ids || [],
      (value) => {
        world.admin_ids = value;
      },
      {
        hint: "这些 QQ 号也能用管理类指令（重载、重置状态、改名片、跑日程、调试）。AstrBot 管理员始终可用。",
        placeholder: "输入 QQ 号后按回车",
        emptyText: "（没有额外管理员：只有 AstrBot 的管理员能用管理指令）",
      },
    ),
  );
  form.appendChild(adminSection);

  /* --- 上下文 --- */
  world.context = world.context || {};
  const contextSection = settingsSection(
    "上下文",
    "群聊记录的留档与提示词用量，以及超出上限时的处理。",
    "留档会持久化，重启后还在；带进提示词的那份可以更小，避免提示词越来越长。",
  );
  contextSection._fields.appendChild(
    inputField(
      "多久内算「还热乎」",
      num(world.decider.chat_window_minutes, 180),
      (value) => (world.decider.chat_window_minutes = num(value, 180)),
      {
        hint: "超过这个时间的聊天不会被带进提示词（也还是留在留档里）。单位：分钟。",
        type: "number",
        min: 1,
      },
    ),
  );
  contextSection._fields.appendChild(
    inputField(
      "留档保留多少条",
      num(world.context.chat_history_max, 300),
      (value) => (world.context.chat_history_max = num(value, 300)),
      {
        hint: "保存在数据库里的原始群聊条数，重启后可以恢复。带进提示词的只有上面那一小份。",
        type: "number",
        min: 20,
      },
    ),
  );
  contextSection._fields.appendChild(
    inputField(
      "每个会话带多少行（还没回过的）",
      num(world.context.chat_lines, 40),
      (value) => {
        world.context.chat_lines = Math.max(1, Math.round(num(value, 40)));
      },
      {
        hint: "同一个人连着说的几句算一行；每个会话各算各的，私聊聊得多不会挤掉群里的。",
        type: "number",
        min: 1,
      },
    ),
  );
  contextSection._fields.appendChild(
    inputField(
      "已回过的那批带多少行",
      num(world.context.chat_answered_lines, 40),
      (value) => {
        world.context.chat_answered_lines = Math.max(1, Math.round(num(value, 40)));
      },
      {
        hint: "「这里刚聊过的（你已经回过话了）」最多带几行，不受那 60 分钟时间窗限制。",
        type: "number",
        min: 1,
      },
    ),
  );
  contextSection._fields.appendChild(
    minuteSliderField(
      "别处同时听到的带多少行",
      num(world.context.chat_elsewhere_lines, 12),
      (value) => {
        world.context.chat_elsewhere_lines = Math.max(1, Math.round(num(value, 12)));
      },
      {
        min: 1,
        max: 40,
        step: 1,
        unit: "行",
        hint: "同一个她在别的群 / 私聊里说的话，只当背景；行数越多提示词越长（标准 12 行）。",
      },
    ),
  );
  contextSection._fields.appendChild(
    checkboxField(
      "把 AstrBot 自己的会话历史也交给她",
      world.context.use_host_history === true,
      (value) => (world.context.use_host_history = value === true),
      {
        hint: "默认不带：宿主那份里常有别的机器人的话、也可能是几小时前的，容易答非所问。",
        confirm: "打开后她会看到 AstrBot 自己记的那段会话历史（可能含别的机器人说过的话、也可能是几小时前的）。确认那段里有她需要的信息再打开。",
      },
    ),
  );
  contextSection._fields.appendChild(
    checkboxField(
      "过滤宿主无关提示词",
      world.context.filter_host_context !== false,
      (value) => (world.context.filter_host_context = value === true),
      {
        hint: "摘掉 AstrBot 自带的技能清单、Linux 运行环境说明这类跟她无关的话（调试页能看到原文）。",
      },
    ),
  );
  contextSection._fields.appendChild(
    inputField(
      "没回过的那批每条最多多少字",
      num(world.context.chat_line_chars, 500),
      (value) => (world.context.chat_line_chars = num(value, 500)),
      {
        hint: "还没回过她的消息要给足（那是她真正要读、要回的内容）；超了会截断并标明还剩多少字。",
        type: "number",
        min: 40,
      },
    ),
  );
  contextSection._fields.appendChild(
    inputField(
      "已经回过的那批每条最多多少字",
      num(world.context.chat_answered_line_chars, 200),
      (value) => (world.context.chat_answered_line_chars = num(value, 200)),
      {
        hint: "已经回过话的那批、以及别处同时听到的：只当背景，短一点省 token。",
        type: "number",
        min: 20,
      },
    ),
  );
  contextSection._fields.appendChild(
    inputField(
      "聊天记录总字数上限",
      num(world.context.chat_total_chars, 16000),
      (value) => (world.context.chat_total_chars = num(value, 16000)),
      {
        hint: "整段聊天记录（这里 + 已回过 + 别处）的字数预算：超了先丢最早的背景。",
        type: "number",
        min: 400,
      },
    ),
  );
  contextSection._fields.appendChild(
    inputField(
      "「刚聊过什么」有效期（分钟）",
      num(world.context.chat_note_max_minutes, 30),
      (value) => {
        world.context.chat_note_max_minutes = Math.max(0, Math.round(num(value, 30)));
      },
      {
        hint: "她上一轮写的那句话题背景最多挂多久，超时就不带了；0 = 不限。",
        type: "number",
        min: 0,
      },
    ),
  );
  contextSection._fields.appendChild(
    inputField(
      "引用旧消息最多写多少字",
      num(world.context.quote_chars, 1000),
      (value) => (world.context.quote_chars = num(value, 1000)),
      {
        hint: "他引用的那条已经不在聊天记录里时，把原文写进提示词的上限（0 = 不写）。",
        type: "number",
        min: 0,
      },
    ),
  );
  contextSection._fields.appendChild(
    pillsField(
      "留档超出上限时的处理",
      world.context.chat_overflow || "compress",
      [
        {
          key: "discard",
          label: "直接丢弃最早的",
          hint: "不额外调用模型，最省；代价是更早的聊天内容会丢失。",
        },
        {
          key: "compress",
          label: "压成摘要",
          hint: "用「上下文压缩模型」把较早的聊天压成一段摘要，摘要会一直带在提示词里。",
        },
      ],
      (value) => {
        world.context.chat_overflow = value;
        renderSettings();
      },
      {},
    ),
  );
  if ((world.context.chat_overflow || "discard") === "compress") {
    contextSection._fields.appendChild(
      inputField(
        "攒到多少条开始压缩",
        num(world.context.chat_compress_threshold, 200),
        (value) => (world.context.chat_compress_threshold = num(value, 200)),
        { hint: "留档达到这个条数才会触发一次压缩。", type: "number", min: 10 },
      ),
    );
    contextSection._fields.appendChild(
      inputField(
        "压缩后保留多少条原文",
        num(world.context.chat_keep_after_compress, 30),
        (value) => (world.context.chat_keep_after_compress = num(value, 30)),
        { hint: "被压掉的是比这更早的那些。", type: "number", min: 1 },
      ),
    );
    contextSection._fields.appendChild(
      inputField(
        "两次压缩至少间隔（分钟）",
        num(world.context.summary_refresh_minutes, 10),
        (value) => (world.context.summary_refresh_minutes = num(value, 10)),
        { hint: "避免攒够一次就压一次，模型调用会太频繁。", type: "number", min: 1 },
      ),
    );
  }
  contextSection._fields.appendChild(
    inputField(
      "一次最多带几张图（没配转述模型时）",
      num(world.context.image_max, 3),
      (value) => (world.context.image_max = Math.max(1, Math.round(num(value, 3)))),
      {
        hint:
          "一次回复最多把几张图直接交给能看图的主模型：超出的先来的旧图会转述成文字。",
        type: "number",
        min: 1,
      },
    ),
  );
  contextSection._fields.appendChild(
    pillsField(
      "聊天记录里的图",
      world.context.chat_image_inline || "auto",
      [
        {
          key: "auto",
          label: "自动",
          hint: "读 AstrBot 里主模型勾选的模态：支持图像就直接把聊天记录里的图发过去，读不到就不发。",
        },
        {
          key: "always",
          label: "总是发",
          hint: "不管检测结果都发（确认主模型能吃图时用）；主模型不支持时会退回纯文字重试。",
        },
        {
          key: "never",
          label: "只发描述",
          hint: "图只以图片转述的文字形式出现，不占用主模型的图像输入。",
        },
      ],
      (value) => {
        world.context.chat_image_inline = value;
        renderSettings();
      },
      {},
    ),
  );
  form.appendChild(contextSection);

  /* --- 用户画像与睡眠整理 --- */
  const profileSection = settingsSection(
    "用户画像",
    "她认识的人：画像、关系、好感度，以及睡着时怎么整理记忆。",
    "画像会在每一轮提示词里给出「你在跟谁说话」；睡眠整理把这段时间的经历消化成记忆与画像，" +
      "一段睡眠最多两次（睡下 20 分钟一次、睡满 5 小时补一次）。",
  );
  profileSection._fields.appendChild(
    pillsField(
      "用户画像",
      world.profile && world.profile.enabled === false ? "off" : "on",
      [
        { key: "on", label: "开", hint: "记画像、记关系与好感度，并写进提示词。" },
        { key: "off", label: "关", hint: "完全不记，也不往提示词里带。" },
      ],
      (value) => {
        world.profile = world.profile || {};
        world.profile.enabled = value === "on";
        renderSettings();
      },
      {},
    ),
  );
  profileSection._fields.appendChild(
    pillsField(
      "睡眠整理",
      world.profile && world.profile.consolidate_enabled === false ? "off" : "on",
      [
        { key: "on", label: "开", hint: "她睡着时消化这段时间的经历（记忆 + 画像）。" },
        { key: "off", label: "关", hint: "不整理；记忆只按日常节奏累积。" },
      ],
      (value) => {
        world.profile = world.profile || {};
        world.profile.consolidate_enabled = value === "on";
        renderSettings();
      },
      {},
    ),
  );
  profileSection._fields.appendChild(
    inputField(
      "睡下多久开始整理（分钟）",
      num(world.profile && world.profile.sleep_consolidate_minutes, 20),
      (value) => {
        world.profile = world.profile || {};
        world.profile.sleep_consolidate_minutes = Math.max(
          1,
          Math.round(num(value, 20)),
        );
      },
      {
        hint: "睡沉了再整理：一段睡眠的第一次整理在这里。",
        type: "number",
        min: 1,
      },
    ),
  );
  profileSection._fields.appendChild(
    inputField(
      "睡满多久补一次（分钟，0 = 只整理一次）",
      num(world.profile && world.profile.sleep_consolidate_late_minutes, 300),
      (value) => {
        world.profile = world.profile || {};
        world.profile.sleep_consolidate_late_minutes = Math.max(
          0,
          Math.round(num(value, 300)),
        );
      },
      {
        hint: "睡到后半段补一次；一段睡眠最多两次。",
        type: "number",
        min: 0,
      },
    ),
  );
  profileSection._fields.appendChild(
    pillsField(
      "小睡也轻整理",
      world.profile && world.profile.nap_consolidate === false ? "off" : "on",
      [
        { key: "on", label: "开", hint: "小睡做要点化 + 刷新缩略版，不碰关系与事实；梦另有一个开关管着（默认开着）。" },
        { key: "off", label: "关", hint: "小睡完全不整理。" },
      ],
      (value) => {
        world.profile = world.profile || {};
        world.profile.nap_consolidate = value === "on";
        renderSettings();
      },
      {},
    ),
  );
  profileSection._fields.appendChild(
    inputField(
      "提示词里最多带几个其他人",
      num(world.profile && world.profile.digest_limit, 5),
      (value) => {
        world.profile = world.profile || {};
        world.profile.digest_limit = Math.max(1, Math.round(num(value, 5)));
      },
      { hint: "当前说话人永远是全文；群里其他人各给一行缩略版。", type: "number", min: 1 },
    ),
  );
  profileSection._fields.appendChild(el("div", "sub-title", "想念"));
  [
    [
      "想念增长/分钟",
      "miss_growth_per_min",
      0.0006,
      "只在她没跟这个人说话时才涨；越大越容易想起某个人。",
      "0.0001",
    ],
    [
      "孤独加成",
      "miss_loneliness_weight",
      0.8,
      "越孤独涨得越快：系数 = 1 + 这个值 × 孤独感（默认 0.8，最多快 1.8 倍）。",
      "0.1",
    ],
    [
      "多少开始想他",
      "miss_threshold",
      0.6,
      "想念超过这个值才写进提示词（「你有点想他们了」）。",
      "0.05",
    ],
    [
      "提示词里最多写几个人",
      "miss_limit",
      3,
      "超过阈值的按想念程度取前几名。",
      "1",
    ],
    [
      "熟到哪一档才会想他",
      "miss_min_level",
      3,
      "关系档位下标，3 = 熟人（默认）：还停在「客气」的人不会出现在想念里；0 = 不限。",
      "1",
    ],
    [
      "聊完多久才会再想他（最短/最长，分钟）",
      "miss_cooldown_min_minutes",
      45,
      "刚聊过（或刚去找过他）之后先等一段，在这两个值之间随机取。",
      "5",
    ],
  ].forEach(([label, key, fallback, hint, step]) => {
    profileSection._fields.appendChild(
      inputField(
        label,
        num(world.profile && world.profile[key], fallback),
        (value) => {
          world.profile = world.profile || {};
          world.profile[key] = Math.max(0, num(value, fallback));
        },
        { hint, type: "number", step },
      ),
    );
  });
  profileSection._fields.appendChild(
    inputField(
      "想念冷却上限（分钟）",
      num(world.profile && world.profile.miss_cooldown_max_minutes, 240),
      (value) => {
        world.profile = world.profile || {};
        world.profile.miss_cooldown_max_minutes = Math.max(0, Math.round(num(value, 240)));
      },
      { hint: "和上面那个「最短」一起组成随机区间，上限不会小于下限。", type: "number", step: "5" },
    ),
  );
  profileSection._fields.appendChild(
    pillsField(
      "想他想得不行就主动找他",
      world.profile && world.profile.miss_push_enabled === false ? "off" : "on",
      [
        { key: "on", label: "开", hint: "想念到「软推」阈值时，她自己决定去哪说（私聊或群里）。" },
        { key: "off", label: "关", hint: "只在提示词里提一句，不主动找。" },
      ],
      (value) => {
        world.profile = world.profile || {};
        world.profile.miss_push_enabled = value === "on";
        renderSettings();
      },
      {},
    ),
  );
  profileSection._fields.appendChild(el("div", "sub-title", "主动问"));
  profileSection._fields.appendChild(
    pillsField(
      "主动问还不知道的事",
      world.profile && world.profile.ask_about_enabled === false ? "off" : "on",
      [
        {
          key: "on",
          label: "开",
          hint: "关系够熟、好奇心又高的时候，她自己找机会问一句对方的事。",
        },
        { key: "off", label: "关", hint: "永远不问，画像只能靠她平时的观察。" },
      ],
      (value) => {
        world.profile = world.profile || {};
        world.profile.ask_about_enabled = value === "on";
        renderSettings();
      },
      {},
    ),
  );
  [
    [
      "熟到第几档才问",
      "ask_about_min_level",
      3,
      "分级表的下标（0 起）：不够熟就不打听，免得像查户口。",
      "1",
    ],
    [
      "每天最多问几件",
      "ask_about_daily_max",
      3,
      "整个会话组一天的总数上限。",
      "1",
    ],
    [
      "对同一个人隔多久再问（小时）",
      "ask_about_person_gap_hours",
      24,
      "同一个人一次只提一件，免得一晚上把生日年龄挨个问一遍。",
      "1",
    ],
    [
      "同一件隔几天才能再问",
      "ask_about_cooldown_days",
      7,
      "问过的记一笔，这段时间里不再重复提。",
      "1",
    ],
  ].forEach(([label, key, fallback, hint, step]) => {
    profileSection._fields.appendChild(
      inputField(
        label,
        num(world.profile && world.profile[key], fallback),
        (value) => {
          world.profile = world.profile || {};
          world.profile[key] = Math.max(0, Math.round(num(value, fallback)));
        },
        { hint, type: "number", step },
      ),
    );
  });
  profileSection._fields.appendChild(
    textareaField(
      "想打听的事（一行一个）",
      (
        (world.profile && world.profile.ask_about_fields) || [
          "性别",
          "生日",
          "年龄",
          "爱吃的东西",
          "所在地",
        ]
      ).join("\n"),
      (value) => {
        world.profile = world.profile || {};
        world.profile.ask_about_fields = value
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean);
      },
      {
        rows: 4,
        hint: "留空 = 不问；已经记进画像的那件不会再问。",
      },
    ),
  );
  profileSection._fields.appendChild(el("div", "sub-title", "记仇"));
  profileSection._fields.appendChild(
    pillsField(
      "会不会记着他一笔账",
      world.profile && world.profile.grudge_enabled === false ? "off" : "on",
      [
        { key: "on", label: "开", hint: "他做了让你生气的事（未兑现承诺、失约等），记录一条。" },
        { key: "off", label: "关", hint: "不记，什么都当场过去。" },
      ],
      (value) => {
        world.profile = world.profile || {};
        world.profile.grudge_enabled = value === "on";
        renderSettings();
      },
      {},
    ),
  );
  [
    ["一笔账记几天", "grudge_days", 7, "到期自己就淡了，并写进记忆。", "1"],
    ["同时最多记几笔", "grudge_max", 2, "整个会话组算一份；同一个人最多一笔。", "1"],
    ["一天最多记几笔", "grudge_daily_max", 1, "不让它变成天天记仇。", "1"],
    ["气着时降几档亲密", "grudge_level_drop", 1, "只在这一档的判定上降，关系与好感数值都不动。", "1"],
  ].forEach(([label, key, fallback, hint, step]) => {
    profileSection._fields.appendChild(
      inputField(
        label,
        num(world.profile && world.profile[key], fallback),
        (value) => {
          world.profile = world.profile || {};
          world.profile[key] = Math.max(0, Math.round(num(value, fallback)));
        },
        { hint, type: "number", step },
      ),
    );
  });
  profileSection._fields.appendChild(el("div", "sub-title", "她自己的事"));
  profileSection._fields.appendChild(
    checkboxField(
      "记她自己答应过、想做的事",
      !(world.state_dynamics && world.state_dynamics.own_topic_enabled === false),
      (value) => {
        world.state_dynamics = world.state_dynamics || {};
        world.state_dynamics.own_topic_enabled = value;
      },
      { hint: "「答应给他看照片」这种她自己还没做的事，她会记着并找机会做掉。" },
    ),
  );
  [
    ["同时最多记几件", "own_topic_max", 2, "多了她会变成一个待办清单。", "1"],
    ["一件记几天", "own_topic_days", 3, "到期后清除：要么早已完成，要么她并不在意。", "1"],
  ].forEach(([label, key, fallback, hint, step]) => {
    profileSection._fields.appendChild(
      inputField(
        label,
        num(world.state_dynamics && world.state_dynamics[key], fallback),
        (value) => {
          world.state_dynamics = world.state_dynamics || {};
          world.state_dynamics[key] = Math.max(0, Math.round(num(value, fallback)));
        },
        { hint, type: "number", step },
      ),
    );
  });
  profileSection._fields.appendChild(
    textareaField(
      "称呼黑名单（一行一个）",
      ((world.profile && world.profile.call_name_blacklist) || []).join("\n"),
      (value) => {
        world.profile = world.profile || {};
        world.profile.call_name_blacklist = value
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean);
      },
      {
        rows: 3,
        hint: "命中的称呼直接拒绝，她不会这么叫他。",
      },
    ),
  );
  profileSection._fields.appendChild(bondsEditor(world));
  profileSection._fields.appendChild(levelsEditor(world));
  form.appendChild(profileSection);

  /* --- 图片转述 --- */
  const visionSection = settingsSection(
    "图片转述",
    "在插件配置里选好「图片转述模型」之后，群里发送的图片会先转成文字再进入上下文。",
    "只把画面写成文字：类型、文字、画面里真有的东西。" +
      "与话题无关，因此可以按图片缓存，同一张图片只识别一次；" +
      "这张图和正在聊的事有什么关系，交给主模型自己判断。",
  );
  // 看图提示词用内置默认，编辑器不提供修改入口（升级后识别效果才稳定）
  visionSection._fields.appendChild(
    el("p", "hint", "看图使用内置提示词，编辑器不提供修改。"),
  );
  visionSection._fields.appendChild(
    checkboxField(
      "同一张图只识别一次（推荐）",
      world.vision.cache_enabled !== false,
      (value) => (world.vision.cache_enabled = value),
      {
        hint: "按图片内容缓存转述结果：表情包、梗图第二次起复用缓存，缓存持久，多图合并成一次调用。",
      },
    ),
  );
  visionSection._fields.appendChild(
    inputField(
      "最多记住多少张图",
      num(world.vision.cache_max, 500),
      (value) => (world.vision.cache_max = num(value, 500)),
      { hint: "超出后按最近使用时间淘汰。", type: "number", min: 20 },
    ),
  );
  visionSection._fields.appendChild(
    inputField(
      "缓存保留多少天",
      num(world.vision.cache_days, 30),
      (value) => (world.vision.cache_days = num(value, 30)),
      { hint: "过期后重新识别一次。填 0 表示不过期。", type: "number", min: 0 },
    ),
  );
  const cacheStats = (ui.config && ui.config.vision_cache) || {};
  visionSection._fields.appendChild(
    el(
      "p",
      "muted full",
      `已记住 ${num(cacheStats.entries, 0)} 张图，累计省下 ${num(
        cacheStats.hits,
        0,
      )} 次识别（本次运行 ${num(cacheStats.session_hits, 0)} 次）。`,
    ),
  );
  const media = (ui.config && ui.config.media) || {};
  // 看图接话（S6）：不依赖意图路由，她自己看一眼觉得有意思就接一句
  visionSection._fields.appendChild(
    checkboxField(
      "看到有意思的图会自己接一句",
      world.media.reaction_enabled !== false,
      (value) => (world.media.reaction_enabled = value),
      {
        hint: "群里有人发图、没点她名：她自己看一眼，觉得有意思就在下一拍接一句。不装意图路由也生效。",
      },
    ),
  );
  visionSection._fields.appendChild(
    minuteSliderField(
      "同一个群两次之间至少隔",
      num(world.media.reaction_cooldown_minutes, 15),
      (value) => (world.media.reaction_cooldown_minutes = value),
      { hint: "标准 15 分钟；拖到 0 就是不限。" },
    ),
  );
  visionSection._fields.appendChild(
    inputField(
      "几分钟内还值得开口",
      num(world.media.reaction_window_minutes, 5),
      (value) => (world.media.reaction_window_minutes = num(value, 5)),
      { hint: "收到图之后过了这么久还没说上话，就不再补了。", type: "number", min: 1 },
    ),
  );
  visionSection._fields.appendChild(
    inputField(
      "多有意思才值得开口（0~3）",
      num(world.media.reaction_interest, 2),
      (value) => (world.media.reaction_interest = num(value, 2)),
      { hint: "0 = 不评，来图就说；2 = 有点意思才说；3 = 只对特别好笑的开口。", type: "number", min: 0, max: 3 },
    ),
  );
  const mediaCount = num(media.count, 0);
  const mediaBytes = num(media.total_bytes, 0);
  const mediaBox = el("div", "full");
  mediaBox.appendChild(
    el(
      "p",
      "muted",
      mediaCount
        ? `本机留了 ${mediaCount} 张图（约 ${formatBytes(mediaBytes)}），保留 ${num(
            (media.limits || {}).retention_days,
            14,
          )} 天、最多 ${num((media.limits || {}).max_total_mb, 512)} MB。`
        : "本机图片资产库还是空的：收进来的图会各留一份副本，平台的临时地址过期了也还找得回来。",
    ),
  );
  const mediaClean = el("button", "ghost small", "清理过期图片…");
  mediaClean.addEventListener("click", async () => {
    try {
      const plan = await apiPost("media/gc", {});
      const count = num(plan.removed_count, 0);
      if (!count) {
        toast("没有需要清理的图片（在保留期内、或者正被引用）");
        return;
      }
      // 插件页在 sandbox iframe 里（没有 allow-modals）：裸 confirm 会被浏览器直接
      // 拦掉并返回 false，点了像"没反应"。统一走 confirmDialog。
      const ok = await confirmDialog({
        title: "清理过期图片",
        message:
          `这一下会删掉 ${count} 张图片（约 ${formatBytes(num(plan.removed_bytes, 0))}）。\n` +
            "只删本机缓存里没人引用的副本，聊天记录里的原图不受影响。要清理吗？",
      });
      if (!ok) {
        return;
      }
      const done = await apiPost("media/gc", { confirm: true });
      toast(`已清理 ${num(done.removed_count, 0)} 张图片`);
      await loadAll();
    } catch (error) {
      toast(error.message || "清理失败");
    }
  });
  mediaBox.appendChild(mediaClean);
  visionSection._fields.appendChild(mediaBox);
  visionSection._fields.appendChild(
    checkboxField(
      "合并转发压成摘要",
      world.vision.forward_summary !== false,
      (value) => {
        world.vision.forward_summary = value;
        renderSettings();
      },
      {
        hint: "把转发的聊天记录（含里面的图）交给看图模型读一遍，压成一段摘要替换进聊天记录；关掉则只落一句「这是一条转发的聊天记录」。",
      },
    ),
  );
  if (world.vision.forward_summary !== false) {
    visionSection._fields.appendChild(
      inputField(
        "摘要最多多少字",
        num(world.vision.forward_max_chars, 300),
        (value) =>
          (world.vision.forward_max_chars = Math.max(
            80,
            Math.round(num(value, 300)),
          )),
        { hint: "超出的部分截断，摘要太短会丢掉细节。", type: "number", min: 80 },
      ),
    );
  }
  form.appendChild(visionSection);

  /* --- 简易人设（给打杂模型） --- */
  const briefSection = settingsSection(
    "简易人设（给打杂模型）",
    "生成事件、整理结果时给打杂模型的人设摘要：只留说话风格，不带世界观和背景故事。",
    "按人格缓存：换人格或改主人设之后要重新生成一次（键跟着人设内容变）。没生成过就退回主人设前 200 字。",
    "persona_brief",
  );
  const briefFull = el("div", "full");
  const briefArea = document.createElement("textarea");
  briefArea.rows = 4;
  briefArea.id = "persona-brief-text";
  briefArea.placeholder = "还没有生成，点下面的按钮从主人设生成一段";
  briefFull.appendChild(fieldHead("简易人设"));
  briefFull.appendChild(briefArea);
  const briefRow = el("div", "row");
  const briefGenerate = el("button", "small primary", "从主人设生成");
  briefGenerate.type = "button";
  briefGenerate.addEventListener("click", async () => {
    const sessionId = $("status-session") ? $("status-session").value : "";
    if (!sessionId) {
      toast("先在「实时状态」里选一个会话");
      return;
    }
    briefGenerate.disabled = true;
    briefGenerate.textContent = "生成中…";
    try {
      const data = await apiPost("persona-brief", { session: sessionId, generate: true });
      const state = (data && data.persona_brief) || {};
      briefArea.value = state.brief || "";
      toast("已生成，点右上角「保存」后生效。");
    } catch (error) {
      toast(`生成失败：${error.message || error}`);
    } finally {
      briefGenerate.disabled = false;
      briefGenerate.textContent = "从主人设生成";
    }
  });
  const briefSave = el("button", "small ghost", "保存到这个人格");
  briefSave.type = "button";
  briefSave.addEventListener("click", async () => {
    const sessionId = $("status-session") ? $("status-session").value : "";
    if (!sessionId) {
      toast("先在「实时状态」里选一个会话");
      return;
    }
    try {
      await apiPost("persona-brief", { session: sessionId, brief: briefArea.value });
      toast("已保存（立即生效）");
    } catch (error) {
      toast(`保存失败：${error.message || error}`);
    }
  });
  briefRow.appendChild(briefGenerate);
  briefRow.appendChild(briefSave);
  briefFull.appendChild(briefRow);
  const briefNote = el("p", "muted", "正在读取…");
  briefFull.appendChild(briefNote);
  briefFull.appendChild(
    el("p", "muted", "提示：这段摘要给打杂模型用，主人格用的是完整人设。它不跟预设走，按人格单独存。")
  );
  // 打开页面就把这一份读出来：以前只在点过"生成"之后才填，重开页面看着像是被重置了
  async function loadPersonaBrief({ overwrite = false } = {}) {
    const sessionId = $("status-session") ? $("status-session").value : "";
    if (!sessionId) {
      briefNote.textContent = "先在「实时状态」里选一个会话。";
      return;
    }
    try {
      const data = await apiGet("persona-brief", { session: sessionId });
      const state = (data && data.persona_brief) || {};
      const brief = String(state.brief || "");
      if (!brief) {
        const latest = (state.latest && state.latest.brief) || "";
        const latestAt = Number((state.latest && state.latest.at) || 0);
        if (latest) {
          if (overwrite || !briefArea.value.trim()) briefArea.value = latest;
          const when = latestAt ? `（${agoText(Date.now() / 1000 - latestAt)}）` : "";
          briefNote.textContent =
            `当前会话读出的人设和这份不一致${when}，先显示上次那份；` +
            "点「从主人设生成」会按现在的人设重写。";
        } else {
          briefNote.textContent =
            "还没有生成过：打杂模型会退回到主人设前 200 字。";
        }
        return;
      }
      if (overwrite || !briefArea.value.trim()) briefArea.value = brief;
      const source = state.source === "generated" ? "由主人设生成" : "手动保存";
      const at = Number(state.at || 0);
      const when = at ? ` · ${agoText(Date.now() / 1000 - at)}` : "";
      briefNote.textContent =
        `${source}${when}（按 ${Number(state.persona_chars || 0)} 字的人设）`;
    } catch (error) {
      briefNote.textContent = `读取失败：${error.message || error}`;
    }
  }
  ui.loadPersonaBrief = loadPersonaBrief;
  loadPersonaBrief();
  briefSection._fields.appendChild(briefFull);
  // 「她这个人」相关的设置都收在人设那一节下面：简易人设紧跟主人设
  if (personaSection && personaSection.parentElement === form) {
    personaSection.insertAdjacentElement("afterend", briefSection);
  } else {
    form.appendChild(briefSection);
  }

  /* --- 天气 --- */
  world.weather = world.weather || {};
  const weather = settingsSection(
    "天气",
    "她所在城市的天气：后台每隔几小时静默查一次，查到的结果会当背景写进提示词。",
    "填了城市就按它查，不用模型猜；超过「多旧就不再提」的时间后，提示词里不再带这份天气。" +
      "当前天气也会显示在地图页顶部。",
  );
  weather._fields.appendChild(
    checkboxField(
      "启用天气",
      world.weather.enabled !== false,
      (value) => (world.weather.enabled = value),
      { hint: "关闭后不后台查天气、也不写进提示词；她主动查天气照常可用。" },
    ),
  );
  weather._fields.appendChild(
    inputField(
      "所属城市",
      world.weather.city || "",
      (value) => (world.weather.city = value.trim()),
      {
        hint: "例如「武汉」。留空就让辅助模型按聊天内容推断，容易猜偏，建议填上。",
        placeholder: "例如 武汉",
      },
    ),
  );
  weather._fields.appendChild(
    inputField(
      "每隔几小时查一次",
      num(world.weather.refresh_hours, 2),
      (value) => (world.weather.refresh_hours = Math.max(0, num(value, 2))),
      {
        hint: "默认 2 小时。她主动查过天气后，这个倒计时从头算。填 0 = 不在后台查。",
        type: "number",
        min: 0,
      },
    ),
  );
  weather._fields.appendChild(
    inputField(
      "多旧就不再提",
      num(world.weather.stale_hours, 24),
      (value) => (world.weather.stale_hours = Math.max(0, num(value, 24))),
      {
        hint: "默认 24 小时；超过这个时长的天气不写进提示词，横幅仍显示并标注时间。",
        type: "number",
        min: 0,
      },
    ),
  );
  weather._fields.appendChild(
    checkboxField(
      "把结果整理成一行",
      world.weather.normalize !== false,
      (value) => (world.weather.normalize = value),
      {
        hint: "查询结果先压成「城市｜温度｜天气｜湿度｜风力｜预报」，横幅和提示词都用这一行；关闭则存原文。",
      },
    ),
  );
  form.appendChild(weather);

  /* --- 群名片 --- */
  const nickname = settingsSection(
    "群名片同步",
    "把她的状态显示在群名片上（例如「小鲸鱼 | 睡觉中」）。只有部分平台支持。",
    "不支持的平台会自动跳过；私聊不处理。",
  );
  nickname._fields.appendChild(
    checkboxField(
      "启用群名片同步",
      world.nickname_sync.enabled !== false,
      (value) => (world.nickname_sync.enabled = value),
      { hint: "关闭后插件不会改她的名片。" },
    ),
  );
  nickname._fields.appendChild(
    inputField("名片模板", world.nickname_sync.template || "{base} | {status}", (value) => {
      world.nickname_sync.template = value;
    }, { hint: "{base} 是原名，{status} 是状态文案，例如「{base}（{status}）」。" }),
  );
  nickname._fields.appendChild(
    inputField("名片最大长度", num(world.nickname_sync.max_length, 30), (value) => {
      world.nickname_sync.max_length = num(value, 30);
    }, { hint: "超出会被截断，避免超过平台限制。", type: "number" }),
  );
  nickname._fields.appendChild(
    inputField("改名冷却（秒）", num(world.nickname_sync.cooldown_seconds, 60), (value) => {
      world.nickname_sync.cooldown_seconds = num(value, 60);
    }, { hint: "两次改名片之间至少隔多久，防止频繁调用平台接口。", type: "number" }),
  );
  nickname._fields.appendChild(
    inputField(
      "事件进行中显示什么",
      world.nickname_sync.event_text ?? "事件中",
      (value) => (world.nickname_sync.event_text = value),
      {
        hint: "她手上有一件没演完的事时，名片显示这截文字（默认「事件中」）；留空则不特别标注。",
      },
    ),
  );
  nickname._fields.appendChild(
    el(
      "p",
      "muted full",
      "「显示什么文案」现在写在它自己身上：动作编辑页的「执行中名片文案」、地点属性里的「在这里时名片文案」；" +
        "这里只保留开关、模板这些通用规则。",
    ),
  );
  form.appendChild(nickname);

  /* --- 内容安全 --- */
  const safety = settingsSection(
    "内容安全与隐私",
    "命中屏蔽词的消息不会进入世界（也不会进她的提示词）。",
    "用户还可以在群里用 /vw forget me 删除关于自己的记忆。",
  );
  const safetyFull = el("div", "full");
  safetyFull.appendChild(
    textareaField(
      "屏蔽词（每行一个）",
      (world.content_safety.blocked_words || []).join("\n"),
      (value) => {
        world.content_safety.blocked_words = value
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean);
      },
      { hint: "整条消息包含任意一个词就跳过处理。", rows: 3 },
    ),
  );
  safety._fields.appendChild(safetyFull);
  const blockFull = el("div", "full");
  blockFull.appendChild(
    textareaField(
      "会话黑名单（每行一个）",
      (world.content_safety.session_blocklist || []).join("\n"),
      (value) => {
        world.content_safety.session_blocklist = value
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean);
      },
      { hint: "这些会话即使在白名单里也不生效。", rows: 2 },
    ),
  );
  safety._fields.appendChild(blockFull);
  form.appendChild(safety);

  /* --- 高级：原始 JSON --- */
  const advanced = document.createElement("details");
  advanced.className = "raw-json";
  advanced.id = "world-json-editor";
  advanced.appendChild(el("summary", "", "高级：直接编辑 world.json"));
  const rawArea = document.createElement("textarea");
  rawArea.rows = 12;
  rawArea.value = JSON.stringify(world, null, 2);
  const applyButton = el("button", "small primary", "应用这段 JSON");
  applyButton.type = "button";
  applyButton.addEventListener("click", () => {
    try {
      const parsed = JSON.parse(rawArea.value);
      ui.config.world = parsed;
      renderEverything();
      toast("已应用到编辑器，点右上角「保存」后生效。");
    } catch (error) {
      toast(`JSON 解析失败：${error.message}`);
    }
  });
  advanced.appendChild(rawArea);
  advanced.appendChild(applyButton);
  form.appendChild(advanced);

  // 低频但一个都不能少的字段收进「高级设置」，页面第一眼只剩要改的东西
  collapseAdvancedFields(form);
  // 分类标签：只影响显示，不影响存下来的数据结构
  renderSettingsMode();
}

async function savePassword() {
  try {
    await apiPost("auth/password", {
      old_password: $("pwd-old").value,
      new_password: $("pwd-new").value,
    });
    toast("密码已更新");
    $("pwd-old").value = "";
    $("pwd-new").value = "";
  } catch (error) {
    toast(error.message || "修改密码失败");
  }
}

async function restoreDefault() {
  const ok = await confirmDialog({
    title: "恢复默认配置？",
    message: "地图、动作、日程、会话白名单、全局设置都会回到初始状态。记忆（state.db）不会被删除。",
    confirmText: "恢复默认",
  });
  if (!ok) return;
  try {
    await apiPost("restore-default", { scope: "all" });
    toast("已恢复默认配置");
    await loadAll();
  } catch (error) {
    toast(error.message || "恢复失败");
  }
}

/* ================================================================== */
/* 调试                                                                */
/* ================================================================== */

/** 把工具的参数定义归一成 {properties, required}（各家写法不一样，跟后端同一套规则）。 */
function normalizeToolSchema(raw) {
  if (Array.isArray(raw)) {
    const properties = {};
    const required = [];
    raw.forEach((item) => {
      if (!item || typeof item !== "object") return;
      const name = String(item.name || item.key || "").trim();
      if (!name) return;
      properties[name] = { description: String(item.description || "") };
      if (item.required) required.push(name);
    });
    return { properties, required };
  }
  if (!raw || typeof raw !== "object") return { properties: {}, required: [] };
  const reserved = ["type", "properties", "required", "title", "description", "additionalProperties", "$schema"];
  let properties = raw.properties;
  if (!properties || typeof properties !== "object") {
    const guessed = {};
    Object.keys(raw).forEach((key) => {
      if (!reserved.includes(key)) guessed[key] = raw[key];
    });
    properties = guessed;
  }
  const required = Array.isArray(raw.required) ? raw.required.map(String) : [];
  Object.keys(properties).forEach((key) => {
    const spec = properties[key];
    if (spec && typeof spec === "object" && spec.required === true && !required.includes(key)) {
      required.push(key);
    }
  });
  return { properties, required };
}

/** 工具参数里到底声明了哪些必填：空的话要显眼地写出来。 */
function toolRequiredSummary(tool) {
  const schema = normalizeToolSchema(tool.parameters);
  const names = Object.keys(schema.properties || {});
  if (!names.length) return "必填：无（工具没声明任何参数）";
  if (!schema.required.length) {
    return `必填：未声明（工具声明了 ${names.join("、")}，但都没标必填——插件会自动尝试补全，缺了会带报错重试一次）`;
  }
  return `必填：${schema.required.join("、")}`;
}

/* ================================================================== */
/* 预设：成套的世界配置                                                */
/* ================================================================== */

async function loadPresets() {
  const list = $("preset-list");
  if (!list) return;
  list.innerHTML = "";
  try {
    const data = await apiGet("presets");
    ui.presets = data.presets || [];
    ui.activePreset = data.active || "";
    renderPresetList();
  } catch (error) {
    list.appendChild(el("p", "muted", error.message || "读取预设失败"));
  }
}

function renderPresetList() {
  const list = $("preset-list");
  list.innerHTML = "";
  if (!(ui.presets || []).length) {
    list.appendChild(
      el("p", "muted", "还没有预设。改好当前配置后点「把当前配置存成预设」，就能随时切回来。"),
    );
    return;
  }
  ui.presets.forEach((preset) => {
    const item = el("div", "list-item");
    const info = el("div", "grow");
    const head = el("div");
    head.appendChild(el("strong", "", preset.name || preset.id));
    if (preset.id === ui.activePreset) head.appendChild(el("span", "badge", "当前"));
    info.appendChild(head);
    info.appendChild(
      el(
        "div",
        "meta",
        `${preset.id}｜${preset.nodes} 地点 / ${preset.actions} 动作 / ${preset.schedules} 日程 / ${preset.sessions} 会话` +
          (preset.updated_at ? `｜${preset.updated_at}` : ""),
      ),
    );
    if (preset.note) info.appendChild(el("div", "meta", preset.note));
    item.appendChild(info);

    const actions = el("div", "row-item");
    const apply = el("button", "small primary", "应用");
    apply.type = "button";
    apply.title = "切换到这个预设（会清空所有会话的状态，记忆和日志保留）";
    apply.addEventListener("click", () => applyPreset(preset));
    actions.appendChild(apply);

    const rename = el("button", "small", "重命名");
    rename.type = "button";
    rename.addEventListener("click", () => renamePreset(preset));
    actions.appendChild(rename);

    const json = el("button", "small ghost", "JSON");
    json.type = "button";
    json.title = "看 / 改 / 复制这个预设的 JSON（方便导入导出）";
    json.addEventListener("click", () => editPresetJson(preset));
    actions.appendChild(json);

    const remove = el("button", "small danger", "删除");
    remove.type = "button";
    remove.addEventListener("click", () => deletePreset(preset));
    actions.appendChild(remove);
    item.appendChild(actions);
    list.appendChild(item);
  });
}

async function saveCurrentAsPreset() {
  openFormDialog({
    title: "把当前配置存成预设",
    hint: "会把当前的地图 / 动作 / 日程 / 会话白名单打包成一个预设文件。",
    fields: [
      { key: "id", label: "预设 id", value: `preset_${Date.now().toString(36).slice(-4)}`, hint: "文件名，用英文/数字" },
      { key: "name", label: "名字", value: "", hint: "给自己看的名字，例如「家里蹲版」" },
      { key: "note", label: "说明", value: "", hint: "可选" },
    ],
    confirmText: "保存",
    onSubmit: async (values) => {
      try {
        const result = await apiPost("presets/save", values);
        toast(`已保存预设 ${result.id}`);
        await loadPresets();
      } catch (error) {
        toast(error.message || "保存失败");
        return false;
      }
    },
  });
}

/** 用内置默认世界新建一份预设：不覆盖当前配置，应用以后才切过去。 */
async function newDefaultPreset() {
  openFormDialog({
    title: "新建默认预设",
    hint: "把内置的默认世界存成一份新预设；当前配置不动，去列表点「应用」才切过去。",
    fields: [
      {
        key: "id",
        label: "预设 id",
        value: `default_${Date.now().toString(36).slice(-4)}`,
        hint: "文件名，用英文/数字",
      },
      { key: "name", label: "名字", value: "默认世界", hint: "给自己看的名字" },
      { key: "note", label: "说明", value: "", hint: "可选" },
    ],
    confirmText: "新建",
    onSubmit: async (values) => {
      try {
        const result = await apiPost("presets/new-default", values);
        toast(`已新建预设 ${result.id}`);
        await loadPresets();
      } catch (error) {
        toast(error.message || "新建失败");
        return false;
      }
    },
  });
}

async function applyPreset(preset) {
  // 分块应用：想换哪一块就勾哪一块。会话白名单与会话组默认不勾——
  // 切预设本来是想换世界，把会话一起换掉等于顺手清空了她聊天的地方。
  openFormDialog({
    title: `应用预设「${preset.name || preset.id}」`,
    hint: "当前配置会先自动备份一份。只替换勾选的部分，其余保持当前配置。",
    fields: [
      {
        key: "blocks",
        label: "要替换的部分",
        type: "checkboxes",
        value: ["map", "actions", "settings", "persona", "schedules"],
        options: [
          { value: "map", label: "地图（区域 / 地点 / 连线）" },
          { value: "actions", label: "动作（含动作设置与生图动作）" },
          { value: "settings", label: "世界设置（作息 / 情绪 / 画像 / 事件…）" },
          { value: "persona", label: `${pronoun()}（人设）` },
          { value: "schedules", label: "日程" },
          { value: "sessions", label: "会话白名单与会话组（会把现在的换掉）" },
        ],
        hint: "「会话」默认不勾：勾了会连着把会话组一起换掉。",
      },
      {
        key: "clear_state",
        label: "同时清空所有会话状态（位置 / 数值 / 计划 / 群聊留档）",
        type: "checkbox",
        value: false,
        hint: "不勾就是保留原会话的上下文，只按上面勾的部分替换配置。",
      },
    ],
    confirmText: "应用",
    onSubmit: async (values) => {
      const blocks = Array.isArray(values.blocks) ? values.blocks : [];
      if (!blocks.length) {
        toast("至少勾一块要替换的内容");
        return false;
      }
      try {
        const result = await apiPost("presets/apply", {
          id: preset.id,
          blocks,
          clear_state: Boolean(values.clear_state),
        });
        const bits = [`已应用 ${(result.blocks || []).length} 块`];
        if (result.cleared_sessions) bits.push(`清空状态 ${result.cleared_sessions} 个会话`);
        if ((result.repairs || []).length) bits.push(`修状态：${result.repairs.join("；")}`);
        if ((result.warnings || []).length) bits.push(`提醒：${result.warnings.join("；")}`);
        toast(bits.join("；"));
        await loadAll();
        await loadPresets();
      } catch (error) {
        toast(error.message || "应用失败");
        return false;
      }
    },
  });
}

function renamePreset(preset) {
  openFormDialog({
    title: "重命名预设",
    fields: [
      { key: "name", label: "名字", value: preset.name || preset.id },
      { key: "note", label: "说明", value: preset.note || "" },
    ],
    confirmText: "保存",
    onSubmit: async (values) => {
      try {
        await apiPost("presets/rename", { id: preset.id, ...values });
        await loadPresets();
      } catch (error) {
        toast(error.message || "重命名失败");
        return false;
      }
    },
  });
}

async function deletePreset(preset) {
  const ok = await confirmDialog({
    title: `删除预设「${preset.name || preset.id}」？`,
    message: "只删除这个预设文件，当前正在用的配置不动。",
    confirmText: "删除",
  });
  if (!ok) return;
  try {
    await apiPost("presets/delete", { id: preset.id });
    toast("已删除");
    await loadPresets();
  } catch (error) {
    toast(error.message || "删除失败");
  }
}

/** 预设 JSON 的分区：整段 / 只看地图 / 只看动作…… */
const PRESET_SECTIONS = [
  { value: "all", label: "整段（完整文件）" },
  { value: "map", label: "只看地图（区域 / 地点 / 连线）" },
  { value: "actions", label: "只看动作" },
  { value: "settings", label: "只看世界设置（作息 / 情绪 / 画像 / 事件…）" },
  { value: "schedules", label: "只看日程" },
  { value: "sessions", label: "只看会话白名单" },
];

const PRESET_MAP_KEYS = ["zones", "zone_edges", "nodes", "edges"];
const PRESET_WORLD_SKIP = [...PRESET_MAP_KEYS, "actions"];

/** 按分区切出要显示的那一段。 */
function presetSectionValue(full, section) {
  const world = (full && full.world) || {};
  switch (section) {
    case "map": {
      const out = {};
      PRESET_MAP_KEYS.forEach((key) => {
        out[key] = world[key] ?? [];
      });
      return out;
    }
    case "actions":
      return { actions: world.actions ?? [] };
    case "settings": {
      const out = {};
      Object.keys(world).forEach((key) => {
        if (!PRESET_WORLD_SKIP.includes(key)) out[key] = world[key];
      });
      return out;
    }
    case "schedules":
      return full.schedules ?? {};
    case "sessions":
      return full.sessions ?? {};
    default:
      return full;
  }
}

/** 把某一分区改过的内容并回整份预设。 */
function mergePresetSection(full, section, patch) {
  const merged = JSON.parse(JSON.stringify(full || {}));
  merged.world = merged.world || {};
  if (section === "map" || section === "actions" || section === "settings") {
    Object.keys(patch || {}).forEach((key) => {
      merged.world[key] = patch[key];
    });
    return merged;
  }
  if (section === "schedules") {
    merged.schedules = patch;
    return merged;
  }
  if (section === "sessions") {
    merged.sessions = patch;
    return merged;
  }
  return patch;
}

async function editPresetJson(preset) {
  let full = {};
  try {
    const data = await apiGet("presets/json", { id: preset.id });
    full = data.preset || {};
  } catch (error) {
    toast(error.message || "读取失败");
    return;
  }
  const jsonField = {
    key: "json",
    label: "预设（JSON）",
    type: "textarea",
    value: JSON.stringify(full, null, 2),
    rows: 22,
  };
  openFormDialog({
    title: `预设 JSON：${preset.name || preset.id}`,
    hint:
      "整段可改；也能只挑一个分区看/改，保存时只并回这一段。复制走就是导出，贴进来就是导入。",
    wide: true,
    fields: [
      {
        key: "section",
        label: "查看范围",
        type: "select",
        value: "all",
        options: PRESET_SECTIONS,
        hint: "换一个分区就只显示那一段，改它也只改这一段",
        onChange: ({ control, body }) => {
          const textarea = body.querySelector('[data-dialog-key="json"]');
          if (!textarea) return;
          // 先把当前编辑框里的改动收回 full，再切分区，免得切一下丢改动
          const previous = control.dataset.prevSection || "all";
          try {
            const edited = JSON.parse(textarea.value || "{}");
            full = mergePresetSection(full, previous, edited);
          } catch (error) {
            // 有语法错误就先不收回，切过去看别的，用户回来还能接着改
          }
          const section = control.value || "all";
          control.dataset.prevSection = section;
          textarea.value = JSON.stringify(presetSectionValue(full, section), null, 2);
        },
      },
      jsonField,
    ],
    confirmText: "保存",
    onSubmit: async (values) => {
      const section = String(values.section || "all");
      let patch;
      try {
        patch = JSON.parse(values.json || "{}");
      } catch (error) {
        toast(`JSON 格式不对：${error.message}`);
        return false;
      }
      const payload = section === "all" ? patch : mergePresetSection(full, section, patch);
      try {
        const result = await apiPost("presets/json", {
          id: preset.id,
          payload,
        });
        toast(
          (result.warnings || []).length
            ? `已保存，提醒：${result.warnings.join("；")}`
            : "已保存",
        );
        await loadPresets();
      } catch (error) {
        toast(error.message || "保存失败");
        return false;
      }
    },
  });
}

function importPreset() {
  openFormDialog({
    title: "粘贴 JSON 导入预设",
    hint: "把别人分享的预设 JSON 整段贴进来。id 重复时会被覆盖。",
    wide: true,
    fields: [
      { key: "id", label: "存成什么 id", value: `imported_${Date.now().toString(36).slice(-4)}` },
      { key: "json", label: "预设（JSON）", type: "textarea", value: "", rows: 18 },
    ],
    confirmText: "导入",
    onSubmit: async (values) => {
      try {
        const result = await apiPost("presets/json", {
          id: values.id,
          json: values.json,
        });
        toast(
          (result.warnings || []).length
            ? `已导入，提醒：${result.warnings.join("；")}`
            : "已导入",
        );
        await loadPresets();
      } catch (error) {
        toast(error.message || "导入失败");
        return false;
      }
    },
  });
}

async function loadTools() {
  const list = $("tools-list");
  list.innerHTML = "";
  try {
    const data = await apiGet("tools");
    (data.tools || []).forEach((tool) => {
      const item = el("div", "list-item");
      const info = el("div");
      info.appendChild(el("div", "", tool.name));
      info.appendChild(el("div", "meta", tool.description || ""));
      info.appendChild(el("div", "meta", `参数：${tool.param_text || "（无）"}`));
      // 工具声明的必填和实现要的经常不一致（schema 写可选、代码里必填），
      // 这里把原始 required 摆出来，排查「为什么没传参数」时一眼能看到
      info.appendChild(el("div", "meta", toolRequiredSummary(tool)));
      item.appendChild(info);
      list.appendChild(item);
    });
    if (!(data.tools || []).length) {
      list.appendChild(el("p", "muted", "AstrBot 还没有注册任何工具。"));
    }
  } catch (error) {
    list.appendChild(el("p", "muted", error.message || "加载失败"));
  }
}

/* ==================== 设置向导 ====================
 *
 * 第一次打开编辑器时自动走一遍：把"必须先配的"和"最影响手感的一批"集中问一遍，
 * 省得新用户面对 20 多个小节不知道从哪下手。之后可以在全局设置里随时重开。
 */

/**
 * 第一步：她在哪儿生活。
 *
 * **不要**在这里回调 ``render`` 去重画整步：render 会再调一次这个 build，
 * 两边互相递归，直接把弹窗顶成空白（栈溢出）。
 * 「重新获取」之后只要重画列表本身（下面的 ``draw``），这一步就够了。
 */
function wizardSessionStep(body) {
  const listBox = el("div", "wizard-list");
  const draw = () => {
    listBox.innerHTML = "";
    if (!ui.sessions.length) {
      listBox.appendChild(
        el(
          "p",
          "muted",
          "还没有会话。先在群里或私聊里对她说「/vw session add」，再点「重新获取」。",
        ),
      );
    }
    ui.sessions.forEach((item) => {
      const row = el("label", "picker-row");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = item.enabled !== false;
      box.addEventListener("change", () => {
        item.enabled = box.checked;
        markDirty();
      });
      row.appendChild(box);
      const name = item.note || (item.type === "private" ? "私聊" : "群聊");
      const main = el("span", "wizard-session-main", name);
      main.title = item.session_id;
      row.appendChild(main);
      row.appendChild(el("span", "muted wizard-session-id", sessionShortName(item.session_id)));
      listBox.appendChild(row);
    });
  };
  draw();
  body.appendChild(listBox);

  const groups = (ui.groups || [])
    .map((group) => {
      const members = (group.sessions || []).length;
      return members ? `${group.name || group.id}（${members} 个会话）` : "";
    })
    .filter(Boolean);
  body.appendChild(
    el(
      "p",
      "muted",
      groups.length
        ? `勾上的才算「她在这儿生活」。会话组（${groups.join("、")}）在「会话」页管理，组内共享状态和记忆。`
        : "勾上的才算「她在这儿生活」；想让几个会话共享状态和记忆，去「会话」页建一个会话组。",
    ),
  );

  const row = el("div", "row");
  const again = el("button", "small ghost", "重新获取");
  again.type = "button";
  again.title = "加完会话后点这里，把最新的白名单拉回来";
  again.addEventListener("click", async () => {
    const restore = busyButton(again, "获取中…");
    try {
      const data = await apiGet("config");
      const sessions = (data.sessions && data.sessions.sessions) || [];
      const groups = (data.sessions && data.sessions.groups) || [];
      ui.sessions = sessions;
      ui.groups = groups;
      // **整份替换**：只更新 ui.sessions 的话，之后点「保存」会把旧白名单写回去，
      // 刚加的那个会话又没了
      ui.config.sessions = data.sessions || { sessions, groups };
      if (typeof renderSessionSelects === "function") renderSessionSelects();
      draw();
      toast(sessions.length ? `拿到 ${sessions.length} 个会话` : "还是空的");
    } catch (error) {
      toast(error.message || "获取失败");
    } finally {
      restore();
    }
  });
  row.appendChild(again);
  body.appendChild(row);
}

function wizardPersonaStep(body) {
  const world = ui.config.world;
  world.persona = world.persona || { mode: "astrbot", text: "" };
  body.appendChild(
    inputField("她的名字", world.bot_name || "", (value) => {
      world.bot_name = value;
      markDirty();
    }, { hint: "互动里的「{bot}」会换成它；留空就用群名片原名。", placeholder: "例如：小鲸鱼" }),
  );
  body.appendChild(
    pillsField("性别", world.gender || "female", GENDERS, (value) => {
      world.gender = value;
      markDirty();
      applyPronoun($("app"));
    }, { hint: "只影响文案里的「她 / 他 / ta」。" }),
  );
  body.appendChild(
    selectField("人设来源", world.persona.mode || "astrbot", [
      { key: "astrbot", label: "跟随 AstrBot（每个会话各自的人格）" },
      { key: "plugin", label: "用下面这一份（所有会话共用）" },
      { key: "append", label: "AstrBot + 下面这一份" },
    ], (value) => {
      world.persona.mode = value;
      markDirty();
    }, { hint: "想让她不随会话漂移，就选「用下面这一份」。" }),
  );
  const area = document.createElement("textarea");
  area.rows = 8;
  area.value = world.persona.text || "";
  area.placeholder = "她是谁、怎么说话、在意什么。留空会沿用 AstrBot 的人格。";
  area.addEventListener("change", () => {
    world.persona.text = area.value;
    markDirty();
  });
  const wrap = el("div", "field");
  wrap.appendChild(fieldHead("角色卡（她是谁）"));
  wrap.appendChild(area);
  const importRow = el("div", "row");
  const importButton = el("button", "small ghost", "从 AstrBot 导入当前人设");
  importButton.type = "button";
  importButton.addEventListener("click", async () => {
    const session = String((ui.sessions[0] || {}).session_id || "");
    const restore = busyButton(importButton, "读取中…");
    try {
      const data = await apiGet("persona-source", { session });
      const text = String(data.text || "");
      if (!text.trim()) {
        toast("AstrBot 这个会话没读到人格，先去 AstrBot 里给它选一份");
        return;
      }
      world.persona.text = text;
      world.persona.mode = "plugin";
      area.value = text;
      markDirty();
      toast(`导入了 ${data.chars || text.length} 字`);
    } catch (error) {
      toast(error.message || "读取失败");
    } finally {
      restore();
    }
  });
  importRow.appendChild(importButton);
  wrap.appendChild(importRow);
  body.appendChild(wrap);
}

/**
 * 一排三档按钮：点一下立刻写入并重绘选中态。
 * （`pillsField` 点了不重绘自己，向导里要看得见选了什么。）
 */
function wizardChoicesField(label, value, choices, onChange, opts = {}) {
  const wrap = el("div", "field");
  const row = el("div", "wizard-choice-row");
  const paint = () => {
    row.innerHTML = "";
    choices.forEach((choice) => {
      const button = el(
        "button",
        `wizard-choice${choice.key === value ? " on" : ""}`,
        choice.label,
      );
      button.type = "button";
      if (choice.hint) button.title = choice.hint;
      button.addEventListener("click", () => {
        if (choice.key === value) return;
        value = choice.key;
        onChange(choice.key);
        paint();
      });
      row.appendChild(button);
    });
  };
  paint();
  wrap.appendChild(fieldHead(label, opts.hint));
  wrap.appendChild(row);
  return wrap;
}

/**
 * 清单里的一行：条目名 + 状态（已配置 / 未配置）+ 一句说明。
 * 未配置时红字，能跳过去的给一个「去配」按钮。
 */
function wizardCheckRow(item) {
  const row = el("div", "wizard-check-row");
  const main = el("div", "wizard-check-main");
  main.appendChild(el("div", "wizard-check-title", item.title));
  main.appendChild(
    el(
      "div",
      item.ok ? "wizard-ok" : "wizard-missing",
      `${item.ok ? "已配置" : "未配置"}：${item.detail}`,
    ),
  );
  if (!item.ok && item.where) main.appendChild(el("div", "hint", item.where));
  row.appendChild(main);
  if (!item.ok && item.jump) {
    const button = el("button", "small ghost", "去配");
    button.type = "button";
    button.addEventListener("click", item.jump);
    row.appendChild(button);
  }
  return row;
}

function wizardTemperamentStep(body) {
  const world = ui.config.world;
  world.ui_knobs = world.ui_knobs || {};
  const presets = KNOB_PRESETS.filter((preset) => preset.id !== "clingy");
  const grid = el("div", "wizard-preset-grid");
  const sliderBox = el("div", "field");
  sliderBox.appendChild(
    fieldHead("细调（可选）", "上面选整体感觉，下面拖具体滑块；两边是同一套字段。"),
  );

  let editor = null;
  // 滑块与预设读写的是同一套字段（`world.ui_knobs` + `applyKnobLevel`）：
  // 拖了滑块，上面的预设高亮跟着变；点了预设，下面的滑块立刻换到那一档。
  const drawSliders = () => {
    if (editor) editor.remove();
    editor = knobEditor(world, drawPresets, { compact: true, showDetails: false });
    sliderBox.appendChild(editor);
  };
  const drawPresets = () => {
    grid.innerHTML = "";
    presets.forEach((preset) => {
      const button = el(
        "button",
        `wizard-preset${knobPresetMatches(world, preset) ? " on" : ""}`,
      );
      button.type = "button";
      button.appendChild(el("span", "wizard-preset-title", preset.label));
      button.appendChild(el("span", "wizard-preset-desc", preset.desc));
      button.addEventListener("click", () => {
        applyKnobPreset(world, preset);
        drawPresets();
        drawSliders();
      });
      grid.appendChild(button);
    });
  };
  drawPresets();
  drawSliders();
  body.appendChild(grid);
  body.appendChild(
    el("p", "muted", "先选整体感觉；想细调再拖下面的滑块。只改主动程度、群聊活跃度、黏人和一次说几句。"),
  );
  body.appendChild(sliderBox);
}

/** 模型槽位：指向哪个 Provider、配没配。编辑器改不了（在插件配置里），只做核对。 */
const WIZARD_MODEL_SLOTS = [
  {
    key: "llm",
    field: "llm_provider_id",
    label: "主模型",
    missing: "她不会说话",
  },
  {
    key: "helper",
    field: "helper_provider_id",
    label: "打杂模型",
    missing: "定时整理、事件生成会退化",
  },
  {
    key: "vision",
    field: "vision_provider_id",
    label: "看图模型",
    missing: "群里的图只会显示成 [图片]",
  },
];

/** 槽位现在指向的 Provider 名字；没配时后端给的是「（…）」占位。 */
function wizardSlotValue(key) {
  return String((ui.config.providers || {})[key] || "").trim();
}

function wizardSlotConfigured(key) {
  const value = wizardSlotValue(key);
  return Boolean(value) && !value.startsWith("（") && !value.startsWith("(");
}

/** 三行状态灯：主模型 / 打杂 / 看图。未配的红字写清少了什么能力。 */
function wizardRunCheck(body) {
  const box = el("div", "wizard-runcheck");
  WIZARD_MODEL_SLOTS.forEach((slot) => {
    const ok = wizardSlotConfigured(slot.key);
    box.appendChild(
      wizardCheckRow({
        title: `${slot.label} · ${slot.field}`,
        ok,
        detail: ok ? wizardSlotValue(slot.key) : slot.missing,
      }),
    );
  });
  body.appendChild(box);
  body.appendChild(
    el(
      "p",
      "muted",
      "模型在 AstrBot 面板 → 插件 → virtual_world（虚拟世界VW）里选；这里只做核对。",
    ),
  );
}

function wizardMemoryStep(body) {
  const world = ui.config.world;
  world.profile = world.profile || {};
  world.context = world.context || {};
  body.appendChild(
    checkboxField("记住每个人（通讯录）", world.profile.enabled !== false, (value) => {
      world.profile.enabled = value;
      markDirty();
    }, { hint: "关掉就不记画像，也不往提示词里带。" }),
  );
  body.appendChild(
    checkboxField("睡觉时整理记忆与画像", world.profile.consolidate_enabled !== false, (value) => {
      world.profile.consolidate_enabled = value;
      markDirty();
    }, { hint: "睡下 20 分钟后整理一次；关掉就一直攒着。" }),
  );
  body.appendChild(
    selectField("上下文超限处理", world.context.chat_overflow || "compress", [
      { key: "compress", label: "压成摘要（推荐）" },
      { key: "discard", label: "丢掉最早的" },
    ], (value) => {
      world.context.chat_overflow = value;
      markDirty();
    }, { hint: "压成摘要会保留「更早聊过什么」；丢掉就是真的没了。" }),
  );
  // 记多少：三档够用了，别让新用户对着一个 1~10000 的输入框发呆
  world.memory = world.memory || {};
  const limitValue = String(num(world.memory.memory_limit, 1000));
  body.appendChild(
    wizardChoicesField(
      "每个会话最多记几条",
      limitValue === "500" || limitValue === "3000" ? limitValue : "1000",
      [
        { key: "500", label: "500 条" },
        { key: "1000", label: "1000 条（默认）" },
        { key: "3000", label: "3000 条" },
      ],
      (value) => {
        world.memory.memory_limit = num(value, 1000);
        markDirty();
      },
      { hint: "满了会按衰减和重复度淘汰；先记 1000 条一般够用。" },
    ),
  );

  // 用户 m11008：新认识的人先算什么关系（默认「群友」；这是她对人开口的第一态度）
  const bonds = Array.isArray(world.profile.bonds) ? world.profile.bonds : [];
  const bondNames = bonds.map((item) => String((item && item.name) || "")).filter(Boolean);
  const bondSelect = document.createElement("select");
  bondNames.forEach((name) => bondSelect.appendChild(option(name, name)));
  bondSelect.value = String(world.profile.default_bond || bondNames[0] || "");
  const bondField = el("label");
  bondField.appendChild(
    fieldHead(
      "新认识的人先算什么关系",
      "第一次见到一个人时先挂这条；聊久了会自己往上走。默认「群友」，不是「陌生人」。",
    ),
  );
  bondField.appendChild(bondSelect);
  body.appendChild(bondField);
  bondSelect.addEventListener("change", () => {
    world.profile.default_bond = bondSelect.value;
    world.profile.bond_chosen = true;
    markDirty();
  });

  // 用户 m10905：向量模型的选择也放进向导（以前只在「记忆与上下文」里）
  const embedField = el("label");
  embedField.appendChild(
    fieldHead(
      "向量模型（可选）",
      "翻旧事时按语义挑，比只认关键词多一层。宿主里还没建 embedding 供应商就留空。",
    ),
  );
  const embedSelect = document.createElement("select");
  embedField.appendChild(embedSelect);
  body.appendChild(embedField);
  async function loadEmbeddingChoices() {
    try {
      const data = await apiGet("embedding/providers");
      const choices = Array.isArray(data.providers) ? data.providers : [];
      embedSelect.replaceChildren(option("", "不用向量（只按关键词翻）"));
      choices.forEach((item) => {
        const id = String(item.id || "");
        const label = String(item.label || "");
        embedSelect.appendChild(option(id, label && label !== id ? `${label}（${id}）` : id));
      });
      embedSelect.value = String(data.current || "");
      embedSelect.disabled = false;
    } catch (error) {
      embedSelect.replaceChildren(option("", error.message || "读不到供应商列表"));
      embedSelect.disabled = true;
    }
  }
  embedSelect.addEventListener("change", async () => {
    try {
      const result = await apiPost("embedding/provider", { provider_id: embedSelect.value });
      toast(result && result.reason ? result.reason : "选好了，下一轮就按新的向量空间走");
    } catch (error) {
      toast(error.message || "选向量模型失败");
    }
    await loadEmbeddingChoices();
  });
  loadEmbeddingChoices();
}

/**
 * 事件热闹程度：三档，只写三档频率。
 * 不碰 `events.min_gap_minutes`——那是两次事件之间的最小区间（节流），不是热闹度。
 */
const WIZARD_EVENTS_PACE = [
  { key: "quiet", label: "安静", micro: 0.6, small: 0.2, big: 0.015 },
  { key: "normal", label: "正常", micro: 1, small: 0.3, big: 0.028 },
  { key: "lively", label: "热闹", micro: 2, small: 0.6, big: 0.04 },
];

/** 现在最接近哪一档（后端允许任意小数，所以按最接近的一档高亮）。 */
function eventsPaceOf(world) {
  const events = world.events || {};
  let best = "normal";
  let bestGap = Infinity;
  WIZARD_EVENTS_PACE.forEach((pace) => {
    const gap =
      Math.abs(num(events.micro_per_hour, 1) - pace.micro) +
      Math.abs(num(events.small_per_hour, 0.3) - pace.small) * 2 +
      Math.abs(num(events.big_per_hour, 0.028) - pace.big) * 5;
    if (gap < bestGap) {
      bestGap = gap;
      best = pace.key;
    }
  });
  return best;
}

function applyEventsPace(world, key) {
  const pace = WIZARD_EVENTS_PACE.find((item) => item.key === key);
  if (!pace) return;
  world.events = world.events || {};
  world.events.micro_per_hour = pace.micro;
  world.events.small_per_hour = pace.small;
  world.events.big_per_hour = pace.big;
  markDirty();
}

/** 这台电脑能用多少：一次把清理阈值、占用上限和自动清理写全。 */
const WIZARD_STORAGE_SPACE = [
  { key: "lean", label: "节省", min_free: 800, max_data: 200 },
  { key: "normal", label: "标准", min_free: 500, max_data: 300 },
  { key: "loose", label: "宽松", min_free: 200, max_data: 1000 },
];

function storageSpaceOf(world) {
  const storage = world.storage || {};
  const minFree = num(storage.min_free_mb, 500);
  const maxData = num(storage.max_data_mb, 300);
  let best = "normal";
  let bestGap = Infinity;
  WIZARD_STORAGE_SPACE.forEach((space) => {
    const gap =
      Math.abs(minFree - space.min_free) + Math.abs(maxData - space.max_data) / 4;
    if (gap < bestGap) {
      bestGap = gap;
      best = space.key;
    }
  });
  return best;
}

function applyStorageSpace(world, key) {
  const space = WIZARD_STORAGE_SPACE.find((item) => item.key === key);
  if (!space) return;
  world.storage = world.storage || {};
  world.storage.min_free_mb = space.min_free;
  world.storage.max_data_mb = space.max_data;
  world.storage.auto_clean_snapshots = true;
  markDirty();
}

/**
 * 「带总开关的功能，要不要开」这一步（用户 m20：带总开关的都进向导）。
 *
 * 只放**总开关**：清单与 `core/defaults.py` 里所有带 `enabled` 的世界配置段一一对应
 * （`WIZARD_FEATURE_SWITCHES` 是唯一来源，测试会核对它没漏也没多）。每项只有一句人话
 * 说明，子开关留在「全局设置」里——向导不是配置面板，新用户在这里只该回答"要不要"。
 *
 * 「记住每个人」「睡觉时整理」两个总开关属于记忆那一档，已经在上一步（记忆与画像）
 * 问过了，这里不再重复问一遍。
 */
const WIZARD_FEATURE_SWITCHES = [
  ["topics", "谈资", "把经历攒成可讲的素材，聊天时顺口提起。管理在「素材 → 谈资」。"],
  ["meme", "表情包", "收下群里的表情，之后自己也能发。管理在「素材 → 表情包」。"],
  ["meme_collect", "攒图评审收藏", "平时看到的图先攒着，隔一阵拼成九宫格让她自己挑；关掉就完全不再收图（聊天里点名收也不收）。"],
  ["agent", "自主能力（Agent）", "允许她动手做事：查记录、写文件、发文件、跑代码。默认关。"],
  ["variation", "表达变奏", "关系近、气氛好时，换个说法，不总是同一个腔调。"],
  ["topic_steer", "拐话头", "闲聊时她可能把话拐到自己正在做的事上，并且之后真的接着讲下去。"],
  ["action_selection", "动作选择", "先筛出此刻能做的，再按目的挑一件；关掉后退回固定规则。"],
  ["weather", "天气", "她知道外面天气，聊到出门、下雨时能对上。"],
  ["media", "图片与媒体", "收到的图留一份本机副本并转述；关掉就不缓存、不描述。"],
  ["nickname_sync", "名片同步", "把状态写进群名片，例如「小鲸鱼 | 睡觉中」。只改名片，不改通讯录。"],
  ["follow_up", "追问", "她问完没人接，隔 10 分钟再问一句（默认只追一次）。"],
  ["dream", "做梦", "睡着后可能做梦，醒来偶尔记得。"],
  ["boredom_online", "无聊时自己找点事做", "一个人闲着又觉得无聊，会自己上网找点事做（查东西、翻帖子）；具体做哪件由动作自己声明。"],
  ["search", "联网搜索", "她查东西用一份全局配置（工具、档位、要不要出处），细节在「高级功能 → 联网搜索」。"],
  ["knowledge", "知识卡", "查过的东西记成一张卡（原文摘要 + 出处），之后聊到能拿出来用；不额外调用模型，也不会自己编。"],
  ["events", "随机事件", "她一个人待着的时候会遇上点事（出门、被人拉住说话），频率在全局设置里调。"],
  ["storage", "存储维护", "世界文件太大时自动整理（真空回收、留一份快照），出问题会告诉你。"],
  ["temp_session", "临时会话接管", "她主动私聊出去的人，先由她接管一阵子，到期自动交回默认路由。"],
  ["meme_send_scale", "表情发送缩放", "发出去的表情长边缩到 320（动图保真）并转成 GIF，QQ 里尺寸更舒服。"],
];

/** 审批档位：新字段留空时按旧布尔开关折算（与引擎 ``owner_approval_mode()`` 同口径）。 */
function ownerApprovalModeOf(world) {
  const mode = String((world.agent && world.agent.owner_approval_mode) || "");
  if (mode === "off" || mode === "risky" || mode === "all") return mode;
  return (world.agent && world.agent.owner_approval_enabled !== false)
    ? "risky"
    : "off";
}

const OWNER_APPROVAL_MODES = [
  { key: "off", label: "不问，直接做" },
  { key: "risky", label: "只问危险的（装插件、改配置、删东西、跑代码）" },
  { key: "all", label: "每件都先问" },
];

/** Agent 的四个放权开关 + 只读详情（都得先打开上面的总开关才生效）。 */
const WIZARD_AGENT_SWITCHES = [
  ["history_lookup_enabled", "查聊天历史与经历", "翻群聊留档、经历账和记忆找线索。"],
  ["artifact_generation_enabled", "生成文件", "把结果写成表格或文档。"],
  ["file_send_enabled", "发送文件", "把生成好的文件发到聊天里。"],
  ["code_execution_enabled", "执行代码", "在这台机器上运行她写的代码。不是沙箱，有真实风险，默认关。"],
  ["inspect_capability_enabled", "查看动作说明", "只读查看动作能做什么、需要什么。"],
  ["ask_user_enabled", "缺信息时问一句", "拿不准就问她/他一句；这类追问不占说话的额度。"],
  ["plugin_forge_risk_accepted", "确认插件工坊的风险", "打开后她会生成代码、写进插件目录；这是高敏感开关，确认再开。"],
  ["plugin_forge_enabled", "允许她写插件", "她能自己写一份插件草稿；装不装由你点。"],
  ["plugin_forge_auto_install", "允许她自己装", "装上会真的跑起来；要先打开上面那条，默认关。"],
];

function wizardFeatureStep(body) {
  const world = ui.config.world;
  const coreKeys = new Set(["topics", "meme", "agent"]);
  const more = document.createElement("details");
  more.className = "wizard-more";
  const moreSummary = document.createElement("summary");
  const moreLabel = el("span", "", "更多功能");
  moreSummary.appendChild(moreLabel);
  more.appendChild(moreSummary);
  const moreBody = el("div", "wizard-more-body");

  WIZARD_FEATURE_SWITCHES.forEach(([key, label, hint]) => {
    world[key] = world[key] || {};
    // 默认值取自世界默认（`agent` 默认关、其余默认开）：向导里没写过就按默认显示
    const fallback = key === "agent" ? false : true;
    const target = coreKeys.has(key) ? body : moreBody;
    let reveal = null; // 只有打开这个总开关才展开的细分项
    const field = checkboxField(
      label,
      world[key].enabled === undefined ? fallback : world[key].enabled === true,
      (value) => {
        world[key].enabled = value;
        markDirty();
        if (key === "agent") drawAgentSub();
        if (reveal) reveal.hidden = value !== true;
      },
      { hint },
    );
    target.appendChild(field);

    if (key === "events") {
      // 事件频率原来只在全局设置里；新用户开着「随机事件」却不知道她多久遇上一件事
      reveal = el("div", "wizard-sub");
      reveal.appendChild(
        wizardChoicesField(
          "事件热闹程度",
          eventsPaceOf(world),
          WIZARD_EVENTS_PACE,
          (value) => applyEventsPace(world, value),
          { hint: "她自己待着时多久遇上一件事；默认「正常」。" },
        ),
      );
      reveal.hidden = world.events.enabled === false;
      target.appendChild(reveal);
      return;
    }

    if (key === "meme") {
      // 收图不在向导里问：它只有「收图与评审」一条路（攒池 -> 评审 -> 才收藏）。
      return;
    }

    if (key === "media") {
      // 用户口径（m01035 D6）：看图有趣就说话不依赖意图路由，所以这里单独问一句
      target.appendChild(
        checkboxField(
          "看到有意思的图会自己接一句",
          world.media.reaction_enabled !== false,
          (value) => {
            world.media.reaction_enabled = value === true;
            markDirty();
          },
          { hint: "群里有人发图、没点她名，她自己看一眼；觉得有意思就接一句。间隔在全局设置里调。" },
        ),
      );
      return;
    }

    if (key !== "agent") return;
    // 自主能力下面几个**具体能力**也在这里问（用户口径：带开关的都进向导）。
    // 只在总开关打开时才展开，避免新用户先看到 5 个没意义的子开关。
    const sub = el("div", "wizard-sub");
    sub.appendChild(
      el("p", "muted", "这几件事可以分别放权；都要先打开上面的「自主能力」。"),
    );
    // 会动这台机器文件的几项：开启前要过一次确认弹窗（用户 2026-10-19），
    // 声明文案与「全局设置 → Agent」共用同一份。
    const wizardPowerKeys = new Set([
      "artifact_generation_enabled",
      "code_execution_enabled",
      "plugin_forge_enabled",
      "plugin_forge_auto_install",
    ]);
    WIZARD_AGENT_SWITCHES.forEach(([name, subLabel, subHint]) => {
      sub.appendChild(
        checkboxField(subLabel, world.agent[name] === true, (value) => {
          world.agent[name] = value === true;
          markDirty();
        }, {
          hint: subHint,
          confirm: wizardPowerKeys.has(name) ? AGENT_FILE_POWER_WARNING : "",
        }),
      );
    });
    sub.appendChild(el("p", "hint", `⚠️ ${AGENT_FILE_POWER_WARNING}`));
    // 审批档位（用户 m01035 决定 D4）：三档，默认「只问危险的」
    sub.appendChild(
      selectField(
        "动系统的事先问主人",
        ownerApprovalModeOf(world),
        OWNER_APPROVAL_MODES,
        (value) => {
          world.agent.owner_approval_mode = value;
          world.agent.owner_approval_enabled = value !== "off";
          markDirty();
        },
        { hint: "不是主人提的麻烦事先私聊问一句；主人自己提的不拦。" },
      ),
    );
    body.appendChild(sub);
    sub.hidden = world.agent.enabled !== true;

    function drawAgentSub() {
      sub.hidden = world.agent.enabled !== true;
    }
  });

  if (moreBody.children.length) {
    moreLabel.textContent = `更多功能（${moreBody.children.length} 项）`;
    more.appendChild(moreBody);
    body.appendChild(more);
  }
  // 电脑空间：别让新用户自己猜 MB，三档一次写完
  body.appendChild(
    wizardChoicesField(
      "这台电脑能给她用多少",
      storageSpaceOf(world),
      WIZARD_STORAGE_SPACE,
      (value) => applyStorageSpace(world, value),
      { hint: "磁盘剩多少开始清理、世界文件最多占多少；默认「正常」。" },
    ),
  );
  body.appendChild(
    el(
      "p",
      "muted",
      "这些随时能在「全局设置」里改；现在选好、点「完成」后统一保存。",
    ),
  );
}

/**
 * 「谁说了算」：主人（单值）与管理员（数组）。
 *
 * 单独一步的原因：这两个 id 决定审批问谁、告警发给谁，留空就等于没人管；
 * 混在「功能开关」十几项里，新用户会直接跳过。
 */
function wizardOwnerStep(body) {
  const world = ui.config.world;
  world.agent = world.agent || {};
  body.appendChild(
    el(
      "p",
      "muted",
      "这两项只决定「审批和告警发给谁」；都可以留空，但留空就没人收。",
    ),
  );
  body.appendChild(
    inputField(
      "主人是谁（账号 id）",
      String((world.agent && world.agent.owner_user_id) || ""),
      (value) => {
        world.agent.owner_user_id = String(value || "").trim();
        markDirty();
      },
      { hint: "要问谁、谁说了算。留空＝视为没有主人（所有需要主人权限的功能关闭）。" },
    ),
  );
  body.appendChild(el("p", "hint", "主人留空时，审批与告警不会发给任何人。"));
  world.admin_ids = Array.isArray(world.admin_ids) ? world.admin_ids : [];
  body.appendChild(
    textareaField(
      "管理员（逗号或换行分隔）",
      world.admin_ids.join("\n"),
      (value) => {
        // 逗号、换行、顿号都当分隔符：用户不会只按一种写
        world.admin_ids = String(value || "")
          .split(/[\s,，、;；]+/)
          .map((item) => item.trim())
          .filter(Boolean);
        markDirty();
      },
      { rows: 3, hint: "这些账号能用管理指令；AstrBot 管理员始终可用。" },
    ),
  );
}

/**
 * 收尾清单：不写"完成"，逐条说还差什么、点哪儿去补。
 *
 * 只认已经落盘的配置（`ui.config`）和手上真有的数据（会话列表、模型槽位），
 * 不做"应该配过了"的猜测——猜错比不提醒更糟。
 */
function wizardChecklistStep(body, close) {
  const world = ui.config.world || {};
  const go = (fn) => () => {
    close();
    fn();
  };
  const owner = String((world.agent && world.agent.owner_user_id) || "").trim();
  const sessions = (Array.isArray(ui.sessions) ? ui.sessions : []).filter(
    (item) => item && item.enabled !== false,
  );
  const media = world.media || {};
  const storage = world.storage || {};
  const visionOk = wizardSlotConfigured("vision");
  const mediaOk = media.enabled !== false && visionOk;
  const storageOk =
    num(storage.min_free_mb, 0) > 0 && num(storage.max_data_mb, 0) > 0;
  const items = [
    {
      title: "她要在哪些会话里生活",
      ok: sessions.length > 0,
      detail: sessions.length
        ? `${sessions.length} 个会话`
        : "一个都没选，她不会在任何群或私聊里说话",
      jump: go(() => showTab("sessions")),
    },
    {
      title: "谁说了算（主人）",
      ok: Boolean(owner),
      detail: owner || "没有主人，审批与告警不会发给任何人",
      jump: go(() => {
        showTab("settings");
        openSettingsTab("advanced");
      }),
    },
    {
      title: "图片转述",
      ok: mediaOk,
      detail: mediaOk
        ? "开着，群里的图会转述成文字"
        : media.enabled === false
          ? "「图片与媒体」关着，群里的图她看不到"
          : "看图模型没配，群里的图只会显示成 [图片]",
      where: mediaOk
        ? ""
        : "开关在「设置 → 记忆与上下文 → 图片转述」；模型在 AstrBot 面板 → 插件 → virtual_world 里选",
      jump:
        media.enabled === false
          ? go(() => {
              showTab("settings");
              openSettingsTab("mind");
            })
          : null,
    },
    {
      title: "存储阈值",
      ok: storageOk,
      detail: storageOk
        ? `剩 ${num(storage.min_free_mb, 500)} MB 就清理，最多占 ${num(storage.max_data_mb, 300)} MB`
        : "阈值没填，满了不会自动清理",
      jump: go(() => {
        showTab("settings");
        openSettingsTab("advanced");
      }),
    },
  ];
  const missing =
    items.filter((item) => !item.ok).length +
    WIZARD_MODEL_SLOTS.filter((slot) => !wizardSlotConfigured(slot.key)).length;
  body.appendChild(
    el(
      "p",
      missing ? "wizard-missing" : "wizard-ok",
      missing
        ? `还差 ${missing} 项才能痛快跑起来；点「去配」直接跳过去。`
        : "都配好了，现在就能跑。",
    ),
  );
  body.appendChild(el("div", "wizard-check-head", "模型槽位"));
  wizardRunCheck(body);
  body.appendChild(el("div", "wizard-check-head", "其他"));
  const list = el("div", "wizard-check-list");
  items.forEach((item) => list.appendChild(wizardCheckRow(item)));
  body.appendChild(list);
  body.appendChild(
    el(
      "p",
      "muted",
      "点「完成」保存这一遍改动；没配的部分之后随时能在「设置」里补。",
    ),
  );
}

function wizardGenerateStep(body) {
  const world = ui.config.world;
  body.appendChild(
    el(
      "p",
      "muted",
      "可选：生成一段给辅助模型用的人设摘要，再攒几句口吻样例。跳过也能正常用。",
    ),
  );
  // ① 简易人设：给辅助模型用的摘要，事件、整理都靠它
  const briefArea = document.createElement("textarea");
  briefArea.rows = 4;
  briefArea.placeholder = "还没有生成，点下面的按钮从主人设生成一段";
  briefArea.value =
    ($("persona-brief-text") && $("persona-brief-text").value) || "";
  const briefWrap = el("div", "field");
  briefWrap.appendChild(fieldHead("简易人设（给辅助模型）"));
  briefWrap.appendChild(briefArea);
  const briefRow = el("div", "row");
  const briefButton = el("button", "small primary", "从主人设生成");
  briefButton.type = "button";
  briefButton.addEventListener("click", async () => {
    const session = String((ui.sessions[0] || {}).session_id || "");
    if (!session) {
      toast("先去第一步加一个会话，她才知道用哪份人设");
      return;
    }
    const restore = busyButton(briefButton, "生成中…");
    try {
      // 带上编辑器里这份角色卡（可能是刚在向导上一步写的、还没保存）：
      // 不带的话后端读的是配置里那份旧的，生成的摘要跟眼前的人设对不上
      const data = await apiPost("persona-brief", {
        session,
        generate: true,
        persona: (world.persona && world.persona.text) || "",
      });
      const state = (data && data.persona_brief) || {};
      briefArea.value = state.brief || "";
      const mirror = $("persona-brief-text");
      if (mirror) mirror.value = briefArea.value;
      toast("生成好了（保存到这个人格）");
    } catch (error) {
      toast(`生成失败：${error.message || error}`);
    } finally {
      restore();
    }
  });
  const briefSave = el("button", "small ghost", "保存到这个人格");
  briefSave.type = "button";
  briefSave.title = "按人格缓存：换人格之后要重新生成一次";
  briefSave.addEventListener("click", async () => {
    const session = String((ui.sessions[0] || {}).session_id || "");
    if (!session) return;
    try {
      await apiPost("persona-brief", { session, brief: briefArea.value });
      const mirror = $("persona-brief-text");
      if (mirror) mirror.value = briefArea.value;
      toast("已保存");
    } catch (error) {
      toast(error.message || "保存失败");
    }
  });
  briefRow.appendChild(briefButton);
  briefRow.appendChild(briefSave);
  briefWrap.appendChild(briefRow);
  body.appendChild(briefWrap);

  // ② 声音样例：让她学着"自己说过的话"说话
  const sampleWrap = el("div", "field");
  sampleWrap.appendChild(fieldHead("口吻样例"));
  sampleWrap.appendChild(
    el(
      "p",
      "muted",
      "按场景生成几句，先放进候选池；挑中的才会写进提示词。",
    ),
  );
  const sampleRow = el("div", "row");
  const sampleButton = el("button", "small primary", "生成候选…");
  sampleButton.type = "button";
  sampleButton.addEventListener("click", () => {
    if (typeof ui.generateVoiceSamples === "function") ui.generateVoiceSamples();
  });
  sampleRow.appendChild(sampleButton);
  sampleWrap.appendChild(sampleRow);
  body.appendChild(sampleWrap);
}

function openWizard() {
  const steps = [
    {
      title: "接入会话",
      hint: "选择她要在哪些群或私聊里生活；没选的会话不会生效。",
      build: (body) => wizardSessionStep(body),
    },
    {
      title: "她是谁",
      hint: "名字、性别和角色卡；留空会沿用 AstrBot 的人格。",
      optional: true,
      build: (body) => wizardPersonaStep(body),
    },
    {
      title: "她的性格",
      hint: "先选整体感觉；不对再拖下面的滑块细调。",
      build: (body) => wizardTemperamentStep(body),
    },
    {
      title: "记忆",
      hint: "默认记住每个人，并在她睡下后整理；可以关掉。",
      build: (body) => wizardMemoryStep(body),
    },
    {
      title: "谁说了算",
      hint: "主人收审批与告警，管理员能用管理指令；都可以留空。",
      build: (body) => wizardOwnerStep(body),
    },
    {
      title: "功能开关",
      hint: "只决定开不开；具体细节都在「全局设置」里。",
      build: (body) => wizardFeatureStep(body),
    },
    {
      title: "口吻样例（可选）",
      hint: "生成几句她说话的样子；跳过也能正常用。",
      optional: true,
      build: (body) => wizardGenerateStep(body),
    },
    {
      title: "还差这几步才能跑起来",
      hint: "逐条核对；没配的点「去配」直接跳过去。",
      build: (body, render, close) => wizardChecklistStep(body, close),
    },
  ];
  let index = 0;

  const backdrop = el("div", "wizard-backdrop");
  const card = el("div", "wizard-card");
  const head = el("div", "wizard-head");
  const title = el("div", "wizard-title", "");
  const stepNote = el("div", "muted wizard-step", "");
  head.appendChild(title);
  head.appendChild(stepNote);
  card.appendChild(head);
  const hintBox = el("p", "muted wizard-hint", "");
  card.appendChild(hintBox);
  const progress = el("div", "wizard-progress");
  const progressFill = el("i");
  progress.appendChild(progressFill);
  card.appendChild(progress);
  const body = el("div", "wizard-body");
  card.appendChild(body);
  const foot = el("div", "wizard-foot");
  const back = el("button", "small ghost", "上一步");
  back.type = "button";
  const skip = el("button", "small ghost", "跳过此步");
  skip.type = "button";
  const next = el("button", "small primary", "下一步");
  next.type = "button";
  const done = el("button", "small ghost", "稍后设置");
  done.type = "button";
  foot.appendChild(done);
  foot.appendChild(skip);
  foot.appendChild(back);
  foot.appendChild(next);
  card.appendChild(foot);
  card.appendChild(
    el(
      "p",
      "muted wizard-foot-note",
      "以后可在「设置 → 新手设置」里重新打开向导。",
    ),
  );
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);

  function close() {
    backdrop.remove();
  }

  function render() {
    const step = steps[index];
    title.textContent = step.title;
    stepNote.textContent = `${index + 1} / ${steps.length}`;
    hintBox.textContent = step.hint || "";
    progressFill.style.width = `${((index + 1) / steps.length) * 100}%`;
    body.innerHTML = "";
    step.build(body, render, close);
    back.disabled = index === 0;
    skip.style.display = step.optional && index < steps.length - 1 ? "" : "none";
    next.textContent = index === steps.length - 1 ? "完成" : "下一步";
  }

  async function finish() {
    ui.config.world.wizard_done = true;
    markDirty();
    toast("向导走完了，这就保存…");
    // 先看保存结果再说"可以开始用了"：保存失败还报成功，是这个向导最伤信任的一处
    const saved = await saveAll();
    if (saved && saved.ok) {
      close();
      toast("已保存，可以开始用了");
      return;
    }
    // 失败不关窗：草稿留着，让用户能改完重试（点右上角「保存」也行）
    toast("没有保存成功：改动还在，点「完成」或右上角「保存」重试");
  }

  back.addEventListener("click", () => {
    if (index > 0) {
      index -= 1;
      render();
    }
  });
  skip.addEventListener("click", () => {
    if (index < steps.length - 1) {
      index += 1;
      render();
    } else {
      finish();
    }
  });
  next.addEventListener("click", () => {
    if (index < steps.length - 1) {
      index += 1;
      render();
    } else {
      finish();
    }
  });
  // 「稍后设置」也标记已看过：不然每次打开都弹
  done.addEventListener("click", () => {
    ui.config.world.wizard_done = true;
    markDirty();
    close();
    toast("向导先关了；在「设置 → 新手设置」里可以重新打开");
  });
  render();
}

/**
 * 一条记忆的完整字段。
 *
 * 重点是把**整理前后的样子**摆在一起：整理过的那条正文是"要点"，原文留在 ``context`` 里。
 * 之前列表只渲染 content，整理过的和没整理过的长得一模一样，看起来就像"整理没生效"。
 */
function openMemoryDetail(memory) {
  const rows = [
    ["整理状态", memory.tier === "gist" ? "已整理成要点" : "原文（还没整理）"],
    ["内容", String(memory.content || "")],
  ];
  if (String(memory.context || "").trim()) {
    rows.push(["整理前的原文", String(memory.context)]);
  }
  rows.push(
    ["类型 / 作用域", `${memory.type || ""} · ${memory.scope || ""}`],
    ["地点", memory.node_id || "无"],
    ["相关的人", (memory.related_users || []).join("、") || "无"],
    ["参与者", (memory.participants || []).join("、") || "无"],
    ["情绪 / 权重", `${memory.emotion || "无"} · ${Number(memory.weight || 0).toFixed(2)}`],
    ["来源", memory.source || "runtime"],
    ["写入时间", memoryStamp(memory.created_at)],
    ["整理时间", memory.folded_at ? memoryStamp(memory.folded_at) : "没整理过"],
    [
      "召回 / 回访",
      `召回 ${Number(memory.recall_count || 0)} 次` +
        (memory.next_review_at
          ? `；下次回访 ${memoryStamp(memory.next_review_at)}`
          : "；没有排回访"),
    ],
    ["钉住", memory.pinned ? "是（不会被顶掉）" : "否"],
  );
  openCustomDialog({
    title: "这条记忆",
    hint: "整理过的记忆只保留要点，原文收在「整理前的原文」里——忘的是细节，不是那件事。",
    confirmText: "关闭",
    build: (body) => {
      rows.forEach(([label, text]) => {
        const field = el("div", "field");
        field.appendChild(el("div", "field-head", label));
        field.appendChild(el("div", "sample-item-text", String(text || "（空）")));
        body.appendChild(field);
      });
    },
    onSubmit: () => true,
  });
}

async function loadPrompt(mode) {
  const session = $("debug-session").value;
  if (!session) {
    toast("先选择会话");
    return;
  }
  try {
    const data = await apiGet("prompt", { session, mode });
    renderDebugOutput(data, mode);
  } catch (error) {
    toast(error.message || "读取失败");
  }
}

/**
 * 预览输出：把一次几千字的提示词拆成"可折叠的段落"。
 * 一屏只看一段，剩下的折起来；原来那段纯文本一点没丢，只是好翻了。
 */
function renderDebugOutput(data, mode) {
  const box = $("debug-output");
  if (!box) return;
  const prompt = String(data.prompt || "");
  const slots = data.state_slots || [];
  const samples = data.voice_samples || [];
  ui.debugPrompt = prompt;
  box.innerHTML = "";

  const summary = $("debug-summary");
  if (summary) {
    const label = mode === "autonomous" ? "自主提示词（模拟预览）" : "接管请求（上一轮实际发出的）";
    summary.textContent = `${label} · ${Number(data.chars || prompt.length)} 字符`;
  }

  // ⓪ 真实请求账：**模拟预览**和**实跑**分开说，别把预览当成实跑（P0-1 / DBG02）
  const calls = Array.isArray(data.calls) ? data.calls : [];
  const callBox = el("div", "debug-block");
  const callTitle = el(
    "div",
    "debug-block-title",
    `最近的模型请求（${calls.length ? `${calls.length} 条` : "还没有记录"}）`,
  );
  callBox.appendChild(callTitle);
  if (calls.length) {
    const list = el("div", "debug-kv");
    calls.slice(0, 8).forEach((call) => {
      const bits = [
        String(call.kind_label || call.kind || "未知类型"),
        String(call.stage || ""),
        `${String(call.session_id || "").split(":").slice(-1)[0] || "（无会话）"}`,
        `配置 rev ${call.config_revision ?? "?"}`,
        call.provider ? `Provider ${call.provider}` : "Provider 未知",
        call.ok === false ? `失败：${call.error || "原因不明"}` : `${call.reply_chars || 0} 字符回复`,
      ].filter(Boolean);
      list.appendChild(el("span", "debug-kv-key", String(call.request_id || "")));
      const value = el("span", "debug-kv-val", bits.join("｜"));
      // 进去了 / 没进去的块：每一条都要有理由
      const included = (call.included || []).join("、");
      const excluded = (call.excluded || [])
        .map((item) => `${item.block}（${item.reason}）`)
        .join("、");
      if (included) value.appendChild(el("div", "muted", `进去了：${included}`));
      if (excluded) value.appendChild(el("div", "muted", `没进去：${excluded}`));
      list.appendChild(value);
    });
    callBox.appendChild(list);
  } else {
    callBox.appendChild(
      el("p", "muted", "她还没发出过请求；上面那份只是按当前配置拼出来的模拟预览。"),
    );
  }
  const revision = Number(data.config_revision || 0);
  callBox.appendChild(
    el(
      "p",
      "muted",
      `当前配置 version ${revision}；预览不调用模型、不发消息、不动额度。`,
    ),
  );
  box.appendChild(callBox);

  // ①- 收图失败流水（2026-10-20）：她"看不见图"的时候，这里能看出是哪一步掉的
  const mediaFails = Array.isArray(data.media_cache) ? data.media_cache : [];
  if (mediaFails.length) {
    const mediaBox = el("div", "debug-block");
    mediaBox.appendChild(
      el("div", "debug-block-title", `没取到的图（最近 ${mediaFails.length} 张）`),
    );
    const list = el("div", "debug-kv");
    mediaFails
      .slice()
      .reverse()
      .forEach((item) => {
        const reason = String(item.reason || "");
        const label =
          {
            download_failed: "地址失效 / 取不回来",
            over_round_limit: "超了这一轮上限",
            timed_out: "等超时了",
            store_failed: "入库失败",
            disabled: "本地资产库关着",
            empty_source: "地址是空的",
          }[reason] || reason || "原因不明";
        const when = item.at ? new Date(Number(item.at)).toLocaleTimeString() : "";
        list.appendChild(el("span", "debug-kv-key", `${when} ${label}`));
        const src = String(item.source || "");
        const tail = src.length > 60 ? `…${src.slice(-58)}` : src;
        list.appendChild(
          el("span", "debug-kv-val", `${tail}${item.detail ? `（${item.detail}）` : ""}`),
        );
      });
    mediaBox.appendChild(list);
    mediaBox.appendChild(
      el(
        "p",
        "muted",
        "重启就清空；放宽「一轮最多收几张图」能减少超限这一种。",
      ),
    );
    box.appendChild(mediaBox);
  }

  // ① 状态槽：槽里有东西却没进提示词，只可能是过期了
  const slotBox = el("div", "debug-block");
  slotBox.appendChild(
    el("div", "debug-block-title", `状态槽（${slots.length ? `${slots.length} 个` : "没有"}）`),
  );
  if (slots.length) {
    const list = el("div", "debug-kv");
    slots.forEach((item) => {
      const label = String(item.label || item.slot || "");
      const text = String(item.text || "").trim() || "（空）";
      list.appendChild(el("span", "debug-kv-key", label));
      list.appendChild(
        el("span", `debug-kv-val${item.expired ? " expired" : ""}`, item.expired ? `${text}（已过期，不会写进提示词）` : text),
      );
    });
    slotBox.appendChild(list);
  } else {
    slotBox.appendChild(el("p", "muted", "（没有状态槽）"));
  }
  box.appendChild(slotBox);

  // ② 声音样例：它就排在第 2 段，长提示词里靠肉眼翻太费劲
  const sampleBox = el("div", "debug-block");
  sampleBox.appendChild(
    el("div", "debug-block-title", `声音样例（本轮抽到 ${samples.length} 条）`),
  );
  if (samples.length) {
    samples.forEach((item) => {
      const row = el("div", "debug-sample");
      row.appendChild(el("span", "pill", item.scene_label || "不限"));
      row.appendChild(el("span", "", String(item.text || "")));
      if (item.move) row.appendChild(el("span", "muted", `（${item.move}）`));
      sampleBox.appendChild(row);
    });
  } else {
    sampleBox.appendChild(
      el("p", "muted", "（没有已采用的样例，这一段整段不会出现在提示词里）"),
    );
  }
  box.appendChild(sampleBox);

  // ③ 全文：按 Markdown 标题切段，和服务端那份「分段索引」对得上
  const blocks = splitPromptBlocks(prompt);
  const index = el("div", "debug-index");
  index.appendChild(el("div", "debug-block-title", `提示词全文（${blocks.length} 段）`));
  const chips = el("div", "debug-index-chips");
  blocks.forEach((block, i) => {
    const jump = el("button", "chip debug-jump", `${block.title} · ${block.text.length}`);
    jump.type = "button";
    jump.addEventListener("click", () => {
      const target = box.querySelector(`[data-block="${i}"]`);
      if (!target) return;
      target.setAttribute("open", "open");
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    chips.appendChild(jump);
  });
  index.appendChild(chips);
  box.appendChild(index);

  blocks.forEach((block, i) => {
    const detail = el("details", "debug-section");
    detail.dataset.block = String(i);
    // 第一段默认展开，剩下折起来：一屏就能看清结构
    if (i === 0) detail.setAttribute("open", "open");
    const head = el("summary");
    head.appendChild(el("span", "debug-section-title", block.title));
    head.appendChild(el("span", "muted", `${block.text.length} 字符`));
    detail.appendChild(head);
    detail.appendChild(el("pre", "pre debug-section-body", block.text));
    box.appendChild(detail);
  });
  applyDebugExpandState();
}

/** 按 `# 标题` 把提示词切成块（和服务端 prompt_section_index 的规则一致）。 */
function splitPromptBlocks(text) {
  const blocks = [];
  let title = "（开头）";
  let buffer = [];
  const flush = () => {
    const body = buffer.join("\n").trim();
    if (body || blocks.length === 0) blocks.push({ title, text: body });
    buffer = [];
  };
  String(text || "")
    .split(/\r?\n/)
    .forEach((line) => {
      const stripped = line.trim();
      let heading = "";
      if (stripped.startsWith("# ==========") && stripped.includes("：")) {
        heading = stripped.replace(/^[#=\s]+/, "").replace(/[=\s]+$/, "");
      } else if (stripped.startsWith("# ") && !stripped.startsWith("# ====")) {
        heading = stripped.slice(2).trim();
      }
      if (heading) {
        flush();
        title = heading.replace(/[：:]\s*$/, "");
        return;
      }
      buffer.push(line);
    });
  flush();
  return blocks.filter((item) => item.text || item.title !== "（开头）");
}

/** 「全部展开 / 收起」按钮：展开时所有段落 open。 */
function applyDebugExpandState() {
  const button = $("debug-expand");
  if (!button) return;
  const sections = Array.from(document.querySelectorAll("#debug-output .debug-section"));
  const allOpen = sections.length > 0 && sections.every((item) => item.hasAttribute("open"));
  button.textContent = allOpen ? "全部收起" : "全部展开";
  button.dataset.expanded = allOpen ? "1" : "";
}

/* ================================================================== */
/* Agent 页（B0/WP8c 的观测段搬到这里，只读）                            */
/* ================================================================== */
/* WP8C-AGENT-BEGIN */
/* 后端这一轮在 /state 快照里新加了 `agent` 段（core/engine.py 的 agent_section()）。
 * 这一段是**只读观测**：老实例、或者回调抛异常时后端只留默认骨架，整段可能缺席、
 * 也可能少几个键，所以下面一律按缺省处理，绝不让这一页因为读不到字段而崩掉。
 * 这一页只陈列事实：没有开关、没有说明、没有跳转（开关都在全局设置里）。
 */

/** 「插件草稿」那块面板本身（装完/删完原地重读用）。 */
let agentDraftsPane = null;
/** provider.source 的机器名 → 人话（她说的话由哪个模型生成）。 */
const AGENT_SOURCE_LABELS = {
  agent: "已配置",
  llm: "跟插件模型",
  session: "跟会话模型",
  none: "没配",
};

/** 名单类字段：最多列 10 条，多的写成"还有 N 条"（跟状态页人多时的处理一致）。 */
function agentListText(value) {
  const list = (Array.isArray(value) ? value : [])
    .map((item) => String(item == null ? "" : item).trim())
    .filter(Boolean);
  if (!list.length) return "";
  const shown = list.slice(0, 10).join("、");
  return list.length > 10 ? `${shown}…（还有 ${list.length - 10} 条）` : shown;
}

/** 状态条上的第一格：开没开 + **能不能真办事**（缺几项前提）。 */
function agentStatusText(agent) {
  const info = agent && typeof agent === "object" ? agent : {};
  if (info.enabled !== true) return "Agent 功能：未开启";
  const ready = info.ready && typeof info.ready === "object" ? info.ready : null;
  if (!ready) return "Agent 功能：已开启";
  const blockers = Array.isArray(ready.blockers) ? ready.blockers.length : 0;
  return blockers ? `Agent 功能：已开启（还差 ${blockers} 项）` : "Agent 功能：已开启 · 就绪";
}

/** 「开工条件」卡：一条前提一行，缺的那条直接把"怎么改"写在后面。 */
function agentReadyRows(agent) {
  const info = agent && typeof agent === "object" ? agent : {};
  const ready = info.ready && typeof info.ready === "object" ? info.ready : null;
  const checks = ready && Array.isArray(ready.checks) ? ready.checks : [];
  return checks
    .map((item) => {
      const row = item && typeof item === "object" ? item : {};
      const title = String(row.title || row.code || "").trim();
      if (!title) return null;
      const detail = String(row.detail || "").trim();
      const fix = String(row.fix || "").trim();
      if (row.ok === true) return [title, `✅ 满足${detail ? `：${detail}` : ""}`];
      return [title, `⚠ ${detail || "还没满足"}${fix ? `｜${fix}` : ""}`];
    })
    .filter(Boolean);
}

/** 状态条上的第二格：她这一轮说话实际用的是哪个模型。 */
function agentModelText(agent) {
  const info = agent && typeof agent === "object" ? agent : {};
  const provider = info.provider && typeof info.provider === "object" ? info.provider : {};
  const source = String(provider.source || "none");
  const name = String(provider.resolved || provider.configured || "").trim();
  const label = AGENT_SOURCE_LABELS[source] || source;
  const head = name ? `${label} · ${name}` : label;
  // P0-4：配置里写什么是一回事，"最近一次真的用了哪个"是另一回事——有就补在后面。
  // 回退也要明说：不能让用户以为"选了 Agent 模型"就一定生效了。
  const used = provider.used && typeof provider.used === "object" ? provider.used : null;
  const bits = [head];
  if (used && (used.provider || used.model)) {
    const actual = String(used.model || used.provider || "").trim();
    const how = String(used.source || "") === "agent" ? "配的" : "跟主模型";
    bits.push(`最近一次实际用：${actual || "不知道"}（${how}）`);
  }
  const reason = String(provider.fallback_reason || "").trim();
  if (reason) bits.push(reason);
  return bits.join(" · ");
}

/** 一张只读卡片：标题 + [标签, 文本] 行；一行都没有就写一句空态。

``detail`` 是"名单类"的次要信息：默认收起在「查看全部」里，展开后限高滚动，
别让四十个工具名或一长串任务把这一页铺成一片。
*/
function agentCard(title, rows, emptyText, detail, note = "") {
  const card = el("div", "card agent-card");
  card.appendChild(el("h2", "", title));
  const body = el("div", "agent-kv");
  if (!rows.length) {
    body.appendChild(el("p", "muted", emptyText || "没有可显示的项"));
  }
  rows.forEach(([label, text]) => {
    body.appendChild(el("span", "agent-kv-key", label));
    body.appendChild(el("span", "agent-kv-val", text));
  });
  card.appendChild(body);
  let lines = [];
  let label = "";
  let inlineNames = false;
  if (Array.isArray(detail)) {
    lines = detail
      .map((item) => String(item == null ? "" : item).trim())
      .filter(Boolean);
    label = `看全部 ${lines.length} 个名字`;
    inlineNames = true;
  } else if (detail && Array.isArray(detail.lines)) {
    lines = detail.lines
      .map((item) => String(item == null ? "" : item).trim())
      .filter(Boolean);
    label = detail.label || `查看全部 ${lines.length} 项`;
  }
  if (lines.length) {
    const more = el("details", "agent-card-more");
    more.appendChild(el("summary", "", label));
    let text = null;
    if (inlineNames) {
      text = el("div", "agent-card-more-text muted", lines.join("、"));
    } else {
      text = el("div", "agent-card-more-text");
      lines.forEach((line) => text.appendChild(el("div", "agent-card-more-line", line)));
    }
    if (note) text.appendChild(el("p", "agent-card-note", note));
    more.appendChild(text);
    card.appendChild(more);
  }
  return card;
}

/** 工具来源：宿主给的是 ``{工具名: 来源}``，这里翻成「来源 数量」几个字。 */
function agentToolSourceText(raw) {
  const map = raw && typeof raw === "object" ? raw : {};
  const counts = {};
  Object.keys(map).forEach((key) => {
    const source = String(map[key] || "").trim() || "未知";
    counts[source] = (counts[source] || 0) + 1;
  });
  const keys = Object.keys(counts).sort();
  return keys.map((key) => `${key} ${counts[key]}`).join(" · ");
}

/** ②③④⑤⑦ 五块只读事实；字段一律按缺省容错。 */
/** 四个子开关全关时的一句话（说明"宿主里有工具、她一个都不能用"的原因）。 */function agentSwitchOffText(switches) {
  const flags = switches && typeof switches === "object" ? switches : {};
  const names = [
    ["agent_history_lookup_enabled", "查聊天历史与经历"],
    ["agent_artifact_generation_enabled", "生成文件"],
    ["agent_file_send_enabled", "发送文件"],
    ["agent_code_execution_enabled", "执行代码"],
  ];
  const off = names.filter(([key]) => flags[key] !== true).map(([, label]) => label);
  if (!off.length) return "";
  return `开关关着：${off.join("、")}（在「全局设置 → Agent」里开）`;
}

/** Agent 的只读详情：工具名单 / 任务与产物 / 最近错误都收在卡片里的 details。 */

function agentFactCards(agent) {
  const info = agent && typeof agent === "object" ? agent : {};
  const obj = (value) => (value && typeof value === "object" ? value : {});
  const budgets = obj(info.budgets);
  const tools = obj(info.tools);
  const isolation = obj(info.isolation);
  const index = obj(info.index);
  const cards = [];

  // ⓪ 开工条件：缺哪条前提就写哪条，省得人去猜「是不是要在 AstrBot 里开 Agent」。
  const ready = obj(info.ready);
  const readyRows = agentReadyRows(info);
  const readyNote = String(ready.note || "").trim();
  if (readyNote) readyRows.push(["AstrBot 那边要开什么", readyNote]);
  if (readyRows.length) {
    const card = agentCard(
      ready.ok === true ? "开工条件（都满足了）" : "开工条件（还差几项）",
      readyRows,
      "",
    );
    // 这卡是整段话里最该先读的：占满一行，别被挤成两三个字一行
    card.classList.add("agent-card-wide");
    cards.push(card);
  }

  // ① 运行参数
  cards.push(
    agentCard("运行参数", [
      ["整件事最多", `${num(budgets.query_total_seconds, 2)} 秒`],
      ["一次调用最多", `${num(budgets.request_timeout_seconds, 0.8)} 秒`],
      ["索引批量上限", `${num(budgets.index_batch_seconds, 30)} 秒`],
      ["最多几步", String(num(info.max_steps, 32))],
    ]),
  );

  // ② 工具：卡片只放数量与来源，几十个名字进折叠块（限高滚动）
  const toolRows = [["已允许", `${num(tools.allowed_count, 0)} 个`]];
  const allowedText = agentListText(tools.allowed);
  if (allowedText) toolRows.push(["允许清单", allowedText]);
  const available = (Array.isArray(tools.available) ? tools.available : [])
    .map((item) => String(item == null ? "" : item).trim())
    .filter(Boolean);
  if (available.length) toolRows.push(["宿主里可用", `${available.length} 个`]);
  const sourceText = agentToolSourceText(tools.sources);
  if (sourceText) toolRows.push(["按来源", sourceText]);
  // P0-6：旧「通用工具名单」的对账——孤儿工具必须点名，否则迁移时会悄悄丢
  const legacy = obj(tools.legacy);
  if (!legacy.error && num((legacy.counts || {}).total, 0) > 0) {
    const orphans = Array.isArray(legacy.orphans) ? legacy.orphans : [];
    const total = num((legacy.counts || {}).total, 0);
    toolRows.push([
      "旧通用名单",
      orphans.length
        ? `${total} 个，其中 ${orphans.length} 个没有动作认领：${orphans.join("、")}`
        : `${total} 个，都已有动作认领`,
    ]);
  }
  if (!num(tools.allowed_count, 0) && available.length) {
    toolRows.push(["状态", "一个都没放出来"]);
  }
  cards.push(
    agentCard(
      "工具",
      toolRows,
      "还没有可用工具",
      available,
      !num(tools.allowed_count, 0) && available.length
        ? agentSwitchOffText(info.switches) || "四个子开关都关着。"
        : "",
    ),
  );

  // ③ 隔离与记忆范围
  cards.push(
    agentCard("隔离与记忆范围", [
      ["记忆范围", String(isolation.memory_scope_mode || "").trim() || "没设置"],
      ["空人设时的范围", String(isolation.empty_persona_scope || "").trim() || "group"],
    ]),
  );

  // ④ 语义索引：卡片保持 3 行；错误进弹窗，别把卡片撑成三倍高
  const indexRows = [
    ["向量模型", index.embedding_configured === true ? "已配置" : "没配"],
    ["向量空间", String(index.space_fingerprint || "").trim() || "未装配"],
    ["已索引 / 待处理", `${num(index.indexed, 0)} / ${num(index.pending, 0)}`],
  ];
  // 维度：0 表示还没装配好。来路写清楚——是宿主自报的，还是供应商配置里填的
  // （用户 m26700：宿主供应商不一定实现 get_dim，但配置里填的维度是可信的）
  const indexDim = Number(index.dim || 0);
  if (indexDim > 0) {
    const source = String(index.dim_source || "");
    const where =
      source === "provider_config" ? "供应商里填的" : source === "get_dim" ? "供应商自报" : "";
    indexRows.splice(2, 0, ["维度", where ? `${indexDim} 维（${where}）` : `${indexDim} 维`]);
  }
  const indexProblem = String(index.problem || "").trim();
  const lastError = String(index.last_error || "").trim();
  if (indexProblem) indexRows.push(["为什么没开", "有，展开看"]);
  if (lastError) indexRows.push(["最近一次错误", "有，展开看"]);
  const indexCard = agentCard("语义索引", indexRows);
  if (indexProblem) {
    const problemMore = el("details", "agent-card-more");
    problemMore.appendChild(el("summary", "", "为什么没开"));
    const problemText = el("div", "agent-card-more-text");
    problemText.appendChild(el("div", "agent-card-more-line", indexProblem));
    problemMore.appendChild(problemText);
    indexCard.appendChild(problemMore);
  }
  if (lastError) {
    const indexMore = el("details", "agent-card-more");
    indexMore.appendChild(el("summary", "", "查看最近错误"));
    const indexText = el("div", "agent-card-more-text");
    indexText.appendChild(el("div", "agent-card-more-line", lastError));
    indexMore.appendChild(indexText);
    indexCard.appendChild(indexMore);
  }
  cards.push(indexCard);

  return cards;
}

/** 「任务与产物」这一页：任务一卡、产物一卡，各自把条目逐行列出来（用户 m45903）。 */
function agentWorkCards(agent) {
  const info = agent && typeof agent === "object" ? agent : {};
  const obj = (value) => (value && typeof value === "object" ? value : {});
  const cards = [];
  [
    ["任务", obj(info.tasks), agentTaskLine, "还没有任务", "一次让她动手办的事（写插件、查资料、出文件）。"],
    ["产物", obj(info.artifacts), agentArtifactLine, "还没有产物", "她办完事留下的文件：发出去的、没发出去的都在这里。"],
  ].forEach(([title, section, render, emptyText, note]) => {
    const card = el("div", "card agent-card agent-card-wide");
    card.appendChild(el("h2", "", title));
    card.appendChild(el("p", "section-note", note));
    if (title === "任务" && info.busy === true) {
      // 用户 m10183：她手上压着一件要动手的事时，页面要说清"正在跑"，以及
      // "你已经叫停/改口、她还没走到检查点"这两种中间态。
      const control = obj(info.control);
      const tail = control.cancel
        ? "（你已经叫停，它下一步就会停）"
        : control.amend
          ? `（你已经改口：${String(control.amend).slice(0, 30)}）`
          : "（这会儿发「算了」或「改成…」会交到它手上）";
      card.appendChild(el("p", "hint", `正在跑：她手上还有一件事没做完${tail}`));
    }
    const box = el("div", "agent-list");
    const sectionNote = String((section && section.note) || "").trim();
    const items = Array.isArray(section.items) ? section.items : [];
    if (!section || section.supported !== true) {
      box.appendChild(el("p", "muted", sectionNote || emptyText));
    } else if (sectionNote) {
      // 读失败 ≠ 没记录
      box.appendChild(el("p", "muted", `读不出来：${sectionNote}`));
    } else if (!items.length) {
      box.appendChild(el("p", "muted", emptyText));
    } else {
      const list = el("div", "agent-list-rows");
      items.forEach((item) => list.appendChild(el("div", "agent-list-row", render(item))));
      box.appendChild(list);
      if (items.length >= 20) {
        box.appendChild(el("p", "muted", `只列了最近 ${items.length} 条。`));
      }
    }
    card.appendChild(box);
    cards.push(card);
  });
  return cards;
}

/** 一条任务的一行摘要：目标 / 状态 / 步数 / 用时（失败原因跟在后面）。 */
function agentTaskLine(task) {
  const item = task && typeof task === "object" ? task : {};
  const bits = [String(item.goal || "").trim() || String(item.id || "未命名任务")];
  bits.push(String(item.status_label || item.status || "未记录"));
  const steps = Number(item.steps || 0);
  if (steps > 0) bits.push(`${Number(item.ok_steps || 0)}/${steps} 步`);
  const ms = Number(item.duration_ms || 0);
  if (ms > 0) bits.push(`${Math.round(ms / 1000)} 秒`);
  const tail = String(item.error || item.note || "").trim();
  return bits.join("｜") + (tail ? `｜${tail.slice(0, 40)}` : "");
}

/** 一条产物的一行摘要：文件名 / 类型 / 大小 / 发没发出去。 */
function agentArtifactLine(artifact) {
  const item = artifact && typeof artifact === "object" ? artifact : {};
  const bits = [String(item.name || item.id || "未命名文件")];
  const mime = String(item.mime || "").trim();
  if (mime) bits.push(mime);
  const size = Number(item.size_bytes || 0);
  if (size > 0) bits.push(formatBytes(size));
  bits.push(item.sent === true ? "已发出" : "还没发出");
  return bits.join("｜");
}

/** 「学习状态」标签：她现在学到了什么、上次整理干了什么、为什么还是 0。 */
function agentLearningPanel(learning) {
  const data = learning && typeof learning === "object" ? learning : {};
  const counts = data.counts && typeof data.counts === "object" ? data.counts : {};
  const skills = data.skill_counts && typeof data.skill_counts === "object" ? data.skill_counts : {};
  const tidy = data.last_tidy && typeof data.last_tidy === "object" ? data.last_tidy : {};
  const card = agentCard(
    "学习状态",
    [
      ["能用的经验", `${counts.active || 0} 条（说话时按情境挑相关的几条）`],
      ["候选经验", `${counts.candidate || 0} 条（证据够了会自动发布）`],
      ["过时的", `${counts.deprecated || 0} 条（依赖变了，不再教她）`],
      ["撤销的", `${counts.revoked || 0} 条（你或她明确否掉）`],
      ["流程", `${skills.active || 0} 条可用 · ${skills.candidate || 0} 条候选`],
      ["上次整理", tidy.note ? String(tidy.note) : "还没整理过"],
    ],
    "",
    null,
    String(data.note || "")
  );
  // 光有"几条几条"看不出她到底学到了什么（用户 m45903）：把每条经验摆出来
  const recent = Array.isArray(data.recent) ? data.recent : [];
  const box = el("div", "agent-learning-list");
  if (!recent.length) {
    box.appendChild(
      el("p", "muted", "还没有记下来的经验。经验只从她真的做成的 Agent 任务里提炼（日常闲聊不进这份账）。")
    );
  } else {
    recent.forEach((raw) => {
      const item = raw && typeof raw === "object" ? raw : {};
      const row = el("div", "agent-learning-item");
      const head = el("div", "agent-skill-head");
      head.appendChild(el("strong", "", String(item.situation || "（没记情境）")));
      head.appendChild(el("span", "tag", String(item.status_label || item.status || "")));
      const confidence = num(item.confidence, 0);
      if (confidence > 0) head.appendChild(el("span", "badge", `把握 ${confidence}`));
      const evidence = num(item.evidence, 0);
      if (evidence > 0) head.appendChild(el("span", "badge", `证据 ${evidence} 条`));
      row.appendChild(head);
      const method = String(item.method || "").trim();
      if (method) row.appendChild(el("p", "agent-learning-method", method));
      const stamp = num(item.updated_at, 0);
      if (stamp > 0) row.appendChild(el("p", "muted", `更新于 ${formatStamp(stamp)}`));
      box.appendChild(row);
    });
    if (num(counts.active, 0) + num(counts.candidate, 0) > recent.length) {
      box.appendChild(el("p", "muted", `只列了最近 ${recent.length} 条。`));
    }
  }
  card.appendChild(box);
  const foot = el("div", "agent-actions");
  const button = el("button", "btn small", "现在整理一遍");
  button.addEventListener("click", () => tidyKnowledgeNow(button));
  foot.appendChild(button);
  card.appendChild(foot);
  return card;
}

async function tidyKnowledgeNow(button) {
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    const result = await apiPost("knowledge/tidy", {});
    const note = result && result.note ? String(result.note) : "整理完了";
    toast(note);
    await loadAgentLearning();
  } catch (error) {
    toast(`整理失败：${error && error.message ? error.message : error}`);
  } finally {
    button.disabled = false;
  }
}

/** 「技能」卡片：内置 + 提炼出来的流程；内置也能展开看步骤（用户 m5221）。 */
function agentSkillsPanel(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const card = el("div", "card agent-card agent-card-wide");
  const top = el("div", "agent-skill-top");
  top.appendChild(el("h2", "", `技能（${list.length} 条）`));
  const formHost = el("div", "agent-skill-form-host");
  const add = el("button", "btn small", "新增一条流程");
  add.addEventListener("click", () => agentSkillForm(formHost));
  top.appendChild(add);
  // 用户 m12067：内置流程落成插件数据目录里的 skills/*.md，能直接改
  const folder = el("button", "btn small ghost", "skill 文件夹");
  folder.addEventListener("click", async () => {
    try {
      const result = await apiPost("skills/folder", {});
      if (result && result.ok) toast(`打开：${result.path || ""}`);
      else toast(`打不开：${(result && result.reason) || "不知道原因"}`);
    } catch (error) {
      toast(`打不开：${error && error.message ? error.message : error}`);
    }
  });
  top.appendChild(folder);
  card.appendChild(top);
  card.appendChild(formHost);
  if (!list.length) {
    card.appendChild(el("p", "muted", "还没有流程：她先把一件事做成，经验攒够之后才会固化。"));
    return card;
  }
  const box = el("div", "agent-skill-list");
  list.forEach((raw) => {
    const item = raw && typeof raw === "object" ? raw : {};
    const official = String(item.source || "") === "official";
    const row = el("div", "agent-skill");
    const head = el("div", "agent-skill-head");
    head.appendChild(el("strong", "", String(item.title || item.skill_id || "未命名流程")));
    head.appendChild(el("span", "tag", agentSkillStatusText(item.status)));
    head.appendChild(
      el("span", "badge", official ? "内置" : String(item.source_label || "自己加的"))
    );
    const missing = Array.isArray(item.missing) ? item.missing.filter(Boolean) : [];
    if (missing.length) head.appendChild(el("span", "badge off", `缺 ${missing.join("、")}`));
    row.appendChild(head);
    const triggers = Array.isArray(item.when_to_use)
      ? item.when_to_use.filter(Boolean)
      : Array.isArray(item.triggers)
        ? item.triggers.filter(Boolean)
        : [];
    if (triggers.length) {
      row.appendChild(el("p", "muted", `什么时候用：${triggers.join("、")}`));
    }
    const steps = Array.isArray(item.steps) ? item.steps.filter(Boolean) : [];
    const summary = String(item.summary || "").trim();
    if (summary) row.appendChild(el("p", "muted", summary));
    // 用户 m9693：步骤/说明/示例/坑太多，展开会把列表撑爆——统一放详情弹窗
    if (official && String(item.file || "").trim()) {
      const edit = el("button", "btn small ghost", "改");
      edit.addEventListener("click", () => editSkillFile(String(item.file), edit));
      head.appendChild(edit);
    }
    const detail = el("button", "btn small ghost", steps.length ? `详情（${steps.length} 步）` : "详情");
    detail.addEventListener("click", () => openSkillDetail(item));
    head.appendChild(detail);
    if (!steps.length && String(item.note || "").trim()) {
      row.appendChild(el("p", "muted", String(item.note)));
    }
    const caps = Array.isArray(item.capabilities) ? item.capabilities.filter(Boolean) : [];
    if (caps.length) {
      row.appendChild(el("p", "muted", `要用到：${caps.join("、")}`));
    }
    if (!official) {
      const actions = el("div", "agent-actions");
      if (String(item.status || "") !== "active") {
        const on = el("button", "btn small", "发布");
        on.addEventListener("click", () => setSkillStatus(item, "promote", on));
        actions.appendChild(on);
      }
      if (String(item.status || "") !== "revoked") {
        const off = el("button", "btn small danger", "撤销");
        off.addEventListener("click", () => setSkillStatus(item, "revoke", off));
        actions.appendChild(off);
      }
      const del = el("button", "btn small danger", "删掉");
      del.addEventListener("click", () => deleteSkill(item, del));
      actions.appendChild(del);
      row.appendChild(actions);
    }
    box.appendChild(row);
  });
  card.appendChild(box);
  return card;
}

/** 手动新增流程的表单（填完存成「自己加的」，直接可用）。 */
function openSkillDetail(item) {
  const skill = item && typeof item === "object" ? item : {};
  const lines = [];
  const push = (label, value) => {
    const text = String(value || "").trim();
    if (text) lines.push(`${label}：${text}`);
  };
  push("说明", skill.summary);
  const triggers = Array.isArray(skill.when_to_use)
    ? skill.when_to_use.filter(Boolean)
    : Array.isArray(skill.triggers)
      ? skill.triggers.filter(Boolean)
      : [];
  if (triggers.length) push("什么时候用", triggers.join("、"));
  push("什么时候别用", skill.when_not);
  const steps = Array.isArray(skill.steps) ? skill.steps.filter(Boolean) : [];
  if (steps.length) {
    lines.push("步骤：");
    steps.forEach((step, index) => {
      const text = String((step && step.text) || step || "").trim();
      const action = String((step && step.action) || "").trim();
      if (text) lines.push(`  ${index + 1}. ${text}${action ? `（${action}）` : ""}`);
    });
  }
  push("做完检查", skill.verify);
  const caps = Array.isArray(skill.capabilities) ? skill.capabilities.filter(Boolean) : [];
  const missing = Array.isArray(skill.missing) ? skill.missing.filter(Boolean) : [];
  if (caps.length) {
    push("要用到", missing.length ? `${caps.join("、")}（现在缺：${missing.join("、")}）` : caps.join("、"));
  }
  push("备注", skill.note);
  confirmDialog({
    title: String(skill.title || "这条流程"),
    message: lines.join("\n"),
    confirmText: "知道了",
    danger: false,
  });
}

async function editSkillFile(file, button) {
  if (button) button.disabled = true;
  try {
    const data = await apiGet("skills/files", {});
    const found = (Array.isArray(data && data.files) ? data.files : []).find(
      (row) => String(row.file || "") === file
    );
    if (!found) {
      toast("没找到这一份文件，刷新一下再试");
      return;
    }
    openFormDialog({
      title: `改「${found.title || file}」`,
      hint: "改完保存，下一轮就按新的走。「## 步骤」这一节不能删。",
      wide: true,
      fields: [{ key: "text", label: `内容（${file}）`, type: "textarea", rows: 22 }],
      values: { text: String(found.text || "") },
      confirmText: "保存",
      onSubmit: async (values) => {
        const result = await apiPost("skills/files/save", {
          file,
          text: String(values.text || ""),
        });
        if (result && result.ok) {
          toast(`改好了：${result.skill || file}`);
          loadAgentLearning();
          return true;
        }
        toast(`没保存：${(result && result.reason) || "不知道原因"}`);
        return false;
      },
    });
  } catch (error) {
    toast(`读不出来：${error && error.message ? error.message : error}`);
  } finally {
    if (button) button.disabled = false;
  }
}

function agentSkillForm(slot) {
  if (!slot) return;
  if (slot.childElementCount) {
    slot.innerHTML = "";
    return;
  }
  const box = el("div", "agent-skill-form");
  const title = el("input", "input");
  title.placeholder = "标题（例如「查账单之前先确认时间」）";
  const triggers = el("input", "input");
  triggers.placeholder = "什么时候用（逗号分开，例如：查账单, 对不上）";
  const steps = el("textarea", "input");
  steps.rows = 4;
  steps.placeholder = "一步一步怎么写，一行一步";
  const caps = el("input", "input");
  caps.placeholder = "要用到的工具（可选，逗号分开，例如：deepseek_web_search）";
  const summary = el("input", "input");
  summary.placeholder = "一句话说明（可选，例如：发之前先对一遍数字）";
  const whenNot = el("input", "input");
  whenNot.placeholder = "什么时候别用（可选）";
  const examples = el("textarea", "input");
  examples.rows = 3;
  examples.placeholder = "示例（可选，一行一个）";
  const pitfalls = el("textarea", "input");
  pitfalls.rows = 3;
  pitfalls.placeholder = "常见坑（可选，一行一个）";
  const verify = el("input", "input");
  verify.placeholder = "怎么算做成（可选，例如：文件对方能打开）";
  [title, triggers, summary, whenNot, steps, caps, examples, pitfalls, verify].forEach((field) =>
    box.appendChild(field)
  );
  const actions = el("div", "agent-actions");
  const save = el("button", "btn small", "存下这条流程");
  save.addEventListener("click", () =>
    saveSkill(
      {
        title: title.value,
        triggers: triggers.value,
        steps: steps.value,
        capabilities: caps.value,
        summary: summary.value,
        when_not: whenNot.value,
        examples: examples.value,
        pitfalls: pitfalls.value,
        verify: verify.value,
      },
      save
    )
  );
  const cancel = el("button", "btn small ghost", "取消");
  cancel.addEventListener("click", () => {
    slot.innerHTML = "";
  });
  actions.appendChild(save);
  actions.appendChild(cancel);
  box.appendChild(actions);
  slot.appendChild(box);
}

async function saveSkill(payload, button) {
  if (!button || button.disabled) return;
  if (!String(payload.title || "").trim() || !String(payload.steps || "").trim()) {
    toast("标题和至少一步都要写");
    return;
  }
  button.disabled = true;
  try {
    await apiPost("skills/create", payload);
    toast("记下了：这条流程现在就能用");
    await loadAgentLearning();
  } catch (error) {
    toast(`没存下：${error && error.message ? error.message : error}`);
  } finally {
    button.disabled = false;
  }
}

async function deleteSkill(skill, button) {
  if (!button || button.disabled) return;
  const item = skill && typeof skill === "object" ? skill : {};
  const ok = await confirmDialog({
    title: "删掉这条流程？",
    message: `「${String(item.title || item.skill_id || "")}」删掉之后她就用不上了。`,
    confirmText: "删掉",
    danger: true,
  });
  if (!ok) return;
  button.disabled = true;
  try {
    await apiPost("skills/delete", { skill_id: item.skill_id });
    toast("已删掉");
    await loadAgentLearning();
  } catch (error) {
    toast(`没删成：${error && error.message ? error.message : error}`);
  } finally {
    button.disabled = false;
  }
}

function agentSkillStatusText(status) {
  const key = String(status || "");
  if (key === "active") return "可用";
  if (key === "candidate") return "候选";
  if (key === "revoked") return "已撤销";
  return key || "未记录";
}

async function setSkillStatus(skill, action, button) {
  if (!button || button.disabled) return;
  const item = skill && typeof skill === "object" ? skill : {};
  button.disabled = true;
  try {
    await apiPost("skills/status", { skill_id: item.skill_id, action });
    toast(action === "revoke" ? "已撤销这条流程" : "已发布这条流程");
    await loadAgentLearning();
  } catch (error) {
    toast(`没改成：${error && error.message ? error.message : error}`);
  } finally {
    button.disabled = false;
  }
}

/**
 * 页签里那一块的内容：只有真的切过去（或刷新）才去拉。
 *
 * 首次渲染不该顺手打三个请求——`renderAgentPage` 每刷新一次状态都会跑，
 * 而「看着的页签」永远是其中一页（用户 m45903 要的是翻页/切换，不是三份并发）。
 */
function agentPaneLoader(name) {
  if (name === "learning") return loadAgentLearning();
  if (name === "drafts") {
    const host = $("agent-drafts");
    if (host && !host.children.length) host.appendChild(agentDraftsPanel().card);
    return loadAgentDrafts();
  }
  if (name === "runs") return loadAgentRuns();
  return null;
}

/** 切 Agent 页的四个页签（用户 m45903）：任务与产物 / 学习 / 插件草稿 / 工具调用。 */
function showAgentPane(name, reload = false) {
  const keys = ["work", "learning", "drafts", "runs"];
  const key = keys.includes(name) ? name : "work";
  ui.agent.pane = key;
  const seg = $("agent-seg");
  // 冒烟测试里的 DOM 桩只有最小实现，没有 querySelectorAll；页面本身有，别让少一个节点就崩。
  if (seg && typeof seg.querySelectorAll === "function") {
    Array.from(seg.querySelectorAll("button[data-seg]")).forEach((button) => {
      button.classList.toggle("on", button.dataset.seg === key);
    });
  }
  keys.forEach((item) => {
    const pane = $(`agent-pane-${item}`);
    if (pane) pane.classList.toggle("hidden", item !== key);
  });
  if (reload) {
    const job = agentPaneLoader(key);
    if (job && typeof job.catch === "function") job.catch(() => {});
  }
  return key;
}

/** 学习页：经验逐条列 + 技能清单（用户 m45903 把两个小标签并成一页，别再让人点空按钮）。 */
async function loadAgentLearning() {
  const body = $("agent-learning");
  if (!body) return;
  body.innerHTML = "";
  body.appendChild(el("p", "muted", "读取中…"));
  try {
    // 列表走 /skills（内置带 file，才渲染得出「改 md」）；读不到就退回 learning 那份
    const [data, skillsData] = await Promise.all([
      apiGet("learning"),
      apiGet("skills").catch(() => null),
    ]);
    const skills =
      skillsData && Array.isArray(skillsData.skills)
        ? skillsData.skills
        : data && data.skills;
    body.innerHTML = "";
    body.appendChild(agentLearningPanel(data));
    body.appendChild(agentSkillsPanel(skills));
  } catch (error) {
    body.innerHTML = "";
    body.appendChild(el("p", "muted", `读不到学习状态：${error && error.message ? error.message : error}`));
  }
}

/**
 * Agent 页开关清单（用户 m8694）：和「全局设置 → Agent（自主能力）」那一节是同一批
 * 世界配置，只是标签更口语、不带说明。两处**必须**覆盖同一组键 ——
 * 以前这一页少了 `inspect_capability_enabled` / `ask_user_enabled`，同一件事在
 * 设置页能开、在 Agent 页看不到（用户 m00001 第 8 条）。测试按这张表逐键核对。
 */
function agentSwitchesCard() {
  const world = ui.config.world || {};
  const agent = world.agent || (world.agent = {});
  const card = el("div", "card agent-card agent-card-wide");
  card.appendChild(el("h2", "", "开关"));
  card.appendChild(
    el(
      "p",
      "muted",
      "和 全局设置 → Agent（自主能力）是同一批开关（同一份世界配置）；改完点右上角保存。",
    ),
  );
  const body = el("div", "agent-switch-body");
  const flip = (key) => (value) => {
    agent[key] = value === true;
    markDirty();
  };
  [
    ["总开关（允许她做事）", "enabled"],
    ["翻聊天记录", "history_lookup_enabled"],
    ["生成文件", "artifact_generation_enabled"],
    ["把文件发出去", "file_send_enabled"],
    ["跑代码", "code_execution_enabled"],
    ["办不成先查资料", "research_enabled"],
    ["确认插件工坊的风险", "plugin_forge_risk_accepted"],
    ["允许她写插件", "plugin_forge_enabled"],
    ["允许她自己装", "plugin_forge_auto_install"],
    // 这两项设置页一直能改、Agent 页却看不到（用户 m00001 第 8 条）：补在这里，
    // 免得同一件事在两页里对不上。
    ["查看动作说明", "inspect_capability_enabled"],
    ["缺信息时问一句", "ask_user_enabled"],
  ].forEach(([label, key]) => {
    body.appendChild(checkboxField(label, agent[key] === true, flip(key), {}));
  });
  body.appendChild(
    selectField(
      "动系统的事先问主人",
      ownerApprovalModeOf(world),
      OWNER_APPROVAL_MODES,
      (value) => {
        agent.owner_approval_mode = value;
        agent.owner_approval_enabled = value !== "off";
        markDirty();
      },
      { hint: "只问危险的 / 每件都问 / 不问。主人是谁在全局设置里配。" },
    ),
  );
  card.appendChild(body);
  return card;
}

/** Agent 页正文：状态条 + 五块事实卡，其余四块按页签现取（用户 m45903）。 */
function renderAgentPage(agent) {
  const info = agent && typeof agent === "object" ? agent : {};
  const head = $("agent-head");
  if (head) {
    head.innerHTML = "";
    head.appendChild(el("span", "chip", agentStatusText(info)));
    head.appendChild(el("span", "chip", `模型：${agentModelText(info)}`));
  }
  const box = $("agent-cards");
  if (!box) return;
  box.innerHTML = "";
  box.appendChild(agentSwitchesCard());
  agentFactCards(info).forEach((card) => box.appendChild(card));
  const work = $("agent-work");
  if (work) {
    work.innerHTML = "";
    agentWorkCards(info).forEach((card) => work.appendChild(card));
  }
  showAgentPane(ui.agent.pane || "work");
}

/** 插件草稿：她自己写的东西，装不装由人按（用户 m31581 / m31660）。 */
function agentDraftsPanel() {
  const card = el("div", "card agent-card agent-card-wide");
  card.appendChild(el("h2", "", "插件草稿"));
  const body = el("div", "agent-draft-body");
  body.appendChild(el("p", "muted", "读不到草稿清单。"));
  card.appendChild(body);
  // 记住这块面板：装完/删完要原地重读一遍，重新渲染整页会把滚动位置和别的页签状态丢掉。
  agentDraftsPane = body;
  return { card, body };
}

/** 一份草稿：叫什么、几个文件、装没装，以及三个只读动作。 */
function agentDraftLines(data) {
  const rows = [];
  const forge = (data && data.forge) || {};
  const drafts = (data && Array.isArray(data.drafts) && data.drafts) || [];
  if (!forge.enabled) {
    rows.push(["开关", "她还没被允许写插件（全局设置 → Agent（自主能力）里的「允许她写插件」）"]);
  } else if (!forge.auto_install) {
    rows.push(["自装", "关着：她只写草稿，装不装由你按"]);
  }
  if (!drafts.length) {
    rows.push(["草稿", "还没有。让她办一件要动手的事，才可能写出一份。"]);
    return rows;
  }
  drafts.forEach((item, index) => {
    const files = Array.isArray(item.files) ? item.files : [];
    const state = item.installed ? "已装进宿主" : "还没装";
    rows.push([
      `草稿 ${index + 1}`,
      `${item.name}（${item.dir}）· ${files.length} 个文件 · ${state}`,
    ]);
  });
  return rows;
}

async function loadAgentDrafts(pane = null) {
  // 一律由 renderAgentPage 把面板交进来：这一段会被单独抽出来跑，别在模块里找 id。
  // 装完/删完的重新加载不给 pane，走这里记住的那块（见 agentDraftsPanel）。
  const body = pane || agentDraftsPane;
  if (!body) return;
  let data = null;
  try {
    data = await apiGet("plugin-drafts");
  } catch (exc) {
    body.innerHTML = "";
    body.appendChild(el("p", "muted", `读不到草稿清单：${exc && exc.message ? exc.message : exc}`));
    return;
  }
  body.innerHTML = "";
  agentDraftLines(data).forEach(([label, text]) => {
    const row = el("div", "agent-draft-row");
    row.appendChild(el("span", "muted", label));
    row.appendChild(el("span", "", text));
    body.appendChild(row);
  });
  const drafts = (data && Array.isArray(data.drafts) && data.drafts) || [];
  drafts.forEach((item) => {
    const row = el("div", "agent-draft-actions");
    row.appendChild(el("span", "muted", item.name));
    const look = el("button", "small", "看全文");
    look.type = "button";
    look.addEventListener("click", () => showDraftDetail(item.slug));
    row.appendChild(look);
    if (!item.installed) {
      const install = el("button", "small", "装上");
      install.type = "button";
      install.addEventListener("click", () => installDraft(item.slug, install));
      row.appendChild(install);
    }
    const drop = el("button", "small danger", "删掉");
    drop.type = "button";
    drop.addEventListener("click", () => deleteDraft(item.slug, drop));
    row.appendChild(drop);
    body.appendChild(row);
  });
}

async function showDraftDetail(slug) {
  try {
    const data = await apiGet(`plugin-drafts/detail?slug=${encodeURIComponent(slug)}`);
    const files = (data && data.files) || {};
    const names = Object.keys(files);
    if (!names.length) {
      toast("这份草稿读不出来");
      return;
    }
    const parts = names.map((name) => `${name}（${String(files[name]).length} 字）`);
    const check = (data && data.check) || {};
    const trouble = [].concat(check.errors || [], check.warnings || []);
    toast(
      `${data.name}：${parts.join("、")}${trouble.length ? `｜${trouble[0]}` : "｜校验通过"}`,
    );
  } catch (exc) {
    toast(`读不到这份草稿：${exc && exc.message ? exc.message : exc}`);
  }
}

async function installDraft(slug, button) {
  if (button) button.disabled = true;
  try {
    const result = await apiPost("plugin-drafts/install", { slug });
    toast((result && result.reason) || "已装进 AstrBot，重启后生效");
    await loadAgentDrafts();
  } catch (exc) {
    toast(`装不上：${exc && exc.message ? exc.message : exc}`);
  } finally {
    if (button) button.disabled = false;
  }
}

async function deleteDraft(slug, button) {
  if (button) button.disabled = true;
  try {
    const result = await apiPost("plugin-drafts/delete", { slug });
    toast((result && result.reason) || "已删掉这份草稿");
    await loadAgentDrafts();
  } catch (exc) {
    toast(`删不掉：${exc && exc.message ? exc.message : exc}`);
  } finally {
    if (button) button.disabled = false;
  }
}
/* WP8C-AGENT-END */

/** 进 Agent 页就现读一次 /state：跟控制台同一份快照，读不到就照旧渲染上次那份。 */
async function loadAgentPage() {
  const select = $("status-session");
  const sessionId = select ? select.value : "";
  if (!sessionId) {
    renderAgentPage(null);
    return;
  }
  try {
    const data = await apiGet("state", { session: sessionId });
    ui.status = data;
    renderAgentPage(data.agent);
  } catch (error) {
    renderAgentPage((ui.status || {}).agent);
    const head = $("agent-head");
    // 一句 muted 就够：这一页是事实陈列，读不到不该弹错误框打断
    if (head) {
      const reason = error && error.message ? String(error.message) : String(error);
      head.appendChild(el("span", "chip", `读取失败：${reason}`));
    }
  }
}

/* ---------------- 工具调用：她调用过哪些工具（只读，倒序分页） ---------------- */

/** 一页拉多少条。后端硬上限 100，这里 50，翻页只认 next_cursor。 */
const AGENT_RUNS_PAGE = 50;

function agentRunStamp(seconds) {
  const value = num(seconds, 0);
  return value > 0 ? formatStamp(value) : "—";
}

/** 耗时：毫秒 → 人话（跨 tick 的动作能跑几十分钟，写 3628227 ms 没人看得懂）。 */
function agentDurationText(value) {
  const ms = num(value, 0);
  if (ms <= 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  // 先取整到秒再进位，否则 899989ms 会写成「14 分 60 秒」
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${Math.round(ms / 100) / 10} 秒`;
  const minutes = Math.floor(seconds / 60);
  const restSeconds = seconds % 60;
  if (minutes < 60) {
    return restSeconds ? `${minutes} 分 ${restSeconds} 秒` : `${minutes} 分`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? `${hours} 小时 ${restMinutes} 分` : `${hours} 小时`;
}

/** 一行明细：只写有值的字段，缺字段就跳过（这一页一律按缺省容错）。 */
function agentRunDetail(item) {
  const kv = el("div", "agent-kv");
  const add = (label, value) => {
    const text = String(value === null || value === undefined ? "" : value).trim();
    if (!text) return;
    kv.appendChild(el("span", "agent-kv-key", label));
    kv.appendChild(el("span", "agent-kv-val", text));
  };
  add("事件编号", item.id);
  add("会话", item.session);
  add("工具调用", item.tool_call_id);
  add("任务", item.task_id);
  add("产物句柄", item.artifact_id);
  add("下一页游标", item.next_cursor);
  add("副作用", (item.side_effects || []).join("、"));
  add("来源事件", (item.source_event_ids || []).join("、"));
  const content = String(item.content || "");
  if (content) {
    const chars = num(item.content_chars, 0);
    add(`回执原文（${chars} 字${item.truncated ? "，已截断" : ""}）`, content);
  }
  return kv;
}

/** 一行：一句话看完，点开才是编号、回执原文这些排查用的东西。 */
function agentRunRow(item) {
  const wrap = el("details", "agent-run");
  const head = el("summary", "agent-run-row");
  head.appendChild(el("span", "agent-run-cell time", agentRunStamp(item.at)));
  head.appendChild(el("span", "agent-run-cell name", item.name || "（没记名字）"));
  head.appendChild(el("span", "agent-run-cell where", item.target || "—"));
  head.appendChild(
    el("span", `agent-run-cell status ${item.ok ? "ok" : "bad"}`, item.status_label || ""),
  );
  head.appendChild(el("span", "agent-run-cell cost", agentDurationText(item.duration_ms)));
  head.appendChild(el("span", "agent-run-cell note", item.summary || ""));
  wrap.appendChild(head);
  const body = el("div", "agent-run-body");
  body.appendChild(agentRunDetail(item));
  wrap.appendChild(body);
  return wrap;
}

function renderAgentRuns() {
  const box = $("agent-runs");
  if (!box) return;
  const state = ui.agent;
  const rows = state.items || [];
  box.innerHTML = "";
  if (state.loading && !rows.length) {
    box.appendChild(el("p", "muted", "读取中…"));
  }
  if (rows.length) {
    const table = el("div", "agent-run-table");
    rows.forEach((row) => table.appendChild(agentRunRow(row)));
    box.appendChild(table);
    // 已经有记录了：这一句只是"这一页没拉全"的说明，不能把表顶掉
    if (state.note) box.appendChild(el("p", "muted", state.note));
  } else if (!state.loading) {
    box.appendChild(el("p", "muted", state.note || "她还没有调用过工具。"));
  }
  const count = $("agent-runs-count");
  if (count) {
    count.textContent = rows.length ? `第 ${state.page || 1} 页 · ${rows.length} 条` : "";
  }
  const prev = $("agent-runs-prev");
  if (prev) prev.classList.toggle("hidden", !((state.page || 1) > 1 && !state.loading));
  const more = $("agent-runs-more");
  if (more) more.classList.toggle("hidden", !(state.hasMore && !state.loading));
}

/** 拉第 N 页（用户 m45903：要能前后翻）。游标是"比它更早"，所以往回翻靠手上记的游标栈。 */
async function loadAgentRunsPage(page = 1) {
  const state = ui.agent;
  if (state.loading) return;
  const wanted = Math.max(1, Math.floor(num(page, 1)) || 1);
  const before = num((state.cursors || [])[wanted - 1], 0);
  const select = $("status-session");
  const sessionId = select ? select.value : "";
  const params = { limit: AGENT_RUNS_PAGE };
  // 不传 session = 全部会话（用户 m20719：默认看全部会话最近 50 条）
  if (sessionId) params.session = sessionId;
  if (before) params.before = before;
  if (state.onlyFailed) params.only_failed = 1;
  state.loading = true;
  renderAgentRuns();
  try {
    const data = await apiGet("agent/runs", params);
    if (!data || data.ok === false) {
      state.note = String((data && data.reason) || "读不到工具调用记录");
      return;
    }
    const rows = Array.isArray(data.items) ? data.items : [];
    state.items = rows;
    state.cursor = num(data.next_cursor, 0);
    state.hasMore = data.has_more === true;
    state.note = rows.length ? "" : "她还没有调用过工具。";
    state.page = wanted;
    state.cursors[wanted - 1] = before;
    if (state.hasMore) state.cursors[wanted] = state.cursor;
    state.cursors.length = state.hasMore ? wanted + 1 : wanted;
  } catch (error) {
    state.note = `读取工具调用记录失败：${error && error.message ? error.message : error}`;
  } finally {
    state.loading = false;
    renderAgentRuns();
  }
}

/** 回到第一页（换会话、换筛选、点刷新都走这里）。 */
function loadAgentRuns() {
  ui.agent.cursors = [0];
  return loadAgentRunsPage(1);
}

/** Agent 页的刷新：五块事实现取，页签里那一块在看着才跟着重取（用户 m45903）。 */
function refreshAgentPage() {
  loadAgentPage();
  showAgentPane(ui.agent.pane || "work", true);
}

/* ================================================================== */
/* 谈资：她经历的事里能拿去讲给别人听的信息                              */
/* ================================================================== */

/** 列表一页拉多少条。后端硬上限 100，这里默认 20，翻页只认 next_cursor。 */
const TOPIC_PAGE_SIZE = 20;

const TOPIC_SOURCE_LABELS = {
  chat: "聊天",
  event: "事件",
  activity: "经历",
  tool: "工具",
  media: "媒体",
  manual: "手写",
  dream: "梦境",
};

function topicPolicyText(topic) {
  const policy = ((topic.details || {}).visibility || {});
  const state = topic.policy_state || topic.policy_status || topic.authority || policy.authority || "legacy_unknown";
  if (topic.invalid_reason || state === "invalid" || policy.deny_all) return "来源失效 / 不可使用";
  if (state === "legacy_unknown" || !(topic.source_event_ids || []).length) return "旧版 / 来源不明（受限）";
  if (policy.hard_private) return "用户保密硬锁";
  return state === "explicit" ? "明确授权" : "来源规则许可";
}

const TOPIC_STATUS_LABELS = {
  archived: "已归档",
  available: "可用",
  disabled: "已禁用",
  expired: "已过期",
  deleted: "已删除",
};

const TOPIC_ACT_LABELS = {
  introduce: "主动介绍",
  progress: "进展",
  outcome: "结果",
  answer: "回答",
  reference: "引用",
};

function topicLabel(table, raw, fallback) {
  const key = raw === null || raw === undefined ? "" : String(raw);
  if (!key) return fallback;
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : key;
}

/** 状态给 CSS 用：只放行纯小写字母，别把后端字段直接拼进 class。 */
function topicStatusKey(topic) {
  const key = String((topic || {}).status || "available");
  return /^[a-z]+$/.test(key) ? key : "unknown";
}

function topicStamp(seconds) {
  const value = num(seconds, 0);
  return value > 0 ? formatStamp(value) : "";
}

/** 谈资一律挂在全局会话选择器上：换会话就得把上一条会话的数据丢掉。 */
function topicSession() {
  const node = $("status-session");
  return node ? String(node.value || "") : "";
}

function topicVersion(topic) {
  return num((topic || {}).version, 0);
}

function topicForget() {
  ui.topics.session = "";
  ui.topics.items = [];
  ui.topics.total = 0;
  ui.topics.nextCursor = "";
  ui.topics.enabled = null;
  ui.topics.selected = "";
  ui.topics.detail = null;
  ui.topics.shares = [];
  // 召回轨迹也是按会话存的：换了会话就得连它一起丢掉
  ui.topics.recall = { session: "", records: [], note: "", loading: false };
}

/**
 * 版本冲突。bridge 把 HTTP 409 包成什么样没人保证过，
 * 所以状态码、响应体、文案三路都认一遍。
 */
function topicConflict(error) {
  if (!error) return false;
  const response = error.response || {};
  const status =
    num(error.status, 0) || num(error.statusCode, 0) || num(response.status, 0);
  if (status === 409) return true;
  const data = error.data || error.body || response.data || {};
  if (num(data.status, 0) === 409 || data.conflict === true) return true;
  return /409|conflict|冲突/i.test(`${error.message || error} ${error.statusText || ""}`);
}

function topicFailText(error) {
  return error && error.message ? String(error.message) : String(error);
}

function topicConflictText() {
  return "这条谈资在别处被改过，刷新后再试";
}

function topicConflictError() {
  const conflict = new Error(topicConflictText());
  conflict.status = 409;
  return conflict;
}

/**
 * 写操作统一走这里：把 409 归一成一种带 status 的错误，
 * 调用方只需要判断一次。``run`` 里那一行要写字面量的接口名（注释里别写样例，
 * 否则会被 tests/test_web_routes.py 的正则当成真调用的接口去核对后端注册表）。
 */
async function topicSend(run) {
  try {
    const data = await run();
    if (data && data.ok === false && num(data.status, 0) === 409) {
      throw topicConflictError();
    }
    return data || {};
  } catch (error) {
    if (topicConflict(error)) throw topicConflictError();
    throw error;
  }
}

/** 已向哪些会话分享过：后端给什么形状就认什么形状，去重后按顺序返回。 */
function topicSharedSessions(topic) {
  const seen = [];
  const push = (value) => {
    const text = value === null || value === undefined ? "" : String(value);
    if (text && seen.indexOf(text) < 0) seen.push(text);
  };
  const row = topic || {};
  if (Array.isArray(row.share_session_ids)) {
    row.share_session_ids.forEach((item) => push(item));
  }
  if (Array.isArray(row.shared_sessions)) {
    row.shared_sessions.forEach((item) => push(item));
  }
  if (Array.isArray(row.shares)) {
    row.shares.forEach((item) => {
      if (item && typeof item === "object") push(item.session_id);
      else push(item);
    });
  }
  return seen;
}

function topicSharedText(topic) {
  const ids = topicSharedSessions(topic);
  if (ids.length) {
    const head = ids.slice(0, 3).join("、");
    return ids.length > 3 ? `已分享给 ${head} 等 ${ids.length} 个会话` : `已分享给 ${head}`;
  }
  const count = num((topic || {}).share_count, 0);
  return count > 0 ? `已分享给 ${count} 个会话` : "";
}

/** 来源事实：details.facts 可能是字符串、也可能是对象列表。 */
function topicFacts(topic) {
  const details = (topic || {}).details || {};
  const raw = details.facts || details.fact || [];
  const list = Array.isArray(raw) ? raw : [raw];
  return list
    .filter((item) => item !== null && item !== undefined && String(item).trim() !== "")
    .map((item) => (typeof item === "string" ? item : JSON.stringify(item)));
}

function topicFeedback(topic) {
  const row = topic || {};
  const details = row.details || {};
  const raw = row.feedback || details.feedback || details.feedbacks || [];
  const list = Array.isArray(raw) ? raw : [raw];
  return list
    .filter((item) => item !== null && item !== undefined && String(item).trim() !== "")
    .map((item) => (typeof item === "string" ? item : JSON.stringify(item)));
}

function topicStoreRow(row) {
  if (!row || !row.topic_id) return;
  ui.topics.items = (ui.topics.items || []).map((item) =>
    item.topic_id === row.topic_id ? row : item,
  );
  if (ui.topics.selected === row.topic_id) ui.topics.detail = row;
}

/** 功能开关 + 底部那一行小字 + 「加载更多」的显隐。 */
function topicSyncFlag() {
  const enabled = ui.topics.enabled !== false;
  const button = $("material-seg-topics");
  if (button) {
    button.classList.toggle("off", !enabled);
    button.setAttribute("aria-disabled", enabled ? "false" : "true");
    button.setAttribute(
      "title",
      enabled
        ? "她能拿去讲给别人听的信息"
        : "谈资功能关着，去「全局设置 → 说话与决策 → 谈资」里打开",
    );
  }
  const pane = $("topic-pane");
  if (pane) pane.classList.toggle("off", !enabled);
  const note = $("topic-note");
  if (note) {
    if (ui.topics.enabled === null) note.textContent = "";
    else if (!enabled)
      note.textContent = "谈资功能关着：在「全局设置 → 说话与决策 → 谈资」里打开";
    else if (!(ui.topics.items || []).length) note.textContent = "没有谈资";
    else note.textContent = `共 ${ui.topics.total || ui.topics.items.length} 条`;
  }
  const more = $("topic-more");
  if (more) more.classList.toggle("hidden", !ui.topics.nextCursor);
}

/* ---------------------------------------------------------------- */
/* 最近召回：她最近几次回话时系统翻过哪些谈资、哪几条进了上下文           */
/* 只读。没通过授权检查的谈资后端不给摘要，这里也只显示位置与原因。        */
/* ---------------------------------------------------------------- */
/* RECALL-VIEW-BEGIN */

const TOPIC_RECALL_USAGE_LABELS = {
  reference: "被动回复",
  introduce: "主动提起",
};

const TOPIC_RECALL_RANKING_LABELS = {
  semantic_vector: "语义重排",
  lexical_substring: "字面命中",
  neutral: "按时间",
};

/** 没进她上下文的原因（短句，一行放得下；完整说法在悬停提示里）。 */
const TOPIC_RECALL_REASON_LABELS = {
  blocked_by_policy: "没通过授权检查",
  authorized_but_over_limit: "名额用完",
  disabled: "谈资功能关着",
};

/** 一次召回明细里最多铺几行候选（再多就写「还有 N 条」）。 */
const TOPIC_RECALL_ROWS = 3;

function topicRecallUsageText(raw) {
  const key = raw === null || raw === undefined ? "" : String(raw);
  if (!key) return "（未知路径）";
  return Object.prototype.hasOwnProperty.call(TOPIC_RECALL_USAGE_LABELS, key)
    ? TOPIC_RECALL_USAGE_LABELS[key]
    : key;
}

function topicRecallModeText(raw) {
  const key = raw === null || raw === undefined ? "" : String(raw);
  if (!key) return "（未知模式）";
  return Object.prototype.hasOwnProperty.call(TOPIC_RECALL_RANKING_LABELS, key)
    ? TOPIC_RECALL_RANKING_LABELS[key]
    : key;
}

function topicRecallStamp(seconds) {
  return topicStamp(seconds) || "（时间不详）";
}

/** 悬停提示里的完整说法（页面上只印短句，别让每一行都写一长串）。 */
function topicRecallReasonText(row) {
  if (row && row.injected) return "已经进了她这次的上下文";
  const reason = String((row && row.reason) || "");
  if (!reason || reason === "injected") return "没进她这次的上下文";
  if (Object.prototype.hasOwnProperty.call(TOPIC_RECALL_REASON_LABELS, reason)) {
    return `没进她这次的上下文：${TOPIC_RECALL_REASON_LABELS[reason]}`;
  }
  return `没进她这次的上下文（${reason}）`;
}

/** 行内那一格：短到一眼看完（没通过授权检查时**不显示内容**这条纪律不变）。 */
function topicRecallShortReason(row) {
  if (row && row.injected) return "已进入上下文";
  const reason = String((row && row.reason) || "");
  if (!reason || reason === "injected") return "没进上下文";
  if (Object.prototype.hasOwnProperty.call(TOPIC_RECALL_REASON_LABELS, reason)) {
    return TOPIC_RECALL_REASON_LABELS[reason];
  }
  return "没进上下文";
}

/** 内部编号拆成能读懂的部分：谈资 id 与「一次经历」的位置格式不同。 */
function topicRecallIdInfo(raw) {
  const value = String(raw || "");
  const info = { kind: "topic", label: "一条谈资", action: "", session: "" };
  if (!value.startsWith("episode:")) return info;
  const parts = value.slice("episode:".length).split(":");
  if (parts.length > 1 && /^plan\d+\.\d+$/.test(parts[parts.length - 1])) parts.pop();
  info.kind = "episode";
  info.label = "一段经历记录";
  if (parts.length > 1 && /^\d+$/.test(parts[parts.length - 1])) {
    parts.pop();
    info.action = parts.pop() || "";
  }
  info.session = parts.join(":");
  return info;
}

/** 主信息：有摘要就显示摘要；没有摘要只说清这是什么，不自己编内容。 */
function topicRecallHeadline(row) {
  const summary = String((row && row.summary) || "").trim();
  if (summary) return summary;
  return topicRecallIdInfo(row && row.topic_id).kind === "episode"
    ? "一段经历记录（没有摘要）"
    : "一条谈资（没有摘要）";
}

/** 没有摘要时补一句它是哪儿来的：本会话还是别处（不再印会话 id）。 */
function topicRecallScopeText(row) {
  const info = topicRecallIdInfo(row && row.topic_id);
  if (!info.session) return "当前会话";
  return info.session === topicSession() ? "当前会话" : "其它会话";
}

/** 字面命中还是意思相近：一眼看出这条是凭什么被翻出来的。 */
function topicRecallHowText(row) {
  const bits = [];
  if (row && row.lexical) bits.push("字面命中");
  const score = row ? row.semantic : null;
  if (typeof score === "number" && Number.isFinite(score)) {
    bits.push(`意思相近 ${Math.round(score * 100)}%`);
  }
  return bits.length ? bits.join(" · ") : "没记下翻出来的方式";
}

/**
 * 面板只搭一次（谈资页签反复进出，不能每次进来都多长一层）。
 *
 * 版面口径（用户 m20「你自己看下谈资页面 是给人看的吗 重做」）：**这一页的主角是谈资本身**，
 * 所以「最近召回」只剩一行小结 + 一个默认收起的「看每次召回的明细」——以前它铺满整屏，
 * 真正的列表被压到看不见。
 */
function topicRecallBox() {
  const host = $("topic-recall");
  if (!host) return null;
  if (!host.dataset.built) {
    host.dataset.built = "1";
    const section = settingsSection("最近召回", "", "", "topic-recall");
    const bar = el("div", "toolbar");
    const refresh = el("button", "ghost small", "刷新");
    refresh.type = "button";
    refresh.addEventListener("click", () => loadTopicRecall());
    bar.appendChild(refresh);
    const summary = el("span", "muted topic-recall-summary");
    bar.appendChild(summary);
    const body = el("div", "topic-recall");
    const more = el("details", "topic-recall-more");
    more.appendChild(el("summary", "", "看每次召回的明细"));
    more.appendChild(body);
    // 卡片里那一格 .fields 是网格：整块占满一行，别让按钮和记录并排成两列
    const wrap = el("div", "full");
    wrap.appendChild(bar);
    wrap.appendChild(more);
    section._fields.appendChild(wrap);
    host.appendChild(section);
    host._recallSummary = summary;
    host._recallBody = body;
  }
  return host;
}

/** 一行候选：这是什么 / 进没进 / 凭什么翻出来的（长解释与内部编号都收起来）。 */
function topicRecallCandidateLine(row) {
  const line = el("div", "topic-recall-candidate");
  const injected = !!(row && row.injected);
  const authorized = !!(row && row.authorized);
  line.appendChild(el("span", "topic-recall-mark", injected ? "✓" : authorized ? "·" : "⨯"));
  const main = el("div", "topic-recall-main");
  main.appendChild(el("span", "topic-recall-title", topicRecallHeadline(row)));
  const verdict = el("span", "topic-recall-verdict", topicRecallShortReason(row));
  verdict.classList.add(injected ? "on" : "off");
  verdict.setAttribute("title", topicRecallReasonText(row));
  main.appendChild(verdict);
  if (!String((row && row.summary) || "").trim()) {
    main.appendChild(el("span", "muted topic-recall-how", topicRecallScopeText(row)));
  }
  main.appendChild(el("span", "muted topic-recall-how", topicRecallHowText(row)));
  // 内部编号降到次要位置：默认收起，排查时才展开
  const detail = el("details", "topic-recall-id");
  detail.appendChild(el("summary", "", "编号"));
  detail.appendChild(
    el("span", "topic-recall-id-value", String((row && row.topic_id) || "（没有编号）")),
  );
  main.appendChild(detail);
  line.appendChild(main);
  return line;
}

function topicRecallRecordBlock(record) {
  const block = el("div", "topic-recall-record");
  const head = el("div", "topic-recall-head");
  head.appendChild(el("span", "topic-recall-when", topicRecallStamp(record.at)));
  head.appendChild(el("span", "pill", topicRecallUsageText(record.usage)));
  head.appendChild(el("span", "muted", topicRecallModeText(record.ranking)));
  const all = Array.isArray(record.candidates) ? record.candidates : [];
  const picked = all.filter((row) => row && row.injected).length;
  head.appendChild(el("span", "muted", `翻到 ${all.length} 条，进入 ${picked} 条`));
  block.appendChild(head);
  // 她这一轮看到的原话：只有一句，放在明细里
  const text = String(record.query_text || "");
  if (text) block.appendChild(el("p", "muted topic-recall-query", `她看到的原话：${text}`));
  if (!all.length) {
    block.appendChild(el("p", "muted", "这一轮没有候选。"));
    return block;
  }
  const shown = all.slice(0, TOPIC_RECALL_ROWS);
  shown.forEach((row) => block.appendChild(topicRecallCandidateLine(row)));
  if (all.length > shown.length) {
    block.appendChild(
      el("p", "muted topic-recall-rest", `还有 ${all.length - shown.length} 条没显示`),
    );
  }
  return block;
}

function renderTopicRecall() {
  const host = topicRecallBox();
  if (!host) return;
  const body = host._recallBody;
  const summary = host._recallSummary;
  const state = ui.topics.recall;
  const records = state.records || [];
  body.innerHTML = "";
  if (summary) summary.textContent = "";
  if (state.loading) {
    body.appendChild(el("p", "muted", "读取中…"));
    return;
  }
  if (state.note) body.appendChild(el("p", "muted", state.note));
  if (!records.length) {
    if (!state.note) body.appendChild(el("p", "muted", "还没有记录。"));
    return;
  }
  // 一行小结：几轮、翻到几条、几条真进了上下文
  let total = 0;
  let picked = 0;
  records.forEach((record) => {
    const rows = Array.isArray(record.candidates) ? record.candidates : [];
    total += rows.length;
    picked += rows.filter((row) => row && row.injected).length;
  });
  if (summary) {
    summary.textContent = `最近 ${records.length} 轮：翻到 ${total} 条，进入上下文 ${picked} 条`;
  }
  records.forEach((record) => body.appendChild(topicRecallRecordBlock(record)));
}

async function loadTopicRecall() {
  const state = ui.topics.recall;
  const session = topicSession();
  // 同一个会话正在读就别重复发；换了会话必须重来
  if (state.loading && state.session === session) return;
  state.session = session;
  state.records = [];
  state.note = "";
  if (!session) {
    state.note = "还没有会话。";
    renderTopicRecall();
    return;
  }
  state.loading = true;
  renderTopicRecall();
  try {
    const data = await apiGet("topics/recall", { session });
    if (state.session !== session) return; // 读的过程中换了会话
    if (!data || data.ok === false) {
      state.note = String((data && data.reason) || "还没有召回记录");
      return;
    }
    state.records = (Array.isArray(data.records) ? data.records : []).slice(0, 5);
    state.note = state.records.length ? "" : "还没有召回记录。";
  } catch (error) {
    if (state.session !== session) return;
    state.note = `读取最近召回失败：${topicFailText(error)}`;
  } finally {
    if (state.session === session) {
      state.loading = false;
      renderTopicRecall();
    }
  }
}

async function loadTopics({ append = false } = {}) {
  const box = $("topic-list");
  if (!box) return;
  const session = topicSession();
  if (!session) {
    topicForget();
    box.innerHTML = "";
    box.appendChild(el("p", "muted", "还没有会话。"));
    topicSyncFlag();
    return;
  }
  // 换了会话：先丢掉上一个会话的谈资，再拉新的
  if (ui.topics.session && ui.topics.session !== session) topicForget();
  ui.topics.session = session;
  if (append && !ui.topics.nextCursor) return;
  if (ui.topics.loading) return;
  ui.topics.loading = true;
  if (!append) {
    box.innerHTML = "";
    box.appendChild(el("p", "muted", "读取中…"));
  }
  const params = { session, limit: TOPIC_PAGE_SIZE };
  if (ui.topics.status) params.status = ui.topics.status;
  if (ui.topics.source) params.source = ui.topics.source;
  if (append) params.cursor = ui.topics.nextCursor;
  try {
    const data = await apiGet("topics", params);
    if (ui.topics.session !== session) return; // 拉的过程中换了会话
    if (data.error || data.degraded) throw new Error(data.error || "谈资查询降级，请稍后重试");
    ui.topics.enabled = data.enabled !== false;
    ui.topics.total = num(data.total, 0);
    const rows = Array.isArray(data.items) ? data.items : [];
    ui.topics.items = append ? ui.topics.items.concat(rows) : rows;
    ui.topics.nextCursor = String(data.next_cursor || "");
    const stillHere = ui.topics.items.some(
      (item) => item.topic_id === ui.topics.selected,
    );
    if (ui.topics.selected && !stillHere) {
      ui.topics.selected = "";
      ui.topics.detail = null;
      ui.topics.shares = [];
    }
    renderTopicsList();
    renderTopicDetail();
  } catch (error) {
    if (ui.topics.session !== session) return;
    if (append) {
      toast(`加载更多失败：${topicFailText(error)}`);
    } else {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", `读取谈资失败：${topicFailText(error)}`));
    }
  } finally {
    ui.topics.loading = false;
    topicSyncFlag();
  }
}

function renderTopicsList() {
  const box = $("topic-list");
  if (!box) return;
  box.innerHTML = "";
  if (ui.topics.enabled === false) {
    box.appendChild(
      el(
        "p",
        "muted",
        "谈资功能关着：在「全局设置 → 说话与决策 → 谈资」里打开，这里才会出现内容。",
      ),
    );
    return;
  }
  const items = ui.topics.items || [];
  if (!items.length) {
    box.appendChild(
      el(
        "p",
        "muted",
        "还没有谈资。她经历的事和聊天里的新信息都会成为素材，也可以点「新建谈资」自己写一条。",
      ),
    );
    return;
  }
  items.forEach((topic) => box.appendChild(topicListRow(topic)));
}

/**
 * 列表行：**一眼看懂这一条是什么**。
 *
 * 版面口径（m20）：一行摘要 + 一行出处与时间；状态只在"不是正常可用"时出现，
 * 授权口径的长句子从列表里去掉（放详情），会话 id 不印（这一页本来就按会话筛过）。
 */
function topicListRow(topic) {
  const row = el("div", "list-item topic-row");
  if (topic.topic_id === ui.topics.selected) row.classList.add("selected");
  const main = el("div", "list-main");
  const head = el("div", "topic-row-head");
  head.appendChild(el("strong", "", topic.summary || "（还没有表达）"));
  if (topic.pinned) head.appendChild(el("span", "pill topic-pin", "已固定"));
  const state = topic.status || "available";
  if (state !== "available") {
    head.appendChild(
      el(
        "span",
        `pill topic-status topic-status-${topicStatusKey(topic)}`,
        topicLabel(TOPIC_STATUS_LABELS, state, state),
      ),
    );
  }
  // 不可用/受限的谈资必须一眼认出来（能改的那几种，详情里有完整说明）
  const policy = topicPolicyShort(topic);
  if (policy) head.appendChild(el("span", "pill topic-status topic-status-disabled", policy));
  main.appendChild(head);
  const bits = [
    topicLabel(TOPIC_SOURCE_LABELS, topic.source_kind, "未知来源"),
    topicStamp(topic.updated_at) ? topicStamp(topic.updated_at) : "",
    topicSharedText(topic),
  ].filter((item) => Boolean(item));
  main.appendChild(el("div", "meta", bits.join(" · ")));
  row.appendChild(main);
  row.addEventListener("click", () => loadTopicDetail(topic.topic_id));
  return row;
}

/** 授权口径的一句话版本：正常可用就不占地方，受限/失效才标出来。 */
function topicPolicyShort(topic) {
  const state = String((topic && (topic.policy_state || topic.policy_status || topic.authority)) || "");
  if (topic && topic.invalid_reason) return "已失效";
  if (state === "invalid") return "已失效";
  if (topic && (topic.details || {}).visibility && (topic.details || {}).visibility.deny_all) {
    return "不可使用";
  }
  if (state === "legacy_unknown") return "来源不明";
  if (!Array.isArray(topic && topic.source_event_ids) || !(topic.source_event_ids || []).length) {
    return "来源不明";
  }
  const hard = ((topic && topic.details) || {}).visibility || {};
  return hard.hard_private ? "用户保密" : "";
}

async function loadTopicDetail(topicId) {
  const id = String(topicId || "");
  const box = $("topic-detail");
  const session = ui.topics.session || topicSession();
  if (!id || !session) return;
  ui.topics.selected = id;
  renderTopicsList();
  if (box) {
    box.innerHTML = "";
    box.appendChild(el("p", "muted", "读取中…"));
  }
  try {
    const data = await apiGet("topics/item", { session, topic: id });
    if (ui.topics.selected !== id) return;
    ui.topics.detail = data.topic || null;
    ui.topics.shares = Array.isArray(data.shares) ? data.shares : [];
    renderTopicDetail();
  } catch (error) {
    if (ui.topics.selected !== id) return;
    if (box) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", `读取详情失败：${topicFailText(error)}`));
    }
  }
}

/** 只重拉曝光记录：分享是异步发生的，可能刚讲出去。 */
async function topicReloadShares(topicId) {
  const session = ui.topics.session || topicSession();
  if (!session || !topicId) return;
  try {
    const data = await apiGet("topics/shares", { session, topic: topicId });
    if (ui.topics.selected !== topicId) return;
    ui.topics.shares = Array.isArray(data.shares) ? data.shares : [];
    renderTopicDetail();
  } catch (error) {
    toast(`读不到分享记录：${topicFailText(error)}`);
  }
}

function topicSection(parent, heading, action) {
  const head = el("div", "section-head topic-section");
  head.appendChild(el("h3", "", heading));
  if (action) head.appendChild(action);
  parent.appendChild(head);
  return head;
}

function topicLine(parent, label, value) {
  const text = value === null || value === undefined || value === "" ? "—" : String(value);
  const row = el("div", "topic-line");
  row.appendChild(el("span", "topic-line-label", label));
  row.appendChild(el("span", "topic-line-value", text));
  parent.appendChild(row);
  return row;
}

function topicSharesNode(shares) {
  const list = el("div", "list topic-shares");
  if (!shares.length) {
    list.appendChild(el("p", "muted", "还没讲出去过。"));
    return list;
  }
  shares.forEach((share) => {
    const row = el("div", "list-item");
    const main = el("div", "list-main");
    const head = el("div", "topic-row-head");
    head.appendChild(
      el(
        "strong",
        "",
        `${share.session_id || "未知会话"} · ${topicLabel(TOPIC_ACT_LABELS, share.communicative_act, "分享")}`,
      ),
    );
    head.appendChild(el("span", "pill", String(share.status || "未知状态")));
    main.appendChild(head);
    if (share.text) main.appendChild(el("div", "topic-share-text", share.text));
    const bits = [
      topicStamp(share.created_at) ? `创建 ${topicStamp(share.created_at)}` : "",
      topicStamp(share.sent_at) ? `发出 ${topicStamp(share.sent_at)}` : "还没发出",
      `信息版本 ${num(share.information_revision, 0)}`,
      Array.isArray(share.media_ids) && share.media_ids.length
        ? `媒体 ${share.media_ids.join("、")}`
        : "",
      Array.isArray(share.platform_message_ids) && share.platform_message_ids.length
        ? `消息 ${share.platform_message_ids.join("、")}`
        : "",
    ].filter((item) => Boolean(item));
    main.appendChild(el("div", "meta", bits.join(" · ")));
    row.appendChild(main);
    list.appendChild(row);
  });
  return list;
}

function renderTopicDetail() {
  const box = $("topic-detail");
  if (!box) return;
  const title = $("topic-title");
  if (title) title.textContent = "谈资详情";
  box.innerHTML = "";
  const topic = ui.topics.detail;
  if (!topic) {
    box.appendChild(
      el("p", "muted", "左边点一条谈资，这里看它的来源事实、曝光记录和能做的操作。"),
    );
    return;
  }
  if (ui.topics.enabled === false) {
    box.appendChild(
      el("p", "muted", "谈资功能关着：在「全局设置 → 说话与决策 → 谈资」里打开才能改。"),
    );
    return;
  }

  const head = el("div", "topic-head");
  head.appendChild(
    el(
      "span",
      `pill topic-status topic-status-${topicStatusKey(topic)}`,
      topicLabel(TOPIC_STATUS_LABELS, topic.status, "可用"),
    ),
  );
  head.appendChild(
    el("span", "pill", topicLabel(TOPIC_SOURCE_LABELS, topic.source_kind, "未知来源")),
  );
  if (topic.pinned) head.appendChild(el("span", "pill topic-pin", "已固定"));
  if (topic.user_edited) head.appendChild(el("span", "pill", "人工改过"));
  head.appendChild(el("span", "pill", `版本 v${topicVersion(topic)}`));
  box.appendChild(head);

  topicLine(box, "能不能用", topicPolicyText(topic));
  if (topic.invalid_reason) topicLine(box, "为什么不能用", topic.invalid_reason);
  if (topic.archived_at) topicLine(box, "归档时间", topicStamp(topic.archived_at));
  topicLine(box, "信息版本", String(num(topic.information_revision, 0)));
  topicLine(box, "重要度", `${Math.round(num(topic.importance, 0) * 100)}%`);
  topicLine(
    box,
    "时间",
    [topicStamp(topic.created_at) ? `创建 ${topicStamp(topic.created_at)}` : "",
     topicStamp(topic.updated_at) ? `更新 ${topicStamp(topic.updated_at)}` : ""]
      .filter(Boolean)
      .join(" · "),
  );
  topicLine(box, "什么时候过期", topic.expires_at ? topicStamp(topic.expires_at) : "不过期");

  const edit = el("button", "ghost small", "改表达");
  edit.addEventListener("click", () => openTopicEditDialog(topic));
  topicSection(box, "她怎么讲（表达）", edit);
  box.appendChild(el("p", "topic-summary", topic.summary || "（还没有表达）"));
  box.appendChild(
    el("p", "muted topic-note-line", "这里只改她怎么讲；事实要去下面的来源事件里改。"),
  );

  const sharesRefresh = el("button", "ghost small", "刷新分享记录");
  sharesRefresh.addEventListener("click", () => topicReloadShares(topic.topic_id));
  topicSection(box, "来源事实", sharesRefresh);
  const ids = Array.isArray(topic.source_event_ids) ? topic.source_event_ids : [];
  if (!ids.length) {
    box.appendChild(el("p", "muted", "这条谈资没有关联的源事件（是手工写的一条）。"));
  }
  ids.forEach((id) => {
    const link = el("button", "ghost small topic-source", String(id));
    link.addEventListener("click", () => openTopicSourceDialog(id));
    box.appendChild(link);
  });
  const facts = topicFacts(topic);
  if (facts.length) {
    const list = el("ul", "topic-facts");
    facts.forEach((item) => list.appendChild(el("li", "", item)));
    box.appendChild(list);
  }

  topicSection(box, "曝光记录（她什么时候讲给了谁）", null);
  box.appendChild(topicSharesNode(ui.topics.shares || []));

  const feedback = topicFeedback(topic);
  if (feedback.length) {
    topicSection(box, "相关反馈", null);
    const list = el("ul", "topic-facts");
    feedback.forEach((item) => list.appendChild(el("li", "", item)));
    box.appendChild(list);
  }

  topicSection(box, "操作", null);
  box.appendChild(topicActions(topic));
}

function topicActions(topic) {
  const wrap = el("div", "toolbar topic-actions");
  const pin = el("button", "ghost small", topic.pinned ? "取消固定" : "固定");
  pin.addEventListener("click", () =>
    topicPatch(
      topic,
      { pinned: !topic.pinned },
      topic.pinned ? "已经取消固定" : "已经固定",
    ),
  );
  wrap.appendChild(pin);

  const live = topic.status !== "disabled";
  const toggle = el("button", "ghost small", live ? "禁用" : "启用");
  toggle.addEventListener("click", () =>
    topicPatch(
      topic,
      { status: live ? "disabled" : "available" },
      live ? "已经禁用" : "已经启用",
    ),
  );
  wrap.appendChild(toggle);

  if (topic.status === "deleted") {
    const restore = el("button", "ghost small", "恢复");
    restore.addEventListener("click", () =>
      topicPatch(topic, { status: "available" }, "已经恢复"),
    );
    wrap.appendChild(restore);
  }

  const remove = el("button", "danger small", "删除");
  remove.addEventListener("click", () => topicDelete(topic));
  wrap.appendChild(remove);
  return wrap;
}

/** 版本冲突时提示 + 重新拉这条谈资（列表也一起对齐）。 */
async function topicAfterConflict(topicId) {
  toast(topicConflictText());
  await loadTopicDetail(topicId);
  await loadTopics();
}

async function topicPatch(topic, patch, okText) {
  if (!topic || !topic.topic_id) return false;
  const body = {
    session: ui.topics.session || topicSession(),
    topic: topic.topic_id,
    version: topicVersion(topic),
  };
  Object.keys(patch).forEach((key) => {
    body[key] = patch[key];
  });
  try {
    const data = await topicSend(() => apiPost("topics/update", body));
    if (data.topic) topicStoreRow(data.topic);
    toast(okText || "已经保存");
    renderTopicsList();
    renderTopicDetail();
    return true;
  } catch (error) {
    if (topicConflict(error)) {
      await topicAfterConflict(topic.topic_id);
      return false;
    }
    toast(`改不动：${topicFailText(error)}`);
    return false;
  }
}

async function topicDelete(topic) {
  if (!topic || !topic.topic_id) return;
  const yes = await confirmDialog({
    title: "删除这条谈资",
    message:
      "删除只是把这条谈资标成已删除，她以后不会再讲它。" +
      "不会删掉来源聊天、来源事件，也不会撤回已经发出去的消息。",
    confirmText: "删除",
  });
  if (!yes) return;
  const body = {
    session: ui.topics.session || topicSession(),
    topic: topic.topic_id,
    version: topicVersion(topic),
  };
  try {
    await topicSend(() => apiPost("topics/delete", body));
    toast("已经删除（源聊天、源事件和已发出的消息都没动）");
    ui.topics.selected = "";
    ui.topics.detail = null;
    ui.topics.shares = [];
    await loadTopics();
  } catch (error) {
    if (topicConflict(error)) {
      await topicAfterConflict(topic.topic_id);
      return;
    }
    toast(`删不掉：${topicFailText(error)}`);
  }
}

/** 把来源事实（源事件 + 它当时讲出去的样子）单独开一个弹窗看。 */
function openTopicSourceDialog(eventId) {
  const id = String(eventId || "");
  const session = ui.topics.session || topicSession();
  if (!id || !session) return;
  const state = { id, session, body: null };
  openCustomDialog({
    title: "来源事实",
    hint:
      "这是谈资的出处。要纠正事实请在来源事件里改，系统会留下审计记录；" +
      "谈资页面只改她怎么讲。",
    confirmText: "关闭",
    hideCancel: true,
    build: (body, inner) => {
      inner.body = body;
      body.appendChild(el("p", "muted", `读取事件 ${id}…`));
      topicRenderSource(inner);
    },
    onSubmit: () => true,
  });
}

function topicSourceAlive(state) {
  return Boolean(
    state &&
      state.body &&
      state.body.isConnected &&
      dialogState &&
      dialogState.values === state,
  );
}

async function topicRenderSource(state) {
  let data = null;
  try {
    data = await apiGet("experience", { session: state.session, id: state.id });
  } catch (error) {
    if (!topicSourceAlive(state)) return;
    state.body.innerHTML = "";
    state.body.appendChild(el("p", "muted", `读不到这个事件：${topicFailText(error)}`));
    return;
  }
  if (!topicSourceAlive(state)) return;
  const body = state.body;
  body.innerHTML = "";
  const experience = data.experience || {};
  topicLine(body, "事件", experience.id || state.id);
  Object.keys(experience)
    .filter((key) => {
      const value = experience[key];
      return value === null || value === undefined ? false : typeof value !== "object";
    })
    .slice(0, 12)
    .forEach((key) => topicLine(body, key, experience[key]));
  const dump = el("pre", "topic-json", JSON.stringify(experience, null, 2));
  const fold = document.createElement("details");
  fold.appendChild(el("summary", "", "这个事件的全部字段"));
  fold.appendChild(dump);
  body.appendChild(fold);

  topicSection(body, "这条事实当时讲出去的样子", null);
  body.appendChild(
    topicSharesNode(Array.isArray(data.shares) ? data.shares : []),
  );
}

function openTopicCreateDialog() {
  const session = ui.topics.session || topicSession();
  if (!session) {
    toast("先在右上角选一个会话");
    return;
  }
  openFormDialog({
    title: "新建谈资",
    hint: "手工写的谈资没有来源事件；事实仍以事件和聊天为准，这里只定她怎么讲。",
    confirmText: "创建",
    fields: [
      {
        key: "summary",
        label: "她怎么讲",
        type: "textarea",
        rows: 3,
        placeholder: "例如：她学会了用烤箱",
      },
      {
        key: "facts",
        label: "来源事实（一行一条，可选）",
        type: "textarea",
        rows: 3,
        placeholder: "一行写一条",
      },
      {
        key: "importance",
        label: "重要度（0~1）",
        type: "number",
        min: 0,
        max: 1,
        step: 0.1,
        value: "0.5",
      },
      { key: "pinned", label: "固定（优先讲）", type: "checkbox" },
    ],
    onSubmit: async (values) => {
      const summary = String(values.summary || "").trim();
      if (!summary) {
        toast("先写一句她怎么讲");
        return false;
      }
      const facts = String(values.facts || "")
        .split("\n")
        .map((item) => item.trim())
        .filter((item) => Boolean(item));
      const body = {
        session,
        summary,
        details: facts.length ? { facts } : {},
        importance: num(values.importance, 0),
        pinned: Boolean(values.pinned),
      };
      let created = null;
      try {
        const data = await topicSend(() => apiPost("topics/create", body));
        created = data.topic || null;
      } catch (error) {
        toast(`建不了：${topicFailText(error)}`);
        return false;
      }
      toast("已经新建");
      await loadTopics();
      if (created) await loadTopicDetail(created.topic_id);
      return true;
    },
  });
}

function openTopicEditDialog(topic) {
  if (!topic) return;
  openFormDialog({
    title: "改表达",
    hint:
      "只改她怎么讲这句话，事实不动。要纠正事实请回来源事实里改，" +
      "系统会记下审计记录。",
    confirmText: "保存",
    fields: [
      {
        key: "summary",
        label: "她怎么讲",
        type: "textarea",
        rows: 4,
        value: topic.summary || "",
      },
      {
        key: "importance",
        label: "重要度（0~1）",
        type: "number",
        min: 0,
        max: 1,
        step: 0.1,
        value: String(num(topic.importance, 0)),
      },
      { key: "pinned", label: "固定（优先讲）", type: "checkbox", value: Boolean(topic.pinned) },
      { key: "status", label: "启用", type: "checkbox", value: topic.status !== "disabled" },
    ],
    onSubmit: async (values) => {
      const summary = String(values.summary || "").trim();
      if (!summary) {
        toast("表达不能是空的");
        return false;
      }
      const patch = {
        summary,
        importance: num(values.importance, 0),
        pinned: Boolean(values.pinned),
        status: values.status ? "available" : "disabled",
      };
      return topicPatch(topic, patch, "已经保存");
    },
  });
}

function showMaterialPane(name) {
  const paneKey = name === "meme" ? "meme" : "topics";
  ui.materialPane = paneKey;
  const seg = $("material-seg");
  if (seg) {
    seg.querySelectorAll("button[data-seg]").forEach((item) => {
      item.classList.toggle("on", item.dataset.seg === paneKey);
    });
  }
  const topics = $("topic-pane");
  const meme = $("meme-pane");
  if (topics) topics.classList.toggle("hidden", paneKey !== "topics");
  if (meme) meme.classList.toggle("hidden", paneKey !== "meme");
  if (paneKey === "topics") {
    loadTopics();
    loadTopicRecall();
  } else {
    // 这三块（配额 / 发送缩放 / 攒图）只在 init 建过一次，切走再切回就显示旧值：
    // 每次进入都按当前世界配置重画一遍。
    // 设置默认收起，但人已经切到表情包这一栏，多半就是来调的：进来就展开。
    const settingsDetails = $("meme-settings-details");
    if (settingsDetails) settingsDetails.open = true;
    memeMasterField();
    memeMasterBox();
    memeQuotaFields();
    memeScaleFields();
    memeCollectFields();
    loadMemes();
  }
}

function bindTopics() {
  const seg = $("material-seg");
  if (seg) {
    seg.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-seg]");
      if (button) showMaterialPane(button.dataset.seg);
    });
  }
  if ($("topic-refresh")) {
    $("topic-refresh").addEventListener("click", () => loadTopics());
  }
  if ($("topic-add")) {
    $("topic-add").addEventListener("click", openTopicCreateDialog);
  }
  if ($("topic-more")) {
    $("topic-more").addEventListener("click", () => loadTopics({ append: true }));
  }
  if ($("topic-status")) {
    $("topic-status").addEventListener("change", () => {
      ui.topics.status = $("topic-status").value;
      loadTopics();
    });
  }
  if ($("topic-source")) {
    $("topic-source").addEventListener("change", () => {
      ui.topics.source = $("topic-source").value;
      loadTopics();
    });
  }
  topicSyncFlag();
  renderTopicsList();
  renderTopicDetail();
  topicRecallBox();
  renderTopicRecall();
}

/* ================================================================== */
/* MEME-VIEW-BEGIN 表情包：收着的图、还能不能用                        */
/*                                                                     */
/* M1 只管"收藏 / 展示记录 / 可用状态"这一层：这里能看、能改名字与含义、  */
/* 能停用与删除、能清过期的展示记录。提示词与动作在 M2/M3 才接。         */
/* 总开关关着时这一页照常能看与清理，只是聊天里不会出现表情包动作。      */
/* ================================================================== */

/** 一进页面先主动排这么多张的缩略图（再多的等滚到视口附近再补，别一次把首页刷爆）。 */
const MEME_THUMB_LIMIT = 36;

/** 列表一次铺多少张卡片（用户 m18846：一百多张一起渲染太卡，先 30 张 + 「加载更多」）。 */
const MEME_PAGE_SIZE = 30;

/** 缩略图同时发几个请求：串行太慢（60 张要等半天），一次全发又会把宿主打满。 */
const MEME_THUMB_CONCURRENCY = 4;

/** 滚动补取的提前量：图位离视口还有这么远就开始取，滚到底时图已经在路上了。 */
const MEME_THUMB_MARGIN = "400px 0px";

/** 等发出去的缩略图（按 media id 去重）；正在跑的放在 MEME_THUMB_RUNNING 里。 */
const MEME_THUMB_QUEUE = [];
const MEME_THUMB_RUNNING = new Set();

/** 滚动补取用的 observer：重渲染时先 disconnect 再建，别让旧图位一直挂在上面。 */
let memeThumbObserver = null;

const MEME_STATE_LABELS = {
  enabled: "可用",
  disabled: "已禁用",
  deleted: "已删除（文件还在）",
  missing: "文件缺失",
};

/** 收藏编号 ``meme_000123`` 在页面上只显示尾号，方便对照回执里的话。 */
function memeShortId(value) {
  const text = String(value || "");
  const digits = text.replace(/^meme_/, "");
  return digits ? `meme_${digits}` : text;
}

/** 一条收藏的搜索键：名字、含义、什么时候用、编号都算。 */
function memeSearchText(entry) {
  return [
    entry.entry_id,
    entry.name,
    entry.meaning,
    entry.use_when,
    entry.avoid_when,
    entry.personal_note,
  ]
    .map((item) => String(item || ""))
    .join(" ")
    .toLowerCase();
}

/** 按页面上的筛选条件挑出要显示的那些（在本地筛，翻页由后端给）。 */
function memeVisibleItems() {
  const state = (ui.meme.state || "").trim();
  const keyword = (ui.meme.search || "").trim().toLowerCase();
  return (ui.meme.items || []).filter((entry) => {
    if (state && String(entry.state || "") !== state) return false;
    if (keyword && !memeSearchText(entry).includes(keyword)) return false;
    return true;
  });
}

function memeScopeParams(offset) {
  // 表情包一律通用（用户 m20）：范围维度已经不参与判定，页面也不再传它。
  // 一页 30 条（用户 m18846/m18961）：后端按 offset+limit 分页，页面靠「加载更多」翻页。
  // 排序（用户 m21273）：收藏时间 / 使用次数，正序倒序都能切；默认收藏时间倒序。
  const order = ui.meme.order === "used" ? "used" : "collected";
  const desc = ui.meme.desc !== false;
  return {
    limit: MEME_PAGE_SIZE,
    offset: Math.max(0, Number(offset) || 0),
    order,
    desc: desc ? "1" : "0",
  };
}

async function loadMemes() {
  if (ui.meme.loading) return;
  ui.meme.loading = true;
  const box = $("meme-grid");
  if (box) {
    box.innerHTML = "";
    box.appendChild(el("p", "muted", "读取中…"));
  }
  try {
    const data = await apiGet("meme/list", memeScopeParams(0));
    ui.meme.enabled = data.enabled !== false;
    ui.meme.reason = String(data.global_reason || "");
    ui.meme.summary = data.summary || null;
    ui.meme.items = data.entries || [];
    ui.meme.total = Number(data.total) || (ui.meme.items || []).length;
    ui.meme.hasMore = data.has_more === true;
    // 选中集只保留库里还在的 id：删掉的、别的页面删掉的、这次没取回来的，
    // 统统移出去。否则「导出选中（N）」的 N 是虚的，点下去后端过滤后为空。
    memePruneChecked();
    // 列表一变，按钮上的 N 也得跟着变：计数只认「交集」，不能等下次点勾选框才更新。
    memeSyncCheckedButtons();
    if (ui.meme.selected) {
      const hit = ui.meme.items.filter((item) => item.entry_id === ui.meme.selected)[0];
      if (!hit) ui.meme.selected = "";
    }
    renderMemeList();
    renderMemeDetail();
    // 卡片一进来就去取缩略图（用户 m20：不要点了才显示）
    memeLoadThumbnails();
  } catch (error) {
    if (box) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", `读取失败：${error.message || error}`));
    }
    const note = $("meme-note");
    if (note) note.textContent = `读取失败：${error.message || error}`;
  } finally {
    ui.meme.loading = false;
    memeMasterBox();
    memeSummaryBox();
  }
}

/** 卡片的图位：`data-media` 标着它属于哪张图，缩略图到了就地填上去（不整页重绘）。 */
function memeThumbSlot(mediaId) {
  const box = el("div", "meme-thumb-box loading");
  box.dataset.media = String(mediaId || "");
  box.appendChild(el("span", "meme-thumb-label", "读取中…"));
  return box;
}

/** 缩略图拿到之后，把所有指向这张图的图位填上（同一张图可能出现在列表与详情两处）。 */
function memeFillThumb(mediaId, dataUrl, firstFrameOnly) {
  const slots = document.querySelectorAll(`.meme-thumb-box[data-media="${mediaId}"]`);
  Array.prototype.forEach.call(slots, (box) => {
    box.innerHTML = "";
    box.classList.remove("loading", "empty");
    const image = el("img", "meme-thumb-image");
    image.src = dataUrl;
    image.alt = "表情包";
    box.appendChild(image);
    if (firstFrameOnly) box.appendChild(el("span", "meme-thumb-label meme-thumb-badge", "GIF 首帧"));
  });
}

/** 缩略图拿不到（文件不在/格式怪）：把图位改成说明性文字，别一直转圈。 */
function memeMarkThumbMissing(mediaId, reason) {
  const slots = document.querySelectorAll(`.meme-thumb-box[data-media="${mediaId}"]`);
  Array.prototype.forEach.call(slots, (box) => {
    box.innerHTML = "";
    box.classList.remove("loading");
    box.classList.add("empty");
    box.appendChild(el("span", "meme-thumb-label", String(reason || "没有图片文件")));
  });
}

/** 把一张图排进取图队列（已经在排队/在跑/取过/已知取不到的都不重复排）。 */
function memeQueueThumb(mediaId) {
  const key = String(mediaId || "");
  if (!key) return;
  if (ui.meme.previews[key] || ui.meme.missing[key]) return;
  if (MEME_THUMB_RUNNING.has(key) || MEME_THUMB_QUEUE.indexOf(key) >= 0) return;
  MEME_THUMB_QUEUE.push(key);
  memeDrainThumbs();
}

/** 并发池：同时最多跑 MEME_THUMB_CONCURRENCY 个；坏一张不挡整批（失败由 memePreview 记进 missing）。 */
function memeDrainThumbs() {
  while (MEME_THUMB_RUNNING.size < MEME_THUMB_CONCURRENCY && MEME_THUMB_QUEUE.length) {
    const key = MEME_THUMB_QUEUE.shift();
    MEME_THUMB_RUNNING.add(key);
    const done = () => {
      MEME_THUMB_RUNNING.delete(key);
      memeDrainThumbs();
    };
    memePreview(key, { silent: true }).then(done, done);
  }
}

/** 滚动补取：图位进到视口（外 400px）才取，首屏之外的不用一进场就全发出去。
 *
 *  ``renderMemeList`` 每次重渲染都会调这里：旧图位已经不在 DOM 里了，先 disconnect
 *  再建，否则 observer 会一个接一个攒着（也把旧节点一起攒着）。
 */
function memeObserveThumbs() {
  if (memeThumbObserver) {
    memeThumbObserver.disconnect();
    memeThumbObserver = null;
  }
  const box = $("meme-grid");
  if (!box) return;
  const slots = box.querySelectorAll(".meme-thumb-box.loading[data-media]");
  if (!slots.length) return;
  if (typeof IntersectionObserver !== "function") {
    // 没有 IntersectionObserver 的浏览器：退回一次全排，总比图位一直停在"读取中"好
    Array.prototype.forEach.call(slots, (slot) => memeQueueThumb(slot.dataset.media));
    return;
  }
  memeThumbObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const slot = entry.target;
        if (memeThumbObserver) memeThumbObserver.unobserve(slot);
        // 还是"读取中"才排：已经填上图或标成缺失的图位不用再要一次
        if (slot.classList.contains("loading")) memeQueueThumb(slot.dataset.media);
      });
    },
    { root: null, rootMargin: MEME_THUMB_MARGIN },
  );
  Array.prototype.forEach.call(slots, (slot) => memeThumbObserver.observe(slot));
}

/** 先给头几张排队取图（其余的在滚到视口附近时由 memeObserveThumbs 补取）。 */
function memeLoadThumbnails() {
  // 只在"已经铺出来的卡片"里主动取图（用户 m18846：列表只铺 30 张，就别去要 36 张图）；
  // 加载更多之后新卡片仍由 IntersectionObserver 滚到附近才取。
  const shown = Math.max(MEME_PAGE_SIZE, Number(ui.meme.shown) || MEME_PAGE_SIZE);
  const rows = memeVisibleItems().slice(0, Math.min(MEME_THUMB_LIMIT, shown));
  rows.forEach((entry) => memeQueueThumb(memeMediaOf(entry)));
  memeObserveThumbs();
}

async function loadMemeDetail(entryId) {
  if (!entryId) {
    ui.meme.detail = null;
    renderMemeDetail();
    return;
  }
  try {
    const data = await apiGet("meme/detail", { id: entryId });
    ui.meme.detail = data.ok ? data : null;
    if (!data.ok) toast(data.reason || "读不到这条收藏");
  } catch (error) {
    ui.meme.detail = null;
    toast(error.message || "读不到这条收藏");
  }
  renderMemeDetail();
}

/** 拿一张缩略图（取过的留在内存里不重复要；到了就地填进图位，不整页重绘）。 */
async function memePreview(mediaId, { silent = false } = {}) {
  const key = String(mediaId || "");
  if (!key) return null;
  if (Object.prototype.hasOwnProperty.call(ui.meme.previews, key)) {
    return ui.meme.previews[key];
  }
  if (Object.prototype.hasOwnProperty.call(ui.meme.missing, key)) return null;
  if (ui.meme.previews[key] === undefined) {
    ui.meme.previews[key] = null;
    try {
      const data = await apiGet("meme/preview", { media: key });
      if (data.ok && data.data_url) {
        ui.meme.previews[key] = data;
      } else {
        ui.meme.missing[key] = data.reason || "取不到这张图";
      }
    } catch (error) {
      ui.meme.missing[key] = error.message || "取不到这张图";
    }
  }
  const ready = ui.meme.previews[key];
  if (ready && !ui.meme.missing[key]) {
    memeFillThumb(key, ready.data_url, ready.first_frame_only);
  } else if (ui.meme.missing[key]) {
    memeMarkThumbMissing(key, ui.meme.missing[key]);
  }
  return ready;
}

function memeMediaOf(entry) {
  return String((entry && (entry.media_id || entry.asset_media_id)) || "");
}

function memeThumbBox(entry) {
  const mediaId = memeMediaOf(entry);
  if (!mediaId) {
    const wrap = el("div", "meme-thumb-box empty");
    wrap.appendChild(el("span", "meme-thumb-label", "没有图片文件"));
    return wrap;
  }
  const preview = ui.meme.previews[mediaId];
  if (preview && preview.data_url) {
    const image = el("img", "meme-thumb-image");
    image.src = preview.data_url;
    image.alt = entry.name || "表情包";
    const wrap = el("div", "meme-thumb-box");
    wrap.dataset.media = mediaId;
    wrap.appendChild(image);
    if (preview.first_frame_only) {
      wrap.appendChild(el("span", "meme-thumb-label meme-thumb-badge", "GIF 首帧"));
    }
    return wrap;
  }
  const missing = ui.meme.missing[mediaId];
  if (missing) {
    const wrap = el("div", "meme-thumb-box empty");
    wrap.dataset.media = mediaId;
    wrap.appendChild(el("span", "meme-thumb-label", String(missing)));
    wrap.setAttribute("title", String(missing));
    return wrap;
  }
  // 还没取到：先占位（缩略图一到就填进去），点一下也能立刻取这张
  // 注意：这里不能拦事件冒泡——点一张还没出图的卡，应当同时取图并打开抽屉
  const wrap = memeThumbSlot(mediaId);
  wrap.addEventListener("click", () => {
    memePreview(mediaId);
  });
  return wrap;
}

function memeStateChip(entry) {
  const state = String(entry.state || "");
  const chip = el("span", `meme-chip meme-chip-${state}`, MEME_STATE_LABELS[state] || state || "未知");
  if (entry.state_reason) chip.setAttribute("title", String(entry.state_reason));
  return chip;
}

/** 卡片上的操作：只改状态与删除，改文案与看细节在右边那一栏。 */
function memeCardActions(entry) {
  const box = el("div", "meme-card-actions");
  const add = (label, handler, hide) => {
    if (hide) return;
    const button = el("button", "ghost small", label);
    button.type = "button";
    button.addEventListener("click", (event) => {
      // 卡片本身是选中并看详情的按钮，卡片上的按钮不能顺带触发它
      event.stopPropagation();
      handler();
    });
    box.appendChild(button);
  };
  const status = String((entry && entry.status) || "");
  add("启用", () => memeSetEnabled(entry, true), status !== "disabled");
  add("停用", () => memeSetEnabled(entry, false), status === "disabled" || status === "deleted");
  // 只有「删除」一个入口：删就连图片文件一起删（用户 2026-10-29 口径）。
  // 「只是不想让她用」点停用，停用不删任何东西。
  add("删除", () => memeDelete(entry), false);
  return box;
}

/** 多选辅助：Shift 连选用的"上一次点过的位置"（只在当前列表内有效）。 */
let memeAnchorIndex = -1;

/** 当前列表在页面上的顺序（筛选后的顺序，不是库里的顺序）。 */
function memeVisibleRows() {
  return Array.from(document.querySelectorAll("#meme-grid .meme-tile"));
}

/** 按 id 同步勾选状态到界面（拖框与全选都走这里，不再逐个点）。 */
function memeApplySelection() {
  const box = $("meme-grid");
  if (!box) return;
  Array.from(box.querySelectorAll(".meme-tile")).forEach((tile) => {
    const id = String(tile.dataset.entryId || "");
    const on = Boolean(id) && ui.meme.checked.has(id);
    tile.classList.toggle("checked", on);
    const input = tile.querySelector("input.meme-tile-check");
    if (input) input.checked = on;
  });
  memeSyncCheckedButtons();
}

/** 全选当前筛选结果 / 清空。 */
function memeSelectAll(select) {
  if (!ui.meme.checked) ui.meme.checked = new Set();
  if (select) {
    memeVisibleItems().forEach((entry) => {
      if (entry.entry_id) ui.meme.checked.add(entry.entry_id);
    });
  } else {
    ui.meme.checked.clear();
  }
  memeApplySelection();
}

/**
 * 拖框多选：在网格上按下左键拖一个框，碰到的卡片都加进选中集。
 *
 * 用户 m21273：「能不能在卡片上也可以拖拽选取 而且拖拽似乎会错乱」。
 * 老实现的三个毛病：①`pointerdown` 只绑在网格上，卡片铺满格子，按在卡片上根本起不来；
 * ②选框用**视口坐标**算、命中却用**文档滚动后的**卡片 rect，滚一下就错位；
 * ③拖到空白处松手才收尾，指针移出网格后 `pointerup` 收不到，选框就留在屏幕上。
 * 改成：文档级 pointer 事件 + 文档坐标算矩形 + rAF 拖边自动滚 + 任何位置松手都收尾。
 */
function memeBindMarquee() {
  const box = $("meme-grid");
  if (!box || box.dataset.marquee === "1") return;
  box.dataset.marquee = "1";

  let start = null; // {x, y} 文档坐标
  let base = null; // 按下时已经选中的集合（Ctrl/Shift 拖框是"追加"）
  let marquee = null;
  let raf = 0;
  let dragging = false;
  let pointer = { x: 0, y: 0 };
  let dragEndedAt = 0;

  const marqueeRect = () => {
    const scrollX = window.scrollX || window.pageXOffset || 0;
    const scrollY = window.scrollY || window.pageYOffset || 0;
    const left = Math.min(start.x, pointer.x) - scrollX;
    const top = Math.min(start.y, pointer.y) - scrollY;
    return {
      left,
      top,
      width: Math.abs(pointer.x - start.x),
      height: Math.abs(pointer.y - start.y),
    };
  };

  const paint = () => {
    if (!marquee || !start) return;
    const rect = marqueeRect();
    marquee.style.left = `${rect.left}px`;
    marquee.style.top = `${rect.top}px`;
    marquee.style.width = `${rect.width}px`;
    marquee.style.height = `${rect.height}px`;
    // 命中：卡片 rect 是视口坐标，选框也算成视口坐标再比（滚动不会错位）
    const hit = new Set(base || []);
    Array.prototype.forEach.call(box.querySelectorAll(".meme-tile"), (tile) => {
      const card = tile.getBoundingClientRect();
      const overlap =
        card.left < rect.left + rect.width &&
        card.right > rect.left &&
        card.top < rect.top + rect.height &&
        card.bottom > rect.top;
      if (overlap && tile.dataset.entryId) hit.add(String(tile.dataset.entryId));
    });
    const before = ui.meme.checked.size;
    let changed = before !== hit.size;
    if (!changed) {
      for (const id of hit) {
        if (!ui.meme.checked.has(id)) {
          changed = true;
          break;
        }
      }
    }
    if (changed) {
      ui.meme.checked = hit;
      memeApplySelection();
    }
  };

  const tick = () => {
    if (!dragging) return;
    // 拖到边缘自动滚：长列表里不用来回松手
    const margin = 64;
    const speed = 18;
    if (pointer.y > window.innerHeight - margin) window.scrollBy(0, speed);
    else if (pointer.y < margin) window.scrollBy(0, -speed);
    paint();
    raf = window.requestAnimationFrame(tick);
  };

  const cleanup = () => {
    if (raf) window.cancelAnimationFrame(raf);
    raf = 0;
    if (marquee && marquee.parentNode) marquee.parentNode.removeChild(marquee);
    marquee = null;
    start = null;
    base = null;
    dragging = false;
    document.body.classList.remove("meme-dragging");
  };

  document.addEventListener(
    "pointerdown",
    (event) => {
      if (event.button !== 0) return;
      if (event.pointerType === "touch") return; // 触屏上拖框和滚动冲突，保持原样
      const grid = event.target.closest ? event.target.closest("#meme-grid") : null;
      if (!grid) return;
      if (event.target.closest("input, button, a, textarea, select, label")) return;
      start = {
        x: event.clientX + (window.scrollX || 0),
        y: event.clientY + (window.scrollY || 0),
      };
      pointer = { x: start.x, y: start.y };
      base = new Set(event.ctrlKey || event.metaKey || event.shiftKey ? ui.meme.checked : []);
      dragging = false;
    },
    true,
  );

  document.addEventListener("pointermove", (event) => {
    if (!start) return;
    if (event.buttons === 0) {
      cleanup();
      return;
    }
    pointer = {
      x: event.clientX + (window.scrollX || 0),
      y: event.clientY + (window.scrollY || 0),
    };
    const moved = Math.hypot(pointer.x - start.x, pointer.y - start.y);
    if (!dragging) {
      // 手抖几像素不算拖：否则点一下卡片就变成"框选"，抽屉打不开
      if (moved < 6) return;
      dragging = true;
      document.body.classList.add("meme-dragging");
      marquee = el("div", "meme-marquee");
      document.body.appendChild(marquee);
      if (window.getSelection) window.getSelection().removeAllRanges();
      raf = window.requestAnimationFrame(tick);
    }
    paint();
  });

  const finish = () => {
    if (!start) return;
    if (dragging) dragEndedAt = Date.now();
    cleanup();
  };
  document.addEventListener("pointerup", finish, true);
  document.addEventListener("pointercancel", finish, true);
  window.addEventListener("blur", finish);

  // 拖图会把图片本身拖走（浏览器默认行为），期间一律拦掉
  box.addEventListener("dragstart", (event) => event.preventDefault());

  box.addEventListener(
    "click",
    (event) => {
      // 刚拖完紧跟一次 click：吞掉它，别顺手打开某张卡的抽屉
      if (Date.now() - dragEndedAt < 320) {
        event.stopPropagation();
        event.preventDefault();
        return;
      }
      const tile = event.target.closest(".meme-tile");
      if (!tile) return;
      if (event.target.closest("input, button, a, label")) return;
      const id = String(tile.dataset.entryId || "");
      if (!id) return;
      if (event.shiftKey) {
        // Shift 连选：从上次点过的那张到这里，中间的一起选上
        const tiles = memeVisibleRows();
        const index = tiles.indexOf(tile);
        if (memeAnchorIndex >= 0 && index >= 0) {
          const [from, to] = [Math.min(index, memeAnchorIndex), Math.max(index, memeAnchorIndex)];
          tiles.slice(from, to + 1).forEach((node) => {
            if (node.dataset.entryId) ui.meme.checked.add(String(node.dataset.entryId));
          });
          memeApplySelection();
          event.stopPropagation();
          event.preventDefault();
          return;
        }
      }
      if (event.ctrlKey || event.metaKey) {
        if (ui.meme.checked.has(id)) ui.meme.checked.delete(id);
        else ui.meme.checked.add(id);
        memeAnchorIndex = memeVisibleRows().indexOf(tile);
        memeApplySelection();
        event.stopPropagation();
        event.preventDefault();
        return;
      }
      memeAnchorIndex = memeVisibleRows().indexOf(tile);
    },
    true,
  );
}

function openMemeDrawer(entryId) {
  if (entryId) ui.meme.selected = entryId;
  const backdrop = $("meme-drawer-backdrop");
  const drawer = $("meme-drawer");
  if (backdrop) backdrop.classList.remove("hidden");
  if (drawer) drawer.classList.remove("hidden");
  // 用户 m21273：「点开详情怎么还要再发一次请求」——列表一页已经带了描述、用途、
  // 状态原因和使用次数，直接用那一行；只有本地没有这条（比如刷新后深链）才去问后端。
  const row = (ui.meme.items || []).filter((item) => item.entry_id === ui.meme.selected)[0];
  if (row) {
    ui.meme.detail = { entry: row, ...memeDetailFromRow(row) };
    renderMemeDetail();
    return;
  }
  if (ui.meme.selected) loadMemeDetail(ui.meme.selected);
}

/** 列表行 → 详情视图（不查库：字段本来就在同一行里）。 */
function memeDetailFromRow(row) {
  return {
    state: row.state,
    state_label: row.state_label,
    state_reason: row.state_reason || "",
    file_present: row.file_present,
    media_id: row.media_id || "",
    pinned: !!row.pinned,
    use_count: Number(row.use_count) || 0,
    asset: {
      mime: row.asset_mime,
      size_bytes: row.asset_size_bytes,
      width: row.asset_width,
      height: row.asset_height,
      frame_count: row.asset_frame_count,
      is_animated: row.asset_is_animated,
    },
  };
}

function closeMemeDrawer() {
  const backdrop = $("meme-drawer-backdrop");
  const drawer = $("meme-drawer");
  if (backdrop) backdrop.classList.add("hidden");
  if (drawer) drawer.classList.add("hidden");
}


/** 选中集的唯一可信读法：与当前列表求交。
 * 直接数 ``checked.size`` 会把已经删掉、或这次没取回来的 id 也算进去，
 * 于是按钮写着「导出选中（2）」，发出去却是一条都不在库里的幽灵 id。
 * 计数、按钮亮不亮、真正发出去的 ids 都从这里取，三者才不会各说各话。
 */
function memeCheckedIds() {
  const live = new Set((ui.meme.items || []).map((item) => entryIdOf(item)));
  return [...(ui.meme.checked || [])].filter((entryId) => live.has(entryId));
}

/** 把不在当前列表里的 id 从选集中摘掉（保留还在的，丢掉没了的）。 */
function memePruneChecked() {
  if (!ui.meme.checked) return;
  const live = new Set((ui.meme.items || []).map((item) => entryIdOf(item)));
  [...ui.meme.checked].forEach((entryId) => {
    if (!live.has(entryId)) ui.meme.checked.delete(entryId);
  });
}

function entryIdOf(item) {
  return String((item && item.entry_id) || "");
}

/** 勾选状态变了：让「删除选中 / 导出选中 / 全部导出」跟着亮或灰。 */
function memeSyncCheckedButtons() {
  const count = memeCheckedIds().length;
  const del = $("meme-delete-checked");
  const exp = $("meme-export-checked");
  if (del) {
    del.disabled = count === 0;
    del.textContent = count ? `删除选中（${count}）` : "删除选中";
    del.title = count ? `删除选中的 ${count} 条（连图片文件一起删）` : "先选几张";
  }
  if (exp) {
    exp.disabled = count === 0;
    exp.textContent = count ? `导出选中（${count}）` : "导出选中";
  }
  // 全部导出跟勾选无关，只看列表里有没有东西：空列表点它只会换来一句失败。
  const all = $("meme-export-all");
  if (all) all.disabled = !(ui.meme.items || []).length;
}

/** 把一份 base64 存成用户的下载文件。 */
/** 导出：选中了几张就只导这几张，``null`` 才是全部。
 *
 * 空数组不再等于「全部」：勾选集与列表求交后为空时（刚把选中的都删了这类），
 * 顺手导出全库会让人以为勾选没生效。那种情况下直接返回，顺手把按钮刷新一遍。
 */
async function memeExport(ids) {
  const picked = Array.isArray(ids) ? ids.filter(Boolean) : [];
  if (Array.isArray(ids) && !picked.length) {
    memeSyncCheckedButtons();
    return;
  }
  const button = picked.length ? $("meme-export-checked") : $("meme-export-all");
  const label = button ? button.textContent : "";
  if (button) button.disabled = true;
  try {
    const params = picked.length ? { ids: picked.join(",") } : {};
    const data = await apiGet("meme/export", params);
    if (!data.ok) {
      // 后端把「选中的没了 / 一条都没收藏 / 文件不在盘」拆成了不同的人话，
      // 这里照抄；只有它什么都没说时才用兜底文案。
      toast(String(data.reason || data.error || data.message || "导出失败"), "warn");
      return;
    }
    // 包直接落在宿主的插件数据目录里（<data>/exports/…）：页面不下载，只报路径。
    const where = String(data.relative || data.filename || "");
    toast(`导出 ${data.count} 张 → ${where}`, "ok");
    if (data.path) console.info("表情包导出：", data.path);
  } catch (error) {
    toast(`导出失败：${error.message || error}`, "warn");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = label || "导出";
    }
    memeSyncCheckedButtons();
  }
}

/** 多选删除（默认只删记录，文件留给资产库清理；与单条删除同一套语义）。 */
async function memeDeleteChecked() {
  const ids = memeCheckedIds();
  if (!ids.length) return;
  const ok = await confirmDialog({
    title: `删除这 ${ids.length} 条收藏？`,
    message:
      "收藏记录与图片文件都会一起删掉，不能恢复。" +
      "同一张图如果还挂在别的收藏上、或者正被使用，文件会先留着。",
    confirmText: "删除",
    danger: true,
  });
  if (!ok) return;
  try {
    const data = await apiPost("meme/delete", { ids });
    toast(String(data.message || "删好了"), "ok");
    ui.meme.checked.clear();
    ui.meme.selected = "";
    await loadMemes();
  } catch (error) {
    toast(`删除失败：${error.message || error}`, "warn");
  }
}

/** 导入压缩包（同名冲突默认改名，另一台机器导出的包可直接吃）。 */
async function memeImportPack(file) {
  if (!file) return;
  // 几十 MB 的包过一遍 base64 页面桥并不可靠（用户 m07774：导出那么干就失败过）：
  // 超过 1.5MB 就让他把包放进 data/exports/，从下拉框里导。
  if (Number(file.size || 0) > 1.5 * 1024 * 1024) {
    toast("这个包太大，页面传不过去：把它放进插件数据目录的 exports/ 里，再用「导入压缩包」里的下拉框导入", "warn");
    return;
  }
  try {
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error || new Error("读不出文件"));
      reader.readAsDataURL(file);
    });
    const data = await apiPost("meme/import-pack", {
      data: dataUrl,
      on_conflict: "rename",
    });
    if (!data) {
      toast("后端没有响应这条导入接口：插件可能还没重载，重启 AstrBot 再试", "warn");
      return;
    }
    if (!data.ok) {
      toast(String(data.reason || "这个压缩包用不了"), "warn");
      return;
    }
    toast(String(data.message || "导入完成"), "ok");
    await loadMemes();
  } catch (error) {
    toast(`导入失败：${error.message || error}`, "warn");
  }
}

/** 「导入压缩包」：先列 data/exports 里现成的包（大包不过页面桥），也能转去选本机文件。 */
async function memeTogglePackBar() {
  const bar = $("meme-pack-bar");
  const select = $("meme-pack-select");
  const note = $("meme-pack-note");
  if (!bar || !select) return;
  if (!bar.classList.contains("hidden")) {
    bar.classList.add("hidden");
    return;
  }
  if (note) note.textContent = "正在看导出目录…";
  try {
    const data = await apiGet("meme/export/packs");
    if (!data) {
      if (note) note.textContent = "";
      toast("后端没有响应导出目录列表：插件可能还没重载，重启 AstrBot 再试", "warn");
      return;
    }
    const packs = data.packs || [];
    select.innerHTML = "";
    if (!packs.length) {
      select.appendChild(el("option", "", "（导出目录里还没有压缩包）"));
      select.value = "";
      if (note) note.textContent = `把压缩包放进 data/${(data && data.relative) || "exports"}/，刷新这里就能选`;
    } else {
      packs.forEach((pack) => {
        const size = Number(pack.size || 0);
        const text = `${pack.name}（${(size / 1024 / 1024).toFixed(1)} MB）`;
        const node = el("option", "", text);
        node.value = pack.name;
        select.appendChild(node);
      });
      if (note) note.textContent = `共 ${packs.length} 个，按时间新的在前`;
    }
    bar.classList.remove("hidden");
  } catch (error) {
    if (note) note.textContent = "";
    toast(`读不出导出目录：${error.message || error}`, "warn");
  }
}

/** 从 data/exports 里导一个包：只传文件名，字节由服务端自己读。 */
async function memeImportPackFromDir() {
  const select = $("meme-pack-select");
  const note = $("meme-pack-note");
  const name = select ? String(select.value || "") : "";
  if (!name) {
    toast("先选一个压缩包", "warn");
    return;
  }
  if (note) note.textContent = "导入中…";
  try {
    const data = await apiPost("meme/import-pack/file", { name, on_conflict: "rename" });
    if (!data) {
      if (note) note.textContent = "";
      toast("后端没有响应这条导入接口：插件可能还没重载，重启 AstrBot 再试", "warn");
      return;
    }
    if (!data.ok) {
      if (note) note.textContent = "";
      toast(String(data.reason || "这个压缩包用不了"), "warn");
      return;
    }
    if (note) note.textContent = "";
    toast(String(data.message || "导入完成"), "ok");
    await loadMemes();
  } catch (error) {
    if (note) note.textContent = "";
    toast(`导入失败：${error.message || error}`, "warn");
  }
}

/** 「加载更多」：先多铺 30 张；本地不够了就跟后端要下一页（用户 m18846/m18961）。 */
async function memeLoadMore() {
  ui.meme.shown = (Number(ui.meme.shown) || MEME_PAGE_SIZE) + MEME_PAGE_SIZE;
  const loaded = (ui.meme.items || []).length;
  const needMore = ui.meme.hasMore && loaded < (Number(ui.meme.total) || 0);
  if (needMore && memeVisibleItems().length < ui.meme.shown) {
    try {
      const data = await apiGet("meme/list", memeScopeParams(loaded));
      const page = data.entries || [];
      if (page.length) ui.meme.items = (ui.meme.items || []).concat(page);
      ui.meme.hasMore = data.has_more === true;
      if (Number(data.total)) ui.meme.total = Number(data.total);
    } catch (error) {
      toast(`读下一页失败：${error.message || error}`);
    }
  }
  renderMemeList();
  memeLoadThumbnails();
  const last = document.querySelector("#meme-grid .meme-more");
  if (last && last.scrollIntoView) last.scrollIntoView({ block: "nearest" });
}

function renderMemeList() {
  const box = $("meme-grid");
  const note = $("meme-note");
  if (!box) return;
  box.innerHTML = "";
  const all = ui.meme.items || [];
  const rows = memeVisibleItems();
  if (!all.length) {
    box.appendChild(
      el(
        "p",
        "muted",
        "还没有收藏。等她在群里决定收下第一张表情，这里就会出现；" +
          "总开关关着时只会记录，不会在聊天里发图。",
      ),
    );
  } else if (!rows.length) {
    box.appendChild(el("p", "muted", "按当前筛选条件没有符合条件的收藏。"));
  }
  // 只铺前 N 张（用户 m18846）：剩下的靠「加载更多」，别一次把一百多张卡片塞进 DOM
  const shown = Math.max(
    MEME_PAGE_SIZE,
    Math.min(Number(ui.meme.shown) || MEME_PAGE_SIZE, rows.length)
  );
  const pageRows = rows.slice(0, shown);
  pageRows.forEach((entry) => {
    const state = String(entry.state || "");
    const name = entry.name || "还没有起名字";
    const tile = el("div", "meme-tile");
    tile.dataset.entryId = String(entry.entry_id || "");
    tile.classList.toggle("selected", entry.entry_id === ui.meme.selected);
    tile.classList.toggle("checked", ui.meme.checked.has(entry.entry_id));
    // 多选：点勾选框不打开详情（stopPropagation），选中态独立于"正在看哪条"
    const check = el("input", "meme-tile-check");
    check.type = "checkbox";
    check.checked = ui.meme.checked.has(entry.entry_id);
    check.title = "选中这一张";
    check.addEventListener("click", (event) => event.stopPropagation());
    check.addEventListener("change", () => {
      if (check.checked) ui.meme.checked.add(entry.entry_id);
      else ui.meme.checked.delete(entry.entry_id);
      tile.classList.toggle("checked", check.checked);
      memeSyncCheckedButtons();
    });
    tile.appendChild(check);
    tile.classList.toggle("off", state !== "enabled");
    tile.setAttribute("role", "button");
    tile.setAttribute("tabindex", "0");
    tile.setAttribute("aria-label", `查看 ${name} 的收藏详情`);
    tile.title = [name, MEME_STATE_LABELS[state] || state || "未知", String(entry.meaning || "").trim()]
      .filter(Boolean)
      .join(" · ");

    const media = el("div", "meme-tile-media");
    media.appendChild(memeThumbBox(entry));
    const overlay = el("div", "meme-tile-overlay");
    overlay.appendChild(el("strong", "meme-tile-name", name));
    const used = Number(entry.use_count) || 0;
    const meta = [
      MEME_STATE_LABELS[state] || state || "未知",
      entry.is_animated || entry.asset_is_animated ? "动图" : "",
      memePixelText(entry),
      used > 0 ? `已用 ${used} 次` : "还没用过",
    ].filter(Boolean);
    overlay.appendChild(el("span", "meme-tile-meta", meta.join(" · ")));
    const meaning = String(entry.meaning || "").trim();
    if (meaning) overlay.appendChild(el("span", "meme-tile-meaning", meaning));
    if (entry.state_reason) overlay.title = String(entry.state_reason);
    media.appendChild(overlay);
    media.appendChild(
      el("span", `meme-tile-state ${state}`, MEME_STATE_LABELS[state] || state || "未知"),
    );
    tile.appendChild(media);

    const open = () => {
      ui.meme.selected = entry.entry_id;
      renderMemeList();
      openMemeDrawer(entry.entry_id);
    };
    tile.addEventListener("click", open);
    tile.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
    box.appendChild(tile);
  });
  // 「加载更多」：一次多铺 MEME_PAGE_SIZE 张；本地翻完了就跟后端要下一页（用户 m18846/m18961）
  const rest = rows.length - pageRows.length;
  const totalHint = Number(ui.meme.total) || rows.length;
  const backendRest = Math.max(0, totalHint - all.length);
  if (rest > 0 || ui.meme.hasMore) {
    const more = el("button", "ghost meme-more");
    more.type = "button";
    const remain = Math.max(rest, backendRest);
    more.textContent = remain > 0 ? `加载更多（还有 ${remain} 张）` : "加载更多";
    more.addEventListener("click", () => {
      memeLoadMore();
    });
    box.appendChild(more);
  }
  // 重新渲染后旧图位已经不在了：observer 在这里重挂，滚到附近才补取缩略图
  memeObserveThumbs();
  memeBindMarquee();
  memeSyncCheckedButtons();
  if (note) {
    const total = Number(ui.meme.total) || (ui.meme.summary && ui.meme.summary.count) || all.length;
    const parts = [];
    if (rows.length !== all.length) parts.push(`筛出 ${rows.length} 条`);
    // 铺不满（还有没显示的）就把「已显示多少」写出来，别让人以为收藏少了
    if (pageRows.length < rows.length) {
      parts.push(`已显示 ${pageRows.length} 条，点下面「加载更多」看剩下的`);
    }
    if (total > all.length) {
      parts.push(`这次只读了最近 ${all.length} 条，共 ${total} 条收藏`);
    } else {
      parts.push(`共 ${total} 条收藏`);
    }
    note.textContent = parts.join("，");
    note.classList.toggle("meme-note-capped", total > all.length);
  }
}

function memeLine(label, value) {
  const row = el("p", "meme-line");
  row.appendChild(el("span", "meme-line-label", label));
  row.appendChild(el("span", "", String(value === undefined || value === null || value === "" ? "—" : value)));
  return row;
}

function renderMemeDetail() {
  const box = $("meme-detail");
  const title = $("meme-detail-title");
  if (!box) return;
  box.innerHTML = "";
  const entry = (ui.meme.detail && ui.meme.detail.entry) || null;
  if (!entry) {
    if (title) title.textContent = "收藏详情";
    box.appendChild(el("p", "muted", "从左侧选择一条收藏，这里显示它的可用状态与原因。"));
    return;
  }
  if (title) title.textContent = entry.name || memeShortId(entry.entry_id);
  const view = ui.meme.detail;
  box.appendChild(memeThumbBox(entry));
  box.appendChild(memeStateChip(entry));
  if (view.state_reason) box.appendChild(el("p", "muted", view.state_reason));
  box.appendChild(el("p", "muted", view.file_present ? "底层图片文件在本机。" : "底层图片文件不在本机。"));

  const fields = el("div", "meme-fields");
  fields.appendChild(
    inputField("名字", entry.name || "", (value) => memePatch(entry, { name: value })),
  );
  fields.appendChild(
    textareaField("含义", entry.meaning || "", (value) => memePatch(entry, { meaning: value })),
  );
  fields.appendChild(
    textareaField("什么时候用", entry.use_when || "", (value) => memePatch(entry, { use_when: value })),
  );
  fields.appendChild(
    textareaField("什么时候不要用", entry.avoid_when || "", (value) => memePatch(entry, { avoid_when: value })),
  );
  fields.appendChild(
    textareaField("备注", entry.personal_note || "", (value) => memePatch(entry, { personal_note: value })),
  );
  // 范围那一栏已经拆掉：表情包一律通用（用户 m20），没有再让用户填会话的理由
  box.appendChild(fields);

  const actions = el("div", "topic-actions");
  const enableButton = el("button", "ghost small", "启用");
  enableButton.type = "button";
  if (entry.status !== "disabled") enableButton.classList.add("hidden");
  enableButton.addEventListener("click", () => memeSetEnabled(entry, true));
  const disableButton = el("button", "ghost small", "停用");
  disableButton.type = "button";
  if (entry.status === "disabled" || entry.status === "deleted") disableButton.classList.add("hidden");
  disableButton.addEventListener("click", () => memeSetEnabled(entry, false));
  const forgetButton = el("button", "ghost small", "删掉收藏记录");
  forgetButton.type = "button";
  forgetButton.addEventListener("click", () => memeDelete(entry, false));
  const purgeButton = el("button", "ghost small", "彻底清掉这条记录");
  purgeButton.type = "button";
  purgeButton.addEventListener("click", () => memeDelete(entry, true));
  actions.appendChild(enableButton);
  actions.appendChild(disableButton);
  actions.appendChild(forgetButton);
  actions.appendChild(purgeButton);
  box.appendChild(actions);
  box.appendChild(
    el(
      "p",
      "muted",
      "停用与删除都只改收藏记录，底层图片文件留在资产库里（需要释放空间时用「媒体」一节的清理）。",
    ),
  );

  const facts = el("div", "topic-section");
  facts.appendChild(el("div", "pane-label", "这条收藏的明细"));
  facts.appendChild(memeLine("编号", memeShortId(entry.entry_id)));
  facts.appendChild(memeLine("图片身份", entry.media_id || "—"));
  facts.appendChild(memeLine("类型", entry.asset_mime || "—"));
  facts.appendChild(memeLine("大小", entry.asset_size_bytes ? formatBytes(entry.asset_size_bytes) : "—"));
  facts.appendChild(memeLine("尺寸", entry.asset_width ? `${entry.asset_width}×${entry.asset_height}` : "—"));
  facts.appendChild(memeLine("帧数", entry.asset_frame_count || "—"));
  facts.appendChild(memeLine("修改次数", entry.revision || 1));
  facts.appendChild(memeLine("收藏时间", memeTimeText(entry.created_at)));
  const sources = (view.sources || []).map((item) => {
    const where = item.session_id || "未知会话";
    const who = item.sender_id || item.origin_kind || "";
    return `${where}${who ? ` / ${who}` : ""}`;
  });
  facts.appendChild(memeLine("来源", sources.length ? sources.join("；") : "—"));
  const variants = (view.variants || []).map((item) => {
    const other = item.entry_a === entry.entry_id ? item.entry_b : item.entry_a;
    const relation = item.relation === "confirmed" ? "确认是变体" : "疑似是变体";
    return `${relation}：${memeShortId(other)}`;
  });
  facts.appendChild(memeLine("相似条目", variants.length ? variants.join("；") : "—"));
  const usage = (view.usage || []).map((item) => {
    const kind = MEME_USAGE_LABELS[item.kind] || item.kind;
    return `${kind}${item.reason ? `（${item.reason}）` : ""}`;
  });
  facts.appendChild(memeLine("最近使用记录", usage.length ? usage.slice(0, 5).join("；") : "—"));
  box.appendChild(facts);
}

const MEME_USAGE_LABELS = {
  attempted: "尝试使用",
  sent: "已发出",
  receipt: "平台回执",
  accepted: "平台已接收",
  rejected: "发送被拒",
  failed: "发送失败",
};

function memeTimeText(value) {
  const seconds = Number(value || 0);
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (number) => String(number).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** 改一条收藏的语义（带上 revision，撞上别人改过就提示刷新）。 */
async function memePatch(entry, fields) {
  const body = {
    id: entry.entry_id,
    revision: entry.revision || 0,
    fields,
  };
  try {
    const data = await apiPost("meme/edit", body);
    if (!data.ok) {
      toast(data.reason || "改不动这条收藏");
      await loadMemeDetail(entry.entry_id);
      return;
    }
    toast("已经保存");
    await loadMemes();
    await loadMemeDetail(entry.entry_id);
  } catch (error) {
    toast(error.message || "改不动这条收藏");
  }
}

async function memeSetEnabled(entry, enabled) {
  try {
    const data = await apiPost("meme/toggle", { id: entry.entry_id, enabled });
    if (!data.ok) {
      toast(data.reason || "改不动这条收藏");
      return;
    }
    toast(enabled ? "已经启用" : "已经停用（聊天里不会再发这一张）");
    await loadMemes();
    await loadMemeDetail(entry.entry_id);
  } catch (error) {
    toast(error.message || "改不动这条收藏");
  }
}

async function memeDelete(entry) {
  // 裸 confirm 在插件的 sandbox iframe 里会被拦掉（没有 allow-modals），
  // 点了像"没反应"；统一走 confirmDialog。
  const ok = await confirmDialog({
    title: "删除这条收藏？",
    message:
      `删除 ${memeShortId(entry.entry_id)}：收藏记录、来源与用法记录一起删掉，` +
      "图片文件也会从本机删掉，不能恢复。「只是不想让她用」请点停用。",
    confirmText: "删除",
    danger: true,
  });
  if (!ok) {
    return;
  }
  const body = { id: entry.entry_id };
  try {
    const data = await apiPost("meme/delete", body);
    if (!data.ok) {
      toast(data.reason || "删不掉这条收藏");
      return;
    }
    toast(data.note || "已经删掉");
    // 删掉的这条必须同时离开多选集合。留着它，「导出选中（2）」会一直显示 2，
    // 点下去后端按 id 过滤后一条都不剩——真机就是这么报「图都不在本机了」的。
    if (ui.meme.checked) ui.meme.checked.delete(entry.entry_id);
    ui.meme.selected = "";
    ui.meme.detail = null;
    closeMemeDrawer();
    await loadMemes();
    renderMemeDetail();
  } catch (error) {
    toast(error.message || "删不掉这条收藏");
  }
}

/** 顶部总开关：读写的是世界配置，跟别的设置一样"保存后生效"。 */
function memeMasterField() {
  const box = $("meme-master-field");
  if (!box) return;
  box.innerHTML = "";
  const world = (ui.config && ui.config.world) || {};
  const meme = world.meme || {};
  const on = meme.enabled !== false;
  const field = checkboxField(
    "打开表情包动作",
    on,
    (value) => {
      if (!ui.config.world) ui.config.world = {};
      if (!ui.config.world.meme) ui.config.world.meme = {};
      ui.config.world.meme.enabled = value;
      ui.settingsTab = "world";  // 表情包那一节在「世界与事件」里
      markDirty();
      memeMasterBox();
      memeSummaryBox();
    },
    { hint: "关掉之后聊天里不会出现表情包动作；已收的图与记录都不会被删除。" },
  );
  box.appendChild(field);
  // 这里以前每次渲染都往「收图与评审」里追加一个「自动收图」开关（绑的字段前端
  // 根本不读），切几次标签就叠出两三个一模一样的开关（用户 2026-10-29 的截图）。
  // 那个字段与这段追加都删了：收图只有「攒进池子 → 拼图评审 → 通过才收藏」一条路。
}

function memeMasterBox() {
  const badge = $("meme-state-badge");
  const note = $("meme-master-note");
  const hint = $("meme-master-hint");
  const world = (ui.config && ui.config.world) || {};
  const meme = world.meme || {};
  const on = meme.enabled !== false;
  const live = ui.meme.enabled;
  if (badge) {
    badge.textContent = on ? "开着" : "关着";
    badge.classList.toggle("off", !on);
  }
  if (note) {
    if (!on) {
      note.textContent =
        "表情包总开关关着，聊天里不会出现表情包动作。这一页照常能看、能改、能清理，" +
        "已收的图与记录都不会被删除。";
    } else if (live === false) {
      note.textContent = "配置里已经打开；保存并生效之后聊天里才会有表情包动作。";
    } else {
      note.textContent = ui.meme.reason || "表情包总开关开着，表情包动作可用。";
    }
  }
  if (hint) {
    hint.textContent = "当前生效状态：" + (live === false ? "关着" : "开着") + "。";
  }
}

/**
 * 「发图额度」：三档上限直接在这页改（用户口径：能点的就别去手改 world.json）。
 *
 * 这里只放**发图相关**的三档——其余二十来个媒体预算属于"调参"，留在世界配置里；
 * 少一个输入框就少一次误改。写完 markDirty，跟别的设置一样"保存后生效"。
 */
function memeQuotaFields() {
  const box = $("meme-quota-fields");
  if (!box) return;
  box.innerHTML = "";
  const world = (ui.config && ui.config.world) || {};
  const meme = world.meme || {};
  // 世界配置里**没有**这个键时不能显示 0：那样看起来像"一张都不发"，而实际生效值
  // 是后端的默认（1/20/100）。所以回退顺序是：配置里的值 → 后端报的生效值 → 内置默认。
  const effective = ((ui.meme.summary || {}).budget) || {};
  const fallback = { max_send_per_reply: 1, send_meme_per_hour: 20, max_send_per_day: 100 };
  const shown = (key) => {
    const configured = meme[key];
    if (configured !== undefined && configured !== null && configured !== "") {
      return Math.round(num(configured, fallback[key]));
    }
    const live = effective[key];
    return Math.round(num(live === undefined || live === null ? fallback[key] : live, fallback[key]));
  };
  const write = (key, value) => {
    if (!ui.config.world) ui.config.world = {};
    if (!ui.config.world.meme) ui.config.world.meme = {};
    ui.config.world.meme[key] = value;
    ui.settingsTab = "world";  // 表情包那一节在「世界与事件」里
    markDirty();
    memeSummaryBox();
  };
  const limits = [
    ["max_send_per_reply", "一次回复最多发几张", "0 = 这一轮不发；实际上限还会被「一轮最多附几张图」压住。", 0, 3],
    ["send_meme_per_hour", "每小时最多发几张", "跨会话一起算：一小时内所有群发出的表情加起来不超过它。", 0, 200],
    ["max_send_per_day", "每天最多发几张", "0 = 不发；跨会话一起算，第二天零点重新计。", 0, 1000],
  ];
  limits.forEach(([key, label, hint, low, high]) => {
    box.appendChild(
      // 数字输入用 inputField 的 number 形态（编辑器里没有单独的 numberField）
      inputField(
        label,
        String(shown(key)),
        (value) => write(key, Math.max(low, Math.min(high, Math.round(num(value, 0))))),
        { type: "number", min: low, max: high, step: 1, hint },
      ),
    );
  });
}

/**
 * 发送前缩图：长边 320、动图保真（用户要求“发出去省流量，但动图不能变静态”）。
 *
 * 三个控件都读写 `ui.config.world.meme_send_scale`（后端 `MemeSendScaleConfig`），
 * 走的是和小节里其它控件同一条 `saveAll` 通道，不新增 API。缺键时回退到后端默认，
 * 不能显示成 0——那看起来像"关掉了缩图"。
 */
function memeScaleFields() {
  const box = $("meme-scale-fields");
  if (!box) return;
  box.innerHTML = "";
  const world = (ui.config && ui.config.world) || {};
  const section = world.meme_send_scale || {};
  const fallback = { enabled: true, max_side: 320, animated: "keep", gif: true };
  const shown = (key) => {
    const value = section[key];
    return value === undefined || value === null || value === "" ? fallback[key] : value;
  };
  const write = (key, value) => {
    if (!ui.config.world) ui.config.world = {};
    if (!ui.config.world.meme_send_scale) ui.config.world.meme_send_scale = {};
    ui.config.world.meme_send_scale[key] = value;
    ui.settingsTab = "world"; // 表情包那一节在「世界与事件」里
    markDirty();
    ui.memeScalePending = true; // 保存成功后提示「下一张生效」
  };
  const enabled = !!shown("enabled");
  box.appendChild(
    checkboxField(
      "发表情包前先缩图",
      enabled,
      (value) => {
        write("enabled", !!value);
        memeScaleFields(); // 关掉时下面两项要一起变灰，立刻重画一次
      },
      {
        hint: "只改发出去的那一份：原图和收藏都不动，管理页里看到的还是原图。",
      },
    ),
  );
  const gifField = checkboxField(
    "发出去的表情转成 GIF",
    !!shown("gif"),
    (value) => write("gif", !!value),
    {
      hint: "QQ 里 GIF 的显示尺寸比同尺寸 PNG 更舒服；本身就是动图的不转（会掉帧）。",
    },
  );
  box.appendChild(gifField);
  const gifInput = gifField.querySelector("input");
  if (gifInput) gifInput.disabled = !enabled;
  if (!enabled) gifField.classList.add("field-disabled");
  const maxSideField = inputField(
    "缩图长边（像素）",
    String(Math.round(num(shown("max_side"), fallback.max_side))),
    (value) => write("max_side", Math.max(240, Math.min(1024, Math.round(num(value, 320))))),
    {
      type: "number",
      min: 240,
      max: 1024,
      step: 40,
    },
  );
  // 缩图总开关关着时，长边与动图两个控件没有意义：置灰并禁用，避免改了却没效果
  const maxSideInput = maxSideField.querySelector("input");
  if (maxSideInput) maxSideInput.disabled = !enabled;
  if (!enabled) maxSideField.classList.add("field-disabled");
  box.appendChild(maxSideField);
  box.appendChild(
    el("p", "field-note", "320 与管理页卡片缩略图同尺度；改小更省流量，改大更清楚。"),
  );
  const animatedField = selectField(
    "动图处理方式",
    String(shown("animated") || "keep"),
    [
      ["keep", "保持动图（逐帧缩放到长边 320）"],
      ["first", "转静态（只发第一帧）"],
      ["skip", "原样发送（不缩放）"],
    ],
    (value) => write("animated", value),
    {
      hint: "保持动图是最费 CPU 的一档：超过 120 帧、或算得超过 3 秒，会自动退回原图。",
    },
  );
  const animatedSelect = animatedField.querySelector("select");
  if (animatedSelect) animatedSelect.disabled = !enabled;
  if (!enabled) animatedField.classList.add("field-disabled");
  box.appendChild(animatedField);
  // 小图时 keep 与 skip 结果完全一样，只有 first 会改内容：常驻说明，别只放在 title 里
  box.appendChild(
    el(
      "p",
      "field-note",
      "长边已不超过 320 时，保持动图与原样发送的结果相同；只有转静态会改变内容。",
    ),
  );
}

/** 攒图收藏：她平时把看到过的图按概率攒着，够一批了再拼图评审（用户 2026-10-24）。
 *
 * 八个控件读写 `ui.config.world.meme_collect`（后端 `MemeCollectConfig`），和本节
 * 其它控件走同一条 `saveAll` 通道，不新增 API。缺键回退后端默认，不能显示成 0。
 * `enabled` 关掉 = 整个收图机制关闭（连聊天里的即时 `collect_meme` 也不出现）。
 */
function memeCollectFields() {
  const box = $("meme-collect-fields");
  if (!box) return;
  box.innerHTML = "";
  // 「候选保留几天」跟清理按钮同属「清理与存储」组，单独挂到那个容器
  const cleanupBox = $("meme-cleanup-fields");
  if (cleanupBox) cleanupBox.innerHTML = "";
  const world = (ui.config && ui.config.world) || {};
  const section = world.meme_collect || {};
  const fallback = {
    enabled: true,
    batch_probability: 0.35,
    min_batch: 6,
    max_batch: 18,
    review_interval_hours: 12,
    max_reviews_per_day: 3,
    sheet_size: 9,
    candidate_ttl_days: 7,
  };
  const shown = (key) => {
    const value = section[key];
    return value === undefined || value === null || value === "" ? fallback[key] : value;
  };
  const write = (key, value) => {
    if (!ui.config.world) ui.config.world = {};
    if (!ui.config.world.meme_collect) ui.config.world.meme_collect = {};
    ui.config.world.meme_collect[key] = value;
    ui.settingsTab = "world";
    markDirty();
  };
  box.appendChild(
    checkboxField(
      "开启攒图收藏（她看到的图攒够一批再问要不要收）",
      !!shown("enabled"),
      (value) => write("enabled", !!value),
      {
        hint: "关掉后连聊天里的即时收图也一起停；已收藏的表情包照常能发、能搜。",
      },
    ),
  );
  const numbers = [
    [
      "batch_probability",
      "每张图入池概率",
      0,
      1,
      0.05,
      "0 = 只在聊天里被明确要求时收（不自动攒）；1 = 看到就攒。",
      null,
    ],
    ["min_batch", "攒够几张才评审", 1, 18, 1, "低于这个数不动，免得为一张图单独调模型。", "round"],
    ["max_batch", "一次评审最多几张", 1, 18, 1, "超过就截断，分到下一次。", "round"],
    [
      "review_interval_hours",
      "两次评审的间隔（小时）",
      1,
      72,
      1,
      "两次之间不再打扰她；攒着的图不会过期（见下面的保留天数）。",
      "round",
    ],
    [
      "max_reviews_per_day",
      "一天最多评审几次",
      0,
      12,
      1,
      "0 = 不自动评（仍可手动触发）；跨会话一起算。",
      "round",
    ],
    ["sheet_size", "每张拼图放几格", 1, 9, 1, "默认 3×3=9；两张拼图最多覆盖 18 张。", "round"],
    [
      "candidate_ttl_days",
      "候选保留几天",
      1,
      60,
      1,
      "攒着一直没轮到的候选到期清掉，不再出现在拼图里。",
      "round",
    ],
  ];
  numbers.forEach(([key, label, low, high, step, hint, kind]) => {
    const scale = key === "batch_probability" ? 100 : 1;
    const current = num(shown(key), fallback[key]);
    const target = key === "candidate_ttl_days" && cleanupBox ? cleanupBox : box;
    target.appendChild(
      inputField(
        label,
        String(kind === "round" ? Math.round(current) : Math.round(current * scale) / scale),
        (value) => {
          const raw = num(value, fallback[key]);
          const next =
            kind === "round" ? Math.round(raw) : Math.round(raw * scale) / scale;
          write(key, Math.max(low, Math.min(high, next)));
        },
        {
          type: "number",
          min: low,
          max: high,
          step,
          hint,
        },
      ),
    );
  });
}

/** 计数、配额与"本轮最多几张"的只读说明。 */
function memeSummaryBox() {
  const box = $("meme-summary");
  if (!box) return;
  box.innerHTML = "";
  const summary = ui.meme.summary;
  if (!summary) return;
  const counts = summary.counts || {};
  // 用前端自己那份短文案：后端那份带「（底层文件还在）」这种长尾，而且还有已经取消的
  // 「范围不可用」那一态（用户 m20 去掉会话限制）
  const labels = MEME_STATE_LABELS;
  const card = el("div", "card");
  card.appendChild(el("h2", "", "收藏总览"));
  const line = el("p", "muted");
  line.textContent = Object.keys(labels)
    .map((key) => `${labels[key]} ${num(counts[key], 0)}`)
    .join("　");
  card.appendChild(line);
  const quota = num(summary.quota_bytes, 0);
  card.appendChild(
    el(
      "p",
      "muted",
      quota
        ? `收藏占用约 ${formatBytes(num(summary.used_bytes, 0))}，上限 ${formatBytes(quota)}。`
        : `收藏占用约 ${formatBytes(num(summary.used_bytes, 0))}。`,
    ),
  );
  const budget = summary.budget || {};
  card.appendChild(
    el(
      "p",
      "muted",
      "单轮上限：表情包补附 " +
        `${num(budget.meme_image_max, 0)} 张、候选预览 ${num(
          budget.max_candidate_images_per_prompt,
          0,
        )} 张、实际发出 ${num(budget.max_send_per_reply, 0)} 张；` +
        `每小时最多发出 ${num(budget.send_meme_per_hour, 0)} 张，` +
        `单次请求最多带 ${num(budget.max_prompt_images, 0)} 张图。`,
    ),
  );
  const exposures = summary.exposures || {};
  card.appendChild(
    el(
      "p",
      "muted",
      "展示记录：" +
        `待补 ${num(exposures.pending, 0)}、认领中 ${num(exposures.claimed, 0)}、` +
        `已展示 ${num(exposures.delivered, 0)}、失败 ${num(exposures.failed, 0)}；` +
        `保留 ${num(budget.exposure_keep_days, 30)} 天（只有"已展示"会封住那次出现）。`,
    ),
  );
  box.appendChild(card);
}

async function memeCleanup() {
  try {
    const plan = await apiPost("meme/cleanup", {});
    if (!plan.ok) {
      toast(plan.reason || "无法计算需要清理的内容");
      return;
    }
    const info = plan.plan || {};
    const ok = await confirmDialog({
      title: "清理过期记录",
      message:
        `将清理超过 ${num(info.exposure_keep_days, 30)} 天的展示记录，` +
          "以及没有被引用的资产元数据。图片文件不受影响。是否继续？",
    });
    if (!ok) {
      return;
    }
    const done = await apiPost("meme/cleanup", { confirm: true });
    if (!done.ok) {
      toast(done.reason || "清理失败");
      return;
    }
    toast(
      `已清理 ${num(done.removed_exposures, 0)} 条展示记录、` +
        `${num(done.removed_assets, 0)} 条没有被引用的资产元数据`,
    );
    await loadMemes();
  } catch (error) {
    toast(error.message || "清理失败");
  }
}

/** 读一个本地文件成 data URL（上传表情包用；浏览器自己的 FileReader，不引依赖）。 */
function memeReadFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("读不到这个文件"));
    reader.readAsDataURL(file);
  });
}

/** 「上传表情包」：点按钮就是打开选文件，选完立刻传——不额外弹窗问名字。 */
function memeOpenUpload() {
  const input = $("meme-upload-file");
  if (!input) return;
  // 同一个文件连着选两次也要能触发 change
  input.value = "";
  input.click();
}

/** 一张一张传（用户可以一次选多张，见 :func:`memeUploadFiles`）。 */
async function memeUploadFile(file) {
  if (!file) return { ok: false, reason: "没有文件" };
  if (!String(file.type || "").startsWith("image/")) {
    return { ok: false, reason: "只能上传图片" };
  }
  try {
    const dataUrl = await memeReadFile(file);
    const comma = dataUrl.indexOf(",");
    const body = {
      data: comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl,
      // 文件名去掉扩展名当默认名字；之后能在右侧详情里改
      name: String(file.name || "").replace(/\.[^.]+$/, ""),
    };
    const result = await apiPost("meme/upload", body);
    if (!result || result.ok === false) {
      return { ok: false, reason: (result && result.reason) || "传不上去" };
    }
    return { ok: true, id: String(result.id || ""), created: result.created !== false };
  } catch (error) {
    return { ok: false, reason: error && error.message ? error.message : "传不上去" };
  }
}

/** 「上传表情包」支持一次选多张：逐个传，最后一句汇总（失败的把原因写清）。 */
async function memeUploadFiles(files) {
  const list = Array.prototype.slice.call(files || []);
  if (!list.length) return;
  const note = $("meme-note");
  let added = 0;
  let existed = 0;
  const problems = [];
  const ids = [];
  for (let index = 0; index < list.length; index += 1) {
    if (note) note.textContent = `上传中…（${index + 1}/${list.length}）`;
    // 一张一张传，所以进度是"已传张数 / 总张数"（用户 m18755 要进度条）
    memeProgress(
      `上传中 ${index}/${list.length}（新增 ${added} 张）`,
      index,
      list.length
    );
    const result = await memeUploadFile(list[index]);
    if (!result.ok) {
      problems.push(`${list[index].name || "这张图"}：${result.reason}`);
      continue;
    }
    if (result.created) added += 1;
    else existed += 1;
    if (result.id) ids.push(result.id);
  }
  if (note) note.textContent = "";
  const parts = [];
  if (added) parts.push(`新增 ${added} 张`);
  if (existed) parts.push(`已有 ${existed} 张（没重复收）`);
  if (problems.length) parts.push(`${problems.length} 张没传上`);
  toast(parts.length ? parts.join("，") : "什么都没传");
  if (problems.length) console.warn("[virtual_world] 上传失败：", problems);
  // 一张没传上的时候把进度条留成红的再收（一眼能看出这次不是全成功）
  if (problems.length) {
    memeProgress(
      `上传完：新增 ${added} 张，${problems.length} 张没传上`,
      list.length,
      list.length,
      { error: true }
    );
  } else {
    memeProgressHide();
  }
  await loadMemes();
  if (ids.length) {
    ui.meme.selected = ids[0];
    renderMemeList();
    await loadMemeDetail(ui.meme.selected);
  }
}

/** 「查一遍本机文件」：把列表里那些图各取一次，取不到的就标成文件缺失。 */
async function memeScanFiles() {
  const rows = ui.meme.items || [];
  const keys = [];
  rows.forEach((entry) => {
    const mediaId = memeMediaOf(entry);
    if (mediaId && keys.indexOf(mediaId) < 0) keys.push(mediaId);
  });
  if (!keys.length) {
    toast("还没有可以检查的图片");
    return;
  }
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(ui.meme.previews, key)) continue;
    await memePreview(key);
  }
  renderMemeList();
  toast(`已检查 ${keys.length} 张图片`);
}

/**
 * 「补齐图片信息」（M5）：把已收图的宽高与帧数补上。
 *
 * 两步走：先预览（一个字节都不写），确认之后再点一次才落盘——避免用户手一抖就把
 * 几百张图重读一遍。探不出来的（坏图/超大/没装 Pillow）如实报数，不编尺寸。
 */
async function memeBackfillFacts() {
  try {
    const dry = await apiPost("meme/backfill", {});
    if (!dry.ok) {
      toast(dry.reason || "补齐失败");
      return;
    }
    const plan = `${num(dry.scanned, 0)} 张待看：可补 ${num(dry.filled, 0) + num(dry.created, 0)} 张` +
      `、已经有了 ${num(dry.unchanged, 0) + num(dry.skipped, 0)} 张` +
      (num(dry.failed, 0) ? `、${num(dry.failed, 0)} 张读不出来` : "");
    if (!num(dry.filled, 0) && !num(dry.created, 0)) {
      toast(`没有要补的：${plan}`);
      return;
    }
    if (
      !(await confirmDialog({
        title: "现在补齐图片信息？",
        message: `${plan}。写完就存进本机清单，随时能再点一次（只会补空的）。`,
        confirmText: "补齐",
      }))
    ) {
      return;
    }
    const done = await apiPost("meme/backfill", { confirm: true });
    if (!done.ok) {
      toast(done.reason || "补齐失败");
      return;
    }
    toast(`已补 ${num(done.filled, 0) + num(done.created, 0)} 张` +
      (num(done.failed, 0) ? `，${num(done.failed, 0)} 张读不出来` : ""));
    await loadMemes();
  } catch (error) {
    toast(error.message || "补齐失败");
  }
}

/** 宽高与帧数的一句话（没有就不显示，不硬编"未知"）。 */
function memePixelText(entry) {
  const width = num(entry && entry.asset_width, 0);
  const height = num(entry && entry.asset_height, 0);
  const frames = num(entry && entry.asset_frame_count, 0);
  const parts = [];
  if (width > 0 && height > 0) parts.push(`${width}×${height}`);
  if (frames > 1) parts.push(`${frames} 帧`);
  return parts.join(" · ");
}

/**
 * 「给没描述的补一句」（S7）：上传只写了文件名，而她检索靠 `meaning` 这几个字段。
 *
 * 两步走：先要一份清单（不调模型），确认之后才真看。数量多时不再一次 12 张地挤
 * （用户 m18541）：后端起一个后台任务并发看图，页面只轮询进度，随时能停。
 */
async function memeDescribeMissing() {
  // 正在跑：这一次点击就是「停」。
  if (ui.meme.describeTimer) {
    try {
      const stopped = await apiPost("meme/describe", { stop: true });
      const job = (stopped && stopped.job) || {};
      const note = $("meme-note");
      if (note) {
        note.textContent = `正在停…（已完成 ${num(job.done, 0)}/${num(job.total, 0)}）`;
      }
    } catch (error) {
      toast(error.message || "停不下来");
    }
    return;
  }
  try {
    // 刷新过页面也能接上正在跑的那一轮（后端只允许一个任务）。
    const current = await apiPost("meme/describe", { status: true });
    const running = (current && current.job) || {};
    if (
      num(running.total, 0) &&
      ["running", "cancelling"].indexOf(String(running.status || "")) >= 0
    ) {
      ui.meme.describeJob = running;
      memeDescribePoll();
      return;
    }
    // 勾了几张就只重写这几张（哪怕它们已经有描述）；没勾就从"没写含义的"里挑。
    const checked = Array.from(ui.meme.checked || []);
    const body = checked.length ? { ids: checked, force: true } : {};
    const dry = await apiPost("meme/describe", body);
    if (!dry.ok) {
      toast(dry.reason || "读不出要补的");
      return;
    }
    // 后端会带"扫了多少条 / 其中多少条含义是空的"（用户 m18371 要能核对）。
    const total = num(dry.blank, 0) || num(dry.total, 0);
    if (!total) {
      toast(dry.reason || "都有描述了");
      return;
    }
    const scanned = num(dry.scanned, 0);
    const workers = Math.max(1, Math.min(4, Math.ceil(total / 8)));
    const minutes = Math.max(1, Math.round((total * 6) / (workers * 60)));
    const names = (dry.targets || [])
      .map((item) => String(item.name || item.id || ""))
      .filter(Boolean)
      .slice(0, 5)
      .join("、");
    if (
      !(await confirmDialog({
        title: checked.length ? "重写这些的描述？" : "把这些的描述补上？",
        message:
          (checked.length ? `选中的 ${total} 张` : `有 ${total} 张还没写含义或用法`) +
          (scanned ? `（这一页共 ${scanned} 条）` : "") +
          `。每张会写「含义 / 什么时候用 / 什么时候不用」三栏。` +
          `开始后会并发 ${workers} 个请求在后台一张张看，估计 ${minutes} 分钟，` +
          "期间可以再点这个按钮停下。" +
          (names ? `先从：${names} 这几张开始。` : ""),
        confirmText: "开始补",
      }))
    ) {
      return;
    }
    const started = await apiPost("meme/describe", {
      ...body,
      all: true,
      confirm: true,
      workers,
    });
    if (!started.ok) {
      toast(started.reason || "起不来批量任务");
      return;
    }
    const job = started.job || {};
    if (!job.id) {
      toast("后端没有交出任务编号");
      return;
    }
    ui.meme.describeJob = job;
    memeDescribePoll();
  } catch (error) {
    toast(error.message || "补描述失败");
  }
}

/** 轮询批量任务的进度；跑完（或出错）就停表，并把结果说清楚。 */
function memeDescribePoll() {
  const note = $("meme-note");
  const button = $("meme-describe");
  const paint = (job) => {
    if (button) button.textContent = job && job.total ? "停止补描述" : "给没描述的补一句";
    if (!job || !job.total) return;
    const where = job.status === "cancelling" ? "正在停" : "补描述中";
    const line =
      `${where} ${num(job.done, 0)}/${num(job.total, 0)}，写好 ${num(job.updated, 0)} 张` +
      (num(job.failed, 0) ? `，${num(job.failed, 0)} 张没成` : "");
    if (note) note.textContent = line;
    // 同一条进度条（用户 m18755）：补描述也走它
    memeProgress(line, num(job.done, 0), num(job.total, 0));
  };
  paint(ui.meme.describeJob);
  const tick = async () => {
    if (!ui.meme.describeTimer) return;
    try {
      const data = await apiPost("meme/describe", { status: true });
      const job = (data && data.job) || {};
      ui.meme.describeJob = job;
      paint(job);
      const status = String(job.status || "");
      if (status === "running" || status === "cancelling") return;
      window.clearInterval(ui.meme.describeTimer);
      ui.meme.describeTimer = 0;
      if (button) button.textContent = "给没描述的补一句";
      if (note) note.textContent = "";
      if (status === "error") {
        memeProgress(`补描述出错了：${job.error || "未知原因"}`, num(job.done, 0), num(job.total, 0), {
          error: true,
        });
      } else {
        memeProgressHide();
      }
      const failed = num(job.failed, 0);
      if (status === "error") {
        toast(job.error || "批量补描述出错了");
      } else {
        toast(
          `${status === "cancelled" ? "停下了" : "补完了"}：写好 ${num(job.updated, 0)} 张` +
            (failed ? `，${failed} 张没成` : "") +
            (failed && (job.failures || []).length
              ? `（例如 ${(job.failures || [])
                  .slice(0, 2)
                  .map((item) => String(item.reason || ""))
                  .filter(Boolean)
                  .join("；")}）`
              : ""),
        );
      }
      await loadMemes();
    } catch (error) {
      // 一两次网络抖动不该把整轮停掉：等下一拍；连续失败由用户点停。
      if (note) note.textContent = `查进度失败：${error.message || error}`;
    }
  };
  ui.meme.describeTimer = window.setInterval(tick, 1500);
}

/** 表情包页的进度条（用户 m18755）：导入与补描述共用同一条。 */
function memeProgress(label, done, total, options) {
  const box = $("meme-progress");
  const text = $("meme-progress-text");
  const fill = $("meme-progress-fill");
  if (!box || !fill) return;
  const cap = Math.max(0, num(total, 0));
  const did = Math.max(0, num(done, 0));
  const ratio = cap > 0 ? Math.min(1, did / cap) : 0;
  box.classList.remove("hidden");
  box.classList.toggle("is-error", Boolean(options && options.error));
  if (text) text.textContent = label || (cap > 0 ? `${did}/${cap}` : "");
  fill.style.width = `${cap > 0 ? Math.round(ratio * 100) : 8}%`;
  box.setAttribute(
    "aria-valuenow",
    String(cap > 0 ? Math.round(ratio * 100) : 0)
  );
}

function memeProgressHide() {
  const box = $("meme-progress");
  if (!box) return;
  box.classList.add("hidden");
  box.classList.remove("is-error");
  const fill = $("meme-progress-fill");
  if (fill) fill.style.width = "0";
}

/** 「从文件夹导入」的后台任务（用户 m18755）：起任务 → 轮询进度 → 报数。 */
async function memeImportPoll() {
  const button = $("meme-import-go");
  const note = $("meme-import-note");
  const job = ui.meme.importJob || {};
  const status = String(job.status || "");
  const total = num(job.total, 0);
  const done = num(job.done, 0);
  memeProgress(
    status === "cancelling"
      ? `正在停…（${done}/${total}）`
      : `导入中 ${done}/${total}（新增 ${num(job.created, 0)} 张）`,
    done,
    total
  );
  if (button) button.textContent = total ? "停止导入" : "开始导入";
  if (["running", "cancelling"].includes(status)) return;
  ui.meme.importTimer = 0;
  ui.meme.importJob = null;
  if (button) button.textContent = "开始导入";
  const bits = [];
  if (num(job.created, 0) > 0) bits.push(`收下 ${num(job.created, 0)} 张`);
  if (num(job.existed, 0) > 0) bits.push(`${num(job.existed, 0)} 张以前就有`);
  if (num(job.failed, 0) > 0) bits.push(`${num(job.failed, 0)} 张没成`);
  if (job.total > job.done) bits.push(`只跑了 ${done}/${total}，剩下的是你停掉的`);
  const reasons = (job.reasons || []).slice(0, 3);
  if (status === "error") {
    memeProgress(`导到一半出错了：${job.error || "未知原因"}`, done, total, {
      error: true,
    });
    toast(job.error || "导入失败");
  } else {
    memeProgressHide();
    toast(bits.length ? bits.join("，") : "没有可导入的图片");
  }
  if (note) note.textContent = reasons.length ? reasons.join("；") : "";
  await loadMemes();
}

async function memeImportFolder() {
  const button = $("meme-import-go");
  if (ui.meme.importTimer) {
    const stopped = await apiPost("meme/import", { stop: true });
    if (stopped && stopped.job) ui.meme.importJob = stopped.job;
    if (button) button.textContent = "正在停…";
    return false;
  }
  const path = String(($("meme-import-path") || {}).value || "").trim();
  if (!path) {
    toast("先写一个目录（服务器上存图的那个文件夹）");
    return false;
  }
  const recursive = ($("meme-import-recursive") || {}).checked !== false;
  const note = $("meme-import-note");
  if (note) note.textContent = "正在扫描…";
  try {
    const started = await apiPost("meme/import", { path, recursive, all: true });
    if (!started || !started.ok) {
      toast((started && started.reason) || "导入失败");
      if (note) note.textContent = "";
      return false;
    }
    const job = started.job || {};
    if (!job.id) {
      toast("后端没有交出任务编号");
      if (note) note.textContent = "";
      return false;
    }
    if (note) note.textContent = "";
    ui.meme.importJob = job;
    if (num(job.total, 0) > num(started.picked, 0)) {
      toast(
        `这个文件夹里共 ${num(job.total, 0)} 张，这次先导 ${num(started.picked, 0)} 张（再点一次接着导）`
      );
    }
    memeProgress(`导入中 0/${num(job.total, 0)}`, 0, num(job.total, 0));
    if (Number(ui.meme.importTimer || 0) === 0) {
      ui.meme.importTimer = window.setInterval(memeImportPoll, 1000);
    }
    return true;
  } catch (error) {
    toast(error.message || "导入失败");
    if (note) note.textContent = "";
    return false;
  }
}

function bindMemes() {
  if ($("meme-drawer-close")) {
    $("meme-drawer-close").addEventListener("click", closeMemeDrawer);
  }
  if ($("meme-drawer-backdrop")) {
    $("meme-drawer-backdrop").addEventListener("click", closeMemeDrawer);
  }
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const drawer = $("meme-drawer");
    if (drawer && !drawer.classList.contains("hidden")) closeMemeDrawer();
  });
  if ($("meme-refresh")) {
    $("meme-refresh").addEventListener("click", () => loadMemes());
  }
  if ($("meme-scan")) {
    $("meme-scan").addEventListener("click", () => memeScanFiles());
  }
  if ($("meme-facts")) {
    $("meme-facts").addEventListener("click", () => memeBackfillFacts());
  }
  if ($("meme-describe")) {
    $("meme-describe").addEventListener("click", () => memeDescribeMissing());
  }
  if ($("meme-import")) {
    $("meme-import").addEventListener("click", () => {
      const bar = $("meme-import-bar");
      if (bar) bar.classList.toggle("hidden");
      const input = $("meme-import-path");
      if (bar && !bar.classList.contains("hidden") && input) input.focus();
    });
  }
  if ($("meme-select-all")) {
    $("meme-select-all").addEventListener("click", () => memeSelectAll(true));
  }
  if ($("meme-clear-select")) {
    $("meme-clear-select").addEventListener("click", () => memeSelectAll(false));
  }
  if ($("meme-delete-checked")) {
    $("meme-delete-checked").addEventListener("click", () => memeDeleteChecked());
  }
  if ($("meme-export-checked")) {
    // 发出去的 ids 与按钮上的 N 同一来源（交集），不可能"显示 2 却导不出"
    $("meme-export-checked").addEventListener("click", () => memeExport(memeCheckedIds()));
  }
  if ($("meme-export-all")) {
    // null 才是"全部"；空数组现在表示"勾选里一条都不剩"，不会再误当成全部
    $("meme-export-all").addEventListener("click", () => memeExport(null));
  }
  if ($("meme-import-pack")) {
    $("meme-import-pack").addEventListener("click", () => memeTogglePackBar());
  }
  if ($("meme-pack-go")) {
    $("meme-pack-go").addEventListener("click", () => memeImportPackFromDir());
  }
  if ($("meme-pack-browse")) {
    $("meme-pack-browse").addEventListener("click", () => $("meme-pack-file").click());
  }
  if ($("meme-pack-file")) {
    $("meme-pack-file").addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      memeImportPack(file);
      event.target.value = "";
    });
  }
  if ($("meme-import-go")) {
    $("meme-import-go").addEventListener("click", () => memeImportFolder());
  }
  if ($("meme-import-path")) {
    $("meme-import-path").addEventListener("keydown", (event) => {
      if (event.key === "Enter") memeImportFolder();
    });
  }
  if ($("meme-upload")) {
    $("meme-upload").addEventListener("click", () => memeOpenUpload());
  }
  if ($("meme-upload-file")) {
    $("meme-upload-file").addEventListener("change", () => {
      const input = $("meme-upload-file");
      memeUploadFiles(input && input.files ? input.files : null);
    });
  }
  if ($("meme-cleanup")) {
    $("meme-cleanup").addEventListener("click", () => memeCleanup());
  }
  if ($("meme-filter-state")) {
    $("meme-filter-state").addEventListener("change", () => {
      ui.meme.state = $("meme-filter-state").value;
      // 换了筛选条件就从第一页重新铺（用户 m18846）
      ui.meme.shown = MEME_PAGE_SIZE;
      renderMemeList();
      memeLoadThumbnails();
    });
  }
  if ($("meme-sort")) {
    $("meme-sort").addEventListener("change", () => {
      ui.meme.order = $("meme-sort").value === "used" ? "used" : "collected";
      // 排序变了要重排：从库里重新取第一页（本地顺序已经不对了）
      void loadMemes();
    });
  }
  if ($("meme-sort-dir")) {
    $("meme-sort-dir").addEventListener("click", () => {
      ui.meme.desc = !(ui.meme.desc !== false);
      if ($("meme-sort-dir")) {
        $("meme-sort-dir").textContent = ui.meme.desc ? "倒序 ↓" : "正序 ↑";
      }
      void loadMemes();
    });
  }
  if ($("meme-search")) {
    $("meme-search").addEventListener("input", () => {
      ui.meme.search = $("meme-search").value;
      ui.meme.shown = MEME_PAGE_SIZE;
      renderMemeList();
      memeLoadThumbnails();
    });
  }
  memeMasterField();
  memeQuotaFields();
  memeScaleFields();
  memeCollectFields();
  memeMasterBox();
  renderMemeList();
  renderMemeDetail();
}

/* MEME-VIEW-END */

boot();
