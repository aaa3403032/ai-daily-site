#!/usr/bin/env python3
"""
AI 每日情报 — 云端生成脚本(阶段B,聚合模式)。

产品架构从「策展」升级到「聚合」:
  旧:搜几条 → GLM 选 5 条 → 写单列表 markdown。
  新:多源大量抓(RSS+Tavily+智谱)→ 去重 → 分类(7类)→ 类内排序取TopN
      → 分类分批喂 GLM 只写文字(sum/lead/take)→ 组装分类 JSON + 反渲染 markdown。

关键安全升级:GLM 只写文字,url/img/src/title 全程由程序从素材带过去、GLM 碰不到链接,
  → 编造链接从结构上不可能(比旧版 validate() 兜底强一个量级)。

输出:
  posts/{date}.json  —— 前端(网站版)用的分类结构(含 hero + 7 分类 + 每条字段)
  posts/{date}.md    —— 反渲染的 markdown(保 Coze/看板/旧前端不断)
  posts/index.json   —— 日期列表
  posts/all.md       —— 全量聚合(Coze 知识库源,勿删)

Key:环境变量 ZHIPU_API_KEY(或 ../anthropic_key.txt,文件名历史遗留);TAVILY_API_KEY 可选。
任何关键步骤失败以非零退出,避免提交残缺内容。
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import classify
import feeds

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = REPO_ROOT / "posts"
BASE = "https://open.bigmodel.cn/api/paas/v4"
MODEL = os.environ.get("DIGEST_MODEL", "glm-5.1")
TEMPERATURE = float(os.environ.get("DIGEST_TEMPERATURE", "0.3"))

# 智谱搜索引擎(search_pro 的 link 字段为空,已剔除)。
# ⑥ 降为补充:RSS 已是主力,智谱只补中文国产模型/政策。引擎 3→2(去 search_std 兜底引擎),
#   recency 2→1,query 6→3 → 总搜索 36→6 次,省搜索资源包 + 提速。
ENGINES = ["search_pro_sogou", "search_pro_quark"]
ZHIPU_RECENCY = os.environ.get("ZHIPU_RECENCY", "oneWeek")  # 单档(时效交给 RSS)
PER_CAT_MAX = int(os.environ.get("DIGEST_PER_CAT_MAX", "15"))   # 每类最多取几条喂 GLM(控成本主旋钮;doc 目标10-15)
MIN_TOTAL = int(os.environ.get("DIGEST_MIN_TOTAL", "8"))         # 候选总量下限,不足则不成稿
MIN_PUBLISH = int(os.environ.get("DIGEST_MIN_PUBLISH", "4"))     # 最终成品条数下限,不足则 fail

TAVILY_URL = "https://api.tavily.com/search"
INTL_QUERIES = [
    "OpenAI Anthropic Google DeepMind latest announcement",
    "AI agent product launch funding round",
    "new large language model release",
    "AI regulation policy lawsuit",
]
TIER1_DOMAINS = [
    "openai.com", "anthropic.com", "deepmind.google", "blog.google",
    "bloomberg.com", "reuters.com", "techcrunch.com", "theverge.com",
    "arstechnica.com", "venturebeat.com", "wired.com",
    "technologyreview.com", "theinformation.com", "semianalysis.com",
]


# ──────────────────────────── Key ────────────────────────────
def get_api_key() -> str:
    key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if key:
        return key
    key_file = REPO_ROOT.parent / "anthropic_key.txt"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    sys.exit("错误:未找到 API Key(环境变量 ZHIPU_API_KEY 或 ../anthropic_key.txt)")


# ──────────────────────────── 抓取 ────────────────────────────
def search_queries(today: str) -> list[str]:
    # 智谱已降为"中文补充":英文一线源由 RSS+Tavily 覆盖,这里只补它们抓不到的——
    # 国产大模型动态 + 当日综合 + 中文政策。3 条足够,别再跑 OpenAI/TechCrunch 等(RSS 已有)。
    return [
        "国产大模型 发布 动态 智谱 阿里 字节 DeepSeek 月之暗面",
        f"AI Agent 大模型 重要新闻 {today}",
        "AI 监管 政策 法案 版权 诉讼 网信办",
    ]


def web_search(key: str, query: str, recency: str, engine: str) -> list[dict]:
    try:
        r = requests.post(
            f"{BASE}/web_search",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "search_query": query[:70], "search_engine": engine,
                "search_intent": False, "count": 10,
                "search_recency_filter": recency, "content_size": "high",
            },
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"搜索[{engine}/{recency}] 请求异常:{e}", flush=True)
        return []
    items = r.json().get("search_result") or [] if r.status_code == 200 else []
    linked = [it for it in items if (it.get("link") or "").strip()]
    print(f"搜索[{engine}/{recency}] {query[:24]} → 状态{r.status_code} "
          f"条数{len(items)} 带链接{len(linked)}", flush=True)
    if not linked and r.status_code != 200:
        print(f"  响应体:{r.text[:160]}", flush=True)
    return linked


def tavily_search(key: str, query: str) -> list[dict]:
    """Tavily news 搜索;归一字段并保留 relevance(score)用于排序。"""
    try:
        r = requests.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"query": query[:380], "topic": "news", "search_depth": "basic",
                  "days": 7, "max_results": 8, "include_domains": TIER1_DOMAINS},
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"Tavily[{query[:24]}] 请求异常:{e}", flush=True)
        return []
    if r.status_code != 200:
        print(f"Tavily[{query[:24]}] 状态{r.status_code}:{r.text[:160]}", flush=True)
        return []
    out = []
    for it in r.json().get("results") or []:
        link = (it.get("url") or "").strip()
        if not link:
            continue
        host = re.sub(r"^https?://(www\.)?", "", link).split("/")[0]
        out.append({
            "title": it.get("title", ""), "link": link, "media": host,
            "publish_date": it.get("published_date", ""),
            "published_ts": _ts(it.get("published_date", "")),
            "content": it.get("content", ""),
            "image": "", "category_hint": "",
            "relevance": it.get("score"),
        })
    print(f"Tavily[{query[:24]}] → 条数{len(out)}", flush=True)
    return out


def _ts(s: str):
    from feeds import _parse_date
    return _parse_date(s)


def _media_of(item: dict) -> str:
    m = (item.get("media") or "").strip()
    if m:
        return m
    link = (item.get("link") or "").strip()
    return re.sub(r"^https?://(www\.)?", "", link).split("/")[0] or "未知"


def gather_candidates(key: str, today: str) -> list[dict]:
    """多源抓取 + 按 link 去重,返回结构化候选 list(不再拼 markdown)。"""
    pool, seen = [], set()

    def add(items, bucket):
        n = 0
        for it in items:
            link = (it.get("link") or "").strip()
            if link and link not in seen:
                seen.add(link)
                it.setdefault("media", _media_of(it))
                bucket.append(it)
                n += 1
        return n

    # 1) RSS(免费主力,官方直链+题图)——失败不阻塞
    rss = []
    try:
        add(feeds.fetch_feeds(), rss)
        print(f"RSS 入池 {len(rss)} 条", flush=True)
    except Exception as e:
        print(f"RSS 抓取失败,跳过(不阻塞):{e}", flush=True)

    # 2) Hacker News(免费热度源)——填 points 热度槽 + 一线英文,失败不阻塞
    hn = []
    try:
        add(feeds.fetch_hackernews(), hn)
        print(f"HN 入池 {len(hn)} 条", flush=True)
    except Exception as e:
        print(f"HN 抓取失败,跳过(不阻塞):{e}", flush=True)

    # 3) Tavily(西方一线源白名单)——有 Key 才跑
    tav = []
    tav_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tav_key:
        for q in INTL_QUERIES:
            add(tavily_search(tav_key, q), tav)
        print(f"Tavily 入池 {len(tav)} 条", flush=True)
    else:
        print("未配置 TAVILY_API_KEY,跳过西方源", flush=True)

    # 4) 智谱搜索(中文补充)——⑥ 已砍量:2 引擎 × 3 query × 1 recency = 6 次(原 36)
    zhi = []
    for engine in ENGINES:
        for q in search_queries(today):
            add(web_search(key, q, ZHIPU_RECENCY, engine), zhi)
    print(f"智谱入池 {len(zhi)} 条(搜索 {len(ENGINES) * len(search_queries(today))} 次)", flush=True)

    pool = rss + hn + tav + zhi   # 优先级:RSS > HN > Tavily > 智谱
    print(f"候选合计 {len(pool)} 条(RSS {len(rss)} / HN {len(hn)} / "
          f"Tavily {len(tav)} / 智谱 {len(zhi)})", flush=True)
    return pool


# ──────────────────────────── 去重(对历史) ────────────────────────────
def recent_titles(n_posts: int = 3) -> list[str]:
    titles = []
    for f in sorted(POSTS_DIR.glob("20??-??-??.md"))[-n_posts:]:
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\*\*\d+\.\s*(.+?)\*\*\s*$", line.strip())
            if m:
                titles.append(m.group(1))
    return titles


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]", "", (s or "").lower())


def drop_recent(cands: list[dict], recent: list[str]) -> list[dict]:
    """过滤掉与近几日标题高度重合的候选(避免重复报道同一事件)。"""
    rn = [_norm(t) for t in recent if t]
    out = []
    for c in cands:
        cn = _norm(c.get("title", ""))
        if cn and any(cn == r or (len(cn) > 8 and cn in r) or (len(r) > 8 and r in cn) for r in rn):
            continue
        out.append(c)
    if len(cands) != len(out):
        print(f"对历史去重:{len(cands)} → {len(out)} 条", flush=True)
    return out


# ──────────────────────────── 生成(分类分批) ────────────────────────────
def build_category_prompt(cat_label: str, items: list[dict], today: str) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(
            f"[{i}] 标题:{it.get('title','')}\n"
            f"    来源:{_media_of(it)} | 时间:{it.get('publish_date','')}\n"
            f"    内容:{(it.get('content') or '')[:500]}"
        )
    material = "\n".join(lines)
    return f"""你是"智读 · AI每日情报"的编辑,读者是中文产品经理。今天 {today}(北京时间)。
