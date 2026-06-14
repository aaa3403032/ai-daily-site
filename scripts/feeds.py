#!/usr/bin/env python3
"""
RSS 抓取模块(阶段A/B)——多源、前沿、配图。

为什么加 RSS:
  - 智谱搜索本质是中文索引,只出二三线站 + 浅链接(首页/栏目页)。
  - RSS 直接拿一线官博/媒体的文章级直链,免费、无额度、自带题图。
  - 题图(enclosure / media:content / 正文首图 / 可选 og:image)随文一起抓,网站配图零成本。

输出:与 generate_digest.py 现有素材字段对齐的 dict 列表:
  {title, link, media(来源名), publish_date, published_ts(epoch或None),
   content, image(题图URL或""), category_hint}

阶段B 地基修复(2026-06-14):
  ① 兼容 RSS 1.0 / RDF(arXiv 旧 export 用此格式,默认命名空间下 <item> 之前匹配不到 → 0 条)。
  ② per-feed 条数上限 + 按发布时间过滤(近 N 天),避免 OpenAI/HF 那种全量档案(1000+ 条)灌进来。
  ③ 改进题图提取;可选 og:image 兜底抓取(FETCH_OGIMAGE=1 时,对入选但缺图的条目补抓)。

只依赖标准库 xml/email/datetime + requests(与主脚本一致,不新增依赖)。
parse_rss() 纯解析、可离线单测;fetch_feeds()/fetch_og_image() 需外网(在 GitHub Actions 上跑)。
"""

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape

import requests

# 命名空间
NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "rss1": "http://purl.org/rss/1.0/",          # RSS 1.0 / RDF 的 item/title/link
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}

# 默认:每源最多取几条、只要近几天的
PER_FEED_CAP = int(os.environ.get("RSS_PER_FEED_CAP", "12"))
RECENT_DAYS = int(os.environ.get("RSS_RECENT_DAYS", "7"))   # 4→7:候选池更大(用户要"数据多")

# ── Hacker News(免费热度源)──
# 用 Algolia 公开 API(无需 Key),抓 AI 相关高分 story,带 points → 填上 classify.score 的热度槽。
# 这是"最热门"真正能排序的关键:RSS/Tavily 给的是"最新/一线",HN 给的是"社区最热"。
HN_API = "https://hn.algolia.com/api/v1/search"
HN_QUERIES = ["AI", "LLM", "AI agent", "OpenAI", "Anthropic", "open source model"]
HN_MIN_POINTS = int(os.environ.get("HN_MIN_POINTS", "30"))   # 热度门槛,低于此不要
HN_PER_QUERY = int(os.environ.get("HN_PER_QUERY", "20"))

# 选源清单:(RSS 地址, 来源显示名, 分类提示)
# 分类提示只是粗标,最终分类由 classify.py 决定;这里给一手倾向。
# 分类码与 classify.CATEGORIES 对齐:llm/agent/product/oss/research/biz/policy
FEEDS = [
    # 一线官博(一手、最前沿)
    ("https://openai.com/news/rss.xml", "OpenAI", "llm"),
    # Anthropic 无官方 RSS(/news/rss.xml 404)。走 RSSHub;失败不阻塞,Anthropic 仍由 Tavily 白名单兜底。
    ("https://rsshub.app/anthropic/news", "Anthropic", "agent"),
    ("https://deepmind.google/blog/rss.xml", "Google DeepMind", "research"),
    ("https://huggingface.co/blog/feed.xml", "Hugging Face", "oss"),
    # 一线科技媒体(带题图,文章级直链)
    ("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch", "product"),
    ("https://venturebeat.com/category/ai/feed/", "VentureBeat", "biz"),
    # Ars 的 /ai/ feed 混入大量泛科技(SpaceX/无人机/Pokémon),且非论文。
    # 去 hint:① 不再灌满 research ② 无 hint → 过 AI 相关性闸,泛科技被剔。
    ("https://arstechnica.com/ai/feed/", "Ars Technica", ""),
    ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "The Verge", "product"),
    ("https://www.wired.com/feed/tag/ai/latest/rss", "WIRED", "product"),
    # 研究/论文(arXiv 用 export 端点,RSS 1.0/RDF 格式;解析已兼容)
    ("https://export.arxiv.org/rss/cs.AI", "arXiv cs.AI", "research"),
    ("https://export.arxiv.org/rss/cs.CL", "arXiv cs.CL", "research"),
    # 更多一线媒体(用户要"数据多";直链优先,RSSHub 兜底,失败不阻塞)
    ("https://www.technologyreview.com/feed/", "MIT Tech Review", ""),
    ("https://www.theregister.com/software/ai_ml/headlines.atom", "The Register", ""),
    # 中文(36氪原生 feed;机器之心走 RSSHub)
    ("https://36kr.com/feed", "36氪", "biz"),
    ("https://rsshub.app/jiqizhixin/main", "机器之心", ""),
]


