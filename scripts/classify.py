#!/usr/bin/env python3
"""
分类 + 类内排序(阶段B 新增)。

职责(纯函数,可离线单测,不联网、不调模型):
  1. classify(item) —— 把一条素材归到 7 个分类之一(规则:category_hint + 来源 + 关键词)。
     规则拿不准的返回 "" ,由调用方决定是否用 GLM 兜底(留接口,默认归到 product 或最大命中类)。
  2. score(item) —— 客观信号打分(新鲜度衰减 / 来源权重 / 社区热度 points / Tavily relevance / 有无题图)。
  3. rank_within_categories(...) —— 每类按分排序取 Top N。

为什么排序是这次质量的胜负手:
  "每类挑最有用的十几条" 不是抓取问题是排序问题。70 候选靠 GLM 主观挑,量一大质量就掉。
  这里用客观信号兜底,把"最相关"从玄学变成可调旋钮(权重在 W 里,随时调)。
"""

import re
import time

# ── 7 个分类:code → (中文名, 颜色)。颜色与前端 C 映射对齐(网站版 HTML)。──
# 顺序即首页分类区块的展示顺序。
CATEGORIES = [
    ("llm",      "大模型",       "#4b3bff"),
    ("agent",    "Agent·智能体", "#0a8f6a"),
    ("product",  "产品与工具",   "#d6932b"),
    ("oss",      "开源发布",     "#c8472b"),
    ("research", "研究与论文",   "#7a52cc"),
    ("biz",      "融资与商业",   "#2b7fd6"),
    ("policy",   "政策与监管",   "#8a8d99"),
]
CAT_CODES = [c[0] for c in CATEGORIES]
CAT_LABEL = {c[0]: c[1] for c in CATEGORIES}
CAT_COLOR = {c[0]: c[2] for c in CATEGORIES}
DEFAULT_CAT = "product"   # 规则全不命中时的归属

# ── 分类关键词(小写匹配 title+content)。命中计分,取最高分类。──
# 一条只归一个主类(避免重复出现稀释)。
KEYWORDS = {
    "llm": [
        "gpt", "claude", "gemini", "llama", "qwen", "deepseek", "mistral",
        "大模型", "语言模型", "language model", "llm", "foundation model",
        "model release", "发布模型", "glm", "doubao", "豆包", "kimi", "文心",
        "frontier model", "multimodal", "多模态", "tokens", "context window",
    ],
    "agent": [
        "agent", "智能体", "agentic", "autonomous", "copilot", "assistant",
        "工具调用", "tool use", "mcp", "workflow", "automation", "自动化",
        "cowork", "operator", "computer use", "browsing agent",
    ],
    "product": [
        "launch", "推出", "上线", "发布产品", "feature", "app", "应用",
        "搜索", "search", "chatbot", "界面", "ui", "用户", "consumer",
        "update", "rolls out", "重做", "redesign", "新功能",
    ],
    "oss": [
        "open source", "开源", "open-source", "open weights", "开放权重",
        "github", "hugging face", "huggingface", "apache", "mit license",
        "权重", "self-host", "私有化部署", "本地部署",
    ],
    "research": [
        "research", "研究", "paper", "论文", "arxiv", "benchmark", "评测",
        "breakthrough", "突破", "study", "实验", "deepmind", "训练方法",
        "scaling", "推理", "reasoning", "数据集", "dataset",
    ],
    "biz": [
        "funding", "融资", "raises", "raised", "million", "billion", "亿",
        "investment", "投资", "valuation", "估值", "ipo", "收购", "acquire",
        "acquisition", "revenue", "营收", "startup", "创业", "商业", "deal",
        "round", "venture", "vc",
    ],
    "policy": [
        "regulation", "监管", "policy", "政策", "law", "法案", "legislation",
        "ban", "禁令", "lawsuit", "诉讼", "court", "法院", "compliance", "合规",
        "antitrust", "反垄断", "executive order", "白宫", "eu ai act", "版权",
        "copyright", "privacy", "隐私", "safety regulation", "government", "政府",
        "investigation", "investigates", "probe", "sue", "sued", "attorney general",
        "subpoena", "起诉", "调查", "传票",
    ],
}

