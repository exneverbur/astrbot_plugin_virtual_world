/**
 * 虚拟世界编辑器前端。
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
  tools: [],
  overview: [],
  selectedNode: "",
  selectedAction: "",
  selectedZone: "",
  mapLevel: "world",
  // 全局设置：当前选中的分类标签，以及哪些标签里有未保存的改动
  settingsTab: "basic",
  dirtyTabs: new Set(),
  events: {},
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
  // 通讯录：认识的人 + 当前选中的人
  contacts: [],
  contactUser: "",
  contactDetail: null,
};

/* ================================================================== */
/* 常量：中文标签与说明                                                 */
/* ================================================================== */

const ATTRS = [
  { key: "energy", label: "精力", hint: "越低越想睡觉，睡觉会恢复" },
  { key: "loneliness", label: "孤独感", hint: "越高越想找人说话" },
  { key: "curiosity", label: "好奇心", hint: "越高越想去书房上网查东西" },
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
    hint: "一段对话被总结成记忆时发出来，方便对着看记得准不准。",
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
  nickname: { icon: "🪪", label: "群名片" },
  engagement: { icon: "🔇", label: "进入安静期" },
  extreme: { icon: "⚠️", label: "极端保护" },
  wake_up: { icon: "👋", label: "被叫醒" },
  interrupt: { icon: "✋", label: "打断" },
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
};

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
  ["intent", "打算怎么办"],
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

/** 图片转述提示词的占位文字：真正的默认值从后端 `/defaults` 取（core/defaults.py 一份）。 */
const DEFAULT_CAPTION_PROMPT =
  "你是群聊图片转述器……（留空即用内置默认，点标题右侧的 ↺ 可以看默认内容）";

const DEFAULT_CAPTION_RELATION_PROMPT =
  "你会拿到图片的文字转述和当前对话……（留空即用内置默认）";

const DEFAULT_FORWARD_PROMPT =
  "你在读一段被转发的聊天记录……（留空即用内置默认，点标题右侧的 ↺ 可以看默认内容）";

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

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.remove("hidden");
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => node.classList.add("hidden"), 2600);
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
function markDirty() {
  ui.dirty = true;
  setSaveState("有未保存的改动");
  // 哪个标签页里改的，就在那个标签上点一个小圆点：
  // 分类以后最容易出的疏漏就是"在别的页改完，忘了保存"
  if (ui.settingsTab) ui.dirtyTabs.add(ui.settingsTab);
  renderSettingsTabs();
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
    hint: "她多久主动冒一次头（被叫到时的回话不算）。这是上限与倾向：具体说多少还看她心情和孤独感。",
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
    hint: "群里在聊的时候她插话的意愿。调高会更爱接话，也更容易被热闹带动。",
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
    hint: "她想你的速度、主动来找你的次数，以及她多想要人陪着贴着。",
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
    hint: "单次回复的长度上限，以及说话太密的判定。调低她会更像群里随手打两行。",
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
    hint: "她多容易被逗乐、多容易被惹到，以及情绪回得多快。调高会更容易兴奋起来。",
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
    hint: "她一个人待着时遇上事的多少。调高更热闹（也更费模型），调低更像安静过日子。",
    levels: [
      { "events.micro_per_hour": 0.3, "events.small_per_hour": 0.1,
        "events.big_per_hour": 0.01, "events.min_gap_minutes": 120 },
      { "events.micro_per_hour": 0.6, "events.small_per_hour": 0.2,
        "events.big_per_hour": 0.015, "events.min_gap_minutes": 60 },
      { "events.micro_per_hour": 1, "events.small_per_hour": 0.3,
        "events.big_per_hour": 0.02, "events.min_gap_minutes": 30 },
      { "events.micro_per_hour": 2, "events.small_per_hour": 0.6,
        "events.big_per_hour": 0.04, "events.min_gap_minutes": 15 },
      { "events.micro_per_hour": 4, "events.small_per_hour": 1.2,
        "events.big_per_hour": 0.08, "events.min_gap_minutes": 5 },
    ],
  },
  {
    id: "whimsy",
    label: "任性度",
    hint: "她有多凭性子来：调高会让这一轮更多交给大模型自由发挥，情绪上头更久，也爱随手拍张照。",
    levels: [
      { "decider.llm_rate_min": 0.01, "decider.llm_rate_max": 0.15,
        "state_dynamics.mood_override_duration": 180, "events.photo_chance": 0.1,
        "events.genre_cooldown_minutes": 480 },
      { "decider.llm_rate_min": 0.03, "decider.llm_rate_max": 0.28,
        "state_dynamics.mood_override_duration": 360, "events.photo_chance": 0.2,
        "events.genre_cooldown_minutes": 300 },
      { "decider.llm_rate_min": 0.05, "decider.llm_rate_max": 0.4,
        "state_dynamics.mood_override_duration": 600, "events.photo_chance": 0.3,
        "events.genre_cooldown_minutes": 180 },
      { "decider.llm_rate_min": 0.09, "decider.llm_rate_max": 0.55,
        "state_dynamics.mood_override_duration": 900, "events.photo_chance": 0.45,
        "events.genre_cooldown_minutes": 90 },
      { "decider.llm_rate_min": 0.15, "decider.llm_rate_max": 0.75,
        "state_dynamics.mood_override_duration": 1500, "events.photo_chance": 0.6,
        "events.genre_cooldown_minutes": 30 },
    ],
  },
  {
    id: "memory_diligence",
    label: "记忆勤奋度",
    hint: "她记你记得多细、整理得多勤。调高留下的画像与记忆更完整（也更容易花 token）。",
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
];

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
function knobTouched(world, knob) {
  const values = knob.levels[knobLevel(world, knob) - 1];
  return Object.keys(values).some((path) => Number(getPath(world, path)) !== Number(values[path]));
}

