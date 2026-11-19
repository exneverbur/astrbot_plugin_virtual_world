"""B1 语义检索的查询文本配方（纯函数：无 I/O、无 async、不 import engine/db）。

为什么单独成模块：查询文本构造错了会造成"主题自循环"——拿她自己（assistant）上一句
当查询，于是又召回她自己刚说过的话。配方只做三件事：
① 优先用**当前这条用户话**；② 太短（"嗯/好/哦/在吗/哈哈"这类）时从新到旧回溯最近的
**用户**实义话；③ 全程**不使用 assistant 轮次的内容**，找不到就返回空串，调用方据此
回退 A 行为（普通词法/中性排序检索）。
输出有硬上限 `max_chars`（默认 200 个 Python 字符），并配一个 sha256 缓存键与一个粗切
词提示，供向量短名单与词法短名单复用。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

# 配方版本：进入缓存键。查询文本构造规则一变就要改这里，否则旧向量缓存会被误用。
QUERY_RECIPE_VERSION = "q1"

# 少于 4 个"实义字符"（去掉空白、标点、emoji 后剩下的字母/数字/汉字）的整句按"太短"处理。
# 口径说明：纯应声（"嗯/好/哦/在吗/哈哈"）主要由 `STOPWORDS` + 整句切分规则拦，
# 长度只兜底"纯标点/纯 emoji/单字/两字"这种连词都算不上的输入。定 4 而不是更高，是因为
# 中文四五个字的正常短句非常常见（"月亮真圆啊""明天要下雨"），定太高会让这些轮次直接
# 跳过语义召回、退化成 A 或回看更早的话。
MIN_MEANINGFUL_CHARS = 4

# 查询文本硬上限（Python 字符数）。向量化前先截断，避免超长查询拖慢供应商或撑爆缓存。
MAX_QUERY_CHARS = 200

# 意图段里 purpose 自身的截断长度：再长也只是个提示，不该把主句挤没。
MAX_PURPOSE_CHARS = 40

_WHITESPACE_RUN = re.compile(r"\s+")
_TERM_TOKEN = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_PURE_DIGITS = re.compile(r"\d+")

# 角色归一：只有明确标成用户的轮次才可能进查询文本；assistant/未知一律不用。
_ROLE_USER = frozenset({"user", "human", "用户", "人", "本人"})
_ROLE_OTHER = frozenset({"assistant", "bot", "ai", "model", "system", "助手", "机器人", "系统"})

# 整句无实义的应声词条。查找时把文本归一成"只留字母数字（小写）"，再判"整句能否被这些
# 词条切干净"（贪心/DP 切分），所以这里只列基本形式，拼接与重复形式（"好的好的""嗯好的"
# "OKOKOK""好的谢谢"）都自动覆盖。
STOPWORDS: set[str] = {
    # —— 语气词/犹豫/沉吟：整句只有声音，没有内容 ——
    "嗯", "嗯嗯", "嗯呢", "唔", "哦", "哦哦", "噢", "喔", "呃", "额", "诶", "欸", "唉", "哎",
    "啊", "呀", "呐", "嘛", "emmm", "emmmm",
    # —— 语气助词：单独不成句，主要配合"整句切分"覆盖 "可以吗/好了吧" 这类 ——
    "吗", "呢", "吧", "啦", "了", "咯", "哟",
    # —— 肯定/否定/应答：只能确认，不能提供检索锚点 ——
    "好", "好的", "好啊", "好吧", "好嘞", "好滴", "行", "行的", "可以", "可以呀", "是", "是的",
    "对", "对的", "对对", "对对对", "没错", "没", "没有", "不是", "不行", "不用", "没事", "无所谓",
    # —— 礼貌/社交套话：有礼貌价值但没有话题价值 ——
    "谢谢", "多谢", "感谢", "辛苦", "辛苦了", "麻烦", "麻烦了", "不客气", "拜托",
    # —— 追问式应声：问的是"你在不在"，不是话题 ——
    "在", "在吗", "在么", "在嘛", "在不在",
    # —— 笑/敷衍/口令 ——
    "哈", "呵", "哈哈", "哈哈哈", "呵呵", "嘿嘿", "嘻嘻", "笑", "笑死", "233", "2333", "666",
    # —— 英文短应声（查找时统一小写，"OK" 保留只为人类核对方便）——
    "OK", "ok", "okay", "okk", "k", "yes", "no", "yeah", "yep", "nope", "thx", "thanks",
    "hi", "hello", "hey", "bye", "goodbye",
    # —— 纯标点串：兜底（真正的纯标点在 core 为空时已被长度判掉）——
    "?", "？", "??", "？？", "!", "！", "~", "～", "...", "…", "……", "。", "。。。", "，", ",",
}


def _normalize_text(text: Any) -> str:
    """合并连续空白、去控制字符、去首尾空白。"""
    value = str(text if text is not None else "")
    value = _WHITESPACE_RUN.sub(" ", value)
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C")
    return value.strip()


def _content_core(text: str) -> str:
    """只留字母与数字（含汉字）：空白、标点、emoji、符号都不算"实义字符"。"""
    return "".join(ch for ch in _normalize_text(text) if unicodedata.category(ch)[0] in ("L", "N"))


def _simplified_key(text: Any) -> str:
    """去空白后小写：用于整句等值匹配（标点停止词如 "？" 靠这条兜底）。"""
    return _WHITESPACE_RUN.sub("", str(text if text is not None else "")).lower()


def _is_stopword_only(core: str) -> bool:
    """整句是否完全由 `STOPWORDS` 词条拼成（"好的好的""嗯好的""OKOKOK""好的谢谢"）。

    用 DP 而不是单纯重复判定：既覆盖同一个词重复，也覆盖多词拼接；DP 保证只要存在
    一种切法就不算实义（贪心最长匹配在同词条互为前缀时会漏，所以直接上精确判定）。
    """
    key = core.lower()
    if not key:
        return False
    tokens = tuple(token for token in STOPWORDS if token)
    reachable = [False] * (len(key) + 1)
    reachable[0] = True
    for index in range(len(key)):
        if not reachable[index]:
            continue
        for token in tokens:
            if key.startswith(token, index):
                reachable[index + len(token)] = True
    return reachable[len(key)]


def is_meaningful(text: str) -> bool:
    """这句用户话是否够"实义"，能当检索查询。

    规则（按顺序）：
    1. 去掉空白后小写正好落在 `STOPWORDS`（覆盖 "？"/"。。。"/"..." 这类纯标点条目）→ 否；
    2. 去掉空白、标点、emoji 后的"实义字符"少于 `MIN_MEANINGFUL_CHARS`(4) → 否
       （纯标点、纯 emoji、单字/两字都在这条被清空）；
    3. 纯数字串且位数 ≤ 4（"1234"、"0000"）→ 否（没有话题信息量）。**口径**：5 位及以上的
       纯数字串仍算实义（用户可能在指某个编号），这里只按 brief 排除 ≤ 4 位；
    4. 整句能被 `STOPWORDS` 词条切干净（"好的好的""嗯好的""好的谢谢"）→ 否；
    5. 其余 → 是。
    """
    raw = str(text if text is not None else "")
    if _simplified_key(raw) in STOPWORDS:
        return False
    core = _content_core(raw)
    if len(core) < MIN_MEANINGFUL_CHARS:
        return False
    if len(core) <= 4 and _PURE_DIGITS.fullmatch(core):
        return False
    if _is_stopword_only(core):
        return False
    return True


def _normalize_role(role: Any) -> str:
    """把各种叫法归一成 'user' / 'other' / ''（未知）。"""
    token = str(role if role is not None else "").strip().lower()
    if not token:
        return ""
    if token in _ROLE_USER:
        return "user"
    if token in _ROLE_OTHER:
        return "other"
    return "other"


def _turn_role_text(turn: Any) -> tuple[str, str]:
    """容错读取一轮的 (role, text)。

    - dict：键 `role`/`role_name` 与 `text`/`content`；
    - 对象：同名属性；
    - 裸字符串：说话人不明，按规范此处**不用**（宁可少用，也不能把她的上一句当查询），
      调用方若要传裸文本请自己先标成用户轮次。
    """
    if isinstance(turn, str) or isinstance(turn, (bytes, bytearray)):
        return "", ""
    if isinstance(turn, Mapping):
        getter = turn.get
    else:
        def getter(key: str, default: Any = None) -> Any:  # type: ignore[misc]
            return getattr(turn, key, default)
    role = ""
    for key in ("role", "role_name"):
        value = getter(key)
        if value:
            role = _normalize_role(value)
            break
    text = ""
    for key in ("text", "content"):
        value = getter(key)
        if value:
            text = value if isinstance(value, str) else str(value)
            break
    return role, text


def _char_limit(max_chars: Any) -> int:
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        return MAX_QUERY_CHARS
    return max(0, limit)


def _intent_segment(purpose: Any) -> str:
    """意图提示段；purpose 为空就没有这一段。"""
    text = _normalize_text(purpose)
    if not text:
        return ""
    return f"（这轮想做的事：{text[:MAX_PURPOSE_CHARS]}）"


def topic_query_text(*, current_user_text: str = "", recent_turns: Sequence[Any] = (),
                     purpose: str = "", max_chars: int = MAX_QUERY_CHARS) -> str:
    """构造这一轮用于向量化的查询文本；返回 "" 表示没有可用查询文本（回退 A）。

    `recent_turns` 是**由旧到新**的一串轮次，每项是 dict 或对象（键/属性见 `_turn_role_text`）。
    规则：① 当前这条用户话有意义就用它；② 否则从新到旧扫用户轮次，取第一句 `is_meaningful`
    的；③ 全程不使用 assistant 轮次；④ 都没有 → ""。
    `purpose` 非空时追加 `（这轮想做的事：…）`（purpose 自身截断到 40 字）；追加后超限就先
    截主句、尽量保住意图段。返回值永远不超过 `max_chars` 个字符。
    """
    limit = _char_limit(max_chars)
    if limit <= 0:
        return ""
    main = _normalize_text(current_user_text)
    if not is_meaningful(main):
        main = ""
        # 从新到旧找最近的用户实义话；assistant/未知角色直接跳过（防止主题自循环）。
        for turn in reversed(tuple(recent_turns or ())):
            role, text = _turn_role_text(turn)
            if role != "user":
                continue
            candidate = _normalize_text(text)
            if is_meaningful(candidate):
                main = candidate
                break
    if not main:
        # 没有主句就不构造"只有意图段"的查询：那种文本没有话题信息量。
        return ""
    intent = _intent_segment(purpose)
    if not intent:
        return main[:limit]
    room = limit - len(intent)
    if room > 0:
        return main[:room] + intent
    # 连意图段都塞不下：主句让位，意图段自己截断（硬上限优先）。
    return intent[:limit]


def query_cache_key(text: str, *, provider_id: str = "", space_fingerprint: str = "") -> str:
    """查询文本的 sha256 缓存键（32 位十六进制）；空/纯空白文本返回 ""（不缓存）。"""
    value = str(text if text is not None else "")
    if not value.strip():
        return ""
    material = f"{QUERY_RECIPE_VERSION}\x1f{provider_id}\x1f{space_fingerprint}\x1f{value}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def query_terms_hint(text: str, *, limit: int = 12) -> tuple[str, ...]:
    """从查询文本粗切词条，供词法短名单复用：中文按 2-gram，英文/数字按空白与标点切。

    只做去重（大小写不敏感，保留首次出现的大小写）与截断，不引入分词依赖；
    单个汉字不成词，直接丢弃。
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 12
    if cap <= 0:
        return ()
    source = _normalize_text(text)
    terms: list[str] = []
    seen: set[str] = set()
    for token in _TERM_TOKEN.findall(source):
        # 中文按 2-gram 滑窗；英文/数字整词保留（单个汉字不成词，滑窗为空即丢弃）。
        pieces = [token[i:i + 2] for i in range(len(token) - 1)] if _CJK.match(token) else [token]
        for piece in pieces:
            key = piece.lower()
            if key in seen:
                continue
            seen.add(key)
            terms.append(piece)
            if len(terms) >= cap:
                return tuple(terms)
    return tuple(terms)
