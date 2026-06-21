#!/usr/bin/env python3
"""智读·键盘日报 生成器(第二频道)。
复用 AI 日报引擎(feeds 抓取 + GLM 摘要 + 配图),但完全独立:只 import 复用函数,
不修改任何 AI 管线文件。输出到 keyboard/posts/。GLM 绝不碰 URL/图(程序携带)。"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import feeds
from generate_digest import get_api_key, call_glm, parse_json_array, _media_of

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_POSTS = REPO_ROOT / "keyboard" / "posts"
TAVILY_URL = "https://api.tavily.com/search"

# 4 分类(对齐键盘趋势简报四角度)。颜色随 JSON 下发给前端。
CATEGORIES = [
    ("market",    "市场与品牌", "#2b7fd6"),
    ("hardware",  "新品与硬件", "#d6932b"),
    ("tech",      "技术趋势",   "#7a52cc"),
    ("community", "社区与玩家", "#0a8f6a"),
]
CAT_CODES = [c[0] for c in CATEGORIES]
CAT_LABEL = {c[0]: c[1] for c in CATEGORIES}
CAT_COLOR = {c[0]: c[2] for c in CATEGORIES}

PER_CAT_MAX = int(os.environ.get("KB_PER_CAT_MAX", "10"))
RECENT_DAYS = int(os.environ.get("KB_RECENT_DAYS", "10"))
MIN_PUBLISH = int(os.environ.get("KB_MIN_PUBLISH", "4"))

# 键盘 RSS 源(url, 来源名, hint)。hint 留空=分类交给 GLM。单源失败不阻塞(feeds.fetch_feeds 已处理)。
# 全部经子agent web_fetch 验证可出 XML(2026-06)。
KB_FEEDS = [
    # 论坛 / 社区(发烧友一手讨论)
    ("https://www.reddit.com/r/MechanicalKeyboards/top/.rss?t=week", "r/MechanicalKeyboards", "community"),
    ("https://keebtalk.com/latest.rss", "KeebTalk", "community"),
    ("https://geekhack.org/index.php?action=.xml;type=rss2", "Geekhack", "community"),
    ("https://deskthority.net/app.php/feed", "Deskthority", "community"),  # 新版 phpBB 用 /app.php/feed
    # 独立站 / 资讯 / 轴体评测
    ("https://kbd.news/rss.xml", "kbd.news", ""),
    ("https://www.theremingoat.com/blog?format=rss", "ThereminGoat", "hardware"),
    # YouTube 测评 KOL(键盘频道;Atom feed,主要拿标题+链接,GLM 据此判类/写摘要)
    ("https://www.youtube.com/feeds/videos.xml?channel_id=UCXlDgfWY2JbsYEam2m68Hyw", "Hipyo Tech", ""),
    ("https://www.youtube.com/feeds/videos.xml?channel_id=UCMHXMAeKkI6HXlPfLiYvo9g", "Taeha Types", ""),
    ("https://www.youtube.com/feeds/videos.xml?channel_id=UCRYOj4DmyxhBVrdvbsUwmAA", "Optimum Tech", ""),
    ("https://www.youtube.com/feeds/videos.xml?channel_id=UCeHOkFGW-7uAZFvq3BXb8YA", ":3ildcat", ""),
    ("https://www.youtube.com/feeds/videos.xml?channel_id=UCxOdCYPuEPY996im9u_tFBQ", "Alexotos", ""),
    ("https://www.youtube.com/feeds/videos.xml?channel_id=UCzqmTtRqjBgQ_cybekKVGHA", "Keybored", ""),
    # 泛科技(键盘相关由 GLM 过滤保留)
    ("https://www.theverge.com/rss/index.xml", "The Verge", ""),
]

# 键盘 Tavily 搜索词(补 RSS 抓不到的;无 AI 域名白名单)。含"新品/市场/评测/亚马逊近似"角度。
KB_QUERIES = [
    "mechanical keyboard news",
    "gaming keyboard launch hall effect magnetic",
    "keyboard switch keycap release 2026",
    "custom keyboard group buy community",
    "keyboard brand market trend best selling",
    "best mechanical keyboard review 2026",
]


def _ts(s):
    from feeds import _parse_date
    return _parse_date(s)


def tavily_kb(query):
    """键盘版 Tavily news 搜索(不限定 AI 域名)。无 key 或失败返回空,不阻塞。"""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return []
    try:
        r = requests.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"query": query[:380], "topic": "news", "search_depth": "basic",
                  "days": 14, "max_results": 10},
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"Tavily[{query[:20]}] 异常:{e}", flush=True)
        return []
    if r.status_code != 200:
        print(f"Tavily[{query[:20]}] 状态{r.status_code}:{r.text[:120]}", flush=True)
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
            "content": it.get("content", ""), "image": "",
            "category_hint": "", "relevance": it.get("score"),
        })
    print(f"Tavily[{query[:20]}] → {len(out)} 条", flush=True)
    return out


def gather(today):
    pool = []
    try:
        pool += feeds.fetch_feeds(feeds=KB_FEEDS, cap=20, days=RECENT_DAYS)
    except Exception as e:
        print(f"RSS 抓取异常(不阻塞):{e}", flush=True)
    for q in KB_QUERIES:
        pool += tavily_kb(q)
    seen, uniq = set(), []
    for it in pool:
        link = (it.get("link") or "").strip()
        if not link or link in seen:
            continue
        seen.add(link)
        uniq.append(it)
    print(f"键盘候选合计 {len(uniq)} 条(去重后)", flush=True)
    return uniq


def build_prompt(items, today):
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"[{i}] 标题:{it.get('title','')}\n"
                     f"    来源:{_media_of(it)} | 内容:{(it.get('content') or '')[:300]}")
    material = "\n".join(lines)
    cats = "、".join(f"{c}({l})" for c, l, _ in CATEGORIES)
    return f"""你是「智读 · 键盘日报」的编辑,读者是键盘行业从业者。今天 {today}(北京时间)。