下面是「{cat_label}」分类下的候选素材(已按重要性排序):

{material}

任务:为其中**值得报道**的每条,写中文摘要。质量优先,软文/重复/价值低的可跳过(不必每条都写)。

每条输出这些字段:
- "n":素材编号(整数,对应上面的 [n])
- "sum":一句话摘要(≤40字,用于卡片)
- "lead":2-3 个段落的正文摘要(数组,每段 1-3 句;新名词用括号给白话解释)
- "take":一句"PM 视角解读",讲清这对做产品意味着什么

严格输出一个 JSON 数组,形如:
[{{"n":1,"sum":"...","lead":["第一段","第二段"],"take":"..."}}, ...]

只输出 JSON 数组本身,不要加解释、不要加 markdown 代码围栏、**绝对不要输出任何 URL 或链接**(链接由系统自动附加)。"""


def call_glm(key: str, prompt: str, max_tokens: int = 6000):
    try:
        r = requests.post(
            f"{BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens, "temperature": TEMPERATURE},
            timeout=300,
        )
    except requests.RequestException as e:
        print(f"  GLM 请求异常:{e}", flush=True)
        return None
    if r.status_code != 200:
        print(f"  GLM 调用失败({r.status_code}):{r.text[:300]}", flush=True)
        return None
    return r.json()


def parse_json_array(text: str):
    """从 GLM 回复里抽出 JSON 数组。容忍代码围栏/前后说明。"""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


def summarize_category(key: str, cat_code: str, cat_label: str,
                       items: list[dict], today: str) -> list[dict]:
    """对一个分类调一次 GLM,把 sum/lead/take 回填到对应素材;返回成功写好的条目。"""
    if not items:
        return []
    data = call_glm(key, build_category_prompt(cat_label, items, today))
    if not data:
        print(f"  [{cat_label}] GLM 无响应,跳过该类", flush=True)
        return []
    arr = parse_json_array(data["choices"][0]["message"]["content"] or "")
    if arr is None:
        print(f"  [{cat_label}] JSON 解析失败,跳过该类", flush=True)
        return []
    out = []
    for rec in arr:
        try:
            n = int(rec.get("n"))
        except (TypeError, ValueError):
            continue
        if not (1 <= n <= len(items)):
            continue
        it = items[n - 1]
        lead = rec.get("lead")
        if isinstance(lead, str):
            lead = [lead]
        if not isinstance(lead, list) or not lead:
            continue
        it = dict(it)  # 不污染原候选
        it["category"] = cat_code
        it["sum"] = str(rec.get("sum", "")).strip()
        it["lead"] = [str(p).strip() for p in lead if str(p).strip()]
        it["take"] = str(rec.get("take", "")).strip()
        if it["sum"] and it["lead"]:
            out.append(it)
    u = data.get("usage", {})
    print(f"  [{cat_label}] 候选{len(items)} → 写成{len(out)} 条 "
          f"(in {u.get('prompt_tokens')} / out {u.get('completion_tokens')} tok)", flush=True)
    return out


# ──────────────────────────── 组装输出 ────────────────────────────
def assemble(today: str, buckets: dict) -> tuple[dict, list[dict]]:
    """按分类顺序拼最终 items,选出全局最高分为 hero。返回 (json_obj, items)。"""
    items = []
    for code, _label, _color in classify.CATEGORIES:
        items.extend(buckets.get(code, []))
    if items:
        # 头条优先从*有题图*的高分条目里选——付费墙源(WSJ/NYT/Bloomberg)常无 og 图,
        # 让它当头条会渲染成空白大块(难看)。全部无图才退回纯按分排序。
        with_img = [x for x in items if x.get("image")]
        hero = max(with_img or items, key=lambda x: x.get("_score", 0))
        for it in items:
            it["hero"] = (it is hero)
    # 只保留出现的分类(前端按此渲染 tab)
    present = [c for c in classify.CAT_CODES if buckets.get(c)]
    cats_meta = [{"code": c, "label": classify.CAT_LABEL[c], "color": classify.CAT_COLOR[c]}
                 for c in present]
    json_items = [{
        "hero": bool(it.get("hero")),
        "category": it["category"],
        "src": _media_of(it),
        "title": it.get("title", ""),
        "url": it.get("link", ""),
        "img": it.get("image", "") or "",
        "sum": it.get("sum", ""),
        "lead": it.get("lead", []),
        "take": it.get("take", ""),
    } for it in items]
    obj = {
        "date": today,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "categories": cats_meta,
        "items": json_items,
    }
    return obj, items


def render_markdown(obj: dict) -> str:
    """从 JSON 反渲染 markdown(保 Coze all.md / 看板 / 旧前端不断)。"""
    today = obj["date"]
    by_cat = {}
    for it in obj["items"]:
        by_cat.setdefault(it["category"], []).append(it)
    lines = [f"# AI / Agent 领域新闻摘要 — {today}", ""]
    n = 0
    for code, label, _ in classify.CATEGORIES:
        group = by_cat.get(code)
        if not group:
            continue
        lines.append(f"## {label}")
        lines.append("")
        for it in group:
            n += 1
            lines.append(f"**{n}. {it['title']}**")
            lines.append(it.get("sum", ""))
            for p in it.get("lead", []):
                lines.append(p)
            if it.get("take"):
                lines.append(f"**PM 信号:** {it['take']}")
            if it.get("url"):
                lines.append(f"来源:[{it['src']}]({it['url']})")
            lines.append("")
    # 趋势段(保留旧结构锚点;简单聚合)
    lines.append("## 趋势一句话")
    lines.append("")
    lines.append(f"今日共 {n} 条,覆盖 {len([c for c in by_cat if by_cat[c]])} 个分类。")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(obj: dict, markdown: str, today: str):
    POSTS_DIR.mkdir(exist_ok=True)
    (POSTS_DIR / f"{today}.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    (POSTS_DIR / f"{today}.md").write_text(markdown, encoding="utf-8")
    dates = sorted(f.stem for f in POSTS_DIR.glob("20??-??-??.md"))
    (POSTS_DIR / "index.json").write_text(
        json.dumps(dates, ensure_ascii=False), encoding="utf-8")
    parts = [(POSTS_DIR / f"{d}.md").read_text(encoding="utf-8").strip() for d in dates]
    all_md = "# AI 每日情报站 — 全部摘要汇总\n\n" + "\n\n---\n\n".join(parts) + "\n"
    (POSTS_DIR / "all.md").write_text(all_md, encoding="utf-8")


# ──────────────────────────── 主流程 ────────────────────────────
def export_date_to_ci(today: str):
    """把内容日期写进 GITHUB_ENV,让 workflow 提交步骤复用同一个日期。
    根因:Python 在 main 开头算 today(北京),而生成耗时约 8 分钟;若运行跨午夜,
    提交步骤自己再 `date` 会比内容晚一天(Run#17:内容06-14 / 提交06-15)。
    单一真相源 = 这里导出,workflow 不再独立取 date。"""
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        try:
            with open(env_file, "a", encoding="utf-8") as f:
                f.write(f"DIGEST_DATE={today}\n")
            print(f"已导出 DIGEST_DATE={today} 给 workflow", flush=True)
        except OSError as e:
            print(f"导出 DIGEST_DATE 失败(不阻塞):{e}", flush=True)


def main():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    export_date_to_ci(today)
    key = get_api_key()
    now = time.time()

    cands = gather_candidates(key, today)

    # 过滤:① 非 AI 新闻(China Inc layoffs 类) ② 教程/软文(CSDN「完全指南」类)
    before = len(cands)
    cands = [c for c in cands
             if classify.is_ai_relevant(c) and not classify.is_tutorial_softarticle(c)]
    print(f"AI相关性+软文过滤:{before} → {len(cands)} 条", flush=True)

    if len(cands) < MIN_TOTAL:
        sys.exit(f"错误:候选只有 {len(cands)} 条(<{MIN_TOTAL}),不足以成稿")

    cands = drop_recent(cands, recent_titles())

    # 事件级去重:同一事件被多源/多语言报道只留最高分一条(治"同事件出现4次")
    before = len(cands)
    cands = classify.dedup_events(cands, now=now)
    print(f"事件级去重:{before} → {len(cands)} 条", flush=True)

    # 分类 + 类内排序取 TopN(控成本:只把 TopN 喂 GLM)
    for c in cands:
        c["category"] = classify.classify_or_default(c)
    buckets = classify.rank_within_categories(cands, per_cat_max=PER_CAT_MAX, now=now)
    dist = {classify.CAT_LABEL[c]: len(v) for c, v in buckets.items() if v}
    print(f"分类分布(排序后取TopN):{dist}", flush=True)

    # 分类分批生成(GLM 只写文字)
    written = {}
    for code, label, _ in classify.CATEGORIES:
        got = summarize_category(key, code, label, buckets.get(code, []), today)
        if got:
            written[code] = got
    total = sum(len(v) for v in written.values())
    if total < MIN_PUBLISH:
        sys.exit(f"错误:成品仅 {total} 条(<{MIN_PUBLISH}),不发布")

    obj, items = assemble(today, written)

    # 可选:对入选但缺图的条目补抓 og:image(配图是核心需求)
    if os.environ.get("FETCH_OGIMAGE") == "1":
        try:
            feeds.backfill_images(items, limit=int(os.environ.get("OGIMAGE_LIMIT", "20")))
            obj, items = assemble(today, written)  # 重组以带上补到的图
        except Exception as e:
            print(f"og:image 兜底失败(不阻塞):{e}", flush=True)

    markdown = render_markdown(obj)
    write_outputs(obj, markdown, today)

    withimg = sum(1 for it in obj["items"] if it["img"])
    print(f"完成:posts/{today}.json + .md 已写入;index.json/all.md 已更新")
    print(f"成品 {total} 条,分类 {len(obj['categories'])} 个,带题图 {withimg} 条,"
          f"hero={next((i['title'][:30] for i in obj['items'] if i['hero']), '无')}")


if __name__ == "__main__":
    main()