# 来源 → 倾向分类(官博通常一手,给个强先验;不是硬规则,仍可被强关键词覆盖)
MEDIA_HINT = {
    "OpenAI": "llm", "Anthropic": "agent", "Google DeepMind": "research",
    "Hugging Face": "oss", "arXiv cs.AI": "research", "arXiv cs.CL": "research",
    "36氪": "biz",
}

# ── 排序信号权重(可调旋钮)──
W = {
    "freshness": 3.0,    # 新鲜度满分权重(按小时衰减)
    "source":    2.0,    # 来源权重满分
    "points":    2.0,    # 社区热度(HN points)满分;暂无 HN 源时恒 0
    "relevance": 1.5,    # Tavily relevance(0~1)满分
    "image":     0.6,    # 有题图加分(配图是产品核心需求,轻微偏好)
}
HALF_LIFE_HOURS = 24.0   # 新鲜度半衰期:24 小时分数减半

# 一线源白名单(高来源权重)。与 generate_digest.TIER1_DOMAINS 思路一致,这里按"来源名/域名"判。
TIER1 = {
    "openai", "anthropic", "google deepmind", "deepmind", "blog.google",
    "bloomberg", "reuters", "techcrunch", "the verge", "theverge",
    "ars technica", "arstechnica", "venturebeat", "wired", "hugging face",
    "huggingface", "technologyreview", "the information", "theinformation",
    "semianalysis",
}
# 明显低质 vendor / SEO 站(降权)。命中即压到底。
LOWQ = {"boltinsurance", "vlasti", "admin5", "ipkd"}

# ── 动作概念(双语)── 用于:① 事件级去重的"动作签名" ② 分类的强信号。
# 同一事件常被中英多源报道(Anthropic retires ↔ Anthropic 停用),
# 单靠标题分词跨语言匹配不到;用"实体 + 动作概念"双语签名才抓得住。
ACTION_CONCEPTS = {
    "acquire":  ["acquire", "acquisition", "acquired", "acquires", "buys", "买下", "收购", "并购"],
    "funding":  ["funding", "raises", "raised", "investment", "valuation", "series a",
                 "series b", "series c", "ipo", "融资", "投资", "估值", "上市"],
    "launch":   ["launch", "launches", "release", "releases", "released", "unveil",
                 "unveils", "rolls out", "introduce", "introduces", "推出", "发布", "上线", "亮相"],
    "retire":   ["retire", "retires", "deprecate", "deprecates", "shut down", "shuts down",
                 "discontinue", "sunset", "停用", "下架", "弃用", "关停", "停服", "退役"],
    "lawsuit":  ["lawsuit", "sue", "sues", "sued", "investigation", "investigates", "probe",
                 "antitrust", "subpoena", "attorney general", " ags ", "诉讼", "起诉", "调查",
                 "反垄断", "传票"],
    "regulate": ["regulation", "regulate", "ban ", "bans ", "legislation", "executive order",
                 "监管", "禁令", "法案", "立法"],
    "partner":  ["partnership", "teams up", "collaborate", "collaboration", "携手", "联手", "合作"],
    "layoff":   ["layoff", "layoffs", "lays off", "job cuts", "裁员"],
}
# 动作概念 → 分类(强先验,只放歧义小的;launch/retire 留给关键词,避免误判模型发布)
ACTION_TO_CAT = {"acquire": "biz", "funding": "biz", "lawsuit": "policy", "regulate": "policy"}

# AI 实体白名单(品牌/产品名)。作 AI 相关性闸 + 事件去重的实体签名。
AI_ENTITIES = [
    "openai", "anthropic", "google deepmind", "deepmind", "microsoft", "nvidia",
    "meta ai", "perplexity", "midjourney", "stability ai", "cohere", "mistral",
    "hugging face", "huggingface", "databricks", "scale ai", "runway",
    "chatgpt", "gpt", "claude", "gemini", "llama", "qwen", "deepseek", "doubao",
    "豆包", "kimi", "文心", "通义", "智谱", "sora", "copilot",
]
_AI_TERM_RE = re.compile(
    r"\bai\b|\bais\b|a\.i\.|artificial intelligence|machine learning|deep learning|"
    r"neural network|generative|large language model|\bllms?\b|chatbot|agentic|\bmodel\b|"
    r"人工智能|机器学习|深度学习|神经网络|大模型|语言模型|生成式|智能体|算力|多模态",
    re.I)
