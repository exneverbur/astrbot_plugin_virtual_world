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
  // 「数值」默认只读展示（彩色条），点「编辑数值」才切成滑杆
  valuesEdit: false,
  valueDraft: {},
  selectedSchedule: "",
  presets: [],
  activePreset: "",
  token: "",
  statusTimer: null,
  historyHours: 24,
  historyBusy: false,
  defaults: { actions: {}, captions: {} },
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
    hint:
      "情绪被激起的强度（不是开心程度）。越高，她的内心活动越翻涌、说出来的感情越浓、越容易做出亲昵或冲动的举动；" +
      "被夸、被抱、吵架、被冷落都会把它推高，然后随时间回落。",
  },
  {
    key: "valence",
    label: "效价",
    hint:
      "心情的好坏（0.5 是中性，越高越偏正面）。它有一个由精力 / 孤独 / 无聊 / 好奇推导的基线，" +
      "事件只在基线上产生短期偏移，过一阵会自己回落——所以\"今天心情不太好\"需要慢慢积累，不是一条消息就能翻转。",
  },
  { key: "boredom", label: "无聊", hint: "越高越想换个地方待着" },
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
    key: "nickname",
    icon: "🏷️",
    label: "群名片变化",
    hint: "她的群名片改成什么、有没有改失败。",
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
];

/** 调试输出的分组：类型多了以后按用途分块，找起来快。 */
const ECHO_GROUPS = [
  { key: "core", label: "决定与动作", hint: "她这一轮想做什么、做成了没有" },
  { key: "tool", label: "工具与指令", hint: "调用了哪个工具 / 指令，拿回了什么" },
  { key: "mind", label: "记忆与回想", hint: "记忆写成什么样、主动回想翻到了什么" },
  { key: "schedule", label: "日程", hint: "日程什么时候被触发、她怎么改自己的日程" },
  { key: "sleep", label: "睡眠与保护", hint: "睡觉门禁、被叫醒、打断、兜底保护" },
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
  schedule_edit: { icon: "🗓️", label: "改日程" },
  command: { icon: "🧩", label: "触发指令" },
  command_call: { icon: "🧩", label: "触发指令" },
  command_result: { icon: "📤", label: "指令返回" },
  tool_call: { icon: "🔧", label: "调用工具" },
  tool_result: { icon: "📥", label: "工具返回" },
  mood_reset: { icon: "🌤️", label: "心情缓过来" },
  storm: { icon: "🌩️", label: "情绪上头 / 平复" },
  poke: { icon: "👉", label: "戳一戳" },
  search_sources: { icon: "🔗", label: "检索来源" },
  weather: { icon: "🌤️", label: "天气" },
  search: { icon: "🌐", label: "联网搜索" },
  chain: { icon: "🔗", label: "动作链" },
  cold_start: { icon: "🌅", label: "冷启动" },
  bot_spoke: { icon: "🗣️", label: "发言等待回应" },
};

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
  ["intent", "打算怎么办"],
];

const STATES = [
  { key: "idle", label: "空闲" },
  { key: "awakening", label: "刚醒" },
  { key: "sleeping", label: "睡觉" },
  { key: "napping", label: "小睡" },
  { key: "staring", label: "发呆" },
  { key: "searching", label: "上网" },
  { key: "reading", label: "看书" },
  { key: "walking", label: "移动中" },
  { key: "thinking", label: "沉思" },
];

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
    hint:
      "触发别的插件的一条指令（例如 /天气）。她只说想干什么，插件把小模型拼好的指令交出去，" +
      "再把那条指令返回的内容交回给她说一句",
  },
];