def _txt(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _findtext_multi(item, names) -> str:
    """按一组 (tag, ns_or_None) 依次找,返回第一个非空文本。"""
    for tag, ns in names:
        el = item.find(tag, NS) if ns else item.find(tag)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
    return ""


def _first_img_in_html(html: str) -> str:
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html or "")
    return m.group(1) if m else ""


def _looks_like_image(url: str) -> bool:
    return bool(url) and (
        re.search(r"\.(jpg|jpeg|png|webp|gif|avif)(\?|$)", url, re.I) is not None
        or "image" in url.lower()
    )


def _extract_image(item: ET.Element) -> str:
    """题图提取:enclosure → media:content → media:thumbnail → media:group 内 → 正文首图。"""
    for enc in item.findall("enclosure"):
        url = enc.get("url", "")
        typ = enc.get("type", "")
        if url and (typ.startswith("image") or _looks_like_image(url)):
            return url
    for tag in ("media:content", "media:thumbnail"):
        for el in item.findall(tag, NS):
            u = el.get("url")
            if u:
                return u
    grp = item.find("media:group", NS)
    if grp is not None:
        for tag in ("media:content", "media:thumbnail"):
            el = grp.find(tag, NS)
            if el is not None and el.get("url"):
                return el.get("url")
    # itunes / image 元素
    img_el = item.find("image")
    if img_el is not None:
        u = _txt(img_el.find("url")) or img_el.get("href") or ""
        if u:
            return u
    # 正文里的首图(content:encoded 或 description / atom:content）
    body = (_txt(item.find("content:encoded", NS))
            or _txt(item.find("description"))
            or _txt(item.find("atom:content", NS)))
    return _first_img_in_html(body)


def _clean_text(html: str, limit: int = 600) -> str:
    """去标签、反转义,截断,作为给 GLM 的素材内容。"""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def _parse_date(s: str):
    """RSS(RFC822)/Atom(ISO8601)/RSS1.0(dc:date ISO) → epoch 秒;解析失败返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)  # 吃 RFC822
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def parse_rss(xml_text: str, source: str, category_hint: str = "") -> list[dict]:
    """解析 RSS 2.0 / Atom / RSS 1.0(RDF),归一成素材 dict 列表。纯函数,可离线单测。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    # 定位 item/entry:依次尝试 RSS2.0(无命名空间)、RSS1.0(rss1:item)、Atom(atom:entry)
    kind = "rss2"
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//rss1:item", NS)
        if items:
            kind = "rss1"
    if not items:
        items = root.findall(".//atom:entry", NS)
        if items:
            kind = "atom"
    if not items:
        return []

    out = []
    for it in items:
        if kind == "atom":
            title = _txt(it.find("atom:title", NS))
            link_el = (it.find("atom:link[@rel='alternate']", NS)
                       or it.find("atom:link", NS))
            link = (link_el.get("href") if link_el is not None else "").strip()
            date = _txt(it.find("atom:published", NS)) or _txt(it.find("atom:updated", NS))
            body = _txt(it.find("atom:content", NS)) or _txt(it.find("atom:summary", NS))
        elif kind == "rss1":
            title = _txt(it.find("rss1:title", NS)) or _txt(it.find("title"))
            link = _txt(it.find("rss1:link", NS)) or _txt(it.find("link"))
            date = _txt(it.find("dc:date", NS))
            body = (_txt(it.find("content:encoded", NS))
                    or _txt(it.find("rss1:description", NS))
                    or _txt(it.find("description")))
        else:  # rss2
            title = _txt(it.find("title"))
            link = _txt(it.find("link"))
            date = _txt(it.find("pubDate")) or _txt(it.find("dc:date", NS))
            body = _txt(it.find("content:encoded", NS)) or _txt(it.find("description"))

        title = unescape(title)
        if not title or not link:
            continue
        out.append({
            "title": title,
            "link": link,
            "media": source,
            "publish_date": date,
            "published_ts": _parse_date(date),
            "content": _clean_text(body),
            "image": _extract_image(it),
            "category_hint": category_hint,
        })
    return out


def _recent_and_capped(items: list[dict], cap: int, days: int) -> list[dict]:
    """按时间过滤(近 days 天;无日期的保留)+ 按时间倒序 + 每源截断到 cap 条。"""
    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        kept = [x for x in items if x["published_ts"] is None or x["published_ts"] >= cutoff]
    else:
        kept = list(items)
    # 有日期的排前面(新→旧),无日期的排后面
    kept.sort(key=lambda x: x["published_ts"] or 0, reverse=True)
    return kept[:cap]