_AI_ENT_RE = re.compile(
    "|".join(re.escape(e) for e in sorted(AI_ENTITIES, key=len, reverse=True)), re.I)
# 模型版本号 token(gpt-5 / claude 3 / gemini 2 / glm-5.1 ...)——增强实体签名
_MODEL_VER_RE = re.compile(
    r"\b(gpt|claude|gemini|llama|qwen|glm|mistral|grok|sora|o\d)[- ]?\d+(?:\.\d+)?\b", re.I)

# 教程/软文标题特征(中文长尾站常见;一线源的合法 how-to 不在此列,见下方白名单判断)
_TUTORIAL_RE = re.compile(
    r"完全指南|实战|手把手|保姆级|从入门到|入门教程|新手|详解|避坑|纯干货|教程|攻略|"
    r"一文(读懂|搞懂|看懂)|step[- ]by[- ]step", re.I)


def _hay(item: dict) -> str:
    return (str(item.get("title", "")) + " " + str(item.get("content", ""))).lower()


def _actions(hay: str) -> set:
    """命中的动作概念集合。"""
    out = set()
    for concept, words in ACTION_CONCEPTS.items():
        if any(w in hay for w in words):
            out.add(concept)
    return out


def _entities(hay: str) -> set:
    """命中的 AI 实体 + 模型版本号(品牌跨语言通常仍是拉丁字母,可跨中英匹配)。"""
    ents = set(m.group(0).lower() for m in _AI_ENT_RE.finditer(hay))
    ents |= set(re.sub(r"[- ]", "", m.group(0).lower()) for m in _MODEL_VER_RE.finditer(hay))
    return ents


def _has_ai_term(hay: str) -> bool:
    return bool(_AI_TERM_RE.search(hay) or _AI_ENT_RE.search(hay))


def is_ai_relevant(item: dict) -> bool:
    """AI 相关性闸:过滤混入的非 AI 新闻(如"China Inc layoffs")。
    RSS 源(带 category_hint)本身已是 AI 策展,直接放行,避免误杀官博。"""
    if (item.get("category_hint") or "").strip():
        return True
    return _has_ai_term(_hay(item))


def is_tutorial_softarticle(item: dict) -> bool:
    """教程/软文判定:标题命中特征 且 来源非一线。一线媒体的正经 how-to 不误杀。"""
    if not _TUTORIAL_RE.search(str(item.get("title", ""))):
        return False
    blob = ((item.get("media") or "") + " " + (item.get("link") or "")).lower()
    return not any(t in blob for t in TIER1)


def dedup_events(items: list, now: float = None) -> list:
    """事件级去重:把"同一事件被多源/多语言报道"的条目归并成一条(保最高分)。
    判定:两条共享 ≥1 个 AI 实体 且 共享 ≥1 个动作概念 → 同一事件。
    无动作概念的条目不参与归并(保守,避免把同公司不同事件误并)。纯函数。"""
    groups = []  # 每组:{"ents":set,"acts":set,"members":[item]}
    for it in items:
        hay = _hay(it)
        ents, acts = _entities(hay), _actions(hay)
        placed = False
        if ents and acts:
            for g in groups:
                if (ents & g["ents"]) and (acts & g["acts"]):
                    g["members"].append(it)
                    g["ents"] |= ents
                    g["acts"] |= acts
                    placed = True
                    break
        if not placed:
            groups.append({"ents": set(ents), "acts": set(acts), "members": [it]})
    out = []
    for g in groups:
        if len(g["members"]) == 1:
            out.append(g["members"][0])
            continue
        winner = max(g["members"], key=lambda x: score(x, now))
        best = dict(winner)
        best["_merged_from"] = [m.get("media", "") for m in g["members"] if m is not winner]
        out.append(best)
    return out


