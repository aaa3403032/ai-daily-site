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


def _hay(item: dict) -> str:
    return (str(item.get("title", "")) + " " + str(item.get("content", ""))).lower()


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