下面是候选素材(机械键盘 / 外设 / 键盘行业领域)。逐条判断,再为合格的写中文摘要。

剔除(keep=false)的:非键盘行业、**乐器键盘 / MIDI / 电子琴 / 合成器(本日报只关注电脑/机械键盘及外设,乐器键盘不算)**、纯带货/优惠广告软文、与键盘无实质关系、与本批其它条目同一事件的重复。
合格条目的分类 category 必须从这几个里选其一:{cats}。

每条素材都要输出一条记录:
- "n":素材编号
- "keep":true 或 false
- 当 keep=false:给 "reason"(一句),其余可省
- 当 keep=true:给 "category"(上面的 code)、"sum"(一句话摘要≤40字)、"lead"(2-3 段正文摘要,数组)、"take"(给键盘从业者的一句洞察:这对产品/市场意味着什么)

严格只输出一个 JSON 数组,形如:
[{{"n":1,"keep":true,"category":"tech","sum":"...","lead":["..."],"take":"..."}}]
只输出 JSON 数组本身,不要解释、不要 markdown 围栏、**绝不输出任何 URL 或链接**(链接由系统自动附加)。"""


def summarize(key, items, today):
    if not items:
        return []
    out, dropped = [], 0
    BATCH = 30
    for s in range(0, len(items), BATCH):
        chunk = items[s:s + BATCH]
        data = call_glm(key, build_prompt(chunk, today), max_tokens=8000)
        if not data:
            continue
        arr = parse_json_array(data["choices"][0]["message"]["content"] or "")
        if not arr:
            continue
        for rec in arr:
            if not isinstance(rec, dict):
                continue
            try:
                n = int(rec.get("n"))
            except (TypeError, ValueError):
                continue
            if not (1 <= n <= len(chunk)):
                continue
            if rec.get("keep") is False:
                dropped += 1
                continue
            cat = rec.get("category")
            if cat not in CAT_CODES:
                cat = "hardware"
            lead = rec.get("lead")
            if isinstance(lead, str):
                lead = [lead]
            if not isinstance(lead, list) or not lead:
                continue
            it = dict(chunk[n - 1])
            it["category"] = cat
            it["sum"] = str(rec.get("sum", "")).strip()
            it["lead"] = [str(p).strip() for p in lead if str(p).strip()]
            it["take"] = str(rec.get("take", "")).strip()
            if it["sum"] and it["lead"]:
                out.append(it)
    print(f"GLM 写成 {len(out)} 条(剔除 {dropped})", flush=True)
    return out


def rank_buckets(items):
    now = time.time()

    def score(it):
        ts = it.get("published_ts") or 0
        fresh = 1.0 if not ts else max(0.1, 1.0 - (now - ts) / (86400 * RECENT_DAYS))
        return fresh + (0.5 if it.get("image") else 0) + (it.get("relevance") or 0)

    buckets = {}
    for it in items:
        buckets.setdefault(it["category"], []).append(it)
    for c in buckets:
        buckets[c].sort(key=score, reverse=True)
        buckets[c] = buckets[c][:PER_CAT_MAX]
    return buckets


def assemble(today, buckets):
    items = []
    for code, _, _ in CATEGORIES:
        items.extend(buckets.get(code, []))
    if items:
        with_img = [x for x in items if x.get("image")]
        hero = (with_img or items)[0]
        for it in items:
            it["hero"] = (it is hero)
    present = [c for c in CAT_CODES if buckets.get(c)]
    cats_meta = [{"code": c, "label": CAT_LABEL[c], "color": CAT_COLOR[c]} for c in present]
    json_items = [{
        "hero": bool(it.get("hero")), "category": it["category"], "src": _media_of(it),
        "title": it.get("title", ""), "url": it.get("link", ""),
        "img": it.get("image", "") or "", "sum": it.get("sum", ""),
        "lead": it.get("lead", []), "take": it.get("take", ""),
    } for it in items]
    return {"date": today, "categories": cats_meta, "items": json_items}, items


def render_md(obj):
    lines = [f"# 智读 · 键盘日报 {obj['date']}", ""]
    for c in obj["categories"]:
        lines += [f"## {c['label']}", ""]
        for it in obj["items"]:
            if it["category"] != c["code"]:
                continue
            lines.append(f"### {it['title']}")
            lines.append(it["sum"])
            lines += it.get("lead", [])
            if it.get("take"):
                lines.append(f"**从业者信号:** {it['take']}")
            if it.get("url"):
                lines.append(f"来源:[{it['src']}]({it['url']})")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(obj, md, today):
    KB_POSTS.mkdir(parents=True, exist_ok=True)
    (KB_POSTS / f"{today}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    (KB_POSTS / f"{today}.md").write_text(md, encoding="utf-8")
    dates = sorted(f.stem for f in KB_POSTS.glob("20??-??-??.json"))
    (KB_POSTS / "index.json").write_text(json.dumps(dates, ensure_ascii=False), encoding="utf-8")


def export_date(today):
    env = os.environ.get("GITHUB_ENV")
    if env:
        try:
            with open(env, "a", encoding="utf-8") as f:
                f.write(f"KB_DIGEST_DATE={today}\n")
        except OSError:
            pass


def load_manual(today):
    """keyboard/posts/manual.json 人工置顶/补充(当天)。坏文件不阻塞。"""
    path = KB_POSTS / "manual.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if not isinstance(it, dict):
            continue
        d = it.get("date")
        if d and d != today:
            continue
        cat = it.get("category", "hardware")
        if cat not in CAT_CODES:
            cat = "hardware"
        title = str(it.get("title", "")).strip()
        sm = str(it.get("sum", "")).strip()
        lead = it.get("lead") or []
        if isinstance(lead, str):
            lead = [lead]
        lead = [str(p).strip() for p in lead if str(p).strip()]
        if not (title and sm and lead):
            continue
        out.append({
            "category": cat, "media": (str(it.get("src", "")).strip() or "编辑精选"),
            "title": title, "link": str(it.get("url", "")).strip(),
            "image": str(it.get("img", "")).strip(), "sum": sm, "lead": lead,
            "take": str(it.get("take", "")).strip(),
        })
    return out


def main():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    export_date(today)
    key = get_api_key()
    cands = gather(today)
    if len(cands) < MIN_PUBLISH:
        sys.exit(f"键盘候选仅 {len(cands)} 条(<{MIN_PUBLISH}),不足成稿")
    written = summarize(key, cands, today)
    buckets = rank_buckets(written)
    for it in load_manual(today):
        buckets.setdefault(it["category"], []).append(it)
    total = sum(len(v) for v in buckets.values())
    if total < MIN_PUBLISH:
        sys.exit(f"键盘成品仅 {total} 条(<{MIN_PUBLISH}),不发布")
    obj, items = assemble(today, buckets)
    if os.environ.get("FETCH_OGIMAGE") == "1":
        try:
            feeds.backfill_images(items, limit=int(os.environ.get("OGIMAGE_LIMIT", "30")))
            obj, items = assemble(today, buckets)
        except Exception as e:
            print(f"og 兜底失败(不阻塞):{e}", flush=True)
    write_outputs(obj, render_md(obj), today)
    withimg = sum(1 for it in obj["items"] if it["img"])
    print(f"键盘日报完成:{total} 条 / {len(obj['categories'])} 类 / 带题图 {withimg}", flush=True)


if __name__ == "__main__":
    main()
