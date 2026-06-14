#!/usr/bin/env python3
"""
RSS 抓取模块(阶段A 新增)——多源、前沿、配图。

为什么加 RSS:
  - 智谱搜索本质是中文索引,只出二三线站 + 浅链接(首页/栏目页)。
  - RSS 直接拿一线官博/媒体的文章级直链,免费、无额度、自带题图。
  - 题图(og:image / RSS 的 enclosure / media:content)随文一起抓,网站配图零成本。

输出:与 generate_digest.py 现有素材字段对齐的 dict 列表:
  {title, link, media(来源名), publish_date, content, image(题图URL或""), category_hint}

只依赖标准库 xml + requests(与主脚本一致,不新增依赖)。
fetch_feeds() 需要外网(在 GitHub Actions 上跑);parse_rss() 纯解析、可离线单测。
"""

import re
import xml.etree.ElementTree as ET
from html import unescape

import requests

# 命名空间(media:content / media:thumbnail 常见于带图的 RSS)
NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# 选源清单:(RSS 地址, 来源显示名, 分类提示)
# 分类提示只是粗标,最终分类由阶段B 的分类器决定;这里给一手倾向。
FEEDS = [
    # 一线官博(一手、最前沿)
    ("https://openai.com/news/rss.xml", "OpenAI", "llm"),
    ("https://www.anthropic.com/news/rss.xml", "Anthropic", "agent"),
    ("https://deepmind.google/blog/rss.xml", "Google DeepMind", "research"),
    ("https://huggingface.co/blog/feed.xml", "Hugging Face", "oss"),
    # 一线科技媒体(带题图,文章级直链)
    ("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch", "product"),
    ("https://venturebeat.com/category/ai/feed/", "VentureBeat", "biz"),
    ("https://arstechnica.com/ai/feed/", "Ars Technica", "research"),
    ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "The Verge", "product"),
    ("https://www.wired.com/feed/tag/ai/latest/rss", "WIRED", "product"),
    # 研究/论文
    ("https://rss.arxiv.org/rss/cs.AI", "arXiv cs.AI", "research"),
    ("https://rss.arxiv.org/rss/cs.CL", "arXiv cs.CL", "research"),
    # 中文(36氪有原生 feed;机器之心/量子位需 RSSHub,阶段B 再加)
    ("https://36kr.com/feed", "36氪", "biz"),
]


def _txt(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _first_img_in_html(html: str) -> str:
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html or "")
    return m.group(1) if m else ""


def _extract_image(item: ET.Element) -> str:
    """题图提取优先级:enclosure(image) → media:content → media:thumbnail → 正文首图。"""
    for enc in item.findall("enclosure"):
        url = enc.get("url", "")
        typ = enc.get("type", "")
        if url and (typ.startswith("image") or re.search(r"\.(jpg|jpeg|png|webp|gif)", url, re.I)):
            return url
    for tag in ("media:content", "media:thumbnail"):
        el = item.find(tag, NS)
        if el is not None and el.get("url"):
            return el.get("url")
    # 正文里的首图(content:encoded 或 description)
    body = _txt(item.find("content:encoded", NS)) or _txt(item.find("description"))
    return _first_img_in_html(body)


def _clean_text(html: str, limit: int = 600) -> str:
    """去标签、反转义,截断,作为给 GLM 的素材内容。"""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def parse_rss(xml_text: str, source: str, category_hint: str = "") -> list[dict]:
    """解析 RSS 2.0 / Atom,归一成素材 dict 列表。纯函数,可离线单测。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    # RSS 2.0: channel/item ; Atom: feed/entry
    items = root.findall(".//item")
    is_atom = False
    if not items:
        items = root.findall(".//atom:entry", NS)
        is_atom = True
    for it in items:
        if is_atom:
            title = _txt(it.find("atom:title", NS))
            link_el = it.find("atom:link[@rel='alternate']", NS) or it.find("atom:link", NS)
            link = (link_el.get("href") if link_el is not None else "").strip()
            date = _txt(it.find("atom:updated", NS)) or _txt(it.find("atom:published", NS))
            body = _txt(it.find("atom:content", NS)) or _txt(it.find("atom:summary", NS))
        else:
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
            "content": _clean_text(body),
            "image": _extract_image(it),
            "category_hint": category_hint,
        })
    return out


def fetch_feeds(timeout: int = 30, feeds: list = None) -> list[dict]:
    """抓取所有 RSS(在 Actions 上跑,需外网)。单个源失败不阻塞其余。"""
    feeds = feeds if feeds is not None else FEEDS
    pool = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AIDailyBot/1.0)"}
    for url, source, hint in feeds:
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code != 200:
                print(f"RSS[{source}] 状态 {r.status_code},跳过", flush=True)
                continue
            items = parse_rss(r.text, source, hint)
            withimg = sum(1 for x in items if x["image"])
            print(f"RSS[{source}] → {len(items)} 条,带题图 {withimg}", flush=True)
            pool.extend(items)
        except requests.RequestException as e:
            print(f"RSS[{source}] 请求异常:{e},跳过", flush=True)
    print(f"RSS 合计 {len(pool)} 条(来自 {len(feeds)} 个源)", flush=True)
    return pool


if __name__ == "__main__":
    # 手动跑:打印各源抓取情况(在有外网的环境)
    for it in fetch_feeds()[:5]:
        print(f"- [{it['media']}] {it['title'][:50]} | 图:{'有' if it['image'] else '无'}")