function applyKnobLevel(world, knob, level) {
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

/** 「手感」滑块：向导和全局设置共用同一块。 */
function knobEditor(world, onChange) {
  world.ui_knobs = world.ui_knobs || {};
  const box = el("div", "full knob-box");
  KNOBS.forEach((knob) => {
    const row = el("div", "knob-row");
    const head = el("div", "knob-head");
    head.appendChild(el("span", "knob-label", knob.label));
    const levelText = el("span", "knob-level", "");
    const touched = el("span", "knob-touched", "");
    head.appendChild(touched);
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

    const detail = document.createElement("details");
    detail.className = "knob-detail";
    const summary = document.createElement("summary");
    summary.appendChild(el("span", "", "会改哪些参数"));
    detail.appendChild(summary);
    const body = el("div", "knob-detail-body");
    detail.appendChild(body);
    row.appendChild(detail);
    if (knob.hint) row.appendChild(el("p", "muted knob-hint", knob.hint));

    function draw() {
      const level = knobLevel(world, knob);
      slider.value = String(level);
      levelText.textContent = `${KNOB_LEVEL_NAMES[level - 1]}（${level}/5）`;
      const values = knob.levels[level - 1];
      body.innerHTML = "";
      Object.keys(values).forEach((path) => {
        const now = Number(getPath(world, path));
        const want = Number(values[path]);
        const changed = now !== want;
        const item = el("div", "knob-detail-row");
        const nameCell = el("div", "knob-detail-name");
        nameCell.appendChild(el("span", "", fieldLabel(path)));
        nameCell.appendChild(el("code", "knob-detail-path", path));
        item.appendChild(nameCell);
        item.appendChild(el("span", "", `${want}`));
        if (changed) {
          item.appendChild(el("span", "muted", `（现在是 ${now}）`));
        }
        body.appendChild(item);
      });
      touched.textContent = knobTouched(world, knob) ? "已手动调整" : "";
    }

    slider.addEventListener("input", () => {
      world.ui_knobs[knob.id] = Number(slider.value);
      applyKnobLevel(world, knob, Number(slider.value));
      markDirty();
      draw();
      if (typeof onChange === "function") onChange();
    });
    draw();
    box.appendChild(row);
  });
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
        "proactive_per_day（每天最多主动找他几次）/ prompt（这一级写给模型的提示词文本，可随便改）。",
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
    const [choiceValue, choiceLabel] =
      typeof choice === "object" ? [choice.key, choice.label] : choice;
    node.appendChild(option(choiceValue, choiceLabel));
  });
  node.value = value ?? "";
  node.addEventListener("change", () => onChange(node.value));
  wrapper.appendChild(node);
  return wrapper;
}

function checkboxField(label, checked, onChange, opts = {}) {
  const wrapper = el("label", "inline");
  if (opts.adv) wrapper.dataset.adv = "1";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = !!checked;
  input.addEventListener("change", () => onChange(input.checked));
  wrapper.appendChild(input);
  wrapper.appendChild(el("span", "", label));
  const tip = tipBox(opts.hint);
  if (tip) wrapper.appendChild(tip);
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
    const tip = `${choice.hint || ""}\n点一下切换：关 → 完整 → 精简。`;
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
}) {
  pickerState = {
    // 兜底再洗一遍：别的入口直接调 openPicker 时也不会带进没有 id 的条目
    items: pickerEntries(items),
    chosen: new Set(pickerValues(selected)),
    multi,
    onConfirm,
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
          `这一步要求她在「${nodeLabel(need)}」。点一下会在它前面插入一步「移动到${nodeLabel(need)}」，` +
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
      "请在 AstrBot WebUI 的插件详情页里打开本页面（插件管理 → 虚拟世界 → 虚拟世界编辑器）。";
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
    await startApp();
    return;
  }
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
  bindTabs();
  bindButtons();
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
        ? "现在是浅色，点一下切到深色（会记住）"
        : "现在是深色，点一下切到浅色（会记住）";
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

async function loadAll() {
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
    if (config.warnings && config.warnings.length) {
      toast(`配置提醒：${config.warnings.join("；")}`);
    }
    renderEverything();
    setSaveState("已加载");
  } catch (error) {
    setSaveState("加载失败");
    toast(error.message || "加载配置失败");
  }
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
  if (name === "events") loadEvents();
  if (name === "contacts") loadContacts();
  if (name === "memories") loadMemories();
  if (name === "logs") loadLogs();
  if (name === "map") loadOverview();
  if (name === "debug") loadTools();
  if (name === "presets") loadPresets();
}