def fetch_feeds(timeout: int = 30, feeds: list = None,
                cap: int = None, days: int = None) -> list[dict]:
    """抓取所有 RSS(在 Actions 上跑,需外网)。单个源失败不阻塞其余。"""
    feeds = feeds if feeds is not None else FEEDS
    cap = PER_FEED_CAP if cap is None else cap
    days = RECENT_DAYS if days is None else days
    pool = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AIDailyBot/1.0)"}
    for url, source, hint in feeds:
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code != 200:
                print(f"RSS[{source}] 状态 {r.status_code},跳过", flush=True)
                continue
            raw = parse_rss(r.text, source, hint)
            items = _recent_and_capped(raw, cap, days)
            withimg = sum(1 for x in items if x["image"])
            print(f"RSS[{source}] 原始 {len(raw)} → 近{days}天截{cap} {len(items)} 条,带题图 {withimg}",
                  flush=True)
            pool.extend(items)
        except requests.RequestException as e:
            print(f"RSS[{source}] 请求异常:{e},跳过", flush=True)
    print(f"RSS 合计 {len(pool)} 条(来自 {len(feeds)} 个源)", flush=True)
    return pool


def _hn_item(hit: dict):
    """单条 HN hit → 候选 dict(纯函数,可离线单测)。无外链(Ask HN 等)返回 None。"""
    url = (hit.get("url") or "").strip()
    if not url:
        return None
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    cts = hit.get("created_at_i")
    return {
        "title": unescape(hit.get("title") or ""),
        "link": url,
        "media": host or "Hacker News",
        "publish_date": hit.get("created_at", ""),
        "published_ts": float(cts) if cts else None,
        "content": _clean_text(hit.get("story_text") or ""),
        "image": "",
        "category_hint": "",
        "points": int(hit.get("points") or 0),   # 热度信号 → classify.score 的 points 槽
    }


def fetch_hackernews(queries: list = None, min_points: int = None, days: int = None,
                     per_query: int = None, timeout: int = 20) -> list[dict]:
    """抓 HN 上 AI 相关高分 story(Algolia 公开 API,无需 Key)。按外链去重。失败不阻塞。"""
    queries = queries if queries is not None else HN_QUERIES
    min_points = HN_MIN_POINTS if min_points is None else min_points
    days = RECENT_DAYS if days is None else days
    per_query = HN_PER_QUERY if per_query is None else per_query
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    pool, seen = [], set()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AIDailyBot/1.0)"}
    for q in queries:
        try:
            r = requests.get(HN_API, headers=headers, timeout=timeout, params={
                "tags": "story", "query": q,
                "numericFilters": f"points>{min_points},created_at_i>{cutoff}",
                "hitsPerPage": per_query,
            })
            if r.status_code != 200:
                print(f"HN[{q}] 状态 {r.status_code},跳过", flush=True)
                continue
            hits = r.json().get("hits") or []
        except (requests.RequestException, ValueError) as e:
            print(f"HN[{q}] 请求异常:{e},跳过", flush=True)
            continue
        n = 0
        for h in hits:
            it = _hn_item(h)
            if it and it["link"] not in seen:
                seen.add(it["link"])
                pool.append(it)
                n += 1
        print(f"HN[{q}] 命中 {len(hits)} → 入池 {n}", flush=True)
    pool.sort(key=lambda x: x.get("points", 0), reverse=True)
    print(f"HN 合计 {len(pool)} 条(去重后,按 points 排序)", flush=True)
    return pool


def fetch_og_image(url: str, timeout: int = 8) -> str:
    """兜底:抓文章页 og:image / twitter:image。best-effort,失败返回空串。"""
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AIDailyBot/1.0)"},
                         timeout=timeout)
        if r.status_code != 200:
            return ""
        html = r.text[:200000]  # 只看头部,og 标签都在 <head>
    except requests.RequestException:
        return ""
    for pat in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return ""


def backfill_images(items: list[dict], limit: int = 30, timeout: int = 8) -> int:
    """对缺图的条目补抓 og:image(FETCH_OGIMAGE=1 时由调用方启用)。返回补到的张数。"""
    n = 0
    for it in items:
        if n >= limit:
            break
        if it.get("image"):
            continue
        url = (it.get("link") or "").strip()
        if not url.startswith("http"):
            continue
        og = fetch_og_image(url, timeout=timeout)
        if og:
            it["image"] = og
            n += 1
    print(f"og:image 兜底补图 {n} 张", flush=True)
    return n


if __name__ == "__main__":
    # 手动跑:打印各源抓取情况(在有外网的环境)
    pool = fetch_feeds()
    if os.environ.get("FETCH_OGIMAGE") == "1":
        backfill_images(pool)
    for it in pool[:8]:
        print(f"- [{it['media']}] {it['title'][:50]} | 图:{'有' if it['image'] else '无'} "
              f"| {it['publish_date'][:25]}")