def classify(item: dict) -> str:
    """返回分类 code;规则全不命中返回 ""(交调用方兜底)。"""
    hay = _hay(item)
    scores = {c: 0 for c in CAT_CODES}

    # 1) 关键词计分
    for cat, words in KEYWORDS.items():
        for w in words:
            if w in hay:
                scores[cat] += 1

    # 2) RSS 的 category_hint:中等先验
    hint = (item.get("category_hint") or "").strip()
    if hint in scores:
        scores[hint] += 2

    # 3) 来源先验:弱
    mh = MEDIA_HINT.get((item.get("media") or "").strip())
    if mh in scores:
        scores[mh] += 1

    # 4) 动作概念:强信号(覆盖媒体先验/弱关键词)。收购/融资→biz,诉讼/监管→policy。
    #    治"OpenAI to acquire Ona"被媒体先验拉进 llm、"投诉/调查"误进 llm 等错分。
    for a in _actions(hay):
        cat = ACTION_TO_CAT.get(a)
        if cat in scores:
            scores[cat] += 4

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return ""   # 完全没信号
    return best


def classify_or_default(item: dict) -> str:
    c = classify(item)
    return c or (item.get("category_hint") if item.get("category_hint") in CAT_CODES else DEFAULT_CAT)


def _source_weight(item: dict) -> float:
    name = (item.get("media") or "").lower()
    link = (item.get("link") or "").lower()
    blob = name + " " + link
    if any(t in blob for t in LOWQ):
        return 0.0
    if any(t in blob for t in TIER1):
        return 1.0
    # arXiv / 官方域名一手
    if "arxiv" in blob or item.get("media", "") in MEDIA_HINT:
        return 0.8
    return 0.45   # 中文索引二三线站等


def _freshness(item: dict, now: float = None) -> float:
    ts = item.get("published_ts")
    if not ts:
        return 0.4   # 无日期:给个中性偏低分,不一棍打死
    now = now if now is not None else time.time()
    age_h = max(0.0, (now - ts) / 3600.0)
    return 0.5 ** (age_h / HALF_LIFE_HOURS)   # 指数衰减,半衰期 HALF_LIFE_HOURS


def score(item: dict, now: float = None) -> float:
    fresh = _freshness(item, now)
    src = _source_weight(item)
    pts = item.get("points") or 0
    pts_norm = min(1.0, pts / 200.0)          # 200 points 封顶为满分
    rel = item.get("relevance")
    rel_norm = float(rel) if isinstance(rel, (int, float)) else 0.0
    img = 1.0 if item.get("image") else 0.0
    return (W["freshness"] * fresh + W["source"] * src + W["points"] * pts_norm
            + W["relevance"] * rel_norm + W["image"] * img)


def rank_within_categories(items: list, per_cat_min: int = 10, per_cat_max: int = 15,
                           now: float = None) -> dict:
    """
    输入:已带 category 字段的素材列表。
    输出:{cat_code: [按分降序的 Top(≤per_cat_max) 条]}。
    质量优先不硬凑:某类候选不足 per_cat_min 也照实放,不塞二三线凑数(由 score 自然决定)。
    """
    buckets = {c: [] for c in CAT_CODES}
    for it in items:
        c = it.get("category") or classify_or_default(it)
        it["category"] = c
        it["_score"] = round(score(it, now), 4)
        buckets.setdefault(c, []).append(it)
    for c in buckets:
        buckets[c].sort(key=lambda x: x.get("_score", 0), reverse=True)
        buckets[c] = buckets[c][:per_cat_max]
    return buckets


if __name__ == "__main__":
    demo = [
        {"title": "OpenAI launches new GPT agent", "content": "autonomous agent tool use",
         "media": "OpenAI", "category_hint": "llm", "image": "x", "published_ts": time.time()},
        {"title": "Startup raises $100M funding round", "content": "venture investment",
         "media": "techcrunch.com", "category_hint": "biz", "published_ts": time.time() - 3600 * 50},
        {"title": "EU passes new AI regulation law", "content": "policy compliance court",
         "media": "reuters.com", "category_hint": ""},
    ]
    for d in demo:
        print(classify(d), round(score(d), 3), "<-", d["title"])