function bindButtons() {
  $("save").addEventListener("click", saveAll);
  bindFilterToggle("memory-filter-toggle", "#tab-memories");
  bindFilterToggle("log-filter-toggle", "#tab-logs");
  bindSegmented("session-seg", { sessions: "session-pane", groups: "group-pane" });
  if ($("theme-toggle")) {
    $("theme-toggle").addEventListener("click", toggleTheme);
  }
  if ($("hero-goto-events")) {
    $("hero-goto-events").addEventListener("click", () => showTab("events"));
  }
  ["map-fit", "map-fit-zone"].forEach((id) => {
    if ($(id)) $(id).addEventListener("click", fitMapToCanvas);
  });
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
        toast("先点一次「预览注入内容」或「预览自主提示词」");
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
  if ($("settings-save-top")) {
    $("settings-save-top").addEventListener("click", saveAll);
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
  $("status-session").addEventListener("change", () => {
    refreshStatus();
    // 换会话 = 可能换了人格：简易人设跟着刷新一遍
    if (typeof ui.loadPersonaBrief === "function") {
      ui.loadPersonaBrief({ overwrite: true });
    }
  });
  $("values-edit").addEventListener("click", () => {
    ui.valuesEdit = !ui.valuesEdit;
    ui.valueDraft = {};
    renderValueRows(((ui.status || {}).values) || {});
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
  $("debug-inject").addEventListener("click", () => loadPrompt("inject"));
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
  setSaveState("保存中…");
  try {
    const worldResult = await apiPost("config/world", { world: ui.config.world });
    const scheduleResult = await apiPost("config/schedules", {
      schedules: ui.config.schedules,
    });
    const sessionResult = await apiPost("config/sessions", {
      sessions: ui.config.sessions,
    });
    const warnings = [
      ...(worldResult.warnings || []),
      ...(scheduleResult.warnings || []),
      ...(sessionResult.warnings || []),
      ...toolActionWarnings(),
    ];
    // 保存时顺手给缺意图的工具 / 指令步骤补的意图（写进配置了，编辑器里能看到）
    const autoIntents = scheduleResult.filled || [];
    // 保存即生效：不用再单独点「热加载」
    const reloadResult = await apiPost("reload", {});
    ui.dirty = false;
    ui.dirtyTabs.clear();
    renderSettingsTabs();
    setSaveState("已保存并生效");
    toast(
      warnings.length
        ? `已保存并生效，提醒：${warnings.join("；")}`
        : autoIntents.length
          ? `已保存并生效，顺手补了这些步骤的意图：${autoIntents.join("；")}`
        : (reloadResult.warnings || []).length
          ? `已保存并生效，提醒：${reloadResult.warnings.join("；")}`
          : "已保存并生效",
    );
    await loadAll();
  } catch (error) {
    setSaveState("保存失败");
    toast(error.message || "保存失败");
  }
}

/** 工具型动作必须选工具；没选的在这里统一提醒（仍然允许保存，运行时会跳过并写日志）。 */
function toolActionWarnings() {
  // 停用的动作运行时会被直接跳过，别再拿它来烦用户
  const live = actions().filter((action) => action.enabled !== false);
  const missing = live
    .filter((action) => action.llm_level === "tool" && !actionToolNames(action).length)
    .map((action) => action.name || action.id);
  const noCommand = live
    .filter((action) => action.llm_level === "command" && !String(action.trigger_command || "").trim())
    .map((action) => action.name || action.id);
  const warnings = [];
  if (missing.length) warnings.push(`这些工具型动作还没选工具，会被跳过：${missing.join("、")}`);
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

/** 把「工具不存在」提醒画到状态页和动作页的横幅上。 */
function renderToolWarnings() {
  const missing = missingToolActions();
  const targets = [
    $("status-banner"),
    $("action-banner"),
    $("debug-banner"),
  ].filter(Boolean);
  targets.forEach((box) => {
    if (!missing.length) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    box.classList.remove("hidden");
    box.innerHTML = "";
    box.appendChild(
      el("span", "banner-icon", "⚠"),
    );
    const body = el("div", "banner-body");
    body.appendChild(
      el(
        "div",
        "banner-title",
        `有 ${missing.length} 个工具型动作对应的工具不存在，执行时会被跳过`,
      ),
    );
    body.appendChild(
      el(
        "div",
        "banner-detail",
        missing
          .map((item) => `${item.name} → ${item.tools.join(" / ")}`)
          .join("　·　"),
      ),
    );
    body.appendChild(
      el(
        "div",
        "banner-hint",
        "在动作里把工具换成 AstrBot 里已经装好的那个（搜索、天气这类内置动作也带备选，装任意一个搜索工具就能用）。",
      ),
    );
    box.appendChild(body);
  });
}

/** 一个动作挂了哪些工具（新写法 tool_names 优先，兼容老的 tool_name）。 */
function actionToolNames(action) {
  const list = Array.isArray(action.tool_names) ? action.tool_names.filter(Boolean) : [];
  if (list.length) return list;
  return action.tool_name ? [action.tool_name] : [];
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
    toast(result.ok ? "群名片已改" : result.reason || "改名片失败");
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

function renderSessionSelects() {
  const options = scopeOptions();
  [
    "status-session",
    "memory-session",
    "debug-session",
    "log-session",
    "map-session",
    "schedule-session",
    "contacts-session",
  ].forEach((id) => {
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
  return options;
}

/** 这个会话属于哪个组（没进组返回 null）。 */
function groupOwning(sessionId) {
  const wanted = String(sessionId || "");
  if (!wanted) return null;
  return (
    groups().find((group) =>
      (group.sessions || []).some((id) => String(id) === wanted),
    ) || null
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

function renderValueRows(values) {
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
  avatar.textContent = label ? Array.from(label)[0] : "？";
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
}

/* ---------------- 数值曲线：她最近过得怎么样 ---------------- */

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
      + "跑多少遍都不会弄脏她的世界。";
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
        "「被人跟着」这种偏暗的题材一次都不会出现。权重 0 = 不生成这一类。",
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
      const weight = document.createElement("input");
      weight.type = "number";
      weight.min = "0";
      weight.step = "1";
      weight.title = "权重：相对比例，0 就是不生成这一类";
      weight.value = item.weight ?? 0;
      weight.addEventListener("change", () => {
        item.weight = num(weight.value, 0);
        markDirty();
      });
      const examples = document.createElement("input");
      examples.type = "text";
      examples.value = item.examples || "";
      examples.placeholder = "例子：被误解、被人跟着";
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
      row.appendChild(examples);
      row.appendChild(remove);
      list.appendChild(row);
    });
    const add = el("button", "small ghost", "＋ 加一类题材");
    add.type = "button";
    add.addEventListener("click", () => {
      world.events.genres.push({ name: "新题材", weight: 5, examples: "" });
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
    renderValueRows(values);

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
        ? `已锁定，插件不会自动改名（原名：${data.nickname_base || "还没记下来"}）。点一下解锁。`
        : "解锁状态下她会随状态自动改名。点一下锁住。";
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

    // 更早的群聊摘要（只有在「上下文 → 留档超了怎么办 = 压成摘要」时才有）
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
      toast(result.interrupted ? "已打断她正在做的事" : "她现在没有在做动作");
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
  if (!ui.tools.length) {
    return [{ id: "", name: "（AstrBot 还没有注册任何工具）", desc: "", group: "其它" }];
  }
  return ui.tools.map((tool) => ({
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
  }));
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
    chip.title = "点一下进对面区域看看";
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
  // 生成结果确认即保存：不然用户以为没添加成功，还要再找一次右上角的「保存」
  await saveAll();
  toast(`已加入 ${added} 个动作并保存`);
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
        hint: "自动摆好位置并按区域连成链（首个连最近的地点，默认 1 tick）。「确认加入」写入配置后立即生效。"
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
  // 生成结果确认即保存
  await saveAll();
  toast(`已加入 ${picked.length} 个地点（含自动连线）并保存`);
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
      toast("跨区连线已加入，记得点右上角保存");
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
      toast("地图已替换（记得点右上角保存）");
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
        "动作库里的归类，随便起名字（下拉里是已有的分组，也可以直接写新的）。" +
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
          hint: "只写「id（名字）」：看一眼就知道干嘛的动作（点头、抱抱、亲亲）用这个，省提示词。",
        },
      ],
      (value) => {
        action.desc_mode = value;
        renderActionForm();
      },
      { hint: "工具型 / 时长由你定 / 只有这个地点才能做这些标记不受影响。" },
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
      const tool = ui.tools.find((item) => item.name === name);
      paramBox.appendChild(el("div", "sub-title", name));
      paramBox.appendChild(
        el("p", "hint", tool ? tool.param_text || "（这个工具不需要参数）" : "（没找到这个工具）"),
      );
    });
    const toolName = currentTools[0] || "";
    const tool = ui.tools.find((item) => item.name === toolName);
    toolBox.appendChild(paramBox);
    toolBox.appendChild(fixedParamsEditor(action, currentTools));

    // 工具型动作最容易踩的坑，直接在表单里点出来
    const missingTools = currentTools.filter(
      (name) => !ui.tools.some((item) => item.name === name),
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
      const searchBox = el("div", "subsection");
      const searchTitle = el("div", "sub-title");
      searchTitle.appendChild(el("span", "", "联网检索设置"));
      searchTitle.appendChild(
        tipBox(
          "她在 intent 里写想查什么，也可给几条 queries 分角度查；" +
            "结果整理成证据交给她，证据里没有的会直说没查到。",
        ),
      );
      searchBox.appendChild(searchTitle);
      searchBox.appendChild(
        pickerField(
          "阅读网页工具（可选）",
          action.reader_tool_names || [],
          toolItems(),
          (chosen) => {
            action.reader_tool_names = chosen;
            // 选完要立刻重画：不然按钮上还是"点击选择工具…"，
            // 再点开时上次选的也没勾上（看起来就像这个字段配不了）
            renderActionForm();
          },
          {
            hint: "能传网址、返回正文的工具（如把网页转成 markdown）。配上后按检索深度抓最靠前的几篇正文；留空只用摘要。",
            empty: "点击选择工具…",
            renderChips: true,
          },
        ),
      );
      searchBox.appendChild(
        pillsField(
          "检索深度",
          action.search_depth || "standard",
          SEARCH_DEPTHS,
          (value) => {
            action.search_depth = value;
            renderActionForm();
          },
        {
          hint: "查询上限：快查只查一轮；标准读前两篇、不够补查一轮；深挖至少三篇、最多补两轮。她只能选更浅的档。",
        },
        ),
      );
      searchBox.appendChild(
        inputField(
          "最多查询条数",
          action.search_max_queries ?? 3,
          (value) => (action.search_max_queries = Math.min(5, Math.max(1, num(value, 3)))),
          {
            hint:
              "她自己一次最多能给几条查询词；没写查询词时，插件会让辅助模型按她的意图翻出一条。",
            type: "number",
            min: 1,
            max: 5,
          },
        ),
      );
      searchBox.appendChild(
        inputField(
          "最多读几篇正文",
          action.search_max_reads ?? 2,
          (value) => (action.search_max_reads = Math.max(0, num(value, 2))),
          { hint: "配了阅读工具才生效；读得越多越慢，也越费工具额度。", type: "number", min: 0, max: 5 },
        ),
      );
      searchBox.appendChild(
        inputField(
          "最多补查几轮",
          action.search_rounds ?? 1,
          (value) => (action.search_rounds = Math.max(0, num(value, 1))),
          {
            hint: "查到的东西太少时，让辅助模型换个角度再查一轮；0 = 不补查。",
            type: "number",
            min: 0,
            max: 3,
          },
        ),
      );
      searchBox.appendChild(
        inputField(
          "搜索主题",
          action.search_topic || "",
          (value) => (action.search_topic = value.trim()),
          {
            hint: "这个动作固定查什么：日程调用且没写意图时用它当查询词。留空则由她按当时处境自己想一句。",
            placeholder: "例如 今日新闻热点",
          },
        ),
      );
      searchBox.appendChild(
        inputField(
          "查询模板（可选）",
          action.search_query_template || "",
          (value) => (action.search_query_template = value.trim()),
          {
            hint: "把主题套成固定格式，占位符 {topic} 与 {date}，例如「{date} 新闻热点」。",
            placeholder: "例如 {date} 新闻热点",
          },
        ),
      );
      searchBox.appendChild(
        pillsField(
          "讲结果时带来源",
          action.search_cite ? "yes" : "no",
          [
            { key: "no", label: "不带", hint: "来源只写进日志与调试输出，群里不出现网址（默认）" },
            { key: "yes", label: "带一条", hint: "允许她在末尾用括号补一条参考链接" },
          ],
          (value) => {
            action.search_cite = value === "yes";
            renderActionForm();
          },
          { hint: "群里通常不想看到一串网址；要核对来源时再打开。" },
        ),
      );
      searchBox.appendChild(
        inputField(
          "查完回落多少好奇",
          num(action.search_satisfy_curiosity ?? 0.3),
          (value) =>
            (action.search_satisfy_curiosity = Math.min(1, Math.max(0, num(value, 0.3)))),
          {
            hint: "查完一次、真拿到东西之后好奇心降多少（默认 0.3）；0 = 不回落",
            type: "number",
            step: "0.05",
          },
        ),
      );
      toolBox.appendChild(searchBox);
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

function visibleActions() {
  const keyword = String(($("action-search") || {}).value || "").trim().toLowerCase();
  const picked = String(ui.actionGroup || "");
  return actions().filter((action) => {
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
  const list = actions();
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
      "如果只是暂时不想让她做，用卡片左上角的开关停用就好。",
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
      toast(`已替换为 ${parsed.length} 个动作（记得点右上角保存）`);
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
  try {
    const result = await apiPost("state/action", {
      session: sessionId,
      action: "run_schedule",
      schedule_id: schedule.id,
      force: true,
    });
    toast(result.ok ? result.note || "已执行" : result.reason || "没有执行");
    if (result.ok) refreshStatus();
  } catch (error) {
    toast(error.message || "执行失败");
  }
}

function renderScheduleForm() {
  const form = $("schedule-form");
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
  form.appendChild(
    inputField("触发时间", schedule.time || "08:00", (value) => (schedule.time = value), {
      hint: "24 小时制，格式 HH:MM，例如 23:30。精度取决于世界时钟的 tick 间隔。",
      placeholder: "23:30",
    }),
  );
  form.appendChild(
    checkboxField(
      "只做这一次（跑完自动删掉）",
      schedule.once === true,
      (value) => {
        schedule.once = value;
        if (!value) schedule.date = "";
        renderScheduleForm();
      },
      {
        hint: "开启后这条日程只在指定那天跑一遍，跑完自动删除；「星期」对它不生效。",
      },
    ),
  );
  if (schedule.once) {
    form.appendChild(
      inputField("日期", schedule.date || "", (value) => (schedule.date = value.trim()), {
        hint: "格式 YYYY-MM-DD；留空表示下一次到点就跑。日期过了的会自己清掉。",
        placeholder: "2026-09-22",
      }),
    );
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
  } else {
    form.appendChild(weekdayField(schedule));
  }
  // 描述对两种日程都适用：固定日程（每天/每周）也要能写清楚"这件事是干什么的"
  if (!schedule.once) {
    form.appendChild(
      inputField(
        "描述（这条日程是干什么的）",
        schedule.note || "",
        (value) => (schedule.note = value.trim()),
        {
          hint: "到点时会连着动作链一起带给她：她知道自己现在在做什么、为什么做。留空就只有动作链。",
          placeholder: "睡前收尾：洗漱完回卧室躺下，别再摸手机",
        },
      ),
    );
  }
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
        schedule.sessions = chosen;
        renderScheduleForm();
      },
      {
        hint:
          "只列她这一组：勾组 = 她自己挑组里的哪一处说，" +
          "单勾会话 = 就发那一处；留空 = 各自跑。",
        empty: "（每个会话各自跑）",
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
  renderScheduleList();
  renderScheduleForm();
}

/* ================================================================== */
/* 会话                                                                */
/* ================================================================== */

function renderSessionList() {
  const list = $("session-list");
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
      renderSessionList();
      renderSessionSelects();
    });
    const del = el("button", "small danger", "删除");
    del.addEventListener("click", () => {
      ui.sessions = ui.sessions.filter((row) => row.session_id !== session.session_id);
      ui.config.sessions.sessions = ui.sessions;
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

/** 会话组：组里的群 / 私聊共享聊天上下文与记忆。 */

function groups() {
  if (!ui.config.sessions) ui.config.sessions = { sessions: [], groups: [] };
  if (!Array.isArray(ui.config.sessions.groups)) ui.config.sessions.groups = [];
  return ui.config.sessions.groups;
}

function renderGroupList() {
  const list = $("group-list");
  if (!list) return;
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
  return card;
}

function addGroup() {
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
      const meta = LOG_TYPES[event.event_type] || { icon: "•", label: event.event_type };
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
  const meta = LOG_TYPES[event.event_type] || { icon: "•", label: event.event_type };
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
    if (item.digest) {
      row.appendChild(el("div", "contact-row-digest", item.digest));
    }
    box.appendChild(row);
  });
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
    if (card) metaBits.push(`在 ${sessionId.split(":").slice(-1)[0]} 叫「${card}」`);
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
    key: "basic",
    label: "基础",
    hint: "她是谁、生活在什么样的世界、谁是管理员",
    sections: ["基础", "persona", "persona_brief", "她的初始状态"],
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
      "手感（滑块）",
      "自主决策",
      "孤独感与插话",
      "没人理她时怎么办",
      "说话频率与预算",
      "说话节奏",
    ],
  },
  {
    key: "world",
    label: "世界与事件",
    hint: "她遇上什么事、怎么求助、动作与地点、工具、天气",
    sections: [
      "事件",
      "作息与夜晚",
      "她自己的账",
      "干涉与线索",
      "动作与地点",
      "工具",
      "天气",
    ],
  },
  {
    key: "mind",
    label: "记忆与上下文",
    hint: "记得什么、怎么把信息喂给模型",
    sections: [
      "记忆",
      "用户画像",
      "上下文",
      "图片转述",
    ],
  },
  {
    key: "debug",
    label: "调试与安全",
    hint: "排查用，以及不许做的事",
    sections: ["调试输出", "内容安全与隐私"],
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
  return found ? found.key : "basic";
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
    button.appendChild(el("span", "", tab.label));
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
    hits.push({
      tab,
      title,
      label: headMatch ? title : fieldHit || title,
      tip: tipHit ? tipHit.slice(0, 80) + "…" : "",
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
      if (target && target.scrollIntoView) {
        target.scrollIntoView({ block: "center", behavior: "smooth" });
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
    "聊天记录最多带几张图",
  ],
  "图片转述": [
    "最多记住多少张图",
    "缓存保留多少天",
    "摘要最多多少字",
    "转发摘要提示词",
  ],
  "说话节奏": [
    "每个字停顿（秒）",
    "单条最多停顿（秒）",
    "回答前先等几秒（安静期）",
    "安静期最长等多久（秒）",
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

function renderSettings() {
  const world = ui.config.world;
  world.default_state = world.default_state || {};
  world.state_dynamics = world.state_dynamics || {};
  world.limits = world.limits || {};
  world.engagement = world.engagement || {};
  world.nickname_sync = world.nickname_sync || {};
  world.content_safety = world.content_safety || {};
  world.reply_style = world.reply_style || {};
  world.vision = world.vision || {};
  const form = $("settings-form");
  form.innerHTML = "";
  form.className = "form settings";

  /* --- 基础 --- */
  const basic = settingsSection(
    "基础",
    "这些是全局规则，决定她生活在什么样的世界里。",
    "新手只需要改「全局提示词」，其它保持默认即可。",
  );
  const basicFull = el("div", "full");
  const wizardRow = el("div", "row");
  const wizardButton = el("button", "small primary", "重新运行设置向导…");
  wizardButton.type = "button";
  wizardButton.title = "把「她在哪儿生活 / 她是谁 / 模型 / 手感 / 记忆」再走一遍（每一步都会预填当前值）";
  wizardButton.addEventListener("click", () => openWizard());
  wizardRow.appendChild(wizardButton);
  wizardRow.appendChild(
    el("span", "muted", "第一次用不知道从哪下手就点它；只会覆盖你在这几步里改过的东西"),
  );
  basicFull.appendChild(wizardRow);
  basicFull.appendChild(
    textareaField("世界规则（全局提示词）", world.global_prompt || "", (value) => (world.global_prompt = value), {
      hint:
        "每次和她对话都会带上这段规则。用一两句话说明「她在过自己的生活、不要解释设定」就够了，不要写太长。",
      rows: 4,
    }),
  );
  basic._fields.appendChild(basicFull);
  basic._fields.appendChild(
    inputField("世界名称", world.name || "小世界", (value) => (world.name = value), {
      hint: "只用于展示。",
    }),
  );
  basic._fields.appendChild(
    pillsField(
      "被 @ 时的回复方式",
      world.reply_mode || "takeover",
      [
        {
          key: "takeover",
          label: "接管回复（推荐）",
          hint: "插件自己调模型、按 JSON 动作执行并发送，主人格不重复回复；模型出错时交回主人格。",
        },
        {
          key: "inject",
          label: "仅注入状态",
          hint: "只把她的处境追加给主人格，由主人格照常用自然语言回复。",
        },
      ],
      (value) => {
        world.reply_mode = value;
        renderSettings();
        applyPronoun($("app"));
      },
      { hint: "决定「消息走到大模型」时由谁负责回复。" },
    ),
  );
  basic._fields.appendChild(
    checkboxField(
      "要求先写推理草稿（reasoning）",
      world.reasoning_enabled !== false,
      (value) => (world.reasoning_enabled = value),
      {
        hint: "输出动作前先写「在哪 / 状态 / 心情 / 和谁说话 / 打算怎么办」。草稿不进群、不占动作数，能减少答错对象。",
      },
    ),
  );
  basic._fields.appendChild(
    inputField("时区", world.timezone || "Asia/Shanghai", (value) => (world.timezone = value), {
      hint: "日程按这个时区判断几点。系统缺少时区数据时自动退回本机时间。",
    }),
  );
  basic._fields.appendChild(
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
  form.appendChild(basic);

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
      hint: "她是谁、怎么说话、在意什么。留空时自动回落到 AstrBot 那份，免得把人格弄没。",
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
      toast(`已导入 ${data.chars || text.length} 字，记得保存`);
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
        toast(`采用了 ${picked.length} 条，记得点右上角保存`);
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
      hint: "这一句会被喂给她当口吻样本——宁缺毋滥，别写成你想让她说的台词。",
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
        toast("改好了，记得点右上角保存");
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
      hint: "选几个场景，每个场景让她说三条不同走法的话。生成的结果会先放进候选池，你可以慢慢挑。",
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
      toast(`候选池加了 ${added} 条，点「候选池」去挑，记得保存`);
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
    toast(`从聊天里挑了 ${found.length} 条候选，记得点右上角保存`);
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
  form.appendChild(stateSection);

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
  [
    ["energy_decay_per_min", "精力衰减/分钟", "越大越容易累"],
    ["loneliness_growth_per_min", "孤独增长/分钟", "越大越容易想找人"],
    ["curiosity_growth_per_min", "好奇增长/分钟", "越大越想上网查东西"],
    ["affect_decay_per_min", "心潮回落/分钟", "越大情绪平复得越快（默认 0.02 ≈ 50 分钟从满值回到平静）"],
    ["valence_decay_per_min", "效价回落/分钟", "越大心情平复得越快（默认 0.014 ≈ 50 分钟回落一半）"],
    ["chat_valence_cap", "聊天单轮最多推动效价", "一轮聊天最多让心情变化多少。调大她会因为几句夸奖就明显开心；默认 0.05 偏向「心情靠经历，不靠嘴甜」"],
    ["chat_valence_daily_cap", "聊天每天最多推动效价", "日常聊天一天最多把心情推多少（正负各算一份）。填 0 = 不限制；日常陪伴改的是好感度，不是心情的量程"],
    ["boredom_growth_per_min", "无聊增长/分钟", "越大越想换地方"],
    ["sleep_energy_recovery_per_min", "睡觉恢复精力/分钟", "越大睡一觉回得越多"],
    ["nap_energy_recovery_per_min", "小睡恢复精力/分钟", "小睡时的恢复速度"],
    ["sleep_curiosity_decay_per_min", "睡觉时好奇回落/分钟", "睡一觉把昨天攒的好奇放下（默认 0.0012 ≈ 整觉降 0.58）"],
    ["sleep_curiosity_floor", "睡醒时最低的好奇", "睡一觉最多把好奇心压到这儿：醒来还是会对新鲜事感兴趣"],
    ["atmosphere_multiplier", "地点氛围影响强度", "0 表示地点氛围完全不影响数值"],
    ["desire_growth_per_min", "欲求增长/分钟", "越大越想要人碰：默认约三天攒满；累着或心情差时涨得更慢"],
    ["desire_relief", "被亲近一次降多少", "再乘动作自己的「亲密程度」：抱一下比拍拍肩解渴"],
    ["desire_tease", "被撩一下涨多少", "只是嘴上撩、没真碰到：再乘关系亲疏，越亲近越管用"],
    ["desire_sleep_fall_per_hour", "睡觉时欲求回落/小时", "睡一觉起来没那么憋"],
    ["desire_wake_keep", "睡醒时保留多少", "0~1，睡醒后欲求乘这个系数"],
    ["desire_soft_top", "欲求涨到多少后减半", "过了这条线涨速减半，免得一直吊在顶上"],
  ].forEach(([key, label, hint]) => {
    dynamics._fields.appendChild(
      inputField(label, num(world.state_dynamics[key]), (value) => (world.state_dynamics[key] = num(value)), {
        hint,
        type: "number",
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
    "手感（滑块）",
    "不想逐个调参数就用这几个滑块：一格一格拖，它会同时改掉一组相关的设置。",
    "中间那档就是内置默认值。滑块给的是上限与倾向，具体说多少还看她当时的心情和孤独感。" +
      "想精调就展开「会改哪些参数」，改过之后这里会标「已手动调整」。",
  );
  knobs._fields.appendChild(knobEditor(world));
  form.appendChild(knobs);

  const limits = settingsSection(
    "说话频率与预算",
    "控制她多久主动说一次话、每次最多说几句，以及大模型的调用预算。",
    "空群里最容易出问题的就是「自言自语刷屏」，这里的上限就是干这个用的。",
  );
  [
    ["max_autonomous_per_hour", "每小时最多自主行动次数", "包括主动搭话、去搜索、换地方"],
    ["max_share_per_hour", "每小时最多分享次数", "「分享见闻」这类动作的上限"],
    [
      "max_replies_per_hour",
      "每小时最多被动回复次数",
      "被 @ 到 / 私聊 / 明确对她说时给的回复；超过之后这一条不回（仍然由本插件接管，不落回主人格）。默认 200，填 0 = 完全不限制",
    ],
    ["max_messages_per_say", "每次最多说几句", "一次回复拆成几条消息发送"],
    ["max_actions_per_message", "一次最多执行几个动作", "一条消息里允许的动作数量"],
    ["plan_valid_duration", "计划有效期（秒）", "一次 LLM 计划覆盖多长时间，默认 1800（30 分钟）"],
    ["max_action_chain_depth", "动作链最大深度", "防止动作套动作无限循环"],
    ["llm_plan_min_interval_seconds", "问计划的间隔（秒）", "两次「问大模型要计划」之间至少隔多久，默认 900"],
    ["max_llm_plan_per_hour", "每小时最多计划决策次数", "降低 token 消耗"],
    ["max_llm_text_per_hour", "每小时最多生成发言次数", "超出后她这一轮保持安静（不会再退到一句写死的通用台词）"],
    [
      "max_arrival_decisions_per_hour",
      "每小时最多抵达决策次数",
      "她走到一个新地方后立刻做一次决策的上限；这类决策不占自主行动额度，单独限流",
    ],
    [
      "max_tool_param_per_hour",
      "每小时最多补全工具参数次数",
      "「把意图翻译成工具参数」的辅助模型调用上限，超出后这类动作会跳过",
    ],
    ["forced_plan_min_interval_seconds", "极端保护最短间隔（秒）", "强制睡觉/强制找人这类兜底行为的最小间隔"],
  ].forEach(([key, label, hint]) => {
    limits._fields.appendChild(
      inputField(label, num(world.limits[key]), (value) => (world.limits[key] = num(value)), {
        hint,
        type: "number",
      }),
    );
  });
  form.appendChild(limits);

  /* --- 说话节奏 --- */
  const style = settingsSection(
    "说话节奏",
    "她一句一句发消息时的停顿，以及「最近话太密」的判定。",
    "群里分段回复如果同一瞬间全冒出来，一眼就能看出是机器；按字数停一下读起来才像人在打字。",
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
      num(world.reply_style.typing_delay_per_char, 0.03),
      (value) => (world.reply_style.typing_delay_per_char = num(value, 0.03)),
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
      num(world.reply_style.typing_delay_max, 2.5),
      (value) => (world.reply_style.typing_delay_max = num(value, 2.5)),
      {
        hint: "上限。长句子不会一直等下去，默认 2.5 秒。",
        type: "number",
        step: "0.5",
        min: 0,
      },
    ),
  );
  style._fields.appendChild(
    pillsField(
      "回复时引用触发她的消息",
      world.reply_style.quote_mode || "smart",
      [
        { key: "off", label: "不引用", hint: "任何情况下都不带引用段" },
        { key: "always", label: "总是引用", hint: "每次回复的第一条都引用触发她的那条消息" },
        {
          key: "smart",
          label: "智能",
          hint: "这一轮要回一串消息（她还在回上一条时又来了新的）才引用，单独一句对答不引用",
        },
      ],
      (value) => {
        world.reply_style.quote_mode = value;
        markDirty();
        renderSettings();
      },
      {
        hint: "她的话或图片的第一条带引用段。仅在 QQ / OneBot 这类平台生效，其他平台自动跳过。",
      },
    ),
  );
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
          "收到消息先等这么久再开口：期间每来一条新消息就重新计时，连着打字就等他说完一起回。" +
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

  /* --- 无人回应保护 --- */
  const engagement = settingsSection(
    "没人理她时怎么办",
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
      "睡觉时被叫怎么办",
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
          "开启后：群里随便一句「起床了」不会把她弄醒，必须 @ 她（私聊等同）。\n" +
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
        hint: "同一段对话攒到这么多条（含她自己说的话）就总结一次。调小=记得更碎、调大=更省调用。",
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
  [
    ["micro_per_hour", "微事件：每小时几次", 1, "只影响她自己的状态和记忆，群里不主动说。默认 1 次/小时。"],
    ["small_per_hour", "小事件：每小时几次", 0.3, "她要做个选择、会掷骰。默认 0.3 次/小时（约三个小时一次）。"],
    ["big_per_hour", "大事件：每小时几次", 0.02, "会说、可能需要找人商量。默认 0.02 次/小时（约两天一次）。"],
  ].forEach(([key, label, fallback, hint]) => {
    eventSection._fields.appendChild(
      inputField(
        label,
        world.events[key] ?? fallback,
        (value) => (world.events[key] = Number(value) || 0),
        { hint, type: "number", min: "0", max: "12", step: "0.05" },
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
  [
    ["share_micro", "微事件", "silent", "默认 silent：只进状态和记忆，不出声。"],
    ["share_small", "小事件", "nodes", "默认 nodes：只在开场和收尾那两幕说，中间静默推演。"],
    ["share_big", "大事件", "always", "默认 always：大事件每一幕都可以说。"],
  ].forEach(([key, label, fallback, hint]) => {
    eventSection._fields.appendChild(
      inputField(
        `${label}：要不要说出来`,
        world.events[key] || fallback,
        (value) => (world.events[key] = value),
        {
          hint: `${hint} silent = 从不说；nodes = 只在开场 / 收尾说；always = 每幕都能说。`,
          placeholder: "silent / nodes / always",
        },
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
      num(world.events.photo_chance, 0.3),
      (value) => (world.events.photo_chance = num(value, 0.3)),
      {
        hint:
          "事件每推进一幕 / 完结时，按这个概率让她拍一张（自拍或拍照），图片会带上这件事的内容。" +
          "默认 0.3；填 0 = 关闭。",
        type: "number",
        min: "0",
        max: "1",
        step: "0.05",
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
      { hint: "关掉 = 挂起的事完全不进提示词（她还是记得，只是不会主动提）。" },
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

  /* --- 上下文 --- */
  world.context = world.context || {};
  const contextSection = settingsSection(
    "上下文",
    "群聊记录怎么留档、带多少进提示词、超了怎么办。",
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
    inputField(
      "别处同时听到的带多少行",
      num(world.context.chat_elsewhere_lines, 12),
      (value) => {
        world.context.chat_elsewhere_lines = Math.max(1, Math.round(num(value, 12)));
      },
      {
        hint: "同一个她在别的群 / 私聊里说的话，只当背景；行数越多提示词越长。",
        type: "number",
        min: 1,
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
      "留档超了怎么办",
      world.context.chat_overflow || "discard",
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
  if ((world.context.chat_image_inline || "auto") !== "never") {
    contextSection._fields.appendChild(
      inputField(
        "聊天记录最多带几张图",
        num(world.context.chat_image_max, 1),
        (value) =>
          (world.context.chat_image_max = Math.max(
            1,
            Math.round(num(value, 1)),
          )),
        {
        hint:
          "按时间取最近几张。带过去的图会在聊天记录里标成「（见图1）」，没带过去的旧图转述成文字。",
          type: "number",
          min: 1,
        },
      ),
    );
  }
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
        { key: "on", label: "开", hint: "小睡做要点化 + 刷新缩略版 + 一条梦，不碰关系与事实。" },
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
        { key: "on", label: "开", hint: "他做了让你气着的事（答应的事没做、放鸽子…），记下来。" },
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
    ["一件记几天", "own_topic_days", 3, "到期丢掉：要么早做完了，要么她其实不在意。", "1"],
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
    "配了「图片转述模型」（插件配置里那个）之后，群里发的图会先转成文字再进上下文。",
    "分两步：先「看图」写成画面描述，再用纯文本补一句「和话题的关系」。" +
      "看图这步与话题无关，所以能按图片缓存、同一张表情包只识别一次。",
  );
  const visionFull = el("div", "full");
  visionFull.appendChild(
    textareaField(
      "看图提示词",
      world.vision.prompt || "",
      (value) => (world.vision.prompt = value),
      {
        hint: "留空用内置默认（推荐）。默认要求输出「画面描述｜类型｜文字」，并照抄图里的关键文字。",
        rows: 8,
        placeholder: DEFAULT_CAPTION_PROMPT,
        onRestore: () => (ui.defaults.captions || {}).look || DEFAULT_CAPTION_PROMPT,
      },
    ),
  );
  visionFull.appendChild(
    textareaField(
      "关系提示词",
      world.vision.relation_prompt || "",
      (value) => (world.vision.relation_prompt = value),
      {
        hint: "第二步用：拿转述 + 当前消息 + 最近群聊写一句「与话题的关系」。不带图，可用便宜的文本模型。",
        rows: 4,
        placeholder: DEFAULT_CAPTION_RELATION_PROMPT,
        onRestore: () =>
          (ui.defaults.captions || {}).relation || DEFAULT_CAPTION_RELATION_PROMPT,
      },
    ),
  );
  visionSection._fields.appendChild(visionFull);
  visionSection._fields.appendChild(
    checkboxField(
      "补一句「与话题的关系」",
      world.vision.relation_enabled !== false,
      (value) => (world.vision.relation_enabled = value),
      {
        hint: "开启后多一次纯文本调用，她会知道这张图和正在聊的事有什么关系；关闭则交给主模型自行判断。",
      },
    ),
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
    const forwardFull = el("div", "full");
    forwardFull.appendChild(
      textareaField(
        "转发摘要提示词",
        world.vision.forward_prompt || "",
        (value) => (world.vision.forward_prompt = value),
        {
          hint: "留空用内置默认（推荐）。默认要求写清谁和谁在聊、事情与结论、图上有用的信息，不分点不换行。",
          rows: 6,
          placeholder: DEFAULT_FORWARD_PROMPT,
          onRestore: () =>
            (ui.defaults.captions || {}).forward || DEFAULT_FORWARD_PROMPT,
        },
      ),
    );
    visionSection._fields.appendChild(forwardFull);
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
      toast("生成好了，记得点右上角保存");
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
    "命中屏蔽词的消息不会进入世界（也不会注入提示词）。",
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
      toast("已应用到编辑器，记得点右上角「保存」");
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
  applySettingsTabs();
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
    hint: "当前配置会先自动备份一份。只勾中的部分会被替换，其它保持你现在这套。",
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
          "还没有会话。去群里（或私聊）对她说一句「/vw session add」，" +
            "她收到之后点下面的「重新获取」就会出现在这里。",
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
      row.appendChild(
        el("span", "", `${item.session_id}${item.note ? `（${item.note}）` : ""}`),
      );
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
        ? `勾上的才算「她在这儿生活」。会话组（${groups.join("、")}）在「会话」页里管，` +
            "组里的会话共享状态和记忆。"
        : "勾上的才算「她在这儿生活」；想让几个会话共享状态和记忆，去「会话」页建一个会话组。",
    ),
  );

  const row = el("div", "row");
  const again = el("button", "small primary", "重新获取");
  again.type = "button";
  again.title = "让 bot 加完会话之后点这里，把最新的白名单拉回来";
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
    inputField("她的名字（Bot 名称）", world.bot_name || "", (value) => {
      world.bot_name = value;
      markDirty();
    }, { hint: "互动动作里的「{bot}」会换成它。留空就先用群名片原名。", placeholder: "例如：小鲸鱼" }),
  );
  body.appendChild(
    pillsField("性别（决定文案里的称呼）", world.gender || "female", GENDERS, (value) => {
      world.gender = value;
      markDirty();
      applyPronoun($("app"));
    }),
  );
  body.appendChild(
    selectField("人设从哪来", world.persona.mode || "astrbot", [
      { key: "astrbot", label: "跟随 AstrBot（每个会话各自的人格）" },
      { key: "plugin", label: "用下面这一份（所有会话共用）" },
      { key: "append", label: "AstrBot 那份 + 下面这一份（接在后面）" },
    ], (value) => {
      world.persona.mode = value;
      markDirty();
    }, { hint: "想让她不随会话漂移就选「用下面这一份」。" }),
  );
  const area = document.createElement("textarea");
  area.rows = 8;
  area.value = world.persona.text || "";
  area.placeholder = "她是谁、怎么说话、在意什么…留空会回落到 AstrBot 那份，不会把人格弄没";
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

function wizardProviderStep(body) {
  const slots = ui.config.providers || {};
  const rows = [
    ["主模型", "llm", "说话用的那个"],
    ["打杂模型", "helper", "补工具参数、压上下文"],
    ["判断模型", "judge", "挑一个 / 打分这类轻决策"],
    ["看图模型", "vision", "把图片转成文字"],
    ["内容生成模型", "creator", "编她身边发生的事"],
    ["事件模型", "event", "写事件最后怎么样了"],
    ["睡眠整理模型", "consolidate", "消化记忆与画像"],
  ];
  const box = el("div", "wizard-list");
  rows.forEach(([label, key, note]) => {
    const row = el("div", "picker-row");
    row.appendChild(el("span", "", label));
    row.appendChild(el("span", "muted", note));
    row.appendChild(el("span", "wizard-provider", String(slots[key] || "（没读到）")));
    box.appendChild(row);
  });
  body.appendChild(box);
  body.appendChild(
    el(
      "p",
      "muted",
      "这些在 AstrBot 面板 → 插件 → 虚拟世界VW 里选；留空的会按括号里的规则回落到别的槽位，" +
        "所以这里全是回落值也能跑。想省钱就把打杂 / 判断换便宜快的模型。",
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
    }, { hint: "关掉就完全不记画像、也不往提示词里带。" }),
  );
  body.appendChild(
    checkboxField("睡觉时整理记忆与画像", world.profile.consolidate_enabled !== false, (value) => {
      world.profile.consolidate_enabled = value;
      markDirty();
    }, { hint: "睡下 20 分钟后整理一次；关掉就一直攒着不消化。" }),
  );
  body.appendChild(
    selectField("聊天留档超了怎么办", world.context.chat_overflow || "compress", [
      { key: "compress", label: "压成摘要（推荐）" },
      { key: "discard", label: "直接丢掉最早的" },
    ], (value) => {
      world.context.chat_overflow = value;
      markDirty();
    }, { hint: "压成摘要会保留「更早聊过什么」，她不会失忆；丢就是真的没了。" }),
  );
}

function wizardGenerateStep(body) {
  const world = ui.config.world;
  body.appendChild(
    el(
      "p",
      "muted",
      "这两样都要调一次模型生成，跳过也完全能用——想让她更像她的时候再回来做。",
    ),
  );
  // ① 简易人设：给打杂模型用的摘要，事件、整理都靠它
  const briefArea = document.createElement("textarea");
  briefArea.rows = 4;
  briefArea.placeholder = "还没有生成，点下面的按钮从主人设生成一段";
  briefArea.value =
    ($("persona-brief-text") && $("persona-brief-text").value) || "";
  const briefWrap = el("div", "field");
  briefWrap.appendChild(fieldHead("简易人设（给打杂模型）"));
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
  sampleWrap.appendChild(fieldHead("声音样例（口吻样本）"));
  sampleWrap.appendChild(
    el(
      "p",
      "muted",
      "按场景让她说几句，生成的结果先进候选池；之后在「她 / 他 / ta」那一页挑中的才会进提示词。",
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
  const world = ui.config.world;
  const steps = [
    {
      title: "她在哪儿生活",
      hint: "先告诉插件：她该在哪些群 / 私聊里出现。没选会话，后面配了也不会生效。",
      build: (body) => wizardSessionStep(body),
    },
    {
      title: "她是谁",
      hint: "名字、性别、角色卡。都可以先跳过——留空会沿用 AstrBot 那份人格。",
      build: (body) => wizardPersonaStep(body),
    },
    {
      title: "模型配了吗",
      hint: "这一步不改配置，只是让你看清插件现在用的是哪些模型。",
      build: (body) => wizardProviderStep(body),
    },
    {
      title: "调手感",
      hint: "不用逐个调参数：拖这几个滑块，它会同时改掉一组相关的设置。中间那档就是默认。",
      build: (body) => body.appendChild(knobEditor(world)),
    },
    {
      title: "记忆与画像",
      hint: "要不要记住每个人、要不要在她睡觉时消化这些经历。",
      build: (body) => wizardMemoryStep(body),
    },
    {
      title: "让她说话像自己（可跳过）",
      hint: "这两样要调模型生成，跳过也完全能用。",
      build: (body) => wizardGenerateStep(body),
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
  // 走完就藏起来了：留一句在哪儿能再打开，省得用户调过一次之后找不着
  card.appendChild(
    el(
      "p",
      "muted wizard-step",
      "以后想再走一遍：全局设置 → 基础 → 最上面的「重新运行设置向导…」",
    ),
  );
  const body = el("div", "wizard-body");
  card.appendChild(body);
  const foot = el("div", "wizard-foot");
  const back = el("button", "small ghost", "上一步");
  back.type = "button";
  const skip = el("button", "small ghost", "跳过这步");
  skip.type = "button";
  const next = el("button", "small primary", "下一步");
  next.type = "button";
  const done = el("button", "small ghost", "以后再说");
  done.type = "button";
  foot.appendChild(done);
  foot.appendChild(skip);
  foot.appendChild(back);
  foot.appendChild(next);
  card.appendChild(foot);
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);

  function close() {
    backdrop.remove();
  }

  function render() {
    const step = steps[index];
    title.textContent = step.title;
    stepNote.textContent = `第 ${index + 1} / ${steps.length} 步`;
    hintBox.textContent = step.hint || "";
    body.innerHTML = "";
    step.build(body, render);
    back.disabled = index === 0;
    skip.style.display = index === steps.length - 1 ? "none" : "";
    next.textContent = index === steps.length - 1 ? "完成" : "下一步";
  }

  async function finish() {
    ui.config.world.wizard_done = true;
    markDirty();
    close();
    toast("向导走完了，这就保存…");
    try {
      await saveAll();
      toast("已保存，可以开始用了");
    } catch (error) {
      toast(`保存失败：${error.message || error}（点右上角「保存」重试）`);
    }
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
  // 「以后再说」也标记已看过：不然每次打开都弹
  done.addEventListener("click", () => {
    ui.config.world.wizard_done = true;
    markDirty();
    close();
    toast("向导先关了；全局设置顶部有按钮可以再打开");
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
    const label = mode === "autonomous" ? "自主提示词" : "注入内容";
    summary.textContent = `${label} · ${Number(data.chars || prompt.length)} 字符`;
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

boot();