const TRIGGERS = [
  {
    key: "none",
    label: "什么都不做",
    hint:
      "动作做完就结束。工具型 / 指令型动作还看全局设置里的「工具结果回话」：" +
      "那个开关开着时，拿到的结果会交回大模型说一句；想彻底不开口就把它关掉。",
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
    hint:
      "查东西专用：她可以一次给几条查询词 → 结果整理成带编号的证据 →（可选）用阅读工具抓正文 →" +
      "证据不够时再补查一轮 → 她照着证据讲，不许编。适合「上网搜索」这类动作。",
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

function setSaveState(text) {
  $("save-state").textContent = text || "";
}

/** 标记"有未保存的改动"（保存成功后会自动热加载）。 */
function markDirty() {
  ui.dirty = true;
  setSaveState("有未保存的改动");
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
    const next = text.replace(/她/g, replacement).replace(/(?<!其)他/g, replacement);
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

function selectField(label, value, choices, onChange, opts = {}) {
  const wrapper = el("label");
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

  const chosen = () => (Array.isArray(world.echo_types) ? world.echo_types : []);
  const setChosen = (names) => {
    world.echo_types = Array.from(new Set(names));
  };

  const toolbar = el("div", "echo-toolbar");
  const counted = el(
    "span",
    "hint",
    `已选 ${chosen().length} / ${ECHO_TYPE_CHOICES.length} 类`,
  );
  const buttons = el("div", "row-item");
  const quick = [
    ["全选", () => ECHO_TYPE_CHOICES.map((item) => item.key)],
    [
      "常用",
      () =>
        ECHO_TYPE_CHOICES.filter((item) =>
          ["plan", "action_start", "action_done", "action", "search", "tool_call", "tool_result", "skip"].includes(
            item.key,
          ),
        ).map((item) => item.key),
    ],
    ["清空", () => []],
  ];
  quick.forEach(([text, pick]) => {
    const button = el("button", "ghost", text);
    button.type = "button";
    button.addEventListener("click", () => {
      setChosen(pick());
      renderSettings();
    });
    buttons.appendChild(button);
  });
  toolbar.appendChild(counted);
  toolbar.appendChild(buttons);

  /** 每一类一个可点的小卡片：图标 + 名字，说明放在悬停提示里。 */
  const typeButton = (choice) => {
    const box = el("button", `echo-chip${chosen().includes(choice.key) ? " on" : ""}`);
    box.type = "button";
    box.appendChild(el("span", "echo-icon", choice.icon));
    box.appendChild(el("span", "", choice.label));
    if (choice.hint) {
      box.setAttribute("data-tip", choice.hint);
      box.setAttribute("title", choice.hint);
    }
    box.addEventListener("click", () => {
      const next = chosen().filter((name) => name !== choice.key);
      setChosen(chosen().includes(choice.key) ? next : next.concat([choice.key]));
      renderSettings();
    });
    return box;
  };

  const groups = el("div", "echo-groups");
  ECHO_GROUPS.forEach((group) => {
    const items = ECHO_TYPE_CHOICES.filter((item) => (item.group || "core") === group.key);
    if (!items.length) return;
    const block = el("div", "echo-group");
    const head = el("div", "echo-group-head");
    const title = el("span", "echo-group-title", group.label);
    if (group.hint) {
      title.setAttribute("data-tip", group.hint);
      title.setAttribute("title", group.hint);
    }
    head.appendChild(title);
    const onCount = items.filter((item) => chosen().includes(item.key)).length;
    head.appendChild(el("span", "hint", `${onCount}/${items.length}`));
    const toggle = el("button", "ghost tiny", onCount === items.length ? "取消本组" : "全选本组");
    toggle.type = "button";
    toggle.addEventListener("click", () => {
      const keys = items.map((item) => item.key);
      const rest = chosen().filter((name) => !keys.includes(name));
      setChosen(onCount === items.length ? rest : rest.concat(keys));
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
    checkboxField(
      "精简模式",
      !!world.echo_compact,
      (value) => {
        world.echo_compact = !!value;
      },
      {
        hint:
          "打开后只发要点，不带参数和结果：\n" +
          "🔧 调用「anysearch_extract」\n" +
          "📥 「anysearch_extract」返回\n" +
          "排查「她做了什么」够用，内容不会刷屏。",
      },
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
    items: items.map((item) => ({ ...item })),
    chosen: new Set(selected),
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
    if (field.type === "number") {
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
  onSubmit,
  onDismiss,
}) {
  const state = {};
  dialogState = { custom: true, values: state, onSubmit, onDismiss };
  $("dialog-title").textContent = title;
  $("dialog-hint").textContent = hint || "";
  $("dialog-ok").textContent = confirmText;
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
  const selected = Array.isArray(values) ? values : [];
  const text = el(
    "div",
    `picker-text${selected.length ? "" : " empty"}`,
    selected.length
      ? selected
          .map((id) => (items.find((item) => item.id === id) || { name: id }).name || id)
          .join("、")
      : opts.empty || "点击选择…",
  );
  button.appendChild(text);
  button.appendChild(el("span", "picker-caret", "▾"));
  button.title = selected.length
    ? selected
        .map((id) => (items.find((item) => item.id === id) || { name: id }).name || id)
        .join("、")
    : opts.empty || "点击选择…";
  button.addEventListener("click", () => {
    openPicker({
      title: label,
      hint: opts.hint,
      items,
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
      const item = items.find((entry) => entry.id === id);
      chips.appendChild(el("span", "chip-item", item ? item.name : id));
    });
    wrapper.appendChild(chips);
  }
  return wrapper;
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
      "按顺序执行。持续动作（睡觉、看书…）会先开始，完成后继续执行后面的步骤。" +
        "注意：地点是硬条件——要求「在书房」的动作，如果她当时不在书房，这一步会被跳过。" +
        "可以在这里补一步「移动到」，或者打开下面日程的「自动先走过去」。" +
        (opts.smart
          ? "（这条日程开了「智能日程」：到点由大模型给这几步补「想干什么」，这里不用填。）"
          : "工具型 / 指令型步骤要填「意图」——说清这一步想干什么，参数才会被补出来。"),
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
  bindTabs();
  bindButtons();
  renderHistoryWindows();
  watchPronoun();
  await loadAll();
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
    document.querySelectorAll("#tabs button").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    document.querySelectorAll(".tab").forEach((section) => {
      section.classList.toggle("active", section.id === `tab-${button.dataset.tab}`);
    });
    if (button.dataset.tab === "status") refreshStatus();
    if (button.dataset.tab === "memories") loadMemories();
    if (button.dataset.tab === "logs") loadLogs();
    if (button.dataset.tab === "map") loadOverview();
    if (button.dataset.tab === "debug") loadTools();
    if (button.dataset.tab === "presets") loadPresets();
  });
}

function bindButtons() {
  $("save").addEventListener("click", saveAll);

  $("status-refresh").addEventListener("click", refreshStatus);
  $("status-session").addEventListener("change", refreshStatus);
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
  $("action-filter").addEventListener("change", renderActionGrid);
  $("action-json").addEventListener("click", editActionsJson);
  $("action-drawer-close").addEventListener("click", closeActionDrawer);
  $("action-cancel").addEventListener("click", closeActionDrawer);
  // 点遮罩等同「取消」：草稿丢掉，不写回列表
  $("drawer-backdrop").addEventListener("click", closeActionDrawer);
  $("action-save").addEventListener("click", saveActionDraft);
  $("schedule-add").addEventListener("click", addSchedule);
  $("session-add").addEventListener("click", addSession);
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
  const missing = actions()
    .filter((action) => action.llm_level === "tool" && !actionToolNames(action).length)
    .map((action) => action.name || action.id);
  const noCommand = actions()
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
  const ids = ui.sessions.map((item) => item.session_id);
  [
    "status-session",
    "memory-session",
    "debug-session",
    "log-session",
    "map-session",
    "schedule-session",
  ].forEach((id) => {
    const select = $(id);
    const previous = select.value;
    select.innerHTML = "";
    if (!ids.length) {
      select.appendChild(option("", "（还没有白名单会话）"));
      return;
    }
    ids.forEach((sessionId) => select.appendChild(option(sessionId, sessionId)));
    select.value = ids.includes(previous) ? previous : ids[0];
  });
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
  ["status-time", "status-progress", "status-runtime"].forEach((id) => {
    const box = $(id);
    if (!box) return;
    box.innerHTML = "";
    statusLine(box, "", text, "muted");
  });
  $("status-warn").innerHTML = "";
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
  [
    `状态：${stateLabel}`,
    `心情：${data.mood}`,
    styleCell
      ? `这一轮：${cellLabels[styleCell] || styleCell}${
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
    `群聊里攒了 ${Number(data.chat_unreplied_count || 0)} 条没回（留档 ${
      data.chat_history_count || 0
    } 条）`,
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
    data = await apiGet("history", { session: sessionId, hours: ui.historyHours });
  } catch (error) {
    data = null;
  } finally {
    ui.historyBusy = false;
  }
  const points = (data && data.points) || [];
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
  const height = 160;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const pad = { left: 26, right: 8, top: 8, bottom: 16 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const style = getComputedStyle(document.body);
  const line = style.getPropertyValue("--line").trim() || "#e2e6ec";
  const muted = style.getPropertyValue("--muted").trim() || "#7a8798";

  ctx.strokeStyle = line;
  ctx.fillStyle = muted;
  ctx.font = "10px sans-serif";
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
    { key: "affect", color: "#5b7cfa", label: "心潮" },
    { key: "valence", color: "#e08c3c", label: "效价" },
  ];
  series.forEach((item) => {
    ctx.strokeStyle = item.color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = xOf(point.at);
      const y = yOf(num(point[item.key], 0.5));
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
  ctx.font = "10px sans-serif";
  let legendX = pad.left;
  series.forEach((item) => {
    ctx.fillStyle = item.color;
    ctx.fillRect(legendX, height - 10, 8, 3);
    ctx.fillStyle = muted;
    ctx.fillText(item.label, legendX + 12, height - 7);
    legendX += 46;
  });
}

async function refreshStatus() {
  const sessionId = $("status-session").value;
  if (!sessionId) {
    setStatusEmpty("还没有添加任何会话白名单。");
    return;
  }
  try {
    const data = await apiGet("state", { session: sessionId });
    ui.status = data;
    const stateLabel = (STATES.find((item) => item.key === data.state) || {}).label || data.state;
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
    const summary = String(data.chat_summary || "").trim();
    summaryBlock.textContent = summary
      ? `留档 ${Number(data.chat_history_count || 0)} 条；更早的群聊摘要：\n${summary}`
      : `留档 ${Number(data.chat_history_count || 0)} 条，暂无摘要。`;

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
  canvas.style.minWidth = `${Math.max(720, maxX + 200)}px`;
  container.style.minHeight = `${Math.max(380, maxY + 120)}px`;
  svg.setAttribute("width", canvas.style.minWidth);
  svg.setAttribute("height", container.style.minHeight);

  const byId = {};
  zones().forEach((zone) => {
    byId[zone.id] = zone;
  });

  const seen = {};
  zoneEdges().forEach((edge) => {
    const from = byId[edge.from_zone];
    const to = byId[edge.to_zone];
    if (!from || !to) return;
    const key = [edge.from_zone, edge.to_zone].sort().join("|");
    seen[key] = (seen[key] || 0) + 1;
    const offset = (seen[key] - 1) * 12;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", num(from.x) + 48);
    line.setAttribute("y1", num(from.y) + 20);
    line.setAttribute("x2", num(to.x) + 48);
    line.setAttribute("y2", num(to.y) + 20);
    line.setAttribute("stroke", "#7f8ea8");
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
  canvas.style.minWidth = `${Math.max(720, maxX + 240)}px`;
  container.style.minHeight = `${Math.max(380, maxY + 160)}px`;
  svg.setAttribute("width", canvas.style.minWidth);
  svg.setAttribute("height", container.style.minHeight);

  const byId = {};
  inside.forEach((node) => {
    byId[node.id] = node;
  });

  edges().forEach((edge) => {
    const from = byId[edge.from];
    const to = byId[edge.to];
    if (!from || !to) return;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", num(from.x) + 48);
    line.setAttribute("y1", num(from.y) + 20);
    line.setAttribute("x2", num(to.x) + 48);
    line.setAttribute("y2", num(to.y) + 20);
    line.setAttribute("stroke", "#9aa7bd");
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
      "让模型按这个区域的样子想几个新地点，自动摆位并接上区域内的路线。" +
        "生成结果只会列在弹窗里，改完、勾选之后才写进配置；动作请到每个地点里生成。" +
        "模型在插件配置的「内容生成模型」里选。",
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
      "让模型照着这个地点（以及它所属区域）的样子想几个动作，生成结果先列在弹窗里，" +
        "改完名字、勾选之后才写进配置。模型在插件配置的「内容生成模型」里选。",
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
        hint:
          "会自动摆好位置，并把它们连起来（第一个连到区域内最近的地点，之后依次相连，默认 1 tick）。" +
          "「确认加入」会直接写进配置并生效。" +
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
      "每条线自带两端的地点：例如「北门 ↔ 商场大门」。同一对区域可以有多条线" +
        "（公园的北门去商场、南门去学校）。她按最短路线自动挑一条走。",
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

  const tickInput = document.createElement("input");
  tickInput.type = "number";
  tickInput.min = "1";
  tickInput.value = num(edge.ticks, 1);
  tickInput.title = "跨过去要花几个 tick";
  tickInput.addEventListener("change", () => {
    edge.ticks = Math.max(1, Math.round(num(tickInput.value, 1)));
    markDirty();
    renderMap();
  });
  line.appendChild(tickInput);

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
    hint:
      "跨区连线是一条「门户对」：两端各自指定一个具体地点（例如公园·北门 ↔ 商场·大门）。" +
      "同一对区域可以有多条线，她走路时会自动挑最近的那条。",
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
      hint:
        "她人在这个地点、又没在做带文案的动作时，群名片上显示什么（例如「在书房」）。\n" +
        "留空就不改名片。文案写在地点上：换预设时地点跟着换，名片也就配套了。",
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
    hint:
      "整段替换地图结构：zones（区域）/ zone_edges（跨区连线，两端各自带地点）/ " +
      "nodes（地点）/ edges（区域内连线）。不含全局设置、动作和日程，写错不会影响它们。",
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
    tipBox(
      "填了之后这个参数每次都用你写的值，不再让模型猜。" +
        "适合「参数写着可选、实现却必须要」的工具，例如查天气固定 city=武汉。",
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
      placeholder: "例如 互动（对人）",
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
          hint:
            "她正在做这个动作时，群名片上显示什么（例如「做饭中」）。留空就用「状态 → 文案」的兜底映射。\n" +
            "写在动作里而不是全局设置里：换预设时动作跟着换，文案也就配套了。",
          placeholder: "例如 做饭中",
        },
      ),
    );
    form.appendChild(durationBox);
  }

  if (action.llm_level === "template") {
    form.appendChild(
      inputField("模板文案", action.template || "", (value) => (action.template = value), {
        hint:
          "不调用大模型时直接发到群里的固定文案。可用占位符：{bot}=她自己、{user}=目标群友、{node}=当前地点，例如「（{bot}抱了你一下）」。",
        placeholder: "例如：（伸了个懒腰）",
      }),
    );
  }

  if (isTool) {
    const toolBox = el("div", "subsection");
    const toolTitle = el("div", "sub-title");
    toolTitle.appendChild(el("span", "", "工具设置"));
    toolTitle.appendChild(
      tipBox(
        "工具的参数由工具自己定义（AstrBot 里已经写好了名称和说明）。她做这个动作时只需要说明想干什么，参数由插件用辅助模型补全，不需要你手写、也不用配默认值。",
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
          hint:
            "直接调用 = 把工具结果交回给她说一句。\n" +
            "联网检索 = 查东西专用的流水线：多条查询词 → 结果整理成证据 → 可选读正文 → 不够就补查。",
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
          hint:
            "工具型动作至少要选一个工具，而且是 AstrBot 里真实注册的那个。\n" +
            "**没选工具 = 这个动作会被跳过**（例如「上网搜索」「查天气」现在都不带默认工具，要先在这里挑一个能用的）。\n" +
            "选了多个就按顺序依次调用（比如先搜索、再把搜到的页面抓下来），结果一起交回给她。\n" +
            "**联网检索形态下只有第一个能用的搜索工具会被当作搜索入口**，它坏了会自动换下一个；" +
            "要抓网页正文请在下面的「阅读网页工具」里单独选。\n" +
            "运行时她只要说明「想干什么」，具体参数由辅助模型按每个工具自己的定义补全，" +
            "所以不用在这里配置默认参数。",
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
          hint:
            "选了多个工具时怎么用：\n" +
            "按顺序都调 = 每个都调一遍，结果合并（例如先搜索、再抓正文）。\n" +
            "依次尝试 = 只用一个，按顺序挑第一个能用的；失败了换下一个。\n" +
            "智能选择 = 让辅助模型按她的意图挑一个（挑工具和补参数一次完成），" +
            "失败先补参数重试，仍失败就换下一个。",
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
    if (!currentTools.length) {
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
          "她把要查的写进 intent，也可以一次给几条 queries 分角度查。\n" +
            "插件把搜索工具返回的内容整理成带编号的证据交给她，证据里没有的她会直说没查到。",
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
            hint:
              "能传网址、返回正文的工具（例如把网页转成 markdown 的那种）。\n" +
              "配了它，插件会挑搜索结果里最靠前的几篇抓正文再交给她；留空就只用搜索摘要。\n" +
              "注意「检索深度」要选「标准」或「深挖」才会读正文——「快查」档只看摘要。\n" +
              "同一篇网页 6 小时内不会重复抓。",
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
          hint:
            "决定这一趟查多远，**配的是上限**：快查只查一轮不读正文；标准读前两篇、不够补查一轮；" +
              "深挖至少读三篇、最多补查两轮。她可以在动作里写更浅的档位（例如简单问题自己选快查），" +
              "但不能超过这里。",
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
            hint:
              "这个动作「固定查什么」。日程里调用它、又没写意图时就用它当查询词" +
              "（例如动作叫「搜索新闻」，主题写「今日新闻热点」）。\n" +
              "优先级：她自己写的意图 > 这里 > 查询模板 > 让她按当时的处境和群里的话题自己说一句 > " +
              "中性兜底（「今天有什么新鲜事」）。\n" +
              "留空也能跑：规则触发（好奇心、日程）时她会自己想一句；配了主题则更稳定、也省一次调用。",
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
            hint:
              "把主题套成固定格式，可用占位符 {topic} 和 {date}。\n" +
              "例如 {date} 新闻热点 → 2026-09-17 新闻热点。",
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
          hint:
            "写清这条指令需要什么参数、怎么给，例如「city：城市名，例如 武汉」。" +
            "辅助模型只按这里的说明和她的意图拼参数，所以写得越清楚越不容易拼错。",
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
      "动作结束后要做什么。默认什么都不做；「接着说一句」会让大模型把结果讲成人话。" +
        "工具型和指令型动作还会受全局设置里的「工具结果回话」影响，结果里的图片会一起交给模型看。",
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
        hint:
          "关掉表示静默执行（例如换位置、发呆）。" +
          "「单轮」动作例外：它的定义就是让大模型说一句，只要目标是「群」或「某个群友」，" +
          "生成的话一定会发出去（想让它纯粹变成内心活动，用「想事情」那种动作）。",
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
  builtin: "内置（引擎专用，只能停用）",
  interact: "互动（对人）",
  express: "表达（说话 / 分享）",
  life: "生活（睡觉 / 看书 / 做饭）",
  tool: "工具（调用 AstrBot 工具）",
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

function visibleActions() {
  const keyword = String(($("action-search") || {}).value || "").trim().toLowerCase();
  const filter = ($("action-filter") || {}).value || "";
  return actions().filter((action) => {
    if (filter && groupOfAction(action) !== filter) return false;
    if (!keyword) return true;
    const haystack = [
      action.id,
      action.name,
      action.description,
      actionToolNames(action).join(" "),
      groupOfAction(action),
    ]
      .map((item) => String(item || "").toLowerCase())
      .join(" ");
    return haystack.includes(keyword);
  });
}

function refreshActionFilterOptions() {
  const select = $("action-filter");
  if (!select) return;
  const current = select.value;
  const groups = Array.from(new Set(actions().map((action) => groupOfAction(action))));
  select.innerHTML = "";
  select.appendChild(option("", "全部分组"));
  groups.sort().forEach((group) => select.appendChild(option(group, group)));
  if (groups.includes(current)) select.value = current;
}

function renderActionGrid() {
  refreshActionFilterOptions();
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
    return;
  }
  list.forEach((action) => grid.appendChild(actionCard(action)));
}

function actionCard(action) {
  const card = el("div", "action-card");
  if (action.enabled === false) card.classList.add("off");

  const head = el("div", "card-head");
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = action.enabled !== false;
  toggle.title = "停用后等于她根本没有这个动作";
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
  card.appendChild(head);

  const meta = el("div", "card-meta");
  meta.appendChild(el("span", "", groupOfAction(action)));
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
  remove.title = action.builtin
    ? "内置动作只能停用、不能删除（关掉左上角的开关即可）"
    : "删除";
  if (action.builtin) {
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

function renderScheduleList() {
  const list = $("schedule-list");
  list.innerHTML = "";
  schedules().forEach((schedule) => {
    const item = el("div", "list-item");
    if (schedule.id === ui.selectedSchedule) item.classList.add("selected");
    const info = el("div", "list-main");
    info.appendChild(
      el("div", "", `${schedule.enabled === false ? "⛔" : "✅"} ${schedule.time}　${schedule.id}`),
    );
    info.appendChild(
      el(
        "div",
        "meta",
        ((schedule.action_chain || []).map((step) => step.type).join(" → ") || "（没有动作）") +
          (schedule.auto_travel ? "　🚶 自动前往" : ""),
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
  form.appendChild(weekdayField(schedule));
  form.appendChild(checkboxField("启用", schedule.enabled !== false, (value) => (schedule.enabled = value)));

  const conditionBox = el("div", "subsection");
  const conditionTitle = el("div", "sub-title");
  conditionTitle.appendChild(el("span", "", "触发条件"));
  conditionTitle.appendChild(
    tipBox("满足这些条件才会执行；不满足就跳过这次（例如正在睡觉时不要执行早安）。"),
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
        hint:
          "开启后，如果某一步要求的地点不满足（例如在书房才能上网），她会先自己走到那个地点再执行。" +
          "关掉的话，地点不对的步骤会被直接跳过。",
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
        hint:
          "开启后，到点会把这条动作链交给大模型，让它给工具 / 指令型步骤写一句「这一步想干什么」，" +
          "再**照原样执行**（步数、动作、时长都不改，只会参考当下的时间和状态）。" +
          "代价是每次到点多一次模型调用；关掉就用你在下面写死的意图。",
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
    const position = (ui.overview || []).find(
      (item) => item.session_id === session.session_id,
    );
    if (position) {
      const stateLabel =
        (STATES.find((item) => item.key === position.state) || {}).label || position.state;
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
  const wanted = ["", ...types];
  if (
    existing.length === wanted.length &&
    existing.every((value, index) => value === wanted[index])
  ) {
    return;
  }
  select.innerHTML = "";
  select.appendChild(option("", "全部"));
  types.forEach((type) => {
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
  $("log-detail").textContent = JSON.stringify(
    {
      id: event.id,
      world_time: event.world_time,
      time: formatClock(event.created_at),
      event_type: event.event_type,
      explanation: event.text,
      detail: event.detail,
    },
    null,
    2,
  );
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
          )} · 召回 ${memory.recall_count}`,
        ),
      );
      item.appendChild(info);
      const box = el("div", "inline");
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

function settingsSection(title, note, tipText) {
  const section = el("div", "card-section");
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
    inputField("Bot 名称", world.bot_name || "", (value) => (world.bot_name = value), {
      hint:
        "互动动作文案里的 {bot} 会替换成这个名字，例如「（小鲸鱼抱了你一下）」。留空时先用群名片原名，再回落到「她」。",
      placeholder: "例如：小鲸鱼",
    }),
  );
  const genderField = pillsField(
    "性别",
    world.gender || "female",
    GENDERS,
    (value) => {
      world.gender = value;
      renderSettings();
      applyPronoun($("app"));
    },
    { hint: "决定文案里的称呼：女→她、男→他、塑料袋→ta。界面文字和 /vw status 都会跟着变。" },
  );
  basic._fields.appendChild(genderField);
  basic._fields.appendChild(
    pillsField(
      "被 @ 时的回复方式",
      world.reply_mode || "takeover",
      [
        {
          key: "takeover",
          label: "接管回复（推荐）",
          hint:
            "本插件自己调大模型、按 JSON 动作执行并发送，主人格不会再重复回复。她说不出话或模型出错时自动交回主人格。注意：此时回复用的是插件配置里的 Provider（llm_provider_id），回复质量由它决定；若想用主人格的好模型说话，请切到「仅注入状态」。",
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
        hint:
          "让大模型在输出动作之前，先写下「我在哪 / 什么状态 / 什么心情 / 在和谁说话 / 打算怎么办」。草稿不会发到群里、不计入动作数量，但能明显减少「状态说错、答错对象」这类低级错误。关掉可以省一点 token。",
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
        hint:
          "这些 QQ 号也能用管理类指令（重载配置、重置状态、改群名片、跑日程、调试）。\n" +
          "AstrBot 自己的管理员始终可以用，所以不用担心把自己锁在外面。\n" +
          "输入 QQ 号后按回车添加。",
        placeholder: "输入 QQ 号后按回车",
        emptyText: "（没有额外管理员：只有 AstrBot 的管理员能用管理指令）",
      },
    ),
  );
  form.appendChild(basic);

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

  /* --- 状态变化速度 --- */
  const dynamics = settingsSection(
    "状态变化速度",
    "数值每分钟变化多少。默认值下，精力大约 11 小时掉到 0，孤独大约 20 小时涨满，心潮大约 50 分钟回落干净。",
    "觉得她太爱睡觉就调小精力衰减；觉得她太黏人就调小孤独增长。",
  );
  [
    ["energy_decay_per_min", "精力衰减/分钟", "越大越容易累"],
    ["loneliness_growth_per_min", "孤独增长/分钟", "越大越容易想找人"],
    ["curiosity_growth_per_min", "好奇增长/分钟", "越大越想上网查东西"],
    ["affect_decay_per_min", "心潮回落/分钟", "越大情绪平复得越快（默认 0.02 ≈ 50 分钟从满值回到平静）"],
    ["boredom_growth_per_min", "无聊增长/分钟", "越大越想换地方"],
    ["sleep_energy_recovery_per_min", "睡觉恢复精力/分钟", "越大睡一觉回得越多"],
    ["nap_energy_recovery_per_min", "小睡恢复精力/分钟", "小睡时的恢复速度"],
    ["atmosphere_multiplier", "地点氛围影响强度", "0 表示地点氛围完全不影响数值"],
  ].forEach(([key, label, hint]) => {
    dynamics._fields.appendChild(
      inputField(label, num(world.state_dynamics[key]), (value) => (world.state_dynamics[key] = num(value)), {
        hint,
        type: "number",
        step: "0.0001",
      }),
    );
  });
  form.appendChild(dynamics);

  /* --- 说话频率 --- */
  const limits = settingsSection(
    "说话频率与预算",
    "控制她多久主动说一次话、每次最多说几句，以及大模型的调用预算。",
    "空群里最容易出问题的就是「自言自语刷屏」，这里的上限就是干这个用的。",
  );
  [
    ["max_autonomous_per_hour", "每小时最多自主行动次数", "包括主动搭话、去搜索、换地方"],
    ["max_share_per_hour", "每小时最多分享次数", "「分享见闻」这类动作的上限"],
    ["max_messages_per_say", "每次最多说几句", "一次回复拆成几条消息发送"],
    ["max_actions_per_message", "一次最多执行几个动作", "一条消息里允许的动作数量"],
    ["plan_valid_duration", "计划有效期（秒）", "一次 LLM 计划覆盖多长时间，默认 1800（30 分钟）"],
    ["max_action_chain_depth", "动作链最大深度", "防止动作套动作无限循环"],
    ["llm_plan_min_interval_seconds", "问计划的间隔（秒）", "两次「问大模型要计划」之间至少隔多久，默认 900"],
    ["max_llm_plan_per_hour", "每小时最多计划决策次数", "降低 token 消耗"],
    ["max_llm_text_per_hour", "每小时最多生成发言次数", "超出后改用内置短句池（零成本）"],
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
        hint:
          "开启后，提示词末尾会多一段「这一轮的表达方式」：由心潮 × 效价两个数值决定" +
          "（几条、多长、能不能分段、要不要用动作代替说话）。\n" +
          "心情好的时候话多一点、心情差的时候话短一点，都是这一段在起作用。\n" +
          "关掉 = 完全交给人设：她任何心情下都按同一种风格说话。",
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
        hint:
          "她的决策意愿接近 0（很平静、不无聊、刚被冷落过）时的概率。0.05 = 5%。\n" +
          "调大 = 她更常被大模型安排事情（更费 token），调小 = 更常安安静静待着。",
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
        hint:
          "她的决策意愿接近 1（很孤独、很无聊、心潮很高）时的概率。0.4 = 40%。\n" +
          "实际概率在上下限之间按意愿线性插值；当前值可以在「实时状态」页看到。",
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
      num(world.decider.chat_window_minutes, 20),
      (value) => (world.decider.chat_window_minutes = num(value, 20)),
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
        hint:
          "睡着时被 @ 但没有唤醒词：\n" +
          "固定文案 = 只发下面那句模板，不调大模型（省钱，也不会让她熬夜聊天）；\n" +
          "完全不回 = 群里什么也不显示，同时挡掉主人格的回复；\n" +
          "照常回复 = 不拦，她照旧半睡半醒地聊天。",
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
        hint:
          "开启（默认）：没 @ 她的消息在她这里就被截住，后面的插件（例如意图路由）也不会执行——" +
          "省掉一次判断，也不会把睡着的她拖进对话。\n" +
          "关掉（方案 B）：只保证本插件不出声，其他插件照常处理这条消息。",
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
        hint:
          "只挡没 @ 她的：别人 @ 她还是会收到「她在睡觉」那句固定文案。\n" +
          "全挡：只有 /指令 和带唤醒词的 @ 能进来，其余一点动静都没有。",
      },
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
        hint:
          "开启后不再逐条记录「谁说了什么」，而是把一段对话攒起来，" +
          "到触发点时让大模型压成一句以她的视角写的记忆（例如「小明说他加班很累，我有点心疼」）。\n" +
          "关掉后聊天内容完全不进记忆，只留她做过的事和内心活动。",
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
        hint:
          "她走开时把刚才在那个地点聊的内容总结成一条记忆，挂在原来那个地点上，" +
          "下次回到那里就能想起来。",
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
        hint:
          "这里勾选的工具在提示词里会直接列出来，她任何地方都能调用。\n" +
          "只在一个地点用得上的工具（比如只在书房上网搜索）不用放这儿——把它绑到那个地点的动作上就行。",
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
        hint:
          "开启后，主人格在她当前地点能用的工具 = 这里的通用工具 + 当前地点可用动作绑定的工具，" +
          "避免「在卧室里上网搜索」。工具改成挂在动作上之后，这里已经不需要再单独勾一遍。",
      },
    ),
  );
  toolsSection._fields.appendChild(
    checkboxField(
      "工具结果交回大模型说一句",
      world.tool_result_reply !== false,
      (value) => (world.tool_result_reply = value),
      {
        hint:
          "工具调用完成后，把结果交回主模型，让她用自己的话说出来（而不是把原始结果直接贴到群里）。\n" +
          "关掉的话，工具结果只记进日志，动作完成后不额外说话，能省一次模型调用。",
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
        hint:
          "开启后：她答应了「去书房查新闻」却没写移动动作时，插件会补一步「移动」再执行那件事，" +
          "日志里会标注「已自动前往」。\n" +
          "关掉后：只有当前地点能做的事才会被执行，别的会被跳过——更像是「场景决定她能想到什么」。",
      },
    ),
  );
  form.appendChild(placeSection);

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
      "最多携带多少条聊天",
      num(world.decider.chat_max_messages, 12),
      (value) => (world.decider.chat_max_messages = num(value, 12)),
      {
        hint: "每轮提示词里带多少条最近的群聊。太大既费 token 又容易让她被带偏，10~15 条足够了。",
        type: "number",
        min: 1,
      },
    ),
  );
  contextSection._fields.appendChild(
    inputField(
      "多久内算「还热乎」",
      num(world.decider.chat_window_minutes, 20),
      (value) => (world.decider.chat_window_minutes = num(value, 20)),
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
      num(world.context.chat_history_max, 200),
      (value) => (world.context.chat_history_max = num(value, 200)),
      {
        hint: "保存在数据库里的原始群聊条数，重启后可以恢复。带进提示词的只有上面那一小份。",
        type: "number",
        min: 20,
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
          hint: "不额外调用模型，最省；代价是很久以前聊过什么就真的忘了。",
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
        num(world.context.chat_compress_threshold, 80),
        (value) => (world.context.chat_compress_threshold = num(value, 80)),
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
        num(world.context.summary_refresh_minutes, 60),
        (value) => (world.context.summary_refresh_minutes = num(value, 60)),
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
          "没配「图片转述模型」时，插件会把图片直接交给多模态主模型：" +
          "自上次回复以来收到的图片 + 这条消息自己的图，最多带这么多张。配了转述模型则走文字转述，不受这里影响。",
        type: "number",
        min: 1,
      },
    ),
  );
  form.appendChild(contextSection);

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
        hint:
          "留空就用内置默认（推荐）。默认要求输出「画面描述｜类型｜文字」，" +
          "并写明是不是表情包、什么梗、什么情绪；图里的文字会照抄关键句。\n" +
          "**不要在这里要求它写与话题的关系**——那部分由下面的提示词单独生成，写在这里就不能跨话题缓存了。",
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
        hint:
          "第二步用：拿上一步的转述 + 当前消息 + 最近群聊，写一句「与话题的关系」。\n" +
          "这一步**不带图**，所以很便宜（可以配一个便宜的文本模型来跑，见插件配置）。留空用内置默认。",
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
        hint:
          "开启后多一次纯文本调用，她会知道这张图和正在聊的事有什么关系。\n" +
          "关掉就只把画面/类型/文字交给主模型，由它自己判断关系（省一次调用）。",
      },
    ),
  );
  visionSection._fields.appendChild(
    checkboxField(
      "同一张图只识别一次（推荐）",
      world.vision.cache_enabled !== false,
      (value) => (world.vision.cache_enabled = value),
      {
        hint:
          "按图片内容缓存转述结果：表情包、梗图会反复出现，第二次起不再调用多模态模型。\n" +
          "缓存是持久的，重启插件后仍然有效；换话题重发同一张图时会复用它" +
          "「画面 / 类型 / 文字」，只重算「与话题的关系」。\n" +
          "多张图会合并成一次调用；模型没按格式输出时自动退回逐张。",
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
  form.appendChild(visionSection);

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
      { hint: "关掉之后不再后台查天气、也不写进提示词（她主动查天气照常可用）。" },
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
        hint: "默认 24 小时（超过一天就当过时了，不再写进提示词）。地图页横幅仍会显示，只是标着「很久以前」。",
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
        hint:
          "查询结果先交给小模型压成「城市｜温度｜天气｜湿度｜风力｜预报」，横幅和提示词都用这一行；" +
          "工具返回图片时会先调图片转述模型把图读成文字。关掉就直接存原文。",
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
    }, { hint: "{base} 是原名，{status} 是状态文案。例如改成「{base}（{status}）」。" }),
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

async function applyPreset(preset) {
  const ok = await confirmDialog({
    title: `应用预设「${preset.name || preset.id}」？`,
    message:
      "当前配置会先自动备份一份。应用后：地图 / 动作 / 日程 / 会话白名单换成这个预设的内容；" +
      "所有会话的位置、数值、计划、群聊留档会被清空。\n\n" +
      "记忆和日志不受影响；需要清理请去「记忆库」「日志」页自己删。",
    confirmText: "应用",
    danger: true,
  });
  if (!ok) return;
  try {
    const result = await apiPost("presets/apply", { id: preset.id, clear_state: true });
    toast(
      `已切换到「${preset.name || preset.id}」，清空状态 ${result.cleared_sessions || 0} 个会话` +
        ((result.warnings || []).length ? `；提醒：${result.warnings.join("；")}` : ""),
    );
    await loadAll();
    await loadPresets();
  } catch (error) {
    toast(error.message || "应用失败");
  }
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

async function editPresetJson(preset) {
  let text = "";
  try {
    const data = await apiGet("presets/json", { id: preset.id });
    text = JSON.stringify(data.preset || {}, null, 2);
  } catch (error) {
    toast(error.message || "读取失败");
    return;
  }
  openFormDialog({
    title: `预设 JSON：${preset.name || preset.id}`,
    hint: "整段内容都可以改；复制走就是导出，贴别人的进来就是导入。保存时会做结构校验并自动补回必需的内置动作。",
    wide: true,
    fields: [{ key: "json", label: "预设（JSON）", type: "textarea", value: text, rows: 20 }],
    confirmText: "保存",
    onSubmit: async (values) => {
      try {
        const result = await apiPost("presets/json", { id: preset.id, json: values.json });
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

async function loadPrompt(mode) {
  const session = $("debug-session").value;
  if (!session) {
    toast("先选择会话");
    return;
  }
  try {
    const data = await apiGet("prompt", { session, mode });
    // 先摆一份分段索引：提示词三四千字，出问题时一眼就能看出哪一段没进去
    const sections = data.sections || [];
    const head = sections.length
      ? `共 ${Number(data.chars || 0)} 字符，${sections.length} 段：\n` +
        sections.map((item) => `· ${item.title}：${item.chars} 字符`).join("\n") +
        "\n\n──────────── 以下是全文 ────────────\n"
      : "";
    $("debug-output").textContent = head + (data.prompt || "（空）");
  } catch (error) {
    toast(error.message || "读取失败");
  }
}

boot();
